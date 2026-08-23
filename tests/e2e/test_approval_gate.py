"""승인 게이트 불변식 — loop.py를 실제로 구동해서 검증한다.

이 스위트가 지키는 것(하나라도 깨지면 실거래에 올리면 안 된다):
  1. 청산은 절대 승인을 거치지 않는다 (kill-switch flatten 포함)
  2. 만료된 요청은 실행되지 않는다
  3. 같은 요청을 두 번 승인해도 한 번만 산다 (멱등)
  4. 텔레그램이 죽으면 신규 진입은 일어나지 않는다 (fail-closed) — 청산은 영향 없음
  5. 승인 시점에 최신 시세로 재검증한다 (가격 드리프트 초과 시 미실행)
그리고 approval=None이면 기존 동작과 100% 동일하다는 회귀 가드.

전부 오프라인 페이크만 쓴다 — 실제 텔레그램 API는 호출하지 않고, run_paper_loop은
asyncio.sleep을 패치해 지정한 사이클 수만큼만 돌린다(tests/test_loop_resilience.py와
동일한 구동 방식).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from quant.trade.approval import ApprovalGate
from quant.apps.config import Settings
from quant.trade.control import TradingControl
from quant.trade.loop import run_cycle, run_paper_loop
from quant.core.ports import Context
from quant.core.models import Fill, Order, Position, Quote, Side, Signal, SignalAction

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
_APPROVAL_CFG = {"expire_seconds": 300, "max_price_drift_pct": 0.5, "prune_after_seconds": 86400}


# --------------------------------------------------------------------- fakes

class FakeClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def is_market_open(self, market: str) -> bool:
        return True

    def minutes_to_close(self, market: str) -> float | None:
        return 120.0

    def cadence_minutes(self) -> float:
        return 0.1

    def should_flatten(self, market: str, flatten_minutes: float) -> bool:
        return False


class FakeDataFeed:
    """price를 테스트 도중 바꿔 승인 시점 재검증(드리프트)을 구동한다."""

    def __init__(self, price: float = 100.0):
        self.price = price

    def quote(self, symbol: str) -> Quote | None:
        if self.price is None:
            return None
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=self.price)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=_OHLCV_COLUMNS)


class FakeBroker:
    def __init__(self, positions: dict[str, Position] | None = None):
        self._positions = positions or {}
        self.orders: list[Order] = []

    def place_order(self, order: Order):
        self.orders.append(order)
        pos = self._positions.get(order.symbol)
        if order.side is Side.BUY:
            if pos is None:
                pos = Position(symbol=order.symbol, qty=0.0, avg_cost=0.0)
                self._positions[order.symbol] = pos
            pos.qty += order.qty
            pos.avg_cost = 100.0
        else:
            if pos is None or order.qty <= 0:
                return None
            pos.qty = max(pos.qty - order.qty, 0.0)
        return Fill(
            symbol=order.symbol, side=order.side, qty=order.qty, price=100.0,
            ts=datetime.now(timezone.utc), strategy_id=order.strategy_id, reason=order.reason,
        )

    def positions(self) -> dict[str, Position]:
        return self._positions

    def cash(self) -> float:
        return 1_000_000.0

    @property
    def buys(self) -> list[Order]:
        return [o for o in self.orders if o.side is Side.BUY]

    @property
    def sells(self) -> list[Order]:
        return [o for o in self.orders if o.side is Side.SELL]


class FakeRisk:
    def __init__(self):
        self.calls: list[Signal] = []
        self.last_block = ""

    def approve(self, signal: Signal, ctx: Context, risk_multiplier: float = 1.0, marks=None):
        self.calls.append(signal)
        if signal.action in (SignalAction.ENTER_LONG, SignalAction.SCALE_IN):
            return Order(symbol=signal.symbol, side=Side.BUY, qty=1.0,
                         strategy_id=signal.strategy_id, reason=signal.reason)
        pos = ctx.broker.positions().get(signal.symbol)
        qty = (pos.qty if pos is not None else 0.0) * signal.exit_fraction
        if qty <= 0:
            self.last_block = "보유 없음"
            return None
        return Order(symbol=signal.symbol, side=Side.SELL, qty=qty,
                     strategy_id=signal.strategy_id, reason=signal.reason)


class FakeSink:
    def __init__(self):
        self.signals: list[Signal] = []
        self.fills: list[Fill] = []

    def on_signal(self, signal: Signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def send(self, text: str) -> None:
        self.messages.append(text)


class FakeStrategy:
    def __init__(self, signals_fn=None, id: str = "fake"):
        self.id = id
        self.symbols = ["TQQQ"]
        self._signals_fn = signals_fn or (lambda: [])
        self.calls = 0

    def on_cycle(self, ctx: Context) -> list[Signal]:
        self.calls += 1
        return self._signals_fn()


class FakeApprovalBot:
    """텔레그램 승인 봇 대역.

    auto_approve/auto_reject가 True면 사용자가 알림을 받자마자 버튼을 누른 것처럼
    다음 poll_decisions()에서 결정을 돌려준다. send_ok=False면 전송 실패(=텔레그램
    다운) 상황이다."""

    def __init__(self, send_ok: bool = True, auto_approve: bool = False,
                 auto_reject: bool = False, press_twice: bool = False):
        self.send_ok = send_ok
        self.auto_approve = auto_approve
        self.auto_reject = auto_reject
        self.press_twice = press_twice
        self.sent: list = []
        self.finalized: list[tuple[int | None, str]] = []
        self._queued: list[tuple[str, bool]] = []
        self._next_message_id = 100

    def send_request(self, req, expire_seconds: float) -> int | None:
        self.sent.append(req)
        if not self.send_ok:
            return None
        if self.auto_approve or self.auto_reject:
            approved = self.auto_approve
            self._queued.append((req.id, approved))
            if self.press_twice:
                self._queued.append((req.id, approved))
        self._next_message_id += 1
        return self._next_message_id

    def poll_decisions(self) -> list[tuple[str, bool]]:
        out, self._queued = self._queued, []
        return out

    def finalize(self, message_id: int | None, text: str) -> None:
        self.finalized.append((message_id, text))


def make_settings(tmp_path, engine_overrides: dict | None = None) -> Settings:
    path = tmp_path / "settings.yaml"
    path.write_text("engine:\n  poll_seconds: 0\n")
    return Settings(raw={"engine": {"poll_seconds": 0, "heartbeat_minutes": 999,
                                    **(engine_overrides or {})}}, path=path)


class _StopLoop(Exception):
    """run_paper_loop을 정확히 N 사이클만 돌리기 위한 탈출 신호."""


async def _drive(n: int, after_cycle=None, **kwargs) -> None:
    state = {"count": 0}

    async def fake_sleep(_seconds):
        state["count"] += 1
        if after_cycle is not None:
            after_cycle(state["count"])
        if state["count"] >= n:
            raise _StopLoop()

    with patch("quant.trade.loop.asyncio.sleep", fake_sleep):
        with pytest.raises(_StopLoop):
            await run_paper_loop(**kwargs)


def _entry() -> Signal:
    return Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.ENTER_LONG,
                  target_weight=1.0, reason="돌파", stop=95.0, target=110.0)


def _exit() -> Signal:
    return Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.EXIT_LONG,
                  target_weight=0.0, exit_fraction=1.0, reason="손절")


def _rig(tmp_path, positions=None, price: float = 100.0):
    data = FakeDataFeed(price=price)
    broker = FakeBroker(positions=positions)
    return {
        "ctx": Context(clock=FakeClock(), data=data, broker=broker),
        "data": data,
        "broker": broker,
        "risk": FakeRisk(),
        "sinks": FakeSink(),
        "notifier": FakeNotifier(),
        "control": TradingControl(state_path=tmp_path / "control.json"),
        "gate": ApprovalGate(state_path=tmp_path / "approvals.json"),
    }


# ------------------------------------------------- 불변식 1: 청산은 승인을 거치지 않는다

def test_exit_executes_immediately_without_approval(tmp_path):
    """가장 중요한 불변식. 손절이 승인 대기로 막히면 3배 레버리지 ETF에 갇힌다."""
    r = _rig(tmp_path, positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0)})
    bot = FakeApprovalBot()
    strat = FakeStrategy(signals_fn=lambda: [_exit()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG)

    assert bot.sent == []                      # 승인 요청 자체가 만들어지지 않는다
    assert r["gate"].pending() == []
    assert len(r["broker"].sells) == 1         # 그 자리에서 청산됐다
    assert r["broker"].positions()["TQQQ"].qty == 0.0


def test_scale_out_also_bypasses_approval(tmp_path):
    r = _rig(tmp_path, positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0)})
    bot = FakeApprovalBot()
    partial = Signal(strategy_id="s", symbol="TQQQ", action=SignalAction.SCALE_OUT,
                     target_weight=0.0, exit_fraction=0.5, reason="1R 부분익절")
    strat = FakeStrategy(signals_fn=lambda: [partial])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG)

    assert bot.sent == []
    assert len(r["broker"].sells) == 1
    assert r["broker"].positions()["TQQQ"].qty == 5.0


def test_kill_switch_flatten_executes_without_approval(tmp_path):
    r = _rig(tmp_path, positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0)})
    r["control"].request_flatten()
    bot = FakeApprovalBot()

    asyncio.run(_drive(
        1, strategies=[FakeStrategy(signals_fn=lambda: [_entry()])],
        ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"], settings=make_settings(tmp_path),
        notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert bot.sent == []
    assert r["broker"].positions()["TQQQ"].qty == 0.0
    assert any("청산 완료" in m for m in r["notifier"].messages)


# ------------------------------------------------- 진입: 승인 전/후

def test_entry_is_not_executed_until_approved(tmp_path):
    r = _rig(tmp_path)
    bot = FakeApprovalBot()  # 사용자가 아직 버튼을 누르지 않음
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG)

    assert r["broker"].orders == []            # 주문이 나가지 않았다
    assert len(bot.sent) == 1
    pending = r["gate"].pending()
    assert len(pending) == 1
    assert pending[0].symbol == "TQQQ"
    assert pending[0].message_id == 101         # 전송된 메시지 id가 기록됐다


def test_approved_entry_executes_on_next_cycle(tmp_path):
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_approve=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if r["broker"].orders == [] else [])

    asyncio.run(_drive(
        2, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert len(r["broker"].buys) == 1
    assert r["gate"].pending() == []
    # 승인 시점에 risk.approve를 다시 돌린다 — 낡은 Order를 재사용하지 않는다
    assert len(r["risk"].calls) >= 2
    assert any("승인됨" in c.reason for c in r["risk"].calls)


def test_rejected_entry_never_executes(tmp_path):
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_reject=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    asyncio.run(_drive(
        2, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert r["broker"].orders == []
    assert [req.status for req in r["gate"].all()] == ["rejected"]
    assert any("거절" in text for _, text in bot.finalized)


def test_repeated_signal_does_not_spam_new_requests(tmp_path):
    """전략은 조건이 유지되는 한 매 사이클 같은 신호를 낸다 — 요청은 1건이어야 한다.
    (2건이 생기면 사용자가 둘 다 수락해 두 번 사는 경로가 열린다.)"""
    r = _rig(tmp_path)
    bot = FakeApprovalBot()
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    asyncio.run(_drive(
        3, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert len(bot.sent) == 1
    assert len(r["gate"].pending()) == 1


def test_rejected_signal_is_not_asked_again_within_expiry(tmp_path):
    """방금 거절한 것을 10초 뒤에 또 묻지 않는다 — 그러면 승인 UI가 무의미해진다."""
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_reject=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    asyncio.run(_drive(
        4, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert len(bot.sent) == 1
    assert r["broker"].orders == []


# ------------------------------------------------- 불변식 2: 만료

def test_expired_request_is_not_executed_even_if_approved(tmp_path):
    """expire_seconds=0이면 다음 사이클에 즉시 만료된다 — 그 뒤 도착한 승인은 no-op."""
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_approve=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])
    cfg = {**_APPROVAL_CFG, "expire_seconds": 0}

    asyncio.run(_drive(
        2, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=cfg,
    ))

    assert r["broker"].orders == []
    assert [req.status for req in r["gate"].all()] == ["expired"]
    assert any("만료" in m for m in r["notifier"].messages)
    assert any("만료" in text for _, text in bot.finalized)


# ------------------------------------------------- 불변식 3: 멱등

def test_pressing_approve_twice_buys_once(tmp_path):
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_approve=True, press_twice=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    asyncio.run(_drive(
        3, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))

    assert len(r["broker"].buys) == 1
    assert [req.status for req in r["gate"].all()] == ["executed"]


# ------------------------------------------------- 불변식 4: fail-closed

def test_telegram_failure_blocks_entry_but_never_blocks_exit(tmp_path):
    """텔레그램이 죽어도 청산은 나가야 한다 — 진입만 막히는 게 안전한 방향이다."""
    r = _rig(tmp_path, positions={"TQQQ": Position(symbol="TQQQ", qty=10.0, avg_cost=90.0)})
    bot = FakeApprovalBot(send_ok=False)
    strat = FakeStrategy(signals_fn=lambda: [_entry(), _exit()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG)

    assert r["broker"].buys == []                      # 진입 없음
    assert len(r["broker"].sells) == 1                 # 청산은 그대로 실행
    assert r["gate"].pending() == []                   # 유령 pending을 남기지 않는다
    assert [req.status for req in r["gate"].all()] == ["expired"]
    assert any("전송 실패" in m for m in r["notifier"].messages)


def test_no_approval_notifier_configured_blocks_entry(tmp_path):
    """게이트만 켜고 봇을 안 붙인 오설정 — 조용히 진입하지 않고 막힌다(fail-closed)."""
    r = _rig(tmp_path)
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=None, approval_cfg=_APPROVAL_CFG)

    assert r["broker"].orders == []


def test_missing_quote_blocks_entry(tmp_path):
    """현재가를 못 읽으면 드리프트 재검증 기준이 없다 — 요청 자체를 만들지 않는다."""
    r = _rig(tmp_path)
    r["data"].price = None
    bot = FakeApprovalBot()
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"],
              approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG)

    assert bot.sent == []
    assert r["gate"].all() == []
    assert r["broker"].orders == []


# ------------------------------------------------- 불변식 5: 승인 시점 재검증

def test_price_drift_beyond_limit_cancels_execution(tmp_path):
    """사용자에게 보여준 가격에서 한도 이상 움직였으면 그건 다른 거래다."""
    r = _rig(tmp_path, price=100.0)
    bot = FakeApprovalBot(auto_approve=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    def move_price(cycle: int) -> None:
        if cycle == 1:
            r["data"].price = 101.0  # +1.0% > 한도 0.5%

    asyncio.run(_drive(
        2, after_cycle=move_price, strategies=[strat], ctx=r["ctx"], risk=r["risk"],
        sinks=r["sinks"], settings=make_settings(tmp_path), notifier=r["notifier"],
        control=r["control"], approval=r["gate"], approval_notifier=bot,
        approval_cfg=_APPROVAL_CFG,
    ))

    assert r["broker"].orders == []
    assert any("승인된 진입을 취소" in m and "변동" in m for m in r["notifier"].messages)


def test_small_price_move_within_limit_still_executes(tmp_path):
    r = _rig(tmp_path, price=100.0)
    bot = FakeApprovalBot(auto_approve=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    def move_price(cycle: int) -> None:
        if cycle == 1:
            r["data"].price = 100.2  # +0.2% < 한도 0.5%

    asyncio.run(_drive(
        2, after_cycle=move_price, strategies=[strat], ctx=r["ctx"], risk=r["risk"],
        sinks=r["sinks"], settings=make_settings(tmp_path), notifier=r["notifier"],
        control=r["control"], approval=r["gate"], approval_notifier=bot,
        approval_cfg=_APPROVAL_CFG,
    ))

    assert len(r["broker"].buys) == 1


def test_halt_between_request_and_approval_cancels_entry(tmp_path):
    """대기 중에 사용자가 거래를 중단시켰다면 승인분도 나가면 안 된다."""
    r = _rig(tmp_path)
    bot = FakeApprovalBot(auto_approve=True)
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    def halt_after_first(cycle: int) -> None:
        if cycle == 1:
            r["control"].halt("사용자 중단")

    asyncio.run(_drive(
        2, after_cycle=halt_after_first, strategies=[strat], ctx=r["ctx"], risk=r["risk"],
        sinks=r["sinks"], settings=make_settings(tmp_path), notifier=r["notifier"],
        control=r["control"], approval=r["gate"], approval_notifier=bot,
        approval_cfg=_APPROVAL_CFG,
    ))

    assert r["broker"].orders == []
    assert any("승인된 진입을 취소" in m for m in r["notifier"].messages)


# ------------------------------------------------- 영속화 / 회귀 가드 / 하트비트

def test_pending_request_survives_engine_restart(tmp_path):
    """엔진이 죽었다 살아나도 승인 대기가 유지되고, 그 뒤 승인하면 실행된다."""
    state_path = tmp_path / "approvals.json"
    r = _rig(tmp_path)
    r["gate"] = ApprovalGate(state_path=state_path)
    bot = FakeApprovalBot()
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    asyncio.run(_drive(
        1, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=r["gate"], approval_notifier=bot, approval_cfg=_APPROVAL_CFG,
    ))
    assert r["broker"].orders == []

    # --- 재시작: 같은 파일을 읽는 새 게이트 인스턴스 ---
    restarted = ApprovalGate(state_path=state_path)
    pending = restarted.pending()
    assert len(pending) == 1
    restarted.decide(pending[0].id, True)

    asyncio.run(_drive(
        1, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path), notifier=r["notifier"], control=r["control"],
        approval=restarted, approval_notifier=FakeApprovalBot(), approval_cfg=_APPROVAL_CFG,
    ))

    assert len(r["broker"].buys) == 1


def test_approval_none_keeps_previous_behaviour(tmp_path):
    """회귀 가드: 백테스트/기존 호출부는 승인 인자를 넘기지 않는다 — 즉시 진입해야 한다."""
    r = _rig(tmp_path)
    strat = FakeStrategy(signals_fn=lambda: [_entry()])

    run_cycle([strat], r["ctx"], r["risk"], r["sinks"], r["notifier"])

    assert len(r["broker"].buys) == 1
    assert len(r["sinks"].fills) == 1


def test_heartbeat_reports_pending_approvals(tmp_path):
    r = _rig(tmp_path)
    bot = FakeApprovalBot()
    strat = FakeStrategy(signals_fn=lambda: [_entry()] if not bot.sent else [])

    asyncio.run(_drive(
        2, strategies=[strat], ctx=r["ctx"], risk=r["risk"], sinks=r["sinks"],
        settings=make_settings(tmp_path, {"heartbeat_minutes": 0}), notifier=r["notifier"],
        control=r["control"], approval=r["gate"], approval_notifier=bot,
        approval_cfg=_APPROVAL_CFG,
    ))

    heartbeats = [m for m in r["notifier"].messages if "엔진 상태 점검" in m]
    assert heartbeats
    assert any("승인 대기: 1건" in m for m in heartbeats)
