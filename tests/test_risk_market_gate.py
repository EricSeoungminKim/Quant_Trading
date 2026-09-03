"""장 마감 게이트(A-5) 테스트: risk.approve()는 시장이 닫혀 있으면 진입뿐 아니라
청산 신호도 막는다.

이건 회로차단기(circuit breaker)의 "청산은 절대 막지 않는다" 원칙(risk/manager.py
모듈 docstring, tests/test_risk_circuit_breakers.py)의 예외가 아니다 — 그 원칙은
"열려 있는 시장에서 리스크 레일이 손실 포지션을 가두지 않는다"는 뜻이고, 여기서
막는 것은 리스크 판단이 아니라 물리적 제약이다: 닫힌 시장에는 애초에 체결될
가격이 없다(2026-08-12 감사 A-5 — KR 종목을 EoD 청산 실패한 채 자정을 넘기면
세션 롤 판정이 시장이 닫힌 새벽에 EXIT_LONG을 내고, paper에서는 존재하지 않는
가격에 체결돼 스코어보드를 오염시키고, live면 시간외로 나가던 결함).
"""
from __future__ import annotations

from quant.core.ports import Context
from quant.core.models import Position
from quant.trade.risk.manager import MARKET_CLOSED_MARKER, RiskManagerImpl

from tests.test_risk_circuit_breakers import (
    _DEFAULT_NOW,
    _MARKET_OF,
    _SYMBOL,
    _FakeBroker,
    _FakeData,
    _entry,
    _exit,
    _risk_cfg,
)


def _risk() -> RiskManagerImpl:
    return RiskManagerImpl(_risk_cfg(), capital_fraction={"donchian": 1.0}, market_of=_MARKET_OF)


def _ctx_with_market(fake_clock_cls, *, market_open: bool, positions=None, cash: float = 10_000_000.0) -> Context:
    data = _FakeData(price=100.0)
    broker = _FakeBroker(cash, positions)
    clock = fake_clock_cls(now=_DEFAULT_NOW, market_open=market_open)
    return Context(clock=clock, data=data, broker=broker)


def test_closed_market_blocks_new_entry(fake_clock_cls):
    risk = _risk()
    ctx = _ctx_with_market(fake_clock_cls, market_open=False)

    order = risk.approve(_entry(), ctx)

    assert order is None
    assert MARKET_CLOSED_MARKER in risk.last_block


def test_closed_market_blocks_exit_too(fake_clock_cls):
    """청산도 존재하지 않는 가격에는 체결될 수 없다 — "청산은 절대 막지 않는다"의
    예외가 아니라 그 원칙이 전제하는 조건(열린 시장) 자체가 없는 경우다."""
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=5.0, avg_cost=90.0)}
    risk = _risk()
    ctx = _ctx_with_market(fake_clock_cls, market_open=False, positions=positions)

    order = risk.approve(_exit(), ctx)

    assert order is None
    assert MARKET_CLOSED_MARKER in risk.last_block


def test_open_market_allows_both_entry_and_exit(fake_clock_cls):
    positions = {_SYMBOL: Position(symbol=_SYMBOL, qty=5.0, avg_cost=90.0)}
    risk = _risk()
    ctx = _ctx_with_market(fake_clock_cls, market_open=True, positions=positions)

    entry_order = risk.approve(_entry(), ctx)
    exit_order = risk.approve(_exit(), ctx)

    assert entry_order is not None
    assert exit_order is not None


def test_gate_is_skipped_when_clock_has_no_is_market_open(fake_clock_cls):
    """구형/미완성 Clock 페이크(is_market_open이 없는 것)의 기존 계약을 보존한다 —
    getattr 방어로 그런 clock에서는 이 게이트를 건너뛴다."""

    class _NoGateClock:
        def now(self):
            return _DEFAULT_NOW
        # is_market_open 없음 — 의도적으로 흉내내는 구형 페이크

    risk = _risk()
    data = _FakeData(price=100.0)
    ctx = Context(clock=_NoGateClock(), data=data, broker=_FakeBroker(10_000_000.0))

    order = risk.approve(_entry(), ctx)

    assert order is not None


# ── 확장 세션 허용 목록 (2026-08-18, scalp_1m 프리마켓) ──────────────────

def _risk_with_extended() -> RiskManagerImpl:
    cfg = _risk_cfg()
    cfg["risk"]["extended_sessions"] = {"scalp_1m": {"KR": ["08:00-08:50"]}}
    return RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0, "scalp_1m": 1.0},
                           market_of={**_MARKET_OF, "005930": "KR"})


def _kst(h, m, day=2026_01_05):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    # 2026-01-05 = 월요일 (평일 게이트 통과용)
    return datetime(2026, 1, 5, h, m, tzinfo=ZoneInfo("Asia/Seoul"))


