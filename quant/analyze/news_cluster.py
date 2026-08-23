"""제목 shingle 클러스터 — 같은 사건을 보도한 다매체 기사를 하나로 묶는다.

순수 함수만 담는다(stdlib만 사용, 신규 의존성 없음) — 문자 2-gram shingle
집합의 자카드 유사도로 "같은 사건, 다른 매체"를 판정한다. MinHash 같은 근사
알고리즘은 원장 규모가 실제로 문제가 될 때 넣는다(스펙
`docs/superpowers/specs/2026-08-16-news-scale-design.md`).

## 임계값 0.6을 지키는 이유 (2026-08-17 실측, KR 2026-08-15 942건)

"중복이 안 걸리게 확실히" 요청에 따라 임계값을 낮출 수 있는지 실측했다.
자카드 [0.35, 0.6) 근접-미스 구간에 213쌍이 있고 상당수가 진짜 같은
사건(예: "현대해상 상반기 순익 36%…" 0.590, "최태원…재산분할 불복" 0.583)
이라 threshold 를 낮추면 잡을 수 있어 보였다. 하지만 다른-사건 통제쌍을
같은 구간에서 대조한 결과 진짜 오탐이 나왔다: "DB증권, 상반기 순이익
708억원…전년比 49.4%↑" vs "iM증권, 상반기 순이익 426억원…전년比 21.2%
감소" — 자카드 0.457, 회사도 실적 방향(증가/감소)도 다른데 실적발표
제목 템플릿이 같아서 점수가 높다. "출발증시 1부/2부"(0.500, 다른 회차)도
같은 함정이다. 단어 2-gram 자카드를 대안으로 시도했으나 이 오탐쌍(0.125)이
진짜 중복쌍(예: 롯데카드 실적 기사, 동일 0.125)과 구분되지 않아 채택하지
않았다 — 분리력이 없다. 그래서 threshold=0.6 을 유지하고, 대신 아래
`dedup_with_counts`의 완전중복 전처리로 자카드가 못 잡는 케이스 중
**오탐 위험이 없는 것만** 별도로 잡는다.
"""
from __future__ import annotations

import re
from datetime import datetime

from quant.collect.collector import normalize_link

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


def _normalize(title: str) -> str:
    """소문자화 + 구두점 제거 + 연속 공백 축약.

    매체마다 같은 제목을 쉼표/말줄임표 등 다른 구두점으로 쓰는 게 실측
    사례(예: "SK텔레콤, ... 급등" vs "SK텔레콤 ... 급등…...")에서 자카드
    유사도를 threshold 밑으로 깎았다 — 구두점은 사건 동일성과 무관한 잡음이라
    shingle 이전에 지운다. 형태소 분석(조사/어미 정규화)까지는 하지 않는다
    — 2-gram shingle 은 부분 문자열 중복에 이미 관대해서 그 이상은 과설계다.
    """
    s = _PUNCT_RE.sub("", title.lower())
    return _WS_RE.sub(" ", s).strip()


def _shingles(title: str) -> set[str]:
    """문자 2-gram 집합. 정규화 후 2자 미만이면 문자열 전체를 한 shingle로
    취급한다(빈 집합끼리는 항상 자카드 1.0 이 되어 무관한 빈 제목들이 서로
    뭉치는 걸 막기 위함)."""
    s = _normalize(title)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cluster_titles(items: list[dict], threshold: float = 0.6) -> list[list[int]]:
    """제목 자카드 유사도 ≥ threshold 인 항목을 같은 클러스터로 묶는다.

    Greedy, 입력 순서 결정론: 앞에서부터 항목을 보며 이미 만들어진 클러스터
    중 그 클러스터의 **첫 항목**(대표 후보)과의 유사도가 threshold 이상인
    첫 번째 클러스터에 합류한다. 없으면 새 클러스터를 연다. 반환은 원본
    인덱스 그룹의 리스트이고, 그룹 순서 = 클러스터가 처음 생성된 순서.
    """
    reps: list[set[str]] = []
    clusters: list[list[int]] = []
    for idx, item in enumerate(items):
        shingles = _shingles(item.get("title", ""))
        for c_idx, rep_shingles in enumerate(reps):
            if _jaccard(shingles, rep_shingles) >= threshold:
                clusters[c_idx].append(idx)
                break
        else:
            clusters.append([idx])
            reps.append(shingles)
    return clusters


def _parse_published(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def dedup_with_counts(items: list[dict], threshold: float = 0.6) -> list[dict]:
    """`cluster_titles` 로 묶고, 완전중복 전처리로 한 번 더 합친 뒤, 클러스터별
    대표 1건만 남겨 `dup_count` 를 붙인다.

    **완전중복 전처리** — 정규화 링크가 같거나(`normalize_link`) 정규화
    제목이 완전히 같으면 자카드 점수와 무관하게 무조건 합친다. 값(링크·
    제목)이 비어 있으면 그 축은 비교하지 않는다(빈 문자열끼리 우연히 같다고
    합치는 것을 막는다). 실측(2026-08-15 KR 942건)에서 같은 기사가 원매체
    직링크와 구글뉴스 RSS 프록시 링크로 각각 수집돼 링크는 다르지만 제목이
    완전히 같은 그룹이 25개 나왔다 — 이 케이스는 정규화 제목이 같으면
    shingle 집합도 같아 자카드 1.0 으로 이미 잡히지만, 링크는 그대로인데
    제목만 갱신되는 경우(예: 속보 제목에 나중에 "(종합)" 이 아니라 완전히
    다른 요약이 붙는 경우)까지 방어하려면 링크 동일성도 자카드와 별개로
    봐야 한다 — 링크는 오탐이 구조적으로 불가능한 신호라 임계값 걱정 없이
    무조건 합친다.

    대표 선정: `published` 기준 최신 1건. 클러스터 안에 `published` 가
    없거나(누락) 파싱 안 되는 항목이 하나라도 있으면 "최신"을 신뢰할 수
    없으므로 클러스터의 **첫 항목**(입력 순서상 처음)으로 폴백한다 — 조용히
    틀린 최신값을 고르는 것보다 안전하다. 대표 dict 는 원본을 얕은 복사해
    `dup_count`(합쳐진 항목 수)만 추가한다. 반환 순서는 각 그룹의 최소
    입력 인덱스 오름차순을 따른다(완전중복 전처리가 없을 때는
    `cluster_titles` 의 그룹 순서와 동일).
    """
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for group in cluster_titles(items, threshold):
        for idx in group[1:]:
            union(group[0], idx)

    by_link: dict[str, int] = {}
    by_title: dict[str, int] = {}
    for i, item in enumerate(items):
        raw_link = item.get("link") or ""
        if raw_link:
            link = normalize_link(raw_link)
            if link in by_link:
                union(by_link[link], i)
            else:
                by_link[link] = i
        title = _normalize(item.get("title", ""))
        if title:
            if title in by_title:
                union(by_title[title], i)
            else:
                by_title[title] = i

    merged: dict[int, list[int]] = {}
    for i in range(n):
        merged.setdefault(find(i), []).append(i)
    groups = [sorted(members) for _, members in sorted(merged.items(), key=lambda kv: kv[0])]

    out: list[dict] = []
    for group in groups:
        parsed = [_parse_published(items[i].get("published")) for i in group]
        if all(dt is not None for dt in parsed):
            best_idx = group[parsed.index(max(parsed))]
        else:
            best_idx = group[0]
        out.append({**items[best_idx], "dup_count": len(group)})
    return out
