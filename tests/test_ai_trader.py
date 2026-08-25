"""신입사원 AI 트레이더(`quant/analyze/ai_trader.py`) — 2026-08-26 소유자 지시.

> "새로운 신입 사원 AI 트레이더가 들어왔다고 가정. 기존 회사의 아이덴티티는
> 무너지지 않되, 이 새로운 직원이 우리에게 득이 되도록."

고정하는 계약:
① **같은 서류, 같은 해시** — 신입의 judgment.input_hash 는 기존 직원
   (watch_scorer, selection_judgment)의 것과 정확히 일치한다. 어긋나면
   리더보드에서 같은 입력끼리 비교한다는 전제가 조용히 깨진다.
② **답안지 미제공** — 프롬프트에 기존 직원의 점수(baseline/ai/trending)를
   보여주지 않는다(독립 판단이어야 비교가 의미 있다).
③ **환각 차단** — 서류에 없는 심볼 픽은 버린다. 픽은 최대 5개(초과분은 점수
   낮은 쪽부터 reject 강등).
④ **결근 처리** — LLM 실패(None/파싱 불가)면 그날 판단 자체가 없다. 지어낸
   판단이 원장에 들어가는 것보다 결근이 낫다.
⑤ **멱등 적재** — natural_key 중복은 다시 쓰지 않는다(cmd_outcomes 관례).
"""
from __future__ import annotations

import json

import pytest

from quant.analyze.ai_trader import (
    append_judgments, daily_note, dossier_lines, parse_stage_json, run_debate,
    to_judgments, MAX_PICKS,
)
from quant.control.judgment import selection_judgment


def _row(symbol="005930", **over):
    r = {
        "schema": 1, "date": "2026-08-26", "market": "KR",
        "symbol": symbol, "name": "삼성전자",
        "baseline_score100": 72, "ai_score100": 68, "trending_score100": 55,
        "news_articles_today": 5, "news_streak_days": 3,
        "best_board_rank": 2, "n_boards": 2, "relative_volume": 1.8,
        "foreign_buy_streak": 4, "inst_buy_streak": 1,
        "analyst_opinion_score": 0.8, "upside_pct": 12.5,
        "close": 71000.0, "change_pct": 2.1, "origin": "news",
        "is_candidate": True,
    }
    r.update(over)
    return r


# ---------------------------------------------------------------- 서류(dossier)

def test_dossier_hides_incumbent_scores_and_verdict():
    """② 답안지 미제공 — 기존 직원의 점수·합격 여부는 프롬프트에 싣지 않는다."""
    [line] = dossier_lines([_row()])
    assert "005930" in line and "삼성전자" in line
    assert "72" not in line and "68" not in line and "55" not in line
    assert "is_candidate" not in line and "candidate" not in line
    # 원자료(뉴스량·수급·등락)는 실린다
    assert "뉴스" in line and "외인" in line


# ---------------------------------------------------------------- 파싱(환각 차단)

def test_parse_rejects_hallucinated_symbols_and_clamps():
    text = '설명 텍스트... {"picks": [' \
        '{"symbol": "005930", "score": 88, "verdict": "pass", "thesis": "수급+뉴스"},' \
        '{"symbol": "999999", "score": 95, "verdict": "pass", "thesis": "환각"},' \
        '{"symbol": "000660", "score": 150, "verdict": "reject", "thesis": "과열"}]}'
    out = parse_stage_json(text, allowed={"005930", "000660"})
    assert set(p["symbol"] for p in out) == {"005930", "000660"}, "서류에 없는 심볼은 버린다"
    assert next(p for p in out if p["symbol"] == "000660")["score"] == 100, "점수는 0~100 클램프"


def test_parse_caps_passes_at_max_picks():
    """③ 픽은 최대 MAX_PICKS — 초과분은 점수 낮은 쪽부터 reject 강등."""
    picks = [{"symbol": f"{i:06d}", "score": 50 + i, "verdict": "pass", "thesis": "t"}
             for i in range(MAX_PICKS + 3)]
    out = parse_stage_json(json.dumps({"picks": picks}),
                           allowed={p["symbol"] for p in picks})
    passes = [p for p in out if p["verdict"] == "pass"]
    assert len(passes) == MAX_PICKS
    assert min(p["score"] for p in passes) > 50 + 2, "낮은 점수부터 강등됐다"


def test_parse_returns_none_on_garbage():
    assert parse_stage_json("JSON이 아님", allowed={"005930"}) is None
    assert parse_stage_json('{"picks": "말이 안 됨"}', allowed={"005930"}) is None


# ---------------------------------------------------------------- 토론(결근 처리)

def _fake_narrate_factory(responses):
    calls = []

    def narrate(prompt):
        calls.append(prompt)
        return responses[len(calls) - 1] if len(calls) <= len(responses) else None
    return narrate, calls


def _final_json(symbol="005930", score=85):
    return json.dumps({"picks": [
        {"symbol": symbol, "score": score, "verdict": "pass", "thesis": "수급·뉴스 겹침"}]})


