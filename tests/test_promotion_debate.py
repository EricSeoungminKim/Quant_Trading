"""승격 토론(`quant/analyze/promotion_debate.py`) — 2026-09-02 신규.

고정하는 계약:
① **환각 차단** — 서류에 없는 심볼 판정은 버린다.
② **결근 처리** — 어느 단계든 LLM 실패/파싱 불가면 그날 판단이 없다.
③ **전 종목 기록** — Judge 가 언급하지 않은 종목도 "유지"로 안전하게 기록한다.
④ **멱등 적재** — 같은 (date, market, symbol) 은 다시 쓰지 않는다.
⑤ **관심종목 불변** — 이 모듈은 어디에도 워치리스트를 쓰지 않는다(기록만).
"""
from __future__ import annotations

import json

import pytest

from quant.analyze.promotion_debate import (
    append_ledger, dossier_lines, notify_text, parse_verdicts, run_debate, to_records,
)


def _item(symbol="005930", score=72, **over):
    d = {
        "symbol": symbol, "score": score, "eff_threshold": 50, "profile": "TREND",
        "breakdown": [("5일 수익률", 25, 25, "+3.2%"), ("상대 거래량", 15, 25, "RVOL 1.2")],
        "reasons": ["[TREND] 5일 수익률 +3.2% (+25)"],
    }
    d.update(over)
    return d


# ---------------------------------------------------------------- 서류(dossier)

def test_dossier_shows_score_breakdown_not_strategy_code():
    [line] = dossier_lines([_item()])
    assert "005930" in line and "72/100" in line
    assert "5일 수익률" in line


# ---------------------------------------------------------------- 파싱(환각 차단)

def test_parse_rejects_hallucinated_symbols_and_normalizes_bad_verdict():
    text = ('앞뒤 산문... {"verdicts": ['
            '{"symbol": "005930", "verdict": "보류", "reason": "추격 우려"},'
            '{"symbol": "999999", "verdict": "보류", "reason": "환각"},'
            '{"symbol": "000660", "verdict": "모르겠음", "reason": "애매"}]}')
    out = parse_verdicts(text, allowed={"005930", "000660"})
    assert {v["symbol"] for v in out} == {"005930", "000660"}
    assert next(v for v in out if v["symbol"] == "000660")["verdict"] == "유지", \
        "알 수 없는 판정 문자열은 안전측(유지)으로 낮춘다"


def test_parse_returns_none_on_garbage():
    assert parse_verdicts("JSON 아님", allowed={"005930"}) is None
    assert parse_verdicts('{"verdicts": "말이 안 됨"}', allowed={"005930"}) is None
    assert parse_verdicts(None, allowed={"005930"}) is None


# ---------------------------------------------------------------- 토론(결근 처리)

def _fake_narrate_factory(responses):
    calls = []

    def narrate(prompt):
        calls.append(prompt)
        return responses[len(calls) - 1] if len(calls) <= len(responses) else None
    return narrate, calls


def _verdict_json(symbol="005930", verdict="유지", reason="근거"):
    return json.dumps({"verdicts": [{"symbol": symbol, "verdict": verdict, "reason": reason}]})


def test_debate_bull_bear_judge_three_calls():
    narrate, calls = _fake_narrate_factory([
        _verdict_json(verdict="유지", reason="점수 구성 탄탄"),
        _verdict_json(verdict="보류", reason="추격 위험"),
        _verdict_json(verdict="보류", reason="종합 보류"),
    ])
    result = run_debate([_item()], "오늘 시황 요약", narrate)
    assert result is not None
    assert len(calls) == 3
    assert result["final"][0] == {"symbol": "005930", "verdict": "보류", "reason": "종합 보류"}
    assert len(result["transcript"]) == 3
    # Bear(2번째 호출)는 Bull 의 초안을 받는다
    assert "유지" in calls[1] or "005930" in calls[1]


def test_debate_absent_when_any_stage_fails():
    narrate, _ = _fake_narrate_factory([None])
    assert run_debate([_item()], "요약", narrate) is None
    narrate2, _ = _fake_narrate_factory([_verdict_json(), "쓰레기", "쓰레기"])
    assert run_debate([_item()], "요약", narrate2) is None


def test_debate_absent_when_no_items():
    narrate, calls = _fake_narrate_factory([])
    assert run_debate([], "요약", narrate) is None
    assert calls == [], "서류가 없으면 LLM 을 부르지도 않는다"


# ---------------------------------------------------------------- 판단 귀속

def test_to_records_keeps_every_item_defaulting_to_keep():
    """③ Judge 가 언급 안 한 종목도 안전하게 '유지'로 기록된다."""
    items = [_item("005930"), _item("000660")]
    final = [{"symbol": "005930", "verdict": "보류", "reason": "위험"}]
    records = to_records(final, items, "KR", "2026-09-02")
    assert len(records) == 2
    by_symbol = {r["symbol"]: r for r in records}
    assert by_symbol["005930"]["verdict"] == "보류"
    assert by_symbol["000660"]["verdict"] == "유지"
    assert "토론 결과에 항목 없음" in by_symbol["000660"]["reason"]


def test_append_ledger_idempotent(tmp_path):
    path = tmp_path / "debate.jsonl"
    records = to_records(
        [{"symbol": "005930", "verdict": "보류", "reason": "위험"}],
        [_item("005930")], "KR", "2026-09-02",
    )
    added1 = append_ledger(records, path)
    added2 = append_ledger(records, path)
    assert added1 == 1
    assert added2 == 0, "같은 (date, market, symbol) 은 다시 쓰지 않는다"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


# ---------------------------------------------------------------- 알림 카드

def test_notify_text_none_when_no_records():
    assert notify_text([], "KR") is None


def test_notify_text_flags_hold_verdicts():
    records = to_records(
        [{"symbol": "005930", "verdict": "보류", "reason": "추격 위험"}],
        [_item("005930")], "KR", "2026-09-02",
    )
    text = notify_text(records, "KR", {"005930": "삼성전자"})
    assert "삼성전자" in text and "보류" in text
    assert "제거하지 않는다" in text
