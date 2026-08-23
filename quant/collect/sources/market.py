"""시세 백본 + 스파크라인 히스토리 + 한국 반도체 앵커.

yfinance 는 야후의 비공식 스크레이퍼라 스키마가 예고 없이 깨질 수 있다. 그래서
지수급 심볼(S&P500/VIX 등) 몇 개는 FRED(공식 API)로 2차 확인한다 — 값이 크게
어긋나면 리포트에 경고를 얹어 "숫자가 틀렸는데 아무도 몰랐다"를 막는다. 다만
API 키가 없거나 매핑에 없는 심볼이면 그냥 교차검증을 생략한다 — 이건 이 소스의
성공/실패 기준이 아니다.

앵커(삼성전자/SK하이닉스)는 KR 리포트에서 뉴스 언급 여부와 무관하게 항상
채운다. 사용자가 한국 시장을 볼 때 반도체를 기본값으로 보기 때문이다.
"""
from __future__ import annotations

import warnings

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

from quant.adapters.env import get_key
from quant.adapters.http import client

warnings.filterwarnings("ignore", module="yfinance")

_KST = timezone(timedelta(hours=9))

TICKERS: dict[str, dict[str, str]] = {
    "KR": {
        "^GSPC": "S&P500",
        "^IXIC": "NASDAQ",
        "^VIX": "VIX",
        "DX-Y.NYB": "DXY",
        "KRW=X": "USD/KRW",
        "GC=F": "금",
        "HG=F": "구리",
        "CL=F": "WTI",
        "BZ=F": "브렌트",
        "BTC-USD": "비트코인",
        # 2026-08-13: 은·천연가스가 US 맵에만 있어 KR 리포트에서 빠져 있었다
        # (금·구리·WTI·브렌트는 있는데 은만 없는 비대칭). 원자재는 시장과 무관하다.
        "SI=F": "은",
        "NG=F": "천연가스",
        "^KS11": "KOSPI",
        "^KQ11": "KOSDAQ",
        "^N225": "니케이",
    },
    "US": {
        "^GSPC": "S&P500",
        "^IXIC": "NASDAQ",
        "^VIX": "VIX",
        "DX-Y.NYB": "DXY",
        "KRW=X": "USD/KRW",
        "GC=F": "금",
        "HG=F": "구리",
        "CL=F": "WTI",
        "BZ=F": "브렌트",
        "BTC-USD": "비트코인",
        "^DJI": "다우",
        "^TNX": "미국10년",
        "SI=F": "은",
        "NG=F": "천연가스",
    },
}

ANCHORS: dict[str, dict[str, str]] = {
    "KR": {"005930.KS": "삼성전자", "000660.KS": "SK하이닉스"},
    "US": {},
}

# FRED 는 공식이지만 관측치가 1~2 거래일 지연될 수 있어, 장중 변동만으로도 1%를
# 넘는 오탐이 났다. 이 교차검증의 목적은 "야후가 완전히 깨졌는지" 잡는 것이지
# 소수점 검증이 아니므로 오탐을 피할 만큼 넉넉하게 잡는다.
CROSSCHECK_TOLERANCE_PCT = 3.0

_FRED_MAP = {"^GSPC": "SP500", "^IXIC": "NASDAQCOM", "^VIX": "VIXCLS", "KRW=X": "DEXKOUS"}

# 시장 관례상 "고가 대비 위치"는 52주(약 252거래일) 기준이다 — 60일 창으로는
# 리포트를 읽는 사람이 오해한다. 1년치를 받아두면 120일선 계산도 함께 가능해지고,
# yfinance 요청 비용은 3mo→1y 로 늘려도 거의 차이가 없다.
HISTORY_DAYS = 252

# OHLCV 는 이동평균·변동성 계산에 쓰인다 — 30행 미만이면 그런 계산이 무의미하므로
# 0 프레임으로 위장하지 않고 키 자체를 생략한다.
MIN_OHLCV_ROWS = 30


