"""텔레그램 공개 채널 인사이트 — 리포트 뷰 + 종목 언급 가산점 입력 (서브프로젝트 S part 2/3).

part 1(`quant/collect/sources/telegram_channels.py`)이 채널별 최신 메시지를
웹 프리뷰에서 긁어온다. 이 모듈은 그 결과를 세 곳에 쓴다:

1. `build_telegram_view` — 리포트 템플릿(§텔레그램 인사이트)이 그대로 뿌릴 수
   있는 카드 뷰. `quant.analyze.render.youtube_view`/`blog_view`와 같은 순수
   포맷 함수(네트워크 없음). part 3(2026-08-17)부터 사진 개수/원본 URL도
   싣는다 — 다만 텔레그램 CDN URL 자체를 카드에 임베드하지 않는다(t.me 원문
   링크만 칩으로 연결).
2. `telegram_mentions` — 당일 단타 스코어러(§intraday_score, v4) "텔레그램
   시그널" 가산점의 입력. 채널 메시지에서 종목 언급을 뽑아 섹터방(tier=sector)
   언급 여부를 표시한다.
3. `narrate_channels` — 채널별 최근 동향 1문장 요약(선택, LLM). `section_advice.
   advise`와 같은 all-or-nothing 계약 — 부분 실패는 통째로 `None`(무LLM 폴백은
   호출부가 담당한다). **마감 리포트(§close_report)는 이 함수를 부르지 않는다**
   — 마감판은 완전 무LLM이 계약이다(`report_cli._emit_close` docstring).
4. `describe_sector_images`(part 3, 선택, LLM) — 섹터방 사진 최대 3장에 한국어
   1문장 해석을 붙인다. `narrate_channels`와 같은 이유로 **마감 리포트는 이
   함수도 부르지 않는다**(사진 칩·링크는 마감판에도 싣지만 결정론적이다).
"""
from __future__ import annotations

import re

from quant.analyze.entities import extract
from quant.collect.sources.feeds import parse_published
from quant.collect.sources.telegram_channels import channels_for
from quant.core.report_clock import KST

ITEMS_PER_CHANNEL = 5
SUMMARY_MAX_LEN = 120
_SENTENCE_SEPS = ("\n", ". ", "! ", "? ")


def _summarize(text: str, max_len: int = SUMMARY_MAX_LEN) -> str:
    """첫 문장 우선, 없으면 `max_len`자 절단(+ '…'). 개행·중복 공백은 정리한다."""
    raw = (text or "").strip()
    if not raw:
        return ""
    cut = len(raw)
    for sep in _SENTENCE_SEPS:
        idx = raw.find(sep)
        if 0 < idx < cut:
            cut = idx
    sentence = " ".join(raw[:cut].split())
    if len(sentence) <= max_len:
        return sentence
    return sentence[:max_len].rstrip() + "…"


def _hhmm(published: str | None) -> str | None:
    """텔레그램 `<time datetime>` 원문(UTC, ISO8601) → KST 'HH:MM'. 파싱 실패는
    `None`(feeds.parse_published 계약 그대로 — 시각을 지어내지 않는다)."""
    dt = parse_published(published)
    if dt is None:
        return None
    return dt.astimezone(KST).strftime("%H:%M")


