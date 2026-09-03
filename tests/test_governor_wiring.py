"""거버너 배선(2026-08-28, ALLOWED 재정의·--live/--revert 2026-08-30) —
governor.py 는 완성돼 있었지만 부르는 프로덕션 코드가 없었다. 이 테스트는
배선 자체(오버레이 병합, 제안 원장 왕복, 스키마 대조, --live 게이트, dry-run,
--revert)를 고정한다. `tests/report/test_governor.py`(governor.py 자체의
6~7층 방어)는 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

import yaml

from quant.apps.config import _deep_merge, _read_merged, load_settings
from quant.apps.cli import _load_recent_governor_proposals, cmd_governor_apply
from quant.control import governor
from quant.control.ledger import base_strategy_id

TODAY = date(2026, 8, 30)

VOL_BREAKOUT_STOP = "strategies.vol_breakout.params.min_stop_bp"  # raise_only, 40~120


# --- ① 깊은 병합 --------------------------------------------------------

def test_deep_merge_overlay_wins_on_scalar():
    base = {"a": 1, "b": 2}
    overlay = {"b": 99}
    assert _deep_merge(base, overlay) == {"a": 1, "b": 99}


def test_deep_merge_without_overlay_keeps_base_untouched():
    base = {"a": {"x": 1}}
    assert _deep_merge(base, {}) == base


def test_deep_merge_merges_nested_dicts_instead_of_replacing():
    base = {"analyze": {"min_articles": 2, "news_hot": 3}}
    overlay = {"analyze": {"min_articles": 3}}
    merged = _deep_merge(base, overlay)
    assert merged == {"analyze": {"min_articles": 3, "news_hot": 3}}


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    _deep_merge(base, overlay)
    assert base == {"a": {"x": 1}}
    assert overlay == {"a": {"y": 2}}


def test_read_merged_without_overlay_file_is_unchanged(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("engine:\n  poll_seconds: 10\n", encoding="utf-8")
    assert _read_merged(settings_path) == {"engine": {"poll_seconds": 10}}


def test_read_merged_with_overlay_deep_merges(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("engine:\n  poll_seconds: 10\nanalyze:\n  min_articles: 2\n",
                              encoding="utf-8")
    (tmp_path / "auto_params.yaml").write_text("analyze:\n  min_articles: 3\n", encoding="utf-8")
    merged = _read_merged(settings_path)
    assert merged["analyze"]["min_articles"] == 3
    assert merged["engine"]["poll_seconds"] == 10  # 오버레이가 안 건드린 키는 그대로


# --- ② 두 파일의 mtime 감시 ----------------------------------------------

def test_reload_if_changed_watches_overlay_mtime_alone(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("analyze:\n  min_articles: 2\n", encoding="utf-8")
    settings = load_settings(str(settings_path))
    assert settings.raw["analyze"]["min_articles"] == 2

    # settings.yaml은 안 건드리고 오버레이만 새로 만든다.
    overlay_path = tmp_path / "auto_params.yaml"
    overlay_path.write_text("analyze:\n  min_articles: 3\n", encoding="utf-8")

    assert settings.reload_if_changed() is True
    assert settings.raw["analyze"]["min_articles"] == 3


def test_reload_if_changed_false_when_neither_file_touched(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("engine:\n  poll_seconds: 10\n", encoding="utf-8")
    settings = load_settings(str(settings_path))
    assert settings.reload_if_changed() is False


def test_reload_if_changed_still_watches_settings_mtime(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("engine:\n  poll_seconds: 10\n", encoding="utf-8")
    settings = load_settings(str(settings_path))
    settings_path.write_text("engine:\n  poll_seconds: 20\n", encoding="utf-8")
    assert settings.reload_if_changed() is True
    assert settings.raw["engine"]["poll_seconds"] == 20


def test_settings_without_any_overlay_behaves_exactly_as_before(tmp_path):
    """오버레이 파일이 아예 없는 배포(기존 동작)는 100% 그대로여야 한다."""
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text("strategies: {}\n", encoding="utf-8")
    settings = load_settings(str(settings_path))
    assert settings.raw == {"strategies": {}}
    assert settings.reload_if_changed() is False


# --- ③ 제안 JSONL ↔ Proposal 왕복 -----------------------------------------

def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_governor_schema_rows_round_trip_to_proposal(tmp_path):
    path = tmp_path / "param_proposals.jsonl"
    _write_jsonl(path, [{
        "date": "2026-08-27", "strategy": "donchian", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 60, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거", "llm": "claude-cli",
    }])
    props = _load_recent_governor_proposals(path, TODAY, window_days=7)
    assert len(props) == 1
    p = props[0]
    assert isinstance(p, governor.Proposal)
    assert (p.name, p.current, p.proposed, p.samples, p.expected_improvement) == (
        VOL_BREAKOUT_STOP, 40, 60, 50, 0.20)


def test_old_schema_rows_from_param_proposer_are_skipped_not_crashed(tmp_path):
    """cmd_param_propose(기존)가 쓰는 {strategy, param, risk, verify} 스키마에는
    name/samples/expected_improvement 가 없다 — 조용히 건너뛰어야지 에러가 나면
    안 된다."""
    path = tmp_path / "param_proposals.jsonl"
    _write_jsonl(path, [{
        "week": "2026-W35", "strategy": "donchian", "param": "trail_pct",
        "current": 1.0, "proposed": 1.2, "rationale": "r", "risk": "낮음",
        "verify": "2주 후 확인", "llm": "claude-cli", "recorded_at": "2026-08-27T00:00:00+00:00",
    }])
    assert _load_recent_governor_proposals(path, TODAY, window_days=7) == []


def test_proposals_outside_window_are_excluded(tmp_path):
    path = tmp_path / "param_proposals.jsonl"
    _write_jsonl(path, [{
        "date": "2026-08-01", "strategy": "donchian", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 60, "samples": 50, "expected_improvement": 0.20,
        "rationale": "너무 오래됨",
    }])
    assert _load_recent_governor_proposals(path, TODAY, window_days=7) == []


def test_missing_proposals_file_returns_empty_list(tmp_path):
    assert _load_recent_governor_proposals(tmp_path / "nope.jsonl", TODAY, window_days=7) == []


# --- ④ ALLOWED 경로가 실제 config/settings.yaml 에 실재 --------------------

def _resolve_path(raw: dict, name: str):
    node = raw
    for key in name.split("."):
        assert isinstance(node, dict) and key in node, (
            f"{name} 경로가 config/settings.yaml 에 없음(막힌 지점: {key!r})")
        node = node[key]
    return node


def test_allowed_paths_all_exist_in_real_settings_yaml():
    """2026-08-28 실측 결함 고정: 그때의 ALLOWED 7개는 하나도 config/settings.yaml
    에 없었다(analyze 평면의 파이썬 모듈 상수였다) — 제안은 나와도 반영될 곳이
    없어 거버너 전체가 죽은 코드였다. 2026-08-30 재정의 후 ALLOWED 의 이름
    자체가 config/settings.yaml 의 점(.) 표기 경로다. 이 테스트는 저장소의
    **실제** config/settings.yaml 을 읽어 그 불변식을 매 실행마다 강제한다 —
    누가 ALLOWED 에 있지도 않은 경로를 추가하면 여기서 바로 잡힌다."""
    from quant.adapters.env import REPO_ROOT

    settings = load_settings(str(REPO_ROOT / "config" / "settings.yaml"))
    assert governor.ALLOWED, "ALLOWED 가 비어 있다 — 재정의가 통째로 날아갔다"
    for name in governor.ALLOWED:
        node = _resolve_path(settings.raw, name)
        assert isinstance(node, (int, float)) and not isinstance(node, bool), (
            f"{name} 의 리프가 숫자가 아님: {node!r}")


def test_allowed_ordinal_paths_exist_and_current_value_is_a_member(tmp_path=None):
    """ALLOWED_ORDINAL(2026-09-02, 작업1 — trend_gate_mode 등 문자열 enum 파라미터)
    도 같은 불변식: 이름은 실제 settings.yaml 경로, 현재값은 그 항목이 나열한
    허용 상태 중 하나."""
    from quant.adapters.env import REPO_ROOT

    settings = load_settings(str(REPO_ROOT / "config" / "settings.yaml"))
    assert governor.ALLOWED_ORDINAL, "ALLOWED_ORDINAL 이 비어 있다"
    for name, (order, cooldown_days) in governor.ALLOWED_ORDINAL.items():
        node = _resolve_path(settings.raw, name)
        assert isinstance(node, str), f"{name} 의 리프가 문자열이 아님: {node!r}"
        assert node in order, f"{name} 의 현재값 {node!r} 이 허용 상태 {order} 에 없음"
        assert cooldown_days > 0


def test_allowed_kill_switch_paths_exist_and_are_boolean():
    """ALLOWED_KILL_SWITCH(작업2 — 사망 판정 전략 자동 비활성)의 이름도 실제
    settings.yaml 경로여야 하고, 리프는 enabled 플래그(bool)여야 한다. 보호
    목록(scalp_1m)은 여기 등재돼 있으면 안 된다 — 사람 지시로 자동 비활성
    대상에서 빠진 전략이 실수로 이 표에 들어가는 것을 여기서 잡는다."""
    from quant.adapters.env import REPO_ROOT

    settings = load_settings(str(REPO_ROOT / "config" / "settings.yaml"))
    protected = set((settings.raw.get("governor") or {}).get("protected_strategies") or [])
    assert governor.ALLOWED_KILL_SWITCH, "ALLOWED_KILL_SWITCH 가 비어 있다"
    for name, cooldown_days in governor.ALLOWED_KILL_SWITCH.items():
        node = _resolve_path(settings.raw, name)
        assert isinstance(node, bool), f"{name} 의 리프가 bool 이 아님: {node!r}"
        assert cooldown_days > 0
        sid = name.split(".")[1]
        assert sid not in protected, f"보호 전략 {sid} 이 ALLOWED_KILL_SWITCH 에 등재됨"
        # A/B 촉매 갈래(`<id>_cat`)는 기준 전략의 보호를 상속한다(2026-09-03) —
        # `scalp_1m` 이 보호 목록이면 `scalp_1m_cat` 도 이 표에 있으면 안 된다.
        assert base_strategy_id(sid) not in protected, (
            f"보호 전략의 A/B 갈래 {sid} 이 ALLOWED_KILL_SWITCH 에 등재됨"
        )


def test_protected_strategies_are_known_settings_yaml_strategies():
    from quant.adapters.env import REPO_ROOT

    settings = load_settings(str(REPO_ROOT / "config" / "settings.yaml"))
    protected = (settings.raw.get("governor") or {}).get("protected_strategies") or []
    assert protected, "governor.protected_strategies 가 비어 있다"
    for sid in protected:
        assert sid in (settings.raw.get("strategies") or {}), (
            f"보호 전략 {sid!r} 이 config/settings.yaml strategies 에 없음")


# --- ⑤ --live 없이는(기본값) 제안만, 오버레이는 안 쓴다 --------------------

def _governor_args(root, *, dry_run=False, live=False, window_days=7, revert=None):
    return argparse.Namespace(root=str(root), date=TODAY.isoformat(),
                              window_days=window_days, dry_run=dry_run, live=live,
                              revert=revert)


def test_proposal_within_allowed_range_is_recorded_but_not_written_without_live(tmp_path):
    """6~7층은 통과해도 --live 없이는(기본값) 오버레이에 안 쓴다 — decisions.jsonl
    에는 accepted=False, layer='not-live' 로 남는다. accepted=True 로 남기지
    않는 이유: governor.last_change() 가 그걸 "실제로 반영된 날"로 읽어 냉각
    (층3)의 기준으로 삼는다 — 미반영을 accepted=True 로 적으면 다음 실행에서
    근거 없는 냉각이 걸린다."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "vol_breakout", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 60, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root))

    overlay_path = root / "config" / "auto_params.yaml"
    assert not overlay_path.exists(), "--live 없이 오버레이가 반영돼 버렸다"

    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["accepted"] is False
    assert rows[0]["layer"] == "not-live"
    assert rows[0]["name"] == VOL_BREAKOUT_STOP


