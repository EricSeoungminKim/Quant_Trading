"""리포트 추종 백테스트 — 서브프로젝트 L, L-6.

**질문**: 자체 리포트(`out/YYYY/MM/DD/KR_engine.json`)의 `AUTO_WATCH:` 후보를 그대로
따랐다면 실제로 돈을 벌었을까? `market_brief.auto_watch_tokens`가 골라내는 종목이
좋은 후보라는 *주장*을 여기서 실측으로 검증한다. 채점(리포트 생성)과 검증(이 모듈)을
분리하는 이유는 `quant/backtest/intraday_verify.py`·`quant/backtest/fitness.py`와
같다 — "여기가 거짓말하면 그 위에 쌓은 모든 판단이 조용히 틀린다."

실행: `uv run python -m quant.backtest.report_follow --root . [--fee-bp 20]`

## 정직한 범위 (사용자 요청에 대한 정직한 답)

**자체 리포트·뉴스 원장은 2026-08-13부터 존재한다** — 그 전엔 이 시스템 자체가
없었다. "최근 한 달"을 재현해 달라는 요청은 데이터가 없어 문자 그대로 불가능하다.
할 수 있는 건 **가능한 전 기간**(08-13~현재, 거래일 기준)을 계산하고 표본 크기를
정직하게 명시하는 것뿐이다. 표본은 매일 자동으로 커진다(리포트가 매일 발행되고,
이 배치도 크론으로 반복 실행 가능).

## 휴장일 리포트 → 다음 개장일 적용 (carryover 와 같은 사고)

리포트는 매일 발행된다(휴장일 포함, `quant/analyze/carryover.py` 참고 — 휴장 기간
재료는 다음 개장일에 자동 편입된다). 그래서 리포트 발행일 D 자체가 거래일이 아니면
(주말·공휴일), 그날의 후보는 **다음 개장일**에 실제로 편입돼 거래된다. 이 모듈은
`market_brief.auto_watch_tokens`가 뽑아낸 후보를, D가 개장일이면 D 그대로, 아니면
D 다음 개장일로 매핑해 시뮬한다(`map_to_trading_day`).

## 두 시뮬레이션

1. **단타(퀀트 방식)**: 적용거래일 시가 진입 → 같은 날 종가 청산. 순bp = 총bp -
   수수료(왕복, bp). 기본 20bp는 **추정치가 아니라 실측값**이다 — US 세 전략 전부
   수수료 전 양수인데 왕복 20bp가 전부 0 근처/음수로 뒤집었다
   (`docs/plans/개선-백로그-2026-08-15.md` P0).
2. **1주 적립(사람 방식)**: 종목이 후보로 오른 적용거래일마다 1주를 시가에 매수
   (연속 보유 = 계속 적립). 후보에서 탈락한 첫 거래일에 1주를 시가에 매도. 기말에
   실현 현금흐름 + 잔여 보유분 × 창(window) 마지막 거래일 종가 = 총손익. 수수료는
   매수·매도 각 거래 명목에 개별 적용한다.

## 표본 부족을 성과로 위장하지 않는다

`quant-expert`·`fitness.py`(MIN_ROUND_TRIPS=30)와 같은 문턱을 쓴다. 종목-일 표본이
`MIN_SAMPLE_FOR_JUDGEMENT` 미만이면 "판단 불가"를 출력에 명시한다. 종료 코드는
항상 0 — 이 배치가 실패해도 다른 파이프라인을 막지 않는다.
"""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.analyze.market_brief import auto_watch_tokens

DEFAULT_FEE_BP = 20.0
MIN_SAMPLE_FOR_JUDGEMENT = 30   # fitness.py MIN_ROUND_TRIPS 와 동일 기준
YF_SLEEP_SECONDS = 0.3
ANCHOR_SYMBOL = "069500"        # KODEX200 — intraday_verify.py 와 동일한 개장일 판정 앵커
FOLLOW_LEDGER = "report_follow.jsonl"
FIRST_REPORT_DATE = date(2026, 8, 13)  # 자체 리포트 시스템 시작일 — 재현 가능 구간의 하한


# ------------------------------------------------------------------ 리포트 스캔(순수 파싱)

def scan_report_days(root: Path, market: str = "KR") -> dict[date, dict]:
    """`out/YYYY/MM/DD/{market}_engine.json` 전부를 글롭해 {날짜: payload} 로 반환.

    날짜는 파일 내용(session_date)이 아니라 **디렉토리 경로**에서 얻는다 — 발행
    시점 폴더가 실제 발행일이고, 이게 이 모듈이 신뢰하는 단일 기준이다."""
    out: dict[date, dict] = {}
    for path in sorted((root / "out").glob(f"*/*/*/{market}_engine.json")):
        try:
            y, m, d = path.parts[-4], path.parts[-3], path.parts[-2]
            day = date(int(y), int(m), int(d))
        except (ValueError, IndexError):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(payload, dict):
            out[day] = payload
    return out


