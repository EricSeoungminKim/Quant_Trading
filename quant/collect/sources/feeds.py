"""뉴스 RSS + 유튜브 RSS 수집. 둘 다 API 키가 필요 없다.

여기서 뽑은 뉴스 제목은 뒤에서 `quant/analyze/mentions.py`가 종목명을 추출하는
입력이 된다 — **제목 텍스트가 정확해야** 한다. HTML 엔티티(`&amp;` 등)를
풀지 않으면 종목 추출이 실패하므로 `parse_feed`에서 반드시 unescape한다.

구글 뉴스 RSS는 제목 끝에 항상 " - {언론사}"를 붙이는데, 언론사 상당수가
상장사라 `quant/analyze/entities.py`의 종목 추출이 출처 표기를 종목 언급으로
오탐한다(예: "...아시아경제" → 아시아경제(127710) 오탐). 그래서 구글 뉴스
피드만 파싱 시점에 접미사를 잘라내고 `outlet` 필드로 보존한다 — 다른 피드는
이 형식이 아니고 정상 제목에 " - "가 들어갈 수 있어 무조건 자르면 안 된다.

피드 하나가 죽어도(404, 파싱 실패 등) 섹션 전체를 죽이지 않는다 — `quant/collect/snapshot.py`의
소스 레벨 실패 격리와는 다른 층으로, 여기서는 피드 단위로 격리한다.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from quant.adapters.http import client

ATOM_NS = "{http://www.w3.org/2005/Atom}"

# 피드당 상한. **표본을 정하는 값이 아니라 폭주 방지용 안전선이다** — 실제 표본은
# `fetch_news(since=...)`의 시간 창이 정한다.
#
# 2026-08-13 실측: 상한이 10이던 시절 US는 피드가 주는 529건 중 186건(35%)만,
# KR은 1,183건 중 190건(16%)만 받고 나머지를 버렸다. 연합뉴스는 120건을 주는데
# 10건만 읽었다. 그래서 팔란티어·테슬라처럼 그날 실제로 뜨거웠던 종목이 "뉴스 노출
# 상위"에 안 잡혔다 — 소스가 부족해서가 아니라 **우리가 버려서**였다.
FEED_LIMIT = 300

# 피드 병렬 수집. 19개를 순차로 받으면 그것만으로 수십 초가 든다.
FEED_WORKERS = 8

# 구글 뉴스 제목 끝의 " - {언론사}" 한 조각만 잘라낸다. <source> 엘리먼트가
# 없을 때의 폴백이다.
_OUTLET_SUFFIX_RE = re.compile(r"\s[-–]\s([^-–]{1,30})$")

NEWS_FEEDS: dict[str, dict[str, str]] = {
    # 2026-08-13 실측 확대: 경제지·종합지·통신사·IT/증권 전문지를 섞어 한 매체가
    # 표본을 독점하지 않게 했다. 후보 전부를 curl로 200 + 실제 item 파싱 확인 후
    # 등록했다 — 죽은 피드(HTML 리다이렉트, 404, 파싱 실패)는 아래 "검증 후 제외"
    # 목록에 사유와 함께 남긴다.
    #
    # 검증 후 제외(2026-08-13):
    #   서울경제 — RSS/S01.xml, RSS/S03.xml, rss/GD.xml 모두 404
    #   아시아경제 — DNS 연결 실패(rss.asiae.co.kr), www/cm 서브도메인도 404
    #   YTN — _ln/0102_rss.xml, rss/economic.xml, rss/hotline.xml 모두 404
    #   비즈니스포스트 — 쿼리·경로 후보 모두 HTML 페이지 반환(RSS 서비스 종료 추정)
    #   뉴스핌 — rss/economy, rss/all.xml 모두 404
    #   이투데이 — rss/economic.xml 404, rss/ 는 HTML(파싱 불가)
    #   헤럴드경제 — 시도한 4개 경로 전부 HTML 페이지 반환
    #   중앙일보 — "서비스 종료 안내" HTML만 반환 (RSS 서비스 자체 폐지)
    #   조선비즈 자체 도메인(biz.chosun.com/rss/) — 404, 대신 조선일보 arc 피드의
    #   economy 카테고리(www.chosun.com/.../category/economy)로 대체 등록
    "KR": {
        "연합뉴스_경제": "https://www.yna.co.kr/rss/economy.xml",
        "한국경제_경제": "https://www.hankyung.com/feed/economy",
        "한국경제_증권": "https://www.hankyung.com/feed/finance",
        "매일경제_경제": "https://www.mk.co.kr/rss/30100041/",
        "매일경제_증권": "https://www.mk.co.kr/rss/50200011/",
        "이데일리_경제": "http://rss.edaily.co.kr/edaily_news.xml",
        "머니투데이_경제": "https://rss.mt.co.kr/mt_news.xml",
        "조선비즈_경제": "https://www.chosun.com/arc/outboundfeeds/rss/category/economy/?outputType=xml",
        "전자신문": "https://rss.etnews.com/Section901.xml",
        "ZDNet코리아": "https://feeds.feedburner.com/zdkorea",
        "인포스탁데일리": "https://www.infostockdaily.co.kr/rss/allArticle.xml",
        "경향신문_경제": "https://www.khan.co.kr/rss/rssdata/economy_news.xml",
        "동아일보_경제": "https://rss.donga.com/economy.xml",
        # 원문 매체 RSS가 없는 주제는 구글 뉴스 검색 피드로 보강한다. 기사마다
        # 실제 언론사가 달라 outlet 필드(강제 strip_outlet=True)로 다양성이
        # 오히려 늘어난다.
        "구글_코스피": "https://news.google.com/rss/search?q=코스피+when:1d&hl=ko&gl=KR&ceid=KR:ko",
        "구글_증시": "https://news.google.com/rss/search?q=증시+종목+when:1d&hl=ko&gl=KR&ceid=KR:ko",
        "구글_실적": "https://news.google.com/rss/search?q=실적+어닝+when:1d&hl=ko&gl=KR&ceid=KR:ko",
        # 뉴스 흐름 품질 스펙(L-2, 2026-08-17): Bloomberg/Yahoo 등 해외 매체가
        # 한국 증시를 다룬 기사를 KR 리포트에 편입하고 싶었으나, KR 리포트는
        # KR 피드만 읽고 US 스냅샷을 보지 않는 계약이라 그 경로로 편입하려면
        # report_cli 가 시장 경계를 넘어야 한다(복잡). 대신 이 저장소의 기존
        # "원문 매체 RSS 없는 주제는 구글 뉴스 검색 피드로 보강" 패턴을 그대로
        # 재사용해 영문 쿼리 피드 하나를 KR 피드 목록에 추가하는 쪽이 계약상
        # 단순하다 — 결과적으로 해외 매체의 한국 증시 보도가 이 피드를 통해
        # 자연스럽게 KR news_flow 에 들어온다(Bloomberg/Yahoo Finance 도 이
        # 쿼리에 걸리면 outlet 필드로 나온다). hl/gl/ceid 를 US 로 둬야 영문
        # 결과 위주로 나온다 — ko/KR 로 두면 한국어 매체만 나와 목적(해외
        # 매체 시각 편입)을 못 이룬다.
        "구글_해외시각_한국증시": "https://news.google.com/rss/search?q=Korea+stock+market+when:1d&hl=en-US&gl=US&ceid=US:en",
    },
    # 품질 감사 후 제외(2026-08-13, 전체 피드 신선도+시그널 실측):
    #   뉴시스_경제 — 200 + 신선하지만(newest 0.1h) 100건 중 종목 언급 1건(1%),
    #   고유 기여 0건. "경제" 카테고리가 실제로는 정책·복지·부동산 세금 위주라
    #   종목 뉴스로서 신호가 거의 없다.
    #   구글_반도체 — 검색어 자체가 "반도체+삼성전자+SK하이닉스"로 좁아 매번
    #   그 두 종목만 반복 노출(distinct=5). 100건을 읽어도 다른 19개 KR
    #   피드가 이미 다 다루는 이름이라 고유 기여 0건 — 구조적으로 항상 그럴
    #   수밖에 없는 질의라 드롭.
    #   한겨레_경제 — RSS 아이템에 pubDate/dc:date 등 발행시각 필드가 아예
    #   없다(직접 XML 확인, 파서 버그 아님 — 피드 자체가 날짜를 안 준다).
    #   `filter_since`는 날짜를 못 읽은 기사를 버리지 않고 항상 통과시키므로
    #   이 피드는 매 리포트 주기마다 전량이 시간창을 무조건 통과한다 — 오래된
    #   기사가 매번 재노출될 수 있는 구조적 블라인드스팟. 종목 신호도 30건 중
    #   6.7%(고유 기여 1건)로 약해 제거.
    #
    # 남겨둔 것(참고): 경향신문_경제도 100% undated였지만 원인이 다르다 —
    # 아이템에 <dc:date>는 있는데 `_published()`가 RSS 분기에서 pubDate만
    # 찾고 dc:date를 안 본다(파서 버그, 피드 결함 아님). 고유 기여는 이번
    # 표본에서 0건이었지만 파서를 고치면 바뀔 수 있어 드롭하지 않고 남겨둔다.
    # 매일경제_경제/증권, 인포스탁데일리는 다른 파서 버그(콜론 오프셋
    # "+09:00"·타임존 없는 로컬시각이 UTC로 오라벨링돼 신선도가 9시간
    # 틀리게 계산됨)가 있지만 콘텐츠 자체는 정상이라 유지한다 — 두 버그 모두
    # `parse_published()`/`_published()` 수정이 필요해 이 감사(NEWS_FEEDS
    # 편집 권한)의 범위 밖이다.
    # 2026-08-12 실측: Reuters(feeds.reuters.com)는 DNS 자체가 죽어 있어(RSS 서비스
    # 종료) 후보에서 뺐다. AP(feeds.apnews.com)도 연결 자체가 안 됐다. 2026-08-13에
    # 후보를 넓혀 실제 200+item 확인된 매체만 추가했다 — 검증 후 제외 사유는 아래.
    #
    # 검증 후 제외(2026-08-13):
    #   Barron's — 403 (봇 차단)
    #   Forbes(money/feed) — 404
    #   Investopedia — 403
    #   AP Business — 연결 실패(feeds.apnews.com)
    #   Reuters(reutersagency.com 피드) — 404
    #   Axios — 403
    #   Zacks / Barchart / MarketBeat / ETF.com — 404 또는 403
    #   Business Insider Markets(markets.businessinsider.com/rss/news) — 200이지만
    #   PR와이어성 스폰서 콘텐츠가 절반 이상 섞여 있어 품질 기준 미달로 제외
    #
    # 검증 후 제외(2026-08-13, investing.com/CNN/Bloomberg/SeekingAlpha 조사):
    #   CNN(rss.cnn.com money/business/edition_business) — 전부 200이지만 최신
    #   글이 2017~2023년에 멈춰 있는 죽은 아카이브(RSS 서비스 방치 추정). CNN
    #   Business에 살아있는 자체 RSS가 없다 — cnn.com/business는 RSS 링크 자체가
    #   없고 rss.app 같은 제3자 스크레이핑 서비스만 대안으로 나온다.
    #   investing.com/rss/ (인덱스 페이지) — 403. 개별 카테고리 .rss 파일은
    #   robots.txt에 안 걸리고 정상 작동해 위 stock_Stocks.rss만 채택.
    #   investing.com IPO 전용 피드 — /news/ipo-news, /news/ipo 모두 404, 웹검색
    #   에서도 전용 RSS 확인 못함(카테고리 자체가 없는 것으로 보임).
    #   investing.com news_25(주식시장)/news_356(기업)/news_1061(애널리스트
    #   의견)/news_1062(실적)/news_95(경제지표) — 전부 200 + 10건 파싱되지만
    #   Palantir/SpaceX/Tesla/Nvidia/CoreWeave 언급이 stock_Stocks.rss보다 약해
    #   중복 추가 보류(회당 10건 상한이라 피드 5개를 다 넣으면 API 콜만 늘고
    #   실제 커버리지 증가는 적었다).
    #   Bloomberg_Politics/Industries — 200 + fresh하지만 위 키워드 히트가
    #   Technology보다 약해 이번엔 보류. Bloomberg_Wealth/Green — Wealth는 200
    #   이지만 현재 아이템 0건(비어있는 채널), Green은 404.
    #   SeekingAlpha feed.xml(30건) — market_currents.xml(7건)보다 양은 많지만
    #   대부분 중소형주 실적 콜 트랜스크립트라 "핫뉴스" 신호가 아니라 노이즈에
    #   가까움 — 보류.
    #   Benzinga — 이전에 실운영에서 0건이 관측됐다는 보고가 있었으나 이번 조사
    #   에서 순차 6회 + fetch_news() 동시성 3회 전부 10건으로 정상 — 유지.
    #
    # 품질 감사 후 제외(2026-08-13, 전체 피드 신선도+시그널 실측):
    #   WSJ_Markets(feeds.a.dj.com/rss/RSSMarketsMain.xml) — 200이지만 최신
    #   글이 2025-01-27(딥시크 급락 당일)에 멈춘 죽은 아카이브. CNN과 같은
    #   실패 유형 — RSS 서비스가 방치된 채 200을 계속 반환한다.
    #   MarketWatch_RealTime(mw_realtimeheadlines) — 200이지만 최신 글이
    #   2025-06-11로, 1년 넘게 갱신이 없다. MarketWatch_Top(mw_topstories)은
    #   정상(newest 0.1h)이라 그건 유지 — RealTime 피드만 죽었다.
    #   Benzinga — 위에서 "유지" 판정했던 건 200/10건 응답만 확인한 것이었다.
    #   이번에 제목을 직접 보니 10건 전부 "Best Tech/Energy/Biotech/Gold
    #   Stocks Right Now" 류의 상시 게시 리스티클로, 발행시각도 전부
    #   동일(같은 배치 재생성) — 실제 뉴스가 아니라 SEO 상시 콘텐츠라 제외.
    #   Fortune — 신선하지만(newest 4.6h) 10건 중 종목 언급 0건, 임원 인터뷰·
    #   라이프스타일·자선 기부 위주라 종목 뉴스 신호가 없다.
    "US": {
        "BBC_Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "CNBC_Top": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "CNBC_Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "Guardian_Business": "https://www.theguardian.com/uk/business/rss",
        "MarketWatch_Top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
        "Yahoo_Finance": "https://finance.yahoo.com/news/rssindex",
        "Investing_Com": "https://www.investing.com/rss/news.rss",
        "SeekingAlpha": "https://seekingalpha.com/market_currents.xml",
        "NYT_Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "Bloomberg_Markets": "https://feeds.bloomberg.com/markets/news.rss",
        "ZeroHedge": "https://feeds.feedburner.com/zerohedge/feed",
        "FT_Home": "https://www.ft.com/rss/home",
        "Motley_Fool": "https://www.fool.com/feeds/index.aspx",
        "TheStreet": "https://www.thestreet.com/.rss/full/",
        "Kiplinger": "https://www.kiplinger.com/feed/all",
        # 2026-08-13 추가: investing.com/rss/ 는 인덱스 페이지가 403이라 카테고리
        # 페이지(예 /news/analyst-ratings)의 <link rel=alternate rss> 태그를 하나씩
        # 확인해 실제 경로를 찾았다. stock_Stocks.rss 실측에서 Palantir 언급이
        # 잡혀 기존 19개 피드(Investing_Com 포함)에 없던 신호를 보탰다.
        "Investing_StockNews": "https://www.investing.com/rss/stock_Stocks.rss",
        # Bloomberg는 Markets 외에 섹션별 피드가 살아 있다. Technology는 실측에서
        # Nvidia/CoreWeave 등 AI 하드웨어 종목 언급이 Markets보다 진하게 잡혔다.
        "Bloomberg_Technology": "https://feeds.bloomberg.com/technology/news.rss",
    },
}

# 피드 이름 -> 실제 언론사 표시명. outlet 다양성 집계(`outlet_diversity`)의
# 기준이다. 구글 뉴스 피드는 기사마다 언론사가 달라 여기 값은 <source>도 없고
# 정규식도 실패했을 때만 쓰는 폴백이다(정상 경로는 item["outlet"]을 그대로 쓴다).
FEED_OUTLET: dict[str, str] = {
    "연합뉴스_경제": "연합뉴스",
    "한국경제_경제": "한국경제",
    "한국경제_증권": "한국경제",
    "매일경제_경제": "매일경제",
    "매일경제_증권": "매일경제",
    "이데일리_경제": "이데일리",
    "머니투데이_경제": "머니투데이",
    "조선비즈_경제": "조선비즈",
    "전자신문": "전자신문",
    "ZDNet코리아": "ZDNet코리아",
    "인포스탁데일리": "인포스탁데일리",
    "경향신문_경제": "경향신문",
    "동아일보_경제": "동아일보",
    "구글_코스피": "구글뉴스",
    "구글_증시": "구글뉴스",
    "구글_실적": "구글뉴스",
    "구글_해외시각_한국증시": "구글뉴스",
    "BBC_Business": "BBC",
    "CNBC_Top": "CNBC",
    "CNBC_Economy": "CNBC",
    "Guardian_Business": "The Guardian",
    "MarketWatch_Top": "MarketWatch",
    "Yahoo_Finance": "Yahoo Finance",
    "Investing_Com": "Investing.com",
    "SeekingAlpha": "Seeking Alpha",
    "NYT_Business": "New York Times",
    "Bloomberg_Markets": "Bloomberg",
    "ZeroHedge": "ZeroHedge",
    "FT_Home": "Financial Times",
    "Motley_Fool": "Motley Fool",
    "TheStreet": "TheStreet",
    "Kiplinger": "Kiplinger",
    "Investing_StockNews": "Investing.com",
    "Bloomberg_Technology": "Bloomberg",
}

# 채널 ID는 유튜브 채널 페이지의 canonical link(/channel/<id>)로 확인한 값이다.
YOUTUBE_CHANNELS: dict[str, dict[str, str]] = {
    "KR": {},
    "US": {
        "Aswath Damodaran": "UCLvnJL8htRR1T9cbSccaoVw",
        "Bloomberg Television": "UCIALMKvObZNtJ6AmdCLP7Lg",
    },
}

_YOUTUBE_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


def _text(el: ET.Element | None) -> str | None:
    return el.text.strip() if el is not None and el.text else None


def _link(item: ET.Element, ns: str) -> str | None:
    """RSS는 <link>텍스트</link>, Atom은 <link href="..."/> — 둘 다 처리한다."""
    if ns:
        el = item.find(f"{ns}link")
        return el.attrib.get("href") if el is not None else None
    el = item.find("link")
    return _text(el)


_DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _published(item: ET.Element, ns: str) -> str | None:
    """발행시각 문자열. RSS 는 pubDate, Atom 은 published/updated, 그리고 **dc:date**.

    dc:date 를 안 읽어서 경향신문 기사 50건이 전부 '날짜 미상'으로 시간 창을
    무조건 통과하고 있었다(2026-08-13 실측: pubDate 0건 / dc:date 50건).
    """
    if ns:
        return (_text(item.find(f"{ns}published"))
                or _text(item.find(f"{ns}updated"))
                or _text(item.find(f"{_DC_NS}date")))
    return _text(item.find("pubDate")) or _text(item.find(f"{_DC_NS}date"))


def _strip_outlet(title: str, entry: ET.Element, ns: str) -> tuple[str, str]:
    """구글 뉴스 제목 끝의 " - {언론사}"를 잘라내고 (제목, 언론사)를 반환한다.

    <source> 엘리먼트가 있으면 그 값을 우선한다 — 제목이 정확히 그 값으로
    끝날 때만 잘라내므로 정규식보다 정확하다. 없을 때만 정규식으로 폴백한다.
    """
    source_el = entry.find(f"{ns}source") if ns else entry.find("source")
    source_text = _text(source_el)
    if source_text:
        outlet = html.unescape(source_text)
        suffix = f" - {outlet}"
        if title.endswith(suffix):
            return title[: -len(suffix)], outlet
        return title, ""

    m = _OUTLET_SUFFIX_RE.search(title)
    if m:
        return title[: m.start()], html.unescape(m.group(1))
    return title, ""


def parse_feed(xml: str, limit: int, strip_outlet: bool = False) -> list[dict]:
    """RSS 2.0과 Atom을 모두 받는다. 깨진 XML은 예외 대신 빈 리스트를 반환한다.

    `strip_outlet=True`면 구글 뉴스 특유의 " - {언론사}" 제목 접미사를 잘라
    `outlet` 필드로 보존한다. 다른 피드는 이 형식이 아니므로 항상 False로 둔다.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    if root.tag == f"{ATOM_NS}feed":
        ns, entries = ATOM_NS, root.findall(f".//{ATOM_NS}entry")
    else:
        ns, entries = "", root.findall(".//item")

    items: list[dict] = []
    for entry in entries:
        title_el = entry.find(f"{ns}title") if ns else entry.find("title")
        title = _text(title_el)
        link = _link(entry, ns)
        if not title or not link:
            continue
        title = html.unescape(title)
        outlet = ""
        if strip_outlet:
            title, outlet = _strip_outlet(title, entry, ns)
        items.append({
            "title": title,
            "link": html.unescape(link),
            "published": _published(entry, ns),
            "outlet": outlet,
        })
        if len(items) >= limit:
            break
    return items


