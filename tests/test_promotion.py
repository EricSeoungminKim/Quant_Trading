"""백테스트 게이트 → 모의투자 승격(quant/control/promotion.py) 회귀 가드
(2026-09-03).

`check_promotable`이 막아야 하는 이유들(증거 부족·판정 미달·오래된 게이트·
이미 승격됨·자본 0)과, `render_promoted_settings`/`apply_promotion`이
settings.yaml의 **대상 전략 블록 필드만** 바꾸고 나머지는 바이트 단위로
그대로 두는지를 고정한다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from quant.control.promotion import (
    apply_promotion,
    check_promotable,
    load_gate,
    render_promoted_settings,
)

NOW = datetime(2026, 9, 3, 12, 0, 0)


def _gate(**overrides) -> dict:
    base = {
        "strategy": "donchian",
        "generated_at": "2026-09-01T09:00:00",
        "data_range": "2020-01-01~2024-06-01",
        "fill_model": "intrabar",
        "cost_assumptions": {"cost_bp": 12.0},
        "gate": {
            "verdict": "GO",
            "criteria": {
                "oos_n_trades": {"value": 120},
                "oos_expectancy": {"value": 3.21},
                "deflated_sharpe": {"value": 1.05},
            },
        },
    }
    base.update(overrides)
    return base


def _settings(**strategy_overrides) -> dict:
    strat = {
        "capital_fraction": {"KR": 0.0, "US": 0.1},
        "enabled": False,
        "validation": {"status": "burn_in"},
    }
    strat.update(strategy_overrides)
    return {"strategies": {"donchian": strat}}


# ---------------------------------------------------------------------------
# load_gate
# ---------------------------------------------------------------------------


def test_load_gate_reads_json_and_injects_gate_path(tmp_path: Path):
    import json

    p = tmp_path / "gate_donchian_20260901.json"
    p.write_text(json.dumps({"strategy": "donchian"}), encoding="utf-8")
    data = load_gate(p)
    assert data["strategy"] == "donchian"
    assert data["gate_path"] == str(p)


def test_load_gate_does_not_overwrite_existing_gate_path_key(tmp_path: Path):
    import json

    p = tmp_path / "g.json"
    p.write_text(json.dumps({"gate_path": "keep-me"}), encoding="utf-8")
    assert load_gate(p)["gate_path"] == "keep-me"


# ---------------------------------------------------------------------------
# check_promotable — 통과
# ---------------------------------------------------------------------------


def test_check_promotable_passes_with_full_evidence():
    assert check_promotable(_gate(), strategy_id="donchian", settings=_settings(), now=NOW) == []


def test_check_promotable_passes_when_already_disabled_but_status_backtest_pass():
    """burn_in→backtest_pass로 승격했다가 나중에 사람이 다시 껐다면(재발동 시나리오)
    재승격을 막지 않는다 — enabled=false 만으로도 조건 충족."""
    settings = _settings(enabled=False, validation={"status": "backtest_pass", "evidence": {}})
    assert check_promotable(_gate(), strategy_id="donchian", settings=settings, now=NOW) == []


# ---------------------------------------------------------------------------
# check_promotable — 차단 사유
# ---------------------------------------------------------------------------


def test_rejects_non_go_verdict():
    gate = _gate(gate={"verdict": "NO_GO", "criteria": {}})
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("GO가 아님" in r for r in reasons)


def test_rejects_mismatched_strategy():
    gate = _gate(strategy="orb_scan")
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("다름" in r for r in reasons)


def test_rejects_missing_data_range():
    gate = _gate(data_range=None)
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("data_range" in r for r in reasons)


def test_rejects_missing_fill_model():
    gate = _gate(fill_model=None)
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("fill_model" in r for r in reasons)


def test_rejects_non_intrabar_fill_model():
    gate = _gate(fill_model="close_only")
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("intrabar" in r for r in reasons)


def test_rejects_missing_cost_assumptions():
    gate = _gate(cost_assumptions=None)
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("cost_assumptions" in r for r in reasons)


def test_rejects_stale_gate():
    gate = _gate(generated_at="2026-08-01T00:00:00")  # NOW보다 33일 전
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("일 전 것" in r for r in reasons)


def test_accepts_gate_exactly_at_the_age_boundary():
    gate = _gate(generated_at="2026-08-20T12:00:00")  # 정확히 14일 전
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert reasons == []


def test_rejects_unparsable_generated_at():
    gate = _gate(generated_at="어제")
    reasons = check_promotable(gate, strategy_id="donchian", settings=_settings(), now=NOW)
    assert any("형식을 읽을 수 없음" in r for r in reasons)


def test_rejects_strategy_missing_from_settings():
    reasons = check_promotable(_gate(), strategy_id="donchian", settings={"strategies": {}}, now=NOW)
    assert any("없음" in r for r in reasons)


def test_rejects_already_promoted():
    settings = _settings(enabled=True, validation={"status": "backtest_pass", "evidence": {}})
    reasons = check_promotable(_gate(), strategy_id="donchian", settings=settings, now=NOW)
    assert any("이미 승격됨" in r for r in reasons)


def test_rejects_zero_capital_fraction_in_both_markets():
    settings = _settings(capital_fraction={"KR": 0.0, "US": 0.0})
    reasons = check_promotable(_gate(), strategy_id="donchian", settings=settings, now=NOW)
    assert any("capital_fraction" in r for r in reasons)


def test_scalar_capital_fraction_above_zero_passes():
    settings = _settings(capital_fraction=0.1)
    assert check_promotable(_gate(), strategy_id="donchian", settings=settings, now=NOW) == []


def test_all_reasons_are_collected_not_just_the_first():
    gate = _gate(fill_model=None, data_range=None, generated_at=None)
    reasons = check_promotable(gate, strategy_id="donchian", settings={"strategies": {}}, now=NOW)
    assert len(reasons) >= 4


# ---------------------------------------------------------------------------
# render_promoted_settings — 필드 반영
# ---------------------------------------------------------------------------

_MINIMAL_YAML = """\
engine:
  poll_seconds: 5

