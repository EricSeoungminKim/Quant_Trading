"""전략 검증 게이트 — 미검증 전략이 선언만으로 자본을 다 받지 못하게 한다.

2026-08-11 사용자 지시("검증 규율을 따르게끔 구축")의 회귀 가드. 첫 dry run에서
백테스트를 한 번도 안 거친 intraday_scan이 자금 배분을 받고 하루 거래의 대부분을
낸 것이 계기 — 검증 규율은 문서가 아니라 조립 루트가 강제해야 한다.

2026-08-12: capital_fraction이 시장별(KR/US)로 분리되며 validated_capital_fractions의
반환 타입이 `{strategy_id: float}`에서 `{strategy_id: {market: float}}`로 바뀌었다.
스칼라 선언은 양 시장에 동일하게 적용되는 것이 하위호환 요건이라 그 경로도 여기서
고정한다.
"""
from __future__ import annotations

import pytest

from quant.apps.assembly import validated_capital_fractions

_MARKETS = ("KR", "US")


def _cfg(strategies: dict, cap: float | None = None) -> dict:
    cfg: dict = {"strategies": strategies}
    if cap is not None:
        cfg["validation_gate"] = {"burn_in_max_capital_fraction": cap}
    return cfg


def _both(v: float) -> dict:
    """스칼라 선언이 시장별로 정규화된 뒤의 기대값 — 양 시장에 동일."""
    return {"KR": v, "US": v}


# ========== 스칼라 선언 — 하위호환(양 시장에 동일하게 적용)

def test_verified_with_evidence_keeps_declared_fraction():
    cfg = _cfg({
        "donchian": {
            "capital_fraction": 0.4,
            "validation": {"status": "verified", "evidence": "전작 라이브 검증"},
        },
    })
    assert validated_capital_fractions(cfg) == {"donchian": _both(0.4)}


def test_burn_in_is_capped_to_the_gate_limit():
    cfg = _cfg({
        "orb_scan": {
            "capital_fraction": 0.4,
            "validation": {"status": "burn_in"},
        },
    })
    assert validated_capital_fractions(cfg) == {"orb_scan": _both(0.2)}


def test_verified_without_evidence_is_demoted_to_burn_in():
    """근거 없는 검증 선언은 검증이 아니다 — 거짓 선언을 막는 최소 장치."""
    cfg = _cfg({
        "s": {"capital_fraction": 0.5, "validation": {"status": "verified"}},
    })
    assert validated_capital_fractions(cfg) == {"s": _both(0.2)}


def test_missing_validation_block_defaults_to_burn_in():
    """선언을 잊으면 안전한 쪽(캡핑)으로 — 게이트 우회 기본값 금지."""
    cfg = _cfg({"s": {"capital_fraction": 1.0}})
    assert validated_capital_fractions(cfg) == {"s": _both(0.2)}


def test_unknown_status_is_treated_as_burn_in():
    """오타(varified 등)가 게이트를 우회하면 안 된다."""
    cfg = _cfg({
        "s": {"capital_fraction": 0.9, "validation": {"status": "varified", "evidence": "x"}},
    })
    assert validated_capital_fractions(cfg) == {"s": _both(0.2)}


def test_burn_in_below_cap_is_not_inflated():
    """캡은 상한이지 목표가 아니다 — 0.1 선언이 0.2로 늘어나면 안 된다."""
    cfg = _cfg({
        "s": {"capital_fraction": 0.1, "validation": {"status": "burn_in"}},
    })
    assert validated_capital_fractions(cfg) == {"s": _both(0.1)}


def test_custom_gate_limit_is_honored():
    cfg = _cfg(
        {"s": {"capital_fraction": 0.9, "validation": {"status": "burn_in"}}},
        cap=0.05,
    )
    assert validated_capital_fractions(cfg) == {"s": _both(0.05)}