def _premarket_signal(strategy_id="scalp_1m"):
    from quant.core.models import Signal, SignalAction
    return Signal(strategy_id=strategy_id, symbol="005930",
                  action=SignalAction.ENTER_LONG, target_weight=0.5)


def test_extended_session_allows_listed_strategy_in_window(fake_clock_cls):
    """허용 목록의 (scalp_1m, KR, 08:00~08:50) 안에서는 정규장 밖이어도 승인 —
    KR 프리마켓(NXT)은 체결이 실재하는 세션이다(Toss 1m 봉 실측)."""
    risk = _risk_with_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    order = risk.approve(_premarket_signal(), ctx)
    assert order is not None, risk.last_block


def test_extended_session_blocks_unlisted_strategy(fake_clock_cls):
    """목록에 없는 전략(donchian 등 9종)은 같은 시각에도 기존 게이트 그대로 차단 —
    허용은 명시된 조합만이다."""
    risk = _risk_with_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    order = risk.approve(_premarket_signal(strategy_id="donchian"), ctx)
    assert order is None
    assert MARKET_CLOSED_MARKER in risk.last_block


def test_extended_session_blocks_outside_window_and_weekend(fake_clock_cls):
    """창 밖(08:50 정각 — 반개구간)과 주말은 목록에 있어도 차단."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    risk = _risk_with_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)

    clock = fake_clock_cls(now=_kst(8, 50), market_open=False)  # 08:50 = blackout
    ctx = Context(clock=clock, data=data, broker=broker)
    assert risk.approve(_premarket_signal(), ctx) is None

    sunday = datetime(2026, 1, 4, 8, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    clock = fake_clock_cls(now=sunday, market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)
    assert risk.approve(_premarket_signal(), ctx) is None


def test_extended_session_absent_config_is_pre_change_behavior(fake_clock_cls):
    """extended_sessions 키가 없으면 판정은 이 기능 도입 전과 완전히 동일하다."""
    risk = _risk()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    assert risk.approve(_premarket_signal(), ctx) is None


# ── US 확장 세션 — 시장별 현지 시간대 판정 (DST 안전, 2026-08-18) ─────────

def _risk_with_us_extended() -> RiskManagerImpl:
    cfg = _risk_cfg()
    cfg["risk"]["extended_sessions"] = {"scalp_1m": {"US": ["08:00-09:25"]}}
    return RiskManagerImpl(cfg, capital_fraction={"donchian": 1.0, "scalp_1m": 1.0},
                           market_of={**_MARKET_OF, "SOXL": "US"})


def _us_premarket_signal(strategy_id="scalp_1m"):
    from quant.core.models import Signal, SignalAction
    return Signal(strategy_id=strategy_id, symbol="SOXL",
                  action=SignalAction.ENTER_LONG, target_weight=0.5)


def test_extended_session_us_premarket_allows_within_et_window(fake_clock_cls):
    """US 프리마켓 창(ET 08:00~09:25)은 **ET 현지 시각**으로 판정돼야 한다 —
    Toss 1m 봉 실측(SOXL, 2026-08-18)으로 그 구간에 실거래가 있음을 확인했다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    risk = _risk_with_us_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    # 2026-01-05(월) 08:30 ET = EST(UTC-5) — 겨울 기준 창 안.
    now = datetime(2026, 1, 5, 8, 30, tzinfo=ZoneInfo("America/New_York"))
    clock = fake_clock_cls(now=now, market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    order = risk.approve(_us_premarket_signal(), ctx)
    assert order is not None, risk.last_block


def test_extended_session_us_premarket_dst_safe_across_edt_and_est(fake_clock_cls):
    """DST 회귀 고정: 같은 ET 벽시계 시각(08:30)이 EDT(여름, UTC-4)와 EST(겨울,
    UTC-5) 양쪽에서 동일하게 창 안으로 판정돼야 한다. `now`를 **UTC로 구성**해
    실제 UTC 오프셋 차이(-4 vs -5)를 명시적으로 통과시킨다 — 시장 로컬 tz 대신
    KST로 고정 판정했다면(과거 버그 모양) 두 계절의 결과가 서로 어긋났을
    것이다(KST는 DST가 없어 오프셋이 항상 +9로 고정이므로)."""
    from datetime import datetime, timezone

    risk = _risk_with_us_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)

    # EDT: 2026-07-06(월) 08:30 America/New_York = 12:30 UTC.
    edt_now = datetime(2026, 7, 6, 12, 30, tzinfo=timezone.utc)
    # EST: 2026-01-05(월) 08:30 America/New_York = 13:30 UTC.
    est_now = datetime(2026, 1, 5, 13, 30, tzinfo=timezone.utc)

    for now in (edt_now, est_now):
        clock = fake_clock_cls(now=now, market_open=False)
        ctx = Context(clock=clock, data=data, broker=broker)
        order = risk.approve(_us_premarket_signal(), ctx)
        assert order is not None, f"{now.isoformat()}: {risk.last_block}"