# RFC822 는 오프셋을 "+0900" 으로 쓰지만 콜론을 넣는("+09:00") 피드가 흔하다
# (2026-08-13 실측: 매일경제 경제/증권, 인포스탁데일리 — 전 항목이 그렇다).
# `parsedate_to_datetime` 은 이걸 **예외 없이 naive 로** 돌려주고, 그걸 UTC 로
# 취급하면 KST 벽시계가 9시간 미래로 찍힌다 → 낡은 기사가 "방금 나온 것"으로
# 시간 창을 통과한다. 매 리포트마다 조용히 틀리던 버그다.
_COLON_OFFSET_RE = re.compile(r"([+-]\d{2}):(\d{2})\s*$")


def parse_published(raw: str | None) -> datetime | None:
    """RSS(RFC822)·Atom(ISO8601) 발행시각 → UTC aware datetime.

    **타임존을 모르면 None 을 돌려준다.** 예전에는 naive 를 UTC 로 간주했는데,
    그건 "모른다"를 "UTC 다"라고 우기는 것이라 한국 피드에서 9시간 오차를 만들었다.
    None 은 호출부(`filter_since`)에서 '날짜 미상'으로 세어져 기사는 남고 집계에
    드러난다 — 조용히 틀린 시각보다 드러난 결측이 낫다.
    """
    if not raw:
        return None
    raw = raw.strip()

    # 콜론 오프셋을 RFC822 형식으로 정규화한 뒤 파싱한다.
    for candidate in (_COLON_OFFSET_RE.sub(r"\1\2", raw), raw):
        try:
            dt = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            continue
        if dt is not None and dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)

    try:  # Atom: "2026-08-12T08:25:27Z" / "2026-08-12T08:25:27+09:00"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else None


