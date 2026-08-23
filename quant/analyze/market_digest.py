"""오늘의 시황 요약 — 사건 단위 국내/미국발 분리 다이제스트 (서브프로젝트 I).

`build_digest`는 순수 함수다(LLM 없음, 네트워크 없음, 완전 결정론) — 사용자
원칙(2026-08-17) "국내 뉴스는 다 똑같은 말 = 노이즈, 사건 단위 중복 제거를
확실히"를 리포트 상단 요약 섹션에도 적용한다.

`quant.analyze.news_cluster.dedup_with_counts`(H-2)로 그날 전체 뉴스(모든
피드 합)를 사건 단위로 묶어 다매체 보도(=중요한 사건)를 `dup_count` 내림차순
상위 몇 건만 남긴 뒤, 제목에 미국 맥락 키워드가 있는지로 "국내"/"미국발"을
가른다. 회사 리포트(13.209.240.206:9999)의 시황 요약 섹션을 벤치마킹한 것.

`summarize_digest`는 예외다 — 리포트 평면은 LLM 이 허용된다(ADR-0002 는
**거래** 핫패스만 금지한다, 사용자 명시 2026-08-17). 그래도 **판단은 하지
않는다** — 무엇을 실을지는 `build_digest`의 결정론이 이미 정했고,
`summarize_digest`는 그 확정된 제목들을 문장으로 바꾸는 서술(narration)만
한다. narrator 가 없거나 실패하면 `None` — 호출부는 결정론 목록만으로 이미
완전하다(무LLM 폴백).
"""
from __future__ import annotations

import re
from datetime import datetime

from quant.analyze.news_cluster import cluster_titles, dedup_with_counts

# 시황 요약에 실을 사건 대표 수. 리포트 상단 섹션이라 짧아야 한다 — 회사
# 리포트 벤치마킹 관찰상 8개 안팎이면 한 화면에 훑힌다.
TOP_N = 8

# 제목에 이 중 하나라도 있으면 "미국발 — 한국 영향"으로 분류한다. **초기값이고
# 실측(오탐/누락) 조정 대상**이다(스펙 §리포트 추가 요소). "금리"처럼 국내
# 기준금리 기사와 겹칠 수 있는 단어도 사용자가 지정한 목록 그대로 우선
# 반영한다 — 정밀도보다 "미국발 후보를 놓치지 않는" 쪽을 택한 초기 설계.
US_CONTEXT_KEYWORDS: tuple[str, ...] = (
    "미국", "연준", "Fed", "나스닥", "S&P", "다우", "금리", "파월", "관세", "달러",
)

# 경제/주식 전문지 상수 목록(스펙 §2 "아무 신문이나 들어간다" 피드백에 대한
# 큐레이션). 실측 근거(2026-08-17, data/news/KR/2026-08-15.jsonl, 942건/
# 99개 고유 outlet 집계) — 사용자가 든 초기 목록(한국경제·매일경제·서울경제·
# 이데일리·머니투데이·아시아경제·파이낸셜뉴스·조선비즈·헤럴드경제·
# 연합인포맥스 등)에 실측으로 확인된 전문지를 추가했다:
#   한국경제 104건, 머니투데이 103건, 조선비즈 100건, 매일경제 98건,
#   이데일리 50건, 인포스탁데일리 50건("스탁" 전문 매체, 목록에 추가),
#   아주경제 3건(사용자가 든 "아시아경제"와 실제 표기가 다른 별개 매체,
#   추가), 이투데이 3건(추가), 한국경제TV/팍스경제TV 3건(각 3건).
# 반대로 실측 상위였지만 **의도적으로 제외**한 매체 — 이게 큐레이션의 핵심:
#   연합뉴스 122건·경향신문 51건·동아일보 51건(종합/시사 일간지),
#   ZDNet코리아 30건·전자신문 23건(IT/산업지). 사용자가 "아무 신문이나
#   들어간다"고 지적한 대상이 정확히 이 부류다.
ECON_OUTLETS: tuple[str, ...] = (
    "한국경제", "한경", "매일경제", "매경", "서울경제", "이데일리", "머니투데이",
    "아시아경제", "아주경제", "파이낸셜뉴스", "조선비즈", "헤럴드경제",
    "연합인포맥스", "인포맥스", "인포스탁데일리", "이투데이", "팍스경제",
    "자본시장뉴스", "서울파이낸스", "알파경제", "이코노미스트", "경제타임스",
    "굿모닝경제", "조세일보", "비즈니스포스트", "서울이코노미뉴스",
)


