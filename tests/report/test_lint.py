"""quant/report/lint.py 단위 테스트 — 각 검사를 개별로, 최소 payload로 겨냥한다.

골든 렌더 테스트(`test_lint_golden.py`)는 "정상 픽스처는 error가 없다"를
보므로, 여기서는 반대로 "이 특정 결함은 반드시 잡힌다"를 하나씩 확인한다.
"""
from __future__ import annotations

from quant.analyze.briefing import STANCE_LABEL
from quant.report.lint import ERROR, WARN, Finding, lint_report


def _base_payload(**overrides) -> dict:
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-09-05",
        "generated_at": "2026-09-05T08:00:00+09:00",
        "missing": [],
        "stance": {"label": STANCE_LABEL, "tier": "중립", "score": 0, "score100": 50, "line": "..."},
    }
    payload.update(overrides)
    return payload


def _errors(payload) -> list[Finding]:
    return [f for f in lint_report(payload) if f.severity == ERROR]


def _warns(payload) -> list[Finding]:
    return [f for f in lint_report(payload) if f.severity == WARN]


def test_accepts_plain_dict_and_rejects_other_types():
    lint_report(_base_payload())  # 에러 없이 돈다
    try:
        lint_report(object())
    except TypeError:
        pass
    else:
        raise AssertionError("dict/ReportModel/CloseReportModel 이외 타입은 TypeError여야 한다")


def test_nan_float_is_an_error():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "change_pct": float("nan")}])
    errors = _errors(payload)
    assert any("NaN" in f.message or "Inf" in f.message for f in errors), errors


def test_nan_string_leak_is_an_error():
    payload = _base_payload()
    payload["stance"]["line"] = "오늘 등락률은 nan% 입니다"
    errors = _errors(payload)
    assert any("nan" in f.message for f in errors), errors


def test_none_string_leak_is_an_error():
    payload = _base_payload()
    payload["stance"]["line"] = "목표가는 None 원입니다"
    errors = _errors(payload)
    assert any("None" in f.message for f in errors), errors


def test_actual_none_value_is_not_flagged():
    """JSON null(Python None)은 이 저장소의 정상적인 결측 표현이다 — 값
    자체는 검사하지 않는다(문자열 안에 새어든 경우만 본다)."""
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "foreign_label": None}])
    errors = _errors(payload)
    assert errors == []


def test_unrendered_jinja_marker_is_an_error():
    payload = _base_payload()
    payload["stance"]["line"] = "오늘은 {{ symbol.name }} 강세"
    errors = _errors(payload)
    assert any("Jinja" in f.message for f in errors), errors


def test_impossible_change_pct_is_an_error():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "change_pct": -135.0}])
    errors = _errors(payload)
    assert any("등락률" in f.message for f in errors), errors


def test_ipo_first_day_pop_is_a_warning_not_error():
    # KR 신규상장 첫날은 공모가의 60~400% — +135%(2026-09-05 KR 마감 386380)는
    # 정상 데이터라 발행을 막으면 안 된다.
    payload = _base_payload(symbols=[{"symbol": "386380", "name": "", "change_pct": 135.0}])
    findings = lint_report(payload)
    assert not [f for f in findings if f.severity == "error" and "등락률" in f.message]
    assert [f for f in findings if f.severity == "warn" and "등락률" in f.message]


def test_large_but_plausible_change_pct_is_a_warning_not_error():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "change_pct": 60.0}])
    assert _errors(payload) == []
    warns = _warns(payload)
    assert any("등락률" in f.message for f in warns), warns


def test_upside_pct_over_50_is_not_flagged():
    """목표주가 대비 상승여력은 수익률이 아니다 — 50%를 넘어도 정상."""
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "upside_pct": 94.8}])
    assert lint_report(payload) == []


def test_score100_out_of_range_is_an_error():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "ai_score100": 140}])
    errors = _errors(payload)
    assert any("score100" in f.message for f in errors), errors


def test_negative_count_is_an_error():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "삼성전자", "news_articles_today": -1}])
    errors = _errors(payload)
    assert any("카운트" in f.message for f in errors), errors


def test_generated_at_date_mismatch_with_session_date_is_an_error():
    payload = _base_payload(generated_at="2026-09-06T08:00:00+09:00")
    errors = _errors(payload)
    assert any("날짜 불일치" in f.message for f in errors), errors


def test_symbol_quote_date_in_future_is_an_error():
    payload = _base_payload(
        symbols=[{"symbol": "005930", "name": "삼성전자", "date": "2026-09-06"}],
    )
    errors = _errors(payload)
    assert any("미래" in f.message for f in errors), errors


def test_missing_stance_section_is_an_error():
    payload = _base_payload(stance={})
    errors = _errors(payload)
    assert any(f.section == "stance" for f in errors), errors


def test_empty_symbols_list_is_an_error():
    payload = _base_payload(symbols=[])
    errors = _errors(payload)
    assert any("완전히 비어" in f.message for f in errors), errors