def candidates_for_day(payload: dict, market: str = "KR") -> list[str]:
    """그날 후보 심볼 목록. `market_brief.auto_watch_tokens`를 그대로 재사용해
    형식·시장(6자리 KR)·상한 검증을 브리핑과 동일하게 받는다 — 이 백테스트가
    실제로 등록됐을 후보와 다른 걸 보면 안 된다."""
    return [tok.split(":", 1)[0] for tok in auto_watch_tokens(payload, market)]


# ------------------------------------------------------------------ 개장일 매핑(순수)

def map_to_trading_day(report_day: date, trading_days: list[date]) -> date | None:
    """D가 개장일이면 D 그대로, 휴장이면 D 다음 개장일. `trading_days`는 오름차순
    이어야 한다. 창 밖(다음 개장일 데이터가 아직 없음)이면 None."""
    idx = bisect.bisect_left(trading_days, report_day)
    if idx < len(trading_days) and trading_days[idx] == report_day:
        return report_day
    if idx < len(trading_days):
        return trading_days[idx]
    return None


def build_applied_candidates(
    report_days: dict[date, dict], trading_days: list[date], market: str = "KR",
) -> tuple[dict[date, set[str]], dict[date, date | None]]:
    """리포트일 → 적용거래일 매핑 + 적용거래일별 후보 합집합(여러 리포트일이 같은
    적용거래일로 몰릴 수 있다 — 연휴 뒤 첫 개장일이 대표적)."""
    mapping: dict[date, date | None] = {}
    applied: dict[date, set[str]] = {}
    for d in sorted(report_days):
        applied_day = map_to_trading_day(d, trading_days)
        mapping[d] = applied_day
        if applied_day is None:
            continue
        syms = candidates_for_day(report_days[d], market)
        applied.setdefault(applied_day, set()).update(syms)
    return applied, mapping


# ------------------------------------------------------------------ 시세(yfinance, 네트워크는 호출자가 넘긴 candle_source)

def fetch_symbol_bars(
    candle_source, symbol: str, days: list[date], sleep_seconds: float = YF_SLEEP_SECONDS,
) -> dict[date, dict]:
    """`days` 구간을 한 번에 fetch해 날짜별 open/close 만 뽑는다. 데이터 없는 날은
    결과 dict에서 빠진다(0으로 위장하지 않는다)."""
    if not days:
        return {}
    start = datetime.combine(min(days), datetime.min.time())
    end = datetime.combine(max(days) + timedelta(days=1), datetime.min.time())
    try:
        df = candle_source.fetch(symbol, start, end)
    except Exception:  # noqa: BLE001 — 종목 하나의 조회 실패가 전체 백테스트를 막지 않는다
        df = None
    finally:
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if df is None or df.empty:
        return {}

    out: dict[date, dict] = {}
    for d in days:
        mask = df.index.date == d
        matched = df.loc[mask]
        if matched.empty:
            continue
        row = matched.iloc[-1]
        if row["open"] is None or row["open"] <= 0:
            continue
        out[d] = {"open": float(row["open"]), "close": float(row["close"])}
    return out


# ------------------------------------------------------------------ 단타 시뮬(순수)

def simulate_day_trades(
    applied: dict[date, set[str]], bars: dict[str, dict[date, dict]], fee_bp: float,
) -> list[dict]:
    """적용거래일 시가 진입 → 같은 날 종가 청산. 봉이 없는 종목-일은 건너뛴다."""
    records: list[dict] = []
    for d in sorted(applied):
        for sym in sorted(applied[d]):
            bar = bars.get(sym, {}).get(d)
            if bar is None:
                continue
            gross_bp = (bar["close"] - bar["open"]) / bar["open"] * 10000
            records.append({
                "date": d.isoformat(), "symbol": sym,
                "gross_bp": gross_bp, "net_bp": gross_bp - fee_bp,
            })
    return records


def aggregate_day_trades(records: list[dict]) -> dict:
    n = len(records)
    if n == 0:
        return {"n": 0, "hit_rate": None, "avg_gross_bp": None, "avg_net_bp": None, "sum_net_bp": None}
    net = [r["net_bp"] for r in records]
    return {
        "n": n,
        "hit_rate": sum(1 for x in net if x > 0) / n,
        "avg_gross_bp": statistics.fmean(r["gross_bp"] for r in records),
        "avg_net_bp": statistics.fmean(net),
        "sum_net_bp": sum(net),
    }