def _is_us_impact(title: str) -> bool:
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in US_CONTEXT_KEYWORDS)


# "오늘의 뉴스 흐름"(econ 큐레이션 없는 전체 목록) 안에서 증권·경제 관련
# 사건을 위로 올리는 데 쓰는 제목 키워드(스펙 §L-2, 2026-08-17). ECON_OUTLETS
# 는 매체 단위 큐레이션이고 이건 제목 단위 — 종합지가 증권 기사를 냈을 때도
# 잡아낸다. **버리지 않는다** — build_digest 의 econ 큐레이션(대표만 남기고
# 탈락은 소실)과 달리 여기는 순서만 바꾼다. 종합지의 비증권 기사는 여전히
# `<details>` 접힘 안에서 전부 찾을 수 있다.
STOCK_KEYWORDS: tuple[str, ...] = (
    "증시", "주가", "코스피", "코스닥", "상장", "실적", "공모", "투자", "반도체",
)


def _is_market_relevant(title: str, outlet: str | None) -> bool:
    """econ 매체 보도이거나 제목이 `STOCK_KEYWORDS`에 매치하면 증권·경제 관련."""
    if _is_econ_outlet(outlet):
        return True
    lowered = title.lower()
    return any(kw.lower() in lowered for kw in STOCK_KEYWORDS)


def _is_econ_outlet(outlet: str | None) -> bool:
    """부분 문자열 매치 — outlet 문자열 안에 `ECON_OUTLETS` 중 하나라도
    들어 있으면 econ 으로 친다. "한국경제TV"는 "한국경제"를 포함하므로
    자동으로 econ 이다(방송 자회사까지 별도 등재할 필요 없이 이 규칙 하나로
    커버 — 실측에서 확인한 "한국경제TV"·"팍스경제TV" 케이스가 이 규칙으로
    풀린다)."""
    if not outlet:
        return False
    return any(name in outlet for name in ECON_OUTLETS)


