"""회계 사다리(accounting ladder) — 손익 불일치의 발생 지점을 계층별로 좁히는 계측기.

## 왜 이 파일이 존재하는가

10년 백테스트가 서로 모순되는 세 개의 숫자를 낸다:
equity curve는 -29.1%(-1,455,000 KRW), `trades["pnl"].sum()`은 -$99,
현금흐름 재구성은 또 다른 값에 불가능한 잔여 수량까지 남긴다.
**어느 계층이 거짓말을 하는지 모른다**는 것이 문제다.

## 방법

손으로 계산 가능한 시나리오 하나를 정의하고(모든 기대값은 산술로 유도해 리터럴로
박아넣는다 — 코드를 돌려서 얻지 않는다), 프로덕션 계층을 한 번에 하나씩 얹으면서
**매 단계 동일한 불변식**을 검증한다. 처음 깨지는 단(rung)이 곧 결함의 위치다.

사다리 단계:
  rung 1  Portfolio 단독 (현금/포지션 산술, equity())
  rung 2  + FX 환산 (to_krw/from_krw, FixedFxProvider)
  rung 3  + PaperBroker.place_order (체결/수수료/포트폴리오 변경)
  rung 4  + RiskManagerImpl.approve (목표비중 -> 수량 사이징)
  rung 5  + run_cycle (signal -> approve -> place -> fill -> state_update 배선)
  rung 6  + 스크립트 전략 (신호가 사전에 확정된 전략 — 전략을 변수에서 제거)
  rung 7  + run_backtest 리플레이 루프 + equity curve 구성
  rung 8  + _compute_pnl / _compute_metrics (리포팅 계층)
  rung 9  실제 stub 백테스트에 동일한 항등식을 적용한 캡스톤

## 핵심 불변식 (rung 3부터 모든 단에서 검증)

    final_equity - initial_equity
        == (실현 총손익 + 미실현 평가손익 - 총수수료) x FX      [단일 통화, KRW]

    종목별  Σbuy_qty - Σsell_qty == 최종 보유 수량

"실현 총손익"은 수수료 차감 **전** 값이다 — 수수료를 별도 항으로 분리해야 어느
쪽이 새는지 보인다.

## 이 파일의 규칙

- `quant/` 아래 어떤 파일도 수정하지 않는다. 이 파일은 순수 계측기다.
- 네트워크/sleep/공유 디스크 상태 없음. Portfolio는 항상 state_path=None.
- 실패 메시지는 그 자체로 진단이어야 한다: 기대값, 실측값, 차이, 방금 추가된 계층.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import quant.backtest.engine as bt_engine
import quant.trade.strategy as strategies_pkg
from quant.backtest.engine import (
    ReconciliationError,
    _compute_metrics,
    _reconcile,
    run_backtest,
)
from quant.core.fx import FixedFxProvider
from quant.core.ports import Context
from quant.core.models import (
    Order,
    Position,
    Quote,
    Side,
    Signal,
    SignalAction,
)
from quant.adapters.execution.paper import PaperBroker
from quant.core.portfolio.portfolio import Portfolio, from_krw, to_krw
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# 시나리오 상수 — 모든 기대값은 이 숫자들로부터 손으로 유도된다.
# ---------------------------------------------------------------------------

SYM = "USX"                 # 가상의 US 종목 (표시 통화 USD)
MARKET_OF = {SYM: "US"}
FX_RATE = 1_000.0           # 주입 환율. 프로덕션 기본값(1500)과 일부러 다르게 잡아,
                            # 어딘가 fx 주입이 누락돼 fallback 1500이 쓰이면 즉시 터지게 한다.
FEE_BPS = 7.0               # 편도 7bp = 0.07%
START_CASH_KRW = 1_000_000.0

TOL = 1e-6                  # KRW 절대 허용오차. 값의 스케일이 1e6이라 float64에서 넉넉하다.

TS0 = datetime(2026, 1, 5, 10, 0, tzinfo=NY)   # 월요일 장중


def fx() -> FixedFxProvider:
    return FixedFxProvider(FX_RATE)


# --- 시나리오 A: 전량 왕복 (이익) ------------------------------------------
# 매수 50주 @ $10.00  -> notional $500.00, 수수료 $500 x 7bp = $0.35
#                        현금 -= (500.00 + 0.35) x 1000 = 500,350 KRW
#                        현금 = 1,000,000 - 500,350 = 499,650 KRW
# 가격 $12.00
# 매도 50주 @ $12.00  -> notional $600.00, 수수료 $600 x 7bp = $0.42
#                        현금 += (600.00 - 0.42) x 1000 = 599,580 KRW
#                        현금 = 499,650 + 599,580 = 1,099,230 KRW
# 실현 총손익 = ($12 - $10) x 50 = $100.00 (수수료 차감 전)
# 총수수료   = $0.35 + $0.42 = $0.77
# 최종 자산  = 1,099,230 KRW (포지션 0)
# 총수익률   = 1,099,230 / 1,000,000 - 1 = +9.9230%
A_BUY_QTY, A_BUY_PX = 50.0, 10.0
A_SELL_QTY, A_SELL_PX = 50.0, 12.0
A_CASH_AFTER_BUY = 499_650.0
A_FINAL_CASH = 1_099_230.0
A_FINAL_QTY = 0.0
A_FINAL_EQUITY = 1_099_230.0
A_GROSS_USD = 100.0
A_FEES_USD = 0.77
A_RETURN_PCT = 9.9230

# --- 시나리오 B: 부분 청산 (스케일아웃) ------------------------------------
# 매수 50주 @ $10.00  -> 현금 499,650 KRW, 평균단가 $10.00
# 가격 $12.00
# 매도 25주 @ $12.00  -> notional $300.00, 수수료 $0.21
#                        현금 += (300.00 - 0.21) x 1000 = 299,790 KRW
#                        현금 = 499,650 + 299,790 = 799,440 KRW
# 잔여 포지션 25주 @ 평균단가 $10.00  (부분매도는 평균단가를 바꾸지 않는다)
# 실현 총손익 = ($12 - $10) x 25 = $50.00
# 미실현      = ($12 - $10) x 25 = $50.00
# 총수수료    = $0.35 + $0.21 = $0.56
# 최종 자산   = 799,440 + 25 x $12 x 1000 = 799,440 + 300,000 = 1,099,440 KRW
# 총수익률    = +9.9440%
B_SELL_QTY = 25.0
B_FINAL_CASH = 799_440.0
B_FINAL_QTY = 25.0
B_FINAL_AVG_COST = 10.0
B_FINAL_EQUITY = 1_099_440.0
B_GROSS_USD = 50.0
B_UNREAL_USD = 50.0
B_FEES_USD = 0.56
B_RETURN_PCT = 9.9440

# --- 시나리오 C: 손실 거래 -------------------------------------------------
# 매수 20주 @ $25.00  -> notional $500.00, 수수료 $0.35
#                        현금 = 1,000,000 - 500,350 = 499,650 KRW
# 가격 $20.00
# 매도 20주 @ $20.00  -> notional $400.00, 수수료 $0.28
#                        현금 += (400.00 - 0.28) x 1000 = 399,720 KRW
#                        현금 = 499,650 + 399,720 = 899,370 KRW
# 실현 총손익 = ($20 - $25) x 20 = -$100.00
# 총수수료    = $0.35 + $0.28 = $0.63
# 최종 자산   = 899,370 KRW
# 총수익률    = -10.0630%
C_BUY_QTY, C_BUY_PX = 20.0, 25.0
C_SELL_QTY, C_SELL_PX = 20.0, 20.0
C_FINAL_CASH = 899_370.0
C_FINAL_EQUITY = 899_370.0
C_GROSS_USD = -100.0
C_FEES_USD = 0.63
C_RETURN_PCT = -10.0630


# ---------------------------------------------------------------------------
# 진단형 assert 헬퍼 — 실패 메시지 자체가 진단이 되도록 한다.
# ---------------------------------------------------------------------------

def check(actual: float, expected: float, *, what: str, layer: str, tol: float = TOL) -> None:
    delta = actual - expected
    assert abs(delta) <= tol, (
        f"\n[사다리 {layer}] {what} 불일치"
        f"\n  기대(손 계산): {expected:,.6f}"
        f"\n  실측(코드)   : {actual:,.6f}"
        f"\n  차이         : {delta:+,.6f}  (허용 {tol:g})"
        f"\n  -> 방금 추가된 계층: {layer}. 직전 단은 통과했으므로 결함은 이 계층에 있다."
    )


def check_invariant(
    *,
    layer: str,
    initial_equity_krw: float,
    final_equity_krw: float,
    realized_gross_usd: float,
    unrealized_usd: float,
    fees_usd: float,
    fx_rate: float = FX_RATE,
    tol: float = TOL,
) -> None:
    """핵심 회계 항등식: 자산 변화 == (실현 + 미실현 - 수수료) x FX. 단일 통화(KRW)."""
    lhs = final_equity_krw - initial_equity_krw
    rhs = (realized_gross_usd + unrealized_usd - fees_usd) * fx_rate
    delta = lhs - rhs
    assert abs(delta) <= tol, (
        f"\n[사다리 {layer}] 회계 항등식 붕괴"
        f"\n  좌변  final_equity - initial_equity = {lhs:+,.6f} KRW"
        f"\n  우변  (실현 {realized_gross_usd:+,.6f} + 미실현 {unrealized_usd:+,.6f}"
        f" - 수수료 {fees_usd:,.6f}) x {fx_rate:,.0f} = {rhs:+,.6f} KRW"
        f"\n  잔차  {delta:+,.6f} KRW  (허용 {tol:g})"
        f"\n  -> 방금 추가된 계층: {layer}. 이 계층이 자산/손익/수수료 중 하나를 잘못 다룬다."
    )


def check_qty_conservation(
    *, layer: str, buy_qty: float, sell_qty: float, final_qty: float, tol: float = 1e-9
) -> None:
    """수량 보존: Σ매수 - Σ매도 == 최종 보유. 깨지면 체결 로그가 포지션과 어긋난 것."""
    expected = buy_qty - sell_qty
    delta = final_qty - expected
    assert abs(delta) <= tol, (
        f"\n[사다리 {layer}] 수량 보존 붕괴"
        f"\n  Σ매수 {buy_qty:g} - Σ매도 {sell_qty:g} = {expected:g}주 이어야 하는데"
        f"\n  실제 보유 수량 = {final_qty:g}주 (차이 {delta:+g})"
        f"\n  -> 방금 추가된 계층: {layer}. 체결 수량과 포지션 수량이 따로 논다."
    )


# ---------------------------------------------------------------------------
# 테스트 전용 Fake — 프로덕션 코드를 건드리지 않고 계층을 하나씩 얹기 위한 것.
# ---------------------------------------------------------------------------

_EMPTY_BARS = pd.DataFrame(
    columns=["open", "high", "low", "close", "volume"],
    index=pd.DatetimeIndex([], tz=NY, name="ts"),
)


class SpotFeed:
    """현재가만 들고 있는 최소 DataFeed. rung 3~6에서 가격을 손으로 움직이기 위한 것."""

    def __init__(self, prices: dict[str, float], ts: datetime = TS0):
        self.prices = dict(prices)
        self.ts = ts

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price

    def quote(self, symbol: str) -> Quote | None:
        price = self.prices.get(symbol)
        if price is None:
            return None
        return Quote(symbol=symbol, ts=self.ts, price=price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return _EMPTY_BARS


class CaptureSink:
    """run_cycle이 흘려보내는 signal/fill을 그대로 모으는 EventSink."""

    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self.fills: list = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class ScriptedStrategy:
    """사이클 인덱스로 신호를 내는 결정론적 전략.

    Donchian을 쓰면 "전략이 무엇을 냈는가"가 변수가 되어 회계 결함과 뒤섞인다.
    여기서는 신호가 사전에 확정돼 있으므로 전략은 상수다.

    build_strategies(cfg)가 `cls(symbols=, params=, market=, id=)`로 부르므로
    시그니처를 그대로 맞춘다. 스크립트는 params["script"](YAML 직렬화 가능한
    dict 리스트)로 주입된다.
    """

    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "ladder"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market
        self.params = params or {}
        self._script: dict[int, list[dict]] = {}
        for step in self.params.get("script", []):
            self._script.setdefault(int(step["cycle"]), []).append(step)
        self.cycle_index = -1

    def on_cycle(self, ctx: Context) -> list[Signal]:
        self.cycle_index += 1
        out = []
        for step in self._script.get(self.cycle_index, []):
            out.append(
                Signal(
                    strategy_id=self.id,
                    symbol=step.get("symbol", self.symbols[0]),
                    action=SignalAction(step["action"]),
                    target_weight=float(step.get("target_weight", 0.0)),
                    exit_fraction=float(step.get("exit_fraction", 1.0)),
                    reason=step.get("reason", f"ladder cycle {self.cycle_index}"),
                )
            )
        return out


# rung 7의 가격 스케줄: 장 전반 $10.00, 12:30부터 $12.00.
LADDER_DAY = datetime(2026, 1, 5, tzinfo=NY)      # 월요일
LADDER_PRICE_EARLY, LADDER_PRICE_LATE = 10.0, 12.0
LADDER_SWITCH_MINUTE = 180                          # 09:30 + 180분 = 12:30


def _ladder_1m_bars() -> pd.DataFrame:
    """09:30~15:59 NY 세션의 1분봉 390개. 종가는 두 단계 계단식."""
    rows = []
    for i in range(390):
        ts = LADDER_DAY.replace(hour=9, minute=30) + timedelta(minutes=i)
        px = LADDER_PRICE_EARLY if i < LADDER_SWITCH_MINUTE else LADDER_PRICE_LATE
        rows.append({"ts": ts, "open": px, "high": px, "low": px, "close": px, "volume": 1000.0})
    return pd.DataFrame(rows).set_index("ts")


class LadderFeed:
    """rung 7에서 StubDataFeed 자리에 끼워 넣는 결정론적 피드.

    engine.run_backtest가 기대하는 계약만 만족한다: `bars_1m`(리플레이 타임라인
    추출용), `set_now`, `quote`, `history`. days/seed 인자는 무시한다.
    """

    def __init__(self, symbols: list[str], days: int | None = None, seed: int | None = None):
        self.symbols = list(symbols)
        bars = _ladder_1m_bars()
        self.bars_1m = {s: bars.copy() for s in self.symbols}
        self._now = bars.index[-1]

    def set_now(self, now) -> None:
        self._now = now

    def _visible(self, symbol: str) -> pd.DataFrame | None:
        bars = self.bars_1m.get(symbol)
        if bars is None:
            return None
        # StubDataFeed와 동일한 look-ahead 금지 규칙: index는 봉 **시가** 시각이므로
        # `index + 1분 <= now`인 봉만 마감된 봉이다. 이 규칙을 흉내내지 않으면
        # 계측기가 프로덕션보다 1분 앞선 가격을 보게 되어 기대값이 어긋난다.
        return bars[bars.index + pd.Timedelta(minutes=1) <= self._now]

    def quote(self, symbol: str) -> Quote | None:
        visible = self._visible(symbol)
        if visible is None or visible.empty:
            return None
        return Quote(symbol=symbol, ts=visible.index[-1], price=float(visible.iloc[-1]["close"]))

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        visible = self._visible(symbol)
        return _EMPTY_BARS if visible is None else visible.tail(n)


RISK_SETTINGS = {
    "risk": {
        "max_position_pct": 50,
        "max_symbol_pct_total": 60,
        "daily_loss_limit_pct": 3,
        "max_orders_per_day": 30,
        # 쿨다운은 이 사다리의 격리 대상이 아니다(회계가 아니라 진입 타이밍 레일).
        # 0으로 꺼서 history() 의존성을 제거한다.
        "cooldown_bars_after_stop": 0,
        "max_order_notional_pct": 60,
    }
}


def make_broker(cash: float = START_CASH_KRW, prices: dict[str, float] | None = None):
    feed = SpotFeed(prices or {SYM: A_BUY_PX})
    portfolio = Portfolio(cash=cash, state_path=None)   # 디스크 상태 절대 안 건드림
    broker = PaperBroker(
        data=feed, portfolio=portfolio, fee_bps=FEE_BPS, market_of=MARKET_OF, fx=fx()
    )
    return feed, portfolio, broker


# ===========================================================================
# RUNG 1 — Portfolio 단독
# ===========================================================================

def test_rung1_portfolio_cash_and_equity_arithmetic():
    """rung 1: Portfolio의 현금/포지션 산술과 equity()만 격리한다.

    브로커도 FX 프로바이더도 없다. 현금은 손으로 계산한 값을 직접 대입하고,
    equity()가 `현금 + 보유수량 x 가격 x FX`를 정확히 재현하는지만 본다.
    여기서 깨지면 그 위 모든 단계가 의미 없다.
    """
    pf = Portfolio(cash=A_CASH_AFTER_BUY, state_path=None)
    pf.positions[SYM] = Position(symbol=SYM, qty=A_BUY_QTY, avg_cost=A_BUY_PX, opened_at=TS0)

    # 평가액 = 50주 x $10.00 x 1000 = 500,000 KRW -> 자산 = 499,650 + 500,000 = 999,650
    check(
        pf.equity({SYM: A_BUY_PX}, MARKET_OF, fx()),
        999_650.0,
        what="매수 직후 자산(진입가 마킹)",
        layer="rung 1 · Portfolio.equity",
    )

    # 가격 $12.00 마킹 -> 50 x 12 x 1000 = 600,000 -> 자산 = 1,099,650 KRW
    check(
        pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx()),
        1_099_650.0,
        what="가격 $12 마킹 시 자산",
        layer="rung 1 · Portfolio.equity",
    )

    # 미실현만 있는 상태의 항등식: 자산변화 = (0 + 미실현 - 지금까지 낸 수수료) x FX
    # 매수 수수료 $0.35만 발생, 미실현 = ($12-$10) x 50 = $100
    check_invariant(
        layer="rung 1 · Portfolio.equity",
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx()),
        realized_gross_usd=0.0,
        unrealized_usd=100.0,
        fees_usd=0.35,
    )


def test_rung1_equity_ignores_flat_positions():
    """rung 1: 청산된(qty=0) 포지션이 자산에 유령으로 남지 않는지 확인한다."""
    pf = Portfolio(cash=A_FINAL_CASH, state_path=None)
    pf.positions[SYM] = Position(symbol=SYM, qty=0.0, avg_cost=0.0)
    check(
        pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx()),
        A_FINAL_CASH,
        what="전량 청산 후 자산(= 현금)",
        layer="rung 1 · Portfolio.equity",
    )


# ===========================================================================
# RUNG 2 — + FX 환산
# ===========================================================================

def test_rung2_fx_conversion_roundtrip():
    """rung 2: to_krw/from_krw만 격리한다. 왕복이 손실 없이 돌아오는가, US/KR 분기가 맞는가."""
    check(to_krw(500.0, "US", fx()), 500_000.0, what="to_krw($500, US)", layer="rung 2 · FX")
    check(to_krw(500.0, "KR", fx()), 500.0, what="to_krw(500, KR) — 환산 없음", layer="rung 2 · FX")
    check(from_krw(500_000.0, "US", fx()), 500.0, what="from_krw(500,000, US)", layer="rung 2 · FX")
    check(
        from_krw(to_krw(1234.5678, "US", fx()), "US", fx()),
        1234.5678,
        what="to_krw -> from_krw 왕복",
        layer="rung 2 · FX",
    )


def test_rung2_omitting_fx_silently_falls_back_to_1500():
    """rung 2: fx 인자를 생략하면 조용히 모듈 기본값(1500)이 쓰인다는 사실을 고정한다.

    이건 버그가 아니라 **함정**이다. 주입 환율과 fallback이 섞이면 자산과 손익이
    다른 환율로 계산되어 조용히 어긋난다. 아래 rung들이 FX_RATE=1000을 주입하는
    이유가 이것 — 어디선가 주입이 빠지면 1000 vs 1500 차이로 즉시 터진다.
    """
    assert to_krw(1.0, "US") == 1_500.0, (
        "\n[사다리 rung 2 · FX] to_krw 기본 fallback이 1500이 아니다 — "
        "이 사다리의 fx 누락 탐지 전제가 무너졌다."
    )
    assert to_krw(1.0, "US", fx()) == FX_RATE


# ===========================================================================
# RUNG 3 — + PaperBroker.place_order
# ===========================================================================

def _place(broker, side: Side, qty: float) -> object:
    return broker.place_order(
        Order(symbol=SYM, side=side, qty=qty, strategy_id="ladder", reason="ladder")
    ).fill


def test_rung3_paper_broker_full_round_trip_scenario_a():
    """rung 3-A: PaperBroker 체결/수수료/포트폴리오 변경. 전량 왕복(이익) 시나리오.

    RiskManager도 run_cycle도 없다 — 수량을 직접 지정해 체결만 시킨다.
    여기서 항등식이 깨지면 현금 반영 또는 수수료 처리가 범인이다.
    """
    layer = "rung 3 · PaperBroker.place_order"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})

    buy = _place(broker, Side.BUY, A_BUY_QTY)
    assert buy is not None
    check(buy.fee, 0.35, what="매수 수수료(USD)", layer=layer)
    check(pf.cash, A_CASH_AFTER_BUY, what="매수 후 현금(KRW)", layer=layer)
    check(pf.positions[SYM].avg_cost, A_BUY_PX, what="매수 후 평균단가(USD)", layer=layer)

    feed.set_price(SYM, A_SELL_PX)
    sell = _place(broker, Side.SELL, A_SELL_QTY)
    assert sell is not None
    check(sell.fee, 0.42, what="매도 수수료(USD)", layer=layer)
    check(pf.cash, A_FINAL_CASH, what="최종 현금(KRW)", layer=layer)
    check(pf.positions[SYM].qty, A_FINAL_QTY, what="최종 보유 수량", layer=layer)

    final_equity = pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx())
    check(final_equity, A_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)
    check(
        (final_equity / START_CASH_KRW - 1) * 100,
        A_RETURN_PCT,
        what="총수익률(%)",
        layer=layer,
        tol=1e-4,
    )
    check_qty_conservation(
        layer=layer, buy_qty=A_BUY_QTY, sell_qty=A_SELL_QTY, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=final_equity,
        realized_gross_usd=A_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=A_FEES_USD,
    )


def test_rung3_paper_broker_partial_exit_scenario_b():
    """rung 3-B: 부분 청산(스케일아웃). 의심되는 결함이 부분 청산에 몰려 있어 별도 단으로 둔다.

    검증 포인트: 부분매도가 평균단가를 건드리지 않는가, 잔여 수량이 정확한가,
    실현+미실현이 자산 변화와 정확히 맞아떨어지는가.
    """
    layer = "rung 3 · PaperBroker 부분청산"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})

    _place(broker, Side.BUY, A_BUY_QTY)
    feed.set_price(SYM, A_SELL_PX)
    sell = _place(broker, Side.SELL, B_SELL_QTY)
    assert sell is not None

    check(pf.cash, B_FINAL_CASH, what="부분청산 후 현금(KRW)", layer=layer)
    check(pf.positions[SYM].qty, B_FINAL_QTY, what="잔여 보유 수량", layer=layer)
    check(
        pf.positions[SYM].avg_cost,
        B_FINAL_AVG_COST,
        what="부분청산 후 평균단가(변하지 않아야 함)",
        layer=layer,
    )

    final_equity = pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx())
    check(final_equity, B_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)
    check(
        (final_equity / START_CASH_KRW - 1) * 100, B_RETURN_PCT,
        what="총수익률(%)", layer=layer, tol=1e-4,
    )
    check_qty_conservation(
        layer=layer, buy_qty=A_BUY_QTY, sell_qty=B_SELL_QTY, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=final_equity,
        realized_gross_usd=B_GROSS_USD,
        unrealized_usd=B_UNREAL_USD,
        fees_usd=B_FEES_USD,
    )


def test_rung3_paper_broker_losing_trade_scenario_c():
    """rung 3-C: 손실 거래. 부호가 뒤집혀도 동일 항등식이 성립해야 한다."""
    layer = "rung 3 · PaperBroker 손실거래"
    feed, pf, broker = make_broker(prices={SYM: C_BUY_PX})

    _place(broker, Side.BUY, C_BUY_QTY)
    check(pf.cash, 499_650.0, what="매수 후 현금(KRW)", layer=layer)

    feed.set_price(SYM, C_SELL_PX)
    _place(broker, Side.SELL, C_SELL_QTY)

    check(pf.cash, C_FINAL_CASH, what="최종 현금(KRW)", layer=layer)
    final_equity = pf.equity({SYM: C_SELL_PX}, MARKET_OF, fx())
    check(final_equity, C_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)
    check(
        (final_equity / START_CASH_KRW - 1) * 100, C_RETURN_PCT,
        what="총수익률(%)", layer=layer, tol=1e-4,
    )
    check_qty_conservation(
        layer=layer, buy_qty=C_BUY_QTY, sell_qty=C_SELL_QTY, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=final_equity,
        realized_gross_usd=C_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=C_FEES_USD,
    )


def test_rung3_fill_realized_pnl_matches_hand_arithmetic_when_present():
    """rung 3: Fill이 realized_pnl을 노출한다면 그 값이 손 계산과 맞는지 본다.

    이 필드는 동시 진행 중인 수정으로 최근 추가됐다 — 없으면 조용히 건너뛴다
    (계측기가 진행 중인 작업에 결합되지 않게 한다).
    """
    layer = "rung 3 · Fill.realized_pnl"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    buy = _place(broker, Side.BUY, A_BUY_QTY)
    feed.set_price(SYM, A_SELL_PX)
    sell = _place(broker, Side.SELL, B_SELL_QTY)

    if getattr(buy, "realized_pnl", None) is None and getattr(sell, "realized_pnl", None) is None:
        pytest.skip("Fill.realized_pnl 미도입 — 이 단은 해당 없음")

    check(getattr(buy, "realized_pnl"), 0.0, what="매수 체결의 실현손익(항상 0)", layer=layer)
    # ($12.00 - $10.00) x 25주 = $50.00, 수수료 차감 전
    check(getattr(sell, "realized_pnl"), 50.0, what="부분매도 실현손익(USD, 수수료 전)", layer=layer)


# ===========================================================================
# RUNG 4 — + RiskManagerImpl.approve (사이징)
# ===========================================================================

def _risk(capital_fraction: float = 1.0) -> RiskManagerImpl:
    return RiskManagerImpl(
        RISK_SETTINGS,
        capital_fraction={"ladder": capital_fraction},
        market_of=MARKET_OF,
        fx=fx(),
    )


def _signal(action: SignalAction, *, weight: float = 0.0, fraction: float = 1.0) -> Signal:
    return Signal(
        strategy_id="ladder",
        symbol=SYM,
        action=action,
        target_weight=weight,
        exit_fraction=fraction,
        reason="ladder",
    )


def test_rung4_risk_sizing_produces_intended_quantity(fake_clock_cls):
    """rung 4: 목표비중 -> 수량 사이징만 격리한다. 의도한 50주가 나오는가.

    자산 1,000,000 KRW, capital_fraction 1.0, target_weight 0.5, max_position_pct 50%
      strategy_capital = 1.0 x 1,000,000 = 1,000,000
      room             = 0.50 x 1,000,000 - 0 = 500,000
      budget           = min(0.5 x 1,000,000, 500,000) = 500,000 KRW
      max_symbol_pct_total 60% -> room_total = 600,000 - 0 = 600,000 (구속 안 됨)
      qty = from_krw(500,000, US) / $10.00 = $500 / $10 = 50주
    """
    layer = "rung 4 · RiskManagerImpl.approve"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)

    order = _risk().approve(_signal(SignalAction.ENTER_LONG, weight=0.5), ctx)
    assert order is not None, f"[사다리 {layer}] 진입 주문이 거부됐다 — 사이징 이전 문제"
    assert order.side is Side.BUY
    check(order.qty, A_BUY_QTY, what="ENTER_LONG 사이징 수량", layer=layer)


def test_rung4_risk_exit_fraction_sizing(fake_clock_cls):
    """rung 4: 청산 사이징. exit_fraction 0.5는 보유 50주 중 정확히 25주여야 한다."""
    layer = "rung 4 · RiskManagerImpl.approve(청산)"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    _place(broker, Side.BUY, A_BUY_QTY)
    feed.set_price(SYM, A_SELL_PX)
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)
    risk = _risk()

    partial = risk.approve(_signal(SignalAction.SCALE_OUT, fraction=0.5), ctx)
    assert partial is not None, f"[사다리 {layer}] SCALE_OUT 거부됨: {risk.last_block}"
    check(partial.qty, B_SELL_QTY, what="SCALE_OUT 0.5 수량", layer=layer)

    full = risk.approve(_signal(SignalAction.EXIT_LONG, fraction=1.0), ctx)
    assert full is not None, f"[사다리 {layer}] EXIT_LONG 거부됨: {risk.last_block}"
    check(full.qty, A_BUY_QTY, what="EXIT_LONG 1.0 수량", layer=layer)


def test_rung4_risk_sizing_then_fill_preserves_invariant(fake_clock_cls):
    """rung 4: 사이징 + 체결을 붙인 상태에서 시나리오 A 전체 항등식이 유지되는가."""
    layer = "rung 4 · approve + place_order"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)
    risk = _risk()

    entry = risk.approve(_signal(SignalAction.ENTER_LONG, weight=0.5), ctx)
    assert entry is not None
    broker.place_order(entry)

    feed.set_price(SYM, A_SELL_PX)
    exit_order = risk.approve(_signal(SignalAction.EXIT_LONG, fraction=1.0), ctx)
    assert exit_order is not None, f"[사다리 {layer}] 청산 거부됨: {risk.last_block}"
    broker.place_order(exit_order)

    final_equity = pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx())
    check(pf.cash, A_FINAL_CASH, what="최종 현금(KRW)", layer=layer)
    check(final_equity, A_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)
    check_qty_conservation(
        layer=layer, buy_qty=entry.qty, sell_qty=exit_order.qty, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=final_equity,
        realized_gross_usd=A_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=A_FEES_USD,
    )


def test_rung4_risk_equity_marks_other_symbols_at_cost_not_market(fake_clock_cls):
    """rung 4(부수 계측): approve()가 쓰는 자산 추정이 **다른 종목을 평균단가로** 마킹한다.

    manager.py:120-128 — 신호 종목만 현재가로, 나머지는 avg_cost로 평가한다.
    그 결과 다른 종목이 크게 오르내려도 사이징 기준 자산이 따라가지 않는다.
    회계 항등식(자산 자체)의 문제는 아니고 **사이징 기준값의 문제**라 별도로 계측한다.

    여기서는 그 편차를 숫자로 고정한다:
      OTH 10주를 $10에 매수 -> notional $100.00, 수수료 $100 x 7bp = $0.07
                              현금 = 1,000,000 - (100 + 0.07) x 1000 = 899,930 KRW
      OTH 현재가 $20 -> 실제 자산 = 899,930 + 10 x 20 x 1000 = 1,099,930 KRW
      approve()의 자산   = 899,930 + 10 x 10 x 1000 =   999,930 KRW  (100,000 과소평가)

    측정 대상 종목(SYM)의 가격은 이 테스트에서만 의도적으로 $0.001로 낮춘다(다른
    rung 테스트가 쓰는 A_BUY_PX=$10과 무관, OTH 매수/마킹 계산에는 영향 없음) — 정수
    수량 사이징(QUICKREF:207) 하에서 9.9993주 같은 소수 수량은 더 이상 나오지 않고
    floor()로 내림된다. $10짜리 종목이면 9.9993 -> 9주로 내림되며 그 1주 오차(약
    10,000원)가 여기서 검증하려는 100,000원짜리 평균단가-마킹 편차 자체를 오염시킨다.
    가격을 $0.001로 낮추면 같은 예산이 99,993주로 커져 1주 내림 오차가 무시할
    수준(1원)이 되고, 이 테스트가 원래 측정하려던 마킹 편차만 정확히 드러난다.
    """
    layer = "rung 4 · approve() 자산 마킹"
    other = "OTH"
    sym_price = 0.001  # 정수 floor 오차를 무시할 수준으로 낮추기 위한 로컬 가격(위 설명 참고)
    market_of = {SYM: "US", other: "US"}
    feed = SpotFeed({SYM: sym_price, other: A_BUY_PX})
    pf = Portfolio(cash=START_CASH_KRW, state_path=None)
    broker = PaperBroker(data=feed, portfolio=pf, fee_bps=FEE_BPS, market_of=market_of, fx=fx())
    broker.place_order(Order(symbol=other, side=Side.BUY, qty=10.0, strategy_id="ladder"))
    check(pf.cash, 899_930.0, what="OTH 매수 후 현금(KRW)", layer=layer)

    feed.set_price(other, 20.0)
    true_equity = pf.equity({SYM: A_BUY_PX, other: 20.0}, market_of, fx())
    check(true_equity, 1_099_930.0, what="Portfolio.equity 기준 실제 자산", layer=layer)

    risk = RiskManagerImpl(
        RISK_SETTINGS, capital_fraction={"ladder": 1.0}, market_of=market_of, fx=fx()
    )
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)
    # target_weight를 자산의 정확히 10%로 요청한다. 사이징이 실제 자산을 봤다면
    # 예산 109,993 KRW -> floor(109,993,000주), 평균단가를 봤다면 99,993 KRW ->
    # floor(99,993,000주) — sym_price=$0.001이라 floor 내림 오차(<=1주=1원)는 무시 가능.
    order = risk.approve(_signal(SignalAction.ENTER_LONG, weight=0.10), ctx)
    assert order is not None, f"[사다리 {layer}] 주문 거부됨: {risk.last_block}"
    implied_equity = order.qty * sym_price * FX_RATE / 0.10

    assert abs(implied_equity - true_equity) > 1.0, (
        f"\n[사다리 {layer}] approve()가 이제 시가 마킹을 쓴다 — 이 계측 테스트는 낡았다."
        f"\n  implied={implied_equity:,.2f} true={true_equity:,.2f}"
    )
    check(
        implied_equity,
        999_930.0,
        what="approve()가 내부적으로 사용한 자산(평균단가 마킹)",
        layer=layer,
        tol=1e-3,
    )


# ===========================================================================
# RUNG 5 — + run_cycle 배선
# ===========================================================================

def test_rung5_run_cycle_wiring_preserves_invariant(fake_clock_cls):
    """rung 5: signal -> approve -> place_order -> sink.on_fill 배선을 얹는다.

    전략은 아직 변수로 두지 않는다 — 신호를 직접 만들어 주입한다.
    여기서 처음 깨진다면 범인은 run_cycle의 배선(체결 누락, 중복 체결, state_update)이다.
    """
    from quant.trade.loop import run_cycle

    layer = "rung 5 · run_cycle"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)
    risk = _risk()
    sink = CaptureSink()

    entry_only = ScriptedStrategy(
        symbols=[SYM],
        params={"script": [{"cycle": 0, "action": "enter_long", "target_weight": 0.5}]},
    )
    run_cycle([entry_only], ctx, risk, sink)
    check(pf.cash, A_CASH_AFTER_BUY, what="run_cycle 진입 후 현금(KRW)", layer=layer)
    check(pf.positions[SYM].qty, A_BUY_QTY, what="run_cycle 진입 후 보유 수량", layer=layer)

    feed.set_price(SYM, A_SELL_PX)
    exit_only = ScriptedStrategy(
        symbols=[SYM],
        params={"script": [{"cycle": 0, "action": "exit_long", "exit_fraction": 1.0}]},
    )
    run_cycle([exit_only], ctx, risk, sink)

    buy_qty = sum(f.qty for f in sink.fills if f.side is Side.BUY)
    sell_qty = sum(f.qty for f in sink.fills if f.side is Side.SELL)
    fees_usd = sum(f.fee for f in sink.fills)

    assert len(sink.fills) == 2, (
        f"\n[사다리 {layer}] 체결 건수 {len(sink.fills)}건 (기대 2건) — 배선이 체결을 흘리거나 중복시킨다."
    )
    check(fees_usd, A_FEES_USD, what="체결 로그 총수수료(USD)", layer=layer)

    final_equity = pf.equity({SYM: A_SELL_PX}, MARKET_OF, fx())
    check(pf.cash, A_FINAL_CASH, what="최종 현금(KRW)", layer=layer)
    check(final_equity, A_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)
    check_qty_conservation(
        layer=layer, buy_qty=buy_qty, sell_qty=sell_qty, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=final_equity,
        realized_gross_usd=A_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=fees_usd,
    )


# ===========================================================================
# RUNG 6 — + 스크립트 전략 (다중 사이클, 스케일아웃 포함)
# ===========================================================================

def test_rung6_scripted_strategy_multi_cycle_scale_out_then_exit(fake_clock_cls):
    """rung 6: 사전 확정 신호를 내는 전략을 여러 사이클에 걸쳐 돌린다.

    Donchian을 쓰지 않는 이유: 전략이 무엇을 냈는지가 변수가 되면 회계 결함과
    구분되지 않는다. 스크립트 전략은 상수이므로 관측되는 차이는 전부 회계 계층 몫이다.

    진행: c0 진입 50주 @$10 -> c1 가격 $12 -> c2 스케일아웃 50%(25주) -> c3 전량청산(25주)
    최종은 시나리오 A와 동일해야 한다: 현금 1,099,230 KRW, 보유 0주.
    """
    from quant.trade.loop import run_cycle

    layer = "rung 6 · 스크립트 전략 x run_cycle"
    feed, pf, broker = make_broker(prices={SYM: A_BUY_PX})
    ctx = Context(clock=fake_clock_cls(TS0), data=feed, broker=broker)
    risk = _risk()
    sink = CaptureSink()

    strategy = ScriptedStrategy(
        symbols=[SYM],
        params={
            "script": [
                {"cycle": 0, "action": "enter_long", "target_weight": 0.5},
                {"cycle": 2, "action": "scale_out", "exit_fraction": 0.5},
                {"cycle": 3, "action": "exit_long", "exit_fraction": 1.0},
            ]
        },
    )

    equity_curve = []
    for cycle in range(4):
        if cycle == 1:
            feed.set_price(SYM, A_SELL_PX)
        run_cycle([strategy], ctx, risk, sink)
        equity_curve.append(pf.equity({SYM: feed.prices[SYM]}, MARKET_OF, fx()))

    # c0: 현금 499,650 + 50 x $10 x 1000 = 999,650
    # c1: 현금 499,650 + 50 x $12 x 1000 = 1,099,650
    # c2: 현금 799,440 + 25 x $12 x 1000 = 1,099,440
    # c3: 현금 1,099,230 + 0             = 1,099,230
    for i, expected in enumerate([999_650.0, 1_099_650.0, 1_099_440.0, 1_099_230.0]):
        check(equity_curve[i], expected, what=f"사이클 {i} 자산(KRW)", layer=layer)

    buy_qty = sum(f.qty for f in sink.fills if f.side is Side.BUY)
    sell_qty = sum(f.qty for f in sink.fills if f.side is Side.SELL)
    fees_usd = sum(f.fee for f in sink.fills)
    check(buy_qty, 50.0, what="Σ매수 수량", layer=layer)
    check(sell_qty, 50.0, what="Σ매도 수량", layer=layer)
    check(fees_usd, A_FEES_USD, what="Σ수수료(USD)", layer=layer)
    check_qty_conservation(
        layer=layer, buy_qty=buy_qty, sell_qty=sell_qty, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=equity_curve[-1],
        realized_gross_usd=A_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=fees_usd,
    )


# ===========================================================================
# RUNG 7 / 8 — + run_backtest 리플레이 루프 + 리포팅 계층
#
# 주의: run_backtest는 PaperBroker/RiskManagerImpl을 **fx 인자 없이** 생성한다
# (engine.py). 즉 백테스트 전 구간이 FixedFxProvider() 기본값 1500을 쓰며 주입
# 지점이 없다. 그래서 rung 7/8만 FX 1500으로 손 계산한다.
# ===========================================================================

FX_BACKTEST = 1_500.0            # run_backtest가 강제하는 기본 환율
BT_START_CASH = 1_500_000.0      # = $1,000.00 — 자릿수를 깔끔하게 맞추려 고른 값

# 가격 스케줄 -> 사이클 매핑 (LadderFeed는 봉이 **마감된** 뒤에만 시세를 노출한다):
#   리플레이 사이클 k의 시각 ts = 09:45 + 15k
#   ts 시점의 시세 = index + 1분 <= ts 인 마지막 1분봉의 종가
#   가격 전환봉은 12:30 시가봉이므로 ts >= 12:31, 즉 ts = 12:45 (k=12)부터 $12.00
BT_REPRICE_CYCLE = 12
_LADDER_SCRIPT = [
    {"cycle": 0, "action": "enter_long", "target_weight": 0.5},
    {"cycle": 14, "action": "scale_out", "exit_fraction": 0.5},
    {"cycle": 20, "action": "exit_long", "exit_fraction": 1.0},
]

# rung 7 손 계산 (FX 1500, 수수료 7bp)
#   시작        자산 = 1,500,000 KRW (아무 것도 하기 전 — 자산곡선의 첫 점)
#   k0  가격 $10 진입: 예산 min(0.5 x 1.5M, 0.5 x 1.5M) = 750,000 KRW
#                      qty = ($750,000/1500) / $10 = $500 / $10 = 50주
#                      현금 = 1,500,000 - (500 + 0.35) x 1500 = 1,500,000 - 750,525 = 749,475
#                      자산 = 749,475 + 50 x 10 x 1500 = 1,499,475 KRW
#   k12 가격 $12 전환: 자산 = 749,475 + 50 x 12 x 1500 = 1,649,475 KRW
#   k14 스케일아웃 25주 @$12: 현금 += (300 - 0.21) x 1500 = 449,685 -> 1,199,160
#                      자산 = 1,199,160 + 25 x 12 x 1500 = 1,649,160 KRW
#   k20 전량청산 25주 @$12:  현금 += 449,685 -> 1,648,845, 자산 = 1,648,845 KRW
#   Σ실현 총손익 = ($12-$10) x 50 = $100 -> 150,000 KRW
#   Σ수수료      = 0.35 + 0.21 + 0.21 = $0.77 -> 1,155 KRW
#   최종자산 - 초기자본 = 1,648,845 - 1,500,000 = 148,845 KRW = 150,000 - 1,155 ✓
#   총수익률     = 1,648,845 / 1,500,000 - 1 = +9.9230% -> 반올림 9.92
BT_EQ_AFTER_ENTRY = 1_499_475.0
BT_EQ_AT_REPRICE = 1_649_475.0
BT_EQ_AFTER_SCALE_OUT = 1_649_160.0
BT_FINAL_EQUITY = 1_648_845.0
BT_GROSS_USD = 100.0
BT_FEES_USD = 0.77
BT_BUY_FEE_USD = 0.35
BT_REALIZED_KRW = 150_000.0
BT_FEES_KRW = 1_155.0
BT_BUY_FEE_KRW = 525.0
BT_EQUITY_CHANGE_KRW = 148_845.0
BT_TRUE_RETURN_PCT = 9.92        # 초기자본 1,500,000 기준 (metrics 반올림 자릿수)

_LADDER_SETTINGS_YAML = f"""
engine:
  poll_seconds: 10