def test_extended_session_us_premarket_blocks_outside_et_window(fake_clock_cls):
    """ET 09:25(블랙아웃 시작 — 반개구간)은 창 밖. KST로 고정 판정했다면 이
    시각이 잘못된 KST 창(예: 22:25 KST 부근)과 겹쳐 통과했을 수 있다."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    risk = _risk_with_us_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    now = datetime(2026, 1, 5, 9, 25, tzinfo=ZoneInfo("America/New_York"))
    clock = fake_clock_cls(now=now, market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    assert risk.approve(_us_premarket_signal(), ctx) is None
    assert MARKET_CLOSED_MARKER in risk.last_block


def test_extended_session_kr_unaffected_by_market_tz_table(fake_clock_cls):
    """`_MARKET_TZ` 도입 후에도 KR(무 DST) 판정은 기존과 100% 동일해야 한다 —
    KST=현지 시각이므로 회귀가 없어야 한다."""
    risk = _risk_with_extended()
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    order = risk.approve(_premarket_signal(), ctx)
    assert order is not None, risk.last_block


# ── A/B 갈래는 기준 전략의 확장 세션 허가를 상속한다 (2026-09-03) ────────────

def test_extended_session_is_inherited_by_the_catalyst_arm(fake_clock_cls):
    """`scalp_1m_cat`(A/B 촉매 갈래)은 `scalp_1m`의 프리마켓 창을 그대로 받는다.

    두 갈래는 **같은 클래스**를 다른 유니버스로 돌리며, 재려는 것은 유니버스
    효과 하나다. 확장 세션 허가가 한쪽에만 있으면 갈래마다 진입 가능 시간대가
    달라져 A/B 가 오염된다 — `risk.extended_sessions`는 갈래마다 다시 선언하는
    값이 아니라 클래스 속성이다(manager.py `_base_strategy_id` 주석).
    """
    risk = _risk_with_extended()   # extended_sessions 에는 scalp_1m 만 있다
    risk.capital_fraction["scalp_1m_cat"] = 1.0
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    base = risk.approve(_premarket_signal("scalp_1m"), ctx)
    arm = risk.approve(_premarket_signal("scalp_1m_cat"), ctx)

    assert base is not None, risk.last_block
    assert arm is not None, f"촉매 갈래가 기준 전략의 프리마켓 허가를 잃었다: {risk.last_block}"


def test_pure_arm_also_inherits_but_unrelated_strategies_still_blocked(fake_clock_cls):
    """`_pure` 갈래도 같은 규칙. 반대로 접미사가 없는 남의 전략은 여전히 차단 —
    상속은 접미사를 벗겼을 때 **정확히** 기준 id 인 경우에만 일어난다."""
    risk = _risk_with_extended()
    risk.capital_fraction.update({"scalp_1m_pure": 1.0, "scalp_1m_other": 1.0})
    data = _FakeData(price=100.0)
    broker = _FakeBroker(10_000_000.0, None)
    clock = fake_clock_cls(now=_kst(8, 30), market_open=False)
    ctx = Context(clock=clock, data=data, broker=broker)

    assert risk.approve(_premarket_signal("scalp_1m_pure"), ctx) is not None, risk.last_block
    assert risk.approve(_premarket_signal("scalp_1m_other"), ctx) is None
    assert MARKET_CLOSED_MARKER in risk.last_block


def test_arm_inherits_bar_interval_and_cooldown_bars_from_the_base_strategy():
    """봉 간격·쿨다운 봉 수도 클래스 속성이다 — 갈래가 자기 params 를 따로 쓰지
    않았으면(YAML 앵커 미사용) 기준 전략 값을 물려받는다. 반대로 갈래가 스스로
    선언했으면 그 값이 이긴다."""
    settings = _risk_cfg()
    settings["risk"].update(cooldown_bars_after_stop=4, cooldown_bar_interval_minutes=15)
    settings["strategies"] = {
        "scalp_1m": {"params": {"bar_interval_minutes": 1, "cooldown_bars_after_stop": 15}},
        # 갈래는 params 를 아예 안 썼다 → 기준 전략 상속
        "scalp_1m_cat": {},
        # 갈래가 스스로 선언 → 자기 값이 이긴다
        "gap_fade": {"params": {"bar_interval_minutes": 5}},
        "gap_fade_cat": {"params": {"bar_interval_minutes": 5, "cooldown_bars_after_stop": 2}},
    }
    risk = RiskManagerImpl(settings, capital_fraction={}, market_of=_MARKET_OF)

    assert risk._bar_minutes_for("scalp_1m_cat") == 1         # 상속
    assert risk._cooldown_bars_for("scalp_1m_cat") == 15      # 상속
    assert risk._bar_minutes_for("gap_fade_cat") == 5
    assert risk._cooldown_bars_for("gap_fade_cat") == 2       # 자기 선언 우선
    assert risk._cooldown_bars_for("gap_fade") == 4           # 전역값