def test_production_settings_yaml_passes_the_gate_as_intended():
    """실제 설정 파일 회귀 가드: donchian만 verified, 나머지는 burn_in으로 형평.

    2026-08-12(3차) 재기준: capital_fraction이 시장별 dict가 되며 검증 게이트의
    출력도 시장별이 됐다. donchian은 KR 배분이 0(TQQQ/SQQQ 고정이라 KR에서 거래
    불가)이고 US는 verified 선언값(0.3) 그대로다. 나머지 burn_in 전략은 시장별
    선언값이 캡(0.2) 아래면 그대로 통과한다.

    2026-08-17: 갈래 A/B(spec §5) 신설로 기존 7전략의 KR 선언값을 균등 x0.5
    했다(config/settings.yaml 상단 "3갈래 자본 배분" 표) — 아래 기대값이 그
    결과다. US는 A/B가 배분 0이라 불변. A(news_scalp)/B(frgn_accumulate)는
    같은 날 태그 배선(서브프로젝트 T Task 1~2)이 끝나 `enabled: true`로 올라
    시장별 합계(아래)에도 포함된다 — B의 declared 0.3이 burn_in 캡 0.2로
    눌리는 것은 활성화 여부와 무관하게 그대로다(게이트는 비활성 상태에서도
    같은 캡을 적용했었다).

    2026-08-18: scalp_1m(1분봉 조기 진입, docs/superpowers/specs/
    2026-08-18-scalp-1m-design.md) 신설 — news_scalp KR 0.2→0.1, intraday_scan
    US 0.2→0.1을 각각 이관해 KR/US 0.1씩 배정한다(같은 유니버스에서 진입
    방식만 달리해 나란히 채점). 시장별 합계는 이관이라 불변이다.
    """
    import yaml

    cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    fractions = validated_capital_fractions(cfg)
    assert fractions["donchian"] == {"KR": 0.0, "US": 0.3}, "검증된 donchian은 시장별 선언값 유지"
    assert fractions["orb_scan"] == {"KR": 0.1, "US": 0.2}
    assert fractions["intraday_scan"] == {"KR": 0.1, "US": 0.1}, "US 0.2→0.1: scalp_1m US에 0.1 이관"
    assert fractions["news_momentum"] == {"KR": 0.1, "US": 0.0}, "US는 EVENT 태그 소스가 없어 0"
    assert fractions["confluence"] == {"KR": 0.075, "US": 0.2}
    # mean_reversion US 0.0: symbols가 069500/229200 고정이라 US 신호를 낼 수 없다.
    # 배분을 주면 donchian의 KR 0.4와 똑같은 죽은 자본이 된다(시장별 분리로 없애려던
    # 바로 그 문제). 회수한 0.1은 US에서 실제 거래 가능한 orb_scan/confluence로 갔다.
    assert fractions["mean_reversion"] == {"KR": 0.075, "US": 0.0}
    assert fractions["cross_momentum"] == {"KR": 0.05, "US": 0.1}
    # 갈래 A — declared 0.1(2026-08-18: 0.2→0.1, scalp_1m KR에 0.1 이관)이
    # burn_in 캡(0.2) 아래라 그대로 통과.
    assert fractions["news_scalp"] == {"KR": 0.1, "US": 0.0}
    # 갈래 B — declared 0.3(spec §5 목표치)이 burn_in 캡(0.2)에 눌린다. paper
    # 번인 동안은 안전 캡이 실제 목표보다 우선한다 — 이 저장소 전체의 기존 관례.
    assert fractions["frgn_accumulate"] == {"KR": 0.2, "US": 0.0}
    # scalp_1m(신규, 2026-08-18) — declared 0.1이 캡 아래라 그대로 통과.
    assert fractions["scalp_1m"] == {"KR": 0.1, "US": 0.1}

    # declared(캡 적용 전) 목표 합계는 시장별로 정확히 1.00이어야 한다 — 3갈래
    # 전체(기존 7전략 + A + B + scalp_1m)를 합친 arithmetic sanity check. burn_in
    # 캡이나 enabled 여부와 무관하게, "설계한 숫자가 실제로 100%로 맞아떨어지는가"를
    # 여기서 검산한다(전략 추가·재배분 때마다 값 갱신을 잊으면 여기서 걸린다).
    branch_c_and_ab = (
        "donchian", "orb_scan", "intraday_scan", "mean_reversion", "cross_momentum",
        "confluence", "news_momentum", "news_scalp", "frgn_accumulate", "scalp_1m",
    )
    for market in ("KR", "US"):
        declared_total = sum(
            cfg["strategies"][sid]["capital_fraction"][market] for sid in branch_c_and_ab
        )
        assert abs(declared_total - 1.0) < 1e-9, (
            f"{market} 3갈래 declared 목표 합계 {declared_total} != 1.00 (spec §5 A20/B30/C50)"
        )

    # 런타임(활성 전략만, burn_in 캡 적용 후) 합계 — **절대 1.00을 넘으면 안 된다**
    # (over-allocation 방지가 진짜 안전 invariant, assembly.py의 비례축소 안전망과
    # 같은 경계). KR은 갈래 C 0.5 + A(0.1) + B(0.2, 캡에 눌림) + scalp_1m(0.1) = 0.9
    # — declared 목표(1.00)보다 낮은 것은 버그가 아니라 B의 burn_in 캡이 declared
    # 0.3 중 0.1을 못 쓰게 막은 결과다(안전이 목표치보다 우선). scalp_1m 신설은
    # news_scalp에서 0.1을 그대로 이관했을 뿐이라 KR 합계 자체는 불변이다.
    active = {k: v for k, v in fractions.items() if cfg["strategies"][k].get("enabled", True)}
    kr_total = sum(v.get("KR", 0.0) for v in active.values())
    us_total = sum(v.get("US", 0.0) for v in active.values())
    assert kr_total == pytest.approx(0.9), f"KR 활성 합계는 0.9여야 한다(B가 캡에 눌림): {kr_total}"
    assert kr_total < 1.0 + 1e-9, "over-allocation 금지 — 활성 합계가 1.00을 넘으면 안 된다"
    assert us_total == pytest.approx(1.0), f"US는 scalp_1m 이관이 intraday_scan에서 그대로 온 것이라 1.00 그대로여야 한다: {us_total}"


