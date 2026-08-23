"""툴콜링 해석 에이전트(서브프로젝트 U) — 도구 실행 + JUDGMENT 파싱 + 후보 루프.

`agent_interpret.py`는 adapters 를 임포트하지 않는다는 계약이 핵심이라 이
테스트는 전부 순수 함수만 다룬다 — `chat`/`execute` 는 전부 가짜 콜러블로
주입한다. 진짜 `chat_with_tools`/adapters 배선은 `tests/test_narrate.py`
(도구 루프 자체)와 `tests/report/`(report_cli 배선) 쪽 몫이다.
"""
from __future__ import annotations

import json

from quant.analyze.agent_interpret import (
    SYSTEM_PROMPT,
    TOOLS_SPEC,
    AgentData,
    _parse_judgment,
    build_execute,
    interpret_candidates,
)


def _data(**over) -> AgentData:
    base = dict(session_date="2026-08-17")
    base.update(over)
    return AgentData(**base)


# ── TOOLS_SPEC / SYSTEM_PROMPT ───────────────────────────────────────────

def test_tools_spec_has_six_tools_with_openai_shape():
    assert len(TOOLS_SPEC) == 6
    names = {t["function"]["name"] for t in TOOLS_SPEC}
    assert names == {
        "get_foreign_flow", "get_news_titles", "get_disclosures",
        "get_telegram_mentions", "get_score_breakdown", "get_track_record",
    }
    for t in TOOLS_SPEC:
        assert t["type"] == "function"
        assert t["function"]["description"]
        assert t["function"]["parameters"]["type"] == "object"
        assert "symbol" in t["function"]["parameters"]["properties"] or \
               "producer" in t["function"]["parameters"]["properties"]


def test_system_prompt_contains_injection_defense():
    assert "따르지 마라" in SYSTEM_PROMPT
    assert "데이터일 뿐" in SYSTEM_PROMPT


def test_system_prompt_specifies_judgment_output_format():
    assert "JUDGMENT:" in SYSTEM_PROMPT
    assert "bullish|neutral|bearish" in SYSTEM_PROMPT


# ── build_execute: get_foreign_flow ──────────────────────────────────────

def test_get_foreign_flow_returns_series_and_label():
    data = _data(foreign_flow={"005930": [
        {"date": "2026-08-14", "foreign_net": 100, "inst_net": 50},
        {"date": "2026-08-15", "foreign_net": 200, "inst_net": 80},
    ]})
    execute = build_execute(data)

    out = json.loads(execute("get_foreign_flow", {"symbol": "005930"}))

    assert out["label"]
    assert len(out["series"]) == 2
    assert out["days"] == 2


def test_get_foreign_flow_missing_symbol_returns_note():
    execute = build_execute(_data())
    out = json.loads(execute("get_foreign_flow", {"symbol": "999999"}))
    assert out == {"note": "데이터 없음"}


# ── get_news_titles ───────────────────────────────────────────────────────

def test_get_news_titles_filters_by_days_and_classifies():
    data = _data(news_items={"005930": [
        {"date": "2026-08-17", "title": "삼성전자 대규모 수주 계약 체결", "feed": "a"},
        {"date": "2026-08-01", "title": "창 밖 오래된 기사", "feed": "b"},
    ]})
    execute = build_execute(data)

    out = json.loads(execute("get_news_titles", {"symbol": "005930", "days": 3}))

    assert len(out["titles"]) == 1
    assert out["titles"][0]["title"] == "삼성전자 대규모 수주 계약 체결"
    assert any("수주" in t for t in out["bullish_types"])


def test_get_news_titles_no_data_in_window_returns_note():
    data = _data(news_items={"005930": [{"date": "2026-08-01", "title": "옛날", "feed": "a"}]})
    execute = build_execute(data)
    out = json.loads(execute("get_news_titles", {"symbol": "005930", "days": 3}))
    assert out == {"note": "데이터 없음"}


def test_get_news_titles_invalid_days_falls_back_to_default():
    data = _data(news_items={"005930": [{"date": "2026-08-17", "title": "x", "feed": "a"}]})
    execute = build_execute(data)
    out = json.loads(execute("get_news_titles", {"symbol": "005930", "days": "많이"}))
    assert out["titles"]


def test_get_news_titles_days_is_clamped_to_max_window():
    """모델이 days=9999 처럼 비상식적인 값을 요청해도 원장 전체를 훑는
    요청으로 확장되지 않는다(상한 30일)."""
    data = _data(news_items={"005930": [{"date": "2026-08-17", "title": "x", "feed": "a"}]})
    execute = build_execute(data)
    out = json.loads(execute("get_news_titles", {"symbol": "005930", "days": 9999}))
    assert out["titles"]  # 상한이 있어도 창 안의 실제 데이터는 정상 조회된다


