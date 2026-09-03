"""전일/당일 상한가 진입 금지 레일(2026-09-03, 소유자 결정) 테스트.

근거: KR 일봉 2016~2026 실측(3,263종목, 백테스트) — 상한가(+29.5%) 다음날 매수가
−227bp(1일 보유)~−358bp(대형주 유니버스)로 전체 스크린에서 가장 강하고 일관된
음의 엣지였다("상한가 종목 진입 금지 필터 단타 전략에 바로 적용해줘").

RiskManagerImpl.approve()가 ENTER_LONG/SCALE_IN만 막는다 — 청산은 절대 영향받지
않는다(이 파일의 회로차단기 공통 원칙, tests/test_risk_circuit_breakers.py와 동일
전제). 데이터 조회 실패(빈 프레임/DataSourceError)는 차단이 아니라 통과다 — 데이터
장애가 거래 정지로 번지면 안 된다는 이 저장소의 반복된 교훈.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.core.ports import DataSourceError
from quant.core.models import Position, Signal, SignalAction
from quant.trade.risk.manager import RiskManagerImpl

from tests.test_risk_circuit_breakers import (
    _FakeBroker,
    _FakeData,
    _ctx,
    _exit,
    _risk_cfg,
)

KST = ZoneInfo("Asia/Seoul")
_KR_SYMBOL = "005930"
_US_SYMBOL = "TQQQ"
_MARKET_OF = {_KR_SYMBOL: "KR", _US_SYMBOL: "US"}
_NOW = datetime(2026, 9, 3, 10, 0, tzinfo=KST)
_CAPITAL_FRACTION = {"scalp_1m": 1.0, "scalp_1m_cat": 1.0, "donchian": 1.0}


def _daily_bars(closes: list[float], end: datetime = _NOW) -> pd.DataFrame:
    """일봉 합성 — closes는 시간순(오래된 것 먼저), 마지막 값이 "마지막 완성 세션"."""
    rows = []
    for i, c in enumerate(closes):
        ts = end - timedelta(days=(len(closes) - 1 - i))
        rows.append({"ts": ts, "open": c, "high": c, "low": c, "close": c, "volume": 1_000_000.0})
    return pd.DataFrame(rows).set_index("ts")


def _signal(
    symbol: str, strategy_id: str = "scalp_1m", action: SignalAction = SignalAction.ENTER_LONG,
    target_weight: float = 0.5,
) -> Signal:
    return Signal(strategy_id=strategy_id, symbol=symbol, action=action, target_weight=target_weight)


def _risk(**risk_overrides) -> RiskManagerImpl:
    return RiskManagerImpl(
        _risk_cfg(**risk_overrides), capital_fraction=dict(_CAPITAL_FRACTION), market_of=_MARKET_OF,
    )


def _data_with_bars(price: float, closes: list[float] | None = None,
                     raise_on_history: Exception | None = None) -> _FakeData:
    d = _FakeData(price=price, now=_NOW, raise_on_history=raise_on_history)
    if closes is not None:
        d.bars[_KR_SYMBOL] = _daily_bars(closes)
    return d


# ============================================================= 전일(historical) 상한가

def test_blocks_at_exactly_threshold_29_5_pct(fake_clock_cls):
    """prev_prev_close=100 -> prev_close=129.5 는 정확히 +29.5% — 경계값 포함 차단."""
    risk = _risk()
    data = _data_with_bars(price=129.5, closes=[100.0, 129.5])  # 당일 이동 0% — 전일 체크만 격리
    ctx = _ctx(fake_clock_cls, price=129.5, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is None
    assert "전일 상한가 종목 진입 금지" in risk.last_block
    assert "+29.5%" in risk.last_block


def test_blocks_above_threshold_30_pct(fake_clock_cls):
    risk = _risk()
    data = _data_with_bars(price=130.0, closes=[100.0, 130.0])
    ctx = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is None
    assert "전일 상한가 종목 진입 금지" in risk.last_block


def test_allows_just_below_threshold_29_4_pct(fake_clock_cls):
    risk = _risk()
    data = _data_with_bars(price=129.4, closes=[100.0, 129.4])
    ctx = _ctx(fake_clock_cls, price=129.4, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is not None
    assert "상한가" not in risk.last_block


# ============================================================= 당일(intraday) 상한가

def test_blocks_intraday_limit_up_even_without_prior_day_jump(fake_clock_cls):
    """전일은 평범했지만(0%) 지금 quote가 전일 종가 대비 +30% — 상한가 진행 중인
    종목을 지금 쫓아 들어가는 것도 같은 논지로 막는다."""
    risk = _risk()
    data = _data_with_bars(price=130.0, closes=[100.0, 100.0])
    ctx = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is None
    assert "당일 상한가 진입 금지" in risk.last_block
    assert "+30.0%" in risk.last_block


def test_allows_intraday_move_below_threshold(fake_clock_cls):
    risk = _risk()
    data = _data_with_bars(price=129.4, closes=[100.0, 100.0])
    ctx = _ctx(fake_clock_cls, price=129.4, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is not None


# ============================================================= 데이터 장애 — 절대 차단하지 않는다

def test_missing_daily_bars_allowed_with_debug_log(fake_clock_cls, caplog):
    """일봉 조회가 빈 프레임을 돌려주면(구독 안 된 신규 관심종목 등) 차단하지 않고
    통과시키되, 디버그 로그로 남긴다."""
    risk = _risk()
    data = _data_with_bars(price=100.0, closes=None)  # bars 미등록 -> history()가 빈 프레임
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=_NOW, data=data)

    with caplog.at_level(logging.DEBUG, logger="quant.trade.risk.manager"):
        order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is not None
    assert any("일봉 데이터 없음" in r.message for r in caplog.records)
    assert risk.breaker_state()["prev_limit_up_block"]["data_missing_skips"] == 1


def test_datasource_error_allowed_not_blocked(fake_clock_cls, caplog):
    """어댑터가 DataSourceError를 던져도(콜드 페치 예산 초과 등) 거래를 막지 않는다
    — 이 저장소가 반복적으로 강조하는 원칙: 데이터 장애가 거래 정지로 번지면 안 된다."""
    risk = _risk()
    data = _data_with_bars(price=100.0, raise_on_history=DataSourceError("콜드 페치 예산 초과"))
    ctx = _ctx(fake_clock_cls, price=100.0, cash=10_000_000.0, now=_NOW, data=data)

    with caplog.at_level(logging.DEBUG, logger="quant.trade.risk.manager"):
        order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is not None
    assert any("조회 실패" in r.message for r in caplog.records)
    assert risk.breaker_state()["prev_limit_up_block"]["data_missing_skips"] == 1


# ============================================================= 시장/경로 범위

def test_us_symbol_untouched(fake_clock_cls):
    """markets 기본값은 [KR] — US 심볼은 같은 폭의 상한가성 급등이 있어도 이 레일을
    타지 않는다(미국은 상하한가 제도 자체가 다르다)."""
    risk = _risk()
    data = _FakeData(price=150.0, now=_NOW)
    data.bars[_US_SYMBOL] = _daily_bars([100.0, 150.0])  # +50%
    ctx = _ctx(fake_clock_cls, price=150.0, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_US_SYMBOL, strategy_id="donchian"), ctx)

    assert order is not None
    assert "상한가" not in risk.last_block


def test_exit_never_blocked_even_with_prior_limit_up(fake_clock_cls):
    """청산(EXIT_LONG)은 이 레일의 대상이 아니다 — 손실 포지션을 가두면 안 된다는
    이 파일 전체의 원칙과 동일."""
    risk = _risk()
    positions = {_KR_SYMBOL: Position(symbol=_KR_SYMBOL, qty=10.0, avg_cost=90.0)}
    data = _data_with_bars(price=130.0, closes=[100.0, 130.0])
    ctx = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, positions=positions, now=_NOW, data=data)

    order = risk.approve(_exit(symbol=_KR_SYMBOL), ctx)

    assert order is not None


# ============================================================= A/B 갈래

def test_cat_arm_blocked_same_as_base_strategy(fake_clock_cls):
    """scalp_1m_cat(촉매 유니버스 갈래)도 scalp_1m과 동일하게 차단돼야 한다 — 이
    레일은 전략별이 아니라 모든 진입 경로 공통이다(생성자 주석)."""
    data_kwargs = dict(price=130.0, closes=[100.0, 130.0])

    risk_base = _risk()
    ctx_base = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, now=_NOW, data=_data_with_bars(**data_kwargs))
    order_base = risk_base.approve(_signal(_KR_SYMBOL, strategy_id="scalp_1m"), ctx_base)

    risk_cat = _risk()
    ctx_cat = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, now=_NOW, data=_data_with_bars(**data_kwargs))
    order_cat = risk_cat.approve(_signal(_KR_SYMBOL, strategy_id="scalp_1m_cat"), ctx_cat)

    assert order_base is None
    assert order_cat is None
    assert "전일 상한가 종목 진입 금지" in risk_base.last_block
    assert "전일 상한가 종목 진입 금지" in risk_cat.last_block


# ============================================================= 설정 토글

def test_disabled_via_config_allows_entry(fake_clock_cls):
    risk = _risk(prev_limit_up_block={"enabled": False})
    data = _data_with_bars(price=130.0, closes=[100.0, 130.0])
    ctx = _ctx(fake_clock_cls, price=130.0, cash=10_000_000.0, now=_NOW, data=data)

    order = risk.approve(_signal(_KR_SYMBOL), ctx)

    assert order is not None