universe:
  us: [{SYM}]
strategies:
  ladder:
    class: ladder
    enabled: true
    capital_fraction: 1.0
    symbols: [{SYM}]
    params:
      script:
{chr(10).join(
    "        - {" + ", ".join(f"{k}: {v}" for k, v in step.items()) + "}"
    for step in _LADDER_SCRIPT
)}
risk:
  max_position_pct: 50
  max_symbol_pct_total: 60
  daily_loss_limit_pct: 3
  max_orders_per_day: 30
  cooldown_bars_after_stop: 0
  cooldown_bar_interval_minutes: 15
  max_order_notional_pct: 60
execution:
  broker: toss
  fee_bps: {FEE_BPS:g}
backtest:
  start_cash_krw: {BT_START_CASH:.0f}
"""


@pytest.fixture
def ladder_backtest(tmp_path, monkeypatch):
    """run_backtest를 스크립트 전략 + 결정론 피드로 갈아끼워 1거래일을 리플레이한다.

    프로덕션 코드는 건드리지 않고 레지스트리/피드만 monkeypatch로 교체한다 —
    리플레이 루프와 equity curve 구성 자체는 engine.py의 진짜 코드가 돈다.
    """
    settings_path = tmp_path / "ladder_settings.yaml"
    settings_path.write_text(_LADDER_SETTINGS_YAML, encoding="utf-8")

    monkeypatch.setitem(strategies_pkg.STRATEGY_REGISTRY, "ladder", ScriptedStrategy)
    monkeypatch.setattr(bt_engine, "StubDataFeed", LadderFeed)

    return run_backtest(
        strategy_id="ladder",
        days=1,
        interval="15m",
        source="stub",
        settings_path=str(settings_path),
    )


def test_rung7_replay_loop_equity_curve_matches_hand_arithmetic(ladder_backtest):
    """rung 7: run_backtest의 리플레이 루프 + equity curve 구성만 격리한다.

    피드도 전략도 상수이므로 equity curve의 모든 값이 손으로 계산 가능하다.
    여기서 처음 깨지면 범인은 리플레이 루프(중복 사이클, 시계 전진, 마킹 가격 선택)다.

    자산곡선의 첫 점은 "아무 것도 하기 전"의 시작 자본이어야 한다 — 첫 사이클에서
    체결이 나면 그 손익이 수익률 계산에서 통째로 빠지기 때문이다. 따라서 점 개수는
    리플레이 봉 25개 + 시작점 1개 = 26개다.
    """
    layer = "rung 7 · run_backtest 리플레이 루프"
    curve = ladder_backtest.equity_curve

    assert len(curve) == 26, (
        f"\n[사다리 {layer}] 자산곡선 점 개수 {len(curve)} (기대 26 = 시작점 1 + 리플레이 25)"
        f"\n  09:30~15:59 1분봉 390개 -> 15분봉 26bin -> 형성중 1bin drop = 25 완성봉."
        f"\n  개수가 다르면 리플레이 타임라인 또는 시작점 처리가 바뀐 것이다."
    )
    check(float(curve.iloc[0]), BT_START_CASH, what="시작점 자산 = 초기자본(KRW)", layer=layer)
    check(float(curve.iloc[1]), BT_EQ_AFTER_ENTRY, what="k0 진입 후 자산(KRW)", layer=layer)
    check(
        float(curve.iloc[BT_REPRICE_CYCLE]), BT_EQ_AFTER_ENTRY,
        what="k11 자산(가격 $10 유지)", layer=layer,
    )
    check(
        float(curve.iloc[BT_REPRICE_CYCLE + 1]), BT_EQ_AT_REPRICE,
        what="k12 자산(가격 $12 전환)", layer=layer,
    )
    check(float(curve.iloc[15]), BT_EQ_AFTER_SCALE_OUT, what="k14 스케일아웃 후 자산", layer=layer)
    check(float(curve.iloc[21]), BT_FINAL_EQUITY, what="k20 전량청산 후 자산", layer=layer)
    check(float(curve.iloc[-1]), BT_FINAL_EQUITY, what="최종 자산(KRW)", layer=layer)

    trades = ladder_backtest.trades
    buy_qty = float(trades.loc[trades["side"] == "buy", "qty"].sum())
    sell_qty = float(trades.loc[trades["side"] == "sell", "qty"].sum())
    check(buy_qty, 50.0, what="Σ매수 수량", layer=layer)
    check(sell_qty, 50.0, what="Σ매도 수량", layer=layer)
    check(float(trades["fee"].sum()), BT_FEES_USD, what="Σ수수료(표시통화 USD)", layer=layer)
    check_qty_conservation(layer=layer, buy_qty=buy_qty, sell_qty=sell_qty, final_qty=0.0)

    check_invariant(
        layer=layer,
        initial_equity_krw=BT_START_CASH,
        final_equity_krw=float(curve.iloc[-1]),
        realized_gross_usd=BT_GROSS_USD,
        unrealized_usd=0.0,
        fees_usd=BT_FEES_USD,
        fx_rate=FX_BACKTEST,
    )


def test_rung8_trade_pnl_is_krw_and_includes_both_side_fees(ladder_backtest):
    """rung 8: 리포팅 계층. 체결별 손익이 KRW이고 **매수/매도 수수료를 모두** 반영하는가.

    이 사다리가 처음 겨눈 결함이 바로 여기였다: 예전 `_compute_pnl`은 매도 행에만
    `(가격-원가)x수량 - 매도수수료`를 넣어 **매수 수수료를 어디에도 반영하지 않았고**,
    값의 통화도 USD여서 KRW 자산곡선과 나란히 놓을 수 없었다.

    손 계산 (FX 1500):
      매수 행: 실현 0, 수수료 $0.35 -> 525 KRW  => pnl = -525 KRW
      매도 행(x2): 실현 ($12-$10)x25 = $50 -> 75,000 KRW, 수수료 $0.21 -> 315 KRW
                   => pnl = 74,685 KRW
      Σpnl = -525 + 74,685 x 2 = 148,845 KRW  == 자산 변화 148,845 KRW
    """
    layer = "rung 8 · trades 손익 통화/수수료"
    trades = ladder_backtest.trades

    for col in ("fee_krw", "realized_pnl_krw", "pnl"):
        assert col in trades.columns, (
            f"\n[사다리 {layer}] 체결 로그에 {col} 컬럼이 없다 — 손익을 기준통화로 읽을 수 없다."
        )

    buys = trades[trades["side"] == "buy"]
    sells = trades[trades["side"] == "sell"]
    assert len(buys) == 1 and len(sells) == 2, (
        f"\n[사다리 {layer}] 체결 구성이 기대와 다르다: 매수 {len(buys)}건 매도 {len(sells)}건 (기대 1/2)"
    )

    check(float(buys["fee_krw"].iloc[0]), BT_BUY_FEE_KRW, what="매수 수수료(KRW)", layer=layer)
    check(float(buys["realized_pnl_krw"].iloc[0]), 0.0, what="매수 실현손익(항상 0)", layer=layer)
    check(float(buys["pnl"].iloc[0]), -BT_BUY_FEE_KRW, what="매수 행 pnl(= -수수료)", layer=layer)
    for i in range(2):
        check(float(sells["realized_pnl_krw"].iloc[i]), 75_000.0,
              what=f"매도 {i}번 실현손익(KRW, 수수료 전)", layer=layer)
        check(float(sells["fee_krw"].iloc[i]), 315.0, what=f"매도 {i}번 수수료(KRW)", layer=layer)
        check(float(sells["pnl"].iloc[i]), 74_685.0, what=f"매도 {i}번 pnl(KRW, 수수료 후)", layer=layer)

    check(float(trades["realized_pnl_krw"].sum()), BT_REALIZED_KRW,
          what="Σ실현손익(KRW)", layer=layer)
    check(float(trades["fee_krw"].sum()), BT_FEES_KRW, what="Σ수수료(KRW)", layer=layer)

    # 핵심: 손익 합계가 자산곡선 변화와 **같은 통화로, 같은 값으로** 맞아야 한다.
    delta_equity = float(ladder_backtest.equity_curve.iloc[-1]) - BT_START_CASH
    check(delta_equity, BT_EQUITY_CHANGE_KRW, what="자산 변화(KRW)", layer=layer)
    check(
        float(trades["pnl"].sum()),
        delta_equity,
        what="Σtrades.pnl (자산 변화와 일치해야 함, 둘 다 KRW)",
        layer=layer,
    )


def test_rung8_reconciliation_block_decomposes_the_equity_change(ladder_backtest):
    """rung 8: run_backtest가 붙여주는 회계 검산 내역이 손 계산과 일치하는가.

    이 블록이 존재한다는 것 자체가 "자산곡선·체결로그·손익합계가 서로 다른 이야기를
    해도 아무도 모르는" 상태의 반대다. 값이 손 계산과 맞는지까지 확인한다.
    """
    layer = "rung 8 · reconciliation"
    rec = ladder_backtest.reconciliation
    assert rec, f"\n[사다리 {layer}] reconciliation 블록이 비어 있다 — 검산 증거가 없다."
    assert rec.get("currency") == "KRW", (
        f"\n[사다리 {layer}] 검산 통화가 KRW가 아니다: {rec.get('currency')!r}"
    )

    check(rec["initial_equity"], BT_START_CASH, what="초기자산(KRW)", layer=layer)
    check(rec["final_equity"], BT_FINAL_EQUITY, what="최종자산(KRW)", layer=layer)
    check(rec["equity_change"], BT_EQUITY_CHANGE_KRW, what="자산 변화(KRW)", layer=layer)
    check(rec["realized_pnl"], BT_REALIZED_KRW, what="Σ실현손익(KRW)", layer=layer)
    check(rec["unrealized_pnl"], 0.0, what="미실현(전량청산이므로 0)", layer=layer)
    check(rec["fees"], BT_FEES_KRW, what="Σ수수료(KRW)", layer=layer)
    check(rec["residual"], 0.0, what="항등식 잔차", layer=layer)

    pos = rec["positions"][SYM]
    check_qty_conservation(
        layer=layer, buy_qty=pos["buy_qty"], sell_qty=pos["sell_qty"], final_qty=pos["final_qty"]
    )


def test_rung8_metrics_denominator_is_initial_capital(ladder_backtest):
    """rung 8: 총수익률의 분모가 **초기자본**인가 (첫 사이클 체결 후 자산이 아니라).

    손 계산:
      초기자본 기준   = 1,648,845 / 1,500,000 - 1 = +9.9230% -> 9.92
      첫 사이클 후 기준 = 1,648,845 / 1,499,475 - 1 = +9.9615% -> 9.96  (틀린 값)
    두 값이 0.04%p 차이 나므로 이 테스트는 분모가 뒤로 밀리면 반드시 실패한다.
    """
    layer = "rung 8 · _compute_metrics"
    metrics = ladder_backtest.metrics
    wrong_pct = round((BT_FINAL_EQUITY / BT_EQ_AFTER_ENTRY - 1) * 100, 2)

    assert metrics["total_return_pct"] != wrong_pct, (
        f"\n[사다리 {layer}] 총수익률 분모가 첫 사이클 체결 **후** 자산이다."
        f"\n  보고 {metrics['total_return_pct']}% == 잘못된 분모 기준값 {wrong_pct}%"
        f"\n  올바른 값(초기자본 {BT_START_CASH:,.0f}원 기준)은 {BT_TRUE_RETURN_PCT}%"
        f"\n  -> 첫 사이클에서 난 체결의 손익이 수익률에서 통째로 빠진다."
    )
    check(
        metrics["total_return_pct"], BT_TRUE_RETURN_PCT,
        what="total_return_pct(초기자본 기준)", layer=layer, tol=1e-9,
    )
    assert metrics["n_trades"] == 2 and metrics["win_rate"] == 100.0, (
        f"\n[사다리 {layer}] 매도 2건/승률 100%가 기대값인데 {metrics}"
    )


def test_rung8_reconcile_raises_when_trade_log_contradicts_equity_curve():
    """rung 8: 검산 레일이 실제로 잡는가 — 일부러 어긋난 입력을 넣어 확인한다.

    검산이 통과만 하고 절대 실패하지 않는다면 그것은 레일이 아니라 장식이다.
    자산 변화(+100,000)와 체결 로그(실현 0)가 모순되는 입력을 만들어, 예외가 나고
    메시지에 잔차가 담기는지 본다.
    """
    layer = "rung 8 · _reconcile 가드"
    idx = pd.DatetimeIndex([TS0, TS0 + timedelta(minutes=15)], name="ts")
    equity = pd.Series([1_000_000.0, 1_100_000.0], index=idx)
    trades = pd.DataFrame(
        [{
            "ts": TS0, "symbol": SYM, "side": "buy", "qty": 10.0, "price": 10.0,
            "fee": 0.0, "fee_krw": 0.0, "realized_pnl_krw": 0.0, "pnl": 0.0, "reason": "",
        }]
    )
    positions = {SYM: Position(symbol=SYM, qty=10.0, avg_cost=10.0)}

    with pytest.raises(ReconciliationError) as excinfo:
        _reconcile(
            equity_curve=equity, trades=trades, positions=positions,
            last_prices={SYM: 10.0}, market_of=MARKET_OF, fx=fx(),
        )
    assert "100,000" in str(excinfo.value), (
        f"\n[사다리 {layer}] 예외는 났지만 메시지에 잔차 100,000원이 없다 — 진단이 안 된다."
        f"\n  {excinfo.value}"
    )

    # 반대로, 같은 자산 변화가 미실현으로 설명되면 통과해야 한다(오탐 방지).
    # 미실현 = 10주 x ($20 - $10) x 1000 = 100,000 KRW
    ok = _reconcile(
        equity_curve=equity, trades=trades, positions=positions,
        last_prices={SYM: 20.0}, market_of=MARKET_OF, fx=fx(),
    )
    check(ok["unrealized_pnl"], 100_000.0, what="미실현으로 설명되는 자산 변화", layer=layer)
    check(ok["residual"], 0.0, what="정상 입력의 잔차", layer=layer)


def test_rung8_compute_metrics_on_empty_and_flat_curves():
    """rung 8: 리포팅 계층의 경계값. 빈 커브와 무변동 커브에서 0을 돌려주는가."""
    layer = "rung 8 · _compute_metrics 경계"
    empty = _compute_metrics(pd.Series(dtype=float), pd.DataFrame())
    assert empty["total_return_pct"] == 0.0 and empty["n_trades"] == 0, (
        f"\n[사다리 {layer}] 빈 커브 지표가 0이 아니다: {empty}"
    )
    idx = pd.DatetimeIndex([TS0 + timedelta(minutes=15 * i) for i in range(5)], name="ts")
    flat = pd.Series([BT_START_CASH] * 5, index=idx)
    m = _compute_metrics(flat, pd.DataFrame(columns=["side", "pnl"]))
    check(m["total_return_pct"], 0.0, what="무변동 커브 총수익률", layer=layer, tol=1e-9)
    check(m["mdd_pct"], 0.0, what="무변동 커브 MDD", layer=layer, tol=1e-9)


# ===========================================================================
# RUNG 9 — 캡스톤: 실제(stub) 백테스트에 같은 항등식을 적용
# ===========================================================================

def test_rung9_real_stub_backtest_reconciles_by_independent_reconstruction():
    """rung 9(캡스톤): 진짜 Donchian stub 백테스트를 **체결 로그만으로** 독립 재구성한다.

    앞선 단들은 손으로 만든 시나리오였다. 이 단은 같은 항등식을 실제 수백 건의
    체결에 적용한다. 중요한 것은 엔진이 스스로 붙여준 `realized_pnl_krw`를 **쓰지
    않는다**는 점이다 — price/qty/side만 읽어 평균단가를 처음부터 다시 굴린다.
    엔진의 계산과 계측기의 계산이 독립적으로 같은 답을 내야 검산에 의미가 있다.

        Δ자산(KRW) == [Σ(매도가 - 평균단가) x 수량 + 미실현 - Σ수수료] x 1500

    성립하면 "자산곡선과 체결 로그가 같은 이야기를 한다"가 확정된다.
    성립하지 않으면 잔차가 어긋난 금액을 그대로 알려준다.
    """
    layer = "rung 9 · 실제 stub 백테스트 정산"
    result = run_backtest(strategy_id="donchian", days=60, interval="15m", source="stub")
    trades = result.trades
    assert not trades.empty, f"[사다리 {layer}] 체결이 0건 — 정산할 것이 없다."

    start_cash = 5_000_000.0   # settings.yaml에 backtest.start_cash_krw가 없을 때의 엔진 기본값
    fx_rate = FX_BACKTEST      # run_backtest는 FixedFxProvider() 기본값 1500을 강제한다
    check(
        float(result.equity_curve.iloc[0]), start_cash,
        what="자산곡선 시작점 = 초기자본(KRW)", layer=layer,
    )

    # --- 체결 로그만으로 평균단가/실현손익/미실현을 독립 재구성 ---
    qty: dict[str, float] = {}
    avg: dict[str, float] = {}
    gross_usd = 0.0
    for _, row in trades.iterrows():
        sym, q, px = row["symbol"], float(row["qty"]), float(row["price"])
        if row["side"] == "buy":
            prev_q, prev_avg = qty.get(sym, 0.0), avg.get(sym, 0.0)
            new_q = prev_q + q
            avg[sym] = (prev_avg * prev_q + px * q) / new_q
            qty[sym] = new_q
        else:
            held = qty.get(sym, 0.0)
            assert q <= held + 1e-6, (
                f"\n[사다리 {layer}] {sym}: 보유 {held:g}주인데 {q:g}주를 매도했다 —"
                f" 체결 로그가 물리적으로 불가능한 수량을 담고 있다."
            )
            gross_usd += (px - avg.get(sym, px)) * q
            qty[sym] = held - q
            if qty[sym] <= 1e-9:
                qty[sym] = 0.0

    fees_usd = float(trades["fee"].sum())

    # 잔여 포지션은 엔진이 마킹에 쓴 것과 **같은 가격**으로 평가해야 한다.
    # (다른 가격을 쓰면 잔차가 나는데, 그건 회계 결함이 아니라 계측 오류다.)
    engine_unreal_krw = float(result.reconciliation["unrealized_pnl"])

    for sym, q in qty.items():
        pos_qty = result.reconciliation["positions"][sym]["final_qty"]
        check_qty_conservation(
            layer=f"{layer} · {sym}",
            buy_qty=float(trades.loc[(trades["symbol"] == sym) & (trades["side"] == "buy"), "qty"].sum()),
            sell_qty=float(trades.loc[(trades["symbol"] == sym) & (trades["side"] == "sell"), "qty"].sum()),
            final_qty=pos_qty,
        )
        check(q, pos_qty, what=f"{sym} 독립 재구성 보유 수량", layer=layer, tol=1e-6)

    final_equity_krw = float(result.equity_curve.iloc[-1])
    check_invariant(
        layer=layer,
        initial_equity_krw=start_cash,
        final_equity_krw=final_equity_krw,
        realized_gross_usd=gross_usd,
        unrealized_usd=engine_unreal_krw / fx_rate,
        fees_usd=fees_usd,
        fx_rate=fx_rate,
        # 수백 건 누적이라 float 여유를 준다. 1원은 초기자본의 0.00002%.
        tol=1.0,
    )

    # 엔진이 붙인 실현손익과 계측기의 독립 재구성이 같은 답인가 (교차검증의 핵심).
    check(
        float(trades["realized_pnl_krw"].sum()),
        gross_usd * fx_rate,
        what="엔진 Σrealized_pnl_krw  vs  독립 재구성 Σ실현손익 x FX",
        layer=layer,
        tol=1.0,
    )
    # 손익 합계와 자산 변화가 같은 통화, 같은 값인가.
    check(
        float(trades["pnl"].sum()) + engine_unreal_krw,
        final_equity_krw - start_cash,
        what="Σtrades.pnl + 미실현  vs  Δ자산 (전부 KRW)",
        layer=layer,
        tol=1.0,
    )


# ===========================================================================
# RUNG 10 — 사다리가 실측에서 끌어낸 프로덕션 결함
#
# rung 1~9는 손으로 만든 짧은 시나리오라 전부 통과한다. 그런데 같은 항등식을
# **긴 구간**의 실제 백테스트에 걸면 무너진다(days>=400에서 ReconciliationError).
# 그 원인을 최소 재현으로 좁힌 것이 아래 두 건이다. 둘 다 실재하는 결함이므로
# xfail(strict=True)로 고정한다 — 수정되면 XPASS로 뒤집혀 이 표시를 지우라고 알린다.
# ===========================================================================

def test_rung10_paper_broker_qty_epsilon_destroys_value_at_small_quantities():
    """rung 10-A: PaperBroker의 `pos.qty <= 1e-9` 스냅이 **수량이 아니라 가치를 파괴**한다.

    quant/adapters/execution/paper.py 매도 분기 말미:

        if pos.qty <= 1e-9:
            pos.qty = 0.0; pos.avg_cost = 0.0; pos.opened_at = None; pos.meta = {}

    의도는 전량청산 후 float 먼지를 터는 것이다. 문제는 **절대 임계값**이라는 점이다.
    주식 수량의 자연스러운 스케일은 가격에 반비례한다(예산이 고정이므로). 가격이
    충분히 크면 정상적인 잔여 포지션이 통째로 1e-9 아래에 들어가고, 그러면 이 분기가
    **현금을 한 푼도 넣어주지 않은 채 포지션을 0으로 지운다.** 사라진 평가액은
    자산에서 그냥 증발한다.

    최소 재현 (수수료 0으로 두어 수수료 효과를 배제):
      현금 1,000,000 KRW, 가격 $3e15, 6.6e-15주 매수
        notional = 6.6e-15 x 3e15 = $19.80 -> 29,700 KRW
        현금 = 970,300 KRW, 자산 = 970,300 + 29,700 = 1,000,000 KRW
      절반(3.3e-15주) 매도:
        현금 += 3.3e-15 x 3e15 x 1500 = 14,850 KRW -> 985,150 KRW
        잔여 3.3e-15주는 1e-9 미만 -> 0으로 스냅 -> 평가액 14,850 KRW 증발
        자산 = 985,150 KRW.  수수료 0인데 자산이 14,850원(1.485%) 줄었다.

    또한 수량 보존이 깨진다: Σ매수 6.6e-15 - Σ매도 3.3e-15 = 3.3e-15 != 최종 0.
    (이것이 "불가능한 잔여 수량"의 정체다.)

    영향: 실거래 가격대(TQQQ ~$50 -> 수십 주)에서는 발동하지 않는 잠재 결함이다.
    그러나 stub 백테스트는 아래 10-B 때문에 가격이 1e15까지 가므로 **매 스케일아웃마다**
    발동해 자산을 반복적으로 갉아먹는다.
    """
    layer = "rung 10-A · PaperBroker 수량 epsilon"
    price, qty = 3.0e15, 6.6e-15
    feed = SpotFeed({SYM: price})
    pf = Portfolio(cash=START_CASH_KRW, state_path=None)
    broker = PaperBroker(data=feed, portfolio=pf, fee_bps=0.0, market_of=MARKET_OF, fx=fx())

    broker.place_order(Order(symbol=SYM, side=Side.BUY, qty=qty, strategy_id="ladder"))
    equity_before = pf.equity({SYM: price}, MARKET_OF, fx())
    check(equity_before, START_CASH_KRW, what="매수 직후 자산(수수료 0이므로 불변)", layer=layer)

    broker.place_order(Order(symbol=SYM, side=Side.SELL, qty=qty / 2, strategy_id="ladder"))
    equity_after = pf.equity({SYM: price}, MARKET_OF, fx())

    check_qty_conservation(
        layer=layer, buy_qty=qty, sell_qty=qty / 2, final_qty=pf.positions[SYM].qty
    )
    check_invariant(
        layer=layer,
        initial_equity_krw=START_CASH_KRW,
        final_equity_krw=equity_after,
        realized_gross_usd=0.0,     # 매수가 == 매도가 이므로 실현손익 0
        unrealized_usd=0.0,
        fees_usd=0.0,               # 수수료를 0으로 껐다
    )


# 2026-08-06 수정 완료 — 이제 정상 통과해야 한다(회귀 방지용으로 유지).


def test_rung10_stub_feed_price_compounds_without_bound():
    """rung 10-B: StubDataFeed의 합성 가격이 **무제한 복리 상승**한다 — 장기 백테스트가 무의미하다.

    quant/adapters/data/stub.py:
        _BURST_EVERY_N_BINS = 8
        _BURST_DRIFT_MEAN   = 0.0016     # 버스트 bin의 분당 드리프트 평균
        _BASE_DRIFT_MEAN    = 0.0        # 나머지 bin
        price = max(price * (1 + drift), 0.5)

    버스트 bin이 8개 중 1개이므로 분당 기대 드리프트 = 0.0016 / 8 = 0.0002.
    세션 1일 = 390분 이므로 하루 기대 배율 = (1 + 0.0002)^390 ~= e^0.078 ~= +8.1%/일.
    **평균회귀 항이 없다.** 따라서 가격은 지수적으로 발산한다:

      120 거래일: 50 x 1.081^120  ~= 1e5 규모
      400 거래일: 50 x 1.081^400  ~= 1e15 규모   (실측 3.3e15)

    결과: (1) 이 데이터로 낸 장기 백테스트 수치는 전략 성과가 아니라 드리프트 상수의
    함수다. (2) 가격이 1e15까지 가면 예산 고정 사이징이 1e-15주를 만들어 위 10-A의
    수량 epsilon을 매 스케일아웃마다 밟는다 — 실제로 days>=400에서 회계 검산이
    ReconciliationError로 터진다.

    이 테스트는 "합성 가격이 시작가의 1000배를 넘지 않는다"는 최소한의 온전성만 건다.
    """
    layer = "rung 10-B · StubDataFeed 가격 발산"
    from quant.adapters.data.stub import START_PRICE, StubDataFeed

    feed = StubDataFeed(["TQQQ"], days=120)
    closes = feed.bars_1m["TQQQ"]["close"]
    max_close = float(closes.max())
    ratio = max_close / START_PRICE

    assert ratio < 1_000.0, (
        f"\n[사다리 {layer}] 120 거래일 만에 합성 가격이 시작가의 {ratio:,.0f}배가 됐다."
        f"\n  시작 {START_PRICE:,.2f} -> 최대 {max_close:,.6g}"
        f"\n  분당 기대 드리프트 0.0016/8 = 0.0002 -> 일 +8.1% 복리, 평균회귀 없음."
        f"\n  -> 이 데이터 위의 장기 백테스트 수치는 전략 성과가 아니다."
    )


# 2026-08-06 수정 완료 — 이제 정상 통과해야 한다(회귀 방지용으로 유지).