def test_proposal_within_allowed_range_is_applied_with_live(tmp_path):
    """--live 를 주면 실제로 config/auto_params.yaml 에 반영되고, _meta 에
    applied_at 이 남는다(사람이 --revert 로 되돌릴 때 근거)."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "vol_breakout", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 60, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root, live=True))

    overlay = yaml.safe_load(
        (root / "config" / "auto_params.yaml").read_text(encoding="utf-8"))
    assert overlay["strategies"]["vol_breakout"]["params"]["min_stop_bp"] == 60
    assert overlay["_meta"][VOL_BREAKOUT_STOP]["applied_at"] == TODAY.isoformat()

    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["accepted"] is True and rows[0]["applied"] == 60


def test_dry_run_writes_neither_overlay_nor_decisions_even_with_live(tmp_path):
    """--dry-run 은 --live 보다 항상 우선한다 — 심사만, 파일은 안 건드린다."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "vol_breakout", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 60, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root, dry_run=True, live=True))

    assert not (root / "config" / "auto_params.yaml").exists()
    assert not (root / "data" / "ledger" / "decisions.jsonl").exists()


def test_out_of_allowed_range_proposal_is_proposal_only_even_with_live(tmp_path):
    """봉투 밖(600bp)은 --live 여부와 무관하게 절대 반영되지 않는다 — governor
    층 1이 막는다. "범위 밖이면 전부 제안만"의 왕복 확인."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "vol_breakout", "name": VOL_BREAKOUT_STOP,
        "current": 40, "proposed": 600, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root, live=True))

    assert not (root / "config" / "auto_params.yaml").exists()
    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["accepted"] is False
    assert rows[0]["layer"] == "1-envelope"


def test_wrong_direction_proposal_is_proposal_only_even_with_live(tmp_path):
    """min_stop_bp 는 raise_only — 손절을 좁히는 제안은 --live 를 줘도 반영 안 됨."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "vol_breakout", "name": VOL_BREAKOUT_STOP,
        "current": 60, "proposed": 40, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root, live=True))

    assert not (root / "config" / "auto_params.yaml").exists()
    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["accepted"] is False
    assert rows[0]["layer"] == "0-direction"


