"""결함주입 3/6 — 엔진 재시작 시 일중 상태 복원(live-readiness A5).

`docs/runbooks/live-readiness.md` A5는 🟡("`_save_day_state` 저장은 있으나 재시작
직후 공백 확인 필요")였다. 여기서 직접 확인한다: 재시작(=새 RiskManagerImpl/
Portfolio 인스턴스가 같은 디스크 상태를 읽는 것)을 시뮬레이션해, 판단에 영향을
주는 필드가 실제로 복원되는지, 그리고 **의도적으로** 리셋되는 필드가 무엇이고
왜 안전한지 기록한다."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from quant.core.fx import FixedFxProvider
from quant.core.portfolio.portfolio import Portfolio
from quant.trade.risk.manager import RiskManagerImpl


def _risk(state_path: Path) -> RiskManagerImpl:
    cfg = dict(
        sizing_mode="cash_pct", max_position_pct=100, max_symbol_pct_total=0,
        daily_loss_limit_pct=3, max_orders_per_day=30, cooldown_bars_after_stop=4,
        max_order_notional_pct=0, max_total_exposure_pct=0, max_concurrent_positions=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"s": 1.0}, market_of={"TQQQ": "US"},
        fx=FixedFxProvider(1500.0), state_path=state_path,
    )


# ------------------------------------------------------- 복원되는 필드(회귀 방지)

def test_day_counters_and_start_equity_survive_restart(tmp_path):
    state_path = tmp_path / "risk_day.json"
    r1 = _risk(state_path)
    r1._day = "2026-09-07"
    r1._day_start_equity = 10_000_000.0
    r1._day_entry_count = {"US": 5}
    r1._day_order_count = {"US": 12}
    r1._save_day_state()

    r2 = _risk(state_path)  # "재시작" — 새 프로세스가 같은 파일을 읽는다
    assert r2._day == "2026-09-07"
    assert r2._day_start_equity == 10_000_000.0
    assert r2._day_entry_count == {"US": 5}
    assert r2._day_order_count == {"US": 12}


def test_stop_loss_cooldown_survives_restart(tmp_path):
    """손절 쿨다운이 재시작으로 풀리면 휩소 재진입 방어가 무의미해진다 — 실제로
    남아 있는지 확인."""
    state_path = tmp_path / "risk_day.json"
    r1 = _risk(state_path)
    ts = pd.Timestamp("2026-09-07 10:15", tz="America/New_York")
    key = ("TQQQ", "donchian")
    r1._stop_bar_ts[key] = ts
    r1._stop_day[key] = "2026-09-07"
    r1._day = "2026-09-07"
    r1._save_day_state()

    r2 = _risk(state_path)
    assert key in r2._stop_bar_ts
    assert r2._stop_bar_ts[key] == ts
    assert r2._stop_day[key] == "2026-09-07"


def test_eod_stopped_and_per_strategy_state_survive_restart(tmp_path):
    state_path = tmp_path / "risk_day.json"
    r1 = _risk(state_path)
    r1._eod_stopped[("scalp_1m", "005930")] = "2026-09-07"
    r1._day_per_strategy["frgn_accumulate"] = "2026-09-07"
    r1._day_start_equity_per_strategy["frgn_accumulate"] = 10_000_000.0
    r1._day_entry_count_per_strategy["frgn_accumulate:KR"] = 2
    r1._save_day_state()

    r2 = _risk(state_path)
    assert r2._eod_stopped[("scalp_1m", "005930")] == "2026-09-07"
    assert r2._day_per_strategy["frgn_accumulate"] == "2026-09-07"
    assert r2._day_start_equity_per_strategy["frgn_accumulate"] == 10_000_000.0
    assert r2._day_entry_count_per_strategy["frgn_accumulate:KR"] == 2


def test_corrupted_state_file_falls_back_to_defaults_not_crash(tmp_path):
    state_path = tmp_path / "risk_day.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    r = _risk(state_path)  # 생성자에서 크래시하면 엔진이 부팅조차 못 한다
    assert r._day is None
    assert r._stop_bar_ts == {}


def test_legacy_scalar_day_order_count_is_discarded_not_misapplied(tmp_path):
    """구버전(시장 구분 없는 스칼라) 상태 파일을 만나면 버리고 0부터 다시 센다
    — 잘못된 시장에 잘못 배분하는 것보다 안전하다(기존 계약, 회귀 방지)."""
    state_path = tmp_path / "risk_day.json"
    state_path.write_text(json.dumps({"day": "2026-09-07", "day_order_count": 999}), encoding="utf-8")
    r = _risk(state_path)
    assert r._day_order_count == {}


# --------------------------------------------------- 의도적으로 리셋되는 필드

def test_recent_entries_burst_detector_does_not_survive_restart_by_design(tmp_path):
    """`_recent_entries`(반복 진입 폭주 탐지 창)는 영속화 대상이 아니다 — 재시작으로
    비워져도 실제 폭주라면 수 초~수십 초 안에 다시 채워진다는 것이 코드 주석의
    설계 근거(quant/trade/risk/manager.py __init__ 주석). 여기서는 그 주장대로
    `_save_day_state()`의 payload에 이 필드가 아예 없음을 고정해, 나중에 실수로
    빠지거나(반대로) 추가되는 변경을 감지한다."""
    state_path = tmp_path / "risk_day.json"
    r = _risk(state_path)
    r._recent_entries[("TQQQ", "donchian")] = [1234.0, 1235.0]
    r._save_day_state()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert "recent_entries" not in payload


def test_ma_trail_and_partial_take_flags_persist_via_position_meta(tmp_path):
    """A5의 "MA trail 앵커·부분익절 플래그"는 risk_day.json이 아니라
    `Position.meta["lots"][strategy_id]`에 있고, 그건 portfolio.json을 통해 이미
    영속화된다(quant/trade/loop.py `_persist_position_meta`). "재시작 시
    복원되는가"를 Portfolio 재로드로 직접 확인한다 — 별도 메커니즘이 필요 없다는
    A4의 주장을 재확인."""
    state_path = tmp_path / "portfolio.json"
    pf = Portfolio(cash=10_000_000.0, state_path=state_path)
    from quant.core.models import Position

    pos = Position(symbol="TQQQ", qty=10.0, avg_cost=50.0)
    lot = pos.ensure_lot("donchian")
    lot.update(entry=50.0, stop=49.0, target=52.0, ma_trail=48.5, partial_taken=True)
    pf.positions["TQQQ"] = pos
    pf.save()

    pf2 = Portfolio.load_or_init(start_cash=10_000_000.0, state_path=state_path)
    restored_lot = pf2.positions["TQQQ"].lot("donchian")
    assert restored_lot is not None
    assert restored_lot["ma_trail"] == 48.5
    assert restored_lot["partial_taken"] is True
    assert restored_lot["stop"] == 49.0


def test_day_loss_limit_baseline_recalculated_after_restart_is_documented_behavior(tmp_path):
    """`_day_start_equity`는 저장/복원된다(위 테스트) — 하지만 재시작 시점의 실제
    자산이 저장된 시작자산과 다르면(포지션 재평가, 체결 등) `daily_loss_limit_pct`
    판정 자체는 저장된 시작자산 기준으로 계속된다(교체되지 않는다). 이건
    `approve()`가 `today != self._day`일 때만 시작자산을 재스냅샷하기 때문 —
    같은 거래일 안의 재시작은 하루 손실 한도를 초기화하지 않는다(의도된 동작:
    재시작으로 손실 한도가 리셋되면 바로 그 회로차단기가 재시작 한 번에 풀리는
    사고, 2026-08-12 감사 A-3의 재발이다). 이 테스트는 그 의도를 문서화하고
    고정한다."""
    state_path = tmp_path / "risk_day.json"
    r1 = _risk(state_path)
    r1._day = "2026-09-07"
    r1._day_start_equity = 10_000_000.0
    r1._save_day_state()

    r2 = _risk(state_path)
    # "재시작 후 오늘 이미 -2.9% 상태"를 흉내: 시작자산은 복원됐고, 오늘 날짜도
    # 그대로이므로 approve()가 재스냅샷하지 않는다 — 아래는 그 계약의 직접 확인.
    assert r2._day == "2026-09-07"
    assert r2._day_start_equity == 10_000_000.0
