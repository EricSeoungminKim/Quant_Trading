"""quant.report.prose_check — 합성 payload로 (a)~(e) 각 검사의 양성/음성을 확인한다.

실제 아카이브 사례(2026-08-25 KR 373220 LG에너지솔루션 — `기관 -1.2조 순매도`
문장이 같은 날 institution_net_100m_krw=+11708과 부호가 반대)를 direction
음성/양성 테스트에 그대로 반영했다 — `results/report_prose_audit/SUMMARY.md`
참고.
"""
from __future__ import annotations

from quant.report.prose_check import (
    ERROR,
    WARN,
    check_prose,
    redact_prose,
    split_sentences,
)


def _base_payload(**overrides) -> dict:
    payload = {
        "schema": 1,
        "market": "KR",
        "session_date": "2026-08-25",
        "features": {
            "kospi_change_pct": -3.124,
            "kosdaq_change_pct": 1.42,
            "foreign_net_100m_krw": -38136,
            "institution_net_100m_krw": 11708,
        },
        "stance": {"label": "하락 신호", "tier": "방어", "score100": 30},
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "change_pct": 2.3},
        ],
    }
    payload.update(overrides)
    return payload


# ──────────────────────────────────────────────────────────── 문장 분리

def test_split_sentences_basic():
    text = "코스피가 상승했다. 외국인이 순매수했다."
    assert split_sentences(text) == ["코스피가 상승했다.", "외국인이 순매수했다."]


def test_split_sentences_empty():
    assert split_sentences("") == []
    assert split_sentences(None) == []


# ──────────────────────────────────────────────────────────── (a) 숫자 근거

def test_unsupported_number_flagged():
    payload = _base_payload(stance_prose="오늘 코스피는 12.7% 폭등했다.")
    findings = check_prose(payload)
    assert any(f.severity == WARN and "unsupported_number" in f.message for f in findings)


def test_supported_number_not_flagged():
    payload = _base_payload(stance_prose="오늘 코스피는 -3.12% 하락했다.")
    findings = check_prose(payload)
    assert not any("unsupported_number" in f.message for f in findings)


def test_number_within_rounding_tolerance_not_flagged():
    # -3.124 -> 문장에서 -3.1 로 반올림 인용(허용 오차 내)
    payload = _base_payload(stance_prose="코스피는 -3.1% 내렸다.")
    findings = check_prose(payload)
    assert not any("unsupported_number" in f.message for f in findings)


# ──────────────────────────────────────────────────────────── (b) 종목/개체

def test_hallucinated_kr_code_flagged():
    payload = _base_payload(stance_prose="오늘 999999 종목이 강세를 보였다.")
    findings = check_prose(payload)
    assert any(f.severity == ERROR and "hallucinated_entity" in f.message for f in findings)


def test_known_kr_code_not_flagged():
    payload = _base_payload(stance_prose="오늘 005930 종목이 순매수를 받았다.")
    findings = check_prose(payload)
    assert not any("hallucinated_entity" in f.message for f in findings)


def test_us_ticker_mentions_are_not_flagged():
    """US 티커(대문자 2~5글자) 정규식으로 개체 환각을 잡으려는 시도는 뺐다 —
    아카이브 감사(2026-09-07)에서 217건 전부가 금융 약어(NFCI 등)나 실존
    비교종목(NVDA 등) 오탐이었다. KR 6자리 코드만 검사한다(모듈 docstring
    §b 참고) — US 시장 문장은 이 카테고리에서 아예 걸리지 않아야 한다."""
    payload = _base_payload(
        market="US",
        symbols=[{"symbol": "AAPL", "name": "Apple", "change_pct": 1.0}],
        stance_prose="CEO는 GDP 둔화 우려 속에 NVDA·AVGO 등 반도체 대형주를 언급했다.",
    )
    findings = check_prose(payload)
    assert not any("hallucinated_entity" in f.message for f in findings)


# ──────────────────────────────────────────────────────────── (c) 방향 모순

def test_midterm_direction_contradiction_matches_real_archive_bug():
    """2026-08-25 실측: institution_net_100m_krw=+11708(순매수)인데 산문은
    '기관 -1.2조 순매도'라고 썼다."""
    payload = _base_payload(
        midterm_watch=[{
            "symbol": "373220",
            "name": "LG에너지솔루션",
            "prose": "오늘 기관 순매도가 이어지며 주가가 눌렸다.",
        }],
    )
    findings = check_prose(payload)
    assert any(
        f.severity == ERROR and "direction_contradiction" in f.message
        and "midterm_watch[373220]" in f.section
        for f in findings
    )


def test_midterm_direction_matching_flow_not_flagged():
    payload = _base_payload(
        midterm_watch=[{
            "symbol": "373220",
            "name": "LG에너지솔루션",
            "prose": "오늘 기관 순매수가 이어지며 주가를 지지했다.",
        }],
    )
    findings = check_prose(payload)
    assert not any(
        "direction_contradiction" in f.message and "midterm_watch[373220]" in f.section
        for f in findings
    )


def test_agent_interpret_direction_contradiction_vs_change_pct():
    payload = _base_payload(
        agent_interpret_view=[{
            "symbol": "005930", "name": "삼성전자", "direction": "bullish",
            "prose": "삼성전자는 오늘 급락하며 약세를 보였다.",
        }],
    )
    findings = check_prose(payload)
    assert any(
        f.severity == ERROR and "direction_contradiction" in f.message
        and "agent_interpret_view[005930]" in f.section
        for f in findings
    )