def filter_since(items: list[dict], since: datetime | None) -> tuple[list[dict], int]:
    """`since` 이후 기사만. 반환 (남은 기사, 날짜를 못 읽어 그냥 남긴 건수).

    **표본을 개수가 아니라 시간으로 정한다.** 피드마다 주는 양이 10건에서 120건까지
    제각각이라, 상위 N건으로 자르면 활발한 매체일수록 더 짧은 시간만 보게 된다 —
    표본이 매체별로 다른 시간대를 덮는 셈이라 종목 언급 집계가 왜곡된다.
    """
    if since is None:
        return items, 0
    kept: list[dict] = []
    undated = 0
    for it in items:
        dt = parse_published(it.get("published"))
        if dt is None:
            undated += 1
            kept.append(it)  # 모르는 것을 버리지 않는다
        elif dt >= since:
            kept.append(it)
    return kept, undated


def _fetch_one(url: str) -> list[dict]:
    """피드 하나를 가져와 파싱한다. 실패하면 빈 리스트 — 호출부에서 섹션을 죽이지 않는다."""
    try:
        with client() as c:
            resp = c.get(url)
            resp.raise_for_status()
        return parse_feed(resp.text, FEED_LIMIT, strip_outlet="news.google.com" in url)
    except Exception:
        return []


