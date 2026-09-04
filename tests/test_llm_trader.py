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

import logging
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
    def __init__(self, now=NOW, kr_open=True, flatten=False):
        self._now = now
        self._kr_open = kr_open
        # 2026-09-03 일중 전환(EoD 강제청산) 회귀 테스트용 — 기본은 False(기존
        # 테스트 전부가 이 값에 의존해 청산이 안 걸리는 걸 전제한다).
        self._flatten = flatten

    def now(self):
        return self._now

    def is_market_open(self, market):
        return self._kr_open if market == "KR" else False

    def minutes_to_close(self, market):
        return 60.0

    def cadence_minutes(self):
        return 1.0

    def should_flatten(self, market, m):
        return self._flatten


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


def _ctx(now=NOW, quotes=None, positions=None, kr_open=True, flatten=False):
    return Context(
        clock=FakeClock(now, kr_open, flatten), data=FakeFeed(quotes), broker=FakeBroker(positions),
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


def test_same_id_is_not_reprocessed_once_fill_is_confirmed_by_position_state():
    """2026-09-02 결함 B 수정: 소비 마킹은 신호 반환 시점이 아니라 다음 사이클의
    포지션 상태로 체결이 "확인"된 시점에 일어난다. 매수가 실제로 체결돼 보유로
    바뀐 뒤에는 같은 id를 다시 평가해도 "이미 보유 중"으로 거부되며, 그 시점에
    비로소 영구 소비 처리된다 — 이후 사이클에서도 재신호가 나오지 않는다."""
    orders = [_order()]
    strat = _strategy(orders)

    first = strat.on_cycle(_ctx(quotes={"005930": 1000.0}))
    assert len(first) == 1
    assert "ord-1" not in strat._consumed_ids  # 체결 미확인 — 아직 소비 안 됨

    # 체결이 반영됐다고 가정(포지션에 랏이 생김).
    ctx_filled = _ctx(quotes={"005930": 1000.0}, positions={"005930": _lot_position("005930")})
    second = strat.on_cycle(ctx_filled)
    assert second == []  # "이미 보유 중"으로 거부 — 이 시점에 소비됨
    assert "ord-1" in strat._consumed_ids

    third = strat.on_cycle(ctx_filled)
    assert third == []  # 계속 소비된 채로 남는다 — 재신호 없음


def test_buy_signal_retries_next_cycle_when_fill_not_yet_confirmed():
    """리스크 승인이 일시적 사유(예: 콜드 페치 예산 초과, 2026-09-02 실사고)로
    실패해도 llm_trader는 그 결과를 직접 볼 수 없다 — 포지션이 안 바뀌면(=체결
    미확인) 다음 사이클에 같은 id로 자동 재시도된다. 과거 버그는 신호 반환
    즉시 영구 소비해 이 재시도 기회를 없앴다."""
    orders = [_order()]
    strat = _strategy(orders)
    ctx = _ctx(quotes={"005930": 1000.0})  # 포지션 불변 = 체결 미확인 시뮬레이션

    first = strat.on_cycle(ctx)
    second = strat.on_cycle(ctx)
    third = strat.on_cycle(ctx)

    assert len(first) == 1
    assert len(second) == 1  # 같은 id, 미확인 상태라 재시도
    assert len(third) == 1
    assert first[0].reason == second[0].reason == third[0].reason


def test_sell_signal_retries_next_cycle_when_fill_not_yet_confirmed():
    """매도의 대칭 버전 — 포지션이 그대로 열려 있으면(체결 미확인) 같은 id로
    계속 재시도된다."""
    orders = [_order(action="sell", weight=None, horizon="스윙")]
    strat = _strategy(orders)
    positions = {"005930": _lot_position("005930")}
    ctx = _ctx(quotes={"005930": 1000.0}, positions=positions)

    first = strat.on_cycle(ctx)
    second = strat.on_cycle(ctx)

    assert len(first) == 1
    assert len(second) == 1
    assert "ord-1" not in strat._consumed_ids

    # 체결 확인 — 포지션이 사라졌다고 가정.
    third = strat.on_cycle(_ctx(quotes={"005930": 1000.0}))
    assert third == []  # "보유 없음"으로 거부 — 소비됨
    assert "ord-1" in strat._consumed_ids


def test_market_closed_rejection_is_retried_once_session_reopens():
    """2026-09-02 결함 B 수정: 동시호가/장마감 거부는 일시적이다 — 소비되지 않고
    연속거래가 재개되면 같은 id가 다시 평가된다."""
    strat = _strategy([_order()])

    first = strat.on_cycle(_ctx(quotes={"005930": 1000.0}, kr_open=False))
    assert first == []
    assert "ord-1" not in strat._consumed_ids  # 소비되지 않음 — 재시도 가능

    second = strat.on_cycle(_ctx(quotes={"005930": 1000.0}, kr_open=True))
    assert len(second) == 1  # 장이 열리자 같은 id가 재평가된다


def test_permanent_rejections_are_not_retried_across_cycles():
    """영구 거부(KR 심볼 아님/horizon 오류/알 수 없는 action)는 지금처럼 즉시
    영구 소비되고, 다음 사이클에도 재평가되지 않는다 — 재평가해도 결론이
    같기 때문이다."""
    strat = _strategy([_order(symbol="TQQQ")])
    ctx = _ctx(quotes={"TQQQ": 50.0})

    first = strat.on_cycle(ctx)
    second = strat.on_cycle(ctx)

    assert first == [] and second == []
    assert "ord-1" in strat._consumed_ids


def test_pending_exit_dropped_when_symbol_already_flat_and_market_closed():
    """2026-09-04 실사고 수리: 시장이 닫혀 있어도 포지션 조회는 가능하다 —
    이미 청산된 종목의 대기 매도 결정을 "시장 닫힘/동시호가" 사유로 매
    사이클 재시도하지 않고 즉시 영구 폐기한다(실측: 088350 등 3종목이 09:15
    청산된 뒤 15:50 재시작 이후 장마감 내내 이 사유로 재시도되며 26,445줄
    중 16,667줄을 차지했다)."""
    strat = _strategy([_order(action="sell", weight=None)])
    ctx = _ctx(quotes={"005930": 1000.0}, kr_open=False)  # 무포지션 + 시장 닫힘

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "ord-1" in strat._consumed_ids  # 영구 폐기 — 재시도 없음
    assert "보류 결정 폐기" in strat.last_reject["005930"]
    assert "포지션 없음" in strat.last_reject["005930"]

    second = strat.on_cycle(ctx)
    assert second == []


def test_pending_exit_still_retried_when_market_closed_and_holding():
    """대칭 확인 — 포지션이 아직 열려 있으면(체결 미확인) 시장이 닫혀 있을 때도
    기존처럼 소비되지 않고 다음 사이클에 재평가된다(위 테스트와의 대조군)."""
    strat = _strategy([_order(action="sell", weight=None)])
    positions = {"005930": _lot_position("005930")}
    ctx = _ctx(quotes={"005930": 1000.0}, kr_open=False, positions=positions)

    signals = strat.on_cycle(ctx)

    assert signals == []
    assert "ord-1" not in strat._consumed_ids
    assert "시장 닫힘/동시호가" in strat.last_reject["005930"]


def test_stale_entry_is_dropped_and_logged_once(caplog):
    """거래일이 지난 보류 결정은 조용히 매 사이클 다시 걸러지는 대신, 한 번
    로그하고 영구 소비된다(2026-09-04)."""
    yesterday = NOW - timedelta(days=1)
    strat = _strategy([_order(ts=yesterday.isoformat())])
    ctx = _ctx(quotes={"005930": 1000.0})

    with caplog.at_level(logging.INFO):
        signals = strat.on_cycle(ctx)

    assert signals == []
    assert "ord-1" in strat._consumed_ids
    assert "보류 결정 폐기" in strat.last_reject["005930"]
    assert "거래일 경과" in strat.last_reject["005930"]
    matching = [r for r in caplog.records if "보류 결정 폐기" in r.message]
    assert len(matching) == 1

    # 이미 영구 소비됐으므로 다음 사이클엔 아무 일도 일어나지 않는다(재로그 없음).
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert strat.on_cycle(ctx) == []
    assert not caplog.records


def test_rejection_log_throttled_to_once_per_30_minutes_per_decision(caplog):
    """같은 결정(oid)의 거부가 30분 안에 반복되면 로그를 또 남기지 않는다 —
    한 심볼에 pending 결정이 여러 개 있을 때 서로 다른 oid가 매 사이클
    last_reject[symbol]을 번갈아 갱신해 무한 재로그되던 결함(2026-09-04,
    15:50 재시작 이후 26,445줄 중 16,667줄) 재발 방지."""
    strat = _strategy([_order()])  # buy, 시장 닫힘 → 소비되지 않고 매 사이클 재시도
    ctx = _ctx(quotes={"005930": 1000.0}, kr_open=False)

    with caplog.at_level(logging.INFO):
        strat.on_cycle(ctx)
        strat.on_cycle(ctx)
        strat.on_cycle(ctx)

    matching = [r for r in caplog.records if "시장 닫힘/동시호가" in r.message]
    assert len(matching) == 1  # 3번 호출했지만 로그는 최초 1번만

    # 30분이 지나면 다시 한 번 로그된다(하트비트 — 계속 억제되진 않는다).
    later_ctx = _ctx(now=NOW + timedelta(minutes=31), quotes={"005930": 1000.0}, kr_open=False)
    with caplog.at_level(logging.INFO):
        strat.on_cycle(later_ctx)
    matching_after = [r for r in caplog.records if "시장 닫힘/동시호가" in r.message]
    assert len(matching_after) == 2


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


# ------------------------------------------------------------- EoD 강제청산(2026-09-03 일중 전환)


def test_eod_flatten_exits_open_position_before_close():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(quotes={"005930": 1010.0}, positions=positions, flatten=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    sig = signals[0]
    assert sig.action == SignalAction.EXIT_LONG
    assert sig.exit_fraction == 1.0
    assert "EoD 강제청산" in sig.reason


def test_eod_flatten_does_not_fire_when_not_near_close():
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(quotes={"005930": 1010.0}, positions=positions, flatten=False)

    assert strat.on_cycle(ctx) == []


def test_eod_flatten_skips_symbols_without_my_lot():
    strat = _strategy([])
    positions = {"005930": Position(symbol="005930", qty=10.0, avg_cost=1000.0, meta={})}
    ctx = _ctx(quotes={"005930": 1010.0}, positions=positions, flatten=True)

    assert strat.on_cycle(ctx) == []


def test_eod_flatten_takes_priority_over_hard_stop_no_duplicate_signal():
    """마감 직전 + 손절가 이하가 동시에 성립해도 신호는 하나(중복 청산 금지)."""
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(quotes={"005930": 900.0}, positions=positions, flatten=True)

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert "EoD 강제청산" in signals[0].reason


def test_eod_flatten_fires_even_outside_continuous_session():
    """오버나이트 방지가 목적이므로 하드 손절과 달리 연속거래 여부와 무관하게 작동한다
    (should_flatten 자체가 동시호가/장마감 이후는 걸러준다 — clock.py)."""
    strat = _strategy([])
    positions = {"005930": _lot_position("005930", qty=10.0, entry=1000.0, stop=950.0)}
    ctx = _ctx(
        now=datetime(2026, 8, 31, 15, 25, tzinfo=KST),  # 연속거래 종료(15:20) 이후 — 동시호가 구간
        quotes={"005930": 1010.0}, positions=positions, flatten=True,
    )

    signals = strat.on_cycle(ctx)

    assert len(signals) == 1
    assert "EoD 강제청산" in signals[0].reason


# --------------------------------------------------------------------------- 생성자 검증


def test_constructor_defaults():
    strat = LlmTraderStrategy(symbols=[], params={}, id="llm_trader")
    assert strat.max_positions == 5
    assert strat.max_weight_per_position == 0.34
    assert strat.stop_pct == 5.0
    assert strat.on_cycle(_ctx()) == []  # inbox_reader 미주입 → 항상 빈 목록
