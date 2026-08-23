"""VIX 기간구조 · MOVE · S&P500 섹터 등락률 · 52주 신고/신저(섹터별).

VIX 기간구조(9D/1M/3M/6M)의 콘탱고/백워데이션 여부는 변동성 시장이 "정상"인지
"패닉"인지를 가르는 표준 신호다. MOVE(채권 변동성)를 곁들이면 주식 변동성만
보고 놓치는 채권 스트레스도 잡힌다. 52주 신고/신저의 섹터 분포는 지수가
플랫해도 내부적으로 순환매가 일어나는지(폭이 넓은지)를 드러낸다.

yf.download 가 야후 비공식 스크레이퍼라 스키마가 깨질 수 있는 건
quant/collect/sources/market.py 와 동일한 리스크다 — 컬럼 없는 심볼은 조용히 skip 한다.
"""
from __future__ import annotations

import io
import time
import warnings

import pandas as pd
import yfinance as yf

from quant.adapters.http import client

warnings.filterwarnings("ignore", module="yfinance")

VIX_TERM = ("^VIX9D", "^VIX", "^VIX3M", "^VIX6M")
_VIX_LABELS = {"^VIX9D": "VIX 9D", "^VIX": "VIX", "^VIX3M": "VIX 3M", "^VIX6M": "VIX 6M"}
_MOVE_SYMBOL = "^MOVE"

SECTORS: dict[str, str] = {
    "XLE": "에너지",
    "XLU": "유틸리티",
    "XLI": "산업재",
    "XLB": "소재",
    "XLF": "금융",
    "XLK": "기술",
    "XLV": "헬스케어",
    "XLP": "필수소비재",
    "XLY": "임의소비재",
    "XLC": "커뮤니케이션",
    "XLRE": "부동산",
}

# quant/collect/sources/derived.py 에 동일한 상수가 계획돼 있으나(docs/plans/
# 2026-08-12-phase1-collection-and-html.md) 아직 파일 자체가 없다. 그 파일이
# 생기면 이쪽을 지우고 import 로 합치면 된다 — 지금은 이 소스만 단독으로 건드리는
# 게 범위라 값만 그대로 복제해둔다.
SP500_LIST_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

_GICS_TO_KR = {
    "Information Technology": "기술",
    "Financials": "금융",
    "Energy": "에너지",
    "Health Care": "헬스케어",
    "Industrials": "산업재",
    "Consumer Staples": "필수소비재",
    "Consumer Discretionary": "임의소비재",
    "Communication Services": "커뮤니케이션",
    "Materials": "소재",
    "Utilities": "유틸리티",
    "Real Estate": "부동산",
}

# 52주 신고/신저 티커 → 네이버 모바일 종목 상세 경로(리포트 UX 3차, 2026-08-17
# 사용자 지적: 기존 검색 랜딩 링크는 인기종목만 보인다). 네이버 자체
# 자동완성 API — quant/analyze/templates/report.html.j2 의 etf_link 매크로가
# 이미 검증한 것과 같은 API 계열이다.
NAVER_AC_URL = "https://ac.stock.naver.com/ac"


def structure_label(near: float, far: float) -> str:
    """VIX(near) 대비 VIX3M(far) 스프레드로 기간구조를 분류하는 순수 함수."""
    diff = far - near
    if diff > 0.5:
        return "콘탱고 (정상)"
    if diff < -0.5:
        return "백워데이션 (역전)"
    return "평탄"


def gics_to_kr(sector: str) -> str:
    """GICS 영문 섹터명 → 회사 리포트 표기 한글 섹터명. 미등록 섹터는 '기타'."""
    return _GICS_TO_KR.get(sector, "기타")


def classify_52w(closes: list[float]) -> str | None:
    """최신 종가가 구간 내 52주 최고/최저 근방이면 'high'/'low', 아니면 None.

    부동소수 비교라 미세 허용오차(0.01%)를 둔다."""
    if not closes:
        return None
    latest = closes[-1]
    hi = max(closes)
    lo = min(closes)
    if latest >= hi * 0.9999:
        return "high"
    if latest <= lo * 1.0001:
        return "low"
    return None


