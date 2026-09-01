"""관심종목 자동 스코어링 v2 — LLM 리포트 분석이 제안한 후보 종목을, 그 후보를
추천한 근거(테마: 추세/눌림목반등/이벤트)에 맞춰 결정론적 규칙으로 채점해, 근거가
있는 종목만 data/watchlist.yaml에 자동 추가되게 한다. 08:40 KST daily-brief cron이
부르는 리포팅 레이어 전용이다 — 거래 핫패스가 아니다.

v1 → v2 변경 배경 (적대적 리뷰 verdict REJECT 반영):
- v1은 구조적 컴포넌트(유동성15+비용10+변동성밴드20=45점 바닥)가 threshold 55의
  대부분을 차지해 사실상 문턱이 장식이었다(C2). KR 개별주가 KR ETF와 동일한 비용
  구조로 채점돼 실제 매도세 15~20bp를 무시했다(C3). regime.json이 오래돼도 조용히
  neutral로 대체됐다(M1). 캔들 최신성 체크가 없었다(M5).
- v2는 "하드 게이트(프리퍼시티) → 증거 점수(0~100)"의 2계층 모델로 바꾼다.
  프리퍼시티는 점수를 주지 않고 통과/실패만 가른다 — 통과해도 0점일 수 있고,
  실패해도 점수는 계속 보고한다(관찰 목적).

**이 채점 규칙이 수익을 낸다는 증거는 없다** — 목적은 근거 없는 후보를 자동
워치리스트 편입에서 걸러내는 것이지 알파 발굴이 아니다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

_MIN_ROWS = 30
# EVENT_SCALP(단타 자동화 갈래 A)·FRGN(외국인 적립 갈래 B) — 2026-08-17 서브프로젝트 T
# 태그 배선. 둘 다 이 파일이 모르는 새 근거 출처(intraday_scorer v4/외국인 수급
# 추종)에서 오지만, 여기 채점 모델은 캔들(OHLCV) 기반 3프로필뿐이라 새 프로필을
# 만들지 않고 기존 프로필을 재사용한다(아래 `_PROFILE_SCORERS` 별칭). FRGN_EXIT는
# 여기 오지 않는다 — own_brief.sh가 watch-score를 완전히 우회해 이미 등록된 종목의
# 태그만 갱신한다(신규 등록 판정이 필요 없는 이탈 신호이므로).
_VALID_TAGS = ("TREND", "REBOUND", "EVENT", "EVENT_SCALP", "FRGN", "CLOSE_BET")
_ETF_NAME_MARKERS = (
    "KODEX", "TIGER", "PLUS", "SOL", "ACE", "RISE", "KOSEF", "HANARO", "KIWOOM", "ETF",
)


@dataclass
class ScoreResult:
    symbol: str
    score: int
    passed: bool
    tags: list[str]  # 입력에서 파싱된 태그. 빈 리스트면 무태그(best-of 세 프로필)
    reasons: list[str]
    prereq_ok: bool  # 프리퍼시티(하드 게이트) 통과 여부 — False면 점수와 무관하게 FAIL
    # ↓ 텔레그램 세분화 출력용(2026-08-10): 항목별 (이름, 득점, 만점, 상세) + 채택 프로필
    profile: str = ""
    breakdown: list[tuple[str, int, int, str]] = field(default_factory=list)
    eff_threshold: int = 0
    threshold_notes: list[str] = field(default_factory=list)


def _is_kr_symbol(symbol: str) -> bool:
    return symbol.isdigit() and len(symbol) == 6


def _as_date(ts) -> date:
    return ts.date() if hasattr(ts, "date") else ts


def _parse_token(token: str) -> tuple[str, list[str], date | None, list[str]]:
    """`SYMBOL[:TAGS[:YYYYMMDD]]` 파싱. TAGS는 '+'로 구분된 다중 태그.
    반환: (symbol, tags, report_date, parse_reasons). 알 수 없는 태그가 하나라도
    섞이면 전체를 무태그로 취급(best-of)하고 사유를 남긴다."""
    parts = token.split(":")
    symbol = parts[0]
    tags: list[str] = []
    report_date: date | None = None
    reasons: list[str] = []

    if len(parts) >= 2 and parts[1]:
        raw_tags = parts[1].split("+")
        if any(t not in _VALID_TAGS for t in raw_tags):
            reasons.append("알 수 없는 태그")
        else:
            tags = raw_tags

    if len(parts) >= 3 and parts[2]:
        try:
            report_date = datetime.strptime(parts[2], "%Y%m%d").date()
        except ValueError:
            reasons.append("리포트 날짜 형식 오류")

    return symbol, tags, report_date, reasons


def _rvol(daily: pd.DataFrame) -> float:
    """마지막 완결일 거래량 / 직전 14일 평균."""
    volume = daily["volume"]
    last_vol = volume.iloc[-1]
    avg14 = volume.iloc[-15:-1].mean()
    if avg14 == 0 or pd.isna(avg14):
        return 0.0
    return last_vol / avg14


def _atr_pct(daily: pd.DataFrame) -> float | None:
    high, low, close = daily["high"], daily["low"], daily["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.tail(14).mean()
    last = close.iloc[-1]
    if last == 0 or pd.isna(atr14):
        return None
    return atr14 / last * 100


# ------------------------------------------------------------------ 프리퍼시티(하드 게이트)

def _fetch_stock_info(symbol: str, client) -> dict | None:
    """`client.stock_info(symbol)`을 1회 호출한다. ETF 여부 판정과 레버리지 정규화가
    같은 응답(securityType/name/leverageFactor)을 쓰므로, 호출부는 이 결과를
    재사용해야 한다 — 같은 심볼에 stock_info를 두 번 부르면 API 호출 예산(rate
    limit)만 두 배로 든다."""
    try:
        info = client.stock_info(symbol)
    except Exception:
        return None
    return info or None


def _check_kr_product(info: dict | None) -> tuple[bool | None, str]:
    """KR 6자리 종목의 ETF 여부. 반환 (is_etf, reason). is_etf=None이면 판정 불가
    (프리퍼시티 실패로 취급하지 않고 사유만 남긴다). `info`는 `_fetch_stock_info`가
    이미 조회한 결과를 받는다 — 여기서 다시 조회하지 않는다."""
    if not info:
        return None, "상품유형 미확인"
    security_type = info.get("securityType")
    if security_type is not None:
        return security_type in ("ETF", "FOREIGN_ETF"), f"securityType={security_type}"
    name = info.get("name") or info.get("englishName") or ""
    if name:
        return any(m in name.upper() for m in _ETF_NAME_MARKERS), f"이름 기반 판정: {name}"
    return None, "상품유형 미확인"


def _leverage_factor(info: dict | None) -> float | None:
    """상품 정보에서 레버리지 배수(절대값)를 뽑는다. `leverageFactor`가 없거나
    파싱 불가면 None — **1.0으로 단정하지 않는다**. 인버스(-3x)도 절대값으로
    취급한다(ATR 정규화는 방향이 아니라 변동성 배수만 본다)."""
    if not info:
        return None
    raw = info.get("leverageFactor")
    if raw is None:
        return None
    try:
        factor = abs(float(raw))
    except (TypeError, ValueError):
        return None
    return factor if factor > 0 else None


# 상품 구조 자체가 위험해 **변동성 지표를 통과해도 거래하지 않는** 종목들.
#
# "ATR 게이트를 통과한다"가 "거래해도 된다"를 뜻하지 않는다 — 아래 위험은 ATR로
# 잡히지 않는다. 문서로만 두면 자동 발굴(거래대금 랭킹)이 이 문을 열고 들어온다:
# VIX가 튀는 날 UVXY가 랭킹 상위에 뜨는 것은 시간 문제다 (2026-08-12 사용자 결정).
#
# - VIX 선물 ETF: 콘탱고 감쇠가 치명적(UVXY 상장 이후 -99.9%, 역분할 반복). 최근
#   ATR이 낮은 것은 시장이 조용해서일 뿐이고 꼬리가 정규분포가 아니다. SVXY는
#   같은 상품군(XIV)이 2018-02-05 하루 -96%로 청산된 이력이 있다.
# - 천연가스 ±2x: 원자재 중 콘탱고 감쇠가 가장 심하고 역분할이 반복된다.
# - 합성(스왑) ETF: 실물 담보가 아니라 거래상대방 위험이 있다.
_STRUCTURAL_EXCLUSIONS = {
    "UVXY": "VIX 선물 콘탱고 감쇠 (상장 이후 -99.9%)",
    "VIXY": "VIX 선물 콘탱고 감쇠",
    "SVXY": "인버스 VIX — 동일 상품군(XIV) 하루 -96% 청산 이력",
    "BOIL": "천연가스 2x — 콘탱고 감쇠 극심",
    "KOLD": "천연가스 -2x — 콘탱고 감쇠 극심",
    "225130": "합성(스왑) 레버리지 — 거래상대방 위험",
}


def _check_prerequisites(
    daily: pd.DataFrame, symbol: str, is_kr: bool, client, today: date,
    allow_kr_stocks: bool = False,
) -> tuple[list[str], list[str]]:
    """(hard_failures, info_reasons) 반환. hard_failures가 비어있으면 프리퍼시티 통과."""
    failures: list[str] = []
    info: list[str] = []

    excluded = _STRUCTURAL_EXCLUSIONS.get(symbol.strip().upper())
    if excluded is not None:
        failures.append(f"상품 구조 배제: {excluded}")

    last_bar_date = _as_date(daily.index[-1])
    staleness = (today - last_bar_date).days
    if staleness > 4:
        failures.append(f"데이터 최신성 부족: 마지막 봉 {last_bar_date} ({staleness}일 전)")

    turnover = (daily["close"] * daily["volume"]).tail(14).mean()
    min_turnover = 1_000_000_000 if is_kr else 2_000_000
    if turnover < min_turnover:
        unit = "원" if is_kr else "$"
        failures.append(
            f"유동성 부족: 14일 평균 거래대금 {turnover:,.0f}{unit} < 기준 {min_turnover:,.0f}{unit}"
        )

    # stock_info는 ETF 판정(KR)과 레버리지 정규화(전 시장) 둘 다에 쓰인다 — 여기서
    # 1회만 조회해 재사용한다(KR/US 공통: TQQQ/SOXL 같은 US 레버리지 ETF도 정규화
    # 대상이다).
    stock_info = _fetch_stock_info(symbol, client)
    leverage = _leverage_factor(stock_info)

    atr_pct = _atr_pct(daily)
    if atr_pct is None:
        failures.append("변동성 비정상: ATR(14) 계산불가 (기준 0.5~15%)")
    elif leverage is None:
        # 레버리지 미상(조회 실패·필드 없음) — 1.0으로 단정하지 않고 기존 게이트를
        # 그대로 적용한다. 3배 ETF가 이 경로로 잘못 걸릴 수 있다는 뜻이지만, 모르는
        # 것을 안전하다고 가정하는 것보다 낫다 — 사유에 정규화 미적용을 명시한다.
        if not (0.5 <= atr_pct <= 15.0):
            failures.append(
                f"변동성 비정상: ATR(14) {atr_pct:.2f}% (기준 0.5~15%, 레버리지 미상 — 정규화 없음)"
            )
    else:
        # 레버리지 ETF는 태생적으로 ATR이 배수만큼 커진다(3배 ETF는 ATR도 ~3배) —
        # 기초자산 기준 변동성으로 환산해 판정한다. 원본 ATR과 정규화 값을 둘 다
        # 사유에 남겨 통과/탈락 이유를 사람이 읽을 수 있게 한다.
        normalized = atr_pct / leverage
        detail = f"ATR {atr_pct:.2f}% ÷ {leverage:g}배 = {normalized:.2f}%"
        if 0.5 <= normalized <= 15.0:
            info.append(f"변동성 정상: {detail}")
        else:
            bound = "초과" if normalized > 15.0 else "미만"
            failures.append(f"변동성 비정상: {detail} (기준 0.5~15% {bound})")

    if is_kr:
        is_etf, product_reason = _check_kr_product(stock_info)
        if is_etf is False:
            if allow_kr_stocks:
                # 2026-08-10 사용자 결정: 개별주 자동 편입 허용 — 단 paper 수수료
                # 모델이 매도세 15bp를 반영하므로 성적이 실비용 기준으로 나온다.
                info.append("KR 개별주(매도세 15bp paper 반영) — 통과")
            else:
                failures.append("KR 개별주: 매도세 15~20bp > 엣지 — 자동등록 차단(수동 /watch는 가능)")
        elif is_etf is None:
            info.append(product_reason)

    # p5 종목 경고(Toss stocks/{symbol}/warnings) — 자동 진입에 위험한 지정 상태 차단.
    # 심각(차단): 정리매매(상폐 절차)·투자위험·투자경고·단기과열(단일가 매매로
    #   orb의 5분봉 전제가 깨짐). 경미(통과+표기): VI_*(일과성 순간 상태),
    #   신주인수권, unknown code(스펙이 미래 코드 허용을 요구 — 모르는 건 막지 않되 표기).
    # 조회 실패는 비차단 — 경고 API 장애가 채점 전체를 죽이면 안 된다.
    warn_failure, warn_info = _check_warnings(symbol, client)
    if warn_failure:
        failures.append(warn_failure)
    if warn_info:
        info.append(warn_info)

    return failures, info


_WARNING_BLOCK = {
    "LIQUIDATION_TRADING": "정리매매(상장폐지 절차)",
    "INVESTMENT_RISK": "투자위험종목",
    "INVESTMENT_WARNING": "투자경고종목",
    "OVERHEATED": "단기과열종목(단일가 매매)",
}


def _check_warnings(symbol: str, client) -> tuple[str | None, str | None]:
    """(차단 사유, 표기 사유). openapi 스키마: result[] = {warningType, ...}."""
    try:
        warnings = client.stock_warnings(symbol)
    except Exception as e:  # noqa: BLE001 — 경고 조회 실패는 비차단
        return None, f"경고 조회 실패(통과 처리): {type(e).__name__}"
    if not warnings:
        return None, None
    types = [str(w.get("warningType", "")) for w in warnings if isinstance(w, dict)]
    blocked = [_WARNING_BLOCK[t] for t in types if t in _WARNING_BLOCK]
    if blocked:
        return f"매수 유의 지정: {', '.join(blocked)} — 자동등록 차단", None
    mild = [t for t in types if t]
    return None, f"경고 표기(통과): {', '.join(mild)}" if mild else None


# ------------------------------------------------------------------ 증거 점수(테마별 0~100)

def _trend_score(daily: pd.DataFrame) -> tuple[int, list[str], list[tuple[str, int, int, str]]]:
    close, open_ = daily["close"], daily["open"]
    reasons: list[str] = []
    breakdown: list[tuple[str, int, int, str]] = []
    pts = 0

    ret5 = (close.iloc[-1] / close.iloc[-6] - 1) * 100
    add = 25 if ret5 > 0 else 0
    pts += add
    reasons.append(f"5일 수익률 {ret5:+.2f}% (+{add})")
    breakdown.append(("5일 수익률", add, 25, f"{ret5:+.2f}%"))

    rvol = _rvol(daily)
    if rvol >= 1.5:
        add = 25
    elif rvol >= 1.0:
        add = 15
    else:
        add = 0
    pts += add
    reasons.append(f"RVOL {rvol:.2f} (+{add})")
    breakdown.append(("상대 거래량", add, 25, f"RVOL {rvol:.2f}"))

    sma5, sma20 = close.tail(5).mean(), close.tail(20).mean()
    add = 25 if sma5 > sma20 else 0
    pts += add
    reasons.append(f"SMA5 {sma5:.2f} vs SMA20 {sma20:.2f} (+{add})")
    breakdown.append(("이평 정배열", add, 25, f"SMA5 {sma5:.2f} vs SMA20 {sma20:.2f}"))

    bullish_days = int((close.tail(5) > open_.tail(5)).sum())
    add = 25 if bullish_days >= 3 else 0
    pts += add
    reasons.append(f"최근 5일 중 양봉 {bullish_days}일 (+{add})")
    breakdown.append(("양봉 빈도", add, 25, f"최근 5일 중 {bullish_days}일"))

    return pts, reasons, breakdown


def _rebound_score(daily: pd.DataFrame) -> tuple[int, list[str], list[tuple[str, int, int, str]]]:
    close, open_, high, low = daily["close"], daily["open"], daily["high"], daily["low"]
    reasons: list[str] = []
    breakdown: list[tuple[str, int, int, str]] = []

    high20 = high.tail(20).max()
    last = close.iloc[-1]
    drawdown = (last - high20) / high20 * 100
    dd_pts = 30 if -40.0 <= drawdown <= -15.0 else 0
    reasons.append(f"20일 고점 대비 낙폭 {drawdown:.2f}% (+{dd_pts})")
    breakdown.append(("낙폭 구간", dd_pts, 30, f"20일 고점 대비 {drawdown:.2f}%"))

    rvol = _rvol(daily)
    bullish = last > open_.iloc[-1]
    confirmed = bullish and rvol >= 2.0
    confirm_pts = 40 if confirmed else 0
    reasons.append(
        f"확인캔들: {'양봉' if bullish else '음봉'}, RVOL {rvol:.2f} "
        f"{'충족' if confirmed else '미충족'} (+{confirm_pts})"
    )
    breakdown.append((
        "반등 확인캔들", confirm_pts, 40,
        f"{'양봉' if bullish else '음봉'} · RVOL {rvol:.2f}",
    ))

    day_range = high.iloc[-1] - low.iloc[-1]
    position = (last - low.iloc[-1]) / day_range if day_range > 0 else 0.0
    range_pts = 30 if position >= 0.7 else 0
    reasons.append(f"당일 레인지 상위 위치 {position * 100:.0f}% (+{range_pts})")
    breakdown.append(("종가 위치", range_pts, 30, f"당일 레인지 상위 {position * 100:.0f}%"))

    raw = dd_pts + confirm_pts + range_pts
    if confirmed:
        score = raw
    else:
        score = min(raw, 30)
        if raw > score:
            reasons.append("확인캔들 미충족 — falling-knife guard로 30점 상한")
            breakdown.append(("떨어지는 칼날 가드", score - raw, 0, "확인캔들 없어 30점 상한"))

    return score, reasons, breakdown


def _event_score(daily: pd.DataFrame, report_date: date | None) -> tuple[int, list[str], list[tuple[str, int, int, str]]]:
    close, open_ = daily["close"], daily["open"]
    reasons: list[str] = []
    breakdown: list[tuple[str, int, int, str]] = []

    prev_close = close.iloc[-2]
    gap = (open_.iloc[-1] - prev_close) / prev_close * 100 if prev_close else 0.0
    gap_pts = 30 if gap >= 1.0 else 0
    reasons.append(f"갭업 {gap:+.2f}% (+{gap_pts})")
    breakdown.append(("갭업", gap_pts, 30, f"{gap:+.2f}%"))

    rvol = _rvol(daily)
    if rvol >= 3.0:
        rvol_pts = 40
    elif rvol >= 2.0:
        rvol_pts = 25
    else:
        rvol_pts = 0
    reasons.append(f"RVOL {rvol:.2f} (+{rvol_pts})")
    breakdown.append(("거래량 스파이크", rvol_pts, 40, f"RVOL {rvol:.2f}"))

    if report_date is None:
        fresh_pts = 0
        reasons.append("리포트 날짜 없음 (+0)")
        breakdown.append(("뉴스 신선도", 0, 30, "리포트 날짜 없음"))
    else:
        last_bar_date = _as_date(daily.index[-1])
        age_days = abs((last_bar_date - report_date).days)
        if age_days <= 2:
            fresh_pts = 30
            reasons.append(f"리포트 발행일 신선도 {age_days}일 이내 (+30)")
            breakdown.append(("뉴스 신선도", 30, 30, f"발행 {age_days}일 이내"))
        else:
            fresh_pts = 0
            reasons.append(f"리포트 발행일 {age_days}일 전 — 뒷북 감점 (+0)")
            breakdown.append(("뉴스 신선도", 0, 30, f"발행 {age_days}일 전 — 뒷북"))

    return gap_pts + rvol_pts + fresh_pts, reasons, breakdown


_PROFILE_SCORERS = {
    "TREND": lambda daily, report_date: _trend_score(daily),
    "REBOUND": lambda daily, report_date: _rebound_score(daily),
    "EVENT": lambda daily, report_date: _event_score(daily, report_date),
    # EVENT_SCALP(갈래 A) — "기존 임계 그대로 태우되 태그만 EVENT_SCALP로"(spec §5/T
    # 배선 지시). 갭업+거래량 스파이크+뉴스 신선도라는 EVENT 프로필의 판단축이 개장
    # 갭을 노리는 단타 후보에도 그대로 맞는다 — 새 채점식을 만들지 않고 별칭한다.
    "EVENT_SCALP": lambda daily, report_date: _event_score(daily, report_date),
    # FRGN(갈래 B) — 외국인 수급 축은 이 파일이 보는 캔들에 없는 신호라 자체 프로필이
    # 없다. 적립 전략은 오버나이트 추세 추종이 본질이라 TREND(가격·거래량 지속성)
    # 프로필이 세 개 중 가장 가까운 물리적 근사다.
    "FRGN": lambda daily, report_date: _trend_score(daily),
    # CLOSE_BET(종가배팅, 2026-08-25 전략 4종 체제 ③) — 장중 리포트가 이미 수급·
    # 거래대금·등락으로 결정론 채점한 뒤 준 태그라, 여기서는 "당일 강세의 지속성"
    # 확인만 하면 된다. 당일 관성(가격·거래량 지속)이 판단축인 TREND 프로필이
    # 가장 가까운 물리적 근사다 — FRGN 과 같은 논리로 별칭한다.
    "CLOSE_BET": lambda daily, report_date: _trend_score(daily),
}


def macro_sector_adjustment(
    symbol: str, sector_map: dict[str, str] | None, sector_tilt: dict[str, dict] | None,
) -> tuple[int, str] | None:
    """자금 흐름 섹터 기울기(§4, 2026-08-31 소유자 지시) → 증거점수 ±2 가감.

    `sector_map`은 `data/ledger/sector_map.json`(네이버 업종, KR 전용 — US
    종목-섹터 매핑은 이 저장소에 없다, `quant.analyze.money_flow` docstring의
    조사 결과 그대로). `sector_tilt`은
    `quant.analyze.money_flow.analyze_money_flow(...)["sector_tilt"]["KR"]`.

    섹터를 모르는 종목(`sector_map`에 없거나 `sector_tilt`이 그 업종을 모름)은
    `None` — 호출부가 0점(불이익 없음)으로 취급한다. 반환 사유 문자열은
    "매크로: <업종> <부호점수> (<근거>)" 형태로, `cmd_watch_score`의 점수
    세부화 출력에 그대로 남는다."""
    if not sector_map or not sector_tilt:
        return None
    sector = sector_map.get(symbol)
    if not sector:
        return None
    entry = sector_tilt.get(sector)
    if entry is None:
        return None
    score = max(-2, min(2, entry["score"]))
    why = " · ".join(entry.get("why") or [])
    reason = f"매크로: {sector} {score:+d}" + (f" ({why})" if why else "")
    return score, reason


def score_symbol(
    daily: pd.DataFrame,
    symbol: str,
    tags: list[str],
    report_date: date | None,
    client,
    today: date | None = None,
    extra_reasons: list[str] | None = None,
    allow_kr_stocks: bool = False,
    macro_sector_adj: tuple[int, str] | None = None,
) -> ScoreResult:
    """일봉 DataFrame(open/high/low/close/volume, 시간 오름차순) 하나를 채점한다.
    `tags`가 비어있으면(무태그) 세 프로필을 모두 계산해 최고점을 취한다(best-of).
    `passed`는 여기서 정하지 않는다 — threshold와 비교는 `run_watch_score`가 한다.

    `macro_sector_adj`(선택) — `macro_sector_adjustment()`가 계산한
    `(점수, 사유)`. 있으면 최종 점수에 그대로 더하고(±2 한도는 호출부가 이미
    보장) breakdown/reasons에 "매크로 섹터 기울기" 항목으로 남긴다."""
    today = today or date.today()
    reasons: list[str] = list(extra_reasons or [])

    # 오늘(및 미래) 날짜의 미완성 행 제거 — 08:40 채점 시점에 오늘 행이 거래량
    # 0/일부로 끼어 있으면 RVOL·갭이 0으로 붕괴한다(2026-08-10 개장 전후 실측).
    # 채점은 항상 "마지막 완성 거래일"까지만 본다.
    if daily is not None and len(daily):
        dates = [_as_date(ts) for ts in daily.index]
        completed = [d < today for d in dates]
        if not all(completed):
            daily = daily[completed]

    if daily is None or len(daily) < _MIN_ROWS:
        reasons.append("데이터 부족(30행 미만)")
        return ScoreResult(symbol=symbol, score=0, passed=False, tags=tags, reasons=reasons, prereq_ok=False)

    is_kr = _is_kr_symbol(symbol)
    prereq_failures, info_reasons = _check_prerequisites(daily, symbol, is_kr, client, today, allow_kr_stocks)
    reasons.extend(info_reasons)

    profiles = tags if tags else list(_VALID_TAGS)
    profile_results = {p: _PROFILE_SCORERS[p](daily, report_date) for p in profiles}
    best_profile = max(profile_results, key=lambda p: profile_results[p][0])
    score, profile_reasons, breakdown = profile_results[best_profile]
    breakdown = list(breakdown)
    reasons.append(f"[{best_profile}] " + "; ".join(profile_reasons))

    if macro_sector_adj is not None:
        adj, adj_reason = macro_sector_adj
        score += adj
        reasons.append(adj_reason)
        breakdown.append(("매크로 섹터 기울기", adj, 2, adj_reason))

    prereq_ok = not prereq_failures
    if prereq_failures:
        reasons = prereq_failures + reasons

    return ScoreResult(
        symbol=symbol, score=score, passed=False, tags=tags, reasons=reasons,
        prereq_ok=prereq_ok, profile=best_profile, breakdown=breakdown,
    )


def effective_threshold(base: int, regime_label: str, is_us: bool = False) -> int:
    """국면별 통과 기준 조정: defensive +10(더 엄격), aggressive -5(더 완화),
    neutral +0. US 심볼은 비용 마진이 얇아 추가로 +5(비용 페널티를 점수가 아니라
    문턱으로 반영)."""
    adjust = {"defensive": 10, "aggressive": -5}.get(regime_label, 0)
    threshold = base + adjust
    if is_us:
        threshold += 5
    return threshold


def resolve_regime_label(regime_state: dict | None, now: datetime | None = None) -> tuple[str, str | None]:
    """regime.json(raw dict, RegimeState.to_dict 스키마)에서 유효 라벨을 결정한다.
    `computed_at`이 24시간 이상 지났으면 neutral로 강제하고 사유를 반환한다(M1 수정
    — 조용히 대체하지 않는다). state가 없으면(파일 없음 등) 기본 neutral, 사유 없음
    — 이건 정상 케이스(최초 실행 등)라 경보 사유가 아니다."""
    if not regime_state:
        return "neutral", None

    label = regime_state.get("label", "neutral")
    computed_at_raw = regime_state.get("computed_at")
    if not computed_at_raw:
        return label, None

    try:
        computed_at = datetime.fromisoformat(computed_at_raw)
    except Exception:
        return "neutral", "regime.json computed_at 파싱 실패 — neutral로 대체"

    reference = now if now is not None else datetime.now(computed_at.tzinfo)
    age = reference - computed_at
    if age > timedelta(hours=24):
        return "neutral", f"regime.json이 {age.total_seconds() / 3600:.1f}시간 전 데이터 — neutral로 대체"
    return label, None


def discover_candidates(client, market: str = "KR", count: int = 30, top: int = 10) -> list[str]:
    """Toss 거래대금 랭킹에서 리포트 밖 후보를 발굴한다 (2026-08-10 사용자 지시:
    "리포트 하나에 종속되지 말 것 — 제공되는 API를 적극 활용").

    거래대금 급증 상위 = "오늘 시장이 실제로 쳐다보는 종목"(stocks-in-play) —
    ORB 문헌의 선별 축과 같은 논리라 TREND 태그를 붙여 같은 확신도 엔진을 태운다.
    실패는 빈 리스트 — 발굴은 보너스 경로라 절대 본 채점을 막지 않는다."""
    try:
        result = client.rankings(
            type="MARKET_TRADING_AMOUNT", market_country=market,
            duration="realtime", count=count, exclude_investment_caution=True,
        )
        out: list[str] = []
        seen: set[str] = set()
        for row in (result or {}).get("rankings", []):
            symbol = str(row.get("symbol", "")).strip()
            if not symbol or symbol in seen:
                continue
            if market == "KR" and not _is_kr_symbol(symbol):
                continue
            seen.add(symbol)
            out.append(f"{symbol}:TREND")
            if len(out) >= top:
                break
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("후보 발굴 실패 — 빈 목록: %s: %s", type(e).__name__, e)
        return []


def market_flow_adjustment(client) -> tuple[int, str]:
    """KRX 시장 수급 조류 → KR 심볼 임계값 조정 (2026-08-10 사용자 요청).

    외국인+기관 합산 순매수(KOSPI+KOSDAQ, 가장 최근 기록)가 개인보다 "힘있게"
    들어오는 날인지 본다 — 리포트 논지의 수급 뒷받침 근거. Toss investor-trading은
    시장 전체만 제공하므로(개별 종목 불가) 점수가 아닌 **임계값 조정**으로 얹는다:
      합산 > +0.3조 → -5 (순풍: 통과 문턱 완화)
      합산 < -0.3조 → +5 (역풍: 문턱 강화)
      그 사이/조회 실패 → 0 (중립, 조용히 강등)
    종목별 수급(키움 REST 기관-외국인)은 모의서버 검증 후 2단계로 교체 예정."""
    try:
        net = 0
        for market in ("KOSPI", "KOSDAQ"):
            rec = (client.investor_trading(market, "1d", 2).get("records") or [{}])[0]
            for side in ("foreigner", "institution"):
                amt = rec.get(side) or {}
                net += int(amt.get("buyAmount", 0)) - int(amt.get("sellAmount", 0))
        if net > 3e11:
            return -5, f"시장 수급 순풍: 외국인+기관 순매수 {net / 1e12:+.2f}조 (임계 -5)"
        if net < -3e11:
            return +5, f"시장 수급 역풍: 외국인+기관 순매수 {net / 1e12:+.2f}조 (임계 +5)"
        return 0, f"시장 수급 중립: 외국인+기관 순매수 {net / 1e12:+.2f}조"
    except Exception as e:  # noqa: BLE001 — 수급 조회 실패가 채점을 막으면 안 된다
        return 0, f"시장 수급 조회 실패(조정 없음): {type(e).__name__}"


def symbol_flow_adjustment(kiwoom_client, symbol: str, today: date) -> tuple[int, str] | None:
    """키움 ka10059 **종목별** 기관-외국인 수급 → 임계 조정 (시장 조류보다 우선).

    가장 최근 완성일의 외국인+기관 순매수 합산 부호로 ±5. 조회 실패·데이터 없음이면
    None을 반환해 시장 레벨 조류(market_flow_adjustment)로 폴백한다 — 키움이 죽어도
    채점은 계속된다(어댑터 예외 흡수 원칙). 2026-08-10 실전 서버 실측으로 검증된
    필드(frgnr_invsr/orgn)만 읽는다."""
    try:
        rows = kiwoom_client.investor_flow_daily(symbol, today.strftime("%Y%m%d"))
        if not rows:
            return None

        def _num(v) -> int:
            s = str(v or "0").replace("+", "").replace(",", "")
            return int(s) if s.lstrip("-").isdigit() else 0

        # 첫 행이 장 시작 전 오늘 날짜(전부 0)일 수 있다 — 실측(2026-08-10 새벽).
        # 값이 있는 첫 행 = 마지막 완성 거래일을 쓴다.
        r0 = next(
            (r for r in rows[:5] if _num(r.get("frgnr_invsr")) or _num(r.get("orgn"))),
            rows[0],
        )
        frgn, orgn = _num(r0.get("frgnr_invsr")), _num(r0.get("orgn"))
        net = frgn + orgn
        label = f"외국인 {frgn:+,} 기관 {orgn:+,} ({r0.get('dt', '?')})"
        if net > 0:
            return -5, f"종목 수급 순풍: {label} (임계 -5)"
        if net < 0:
            return +5, f"종목 수급 역풍: {label} (임계 +5)"
        return 0, f"종목 수급 중립: {label}"
    except Exception:  # noqa: BLE001 — 키움 조회 실패는 시장 조류 폴백으로
        return None


def run_watch_score(
    tokens: list[str],
    client,
    threshold: int,
    regime_label: str,
    enabled: bool = True,
    today: date | None = None,
    kiwoom_client=None,
    allow_kr_stocks: bool = False,
    sector_map: dict[str, str] | None = None,
    sector_tilt: dict[str, dict] | None = None,
) -> list[ScoreResult]:
    """토큰(`SYMBOL[:TAGS[:YYYYMMDD]]`) 목록을 채점한다. `enabled=False`면 네트워크를
    전혀 부르지 않고 전부 FAIL 처리한다(설정으로 자동채점을 끈 경우). 종목 하나의
    조회/채점 실패가 전체를 막지 않도록 예외는 종목 단위로 삼킨다.

    `sector_map`/`sector_tilt`(선택, §4 2026-08-31) — 각각 `data/ledger/
    sector_map.json`(KR 종목→네이버 업종)과
    `quant.analyze.money_flow.analyze_money_flow(...)["sector_tilt"]["KR"]`.
    둘 다 있으면 KR 종목에 `macro_sector_adjustment()`로 증거점수 ±2를
    가감한다. US 종목은 섹터 매핑이 없어(모듈 docstring 조사 결과) 항상
    영향받지 않는다 — 모르는 건 불이익이 아니다."""
    if not enabled:
        results = []
        for token in tokens:
            symbol, tags, _, _ = _parse_token(token)
            results.append(ScoreResult(
                symbol=symbol, score=0, passed=False, tags=tags,
                reasons=["auto_score 비활성"], prereq_ok=False,
            ))
        return results

    # 시장 수급 조류는 KR 후보가 있을 때만 1회 조회 — 종목 루프 밖(호출 2번 고정).
    flow_adj, flow_reason = (0, "")
    if any(_is_kr_symbol(_parse_token(t)[0]) for t in tokens):
        flow_adj, flow_reason = market_flow_adjustment(client)

    results: list[ScoreResult] = []
    for token in tokens:
        symbol, tags, report_date, parse_reasons = _parse_token(token)
        is_us = not _is_kr_symbol(symbol)
        eff_threshold = effective_threshold(threshold, regime_label, is_us=is_us)
        # KR: 종목별 수급(키움)이 있으면 그것을, 없으면 시장 조류(토스)를 임계에 적용.
        sym_adj, sym_reason = (None, "")
        if not is_us and kiwoom_client is not None:
            sym_flow = symbol_flow_adjustment(kiwoom_client, symbol, today or date.today())
            if sym_flow is not None:
                sym_adj, sym_reason = sym_flow
        if not is_us:
            if sym_adj is not None:
                eff_threshold += sym_adj
            elif flow_adj:
                eff_threshold += flow_adj
        macro_adj = None if is_us else macro_sector_adjustment(symbol, sector_map, sector_tilt)
        try:
            daily = client.candles(symbol, interval="day", count=90)
            result = score_symbol(
                daily, symbol, tags, report_date, client, today=today, extra_reasons=parse_reasons,
                allow_kr_stocks=allow_kr_stocks, macro_sector_adj=macro_adj,
            )
        except Exception as e:
            result = ScoreResult(
                symbol=symbol, score=0, passed=False, tags=tags,
                reasons=[f"조회 실패: {type(e).__name__}: {e}"], prereq_ok=False,
            )
        if not is_us:
            if sym_reason:
                result.reasons.append(sym_reason)
            elif flow_reason:
                result.reasons.append(flow_reason)
        # 임계값 구성 명세(세분화 출력용) — 어디서 몇 점이 붙고 깎였는지 전부 보인다.
        notes = [f"기본 {threshold}"]
        regime_adj = {"defensive": "+10 방어국면", "aggressive": "-5 공격국면"}.get(regime_label)
        if regime_adj:
            notes.append(regime_adj)
        if is_us:
            notes.append("+5 US비용")
        else:
            applied = sym_adj if sym_adj is not None else flow_adj
            if applied:
                notes.append(f"{applied:+d} 수급{'역풍' if applied > 0 else '순풍'}")
        result.eff_threshold = eff_threshold
        result.threshold_notes = notes
        result.passed = result.prereq_ok and result.score >= eff_threshold
        results.append(result)
    return results