def build_digest(feeds: dict, market: str, usnews_titles: list[str] | None = None) -> dict:
    """오늘의 시황 요약 — 경제지 큐레이션을 거친 사건 대표 상위 `TOP_N`을
    국내/미국발로 분리.

    `feeds`는 스냅샷 뉴스 SourceResult 의 `data["feeds"]`(피드명 → 기사
    리스트) 그대로다. 모든 피드의 기사를 하나로 합쳐 사건 단위로 묶는다 —
    같은 사건이 여러 피드에 걸쳐 있어도 한 대표로 합쳐져야 다매체 판정
    (`dup_count`)이 정확하다.

    `usnews_titles`(선택, P0 배선 수정 2026-08-19) — 텔레그램 usnews tier
    채널(walterbloomberg/financialjuice) 헤드라인. 이 리포트의 다른 섹션
    (`🇺🇸→🇰🇷 미국발 섹터 수혜주`/`🇺🇸 실시간 헤드라인`)엔 이미 닿는데
    `feeds`(econ 매체 RSS)엔 안 걸려 있어, `us_impact`가 비면 이 함수를
    감싸는 `summarize_digest`가 "미국발 뉴스 없음"을 그대로 서술하는
    문제가 있었다. 이 채널은 채널 자체가 이미 "미국발"로 선별돼 있으므로
    `US_CONTEXT_KEYWORDS`/econ 큐레이션 재분류 없이 바로 `us_impact`
    후보로 얹는다 — 근거: `quant/analyze/us_sector_map.py` 모듈 docstring이
    같은 채널을 "US 뉴스는 그대로"로 취급하는 전제와 동일하다.

    **경제지 큐레이션**(스펙 §2): 사건 대표는 자신의 `outlet` 이 econ 이거나,
    같은 사건을 보도한 클러스터 멤버 중 하나라도 econ 이면 후보다 — 비econ
    매체가 대표로 뽑혔어도(예: `published` 최신이라서) 경제지가 같이 다룬
    사건이면 살아남는다. `dedup_with_counts` 는 최종 대표만 돌려주고 원본
    클러스터 멤버 목록은 노출하지 않으므로, 여기서 `cluster_titles`(제목
    자카드 클러스터링 — `dedup_with_counts` 가 쓰는 것과 동일 함수)로 먼저
    멤버십을 구해 각 원본 기사에 econ 자격 플래그를 심어 넘긴다.
    `dedup_with_counts` 의 대표 dict 는 원본을 얕은 복사하므로 이 플래그가
    대표에 그대로 실려 나온다 — `news_cluster.py` 내부(완전중복 link 병합
    등)를 손대지 않고 얻는 방법이다. econ 자격이 없는 사건은 다이제스트에서
    빠지되 **소실되지 않는다** — "오늘의 뉴스 흐름" 섹션(별도 작업)이
    전체를 그대로 보여준다.

    `market`은 현재 분류 규칙에 쓰이지 않는다 — 시장별 키워드 분리가
    필요해지면 이 시그니처를 그대로 확장한다(호출부 계약 유지).

    뉴스가 하나도 없으면(또는 econ 큐레이션 후 남는 사건이 없으면) 두
    리스트 모두 빈 리스트로 정직하게 돌려준다(섹션 자체를 생략할지는
    템플릿이 판단한다).
    """
    all_items = [item for items in feeds.values() for item in items]
    domestic: list[dict] = []
    us_impact: list[dict] = []

    if all_items:
        own_econ = [_is_econ_outlet(item.get("outlet")) for item in all_items]
        econ_ok = list(own_econ)
        for group in cluster_titles(all_items):
            if any(own_econ[i] for i in group):
                for i in group:
                    econ_ok[i] = True
        flagged_items = [
            {**item, "_econ_ok": econ_ok[i]} for i, item in enumerate(all_items)
        ]

        reps = [r for r in dedup_with_counts(flagged_items) if r.get("_econ_ok")]
        # 다매체 보도 = 큰 사건이라 dup_count 내림차순. 안정 정렬이라 동점은
        # dedup_with_counts 가 이미 정한 순서(그룹 최소 입력 인덱스 오름차순)를
        # 그대로 유지한다 — 결정론.
        reps.sort(key=lambda r: -(r.get("dup_count") or 1))
        top = reps[:TOP_N]

        for r in top:
            title = r.get("title", "")
            entry = {
                "title": title,
                "link": r.get("link", ""),
                "dup_count": r.get("dup_count", 1),
            }
            outlet = r.get("outlet")
            if outlet:
                entry["outlet"] = outlet
            (us_impact if _is_us_impact(title) else domestic).append(entry)

    if usnews_titles:
        existing_titles = {e["title"] for e in us_impact}
        candidates = [
            {"title": t} for t in usnews_titles if t and t not in existing_titles
        ]
        if candidates:
            usnews_reps = dedup_with_counts(candidates)
            usnews_reps.sort(key=lambda r: -(r.get("dup_count") or 1))
            for r in usnews_reps:
                if len(us_impact) >= TOP_N:
                    break
                us_impact.append({
                    "title": r["title"],
                    "link": "",
                    "dup_count": r.get("dup_count", 1),
                    "outlet": "텔레그램 실시간(usnews)",
                })

    return {"domestic": domestic, "us_impact": us_impact}


