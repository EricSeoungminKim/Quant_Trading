"""전략을 순수함수로 만드는 계약 — 엔진 분리 설계 Phase A
(`docs/superpowers/specs/2026-08-19-engine-separation-design.md`).

기존 `Strategy` Protocol(`quant.core.ports.Strategy`, `on_cycle(ctx) -> list[Signal]`)은
전략이 `ctx`로 세계 아무 데나 손을 뻗고 인스턴스 가변 상태를 숨겨 든 채로 판단한다.
이 모듈은 그 대신 "받은 것만 보고 판단해 신호와 다음 상태를 반환하는" 순수함수
계약을 정의한다. 두 계약은 공존한다 — `quant/trade/strategy/shell.py`의
`PureStrategyShell`이 `PureStrategy`를 감싸 기존 `Strategy` Protocol을 만족시키므로,
기존 전략 10종은 한 줄도 바뀌지 않고 계속 돈다.

외부 의존 0 (quant/core/CLAUDE.md). `bars`가 pandas.DataFrame인 이유: `DataFeed.history()`
가 이미 이걸 돌려주고(`quant/core/ports.py`가 이미 pandas를 타입힌트로 쓴다), 여기서
새 타입을 만들면 어댑터 경계에 불필요한 변환 계층만 생긴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol, runtime_checkable

import pandas as pd

from .models import Quote, Signal


@dataclass(frozen=True)
class DataNeeds:
    """전략이 매 사이클 무엇이 필요한지 선언한다. 껍질은 이것만 보고 `ctx`에서
    데이터를 모아 `StrategySnapshot`을 만든다 — 전략은 `ctx`에 직접 손대지 않는다.

    사이클마다 바뀌지 않는 정적 선언이다. 조건부로 다른 것이 필요한 전략(예: 포지션
    유무에 따라 다른 봉을 본다)은 필요한 것의 합집합을 선언한다.
    """
    bars: tuple[tuple[str, str, int], ...] = ()   # (symbol, interval, count)
    quotes: tuple[str, ...] = ()
    needs_positions: bool = False
    # 2026-09-02: 껍질(`PureStrategyShell._snapshot`)은 기본적으로 **닫힌 시장의
    # 심볼은 조회하지 않는다** — 전략 대부분이 `snap.market_open` 이 False 면
    # 아무것도 하지 않으므로 그 조회는 낭비이고, 그 낭비가
    # `cold_fetch_budget_per_cycle` 을 먼저 소진해 열려 있는 시장의 손절 판정을
    # 굶겼다. 프리마켓/시간외를 **의도적으로** 거래하는 전략(scalp_1m —
    # `risk.extended_sessions`)만 이 값을 True 로 선언해 그 게이트를 끈다.
    # 기본 False 가 안전측인 이유: 게이트가 걸려도 그 전략은 어차피 닫힌 시장에서
    # 판단하지 않고, 반대로 열어두면 낭비가 손절을 굶긴다.
    fetch_when_closed: bool = False


@dataclass(frozen=True)
class StrategySnapshot:
    """전략이 판단 시점에 볼 수 있는 세계의 전부 — 이 밖의 것은 볼 수 없다.

    `market_open`/`minutes_to_close`는 시장별 매핑이다 — 껍질이 이 전략의
    `symbols`에서 실제로 쓰이는 시장("US"/"KR")만 채운다. `Clock`은 데이터가 없는
    시장을 물으면 명시적으로 예외를 낸다(`quant/backtest/engine.py`: "되는 척하지
    않는다"가 설계 원칙) — 그래서 전략이 거래하지 않는 시장까지 무조건 묻지
    않는다. `cadence_minutes`는
    `Clock.should_flatten`을 순수 계산으로 재현하는 데 필요하다 — should_flatten
    자체는 전략 고유 파라미터(`flatten_minutes`)를 받으므로 스냅샷 필드로 미리
    계산해 넣을 수 없고, 재현에 필요한 원재료(`minutes_to_close`, `cadence_minutes`)
    만 여기 있다(`quant/core/clock.py`의 `_should_flatten` 참고).

    `lots`는 **이 전략 자신의 몫만** 담는다 — 심볼 하나의 포지션을 여러 전략이
    나눠 가질 수 있다(`Position.meta["lots"]`). 심볼이 이 매핑에 있다는 것 자체가
    "그 심볼의 포지션이 지금 열려 있다"는 뜻이다(원본 `pos.is_open` 판정과 동치).
    비어 있는 dict({})는 "열려 있지만 아직 이 전략이 기록한 lot 필드가 없다"는
    뜻이다(체결 직후 첫 사이클 등).
    """
    now: datetime
    market_open: Mapping[str, bool]
    minutes_to_close: Mapping[str, float | None]
    cadence_minutes: float
    bars: Mapping[tuple[str, str], pd.DataFrame]
    quotes: Mapping[str, Quote]
    lots: Mapping[str, dict]


@dataclass(frozen=True)
class Decision:
    """전략의 판단 결과.

    `next_state`는 껍질이 **보관만 하고 다음 사이클에 그대로 넘긴다** — 이번
    범위에서는 체결 확인 후에만 적용하는 것까지 하지 않는다(그러려면 루프 변경이
    필요하다). risk가 거부하거나 주문이 미체결이어도 `next_state`는 그대로
    반영된다. 체결 연동은 다음 단계다(`quant/trade/strategy/shell.py` 참고).
    """
    signals: tuple[Signal, ...] = ()
    next_state: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class PureStrategy(Protocol):
    id: str
    symbols: list[str]

    def requirements(self) -> DataNeeds:
        """이 전략이 사이클마다 필요로 하는 데이터를 선언한다."""
        ...

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        """받은 스냅샷과 이전 상태만 보고 판단한다. `ctx`도, 인스턴스 가변 상태도
        읽지 않는다 — 같은 (snap, state) 입력에는 같은 Decision을 반환해야 한다."""
        ...