def fetch_conditional(url: str, cache: dict[str, dict]) -> tuple[list[dict], dict, str]:
    """`_fetch_one`의 조건부 GET 버전 — `data/cache/feed_headers.json`을 30분마다
    갈아엎던 33피드 재전송 비용을 줄이려는 것이다.

    `cache`는 url -> {"etag": …, "last_modified": …} 맵. 저장된 값이 있으면
    `If-None-Match`/`If-Modified-Since`를 실어 보낸다. 304(Not Modified)면 바뀐
    게 없다는 뜻이므로 빈 목록과 **기존 캐시 항목 그대로**를 돌려준다 — collector의
    중복 판정(저장소가 append-only)이 이미 안전망이라 304를 "신규 없음"으로
    취급해도 기사가 새지 않는다. 200이면 새 ETag/Last-Modified로 캐시 항목을
    새로 만들고, 둘 다 없는 서버는 빈 dict를 돌려준다 — 그 url은 캐시에 남기지
    않고 다음에도 무조건 전체 재요청한다.

    네트워크 예외·4xx/5xx는 `_fetch_one`과 마찬가지로 삼키되, 캐시 항목은 그대로
    돌려준다 — 한 피드의 실패가 그 피드의 조건부 GET 자격을 지우지 않는다.

    세 번째 반환값 `status`("ok"/"not_modified"/"error")는 호출부(collector)가
    건강도를 판정하기 위한 것이다. **304 는 성공이다** — 빈 목록만 보고 "죽었다"고
    판정하면 안 된다(그래서 별도 status 가 필요하다: items 개수만으로는 "변경
    없음"과 "진짜 빈 응답"을 구분할 수 없다).
    """
    entry = cache.get(url) or {}
    headers = {}
    if entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    if entry.get("last_modified"):
        headers["If-Modified-Since"] = entry["last_modified"]
    try:
        with client() as c:
            resp = c.get(url, headers=headers)
            if resp.status_code == 304:
                return [], entry, "not_modified"
            resp.raise_for_status()
        items = parse_feed(resp.text, FEED_LIMIT, strip_outlet="news.google.com" in url)
        new_entry: dict[str, str] = {}
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        if etag:
            new_entry["etag"] = etag
        if last_modified:
            new_entry["last_modified"] = last_modified
        return items, new_entry, "ok"
    except Exception:
        return [], entry, "error"