def test_debate_three_roles_then_final():
    """애널리스트 → 리스크 → 트레이더 3회 호출, 최종 픽과 토론 기록을 돌려준다."""
    rows = [_row()]
    narrate, calls = _fake_narrate_factory([
        '{"picks": [{"symbol": "005930", "score": 80, "verdict": "pass", "thesis": "강세론"}]}',
        '{"picks": [{"symbol": "005930", "score": 70, "verdict": "pass", "thesis": "반박 반영"}]}',
        _final_json(),
    ])
    result = run_debate(rows, narrate)
    assert result is not None
    assert len(calls) == 3
    assert result["final"][0]["symbol"] == "005930"
    assert result["final"][0]["score"] == 85
    assert "transcript" in result and len(result["transcript"]) == 3
    # 리스크 매니저(2번째 호출)는 애널리스트의 강세론을 받아 반박한다
    assert "강세론" in calls[1]


def test_debate_absent_when_llm_fails():
    """④ 어느 단계든 실패하면 결근 — 지어낸 판단을 원장에 넣지 않는다."""
    narrate, _ = _fake_narrate_factory([None])
    assert run_debate([_row()], narrate) is None
    narrate2, _ = _fake_narrate_factory(['{"picks": []}', "쓰레기", "쓰레기"])
    assert run_debate([_row()], narrate2) is None


def test_debate_absent_when_no_rows():
    narrate, calls = _fake_narrate_factory([])
    assert run_debate([], narrate) is None
    assert calls == [], "서류가 없으면 LLM 을 부르지도 않는다"


# ---------------------------------------------------------------- 판단 귀속(핵심 계약)

def test_judgment_hash_matches_incumbent_exactly():
    """① 같은 서류 → 같은 input_hash. 이게 깨지면 리더보드 비교 전제가 무너진다."""
    row = _row()
    final = [{"symbol": "005930", "score": 85, "verdict": "pass", "thesis": "논지"}]
    [j] = to_judgments(final, [row], version="1")
    incumbent = selection_judgment(row, producer_version="2")
    assert j.input_hash == incumbent.input_hash
    assert j.producer == "ai_trader"
    assert j.score == 85 and j.verdict == "pass"
    assert j.session_date == "2026-08-26" and j.market == "KR"


def test_judgments_cover_every_row_not_just_picks():
    """떨어뜨린 종목도 판단이다(selection_judgment 와 같은 원칙) — 픽에 없는
    행은 reject 로 기록된다. 전 행을 남겨야 rank IC 가 성립한다."""
    rows = [_row(), _row(symbol="000660", name="SK하이닉스")]
    final = [{"symbol": "005930", "score": 85, "verdict": "pass", "thesis": "t"}]
    js = to_judgments(final, rows, version="1")
    assert len(js) == 2
    rej = next(j for j in js if j.symbol == "000660")
    assert rej.verdict == "reject" and rej.score is None


def test_append_judgments_idempotent(tmp_path):
    """⑤ 같은 판단은 두 번 쓰지 않는다."""
    p = tmp_path / "judgments.jsonl"
    row = _row()
    final = [{"symbol": "005930", "score": 85, "verdict": "pass", "thesis": "t"}]
    js = to_judgments(final, [row], version="1")
    assert append_judgments(js, p) == 1
    assert append_judgments(js, p) == 0


# ---------------------------------------------------------------- 텔레그램 카드

def test_daily_note_shows_picks_and_probation_disclaimer():
    final = [
        {"symbol": "005930", "score": 85, "verdict": "pass", "thesis": "수급·뉴스 겹침"},
        {"symbol": "000660", "score": 30, "verdict": "reject", "thesis": "과열"},
    ]
    note = daily_note(final, market="KR", names={"005930": "삼성전자"})
    assert "삼성전자" in note and "수급·뉴스 겹침" in note
    assert "000660" not in note, "reject 는 카드에 싣지 않는다"
    assert "수습" in note
    assert "주문" in note, "제안일 뿐 주문하지 않는다는 고지가 있어야 한다"


def test_daily_note_none_when_no_picks():
    assert daily_note([{"symbol": "A", "score": 1, "verdict": "reject", "thesis": "t"}],
                      market="KR", names={}) is None


# ---------------------------------------------------------------- 2단계: 태그 소스 승격

def test_watch_line_lists_passes_for_promotion():
    """승격 스위치가 켜졌을 때 셸(ai_trader.sh)이 파싱할 마커 줄 — pass 픽만,
    무태그(확신도 엔진 best-of 관문을 그대로 통과해야 편입된다 — own_brief 와
    같은 이중 게이트)."""
    from quant.analyze.ai_trader import watch_line

    final = [
        {"symbol": "005930", "score": 85, "verdict": "pass", "thesis": "t"},
        {"symbol": "000660", "score": 70, "verdict": "pass", "thesis": "t"},
        {"symbol": "042700", "score": 30, "verdict": "reject", "thesis": "t"},
    ]
    assert watch_line(final) == "AI_WATCH: 005930 000660"
    assert watch_line([{"symbol": "A", "score": 1, "verdict": "reject", "thesis": "t"}]) is None