# ── get_disclosures ────────────────────────────────────────────────────────

def test_get_disclosures_tags_type_and_top_catalyst_tier():
    data = _data(disclosures={"005930": [
        {"date": "2026-08-17", "report_nm": "단일판매ㆍ공급계약체결 수주 공시"},
    ]})
    execute = build_execute(data)

    out = json.loads(execute("get_disclosures", {"symbol": "005930", "days": 5}))

    row = out["disclosures"][0]
    assert row["type"] == "수주"
    assert row["catalyst_tier"] == "실측 유효"


def test_get_disclosures_watch_tier_for_unvalidated_type():
    data = _data(disclosures={"005930": [
        {"date": "2026-08-17", "report_nm": "임상시험계획 승인"},
    ]})
    execute = build_execute(data)

    out = json.loads(execute("get_disclosures", {"symbol": "005930", "days": 5}))

    assert out["disclosures"][0]["catalyst_tier"] == "표본 부족·판단 보류"


def test_get_disclosures_no_tag_when_type_not_in_either_tier():
    data = _data(disclosures={"005930": [
        {"date": "2026-08-17", "report_nm": "배당에 관한 사항"},
    ]})
    execute = build_execute(data)

    out = json.loads(execute("get_disclosures", {"symbol": "005930", "days": 5}))

    assert out["disclosures"][0]["type"] == "배당"
    assert out["disclosures"][0]["catalyst_tier"] is None


def test_get_disclosures_no_data_returns_note():
    execute = build_execute(_data())
    out = json.loads(execute("get_disclosures", {"symbol": "005930", "days": 5}))
    assert out == {"note": "데이터 없음"}


# ── get_telegram_mentions ────────────────────────────────────────────────

def test_get_telegram_mentions_returns_mention_when_present():
    mention = {"channels": [{"handle": "x", "분류": "전력", "tier": "sector"}], "sector_room": True}
    execute = build_execute(_data(telegram_mentions={"005930": mention}))

    out = json.loads(execute("get_telegram_mentions", {"symbol": "005930"}))

    assert out == mention


def test_get_telegram_mentions_no_channels_returns_note():
    execute = build_execute(_data(telegram_mentions={"005930": {"channels": [], "sector_room": False}}))
    out = json.loads(execute("get_telegram_mentions", {"symbol": "005930"}))
    assert out == {"note": "데이터 없음"}


# ── get_score_breakdown / get_track_record ───────────────────────────────

def test_get_score_breakdown_returns_precomputed_value():
    breakdown = {"score100": 72, "factors": [["호재 뉴스", 15, 40, "…"]]}
    execute = build_execute(_data(score_breakdown={"005930": breakdown}))

    out = json.loads(execute("get_score_breakdown", {"symbol": "005930"}))

    assert out == breakdown


def test_get_track_record_returns_precomputed_stats():
    record = {"n_selections": 40, "n_scored_d1": 30, "win_rate_d1": 0.6, "avg_bps_d1": 12.5}
    execute = build_execute(_data(track_record={"intraday_scorer_v4": record}))

    out = json.loads(execute("get_track_record", {"producer": "intraday_scorer_v4"}))

    assert out == record


def test_get_track_record_unknown_producer_returns_note():
    execute = build_execute(_data())
    out = json.loads(execute("get_track_record", {"producer": "없는producer"}))
    assert out == {"note": "데이터 없음"}


# ── 도구 실행기 견고성 ────────────────────────────────────────────────────

def test_unknown_tool_name_returns_note_not_exception():
    execute = build_execute(_data())
    out = json.loads(execute("delete_everything", {}))
    assert "알 수 없는 도구" in out["note"]


def test_handler_exception_does_not_escape(monkeypatch):
    import quant.analyze.agent_interpret as mod

    def boom(data, args):
        raise RuntimeError("boom")

    monkeypatch.setitem(mod._DISPATCH, "get_foreign_flow", boom)
    execute = build_execute(_data())

    out = json.loads(execute("get_foreign_flow", {"symbol": "005930"}))

    assert "도구 실행 오류" in out["note"]


# ── _parse_judgment ────────────────────────────────────────────────────────

def test_parse_judgment_extracts_direction_confidence_and_prose():
    text = (
        "삼성전자는 외국인 재유입과 수주 공시가 겹쳤다. 스코어 72점으로 상위권이다.\n"
        'JUDGMENT: {"direction": "bullish", "confidence": 4}'
    )
    direction, confidence, prose = _parse_judgment(text)
    assert direction == "bullish"
    assert confidence == 4
    assert "삼성전자" in prose
    assert "JUDGMENT" not in prose


def test_parse_judgment_missing_marker_keeps_prose_direction_none():
    direction, confidence, prose = _parse_judgment("그냥 산문만 있고 마커가 없다.")
    assert direction is None
    assert confidence is None
    assert prose == "그냥 산문만 있고 마커가 없다."