def fetch_vix_term() -> dict:
    """VIX 기간구조 4포인트 + MOVE. MOVE 실패해도 VIX 쪽은 살린다."""
    symbols = list(VIX_TERM) + [_MOVE_SYMBOL]
    data = yf.download(symbols, period="5d", progress=False, auto_adjust=False, threads=True)[
        "Close"
    ]

    points = []
    for sym in VIX_TERM:
        if sym not in data.columns:
            continue
        series = data[sym].dropna()
        if series.empty:
            continue
        c = float(series.iloc[-1])
        prev = float(series.iloc[-2]) if len(series) >= 2 else c
        points.append(
            {
                "label": _VIX_LABELS[sym],
                "symbol": sym,
                "value": round(c, 2),
                "change": round(c - prev, 2),
                "change_pct": round((c / prev - 1) * 100, 2) if prev else 0.0,
            }
        )

    if not points:
        raise ValueError("VIX 기간구조 조회 실패 — 유효 데이터 0건")

    values = {p["symbol"]: p["value"] for p in points}
    if "^VIX" in values and "^VIX3M" in values:
        structure = structure_label(values["^VIX"], values["^VIX3M"])
        spread = round(values["^VIX3M"] - values["^VIX"], 2)
    else:
        structure = "평탄"
        spread = 0.0

    move = None
    if _MOVE_SYMBOL in data.columns:
        move_series = data[_MOVE_SYMBOL].dropna()
        if not move_series.empty:
            mv = float(move_series.iloc[-1])
            mv_prev = float(move_series.iloc[-2]) if len(move_series) >= 2 else mv
            move = {"value": round(mv, 2), "change": round(mv - mv_prev, 2)}

    return {"points": points, "structure": structure, "spread": spread, "move": move}


def fetch_sectors() -> dict:
    """S&P 섹터 ETF 11종의 전일 대비 등락률, change_pct 내림차순."""
    symbols = list(SECTORS)
    data = yf.download(symbols, period="5d", progress=False, auto_adjust=False, threads=True)[
        "Close"
    ]

    sectors = []
    for sym, name in SECTORS.items():
        if sym not in data.columns:
            continue
        series = data[sym].dropna()
        if len(series) < 2:
            continue
        c = float(series.iloc[-1])
        prev = float(series.iloc[-2])
        change_pct = round((c / prev - 1) * 100, 2) if prev else 0.0
        sectors.append({"ticker": sym, "name": name, "change_pct": change_pct})

    if not sectors:
        raise ValueError("섹터 등락률 조회 실패 — 유효 데이터 0건")

    sectors.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"sectors": sectors}


def _fetch_ticker_name(ticker: str) -> str | None:
    """야후 개별 종목 정보에서 회사명(shortName)을 가져온다. 실패하면 None —
    이름 없이 티커만 표시해도 파이프라인은 계속 돈다."""
    try:
        info = yf.Ticker(ticker).info
    except Exception:  # noqa: BLE001 — 개별 종목 조회 실패, 이름만 빠뜨리고 진행
        return None
    return info.get("shortName")


