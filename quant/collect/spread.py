"""호가창 스프레드 **실측** 수집 — 스캘핑 비용 가정을 숫자로 바꾼다.

## 왜 있나

`config/settings.yaml`의 `execution.slippage_bps: 2.5`는 주석이 스스로 "실측
데이터로 검증된 값이 아니다"라고 적어 두었고, 저장소가 반복해 인용하는 "왕복
20bp"도 같은 성격의 추정치다. 스캘핑은 **비용이 엣지보다 크면 알파 탐색 자체가
무의미**하므로, 전략을 늘리기 전에 이 숫자부터 실측으로 갈아야 한다.

우리는 이미 호가를 받을 수 있었는데 쓰지 않고 있었다 —
`quant.adapters.brokers.toss.client.TossClient.orderbook()`은 구현돼 있었지만
저장소 전체에 호출부가 0건이었다(2026-08-28 감사). 이 모듈이 그 첫 호출부다.

## 이 모듈은 측정만 한다

거래 평면(`quant/trade/`)을 건드리지 않는다. 여기서 나온 숫자는 원장
(`data/ledger/spread.jsonl`)에 쌓일 뿐이고, 그것으로 `slippage_bps`를 고칠지는
사람이 판단한다. 수집 평면이라 네트워크는 허용되지만, 순수 계산부
(`spread_row`)를 분리해 두어 검증은 네트워크 없이 끝난다.

## 정직성

- **US 심볼에서 Toss 호가가 실데이터를 주는지는 아직 모른다.** 빈 응답이면
  `SpreadSample.empty`에 심볼을 남긴다 — 요약에서 "응답 없음"으로 드러나야 하고,
  0bp 같은 그럴듯한 숫자로 위장하면 안 된다.
- 호가 **단계 수**는 스펙에 고정값이 없다(openapi.json 예시는 KR 3단/US 2단).
  그래서 최우선 1단만 쓴다. 게다가 그 예시의 `asks`는 **내림차순**이라
  "첫 원소 = 최우선"이 성립하지 않는다 — 정렬에 기대지 않고 가격으로 직접
  고른다(최우선 매도 = 최저가 ask, 최우선 매수 = 최고가 bid).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, NamedTuple

logger = logging.getLogger(__name__)

# Toss MARKET_DATA 그룹의 상한은 10 TPS 인데, 그 버킷을 **엔진이 이미 쓰고 있다**
# (`prices()` 시세 폴링, 폴백 조회). 측정 잡이 자기 몫을 다 쓰면 장중 시세가
# 429 로 밀린다 — 돈이 걸린 쪽이 우선이다. 그래서 절반 이하인 5 TPS 를 스스로의
# 상한으로 두고, 호출 간 최소 간격 0.2초를 `sample_spread`가 강제한다.
# 호출자가 이보다 촘촘한 간격을 넘겨도 이 바닥값으로 잘린다.
MAX_CALLS_PER_SECOND = 5.0
MIN_CALL_INTERVAL = 1.0 / MAX_CALLS_PER_SECOND  # 0.2s


class SpreadSample(NamedTuple):
    """한 라운드의 결과. 버린 것을 조용히 삼키지 않으려고 4갈래로 나눠 돌려준다."""

    rows: list[dict]
    dropped: list[str]  # 호가는 왔는데 이상치(ask<=bid, 가격 파싱 불가)라 버린 심볼
    empty: list[str]  # 응답에 호가가 없던 심볼 — US 실데이터 여부가 여기 드러난다
    failed: list[tuple[str, str]]  # (심볼, 사유) — 예외로 조회 자체가 실패


def _levels(raw: object) -> list[tuple[float, float]]:
    """호가 단계 리스트 → [(price, volume)]. 파싱 불가한 단계는 건너뛴다.

    Toss 응답의 price/volume 은 **문자열**이다(openapi.json 예시: "72300").
    """
    out: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return out
    for level in raw:
        if not isinstance(level, dict):
            continue
        try:
            price = float(level["price"])
            volume = float(level.get("volume") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out.append((price, volume))
    return out


def spread_row(bids: object, asks: object, symbol: str, ts: str) -> dict | None:
    """호가 한 장 → 원장 한 줄. 이상치면 `None`(호출자가 버린 건수를 센다).

    순수 함수 — 네트워크도 시계도 만지지 않는다. 이상치 판정은 두 가지뿐이다:
    한쪽이 비었거나(`None`), 최우선 ask <= 최우선 bid(교차/동일 호가 — 정상적인
    연속 호가창에서는 나올 수 없고, 나왔다면 스냅샷이 깨진 것이다).
    """
    parsed_bids = _levels(bids)
    parsed_asks = _levels(asks)
    if not parsed_bids or not parsed_asks:
        return None

    bid, bid_size = max(parsed_bids, key=lambda lv: lv[0])
    ask, ask_size = min(parsed_asks, key=lambda lv: lv[0])
    if ask <= bid:
        return None

    mid = (ask + bid) / 2
    total_size = bid_size + ask_size
    return {
        "ts": ts,
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "spread_bp": (ask - bid) / mid * 10000,
        "bid_size": bid_size,
        "ask_size": ask_size,
        # 호가 불균형 — 스캘핑의 표준 지표(+1 매수 일방, -1 매도 일방).
        # 양쪽 잔량이 0으로 오는 책은 불균형을 정의할 수 없어 0.0(중립)으로 둔다:
        # 가격 자체는 유효하므로 스프레드 표본까지 버릴 이유는 없다.
        "imbalance": (bid_size - ask_size) / total_size if total_size > 0 else 0.0,
    }


def sample_spread(
    client,
    symbols: list[str],
    *,
    now: datetime,
    min_interval: float = MIN_CALL_INTERVAL,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SpreadSample:
    """심볼별 호가를 **1회씩** 조회해 원장 줄로 만든다.

    한 심볼의 실패가 나머지를 막지 않는다 — 예외는 심볼 단위로 삼키고 사유만
    `failed`에 남긴다. 호출 간격은 `min_interval`(바닥값 `MIN_CALL_INTERVAL`)로
    스스로 제한한다.

    `sleep`/`monotonic` 주입은 테스트용이다(가짜 시계로 슬립 삽입을 검증한다).
    """
    interval = max(float(min_interval), MIN_CALL_INTERVAL)
    ts = now.isoformat()

    rows: list[dict] = []
    dropped: list[str] = []
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    last_call: float | None = None

    for symbol in symbols:
        if last_call is not None:
            wait = interval - (monotonic() - last_call)
            if wait > 0:
                sleep(wait)
        last_call = monotonic()

        try:
            raw = client.orderbook(symbol)
        except Exception as e:  # noqa: BLE001 — 한 종목 실패가 측정 전체를 막지 않는다
            failed.append((symbol, f"{type(e).__name__}: {e}"))
            logger.warning("spread: %s 호가 조회 실패 — %s: %s", symbol, type(e).__name__, e)
            continue

        book = raw if isinstance(raw, dict) else {}
        if not book.get("bids") or not book.get("asks"):
            empty.append(symbol)
            continue

        row = spread_row(book["bids"], book["asks"], symbol, ts)
        if row is None:
            dropped.append(symbol)
            continue
        rows.append(row)

    return SpreadSample(rows=rows, dropped=dropped, empty=empty, failed=failed)
