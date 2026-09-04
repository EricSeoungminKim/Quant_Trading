"""전략 설명 콘텐츠(`quant.control.strategy_help.build_strategy_help`) 테스트.

이 스위트가 고정하는 것 (프롬프트 계약):
- STRATEGY_REGISTRY의 순수함수 계약 껍질(`_pure`)이 아닌 전략 id 23개 전부
  `help`를 낸다 — KO/EN 모든 필드가 비어있지 않다.
- `_cat`(A/B 촉매 갈래) id는 base와 다른 `entry_ko/en`을 내고(촉매 문구가
  붙는다), 실제 `config/settings.yaml`의 `universe_filter` 태그를 반영한다.
- 완전히 낯선 id(레지스트리에 없는 id)는 크래시 없이 일반 fallback을 낸다.
- 모든 문자열 필드가 400자 이하.
- `refs`의 url은 전부 `https://`로 시작한다.
- 파라미터가 없으면(빈 strategies_cfg) 숫자를 지어내지 않고 절이 빠질 뿐,
  크래시하지 않는다.
- 같은 입력에는 항상 같은 출력(결정론).
"""
from __future__ import annotations

import yaml

from quant.control.strategy_help import build_strategy_help
from quant.core.strategy_ids import base_strategy_id
from quant.trade.strategy import STRATEGY_REGISTRY

with open("config/settings.yaml", encoding="utf-8") as _f:
    _SETTINGS_STRATEGIES = yaml.safe_load(_f)["strategies"]

# STRATEGY_REGISTRY 중 순수함수 계약 껍질(`_pure`)이 아닌 것 — 원장에 나타날 수
# 있는 실제 전략 id 23개(프롬프트 "23개 non-pure ids"). `_pure` 껍질은
# `base_strategy_id`가 벗겨 기준 id로 조회하므로 별도 커버리지가 필요 없다
# (이 파일의 test_pure_shell_id_inherits_base_help가 그 사실을 확인한다).
NON_PURE_IDS = sorted(
    sid for sid in STRATEGY_REGISTRY if not sid.endswith("_pure")
)

_STR_FIELDS = (
    "theory_ko", "theory_en", "entry_ko", "entry_en", "exit_ko", "exit_en",
    "sizing_ko", "sizing_en", "evidence_ko", "evidence_en",
)

_CATEGORIES = {"intraday", "swing", "experimental"}


def test_covers_all_23_non_pure_registry_ids():
    assert len(NON_PURE_IDS) == 23, NON_PURE_IDS


def test_every_registry_id_has_complete_bilingual_help():
    for sid in NON_PURE_IDS:
        help_ = build_strategy_help(sid, _SETTINGS_STRATEGIES)
        assert help_["category"] in _CATEGORIES, f"{sid}: 알 수 없는 category {help_['category']!r}"
        for field in _STR_FIELDS:
            value = help_.get(field)
            assert value, f"{sid}.{field} 비어있음"
            assert isinstance(value, str)
            assert len(value) <= 400, f"{sid}.{field} {len(value)}자 — 400자 초과"
        assert isinstance(help_["refs"], list) and len(help_["refs"]) <= 3, sid
        for ref in help_["refs"]:
            assert set(ref) == {"label", "url"}, f"{sid}: refs 항목 형식 오류 {ref!r}"
            assert ref["url"].startswith("https://"), f"{sid}: refs url이 https가 아님 {ref!r}"
            assert ref["label"]


def test_unknown_id_gets_generic_fallback_without_crashing():
    help_ = build_strategy_help("totally_unknown_strategy_id", _SETTINGS_STRATEGIES)
    assert help_["category"] == "experimental"
    assert help_["evidence_ko"] == "문서 없음."
    assert help_["evidence_en"] == "No documentation."
    for field in _STR_FIELDS:
        assert help_[field]


def test_missing_strategies_cfg_does_not_crash_or_invent_numbers():
    """`strategies_cfg`가 없거나(None) 그 id가 없으면 파라미터 의존 문장의
    숫자를 지어내지 않고 절만 조용히 빠져야 한다 — 크래시도 안 된다."""
    for sid in NON_PURE_IDS:
        help_ = build_strategy_help(sid, None)
        for field in _STR_FIELDS:
            assert help_[field]
        # 파라미터가 없으니 사이징엔 배분 정보가 없다는 사실만 남아야 한다
        assert "현재 배분 없음" in help_["sizing_ko"] or "%" in help_["sizing_ko"]


