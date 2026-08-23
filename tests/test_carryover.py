"""`quant/analyze/carryover.py` 단위 테스트 — 개장일 집계 병합(서브프로젝트 G Task 3).

케이스는 계획서 Task 3 Step 1 (a)~(e)를 따른다. 배선 테스트((f) report_cli 경로
병합 확인, (g) selections 원장에 캐리 심볼 미포함)는
`tests/report/test_report_build_quotes.py` 에 있다.
"""
from __future__ import annotations

from datetime import date

from quant.analyze.carryover import merge_carryover


def _sym(symbol: str, **kw) -> dict:
    return {"symbol": symbol, "name": symbol, **kw}


# ── (a) 오늘 심볼 우선 ──────────────────────────────────────────────────

def test_today_symbol_wins_over_prior_same_symbol():
    payload = {"symbols": [_sym("005930", trending_score100=10)]}
    prior = [(date(2026, 8, 15), {"symbols": [_sym("005930", trending_score100=99)]})]

    out = merge_carryover(payload, prior)

    entries = {s["symbol"]: s for s in out["symbols"]}
    assert entries["005930"]["trending_score100"] == 10
    assert "carried_from" not in entries["005930"]
    assert len(out["symbols"]) == 1


# ── (b) 이전 여러 날 중 최신일 채택 ─────────────────────────────────────

def test_missing_symbol_carried_from_the_most_recent_prior_day():
    payload = {"symbols": []}
    prior = [
        (date(2026, 8, 15), {"symbols": [_sym("000660", trending_score100=1)]}),
        (date(2026, 8, 16), {"symbols": [_sym("000660", trending_score100=2)]}),
    ]

    out = merge_carryover(payload, prior)

    entries = {s["symbol"]: s for s in out["symbols"]}
    assert entries["000660"]["trending_score100"] == 2  # 최신(08-16) 값
    assert entries["000660"]["carried_from"] == "2026-08-16"


# ── (c) carried_from 부여 — 오늘 것엔 안 붙고 캐리한 것에만 붙는다 ──────

def test_carried_from_is_set_only_on_carried_entries():
    payload = {"symbols": [_sym("005930")]}
    prior = [(date(2026, 8, 15), {"symbols": [_sym("000660")]})]

    out = merge_carryover(payload, prior)

    entries = {s["symbol"]: s for s in out["symbols"]}
    assert "carried_from" not in entries["005930"]
    assert entries["000660"]["carried_from"] == "2026-08-15"


# ── (d) 원본 dict/list 는 변경하지 않는다 ────────────────────────────────

def test_inputs_are_not_mutated():
    payload = {"symbols": [_sym("005930")], "auto_watch": "AUTO_WATCH: 005930:NEWS"}
    prior_payload = {"symbols": [_sym("000660")], "auto_watch": "AUTO_WATCH: 000660:RANK"}
    prior = [(date(2026, 8, 15), prior_payload)]

    payload_snapshot = {"symbols": [dict(payload["symbols"][0])],
                         "auto_watch": payload["auto_watch"]}
    prior_snapshot = {"symbols": [dict(prior_payload["symbols"][0])],
                       "auto_watch": prior_payload["auto_watch"]}

    merge_carryover(payload, prior)

    assert payload == payload_snapshot
    assert prior_payload == prior_snapshot
    assert "carried_from" not in prior_payload["symbols"][0]


# ── (e) 빈 prior → 동일 payload ──────────────────────────────────────────

def test_empty_prior_returns_an_equal_payload():
    payload = {"symbols": [_sym("005930")], "auto_watch": "AUTO_WATCH: 005930:NEWS"}

    out = merge_carryover(payload, [])

    assert out == payload
    assert out is not payload


# ── 두 키(symbols/auto_watch) 다 없으면 그대로 반환(passthrough) ────────

def test_missing_keys_pass_through_unchanged():
    payload = {"market": "KR", "session_date": "2026-08-17"}

    out = merge_carryover(payload, [(date(2026, 8, 15), {"symbols": [_sym("000660")]})])

    assert out == payload


# ── auto_watch 문자열 병합 ────────────────────────────────────────────────

def test_auto_watch_appends_prior_symbols_not_present_today():
    payload = {"auto_watch": "AUTO_WATCH: 005930:NEWS"}
    prior = [(date(2026, 8, 15), {"auto_watch": "AUTO_WATCH: 000660:RANK 005930:STREAK"})]

    out = merge_carryover(payload, prior)

    # 005930 은 오늘에도 있으므로 중복 추가되지 않는다(dedup).
    assert out["auto_watch"] == "AUTO_WATCH: 005930:NEWS 000660:RANK"


def test_auto_watch_dedups_symbol_across_multiple_prior_days_keeping_latest():
    payload = {"auto_watch": "AUTO_WATCH: 005930:NEWS"}
    prior = [
        (date(2026, 8, 15), {"auto_watch": "AUTO_WATCH: 000660:RANK"}),
        (date(2026, 8, 16), {"auto_watch": "AUTO_WATCH: 000660:RANK+STREAK"}),
    ]

    out = merge_carryover(payload, prior)

    assert out["auto_watch"] == "AUTO_WATCH: 005930:NEWS 000660:RANK+STREAK"


def test_auto_watch_keeps_todays_token_order_first():
    payload = {"auto_watch": "AUTO_WATCH: 005930:NEWS 000660:RANK"}
    prior = [(date(2026, 8, 15), {"auto_watch": "AUTO_WATCH: 373220:EVENT"})]

    out = merge_carryover(payload, prior)

    assert out["auto_watch"] == "AUTO_WATCH: 005930:NEWS 000660:RANK 373220:EVENT"