strategies:
  donchian:
    class: donchian
    enabled: false # 미검증 — 백테스트 전까지 비활성
    capital_fraction:
      KR: 0.0
      US: 0.3
    validation:
      status: burn_in # 신규 전략
    symbols: [TQQQ, SQQQ]

  orb:
    class: orb
    enabled: false
    capital_fraction: 1.0
    symbols: [TQQQ]

risk:
  max_daily_loss_pct: 3.0
"""


def test_render_sets_enabled_status_and_evidence():
    out = render_promoted_settings(_MINIMAL_YAML, "donchian", _gate(), promoted_at=NOW)
    parsed = yaml.safe_load(out)
    d = parsed["strategies"]["donchian"]
    assert d["enabled"] is True
    assert d["validation"]["status"] == "backtest_pass"
    ev = d["validation"]["evidence"]
    assert ev["verdict"] == "GO"
    assert ev["oos_trades"] == 120
    assert ev["expectancy_bp"] == 3.21
    assert ev["deflated_sharpe"] == 1.05
    assert ev["data_range"] == "2020-01-01~2024-06-01"
    assert ev["fill_model"] == "intrabar"
    assert ev["cost_bp"] == 12.0
    assert ev["promoted_at"] == "2026-09-03T12:00:00"


def test_render_drops_stale_inline_comment_on_enabled_line():
    """enabled 줄의 인라인 주석은 옛 비활성 사유를 설명하던 것이라 승격 후엔
    부정확해진다 — 지우는 대신 남기면 거짓말이 되므로 값 교체 시 같이 버린다."""
    out = render_promoted_settings(_MINIMAL_YAML, "donchian", _gate(), promoted_at=NOW)
    assert "미검증 — 백테스트 전까지 비활성" not in out


def test_render_preserves_capital_fraction_when_no_override():
    out = render_promoted_settings(_MINIMAL_YAML, "donchian", _gate(), promoted_at=NOW)
    parsed = yaml.safe_load(out)
    assert parsed["strategies"]["donchian"]["capital_fraction"] == {"KR": 0.0, "US": 0.3}


def test_render_overrides_capital_fraction_when_given():
    out = render_promoted_settings(
        _MINIMAL_YAML, "donchian", _gate(), capital_fraction={"KR": 0.05, "US": 0.05}, promoted_at=NOW,
    )
    parsed = yaml.safe_load(out)
    assert parsed["strategies"]["donchian"]["capital_fraction"] == {"KR": 0.05, "US": 0.05}


def test_render_inserts_validation_block_when_entirely_missing():
    text = """\