def _fetch_naver_path(ticker: str) -> str | None:
    """네이버 자동완성 API에서 개별 종목의 모바일 상세 경로를 찾는다.

    2026-08-17 실측(MRK, `curl "https://ac.stock.naver.com/ac?q=MRK&target=
    stock,index,marketindicator"`) — 응답은 다음 형태다:
    `{"query":"MRK","items":[{"code":"MRK","name":"머크 앤 코",
    "typeCode":"NYSE","url":"/worldstock/stock/MRK/total",
    "reutersCode":"MRK",...}, {"code":"MRKR","url":"/worldstock/stock/
    MRKR.O/total","reutersCode":"MRKR.O",...}, ...]}` — 자동완성이라 유사
    티커(MRKR 등)도 같이 돌아온다. `code`(또는 거래소 접미사를 뗀
    `reutersCode`)가 `ticker` 와 정확히 일치하는 항목만 고른다 — 첫 항목을
    그냥 집으면 다른 회사로 잘못 링크된다(검색 랜딩이 인기종목만 보인다는
    사용자 지적과 같은 함정 — fuzzy 매칭은 결과가 똑같이 못 미덥다).

    `url` 은 이미 `/total` 로 끝나므로(위 실측) 저장은 그 접미사를 뗀
    값으로 한다 — 템플릿의 `etf_link` 매크로와 동일하게 "티커부 경로 +
    호출부가 붙이는 /total" 관례를 유지해 두 링크 생성 방식이 갈리지
    않게 한다.

    실패(HTTP 에러, 매칭 없음)는 전부 None — 52주 표는 이미 확보된 티커
    데이터라 링크 하나 없어도 표는 그대로 나간다(`_fetch_ticker_name` 과
    같은 관례).
    """
    try:
        with client(timeout=5.0) as c:
            resp = c.get(
                NAVER_AC_URL,
                params={"q": ticker, "target": "stock,index,marketindicator"},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001 — 개별 종목 조회 실패, 링크만 빠뜨리고 진행
        return None

    for item in data.get("items") or []:
        code = item.get("code") or ""
        reuters_base = (item.get("reutersCode") or "").split(".")[0]
        if code == ticker or reuters_base == ticker:
            url = item.get("url") or ""
            if url.endswith("/total"):
                url = url[: -len("/total")]
            return url or None
    return None


def fetch_breadth_52w(name_fetcher=None, sleep=None, naver_path_fetcher=None) -> dict:
    """S&P500 구성종목 중 오늘 52주 신고가/신저가를 낸 종목을 섹터별로 묶는다.

    500종목 × 1년치라 30~90초 걸린다. 개별 종목 실패는 건너뛰고, 유효 종목이
    0개일 때만 실패로 처리한다(collect.run_source 가 소스 단위로 격리한다).

    52주 표에 실제로 표시될 티커(그날 신고가·신저가 해당분)에 한해 회사명
    (shortName)을 yf.Ticker(...).info 로, 네이버 모바일 상세 경로를
    `_fetch_naver_path`(네이버 자동완성 API)로 추가 조회한다 — 500종목
    전체가 아니라 그날 신고/신저를 낸 수십 종목 수준이라 부담이 작다.
    종목 사이 0.2초 sleep 으로 연속 요청을 완화한다(리포트 UX 2차 L-3 —
    "TECH가 섹터명처럼 읽힌다", 티커만으론 회사명이 안 보여 혼란스럽다는
    피드백; 리포트 UX 3차 — 상세 링크가 검색 랜딩이라 인기종목만 보인다는
    피드백). 이름·경로 조회가 실패해도 티커 자체는 이미 확보된 데이터라
    표는 그대로 나간다(각각 None으로 빠질 뿐).
    """
    fetch_name = name_fetcher or _fetch_ticker_name
    fetch_naver_path = naver_path_fetcher or _fetch_naver_path
    zzz = sleep or time.sleep
    # pd.read_html 은 urllib 기본 UA 를 쓰는데 위키피디아가 이를 403 으로 막는다
    # (2026-08-12 실측) — quant/adapters/http.py 의 client() 로 받은 HTML 텍스트를 넘긴다.
    with client(timeout=20.0) as c:
        resp = c.get(SP500_LIST_URL)
        resp.raise_for_status()
    table = pd.read_html(io.StringIO(resp.text))[0]
    yahoo_symbols = [s.replace(".", "-") for s in table["Symbol"].tolist()]
    sector_by_symbol = {
        sym: gics_to_kr(sector) for sym, sector in zip(yahoo_symbols, table["GICS Sector"])
    }

    df = yf.download(yahoo_symbols, period="1y", progress=False, auto_adjust=False, threads=True)[
        "Close"
    ]

    highs_by_sector: dict[str, list[str]] = {}
    lows_by_sector: dict[str, list[str]] = {}
    valid = 0
    for sym in yahoo_symbols:
        if sym not in df.columns:
            continue
        closes = df[sym].dropna().tolist()
        if not closes:
            continue
        valid += 1
        label = classify_52w(closes)
        if label == "high":
            highs_by_sector.setdefault(sector_by_symbol[sym], []).append(sym)
        elif label == "low":
            lows_by_sector.setdefault(sector_by_symbol[sym], []).append(sym)

    if valid == 0:
        raise ValueError("52주 신고/신저 계산 실패 — 유효 종목 0개")

    for bucket in highs_by_sector.values():
        bucket.sort()
    for bucket in lows_by_sector.values():
        bucket.sort()

    displayed = sorted(
        {t for bucket in highs_by_sector.values() for t in bucket}
        | {t for bucket in lows_by_sector.values() for t in bucket}
    )
    names: dict[str, str | None] = {}
    naver_paths: dict[str, str | None] = {}
    for i, ticker in enumerate(displayed):
        names[ticker] = fetch_name(ticker)
        naver_paths[ticker] = fetch_naver_path(ticker)
        if i < len(displayed) - 1:
            zzz(0.2)

    def _annotate(by_sector: dict[str, list[str]]) -> dict[str, list[dict]]:
        return {
            sector: [
                {"ticker": t, "name": names.get(t), "naver_path": naver_paths.get(t)}
                for t in tickers
            ]
            for sector, tickers in by_sector.items()
        }

    return {
        "highs": {
            "count": sum(len(v) for v in highs_by_sector.values()),
            "by_sector": _annotate(highs_by_sector),
        },
        "lows": {
            "count": sum(len(v) for v in lows_by_sector.values()),
            "by_sector": _annotate(lows_by_sector),
        },
        "universe": len(yahoo_symbols),
        "as_of": df.index[-1].strftime("%Y-%m-%d"),
    }
