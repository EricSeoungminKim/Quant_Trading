"""`quant.analyze.telegram_view` — 텔레그램 인사이트 뷰 + 종목 언급 가산점
입력 (서브프로젝트 S part 2)."""
from __future__ import annotations

from quant.analyze.telegram_view import (
    IMAGE_DESC_BUDGET,
    _summarize,
    build_telegram_view,
    describe_sector_images,
    narrate_channels,
    telegram_mentions,
)


class _RecordingNarrator:
    """프롬프트를 저장하고, 사전 지정한 텍스트(또는 None)를 돌려준다(section_advice
    테스트의 `_RecordingNarrator`와 같은 패턴)."""

    def __init__(self, reply):
        self._reply = reply
        self.prompts: list[str] = []

    def narrate(self, prompt: str):
        self.prompts.append(prompt)
        return self._reply


# ── _summarize ───────────────────────────────────────────────────────────


def test_summarize_empty_text_returns_empty_string():
    assert _summarize("") == ""
    assert _summarize(None) == ""


def test_summarize_picks_first_sentence_when_short():
    assert _summarize("전력 테마 강세. 자세한 내용은 링크 참고") == "전력 테마 강세"


def test_summarize_truncates_long_text_without_sentence_break():
    text = "가" * 200
    out = _summarize(text)
    assert len(out) == 121  # 120자 + '…'
    assert out.endswith("…")


def test_summarize_short_text_without_period_returned_as_is():
    assert _summarize("짧은 메시지") == "짧은 메시지"


def test_summarize_collapses_newlines_and_repeated_whitespace():
    assert _summarize("첫줄만 본다\n둘째줄은 버려진다") == "첫줄만 본다"


# ── build_telegram_view ─────────────────────────────────────────────────


def _msgs(handle: str, texts: list[str], error: str | None = None,
          images: list[list[str]] | None = None) -> dict:
    imgs = images or [[] for _ in texts]
    return {
        handle: {
            "messages": [
                {"msg_id": str(100 + i), "text": t, "published": "2026-08-17T00:00:00+00:00",
                 "images": imgs[i]}
                for i, t in enumerate(texts)
            ],
            "error": error,
        },
    }


def test_build_telegram_view_filters_by_market():
    view = build_telegram_view(_msgs("pikachu_aje", ["전력 강세"]), "KR")
    handles = {c["handle"] for c in view}
    assert "pikachu_aje" in handles  # market: KR
    assert "insidertracking" not in handles  # market: US only


