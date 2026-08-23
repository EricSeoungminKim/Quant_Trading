"""원장(trades.jsonl)과 실제 포트폴리오 현금을 대조한다 — 순수 계산, 네트워크 없음.

"숫자가 자본 배분을 결정한다"는 원칙 아래, 원장 체결로 재구성한 현금 흐름이 실제
포트폴리오 현금과 일치하는지 검사하는 감사 장치다. `reconcile.py`의 Reconciler와는
축이 다르다 — 그건 브로커 잔고 vs 장부이고, 이건 **원장 vs 현금**이다. 이 대조 함수가
없으면 원장이 조용히 현금과 어긋나도 아무도 모른다.

## 계산 규칙 (quant.core.portfolio.portfolio.to_krw와 동일한 환산 규칙)

    매수 체결 현금흐름 = -(qty * price + fee)
    매도 체결 현금흐름 = +(qty * price - fee)
    market == "US"이면 위 값에 usd_krw 환율을 곱해 KRW로 환산한다. KR이면 그대로.

**`realized_pnl`은 절대 쓰지 않는다** — 그건 원가 기준 손익이지 현금 흐름이 아니다.
매도 체결에서 realized_pnl(=(price-avg_cost)*qty)과 현금흐름(=price*qty-fee)은 값도
부호도 다른 별개의 숫자다. 이 둘을 섞으면 대조 자체가 무의미해진다.

## 한계 — 반드시 읽을 것

원장(TradeLedgerSink.on_fill)은 체결 시점의 USD/KRW 환율을 기록하지 않는다. 그래서
이 함수는 호출자가 넘긴 **단일** 환율로 모든 US 체결을 환산하는데, 실제로는 체결마다
그날그날 환율이 달랐을 것이다. US 체결이 하나라도 있으면 그 환율 이력 미보존 때문에
오차가 원리적으로 발생한다 — 이건 원장의 버그가 아니라 데이터 부재의 필연적 결과다.
그래서 US 체결이 있으면 임계값을 완화하되(US 명목총액 x `US_FX_UNCERTAINTY_PCT`),
얼마나 완화했는지는 `AuditResult.us_slack_krw`와 `summary`에 항상 숫자로 드러낸다 —
조용히 봐주지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from quant.core.models import market_of_symbol

# 절대/비율 임계 중 큰 쪽을 쓴다 (예: 10,000원 또는 실제 현금의 0.1% 중 큰 쪽).
ABS_THRESHOLD_KRW = 10_000.0
PCT_THRESHOLD = 0.001  # 0.1%

# US 체결이 있으면 환율 이력 미보존으로 인한 오차를 흡수하기 위해 임계를 완화한다.
# 하루 USD/KRW 변동폭을 넉넉히 3%로 가정하고, US 체결 명목총액(KRW 환산) x 3%를
# 추가 허용치로 더한다. 이 가정은 보수적으로 넓게 잡은 것이며 조용히 적용하지 않고
# summary에 항상 금액과 퍼센트를 명시한다.
US_FX_UNCERTAINTY_PCT = 0.03


@dataclass(frozen=True)
class AuditResult:
    """대조 결과. `reconcilable=False`면 나머지 숫자 필드는 전부 None이다(위장 금지)."""

    reconcilable: bool
    expected_cash_krw: float | None
    actual_cash_krw: float
    diff_krw: float | None
    threshold_krw: float | None
    is_mismatch: bool | None
    has_us_fills: bool
    us_slack_krw: float
    n_trades: int
    summary: str


def _trade_cash_flow_krw(trade: dict, usd_krw: float) -> tuple[float, str]:
    """체결 하나의 현금흐름(KRW)과 시장을 반환한다. 필드가 없거나 side가 이상하면
    0으로 위장하지 않고 즉시 예외를 던진다 — 원장 손상을 조용히 넘기면 대조가 무의미."""
    side = str(trade.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"알 수 없는 side={trade.get('side')!r} (symbol={trade.get('symbol')!r})")
    try:
        qty = float(trade["qty"])
        price = float(trade["price"])
        fee = float(trade.get("fee", 0) or 0)
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"체결 필드 파싱 실패: {trade!r}") from e

    symbol = str(trade.get("symbol", ""))
    market = trade.get("market") or market_of_symbol(symbol)

    notional = qty * price
    flow = -(notional + fee) if side == "BUY" else (notional - fee)
    if market == "US":
        flow *= usd_krw
    return flow, str(market)


def audit_cash(
    trades: list[dict],
    start_capital_krw: float,
    actual_cash_krw: float,
    usd_krw: float,
) -> AuditResult:
    """원장 체결로 재구성한 기대 현금과 실제 현금을 대조한다.

    trades: `ledger.load_trades()`가 반환하는 형태의 dict 리스트
        (키: side, qty, price, fee, symbol, market — market 없으면 심볼로 추론).
    start_capital_krw: 시작 자본(KRW).
    actual_cash_krw: 현재 실제 포트폴리오 현금(KRW).
    usd_krw: US 체결 환산에 쓸 단일 환율. 체결마다 실제 환율이 달랐을 수 있으므로
        US 체결이 섞이면 원리적으로 오차가 생긴다 (모듈 docstring 참고).
    """
    if not trades:
        return AuditResult(
            reconcilable=False,
            expected_cash_krw=None,
            actual_cash_krw=actual_cash_krw,
            diff_krw=None,
            threshold_krw=None,
            is_mismatch=None,
            has_us_fills=False,
            us_slack_krw=0.0,
            n_trades=0,
            summary="대조 불가: 원장에 체결이 없다 — 빈 원장으로는 현금을 설명할 수 없다.",
        )

    total_flow_krw = 0.0
    us_notional_krw = 0.0
    has_us_fills = False
    for t in trades:
        flow, market = _trade_cash_flow_krw(t, usd_krw)
        total_flow_krw += flow
        if market == "US":
            has_us_fills = True
            qty = float(t["qty"])
            price = float(t["price"])
            us_notional_krw += abs(qty * price) * usd_krw

    expected_cash_krw = start_capital_krw + total_flow_krw
    diff_krw = actual_cash_krw - expected_cash_krw

    base_threshold_krw = max(ABS_THRESHOLD_KRW, abs(actual_cash_krw) * PCT_THRESHOLD)
    us_slack_krw = US_FX_UNCERTAINTY_PCT * us_notional_krw if has_us_fills else 0.0
    threshold_krw = base_threshold_krw + us_slack_krw
    is_mismatch = abs(diff_krw) > threshold_krw

    lines = [
        f"기대 현금 {expected_cash_krw:,.0f}원 vs 실제 현금 {actual_cash_krw:,.0f}원"
        f" (원장 체결 {len(trades)}건)",
        f"차액 {diff_krw:+,.0f}원",
        f"임계 {threshold_krw:,.0f}원 (기본 {base_threshold_krw:,.0f}원"
        + (
            f" + US 환율완화 {us_slack_krw:,.0f}원"
            f"[US 명목 {us_notional_krw:,.0f}원 x {US_FX_UNCERTAINTY_PCT:.0%}, "
            "환율 이력 미보존으로 인한 완화]"
            if has_us_fills
            else ""
        )
        + ")",
        "판정: " + ("불일치 — 임계 초과" if is_mismatch else "일치 — 임계 이내"),
    ]

    return AuditResult(
        reconcilable=True,
        expected_cash_krw=expected_cash_krw,
        actual_cash_krw=actual_cash_krw,
        diff_krw=diff_krw,
        threshold_krw=threshold_krw,
        is_mismatch=is_mismatch,
        has_us_fills=has_us_fills,
        us_slack_krw=us_slack_krw,
        n_trades=len(trades),
        summary="\n".join(lines),
    )


def cumulative_cash_trace(
    trades: list[dict], start_capital_krw: float, usd_krw: float
) -> list[dict]:
    """체결 순서대로 기대 현금이 어떻게 누적되는지 추적한다 (진단용, 대조 판정에는 안 씀).

    각 원소: {index, ts, symbol, side, market, flow_krw, running_expected_cash_krw}.
    실제 현금은 체결 시점별로 남지 않으므로(포트폴리오는 최종 스냅샷만 보존) 이 자체로
    "어느 체결에서 어긋났는지"를 단정할 수는 없다 — 다만 기대 현금의 궤적을 눈으로
    살펴 이상 지점(비정상적으로 큰 흐름 등)을 찾는 데 쓴다.
    """
    running = start_capital_krw
    out: list[dict] = []
    for i, t in enumerate(trades):
        flow, market = _trade_cash_flow_krw(t, usd_krw)
        running += flow
        out.append({
            "index": i,
            "ts": t.get("ts"),
            "symbol": t.get("symbol"),
            "side": t.get("side"),
            "market": market,
            "flow_krw": flow,
            "running_expected_cash_krw": running,
        })
    return out
