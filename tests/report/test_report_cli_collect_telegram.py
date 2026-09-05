"""`report_cli collect --telegram` — 텔레그램 채널 누적 수집 CLI 모드(2026-09-03).

news-collect@ 와 같은 30분 주기 패턴이되, 대상이 원장 하나(`telegram_msgs.jsonl`)
뿐이다 — `telegram-collect.service`가 이 서브커맨드를 호출한다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from quant.apps import report_cli
from quant.collect.sources import telegram_channels


def _msgs(handle: str, ids: list[str]) -> dict:
    return {"messages": [
        {"msg_id": i, "text": f"{handle}-{i}", "published": "2026-09-02T00:00:00Z",
         "links": [], "images": []}
        for i in ids
    ], "error": None}


def test_collect_telegram_appends_ledger_and_returns_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"tazastock": _msgs("tazastock", ["1", "2"])},
    )

    rc = report_cli.main([
        "collect", "--market", "KR", "--date", date(2026, 9, 2).isoformat(),
        "--root", str(tmp_path), "--telegram",
    ])

    assert rc == 0
    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    rows = telegram_channels.load_ledger(path)
    assert {(r["handle"], r["msg_id"]) for r in rows} == {("tazastock", "1"), ("tazastock", "2")}
    out = capsys.readouterr().out
    assert "신규 2건" in out


def test_collect_telegram_reports_errored_channels(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {
            "tazastock": _msgs("tazastock", ["1"]),
            "pikachu_aje": {"messages": [], "error": "ConnectionError: boom"},
        },
    )

    rc = report_cli.main([
        "collect", "--market", "KR", "--date", date(2026, 9, 2).isoformat(),
        "--root", str(tmp_path), "--telegram",
    ])

    assert rc == 0
    err = capsys.readouterr().err
    assert "pikachu_aje" in err


def test_collect_telegram_does_not_touch_news_store(tmp_path, monkeypatch):
    """`--telegram` 플래그가 없는 기존 `collect --market KR` 경로(뉴스)와 다른
    파일을 만져야 한다 — 텔레그램 수집이 뉴스 저장소를 건드리면 안 된다."""
    monkeypatch.setattr(telegram_channels, "fetch_all", lambda getter=None: {})

    report_cli.main([
        "collect", "--market", "KR", "--date", date(2026, 9, 2).isoformat(),
        "--root", str(tmp_path), "--telegram",
    ])

    assert not (tmp_path / "data" / "news").exists()


def test_collect_telegram_skips_content_less_rows(tmp_path, monkeypatch):
    """텍스트도 이미지도 없는 행(clawnewssummary text_not_supported 등)은
    원장에 안 남긴다 — 그대로 두면 나중에 오너가 봇으로 포워딩한 실제 본문이
    append_ledger 의 dedup 에 가려 조용히 버려진다(2026-09-05 "포워딩 우회")."""
    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {
            "clawnewssummary": {"messages": [
                {"msg_id": "1", "text": "", "published": "2026-09-02T00:00:00Z",
                 "links": [], "images": []},
            ], "error": "미리보기 없음"},
            "tazastock": _msgs("tazastock", ["2"]),
        },
    )

    rc = report_cli.main([
        "collect", "--market", "KR", "--date", date(2026, 9, 2).isoformat(),
        "--root", str(tmp_path), "--telegram",
    ])

    assert rc == 0
    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    rows = telegram_channels.load_ledger(path)
    assert {(r["handle"], r["msg_id"]) for r in rows} == {("tazastock", "2")}


def test_collect_telegram_prunes_old_rows(tmp_path, monkeypatch):
    from quant.collect.sources.telegram_channels import append_ledger

    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    append_ledger([{"handle": "tazastock", "msg_id": "old", "text": "old",
                    "published": "2026-08-01T00:00:00Z", "links": [], "images": []}], path)

    monkeypatch.setattr(telegram_channels, "fetch_all", lambda getter=None: {})

    report_cli.main([
        "collect", "--market", "KR", "--date", date(2026, 9, 2).isoformat(),
        "--root", str(tmp_path), "--telegram",
    ])

    rows = telegram_channels.load_ledger(path)
    assert rows == []