def _fetch_symbols(symbols: dict[str, str], *, today: date | None = None) -> dict:
    """야후 심볼 → 표시이름 딕셔너리를 한 번의 다운로드로 시세+히스토리로 바꾼다.

    야후 일봉의 마지막 행은 그날 거래가 진행 중이거나(장중) 아직 시작도 안 했는데도
    (장 시작 전, 자리만 잡은 placeholder) 이미 그 날짜로 존재할 수 있다 — 호출할
    때마다 값이 계속 바뀌는 미확정 스냅샷이다. 이걸 날짜 검증 없이 "오늘 종가"로
    믿고 그 앞 값을 "전일"로 짝지으면, 확정되지 않은 값이 "전일 대비"로 둔갑한다
    (2026-08-19 KOSPI 리포트가 +2.42%를 냈는데 실제 마지막 확정 종가는 -1.55%였던
    사고 — 마지막 두 봉을 날짜 검증 없이 오늘/전일로 취급한 게 원인이었다). 그래서
    `today`(KST 기준, 없으면 지금) 이상 날짜의 봉은 버리고 그 이전 확정 거래일만
    쓴다. 확정 거래일이 1개뿐이면 "전일 대비"를 계산할 기준이 없다는 뜻이므로
    0%(변동없음)로 위장하지 않고 prev/change_pct 키 자체를 생략한다.
    """
    today = today or datetime.now(_KST).date()
    close = yf.download(
        list(symbols), period="1y", progress=False, auto_adjust=False, threads=True
    )["Close"]
    out: dict[str, dict] = {}
    for sym, label in symbols.items():
        if sym not in close.columns:
            continue  # 야후가 이 심볼만 조용히 못 받아온 경우 — 나머지는 살린다
        series = close[sym].dropna()
        series = series[series.index.date < today]  # 미확정 '오늘' 봉 제외
        if series.empty:
            continue
        history = [float(v) for v in series.tolist()[-HISTORY_DAYS:]]
        c = history[-1]
        entry = {
            "label": label,
            "close": c,
            "history": history,
            "date": series.index[-1].date().isoformat(),
        }
        if len(history) >= 2:
            prev = history[-2]
            entry["prev"] = prev
            entry["change_pct"] = round((c / prev - 1) * 100, 3) if prev else 0.0
        out[sym] = entry
    return out


def _fred_secondary(primary: dict[str, float]) -> dict[str, float]:
    """2차 확인값. 키가 없거나 개별 시리즈가 실패해도 예외를 삼키고 빈/부분 dict —
    교차검증만 생략된다(시세 자체는 계속 나와야 한다)."""
    api_key = get_key("FRED_API_KEY")
    if not api_key:
        return {}
    secondary: dict[str, float] = {}
    try:
        with client(timeout=20.0) as c:
            for sym, series_id in _FRED_MAP.items():
                if sym not in primary:
                    continue
                try:
                    r = c.get(
                        "https://api.stlouisfed.org/fred/series/observations",
                        params={
                            "series_id": series_id,
                            "api_key": api_key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": 5,
                        },
                    )
                    r.raise_for_status()
                    observations = r.json()["observations"]
                    for obs in observations:
                        value = obs["value"]
                        if value in (".", ""):
                            continue  # FRED 는 휴장일에 "." 을 넣는다 — float() 하면 죽는다
                        secondary[sym] = float(value)
                        break
                except Exception:
                    continue
    except Exception:
        return {}
    return secondary


def crosscheck(primary: dict[str, float], secondary: dict[str, float]) -> dict:
    """두 소스가 허용오차를 넘겨 어긋나는 심볼을 경고 문자열로 뽑는 순수 함수."""
    warnings_list = []
    for sym, sec_val in sorted(secondary.items()):
        if sym not in primary or not sec_val:
            continue
        pri_val = primary[sym]
        diff_pct = abs(pri_val - sec_val) / sec_val * 100
        if diff_pct > CROSSCHECK_TOLERANCE_PCT:
            warnings_list.append(
                f"{sym}: yfinance {pri_val:,.2f} vs fred {sec_val:,.2f} "
                f"({diff_pct:.1f}% 괴리)"
            )
    return {"checked": sorted(secondary), "warnings": warnings_list}


def _symbol_ohlcv(raw, sym: str, is_multi: bool, today: date) -> pd.DataFrame | None:
    """`raw` yf.download 프레임에서 심볼 1개의 OHLCV 를 뽑는다.

    5개 필드(open/high/low/close/volume) 중 하나라도 NaN 인 행은 버린다 —
    거래량이 통째로 안 잡힌 심볼을 종가만 있는 반쪽 프레임으로 위장하지
    않기 위해서다. `today`(KST) 이상 날짜의 행도 버린다 — 이동평균·변동성
    계산에 아직 마감 안 된 미확정 봉이 섞이면 안 된다(`_fetch_symbols`와 동일
    사유, 2026-08-19 KOSPI 사고). 남은 행이 `MIN_OHLCV_ROWS` 미만이면 None —
    호출부가 "ohlcv" 키 자체를 생략한다.
    """
    fields = ["Open", "High", "Low", "Close", "Volume"]
    try:
        if is_multi:
            data = {f.lower(): raw[f][sym] for f in fields}
        else:
            data = {f.lower(): raw[f] for f in fields}
        df = pd.DataFrame(data).dropna(how="any").sort_index()
    except Exception:
        return None
    df = df[df.index.date < today]
    if len(df) < MIN_OHLCV_ROWS:
        return None
    return df


