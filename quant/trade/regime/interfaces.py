"""regime/ 전용 Protocol 경계 — domain/interfaces.py의 Clock/DataFeed/Broker/...
계열과는 별개다. market-indicator/비트코인 클라이언트는 코어 실행 루프(전략·리스크·
브로커)가 의존하는 포트가 아니라 이 패키지의 하루 1회 계산에만 쓰이는 좁은 입력이라
domain/에 넣지 않는다.

실패 표현은 domain과 같은 관례를 따른다: 조회 실패는 예외가 아니라 None으로
표현한다. 구현체(어댑터)가 네트워크 예외를 내부에서 삼키고 None을 반환할 책임을
진다 — 이 패키지 쪽에서도 방어적으로 예외를 잡지만, 계약상 기대되는 실패 형태는
None이다.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MarketIndicatorClient(Protocol):
    """Toss market-indicators API(`/api/v1/market-indicators/*`) 클라이언트가 구현.
    지원 심볼은 카탈로그 8종(KOSPI, KOSDAQ, KR_BOND_2Y/3Y/5Y/10Y/20Y/30Y)뿐이다."""

    def indicator_price(self, symbol: str) -> float | None:
        """symbol의 최신가(지수 포인트 또는 국채 수익률 %). 조회 실패 시 None."""
        ...

    def indicator_prev_close(self, symbol: str) -> float | None:
        """symbol의 전일 종가/전일 수익률(등락 계산의 기준점). 조회 실패 시 None."""
        ...


@runtime_checkable
class BitcoinPriceAdapter(Protocol):
    """비트코인 가격 소스. **구현체는 아직 없다 — 어떤 공개 API를 쓸지 [미결정].**
    None을 반환하는 어댑터를 기본값으로 쓰면 bitcoin_score()가 해당 지표를 항상
    집계에서 제외한다(판단 불가 ≠ 위험회피/위험선호)."""

    def price_change_pct(self) -> float | None:
        """전일 대비(또는 최근 24시간) 가격 등락률(%). 조회 실패/미구현 시 None."""
        ...