def test_agent_interpret_direction_matching_change_pct_not_flagged():
    payload = _base_payload(
        agent_interpret_view=[{
            "symbol": "005930", "name": "삼성전자", "direction": "bullish",
            "prose": "삼성전자는 오늘 상승세를 이어갔다.",
        }],
    )
    findings = check_prose(payload)
    assert not any(
        "direction_contradiction" in f.message and "agent_interpret_view[005930]" in f.section
        for f in findings
    )


# ──────────────────────────────────────────────────────────── (d) 인과 근거

def test_unsupported_causality_flagged():
    payload = _base_payload(
        stance_prose="시장은 반도체 슈퍼사이클 기대감 때문에 강하게 반등할 것이다.",
    )
    findings = check_prose(payload)
    assert any(f.severity == WARN and "unsupported_causality" in f.message for f in findings)


def test_causality_with_number_not_flagged():
    payload = _base_payload(
        stance_prose="코스피는 외국인 순매도 -38136(억원) 영향으로 하락했다.",
    )
    findings = check_prose(payload)
    assert not any("unsupported_causality" in f.message for f in findings)


def test_causality_with_matching_evidence_text_not_flagged():
    payload = _base_payload(
        midterm_watch=[{
            "symbol": "373220",
            "name": "LG에너지솔루션",
            "reasons": ["외국인 연속 순매수 4일(>=2일)"],
            "prose": "외국인 연속 순매수 영향으로 주가가 견조했다.",
        }],
    )
    findings = check_prose(payload)
    assert not any(
        "unsupported_causality" in f.message and "midterm_watch[373220]" in f.section
        for f in findings
    )


# ──────────────────────────────────────────────────────────── (e) 스탠스 모순

def test_stance_contradiction_flagged():
    # tier=방어(하락 신호)인데 낙관적 어조
    payload = _base_payload(stance_prose="지금은 낙관적으로 매수 우위 대응이 유효하다.")
    findings = check_prose(payload)
    assert any(f.severity == ERROR and "contradicts_stance" in f.message for f in findings)


def test_stance_matching_tone_not_flagged():
    payload = _base_payload(stance_prose="지금은 방어적으로 보수적으로 대응할 시점이다.")
    findings = check_prose(payload)
    assert not any("contradicts_stance" in f.message for f in findings)


# ──────────────────────────────────────────────────────────── 블록 커버리지

def test_exec_summary_and_digest_prose_and_section_advice_are_checked_when_present():
    """engine.json에는 아직 이 키들이 없지만(2026-09-07 감사 확인), payload에
    실리는 순간 check_prose가 바로 검사한다 — 향후 배선을 위한 회귀 방지."""
    payload = _base_payload(
        exec_summary={"market": "코스피가 12.7% 폭등했다.", "flow": "정상", "catalyst": "정상"},
        digest_prose={"domestic_prose": "코스피가 12.7% 폭등했다.", "us_prose": "정상"},
        section_advice={"supply": "코스피가 12.7% 폭등했다."},
    )
    findings = check_prose(payload)
    sections = {f.section for f in findings if "unsupported_number" in f.message}
    assert "exec_summary.market" in sections
    assert "digest_prose.domestic_prose" in sections
    assert "section_advice.supply" in sections


def test_money_flow_and_holiday_synthesis_prose_checked():
    payload = _base_payload(
        money_flow={"prose": "오늘 유가는 500% 폭등했다."},
        holiday_synthesis={"prose": "휴장 기간 코스피는 500% 상승했다."},
    )
    findings = check_prose(payload)
    sections = {f.section for f in findings if "unsupported_number" in f.message}
    assert "money_flow.prose" in sections
    assert "holiday_synthesis.prose" in sections


def test_empty_payload_returns_no_findings():
    assert check_prose({}) == []


def test_none_prose_fields_do_not_crash():
    payload = _base_payload(
        money_flow={"prose": None}, exec_summary=None, digest_prose=None,
        section_advice=None, holiday_synthesis=None, agent_interpret_view=None,
        midterm_watch=None,
    )
    assert check_prose(payload) == []


# ──────────────────────────────────────────────────────────── redact_prose

def test_redact_prose_replaces_only_error_sentences():
    text = "오늘 기관 순매도가 이어지며 주가가 눌렸다. 외국인 수급은 양호했다."
    payload = _base_payload()
    new_text, findings = redact_prose(
        "midterm_watch[373220]", text, payload,
        own_symbol="373220",
        institution_net=payload["features"]["institution_net_100m_krw"],
    )
    assert "근거 부족으로 생략" in new_text
    assert "외국인 수급은 양호했다." in new_text
    assert "기관 순매도가 이어지며" not in new_text
    assert any(f.severity == ERROR for f in findings)


def test_redact_prose_leaves_clean_text_untouched():
    text = "오늘 기관 순매수가 이어지며 주가를 지지했다."
    payload = _base_payload()
    new_text, findings = redact_prose(
        "midterm_watch[373220]", text, payload,
        own_symbol="373220",
        institution_net=payload["features"]["institution_net_100m_krw"],
    )
    assert new_text == text
    assert findings == []


def test_redact_prose_all_bad_sentences_returns_none():
    text = "오늘 기관 순매도가 이어졌다."
    payload = _base_payload()
    new_text, findings = redact_prose(
        "midterm_watch[373220]", text, payload,
        own_symbol="373220",
        institution_net=payload["features"]["institution_net_100m_krw"],
    )
    assert new_text is None
    assert any(f.severity == ERROR for f in findings)


def test_redact_prose_empty_text_passthrough():
    payload = _base_payload()
    assert redact_prose("x", None, payload) == (None, [])
    assert redact_prose("x", "", payload) == ("", [])
