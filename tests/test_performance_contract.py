"""공개 성과 JSON 발행 게이트(`quant.control.performance_contract.validate_payload`) 테스트.

합성 payload로 각 규칙을 하나씩 고정한다 — 실제 `build_performance_payload` 출력에
의존하지 않는다(그 함수의 회귀는 `tests/test_performance.py`가 이미 잡는다). 여기서
잡는 것은 "이 검증기가 계약대로 판정하는가" 하나다.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime

import pytest

from quant.control.performance import FX_KRW_PER_USD
from quant.control.performance_contract import Finding, validate_payload


def _equity_book(currency: str) -> dict:
    seed = 10_000_000 if currency == "KRW" else 10_000
    return {
        "currency": currency,
        "seed": seed,
        "seed_basis": "현금만",
        "seed_basis_en": "cash only",
        "rows": [
            {"date": "2026-09-01", "cum_pct": 0.5, "day_pct": 0.5, "fills": 2, "phase": "real_seeded"},
            {"date": "2026-09-02", "cum_pct": 1.0, "day_pct": 0.5, "fills": 2, "phase": "real_seeded"},
        ],
        "max_drawdown_pct": 0.0,
        "chart": {
            "y_axis": {"min": -1.0, "max": 1.0, "ticks": [1.0, 0.5, 0.0, -0.5, -1.0], "zero": 0.0},
            "phase_boundaries": [],
        },
    }


def _strategy(sid: str = "donchian") -> dict:
    market_stats = {
        "trips": 40, "wins": 22, "win_rate": 0.55, "ci_low": 0.4, "ci_high": 0.69,
        "expectancy_bp": 12.5, "verdict": "유의미", "sample_warning": False,
    }
    return {
        "id": sid,
        "name_ko": "돈치안 채널 추세추종",
        "name_en": "Donchian Channel Trend Following",
        "total": {**market_stats, "markets": ["KR"]},
        "by_market": {"asia": dict(market_stats), "us": None},
        "trades_per_day": 2.0,
        "avg_hold_minutes": 30.0,
        "enabled": True,
        "help": {"category": "intraday"},
        "curve": {
            "asia": [{"date": "2026-09-01", "cum_net": 100.0, "day_net": 100.0, "cum_trips": 1}],
            "us": [],
        },
    }


def _valid_payload() -> dict:
    return {
        "generated_at": "2026-09-06T12:00:00+09:00",
        "disclaimer": "모의투자(paper) 기록입니다. 실제 자금이 투입되지 않았습니다.",
        "disclaimer_en": "Paper trading record. No real capital was deployed.",
        "period": {
            "start": "2026-09-01", "end": "2026-09-02", "sessions": 2, "total_fills": 4,
            "scope": "real_seeded", "note": "실계좌 이식 이후", "note_en": "Since real-account transplant",
        },
        "phases": [
            {
                "id": "real_seeded", "label": "실계좌 스냅샷 이식", "label_en": "Live-account snapshot transplant",
                "from": "2026-09-01T00:00:00+09:00", "to": None,
                "seed_krw": 10_000_000, "seed_basis": "현금만", "seed_basis_en": "cash only",
                "note": "실계좌 이식", "note_en": "Live-account transplant",
            }
        ],
        "equity_asia": _equity_book("KRW"),
        "equity_us": _equity_book("USD"),
        "strategies": [_strategy("donchian")],
        "strategies_scope": "lifetime",
        "enabled_count": 1,
        "strategies_note": "전략 통계는 모의 시대 포함 누적 40왕복",
        "strategies_note_en": "Strategy stats are lifetime incl. paper era, 40 round trips",
        "strategy_curves_note": "전략별 곡선은 종결 왕복 기준 누적 순손익",
        "strategy_curves_note_en": "Per-strategy curves are cumulative net P&L from closed round trips",
        "excluded": {},
        "costs": {
            "kr_stock_roundtrip_bp": 30.0, "kr_etf_roundtrip_bp": 10.0, "us_roundtrip_bp": 10.0,
            "kr_tax_bp": 20.0, "note": "설명", "note_en": "note", "fee_drag_pct_of_gross": 5.0,
        },
        "prior_paper": {},
        "paper_epoch": {},
    }


def _valid_paper_epoch() -> dict:
    seed_krw = 10_000_000 + 10_000 * FX_KRW_PER_USD
    return {
        "epoch": "2026-09-07T00:00:00+09:00",
        "account_model": {
            "kr_start_krw": 10_000_000, "us_start_usd": 10_000,
            "note_ko": "설명", "note_en": "note",
        },
        "overall": {
            "currency": "KRW", "seed_krw": seed_krw,
            "fx_source_note": "설명", "fx_source_note_en": "note",
            "rows": [{"date": "2026-09-07", "day_krw": 1000.0, "cum_krw": 1000.0, "cum_pct": 0.005}],
            "max_drawdown_pct": 0.0,
            "chart": {"y_axis": {"min": -1.0, "max": 1.0, "ticks": [1.0, 0.0, -1.0], "zero": 0.0}},
        },
        "strategies": [
            {
                "id": "donchian",
                "start_capital": {"KRW": 10_000_000},
                "curve": {
                    "asia": [{"date": "2026-09-07", "day_native": 100.0, "cum_native": 100.0, "cum_pct": 0.001, "trips": 1}],
                    "us": [],
                },
            },
            {
                "id": "gap_fade",
                "start_capital": {"USD": 10_000},
                "curve": {"asia": [], "us": []},
            },
        ],
    }


def _errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


def _warns(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "warn"]


def _has_path(findings: list[Finding], path: str) -> bool:
    return any(f.path == path for f in findings)


# ---------------------------------------------------------------------------
# 기준선 — 유효한 payload는 findings 없음
# ---------------------------------------------------------------------------


def test_valid_payload_has_no_findings():
    assert validate_payload(_valid_payload()) == []


def test_valid_payload_with_paper_epoch_has_no_findings():
    payload = _valid_payload()
    payload["paper_epoch"] = _valid_paper_epoch()
    assert validate_payload(payload) == []


def test_finding_to_dict():
    f = Finding("error", "foo.bar", "메시지")
    assert f.to_dict() == {"severity": "error", "path": "foo.bar", "message": "메시지"}


def test_unknown_top_level_keys_are_ignored():
    """다른 워커가 얹는 report_accuracy 같은 미지의 최상위 키는 무시한다."""
    payload = _valid_payload()
    payload["report_accuracy"] = {"whatever": "shape", "it": ["wants"]}
    assert validate_payload(payload) == []


def test_payload_not_a_dict():
    findings = validate_payload([])  # type: ignore[arg-type]
    assert _errors(findings) and findings[0].path == "$"


# ---------------------------------------------------------------------------
# 1) 필수 키/타입
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected_path"),
    [
        ("generated_at", "$.generated_at"),
        ("disclaimer", "$.disclaimer"),
        ("period", "period"),
        ("phases", "phases"),
        ("equity_asia", "equity_asia"),
        ("equity_us", "equity_us"),
        ("strategies", "strategies"),
        ("excluded", "excluded"),
        ("costs", "costs"),
    ],
)
def test_missing_required_top_level_key_is_error(key, expected_path):
    payload = _valid_payload()
    del payload[key]
    findings = _errors(validate_payload(payload))
    assert any(f.path == expected_path for f in findings), findings


def test_wrong_type_for_required_field_is_error():
    payload = _valid_payload()
    payload["disclaimer"] = 12345  # 문자열이어야 함
    findings = _errors(validate_payload(payload))
    assert any(f.path == "$.disclaimer" for f in findings)


def test_optional_field_absent_is_fine():
    payload = _valid_payload()
    del payload["prior_paper"]
    del payload["paper_epoch"]
    del payload["disclaimer_en"]
    assert validate_payload(payload) == []


# ---------------------------------------------------------------------------
# 2) NaN/Inf/null 금지
# ---------------------------------------------------------------------------


def test_nan_number_is_error():
    payload = _valid_payload()
    payload["costs"]["kr_tax_bp"] = float("nan")
    findings = _errors(validate_payload(payload))
    assert any("costs" in f.path and "NaN" in f.message for f in findings)


def test_inf_number_is_error():
    payload = _valid_payload()
    payload["equity_asia"]["rows"][0]["day_pct"] = float("inf")
    findings = _errors(validate_payload(payload))
    assert any("equity_asia.rows[0]" in f.path for f in findings)


def test_null_where_number_required_is_error():
    payload = _valid_payload()
    payload["period"]["sessions"] = None
    findings = _errors(validate_payload(payload))
    assert any(f.path == "period.sessions" for f in findings)


def test_null_allowed_where_type_says_nullable():
    payload = _valid_payload()
    payload["equity_asia"]["seed"] = None  # EquityBook.seed: number | null
    assert validate_payload(payload) == []


# ---------------------------------------------------------------------------
# 3) 날짜 ISO + 단조성
# ---------------------------------------------------------------------------


def test_non_iso_date_is_error():
    payload = _valid_payload()
    payload["equity_asia"]["rows"][0]["date"] = "09/01/2026"
    findings = _errors(validate_payload(payload))
    assert any("equity_asia.rows[0].date" in f.path for f in findings)


def test_reversed_dates_in_curve_is_error():
    payload = _valid_payload()
    rows = payload["equity_asia"]["rows"]
    rows[0]["date"], rows[1]["date"] = rows[1]["date"], rows[0]["date"]
    findings = _errors(validate_payload(payload))
    assert any("역순" in f.message for f in findings)


def test_duplicate_dates_in_curve_is_error():
    payload = _valid_payload()
    payload["equity_asia"]["rows"][1]["date"] = payload["equity_asia"]["rows"][0]["date"]
    findings = _errors(validate_payload(payload))
    assert any("역순" in f.message or "중복" in f.message for f in findings)


# ---------------------------------------------------------------------------
# 4) 거래일 공백(4일, 주말/휴일 제외)
# ---------------------------------------------------------------------------


def test_large_gap_without_weekend_excuse_is_warn():
    payload = _valid_payload()
    # 화(09-01) → 그 다음 화(09-08): 영업일 환산으로도 5일 공백
    payload["equity_asia"]["rows"][1]["date"] = "2026-09-08"
    findings = _warns(validate_payload(payload))
    assert any("거래일 공백" in f.message for f in findings)


def test_weekend_gap_is_not_flagged():
    payload = _valid_payload()
    # 금(09-04) → 월(09-07): 주말 2일 뿐이라 영업일 환산 공백은 1일
    payload["equity_asia"]["rows"][0]["date"] = "2026-09-04"
    payload["equity_asia"]["rows"][1]["date"] = "2026-09-07"
    findings = validate_payload(payload)
    assert not any("거래일 공백" in f.message for f in findings)


def test_holiday_covered_gap_is_not_flagged():
    payload = _valid_payload()
    # 월(09-07) → 그 다음 월(09-14): 사이 토/일 2쌍 + 평일 5일. holidays로 5일 중
    # 4일을 휴일 처리하면 영업일 환산 공백은 1일로 떨어진다.
    payload["equity_asia"]["rows"][0]["date"] = "2026-09-07"
    payload["equity_asia"]["rows"][1]["date"] = "2026-09-14"
    holidays = {"2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"}
    findings = validate_payload(payload, holidays=holidays)
    assert not any("거래일 공백" in f.message for f in findings)


# ---------------------------------------------------------------------------
# 5) 퍼센트 정합성 범위
# ---------------------------------------------------------------------------


def test_win_rate_out_of_0_1_is_error():
    payload = _valid_payload()
    payload["strategies"][0]["total"]["win_rate"] = 1.5
    findings = _errors(validate_payload(payload))
    assert any("strategies[0].total" in f.path for f in findings)


def test_ci_low_greater_than_ci_high_is_error():
    payload = _valid_payload()
    payload["strategies"][0]["total"]["ci_low"] = 0.9
    payload["strategies"][0]["total"]["ci_high"] = 0.1
    findings = _errors(validate_payload(payload))
    assert any("ci_low" in f.message and "ci_high" in f.message for f in findings)


def test_wins_greater_than_trips_is_error():
    payload = _valid_payload()
    payload["strategies"][0]["total"]["wins"] = 999
    findings = _errors(validate_payload(payload))
    assert any("wins" in f.path for f in findings)


def test_extreme_day_pct_is_warn_not_error():
    payload = _valid_payload()
    payload["equity_asia"]["rows"][0]["day_pct"] = 500.0  # 하루 500% — 상식 밖이지만 하드 오류는 아님
    findings = validate_payload(payload)
    assert not _errors(findings)
    assert any("day_pct" in f.path for f in _warns(findings))


# ---------------------------------------------------------------------------
# 6) enabled_count == settings.yaml 활성 전략 수
# ---------------------------------------------------------------------------


def test_enabled_count_mismatch_is_error():
    payload = _valid_payload()
    payload["enabled_count"] = 1
    strategies_cfg = {"donchian": {"enabled": True}, "gap_fade": {"enabled": True}}
    findings = _errors(validate_payload(payload, strategies_cfg=strategies_cfg))
    assert any(f.path == "enabled_count" for f in findings)


def test_enabled_count_match_is_clean():
    payload = _valid_payload()
    payload["enabled_count"] = 1
    strategies_cfg = {"donchian": {"enabled": True}, "gap_fade": {"enabled": False}}
    findings = validate_payload(payload, strategies_cfg=strategies_cfg)
    assert not any(f.path == "enabled_count" for f in findings)


def test_enabled_count_check_skipped_without_settings():
    payload = _valid_payload()
    payload["enabled_count"] = 999  # 뭐가 됐든 settings 없이는 대조 안 함
    assert validate_payload(payload) == []


# ---------------------------------------------------------------------------
# 7) 활성 전략은 strategies[]에 name_ko/name_en/help와 함께 있어야 함
# ---------------------------------------------------------------------------


def test_enabled_strategy_missing_from_list_is_warn():
    payload = _valid_payload()
    payload["enabled_count"] = 2
    strategies_cfg = {"donchian": {"enabled": True}, "gap_fade": {"enabled": True}}
    findings = validate_payload(payload, strategies_cfg=strategies_cfg)
    assert not _errors(findings)
    assert any("gap_fade" in f.path for f in _warns(findings))


def test_enabled_strategy_present_but_missing_name_ko_is_error():
    payload = _valid_payload()
    payload["strategies"][0]["name_ko"] = ""
    strategies_cfg = {"donchian": {"enabled": True}}
    findings = _errors(validate_payload(payload, strategies_cfg=strategies_cfg))
    assert any("name_ko" in f.path for f in findings)


def test_enabled_strategy_present_but_missing_help_is_warn():
    payload = _valid_payload()
    payload["strategies"][0]["help"] = None
    strategies_cfg = {"donchian": {"enabled": True}}
    findings = validate_payload(payload, strategies_cfg=strategies_cfg)
    assert not _errors(findings)
    assert any("help" in f.path for f in _warns(findings))


def test_disabled_strategy_absent_is_not_flagged():
    payload = _valid_payload()
    strategies_cfg = {"donchian": {"enabled": True}, "gap_fade": {"enabled": False}}
    findings = validate_payload(payload, strategies_cfg=strategies_cfg)
    assert findings == []


# ---------------------------------------------------------------------------
# 8) paper_epoch 있으면 전략별 start_capital 필수
# ---------------------------------------------------------------------------


def test_paper_epoch_strategy_missing_start_capital_is_error():
    payload = _valid_payload()
    pe = _valid_paper_epoch()
    del pe["strategies"][0]["start_capital"]
    payload["paper_epoch"] = pe
    findings = _errors(validate_payload(payload))
    assert any("start_capital" in f.path for f in findings)


def test_paper_epoch_strategy_empty_start_capital_is_error():
    payload = _valid_payload()
    pe = _valid_paper_epoch()
    pe["strategies"][0]["start_capital"] = {}
    payload["paper_epoch"] = pe
    findings = _errors(validate_payload(payload))
    assert any("start_capital" in f.path for f in findings)


def test_paper_epoch_negative_start_capital_is_error():
    payload = _valid_payload()
    pe = _valid_paper_epoch()
    pe["strategies"][0]["start_capital"]["KRW"] = -1000
    payload["paper_epoch"] = pe
    findings = _errors(validate_payload(payload))
    assert any("start_capital.KRW" in f.path for f in findings)


def test_empty_paper_epoch_skips_start_capital_check():
    payload = _valid_payload()
    payload["paper_epoch"] = {}
    assert validate_payload(payload) == []


# ---------------------------------------------------------------------------
# 9) paper_epoch.overall.seed_krw == 전략별 start_capital 합(반올림 오차 이내)
# ---------------------------------------------------------------------------


def test_overall_seed_mismatch_is_error():
    payload = _valid_payload()
    pe = _valid_paper_epoch()
    pe["overall"]["seed_krw"] = 1.0  # 명백히 합과 안 맞음
    payload["paper_epoch"] = pe
    findings = _errors(validate_payload(payload))
    assert any(f.path == "paper_epoch.overall.seed_krw" for f in findings)


def test_overall_seed_within_rounding_is_clean():
    payload = _valid_payload()
    pe = _valid_paper_epoch()
    pe["overall"]["seed_krw"] = round(pe["overall"]["seed_krw"], 0)  # 반올림 오차만
    payload["paper_epoch"] = pe
    findings = validate_payload(payload)
    assert not any(f.path == "paper_epoch.overall.seed_krw" for f in findings)


# ---------------------------------------------------------------------------
# 10) generated_at은 36시간 이내
# ---------------------------------------------------------------------------


def test_stale_generated_at_is_error():
    payload = _valid_payload()
    payload["generated_at"] = "2026-09-01T00:00:00+09:00"  # now 기준 훨씬 과거
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    findings = _errors(validate_payload(payload, now=now))
    assert any(f.path == "generated_at" for f in findings)


def test_fresh_generated_at_is_clean():
    now = datetime(2026, 9, 6, 12, 0, 0, tzinfo=UTC)
    payload = _valid_payload()
    payload["generated_at"] = "2026-09-06T20:00:00+09:00"  # now로부터 몇 시간 이내
    findings = validate_payload(payload, now=now)
    assert not any(f.path == "generated_at" for f in findings)


def test_non_iso_generated_at_is_error():
    payload = _valid_payload()
    payload["generated_at"] = "not-a-timestamp"
    findings = _errors(validate_payload(payload))
    assert any(f.path == "generated_at" for f in findings)


# ---------------------------------------------------------------------------
# 11) data_version(체결 왕복 수 합)은 직전 발행본보다 줄면 안 됨
# ---------------------------------------------------------------------------


def test_trade_count_decrease_vs_previous_is_error():
    previous = _valid_payload()
    current = copy.deepcopy(previous)
    current["strategies"][0]["total"]["trips"] = 10  # 이전 40 → 이번 10
    findings = _errors(validate_payload(current, previous=previous))
    assert any(f.path == "strategies" and "유실" in f.message for f in findings)


def test_trade_count_increase_vs_previous_is_clean():
    previous = _valid_payload()
    current = copy.deepcopy(previous)
    current["strategies"][0]["total"]["trips"] = 41
    current["strategies"][0]["total"]["wins"] = 22
    findings = validate_payload(current, previous=previous)
    assert not any(f.path == "strategies" for f in _errors(findings))


def test_trade_count_check_skipped_without_previous():
    payload = _valid_payload()
    payload["strategies"][0]["total"]["trips"] = 0
    payload["strategies"][0]["total"]["wins"] = 0
    assert validate_payload(payload) == []


def test_trade_count_equal_vs_previous_is_clean():
    previous = _valid_payload()
    current = copy.deepcopy(previous)
    findings = validate_payload(current, previous=previous)
    assert not any(f.path == "strategies" for f in _errors(findings))
