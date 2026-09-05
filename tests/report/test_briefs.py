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


# --- `preview: False` 채널 예외(2026-09-05, "포워딩 우회" 절) ---
#
# clawnewssummary 는 실제 CHANNELS 레지스트리에 preview:False 로 등록돼 있다
# (tests/test_telegram_channels.py 가 그 사실 자체를 검증). 여기서는
# `_merge_telegram_results`가 그 플래그를 실제로 어떻게 쓰는지 검증한다.


def test_merge_prefers_ledger_content_over_empty_fresh_for_preview_false_channel():
    """fresh(스크레이핑)는 text_not_supported 라 msg_id는 있지만 본문이 항상
    비어 있다 — 오너가 봇으로 포워딩해 원장에 쌓인 실제 본문이 "fresh 우선"
    규칙에 가려지면 안 된다."""
    fresh = {"clawnewssummary": {
        "messages": [{"msg_id": "1", "text": "", "published": "2026-09-05T00:00:00Z",
                      "links": [], "images": []}],
        "error": "미리보기 없음 — 메시지는 있으나 본문이 웹 프리뷰 미지원 형식",
    }}
    store_rows = [_store_row("clawnewssummary", "1", text="오너가 포워딩한 실제 본문")]

    out = _merge_telegram_results(fresh, store_rows)

    assert len(out["clawnewssummary"]["messages"]) == 1
    assert out["clawnewssummary"]["messages"][0]["text"] == "오너가 포워딩한 실제 본문"


def test_merge_clears_preview_error_when_ledger_has_real_content():
    fresh = {"clawnewssummary": {
        "messages": [{"msg_id": "1", "text": "", "published": "2026-09-05T00:00:00Z",
                      "links": [], "images": []}],
        "error": "미리보기 없음 — 메시지는 있으나 본문이 웹 프리뷰 미지원 형식",
    }}
    store_rows = [_store_row("clawnewssummary", "1", text="실제 본문")]

    out = _merge_telegram_results(fresh, store_rows)

    assert out["clawnewssummary"]["error"] is None


def test_merge_keeps_preview_error_when_ledger_has_no_content_yet():
    """아직 아무도 포워딩하지 않았으면(원장에 내용 없음) "미리보기 없음"
    오류가 정직하게 그대로 남아야 한다."""
    fresh = {"clawnewssummary": {
        "messages": [{"msg_id": "1", "text": "", "published": "2026-09-05T00:00:00Z",
                      "links": [], "images": []}],
        "error": "미리보기 없음 — 메시지는 있으나 본문이 웹 프리뷰 미지원 형식",
    }}

    out = _merge_telegram_results(fresh, [])

    assert out["clawnewssummary"]["error"] == "미리보기 없음 — 메시지는 있으나 본문이 웹 프리뷰 미지원 형식"


def test_merge_normal_channel_unaffected_by_preview_false_exception():
    """preview:False 가 아닌 채널(tazastock)은 기존 "fresh 우선" 규칙 그대로다."""
    fresh = {"tazastock": {"messages": [_msg("1", text="신선한 버전")], "error": None}}
    store_rows = [_store_row("tazastock", "1", text="저장된 버전")]

    out = _merge_telegram_results(fresh, store_rows)

    assert out["tazastock"]["messages"][0]["text"] == "신선한 버전"


# --- 내용 없는 행은 원장에 남기지 않는다(2026-09-05, "포워딩 우회" 절) ---


def test_fetch_telegram_briefs_does_not_persist_content_less_rows(tmp_path, monkeypatch):
    """텍스트도 이미지도 없는 행(clawnewssummary text_not_supported 등)을
    그대로 원장에 적으면 append_ledger 의 (handle,msg_id) dedup 이 그 자리를
    선점해, 나중에 오너가 봇으로 포워딩한 실제 본문이 조용히 버려진다."""
    from quant.collect.sources import telegram_channels

    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"clawnewssummary": {
            "messages": [{"msg_id": "1", "text": "", "published": "2026-09-05T00:00:00Z",
                          "links": [], "images": []}],
            "error": "미리보기 없음",
        }},
    )
    monkeypatch.setattr(telegram_channels, "load_window", lambda path, since, until=None: [])

    _fetch_telegram_briefs(tmp_path)

    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    rows = telegram_channels.load_ledger(path) if path.exists() else []
    assert rows == []


def test_fetch_telegram_briefs_still_persists_rows_with_images_but_no_text(tmp_path, monkeypatch):
    from quant.collect.sources import telegram_channels

    monkeypatch.setattr(
        telegram_channels, "fetch_all",
        lambda getter=None: {"pikachu_aje": {
            "messages": [{"msg_id": "1", "text": "", "published": "2026-09-05T00:00:00Z",
                          "links": [], "images": ["https://cdn.example/x.jpg"]}],
            "error": None,
        }},
    )
    monkeypatch.setattr(telegram_channels, "load_window", lambda path, since, until=None: [])

    _fetch_telegram_briefs(tmp_path)

    path = tmp_path / "data" / "ledger" / "telegram_msgs.jsonl"
    rows = telegram_channels.load_ledger(path)
    assert [(r["handle"], r["msg_id"]) for r in rows] == [("pikachu_aje", "1")]


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
