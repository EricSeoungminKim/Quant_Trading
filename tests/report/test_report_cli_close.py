"""마감 포지션 리포트(서브프로젝트 R) — `--session close` 배선.

아침 파이프라인 테스트(`test_report_build_quotes.py`, `test_report_cli_intraday.py`)
와 같은 관례: 네트워크를 타지 않는 헬퍼/`_emit_close`를 직접 호출해 검증한다.
전체 CLI(`main(["build", ...])`)는 open 세션과 마찬가지로 실네트워크가 필요해
여기서도 태우지 않는다 — `--session close` 인자 파싱 + KR 가드만 `main()`을
통해 확인한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from quant.apps import report_cli
from quant.report.collect import core as report_core
from quant.report.collect import midterm as report_midterm
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.collect.snapshot import load_snapshot, save_snapshot
from quant.control import selections

KST = ZoneInfo("Asia/Seoul")


def _snap(market: str = "KR", session_date=date(2026, 8, 17),
          generated_at=None, results=None) -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION, market=market, session_date=session_date,
        generated_at=generated_at or datetime(2026, 8, 17, 13, 40, tzinfo=KST),
        results=results or {},
    )


def _ranking_result(boards: dict) -> SourceResult:
    at = datetime(2026, 8, 17, 13, 30, tzinfo=KST)
    return SourceResult(key="toss_rankings", ok=True, data={"boards": boards},
                         error=None, url="https://x", fetched_at=at, latency_ms=1)


def _news_result(feeds: dict) -> SourceResult:
    at = datetime(2026, 8, 17, 13, 30, tzinfo=KST)
    return SourceResult(key="news", ok=True, data={"feeds": feeds}, error=None,
                         url="https://x", fetched_at=at, latency_ms=1)


# ── close_news_since_for ─────────────────────────────────────────────────

def test_close_news_since_uses_morning_snapshot_generated_at():
    morning = _snap(generated_at=datetime(2026, 8, 17, 8, 0, tzinfo=KST))
    now = datetime(2026, 8, 17, 13, 40, tzinfo=KST)

    since = report_cli.close_news_since_for(morning, now)

    assert since == datetime(2026, 8, 17, 8, 0, tzinfo=KST)


def test_close_news_since_falls_back_to_six_hours_without_morning_snapshot():
    now = datetime(2026, 8, 17, 13, 40, tzinfo=KST)

    since = report_cli.close_news_since_for(None, now)

    assert since == now - timedelta(hours=6)
    assert since == now - report_cli.CLOSE_NEWS_FALLBACK_WINDOW


# ── 스냅샷 경로 분리 + 아침 체인 격리(CRITICAL) ──────────────────────────

def test_close_snapshot_path_has_distinct_suffix(tmp_path):
    p = report_cli._close_snapshot_path(tmp_path, "KR", date(2026, 8, 17))

    assert p == tmp_path / "KR" / "2026-08-17_close.json"
    assert p != tmp_path / "KR" / "2026-08-17.json"


def test_morning_snapshot_missing_returns_none(tmp_path):
    assert report_cli._load_morning_snapshot(tmp_path, "KR", date(2026, 8, 17)) is None


def test_morning_snapshot_loads_when_present(tmp_path):
    morning = _snap(generated_at=datetime(2026, 8, 17, 8, 0, tzinfo=KST))
    save_snapshot(morning, tmp_path)

    loaded = report_cli._load_morning_snapshot(tmp_path, "KR", date(2026, 8, 17))

    assert loaded is not None
    assert loaded.generated_at == morning.generated_at


def test_close_snapshot_never_picked_as_mornings_previous_snapshot(tmp_path):
    """CRITICAL — 마감 스냅샷만 있고(아침 스냅샷 없음) 다음날 아침 리포트가
    `previous_snapshot()`으로 전날을 찾으면, `_close.json`은 파일명 패턴이
    달라 절대 걸리지 않아야 한다(`{date}.json`만 스캔)."""
    from quant.analyze.delta import previous_snapshot

    close_snap = _snap(session_date=date(2026, 8, 17),
                        generated_at=datetime(2026, 8, 17, 13, 40, tzinfo=KST))
    close_path = report_cli._close_snapshot_path(tmp_path, "KR", date(2026, 8, 17))
    close_path.parent.mkdir(parents=True, exist_ok=True)
    close_path.write_text(close_snap.to_json(), encoding="utf-8")

    prev = previous_snapshot("KR", date(2026, 8, 18), tmp_path)

    assert prev is None  # 마감 스냅샷만 있고 아침 스냅샷이 없으므로 못 찾아야 정상


def test_open_previous_snapshot_still_resolves_when_both_exist(tmp_path):
    """같은 날 아침(open)·마감(close) 스냅샷이 둘 다 있어도, 다음날 아침
    리포트의 `previous_snapshot()`은 **아침** 스냅샷만 돌려줘야 한다 —
    마감 스냅샷이 섞여 들어오면 안 된다."""
    from quant.analyze.delta import previous_snapshot

    morning = _snap(session_date=date(2026, 8, 17),
                     generated_at=datetime(2026, 8, 17, 8, 0, tzinfo=KST))
    save_snapshot(morning, tmp_path)

    close_snap = _snap(session_date=date(2026, 8, 17),
                        generated_at=datetime(2026, 8, 17, 13, 40, tzinfo=KST))
    close_path = report_cli._close_snapshot_path(tmp_path, "KR", date(2026, 8, 17))
    close_path.write_text(close_snap.to_json(), encoding="utf-8")

    prev = previous_snapshot("KR", date(2026, 8, 18), tmp_path)

    assert prev is not None
    assert prev.generated_at == morning.generated_at  # 아침 것, 마감 것(13:40) 아님


# ── _build_close_news_view ───────────────────────────────────────────────

def test_close_news_view_sorts_bullish_tier_first():
    news_flow = [
        {"title": "평범한 뉴스", "link": "https://a", "outlet": "매체A", "dup_count": 1},
        {"title": "삼성전자 대규모 수주 계약 체결", "link": "https://b", "outlet": "매체B", "dup_count": 1},
    ]

    out = report_cli._build_close_news_view(news_flow)

    assert out[0]["title"] == "삼성전자 대규모 수주 계약 체결"
    assert out[0]["bullish_tier"] == 2
    assert out[1]["bullish_tier"] == 0


def test_close_news_view_flags_bearish_veto():
    news_flow = [{"title": "어닝쇼크 목표가 하향", "link": "https://a", "outlet": "매체A", "dup_count": 1}]

    out = report_cli._build_close_news_view(news_flow)

    assert out[0]["bearish"] is True


def test_close_news_view_empty_input_returns_empty():
    assert report_cli._build_close_news_view([]) == []


# ── _build_close_flow_view ───────────────────────────────────────────────

def test_close_flow_view_reads_market_features():
    payload = {"features": {
        "foreign_net_100m_krw": 1200, "institution_net_100m_krw": -300,
        "individual_net_100m_krw": -900,
    }}

    view = report_cli._build_close_flow_view(payload)

    assert view == {"foreign_net": 1200, "institution_net": -300, "individual_net": -900}


def test_close_flow_view_none_when_features_missing():
    view = report_cli._build_close_flow_view({})

    assert view == {"foreign_net": None, "institution_net": None, "individual_net": None}


# ── _build_close_ranking_view ────────────────────────────────────────────

def test_close_ranking_view_picks_only_amount_and_gainers_boards():
    boards = {
        "거래대금": [{"rank": 1, "symbol": "005930", "name": "삼성전자", "change_pct": 1.0}],
        "상승률": [{"rank": 1, "symbol": "000660", "name": "SK하이닉스", "change_pct": 8.0}],
        "하락률": [{"rank": 1, "symbol": "999999", "name": "손실주", "change_pct": -5.0}],
        "토스 사용자 거래대금": [{"rank": 1, "symbol": "035420", "name": "네이버", "change_pct": 0.5}],
    }
    snap = _snap(results={"toss_rankings": _ranking_result(boards)})

    view = report_cli._build_close_ranking_view(snap)

    assert set(view.keys()) == {"거래대금", "상승률"}
    assert view["거래대금"][0]["symbol"] == "005930"


def test_close_ranking_view_respects_top_limit():
    items = [{"rank": i, "symbol": f"{i:06d}", "change_pct": 1.0} for i in range(1, 11)]
    snap = _snap(results={"toss_rankings": _ranking_result({"거래대금": items})})

    view = report_cli._build_close_ranking_view(snap, top=8)

    assert len(view["거래대금"]) == 8


def test_close_ranking_view_empty_when_ranking_source_failed():
    at = datetime(2026, 8, 17, 13, 30, tzinfo=KST)
    snap = _snap(results={"toss_rankings": SourceResult(
        key="toss_rankings", ok=False, data=None, error="403", url="https://x",
        fetched_at=at, latency_ms=1,
    )})

    assert report_cli._build_close_ranking_view(snap) == {}


# ── producer 분리 (_record_intraday_selections) ──────────────────────────

def _payload(**over) -> dict:
    base = {
        "session_date": "2026-08-17", "market": "KR",
        "symbols": [{"symbol": "005930", "name": "삼성전자", "close": 71_000.0,
                     "change_pct": 1.2}],
    }
    base.update(over)
    return base


def _view_item() -> dict:
    return {
        "symbol": "005930", "name": "삼성전자", "score100": 55,
        "factors": [{"name": "호재 뉴스", "pts": 15, "max": 40, "evidence": "…"}],
        "foreign_label": None,
    }


def test_close_producer_does_not_collide_with_open_or_morning_scorer_rows(tmp_path):
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    # 아침 리포트 본선 행(producer 없음) + 아침 단타 스코어러 행(v3)
    selections.append([
        {"schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
         "close": 71_000.0, "outcome_filled": False},
    ], path)
    report_cli._record_intraday_selections(
        [_view_item()], _payload(), tmp_path, producer=report_cli._INTRADAY_PRODUCER,
    )

    # 마감 스코어러 producer로 같은 종목·같은 날 다시 기록
    report_cli._record_intraday_selections(
        [_view_item()], _payload(), tmp_path, producer=report_cli._INTRADAY_PRODUCER_CLOSE,
    )

    rows = selections.load(path)
    assert len(rows) == 3
    producers = {r.get("producer") for r in rows}
    assert producers == {None, report_cli._INTRADAY_PRODUCER, report_cli._INTRADAY_PRODUCER_CLOSE}


def test_close_producer_default_still_uses_open_producer_when_unspecified(tmp_path):
    """하위호환 — `producer` 인자를 안 주면 기존 아침 동작(`_INTRADAY_PRODUCER`)
    그대로다(기존 호출부·테스트가 바이트 단위로 안전해야 한다)."""
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    report_cli._record_intraday_selections([_view_item()], _payload(), tmp_path)

    rows = selections.load(path)
    assert rows[0]["producer"] == report_cli._INTRADAY_PRODUCER


# ── _emit_close: 산출물 경로 + 아침 산출물 무변경 ────────────────────────

def _wire_cont(monkeypatch, cont: dict) -> None:
    monkeypatch.setattr(report_core, "load_us_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "collect_mentions", lambda snap, table, market: [])
    monkeypatch.setattr(report_core, "append_ledger", lambda mentions, path: 0)
    monkeypatch.setattr(report_core, "load_ledger", lambda path: [])
    monkeypatch.setattr(report_core, "continuity", lambda ledger, today, market=None: cont)
    # `_midterm_entities`(→ `_build_midterm_watch_view`, `_emit_close`가 부른다)는
    # `_derive`와 별개로 `load_table`을 자기 모듈에서 다시 임포트한다(Phase D
    # 엔진 분리) — 두 곳 다 막아야 이 테스트가 네트워크를 타지 않는다.
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: {})
    # 텔레그램 인사이트(서브프로젝트 S part 2) — _emit_close 도 오후엔
    # 실제 네트워크를 타지 않게 막는다(§내용 3 지시로 마감판도 부른다).
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})


def _cont_entry(today_articles: int = 0) -> dict:
    return {
        "name": "삼성전자", "days": 1, "articles": today_articles,
        "today_articles": today_articles, "streak_days": 1, "is_new": True,
        "history": [True], "titles": [],
    }


def test_emit_close_writes_close_named_artifacts_only(monkeypatch, tmp_path):
    _wire_cont(monkeypatch, {"005930": _cont_entry()})
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
    assert (day_dir / "KR_close_report.html").exists()
    assert (day_dir / "KR_close_engine.json").exists()
    # 아침 산출물은 이 경로에서 절대 생기지 않는다 — 파일명이 다르다.
    assert not (day_dir / "KR_report.html").exists()
    assert not (day_dir / "KR_engine.json").exists()

    payload = json.loads((day_dir / "KR_close_engine.json").read_text(encoding="utf-8"))
    assert payload["session"] == "close"
    assert payload["market"] == "KR"


def test_emit_close_does_not_touch_existing_morning_artifacts(monkeypatch, tmp_path):
    """아침 산출물이 이미 있는 디렉토리에 마감판을 발행해도 아침 파일은
    한 바이트도 바뀌지 않는다(경로 헬퍼가 additive 라는 계약의 직접 증거)."""
    _wire_cont(monkeypatch, {"005930": _cont_entry()})
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 71_000.0, "change_pct": 1.0} for s in syms},
    )
    monkeypatch.setattr(report_core, "fetch_many", lambda codes, limit=6: {})

    out_root = tmp_path / "out"
    day_dir = out_root / "2026" / "08" / "17"
    day_dir.mkdir(parents=True)
    morning_html = day_dir / "KR_report.html"
    morning_json = day_dir / "KR_engine.json"
    morning_html.write_text("MORNING-HTML-SENTINEL", encoding="utf-8")
    morning_json.write_text("MORNING-JSON-SENTINEL", encoding="utf-8")

    snap = _snap(results={"toss_rankings": _ranking_result({})})
    report_cli._emit_close(snap, tmp_path, out_root, tmp_path / "snapshots")

    assert morning_html.read_text(encoding="utf-8") == "MORNING-HTML-SENTINEL"
    assert morning_json.read_text(encoding="utf-8") == "MORNING-JSON-SENTINEL"


def test_emit_close_records_intraday_selections_with_close_producer(monkeypatch, tmp_path):
    _wire_cont(monkeypatch, {"005930": _cont_entry(today_articles=3)})
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 71_000.0, "change_pct": 1.0} for s in syms},
    )
    monkeypatch.setattr(report_core, "fetch_many", lambda codes, limit=6: {})

    snap = _snap(results={"toss_rankings": _ranking_result({
        "거래대금": [{"rank": 1, "symbol": "005930", "name": "삼성전자", "change_pct": 1.0}],
    })})

    report_cli._emit_close(snap, tmp_path, tmp_path / "out", tmp_path / "snapshots")

    ledger_path = tmp_path / "data" / "ledger" / "selections.jsonl"
    if ledger_path.exists():  # AUTO_WATCH 후보가 비어 있으면(뉴스 근거 부족) 기록 자체가 없을 수 있다
        rows = selections.load(ledger_path)
        producers = {r.get("producer") for r in rows}
        assert report_cli._INTRADAY_PRODUCER not in producers


def test_emit_close_does_not_write_report_ledger_selections(monkeypatch, tmp_path):
    """아침 전용 원장(_record_selections/_log_overlap/_record_flows)은 마감
    경로에서 다시 쓰지 않는다 — 하루 두 번 같은 날짜로 재기록하면 flows.jsonl
    기간합이 중복 오염된다."""
    _wire_cont(monkeypatch, {"005930": _cont_entry()})
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 71_000.0, "change_pct": 1.0} for s in syms},
    )
    monkeypatch.setattr(
        report_core, "fetch_many",
        lambda codes, limit=6: {"005930": {"flow": [{"date": "2026-08-17", "foreign_net": 1, "inst_net": 1}]}},
    )

    snap = _snap(results={"toss_rankings": _ranking_result({})})
    report_cli._emit_close(snap, tmp_path, tmp_path / "out", tmp_path / "snapshots")

    assert not (tmp_path / "data" / "ledger" / "flows.jsonl").exists()
    assert not (tmp_path / "data" / "ledger" / "overlap.jsonl").exists()


# ── main() 배선: --session 인자 + KR 전용 가드 ───────────────────────────

def test_main_build_close_rejects_us_market(tmp_path, capsys):
    rc = report_cli.main([
        "build", "--market", "US", "--session", "close", "--root", str(tmp_path),
        "--date", "2026-08-17",
    ])

    assert rc == 2
    assert "KR 전용" in capsys.readouterr().err


def test_main_when_unaffected_by_session_flag_addition():
    """`when` 서브커맨드엔 `--session`을 안 붙였다 — 기존 파서 동작 회귀 없음."""
    rc = report_cli.main(["when", "--market", "KR", "--date", "2026-08-17"])
    assert rc == 0


# ── summary --session close ──────────────────────────────────────────────

def _write_close_engine_json(out_root, market: str, d: date, payload: dict) -> None:
    path = out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_close_engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_summary_close_prints_candidate_count_and_top3(tmp_path, capsys):
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-08-17", "session": "close",
        "intraday_view": [
            {"symbol": "005930", "name": "삼성전자", "score100": 70},
            {"symbol": "000660", "name": "SK하이닉스", "score100": 85},
            {"symbol": "035420", "name": "네이버", "score100": 60},
            {"symbol": "051910", "name": "LG화학", "score100": 40},
        ],
    }
    _write_close_engine_json(tmp_path / "out", "KR", date(2026, 8, 17), payload)

    rc = report_cli.main([
        "summary", "--market", "KR", "--session", "close",
        "--date", "2026-08-17", "--root", str(tmp_path),
    ])
    out = capsys.readouterr().out.strip("\n")

    assert rc == 0
    lines = out.split("\n")
    assert lines[0] == "마감 후보 4개"
    assert lines[1] == "상위: SK하이닉스(85) · 삼성전자(70) · 네이버(60)"


def test_summary_close_missing_engine_json_prints_nothing(tmp_path, capsys):
    rc = report_cli.main([
        "summary", "--market", "KR", "--session", "close",
        "--date", "2026-08-17", "--root", str(tmp_path),
    ])

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_summary_open_session_unaffected_by_close_variant(tmp_path, capsys):
    """`--session` 을 안 주거나 open 을 주면 기존 아침 summary 동작 그대로
    (엔진 JSON 경로/포맷이 close 와 완전히 분리됐다는 회귀 방지)."""
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-08-17",
        "auto_watch": "AUTO_WATCH: 005930:NEWS",
        "symbols": [{"symbol": "005930", "name": "삼성전자", "baseline_score100": 70}],
    }
    path = (tmp_path / "out" / "2026" / "08" / "17" / "KR_engine.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    rc = report_cli.main([
        "summary", "--market", "KR", "--date", "2026-08-17", "--root", str(tmp_path),
    ])
    out = capsys.readouterr().out.strip("\n")

    assert rc == 0
    assert out.split("\n")[0] == "후보 1개"