# "오늘의 뉴스 흐름"에 실을 사건 상한(스펙 §1). 다이제스트(TOP_N=8)보다 훨씬
# 넉넉하다 — 여기는 econ 큐레이션이 없는 전체 흐름이 목적이라 템플릿이 상위
# 12건만 바로 보여주고 나머지는 <details>로 접는다. 이 한도 자체가 "전체"의
# 상한이다(정보 소실 방지 vs. 무한정 렌더링 사이 타협).
NEWS_FLOW_LIMIT = 30


def _parse_published(value) -> datetime | None:
    """`news_cluster._parse_published`와 같은 규칙(모듈 간 결합을 피하려고
    로컬에 다시 둔다 — 둘 다 stdlib 만 쓰는 작은 헬퍼라 중복 비용이 낮다)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _published_sort_key(value) -> float:
    """정렬 전용 — 파싱 불가/결측은 가장 오래된 것으로 취급해 맨 뒤로 민다
    (소실이 아니라 순서 문제일 뿐, `NEWS_FLOW_LIMIT` 안에서는 여전히 보인다)."""
    dt = _parse_published(value)
    return dt.timestamp() if dt is not None else float("-inf")


def build_news_flow(feeds: dict, limit: int = NEWS_FLOW_LIMIT) -> list[dict]:
    """"오늘의 뉴스 흐름" — `build_digest`(경제지 큐레이션 + 대표 8건)의 반대
    극단이다(스펙 §1, 사용자 피드백 2026-08-17: "리포트는 네이버 증권처럼
    여러 매체의 뉴스가 풍성하게 보여야 한다"). econ 필터도 TOP_N 자름도 없이
    그날 전체 뉴스를 `dedup_with_counts`로 사건 단위만 묶는다 — 다양성 자체가
    목적이라 어떤 매체든 살아남는다.

    정렬은 (증권·경제 관련 우선, dup_count 내림차순, published 내림차순) —
    `_is_market_relevant`(econ 매체 또는 증시 키워드)가 참인 사건이 먼저 오고,
    그 안에서 다매체 보도된 사건이 앞, 동률이면 최신이 앞(스펙 §L-2, "아무
    신문이나 들어간다"는 지적에 대한 우선순위 조정 — 제거가 아니라 순서).
    결과는 상위 `limit`건까지만 — 이 상한 안에서는 아무것도 소실되지 않는다
    (템플릿이 앞 12건은 바로, 나머지는 `<details>`로 전부 보여준다 — 종합지
    비증권 기사는 이 정렬로 자연히 `<details>` 쪽으로 밀린다).

    각 항목은 `{title, link, outlet, dup_count}` + (파싱 가능하면)
    `published_hhmm`. `outlet`은 `build_digest`와 달리 항상 키가 있다 —
    빈 문자열이어도("" ) 넣는다. 매체 배지가 "항상 표시"돼야 한다는 스펙
    §템플릿 요구를 만족하려면 호출부(템플릿)가 결측을 직접 판단할 수 있어야
    하기 때문이다.
    """
    all_items = [item for items in feeds.values() for item in items]
    if not all_items:
        return []

    reps = dedup_with_counts(all_items)
    reps.sort(
        key=lambda r: (
            not _is_market_relevant(r.get("title", ""), r.get("outlet")),
            -(r.get("dup_count") or 1),
            -_published_sort_key(r.get("published")),
        )
    )

    out: list[dict] = []
    for r in reps[:limit]:
        entry = {
            "title": r.get("title", ""),
            "link": r.get("link", ""),
            "outlet": r.get("outlet") or "",
            "dup_count": r.get("dup_count", 1),
        }
        published = _parse_published(r.get("published"))
        if published is not None:
            entry["published_hhmm"] = published.strftime("%H:%M")
        out.append(entry)
    return out


_TWO_PARA_LABEL_RE = re.compile(
    r"^[①②]?\s*(?:국내\s*시장\s*요약|미국발.*?영향)\s*[:：]?\s*", re.UNICODE,
)


def _split_two_paragraphs(text: str) -> list[str] | None:
    """narrator 응답을 정확히 두 문단으로 쪼갠다. 못 쪼개면 `None`(불가해 —
    사용자 스펙 "unparsable → None").

    ①/② 마커가 둘 다 있으면 그 경계로 자른다(모델이 프롬프트의 형식
    지시를 그대로 따른 가장 흔한 경우). 없으면 빈 줄 기준 문단 분리로
    폴백한다. 어느 쪽이든 정확히 2문단이 아니면 실패로 취급한다 —
    문단이 1개(구분 실패)거나 3개 이상(모델이 서두/군더더기를 붙임)이면
    "판단이 아니라 서술"이라는 계약을 지키기 위해 함부로 잘라 쓰지 않는다.
    """
    text = text.strip()
    if "①" in text and "②" in text:
        idx = text.index("②")
        parts = [text[:idx].strip(), text[idx:].strip()]
    else:
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    parts = [p for p in parts if p]
    if len(parts) != 2:
        return None
    return parts


def _strip_label(paragraph: str) -> str:
    return _TWO_PARA_LABEL_RE.sub("", paragraph).strip()


def summarize_digest(digest: dict, narrator) -> dict | None:
    """다이제스트 제목만으로 국내/미국발 2문단 요약을 만든다(스펙 §2, LLM
    허용 — 리포트 평면, ADR-0002 는 거래 핫패스만 금지).

    **판단이 아니라 서술이다** — 어떤 사건을 실을지는 `build_digest`의
    결정론(ECON_OUTLETS 필터 + dup_count 다매체 보도량 순)이 이미 정했다.
    여기 들어오는 건 확정된 제목 목록뿐이고, narrator 가 하는 일은 그것을
    자연스러운 문장으로 바꾸는 것뿐이다 — 종목을 추천하거나 사건을 새로
    고르지 않는다(`quant.adapters.narrate` 포트 계약과 같은 선).

    본문(article body)은 프롬프트에 넣지 않는다 — 제목만으로 "사실 나열,
    투자 권유 금지, 3문장 이내" 짧은 요약을 요청한다.

    `narrator.narrate` 가 실패(`None`)하거나 응답을 정확히 두 문단으로
    쪼갤 수 없으면 `None` — 호출부는 결정론 목록만으로 이미 완전하다
    (무LLM 폴백, `digest`가 그 자체로 유효한 리포트다).
    """
    domestic = digest.get("domestic") or []
    us_impact = digest.get("us_impact") or []
    if not domestic and not us_impact:
        return None

    lines = [
        "다음은 오늘 한국 증시 관련 뉴스 제목이다. 사실만 나열하고 투자",
        "권유·추천은 하지 말 것. 각 문단은 3문장 이내로 짧게 쓸 것.",
        "미국발 제목은 원문이 영어일 수 있다 — 그래도 반드시 한국어로만",
        "답하라(영어·중국어 등 다른 언어를 섞지 마라).",
        "",
        f"국내 {len(domestic)}건:",
    ]
    lines += [f"- {item.get('title', '')}" for item in domestic]
    lines += ["", f"미국발 {len(us_impact)}건:"]
    lines += [f"- {item.get('title', '')}" for item in us_impact]
    lines += [
        "",
        "다음 형식으로 정확히 두 문단만 답하라(다른 텍스트 없이):",
        "① 국내 시장 요약: <문단>",
        "",
        "② 미국발 재료가 한국 증시에 주는 영향: <문단>",
    ]
    prompt = "\n".join(lines)

    text = narrator.narrate(prompt)
    if text is None:
        return None

    parts = _split_two_paragraphs(text)
    if parts is None:
        return None
    domestic_prose = _strip_label(parts[0])
    us_prose = _strip_label(parts[1])
    if not domestic_prose or not us_prose:
        return None
    return {"domestic_prose": domestic_prose, "us_prose": us_prose}