# ------------------------------------------------------------------ 1주 적립 시뮬(순수)

def simulate_weekly_accumulate(
    applied: dict[date, set[str]], trading_days: list[date],
    bars: dict[str, dict[date, dict]], fee_bp: float,
) -> dict[str, dict]:
    """종목별: 후보인 적용거래일마다 1주 시가 매수(연속이면 계속 적립), 후보에서
    빠진 첫 거래일에 보유분에서 1주 시가 매도. 기말 잔여 보유분은 `trading_days`의
    마지막 날 종가로 시가평가한다(그 값을 못 구하면 그 종목은 미확정 — pnl=None).
    """
    symbols = sorted({s for syms in applied.values() for s in syms})
    last_day = trading_days[-1] if trading_days else None

    out: dict[str, dict] = {}
    for sym in symbols:
        holding = 0
        was_in = False
        invested = 0.0
        realized = 0.0
        buys = 0
        sells = 0
        for d in trading_days:
            in_watch = sym in applied.get(d, set())
            bar = bars.get(sym, {}).get(d)
            if in_watch:
                if bar is not None:
                    price = bar["open"]
                    fee = price * fee_bp / 10000
                    invested += price
                    realized -= price + fee
                    holding += 1
                    buys += 1
                was_in = True
            else:
                if was_in and holding > 0 and bar is not None:
                    price = bar["open"]
                    fee = price * fee_bp / 10000
                    realized += price - fee
                    holding -= 1
                    sells += 1
                was_in = False

        remaining_value = None
        if holding > 0 and last_day is not None:
            last_bar = bars.get(sym, {}).get(last_day)
            if last_bar is not None:
                remaining_value = holding * last_bar["close"]

        if holding == 0:
            pnl = realized
        elif remaining_value is not None:
            pnl = realized + remaining_value
        else:
            pnl = None  # 잔여 보유분 시가를 못 구함 — 판단 불가, 0으로 위장하지 않는다

        return_pct = (pnl / invested * 100) if (pnl is not None and invested > 0) else None

        out[sym] = {
            "buys": buys, "sells": sells,
            "invested_krw": invested, "realized_krw": realized,
            "remaining_shares": holding, "remaining_value_krw": remaining_value,
            "pnl_krw": pnl, "return_pct": return_pct,
        }
    return out


def aggregate_weekly(results: dict[str, dict]) -> dict:
    n = len(results)
    resolved = [r for r in results.values() if r["pnl_krw"] is not None]
    total_invested = sum(r["invested_krw"] for r in results.values())
    total_pnl = sum(r["pnl_krw"] for r in resolved) if resolved else None
    return {
        "n_symbols": n,
        "n_resolved": len(resolved),
        "n_unresolved": n - len(resolved),
        "total_invested_krw": total_invested,
        "total_pnl_krw": total_pnl,
        "total_return_pct": (total_pnl / total_invested * 100)
        if (total_pnl is not None and total_invested > 0) else None,
    }


# ------------------------------------------------------------------ 오케스트레이션(I/O)

def run_follow(root: Path, fee_bp: float, candle_source, market: str = "KR",
               today: date | None = None) -> dict:
    today = today or date.today()
    report_days = scan_report_days(root, market)

    empty = {
        "market": market, "fee_bp": fee_bp,
        "report_days": [], "trading_days": [], "mapping": {},
        "day_trade_records": [], "day_trade_agg": aggregate_day_trades([]),
        "weekly_results": {}, "weekly_agg": aggregate_weekly({}),
    }
    if not report_days:
        return empty

    span_start = min(report_days)
    span_end = max(max(report_days), today)
    anchor_start = datetime.combine(span_start, datetime.min.time())
    anchor_end = datetime.combine(span_end + timedelta(days=1), datetime.min.time())
    try:
        anchor_df = candle_source.fetch(ANCHOR_SYMBOL, anchor_start, anchor_end)
    except Exception:  # noqa: BLE001 — 앵커 조회 실패 시 개장일 판정 불가, 빈 결과로 처리
        anchor_df = None
    finally:
        if YF_SLEEP_SECONDS:
            time.sleep(YF_SLEEP_SECONDS)

    trading_days = sorted({d for d in anchor_df.index.date}) if anchor_df is not None and not anchor_df.empty else []

    applied, mapping = build_applied_candidates(report_days, trading_days, market)
    result = {**empty, "report_days": sorted(report_days), "trading_days": trading_days, "mapping": mapping}
    if not applied:
        return result

    applied_days_sorted = sorted(applied)
    weekly_trading_days = [
        d for d in trading_days if applied_days_sorted[0] <= d <= applied_days_sorted[-1]
    ]

    all_symbols = sorted({s for syms in applied.values() for s in syms})
    bars: dict[str, dict[date, dict]] = {
        sym: fetch_symbol_bars(candle_source, sym, weekly_trading_days) for sym in all_symbols
    }

    day_trade_records = simulate_day_trades(applied, bars, fee_bp)
    weekly_results = simulate_weekly_accumulate(applied, weekly_trading_days, bars, fee_bp)

    result.update({
        "day_trade_records": day_trade_records,
        "day_trade_agg": aggregate_day_trades(day_trade_records),
        "weekly_results": weekly_results,
        "weekly_agg": aggregate_weekly(weekly_results),
    })
    return result


