"""quant.core.tglanes — 텔레그램 포럼 토픽 레인 라우팅(순수 함수, I/O 없음)."""
from __future__ import annotations

from quant.core import tglanes


def test_lanes_registry_has_five_fixed_ids():
    assert set(tglanes.LANES) == {"control", "trades", "briefs", "intel", "ops"}


def test_header_known_lane():
    assert tglanes.header("trades") == "📈 매매"
    assert tglanes.header("ops") == "🚨 운영"
    assert tglanes.header("control") == "🎛 제어실"
    assert tglanes.header("briefs") == "📰 브리핑"
    assert tglanes.header("intel") == "📡 채널 인텔"


def test_header_unknown_lane_returns_the_id_itself():
    """오타 레인을 조용히 빈 문자열로 감추지 않는다."""
    assert tglanes.header("nope") == "nope"


def test_resolve_no_mapping_falls_back_to_legacy():
    assert tglanes.resolve("trades", None, legacy_chat_id=999) == (999, None)


def test_resolve_empty_mapping_falls_back_to_legacy():
    assert tglanes.resolve("trades", {}, legacy_chat_id=999) == (999, None)


def test_resolve_bound_lane_returns_chat_and_thread():
    mapping = {"chat_id": 111, "threads": {"trades": 42, "ops": 7}}
    assert tglanes.resolve("trades", mapping, legacy_chat_id=999) == (111, 42)
    assert tglanes.resolve("ops", mapping, legacy_chat_id=999) == (111, 7)


def test_resolve_unbound_lane_in_partially_bound_mapping_falls_back():
    """일부 레인만 바인딩된 매핑에서, 아직 안 묶인 레인은 레거시로 폴백한다."""
    mapping = {"chat_id": 111, "threads": {"trades": 42}}
    assert tglanes.resolve("briefs", mapping, legacy_chat_id=999) == (999, None)


def test_resolve_missing_chat_id_falls_back_even_with_threads():
    """chat_id가 없는 매핑(파손/부분 기록)은 레인이 있어도 폴백한다."""
    mapping = {"threads": {"trades": 42}}
    assert tglanes.resolve("trades", mapping, legacy_chat_id=999) == (999, None)


def test_resolve_passes_through_string_chat_ids():
    """파이썬 노티파이어는 chat_id를 문자열 env 값으로 쓴다 — 타입을 강제하지 않는다."""
    mapping = {"chat_id": "-100123", "threads": {"ops": "55"}}
    assert tglanes.resolve("ops", mapping, legacy_chat_id="legacy-chat") == ("-100123", "55")


def test_is_bound_false_when_no_mapping():
    assert tglanes.is_bound(None) is False
    assert tglanes.is_bound({}) is False


def test_is_bound_false_when_threads_empty():
    assert tglanes.is_bound({"chat_id": 111, "threads": {}}) is False


def test_is_bound_false_when_chat_id_missing():
    assert tglanes.is_bound({"threads": {"trades": 42}}) is False


def test_is_bound_true_when_at_least_one_lane_bound():
    assert tglanes.is_bound({"chat_id": 111, "threads": {"trades": 42}}) is True
