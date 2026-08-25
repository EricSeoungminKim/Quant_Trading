"""TCA(Transaction Cost Analysis) — 주문 의도(신호 시점 가격) vs 실제 체결가 슬리피지.

소유자 로드맵 #5. 순수 함수만 둔다 — `order_intents.jsonl`/`trades.jsonl` 로딩은
호출부(cli) 몫이다.

**숫자를 해석하기 전에 읽을 두 가지 한계:**

1. paper 체결가는 브로커가 만드는 모델값이다(실거래소에 낸 값이 아니다) — 이
   모듈이 지금 검증하는 것은 "브로커 모델이 가정하는 슬리피지"이지 "실거래
   슬리피지"가 아니다. 진짜 쓸모는 실거래 전환 시 이 모델 가정과 실측 슬리피지를
   대조하는 데 있다.

2. intent 행의 가격은 2026-08-26부터 남는다: 리스크 레이어가 사이징에 쓴 결정
   시점 시세를 `Order.ref_price`로 싣고, `TossBroker._append_intent`가 이를
   `price` 키로 기록한다(같은 날 이 모듈과 함께 배선). **그 이전에 쌓인 intent
   행에는 가격이 없으므로** 이 모듈은 가격 필드(`price` 또는 `intent_price`)가
   있는 행만 처리한다 — 없는 가격을 지어내지 않는다. 과거 구간은 표본 0 →
   None이 정상이다. 또한 intent 는 TossBroker(실주문 경로)만 기록하므로 paper
   모드에서는 여전히 표본이 없다 — 이 도구의 표본은 실거래 전환과 함께 시작된다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# intent 행이 신호 시점 가격을 실었다면 쓸 법한 키 — 둘 중 먼저 찾은 것을 쓴다.
_PRICE_KEYS = ("price", "intent_price")


def _market_of(symbol: str) -> str:
    """6자리 숫자 = KR — 저장소 전역과 동일한 추론(ledger._market_of와 동일 규칙,
    이 모듈을 순수하게 유지하려고 별도로 둔다)."""
    return "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"


def _price(row: dict) -> float | None:
    for key in _PRICE_KEYS:
        v = row.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def _parse_ts(value) -> datetime | None:
    """naive는 UTC로 취급 — intent/체결 저널 둘 다 오프셋 포함으로 기록하는 게
    기본값이라(ledger.trades_in_session과 동일한 관례) 이건 방어적 폴백일 뿐."""
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _group_key(row: dict, side_field: str) -> tuple[str, str, str]:
    return (
        str(row.get("symbol")),
        str(row.get(side_field, "")).upper(),
        str(row.get("strategy_id") or ""),
    )


def join_intents_fills(
    intents: list[dict], trades: list[dict], window_seconds: float = 120,
) -> list[dict]:
    """가격이 있는 intent 행만, 같은 (symbol, side, strategy_id) 그룹 안에서
    체결과 시각 근접 매칭한다: intent_ts <= fill_ts <= intent_ts + window_seconds.

    1의도:1체결 그리디(의도 시각순, 그룹 내 가장 이른 미사용 체결) — 한 번 쓴
    체결은 다시 배정하지 않는다. 매칭 실패(가격 없음 포함)는 결과에서 버리고
    개수만 로그로 남긴다(반환값은 매칭된 행만 담은 list[dict]).
    """
    priced_intents = [
        it for it in intents
        if it.get("event", "intent") == "intent" and it.get("symbol") and _price(it) is not None
    ]
    n_intent_candidates = sum(
        1 for it in intents if it.get("event", "intent") == "intent" and it.get("symbol")
    )

    fills_by_group: dict[tuple[str, str, str], list[dict]] = {}
    for tr in trades:
        if not tr.get("symbol"):
            continue
        fills_by_group.setdefault(_group_key(tr, "side"), []).append(tr)
    for group in fills_by_group.values():
        group.sort(key=lambda r: _parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))

    intents_by_group: dict[tuple[str, str, str], list[dict]] = {}
    for it in priced_intents:
        intents_by_group.setdefault(_group_key(it, "side"), []).append(it)

    joined: list[dict] = []
    n_matched = 0
    for key, group_intents in intents_by_group.items():
        candidates = fills_by_group.get(key, [])
        used = [False] * len(candidates)
        ordered_intents = sorted(
            group_intents,
            key=lambda r: _parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        for it in ordered_intents:
            it_ts = _parse_ts(it.get("ts"))
            if it_ts is None:
                continue
            deadline = it_ts + timedelta(seconds=window_seconds)
            best_idx = None
            for idx, tr in enumerate(candidates):
                if used[idx]:
                    continue
                tr_ts = _parse_ts(tr.get("ts"))
                if tr_ts is None:
                    continue
                if it_ts <= tr_ts <= deadline:
                    best_idx = idx
                    break  # candidates는 ts 오름차순 정렬 — 첫 적합 = 가장 이른 체결
            if best_idx is None:
                continue
            used[best_idx] = True
            n_matched += 1
            tr = candidates[best_idx]
            symbol = str(it.get("symbol"))
            joined.append({
                "symbol": symbol,
                "side": key[1],
                "strategy_id": key[2] or None,
                "market": str(tr.get("market") or _market_of(symbol)),
                "intent_ts": it.get("ts"),
                "intent_price": _price(it),
                "intent_qty": it.get("quantity"),
                "fill_ts": tr.get("ts"),
                "fill_price": tr.get("price"),
                "fill_qty": tr.get("qty"),
            })

    logger.info(
        "TCA 매칭: intent_candidates=%d priced=%d matched=%d unmatched=%d",
        n_intent_candidates, len(priced_intents), n_matched,
        len(priced_intents) - n_matched,
    )
    return joined


def slippage_bps(joined: list[dict]) -> list[dict]:
    """매칭된 행마다 'bps'를 붙인다. 양수 = 불리하게 체결됐다는 뜻:

    - 매수: (fill_price - intent_price) / intent_price * 1e4 (더 비싸게 샀다)
    - 매도: (intent_price - fill_price) / intent_price * 1e4 (더 싸게 팔았다)

    가격을 못 구하거나 intent_price <= 0인 행(0 나눗셈/부호 무의미)은 버린다.
    """
    out: list[dict] = []
    for row in joined:
        intent_price = row.get("intent_price")
        fill_price = row.get("fill_price")
        if intent_price is None or fill_price is None:
            continue
        try:
            intent_price = float(intent_price)
            fill_price = float(fill_price)
        except (TypeError, ValueError):
            continue
        if intent_price <= 0:
            continue
        side = str(row.get("side", "")).upper()
        if side == "BUY":
            bps = (fill_price - intent_price) / intent_price * 1e4
        elif side == "SELL":
            bps = (intent_price - fill_price) / intent_price * 1e4
        else:
            continue
        out.append({**row, "bps": bps})
    return out


def _percentile(values: list[float], p: float) -> float:
    """선형보간 백분위수(numpy 기본 방식과 동일) — 순수 stdlib로 표본 소수에도
    안전하게 동작한다."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _bucket(rows: list[dict]) -> dict:
    bps = [float(r["bps"]) for r in rows]
    return {
        "n": len(bps),
        "avg_bps": round(sum(bps) / len(bps), 2),
        "p90_bps": round(_percentile(bps, 90), 2),
    }