# ------------------------------------------------------------------ 출력 + 원장 기록

def _fmt_bp(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}bp"


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _fmt_pct2(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _fmt_krw(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+,.0f}원"


def format_report(result: dict, fee_bp: float) -> str:
    lines = [
        "=== 리포트 추종 백테스트 (KR, 단타 vs 1주 적립) ===",
        f"자체 리포트 시작일: {FIRST_REPORT_DATE.isoformat()} — 그 이전은 데이터가 없어 재현 불가. "
        "표본은 매일 자동으로 커진다.",
        f"수수료: 왕복 {fee_bp:.1f}bp (docs/plans/개선-백로그-2026-08-15.md P0 실측값)",
    ]
    report_days = result["report_days"]
    if not report_days:
        lines.append("")
        lines.append("리포트(out/YYYY/MM/DD/KR_engine.json) 없음 — 표본 0.")
        return "\n".join(lines)

    lines.append(f"기간: {report_days[0].isoformat()} ~ {report_days[-1].isoformat()} (리포트 {len(report_days)}일)")

    agg = result["day_trade_agg"]
    lines.append("")
    lines.append("--- 단타(적용거래일 시가진입 → 종가청산) ---")
    lines.append(
        f"종목-일 n={agg['n']} · 적중률 {_fmt_pct(agg['hit_rate'])} · "
        f"평균 총bp {_fmt_bp(agg['avg_gross_bp'])} · 평균 순bp {_fmt_bp(agg['avg_net_bp'])} · "
        f"합계 순bp {_fmt_bp(agg['sum_net_bp'])}"
    )
    if agg["n"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(f"⚠️ 표본 부족(종목-일 n={agg['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가.")

    wagg = result["weekly_agg"]
    lines.append("")
    lines.append("--- 1주 적립(후보일마다 1주 매수 → 탈락일 1주 매도 → 잔여 시가평가) ---")
    lines.append(
        f"종목 n={wagg['n_symbols']}(확정 {wagg['n_resolved']}·미확정 {wagg['n_unresolved']}) · "
        f"투입 {_fmt_krw(wagg['total_invested_krw'])} · 손익 {_fmt_krw(wagg['total_pnl_krw'])} · "
        f"수익률 {_fmt_pct2(wagg['total_return_pct'])}"
    )
    if wagg["n_symbols"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(f"⚠️ 표본 부족(종목 n={wagg['n_symbols']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가.")

    return "\n".join(lines)


def write_ledger(result: dict, root: Path, today: date | None = None) -> Path:
    """`data/ledger/report_follow.jsonl`에 이번 실행 요약을 append 한다."""
    today = today or date.today()
    path = root / "data" / "ledger" / FOLLOW_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    report_days = result["report_days"]
    row = {
        "date": today.isoformat(),
        "market": result["market"],
        "fee_bp": result.get("fee_bp"),
        "n_report_days": len(report_days),
        "period": {
            "start": report_days[0].isoformat() if report_days else None,
            "end": report_days[-1].isoformat() if report_days else None,
        },
        "day_trade": result["day_trade_agg"],
        "weekly_accumulate": result["weekly_agg"],
        "sufficient_day_trade": result["day_trade_agg"]["n"] >= MIN_SAMPLE_FOR_JUDGEMENT,
        "sufficient_weekly": result["weekly_agg"]["n_symbols"] >= MIN_SAMPLE_FOR_JUDGEMENT,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="report_follow")
    parser.add_argument("--root", default=".")
    parser.add_argument("--fee-bp", type=float, default=DEFAULT_FEE_BP)
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        candle_source = YFinanceCandleSource("1d")
        result = run_follow(root, args.fee_bp, candle_source)
        print(format_report(result, args.fee_bp))
        ledger_path = write_ledger(result, root)
        print(f"\n원장 기록: {ledger_path}")
    except Exception as e:  # noqa: BLE001 — 이 배치는 항상 exit 0 (다른 파이프라인을 막지 않는다)
        print(f"리포트 추종 백테스트 실행 실패: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
