"""호재 마커 사전 정확도 실측 — 서브프로젝트 P Part 2.

`quant/analyze/bullish_markers.py`의 `BULLISH_MARKERS`(타입별 강도 티어)가 실측과
맞는지 큰 표본으로 확인한다. 방법론·look-ahead 경계는 `quant/backtest/
catalyst_study.py`(서브프로젝트 M)와 동일하다 — 이 모듈은 그 인프라(DART 백필,
봉 캐시, `disclosure_outcome`의 익일 시가→종가 산식, `aggregate_by_type`/
`flag_candidates`의 base rate 비교)를 그대로 재사용하고 **태깅 함수만
`BULLISH_MARKERS` 기준으로 바꾼다**. 채점(스코어러)과 검증(이 모듈)을 분리하는
이유도 동일하다 — 여기가 거짓말하면 그 위에 쌓은 판단이 조용히 틀린다.

실행: `uv run python -m quant.backtest.bullish_accuracy --days 60 --max-symbols 300`

## 두 갈래

- **Part A** (주 표본, n 수천 가능): `disclosures.jsonl`(공시 제목, `report_nm`)에
  `BULLISH_MARKERS`를 태깅 → 유형별 **익일** 시가→종가 % vs base rate. 이게
  사전 티어를 **큰 n으로 검증**하는 갈래다.
- **Part B** (얇지만 정직한 n): `mentions.jsonl`(우리 뉴스 창, 며칠)의 제목에
  같은 사전을 태깅 → 유형별 **당일**(크롤일을 `report_follow.map_to_trading_day`로
  거래일에 맞춘 뒤) 시가→종가 %. 표본이 작아 표기만 한다 — n 없이 성과를
  주장하지 않는다(`catalyst_study.py`와 동일 규율, `MIN_N_FOR_FLAG` 미만은
  후보로 올리지 않는다).

## look-ahead 경계

`catalyst_study.py`와 완전히 동일하다(같은 함수를 그대로 재사용하므로) — 자세한
근거는 그 모듈 docstring 참고. 요약: Part A는 익일 시가→종가가 주 지표(D 당일은
장중 공시로 오염 가능), Part B는 크롤일이 휴장일이면 다음 거래일로 carryover.

## 사전을 실측에 맞춰 조정할 때

이 모듈은 사전을 고치지 않는다 — 근거만 만든다. 실측이 티어와 어긋나면
`quant/analyze/bullish_markers.py`의 `BULLISH_TYPE_TIERS`를 조정하고, 그 근거를
그 모듈의 docstring에 남긴다(이 모듈 docstring이 아니라 — 사전이 그 근거를
지녀야 다음에 사전만 봐도 왜 그런지 알 수 있다).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.adapters.env import get_key
from quant.analyze.bullish_markers import classify_titles
from quant.backtest.catalyst_study import (
    BASE_RATE_KEY,
    DISCLOSURES_LEDGER,
    MENTIONS_LEDGER,
    TRADING_DAY_BUFFER,
    _read_jsonl,
    aggregate_by_type,
    backfill_disclosures,
    build_bar_cache,
    disclosure_outcome,
    flag_candidates,
    select_symbols_by_frequency,
    symbol_frequency,
    trading_days_from_bar_cache,
)
from quant.backtest.report_follow import map_to_trading_day

DEFAULT_DAYS = 60
DEFAULT_MAX_SYMBOLS = 300
ACCURACY_LEDGER = "bullish_accuracy.jsonl"


def type_for_bullish(title: str | None) -> str:
    """제목에서 `BULLISH_MARKERS`로 찾은 첫 유형(사전 순서 = 우선순위). 매치
    없으면 `"미매칭"` — `catalyst_study.type_for`와 같은 관례로 정직하게
    표기한다(태그가 없다는 뜻을 조용히 지우지 않는다)."""
    if not title:
        return "미매칭"
    types = classify_titles([title])["bullish_types"]
    return types[0] if types else "미매칭"


# ------------------------------------------------------------------ Part A — 공시 제목(순수, bar_cache 주입)

def run_part_a(disclosures_rows: list[dict], bar_cache: dict[str, dict[date, dict]]) -> dict:
    """공시 제목에 `BULLISH_MARKERS` 태깅 → 유형별 익일 수익률. `catalyst_study.
    run_part_a`와 산식은 동일 — 태깅 함수만 `type_for_bullish`로 바뀐다."""
    outcome_rows: list[dict] = []
    skipped_no_bars = 0
    skipped_no_outcome = 0
    for r in disclosures_rows:
        sym = r.get("stock_code")
        rcept_dt = r.get("rcept_dt")
        if not sym or not rcept_dt:
            continue
        bars = bar_cache.get(sym)
        if bars is None:
            skipped_no_bars += 1
            continue
        try:
            d = datetime.strptime(rcept_dt, "%Y%m%d").date()
        except ValueError:
            continue
        outcome = disclosure_outcome(bars, d)
        if outcome is None:
            skipped_no_outcome += 1
            continue
        outcome_rows.append({
            "type": type_for_bullish(r.get("report_nm")), "symbol": sym, "date": d.isoformat(), **outcome,
        })

    type_stats = aggregate_by_type(outcome_rows)
    positive, negative = flag_candidates(type_stats)
    return {
        "rows": outcome_rows,
        "n_disclosures": len(disclosures_rows),
        "skipped_no_bars": skipped_no_bars,
        "skipped_no_outcome": skipped_no_outcome,
        "type_stats": type_stats,
        "positive_flags": positive,
        "negative_flags": negative,
    }


# ------------------------------------------------------------------ Part B — 뉴스 제목(순수, bar_cache 주입)

def run_part_b(
    mentions_rows: list[dict], bar_cache: dict[str, dict[date, dict]], trading_days: list[date] | None = None,
) -> dict:
    """뉴스 제목에 `BULLISH_MARKERS` 태깅 → 유형별 **적용거래일** 시가→종가 %.
    `catalyst_study.run_part_c`와 같은 carryover(`map_to_trading_day`) — 태깅
    함수만 `type_for_bullish`로 바뀐다."""
    trading_days = trading_days or []
    outcome_rows: list[dict] = []

    for r in mentions_rows:
        sym = r.get("symbol")
        raw_date = r.get("date")
        title = r.get("title") or ""
        if not sym or not raw_date:
            continue
        try:
            crawl_date = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        d = map_to_trading_day(crawl_date, trading_days) if trading_days else crawl_date
        if d is None:
            continue
        bars = bar_cache.get(sym)
        if not bars or d not in bars:
            continue
        bar = bars[d]
        if not bar.get("open") or bar["open"] <= 0:
            continue
        pct = (bar["close"] - bar["open"]) / bar["open"] * 100
        outcome_rows.append({"type": type_for_bullish(title), "open_close_next_pct": pct})

    type_stats = aggregate_by_type(outcome_rows)
    positive, negative = flag_candidates(type_stats)
    return {
        "rows": outcome_rows,
        "n_mentions": len(mentions_rows),
        "type_stats": type_stats,
        "positive_flags": positive,
        "negative_flags": negative,
    }


# ------------------------------------------------------------------ 전체 오케스트레이션(I/O)

def run_accuracy_study(
    root: Path,
    days: int,
    max_symbols: int,
    candle_source,
    dart_api_key: str | None = None,
    dart_getter=None,
    today: date | None = None,
) -> dict:
    today = today or date.today()

    backfill = backfill_disclosures(root, days, today, dart_api_key, getter=dart_getter)

    disclosures_rows = _read_jsonl(root / "data" / "ledger" / DISCLOSURES_LEDGER)
    mentions_rows = _read_jsonl(root / "data" / "ledger" / MENTIONS_LEDGER)

    freq = symbol_frequency(disclosures_rows, mentions_rows)
    symbols = select_symbols_by_frequency(freq, max_symbols)

    window_start = today - timedelta(days=days)
    window_end = today
    fetch_start = datetime.combine(window_start - timedelta(days=TRADING_DAY_BUFFER), datetime.min.time())
    fetch_end = datetime.combine(window_end + timedelta(days=1), datetime.min.time())

    bar_cache, failed_symbols = build_bar_cache(candle_source, symbols, fetch_start, fetch_end)
    trading_days = trading_days_from_bar_cache(bar_cache)

    part_a = run_part_a(disclosures_rows, bar_cache)
    part_b = run_part_b(mentions_rows, bar_cache, trading_days)

    return {
        "params": {"days": days, "max_symbols": max_symbols, "today": today.isoformat()},
        "backfill": backfill,
        "n_symbols_fetched": len(bar_cache),
        "n_symbols_failed": len(failed_symbols),
        "part_a": part_a,
        "part_b": part_b,
    }


# ------------------------------------------------------------------ 출력 + 원장 기록

def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _fmt_val(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def _format_type_table(type_stats: dict[str, dict]) -> list[str]:
    lines = [f"{'유형':<24}{'n':>8}{'평균%':>10}{'승률':>10}{'급등율(≥5%)':>14}{'상한가급(≥15%)':>16}"]
    ordered = sorted((t for t in type_stats if t != BASE_RATE_KEY), key=lambda t: -(type_stats[t]["n"]))
    for t in [*ordered, BASE_RATE_KEY]:
        s = type_stats[t]
        lines.append(
            f"{t:<24}{s['n']:>8}{_fmt_val(s['avg_pct']):>10}{_fmt_pct(s['hit_rate']):>10}"
            f"{_fmt_pct(s['surge_rate']):>14}{_fmt_pct(s['limit_rate']):>16}"
        )
    return lines


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("=== 호재 마커 사전 정확도 실측 (BULLISH_MARKERS) ===")
    p = result["params"]
    lines.append(f"기간: 최근 {p['days']}일 · 종목 상한 {p['max_symbols']} · 기준일 {p['today']}")

    bf = result["backfill"]
    lines.append(
        f"DART 백필: 조회 {len(bf['fetched_days'])}일 · 스킵(이미 밀집) {len(bf['skipped_days'])}일 · "
        f"신규 {bf['added_total']}건 · 에러 {len(bf['errors'])}건"
    )
    lines.append(f"봉 캐시: 성공 {result['n_symbols_fetched']}종목 · 실패 {result['n_symbols_failed']}종목")

    pa = result["part_a"]
    lines.append("")
    lines.append("--- Part A: 공시 제목 · BULLISH_MARKERS 유형별 익일(익일 시가→종가) 수익률 ---")
    lines.append(
        f"공시 원장 n={pa['n_disclosures']} · 봉 없어 skip {pa['skipped_no_bars']} · "
        f"익일 데이터 없어 skip {pa['skipped_no_outcome']}"
    )
    lines.extend(_format_type_table(pa["type_stats"]))
    lines.append(f"긍정 후보(실측 검증됨): {pa['positive_flags'] or '없음'}")
    lines.append(f"부정 후보(실측상 base보다 나쁨): {pa['negative_flags'] or '없음'}")

    pb = result["part_b"]
    lines.append("")
    lines.append("--- Part B: 뉴스 제목(우리 창) · BULLISH_MARKERS 유형별 당일 시가→종가 % (표본 작음 — 표기만) ---")
    lines.append(f"뉴스 원장 n={pb['n_mentions']}")
    lines.extend(_format_type_table(pb["type_stats"]))

    return "\n".join(lines)


def write_ledger(result: dict, root: Path, today: date | None = None) -> Path:
    """`data/ledger/bullish_accuracy.jsonl`에 이번 실행 요약을 append."""
    today = today or date.today()
    path = root / "data" / "ledger" / ACCURACY_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    pa, pb = result["part_a"], result["part_b"]
    row = {
        "date": today.isoformat(),
        "params": result["params"],
        "part_a_base": pa["type_stats"].get(BASE_RATE_KEY),
        "part_a_positive_flags": pa["positive_flags"],
        "part_a_negative_flags": pa["negative_flags"],
        "part_a_type_stats": pa["type_stats"],
        "part_b_base": pb["type_stats"].get(BASE_RATE_KEY),
        "part_b_type_stats": pb["type_stats"],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bullish_accuracy")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        candle_source = YFinanceCandleSource("1d")
        api_key = get_key("DART_API_KEY")
        result = run_accuracy_study(root, args.days, args.max_symbols, candle_source, dart_api_key=api_key)
        print(format_report(result))
        ledger_path = write_ledger(result, root)
        print(f"\n원장 기록: {ledger_path}")
    except Exception as e:  # noqa: BLE001 — 이 배치는 항상 exit 0 (다른 파이프라인을 막지 않는다)
        print(f"호재 정확도 실측 실행 실패: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
