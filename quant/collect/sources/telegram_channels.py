"""텔레그램 공개 채널 8방 — 웹 프리뷰 기반 브리핑 (2026-09-05, 소유자 지정 재편, "텔레그램
인텔리전스 레인" 착수). `blog_brief.py`/`youtube_brief.py`와 같은 자리, 같은 실패 격리
계약이다. 텔레그램 공개 채널은 `https://t.me/s/{handle}`에서 로그인 없이 웹 프리뷰(최근
메시지 스냅샷)를 HTML로 내려준다 — 공식 API 가 아니라 웹 페이지 스크레이핑이므로
`quant/collect/`(스크래핑 허용 평면)에 둔다. 채널을 늘리거나 바꾸고 싶으면
**`CHANNELS`만 바꾸면 된다.**

## 2026-09-05 재편 — 소유자가 지정한 8개로 교체

소유자가 "리포트는 이 8개 공개 채널만 참조한다"고 범위를 명시 — 기존 13개(usnews/
usdigest tier 포함, tazastock/pikachu_aje/Samsung_Global_AI_SW/rafikiresearch/
aetherjapanresearch 5개만 겹침)를 전량 교체한다. `usnews`/`usdigest` tier는 이제
아무 채널도 갖지 않는다(소비자는 `quant/report/collect/telegram.py` 모듈 docstring
"2026-09-05 채널 재편" 절 참고 — 이미 빈 리스트로 정직하게 졸아드는 순수 함수들이라
코드 변경 없이 그대로 둔다, 렌더 쪽만 "채널 없음" 명시 폴백을 추가했다).

## 실측 확인(2026-09-05, `curl -sL -o /dev/null -w '%{http_code}' https://t.me/s/{handle}`
+ `tgme_widget_message_wrap` 블록 카운트, 채널당 0.5s 간격 — `fetch_all` 그대로):

    tazastock              200, 20건
    clawnewssummary         200, 20건 (아래 "clawnewssummary — 본문 미지원" 절 참고)
    daegurr                 200, 18건
    hanwhastrategy          200, 20건
    pikachu_aje             200, 18건
    aetherjapanresearch     200, 20건
    rafikiresearch           200, 20건
    Samsung_Global_AI_SW    200, 19건

8개 전부 HTTP 200 + `tgme_channel_history` 섹션 존재(프리뷰 활성). 채널 성격은
프리뷰의 `tgme_channel_info_header_title`/`description`과 실제 메시지 샘플로
확인했다(추측 아님):

    tazastock          "타점 읽어주는 여자(타자)" — 장중 시황·국내 시황 코멘터리.
    clawnewssummary     표시명 "에테르의 AI뉴스"(핸들과 표시명이 다르다 — 채널
                       리브랜딩, canonical URL은 그대로 /s/clawnewssummary).
                       설명: "매 시각 정각즈음 최신뉴스 위주로 정리... AI를
                       활용하여 업로드하는 특성상 환각 등의 증상이 발생할 수
                       있으므로 반드시 원문을 확인하여 주세요" — 채널 스스로
                       AI 환각 가능성을 경고하는 시간당 해외뉴스(블룸버그·
                       로이터·NYT·AP·BBC·알자지라 취합) 요약 채널. 소유자 요구
                       (2) "채널 숫자를 그대로 사실로 쓰지 않는다"와 정확히
                       맞물리는 채널 — `tg_digest.py`가 이 채널 발 숫자를
                       "채널 주장"으로만 표기하고 검증 없이 우리 판단으로
                       쓰지 않는 이유가 여기 있다.
    daegurr             "💯똥밭에 굴러도 주식판" — 국내 시황 + 경제 캘린더
                       (예: "오늘의 캘린더 09.03, 총 33건 — ISM 비제조업,
                       월러 연준 이사 연설") + 지정학/매크로 코멘터리. 소유자
                       요구 (3)의 "리스크 브리핑 방"에 해당(매크로 이벤트·
                       지정학 리스크 취합).
    hanwhastrategy      "한화투자증권 리서치센터 투자전략팀" — 증권사 공식
                       채널, 주식/채권/환율 마감 시황 3종을 매일 발행
                       (예: "★ 주식 마감 시황 (9/4)- KOSPI 6,687.21pt
                       +1.64%..."). 소유자 요구 (3)의 리스크 브리핑 방 —
                       전략 코멘터리에 리스크 요인이 실린다.
    pikachu_aje         전력 섹터(기존과 동일, 변경 없음).
    aetherjapanresearch 일본·미국 리서치(기존과 동일, 변경 없음).
    rafikiresearch      Global macro research(기존과 동일, 변경 없음).
    Samsung_Global_AI_SW 삼성증권 이영진 — 글로벌 AI/SW(기존과 동일, 변경 없음).

### clawnewssummary — 본문 미지원(text_not_supported), 등록은 유지

`fetch_all`은 200 + 메시지 wrap 20건을 정상적으로 받아온다("프리뷰 꺼짐"이
아니다 — `tgme_channel_history` 섹션이 있고 위젯도 있다). 하지만 메시지 20/20건
전부가 `<div class="tgme_widget_message text_not_supported_wrap ...">`로 와서
본문 자체가 없다(웹 위젯이 "Please open Telegram to view this post / VIEW IN
TELEGRAM"만 보여준다 — 텔레그램 앱에서만 열람 가능한 형식으로 posting 중으로
추정, 원인은 채널 쪽 설정이라 이쪽에서 재현·회피할 수 없다). curl 3회 반복
재현, 20/20건 일관됨 — 일시적 장애가 아니라 이 채널의 지속 상태다.

**등록은 유지한다** — 소유자가 8개를 명시했고, 채널 자체·프리뷰 자체는 살아
있다(`report_figure_by_offset`처럼 프리뷰가 완전히 꺼진 것과는 다른 상태 —
그 채널은 실제로 `CHANNELS`에서 제거됐었다, 아래 옛 이력 참고). 대신
`_parse_messages_with_reason`이 "가져온 메시지 전부가 text_not_supported"를
감지해 `fetch_all`이 `error`에 명시적 사유("미리보기 없음 — 메시지는 있으나
본문이 웹 프리뷰 미지원 형식")를 남기게 고쳤다 — 빈 text 메시지 20개를
정상 메시지처럼 조용히 돌려주지 않는다. `tg_digest.py`는 이 채널에서 실질
콘텐츠를 못 뽑으므로 매 다이제스트에 "clawnewssummary: 미리보기 없음"으로
정직하게 나타난다. 소유자에게 우회 경로(봇 포워딩 등)를 문의할 근거 자료가
이 절이다.

## 이전 등록(13개) 이력 — report_figure_by_offset 제거(2026-09-03, F8)

2026-08-17 등록된 `report_figure_by_offset`은 3주 넘게 302 리다이렉트(프리뷰
완전 비활성, `tgme_channel_history` 섹션 자체 없음)로 정지 상태였고 2026-09-03
제거됐다 — clawnewssummary(위 절)와는 다른 상태였다(그쪽은 섹션 자체가 없어
데이터를 아예 못 받았고, 이쪽은 메시지는 받지만 본문이 없다).

파싱 픽스처(`tests/report/fixtures/telegram_tazastock.html`)는 2026-08-17 실측
tazastock 응답에서 실제 메시지 5건을 그대로 잘라 저장한 것 — 추측이 아니라 실제
HTML. `telegram_preview_disabled.html`은 옛 `report_figure_by_offset` 리다이렉트
목적지를 그대로 저장한 것 — 채널 등록과 무관하게 "프리뷰 꺼짐" HTML 모양 자체를
검증하는 데 계속 쓴다(`_parse_messages_with_reason`의 "no preview" 분기).
`telegram_pikachu_photo.html`(2026-08-17 실측, 서브프로젝트 S part 3)은 사진
메시지 파싱(`_BG_IMAGE_RE`) 검증에 계속 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import lxml.html as LH

from quant.adapters.http import client
from quant.collect.sources.feeds import parse_published

# 여기만 바꾸면 된다 — handle, 분류, market(KR|US|BOTH), tier(sector|macro|news).
# 2026-09-05: 소유자가 지정한 8개로 전량 교체(모듈 docstring "2026-09-05 재편"
# 절 참고) — usnews/usdigest tier는 이제 아무 채널도 없다(빈 필터 결과, 코드는
# 그대로 둔다 — `quant/report/collect/telegram.py` 모듈 docstring 참고).
CHANNELS: list[dict] = [
    {"handle": "tazastock", "분류": "장중 시황·국내 시황", "market": "KR", "tier": "news"},
    # clawnewssummary — 표시명 "에테르의 AI뉴스", 시간당 해외뉴스 AI 요약(채널
    # 스스로 환각 가능성을 경고한다 — 모듈 docstring "clawnewssummary" 절).
    # 웹 프리뷰가 메시지 본문을 못 주는 상태(text_not_supported)라 실질적으로
    # 매 수집마다 "미리보기 없음"으로 나타난다 — 등록은 소유자 지정대로 유지.
    # `preview: False`(2026-09-05, "포워딩 우회" 절) — 프리뷰가 본문을 못 주는
    # 채널이라는 표시. 오너가 이 채널 게시물을 봇 채팅으로 직접 포워딩하면
    # `server/scripts/tg_bridge.py`가 원장에 실제 본문을 적립하고,
    # `quant/report/collect/briefs.py`의 `_merge_telegram_results`가 이 플래그를
    # 보고 fresh(항상 빈 스크레이핑) 대신 원장분을 쓴다.
    {"handle": "clawnewssummary", "분류": "해외뉴스 AI 요약(시간당)", "market": "BOTH", "tier": "news",
     "preview": False},
    # daegurr — "💯똥밭에 굴러도 주식판", 국내 시황 + 경제 캘린더 + 지정학
    # 코멘터리. 소유자 요구 (3) 리스크 브리핑 방.
    {"handle": "daegurr", "분류": "국내 시황·경제 캘린더·리스크 코멘터리", "market": "KR", "tier": "news"},
    # hanwhastrategy — 한화투자증권 리서치센터 투자전략팀 공식 채널, 주식/채권/
    # 환율 마감 시황 3종. 소유자 요구 (3) 리스크 브리핑 방.
    {"handle": "hanwhastrategy", "분류": "증권사 전략·리스크 브리핑", "market": "KR", "tier": "macro"},
    {"handle": "pikachu_aje", "분류": "전력 섹터", "market": "KR", "tier": "sector"},
    {"handle": "aetherjapanresearch", "분류": "일본·미국 리서치", "market": "BOTH", "tier": "macro"},
    {"handle": "rafikiresearch", "분류": "매크로 종합", "market": "BOTH", "tier": "macro"},
    {"handle": "Samsung_Global_AI_SW", "분류": "글로벌 AI/SW — 삼성 이영진", "market": "BOTH", "tier": "sector"},
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
    # `text_not_supported`(2026-09-05, clawnewssummary 실측) — 웹 프리뷰가 아예
    # 안 꺼졌는데도(`tgme_channel_history` 섹션 있음, 메시지 wrap 20개 있음)
    # 메시지 div 자체가 `<div class="tgme_widget_message text_not_supported_wrap
    # ...">`로 와서 본문이 없다("Please open Telegram to view this post" 안내만
    # 있음) — 20/20건 재현(clawnewssummary, curl 3회 반복). "프리뷰 꺼짐"과 달리
    # 채널 자체·프리뷰 자체는 살아 있어 `_history_section`만으로는 못 잡는다 —
    # 아래에서 메시지 단위로 감지해, 가져온 메시지 전부가 이 상태면 명시적
    # 사유를 남긴다(빈 text 메시지를 정상 메시지처럼 조용히 돌려주지 않는다).
    unsupported_flags: list[bool] = []
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
        is_unsupported = "text_not_supported_wrap" in (msg_divs[0].get("class") or "")

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
        unsupported_flags.append(is_unsupported)

    messages.reverse()  # DOM은 오래된 메시지부터 나온다 — 최신순으로 뒤집는다
    unsupported_flags.reverse()
    sliced = messages[:limit]
    if sliced and all(unsupported_flags[: len(sliced)]) and not any(
        m["text"] or m["images"] for m in sliced
    ):
        return sliced, (
            "미리보기 없음 — 메시지는 있으나 본문이 웹 프리뷰 미지원 형식"
            "(text_not_supported, 텔레그램 앱에서만 열람 가능)"
        )
    return sliced, None


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


def load_window(path: Path, since: datetime, until: datetime | None = None) -> list[dict]:
    """원장(`telegram_msgs.jsonl`)에서 `[since, until]` 발행 구간 메시지만,
    최신순으로 돌려준다.

    `load_ledger`는 원장 전체를 읽는다 — 이 함수는 그중 리포트 창에 해당하는
    메시지만 골라 `fetch_all()`이 돌려주는 채널별 순서(최신순)와 같은 정렬로
    맞춘다. `append_ledger`가 이미 (handle, msg_id) 중복을 막지만, 방어적으로
    한 번 더 제거한다. 발행시각을 못 읽은 행은 버리지 않는다(`collector.
    load_window`와 같은 원칙).
    """
    until = until or datetime.now(timezone.utc)
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    for row in load_ledger(path):
        dt = parse_published(row.get("published"))
        if dt is not None and not (since <= dt <= until):
            continue
        key = (row.get("handle"), row.get("msg_id"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(
        key=lambda r: parse_published(r.get("published")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return rows


def prune(path: Path, today: date, keep_days: int = 14) -> int:
    """`telegram_msgs.jsonl`을 `keep_days`(기본 14일) 이내로 축소한다.

    `collector.prune`(뉴스, 날짜별 파일 삭제)과 달리 이 원장은 파일 하나뿐이라
    행 단위로 걸러 다시 쓴다. 발행시각을 못 읽은 행은 지우지 않는다(collector와
    같은 원칙 — 모르는 것을 지우지 않는다). 지운 행 수를 반환한다."""
    if not path.exists():
        return 0
    cutoff = today - timedelta(days=keep_days)
    kept: list[dict] = []
    removed = 0
    for row in load_ledger(path):
        dt = parse_published(row.get("published"))
        if dt is not None and dt.date() < cutoff:
            removed += 1
            continue
        kept.append(row)
    if removed:
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for row in kept:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    return removed