# ========== 시장별 dict 선언 — 해석 + 빠진 시장 처리

def test_market_dict_declaration_is_used_as_is_when_verified():
    cfg = _cfg({
        "donchian": {
            "capital_fraction": {"KR": 0.0, "US": 0.3},
            "validation": {"status": "verified", "evidence": "근거"},
        },
    })
    assert validated_capital_fractions(cfg) == {"donchian": {"KR": 0.0, "US": 0.3}}


def test_market_missing_from_dict_is_treated_as_zero():
    """명시하지 않은 시장엔 자본을 주지 않는다 — "모르면 안전한 쪽"."""
    cfg = _cfg({
        "news_momentum": {
            "capital_fraction": {"KR": 0.2},  # US 미기재
            "validation": {"status": "verified", "evidence": "근거"},
        },
    })
    assert validated_capital_fractions(cfg) == {"news_momentum": {"KR": 0.2, "US": 0.0}}


def test_market_dict_burn_in_cap_applies_per_market_independently():
    """시장별 캡 — 한 시장이 캡을 넘어도 다른 시장 값은 그대로다."""
    cfg = _cfg({
        "s": {
            "capital_fraction": {"KR": 0.05, "US": 0.5},
            "validation": {"status": "burn_in"},
        },
    })
    assert validated_capital_fractions(cfg) == {"s": {"KR": 0.05, "US": 0.2}}


# ========== 배분 합계 가드 (2026-08-12 감사: 승격/추가가 조용히 100%를 넘김) — 시장별 독립

def test_total_allocation_over_100pct_is_scaled_down():
    """전략을 추가하거나 승격하면 합이 조용히 1.0을 넘는다 — 실제로 1.10이 됐었다.
    각 전략의 선언값은 다 맞는데 합계를 검산하는 곳이 없었다. 초과 배분은
    의도치 않은 레버리지다. 스칼라 선언이므로 KR/US 양쪽 다 초과해 같이 축소된다."""
    cfg = {
        "validation_gate": {"burn_in_max_capital_fraction": 0.2},
        "strategies": {
            "a": {"enabled": True, "capital_fraction": 0.8,
                  "validation": {"status": "verified", "evidence": "근거"}},
            "b": {"enabled": True, "capital_fraction": 0.8,
                  "validation": {"status": "verified", "evidence": "근거"}},
        },
    }
    fr = validated_capital_fractions(cfg)
    for m in _MARKETS:
        total = sum(f[m] for f in fr.values())
        assert total == pytest.approx(1.0), f"{m} 시장 합계는 100%를 넘지 않아야 한다"
        assert fr["a"][m] == pytest.approx(fr["b"][m])


def test_market_allocation_scale_down_is_independent_per_market():
    """KR 합이 100%를 넘어도 US는 넘지 않으면 US는 축소되지 않는다 — 시장별 독립 판정."""
    cfg = {
        "validation_gate": {"burn_in_max_capital_fraction": 0.5},
        "strategies": {
            "a": {"enabled": True,
                  "capital_fraction": {"KR": 0.7, "US": 0.3},
                  "validation": {"status": "verified", "evidence": "근거"}},
            "b": {"enabled": True,
                  "capital_fraction": {"KR": 0.7, "US": 0.3},
                  "validation": {"status": "verified", "evidence": "근거"}},
        },
    }
    fr = validated_capital_fractions(cfg)
    assert sum(f["KR"] for f in fr.values()) == pytest.approx(1.0), "KR은 1.4 → 1.0으로 축소"
    assert sum(f["US"] for f in fr.values()) == pytest.approx(0.6), "US는 0.6로 100% 미만 — 축소 없음"
    assert fr["a"]["US"] == pytest.approx(0.3)


def test_disabled_strategies_do_not_count_toward_the_total():
    cfg = {
        "validation_gate": {"burn_in_max_capital_fraction": 0.2},
        "strategies": {
            "on": {"enabled": True, "capital_fraction": 0.9,
                   "validation": {"status": "verified", "evidence": "근거"}},
            "off": {"enabled": False, "capital_fraction": 0.9,
                    "validation": {"status": "verified", "evidence": "근거"}},
        },
    }
    fr = validated_capital_fractions(cfg)
    assert fr["on"] == _both(0.9), "비활성 전략은 합계에 안 들어가므로 축소 없음"


def test_real_config_allocation_sums_to_at_most_one_per_market():
    """실제 설정 회귀 가드 — 전략을 추가할 때마다 시장별로 여기서 걸린다."""
    import yaml

    cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    fr = validated_capital_fractions(cfg)
    active = {k: v for k, v in fr.items() if cfg["strategies"][k].get("enabled", True)}
    for m in _MARKETS:
        total = sum(v[m] for v in active.values())
        assert total <= 1.0 + 1e-9, f"{m} 시장 활성 전략 배분 합 초과: {total} ({active})"
