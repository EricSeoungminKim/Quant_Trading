"""당일 단타 스코어러 과거 재현 검증 하네스 — 서브프로젝트 K Part 2.

**사용자 요구 원문 요지(2026-08-17)**: "정확도는 과거 뉴스로 재현한 선정이
실제 그날 가격·거래량이 올랐는지로 검증해 계속 확신도 있게." `quant.analyze.
intraday_score.score_intraday`가 좋은 후보를 고른다는 *주장*을 여기서 실측으로
검증한다. 채점 로직과 검증 로직을 분리하는 이유는 `quant/backtest/fitness.py`와
같다 — "여기가 거짓말하면 그 위에 쌓은 모든 자동 개선이 조용히 틀린다."

실행: `uv run python -m quant.backtest.intraday_verify --days 10 --top 5`

## look-ahead 경계 (정직하게 문서화 — 숨기지 않는다)

- **mentions.jsonl**: 행의 `date`는 크롤 실행일이지 기사 발행 타임스탬프가
  아니다(원장에 시각 필드가 없다). KR 파이프라인은 08:00 크롤 → 08:40 리포트
  순으로 돈다(`CLAUDE.md`) — 그래서 크롤일 D에 잡힌 행은 "D 개장 전 시점의
  정보"라고 **가정**한다. 이 가정은 행 단위로 검증할 방법이 없다(발행 시각이
  없으므로) — 크롤 배치가 장중에 재실행되면 이 경계가 새서 룩어헤드가 생길 수
  있다. **알려진 한계로 남긴다.**
- **frgn_flow.jsonl**: 종목별 외국인 수급은 그날 장 마감 후에만 확정된다.
  D의 외국인 라벨 산정에는 **D-1까지의 행만** 쓴다 — D 당일 행은 절대 읽지
  않는다(날짜 문자열 비교로 강제).
- **disclosures.jsonl**: `rcept_dt`(접수일자)가 D **이하**인 공시만 쓴다. DART는
  장중에도 공시가 올라오므로 D 당일 오전 공시까지는 정당하게 pre-open 정보일
  수 있지만, 장중 공시가 섞였을 가능성은 배제하지 못한다 — **알려진 한계**.
- **trending_score100 / theme_change_pct**: 과거 시점의 랭킹 스냅샷·테마 등락률을
  재구성할 원장이 없다(둘 다 실시간 API 전용, 원장화되지 않음). 이 하네스는
  두 입력을 항상 `None`으로 채운다 — `score_intraday`의 nullable 4개 중 2개가
  구조적으로 늘 빠진다는 뜻이라, `news_z`/`foreign_v2_score` 중 하나라도 더
  없으면 그 종목은 채점 자체가 `None`(판단 불가)이 된다. 버그가 아니라 이
  하네스가 가진 데이터의 정직한 한계다.
- **foreign_v2_score 의 강도 서브점수**(2026-08-17, 외국인 축 라벨→v2 교체
  후속): `foreign_score_v2()`는 봉(거래량) 캐시가 있어야 강도 규칙을 채점할
  수 있는데, 이 하네스는 pre-open 재구성에 봉을 쓰지 않는다(`symbol_outcome`
  이 봉을 조회하는 건 채점 **이후** 결과 측정 단계뿐) — 그래서
  `foreign_score_v2(prior, {})`를 빈 봉 캐시로 호출한다. 강도 서브점수는
  항상 0으로 평가되고(`foreign_intensity_ratio`가 정직하게 `None`을 돌려줌),
  쌍끌이·연속일·정합·이탈 4개 규칙은 정상 평가된다 — 봉 캐시를 추가하면
  없앨 수 있는 한계지만, 이 하네스 범위 밖으로 남긴다.

## 표본 부족을 성과로 위장하지 않는다

`quant-expert` 규율과 동일: n 없이 성과를 주장하지 않는다. 표본(종목-일)이
`MIN_SAMPLE_FOR_JUDGEMENT` 미만이면 출력에 "표본 부족 — 판단 불가"를 명시한다.
그래도 종료 코드는 항상 0이다 — 이 검증 배치가 실패해도 다른 파이프라인을
막으면 안 된다(이 저장소의 배치 관례).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.analyze.foreign_flow_v2 import foreign_score_v2
from quant.analyze.intraday_score import rank_intraday
from quant.analyze.news_cluster import dedup_with_counts
from quant.analyze.news_momentum import news_zscore
from quant.collect.sources.dart import classify_report

DEFAULT_DAYS = 10
DEFAULT_TOP = 5
MAX_SYMBOLS_PER_DAY = 15
YF_SLEEP_SECONDS = 0.3
NEWS_HISTORY_DAYS = 20          # news_zscore 가 요구하는 baseline 창(news_momentum 과 동일)
FRGN_FLOW_WINDOW_DAYS = 20      # foreign_score_v2 에 넘길 최근 창
TRAILING_VOL_DAYS = 20          # 거래량 배율 baseline
MIN_VOL_BASELINE_DAYS = 5       # 이보다 짧은 baseline 은 배율을 계산하지 않는다
MIN_SAMPLE_FOR_JUDGEMENT = 20   # 종목-일 표본 — 이 미만은 "판단 불가"
ANCHOR_SYMBOL = "069500"        # KODEX200 — opendays.py 와 동일한 시장 대리 지표

MENTIONS_LEDGER = "mentions.jsonl"
FRGN_FLOW_LEDGER = "frgn_flow.jsonl"
DISCLOSURES_LEDGER = "disclosures.jsonl"
VERIFY_LEDGER = "intraday_verify.jsonl"


# ------------------------------------------------------------------ 원장 로딩(순수, 네트워크 없음)

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def index_mentions(rows: list[dict]) -> dict[str, dict[date, list[dict]]]:
    """symbol → {date → 그날 행들}. 파싱 안 되는 행(symbol/date 없음)은 버린다."""
    idx: dict[str, dict[date, list[dict]]] = {}
    for r in rows:
        symbol = r.get("symbol")
        raw_date = r.get("date")
        if not symbol or not raw_date:
            continue
        try:
            d = date.fromisoformat(str(raw_date))
        except ValueError:
            continue
        idx.setdefault(symbol, {}).setdefault(d, []).append(r)
    return idx


def index_frgn_flow(rows: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for r in rows:
        symbol = r.get("symbol")
        if not symbol or not r.get("date"):
            continue
        idx.setdefault(symbol, []).append(r)
    return idx


def index_disclosures(rows: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for r in rows:
        stock_code = r.get("stock_code")
        if not stock_code:
            continue
        idx.setdefault(stock_code, []).append(r)
    return idx


# ------------------------------------------------------------------ pre-open 입력 재구성(순수)

def candidate_symbols(
    mentions_idx: dict[str, dict[date, list[dict]]], d: date, cap: int = MAX_SYMBOLS_PER_DAY,
) -> list[str]:
    """D 에 뉴스가 잡힌 종목들 — 오늘 기사 건수 내림차순으로 상위 `cap`개만 채점한다
    (yfinance 호출 예산을 지키기 위함, cap 은 항상 기사가 가장 많은 종목을 우선한다)."""
    counted = [(sym, len(dated.get(d, []))) for sym, dated in mentions_idx.items() if dated.get(d)]
    counted.sort(key=lambda t: (-t[1], t[0]))
    return [sym for sym, _ in counted[:cap]]


def _counts_series(symbol_dates: dict[date, list[dict]], upto: date, days: int = NEWS_HISTORY_DAYS) -> list[int]:
    """과거→오늘(upto, 포함) 순 일별 건수. `news_momentum.daily_counts`와 같은 계약."""
    start = upto - timedelta(days=days - 1)
    return [len(symbol_dates.get(start + timedelta(days=i), [])) for i in range(days)]


def _max_dup_count(symbol_dates: dict[date, list[dict]], d: date) -> int:
    rows = symbol_dates.get(d, [])
    if not rows:
        return 0
    deduped = dedup_with_counts(rows)
    return max((item["dup_count"] for item in deduped), default=0)


def _foreign_v2_score_for(frgn_idx: dict[str, list[dict]], symbol: str, d: date, days: int = FRGN_FLOW_WINDOW_DAYS) -> int | None:
    """D **미만**(strictly before) 행만 써서 `foreign_score_v2` 원점수를
    낸다 — D 당일 수급은 마감 후 확정이라 D 개장 전 시점에는 알 수 없다
    (look-ahead 방지). 시계열이 아예 없으면 `None`(0으로 위장하지 않는다 —
    `foreign_score_v2([], {})`는 항상 int를 돌려주므로 호출자가 이 게이트를
    직접 지켜야 한다). 봉 캐시가 없어(모듈 docstring) 강도 서브점수는 이
    경로에서 항상 0으로 평가된다."""
    rows = frgn_idx.get(symbol, [])
    d_str = d.isoformat()
    prior = sorted((r for r in rows if r.get("date") and str(r["date"]) < d_str), key=lambda r: r["date"])
    if not prior:
        return None
    return foreign_score_v2(prior[-days:], {})[0]


def _dart_types_for(disclosures_idx: dict[str, list[dict]], symbol: str, d: date) -> list[str]:
    """`rcept_dt` (YYYYMMDD 문자열) 가 D **이하**인 공시만 쓴다."""
    rows = disclosures_idx.get(symbol, [])
    d_str = d.strftime("%Y%m%d")
    types: list[str] = []
    seen: set[str] = set()
    for r in rows:
        rcept_dt = r.get("rcept_dt")
        if not rcept_dt or str(rcept_dt) > d_str:
            continue
        label = classify_report(r.get("report_nm") or "")
        if label and label not in seen:
            seen.add(label)
            types.append(label)
    return types


def _titles_for(symbol_dates: dict[date, list[dict]], d: date) -> list[str]:
    """D 당일 mentions 행들의 `title`. `mentions.jsonl`은 우리 뉴스 파이프라인이
    실제로 수집한 원문 제목을 갖고 있어(`catalyst_study.run_part_c`가 같은
    필드를 읽는다) — 과거 재구성에서도 정직하게 복원 가능하다(0으로 위장하는
    게 아니라 실제로 있는 데이터)."""
    return [r.get("title", "") for r in symbol_dates.get(d, []) if r.get("title")]


def reconstruct_inputs(
    symbol: str, d: date,
    mentions_idx: dict[str, dict[date, list[dict]]],
    frgn_idx: dict[str, list[dict]],
    disclosures_idx: dict[str, list[dict]],
) -> dict:
    """D 개장 전 시점에 실제로 알 수 있었던 것만으로 `score_intraday` 입력을 만든다.
    `trending_score100`/`theme_change_pct`는 모듈 docstring 의 이유로 항상 None.
    `titles`(호재 마커/악재 거부권 판정용, 서브프로젝트 P)는 `mentions.jsonl`이
    D 당일 행의 원문 제목을 그대로 갖고 있어 정직하게 재구성된다."""
    symbol_dates = mentions_idx.get(symbol, {})
    counts = _counts_series(symbol_dates, d)
    return {
        "today_articles": counts[-1] if counts else 0,
        "news_z": news_zscore(counts),
        "max_dup_count": _max_dup_count(symbol_dates, d),
        "titles": _titles_for(symbol_dates, d),
        "foreign_v2_score": _foreign_v2_score_for(frgn_idx, symbol, d),
        "trending_score100": None,
        "dart_types": _dart_types_for(disclosures_idx, symbol, d),
        "theme_change_pct": None,
    }


# ------------------------------------------------------------------ 결과(yfinance, 네트워크)

def _row_for_date(df, d: date):
    """일봉 DataFrame(UTC tz-aware DatetimeIndex)에서 달력일 `d`의 행 하나. 없으면 None."""
    if df is None or df.empty:
        return None
    mask = df.index.date == d
    matched = df.loc[mask]
    if matched.empty:
        return None
    return matched.iloc[-1]


def symbol_outcome(candle_source, symbol: str, d: date, sleep_seconds: float = YF_SLEEP_SECONDS) -> dict | None:
    """D의 open→close bp(basis point)와 거래량 배율(D 거래량 / 자기 자신의 직전
    `TRAILING_VOL_DAYS`일 평균). 데이터가 없으면(비상장·상폐·조회 실패 등) None —
    0으로 위장하지 않는다."""
    start = datetime.combine(d - timedelta(days=TRAILING_VOL_DAYS + 20), datetime.min.time())
    end = datetime.combine(d + timedelta(days=1), datetime.min.time())
    try:
        df = candle_source.fetch(symbol, start, end)
    except Exception:  # noqa: BLE001 — 종목 하나의 조회 실패가 전체 검증을 막지 않는다
        df = None
    finally:
        if sleep_seconds:
            time.sleep(sleep_seconds)

    row = _row_for_date(df, d)
    if row is None or row["open"] is None or row["open"] <= 0:
        return None

    open_close_bp = (row["close"] - row["open"]) / row["open"] * 10000

    prior = df.loc[df.index.date < d]
    trailing = prior.tail(TRAILING_VOL_DAYS)
    vol_multiple = None
    if len(trailing) >= MIN_VOL_BASELINE_DAYS:
        base = trailing["volume"].mean()
        if base and base > 0:
            vol_multiple = row["volume"] / base

    return {"open_close_bp": float(open_close_bp), "volume_multiple": vol_multiple}


# ------------------------------------------------------------------ 지표 산수(순수)

def aggregate_metrics(records: list[dict]) -> dict:
    """레코드(각 {"open_close_bp", "volume_multiple", "excess_bp"}) 리스트를 하나의
    지표 묶음으로 pooling 한다 — 일별 평균의 평균이 아니라 종목-일 전체를 직접
    풀링해 표본 왜곡(일자별 종목 수 차이)을 피한다."""
    n = len(records)
    if n == 0:
        return {
            "n_symbol_days": 0, "hit_rate": None, "avg_open_close_bp": None,
            "vol_multiple_median": None, "excess_vs_anchor_bp": None,
        }
    bps = [r["open_close_bp"] for r in records]
    vol_mults = [r["volume_multiple"] for r in records if r.get("volume_multiple") is not None]
    excess = [r["excess_bp"] for r in records if r.get("excess_bp") is not None]
    return {
        "n_symbol_days": n,
        "hit_rate": sum(1 for bp in bps if bp > 0) / n,
        "avg_open_close_bp": statistics.fmean(bps),
        "vol_multiple_median": statistics.median(vol_mults) if vol_mults else None,
        "excess_vs_anchor_bp": statistics.fmean(excess) if excess else None,
    }


# ------------------------------------------------------------------ 오케스트레이션(I/O)

def _trading_days(candle_source, days: int, today: date) -> list[date]:
    """앵커(069500) 일봉으로 실제 개장일을 판정한다(달력·공휴일표 대신 —
    `quant/analyze/opendays.py`와 같은 사고방식). `today` 미만 최근 `days`일."""
    start = datetime.combine(today - timedelta(days=days * 3 + 10), datetime.min.time())
    end = datetime.combine(today, datetime.min.time())
    try:
        df = candle_source.fetch(ANCHOR_SYMBOL, start, end)
    except Exception:  # noqa: BLE001 — 판정 불가면 빈 리스트(집계 자체를 건너뛴다)
        return []
    finally:
        time.sleep(YF_SLEEP_SECONDS)
    if df is None or df.empty:
        return []
    all_dates = sorted({d for d in df.index.date if d < today})
    return all_dates[-days:] if days else all_dates


def run_verify(root: Path, days: int, top: int, candle_source, today: date | None = None) -> dict:
    """전체 검증 실행. 반환: {"trading_days", "day_rows", "records", "aggregate"}."""
    today = today or date.today()

    mentions_idx = index_mentions(_read_jsonl(root / "data" / "ledger" / MENTIONS_LEDGER))
    frgn_idx = index_frgn_flow(_read_jsonl(root / "data" / "ledger" / FRGN_FLOW_LEDGER))
    disclosures_idx = index_disclosures(_read_jsonl(root / "data" / "ledger" / DISCLOSURES_LEDGER))

    trading_days = _trading_days(candle_source, days, today)

    day_rows: list[dict] = []
    records: list[dict] = []

    for d in trading_days:
        symbols = candidate_symbols(mentions_idx, d)
        if not symbols:
            day_rows.append({"date": d.isoformat(), **aggregate_metrics([])})
            continue

        inputs_by_symbol = {
            sym: reconstruct_inputs(sym, d, mentions_idx, frgn_idx, disclosures_idx) for sym in symbols
        }
        ranked = rank_intraday(inputs_by_symbol, top=top)
        if not ranked:
            day_rows.append({"date": d.isoformat(), **aggregate_metrics([])})
            continue

        anchor_outcome = symbol_outcome(candle_source, ANCHOR_SYMBOL, d)
        anchor_bp = anchor_outcome["open_close_bp"] if anchor_outcome else None

        day_records: list[dict] = []
        for symbol, _score_result in ranked:
            outcome = symbol_outcome(candle_source, symbol, d)
            if outcome is None:
                continue
            rec = {
                "date": d.isoformat(),
                "symbol": symbol,
                "open_close_bp": outcome["open_close_bp"],
                "volume_multiple": outcome["volume_multiple"],
                "excess_bp": (outcome["open_close_bp"] - anchor_bp) if anchor_bp is not None else None,
            }
            day_records.append(rec)

        records.extend(day_records)
        day_rows.append({"date": d.isoformat(), **aggregate_metrics(day_records)})

    return {
        "trading_days": trading_days,
        "day_rows": day_rows,
        "records": records,
        "aggregate": aggregate_metrics(records),
    }


# ------------------------------------------------------------------ 출력 + 원장 기록

def _fmt(v, suffix: str = "", pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:+.1f}{suffix}" if suffix == "bp" else f"{v:.2f}{suffix}"


def format_report(result: dict, days: int, top: int) -> str:
    lines = [
        f"=== 단타 스코어러 재현 검증 (요청 {days}일, top {top}) ===",
        f"표본: 거래일 {len(result['trading_days'])}일 · 종목-일 {len(result['records'])}건",
        "",
        f"{'날짜':<12}{'후보':>6}{'적중률':>10}{'평균bp':>10}{'거래량배율(중앙)':>16}{'초과bp(vs 069500)':>18}",
    ]
    for row in result["day_rows"]:
        lines.append(
            f"{row['date']:<12}{row['n_symbol_days']:>6}"
            f"{_fmt(row['hit_rate'], pct=True):>10}"
            f"{_fmt(row['avg_open_close_bp'], suffix='bp'):>10}"
            f"{_fmt(row['vol_multiple_median']):>16}"
            f"{_fmt(row['excess_vs_anchor_bp'], suffix='bp'):>18}"
        )
    agg = result["aggregate"]
    lines.append("-" * 72)
    lines.append(
        f"{'종합':<12}{agg['n_symbol_days']:>6}"
        f"{_fmt(agg['hit_rate'], pct=True):>10}"
        f"{_fmt(agg['avg_open_close_bp'], suffix='bp'):>10}"
        f"{_fmt(agg['vol_multiple_median']):>16}"
        f"{_fmt(agg['excess_vs_anchor_bp'], suffix='bp'):>18}"
    )
    if agg["n_symbol_days"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(
            f"\n표본 부족(종목-일 n={agg['n_symbol_days']} < {MIN_SAMPLE_FOR_JUDGEMENT}) "
            "— 판단 불가. 이 숫자로 스코어러 정확도를 주장하지 않는다."
        )
    return "\n".join(lines)


def write_ledger(result: dict, root: Path, days: int, top: int, today: date | None = None) -> Path:
    """`data/ledger/intraday_verify.jsonl`에 이번 실행 요약을 append 한다."""
    today = today or date.today()
    path = root / "data" / "ledger" / VERIFY_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": today.isoformat(),
        "params": {"days": days, "top": top},
        "n_trading_days": len(result["trading_days"]),
        "metrics": result["aggregate"],
        "sufficient": result["aggregate"]["n_symbol_days"] >= MIN_SAMPLE_FOR_JUDGEMENT,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intraday_verify")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        candle_source = YFinanceCandleSource("1d")
        result = run_verify(root, args.days, args.top, candle_source)
        print(format_report(result, args.days, args.top))
        ledger_path = write_ledger(result, root, args.days, args.top)
        print(f"\n원장 기록: {ledger_path}")
    except Exception as e:  # noqa: BLE001 — 이 배치는 항상 exit 0 (다른 파이프라인을 막지 않는다)
        print(f"검증 실행 실패: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
