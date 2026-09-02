"""PnL 귀속 요약 — 결정론, LLM 없음. 리포팅 레이어(2026-09-02, 소유자 지시
"회사형 AI 에이전트 레이어" 레인 3).

## 왜 LLM 을 안 쓰는가 (명시적 선택)

그날 순손익을 [엣지(수수료 전 실현) − 수수료 − 세금]으로 나누는 것은 사칙연산
이다 — `quant.control.ledger.session_pnl_summary()`가 이미 gross/fees/net 을
계산해 두므로, 여기서 더할 것은 세금 성분 분해와 전략별 상/하위 선정뿐이다.
숫자 요약에 환각 리스크를 질 이유가 없다(이 저장소 원칙: "숫자가 자본을
배분한다" — LLM 은 판단이 필요한 곳에만 쓴다, ADR-0002).

## 세금 성분 — "정확한 값"이 아니라 "상한 추정치"

체결 원장(`trades.jsonl`)의 `fee` 필드는 수수료와 세금이 이미 합산된 값이다
(`quant.adapters.execution.paper.PaperBroker`가 체결 시점에 하나로 계산해
기록한다 — 별도 세금 필드가 없다). 이 모듈은 그걸 역산한다: KR 매도 체결의
명목 × `kr_stock_sell_tax_bps`를 세금으로 추정한다.

**이 저장소는 ETF 매도세 면제 목록(`kr_etf_symbols`)을 실시간 조회(Toss
`stock_info`)로만 안다 — 원장에는 남지 않는다.** 그래서 이 추정치는 ETF를
빼지 못한 **상한**이다(실제 세금은 이보다 작거나 같다). 요약에 "추정"이라고
반드시 표기하고, 상한이 실제 수수료 총액을 넘어서면(=세션이 ETF 위주라
추정이 크게 어긋났다는 신호) 수수료 성분이 음수가 되지 않도록 0으로
클램프하고 그 사실을 `tax_clamped`로 남긴다 — 없는 정밀도를 지어내지 않는다
(`quant.control.cost_model` 모듈 docstring과 같은 원칙).

이 모듈 자신은 파일 I/O를 하지 않는다 — 호출부(`quant.apps.cli`)가 이미 읽은
`session_pnl_summary()` 출력과 세션 체결 목록을 넘긴다.
"""
from __future__ import annotations

PRODUCER = "pnl_attribution"


def _kr_sell_tax(trade: dict, tax_bps: float) -> float:
    """체결 1건의 KR 매도세 추정(ETF 면제 미반영 — 모듈 docstring 참고)."""
    if str(trade.get("market")) != "KR":
        return 0.0
    if str(trade.get("side", "")).upper() == "BUY":
        return 0.0
    if tax_bps <= 0:
        return 0.0
    notional = float(trade.get("qty", 0) or 0) * float(trade.get("price", 0) or 0)
    return notional * tax_bps / 1e4


def decompose(session: dict, session_trades: list[dict], kr_stock_sell_tax_bps: float) -> dict:
    """`session_pnl_summary()` 출력 + 세션 체결 목록 → [엣지 − 수수료 − 세금] 분해.

    `edge`(수수료 전 실현손익) − `commission` − `tax` == `net`(수수료 차감 후
    실현손익)이 항상 성립한다 — 이게 이 함수의 유일한 산수 계약이다."""
    edge = float(session.get("gross_realized", 0.0))
    fees_total = float(session.get("fees", 0.0))
    net = edge - fees_total

    tax_upper_bound = sum(_kr_sell_tax(t, kr_stock_sell_tax_bps) for t in session_trades)
    tax_clamped = tax_upper_bound > fees_total
    tax = min(tax_upper_bound, fees_total) if fees_total > 0 else 0.0
    commission = fees_total - tax

    return {
        "edge": edge, "commission": commission, "tax": tax, "net": net,
        "tax_is_estimate": True, "tax_clamped": tax_clamped,
    }


def top_bottom_strategies(by_strategy: dict) -> tuple[dict | None, dict | None]:
    """`session_pnl_summary()["by_strategy"]` → (최고 기여, 최저 기여) 전략 1개씩.

    각 항목은 `{"strategy": id, "net": gross-fees}`. 전략이 하나뿐이면 최고/
    최저가 같은 전략을 가리킨다(그 자체가 사실이므로 숨기지 않는다). 전략이
    없으면 `(None, None)`."""
    if not by_strategy:
        return None, None
    rows = [
        {"strategy": sid, "net": float(d.get("gross", 0.0)) - float(d.get("fees", 0.0))}
        for sid, d in by_strategy.items()
    ]
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows[0], rows[-1]


def _fmt(v: float, market: str) -> str:
    return f"{v:,.0f}원" if market == "KR" else f"${v:,.2f}"


def format_summary(market: str, date_str: str, decomp: dict,
                   top: dict | None, bottom: dict | None) -> str:
    """정확히 4줄 요약(소유자 지시 형식). notify_auto로 그대로 발송한다."""
    unit_note = " (추정 상한 — ETF 면제 미반영)" if decomp["tax"] > 0 else ""
    lines = [
        f"📊 PnL 귀속 — {market} {date_str} 순손익 {_fmt(decomp['net'], market)}",
        f"분해: 엣지 {_fmt(decomp['edge'], market)} − 수수료 {_fmt(decomp['commission'], market)}"
        f" − 세금 {_fmt(decomp['tax'], market)}{unit_note}",
    ]
    if top is not None and bottom is not None and top["strategy"] != bottom["strategy"]:
        lines.append(f"최고 기여: [{top['strategy']}] {_fmt(top['net'], market)}")
        lines.append(f"최저 기여: [{bottom['strategy']}] {_fmt(bottom['net'], market)}")
    elif top is not None:
        lines.append(f"전략: [{top['strategy']}] {_fmt(top['net'], market)} (전략 1개뿐)")
        lines.append("(최고/최저 비교 불가 — 이 세션 전략이 하나뿐)")
    else:
        lines.append("전략별 집계 없음")
        lines.append("")
    return "\n".join(lines)
