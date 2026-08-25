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
    """실제 설정 파일 회귀 가드 — 2026-08-25 전략 4종 체제(소유자 지시) 기준.

    활성 4종: ①news_momentum(뉴스·미국섹터 개장 단타) ②scalp_1m(1분봉 스캘핑)
    ③close_bet(종가배팅, 신규) ④frgn_accumulate(외국인 추세, 유일한 장기).
    나머지 7종은 비활성(코드·원장 보존) — 이전 배분표 단언은 그 체제와 함께
    내려갔다(git 히스토리 2026-08-18 판 참고).

    검산 두 개:
    - 활성 declared 합계가 시장별로 정확히 1.00 (설계표가 100%로 맞아떨어지는가)
    - 캡 적용 후(런타임) 합계가 1.00 이하 (over-allocation 금지 — 진짜 안전 invariant)

    전부 burn_in 이므로 캡 0.2가 실효 상한이다: KR 0.2+0.2+0.2+0.2=0.8,
    US 는 scalp_1m 만(0.2). declared 를 캡보다 크게 두는 이유는 frgn(0.3)·
    scalp US(1.0)가 승격 시 도달할 목표 선언이기 때문 — 승격 전까지 안전 캡이
    우선한다(이 저장소 기존 관례).
    """
    import yaml

    cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    active = ("news_momentum", "scalp_1m", "close_bet", "frgn_accumulate")

    for market in ("KR", "US"):
        declared = sum(cfg["strategies"][sid]["capital_fraction"][market] for sid in active)
        assert abs(declared - 1.0) < 1e-9, f"{market} 활성 declared 합계 {declared} != 1.00"

    fractions = validated_capital_fractions(cfg)
    assert fractions["news_momentum"] == {"KR": 0.2, "US": 0.0}
    assert fractions["close_bet"] == {"KR": 0.2, "US": 0.0}
    # declared 0.3/1.0 이 burn_in 캡 0.2 에 눌린다 — 승격 전 안전 캡 우선.
    assert fractions["frgn_accumulate"] == {"KR": 0.2, "US": 0.0}
    assert fractions["scalp_1m"] == {"KR": 0.2, "US": 0.2}

    for market in ("KR", "US"):
        runtime = sum(fractions[sid][market] for sid in active)
        assert runtime <= 1.0 + 1e-9, f"{market} 런타임 합계 {runtime} > 1.00 — over-allocation"

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
