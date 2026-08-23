"""텔레그램 공개 채널 12방 — 웹 프리뷰 기반 브리핑 (2026-08-17, 사용자 지정, 서브프로젝트 S).

`blog_brief.py`/`youtube_brief.py`와 같은 자리, 같은 실패 격리 계약이다. 텔레그램
공개 채널은 `https://t.me/s/{handle}`에서 로그인 없이 웹 프리뷰(최근 메시지
스냅샷)를 HTML로 내려준다 — 공식 API 가 아니라 웹 페이지 스크레이핑이므로
`quant/collect/`(스크래핑 허용 평면)에 둔다. 채널을 늘리거나 바꾸고 싶으면
**`CHANNELS`만 바꾸면 된다.**

## 실측 확인(2026-08-17, `curl -o /dev/null -w '%{http_code}' https://t.me/s/{handle}`
+ `tgme_widget_message ` 블록 카운트, 채널당 0.5s 간격):

    insidertracking          200, 20건
    pikachu_aje               200, 20건
    pharmbiohana               200, 20건
    aetherjapanresearch       200, 17건
    yieldnspread               200, 20건
    report_figure_by_offset  302 → https://t.me/report_figure_by_offset (앱 오픈
                              페이지, `tgme_channel_history` 섹션 자체가 없다 —
                              프리뷰가 꺼진 채널로 판단, 추측 아님)
    gaoshoukorea                200, 19건
    tazastock                  200, 20건
    mootda                      200, 19건
    harveyspecterMike        200, 18건
    Samsung_Global_AI_SW     200, 19건
    rafikiresearch              200, 20건

12개 중 11개가 실측으로 프리뷰를 서빙한다. `report_figure_by_offset`만 예외이며
등록은 유지한다 — `fetch_channel`/`fetch_all`이 그 경우도 예외 없이 빈 리스트 +
사유 문자열로 정직하게 드러낸다(아래 `fetch_all` 참고).

파싱 픽스처(`tests/report/fixtures/telegram_tazastock.html`)는 위 실측 중
tazastock 응답에서 실제 메시지 5건(75633~75637, 링크 없는 것/있는 것 섞음)을
그대로 잘라 저장한 것이다 — 추측이 아니라 실제 HTML.
`telegram_preview_disabled.html`은 `report_figure_by_offset` 리다이렉트 목적지를
그대로 저장한 것(프리뷰 꺼짐 케이스 검증용).

## 시간당 US 뉴스 채널 실측(2026-08-17T13:30:24Z, 서브프로젝트 W part 1)

사용자가 "1시간마다 뉴스 올리는 텔레그램 채널(미국 중심)"을 요청 — 아래 후보를
`curl https://t.me/s/{handle}`로 실측(HTTP 200 + `tgme_widget_message_wrap` 개수
+ 최근 20건의 `datetime` 타임스탬프 분포)했다:

    walterbloomberg   200, 20건, 2026-08-17 12:05~13:25 (80분에 20건 — 4분당
                      1건 수준). 사용자가 예시로 든 "DeItaone(Walter
                      Bloomberg)"의 현재 핸들 — 메시지 서명이 `(@WalterBloomberg)`.
                      매크로·중앙은행·지수 헤드라인.
    financialjuice     200, 20건, 2026-08-17 12:30~13:25 (55분에 20건). 메시지
                      끝에 `|FJ` 서명. 매크로·지정학·중앙은행 헤드라인.
    zerohedgenews      200, 20건, 2026-08-17 00:07~13:20 (13시간에 20건 —
                      시간당 수준이지만 위 둘보다 낮은 빈도). 미채택(2개면
                      충분, 상위 2개만).
    DeItaone           302 → 앱 오픈 페이지(프리뷰 비활성, `tgme_channel_history`
                      섹션 없음) — 예전 핸들은 막혀 있다. `walterbloomberg`가
                      실제 대체 핸들.
    marketcurrents     200이지만 메시지 2건뿐, 최신이 2024-09-15 — 죽은 채널,
                      제외.
    WatcherGuru        200, 19건이지만 2026-08-12~08-17(5일)에 19건 — 시간당
                      아님(하루 4건 수준), 제외.
    breakingnewsalerts 200, 20건이지만 전부 2020-10 — 죽은 채널, 제외.
    CNBC/financialtimes/ReutersBiz/FastFT/benzinga/StockMKTNewz/stocktwits/
    YahooFinance/marketwatch/ForexLive 등          302(프리뷰 비활성) 또는
                      접근 불가 — 제외.

실측에서 2개 이상(walterbloomberg, financialjuice, zerohedgenews)이 시간당
수준을 충족해 상위 2개(`walterbloomberg`, `financialjuice`)만 `tier: "usnews"`,
`market: "US"`로 추가한다.

## 사진 메시지(2026-08-17, 서브프로젝트 S part 3)

`curl https://t.me/s/pikachu_aje` 재실측 — 20건 중 4건이 사진 메시지였다(캡션
없는 것/있는 것 섞임). 프리뷰 HTML은 사진을 `<img>`가 아니라
`<a class="tgme_widget_message_photo_wrap ..." style="...background-image:
url('https://cdn5.telesco.pe/file/...')">`로 얹는다 — `_BG_IMAGE_RE`가 그 URL을
뽑는다. 파싱 픽스처(`tests/report/fixtures/telegram_pikachu_photo.html`)는 이
실측에서 메시지 3건(8213 텍스트만/8215 사진만/8221 사진+캡션)을 그대로 잘라
저장한 것 — 추측이 아니라 실제 HTML.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import lxml.html as LH

from quant.adapters.http import client

# 여기만 바꾸면 된다 — handle, 분류, market(KR|US|BOTH), tier(sector|macro|news).
CHANNELS: list[dict] = [
    # tier "usdigest"(2026-08-21): 「미국 기업 섹터별 소식 정리」·「🌎 글로벌 뉴스
    # 브리핑」을 한국어로 하루 한 번(KST 05시 전후) 보내는 **일일 다이제스트**다
    # (원장 275건 실측). usnews tier(walterbloomberg/financialjuice)는 영문 시간당
    # 헤드라인이라 성격이 다르므로 같은 tier 로 묶지 않는다 — 서사에는 넣고
    # "실시간 헤드라인" 구획에는 넣지 않기 위한 구분이다.
    {"handle": "insidertracking", "분류": "미국 주식·해외 뉴스", "market": "US", "tier": "usdigest"},
    {"handle": "pikachu_aje", "분류": "전력 섹터", "market": "KR", "tier": "sector"},
    {"handle": "pharmbiohana", "분류": "바이오·미용 섹터", "market": "KR", "tier": "sector"},
    {"handle": "aetherjapanresearch", "분류": "해외 리포트·시황 인사이트", "market": "BOTH", "tier": "macro"},
    {"handle": "yieldnspread", "분류": "채권·경제", "market": "BOTH", "tier": "macro"},
    {"handle": "report_figure_by_offset", "분류": "증권사 리포트 요약", "market": "KR", "tier": "news"},
    {"handle": "gaoshoukorea", "분류": "국내외 기업 실적·뉴스", "market": "BOTH", "tier": "news"},
    {"handle": "tazastock", "분류": "장중 시황·국내 시황", "market": "KR", "tier": "news"},
    {"handle": "mootda", "분류": "국내 시황", "market": "KR", "tier": "news"},
    {"handle": "harveyspecterMike", "분류": "매크로 분석", "market": "BOTH", "tier": "macro"},
    {"handle": "Samsung_Global_AI_SW", "분류": "글로벌 AI/SW — 삼성 이영진", "market": "BOTH", "tier": "sector"},
    {"handle": "rafikiresearch", "분류": "매크로 종합", "market": "BOTH", "tier": "macro"},
    # 시간당 US 뉴스(서브프로젝트 W part 1, 실측 2026-08-17T13:30:24Z — 위 docstring
    # "시간당 US 뉴스 채널 실측" 절 참고). walterbloomberg = 사용자가 예시로 든
    # "DeItaone" 의 현재 핸들(구 핸들은 302로 막혀 있다), 80분에 20건.
    {"handle": "walterbloomberg", "분류": "미국 매크로·지수 헤드라인(구 DeItaone)", "market": "US", "tier": "usnews"},
    # financialjuice — 55분에 20건, 매크로·지정학 헤드라인(`|FJ` 서명).
    {"handle": "financialjuice", "분류": "미국 매크로·지정학 헤드라인", "market": "US", "tier": "usnews"},
]

_PREVIEW_URL = "https://t.me/s/{handle}"
LIMIT = 20

# 사진 메시지(서브프로젝트 S part 3) — 프리뷰 HTML은 `<img>`가 아니라
# `<a class="tgme_widget_message_photo_wrap ..." style="...background-image:
# url('https://cdn5.telesco.pe/file/...')">`로 이미지를 얹는다(실측,
# 2026-08-17 `curl https://t.me/s/pikachu_aje` — 픽스처 참고 아래).
_BG_IMAGE_RE = re.compile(r"background-image:url\('([^']+)'\)")


def _http_get(url: str) -> str:
    # blog_brief/youtube_brief와 같은 이유로 5s — 채널 하나의 응답 없음이 리포트
    # 빌드 전체를 오래 붙잡지 않게 한다.
    with client(timeout=5.0) as c:
        resp = c.get(url)
        resp.raise_for_status()
    return resp.text


def channels_for(market: str) -> list[dict]:
    """`CHANNELS` 중 `market`(KR/US)에 해당하는 것만. `market: "BOTH"`는 둘 다에 포함된다."""
    return [c for c in CHANNELS if c["market"] in (market, "BOTH")]


def _history_section(doc):
    sections = doc.xpath(
        ".//section[contains(concat(' ', normalize-space(@class), ' '), ' tgme_channel_history ')]"
    )
    return sections[0] if sections else None


def _parse_messages_with_reason(html_text: str, limit: int = LIMIT) -> tuple[list[dict], str | None]:
    """프리뷰 HTML → (메시지 리스트 최신순, 실패 사유|None).

    `report_figure_by_offset`처럼 프리뷰가 꺼진 채널은 `/s/{handle}` 요청이
    `follow_redirects=True`로 앱 오픈 페이지까지 자동으로 넘어가는데, 그 페이지엔
    `tgme_channel_history` 섹션 자체가 없다(실측 확인) — 이걸 "메시지 0건"과
    구분해 사유를 남긴다. 섹션은 있는데 메시지가 0건인 경우(채널이 그냥 조용한
    경우)는 에러가 아니다.
    """
    try:
        doc = LH.fromstring(html_text)
    except Exception as e:  # noqa: BLE001 — 깨진 HTML도 예외 대신 빈 리스트+사유
        return [], f"parse error: {type(e).__name__}"

    section = _history_section(doc)
    if section is None:
        return [], "no preview (프리뷰 비활성 또는 존재하지 않는 채널로 추정 — 프리뷰 섹션 없음)"

    wraps = section.xpath(
        ".//div[contains(concat(' ', normalize-space(@class), ' '), ' tgme_widget_message_wrap ')]"
    )
    messages: list[dict] = []
    for wrap in wraps:
        msg_divs = wrap.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), ' tgme_widget_message ')"
            " and @data-post]"
        )
        if not msg_divs:
            continue
        data_post = msg_divs[0].get("data-post") or ""
        msg_id = data_post.rsplit("/", 1)[-1] if data_post else ""
        if not msg_id:
            continue

        text_divs = wrap.xpath(
            ".//div[contains(concat(' ', normalize-space(@class), ' '), ' tgme_widget_message_text ')]"
        )
        text = text_divs[0].text_content().strip() if text_divs else ""
        links = [href for href in text_divs[0].xpath(".//a/@href")] if text_divs else []

        # 사진(서브프로젝트 S part 3) — 캡션 없는 사진 단독 메시지도 있고
        # (`text_divs` 자체가 없음), 캡션 있는 사진 메시지도 있다(둘 다 실측
        # 픽스처에 있다) — 두 경우 모두 `photo_wraps`는 `wrap` 범위에서 별도로 찾는다.
        photo_wraps = wrap.xpath(
            ".//a[contains(concat(' ', normalize-space(@class), ' '), "
            "' tgme_widget_message_photo_wrap ')]"
        )
        images = []
        for pw in photo_wraps:
            m = _BG_IMAGE_RE.search(pw.get("style") or "")
            if m:
                images.append(m.group(1))

        time_els = wrap.xpath(".//time[@datetime]")
        published = time_els[0].get("datetime") if time_els else None

        messages.append({
            "msg_id": msg_id,
            "text": text,
            "published": published,
            "links": links,
            "images": images,
        })

    messages.reverse()  # DOM은 오래된 메시지부터 나온다 — 최신순으로 뒤집는다
    return messages[:limit], None


def fetch_channel(handle: str, getter=None) -> list[dict]:
    """채널 하나의 최신 메시지. `[{"msg_id","text","published","links","images"}]`,
    최신순 최대 20. `images`는 사진 첨부 URL 리스트(없으면 빈 리스트).

    네트워크·파싱·프리뷰 비활성 어느 쪽이 실패해도 예외를 올리지 않고 빈
    리스트를 돌려준다 — `fetch_all`이 채널 단위로 실패를 격리할 수 있는 건
    이 계약 덕분이다.
    """
    get = getter or _http_get
    try:
        html_text = get(_PREVIEW_URL.format(handle=handle))
    except Exception:  # noqa: BLE001 — 채널 하나의 네트워크 실패가 전체를 막지 않는다
        return []
    messages, _reason = _parse_messages_with_reason(html_text)
    return messages


def fetch_all(getter=None, sleep=None) -> dict[str, dict]:
    """`CHANNELS` 전체를 순회. 채널 단위로 실패를 격리하고 실패 사유를 정직하게 남긴다.

    반환: `{handle: {"messages": [...], "error": str|None}}`. 채널 사이 0.5s 로
    쉰다(레이트리밋 회피 — `youtube_brief.py`의 순차 호출과 같은 이유).
    """
    get = getter or _http_get
    sleep_fn = sleep or time.sleep

    out: dict[str, dict] = {}
    for entry in CHANNELS:
        handle = entry["handle"]
        try:
            html_text = get(_PREVIEW_URL.format(handle=handle))
        except Exception as e:  # noqa: BLE001 — 채널 하나의 네트워크 실패가 전체를 막지 않는다
            out[handle] = {"messages": [], "error": f"{type(e).__name__}: {e}"}
            sleep_fn(0.5)
            continue
        messages, reason = _parse_messages_with_reason(html_text)
        out[handle] = {"messages": messages, "error": reason}
        sleep_fn(0.5)
    return out


def load_ledger(path: Path) -> list[dict]:
    """`telegram_msgs.jsonl` 전체를 읽는다(서브프로젝트 W part 2, 2026-08-17) —
    `quant.analyze.mentions.load_ledger`와 같은 관례(빈 줄 skip, 깨진 줄 없음
    가정 — 이 원장은 `append_ledger`만 쓴다). 중기 관심종목(§midterm_watch)이
    "최근 N일 언급" 판정에 여러 날짜의 누적 메시지가 필요해서, `fetch_all()`
    (최신 20개 스냅샷)이 아니라 이 누적 원장을 읽는다."""
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def append_ledger(rows: list[dict], path: Path) -> int:
    """`(handle, msg_id)` 기준 중복 제거 후 append. 반환값은 실제로 추가된 건수.

    각 row는 최소 `"handle"`, `"msg_id"` 키를 가져야 한다 — `fetch_all`이 돌려주는
    메시지 dict 자체엔 handle이 없으므로(채널별로 묶여 있음) 호출부가 채널 핸들을
    붙여 넘긴다. `dart.append_ledger`(단일 키 `rcept_no`)와 같은 append-only
    dedup 패턴이되, 텔레그램은 메시지 id가 채널 내부에서만 유일하므로 복합키를 쓴다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple[str, str]] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            handle = row.get("handle")
            msg_id = row.get("msg_id")
            if handle and msg_id:
                seen.add((handle, msg_id))

    to_write: list[dict] = []
    for row in rows:
        handle = row.get("handle")
        msg_id = row.get("msg_id")
        if not handle or not msg_id or (handle, msg_id) in seen:
            continue
        seen.add((handle, msg_id))
        to_write.append(row)

    if to_write:
        with path.open("a", encoding="utf-8") as f:
            for row in to_write:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(to_write)
