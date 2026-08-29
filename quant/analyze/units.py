"""심볼/시리즈별 단위 메타데이터 + 사람이 읽는 포맷터.

리포트가 숫자를 단위 없이 보여주면(예: "7,728.20") 그게 지수 포인트인지
달러인지 원인지 읽는 사람이 알 수 없다. 여기서 심볼 → {카테고리, 단위,
소수자리}를 고정해두고, 그 메타데이터로 시세표를 카테고리별로 묶고
값을 단위와 함께 포맷한다. 순수 함수만 있고 네트워크 호출은 없다.
"""
from __future__ import annotations

CATEGORY_ORDER: tuple[str, ...] = (
    "한국 증시", "미국 증시", "아시아 증시", "변동성·금리", "환율", "원자재", "디지털자산",
)

SYMBOL_META: dict[str, dict] = {
    "^KS11": {"category": "한국 증시", "unit": "pt", "decimals": 2},
    "^KQ11": {"category": "한국 증시", "unit": "pt", "decimals": 2},
    "^GSPC": {"category": "미국 증시", "unit": "pt", "decimals": 2},
    "^IXIC": {"category": "미국 증시", "unit": "pt", "decimals": 2},
    "^DJI": {"category": "미국 증시", "unit": "pt", "decimals": 2},
    "^N225": {"category": "아시아 증시", "unit": "pt", "decimals": 2},
    "^VIX": {"category": "변동성·금리", "unit": "pt", "decimals": 2},
    "^TNX": {"category": "변동성·금리", "unit": "%", "decimals": 2},
    "KRW=X": {"category": "환율", "unit": "원", "decimals": 2},
    "DX-Y.NYB": {"category": "환율", "unit": "pt", "decimals": 2},
    "GC=F": {"category": "원자재", "unit": "달러/온스", "decimals": 2},
    "SI=F": {"category": "원자재", "unit": "달러/온스", "decimals": 2},
    "HG=F": {"category": "원자재", "unit": "달러/파운드", "decimals": 2},
    "CL=F": {"category": "원자재", "unit": "달러/배럴", "decimals": 2},
    "BZ=F": {"category": "원자재", "unit": "달러/배럴", "decimals": 2},
    "NG=F": {"category": "원자재", "unit": "달러/MMBtu", "decimals": 2},
    "BTC-USD": {"category": "디지털자산", "unit": "달러", "decimals": 2},
}

_UNKNOWN_META = {"category": "기타", "unit": "", "decimals": 2}

# FRED 원본은 대부분 백만달러 단위다. 그대로 찍으면 "5,841,241" 처럼 읽히지
# 않으므로 조 달러로 스케일을 낮춘다. scale 은 "원본값을 이 값으로 나누면
# unit 이 된다"는 뜻 — 무단위 지수(NFCI)와 %(DGS10/DGS2)는 scale=1.
FRED_META: dict[str, dict] = {
    "WALCL": {"unit": "조 달러", "scale": 1_000_000},
    "WTREGEN": {"unit": "조 달러", "scale": 1_000_000},
    "RRPONTSYD": {"unit": "조 달러", "scale": 1_000_000},
    "NFCI": {"unit": "index", "scale": 1},
    "DGS10": {"unit": "%", "scale": 1},
    "DGS2": {"unit": "%", "scale": 1},
}


def meta(symbol: str) -> dict:
    return SYMBOL_META.get(symbol, _UNKNOWN_META)


def group_quotes(quotes: dict) -> list[tuple[str, list[tuple[str, dict]]]]:
    """quotes({심볼: quote}) 를 카테고리별로 묶는다.

    빈 카테고리는 결과에서 뺀다(빈 섹션이 렌더되면 안 된다). SYMBOL_META
    에 없는 심볼은 "기타"로 모아 맨 뒤에 둔다 — 새 심볼이 추가돼도 조용히
    사라지지 않고 눈에 띄게 하기 위함이다.
    """
    buckets: dict[str, list[tuple[str, dict]]] = {cat: [] for cat in CATEGORY_ORDER}
    etc: list[tuple[str, dict]] = []
    for sym, q in quotes.items():
        m = SYMBOL_META.get(sym)
        if m is None:
            etc.append((sym, q))
        else:
            buckets[m["category"]].append((sym, q))

    result = [(cat, buckets[cat]) for cat in CATEGORY_ORDER if buckets[cat]]
    if etc:
        result.append(("기타", etc))
    return result


def fmt_value(symbol: str, value: float | None) -> str:
    """"7,728.20 pt" 처럼 숫자 + 단위. "원"/"%"는 붙여쓰고 그 외는 공백 한 칸."""
    if value is None:
        return "—"
    m = meta(symbol)
    num = f"{value:,.{m['decimals']}f}"
    unit = m["unit"]
    if not unit:
        return num
    if unit in ("원", "%"):
        return f"{num}{unit}"
    return f"{num} {unit}"


def fmt_fred(series_id: str, value: float | None) -> str:
    """FRED 시리즈값을 스케일 적용 후 단위와 함께 포맷. 매핑에 없으면 콤마 숫자만."""
    if value is None:
        return "—"
    m = FRED_META.get(series_id)
    if m is None:
        return f"{value:,.2f}"
    v = value / m["scale"]
    unit = m["unit"]
    if unit in ("조 달러", "%"):
        return f"{v:,.2f}{unit}"
    return f"{v:,.2f}"  # index(NFCI) 등 무단위 — 설명은 템플릿이 붙인다


def fmt_krw_eok(value: int) -> str:
    """억원 단위 수급을 "+2조 6,395억원" 처럼. 1조 = 10,000억, 부호는 항상 표기."""
    if value == 0:
        return "0억원"
    sign = "+" if value > 0 else "-"
    trillion, eok = divmod(abs(value), 10000)
    if trillion:
        return f"{sign}{trillion}조 {eok:,}억원"
    return f"{sign}{eok:,}억원"


def fmt_shares(value: int) -> str:
    """주식 수를 "+246만주"/"+1.2만주"/"+850주" 처럼. 부호는 항상 표기."""
    if abs(value) >= 10000:
        if abs(value) < 100000:
            return f"{value / 10000:+,.1f}만주"
        return f"{value / 10000:+,.0f}만주"
    return f"{value:+,}주"
