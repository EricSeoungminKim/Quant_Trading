"""Executive Summary — 본문 근거 기반 AI 통합 요약 (서브프로젝트 L, L-1).

스펙: docs/superpowers/specs/2026-08-17-exec-summary-design.md §L-1.

회사 리포트(13.209.240.206:9999)의 Executive Summary 급 통합 요약을 우리
근거(뉴스 본문 스크랩 + 수급 수치)로 만든다. `market_digest.summarize_digest`
(제목만으로 서술)보다 한 단계 더 나간다 — 상위 사건의 **기사 본문**을
`article_body.fetch_body`로 긁어 LLM 프롬프트에 근거로 넣는다(사용자 지시
2026-08-17: "근거들을 정확하게 모아서 LLM이 글을 생성, LLM의 근거도 조금
들어가면 확신도 있게").

**판단이 아니라 서술이다** — 어떤 사건을 실을지는 `market_digest.build_digest`
의 결정론(econ 큐레이션 + 다매체 보도량 순)이 이미 정했다. 여기 들어오는 건
확정된 사건 목록 + 시장 피처 + 외국인 수급 상위뿐이고, `summarize`가 하는
일은 그것을 세 문단(시장 흐름/수급/주요 재료)으로 서술하는 것뿐이다.

무LLM 폴백: `summarize`가 `None`을 돌려주면 호출부(`report_cli`)는 기존
시황 다이제스트 + 스탠스 블록만으로 이미 완전하다 — `market_digest.
summarize_digest`와 같은 계약.
"""
from __future__ import annotations

import re

from quant.collect.sources import article_body

# 근거로 쓸 사건 상한. 회사 리포트 벤치마킹상 3문단 요약에 6건이면 충분하고,
# 그 이상은 본문 fetch 비용만 늘린다(스펙 §2: "cap 6 fetches").
MAX_EVENTS = 6
BODY_EXCERPT_CHARS = 600


def gather_evidence(
    digest: dict,
    news_flow: list[dict] | None,
    features: dict | None,
    foreign_view: dict | None,
    fetch_body=None,
) -> dict:
    """상위 사건 본문 + 시장 피처 + 외국인 수급 상위를 한 근거 dict로 모은다.

    사건은 `digest`(국내+미국발, `build_digest`가 이미 econ 큐레이션 +
    다매체 보도량 순으로 정렬)에서 상위 `MAX_EVENTS`건을 뽑는다. `digest`가
    비어 있으면(econ 큐레이션을 통과한 사건이 없는 날) `news_flow`(전 매체
    다양성 목록, 이미 dup_count 내림차순 정렬)로 폴백한다 — "주요 재료" 문단이
    아예 비는 것보다는 큐레이션이 느슨한 사건이라도 있는 편이 낫다.

    각 사건의 본문은 `fetch_body`(기본 `article_body.fetch_body`)로 긁는다.
    실패(`None`)해도 사건 자체는 title-only로 근거에 남는다 — 완전 실패가
    아니라 저해상도 근거로 격리한다(개별 기사 실패가 전체를 죽이지 않는
    `article_body.fetch_body` 자체 계약과 같은 선).
    """
    fetch = fetch_body or article_body.fetch_body
    domestic = digest.get("domestic") or []
    us_impact = digest.get("us_impact") or []
    combined = domestic + us_impact
    if not combined:
        combined = list(news_flow or [])
    top = combined[:MAX_EVENTS]

    events: list[dict] = []
    for item in top:
        link = item.get("link", "")
        body_excerpt = None
        if link:
            body = fetch(link)
            if body:
                body_excerpt = body[:BODY_EXCERPT_CHARS]
        events.append({
            "title": item.get("title", ""),
            "outlet": item.get("outlet"),
            "link": link,
            "body_excerpt": body_excerpt,
        })

    f = features or {}
    flows = {
        "kospi_change_pct": f.get("kospi_change_pct"),
        "kosdaq_change_pct": f.get("kosdaq_change_pct"),
        "vix": f.get("vix"),
        "foreign_net_100m_krw": f.get("foreign_net_100m_krw"),
        "institution_net_100m_krw": f.get("institution_net_100m_krw"),
    }

    foreign_top: list[str] = []
    for sector in (foreign_view or {}).get("sectors") or []:
        for row in sector.get("rows") or []:
            label = row.get("label")
            name = row.get("name")
            if label and name:
                foreign_top.append(f"{name}({label})")

    return {"events": events, "flows": flows, "foreign_top": foreign_top[:5]}


