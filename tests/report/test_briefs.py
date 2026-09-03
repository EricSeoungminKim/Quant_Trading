"""텔레그램 브리핑 저장소 유니언(2026-09-03).

핵심 회귀: `_fetch_telegram_briefs`는 원래 `fetch_all()`의 채널당 최신 20개만
봤다 — 오후 빌드 시점엔 오전 메시지가 이미 그 20개 밖으로 밀려나 있을 수
있다(뉴스 RSS 와 같은 문제). 30분마다 도는 `telegram-collect` 수집기가 쌓은
원장(`telegram_msgs.jsonl`)을 `since` 창으로 읽어 `fetch_all()` 결과와
합친다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quant.report.collect.briefs import (
    _fetch_telegram_briefs,
    _merge_telegram_results,
    _telegram_default_window,
)

UTC = timezone.utc


def _msg(msg_id, text="t", published="2026-09-02T00:00:00Z"):
    return {"msg_id": msg_id, "text": text, "published": published, "links": [], "images": []}


def _store_row(handle, msg_id, text="stored", published="2026-09-02T00:00:00Z"):
    return {"handle": handle, "msg_id": msg_id, "text": text, "published": published,
            "links": [], "images": []}


# --- _merge_telegram_results: 순수 병합 로직 ---

def test_merge_unions_fresh_and_store_by_msg_id():
    fresh = {"tazastock": {"messages": [_msg("1")], "error": None}}
    store_rows = [_store_row("tazastock", "2"), _store_row("mootda", "1")]

    out = _merge_telegram_results(fresh, store_rows)

    assert {m["msg_id"] for m in out["tazastock"]["messages"]} == {"1", "2"}
    assert {m["msg_id"] for m in out["mootda"]["messages"]} == {"1"}


def test_merge_prefers_fresh_on_duplicate_msg_id():
    fresh = {"tazastock": {"messages": [_msg("1", text="신선한 버전")], "error": None}}
    store_rows = [_store_row("tazastock", "1", text="저장된 버전")]

    out = _merge_telegram_results(fresh, store_rows)

    assert len(out["tazastock"]["messages"]) == 1
    assert out["tazastock"]["messages"][0]["text"] == "신선한 버전"


def test_merge_sorts_messages_newest_first():
    fresh = {"tazastock": {"messages": [_msg("1", published="2026-09-02T00:00:00Z")], "error": None}}
    store_rows = [_store_row("tazastock", "2", published="2026-09-02T05:00:00Z")]

    out = _merge_telegram_results(fresh, store_rows)

    assert [m["msg_id"] for m in out["tazastock"]["messages"]] == ["2", "1"]


def test_merge_preserves_channel_error_from_fresh():
    fresh = {"tazastock": {"messages": [], "error": "ConnectionError: boom"}}
    out = _merge_telegram_results(fresh, [])
    assert out["tazastock"]["error"] == "ConnectionError: boom"


def test_merge_store_only_channel_has_no_error():
    """fresh 자체가 실패해 result={}로 들어와도(전체 실패) 저장소분만으로
    채널을 구성할 수 있다 — 그 경우 error는 None(모르는 게 아니라 없음)."""
    store_rows = [_store_row("tazastock", "1")]
    out = _merge_telegram_results({}, store_rows)
    assert out["tazastock"]["error"] is None
    assert len(out["tazastock"]["messages"]) == 1


def test_merge_empty_inputs_returns_empty_dict():
    assert _merge_telegram_results({}, []) == {}


# --- _telegram_default_window ---

def test_default_window_is_start_of_kst_day():
    now = datetime(2026, 9, 2, 23, 30, tzinfo=UTC)  # KST 09-03 08:30
    start = _telegram_default_window(now)
    kst = timezone(timedelta(hours=9))
    local = start.astimezone(kst)
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 9, 3, 0, 0)


# --- _fetch_telegram_briefs: 통합 (fetch_all + append_ledger + load_window 유니언) ---

def test_fetch_telegram_briefs_merges_store_with_fresh(tmp_path, monkeypatch):
    from quant.collect.sources import telegram_channels

    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"tazastock": {"messages": [_msg("1")], "error": None}},
    )
    monkeypatch.setattr(
        telegram_channels, "load_window",
        lambda path, since, until=None: [_store_row("tazastock", "2"), _store_row("mootda", "1")],
    )

    result = _fetch_telegram_briefs(tmp_path)

    assert {m["msg_id"] for m in result["tazastock"]["messages"]} == {"1", "2"}
    assert {m["msg_id"] for m in result["mootda"]["messages"]} == {"1"}


def test_fetch_telegram_briefs_survives_fetch_all_failure_using_store(tmp_path, monkeypatch):
    """fetch_all() 자체가 죽어도(레이트리밋 등) 저장소분으로는 계속 브리핑을
    만들 수 있어야 한다."""
    from quant.collect.sources import telegram_channels

    def boom(getter=None):
        raise ConnectionError("rate limited")

    monkeypatch.setattr(telegram_channels, "fetch_all", boom)
    monkeypatch.setattr(
        telegram_channels, "load_window",
        lambda path, since, until=None: [_store_row("tazastock", "9")],
    )

    result = _fetch_telegram_briefs(tmp_path)

    assert {m["msg_id"] for m in result["tazastock"]["messages"]} == {"9"}


def test_fetch_telegram_briefs_survives_store_read_failure(tmp_path, monkeypatch):
    """저장소 읽기가 죽어도 최소한 fresh 분으로는 브리핑이 나와야 한다."""
    from quant.collect.sources import telegram_channels

    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"tazastock": {"messages": [_msg("1")], "error": None}},
    )

    def boom(path, since, until=None):
        raise OSError("disk full")

    monkeypatch.setattr(telegram_channels, "load_window", boom)

    result = _fetch_telegram_briefs(tmp_path)

    assert {m["msg_id"] for m in result["tazastock"]["messages"]} == {"1"}


def test_fetch_telegram_briefs_appends_fresh_to_ledger(tmp_path, monkeypatch):
    from quant.collect.sources import telegram_channels

    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"tazastock": {"messages": [_msg("1")], "error": None}},
    )
    monkeypatch.setattr(telegram_channels, "load_window", lambda path, since, until=None: [])

    _fetch_telegram_briefs(tmp_path)

    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    assert path.exists()
    rows = telegram_channels.load_ledger(path)
    assert [(r["handle"], r["msg_id"]) for r in rows] == [("tazastock", "1")]


def test_fetch_telegram_briefs_honors_explicit_since(tmp_path, monkeypatch):
    from quant.collect.sources import telegram_channels

    seen = {}
    monkeypatch.setattr(telegram_channels, "fetch_all", lambda getter=None: {})

    def fake_load_window(path, since, until=None):
        seen["since"] = since
        return []

    monkeypatch.setattr(telegram_channels, "load_window", fake_load_window)

    custom_since = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    _fetch_telegram_briefs(tmp_path, since=custom_since)

    assert seen["since"] == custom_since
