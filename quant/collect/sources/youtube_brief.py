"""장전 시황·종목 추천 유튜버 브리핑 — 리포트 하단 고정 채널 칸(2026-08-16, 사용자 추가 요구).

`feeds.py`의 유튜브 RSS 방식(API 키 불필요)을 그대로 재사용한다 — 여기서는
`BRIEF_CHANNELS`(리포트 하단 브리핑 전용)와 `feeds.YOUTUBE_CHANNELS`(뉴스 추출
파이프라인의 소스 레지스트리, `quant/collect/sources/__init__.py`)가 서로
다른 용도라 분리했다. 채널을 늘리거나 바꾸고 싶으면 **여기만 바꾸면 된다.**

channel_id 얻는 법: 채널 페이지(예 `https://www.youtube.com/@핸들`) 소스 보기에서
`"externalId":"UC..."` 를 찾거나 `<link rel="canonical" href=".../channel/UC...">`
를 본다. 등록 전 반드시 `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`
가 실제 RSS(내부 `<title>`이 채널명과 일치)를 주는지 확인한다 — 추측 금지.
아래 4개는 2026-08-16 위 방법으로 실측 확인했다(채널명·핸들도 같이 남긴다):
  삼프로TV(@3protv), 한국경제TV(@hkwowtv), 매경 월가월부(@MK_Invest),
  한경 글로벌마켓(핸들에 한글 포함 — canonicalBaseUrl 로만 확인).
소라게아빠는 2026-08-17 같은 방법으로 실측 확인했다(채널명 "소라게아빠의
차트 맛집", RSS `<title>` 일치 확인) — 용도: 미·한 시장 신호등 — 잠깐 멈출지
차트 분석, 2026-08-17 사용자 지정.
"""
from __future__ import annotations

from quant.adapters.http import client
from quant.collect.sources.feeds import parse_feed

# 여기만 바꾸면 된다 — 채널명 -> channel_id.
BRIEF_CHANNELS: dict[str, str] = {
    "삼프로TV": "UChlv4GSd7OQl3js-jkLOnFA",
    "한국경제TV": "UCF8AeLlUbEpKju6v1H6p8Eg",
    "매경 월가월부": "UCIipmgxpUxDmPP-ma3Ahvbw",
    "한경 글로벌마켓": "UCWskYkV4c4S9D__rsfOl2JA",
    # 용도: 미·한 시장 신호등 — 잠깐 멈출지 차트 분석, 2026-08-17 사용자 지정.
    "소라게아빠": "UCND_HhRw8lbvJSJ4oFvbAAw",
}

# 채널명 -> market(KR|US|BOTH) — 텔레그램 섹션 KR/US 고정 탭(2026-08-18)과
# 같은 방식으로 유튜브 브리핑도 탭을 나눈다. `telegram_channels.CHANNELS`의
# market 필드와 같은 계약: `channels_for`가 이 매핑으로 필터한다.
#
# 실측(2026-08-18, `curl .../feeds/videos.xml?channel_id=...` 최근 영상
# 제목)으로 분류했다 — 추측 금지 원칙(위 채널 등록 절차와 동일):
#   삼프로TV: "한국·미국 증시 진단"(켄 피셔 인터뷰 등) 두 시장을 두루 다룬다 → BOTH
#   한국경제TV: 코스피·삼성전자·SK하이닉스 등 국내 종목/지수 중심 → KR
#   매경 월가월부: "뉴욕브리핑"·"美경제일정" 등 월가(미국 시장) 전용 → US
#   한경 글로벌마켓: "월가백브리핑" 등 미국 시장 중심 해설 → US
#   소라게아빠: 실측 영상에 AMZN/AAPL 등 미국 티커 + 모듈 상단 docstring이
#     명시한 용도("미·한 시장 신호등") → BOTH
BRIEF_CHANNEL_MARKET: dict[str, str] = {
    "삼프로TV": "BOTH",
    "한국경제TV": "KR",
    "매경 월가월부": "US",
    "한경 글로벌마켓": "US",
    "소라게아빠": "BOTH",
}


def channels_for(market: str) -> list[str]:
    """`BRIEF_CHANNELS` 중 `market`(KR/US)에 해당하는 채널명만.
    `market: "BOTH"`로 분류된 채널은 두 시장 모두에 포함된다
    (`telegram_channels.channels_for`와 같은 계약)."""
    return [name for name in BRIEF_CHANNELS if BRIEF_CHANNEL_MARKET.get(name) in (market, "BOTH")]

_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# channel_id -> 채널명 역참조. BRIEF_CHANNELS 가 유일한 진실이므로 여기서 다시
# 나열하지 않고 파생시킨다.
_CHANNEL_NAMES: dict[str, str] = {cid: name for name, cid in BRIEF_CHANNELS.items()}


def _http_get(url: str) -> str:
    # 채널 4개를 순차로 돈다 — 기본 20s 타임아웃이면 최악 80s(전 채널 응답 없음)까지
    # 리포트 빌드를 붙잡는다. 5s 로 좁혀 최악을 20s 로 낮춘다(2026-08-16).
    with client(timeout=5.0) as c:
        resp = c.get(url)
        resp.raise_for_status()
    return resp.text


def fetch_channel_videos(channel_id: str, getter=None, limit: int = 3) -> list[dict]:
    """채널 하나의 최신 영상. `[{"title","link","published","channel"}]`.

    네트워크·파싱 어느 쪽이 실패해도 예외를 올리지 않고 빈 리스트를 돌려준다 —
    `fetch_briefs`가 채널 단위로 실패를 격리할 수 있는 건 이 계약 덕분이다.
    """
    get = getter or _http_get
    try:
        xml = get(_FEED_URL.format(channel_id=channel_id))
    except Exception:  # noqa: BLE001 — 채널 하나의 네트워크 실패가 전체를 막지 않는다
        return []
    channel_name = _CHANNEL_NAMES.get(channel_id, channel_id)
    return [
        {
            "title": item["title"],
            "link": item["link"],
            "published": item["published"],
            "channel": channel_name,
        }
        for item in parse_feed(xml, limit)
    ]


def fetch_briefs(getter=None, limit: int = 3) -> dict[str, list[dict]]:
    """`BRIEF_CHANNELS` 전체를 돈다. 실패한 채널은 그 채널만 결과에서 빠진다
    (전 채널 실패 시 빈 dict — 호출부가 이걸 보고 섹션 자체를 생략한다)."""
    out: dict[str, list[dict]] = {}
    for name, channel_id in BRIEF_CHANNELS.items():
        videos = fetch_channel_videos(channel_id, getter=getter, limit=limit)
        if videos:
            out[name] = videos
    return out
