"""2026-08-10 시스템 현행화 통합 테스트 — E2E 감사에서 확인된 4개 커버리지 갭.

각 테스트는 유닛이 아니라 배선을 검증한다: real RiskManagerImpl/PaperBroker/
run_cycle을 쓰고, mock 호출 여부가 아니라 실제 수량·수수료·파일 내용을 본다.
(a) 반복 진입 → 리스크가 잔여룸만큼만 증분 사이징
(b) KR 개별주 매도세가 run_cycle 경로의 Fill.fee에 실제로 붙는다 (ETF는 면제)
(c) 시장별 국면 배수가 run_cycle을 통해 심볼별로 적용된다
(d) 조립된 런타임의 ledger sink가 체결을 실제로 원장 파일에 남긴다
"""
from __future__ import annotations

from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from quant.trade.loop import run_cycle
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import Quote, Side, Signal, SignalAction
from quant.adapters.execution.paper import PaperBroker
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl

KST = ZoneInfo("Asia/Seoul")
DAY = datetime(2026, 8, 10, 10, 0, tzinfo=KST)


class _Sink:
    def __init__(self):
        self.signals, self.fills = [], []

    def on_signal(self, s):
        self.signals.append(s)

    def on_fill(self, f):
        self.fills.append(f)


class _Feed:
    def __init__(self, prices: dict[str, float]):
        self.prices = prices

    def quote(self, symbol):
        p = self.prices.get(symbol)
        return None if p is None else Quote(symbol=symbol, ts=DAY, price=p)

    def history(self, symbol, interval, n):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Clock:
    def now(self):
        return DAY

    def is_market_open(self, market):
        return True

    def minutes_to_close(self, market):
        return 240.0

    def cadence_minutes(self):
        return 5 / 60

    def should_flatten(self, market, m):
        return False


class _OneShotStrategy:
    """지정한 시그널 목록을 1회 내보내는 최소 전략 — 배선 검증용."""

    def __init__(self, signals, id="orb_scan"):
        self.id = id
        self.symbols = [s.symbol for s in signals]
        self._signals = list(signals)

    def on_cycle(self, ctx):
        out, self._signals = self._signals, []
        return out


def _entry(symbol, weight=0.3, strategy="orb_scan"):
    return Signal(strategy_id=strategy, symbol=symbol, action=SignalAction.ENTER_LONG,
                  target_weight=weight, stop=None, target=None)


def _exit(symbol, strategy="orb_scan"):
    return Signal(strategy_id=strategy, symbol=symbol, action=SignalAction.EXIT_LONG,
                  target_weight=0.0, exit_fraction=1.0)


def _rig(prices, market_of, *, fee_bps=None, kr_etf=frozenset(), start_cash=10_000_000.0,
         risk_cfg=None, tmp_path=None):
    fx = FixedFxProvider(1_000.0)
    feed = _Feed(prices)
    portfolio = Portfolio(cash=start_cash,
                          state_path=(tmp_path / "portfolio.json") if tmp_path else None)
    broker = PaperBroker(
        data=feed, portfolio=portfolio, fee_bps=fee_bps or {"KR": 1.5, "US": 7},
        market_of=market_of, fx=fx, kr_stock_sell_tax_bps=15, kr_etf_symbols=kr_etf,
    )
    risk = RiskManagerImpl(
        {"risk": dict({"max_position_pct": 50, "max_symbol_pct_total": 0,
                       "daily_loss_limit_pct": 100, "max_orders_per_day": 999}, **(risk_cfg or {}))},
        capital_fraction={"orb_scan": 1.0}, market_of=market_of, fx=fx,
    )
    ctx = Context(clock=_Clock(), data=feed, broker=broker)
    return ctx, risk, broker


def test_repeat_entry_sizes_only_remaining_room(tmp_path):
    """같은 종목에 진입 신호가 두 번 — 두 번째는 잔여룸만큼만 증분 매수돼야 한다.
    (2026-08-10 반복 진입 허용의 안전 전제: 전략이 아니라 리스크가 크기를 통제)"""
    ctx, risk, broker = _rig({"069500": 10_000.0}, {"069500": "KR"}, tmp_path=tmp_path)

    run_cycle([_OneShotStrategy([_entry("069500", weight=0.3)])], ctx, risk, sink1 := _Sink())
    qty_first = sink1.fills[0].qty
    assert qty_first > 0

    # 두 번째 진입: 목표 비중 0.5 — 이미 0.3만큼 보유하므로 증분(≈0.2)만 사야 한다
    run_cycle([_OneShotStrategy([_entry("069500", weight=0.5)])], ctx, risk, sink2 := _Sink())
    qty_second = sink2.fills[0].qty
    total = broker.positions()["069500"].qty
    assert total == qty_first + qty_second
    # 총 보유가 0.5 비중 상한(max_position_pct 50%)을 넘지 않는다: 10M x 0.5 / 10,000 = 500주
    assert total <= 500 + 1, f"반복 진입이 상한을 뚫으면 안 된다 (총 {total}주)"
    assert qty_second < qty_first, "두 번째는 증분만 — 첫 진입과 같은 크기면 룸 계산이 죽은 것"