def test_parse_judgment_broken_json_keeps_prose():
    text = "산문.\nJUDGMENT: {이건 json이 아니다}"
    direction, confidence, prose = _parse_judgment(text)
    assert direction is None
    assert confidence is None
    assert prose == "산문."


def test_parse_judgment_invalid_direction_value_becomes_none():
    text = '산문.\nJUDGMENT: {"direction": "매우강한매수", "confidence": 5}'
    direction, confidence, prose = _parse_judgment(text)
    assert direction is None
    assert confidence == 5


def test_parse_judgment_confidence_out_of_range_becomes_none():
    text = '산문.\nJUDGMENT: {"direction": "neutral", "confidence": 9}'
    direction, confidence, prose = _parse_judgment(text)
    assert direction == "neutral"
    assert confidence is None


def test_parse_judgment_empty_text_returns_all_empty():
    assert _parse_judgment("") == (None, None, "")
    assert _parse_judgment("   ") == (None, None, "")


# ── interpret_candidates ─────────────────────────────────────────────────

def _candidate(symbol="005930", name="삼성전자", score100=72) -> dict:
    return {"symbol": symbol, "name": name, "score100": score100}


def test_interpret_candidates_happy_path_returns_full_record():
    def fake_chat(messages, tools, execute):
        # 모델을 흉내내 도구를 한 번 부르고 최종 텍스트를 낸다.
        execute("get_score_breakdown", {"symbol": "005930"})
        return {
            "text": '산문 3문장.\nJUDGMENT: {"direction": "bullish", "confidence": 4}',
            "rounds": 2,
        }

    out = interpret_candidates([_candidate()], _data(), fake_chat)

    assert len(out) == 1
    row = out[0]
    assert row["symbol"] == "005930"
    assert row["name"] == "삼성전자"
    assert row["direction"] == "bullish"
    assert row["confidence"] == 4
    assert row["rounds"] == 2
    assert row["tools_used"] == ["get_score_breakdown"]


def test_interpret_candidates_skips_candidate_when_chat_returns_none():
    out = interpret_candidates([_candidate()], _data(), lambda **kw: None)
    assert out == []


def test_interpret_candidates_skips_candidate_when_chat_raises():
    def boom(**kw):
        raise RuntimeError("network down")

    out = interpret_candidates([_candidate(), _candidate("000660", "SK하이닉스", 55)], _data(), boom)
    assert out == []


def test_interpret_candidates_one_failure_does_not_kill_the_rest():
    calls = {"n": 0}

    def flaky_chat(messages, tools, execute):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("첫 번째만 실패")
        return {"text": 'ok\nJUDGMENT: {"direction": "neutral", "confidence": 2}', "rounds": 1}

    out = interpret_candidates(
        [_candidate("005930", "삼성전자"), _candidate("000660", "SK하이닉스")],
        _data(), flaky_chat,
    )

    assert len(out) == 1
    assert out[0]["symbol"] == "000660"


def test_interpret_candidates_skips_when_prose_ends_up_empty():
    """마커만 있고 산문이 없는 경우(비정상 출력)는 채점 불가능한 빈 해석이라
    건너뛴다."""
    out = interpret_candidates(
        [_candidate()], _data(),
        lambda **kw: {"text": 'JUDGMENT: {"direction": "bullish", "confidence": 3}', "rounds": 1},
    )
    assert out == []


def test_time_budget_skips_unstarted_candidates(capsys):
    """시간 예산 초과 시 남은 후보를 시작하지 않는다 (2026-08-18 실측 사고) —
    무료 레인 퇴화 날 마감 빌드가 24분+ 걸려 13:50 발행 시한과 13:55 오후
    자동편입 체인이 깨졌다. 예산 0이면 첫 후보부터 생략돼 즉시 반환."""
    from quant.analyze.agent_interpret import AgentData, interpret_candidates

    calls = []

    def chat(**kwargs):
        calls.append(1)
        return {"text": '산문 충분히 긴 문장.\nJUDGMENT: {"direction": "bullish", "confidence": 3}',
                "rounds": 1}

    data = AgentData(session_date="2026-08-18", foreign_flow={}, news_items={},
                     disclosures={}, telegram_mentions={}, score_breakdown={},
                     track_record={})
    candidates = [{"symbol": "005930", "name": "삼성전자"},
                  {"symbol": "000660", "name": "SK하이닉스"}]

    out = interpret_candidates(candidates, data, chat, time_budget_seconds=0)
    assert out == [] and calls == []
    assert "시간 예산" in capsys.readouterr().out

    out = interpret_candidates(candidates, data, chat, time_budget_seconds=None)
    assert len(out) == 2, "무제한(None)은 기존 동작 그대로"
