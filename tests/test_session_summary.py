"""세션 마감 요약 강화(2026-09-01) — 텔레그램 메시지 기록만으로 그날 매매 흐름과
워크플로우 정상 여부를 판단할 수 있어야 한다는 소유자 요구에 대한 고정 테스트.

새 메시지를 추가하지 않는다는 제약(server/scripts/lib/notify.sh 상단 주석) 때문에
이미 나가는 세션 마감 요약(`_session_summary_text`)에만 정보를 추가한다:
- 전략별 순손익(수수료 차감) 한 줄
- 미청산 포지션의 소유 전략·보유기한(오버나이트/단타)
- 배관 점검 한 줄(유니버스 종목 수·사이클 수·에러·정지·시세 폴백)

고정하는 것:
- 전략별 성적은 gross가 아니라 **순손익**(수수료 차감 후) 기준이다.
- 거래 없는 세션(체결 0건)에서도 깨지지 않는다.
- 미청산 사유는 오버나이트 허용 전략(`_OVERNIGHT_STRATEGIES`)에 "오버나이트"가,
  그 외엔 "단타"가 붙는다.
- KR/US 세션은 통화를 섞지 않는다(원/달러 어느 한쪽만 나온다).
- 세션 리셋(`tally.reset()`) 후에도 진행 중인 트립(오버나이트 캐리)은 살아남아
  청산 세션에 올바르게 집계된다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.core.models import Fill, Position, Side
from quant.core.ports import Context
from quant.trade.control import TradingControl
from quant.trade.loop import _SessionTallySink, _session_summary_text

TS = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


class _NullSink:
    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass

    def on_order(self, state):
        pass


class _Clock:
    def now(self):
        return TS

    def is_market_open(self, market):
        return True


class _Broker:
    def __init__(self, positions: dict | None = None):
        self._positions = positions or {}

    def positions(self):
        return self._positions


def _ctx(positions: dict | None = None) -> Context:
    return Context(clock=_Clock(), data=None, broker=_Broker(positions))


def _fill(strategy_id, symbol, side, qty, price, fee=0.0, realized_pnl=None, ts=TS):
    return Fill(
        symbol=symbol, side=side, qty=qty, price=price, ts=ts,
        strategy_id=strategy_id, fee=fee, realized_pnl=realized_pnl,
    )


class _RiskNoBreaker:
    """breaker_state가 없는 최소 RiskManager 더블 — _breaker_snapshot이 None으로
    조용히 넘어가는 duck-typing 경로를 그대로 탄다."""


# --- _SessionTallySink: 라운드트립 집계 ----------------------------------------

def test_round_trip_net_pnl_is_after_fees():
    """왕복 완주 시 순손익 = 매도 실현손익 - 왕복 전체 수수료."""
    tally = _SessionTallySink(_NullSink(), market_of={"005930": "KR"}, fx=None)
    tally.on_fill(_fill("donchian", "005930", Side.BUY, 10, 1000.0, fee=100.0, realized_pnl=0.0))
    tally.on_fill(_fill("donchian", "005930", Side.SELL, 10, 1100.0, fee=100.0, realized_pnl=1000.0))

    stats = tally.strategy_stats["donchian"]
    assert stats["trips"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 0
    assert stats["net_pnl"] == 1000.0 - 200.0  # gross(1000) - fees(100+100)


def test_losing_trip_after_fees_counts_as_loss_even_if_gross_positive():
    """수수료 차감 후 부호가 바뀌면 패로 잡는다 — gross 기준으로 승 처리하지 않는다."""
    tally = _SessionTallySink(_NullSink(), market_of={"TQQQ": "US"}, fx=None)
    tally.on_fill(_fill("donchian", "TQQQ", Side.BUY, 10, 100.0, fee=5.0, realized_pnl=0.0))
    tally.on_fill(_fill("donchian", "TQQQ", Side.SELL, 10, 100.5, fee=5.0, realized_pnl=5.0))

    stats = tally.strategy_stats["donchian"]
    assert stats["net_pnl"] == 5.0 - 10.0
    assert stats["wins"] == 0
    assert stats["losses"] == 1


def test_unknown_realized_pnl_excluded_not_faked_as_zero():
    """브로커가 realized_pnl을 모르는 매도(None)는 승/패·순손익 집계에서 빼고 unknown에 센다."""
    tally = _SessionTallySink(_NullSink(), market_of={"TQQQ": "US"}, fx=None)
    tally.on_fill(_fill("donchian", "TQQQ", Side.BUY, 10, 100.0, fee=1.0))
    tally.on_fill(_fill("donchian", "TQQQ", Side.SELL, 10, 101.0, fee=1.0, realized_pnl=None))

    stats = tally.strategy_stats["donchian"]
    assert stats["trips"] == 0
    assert stats["unknown"] == 1
    assert stats["net_pnl"] == 0.0


def test_reset_clears_session_stats_but_keeps_in_progress_trip_across_sessions():
    """오버나이트 진입(전날 매수)이 세션 리셋을 지나 다음 세션의 매도에서 청산되면,
    그 트립은 청산 세션에 집계돼야 한다 — _trip_* 는 reset()에서 비우지 않는다."""
    tally = _SessionTallySink(_NullSink(), market_of={"TQQQ": "US"}, fx=None)
    tally.on_fill(_fill("overnight_drift", "TQQQ", Side.BUY, 10, 100.0, fee=1.0, realized_pnl=0.0))
    assert tally.strategy_stats == {}  # 아직 트립이 안 닫혔다

    tally.reset()  # 세션 마감 — 진행 중 트립은 살아 있어야 한다
    assert tally.strategy_stats == {}

    tally.on_fill(_fill("overnight_drift", "TQQQ", Side.SELL, 10, 103.0, fee=1.0, realized_pnl=30.0))
    stats = tally.strategy_stats["overnight_drift"]
    assert stats["trips"] == 1
    assert stats["net_pnl"] == 30.0 - 2.0


def test_reset_clears_fills_fee_and_pipeline_counters():
    tally = _SessionTallySink(_NullSink(), market_of={"TQQQ": "US"}, fx=None)
    tally.on_fill(_fill("donchian", "TQQQ", Side.BUY, 10, 100.0, fee=1.0, realized_pnl=0.0))
    tally.cycles = 42
    tally.error_cycles = 3
    tally.halted_seen = True
    tally.degraded_seen = True

    tally.reset()

    assert tally.fills == 0
    assert tally.fee_krw == 0.0
    assert tally.notional_krw == 0.0
    assert tally.cycles == 0
    assert tally.error_cycles == 0
    assert tally.halted_seen is False
    assert tally.degraded_seen is False


# --- _session_summary_text: 렌더링 -------------------------------------------

def _control():
    import tempfile
    from pathlib import Path

    d = Path(tempfile.mkdtemp())
    return TradingControl(state_path=d / "control.json")


def test_no_trades_session_does_not_crash_and_omits_strategy_lines():
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)
    text = _session_summary_text("KR", _ctx(), _RiskNoBreaker(), _control(), None, tally, [])

    assert "🔔 세션 마감" in text
    assert "체결 0건" in text
    assert "🧠" not in text  # 거래 없는 전략은 한 줄도 안 나온다
    assert "없음 (전량 청산됨)" in text


def test_strategy_line_shows_net_pnl_and_currency_matches_market():
    tally = _SessionTallySink(_NullSink(), market_of={"005930": "KR"}, fx=None)
    tally.on_fill(_fill("donchian", "005930", Side.BUY, 10, 1000.0, fee=100.0, realized_pnl=0.0))
    tally.on_fill(_fill("donchian", "005930", Side.SELL, 10, 1100.0, fee=100.0, realized_pnl=1000.0))

    text = _session_summary_text("KR", _ctx(), _RiskNoBreaker(), _control(), None, tally, [])

    assert "donchian" in text
    assert "1건 · 1승 0패" in text
    assert "순손익 +800원" in text  # 1000 gross - 200 fees, KR이므로 '원'
    assert "달러" not in text  # KR 세션은 원화만 — 통화 혼합 금지


def test_us_market_strategy_line_uses_dollars_not_won():
    tally = _SessionTallySink(_NullSink(), market_of={"TQQQ": "US"}, fx=None)
    tally.on_fill(_fill("donchian", "TQQQ", Side.BUY, 10, 100.0, fee=1.0, realized_pnl=0.0))
    tally.on_fill(_fill("donchian", "TQQQ", Side.SELL, 10, 101.0, fee=1.0, realized_pnl=10.0))

    text = _session_summary_text("US", _ctx(), _RiskNoBreaker(), _control(), None, tally, [])

    strategy_line = next(line for line in text.splitlines() if line.startswith("🧠"))
    assert "순손익 +8.00달러" in strategy_line
    assert "원" not in strategy_line  # 전략별 순손익은 시장 통화 하나만 — 원화 혼입 금지


def test_open_position_tags_overnight_strategy_as_overnight():
    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=10, avg_cost=100.0, meta={"strategy": "overnight_drift"}),
    }
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)

    text = _session_summary_text("US", _ctx(positions), _RiskNoBreaker(), _control(), None, tally, [])

    assert "overnight_drift·오버나이트" in text


def test_open_position_tags_day_strategy_as_day_trade():
    positions = {
        "TQQQ": Position(symbol="TQQQ", qty=10, avg_cost=100.0, meta={"strategy": "donchian"}),
    }
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)

    text = _session_summary_text("US", _ctx(positions), _RiskNoBreaker(), _control(), None, tally, [])

    assert "donchian·단타" in text
    assert "오버나이트" not in text


def test_open_position_multi_lot_shows_both_owners():
    positions = {
        "TQQQ": Position(
            symbol="TQQQ", qty=8, avg_cost=100.0,
            meta={"lots": {
                "donchian": {"qty": 5.0},
                "overnight_drift": {"qty": 3.0},
            }},
        ),
    }
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)

    text = _session_summary_text("US", _ctx(positions), _RiskNoBreaker(), _control(), None, tally, [])

    assert "donchian·단타" in text
    assert "overnight_drift·오버나이트" in text


class _Strat:
    def __init__(self, symbols):
        self.symbols = symbols
        self.id = "s"


def test_pipeline_line_reports_universe_cycles_errors_halt_and_fallback():
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)
    tally.cycles = 138
    tally.error_cycles = 2
    tally.halted_seen = True
    tally.degraded_seen = True
    strategies = [_Strat(["TQQQ", "SQQQ"]), _Strat(["AAPL"])]

    text = _session_summary_text("US", _ctx(), _RiskNoBreaker(), _control(), None, tally, strategies)

    assert "배관 점검" in text
    assert "유니버스 3종목" in text
    assert "사이클 138회" in text
    assert "에러 2건" in text
    assert "정지 발생" in text
    assert "시세 폴백 발생" in text


def test_pipeline_line_reports_clean_session():
    tally = _SessionTallySink(_NullSink(), market_of={}, fx=None)
    text = _session_summary_text("KR", _ctx(), _RiskNoBreaker(), _control(), None, tally, [])

    assert "정지 없음" in text
    assert "시세 폴백 없음" in text
    assert "에러 0건" in text
