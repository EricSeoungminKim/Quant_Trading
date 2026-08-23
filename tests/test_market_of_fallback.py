"""market_of 매핑의 "US" 기본값 폴백 금지(A-4) 테스트.

domain/models.py의 market_of_symbol docstring이 명시적으로 금지하는 패턴은
`market_of.get(sym, "US")`다 — 관심종목은 프로세스 기동 후에도 늘어나는데, 부팅
시점 스냅샷에 없는 KR 심볼이 US로 떨어지면 평가액에 환율(1500)이 잘못 곱해진다.
2026-08-11 실운영에서 058610을 0.0015주 매수한 사고가 바로 이 패턴 때문이었다.

이 파일은 감사에서 지목된 평가(valuation) 지점 중 risk/manager.py의 equity·총노출
계산과 portfolio/portfolio.py의 equity()가 매핑에 없는 6자리 KR 심볼을 여전히
KRW로(환율 미적용) 평가하는지 고정한다. 사이징 지점(risk/manager.py의 market 계산,
execution/paper.py)은 이미 고쳐져 있었다 — 여기서 다루는 건 그 나머지 평가 지점들과,
그 지점들에 새 심볼을 실제로 채워 넣는 `_rebuild`의 dict 공유/in-place 업데이트다.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.apps.assembly import build_universe, rebuild_strategies
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Position, Quote, Signal, SignalAction
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
FX_RATE = 1500.0
# 6자리 숫자면서 boot-time 매핑(risk/portfolio에 주입되는 market_of dict)에는 없는
# 심볼 — "장중에 새로 편입된 KR 관심종목"을 흉내낸다.
_UNMAPPED_KR_SYMBOL = "058610"


class _Data:
    def quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ts=NOW, price=100.0)

    def history(self, symbol: str, interval: str, n: int):
        import pandas as pd
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Broker:
    def __init__(self, cash: float, positions: dict[str, Position]):
        self._cash = cash
        self._positions = positions

    def positions(self):
        return self._positions

    def cash(self) -> float:
        return self._cash


class _FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def is_market_open(self, market: str) -> bool:
        return True


# ------------------------------------------- risk/manager.py: equity/총노출 평가 지점


def test_risk_equity_treats_unmapped_kr_symbol_as_krw_not_us():
    """`_day_start_equity`(첫 approve() 호출이 계산하는 equity 스냅샷)가 매핑에 없는
    6자리 KR 심볼을 KRW로 평가해야 한다 — "US"로 떨어지면 환율 1500이 잘못 곱해져
    equity가 부풀고, 그러면 일일 손실 한도 회로차단기가 절대 발동하지 않는다."""
    cash = 10_000_000.0
    qty, avg_cost = 1000.0, 1000.0  # 표시통화(KRW) 평가액 = 1,000,000
    positions = {_UNMAPPED_KR_SYMBOL: Position(symbol=_UNMAPPED_KR_SYMBOL, qty=qty, avg_cost=avg_cost)}
    risk = RiskManagerImpl(
        {"risk": {"max_position_pct": 100, "max_symbol_pct_total": 0, "max_order_notional_pct": 0,
                   "daily_loss_limit_pct": 100, "max_orders_per_day": 1000, "cooldown_bars_after_stop": 0}},
        capital_fraction={"s": 1.0},
        market_of={},  # 이 심볼은 boot 스냅샷에 없다 — 폴백이 발동해야 하는 지점
        fx=FixedFxProvider(FX_RATE),
    )
    ctx = Context(
        clock=_FakeClock(NOW), data=_Data(),
        broker=_Broker(cash, positions),
    )
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=0.0001)

    risk.approve(signal, ctx)  # 첫 호출 — 새 거래일이라 _day_start_equity를 계산해 남긴다

    correct_krw_equity = cash + qty * avg_cost  # 11,000,000 — 환율 미적용
    buggy_us_equity = cash + qty * avg_cost * FX_RATE  # "US" 폴백이었다면 이 값
    assert risk._day_start_equity == pytest.approx(correct_krw_equity)
    assert risk._day_start_equity != pytest.approx(buggy_us_equity)


def test_risk_total_exposure_treats_unmapped_kr_symbol_as_krw_not_us():
    """총노출 계산도 같은 폴백을 쓴다(risk/manager.py 노출 계산 지점) — 여기가
    "US"로 떨어지면 KR 보유가 실제보다 1500배 큰 노출로 잡혀 신규 진입이 부당하게
    막힌다(이 테스트는 정확히 그 봉쇄가 사라지는지를 본다)."""
    cash = 10_000_000.0
    qty, avg_cost = 1000.0, 1000.0  # KRW 평가액 1,000,000
    positions = {_UNMAPPED_KR_SYMBOL: Position(symbol=_UNMAPPED_KR_SYMBOL, qty=qty, avg_cost=avg_cost)}
    risk = RiskManagerImpl(
        {"risk": {
            "max_position_pct": 100, "max_symbol_pct_total": 0, "max_order_notional_pct": 0,
            "daily_loss_limit_pct": 100, "max_orders_per_day": 1000, "cooldown_bars_after_stop": 0,
            "max_total_exposure_pct": 90,  # 노출 상한 활성화 — 이 레일을 직접 겨냥
        }},
        capital_fraction={"s": 1.0},
        market_of={"TQQQ": "US"},  # 신규 진입 심볼만 매핑돼 있고, 보유 중인 KR 심볼은 없다
        fx=FixedFxProvider(FX_RATE),
    )
    ctx = Context(clock=_FakeClock(NOW), data=_Data(), broker=_Broker(cash, positions))
    signal = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=0.5)

    order = risk.approve(signal, ctx)

    # 올바르게 KRW로 잡히면(노출 1,000,000, 자산 11,000,000) room이 넉넉해 통과한다.
    # "US" 폴백이었다면 노출이 1,500,000,000으로 부풀어 자산(비슷하게 부푼) 대비
    # 상한을 넘겨 "총노출 상한 도달"로 막혔을 것이다.
    assert order is not None, f"막힘 사유: {risk.last_block}"
    assert "총노출" not in risk.last_block


# ------------------------------------------------------- portfolio/portfolio.py: equity()


def test_portfolio_equity_treats_unmapped_kr_symbol_as_krw_not_us():
    cash = 10_000_000.0
    qty, avg_cost = 1000.0, 1000.0
    portfolio = Portfolio(
        cash=cash,
        positions={_UNMAPPED_KR_SYMBOL: Position(symbol=_UNMAPPED_KR_SYMBOL, qty=qty, avg_cost=avg_cost)},
        state_path=None,
    )

    equity = portfolio.equity(prices={}, market_of={}, fx=FixedFxProvider(FX_RATE))

    assert equity == pytest.approx(cash + qty * avg_cost)
    assert equity != pytest.approx(cash + qty * avg_cost * FX_RATE)


def test_portfolio_equity_still_uses_us_for_actually_us_symbols():
    """회귀 가드: 폴백을 심볼 추론으로 바꿨다고 진짜 US 심볼(비6자리)까지 KR
    취급하면 안 된다 — market_of_symbol("TQQQ") == "US"가 그대로 유지돼야 한다."""
    portfolio = Portfolio(
        cash=0.0, positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=50.0)}, state_path=None,
    )

    equity = portfolio.equity(prices={}, market_of={}, fx=FixedFxProvider(FX_RATE))

    assert equity == pytest.approx(10.0 * 50.0 * FX_RATE)


# --------------------------------------------------- _rebuild: dict 공유 + in-place 업데이트

_DONCHIAN_PARAMS = {
    "interval_minutes": 15, "lookback_bars": 40, "volume_mult": 1.5,
    "stop_fallback_pct": 1.5, "risk_reward": 2.0, "max_concurrent_names": 1,
    "flatten_before_close_minutes": 10,
}


def test_rebuild_markets_dict_must_be_merged_in_place_not_reassigned(tmp_path):
    """run.py의 `_rebuild()` 클로저가 실제로 해야 하는 것을 그대로 재현한다.

    assembly.build_paper_runtime은 `risk.market_of`와 `broker.market_of`에 **동일한
    dict 객체**를 넘긴다(같은 `markets` 변수를 양쪽 생성자에 전달) — 그래서 세션
    롤마다 rebuild_strategies가 돌려주는 새 dict를 risk.market_of에 `.update()`로
    합치기만 하면 broker 쪽도 재할당 없이 자동으로 최신 상태가 된다. 여기서
    `.update()`가 아니라 `=` 재할당을 했다면 이 테스트는 broker_market_of가 여전히
    옛 dict를 가리켜 실패했을 것이다."""
    watchlist_path = tmp_path / "w.yaml"
    cfg = {
        "universe": {"us": ["TQQQ"], "kr": [], "watchlist": {"enabled": True, "path": str(watchlist_path)}},
        "strategies": {
            "scan": {
                "class": "donchian", "enabled": True, "universe": "watchlist",
                "symbols": [], "params": dict(_DONCHIAN_PARAMS),
            },
        },
    }

    # 부팅 시점: 관심종목 파일이 비어 있다 — 058610은 아직 유니버스에 없다.
    watchlist_path.write_text("symbols: []\n", encoding="utf-8")
    universe = build_universe(cfg)
    _strategies0, markets0, _active0 = rebuild_strategies(cfg, universe)

    # assembly.build_paper_runtime과 동일한 배선: risk와 broker가 같은 dict 객체를 공유.
    risk = RiskManagerImpl({"risk": {}}, capital_fraction={}, market_of=markets0)

    class _DuckBroker:
        def __init__(self, market_of: dict[str, str]):
            self.market_of = market_of  # PaperBroker.__init__과 동일 패턴(재할당 없음)

    broker = _DuckBroker(markets0)
    assert broker.market_of is risk.market_of
    assert _UNMAPPED_KR_SYMBOL not in risk.market_of

    # 세션 롤: 관심종목에 058610이 편입됐다.
    watchlist_path.write_text(f"symbols: ['{_UNMAPPED_KR_SYMBOL}']\n", encoding="utf-8")
    universe.refresh()
    _strategies1, markets1, _active1 = rebuild_strategies(cfg, universe)
    assert markets1 is not markets0  # rebuild_strategies는 매번 새 dict를 만든다(그냥 버리면 안 됨)

    # 수정된 run.py._rebuild()가 하는 것: 재할당이 아니라 in-place update.
    risk.market_of.update(markets1)

    assert _UNMAPPED_KR_SYMBOL in risk.market_of
    assert risk.market_of[_UNMAPPED_KR_SYMBOL] == "KR"
    # 재할당하지 않았으므로 broker는 여전히 같은 객체를 보고 있고, 그래서 업데이트가
    # 자동으로 broker에도 반영된다(별도로 broker.market_of를 갱신할 필요가 없다).
    assert broker.market_of is risk.market_of
    assert broker.market_of[_UNMAPPED_KR_SYMBOL] == "KR"
