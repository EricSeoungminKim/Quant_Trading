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
    fetch_channel,
)

_TAZASTOCK_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_tazastock.html"
_DISABLED_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_preview_disabled.html"
_PIKACHU_PHOTO_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_pikachu_photo.html"
_WALTERBLOOMBERG_FIXTURE = Path(__file__).parent / "report" / "fixtures" / "telegram_walterbloomberg.html"
_TAZASTOCK_URL = "https://t.me/s/tazastock"


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


def test_fetch_channel_parses_walterbloomberg_real_html_fixture():
    """새 usnews 채널도 기존 파서가 그대로 태운다 — 하드코딩 분기 없음을 실증.

    픽스처는 2026-08-17T13:30:24Z 실측(`curl https://t.me/s/walterbloomberg`)에서
    메시지 3건(34446~34448)을 그대로 잘라 저장한 것 — 추측이 아니라 실제 HTML.
    """
    html = _WALTERBLOOMBERG_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("walterbloomberg", getter=lambda url: html)
    assert [m["msg_id"] for m in messages] == ["34448", "34447", "34446"]
    assert messages[-1]["published"] == "2026-08-17T12:05:47+00:00"
    assert "RAYTHEON" in messages[-1]["text"]


def test_fetch_channel_parses_real_html_fixture_newest_first():
    """실 HTML 조각(tazastock, msg 75633~75637)을 파싱한다 — 최신순(75637이 먼저)."""
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("tazastock", getter=lambda url: html)
    assert len(messages) == 5
    assert [m["msg_id"] for m in messages] == ["75637", "75636", "75635", "75634", "75633"]
    newest = messages[0]
    assert newest["published"] == "2026-08-16T09:45:28+00:00"
    assert "앤트로픽" in newest["text"]
    assert newest["links"] == ["https://n.news.naver.com/article/016/0002684459"]


def test_fetch_channel_message_without_link_has_empty_links():
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("tazastock", getter=lambda url: html)
    oldest = messages[-1]
    assert oldest["msg_id"] == "75633"
    assert oldest["links"] == []


def test_fetch_channel_respects_limit_via_module_constant():
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("tazastock", getter=lambda url: html)
    # 픽스처엔 5건뿐이라 상한(20)에 걸리지 않는다 — 상한 이하는 전부 반환돼야 한다
    assert len(messages) == 5


def test_fetch_channel_passes_preview_url_to_getter():
    seen = {}

    def getter(url):
        seen["url"] = url
        return _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")

    fetch_channel("tazastock", getter=getter)
    assert seen["url"] == _TAZASTOCK_URL


def test_fetch_channel_network_failure_returns_empty_list():
    def boom(url):
        raise ConnectionError("boom")

    assert fetch_channel("tazastock", getter=boom) == []


def test_fetch_channel_broken_html_returns_empty_list():
    assert fetch_channel("tazastock", getter=lambda url: "<<<not html") == []


# ── 사진(서브프로젝트 S part 3) ──────────────────────────────────────────
# 픽스처는 2026-08-17 재실측(`curl https://t.me/s/pikachu_aje`)에서 실제 메시지
# 3건(8213 텍스트만/8215 사진만·캡션 없음/8221 사진+캡션)을 그대로 잘라 저장한 것.


def test_fetch_channel_photo_only_message_has_image_url_and_empty_text():
    html = _PIKACHU_PHOTO_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("pikachu_aje", getter=lambda url: html)
    by_id = {m["msg_id"]: m for m in messages}
    assert len(by_id["8215"]["images"]) == 1
    assert by_id["8215"]["images"][0].startswith(
        "https://cdn5.telesco.pe/file/UOrSVRHDF4ztNpyRw"
    )
    assert by_id["8215"]["text"] == ""


def test_fetch_channel_photo_with_caption_has_both_image_and_text():
    html = _PIKACHU_PHOTO_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("pikachu_aje", getter=lambda url: html)
    by_id = {m["msg_id"]: m for m in messages}
    assert len(by_id["8221"]["images"]) == 1
    assert by_id["8221"]["images"][0].startswith("https://cdn5.telesco.pe/file/k8v4nf5MFhXK")
    assert "인형뽑기" in by_id["8221"]["text"]


def test_fetch_channel_text_only_message_has_empty_images_list():
    html = _PIKACHU_PHOTO_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("pikachu_aje", getter=lambda url: html)
    by_id = {m["msg_id"]: m for m in messages}
    assert by_id["8213"]["images"] == []


def test_fetch_channel_messages_without_photos_still_have_images_key():
    """기존(사진 없는) 픽스처도 `images` 키를 갖는다 — 스키마 하위호환."""
    html = _TAZASTOCK_FIXTURE.read_text(encoding="utf-8")
    messages = fetch_channel("tazastock", getter=lambda url: html)
    assert messages
    assert all(m["images"] == [] for m in messages)


def test_fetch_channel_preview_disabled_returns_empty_list():
    """`report_figure_by_offset`처럼 프리뷰가 꺼진 채널의 리다이렉트 목적지(실측 저장)
    — 예외 없이 빈 리스트."""
    html = _DISABLED_FIXTURE.read_text(encoding="utf-8")
    assert fetch_channel("report_figure_by_offset", getter=lambda url: html) == []


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