def build_telegram_view(msgs_by_handle: dict, market: str) -> list[dict]:
    """`telegram_channels.fetch_all()`의 결과(`{handle: {"messages":[...], "error":...}}`)를
    `market`(KR/US)에 해당하는 채널만 골라 카드 뷰로 정리한다.

    채널 순서는 `channels_for`가 `CHANNELS`(telegram_channels.py) 등록 순서를
    그대로 보존한다. 메시지가 0건이고 error도 없는 채널(그냥 조용한 채널)도
    카드는 그린다 — "결과 없음"과 "수집 실패"를 템플릿이 구분해야 한다.

    반환: `[{"handle", "분류", "tier", "msgs": [{"text","link","published_hhmm",
    "image_count","first_image_url"}, ...], "error"}]`. `msgs`는 채널당 최신
    `ITEMS_PER_CHANNEL`(5)개. 키를 "items"가 아니라 "msgs"로 쓰는 이유: Jinja 의
    점(.) 접근이 dict 내장 메서드(`dict.items`)와 이름이 겹치면 값이 아니라
    메서드 객체를 돌려준다(실측, `render.group_carried_by_sector`의 "tiles"
    관례와 같은 회피).

    `image_count`/`first_image_url`(서브프로젝트 S part 3)은 `telegram_channels.
    fetch_channel`이 뽑은 사진 URL 리스트에서 만든다 — 텔레그램 CDN URL 자체는
    카드에 임베드하지 않는다(외부 이미지 핫링크는 리포트 자립성·프라이버시상
    피한다, 템플릿은 "🖼 사진 N" 칩으로 t.me 원문 링크만 건다).
    `first_image_url`은 화면에 노출하지 않고 `describe_sector_images`(선택,
    LLM 해석)의 입력으로만 쓴다.
    """
    out: list[dict] = []
    for entry in channels_for(market):
        handle = entry["handle"]
        result = msgs_by_handle.get(handle) or {}
        messages = result.get("messages") or []
        msgs = [
            {
                "text": _summarize(msg.get("text", "")),
                "link": f"https://t.me/{handle}/{msg['msg_id']}" if msg.get("msg_id") else None,
                "published_hhmm": _hhmm(msg.get("published")),
                "image_count": len(msg.get("images") or []),
                "first_image_url": (msg.get("images") or [None])[0],
            }
            for msg in messages[:ITEMS_PER_CHANNEL]
        ]
        out.append({
            "handle": handle,
            "분류": entry["분류"],
            "tier": entry["tier"],
            "msgs": msgs,
            "error": result.get("error"),
        })
    return out


# ---------------------------------------------------------------------------
# 종목 언급 → 단타 스코어러 v4 "텔레그램 시그널" 가산점 입력
# ---------------------------------------------------------------------------

# US는 오늘 payload 심볼(이미 검증된 후보)만 화이트리스트로 쓴다 — entities.
# extract_us 의 상장 전체 사전을 텔레그램 언급 채점 때문에 새로 받지 않는다
# (사용자 지시: "simple regex uppercase 2-5 + validation against known
# tickers in payload only"). 대문자 2~5글자만 후보로 본다 — entities.py의
# MIN_TICKER_LEN(3)보다 느슨하게 잡고 화이트리스트 대조로 방어한다(회사명
# 매칭까지는 하지 않는다 — 텔레그램 메시지는 헤드라인보다 약어가 잦다).
_US_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")


def telegram_mentions(msgs_by_handle: dict, name_table, market: str) -> dict[str, dict]:
    """텔레그램 메시지에서 종목 언급을 뽑아 `{symbol: {"channels": [...], "sector_room": bool}}`을 만든다.

    `name_table`은 시장별로 형태가 다르다:
    - KR: `entities.load_table(cache_dir)`가 만드는 `[(name, code), ...]`
      (긴 이름 우선 정렬) — `entities.extract`의 경계 검사(조각 오탐 방어)를
      그대로 재사용한다.
    - US: 오늘 리포트 payload 심볼(문자열) 집합/리스트 — 정규식으로 뽑은
      대문자 후보를 이 집합과 대조해서만 채택한다.

    `channels`의 각 원소는 `{"handle", "분류", "tier"}` — evidence 문자열
    조립(intraday_score._telegram_factor)에 채널 정보가 그대로 필요해서
    handle만 남기지 않는다. `sector_room`은 언급 채널 중 하나라도
    tier=="sector"면 True.
    """
    known_us = set(name_table) if market == "US" else None
    out: dict[str, dict] = {}
    for entry in channels_for(market):
        handle = entry["handle"]
        result = msgs_by_handle.get(handle) or {}
        for msg in result.get("messages") or []:
            text = msg.get("text") or ""
            if not text:
                continue
            if market == "KR":
                symbols = {hit["symbol"] for hit in extract(text, name_table)}
            else:
                symbols = {sym for sym in _US_TICKER_RE.findall(text) if sym in known_us}
            for symbol in symbols:
                rec = out.setdefault(symbol, {"channels": [], "sector_room": False})
                if not any(c["handle"] == handle for c in rec["channels"]):
                    rec["channels"].append({
                        "handle": handle, "분류": entry["분류"], "tier": entry["tier"],
                    })
                if entry["tier"] == "sector":
                    rec["sector_room"] = True
    return out


# ---------------------------------------------------------------------------
# 채널별 동향 1문장 요약 (선택, LLM) — section_advice.advise 와 같은 관례
# ---------------------------------------------------------------------------

