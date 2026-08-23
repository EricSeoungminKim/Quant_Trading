from pathlib import Path

from quant.collect.sources.youtube_brief import (
    BRIEF_CHANNELS, fetch_briefs, fetch_channel_videos,
)

FIXTURE = Path(__file__).parent / "report" / "fixtures" / "youtube_brief_3protv.xml"
_SAMPRO_ID = "UChlv4GSd7OQl3js-jkLOnFA"  # 삼프로TV — 실측 확인된 channel_id


def test_brief_channels_has_at_least_two_verified_channels():
    """추측 금지 — id 를 확인한 채널만 최소 2개 등록돼 있어야 한다."""
    assert len(BRIEF_CHANNELS) >= 2
    assert all(cid.startswith("UC") for cid in BRIEF_CHANNELS.values())


def test_brief_channels_includes_soragehappa():
    """소라게아빠(§2026-08-17 사용자 지정) — RSS 실측으로 확인한 channel_id."""
    assert BRIEF_CHANNELS["소라게아빠"] == "UCND_HhRw8lbvJSJ4oFvbAAw"


def test_fetch_channel_videos_parses_real_rss_fragment():
    """실 유튜브 RSS 조각(삼프로TV, 2026-08-16 실측 저장)을 파싱한다."""
    xml = FIXTURE.read_text(encoding="utf-8")
    videos = fetch_channel_videos(_SAMPRO_ID, getter=lambda url: xml, limit=3)
    assert len(videos) == 2
    assert videos[0]["title"].startswith("[한국어] 켄 피셔 회장")
    assert videos[0]["link"] == "https://www.youtube.com/watch?v=i-4V4E8izfI"
    assert videos[0]["published"] == "2026-08-15T06:00:14+00:00"
    assert videos[0]["channel"] == "삼프로TV"


def test_fetch_channel_videos_respects_limit():
    xml = FIXTURE.read_text(encoding="utf-8")
    videos = fetch_channel_videos(_SAMPRO_ID, getter=lambda url: xml, limit=1)
    assert len(videos) == 1


def test_fetch_channel_videos_passes_channel_id_in_url():
    seen = {}

    def getter(url):
        seen["url"] = url
        return FIXTURE.read_text(encoding="utf-8")

    fetch_channel_videos(_SAMPRO_ID, getter=getter)
    assert _SAMPRO_ID in seen["url"]
    assert seen["url"].startswith("https://www.youtube.com/feeds/videos.xml")


def test_fetch_channel_videos_network_failure_returns_empty_list():
    """채널 하나가 죽어도(네트워크 실패) 예외 없이 빈 리스트 — 그 채널만 생략."""
    def boom(url):
        raise ConnectionError("boom")

    assert fetch_channel_videos("UCdead00000000000000000", getter=boom) == []


def test_fetch_channel_videos_broken_xml_returns_empty_list():
    assert fetch_channel_videos(_SAMPRO_ID, getter=lambda url: "<not xml", limit=3) == []


def test_fetch_briefs_skips_failed_channel_keeps_others():
    """§요구사항: 실패 채널은 그 채널만 빠지고, 성공한 채널은 그대로 남는다."""
    xml = FIXTURE.read_text(encoding="utf-8")

    def getter(url):
        if _SAMPRO_ID in url:
            return xml
        raise ConnectionError("dead channel")

    result = fetch_briefs(getter=getter)
    assert "삼프로TV" in result
    assert len(result["삼프로TV"]) == 2
    # 나머지 등록 채널은 이 테스트의 getter 에서 전부 실패하도록 만들었으므로 빠져야 한다
    assert len(result) == 1


def test_fetch_briefs_returns_empty_dict_when_all_channels_fail():
    """전 채널 실패면 섹션 자체를 생략할 수 있도록 빈 dict(빈 섹션 아님)를 돌려준다."""
    def boom(url):
        raise ConnectionError("boom")

    assert fetch_briefs(getter=boom) == {}