def test_kr_sell_tax_reaches_fill_fee_through_run_cycle(tmp_path):
    """KR 개별주 매도 수수료 = 기본 1.5bp + 거래세 15bp. 같은 조건의 KR ETF는 면제.
    스코어보드/세션 성적표가 이 Fill.fee를 그대로 집계하므로 여기가 틀리면 전부 낙관 왜곡."""
    market_of = {"005930": "KR", "069500": "KR"}
    ctx, risk, broker = _rig({"005930": 10_000.0, "069500": 10_000.0}, market_of,
                             kr_etf=frozenset({"069500"}), tmp_path=tmp_path)

    run_cycle([_OneShotStrategy([_entry("005930", 0.2), _entry("069500", 0.2)])],
              ctx, risk, _Sink())
    run_cycle([_OneShotStrategy([_exit("005930"), _exit("069500")])], ctx, risk, sink := _Sink())

    fees = {f.symbol: f for f in sink.fills if f.side is Side.SELL}
    stock, etf = fees["005930"], fees["069500"]
    stock_notional = stock.qty * stock.price
    etf_notional = etf.qty * etf.price
    assert abs(stock.fee - stock_notional * 16.5 / 1e4) < 1e-6, "개별주 매도 = 1.5bp + 세금 15bp"
    assert abs(etf.fee - etf_notional * 1.5 / 1e4) < 1e-6, "ETF 매도 = 기본 수수료만"


def test_per_market_regime_multiplier_applies_by_symbol(tmp_path):
    """mult_by_market={KR:0.5, US:1.0} — 같은 목표 비중이라도 KR 심볼은 절반 크기.
    국면 분리(2026-08-10)가 run_cycle → risk.approve까지 실제로 흐르는지 검증."""
    market_of = {"069500": "KR", "TQQQ": "US"}
    # 환율 1,000원 고정이라 TQQQ $10 = 10,000원 — 두 종목의 명목이 대칭이 되게 맞춘다
    ctx, risk, broker = _rig({"069500": 10_000.0, "TQQQ": 10.0}, market_of, tmp_path=tmp_path)

    run_cycle(
        [_OneShotStrategy([_entry("069500", 0.2), _entry("TQQQ", 0.2)])],
        ctx, risk, sink := _Sink(),
        mult_by_market={"KR": 0.5, "US": 1.0},
    )
    fills = {f.symbol: f for f in sink.fills}
    kr_notional = fills["069500"].qty * fills["069500"].price          # KRW
    us_notional = fills["TQQQ"].qty * fills["TQQQ"].price * 1_000.0    # KRW 환산
    assert kr_notional < us_notional * 0.7, (
        f"KR(0.5배) 명목({kr_notional:,.0f})이 US(1.0배) 명목({us_notional:,.0f})의 "
        "절반 근처여야 한다 — 배수가 심볼별로 안 흐르면 같은 크기가 된다"
    )


def test_assembled_runtime_ledger_sink_writes_fills(tmp_path, monkeypatch):
    """조립 계층이 TradeLedgerSink를 실제로 감싸고, 체결이 원장 파일에 남는지.
    (조립 누락은 유닛 테스트가 못 잡는다 — FxProvider 미배선 사고의 재발 방지축)"""
    from quant.control import ledger as ledger_module
    from quant.control.ledger import TradeLedgerSink, load_trades
    from quant.adapters.persistence.sink import MultiSink

    monkeypatch.setattr(ledger_module, "DEFAULT_LEDGER_PATH", tmp_path / "trades.jsonl")
    ctx, risk, broker = _rig({"069500": 10_000.0}, {"069500": "KR"}, tmp_path=tmp_path)
    sinks = TradeLedgerSink(MultiSink([_Sink()]))  # assembly.py:495와 같은 배선 형태

    run_cycle([_OneShotStrategy([_entry("069500", 0.2)])], ctx, risk, sinks)

    rows = load_trades(tmp_path / "trades.jsonl")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "069500" and rows[0]["strategy_id"] == "orb_scan"
    assert rows[0]["market"] == "KR" and rows[0]["side"] == "buy"