def tca_summary(
    rows: list[dict], start_date: date | None = None, end_date: date | None = None,
) -> dict | None:
    """market별·strategy별 {n, avg_bps, p90_bps} + 전체. 표본이 0이면 None
    (자본배분 근거로 쓸 수 없는 걸 조용히 0으로 위장하지 않는다).

    날짜 필터는 체결(fill_ts) 기준 — weekly_review 등 호출부가 쓰는 주간
    범위와 같은 관례로 앞 10자(YYYY-MM-DD) 문자열 비교를 쓴다(ledger.py의
    weekly_strategy_stats/trades_in_session과 동일한 관례; 저널이 항상 UTC
    오프셋 포함으로 기록되므로 이 정밀도로 실무상 충분하다)."""
    filtered = rows
    if start_date is not None or end_date is not None:
        s_iso = start_date.isoformat() if start_date else "0000-00-00"
        e_iso = (end_date.isoformat() + "~") if end_date else "9999-99-99~"
        filtered = [r for r in rows if s_iso <= str(r.get("fill_ts") or "")[:10] <= e_iso]

    if not filtered:
        return None

    by_market: dict[str, list[dict]] = {}
    by_strategy: dict[str, list[dict]] = {}
    for r in filtered:
        by_market.setdefault(str(r.get("market") or "?"), []).append(r)
        by_strategy.setdefault(str(r.get("strategy_id") or "?"), []).append(r)

    return {
        "overall": _bucket(filtered),
        "by_market": {k: _bucket(v) for k, v in sorted(by_market.items())},
        "by_strategy": {k: _bucket(v) for k, v in sorted(by_strategy.items())},
    }