def _narration_prompt(view: list[dict], present: list[str]) -> str:
    by_handle = {c["handle"]: c for c in view}
    lines = [
        "다음은 텔레그램 공개 채널들의 최근 메시지 목록이다. 채널별 최근 동향을",
        "1문장으로 요약하라. 규칙:",
        "- 채널당 정확히 1문장.",
        "- 제시된 메시지 내용만 근거로 쓰고, 새 사실을 지어내지 않는다.",
        "- 매수/매도 지시나 단정적 추천은 하지 않는다.",
        "",
    ]
    for handle in present:
        ch = by_handle[handle]
        lines.append(f"[{handle}] ({ch['분류']})")
        for item in ch["msgs"]:
            if item["text"]:
                lines.append(f"- {item['text']}")
        lines.append("")

    lines.append("다음 형식으로 정확히 요청된 채널만, 마커 그대로 답하라(다른 텍스트 없이):")
    for handle in present:
        lines.append(f"[{handle}]")
        lines.append("<1문장>")
        lines.append("")
    return "\n".join(lines)


def _parse_narration(text: str, present: list[str]) -> dict[str, str] | None:
    """`section_advice._parse`와 같은 all-or-nothing — 마커가 하나라도 없거나
    어느 채널 내용이든 비어 있으면 통째로 실패(`None`)."""
    text = text.strip()
    positions = []
    for handle in present:
        marker = f"[{handle}]"
        idx = text.find(marker)
        if idx == -1:
            return None
        positions.append((idx, handle))
    positions.sort()

    result: dict[str, str] = {}
    for i, (idx, handle) in enumerate(positions):
        start = idx + len(f"[{handle}]")
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        content = text[start:end].strip()
        if not content:
            return None
        result[handle] = content
    return result


def narrate_channels(view: list[dict], narrator) -> dict[str, str] | None:
    """채널별 최근 동향 1문장 요약. 메시지가 있는 채널이 하나도 없으면
    narrator 를 부르지 않는다(빈 입력으로 LLM 을 부르는 건 낭비 —
    `section_advice.advise`와 같은 원칙)."""
    present = [c["handle"] for c in view if c.get("msgs")]
    if not present:
        return None

    text = narrator.narrate(_narration_prompt(view, present))
    if text is None:
        return None
    return _parse_narration(text, present)


# ---------------------------------------------------------------------------
# 사진 AI 해석 (선택, LLM, 서브프로젝트 S part 3) — 섹터방 사진만, 예산 상한
# ---------------------------------------------------------------------------

IMAGE_DESC_BUDGET = 3


def describe_sector_images(view: list[dict], describe) -> dict[str, str]:
    """섹터방(`tier == "sector"`) 카드의 사진에 한국어 1문장 해석을 붙인다(선택, LLM).

    `describe`는 API 키가 이미 바인딩된 콜러블(`url -> str|None`) — 호출부(`report_cli`)
    가 `quant.adapters.narrate.describe_image`를 감싸서 넘긴다(`narrate_channels`가
    `narrator` 객체를 받는 것과 같은 DI 관례, 다만 프롬프트가 아니라 이미지 URL이라
    포트 모양이 달라 별도 콜러블로 받는다).

    최대 `IMAGE_DESC_BUDGET`(3)장까지만 호출한다(예산 상한 — 사용자 지시). 소진
    순서는 `view` 채널 등록 순 → 채널 안에서는 이미 최신순인 `msgs` 순서다. 채널을
    넘나드는 "진짜 최신" 판정은 하지 않는다 — `published_hhmm`은 시:분만 있고
    날짜가 없어 자정을 넘나드는 채널 간 비교가 오탐 위험이 크다(정직한 근사).

    반환: `{link: 1문장 해석}` — 템플릿이 `item.link`로 조회한다. 호출이 실패하면
    (`describe`가 `None`) 예산만 소모하고 결과엔 남기지 않는다 — 실패는 조용히
    생략된다(`Narrator` 포트와 같은 계약).
    """
    out: dict[str, str] = {}
    budget = IMAGE_DESC_BUDGET
    for ch in view:
        if ch["tier"] != "sector":
            continue
        for item in ch["msgs"]:
            if budget <= 0:
                return out
            url = item.get("first_image_url")
            if not url:
                continue
            budget -= 1
            desc = describe(url)
            if desc:
                out[item["link"]] = desc
    return out
