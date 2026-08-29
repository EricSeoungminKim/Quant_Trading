"""llm_trader — LLM 판단 인박스 → Signal 변환의 가드레일 회귀 테스트.

설계는 quant/trade/strategy/llm_trader.py 모듈 docstring 참고. 여기서는:
- 인박스 파싱·검증(잘못된 심볼/weight/horizon/과다 포지션 거부)
- 당일(거래일) ts 필터 — 재시작 시 과거 주문 미재실행의 실질적 방어선
- 소비 idempotency(같은 id 재처리 없음)
- buy/sell → Signal 변환(비중 상한, reason horizon 접두사)
- 하드 손절 레일이 lots에 기록되고 실제로 발동하는지
를 검증한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.models import Position, Quote, SignalAction
from quant.core.ports import Context
from quant.trade.strategy.llm_trader import LlmTraderStrategy

KST = ZoneInfo("Asia/Seoul")
# 2026-08-31은 월요일, 10:00 KST는 KR 연속거래 구간(09:00~15:20) 안이다.
NOW = datetime(2026, 8, 31, 10, 0, tzinfo=KST)


class FakeClock:
    def __init__(self, now=NOW, kr_open=True):
        self._now = now
        self._kr_open = kr_open

    def now(self):
        return self._now

    def is_market_open(self, market):
        return self._kr_open if market == "KR" else False

    def minutes_to_close(self, market):
        return 60.0

    def cadence_minutes(self):
        return 1.0

    def should_flatten(self, market, m):
        return False


class FakeFeed:
    def __init__(self, quotes=None):
        self._quotes = quotes or {}

    def history(self, symbol, interval, n):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def quote(self, symbol):
        p = self._quotes.get(symbol)
        return Quote(symbol=symbol, ts=NOW, price=p) if p else None


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or {}

    def positions(self):
        return self._positions

    def cash(self):
        return 10_000_000.0


def _ctx(now=NOW, quotes=None, positions=None, kr_open=True):
    return Context(
        clock=FakeClock(now, kr_open), data=FakeFeed(quotes), broker=FakeBroker(positions),
    )


def _order(**overrides):
    base = dict(
        id="ord-1", ts=NOW.isoformat(), action="buy", symbol="005930",
        weight=0.5, horizon="단타", reason="테스트 근거",
    )
    base.update(overrides)
    return base


def _strategy(orders, **params):
    return LlmTraderStrategy(
        symbols=[], params=params, id="llm_trader", inbox_reader=lambda: list(orders),
    )


def _lot_position(symbol, qty=10.0, entry=100.0, stop=None, extra=None):
    lot = {"qty": qty, "entry": entry}
    if stop is not None:
        lot["stop"] = stop
    if extra:
        lot.update(extra)
    return Position(symbol=symbol, qty=qty, avg_cost=entry, meta={"lots": {"llm_trader": lot}})


# --------------------------------------------------------------------------- 매수 변환


def test_buy_converts_to_enter_long_with_capped_weight_and_stop_state_update():
    strat = _strategy([_order(weight=0.9)], max_weight_per_position=0.34, stop_pct=5.0)
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.ENTER_LONG
    assert sig.symbol == "005930"
    assert sig.target_weight == 0.34  # 요청 0.9가 상한 0.34로 잘린다
    assert sig.stop == 1000.0 * 0.95
    assert sig.reason.startswith("[단타] LLM 매수(#ord-1):")
    assert sig.state_update == {
        "entry": 1000.0, "stop": 950.0, "horizon": "단타",
        "entered_at": NOW.isoformat(), "strategy": "llm_trader",
    }


def test_sell_converts_to_exit_long_full_when_holding():
    strat = _strategy([_order(action="sell", weight=None, horizon="스윙")])
    positions = {"005930": _lot_position("005930")}
    ctx = _ctx(quotes={"005930": 1000.0}, positions=positions)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.exit_fraction == 1.0
    assert sig.target_weight == 0.0
    assert sig.reason.startswith("[스윙] LLM 매도(#ord-1):")


def test_sell_without_holding_is_rejected():
    strat = _strategy([_order(action="sell", weight=None)])
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "보유 없음" in strat.last_reject["005930"]


# --------------------------------------------------------------------------- 검증(가드레일)


def test_non_kr_symbol_is_rejected():
    strat = _strategy([_order(symbol="TQQQ")])
    ctx = _ctx(quotes={"TQQQ": 50.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "KR 심볼 아님" in strat.last_reject["TQQQ"]


def test_invalid_horizon_is_rejected():
    strat = _strategy([_order(horizon="장투")])
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "horizon" in strat.last_reject["005930"]


def test_missing_horizon_is_rejected():
    order = _order()
    del order["horizon"]
    strat = _strategy([order])
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "horizon" in strat.last_reject["005930"]


def test_invalid_weight_is_rejected():
    strat = _strategy([_order(weight="많이")])
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "weight 형식 오류" in strat.last_reject["005930"]


def test_out_of_range_weight_is_rejected():
    strat = _strategy([_order(weight=1.5)])
    ctx = _ctx(quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "weight 범위 오류" in strat.last_reject["005930"]


def test_buy_over_max_positions_is_rejected():
    strat = _strategy([_order(symbol="000660")], max_positions=2)
    positions = {
        "005930": _lot_position("005930"),
        "035420": _lot_position("035420"),
    }
    ctx = _ctx(quotes={"000660": 500.0}, positions=positions)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "동시 보유 한도 초과" in strat.last_reject["000660"]


def test_duplicate_buy_on_held_symbol_is_rejected():
    strat = _strategy([_order()])
    positions = {"005930": _lot_position("005930")}
    ctx = _ctx(quotes={"005930": 1000.0}, positions=positions)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "중복 매수" in strat.last_reject["005930"]


def test_missing_id_row_is_silently_skipped():
    order = _order()
    del order["id"]
    strat = _strategy([order])
    ctx = _ctx(quotes={"005930": 1000.0})

    assert strat.on_cycle(ctx) == []


def test_order_rejected_when_market_not_continuous_session():
    strat = _strategy([_order()])
    # 개장 전(08:00 KST) — 시장이 열려 있어도 연속거래 구간 밖.
    ctx = _ctx(now=datetime(2026, 8, 31, 8, 0, tzinfo=KST), quotes={"005930": 1000.0})

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "시장 닫힘/동시호가" in strat.last_reject["005930"]


def test_order_rejected_when_market_closed():
    strat = _strategy([_order()])
    ctx = _ctx(quotes={"005930": 1000.0}, kr_open=False)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "시장 닫힘/동시호가" in strat.last_reject["005930"]


# --------------------------------------------------------------------------- 당일 필터 + 소비 idempotency


def test_order_from_a_previous_trading_day_is_ignored():
    yesterday = NOW - timedelta(days=1)
    strat = _strategy([_order(ts=yesterday.isoformat())])
    ctx = _ctx(quotes={"005930": 1000.0})

    assert strat.on_cycle(ctx) == []


def test_order_with_unparseable_ts_is_ignored():
    strat = _strategy([_order(ts="언젠가")])
    ctx = _ctx(quotes={"005930": 1000.0})

    assert strat.on_cycle(ctx) == []


def test_same_id_is_not_reprocessed_within_the_same_trading_day():
    orders = [_order()]
    strat = _strategy(orders)
    ctx = _ctx(quotes={"005930": 1000.0})

    first = strat.on_cycle(ctx)
    second = strat.on_cycle(ctx)

    assert len(first) == 1
    assert second == []  # 같은 id, 소비됨 — 재처리 없음


def test_restart_does_not_replay_a_previous_trading_days_order():
    """재시작(=새 인스턴스, _consumed_ids 소실) 후에도 과거 거래일 주문은
    ts 필터가 걸러낸다 — 모듈 docstring "상태" 절의 핵심 주장."""
    yesterday = NOW - timedelta(days=1)
    orders = [_order(ts=yesterday.isoformat())]

    # 재시작 전 인스턴스도 처리하지 않았고,
    strat_before = _strategy(orders)
    assert strat_before.on_cycle(_ctx(quotes={"005930": 1000.0})) == []

    # "재시작"으로 상태가 없는 새 인스턴스를 만들어도 여전히 걸러진다.
    strat_after = _strategy(orders)
    assert strat_after.on_cycle(_ctx(quotes={"005930": 1000.0})) == []


# --------------------------------------------------------------------------- 하드 손절 레일


def test_hard_stop_exits_when_price_falls_to_or_below_stop():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(quotes={"005930": 950.0}, positions=positions)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.exit_fraction == 1.0
    assert "하드레일 손절" in sig.reason


def test_hard_stop_does_not_fire_above_stop_price():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(quotes={"005930": 960.0}, positions=positions)

    assert strat.on_cycle(ctx) == []


def test_hard_stop_skipped_when_lot_has_no_stop_recorded():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=None)}
    ctx = _ctx(quotes={"005930": 1.0}, positions=positions)

    assert strat.on_cycle(ctx) == []


def test_hard_stop_not_checked_outside_continuous_session():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(
        now=datetime(2026, 8, 31, 8, 0, tzinfo=KST),
        quotes={"005930": 900.0}, positions=positions,
    )

    assert strat.on_cycle(ctx) == []


# --------------------------------------------------------------------------- 생성자 검증


def test_constructor_defaults():
    strat = LlmTraderStrategy(symbols=[], params={}, id="llm_trader")
    assert strat.max_positions == 5
    assert strat.max_weight_per_position == 0.34
    assert strat.stop_pct == 5.0
    assert strat.on_cycle(_ctx()) == []  # inbox_reader 미주입 → 항상 빈 목록
