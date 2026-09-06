"""`quant.analyze.regime_state.load_regime_for_market` 유닛 테스트.

세 곳(`tg_digest_section._load_regime_for_report`, `report/collect/core.py::
_load_regime_for_stance`, `apps/cli.py::_tg_digest_load_regime`)에 복제돼
있던 로직을 통합한 함수(2026-09-07) — 그 세 복제본이 공유하던 계약을 그대로
검증한다: markets[market] sub-dict, US는 markets 없으면 최상위 payload로
하위호환, 실패는 예외가 아니라 None.
"""
from __future__ import annotations

import json

from quant.analyze.regime_state import load_regime_for_market


def test_missing_file_returns_none(tmp_path):
    assert load_regime_for_market(tmp_path, "KR") is None


def test_corrupt_json_returns_none(tmp_path):
    path = tmp_path / "data" / "state" / "regime.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    assert load_regime_for_market(tmp_path, "KR") is None


def test_reads_market_subdict_current_schema(tmp_path):
    path = tmp_path / "data" / "state" / "regime.json"
    path.parent.mkdir(parents=True)
    payload = {
        "markets": {
            "KR": {"label": "aggressive", "risk_multiplier": 1.5, "reasons": ["r1"]},
            "US": {"label": "defensive", "risk_multiplier": 0.5, "reasons": ["r2"]},
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_regime_for_market(tmp_path, "KR") == {
        "label": "aggressive", "risk_multiplier": 1.5, "reasons": ["r1"],
    }
    assert load_regime_for_market(tmp_path, "US") == {
        "label": "defensive", "risk_multiplier": 0.5, "reasons": ["r2"],
    }


def test_us_falls_back_to_top_level_legacy_schema(tmp_path):
    path = tmp_path / "data" / "state" / "regime.json"
    path.parent.mkdir(parents=True)
    payload = {"label": "neutral", "risk_multiplier": 1.0, "reasons": []}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_regime_for_market(tmp_path, "US") == payload


def test_kr_does_not_fall_back_to_top_level_legacy_schema(tmp_path):
    path = tmp_path / "data" / "state" / "regime.json"
    path.parent.mkdir(parents=True)
    payload = {"label": "neutral", "risk_multiplier": 1.0, "reasons": []}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_regime_for_market(tmp_path, "KR") is None


def test_missing_market_key_returns_none(tmp_path):
    path = tmp_path / "data" / "state" / "regime.json"
    path.parent.mkdir(parents=True)
    payload = {"markets": {"KR": {"label": "neutral", "risk_multiplier": 1.0, "reasons": []}}}
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_regime_for_market(tmp_path, "US") is None
