"""`selections.build_rows` — 엔진 페이로드 심볼 속성 → 선정 원장 행.

`_attributes()` 는 페이로드 심볼 dict 를 통째로 펼치지 않고 **명시적 허용목록**으로
읽는다(`.get()`). 그래서 `machine_payload` 가 새 필드를 붙여도 이 허용목록에 없으면
원장까지 흘러가지 않는다 — 스키마를 추측하지 않고 직접 확인해야 하는 이유
(judgment.py 상단 주석의 2026-08-14 실측 버그와 같은 종류의 함정).
"""
from __future__ import annotations

from quant.control.selections import append, build_rows, load


def _payload(**sym_over) -> dict:
    sym = {"symbol": "005930", "name": "삼성전자", "close": 71_000.0}
    sym.update(sym_over)
    return {
        "session_date": "2026-08-15", "market": "KR",
        "symbols": [sym], "stance": {}, "features": {},
    }


def test_baseline_score100_flows_from_payload_symbol_to_the_row():
    """§E-2 — judgment v2 는 이 필드가 원장에 있어야 성립한다. 없으면
    selection_judgment 가 영원히 trending_score100 폴백만 타게 된다."""
    rows = build_rows(_payload(baseline_score100=62), candidate_symbols=set())

    assert rows[0]["baseline_score100"] == 62


def test_baseline_score100_absent_in_payload_is_none_in_the_row():
    """채점 불가 심볼(payload 에 키 자체가 없음) → 원장에도 None(0 위장 금지)."""
    rows = build_rows(_payload(), candidate_symbols=set())

    assert rows[0]["baseline_score100"] is None


def test_baseline_score100_zero_survives_the_row_build():
    """0 은 유효한 점수다 — 허용목록 매핑이 `sym.get(...) or None` 처럼 truthy 로
    걸러버리면 0 점이 조용히 None 으로 바뀐다."""
    rows = build_rows(_payload(baseline_score100=0), candidate_symbols=set())

    assert rows[0]["baseline_score100"] == 0


# ── news_z (H-2 Task 4) ─────────────────────────────────────────────────

def test_news_z_present_when_computable():
    rows = build_rows(_payload(), candidate_symbols=set(),
                      news_z_by_symbol={"005930": 1.23})

    assert rows[0]["news_z"] == 1.23


def test_news_z_key_is_omitted_when_not_computable():
    """`baseline_score100` 과 달리 news_z 는 계산 불가면 **키 자체가 없다** —
    None 을 남기지 않는다(엔진 페이로드 필드가 아니라 원장 밖에서 얹는 값)."""
    rows = build_rows(_payload(), candidate_symbols=set(), news_z_by_symbol={})

    assert "news_z" not in rows[0]


def test_news_z_key_is_omitted_when_argument_not_given():
    """하위호환 — `news_z_by_symbol` 을 아예 안 줘도 기존 호출부가 그대로 동작한다."""
    rows = build_rows(_payload(), candidate_symbols=set())

    assert "news_z" not in rows[0]


def test_news_z_only_applies_to_the_matching_symbol():
    rows = build_rows(_payload(), candidate_symbols=set(),
                      news_z_by_symbol={"000660": 2.0})

    assert "news_z" not in rows[0]


# ── producer (K, 단타 스코어러 — 별도 producer 로 원장 기록) ────────────────

def test_producer_absent_by_default_is_backward_compatible():
    """`producer` 인자를 안 주면 행에 `producer` 키 자체가 없다 — 기존
    리포트 본선 호출부(`report_cli._record_selections`)가 바이트 단위로
    하위호환이다."""
    rows = build_rows(_payload(), candidate_symbols=set())

    assert "producer" not in rows[0]


def test_producer_present_when_given():
    rows = build_rows(_payload(), candidate_symbols=set(), producer="intraday_scorer")

    assert rows[0]["producer"] == "intraday_scorer"


def test_append_natural_key_distinguishes_producers(tmp_path):
    """같은 (날짜,시장,종목)이라도 producer가 다르면 서로 다른 행으로
    남는다 — 한쪽이 다른 쪽을 '중복'으로 가려 스킵되면 그 producer의 그날
    표본이 통째로 사라진다(K, 단타 스코어러가 이 계약에 의존)."""
    path = tmp_path / "selections.jsonl"
    report_row = {"date": "2026-08-17", "market": "KR", "symbol": "005930", "close": 71_000.0}
    intraday_row = {
        "date": "2026-08-17", "market": "KR", "symbol": "005930",
        "producer": "intraday_scorer", "close": 71_500.0, "score100": 72,
    }

    added1 = append([report_row], path)
    added2 = append([intraday_row], path)

    assert added1 == 1
    assert added2 == 1
    rows = load(path)
    assert len(rows) == 2
    assert {r.get("producer") for r in rows} == {None, "intraday_scorer"}


def test_append_same_producer_still_dedupes(tmp_path):
    """producer가 같으면 기존 계약(같은 날짜·시장·종목은 중복 스킵)이 그대로다."""
    path = tmp_path / "selections.jsonl"
    row = {"date": "2026-08-17", "market": "KR", "symbol": "005930",
           "producer": "intraday_scorer", "close": 71_000.0}

    added1 = append([dict(row)], path)
    added2 = append([dict(row)], path)

    assert added1 == 1
    assert added2 == 0
    assert len(load(path)) == 1
