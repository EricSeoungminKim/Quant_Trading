"""텔레그램 공개 채널 프리뷰 파서. 픽스처는 2026-08-17 실측(`curl https://t.me/s/tazastock`)
저장분이다 — 추측이 아니라 실제 HTML(`telegram_tazastock.html`: 메시지 5건,
`telegram_preview_disabled.html`: 2026-09-03 F8로 제거된 report_figure_by_offset
채널의 리다이렉트 목적지 — 채널 등록과 무관하게 "프리뷰 꺼짐" HTML 모양 자체를
검증하는 데 계속 쓴다, 아래 `test_fetch_all_records_reason_for_disabled_preview`).
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

from quant.collect.sources.telegram_channels import (
    CHANNELS,
    append_ledger,
    channels_for,
    fetch_all,
    load_window,
    prune,
)

UTC = timezone.utc

_TAZASTOCK_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_tazastock.html"
_DISABLED_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_preview_disabled.html"


def test_channels_has_13_entries():
    """2026-09-03(F8) — report_figure_by_offset 제거로 14 → 13(모듈 docstring
    "실측 확인" 절 참고: 등록 후 3주 넘게 프리뷰가 영구히 꺼져 있었다)."""
    assert len(CHANNELS) == 13


def test_channels_entries_have_required_fields():
    for entry in CHANNELS:
        assert entry["handle"]
        assert entry["분류"]
        assert entry["market"] in ("KR", "US", "BOTH")
        assert entry["tier"] in ("sector", "macro", "news", "usnews", "usdigest")


def test_channels_has_unique_handles():
    handles = [c["handle"] for c in CHANNELS]
    assert len(handles) == len(set(handles))


def test_channels_for_kr_includes_kr_and_both():
    kr = channels_for("KR")
    handles = {c["handle"] for c in kr}
    assert "pikachu_aje" in handles  # market: KR
    assert "yieldnspread" in handles  # market: BOTH
    assert "insidertracking" not in handles  # market: US only


def test_channels_for_us_includes_us_and_both():
    us = channels_for("US")
    handles = {c["handle"] for c in us}
    assert "insidertracking" in handles  # market: US
    assert "rafikiresearch" in handles  # market: BOTH
    assert "pikachu_aje" not in handles  # market: KR only


def test_channels_has_usnews_tier_channels():
    """서브프로젝트 W part 1(2026-08-17) — 시간당 US 뉴스 채널 2개."""
    usnews = [c for c in CHANNELS if c["tier"] == "usnews"]
    handles = {c["handle"] for c in usnews}
    assert handles == {"walterbloomberg", "financialjuice"}
    for entry in usnews:
        assert entry["market"] == "US"


def test_fetch_all_returns_entry_per_channel():
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    result = fetch_all(getter=lambda url: html, sleep=lambda s: None)
    assert set(result.keys()) == {c["handle"] for c in CHANNELS}
    for entry in result.values():
        assert "messages" in entry
        assert "error" in entry


def test_fetch_all_isolates_per_channel_failure():
    """한 채널 네트워크 실패가 다른 채널 결과를 막지 않는다."""
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")

    def getter(url):
        if "pikachu_aje" in url:
            raise ConnectionError("boom")
        return html

    result = fetch_all(getter=getter, sleep=lambda s: None)
    assert result["pikachu_aje"]["messages"] == []
    assert result["pikachu_aje"]["error"] is not None
    assert "ConnectionError" in result["pikachu_aje"]["error"]
    # 다른 채널은 정상 수집됐다
    assert len(result["tazastock"]["messages"]) == 5
    assert result["tazastock"]["error"] is None


def test_fetch_all_records_reason_for_disabled_preview():
    """이 메커니즘 자체의 회귀 가드 — 2026-09-03(F8)에 등록이 제거된
    report_figure_by_offset이 실측으로 걸렸던 바로 그 "프리뷰 꺼짐" HTML 모양
    (`telegram_preview_disabled.html`)을, 지금 등록된 아무 채널(pikachu_aje)에
    물려도 fetch_all이 똑같이 사유를 남기는지 확인한다."""
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    disabled = _DISABLED_FIXTURE.read_text(encoding="utf-8")

    def getter(url):
        return disabled if "pikachu_aje" in url else html

    result = fetch_all(getter=getter, sleep=lambda s: None)
    assert result["pikachu_aje"]["messages"] == []
    assert result["pikachu_aje"]["error"] is not None
    assert "프리뷰" in result["pikachu_aje"]["error"]


def test_fetch_all_sleeps_between_every_channel():
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    sleeps = []
    fetch_all(getter=lambda url: html, sleep=lambda s: sleeps.append(s))
    assert sleeps == [0.5] * len(CHANNELS)


def test_append_ledger_dedup_by_handle_and_msg_id(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    rows = [
        {"handle": "tazastock", "msg_id": "1", "text": "a"},
        {"handle": "tazastock", "msg_id": "2", "text": "b"},
    ]
    added1 = append_ledger(rows, path)
    assert added1 == 2

    # 같은 (handle, msg_id) 재삽입 시 0건, 다른 채널의 같은 msg_id는 별개 키라 추가된다
    more = rows + [
        {"handle": "mootda", "msg_id": "1", "text": "c"},  # 다른 채널의 msg_id=1 — 충돌 아님
        {"handle": "tazastock", "msg_id": "3", "text": "d"},
    ]
    added2 = append_ledger(more, path)
    assert added2 == 2

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    keys = [(json.loads(line)["handle"], json.loads(line)["msg_id"]) for line in lines]
    assert keys == [("tazastock", "1"), ("tazastock", "2"), ("mootda", "1"), ("tazastock", "3")]


def test_append_ledger_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "telegram_msgs.jsonl"
    added = append_ledger([{"handle": "tazastock", "msg_id": "1", "text": "a"}], path)
    assert added == 1
    assert path.exists()


def test_append_ledger_empty_rows_noop(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    added = append_ledger([], path)
    assert added == 0
    assert not path.exists()


def test_append_ledger_skips_rows_missing_keys(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    added = append_ledger([{"handle": "tazastock", "text": "no msg_id"}], path)
    assert added == 0


# --- 저장소 창 읽기 (`load_window`, 2026-09-03) ---
#
# `fetch_all()`은 채널당 최신 20개뿐이다 — 오후 빌드 시점엔 오전 메시지가 이미
# 그 20개 밖으로 밀려나 있을 수 있다. `load_window`는 30분마다 도는 수집기
# (`telegram-collect`)가 쌓은 원장에서 리포트 창에 해당하는 메시지를 읽는다.

def _row(handle, msg_id, published, text="t"):
    return {"handle": handle, "msg_id": msg_id, "text": text, "published": published,
            "links": [], "images": []}


def test_load_window_filters_by_published_range(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    append_ledger([
        _row("tazastock", "1", "2026-09-02T22:00:00Z"),  # 창 밖(이전)
        _row("tazastock", "2", "2026-09-03T00:30:00Z"),  # 창 안
        _row("tazastock", "3", "2026-09-03T05:00:00Z"),  # 창 밖(이후)
    ], path)

    rows = load_window(
        path,
        since=datetime(2026, 9, 3, 0, 0, tzinfo=UTC),
        until=datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
    )
    assert [r["msg_id"] for r in rows] == ["2"]


def test_load_window_keeps_undated_rows(tmp_path):
    """발행시각을 못 읽은 행은 창 밖이어도 버리지 않는다 — collector.load_window와
    같은 원칙."""
    path = tmp_path / "telegram_msgs.jsonl"
    append_ledger([_row("tazastock", "1", None)], path)

    rows = load_window(path, since=datetime(2026, 9, 3, tzinfo=UTC))
    assert [r["msg_id"] for r in rows] == ["1"]


def test_load_window_sorts_newest_first(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    append_ledger([
        _row("tazastock", "1", "2026-09-03T00:00:00Z"),
        _row("tazastock", "2", "2026-09-03T02:00:00Z"),
        _row("mootda", "1", "2026-09-03T01:00:00Z"),
    ], path)

    rows = load_window(
        path, since=datetime(2026, 9, 2, tzinfo=UTC), until=datetime(2026, 9, 3, 3, 0, tzinfo=UTC),
    )
    assert [(r["handle"], r["msg_id"]) for r in rows] == [
        ("tazastock", "2"), ("mootda", "1"), ("tazastock", "1"),
    ]


def test_load_window_missing_file_returns_empty(tmp_path):
    assert load_window(tmp_path / "nope.jsonl", since=datetime(2026, 9, 3, tzinfo=UTC)) == []


# --- 보존 (`prune`, 2026-09-03) ---

def test_prune_removes_rows_older_than_keep_days(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    append_ledger([
        _row("tazastock", "1", "2026-08-01T00:00:00Z"),  # 오래됨
        _row("tazastock", "2", "2026-09-01T00:00:00Z"),  # 최근
    ], path)

    removed = prune(path, today=date(2026, 9, 3), keep_days=14)

    assert removed == 1
    remaining = load_window(path, since=datetime(2026, 1, 1, tzinfo=UTC))
    assert [r["msg_id"] for r in remaining] == ["2"]


def test_prune_keeps_undated_rows(tmp_path):
    path = tmp_path / "telegram_msgs.jsonl"
    append_ledger([_row("tazastock", "1", None)], path)

    removed = prune(path, today=date(2026, 9, 3), keep_days=14)

    assert removed == 0


def test_prune_missing_file_is_noop(tmp_path):
    assert prune(tmp_path / "nope.jsonl", today=date(2026, 9, 3)) == 0
