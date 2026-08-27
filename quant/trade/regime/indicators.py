"""국면 판단에 쓰는 개별 지표 채점 규칙. 전부 순수 함수 — I/O 없음, 결정론적.

각 함수는 +1(위험선호)/0(중립)/-1(위험회피) 정수 점수와, 사람이 읽을 수 있는 판단
사유 문자열을 함께 반환한다. 입력을 못 구했거나(예: API 실패, 데이터 부족) 계산에
필요한 최소 조건을 못 채우면 score=None으로 해당 지표를 집계에서 제외한다 — 억지로
0(중립)을 반환하지 않는다. 0은 "판단했는데 중립"이고 None은 "판단 못 함"이다.

**이 점수 규칙이 수익을 낸다는 증거는 없다.** ML 없이 규칙 기반·해석 가능하게
설계했고, 목적은 명백한 하락/과열 국면에서 노출을 줄이는 방어이지 알파 창출이
아니다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class IndicatorResult:
    name: str
    score: int | None  # +1/0/-1, None=판단 불가(집계에서 제외)
    reason: str


def qqq_trend_score(daily_closes: pd.Series, ma_window: int = 20, band_pct: float = 1.0) -> IndicatorResult:
    """QQQ 종가의 ma_window일 이동평균 대비 위치(추세).

    이평선을 band_pct% 이상 상회하면 +1(추세 상승), band_pct% 이상 하회하면
    -1(추세 하락), 그 사이는 0(횡보)."""
    name = "qqq_trend"
    if daily_closes is None or len(daily_closes) < ma_window:
        return IndicatorResult(name, None, f"QQQ 일봉 {ma_window}개 미만 — 판단 불가")
    ma = daily_closes.tail(ma_window).mean()
    last = daily_closes.iloc[-1]
    if ma == 0 or pd.isna(ma) or pd.isna(last):
        return IndicatorResult(name, None, "QQQ 이평선/종가 계산 불가")
    dist_pct = (last - ma) / ma * 100
    if dist_pct >= band_pct:
        return IndicatorResult(name, 1, f"QQQ 종가가 {ma_window}일 이평 대비 {dist_pct:+.2f}% — 추세 상승")
    if dist_pct <= -band_pct:
        return IndicatorResult(name, -1, f"QQQ 종가가 {ma_window}일 이평 대비 {dist_pct:+.2f}% — 추세 하락")
    return IndicatorResult(name, 0, f"QQQ 종가가 {ma_window}일 이평 대비 {dist_pct:+.2f}% — 횡보")


def qqq_volatility_score(
    daily_closes: pd.Series,
    short_window: int = 5,
    long_window: int = 60,
    high_ratio: float = 1.3,
    low_ratio: float = 0.8,
) -> IndicatorResult:
    """최근 short_window일 실현변동성(일간수익률 표준편차) vs long_window일 변동성(변동성 국면).

    단기 변동성이 장기 대비 high_ratio배 이상이면 -1(변동성 급등=위험회피),
    low_ratio배 이하면 +1(안정=위험선호), 그 사이는 0(평상 수준)."""
    name = "qqq_volatility"
    if daily_closes is None or len(daily_closes) < long_window + 1:
        return IndicatorResult(name, None, f"QQQ 일봉 {long_window + 1}개 미만 — 판단 불가")
    returns = daily_closes.pct_change().dropna()
    if len(returns) < long_window:
        return IndicatorResult(name, None, f"QQQ 일간수익률 {long_window}개 미만 — 판단 불가")
    short_vol = returns.tail(short_window).std()
    long_vol = returns.tail(long_window).std()
    if long_vol == 0 or pd.isna(long_vol) or pd.isna(short_vol):
        return IndicatorResult(name, None, "변동성 계산 불가(표준편차 0 또는 NaN)")
    ratio = short_vol / long_vol
    if ratio >= high_ratio:
        return IndicatorResult(name, -1, f"{short_window}일 변동성이 {long_window}일 대비 {ratio:.2f}배 — 변동성 급등")
    if ratio <= low_ratio:
        return IndicatorResult(name, 1, f"{short_window}일 변동성이 {long_window}일 대비 {ratio:.2f}배 — 안정 국면")
    return IndicatorResult(name, 0, f"{short_window}일 변동성이 {long_window}일 대비 {ratio:.2f}배 — 평상 수준")


def bond_yield_score(change_bp: float | None, band_bp: float = 3.0) -> IndicatorResult:
    """미국 국채 10년물(US_BOND_10Y, FRED DGS10) 수익률 변화(bp, 전일 대비).

    금리가 band_bp 이상 상승하면 -1(할인율 상승=위험회피), band_bp 이상 하락하면
    +1(위험선호), 그 사이는 0.

    2026-08-28: 이전엔 Toss API가 국내 지표만 지원해 국내 국채(KR_BOND_10Y, 항상
    None)를 대신할 수도 없었고 코스피와 함께 "거시 리스크심리 대리지표"로 쓸
    수밖에 없었다 — 그런데 이 저장소가 거래하는 TQQQ/SQQQ는 미국 레버리지 ETF라
    국내 금리는 애초에 직접적 동인이 아니었다(docs/adr/0009, kospi_score 문서
    참고). quant/adapters/macro/fred.py로 실제 미국 10년물을 수집하게 되면서
    대리지표 대신 직접 지표로 바꿨다 — 호출부는
    quant/trade/regime/provider.py._bond_yield_indicator."""
    name = "us_bond_yield"
    if change_bp is None:
        return IndicatorResult(name, None, "국채 10년 금리 조회 실패 — 지표 제외")
    if change_bp >= band_bp:
        return IndicatorResult(name, -1, f"국채 10년 금리 {change_bp:+.1f}bp — 금리 상승(위험회피 신호)")
    if change_bp <= -band_bp:
        return IndicatorResult(name, 1, f"국채 10년 금리 {change_bp:+.1f}bp — 금리 하락(위험선호 신호)")
    return IndicatorResult(name, 0, f"국채 10년 금리 {change_bp:+.1f}bp — 변화 미미")


def kospi_score(change_pct: float | None, band_pct: float = 0.5) -> IndicatorResult:
    """코스피 지수 등락률(전일 대비 %).

    band_pct 이상 상승하면 +1(위험선호), band_pct 이상 하락하면 -1(위험회피),
    그 사이는 0. 국내 지수라 TQQQ/SQQQ(미국 레버리지 ETF)의 직접적 동인은
    아니다 — 대리지표 한계는 그대로 남는다(2026-08-28: bond_yield_score는
    US_BOND_10Y로 바뀌어 이 한계를 벗어났다 — 대체 가능한 미국 지수 소스가
    아직 없어 코스피만 대리지표로 남았다)."""
    name = "kospi"
    if change_pct is None:
        return IndicatorResult(name, None, "코스피 조회 실패 — 지표 제외")
    if change_pct >= band_pct:
        return IndicatorResult(name, 1, f"코스피 {change_pct:+.2f}% — 위험선호")
    if change_pct <= -band_pct:
        return IndicatorResult(name, -1, f"코스피 {change_pct:+.2f}% — 위험회피")
    return IndicatorResult(name, 0, f"코스피 {change_pct:+.2f}% — 변화 미미")


def bitcoin_score(change_pct: float | None, band_pct: float = 2.0) -> IndicatorResult:
    """비트코인 가격 등락률(%, 전일 대비 또는 최근 24시간).

    band_pct 이상 상승하면 +1(위험선호 확산), band_pct 이상 하락하면 -1(위험회피
    확산), 그 사이는 0. 어댑터 미구현/조회 실패 시 change_pct=None → 지표 제외.

    비트코인 가격 소스는 [미결정] — interfaces.BitcoinPriceAdapter Protocol만
    정의돼 있고 구현체는 없다. 구현 전까지 이 지표는 항상 제외된다."""
    name = "bitcoin"
    if change_pct is None:
        return IndicatorResult(name, None, "비트코인 어댑터 미구현/조회 실패 — 지표 제외")
    if change_pct >= band_pct:
        return IndicatorResult(name, 1, f"비트코인 {change_pct:+.2f}% — 위험선호 확산")
    if change_pct <= -band_pct:
        return IndicatorResult(name, -1, f"비트코인 {change_pct:+.2f}% — 위험회피 확산")
    return IndicatorResult(name, 0, f"비트코인 {change_pct:+.2f}% — 변화 미미")