def build_prompt(evidence: dict) -> str:
    """근거 자료(기사 발췌 + 수급 수치)를 나열하고 3문단 형식을 지시하는 프롬프트.

    문단 순서는 스펙 고정이다 — ① 시장 흐름 ② 수급(외국인 중심) ③ 주요
    재료(국내/미국발). 각 문단은 실제로 참고한 매체명을 `[근거: 매체1·매체2]`
    로 밝히게 한다 — 판단·추천이 아니라 근거가 있는 서술임을 문장 자체가
    증명하게 하려는 지시(사용자 2026-08-17: "LLM의 근거도 조금 들어가면
    확신도 있게").
    """
    flows = evidence.get("flows") or {}
    foreign_top = evidence.get("foreign_top") or []
    events = evidence.get("events") or []

    lines = [
        "다음은 오늘 한국 증시 근거 자료(시장 지표 · 수급 수치 · 기사 발췌)다.",
        "사실만 서술하고 투자 권유·추천은 하지 말 것. 아래 세 문단을 이 순서로,",
        "각 문단은 4문장 이내로 작성하라. 각 문단 끝에는 그 문단에서 실제로",
        "참고한 기사의 매체명을 [근거: 매체1·매체2] 형식으로 붙여라(참고한",
        "기사가 없으면 생략). 사건 자료 중 일부(usnews 텔레그램 헤드라인)는",
        "제목이 영어일 수 있다 — 그래도 반드시 한국어로만 답하라(영어·중국어",
        "등 다른 언어를 섞지 마라).",
        "",
        "① 시장 흐름: 코스피·코스닥 등락과 변동성(VIX).",
        "② 수급: 외국인·기관 순매수/순매도 중심.",
        "③ 주요 재료: 아래 사건 자료 중 근거가 되는 것만 골라 국내/미국발로.",
        "",
        "[시장 지표]",
    ]
    if flows.get("kospi_change_pct") is not None:
        lines.append(f"- 코스피 등락률: {flows['kospi_change_pct']:+.2f}%")
    if flows.get("kosdaq_change_pct") is not None:
        lines.append(f"- 코스닥 등락률: {flows['kosdaq_change_pct']:+.2f}%")
    if flows.get("vix") is not None:
        lines.append(f"- VIX: {flows['vix']:.1f}")
    if flows.get("foreign_net_100m_krw") is not None:
        lines.append(f"- 외국인 순매수: {flows['foreign_net_100m_krw']:+,}억원")
    if flows.get("institution_net_100m_krw") is not None:
        lines.append(f"- 기관 순매수: {flows['institution_net_100m_krw']:+,}억원")

    lines.append("")
    lines.append("[외국인 수급 상위 종목]")
    if foreign_top:
        lines += [f"- {label}" for label in foreign_top]
    else:
        lines.append("- (근거 없음)")

    lines.append("")
    lines.append("[사건 자료]")
    if events:
        for i, ev in enumerate(events, 1):
            outlet = ev.get("outlet") or "출처 미상"
            lines.append(f"{i}. [{outlet}] {ev.get('title', '')}")
            if ev.get("body_excerpt"):
                lines.append(f"   본문 발췌: {ev['body_excerpt']}")
    else:
        lines.append("- (근거 없음)")

    lines += [
        "",
        "다음 형식으로 정확히 세 문단만 답하라(다른 텍스트 없이):",
        "① 시장 흐름: <문단>",
        "",
        "② 수급: <문단>",
        "",
        "③ 주요 재료: <문단>",
    ]
    return "\n".join(lines)


_LABEL_RE = re.compile(
    r"^[①②③]?\s*(?:시장\s*흐름|수급|주요\s*재료)\s*[:：]?\s*", re.UNICODE,
)


def _split_three_paragraphs(text: str) -> list[str] | None:
    """narrator 응답을 정확히 세 문단으로 쪼갠다. 못 쪼개면 `None`(불가해).

    `market_digest._split_two_paragraphs`와 같은 규칙 — ①②③ 마커가 모두
    있으면 그 경계로 자르고, 없으면 빈 줄 기준으로 나눈다. 어느 쪽이든
    정확히 3문단이 아니면 실패로 취급한다(모델이 서두·군더더기를 붙였거나
    문단을 합쳤을 때 함부로 잘라 쓰지 않는다)."""
    text = text.strip()
    if "①" in text and "②" in text and "③" in text:
        i2 = text.index("②")
        i3 = text.index("③")
        parts = [text[:i2].strip(), text[i2:i3].strip(), text[i3:].strip()]
    else:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    parts = [p for p in parts if p]
    if len(parts) != 3:
        return None
    return parts


def _strip_label(paragraph: str) -> str:
    return _LABEL_RE.sub("", paragraph).strip()


def summarize(evidence: dict, narrator) -> dict | None:
    """근거를 세 문단(시장 흐름/수급/주요 재료) 산문으로 바꾼다.

    사건도 시장 지표도 하나도 없으면(완전히 빈 근거) narrator 를 부르지
    않는다 — 빈 입력으로 LLM 을 부르는 건 낭비다(`summarize_digest` 와
    같은 관례).

    `narrator.narrate`가 실패(`None`)하거나 응답을 정확히 세 문단으로 쪼갤
    수 없으면(또는 한 문단이라도 라벨을 뗀 뒤 빈 문자열이면) `None` — 호출부는
    결정론 다이제스트만으로 이미 완전하다(무LLM 폴백).

    `sources`는 LLM 이 실제로 인용했는지와 무관하게 근거로 투입된 사건들의
    매체명을 결정론으로 모은 것이다 — LLM 응답 안의 `[근거: ...]` 표기
    형식에 의존하면 파싱이 깨질 때 각주 자체가 사라지므로, 더 신뢰할 수 있는
    입력 쪽(evidence.events)에서 뽑는다.
    """
    events = evidence.get("events") or []
    flows = evidence.get("flows") or {}
    if not events and not any(v is not None for v in flows.values()):
        return None

    prompt = build_prompt(evidence)
    text = narrator.narrate(prompt)
    if text is None:
        return None

    parts = _split_three_paragraphs(text)
    if parts is None:
        return None
    market = _strip_label(parts[0])
    flow = _strip_label(parts[1])
    catalyst = _strip_label(parts[2])
    if not market or not flow or not catalyst:
        return None

    sources = sorted({ev["outlet"] for ev in events if ev.get("outlet")})
    return {"market": market, "flow": flow, "catalyst": catalyst, "sources": sources}
