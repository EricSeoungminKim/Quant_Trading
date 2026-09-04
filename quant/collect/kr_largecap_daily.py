"""KR 대형주(시총 ≥3,000억) 유니버스 결정 + 일봉 백필 — 스윙 시그널
(단기반전/거래량충격, `quant/analyze/swing_signals.py`) 전제 데이터.

## 왜 필요한가 (2026-09-03)

quant-backtest 워크포워드가 확인한 두 스윙 엣지는 유니버스가 "시총≥3,000억 +
20일 중앙값 거래대금≥50억"인 KR 대형주 전제다. 그런데 `data/history/*/1d`엔
관심종목(watchlist) 기반 ~154종목뿐이라 그 유니버스(~300종목)를 못 채운다 —
이 모듈이 그 갭을 메운다.

## 유니버스 결정과 일봉 백필을 분리한 이유

시가총액은 `sharesOutstanding × 마지막가`로 근사하는데(watch_scorer의 시총
게이트와 동일 알고리즘, 아래 `_market_cap_krw` 참고), `sharesOutstanding`은
Toss `stock_info`로 **심볼당 1회 호출**(STOCK 그룹, 5 TPS)해야 한다. KIND
상장법인목록 전체(~2,500종목)를 매일 다시 스캔하면 500초(8분+) 넘게 걸려
15:36 크론(manual_recs 15:50 전까지의 예산)을 통째로 집어삼킨다.

그래서 유니버스(시총 상위 top_n, 기본 300)는 로컬 캐시
(`data/state/kr_largecap_universe.json`)에 저장해두고 **기본 30일에 한 번만
재계산**한다(`--max-age-days`) — 나머지 날은 캐시를 그대로 읽고 그 심볼들의
일봉만 증분 백필한다(진짜 매일 필요한 건 이쪽뿐이다, `backfill()`의 gap-only
재조회라 훨씬 싸다). `--rebuild-universe`로 강제 재계산할 수 있다.

**첫 실행(캐시 없음)이나 재계산일은 크론 예산을 넘길 수 있다** — 그래도
안전하다: `backfill()`은 심볼 단위로 멱등/재개 가능하고 이 모듈은 심볼을
정렬된 순서로 순회하므로, 오늘 타임아웃으로 일부만 받아도 내일 실행이 이어서
받는다(처음부터 다시 받지 않는다).

## quant.collect → quant.analyze 임포트 금지 (평면 규칙)

KIND 상장법인목록 HTML 파싱은 `quant/analyze/entities.py`(`parse_corp_list`)에
이미 있고 시총 게이트 알고리즘은 `quant/analyze/watch_scorer.py`
(`_market_cap_krw`)에 이미 있지만, `quant.collect`는 `quant.analyze`를
임포트할 수 없다(`tests/test_architecture.py` FORBIDDEN, 루트 CLAUDE.md
"수집은 분석·설정을 몰라야 한다"). 둘 다 여기 최소 재구현했다 — 표 구조/필드가
갈리지 않는 한 구현이 어긋날 일이 적고(`tests/test_kr_largecap_daily.py`가
같은 입력으로 두 구현을 대조한다), KIND HTML 다운로드 자체는
`quant.collect.listed_companies.fetch_kind_corp_list`를 그대로 재사용해
캐시 파일(`data/cache/kind_corplist.html`)도 entities.py와 공유한다(한쪽이
이미 받았으면 재다운로드하지 않는다).

## quant.apps 임포트 금지

`.env.local` 로딩은 보통 `quant.apps.config.load_settings()`가 하지만
`quant.collect`는 `quant.apps`를 임포트할 수 없다 — `quant.adapters.env.get_key()`로
필요한 키 3개(TOSS_CLIENT_ID/SECRET/ACCOUNT_SEQ)만 직접 읽는다
(`quant.apps.assembly.build_toss_client`와 같은 자격증명 부재 가드, 다만
apps를 못 쓰니 그 함수를 재사용하지 못하고 여기 다시 최소 구현했다).

## 백필 경로

`data/history/{symbol}/1d/YYYY/MM.parquet` — `quant.collect.quotes.backfill.backfill()`
+ `TossCandleSource`를 그대로 재사용한다(신규 포맷 없음,
`server/scripts/backfill_kr_stock_daily.sh`와 동일 계약). 다만 그 스크립트처럼
심볼마다 별도 `cli fetch` 프로세스를 띄우지 않고 **한 프로세스 안에서 심볼을
순회**한다 — 프로세스 경계마다 TossClient 레이트리미터 상태가 리셋되는 낭비
(그 스크립트 자신의 지적)를 ~300종목 규모에서는 더는 감수할 이유가 없다.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from quant.adapters.env import REPO_ROOT, get_key
from quant.collect.listed_companies import fetch_kind_corp_list
from quant.collect.quotes.backfill import DEFAULT_HISTORY_DIR, BackfillReport, backfill
from quant.collect.quotes.toss_source import TossCandleSource

logger = logging.getLogger(__name__)

# KIND 상장법인목록 파싱 — entities.parse_corp_list와 같은 규칙(모듈 docstring
# "quant.collect → quant.analyze 임포트 금지" 참고).
TRADABLE = ("유가", "코스닥")
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"^\d{6}$")

DEFAULT_TOP_N = 300
DEFAULT_MIN_CAP = 3e11  # 3,000억원 — watch_scorer._MIN_MARKET_CAP_KRW와 동일 값
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_PRICE_BATCH = 50
DEFAULT_DEPTH_YEARS = 2

UNIVERSE_CACHE_PATH = REPO_ROOT / "data" / "state" / "kr_largecap_universe.json"
KIND_CACHE_DIR = REPO_ROOT / "data" / "cache"


# ========================================================================
# KIND 상장법인목록 → 후보 (코드, 이름)
# ========================================================================

def parse_kind_codes(raw: bytes) -> list[tuple[str, str, str]]:
    """(종목코드, 종목명, 시장구분) — `entities.parse_corp_list`와 같은 파싱
    규칙, 반환 튜플 순서만 다르다(이 모듈은 이름보다 코드를 더 자주 쓴다)."""
    text = raw.decode("euc-kr", "replace")
    out: list[tuple[str, str, str]] = []
    for tr in _TR.findall(text)[1:]:
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        if len(cells) >= 3 and _CODE.match(cells[2]) and cells[1] in TRADABLE:
            out.append((cells[2], cells[0], cells[1]))
    if not out:
        raise ValueError("상장법인목록 파싱 0건 — KIND 표 구조 변경 의심")
    return out


def _dart_candidate_codes(root: Path, *, api_key: str | None = None, getter=None) -> dict[str, str]:
    """DART 공시 법인목록(`data/cache/dart_corp_codes.json`) → {종목코드: 회사명}.

    `quant/collect/sources/dart_financials.py`가 이미 매일 이 캐시를 갱신하므로
    (재무제표 배치의 corp_code 매핑 전제) **새 네트워크 의존이 생기지 않는다** —
    같은 모듈(`quant.collect`)이라 평면 규칙 위반도 없다. 캐시가 없으면 그 모듈이
    직접 DART API를 1회 호출해 채운다(`get_corp_code_map`). 시장구분(유가/코스닥)은
    DART에 없어 못 채우지만 이 후보 목록은 시장구분을 쓰지 않는다."""
    from quant.collect.sources.dart_financials import get_corp_code_map

    corp_map, err = get_corp_code_map(root, api_key=api_key, getter=getter)
    if err:
        logger.warning("DART 공시 법인목록도 실패: %s", err)
        return {}
    return {code: info["corp_name"] for code, info in corp_map.items() if info.get("corp_name")}


def _local_symbol_union(root: Path) -> set[str]:
    """`data/history/{symbol}/1d` 디렉터리 + `data/ledger/frgn_flow.jsonl` 심볼의
    합집합 — KIND·DART 상장법인목록 소스가 둘 다 죽어도 유니버스가 완전히 비지
    않게 하는 마지막 방어선이다(2026-09-04, EC2에서 KIND 403 차단으로 유니버스가
    하루 통째로 빈 사고). "우리가 이미 아는 종목"일 뿐 상장법인목록이 아니라
    6자리 종목코드만 남긴다(US 티커 등 다른 시장 혼입 방지)."""
    out: set[str] = set()

    history_dir = root / "data" / "history"
    if history_dir.is_dir():
        for entry in history_dir.iterdir():
            if entry.is_dir() and _CODE.match(entry.name) and (entry / "1d").is_dir():
                out.add(entry.name)

    flow_path = root / "data" / "ledger" / "frgn_flow.jsonl"
    if flow_path.exists():
        for line in flow_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            symbol = row.get("symbol") if isinstance(row, dict) else None
            if symbol and _CODE.match(str(symbol)):
                out.add(str(symbol))

    return out


def candidate_codes(
    cache_dir: Path = KIND_CACHE_DIR, *, root: Path = REPO_ROOT,
    dart_api_key: str | None = None, dart_getter=None,
) -> list[tuple[str, str]]:
    """전체 상장 종목 (코드, 이름) 후보, 코드 오름차순, 중복 제거.

    3단계 폴백(2026-09-04): KIND 상장법인목록 → DART 공시 법인목록(리포트 계층이
    쓰는 것과 같은 폴백, `quant/analyze/entities.py`의 `_dart_name_map` 참고) →
    로컬 보유 심볼 합집합(`_local_symbol_union`). KIND가 EC2 IP를 403으로 막으면
    (2026-08부터 알려진 문제, 리포트 계층은 이미 DART로 폴백한다) 이 함수만 그
    폴백이 없어 유니버스 재계산이 통째로 실패했었다 — 스윙 추천 생산기가 그날
    0건을 냈다. 마지막 단계(로컬 합집합)는 상장법인목록이 아니라 "우리가 이미
    아는 종목"이라 이름을 모른다 — 코드를 이름 자리에도 채운다(모르는 이름을
    지어내지 않는다)."""
    try:
        raw = fetch_kind_corp_list(cache_dir / "kind_corplist.html")
        records = parse_kind_codes(raw)
    except Exception as e:  # noqa: BLE001 — KIND 실패는 DART 폴백으로
        logger.warning("KIND 상장법인목록 실패 — DART 공시 법인목록으로 폴백: %s: %s",
                       type(e).__name__, e)
    else:
        by_code: dict[str, str] = {}
        for code, name, _market in records:
            by_code.setdefault(code, name)
        logger.info("후보 종목 목록 소스: KIND 상장법인목록 (%d종목)", len(by_code))
        return sorted(by_code.items())

    dart_map = _dart_candidate_codes(root, api_key=dart_api_key, getter=dart_getter)
    if dart_map:
        logger.info("후보 종목 목록 소스: DART 공시 법인목록 (%d종목)", len(dart_map))
        return sorted(dart_map.items())

    local_codes = _local_symbol_union(root)
    logger.warning(
        "KIND·DART 상장법인목록 모두 불가 — 로컬 보유 심볼 합집합으로 대체. "
        "후보 종목 목록 소스: 로컬 합집합 (%d종목, 이름 없음)", len(local_codes),
    )
    return sorted((code, code) for code in local_codes)


# ========================================================================
# 시가총액 — watch_scorer._market_cap_krw와 동일 알고리즘(재구현 이유는
# 모듈 docstring 참고)
# ========================================================================

def _market_cap_krw(info: dict | None, last_price: float) -> int | None:
    """`sharesOutstanding` × 마지막가로 시가총액(KRW) 근사. 결측/파싱 불가면
    `None` — 0으로 위장하지 않는다."""
    if not info:
        return None
    raw = info.get("sharesOutstanding")
    if raw is None:
        return None
    try:
        shares = float(raw)
    except (TypeError, ValueError):
        return None
    if shares <= 0 or last_price <= 0:
        return None
    return int(shares * last_price)


def _chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_last_prices(client, codes: list[str], batch_size: int = DEFAULT_PRICE_BATCH) -> dict[str, float]:
    """`client.prices()` 배치 호출(최대 `batch_size`개씩)로 마지막가를 모은다.
    배치 하나가 실패해도(레이트리밋/일시 오류) 나머지 배치는 계속한다 — 그
    배치의 심볼만 빠질 뿐 전체 유니버스 계산이 죽지 않는다."""
    out: dict[str, float] = {}
    for batch in _chunks(codes, batch_size):
        try:
            rows = client.prices(batch)
        except Exception as e:  # noqa: BLE001 — 배치 하나 실패, 나머지는 계속
            logger.warning("prices 배치 조회 실패(%d종목): %s: %s", len(batch), type(e).__name__, e)
            continue
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            price = row.get("lastPrice") or row.get("price") or row.get("close")
            try:
                if price is not None and float(price) > 0:
                    out[row["symbol"]] = float(price)
            except (TypeError, ValueError):
                continue
    return out


def fetch_market_caps(client, codes: list[str], prices: dict[str, float]) -> dict[str, int]:
    """가격이 있는 심볼만 `stock_info`(STOCK 그룹, 5 TPS, 심볼당 1회)를 호출해
    시가총액을 계산한다. 심볼 하나 실패해도 나머지는 계속한다."""
    out: dict[str, int] = {}
    for code in codes:
        price = prices.get(code)
        if price is None:
            continue
        try:
            info = client.stock_info(code)
        except Exception as e:  # noqa: BLE001
            logger.warning("stock_info 조회 실패 %s: %s: %s", code, type(e).__name__, e)
            continue
        cap = _market_cap_krw(info, price)
        if cap is not None:
            out[code] = cap
    return out


def build_universe(
    client, cache_dir: Path = KIND_CACHE_DIR, *,
    top_n: int = DEFAULT_TOP_N, min_cap: float = DEFAULT_MIN_CAP,
    price_batch: int = DEFAULT_PRICE_BATCH,
) -> list[dict]:
    """전체 상장 종목 → 시총 계산 → `min_cap` 이상만 → 시총 내림차순 `top_n`.

    반환: `[{"symbol", "name", "market_cap", "last_price"}, ...]`."""
    records = candidate_codes(cache_dir)
    codes = [code for code, _name in records]
    names = dict(records)
    prices = fetch_last_prices(client, codes, batch_size=price_batch)
    caps = fetch_market_caps(client, codes, prices)
    qualified = sorted(
        ((code, cap) for code, cap in caps.items() if cap >= min_cap),
        key=lambda kv: kv[1], reverse=True,
    )
    return [
        {"symbol": code, "name": names.get(code), "market_cap": cap, "last_price": prices[code]}
        for code, cap in qualified[:top_n]
    ]


# ========================================================================
# 유니버스 캐시 (data/state/kr_largecap_universe.json)
# ========================================================================

def load_universe(path: Path = UNIVERSE_CACHE_PATH) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def save_universe(symbols: list[dict], as_of: str, path: Path = UNIVERSE_CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"as_of": as_of, "symbols": symbols}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def is_stale(payload: dict | None, today: date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> bool:
    """캐시가 없거나, `as_of`가 없거나 파싱 불가하거나, `max_age_days`보다
    오래됐으면 True. 판정 불가는 안전측(재계산)으로 기운다."""
    if payload is None:
        return True
    as_of = payload.get("as_of")
    if not as_of:
        return True
    try:
        as_of_date = date.fromisoformat(as_of)
    except ValueError:
        return True
    return (today - as_of_date).days > max_age_days


# ========================================================================
# 일봉 백필 — quant.collect.quotes.backfill.backfill() 재사용
# ========================================================================

def backfill_universe(
    symbols: list[str], client, *, start: datetime, end: datetime | None = None,
    history_dir: str | Path = DEFAULT_HISTORY_DIR,
) -> dict[str, BackfillReport]:
    """심볼별 일봉 증분 백필 — 한 `TossClient`(레이트리미터 공유)로 순회한다
    (모듈 docstring "백필 경로" 참고). 심볼 하나 실패해도 나머지는 계속한다 —
    실패한 심볼은 반환 dict에서 빠진다(0건으로 위장하지 않는다)."""
    source = TossCandleSource(client)
    end = end or datetime.now()
    reports: dict[str, BackfillReport] = {}
    for symbol in symbols:
        try:
            reports[symbol] = backfill(symbol, source, start, end, interval="1d", history_dir=history_dir)
        except Exception as e:  # noqa: BLE001 — 심볼 하나 실패, 나머지는 계속
            logger.warning("일봉 백필 실패 %s: %s: %s", symbol, type(e).__name__, e)
    return reports


# ========================================================================
# CLI — python -m quant.collect.kr_largecap_daily (cli.py를 거치지 않는다:
# 이 모듈은 quant.apps를 임포트할 수 없어 apps.cli에서 조립할 수 없다)
# ========================================================================

def _build_client():
    from quant.adapters.brokers.toss.client import TossClient

    client_id = get_key("TOSS_CLIENT_ID") or ""
    client_secret = get_key("TOSS_CLIENT_SECRET") or ""
    if not (client_id and client_secret):
        raise RuntimeError(
            "TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 가 없다(.env.local 확인) — "
            "유니버스 재계산·일봉 백필 모두 실시세가 필요해 stub으로 대체하지 않는다."
        )
    return TossClient(
        client_id=client_id, client_secret=client_secret,
        account_seq=get_key("TOSS_ACCOUNT_SEQ") or "", mode="paper",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KR 대형주 유니버스 결정 + 일봉 백필 (모듈 docstring 참고)",
    )
    parser.add_argument("--rebuild-universe", action="store_true", help="캐시 무시하고 시총 재계산")
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--min-cap", type=float, default=DEFAULT_MIN_CAP)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--depth-years", type=int, default=DEFAULT_DEPTH_YEARS)
    parser.add_argument("--dry-run", action="store_true", help="네트워크 백필/캐시 기록 없이 대상만 출력")
    args = parser.parse_args(argv)

    today = datetime.now().date()
    payload = load_universe(UNIVERSE_CACHE_PATH)
    need_rebuild = args.rebuild_universe or is_stale(payload, today, args.max_age_days)
    client = None

    # --dry-run은 **네트워크를 전혀 부르지 않는다**(backfill_kr_stock_daily.sh의
    # DRY_RUN 관례와 동일 계약) — 재계산이 필요해도 여기서는 하지 않고 기존
    # 캐시(있으면)만으로 무엇을 할지 보여준다. 재계산 자체가 Toss stock_info를
    # 심볼당 1회 부르는 느린/과금성 경로라 "그냥 보기만" 하려는 호출에서 실행하면
    # dry-run의 의미가 없어진다.
    if need_rebuild and args.dry_run:
        reason = "캐시 없음" if payload is None else ("강제 지정" if args.rebuild_universe else "캐시 낡음")
        print(f"[DRY_RUN] 유니버스 재계산이 필요하지만({reason}) dry-run이라 네트워크 호출을 생략한다", file=sys.stderr)
        symbols = (payload or {}).get("symbols", [])
    elif need_rebuild:
        reason = "캐시 없음" if payload is None else ("강제 지정" if args.rebuild_universe else "캐시 낡음")
        print(f"유니버스 재계산 시작({reason}) — 시간이 걸릴 수 있다(모듈 docstring 참고)", file=sys.stderr)
        client = _build_client()
        symbols = build_universe(client, top_n=args.top_n, min_cap=args.min_cap)
        if not symbols:
            print("유니버스 재계산 결과 0종목 — 캐시 기록 생략, 기존 캐시 유지", file=sys.stderr)
            symbols = (payload or {}).get("symbols", [])
        else:
            save_universe(symbols, today.isoformat())
        print(f"유니버스 재계산 완료: {len(symbols)}종목 (top_n={args.top_n}, min_cap={args.min_cap:,.0f})")
    else:
        symbols = payload["symbols"] if isinstance(payload.get("symbols"), list) else []
        print(f"유니버스 캐시 사용: {len(symbols)}종목 (as_of={payload.get('as_of')})")

    codes = [s["symbol"] for s in symbols if isinstance(s, dict) and s.get("symbol")]
    if not codes:
        print("유니버스가 비어 있다 — 백필 생략", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[DRY_RUN] backfill symbols({len(codes)}): {' '.join(codes)}")
        return 0

    client = client or _build_client()
    start = datetime.now() - timedelta(days=365 * args.depth_years)
    reports = backfill_universe(codes, client, start=start, history_dir=REPO_ROOT / "data" / "history")

    ok = len(reports)
    fail = len(codes) - ok
    total_bars = sum(r.total_bars for r in reports.values())
    print(f"KR 대형주 일봉 백필 종료: 성공 {ok}/{len(codes)}, 실패 {fail}, 봉 합계(누적) {total_bars}개")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