def fetch_news(market: str, since: datetime | None = None) -> dict:
    """`since` 이후 발행분만 모은다(None 이면 피드가 주는 전부, FEED_LIMIT 상한까지).

    `since` 는 직전 리포트의 생성시각(`clock.session_window`)이다 — 주말·공휴일·
    장애로 하루를 걸러도 그 구간이 자동으로 메워진다.
    """
    names = list(NEWS_FEEDS.get(market, {}).items())
    with ThreadPoolExecutor(max_workers=FEED_WORKERS) as ex:
        raw = dict(zip((n for n, _ in names), ex.map(lambda kv: _fetch_one(kv[1]), names)))

    feeds: dict[str, list[dict]] = {}
    stats = {"fetched": 0, "kept": 0, "undated": 0, "empty_feeds": []}
    for name, items in raw.items():
        kept, undated = filter_since(items, since)
        feeds[name] = kept
        stats["fetched"] += len(items)
        stats["kept"] += len(kept)
        stats["undated"] += undated
        if not items:
            # 죽은 피드를 조용히 0건으로 넘기지 않는다 — 없는 것과 못 가져온 것은 다르다.
            stats["empty_feeds"].append(name)
    return {"feeds": feeds, "window_start": since.isoformat() if since else None, **stats}


def resolve_outlet(feed_name: str, item: dict) -> str:
    """기사 한 건의 실제 언론사명.

    구글 뉴스 피드는 검색어 하나에 여러 언론사가 섞여 나오므로 기사 자체가
    들고 있는 `item["outlet"]`(parse_feed가 strip_outlet=True로 뽑은 값)을
    우선한다. 원문 매체 피드는 피드 자체가 그 언론사이므로 `FEED_OUTLET`
    매핑을 쓴다 — 매핑에 없는 피드명은 그대로 표시명으로 쓴다(새 피드를
    추가하고 FEED_OUTLET 등록을 깜빡해도 다양성 집계가 조용히 죽지 않는다).
    """
    return item.get("outlet") or FEED_OUTLET.get(feed_name, feed_name)


def outlet_diversity(news_data: dict) -> dict:
    """언론사별 기사 수 + 총 기사 수 + 고유 언론사 수.

    피드 개수를 아무리 늘려도 실제로 몇 개의 서로 다른 언론사에서 왔는지는
    이 카운트로만 보인다 — 한 매체가 표본을 독점하면 `by_outlet` 상위에서
    바로 드러난다.
    """
    counts: dict[str, int] = {}
    for feed_name, items in news_data.get("feeds", {}).items():
        for item in items:
            outlet = resolve_outlet(feed_name, item)
            counts[outlet] = counts.get(outlet, 0) + 1
    return {
        "total_articles": sum(counts.values()),
        "distinct_outlets": len(counts),
        "by_outlet": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def fetch_youtube(market: str) -> dict:
    channels = {
        name: _fetch_one(_YOUTUBE_FEED_URL.format(channel_id=cid))
        for name, cid in YOUTUBE_CHANNELS.get(market, {}).items()
    }
    return {"channels": channels}
