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


def test_backtest_pass_is_capped_like_burn_in():
    """2026-09-03: 승격 CLI(quant/control/promotion.py)가 붙이는 backtest_pass는
    백테스트 게이트 GO는 통과했지만 실거래/paper 검증 전이라 burn_in과 같은
    상한을 받는다 — 자동 승격은 없다(사람이 30 라운드트립 이후 verified로 올린다)."""
    cfg = _cfg({
        "s": {
            "capital_fraction": 0.4,
            "validation": {"status": "backtest_pass", "evidence": {"gate_path": "x"}},
        },
    })
    assert validated_capital_fractions(cfg) == {"s": _both(0.2)}


def test_backtest_pass_does_not_log_as_unknown_status(caplog):
    """backtest_pass는 인식된 상태다 — '알 수 없는 status' 경고(오타 감지용)를
    타면 안 된다. info 로그(승격 진행 상황)만 남긴다."""
    import logging

    cfg = _cfg({
        "s": {
            "capital_fraction": 0.1,
            "validation": {"status": "backtest_pass", "evidence": {"gate_path": "x"}},
        },
    })
    with caplog.at_level(logging.INFO):
        validated_capital_fractions(cfg)
    assert not any("알 수 없음" in r.message for r in caplog.records)
    assert any("backtest_pass" in r.message for r in caplog.records)


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
    """실제 settings.yaml이 **성과 기반 배분 체제**의 불변식을 지키는지 검산한다.

    시대 전환 기록:
    - ~2026-09-01: 균등 대회 체제(전 레인 burn_in 캡 0.2 + 비례 축소 → KR 1/7,
      US 0.125). 이 테스트의 이전 버전이 그 균등을 단언했다.
    - 2026-09-01 심야: 소유자 지시로 실계좌 이식 + **성과 기반 배분**(scalp_1m
      앞줄 — 유일한 수수료 후 순양수 실측, llm_trader 축소 — 회전율 진단) +
      **상황 배분 프로토콜**(Claude가 08:24/20:03에 ±5%p 한도로 재조정, 최소
      3% 바닥). 균등 단언은 이제 정책과 반대라 제거하고, 프로토콜이 어떤 값을
      고르든 지켜져야 하는 안전 불변식만 남긴다.

    안전 invariant (배분 값이 바뀌어도 항상 참이어야 하는 것):
    - 어떤 burn_in 레인도 런타임 0.2(캡)를 넘지 않는다 — 미검증 전략 과대 배분 금지.
    - 시장별 런타임 합계 ≤ 1.0 — 초과 배분(의도치 않은 레버리지) 금지.
      (합계 < 1.0 은 허용 — 현금 버퍼는 프로토콜의 의도적 선택지다.)
    - 자본을 받는 모든 활성 레인 ≥ 0.03 — 프로토콜 바닥. 레인을 0으로 조용히
      굶기면 표본이 끊겨 개선 루프가 죽는다(소유자: "버리지 말고 개선").
    - 자본 레인 집합이 명단과 정확히 일치 — 새 레인이 0 선언으로 조용히 빠지거나
      명단 밖 레인이 자본을 받으면 잡는다.
    """
    import yaml

    cfg = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    fractions = validated_capital_fractions(cfg)

    # 2026-09-03 A/B 분할: `<id>`/`<id>_cat` 은 기준 배분을 반씩 나눠 가진다
    # (시장 합계 불변). news_scalp 는 같은 날 "after" 갈래로 재활성됐다.
    # 2026-09-03(같은 날 저녁, 소유자 결정 갱신) — 자동매매는 단타·스캘핑만:
    # close_bet/frgn_accumulate(KR)·overnight_drift/rsi2_dip(US)·rsi2_dip(KR)를
    # 비활성화했다(전부 오버나이트·다일 보유가 전략 정의). capital_fraction 값은
    # 손대지 않았으므로(코드·params·배분 보존 방침) 레인 목록에서만 뺀다 —
    # 비활성 전략은 validated_capital_fractions의 active 판정에서 제외되고
    # 이 테스트의 nonzero 대조는 active 기준이라 자동으로 맞는다.
    kr_lanes = ("news_momentum", "news_scalp",
                "scalp_1m", "scalp_1m_cat", "vol_breakout", "vol_breakout_cat",
                "llm_trader")
    us_lanes = ("scalp_1m", "scalp_1m_cat", "pullback_impulse", "pullback_impulse_cat",
                "mr_vwap_quiet", "vol_breakout", "vol_breakout_cat",
                "intraday_momentum", "gap_fade")

    active = [sid for sid, c in cfg["strategies"].items() if c.get("enabled", True)]
    for market, lanes in (("KR", kr_lanes), ("US", us_lanes)):
        for sid in lanes:
            f = fractions[sid][market]
            assert f <= 0.2 + 1e-9, (
                f"{market} {sid} 런타임 {f} > 0.2 — burn_in 캡 초과(미검증 과대 배분)"
            )
            assert f >= 0.03 - 1e-9, (
                f"{market} {sid} 런타임 {f} < 0.03 — 프로토콜 바닥 위반(레인 굶김)"
            )
        runtime = sum(fractions[sid][market] for sid in active)
        assert runtime <= 1.0 + 1e-9, f"{market} 런타임 합계 {runtime} > 1.00 — 초과 배분"
        nonzero = {sid for sid in active if fractions[sid][market] > 0}
        assert nonzero == set(lanes), f"{market} 자본 레인 불일치: {sorted(nonzero)}"


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


def test_disabled_burn_in_strategy_logs_at_debug_not_warning(caplog):
    """F7(2026-09-03, 감사 #7) — enabled: false 전략은 조립되지 않으므로 매
    기동마다 "미검증(burn_in) 캡핑" 경고를 찍을 이유가 없다. 캡 계산 자체는
    켜진 전략과 동일하게 유지하고(math 불변), 로그 레벨만 낮춘다."""
    import logging

    cfg = _cfg({
        "off": {"enabled": False, "capital_fraction": 0.9, "validation": {"status": "burn_in"}},
    }, cap=0.2)

    with caplog.at_level(logging.DEBUG):
        fr = validated_capital_fractions(cfg)

    assert fr == {"off": _both(0.2)}, "캡 계산은 활성 여부와 무관하게 그대로"
    capping_records = [r for r in caplog.records if "캡핑" in r.message]
    assert capping_records, "캡핑 로그 자체는 여전히 남아야 한다(디버깅 가능성 유지)"
    assert all(r.levelno == logging.DEBUG for r in capping_records)
    assert not any(r.levelno == logging.WARNING for r in capping_records)


def test_enabled_burn_in_strategy_still_logs_at_warning(caplog):
    """F7 회귀 가드 — 활성 전략의 캡핑 경고는 그대로 warning이어야 한다."""
    import logging

    cfg = _cfg({
        "on": {"enabled": True, "capital_fraction": 0.9, "validation": {"status": "burn_in"}},
    }, cap=0.2)

    with caplog.at_level(logging.DEBUG):
        validated_capital_fractions(cfg)

    capping_records = [r for r in caplog.records if "캡핑" in r.message]
    assert capping_records and all(r.levelno == logging.WARNING for r in capping_records)
