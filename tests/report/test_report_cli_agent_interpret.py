"""AI 심층 해석(서브프로젝트 U) — report_cli 배선.

`_emit`(전체 파이프라인)을 통째로 태우지 않는다 — 다른 `_build_*` 배선
테스트(`test_report_cli_intraday.py` 등)와 같은 관례로 헬퍼만 단위 검증
한다. 도구 실행/JUDGMENT 파싱 자체는 `tests/test_agent_interpret.py`,
HTTP 재시도는 `tests/test_narrate.py`의 몫이다 — 여긴 "리포트가 이미 아는
데이터에서 AgentData가 올바르게 조립되고, LLM 이 없거나 실패해도 빌드가
안 죽는가"만 본다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quant.apps import report_cli
from quant.report.collect import core as report_core
from quant.report.collect import midterm as report_midterm
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.control import selections

KST = ZoneInfo("Asia/Seoul")


def _set_key(monkeypatch, value) -> None:
    import quant.adapters.env as env_mod

    monkeypatch.setattr(env_mod, "get_key", lambda name: value)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── _build_agent_foreign_flow ────────────────────────────────────────────

def test_build_agent_foreign_flow_reads_series_per_symbol(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", [
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 100, "inst_net": 50},
        {"date": "2026-08-15", "symbol": "005930", "foreign_net": 200, "inst_net": 80},
    ])

    out = report_cli._build_agent_foreign_flow(tmp_path, {"005930", "000660"})

    assert len(out["005930"]) == 2
    assert out["000660"] == []


# ── _build_agent_news_items ──────────────────────────────────────────────

def test_build_agent_news_items_filters_symbol_and_window(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "mentions.jsonl", [
        {"date": "2026-08-17", "symbol": "005930", "title": "오늘 기사", "feed": "a"},
        {"date": "2026-07-01", "symbol": "005930", "title": "너무 옛날", "feed": "a"},
        {"date": "2026-08-17", "symbol": "000660", "title": "다른 종목", "feed": "a"},
    ])

    out = report_cli._build_agent_news_items(tmp_path, {"005930"}, date(2026, 8, 17), days=30)

    assert [it["title"] for it in out["005930"]] == ["오늘 기사"]
    assert "000660" not in out


def test_build_agent_news_items_missing_ledger_returns_empty(tmp_path):
    assert report_cli._build_agent_news_items(tmp_path, {"005930"}, date(2026, 8, 17)) == {}


# ── _build_agent_disclosures ─────────────────────────────────────────────

def test_build_agent_disclosures_converts_rcept_dt_and_filters(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "disclosures.jsonl", [
        {"rcept_no": "1", "stock_code": "005930", "report_nm": "수주 공시", "rcept_dt": "20260817"},
        {"rcept_no": "2", "stock_code": "005930", "report_nm": "너무 옛날", "rcept_dt": "20260101"},
        {"rcept_no": "3", "stock_code": "000660", "report_nm": "다른 종목", "rcept_dt": "20260817"},
    ])

    out = report_cli._build_agent_disclosures(tmp_path, {"005930"}, date(2026, 8, 17), days=30)

    assert out["005930"] == [{"date": "2026-08-17", "report_nm": "수주 공시"}]
    assert "000660" not in out


def test_build_agent_disclosures_missing_ledger_returns_empty(tmp_path):
    assert report_cli._build_agent_disclosures(tmp_path, {"005930"}, date(2026, 8, 17)) == {}


# ── _score_breakdown_from_intraday ───────────────────────────────────────

def test_score_breakdown_from_intraday_wraps_score_and_factors():
    candidates = [{
        "symbol": "005930", "name": "삼성전자", "score100": 72,
        "factors": [{"name": "호재 뉴스", "pts": 15, "max": 40, "evidence": "…"}],
        "foreign_label": "매수 시그널(재유입)",
    }]

    out = report_cli._score_breakdown_from_intraday(candidates)

    assert out == {"005930": {"score100": 72, "factors": candidates[0]["factors"]}}


# ── _build_track_record ──────────────────────────────────────────────────

def test_build_track_record_aggregates_by_producer(tmp_path):
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    selections.append([
        {"schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
         "producer": "intraday_scorer_v4", "outcome_d1_bps": 50.0},
        {"schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "000660",
         "producer": "intraday_scorer_v4", "outcome_d1_bps": -20.0},
        {"schema": 1, "date": "2026-08-16", "market": "KR", "symbol": "035420",
         "producer": "intraday_scorer_v4", "outcome_d1_bps": None},
    ], path)

    out = report_cli._build_track_record(tmp_path)

    rec = out["intraday_scorer_v4"]
    assert rec["n_selections"] == 3
    assert rec["n_scored_d1"] == 2
    assert rec["win_rate_d1"] == 0.5
    assert rec["avg_bps_d1"] == 15.0


def test_build_track_record_ignores_rows_without_producer(tmp_path):
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    selections.append([
        {"schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
         "outcome_d1_bps": 50.0},
    ], path)

    assert report_cli._build_track_record(tmp_path) == {}


def test_build_track_record_missing_ledger_returns_empty(tmp_path):
    assert report_cli._build_track_record(tmp_path) == {}


# ── _build_agent_interpret ───────────────────────────────────────────────

def _payload(**over) -> dict:
    base = {
        "session_date": "2026-08-17", "market": "KR",
        "symbols": [{"symbol": "005930", "name": "삼성전자", "close": 71_000.0, "change_pct": 1.2}],
    }
    base.update(over)
    return base


class _SnapStub:
    def __init__(self, session_date=date(2026, 8, 17)):
        self.session_date = session_date


def _candidate() -> dict:
    return {
        "symbol": "005930", "name": "삼성전자", "score100": 72,
        "factors": [{"name": "호재 뉴스", "pts": 15, "max": 40, "evidence": "…"}],
        "foreign_label": None,
    }


def _midterm_candidate() -> dict:
    """중기 관심 종목 뷰 원소(`midterm_watch.build_midterm_watch` 반환) —
    단타 후보와 달리 `score100`/`factors` 키가 없다(2026-08-18 폴백 테스트)."""
    return {
        "symbol": "000660", "name": "SK하이닉스", "mentions": 3, "grade": 4,
        "grade_label": "분할 매수", "reasons": ["외국인 순매수"],
        "telegram_snippets": ["SK하이닉스 강세"], "prose": None,
    }


def test_build_agent_interpret_skips_when_no_candidates(tmp_path):
    view, status = report_cli._build_agent_interpret(tmp_path, _SnapStub(), _payload(), [], [], {})
    assert view == []
    assert status == "skipped_no_candidates"


def test_build_agent_interpret_skips_when_no_key(tmp_path, monkeypatch):
    _set_key(monkeypatch, None)

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [_candidate()], [], {},
    )

    assert view == []
    assert status == "skipped_no_key"


def test_build_agent_interpret_success_records_direction_and_confidence(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    def fake_chat_with_tools(messages, tools, api_key, execute=None, **kw):
        assert api_key == "sk-test"
        return {"text": '산문.\nJUDGMENT: {"direction": "bullish", "confidence": 4}', "rounds": 1}

    monkeypatch.setattr(narrate_mod, "chat_with_tools", fake_chat_with_tools)

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [_candidate()], [], {},
    )

    assert status == "ok"
    assert len(view) == 1
    assert view[0]["direction"] == "bullish"
    assert view[0]["confidence"] == 4
    assert view[0]["source"] == "intraday"


def test_build_agent_interpret_failed_when_all_candidates_fail(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod, "chat_with_tools", lambda **kw: None)

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [_candidate()], [], {},
    )

    assert view == []
    assert status == "failed"


def test_build_agent_interpret_limits_to_top_n(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    seen = []

    def fake_chat_with_tools(messages, tools, api_key, execute=None, **kw):
        seen.append(messages[1]["content"])
        return {"text": '산문.\nJUDGMENT: {"direction": "neutral", "confidence": 2}', "rounds": 1}

    monkeypatch.setattr(narrate_mod, "chat_with_tools", fake_chat_with_tools)

    candidates = [
        {"symbol": f"{i:06d}", "name": f"종목{i}", "score100": 90 - i, "factors": []}
        for i in range(8)
    ]
    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), candidates, [], {},
    )

    assert status == "ok"
    assert report_cli.AGENT_INTERPRET_TOP_N == 5
    assert len(view) == 5
    assert len(seen) == 5


def test_build_agent_interpret_exception_returns_failed_not_raise(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    def boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(narrate_mod, "chat_with_tools", boom)

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [_candidate()], [], {},
    )

    assert view == []
    assert status == "failed"


# ── _build_agent_interpret — 중기 관심 종목 폴백(2026-08-18) ─────────────
# US 리포트는 `_build_intraday_view`가 KR 전용이라 단타 후보가 항상 0건이다
# — 이 경우 중기 관심 종목(midterm_view) 상위 5개로 대상을 바꾼다.


def test_build_agent_interpret_falls_back_to_midterm_when_no_intraday_candidates(
    tmp_path, monkeypatch,
):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(
        narrate_mod, "chat_with_tools",
        lambda **kw: {"text": '산문.\nJUDGMENT: {"direction": "bullish", "confidence": 3}', "rounds": 1},
    )

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [], [_midterm_candidate()], {},
    )

    assert status == "ok_midterm_fallback"
    assert len(view) == 1
    assert view[0]["symbol"] == "000660"
    assert view[0]["source"] == "midterm"


def test_build_agent_interpret_midterm_fallback_limits_to_top_n(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(
        narrate_mod, "chat_with_tools",
        lambda **kw: {"text": '산문.\nJUDGMENT: {"direction": "neutral", "confidence": 2}', "rounds": 1},
    )

    midterm = [
        {"symbol": f"{i:06d}", "name": f"종목{i}", "mentions": 8 - i, "grade": 5 - i,
         "grade_label": "관심", "reasons": [], "telegram_snippets": [], "prose": None}
        for i in range(8)
    ]
    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [], midterm, {},
    )

    assert status == "ok_midterm_fallback"
    assert len(view) == 5


def test_build_agent_interpret_midterm_fallback_failed_status(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod, "chat_with_tools", lambda **kw: None)

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [], [_midterm_candidate()], {},
    )

    assert view == []
    assert status == "failed_midterm_fallback"


def test_build_agent_interpret_skips_when_both_intraday_and_midterm_empty(tmp_path, monkeypatch):
    _set_key(monkeypatch, "sk-test")

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [], [], {},
    )

    assert view == []
    assert status == "skipped_no_candidates"


def test_build_agent_interpret_prefers_intraday_over_midterm_when_both_present(
    tmp_path, monkeypatch,
):
    """단타 후보가 있으면 중기 관심 종목은 쓰지 않는다 — 폴백은 단타 0건일
    때만 발동한다."""
    _set_key(monkeypatch, "sk-test")
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(
        narrate_mod, "chat_with_tools",
        lambda **kw: {"text": '산문.\nJUDGMENT: {"direction": "bullish", "confidence": 4}', "rounds": 1},
    )

    view, status = report_cli._build_agent_interpret(
        tmp_path, _SnapStub(), _payload(), [_candidate()], [_midterm_candidate()], {},
    )

    assert status == "ok"
    assert view[0]["symbol"] == "005930"
    assert view[0]["source"] == "intraday"


# ── _record_agent_interpret_selections ───────────────────────────────────

def _view_item(symbol="005930", name="삼성전자", direction="bullish", confidence=4) -> dict:
    return {
        "symbol": symbol, "name": name, "prose": "산문",
        "direction": direction, "confidence": confidence, "rounds": 2,
        "tools_used": ["get_score_breakdown"],
    }


def test_record_agent_interpret_selections_writes_direction_and_confidence(tmp_path):
    report_cli._record_agent_interpret_selections([_view_item()], _payload(), tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert len(rows) == 1
    row = rows[0]
    assert row["producer"] == report_cli._AGENT_INTERPRET_PRODUCER
    assert row["direction"] == "bullish"
    assert row["confidence"] == 4
    assert row["close"] == 71_000.0


def test_record_agent_interpret_selections_records_even_when_direction_is_none(tmp_path):
    """JUDGMENT 파싱 실패(direction=None)여도 산문 생성 성공은 그대로 기록한다
    (사용자 결정 2026-08-17: "전부 기록하되 direction·confidence를 payload에 싣는다")."""
    item = _view_item(direction=None, confidence=None)
    report_cli._record_agent_interpret_selections([item], _payload(), tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["direction"] is None
    assert rows[0]["confidence"] is None


def test_record_agent_interpret_selections_writes_source_tag(tmp_path):
    """중기 폴백(2026-08-18) 채점 분해용 — view 원소의 source 를 원장에 그대로 싣는다."""
    item = {**_view_item(), "source": "midterm"}
    report_cli._record_agent_interpret_selections([item], _payload(), tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["source"] == "midterm"


def test_record_agent_interpret_selections_source_defaults_to_none_when_absent(tmp_path):
    report_cli._record_agent_interpret_selections([_view_item()], _payload(), tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["source"] is None


def test_record_agent_interpret_selections_uses_close_producer_when_given(tmp_path):
    report_cli._record_agent_interpret_selections(
        [_view_item()], _payload(), tmp_path, producer=report_cli._AGENT_INTERPRET_PRODUCER_CLOSE,
    )
    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["producer"] == "agent_interpret_v1_close"


def test_record_agent_interpret_selections_empty_view_writes_nothing(tmp_path):
    report_cli._record_agent_interpret_selections([], _payload(), tmp_path)
    assert not (tmp_path / "data" / "ledger" / "selections.jsonl").exists()


def test_record_agent_interpret_selections_does_not_collide_with_intraday_scorer_row(tmp_path):
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    selections.append([{
        "schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
        "producer": report_cli._INTRADAY_PRODUCER, "score100": 72, "outcome_filled": False,
    }], path)

    report_cli._record_agent_interpret_selections([_view_item()], _payload(), tmp_path)

    rows = selections.load(path)
    assert len(rows) == 2
    assert {r.get("producer") for r in rows} == {
        report_cli._INTRADAY_PRODUCER, report_cli._AGENT_INTERPRET_PRODUCER,
    }


def test_record_agent_interpret_selections_failure_does_not_raise(tmp_path, capsys):
    report_cli._record_agent_interpret_selections(
        [_view_item()], {"symbols": "이것은 리스트가 아니다"}, tmp_path,
    )
    err = capsys.readouterr().err
    assert "건너뜀" in err


# ── 스모크: 키 없이도 마감 리포트 빌드가 죽지 않는다 ─────────────────────

def _snap(market: str = "KR", session_date=date(2026, 8, 17), results=None) -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION, market=market, session_date=session_date,
        generated_at=datetime(2026, 8, 17, 13, 40, tzinfo=KST), results=results or {},
    )


def _ranking_result(boards: dict) -> SourceResult:
    at = datetime(2026, 8, 17, 13, 30, tzinfo=KST)
    return SourceResult(key="toss_rankings", ok=True, data={"boards": boards},
                         error=None, url="https://x", fetched_at=at, latency_ms=1)


def _cont_entry(today_articles: int = 0) -> dict:
    return {
        "name": "삼성전자", "days": 1, "articles": today_articles,
        "today_articles": today_articles, "streak_days": 1, "is_new": True,
        "history": [True], "titles": [],
    }


def test_emit_close_agent_interpret_status_recorded_without_key(monkeypatch, tmp_path):
    """`OPENROUTER_API_KEY`가 없어도 마감 리포트 빌드는 정상 완료되고,
    engine.json 에 `agent_interpret` 상태가 남는다 — 빌드가 LLM 때문에
    죽지 않는다는 계약의 엔드투엔드 증거."""
    _set_key(monkeypatch, None)
    monkeypatch.setattr(report_core, "load_us_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "collect_mentions", lambda snap, table, market: [])
    monkeypatch.setattr(report_core, "append_ledger", lambda mentions, path: 0)
    monkeypatch.setattr(report_core, "load_ledger", lambda path: [])
    monkeypatch.setattr(
        report_core, "continuity",
        lambda ledger, today, market=None: {"005930": _cont_entry(today_articles=3)},
    )
    # `_midterm_entities`(→ `_build_midterm_watch_view`, `_emit_close`가 부른다)는
    # `_derive`와 별개로 `load_table`을 자기 모듈에서 다시 임포트한다(Phase D
    # 엔진 분리) — 두 곳 다 막아야 이 테스트가 네트워크를 타지 않는다.
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 71_000.0, "change_pct": 1.0} for s in syms},
    )
    monkeypatch.setattr(report_core, "fetch_many", lambda codes, limit=6: {})

    snap = _snap(results={"toss_rankings": _ranking_result({
        "거래대금": [{"rank": 1, "symbol": "005930", "name": "삼성전자", "change_pct": 1.0}],
    })})

    out_root = tmp_path / "out"
    report_cli._emit_close(snap, tmp_path, out_root, tmp_path / "snapshots")

    day_dir = out_root / "2026" / "08" / "17"
    payload = json.loads((day_dir / "KR_close_engine.json").read_text(encoding="utf-8"))
    # 후보가 있었다면 "skipped_no_key", AUTO_WATCH 후보가 비어 있었다면
    # "skipped_no_candidates" — 어느 쪽이든 빌드가 죽지 않았다는 게 핵심이다.
    assert payload["agent_interpret"] in {"skipped_no_key", "skipped_no_candidates"}
