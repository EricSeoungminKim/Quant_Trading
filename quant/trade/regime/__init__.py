"""국면(방어/중립/공격) 판단 → risk_multiplier 하나.

하루 1회 세션 시작 전에만 계산하고(RegimeProvider.refresh()), 거래 사이클은 캐시된
값만 읽는다(RegimeProvider.risk_multiplier() — 네트워크 호출 없음). 규칙 기반,
해석 가능(ML 없음). 통합 지점은 risk_multiplier() 하나뿐 — risk/·app/·brokers/
배선은 이 패키지 밖(오케스트레이터) 책임이다. 설계 배경은 docs/adr/0009 참고.
"""
from quant.trade.regime.indicators import IndicatorResult
from quant.trade.regime.interfaces import BitcoinPriceAdapter, MarketIndicatorClient
from quant.trade.regime.models import RegimeState
from quant.trade.regime.provider import DEFAULT_MULTIPLIERS, RegimeProvider

__all__ = [
    "RegimeState",
    "RegimeProvider",
    "MarketIndicatorClient",
    "BitcoinPriceAdapter",
    "IndicatorResult",
    "DEFAULT_MULTIPLIERS",
]
