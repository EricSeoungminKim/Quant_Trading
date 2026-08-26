"""전략 사유 문자열용 가격 표시 헬퍼.

`%.4g` 같은 공학 표기(예: "2.178e+05")는 사람이 읽을 수 없다(2026-08-26 소유자
지적: "손절 가격이랑 익절 가격들이 공식으로만 쓰여있어서 내가 읽을 수가 없잖아").
`market_of_symbol`로 심볼의 시장을 판단해 KR은 원 단위 정수, US는 센트까지
표시한다. 거래 평면 공용 자리 — `quant.core`만 의존한다(외부 의존 0).
"""
from __future__ import annotations

from quant.core.models import market_of_symbol


def fmt_price(value: float, symbol: str) -> str:
    """가격 하나를 사람이 읽는 형식으로. KR="1,234,000" / US="123.45"."""
    if market_of_symbol(symbol) == "KR":
        return f"{value:,.0f}"
    return f"{value:,.2f}"