def test_pure_shell_id_inherits_base_help():
    """`donchian_pure`처럼 `_pure` 접미사가 붙은 등록 id는 `base_strategy_id`가
    벗긴 기준 id(`donchian`)와 같은 정적 콘텐츠를 낸다."""
    for sid in STRATEGY_REGISTRY:
        if not sid.endswith("_pure"):
            continue
        base = base_strategy_id(sid)
        pure_help = build_strategy_help(sid, _SETTINGS_STRATEGIES)
        base_help = build_strategy_help(base, _SETTINGS_STRATEGIES)
        assert pure_help["theory_ko"] == base_help["theory_ko"], sid
        assert pure_help["category"] == base_help["category"], sid


def test_catalyst_arm_entry_mentions_catalyst_and_differs_from_base():
    for cat_id, base_id in (
        ("scalp_1m_cat", "scalp_1m"),
        ("pullback_impulse_cat", "pullback_impulse"),
        ("vol_breakout_cat", "vol_breakout"),
    ):
        cat_help = build_strategy_help(cat_id, _SETTINGS_STRATEGIES)
        base_help = build_strategy_help(base_id, _SETTINGS_STRATEGIES)
        assert cat_help["entry_ko"] != base_help["entry_ko"], cat_id
        assert cat_help["entry_en"] != base_help["entry_en"], cat_id
        assert "A/B" in cat_help["entry_ko"]
        assert "A/B" in cat_help["entry_en"]
        assert base_id in cat_help["evidence_ko"] or base_id in cat_help["evidence_en"]


def test_catalyst_arm_tag_reflects_actual_universe_filter_setting():
    """촉매 갈래 문구의 태그는 코드에 하드코딩된 것이 아니라
    `config/settings.yaml`의 실제 `universe_filter`에서 읽는다 — 예를 들어
    scalp_1m_cat의 KR 쪽은 FRGN 태그를 요구한다(설정 그대로)."""
    scalp_cat_cfg = _SETTINGS_STRATEGIES["scalp_1m_cat"]
    kr_filter = scalp_cat_cfg["universe_filter"]["KR"]
    assert "FRGN" in kr_filter.get("require_any", [])
    help_ = build_strategy_help("scalp_1m_cat", _SETTINGS_STRATEGIES)
    assert "FRGN" in help_["entry_ko"]
    assert "FRGN" in help_["entry_en"]


def test_sizing_includes_capital_fraction_and_hard_rails():
    help_ = build_strategy_help("scalp_1m", _SETTINGS_STRATEGIES)
    assert "%" in help_["sizing_ko"]
    assert "5" in help_["sizing_ko"] and "10" in help_["sizing_ko"]  # -5%/+10% 하드레일
    assert "29.5" in help_["sizing_ko"]  # KR 상한가 진입 금지 레일


def test_overnight_strategy_sizing_notes_exemption_from_hard_rail():
    """오버나이트 보유형(예: close_bet)은 장중 -5%/+10% 하드레일 대상이 아니다
    (`config/settings.yaml`의 `risk.overnight_strategies`) — sizing 문구가
    그 예외를 명시해야 한다."""
    help_ = build_strategy_help("close_bet", _SETTINGS_STRATEGIES)
    assert "오버나이트" in help_["sizing_ko"]
    assert "exempt" in help_["sizing_en"] or "overnight" in help_["sizing_en"].lower()


def test_no_kr_limit_up_note_when_strategy_has_no_kr_allocation():
    """KR 배분이 0인 전략(예: overnight_drift, symbols=[QQQ])은 KR 상한가
    레일 문구를 낼 이유가 없다 — 없는 노출을 있는 것처럼 말하지 않는다."""
    help_ = build_strategy_help("overnight_drift", _SETTINGS_STRATEGIES)
    assert "29.5" not in help_["sizing_ko"]


def test_build_strategy_help_is_deterministic():
    for sid in NON_PURE_IDS:
        first = build_strategy_help(sid, _SETTINGS_STRATEGIES)
        second = build_strategy_help(sid, _SETTINGS_STRATEGIES)
        assert first == second, sid