# --- ⑥ --revert -------------------------------------------------------

def test_revert_removes_the_overlay_key_and_records_a_manual_decision(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True)
    (root / "config" / "auto_params.yaml").write_text(
        "strategies:\n  vol_breakout:\n    params:\n      min_stop_bp: 60\n"
        "_meta:\n  " + VOL_BREAKOUT_STOP + ":\n    applied_at: '2026-08-27'\n",
        encoding="utf-8",
    )

    cmd_governor_apply(_governor_args(root, revert=VOL_BREAKOUT_STOP))

    overlay = yaml.safe_load(
        (root / "config" / "auto_params.yaml").read_text(encoding="utf-8")) or {}
    assert "min_stop_bp" not in overlay.get("strategies", {}).get(
        "vol_breakout", {}).get("params", {})
    assert VOL_BREAKOUT_STOP not in overlay.get("_meta", {})

    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert rows[-1]["layer"] == "revert"
    assert rows[-1]["accepted"] is True
    assert rows[-1]["name"] == VOL_BREAKOUT_STOP


def test_revert_of_unknown_key_is_refused(tmp_path):
    """governor.ALLOWED 밖 이름은 --revert 로도 못 건드린다 — 거버너가 애초에
    권한 없던 키를 이 문으로 우회하게 두지 않는다."""
    root = tmp_path
    cmd_governor_apply(_governor_args(root, revert="strategies.made_up.params.x"))
    assert not (root / "data" / "ledger" / "decisions.jsonl").exists()


def test_revert_dry_run_does_not_touch_files(tmp_path):
    root = tmp_path
    (root / "config").mkdir(parents=True)
    (root / "config" / "auto_params.yaml").write_text(
        "strategies:\n  vol_breakout:\n    params:\n      min_stop_bp: 60\n",
        encoding="utf-8",
    )

    cmd_governor_apply(_governor_args(root, revert=VOL_BREAKOUT_STOP, dry_run=True))

    overlay = yaml.safe_load(
        (root / "config" / "auto_params.yaml").read_text(encoding="utf-8"))
    assert overlay["strategies"]["vol_breakout"]["params"]["min_stop_bp"] == 60
    assert not (root / "data" / "ledger" / "decisions.jsonl").exists()
