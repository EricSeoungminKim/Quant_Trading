"""Private Banker — 읽기 전용, 규칙 기반 계좌 진단. LLM 없음.

Toss 실계좌 holdings + 환율 → 한국어 텔레그램 리포트. build_daily_report는 순수 함수라
네트워크 없이 단위 테스트 가능하다. 진단은 항상 "기준 충족 여부"로 제시한다 — 명령형
투자 조언이 아니다 (예: "-22% — 손절 기준(-20%) 초과, 청산 검토 대상").
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_CONCENTRATION_THRESHOLD = 0.40   # 단일 종목 비중 경고 기준
_LEVERAGE_THRESHOLD = 0.50        # 레버리지 ETF 노출 합계 경고 기준
_REVIEW_THRESHOLD = -0.10         # 손실률 -10% 이하: 검토 필요
_EXIT_THRESHOLD = -0.20           # 손실률 -20% 이하: 청산 검토

# 레버리지 배수를 조회하지 못했을 때만 쓰는 최소 폴백. 이 목록에 의존하면 안 된다 —
# 실계좌에는 SOXL/SOXS/UPRO/TMF 등 무엇이든 들어 있을 수 있고, 리스크 경고 도구가
# 모르는 종목을 조용히 "레버리지 아님"으로 처리하는 것이 가장 위험한 실패다.
# 정답은 브로커의 stock_info.leverageFactor다 (run_report가 주입한다).
_FALLBACK_LEVERAGED_SYMBOLS = {"TQQQ", "SQQQ"}


def _to_krw(amount: float, currency: str, fx_rate: float) -> float:
    return amount * fx_rate if currency == "USD" else amount


def _leverage_of(symbol: str, factors: dict[str, float] | None) -> float | None:
    """레버리지 배수(절대값). 알 수 없으면 None — 1.0으로 단정하지 않는다.

    인버스(-3x)도 레버리지이므로 절대값을 쓴다. 호출자가 부호를 정규화해서
    넘겨줄 것이라고 가정하지 않는다 — 공개 함수는 스스로 방어한다."""
    if factors is not None and symbol in factors:
        try:
            return abs(float(factors[symbol]))
        except (TypeError, ValueError):
            return None
    if symbol in _FALLBACK_LEVERAGED_SYMBOLS:
        return 3.0
    return None


def build_daily_report(
    holdings: list[dict],
    fx_rate: float,
    leverage_factors: dict[str, float] | None = None,
) -> str:
    """holdings: [{symbol, currency, qty, avg_cost, price}, ...] (표시 통화 기준 가격/원가).
    fx_rate: USD/KRW 환율.
    leverage_factors: {symbol: 배수}. 브로커에서 조회한 값. 없으면 최소 폴백을 쓰고,
      배수를 모르는 종목은 경고에 '확인 불가'로 명시한다 — 조용히 빼지 않는다.
    반환값은 텔레그램 전송 준비가 된 한국어 문자열."""
    if not holdings:
        return "[일일 계좌 진단]\n총자산: ₩0 (보유 종목 없음)"

    rows = []
    total_value_krw = 0.0
    leveraged_value_krw = 0.0
    unknown_leverage: list[str] = []
    unknown_value_krw = 0.0
    for h in holdings:
        qty = h["qty"]
        avg_cost = h["avg_cost"]
        price = h["price"]
        currency = h["currency"]
        value_krw = _to_krw(qty * price, currency, fx_rate)
        cost_krw = _to_krw(qty * avg_cost, currency, fx_rate)
        pnl_pct = (price / avg_cost - 1) if avg_cost else 0.0
        total_value_krw += value_krw
        factor = _leverage_of(h["symbol"], leverage_factors)
        if factor is None:
            # 조회를 "시도했는데" 빠진 것만 데이터 공백으로 센다. leverage_factors가
            # 아예 None이면 호출자가 조회 자체를 안 한 것이므로 경고 대상이 아니다.
            if leverage_factors is not None:
                unknown_leverage.append(h["symbol"])
                unknown_value_krw += value_krw
        elif factor > 1.0:
            leveraged_value_krw += value_krw
        rows.append({
            "symbol": h["symbol"],
            "value_krw": value_krw,
            "pnl_krw": value_krw - cost_krw,
            "pnl_pct": pnl_pct,
        })

    if total_value_krw <= 0:
        return "[일일 계좌 진단]\n총자산: ₩0 (보유 종목 없음)"

    lines = ["[일일 계좌 진단]", f"총자산: ₩{total_value_krw:,.0f}", "", "[종목별 평가손익/비중]"]
    warnings: list[str] = []
    diagnoses: list[str] = []
    for row in rows:
        weight = row["value_krw"] / total_value_krw
        # 부동소수점 오차(예: 18/20-1 == -0.09999999999999998)가 -10%/-20% 경계에서
        # 잘못 판정하지 않도록 소수 넷째 자리에서 반올림한 값으로 임계값을 비교한다.
        pnl_pct = round(row["pnl_pct"], 4)
        lines.append(
            f"  {row['symbol']}: ₩{row['value_krw']:,.0f} (비중 {weight * 100:.1f}%) "
            f"손익 {row['pnl_krw']:+,.0f}원 ({pnl_pct * 100:+.2f}%)"
        )
        if weight > _CONCENTRATION_THRESHOLD:
            warnings.append(
                f"  ⚠ {row['symbol']} 비중 {weight * 100:.1f}% — 집중도 경고 기준"
                f"({_CONCENTRATION_THRESHOLD * 100:.0f}%) 초과"
            )
        if pnl_pct <= _EXIT_THRESHOLD:
            diagnoses.append(
                f"  {row['symbol']}: {pnl_pct * 100:.1f}% — 손절 기준"
                f"({_EXIT_THRESHOLD * 100:.0f}%) 초과, 청산 검토 대상"
            )
        elif pnl_pct <= _REVIEW_THRESHOLD:
            diagnoses.append(
                f"  {row['symbol']}: {pnl_pct * 100:.1f}% — 손실 기준"
                f"({_REVIEW_THRESHOLD * 100:.0f}%) 초과, 검토 필요"
            )

    leverage_weight = leveraged_value_krw / total_value_krw
    if leverage_weight > _LEVERAGE_THRESHOLD:
        warnings.append(
            f"  ⚠ 레버리지 ETF 노출 합계 {leverage_weight * 100:.1f}% — 기준"
            f"({_LEVERAGE_THRESHOLD * 100:.0f}%) 초과"
        )
    # 모르는 것을 "레버리지 아님"으로 처리하면 위험이 과소보고된다. 단, 모든 리포트에
    # 띄우면 경고가 무의미해지므로, 그 종목들이 전부 레버리지였을 때 기준을 넘길
    # 수 있는 경우에만 — 즉 판정이 실제로 뒤집힐 수 있을 때만 드러낸다.
    if unknown_leverage:
        worst_case = (leveraged_value_krw + unknown_value_krw) / total_value_krw
        if worst_case > _LEVERAGE_THRESHOLD >= leverage_weight:
            warnings.append(
                f"  ⚠ 레버리지 배수 확인 불가: {', '.join(sorted(unknown_leverage))} — "
                f"이들이 레버리지라면 노출 합계가 최대 {worst_case * 100:.1f}%까지 오를 수 있음"
            )
        elif leverage_weight > _LEVERAGE_THRESHOLD:
            warnings.append(
                f"  ⚠ 레버리지 배수 확인 불가: {', '.join(sorted(unknown_leverage))} — "
                f"위 노출 합계에서 제외됐으므로 실제 노출은 더 클 수 있음"
            )

    if warnings:
        lines += ["", "[경고]"] + warnings
    if diagnoses:
        lines += ["", "[손실 종목 진단]"] + diagnoses

    return "\n".join(lines)


def run_report(client, notifier) -> None:
    """Toss holdings + 환율 조회 → build_daily_report → notifier.send.
    데이터 조회 실패는 로깅 후 삼킨다 — 리포트 실패가 엔진에 영향을 주면 안 된다."""
    try:
        raw = client.holdings()
        fx_rate = client.usd_krw()
    except Exception as e:
        logger.warning("일일 리포트 생성 실패 (데이터 조회): %s: %s", type(e).__name__, e)
        return

    holdings = [
        {
            "symbol": item["symbol"],
            "currency": item["currency"],
            "qty": float(item["quantity"]),
            "avg_cost": float(item["averagePurchasePrice"]),
            "price": float(item["lastPrice"]),
        }
        for item in raw.get("items", [])
    ]
    report = build_daily_report(holdings, fx_rate, fetch_leverage_factors(client, holdings))
    notifier.send(report)


def fetch_leverage_factors(client, holdings: list[dict]) -> dict[str, float]:
    """보유 종목의 레버리지 배수를 브로커에서 조회한다 (stock_info.leverageFactor).

    하드코딩 목록에 의존하지 않기 위한 것이다 — 실계좌에는 SOXL/UPRO 등 무엇이든
    들어 있을 수 있다. 조회 실패한 종목은 dict에서 빠지고, 리포트가 '확인 불가'로
    명시한다. 실패를 1.0(레버리지 아님)으로 메우지 않는다.
    """
    factors: dict[str, float] = {}
    for h in holdings:
        symbol = h["symbol"]
        try:
            info = client.stock_info(symbol)
        except Exception as e:
            logger.warning("레버리지 배수 조회 실패 (%s): %s: %s", symbol, type(e).__name__, e)
            continue
        raw_factor = (info or {}).get("leverageFactor")
        if raw_factor is None:
            continue
        try:
            factors[symbol] = abs(float(raw_factor))  # 인버스(-3)도 레버리지다
        except (TypeError, ValueError):
            logger.warning("레버리지 배수 파싱 실패 (%s): %r", symbol, raw_factor)
    return factors
