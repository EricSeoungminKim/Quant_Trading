"""자금 흐름 해석 — 유가·금리·환율·VIX 매크로 시계열(`data/ledger/macro_rates.jsonl`,
`quant/adapters/macro/fred.py`가 적재)을 결정론 규칙으로 읽어 "큰돈이 어디로
쏠리는지"를 판정한다 (2026-08-29 소유자 지시: "유가·금리·원자재·지수의 숫자
흐름으로 큰손들의 돈이 국채로 쏠릴지, 주식으로 갈지, 어디 섹터로 갈지 ... 읽어서
리포트와 종목·섹터 선정에 녹여라").

**순수 함수만** — 네트워크·LLM·디스크 쓰기 없음(`quant.analyze.us_kr_bridge`와
같은 계약). 유일한 파일 I/O는 `load_ledger`의 읽기 하나뿐이고, 나머지는 그
결과를 받아 계산만 한다. `quant/analyze/` → `quant/adapters/` 임포트는
`tests/test_architecture.py`가 금지하므로(FORBIDDEN 표), 원장 기본 경로
문자열은 `quant.adapters.macro.fred.DEFAULT_LEDGER_PATH`와 값을 맞춰 이 파일
안에 따로 둔다 — 호출부(`quant/report/collect/`)가 진짜 경로를 넘긴다.

이 판정이 수익을 낸다는 증거는 없다(`watch_scorer.py` 모듈 docstring과 같은
계약) — 목적은 숫자 근거 없는 "감"을 걷어내는 것이지 알파 발굴이 아니다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from quant.analyze.briefing import VIX_CALM, VIX_STRESS
from quant.collect.sources.fred import curve_label

# quant/adapters/macro/fred.py DEFAULT_LEDGER_PATH 와 값이 같아야 한다(analyze는
# adapters를 임포트할 수 없어 문자열을 따로 든다 — 값이 갈리면
# tests/test_money_flow.py가 잡는다).
DEFAULT_LEDGER_PATH = "data/ledger/macro_rates.jsonl"

_SERIES_LABELS: dict[str, str] = {
    "us_10y": "미국 10년물",
    "us_2y": "미국 2년물",
    "term_spread_10y2y": "10년-2년 스프레드",
    "vix": "VIX",
    "dollar_index": "달러 인덱스",
    "usdkrw": "원/달러",
    "oil_wti": "WTI 유가",
}
ALL_SERIES = tuple(_SERIES_LABELS.keys())

# 방향 판정 임계값(라운드 숫자 — 과최적화 회피):
_RATE_5D_THRESHOLD = 0.05     # %p, 국채금리 5일 변화
_EQUITY_THRESHOLD = 0.3       # %, 지수 당일 등락
_DOLLAR_5D_PCT_THRESHOLD = 1.0  # %, 달러 인덱스 5일 변화율
_OIL_5D_PCT_THRESHOLD = 3.0     # %, WTI 5일 변화율


@dataclass
class SeriesSnapshot:
    series: str
    label: str
    date: str
    value: float
    chg_1d: float | None
    chg_5d: float | None
    chg_20d: float | None
    direction_5d: str  # "↑" | "↓" | "→"


def load_ledger(path: str | Path) -> dict[str, list[tuple[str, float]]]:
    """`macro_rates.jsonl` → `{series: [(날짜, 값), ...]}` (시리즈별 날짜 오름차순).

    발표 주기가 다른 시리즈(예: usdkrw는 매영업일, term_spread는 국채 두 개가
    다 있어야 계산됨)를 같은 리스트에 억지로 맞추지 않는다 — 시리즈 각자의
    관측일만 모은다. 정합(같은 날짜인지)이 필요한 지점은 호출부가
    `SeriesSnapshot.date`를 비교해서 판단한다.

    파일이 없거나 줄이 깨지면(JSON 파싱 실패) 그 줄만 건너뛴다 — 원장 전체를
    버리지 않는다(`quant.adapters.macro.fred.append_macro_rows`와 같은 관례).
    """
    p = Path(path)
    if not p.exists():
        return {}
    by_series: dict[str, dict[str, float]] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        series, date, value = row.get("series"), row.get("date"), row.get("value")
        if not series or not date or value is None:
            continue
        try:
            by_series.setdefault(series, {})[date] = float(value)
        except (TypeError, ValueError):
            continue
    return {s: sorted(v.items()) for s, v in by_series.items()}


def _direction(chg: float | None, threshold: float = 0.0) -> str:
    if chg is None:
        return "→"
    if chg > threshold:
        return "↑"
    if chg < -threshold:
        return "↓"
    return "→"


def series_snapshot(series: str, rows: list[tuple[str, float]]) -> SeriesSnapshot | None:
    """단일 시리즈의 (날짜, 값) 오름차순 리스트 → 최신값 + 1/5/20 **관측치** 전
    대비 변화. 달력일이 아니라 그 시리즈 자신의 관측치 개수를 센다 — 휴장일이
    섞여 있어도(예: 국채는 미 공휴일, usdkrw는 한미 양쪽 휴일) 시리즈 내부에서는
    항상 정합이 맞는다. 관측치가 모자라면(신규 시리즈 등) 해당 구간은 `None`."""
    if not rows:
        return None
    dates = [d for d, _ in rows]
    values = [v for _, v in rows]
    latest_date, latest_value = dates[-1], values[-1]

    def _chg(n: int) -> float | None:
        if len(values) <= n:
            return None
        return round(latest_value - values[-1 - n], 4)

    chg_1d, chg_5d, chg_20d = _chg(1), _chg(5), _chg(20)
    return SeriesSnapshot(
        series=series, label=_SERIES_LABELS.get(series, series), date=latest_date,
        value=latest_value, chg_1d=chg_1d, chg_5d=chg_5d, chg_20d=chg_20d,
        direction_5d=_direction(chg_5d),
    )


def build_snapshots(
    ledger_path: str | Path, series_names: tuple[str, ...] = ALL_SERIES,
) -> dict[str, SeriesSnapshot]:
    """원장 경로 → `{series: SeriesSnapshot}`. 값이 하나도 없는 시리즈는
    빠진다(0으로 위장하지 않는다)."""
    by_series = load_ledger(ledger_path)
    out: dict[str, SeriesSnapshot] = {}
    for name in series_names:
        snap = series_snapshot(name, by_series.get(name, []))
        if snap is not None:
            out[name] = snap
    return out


# --------------------------------------------------------------------- 자금 흐름 판정

def judge_money_flow(
    snapshots: dict[str, SeriesSnapshot],
    equity_change_pct: float | None = None,
    equity_label: str = "지수",
) -> dict:
    """금리(미국 10년물, 5일 변화)와 주가(당일 등락) 방향의 조합으로 "돈이 어디로
    가는가"를 판정한다.

    네 규칙 다 교과서적 cross-asset 관계다(과최적화 회피 — 표본 검증 없이 방향성
    관계만 쓴다):
    - **금리↑ + 주가↓**: 할인율 부담이 채권가·주가를 동시에 누른다(금리 상승
      자체가 곧 채권가 하락이므로 "채권에서도 이탈"은 별도 계산이 필요 없다) →
      "긴축 부담 — 채권·주식 동반 이탈".
    - **금리↓ + 주가↑**: 조달비용 완화 기대가 위험자산으로 흐른다 →
      "위험자산 선호(리스크온)".
    - **금리↑ + 주가↑**: 금리가 오르는데도 주가가 버틴다 — 성장 기대가 할인율
      부담을 상쇄한다고 읽는다 → "성장 기대 우위(경기 낙관)".
    - **금리↓ + 주가↓**: 금리가 내려가는데도 주가가 밀린다 — 경기 둔화 공포가
      안전자산(채권) 선호로 이어진다고 읽는다 → "경기 둔화 우려(안전자산 선호)".

    `equity_change_pct`가 없으면(호출부가 지수 시세를 못 구함) 주가축 판정을
    비우고 금리 방향만 보고한다 — 지어내지 않는다.

    반환: `{"label": str, "reasons": [숫자 인용 문자열, ...], "rate_direction": str,
    "equity_direction": str | None}`."""
    us10y = snapshots.get("us_10y")
    rate_dir = _direction(us10y.chg_5d if us10y else None, _RATE_5D_THRESHOLD)
    equity_dir = _direction(equity_change_pct, _EQUITY_THRESHOLD) if equity_change_pct is not None else None

    reasons: list[str] = []
    if us10y is not None:
        reasons.append(
            f"미국 10년물 5일 변화 {us10y.chg_5d:+.2f}%p"
            if us10y.chg_5d is not None else "미국 10년물 5일 변화 데이터 부족"
        )
    else:
        reasons.append("미국 10년물 데이터 없음")
    if equity_change_pct is not None:
        reasons.append(f"{equity_label} 당일 {equity_change_pct:+.2f}%")

    if equity_dir is None:
        label = f"금리 {rate_dir} (지수 데이터 없어 자금 흐름 판정 보류)"
        return {"label": label, "reasons": reasons, "rate_direction": rate_dir, "equity_direction": None}

    if rate_dir == "↑" and equity_dir == "↓":
        label = "긴축 부담 — 채권·주식 동반 이탈"
    elif rate_dir == "↓" and equity_dir == "↑":
        label = "위험자산 선호(리스크온)"
    elif rate_dir == "↑" and equity_dir == "↑":
        label = "성장 기대 우위(경기 낙관)"
    elif rate_dir == "↓" and equity_dir == "↓":
        label = "경기 둔화 우려(안전자산 선호)"
    else:
        label = "방향 불명확 — 금리·지수 모두 중립권"

    return {"label": label, "reasons": reasons, "rate_direction": rate_dir, "equity_direction": equity_dir}


def judge_cash_flow(snapshots: dict[str, SeriesSnapshot]) -> dict:
    """VIX·달러 인덱스·10-2년 스프레드로 "현금이 죽었나 살았나" 한 줄 판정.

    - **VIX**: `VIX_CALM`(15) 미만이면 안정 — 위험자산에 현금이 머문다.
      `VIX_STRESS`(22) 이상이면 스트레스 — 현금이 사이드라인으로 도피(방어).
      임계값은 `quant.analyze.briefing`이 이미 리포트 전역에서 쓰는 값을 그대로
      재사용한다(새 임계값을 만들지 않는다).
    - **달러 인덱스** 5일 변화율 ±1%: 달러 강세는 글로벌 달러 유동성을 흡수해
      신흥국·위험자산에서 자금이 빠지는 압력으로 읽는다(교과서적 역상관).
    - **10년-2년 스프레드**: `quant.collect.sources.fred.curve_label`(기존 함수
      재사용)로 역전/평탄/정상 판정 — 역전은 장기 자금이 안전자산으로 도피
      중이라는 신호로 읽는다.

    반환: `{"label": str, "reasons": [...]}."""
    reasons: list[str] = []
    parts: list[str] = []

    vix = snapshots.get("vix")
    if vix is not None:
        if vix.value < VIX_CALM:
            parts.append("VIX 안정 — 현금이 위험자산에 머문다")
        elif vix.value >= VIX_STRESS:
            parts.append("VIX 스트레스 — 현금이 사이드라인으로 도피")
        else:
            parts.append("VIX 중립")
        reasons.append(f"VIX {vix.value:.1f} (안정<{VIX_CALM:g}, 스트레스≥{VIX_STRESS:g})")

    dollar = snapshots.get("dollar_index")
    if dollar is not None and dollar.chg_5d is not None:
        dollar_pct = (dollar.chg_5d / (dollar.value - dollar.chg_5d) * 100) if (dollar.value - dollar.chg_5d) else 0.0
        if dollar_pct >= _DOLLAR_5D_PCT_THRESHOLD:
            parts.append("달러 강세 — 글로벌 유동성 흡수 압력")
        elif dollar_pct <= -_DOLLAR_5D_PCT_THRESHOLD:
            parts.append("달러 약세 — 글로벌 유동성 완화")
        reasons.append(f"달러 인덱스 5일 {dollar_pct:+.2f}%")

    spread = snapshots.get("term_spread_10y2y")
    if spread is not None:
        label = curve_label(spread.value)
        if label == "역전":
            parts.append("장단기 금리 역전 — 안전자산 도피 신호")
        reasons.append(f"10년-2년 스프레드 {spread.value:+.2f}%p ({label})")

    if not parts:
        return {"label": "판정 불가 — 매크로 데이터 부족", "reasons": reasons}
    return {"label": " · ".join(parts), "reasons": reasons}


# --------------------------------------------------------------------- 섹터 기울기

# 드라이버별 섹터 기울기 — 유가는 FRED oil_wti(WTI 5일 변화율), 금리는 us_10y(5일
# %p 변화), 달러는 dollar_index(5일 변화율)로 판단한다. KR은 네이버 업종명
# (`data/ledger/sector_map.json`의 값, `quant/analyze/us_kr_bridge.py` docstring이
# 확인한 "GICS 산업 분류의 한국어판" 그대로 — 새 taxonomy를 만들지 않는다), US는
# GICS 섹터 ETF 라벨(표시용 — 종목 단위 매칭은 아래 §4 docstring 참고).
#
# 점수는 -2..+2. 방향이 반대(예: 유가 하락)면 부호를 뒤집어 그대로 쓴다
# (`_flip_tilt`) — 정유가 유가 상승에 웃는다면 하락에는 그만큼 운다는 게
# 교과서적 대칭 가정이라 별도 표를 손으로 두 벌 관리하지 않는다.
_OIL_TILT_UP: dict[str, list[dict]] = {
    "KR": [
        {"sector": "석유와가스", "score": 2, "why": "정제마진 확대 — 원유는 이미 보유, 판가만 오른다"},
        {"sector": "조선", "score": 1, "why": "산유국 해양플랜트·LNG선 발주 기대"},
        {"sector": "항공사", "score": -2, "why": "항공유가 총원가의 20~30%를 차지 — 직접 비용 압박"},
        {"sector": "해운사", "score": -1, "why": "선박연료(벙커C유) 비용 상승"},
    ],
    "US": [
        {"sector": "XLE(에너지)", "score": 2, "why": "정유·E&P 업종 마진 확대"},
        {"sector": "항공(운송)", "score": -2, "why": "항공유 비용 압박"},
    ],
}
_RATE_TILT_UP: dict[str, list[dict]] = {
    "KR": [
        {"sector": "은행", "score": 2, "why": "예대마진 확대"},
        {"sector": "손해보험", "score": 1, "why": "운용자산 이자수익 증가"},
        {"sector": "생명보험", "score": 1, "why": "운용자산 이자수익 증가"},
        {"sector": "반도체와반도체장비", "score": -1, "why": "성장주 — 미래 현금흐름 할인율 부담"},
    ],
    "US": [
        {"sector": "XLF(금융)", "score": 2, "why": "예대마진 확대"},
        {"sector": "XLRE(리츠)", "score": -2, "why": "차입비용 상승 + 배당 매력 저하"},
        {"sector": "XLK(기술/성장주)", "score": -1, "why": "미래 현금흐름 할인율 부담"},
    ],
}
_DOLLAR_TILT_UP: dict[str, list[dict]] = {
    "KR": [
        {"sector": "자동차", "score": 1, "why": "원화 약세 — 수출 채산성 개선(환헤지 완충 감안 절반 가중)"},
        {"sector": "반도체와반도체장비", "score": 1, "why": "원화 약세 — 수출 채산성 개선"},
    ],
    "US": [],
}
_SECTOR_TILT_DRIVERS: dict[str, dict[str, list[dict]]] = {
    "oil": _OIL_TILT_UP, "rate": _RATE_TILT_UP, "dollar": _DOLLAR_TILT_UP,
}


def _flip_tilt(rules: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {
        market: [{**r, "score": -r["score"]} for r in rows]
        for market, rows in rules.items()
    }


def sector_tilt(snapshots: dict[str, SeriesSnapshot]) -> dict[str, dict[str, dict]]:
    """매크로 방향(유가/금리/달러, 5일 변화 기준 임계값 통과분만) → 섹터 기울기.

    반환: `{market: {sector: {"score": int(-2..2), "why": [근거 문자열, ...]}}}`.
    한 섹터가 여러 드라이버에 동시에 걸리면 점수를 합산하고 -2..2로 자른다 —
    근거 문자열은 전부 남긴다(합산 과정을 숨기지 않는다).
    """
    active: list[dict[str, list[dict]]] = []

    oil = snapshots.get("oil_wti")
    if oil is not None and oil.chg_5d is not None and oil.value:
        prev = oil.value - oil.chg_5d
        pct = (oil.chg_5d / prev * 100) if prev else 0.0
        if pct >= _OIL_5D_PCT_THRESHOLD:
            active.append(_OIL_TILT_UP)
        elif pct <= -_OIL_5D_PCT_THRESHOLD:
            active.append(_flip_tilt(_OIL_TILT_UP))

    us10y = snapshots.get("us_10y")
    if us10y is not None and us10y.chg_5d is not None:
        if us10y.chg_5d >= _RATE_5D_THRESHOLD:
            active.append(_RATE_TILT_UP)
        elif us10y.chg_5d <= -_RATE_5D_THRESHOLD:
            active.append(_flip_tilt(_RATE_TILT_UP))

    dollar = snapshots.get("dollar_index")
    if dollar is not None and dollar.chg_5d is not None and dollar.value:
        prev = dollar.value - dollar.chg_5d
        pct = (dollar.chg_5d / prev * 100) if prev else 0.0
        if pct >= _DOLLAR_5D_PCT_THRESHOLD:
            active.append(_DOLLAR_TILT_UP)
        elif pct <= -_DOLLAR_5D_PCT_THRESHOLD:
            active.append(_flip_tilt(_DOLLAR_TILT_UP))

    out: dict[str, dict[str, dict]] = {}
    for rules in active:
        for market, rows in rules.items():
            bucket = out.setdefault(market, {})
            for r in rows:
                entry = bucket.setdefault(r["sector"], {"score": 0, "why": []})
                entry["score"] = max(-2, min(2, entry["score"] + r["score"]))
                entry["why"].append(r["why"])
    return out


# --------------------------------------------------------------------- 종합 + 서술 폴백

def analyze_money_flow(
    ledger_path: str | Path,
    equity_change_pct: float | None = None,
    equity_label: str = "지수",
) -> dict:
    """리포트 §"돈의 흐름" 섹션이 쓰는 종합 결과 하나.

    반환: `{"series": {name: SeriesSnapshot}, "flow": judge_money_flow 결과,
    "cash": judge_cash_flow 결과, "sector_tilt": sector_tilt 결과}`."""
    snapshots = build_snapshots(ledger_path)
    return {
        "series": snapshots,
        "flow": judge_money_flow(snapshots, equity_change_pct, equity_label),
        "cash": judge_cash_flow(snapshots),
        "sector_tilt": sector_tilt(snapshots),
    }


def format_money_flow_text(result: dict) -> str:
    """LLM 산문이 실패했을 때 쓰는 결정론 폴백 문장(§3 "LLM 실패 시 결정론
    문장으로 폴백" — 이 리포트 파이프라인의 무자격증명 안전망 관례). 숫자를
    반드시 인용한다."""
    flow = result.get("flow") or {}
    cash = result.get("cash") or {}
    parts = [flow.get("label", "")]
    if flow.get("reasons"):
        parts.append("(" + ", ".join(flow["reasons"]) + ")")
    parts.append("· " + cash.get("label", ""))
    return " ".join(p for p in parts if p)


def sector_tilt_for_symbol(symbol_sector: str | None, tilt_kr: dict[str, dict]) -> tuple[int, str] | None:
    """§4(watch_scorer) 연동 헬퍼 — KR 종목의 네이버 업종명(`sector_map.json`
    값)을 `sector_tilt()["KR"]`과 매칭한다. 섹터를 모르면(`symbol_sector`가
    `None`이거나 표에 없으면) `None` — 호출부가 0점(불이익 없음)으로 취급한다."""
    if not symbol_sector:
        return None
    entry = tilt_kr.get(symbol_sector)
    if entry is None:
        return None
    return entry["score"], " · ".join(entry["why"])
