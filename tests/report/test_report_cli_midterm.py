"""중기 관심 종목(서브프로젝트 W part 3) — `report_cli` 배선.

`_emit`/`_emit_close`(전체 파이프라인)을 통째로 태우지 않는다 — 다른
`_build_*` 배선 테스트(`test_report_cli_agent_interpret.py` 등)와 같은
관례로 헬퍼만 단위 검증한다. 후보 선정·등급 산정 자체는
`tests/test_midterm_watch.py`의 몫이다. 마지막에 스모크 하나만 `_emit_close`
전체를 태워 "AI 키가 없어도 빌드가 죽지 않는다"를 확인한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.control import selections
from quant.report.collect import core as report_core
from quant.report.collect import midterm as report_midterm

KST = ZoneInfo("Asia/Seoul")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _telegram_row(handle: str, msg_id: str, text: str, days_ago: float = 0.5) -> dict:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"handle": handle, "msg_id": msg_id, "text": text,
            "published": dt.isoformat().replace("+00:00", "Z")}


def _payload(**over) -> dict:
    base = {
        "session_date": "2026-08-17", "market": "KR",
        "symbols": [{"symbol": "005930", "name": "삼성전자", "close": 71_000.0, "change_pct": 1.2}],
    }
    base.update(over)
    return base


# ── _load_midterm_telegram_msgs ──────────────────────────────────────────

def test_load_midterm_telegram_msgs_reads_ledger(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "telegram_msgs.jsonl", [
        _telegram_row("tazastock", "1", "삼성전자 강세"),
    ])
    out = report_cli._load_midterm_telegram_msgs(tmp_path)
    assert len(out) == 1
    assert out[0]["text"] == "삼성전자 강세"


def test_load_midterm_telegram_msgs_missing_ledger_returns_empty(tmp_path):
    assert report_cli._load_midterm_telegram_msgs(tmp_path) == []


# ── _build_midterm_bullish ───────────────────────────────────────────────

def test_build_midterm_bullish_classifies_recent_titles(tmp_path):
    _write_jsonl(tmp_path / "data" / "ledger" / "mentions.jsonl", [
        {"date": "2026-08-17", "symbol": "005930", "title": "삼성전자 대규모 수주", "feed": "a"},
    ])
    out = report_cli._build_midterm_bullish(tmp_path, {"005930"}, date(2026, 8, 17))
    assert out["005930"]["bullish_types"] == ["수주/공급계약"]
    assert out["005930"]["bearish"] is False


def test_build_midterm_bullish_missing_ledger_returns_empty(tmp_path):
    assert report_cli._build_midterm_bullish(tmp_path, {"005930"}, date(2026, 8, 17)) == {}


# ── _midterm_entities / _midterm_name_by_symbol ──────────────────────────

def test_midterm_entities_kr_uses_load_table(tmp_path, monkeypatch):
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: [("삼성전자", "005930")])
    out = report_cli._midterm_entities(tmp_path, "KR", _payload())
    assert out == [("삼성전자", "005930")]


def test_midterm_entities_us_uses_payload_symbols(tmp_path):
    payload = _payload(market="US", symbols=[{"symbol": "AAPL"}])
    out = report_cli._midterm_entities(tmp_path, "US", payload)
    assert out == {"AAPL"}


def test_midterm_name_by_symbol_kr_uses_load_name_map(tmp_path, monkeypatch):
    monkeypatch.setattr(report_midterm, "load_name_map", lambda cache_dir, market: {"005930": "삼성전자"})
    out = report_cli._midterm_name_by_symbol(tmp_path, "KR", _payload())
    assert out == {"005930": "삼성전자"}


def test_midterm_name_by_symbol_us_uses_payload_symbols(tmp_path):
    payload = _payload(market="US", symbols=[{"symbol": "AAPL", "name": "Apple"}])
    out = report_cli._midterm_name_by_symbol(tmp_path, "US", payload)
    assert out == {"AAPL": "Apple"}


# ── _build_midterm_watch_view ────────────────────────────────────────────

def test_build_midterm_watch_view_end_to_end_kr(tmp_path, monkeypatch):
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: [("삼성전자", "005930")])
    monkeypatch.setattr(report_midterm, "load_name_map", lambda cache_dir, market: {"005930": "삼성전자"})
    telegram_msgs = [
        _telegram_row("tazastock", "1", "삼성전자 강세"),
        _telegram_row("tazastock", "2", "삼성전자 추가 매수세"),
    ]
    _write_jsonl(tmp_path / "data" / "ledger" / "frgn_flow.jsonl", [
        {"date": f"2026-08-{i+10:02d}", "symbol": "005930", "foreign_net": 100, "inst_net": 50}
        for i in range(5)
    ])

    out = report_cli._build_midterm_watch_view(
        tmp_path, "KR", _payload(), telegram_msgs, date(2026, 8, 17),
    )

    assert len(out) == 1
    assert out[0]["symbol"] == "005930"
    assert out[0]["name"] == "삼성전자"
    assert out[0]["prose"] is None


def test_build_midterm_watch_view_no_candidates_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: [("삼성전자", "005930")])
    out = report_cli._build_midterm_watch_view(tmp_path, "KR", _payload(), [], date(2026, 8, 17))
    assert out == []


def test_build_midterm_watch_view_exception_returns_empty_not_raise(tmp_path, monkeypatch):
    def boom(cache_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(report_midterm, "load_table", boom)
    out = report_cli._build_midterm_watch_view(
        tmp_path, "KR", _payload(), [_telegram_row("tazastock", "1", "x")], date(2026, 8, 17),
    )
    assert out == []


# ── _build_midterm_prose / _apply_midterm_prose ──────────────────────────

def _set_key(monkeypatch, value) -> None:
    import quant.adapters.env as env_mod

    monkeypatch.setattr(env_mod, "get_key", lambda name: value)


def _candidate(symbol="005930") -> dict:
    return {
        "symbol": symbol, "name": "삼성전자", "mentions": 2, "grade": 3, "grade_label": "관심",
        "reasons": [], "telegram_snippets": ["강세"], "prose": None,
    }


class _FakeNarrator:
    def __init__(self, response: str | None):
        self._response = response

    def narrate(self, prompt: str) -> str | None:
        return self._response


def test_build_midterm_prose_empty_candidates_returns_empty():
    assert report_cli._build_midterm_prose([]) == {}


def test_build_midterm_prose_no_key_returns_empty(monkeypatch):
    """`make_narrator`가 키 없을 때 계약대로 `NullNarrator`(narrate→None)를
    돌려주는 상황을 가짜로 재현한다 — `test_report_cli_digest_prose.py`와
    같은 관례(실제 env/네트워크를 타지 않는다, `make_narrator` 자체를
    가짜로 바꾼다)."""
    monkeypatch.setattr("quant.adapters.narrate.make_narrator", lambda **kw: _FakeNarrator(None))

    out = report_cli._build_midterm_prose([_candidate()])
    assert out == {}


def test_build_midterm_prose_success_uses_tool_model(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    seen_kwargs = []

    def fake_make_narrator(**kwargs):
        seen_kwargs.append(kwargs)
        return _FakeNarrator("전망 좋음.")

    monkeypatch.setattr(narrate_mod, "make_narrator", fake_make_narrator)

    out = report_cli._build_midterm_prose([_candidate()])

    assert out == {"005930": "전망 좋음."}
    assert seen_kwargs == [{"model": narrate_mod.TOOL_MODEL}]


def test_apply_midterm_prose_fills_matching_symbols():
    view = [_candidate("005930"), _candidate("000660")]
    out = report_cli._apply_midterm_prose(view, {"005930": "전망 좋음."})
    assert out[0]["prose"] == "전망 좋음."
    assert out[1]["prose"] is None


# ── _usnews_titles / _usnews_headlines / _build_us_news_kr_view ─────────

def _telegram_result_with_usnews() -> dict:
    return {
        "walterbloomberg": {"messages": [
            {"text": "NVIDIA AI CHIP DEMAND SURGES", "published": "2026-08-17T03:00:00Z"},
        ], "error": None},
        "financialjuice": {"messages": [
            {"text": "SEMICONDUCTOR EXPORTS RECORD HIGH", "published": "2026-08-17T02:30:00Z"},
        ], "error": None},
    }


def test_usnews_titles_collects_from_usnews_tier_only():
    out = report_cli._usnews_titles(_telegram_result_with_usnews())
    assert set(out) == {"NVIDIA AI CHIP DEMAND SURGES", "SEMICONDUCTOR EXPORTS RECORD HIGH"}


def test_usnews_titles_ignores_non_usnews_channels():
    result = {"tazastock": {"messages": [{"text": "국내 시황"}], "error": None}}
    assert report_cli._usnews_titles(result) == []


def test_usnews_headlines_sorted_newest_first_and_capped():
    out = report_cli._usnews_headlines(_telegram_result_with_usnews())
    assert [h["text"] for h in out] == [
        "NVIDIA AI CHIP DEMAND SURGES", "SEMICONDUCTOR EXPORTS RECORD HIGH",
    ]
    assert out[0]["published_hhmm"] is not None


def test_build_us_news_kr_view_empty_titles_returns_empty(tmp_path):
    assert report_cli._build_us_news_kr_view(tmp_path, {}, date(2026, 8, 17)) == []


def test_build_us_news_kr_view_returns_sectors_with_grades(tmp_path):
    out = report_cli._build_us_news_kr_view(
        tmp_path, _telegram_result_with_usnews(), date(2026, 8, 17),
    )
    assert out
    assert out[0]["sector"] == "Information Technology"
    assert out[0]["stocks"]
    assert "grade" in out[0]["stocks"][0]


# ── _record_midterm_selections ───────────────────────────────────────────

def test_record_midterm_selections_writes_producer_and_grade(tmp_path):
    view = [{
        "symbol": "005930", "name": "삼성전자", "mentions": 3, "grade": 5,
        "grade_label": "적극 매수", "reasons": [], "telegram_snippets": [], "prose": None,
    }]
    report_cli._record_midterm_selections(view, _payload(), tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert len(rows) == 1
    assert rows[0]["producer"] == report_cli._MIDTERM_PRODUCER
    assert rows[0]["grade"] == 5
    assert rows[0]["mentions"] == 3
    assert rows[0]["close"] == 71_000.0


def test_record_midterm_selections_writes_close_date_from_payload(tmp_path):
    """render.py 가 payload["symbols"] 항목에 실은 date(quote 의 실제 거래일,
    D2 2026-09-03)가 close_date 로 그대로 옮겨진다."""
    payload = _payload(symbols=[
        {"symbol": "005930", "name": "삼성전자", "close": 71_000.0, "change_pct": 1.2,
         "date": "2026-08-14"},
    ])
    view = [{
        "symbol": "005930", "name": "삼성전자", "mentions": 3, "grade": 5,
        "grade_label": "적극 매수", "reasons": [], "telegram_snippets": [], "prose": None,
    }]
    report_cli._record_midterm_selections(view, payload, tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["close_date"] == "2026-08-14"


def test_record_midterm_selections_empty_view_writes_nothing(tmp_path):
    report_cli._record_midterm_selections([], _payload(), tmp_path)
    assert not (tmp_path / "data" / "ledger" / "selections.jsonl").exists()


def test_record_midterm_selections_uses_close_producer_when_given(tmp_path):
    view = [{"symbol": "005930", "name": "삼성전자", "mentions": 2, "grade": 3,
             "grade_label": "관심", "reasons": [], "telegram_snippets": [], "prose": None}]
    report_cli._record_midterm_selections(
        view, _payload(), tmp_path, producer=report_cli._MIDTERM_PRODUCER_CLOSE,
    )
    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["producer"] == "midterm_watch_v1_close"


def test_record_midterm_selections_does_not_collide_with_intraday_scorer_row(tmp_path):
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    selections.append([{
        "schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
        "producer": report_cli._INTRADAY_PRODUCER, "score100": 72, "outcome_filled": False,
    }], path)

    view = [{"symbol": "005930", "name": "삼성전자", "mentions": 2, "grade": 3,
             "grade_label": "관심", "reasons": [], "telegram_snippets": [], "prose": None}]
    report_cli._record_midterm_selections(view, _payload(), tmp_path)

    rows = selections.load(path)
    assert len(rows) == 2
    assert {r.get("producer") for r in rows} == {
        report_cli._INTRADAY_PRODUCER, report_cli._MIDTERM_PRODUCER,
    }


def test_record_midterm_selections_failure_does_not_raise(tmp_path, capsys):
    view = [{"symbol": "005930", "name": "삼성전자", "mentions": 2, "grade": 3,
             "grade_label": "관심", "reasons": [], "telegram_snippets": [], "prose": None}]
    report_cli._record_midterm_selections(view, {"symbols": "이것은 리스트가 아니다"}, tmp_path)
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


def test_emit_close_midterm_watch_recorded_without_key(monkeypatch, tmp_path):
    """`OPENROUTER_API_KEY`가 없어도 마감 리포트 빌드는 정상 완료되고,
    engine.json 에 `midterm_watch`(빈 리스트 포함) 키가 남는다 — 빌드가
    LLM/텔레그램 부재 때문에 죽지 않는다는 계약의 엔드투엔드 증거."""
    _set_key(monkeypatch, None)
    monkeypatch.setattr(report_core, "load_us_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "load_table", lambda cache_dir: [])
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
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: [])
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
    assert payload["midterm_watch"] == []
    assert payload["us_news_kr_map"] == []


# ── insidertracking(일일 글로벌 다이제스트) 배선 ─────────────────────────
# 2026-08-21 소유자 지시: "미국장이 끝나면 글로벌 뉴스를 보내주는것 같아(05:00쯤).
# 이걸 필두로 리포트 작성할때도 반영하면 더 좋은 방향성이 생길 것 같아."
#
# 실측(원장 275건): 이 채널은 「미국 기업 섹터별 소식 정리」·「🌎 글로벌 뉴스
# 브리핑」을 **한국어 일일 다이제스트**로 하루 한 번 보낸다 — walterbloomberg/
# financialjuice 의 영문 시간당 헤드라인과 성격이 다르다. 그래서 tier 를 그냥
# "usnews" 로 바꾸지 않고 별도 tier("usdigest")를 준다:
#   - 서사(_usnews_titles → 시황 다이제스트/Exec Summary/미국발 뉴스 뷰)에는 넣는다.
#   - "🇺🇸 실시간 헤드라인" 구획(_usnews_headlines)에는 **넣지 않는다** —
#     일일 요약을 실시간 헤드라인으로 표시하면 라벨이 거짓이 된다.

def _telegram_result_with_digest() -> dict:
    r = _telegram_result_with_usnews()
    r["insidertracking"] = {"messages": [
        {"text": "🌎 글로벌 뉴스 브리핑 - 2026년 8월 21일 · 아메리카 ...",
         "published": "2026-08-21T20:05:00Z"},
    ], "error": None}
    return r


def test_usnews_titles_includes_daily_digest_channel():
    out = report_cli._usnews_titles(_telegram_result_with_digest())
    assert any("글로벌 뉴스 브리핑" in t for t in out)
    # 기존 usnews tier 도 그대로 살아 있어야 한다.
    assert "NVIDIA AI CHIP DEMAND SURGES" in out


def test_usnews_headlines_excludes_daily_digest_channel():
    """실시간 헤드라인 구획은 시간당 채널만 — 일일 다이제스트가 섞이면 안 된다."""
    out = report_cli._usnews_headlines(_telegram_result_with_digest())
    assert all("글로벌 뉴스 브리핑" not in h["text"] for h in out)
    assert len(out) == 2