def fetch_symbol_quotes(yahoo_symbols: list[str], *, today: date | None = None) -> dict[str, dict]:
    """야후 심볼 리스트 → {야후심볼: {"close","date","history"[,"prev","change_pct","ohlcv"]}}.

    빈 리스트면 {} 반환. 개별 심볼 결측은 조용히 건너뛴다 — 뉴스로 발굴된
    종목은 야후에 상장 안 돼 있거나 심볼이 어긋날 수 있고, 그래도 나머지
    종목 시세는 살아야 한다.

    마지막 행이 아직 마감 안 된(장중 진행 중이거나 장 시작 전 placeholder)
    "오늘"(KST, `today` 인자로 주입 가능 — 기본은 지금) 봉이면 버리고 그 이전
    확정 거래일만 쓴다 — 안 그러면 호출할 때마다 값이 바뀌는 미확정 스냅샷이
    "전일 대비"로 둔갑한다(2026-08-19 KOSPI 리포트 +2.42% vs 실제 -1.55% 사고,
    `_fetch_symbols`와 동일 원인). `close`가 가리키는 실제 날짜는 "date"로
    드러낸다 — 호출부가 "오늘"이라고 임의로 가정하지 않게. 확정 거래일이
    1개뿐이면 "prev"/"change_pct" 키 자체를 생략한다(0%로 위장 금지).

    "ohlcv"(open/high/low/close/volume 컬럼, DatetimeIndex 오름차순, 최대
    1y)는 야후가 그 심볼의 OHLCV 를 충분히 못 주면 키 자체가 없다 — 0행
    프레임으로 위장하지 않는다. close/prev/change_pct/history 는 기존과
    동일하게 계산된다(같은 1회 배치 다운로드에서 close 경로만 그대로 씀).
    """
    if not yahoo_symbols:
        return {}
    today = today or datetime.now(_KST).date()
    try:
        raw = yf.download(
            yahoo_symbols, period="1y", progress=False, auto_adjust=False, threads=True
        )
    except Exception:
        return {}  # 전부 실패해도 시세 없이 리포트는 나와야 한다

    close = raw["Close"]
    # 심볼이 1개면 일부 yfinance 버전에서 컬럼 레벨이 없는 Series 를 준다 —
    # DataFrame 으로 맞춰서 아래 컬럼 조회 로직을 하나로 유지한다.
    if not hasattr(close, "columns"):
        close = close.to_frame(name=yahoo_symbols[0])
    is_multi = isinstance(raw.columns, pd.MultiIndex)

    out: dict[str, dict] = {}
    for sym in yahoo_symbols:
        if sym not in close.columns:
            continue  # 야후가 이 심볼만 조용히 못 받아온 경우 — 나머지는 살린다
        series = close[sym].dropna()
        series = series[series.index.date < today]  # 미확정 '오늘' 봉 제외
        if series.empty:
            continue
        history = [float(v) for v in series.tolist()[-HISTORY_DAYS:]]
        c = history[-1]
        entry = {
            "close": c,
            "history": history,
            "date": series.index[-1].date().isoformat(),
        }
        if len(history) >= 2:
            prev = history[-2]
            entry["prev"] = prev
            entry["change_pct"] = round((c / prev - 1) * 100, 3) if prev else 0.0
        ohlcv = _symbol_ohlcv(raw, sym, is_multi, today)
        if ohlcv is not None:
            entry["ohlcv"] = ohlcv
        out[sym] = entry
    return out


def fetch_quotes(market: str, *, today: date | None = None) -> dict:
    today = today or datetime.now(_KST).date()
    quotes = _fetch_symbols(TICKERS[market], today=today)
    if not quotes:
        raise ValueError(f"{market} 시세를 하나도 받지 못했다")

    anchors: dict[str, dict] = {}
    if market == "KR":
        anchors = _fetch_symbols(ANCHORS["KR"], today=today)
        for sym, data in anchors.items():
            data["symbol"] = sym.split(".")[0]

    primary = {sym: d["close"] for sym, d in quotes.items()}
    secondary = _fred_secondary(primary)

    return {
        "quotes": quotes,
        "anchors": anchors,
        "crosscheck": crosscheck(primary, secondary),
    }