def test_build_telegram_view_includes_channel_with_no_messages_and_no_error():
    view = build_telegram_view({}, "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    assert pikachu["msgs"] == []
    assert pikachu["error"] is None


def test_build_telegram_view_honestly_reports_error():
    view = build_telegram_view(
        _msgs("tazastock", [], error="no preview (프리뷰 비활성)"), "KR",
    )
    taza = next(c for c in view if c["handle"] == "tazastock")
    assert taza["msgs"] == []
    assert taza["error"] == "no preview (프리뷰 비활성)"


def test_build_telegram_view_caps_at_5_items_per_channel():
    texts = [f"메시지 {i}" for i in range(8)]
    view = build_telegram_view(_msgs("pikachu_aje", texts), "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    assert len(pikachu["msgs"]) == 5


def test_build_telegram_view_item_has_link_and_hhmm():
    view = build_telegram_view(_msgs("pikachu_aje", ["전력 강세"]), "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    item = pikachu["msgs"][0]
    assert item["link"] == "https://t.me/pikachu_aje/100"
    assert item["text"] == "전력 강세"
    assert item["published_hhmm"] == "09:00"  # UTC 00:00 → KST 09:00


def test_build_telegram_view_item_has_zero_image_count_and_none_first_image_url_by_default():
    view = build_telegram_view(_msgs("pikachu_aje", ["전력 강세"]), "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    item = pikachu["msgs"][0]
    assert item["image_count"] == 0
    assert item["first_image_url"] is None


def test_build_telegram_view_item_carries_image_count_and_first_image_url():
    msgs = _msgs("pikachu_aje", ["차트 첨부"],
                  images=[["https://cdn5.telesco.pe/a.jpg", "https://cdn5.telesco.pe/b.jpg"]])
    view = build_telegram_view(msgs, "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    item = pikachu["msgs"][0]
    assert item["image_count"] == 2
    assert item["first_image_url"] == "https://cdn5.telesco.pe/a.jpg"


def test_build_telegram_view_missing_published_gives_none_hhmm():
    view = build_telegram_view(
        {"pikachu_aje": {"messages": [{"msg_id": "1", "text": "x", "published": None}], "error": None}},
        "KR",
    )
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    assert pikachu["msgs"][0]["published_hhmm"] is None


def test_build_telegram_view_carries_classification_and_tier():
    view = build_telegram_view({}, "KR")
    pikachu = next(c for c in view if c["handle"] == "pikachu_aje")
    assert pikachu["분류"] == "전력 섹터"
    assert pikachu["tier"] == "sector"


def test_build_telegram_view_preserves_channel_registration_order():
    from quant.collect.sources.telegram_channels import channels_for

    view = build_telegram_view({}, "US")
    handles = [c["handle"] for c in view]
    assert handles == [c["handle"] for c in channels_for("US")]


# ── telegram_mentions ────────────────────────────────────────────────────

_KR_TABLE = [("삼성전자", "005930"), ("SK하이닉스", "000660"), ("태양", "999999")]


def test_telegram_mentions_kr_extracts_symbol_and_channel_metadata():
    msgs = _msgs("pikachu_aje", ["삼성전자 강세, 전력 테마 동반 상승"])
    out = telegram_mentions(msgs, _KR_TABLE, "KR")
    assert "005930" in out
    rec = out["005930"]
    assert rec["sector_room"] is True
    assert rec["channels"] == [{"handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector"}]


def test_telegram_mentions_kr_anti_collision_does_not_match_substring():
    """'태양광 산업' 안의 '태양'을 오탐하지 않는다 — entities.extract 의
    경계 검사(조사 뒤 한글이면 조각으로 판정) 재사용 확인."""
    msgs = _msgs("pikachu_aje", ["태양광 산업 관련 뉴스입니다"])
    out = telegram_mentions(msgs, _KR_TABLE, "KR")
    assert "999999" not in out


def test_telegram_mentions_kr_no_hits_returns_empty_dict():
    msgs = _msgs("pikachu_aje", ["오늘은 특별한 종목 언급이 없습니다"])
    out = telegram_mentions(msgs, _KR_TABLE, "KR")
    assert out == {}


def test_telegram_mentions_kr_news_tier_channel_not_sector_room():
    msgs = _msgs("tazastock", ["삼성전자 목표가 상향"])
    out = telegram_mentions(msgs, _KR_TABLE, "KR")
    assert out["005930"]["sector_room"] is False
    assert out["005930"]["channels"][0]["tier"] == "news"


def test_telegram_mentions_kr_same_symbol_multiple_channels_dedup_and_flags_sector():
    msgs = {
        "pikachu_aje": {"messages": [{"msg_id": "1", "text": "삼성전자 전력 관련 호재"}], "error": None},
        "tazastock": {"messages": [{"msg_id": "2", "text": "삼성전자 목표가 상향"}], "error": None},
    }
    out = telegram_mentions(msgs, _KR_TABLE, "KR")
    rec = out["005930"]
    assert rec["sector_room"] is True
    handles = {c["handle"] for c in rec["channels"]}
    assert handles == {"pikachu_aje", "tazastock"}
    assert len(rec["channels"]) == 2  # 같은 채널 중복 없음


def test_telegram_mentions_us_validates_against_known_tickers_only():
    # rafikiresearch: market "BOTH" — US 필터(channels_for("US"))에 걸린다
    # (2026-09-05 채널 재편으로 insidertracking 은 더 이상 등록돼 있지 않다).
    msgs = _msgs("rafikiresearch", ["AAPL surges as IT spending rises"])
    out = telegram_mentions(msgs, {"AAPL"}, "US")
    assert set(out.keys()) == {"AAPL"}  # "IT"는 화이트리스트 밖이라 무시된다


def test_telegram_mentions_us_lowercase_ticker_ignored():
    msgs = _msgs("rafikiresearch", ["aapl mentioned casually"])
    out = telegram_mentions(msgs, {"AAPL"}, "US")
    assert out == {}


def test_telegram_mentions_empty_msgs_by_handle_returns_empty_dict():
    assert telegram_mentions({}, _KR_TABLE, "KR") == {}


# ── narrate_channels ─────────────────────────────────────────────────────


def _view_with_items():
    return build_telegram_view(_msgs("pikachu_aje", ["전력 테마 강세"]), "KR")


def test_narrate_channels_returns_none_when_no_channel_has_items():
    view = build_telegram_view({}, "KR")  # 전 채널 msgs 빈 리스트
    narrator = _RecordingNarrator("아무 응답")
    assert narrate_channels(view, narrator) is None
    assert narrator.prompts == []  # 빈 입력으로 LLM 을 부르지 않는다


def test_narrate_channels_parses_response_for_present_channels():
    view = _view_with_items()
    narrator = _RecordingNarrator("[pikachu_aje]\n전력 테마가 강세를 보이고 있다.")
    result = narrate_channels(view, narrator)
    assert result == {"pikachu_aje": "전력 테마가 강세를 보이고 있다."}


def test_narrate_channels_returns_none_when_narrator_fails():
    view = _view_with_items()
    narrator = _RecordingNarrator(None)
    assert narrate_channels(view, narrator) is None


def test_narrate_channels_returns_none_when_marker_missing():
    view = _view_with_items()
    narrator = _RecordingNarrator("전력 테마가 강세다.")  # [pikachu_aje] 마커 없음
    assert narrate_channels(view, narrator) is None


def test_narrate_channels_returns_none_when_content_empty():
    view = _view_with_items()
    narrator = _RecordingNarrator("[pikachu_aje]\n")
    assert narrate_channels(view, narrator) is None


def test_narrate_channels_only_prompts_for_channels_with_items():
    view = build_telegram_view(_msgs("pikachu_aje", ["전력 강세"]), "KR")
    narrator = _RecordingNarrator("[pikachu_aje]\n전력 강세 지속.")
    narrate_channels(view, narrator)
    assert len(narrator.prompts) == 1
    prompt = narrator.prompts[0]
    assert "[pikachu_aje]" in prompt
    assert "[tazastock]" not in prompt  # 메시지 없는 채널은 프롬프트에서 빠진다


# ── describe_sector_images ──────────────────────────────────────────────


def _recording_describe(replies: dict[str, str | None] | None = None):
    """`url -> str|None` 콜러블 + 호출 기록. `replies`에 없는 url은 고정 문구."""
    replies = replies or {}
    calls: list[str] = []

    def describe(url: str) -> str | None:
        calls.append(url)
        if url in replies:
            return replies[url]
        return f"해석: {url}"

    describe.calls = calls  # type: ignore[attr-defined]
    return describe


def test_describe_sector_images_skips_non_sector_channels():
    # tazastock: tier="news" — 사진이 있어도 해석 대상이 아니다.
    msgs = _msgs("tazastock", ["장중 시황"], images=[["https://cdn5.telesco.pe/a.jpg"]])
    view = build_telegram_view(msgs, "KR")
    describe = _recording_describe()
    out = describe_sector_images(view, describe)
    assert out == {}
    assert describe.calls == []


def test_describe_sector_images_describes_sector_room_photo():
    # pikachu_aje: tier="sector"
    msgs = _msgs("pikachu_aje", ["전력 테마"], images=[["https://cdn5.telesco.pe/a.jpg"]])
    view = build_telegram_view(msgs, "KR")
    describe = _recording_describe({"https://cdn5.telesco.pe/a.jpg": "발전 설비 사진."})
    out = describe_sector_images(view, describe)
    link = next(c for c in view if c["handle"] == "pikachu_aje")["msgs"][0]["link"]
    assert out == {link: "발전 설비 사진."}


def test_describe_sector_images_skips_items_without_photos():
    msgs = _msgs("pikachu_aje", ["텍스트만"])  # images 기본값 [] — 사진 없음
    view = build_telegram_view(msgs, "KR")
    describe = _recording_describe()
    out = describe_sector_images(view, describe)
    assert out == {}
    assert describe.calls == []


def test_describe_sector_images_drops_failed_calls_but_still_spends_budget():
    msgs = _msgs("pikachu_aje", ["사진1", "사진2"],
                  images=[["https://cdn5.telesco.pe/a.jpg"], ["https://cdn5.telesco.pe/b.jpg"]])
    view = build_telegram_view(msgs, "KR")
    describe = _recording_describe({"https://cdn5.telesco.pe/a.jpg": None})
    out = describe_sector_images(view, describe)
    # a.jpg 는 호출됐지만 실패(None) → 결과에 없음. b.jpg 는 성공.
    assert len(describe.calls) == 2
    assert len(out) == 1


def test_describe_sector_images_stops_at_budget_cap():
    # 4장짜리 사진(예산은 3) — 4번째는 호출 자체가 안 된다.
    texts = [f"사진{i}" for i in range(4)]
    images = [[f"https://cdn5.telesco.pe/{i}.jpg"] for i in range(4)]
    msgs = _msgs("pikachu_aje", texts, images=images)
    view = build_telegram_view(msgs, "KR")
    describe = _recording_describe()
    describe_sector_images(view, describe)
    assert len(describe.calls) == IMAGE_DESC_BUDGET == 3


def test_describe_sector_images_budget_shared_across_sector_channels():
    # pikachu_aje(전력 섹터)와 Samsung_Global_AI_SW(글로벌 AI/SW)는 둘 다
    # tier="sector" — 예산은 두 채널을 합쳐 3장이다.
    view = build_telegram_view(
        {
            **_msgs("pikachu_aje", ["a", "b"],
                    images=[["https://cdn5.telesco.pe/a.jpg"], ["https://cdn5.telesco.pe/b.jpg"]]),
            **_msgs("Samsung_Global_AI_SW", ["c", "d"],
                    images=[["https://cdn5.telesco.pe/c.jpg"], ["https://cdn5.telesco.pe/d.jpg"]]),
        },
        "KR",
    )
    describe = _recording_describe()
    describe_sector_images(view, describe)
    assert len(describe.calls) == 3


def test_describe_sector_images_empty_view_returns_empty_dict():
    assert describe_sector_images([], _recording_describe()) == {}
