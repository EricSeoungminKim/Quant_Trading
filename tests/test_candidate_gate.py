"""candidate_gate 순수 함수 테스트 (SUMMARY.md §② 오류 수정 — 방어 국면에서도
후보 리스트 크기가 줄지 않던 문제)."""
from __future__ import annotations

from quant.analyze import candidate_gate as cg


def test_is_defensive_stance_true_on_regime_defensive():
    assert cg.is_defensive_stance("defensive", None) is True


def test_is_defensive_stance_true_on_strong_bear_diagnostic():
    assert cg.is_defensive_stance(None, 20) is True
    assert cg.is_defensive_stance("neutral", 25) is True  # 경계값 포함


def test_is_defensive_stance_false_otherwise():
    assert cg.is_defensive_stance("neutral", 40) is False
    assert cg.is_defensive_stance("aggressive", 10) is True  # 진단이 강한 하락이면 그것만으로 충분
    assert cg.is_defensive_stance(None, None) is False


def _symbols(*rows):
    return [dict(r) for r in rows]


def test_gate_passthrough_when_not_defensive():
    auto_watch = "AUTO_WATCH: A:NEWS B:NEWS C:NEWS"
    out = cg.gate_candidates(auto_watch, [], regime_label="neutral", diagnostic_score100=50)
    assert out == {"auto_watch": auto_watch, "watch_only": [], "capped": False, "gate_reason": None}


def test_gate_passthrough_when_under_top_n():
    auto_watch = "AUTO_WATCH: A:NEWS B:NEWS"
    out = cg.gate_candidates(auto_watch, [], regime_label="defensive", diagnostic_score100=None, top_n=8)
    assert out["capped"] is False
    assert out["auto_watch"] == auto_watch


def test_gate_caps_to_top_n_by_confirmation_then_trending():
    tokens = " ".join(f"S{i}:NEWS" for i in range(10))
    auto_watch = f"AUTO_WATCH: {tokens}"
    symbols = _symbols(
        {"symbol": "S0", "trending_score100": 10, "origin": "news"},
        {"symbol": "S1", "trending_score100": 90, "origin": "both"},  # 확인 + 최고 트렌딩 -> 1순위
        {"symbol": "S2", "trending_score100": 80, "origin": "news"},
        {"symbol": "S3", "trending_score100": 70, "origin": "news"},
        {"symbol": "S4", "trending_score100": 60, "origin": "news"},
        {"symbol": "S5", "trending_score100": 50, "origin": "news"},
        {"symbol": "S6", "trending_score100": 40, "origin": "news"},
        {"symbol": "S7", "trending_score100": 30, "origin": "news"},
        {"symbol": "S8", "trending_score100": 20, "origin": "news"},
        {"symbol": "S9", "trending_score100": 5, "origin": "news"},
    )
    out = cg.gate_candidates(auto_watch, symbols, regime_label="defensive",
                             diagnostic_score100=None, top_n=8)
    assert out["capped"] is True
    kept = {t.split(":")[0] for t in out["auto_watch"].split()[1:]}
    assert kept == {"S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"}
    assert set(out["watch_only"]) == {"S0", "S9"}
    assert "S1" in out["auto_watch"]  # 확인(both)이 최우선으로 남는다
    assert "8건" in out["gate_reason"] and "관망(방어 국면)" in out["gate_reason"]


def test_gate_never_touches_symbols_list():
    """원장(payload["symbols"])은 이 함수의 입력일 뿐 반환값에 없다 — 호출부가
    그 리스트에서 아무것도 지우지 않는다는 계약을 코드로 보증한다."""
    tokens = " ".join(f"S{i}:NEWS" for i in range(9))
    auto_watch = f"AUTO_WATCH: {tokens}"
    symbols = _symbols(*({"symbol": f"S{i}", "trending_score100": 100 - i} for i in range(9)))
    out = cg.gate_candidates(auto_watch, symbols, regime_label="defensive",
                             diagnostic_score100=None, top_n=8)
    assert "symbols" not in out
    assert len(symbols) == 9  # 입력 리스트 자체가 변형되지 않았다


def test_gate_empty_auto_watch_is_noop():
    out = cg.gate_candidates("AUTO_WATCH: 없음", [], regime_label="defensive",
                             diagnostic_score100=None)
    assert out["capped"] is False


def test_gate_missing_symbol_metadata_falls_back_to_symbol_order():
    """symbols 목록에 없는 심볼(속성 결측)도 예외 없이 정렬 뒤로 밀린다 —
    0으로 위장하지 않고 그냥 최하위 취급."""
    tokens = " ".join(f"S{i}:NEWS" for i in range(9))
    auto_watch = f"AUTO_WATCH: {tokens}"
    out = cg.gate_candidates(auto_watch, [], regime_label="defensive",
                             diagnostic_score100=None, top_n=8)
    assert out["capped"] is True
    assert len(out["watch_only"]) == 1