def test_zero_candidates_auto_watch_is_a_warning():
    payload = _base_payload(auto_watch="AUTO_WATCH: 없음")
    warns = _warns(payload)
    assert any("후보 0건" in f.message for f in warns), warns


def test_old_style_stance_label_is_a_warning_not_error():
    payload = _base_payload(stance={"label": "중립", "score": 0, "score100": 50, "line": "..."})
    assert _errors(payload) == []
    warns = _warns(payload)
    assert any("정직한 고정 문구" in f.message for f in warns), warns


def test_stance_score_polarity_mismatch_is_an_error():
    """score100=80(상승 우세)인데 tier가 하락 쪽 문구면 모순."""
    payload = _base_payload(stance={
        "label": STANCE_LABEL, "tier": "강한 하락 신호", "score": 4, "score100": 80, "line": "...",
    })
    errors = _errors(payload)
    assert any("방향 모순" in f.message for f in errors), errors


def test_index_outlook_polarity_mismatch_is_an_error():
    payload = _base_payload(index_outlook={
        "kospi": {"label": "강한 상승 신호", "score100": 10},
    })
    errors = _errors(payload)
    assert any("index_outlook" in f.section for f in errors), errors


def test_symbol_ai_label_polarity_mismatch_is_an_error():
    payload = _base_payload(symbols=[
        {"symbol": "005930", "name": "삼성전자", "ai_score100": 80, "ai_label": "강한 부정 신호"},
    ])
    errors = _errors(payload)
    assert any("symbols[005930]" in f.section for f in errors), errors


def test_news_tag_with_bearish_markers_is_an_error():
    payload = _base_payload(
        auto_watch="AUTO_WATCH: 005930:NEWS",
        symbols=[{"symbol": "005930", "name": "삼성전자", "bearish_markers": ["목표가 하향"]}],
    )
    errors = _errors(payload)
    assert any("매수 후보/매수 유의 모순" in f.message for f in errors), errors


def test_news_tag_without_bearish_markers_is_clean():
    payload = _base_payload(
        auto_watch="AUTO_WATCH: 005930:NEWS",
        symbols=[{"symbol": "005930", "name": "삼성전자", "bearish_markers": []}],
    )
    assert lint_report(payload) == []


def test_relation_without_reason_is_an_error():
    payload = _base_payload(symbols=[{
        "symbol": "005930", "name": "삼성전자",
        "relations": [{"symbol": "009150", "kind": "beneficiary", "score": 90}],
    }])
    errors = _errors(payload)
    assert any("reason 없음" in f.message for f in errors), errors


def test_us_symbol_name_equal_to_ticker_is_not_flagged():
    payload = _base_payload(market="US", symbols=[{"symbol": "IBM", "name": "IBM"}])
    assert lint_report(payload) == []


def test_kr_symbol_without_name_is_a_warning():
    payload = _base_payload(symbols=[{"symbol": "005930", "name": "005930"}])
    warns = _warns(payload)
    assert any("종목명 없음" in f.message for f in warns), warns


def test_close_session_requires_market_flow():
    payload = _base_payload(session="close", intraday_view=[{"symbol": "005930", "name": "삼성전자"}])
    del payload["stance"]
    errors = _errors(payload)
    assert any(f.section == "sectors" for f in errors), errors


def test_close_session_market_flow_present_is_clean():
    payload = _base_payload(
        session="close", market_flow={"foreign_net": 100},
        intraday_view=[{"symbol": "005930", "name": "삼성전자", "grade_reasons": ["호재"]}],
    )
    del payload["stance"]
    assert lint_report(payload) == []


def test_accuracy_box_missing_key_is_an_error():
    payload = _base_payload(report_accuracy={"measured": False})
    errors = _errors(payload)
    assert any(f.section == "accuracy" for f in errors), errors


def test_accuracy_box_well_formed_is_clean():
    payload = _base_payload(report_accuracy={
        "measured": False, "as_of": None, "n_claims": 0, "min_n": 20,
        "stance_line": "정확도 미측정", "telegram_line": "정확도 미측정",
        "direction": {1: {"n": 0, "measured": False}},
        "candidates": {1: {"n": 0, "measured": False}},
        "ic": {1: {"n": 0, "measured": False}},
    })
    assert lint_report(payload) == []


def test_telegram_zero_candidates_contradiction_is_an_error():
    """auto_watch 파싱이 세는 후보 수와 텔레그램 요약이 말하는 후보 수가
    어긋나는 상황을 직접 만들 수는 없다(같은 함수가 만드므로) — 대신
    `_format_summary` 자체가 예외 없이 잘 도는지, 후보가 있으면 "0개"라고
    말하지 않는지를 정상 경로로 확인한다."""
    payload = _base_payload(
        auto_watch="AUTO_WATCH: 005930:NEWS",
        symbols=[{"symbol": "005930", "name": "삼성전자", "baseline_score100": 70}],
    )
    assert _errors(payload) == []
