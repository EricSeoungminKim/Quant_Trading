"""텔레그램 공개 채널 프리뷰 파서. 픽스처는 2026-08-17 실측(`curl https://t.me/s/tazastock`)
저장분이다 — 추측이 아니라 실제 HTML(`telegram_tazastock.html`: 메시지 5건,
`telegram_preview_disabled.html`: `report_figure_by_offset` 리다이렉트 목적지).
"""
import json
from pathlib import Path

from quant.collect.sources.telegram_channels import (
    CHANNELS,
    append_ledger,
    channels_for,
    fetch_all,
)

_TAZASTOCK_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_tazastock.html"
_DISABLED_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_preview_disabled.html"


def test_channels_has_14_entries():
    assert len(CHANNELS) == 14


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
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    disabled = _DISABLED_FIXTURE.read_text(encoding="utf-8")

    def getter(url):
        return disabled if "report_figure_by_offset" in url else html

    result = fetch_all(getter=getter, sleep=lambda s: None)
    assert result["report_figure_by_offset"]["messages"] == []
    assert result["report_figure_by_offset"]["error"] is not None
    assert "프리뷰" in result["report_figure_by_offset"]["error"]


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
