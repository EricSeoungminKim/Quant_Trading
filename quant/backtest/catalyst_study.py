"""촉매 연구 — DART 공시 유형·급등 선행신호·뉴스 키워드의 실측 상관 (서브프로젝트 M).

**질문**: 어떤 뉴스/공시 촉매가 실제로 가격 급등에 *선행*했는가? `quant.analyze.
intraday_score`의 촉매 요인(공시 유형 화이트리스트 `{"수주", "임상·승인", "실적"}`,
테마 등락 +3%p)은 지금 지어낸 초기값이다 — 이 모듈이 실측으로 근거를 만든다.
채점(스코어러)과 검증(이 모듈)을 분리하는 이유는 `quant/backtest/intraday_verify.py`·
`quant/backtest/report_follow.py`와 같다 — "여기가 거짓말하면 그 위에 쌓은 모든
판단이 조용히 틀린다."

실행: `uv run python -m quant.backtest.catalyst_study --days 60 --max-symbols 300`

## 세 갈래 (사용자 원문, 2026-08-17)

- **Part A** (주 표본, n 수천 가능): DART 공시 유형별 **익일** 수익률을 base
  rate(전체 평균)와 비교한다.
- **Part B**: ≥15% 급등한 날을 역추적해 D 또는 D-1에 공시가 있었는지 본다 —
  "급등의 대부분은 무공시(뉴스/테마 재료)"라면 그것도 그대로 보고한다.
- **Part C** (얇지만 정합 확인): 우리 뉴스 창(2026-08-13~)의 제목 키워드 클래스별
  당일 성과. 표본이 작아 표기만 한다.

## look-ahead 경계 (정직하게 문서화 — 숨기지 않는다)

- **주 지표는 익일(다음 거래일)이다.** `rcept_dt`(접수일자)가 D인 공시는 장중에
  올라올 수 있으므로, D 당일 시가→종가는 "공시를 알기 전" 가격이라고 보장할 수
  없다(오염 가능). 그래서 Part A는 **익일 시가→종가 %**를 주 지표로 쓰고, D
  당일 시가→종가는 참고 열(`same_day_open_close_pct`)로만 남긴다 — 판단에
  쓰지 않는다.
- **전일 종가 대비 익일 종가 %**(`close_to_close_pct`)도 같이 계산한다 — D의
  종가가 있을 때만(휴장·데이터 결측이면 None).
- **disclosures.jsonl 커버리지는 백필한 `--days` 창으로 제한된다.** Part B의
  급등 역추적은 이 창을 벗어난 날짜는 "공시 없음"이 아니라 "확인 불가"다 —
  창 경계 밖 급등은 집계에서 제외한다(과대/과소 추정 방지).
- **sector_members.json은 이 저장소에 없다.** 사용자 요구 원문은 그 파일명을
  가정했지만 실제로 존재하는 동등 원장은 `data/ledger/sector_map.json`
  (symbol → 업종명 dict)이다 — 그 `.keys()`를 "섹터 구성종목" 유니버스로 쓴다.
  이 사실을 그대로 밝힌다(파일을 지어내지 않는다).
- **유동성 데이터가 없다.** Part B 유니버스 400 cap은 "유동성 우선"을 문자
  그대로 구현할 수 없어(거래대금 원장 없음) disclosures+mentions 등장 빈도를
  대리 지표로 쓴다 — 실측 빈도가 유동성의 근사치라는 가정이며, 그 가정을 밝힌다.
- **KOSDAQ 커버리지 한계**: `YFinanceCandleSource`는 6자리 숫자 심볼을 전부
  `.KS`(코스피)로만 질의한다(`quant/collect/quotes/yf_source.py`) — 코스닥
  전용 심볼(`.KQ`)은 이 경로로 봉을 못 가져와 `skipped_no_bars`로 잡힌다. 이
  모듈이 새로 만든 문제가 아니라 기존 인프라(`intraday_verify.py`도 동일 소스를
  씀)의 한계를 그대로 물려받은 것 — 실측 결과의 "봉 조회 실패" 카운트로 드러난다.

## 표본 부족을 성과로 위장하지 않는다

`quant-expert`·`intraday_verify.py`·`report_follow.py`와 같은 규율: n 없이
성과를 주장하지 않는다. 유형별 표는 항상 n을 같이 찍고, base rate(전체 평균)
대비로만 "촉매 후보"를 표시한다(`MIN_N_FOR_FLAG` 미만은 후보로 올리지 않는다).
종료 코드는 항상 0 — 이 배치가 실패해도 다른 파이프라인을 막지 않는다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.adapters.env import get_key
from quant.backtest.report_follow import map_to_trading_day
from quant.collect.sources.dart import append_ledger, classify_report, fetch_disclosures

DEFAULT_DAYS = 60
DEFAULT_MAX_SYMBOLS = 300
YF_SLEEP_SECONDS = 0.3
DART_SLEEP_SECONDS = 0.5
TRADING_DAY_BUFFER = 10          # 봉 조회 창 앞뒤 여유(휴장일 감안)
DENSE_DAY_THRESHOLD = 20         # 이 이상 이미 원장에 있으면 그 날은 재조회 skip(휴리스틱)
SECTOR_UNIVERSE_CAP = 400        # Part B 유니버스 상한(사용자 스펙 — "유동성" 대리로 빈도 사용)

MIN_N_FOR_FLAG = 30              # 이 미만 표본은 유형을 촉매 후보로 올리지 않는다
MARGIN_PCT = 0.5                 # base rate 대비 +50bp 이상이어야 "후보"

DISCLOSURE_SURGE_PCT = 5.0       # Part A: 익일 "급등"으로 셀 임계값
DISCLOSURE_LIMIT_PCT = 15.0      # Part A: 익일 "상한가급"으로 셀 임계값
MARKET_SURGE_PCT = 15.0          # Part B: "급등일" 판정 임계값(전일 종가 대비)
MARKET_LIMIT_PCT = 29.5          # Part B: "상한가급"(코스피/코스닥 상한 30% 근접)

DISCLOSURES_LEDGER = "disclosures.jsonl"
MENTIONS_LEDGER = "mentions.jsonl"
SECTOR_MAP_FILE = "sector_map.json"
STUDY_LEDGER = "catalyst_study.jsonl"

# Part C — 제목 키워드 클래스(순서 = 매칭 우선순위). "급등 사후보도"를 맨 앞에 둬서
# 다른 클래스와 겹치더라도 look-ahead 오염 클래스로 먼저 격리한다.
KEYWORD_CLASSES: dict[str, tuple[str, ...]] = {
    "급등 사후보도(오염 클래스)": ("상한가", "급등", "치솟"),
    "수주/공급계약": ("수주", "공급계약"),
    "실적/흑자": ("실적", "흑자전환", "영업이익"),
    "신제품/기술": ("신제품", "신기술", "특허", "출시"),
    "정부/정책": ("정부", "정책", "규제", "지원책"),
    "FDA/임상": ("FDA", "임상", "품목허가"),
    "M&A/지분": ("M&A", "인수", "지분", "합병"),
}


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


def index_disclosures_by_symbol(rows: list[dict]) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for r in rows:
        sym = r.get("stock_code")
        if sym:
            idx.setdefault(sym, []).append(r)
    return idx


def symbol_frequency(disclosures_rows: list[dict], mentions_rows: list[dict]) -> dict[str, int]:
    """DART 공시 + 뉴스 언급의 종목별 등장 빈도. "유동성"을 직접 잴 원장이 없어
    이 빈도를 대리 지표로 쓴다(모듈 docstring에 그 가정을 명시)."""
    freq: dict[str, int] = {}
    for r in disclosures_rows:
        sym = r.get("stock_code")
        if sym:
            freq[sym] = freq.get(sym, 0) + 1
    for r in mentions_rows:
        sym = r.get("symbol")
        if sym:
            freq[sym] = freq.get(sym, 0) + 1
    return freq


def select_symbols_by_frequency(freq: dict[str, int], cap: int) -> list[str]:
    ordered = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sym for sym, _ in ordered[:cap]]


# ------------------------------------------------------------------ 유형 분류(순수)

def type_for(report_nm: str | None) -> str:
    """`classify_report`를 감싸 None을 "기타"로 정직하게 표기(태그가 없다는 뜻을
    "미매칭"으로 조용히 지우지 않는다)."""
    return classify_report(report_nm or "") or "기타"


def classify_title(title: str) -> str | None:
    """제목 키워드 클래스(Part C). `KEYWORD_CLASSES` 순서가 우선순위 — 먼저
    매치되는 클래스가 이긴다. 매치 없으면 None(그 행은 base rate에만 잡힌다)."""
    if not title:
        return None
    for cls, keywords in KEYWORD_CLASSES.items():
        if any(kw in title for kw in keywords):
            return cls
    return None


# ------------------------------------------------------------------ 봉(bar) 구조 산수(순수)

def next_trading_day(bars: dict[date, dict], d: date) -> date | None:
    later = sorted(x for x in bars if x > d)
    return later[0] if later else None


def disclosure_outcome(bars: dict[date, dict], d: date) -> dict | None:
    """D 공시 → 익일(다음 거래일) 결과. 반환 None: 익일 봉이 아직 없다(최근 공시라
    다음 거래일 데이터가 아직 확정되지 않음 — 0으로 위장하지 않는다).

    - `open_close_next_pct`: 익일 시가→종가 %(주 지표, look-ahead 안전).
    - `close_to_close_pct`: D 종가 → 익일 종가 %(전일 종가 대비). D 종가가 없으면 None.
    - `same_day_open_close_pct`: D 당일 시가→종가 %(참고 열 — 장중 공시면 오염 가능,
      판단에 쓰지 않는다).
    """
    nd = next_trading_day(bars, d)
    if nd is None:
        return None
    next_bar = bars[nd]
    if not next_bar.get("open") or next_bar["open"] <= 0:
        return None
    open_close_next_pct = (next_bar["close"] - next_bar["open"]) / next_bar["open"] * 100

    d_bar = bars.get(d)
    close_to_close_pct = None
    same_day_pct = None
    if d_bar is not None and d_bar.get("close") and d_bar["close"] > 0:
        close_to_close_pct = (next_bar["close"] - d_bar["close"]) / d_bar["close"] * 100
        if d_bar.get("open") and d_bar["open"] > 0:
            same_day_pct = (d_bar["close"] - d_bar["open"]) / d_bar["open"] * 100

    return {
        "next_trading_day": nd.isoformat(),
        "open_close_next_pct": open_close_next_pct,
        "close_to_close_pct": close_to_close_pct,
        "same_day_open_close_pct": same_day_pct,
    }


def find_surges(bars: dict[date, dict], threshold_pct: float = MARKET_SURGE_PCT) -> list[dict]:
    """전일 종가 대비 등락률이 `threshold_pct` 이상인 날들. 반환: [{"date","pct"}],
    날짜 오름차순. 연속하지 않는 거래일(원장 결측)은 "전일"로 취급하지 않는다 —
    `bars`에 실제로 존재하는 날짜만 순서대로 비교한다."""
    days = sorted(bars.keys())
    out: list[dict] = []
    for i in range(1, len(days)):
        prev, cur = days[i - 1], days[i]
        prev_close = bars[prev].get("close")
        cur_close = bars[cur].get("close")
        if not prev_close or prev_close <= 0 or cur_close is None:
            continue
        pct = (cur_close - prev_close) / prev_close * 100
        if pct >= threshold_pct:
            out.append({"date": cur, "pct": pct})
    return out


def precursor_types(disclosures_idx: dict[str, list[dict]], symbol: str, d: date) -> list[str]:
    """급등일 D 또는 D-1에 접수된(`rcept_dt`) 공시의 유형들(중복 제거, 등장 순)."""
    rows = disclosures_idx.get(symbol, [])
    targets = {d.strftime("%Y%m%d"), (d - timedelta(days=1)).strftime("%Y%m%d")}
    types: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if r.get("rcept_dt") in targets:
            t = type_for(r.get("report_nm"))
            if t not in seen:
                seen.add(t)
                types.append(t)
    return types


# ------------------------------------------------------------------ 집계(순수)

def aggregate_outcomes(rows: list[dict], metric: str = "open_close_next_pct") -> dict:
    vals = [r[metric] for r in rows if r.get(metric) is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "avg_pct": None, "hit_rate": None, "surge_rate": None, "limit_rate": None}
    return {
        "n": n,
        "avg_pct": statistics.fmean(vals),
        "hit_rate": sum(1 for v in vals if v > 0) / n,
        "surge_rate": sum(1 for v in vals if v >= DISCLOSURE_SURGE_PCT) / n,
        "limit_rate": sum(1 for v in vals if v >= DISCLOSURE_LIMIT_PCT) / n,
    }


BASE_RATE_KEY = "전체(기준)"


def aggregate_by_type(rows: list[dict]) -> dict[str, dict]:
    """`rows`(각 {"type", "open_close_next_pct", ...}) → 유형별 집계 + base rate
    (`BASE_RATE_KEY` — 표본 전체)."""
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    result = {t: aggregate_outcomes(rs) for t, rs in by_type.items()}
    result[BASE_RATE_KEY] = aggregate_outcomes(rows)
    return result


def flag_candidates(
    type_stats: dict[str, dict],
    base_key: str = BASE_RATE_KEY,
    min_n: int = MIN_N_FOR_FLAG,
    margin_pct: float = MARGIN_PCT,
) -> tuple[list[str], list[str]]:
    """n≥min_n 이고 base rate 대비 ±margin_pct 를 넘는 유형만 후보로 올린다.
    반환: (긍정 후보 유형들, 부정 후보 유형들) — 표본 부족은 아예 후보에서 뺀다."""
    base = type_stats.get(base_key)
    if base is None or base["avg_pct"] is None:
        return [], []
    positive, negative = [], []
    for t, stats in type_stats.items():
        if t == base_key or stats["n"] < min_n or stats["avg_pct"] is None:
            continue
        if stats["avg_pct"] >= base["avg_pct"] + margin_pct:
            positive.append(t)
        elif stats["avg_pct"] <= base["avg_pct"] - margin_pct:
            negative.append(t)
    return positive, negative


# ------------------------------------------------------------------ DART 백필(I/O, 네트워크)

def backfill_disclosures(
    root: Path,
    days: int,
    today: date,
    api_key: str | None,
    getter=None,
    sleep_seconds: float = DART_SLEEP_SECONDS,
) -> dict:
    """최근 `days`일(오늘 제외, D-days ~ D-1)을 하루씩 조회해 `disclosures.jsonl`에
    append한다. 하루 = `bgn_de==end_de`로 좁혀 10페이지 상한을 우회한다(모듈
    docstring). 이미 `DENSE_DAY_THRESHOLD`건 이상 원장에 있는 날은 재조회하지
    않는다(호출 예산 절약 — `append_ledger`가 `rcept_no` 기준 idempotent라
    재조회해도 안전은 하지만, 매번 10페이지×2 corp_cls를 도는 건 낭비다)."""
    path = root / "data" / "ledger" / DISCLOSURES_LEDGER
    existing = _read_jsonl(path)
    counts_by_day: dict[str, int] = {}
    for r in existing:
        rcept_dt = r.get("rcept_dt")
        if rcept_dt:
            counts_by_day[rcept_dt] = counts_by_day.get(rcept_dt, 0) + 1

    fetched_days: list[str] = []
    skipped_days: list[str] = []
    added_total = 0
    errors: list[str] = []

    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y%m%d")
        if counts_by_day.get(d_str, 0) >= DENSE_DAY_THRESHOLD:
            skipped_days.append(d_str)
            continue
        rows, err = fetch_disclosures(d_str, d_str, api_key=api_key, getter=getter)
        if err:
            errors.append(f"{d_str}: {err}")
        added = append_ledger(rows, path)
        added_total += added
        fetched_days.append(d_str)
        if sleep_seconds:
            time.sleep(sleep_seconds)

    return {
        "fetched_days": fetched_days,
        "skipped_days": skipped_days,
        "added_total": added_total,
        "errors": errors,
    }


# ------------------------------------------------------------------ 봉 캐시(I/O, 네트워크는 candle_source가 담당)

def fetch_daily_bars(
    candle_source, symbol: str, start: datetime, end: datetime, sleep_seconds: float = YF_SLEEP_SECONDS,
) -> dict[date, dict] | None:
    """종목 하나의 일봉을 {date: {open, close}} 로 캐시. 실패·빈 결과는 None
    (0으로 위장하지 않는다 — 호출자가 "실패 카운트"로 집계)."""
    try:
        df = candle_source.fetch(symbol, start, end)
    except Exception:  # noqa: BLE001 — 종목 하나의 실패가 전체 연구를 막지 않는다
        df = None
    finally:
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if df is None or df.empty:
        return None

    out: dict[date, dict] = {}
    for ts, row in df.iterrows():
        o, c = row["open"], row["close"]
        if o is None or o <= 0:
            continue
        out[ts.date()] = {"open": float(o), "close": float(c)}
    return out if out else None


def build_bar_cache(
    candle_source, symbols: list[str], start: datetime, end: datetime, sleep_seconds: float = YF_SLEEP_SECONDS,
) -> tuple[dict[str, dict[date, dict]], list[str]]:
    cache: dict[str, dict[date, dict]] = {}
    failed: list[str] = []
    for sym in symbols:
        bars = fetch_daily_bars(candle_source, sym, start, end, sleep_seconds)
        if bars is None:
            failed.append(sym)
        else:
            cache[sym] = bars
    return cache, failed


# ------------------------------------------------------------------ Part A/B/C 오케스트레이션(순수 — bar_cache는 인자로 받는다)

def run_part_a(disclosures_rows: list[dict], bar_cache: dict[str, dict[date, dict]]) -> dict:
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
            "type": type_for(r.get("report_nm")), "symbol": sym, "date": d.isoformat(), **outcome,
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


def run_part_b(
    disclosures_rows: list[dict],
    bar_cache: dict[str, dict[date, dict]],
    window_start: date,
    window_end: date,
) -> dict:
    """`window_start`~`window_end`(disclosures 커버리지 창) 안의 급등일만 집계한다
    — 창 밖은 "공시 없었음"이 아니라 "확인 불가"이므로 뺀다(coverage 경계 정직화)."""
    disclosures_idx = index_disclosures_by_symbol(disclosures_rows)
    surge_records: list[dict] = []
    for sym, bars in bar_cache.items():
        for surge in find_surges(bars, MARKET_SURGE_PCT):
            d = surge["date"]
            if not (window_start <= d <= window_end):
                continue
            types = precursor_types(disclosures_idx, sym, d)
            surge_records.append({
                "symbol": sym,
                "date": d.isoformat(),
                "pct": surge["pct"],
                "is_limit_like": surge["pct"] >= MARKET_LIMIT_PCT,
                "disclosure_types": types,
                "has_precursor": bool(types),
            })

    n = len(surge_records)
    n_with_precursor = sum(1 for r in surge_records if r["has_precursor"])
    n_limit_like = sum(1 for r in surge_records if r["is_limit_like"])
    type_counter: dict[str, int] = {}
    for r in surge_records:
        for t in r["disclosure_types"]:
            type_counter[t] = type_counter.get(t, 0) + 1

    return {
        "records": surge_records,
        "n": n,
        "n_limit_like": n_limit_like,
        "n_with_precursor": n_with_precursor,
        "precursor_rate": (n_with_precursor / n) if n else None,
        "type_distribution": type_counter,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
    }


def trading_days_from_bar_cache(bar_cache: dict[str, dict[date, dict]]) -> list[date]:
    """모든 종목 봉의 날짜 합집합 — 달력/공휴일표 대신 실제 체결된 거래일 집합을
    이렇게 재구성한다(`quant/analyze/opendays.py`와 같은 사고)."""
    days: set[date] = set()
    for bars in bar_cache.values():
        days.update(bars.keys())
    return sorted(days)


def run_part_c(
    mentions_rows: list[dict], bar_cache: dict[str, dict[date, dict]], trading_days: list[date] | None = None,
) -> dict:
    """제목 키워드 클래스별 **적용거래일** 시가→종가 %. `mentions.jsonl`의 `date`는
    크롤 실행일이지 거래일이 아니다(`intraday_verify.py` 모듈 지침과 동일 경계) —
    크롤일이 휴장일이면 `report_follow.map_to_trading_day`와 같은 carryover 로
    다음 거래일에 적용한다(실측: 이 원장의 유일한 크롤일 2026-08-15가 토요일이라
    이 매핑 없이는 Part C가 항상 n=0이었다). `bar_cache`에 해당 종목·적용거래일
    봉이 있어야 계산된다 — 없으면 그 행은 건너뛴다(0으로 위장하지 않는다)."""
    trading_days = trading_days or []
    class_vals: dict[str, list[float]] = {}
    base_vals: list[float] = []

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
        base_vals.append(pct)
        cls = classify_title(title)
        if cls:
            class_vals.setdefault(cls, []).append(pct)

    def _stats(vals: list[float]) -> dict:
        n = len(vals)
        if n == 0:
            return {"n": 0, "avg_pct": None, "hit_rate": None}
        return {"n": n, "avg_pct": statistics.fmean(vals), "hit_rate": sum(1 for v in vals if v > 0) / n}

    result = {cls: _stats(vals) for cls, vals in class_vals.items()}
    result[BASE_RATE_KEY] = _stats(base_vals)
    return result


# ------------------------------------------------------------------ 전체 오케스트레이션(I/O)

def run_study(
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

    sector_map_path = root / "data" / "ledger" / SECTOR_MAP_FILE
    sector_symbols: list[str] = []
    if sector_map_path.exists():
        try:
            sector_map = json.loads(sector_map_path.read_text(encoding="utf-8"))
            if isinstance(sector_map, dict):
                sector_symbols = list(sector_map.keys())
        except ValueError:
            sector_symbols = []

    freq = symbol_frequency(disclosures_rows, mentions_rows)
    part_a_symbols = select_symbols_by_frequency(freq, max_symbols)

    disclosure_symbols = {r["stock_code"] for r in disclosures_rows if r.get("stock_code")}
    part_b_universe = sorted(disclosure_symbols | set(sector_symbols))
    part_b_universe_ranked = select_symbols_by_frequency(
        {s: freq.get(s, 0) for s in part_b_universe}, SECTOR_UNIVERSE_CAP,
    )

    symbols_to_fetch = sorted(set(part_a_symbols) | set(part_b_universe_ranked))

    window_start = today - timedelta(days=days)
    window_end = today
    fetch_start = datetime.combine(window_start - timedelta(days=TRADING_DAY_BUFFER), datetime.min.time())
    fetch_end = datetime.combine(window_end + timedelta(days=1), datetime.min.time())

    bar_cache, failed_symbols = build_bar_cache(candle_source, symbols_to_fetch, fetch_start, fetch_end)

    trading_days = trading_days_from_bar_cache(bar_cache)

    part_a = run_part_a(disclosures_rows, bar_cache)
    part_b = run_part_b(disclosures_rows, bar_cache, window_start, window_end)
    part_c = run_part_c(mentions_rows, bar_cache, trading_days)

    return {
        "params": {"days": days, "max_symbols": max_symbols, "today": today.isoformat()},
        "backfill": backfill,
        "n_symbols_fetched": len(bar_cache),
        "n_symbols_failed": len(failed_symbols),
        "failed_symbols_sample": failed_symbols[:20],
        "part_a": part_a,
        "part_b": part_b,
        "part_c": part_c,
    }


# ------------------------------------------------------------------ 출력 + 원장 기록

def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _fmt_val(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.2f}%"


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("=== 촉매 연구 (DART 공시 · 급등 역추적 · 뉴스 키워드) ===")
    p = result["params"]
    lines.append(f"기간: 최근 {p['days']}일 · 종목 상한 {p['max_symbols']} · 기준일 {p['today']}")

    bf = result["backfill"]
    lines.append(
        f"DART 백필: 조회 {len(bf['fetched_days'])}일 · 스킵(이미 밀집) {len(bf['skipped_days'])}일 · "
        f"신규 {bf['added_total']}건 · 에러 {len(bf['errors'])}건"
    )
    lines.append(
        f"봉 캐시: 성공 {result['n_symbols_fetched']}종목 · 실패 {result['n_symbols_failed']}종목"
    )

    # --- Part A ---
    pa = result["part_a"]
    lines.append("")
    lines.append("--- Part A: DART 공시 유형별 익일(익일 시가→종가) 수익률 ---")
    lines.append(
        f"공시 원장 n={pa['n_disclosures']} · 봉 없어 skip {pa['skipped_no_bars']} · "
        f"익일 데이터 없어 skip {pa['skipped_no_outcome']}"
    )
    lines.append(f"{'유형':<20}{'n':>8}{'평균%':>10}{'승률':>10}{'급등율(≥5%)':>14}{'상한가급(≥15%)':>16}")
    ts = pa["type_stats"]
    ordered_types = sorted((t for t in ts if t != BASE_RATE_KEY), key=lambda t: -(ts[t]["n"]))
    for t in [*ordered_types, BASE_RATE_KEY]:
        s = ts[t]
        lines.append(
            f"{t:<20}{s['n']:>8}{_fmt_val(s['avg_pct']):>10}{_fmt_pct(s['hit_rate']):>10}"
            f"{_fmt_pct(s['surge_rate']):>14}{_fmt_pct(s['limit_rate']):>16}"
        )
    lines.append(
        f"긍정 후보(n≥{MIN_N_FOR_FLAG}, base+{MARGIN_PCT}%p 이상): {pa['positive_flags'] or '없음'}"
    )
    lines.append(
        f"부정 후보(n≥{MIN_N_FOR_FLAG}, base-{MARGIN_PCT}%p 이하): {pa['negative_flags'] or '없음'}"
    )

    # --- Part B ---
    pb = result["part_b"]
    lines.append("")
    lines.append("--- Part B: 급등(전일 종가 대비 ≥15%) 역추적 — 공시 선행 여부 ---")
    lines.append(f"창: {pb['window']['start']} ~ {pb['window']['end']} (disclosures 커버리지 경계 내)")
    lines.append(
        f"급등 n={pb['n']}(상한가급 n={pb['n_limit_like']}) · "
        f"공시 선행 {pb['n_with_precursor']}건({_fmt_pct(pb['precursor_rate'])})"
    )
    if pb["type_distribution"]:
        dist = ", ".join(f"{t}:{c}" for t, c in sorted(pb["type_distribution"].items(), key=lambda kv: -kv[1]))
        lines.append(f"선행 공시 유형 분포: {dist}")
    else:
        lines.append("선행 공시 유형 분포: 없음(급등의 대부분이 무공시 재료)")

    # --- Part C ---
    pc = result["part_c"]
    lines.append("")
    lines.append("--- Part C: 뉴스 제목 키워드 클래스별 당일 시가→종가 % (표본 작음 — 표기만) ---")
    lines.append(f"{'클래스':<26}{'n':>8}{'평균%':>10}{'승률':>10}")
    ordered_c = sorted((c for c in pc if c != BASE_RATE_KEY), key=lambda c: -(pc[c]["n"]))
    for c in [*ordered_c, BASE_RATE_KEY]:
        s = pc[c]
        lines.append(f"{c:<26}{s['n']:>8}{_fmt_val(s['avg_pct']):>10}{_fmt_pct(s['hit_rate']):>10}")

    return "\n".join(lines)


def write_ledger(result: dict, root: Path, today: date | None = None) -> Path:
    """`data/ledger/catalyst_study.jsonl`에 이번 실행 요약을 append."""
    today = today or date.today()
    path = root / "data" / "ledger" / STUDY_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    pa, pb = result["part_a"], result["part_b"]
    row = {
        "date": today.isoformat(),
        "params": result["params"],
        "part_a_base": pa["type_stats"].get(BASE_RATE_KEY),
        "part_a_positive_flags": pa["positive_flags"],
        "part_a_negative_flags": pa["negative_flags"],
        "part_b_n_surges": pb["n"],
        "part_b_precursor_rate": pb["precursor_rate"],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalyst_study")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        candle_source = YFinanceCandleSource("1d")
        api_key = get_key("DART_API_KEY")
        result = run_study(root, args.days, args.max_symbols, candle_source, dart_api_key=api_key)
        print(format_report(result))
        ledger_path = write_ledger(result, root)
        print(f"\n원장 기록: {ledger_path}")
    except Exception as e:  # noqa: BLE001 — 이 배치는 항상 exit 0 (다른 파이프라인을 막지 않는다)
        print(f"촉매 연구 실행 실패: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
