"""거버너 배선(2026-08-28) — governor.py 는 완성돼 있었지만 부르는 프로덕션
코드가 없었다. 이 테스트는 배선 자체(오버레이 병합, 제안 원장 왕복, 매핑 없는
이름 거부, dry-run)를 고정한다. `tests/report/test_governor.py`(governor.py
자체의 6층 방어)는 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import json
from datetime import date

import yaml

from quant.apps.config import _deep_merge, _read_merged, load_settings
from quant.apps.cli import (
    GOVERNOR_SETTINGS_PATH,
    _load_recent_governor_proposals,
    cmd_governor_apply,
)
from quant.control import governor

TODAY = date(2026, 8, 28)


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
        "date": "2026-08-27", "strategy": "donchian", "name": "min_articles",
        "current": 2, "proposed": 3, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거", "llm": "claude-cli",
    }])
    props = _load_recent_governor_proposals(path, TODAY, window_days=7)
    assert len(props) == 1
    p = props[0]
    assert isinstance(p, governor.Proposal)
    assert (p.name, p.current, p.proposed, p.samples, p.expected_improvement) == (
        "min_articles", 2, 3, 50, 0.20)


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
        "date": "2026-08-01", "strategy": "donchian", "name": "min_articles",
        "current": 2, "proposed": 3, "samples": 50, "expected_improvement": 0.20,
        "rationale": "너무 오래됨",
    }])
    assert _load_recent_governor_proposals(path, TODAY, window_days=7) == []


def test_missing_proposals_file_returns_empty_list(tmp_path):
    assert _load_recent_governor_proposals(tmp_path / "nope.jsonl", TODAY, window_days=7) == []


# --- ④ 매핑 없는 이름은 거부 ----------------------------------------------

def _governor_args(root, *, dry_run=False, window_days=7):
    return argparse.Namespace(root=str(root), date=TODAY.isoformat(),
                              window_days=window_days, dry_run=dry_run)


def test_current_allowed_names_have_no_settings_mapping_yet():
    """전수 확인 사실 고정: ALLOWED 의 어떤 이름도 지금 settings.yaml 경로가
    없다(analyze 평면 모듈 상수라서). 이 상태가 바뀌면(=매핑이 채워지면) 이
    테스트가 깨져서 알려준다 — 그때 이 테스트를 지우고 실제 매핑 테스트로
    바꾸면 된다."""
    assert GOVERNOR_SETTINGS_PATH == {}


def test_accepted_proposal_without_mapping_is_rejected_and_recorded(tmp_path):
    """governor 의 6층은 다 통과해도 GOVERNOR_SETTINGS_PATH 에 없으면 반영하지
    않고, 그 사실을 decisions.jsonl 에 accepted=False, layer='mapping' 으로
    남긴다 — 조용한 무시 금지."""
    root = tmp_path
    _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
        "date": "2026-08-27", "strategy": "donchian", "name": "min_articles",
        "current": 2, "proposed": 3, "samples": 50, "expected_improvement": 0.20,
        "rationale": "표본 근거",
    }])

    cmd_governor_apply(_governor_args(root))

    overlay_path = root / "config" / "auto_params.yaml"
    assert not overlay_path.exists(), "매핑 없는 제안이 오버레이에 반영돼 버렸다"

    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["accepted"] is False
    assert rows[0]["layer"] == "mapping"
    assert rows[0]["name"] == "min_articles"


# --- ⑤ dry-run 은 파일을 쓰지 않는다 --------------------------------------

def test_dry_run_writes_neither_overlay_nor_decisions(tmp_path, monkeypatch):
    monkeypatch.setitem(GOVERNOR_SETTINGS_PATH, "min_articles", ("test_section", "min_articles"))
    try:
        root = tmp_path
        _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
            "date": "2026-08-27", "strategy": "donchian", "name": "min_articles",
            "current": 2, "proposed": 3, "samples": 50, "expected_improvement": 0.20,
            "rationale": "표본 근거",
        }])

        cmd_governor_apply(_governor_args(root, dry_run=True))

        assert not (root / "config" / "auto_params.yaml").exists()
        assert not (root / "data" / "ledger" / "decisions.jsonl").exists()
    finally:
        GOVERNOR_SETTINGS_PATH.pop("min_articles", None)


def test_without_dry_run_the_same_proposal_is_actually_applied(tmp_path, monkeypatch):
    """dry-run 을 떼면(=기본값) 매핑이 있는 제안은 실제로 오버레이에 쓰인다 —
    ④/⑤가 서로 대칭임을 왕복으로 확인한다."""
    monkeypatch.setitem(GOVERNOR_SETTINGS_PATH, "min_articles", ("test_section", "min_articles"))
    try:
        root = tmp_path
        _write_jsonl(root / "data" / "ledger" / "param_proposals.jsonl", [{
            "date": "2026-08-27", "strategy": "donchian", "name": "min_articles",
            "current": 2, "proposed": 3, "samples": 50, "expected_improvement": 0.20,
            "rationale": "표본 근거",
        }])

        cmd_governor_apply(_governor_args(root, dry_run=False))

        overlay = yaml.safe_load(
            (root / "config" / "auto_params.yaml").read_text(encoding="utf-8"))
        assert overlay == {"test_section": {"min_articles": 3}}

        decisions_path = root / "data" / "ledger" / "decisions.jsonl"
        rows = [json.loads(x) for x in decisions_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["accepted"] is True and rows[0]["applied"] == 3
    finally:
        GOVERNOR_SETTINGS_PATH.pop("min_articles", None)