strategies:
  bare:
    class: bare
    enabled: false
    capital_fraction: 0.1
"""
    out = render_promoted_settings(text, "bare", _gate(strategy="bare"), promoted_at=NOW)
    parsed = yaml.safe_load(out)
    assert parsed["strategies"]["bare"]["validation"]["status"] == "backtest_pass"
    assert parsed["strategies"]["bare"]["enabled"] is True


def test_render_unknown_strategy_raises():
    with pytest.raises(ValueError):
        render_promoted_settings(_MINIMAL_YAML, "does_not_exist", _gate(), promoted_at=NOW)


def test_render_missing_strategies_section_raises():
    with pytest.raises(ValueError):
        render_promoted_settings("engine:\n  poll_seconds: 5\n", "donchian", _gate(), promoted_at=NOW)


# ---------------------------------------------------------------------------
# render_promoted_settings — 다른 블록은 바이트 단위로 그대로
# ---------------------------------------------------------------------------


def test_other_strategy_block_and_surrounding_sections_are_byte_identical():
    out = render_promoted_settings(_MINIMAL_YAML, "donchian", _gate(), promoted_at=NOW)

    def _between(text: str, start_marker: str, end_marker: str) -> str:
        s = text.index(start_marker)
        e = text.index(end_marker, s)
        return text[s:e]

    orig_orb = _between(_MINIMAL_YAML, "  orb:", "risk:")
    new_orb = _between(out, "  orb:", "risk:")
    assert orig_orb == new_orb

    orig_head = _MINIMAL_YAML[: _MINIMAL_YAML.index("strategies:")]
    new_head = out[: out.index("strategies:")]
    assert orig_head == new_head

    orig_tail = _MINIMAL_YAML[_MINIMAL_YAML.index("risk:") :]
    new_tail = out[out.index("risk:") :]
    assert orig_tail == new_tail


def test_real_settings_yaml_other_blocks_are_byte_identical_after_promotion():
    """실제 config/settings.yaml 회귀 가드 — orb_scan은 validation에 evidence가
    없고 status에 인라인 주석이 있고 capital_fraction이 여러 줄인 실전 케이스다.
    승격이 이 블록만 건드리고 나머지(mean_reversion 이후 전체 + 파일 앞부분)는
    그대로인지 확인한다."""
    real_path = Path("config/settings.yaml")
    original = real_path.read_text(encoding="utf-8")
    gate = _gate(strategy="orb_scan", generated_at=NOW.isoformat())

    out = render_promoted_settings(original, "orb_scan", gate, promoted_at=NOW)

    # yaml로 파싱 가능해야 한다(문법을 깨지 않았는지).
    parsed = yaml.safe_load(out)
    assert parsed["strategies"]["orb_scan"]["enabled"] is True
    assert parsed["strategies"]["orb_scan"]["validation"]["status"] == "backtest_pass"

    before_block = original[: original.index("  orb_scan:")]
    after_block = original[original.index("  mean_reversion:") :]
    new_before = out[: out.index("  orb_scan:")]
    new_after = out[out.index("  mean_reversion:") :]
    assert before_block == new_before
    assert after_block == new_after


# ---------------------------------------------------------------------------
# apply_promotion — 유일한 파일 쓰기 지점
# ---------------------------------------------------------------------------


def test_apply_promotion_writes_the_file(tmp_path: Path):
    p = tmp_path / "settings.yaml"
    p.write_text(_MINIMAL_YAML, encoding="utf-8")
    apply_promotion(p, "donchian", _gate(), promoted_at=NOW)
    parsed = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert parsed["strategies"]["donchian"]["enabled"] is True
    assert parsed["strategies"]["donchian"]["validation"]["status"] == "backtest_pass"
    # 손대지 않은 전략은 그대로.
    assert parsed["strategies"]["orb"]["enabled"] is False
    assert parsed["strategies"]["orb"]["capital_fraction"] == 1.0
