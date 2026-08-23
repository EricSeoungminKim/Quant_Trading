"""테마 기반 종목 탐색 (스펙 접근 F). **순수 모듈** — 네트워크·DB·파일을 만지지
않는다. 전부 인자로 받는다(themes 는 `naver_theme.fetch_themes` 산출, title_matches
는 `relations.match_codes` 산출, market_leaders 는 `naver_quant.fetch_quant_top`
산출).

핵심 계약: **한 테마가 소스를 독식하지 않는다.** 뉴스가 아무리 반도체 편중이어도
`select_sources` 는 테마 수 ≤ max_themes, 테마당 종목 수 ≤ per_theme 를 지킨다
(사용자 요구: "막무가내로 삼성·하이닉스만 하는 게 아니다").

`market_leaders` 의 `code` 는 6자리 숫자만이 아니다 — ETN 류 영숫자 혼합 코드
(예: `0183J0`, 2026-08-15 실측 거래상위 100종목 중 19종목)가 섞인다. 이 모듈은
코드 형식을 가정하지 않고 그대로 통과시킨다 — 필터링은 하류(Task 3) 결정이다.
"""
from __future__ import annotations


def _sort_by_value_traded(symbols: list[dict]) -> list[dict]:
    """거래대금 내림차순. 값이 없으면 뒤로 보내되 제외하지 않는다."""
    return sorted(
        symbols,
        key=lambda s: (s.get("value_traded") is None, -(s.get("value_traded") or 0)),
    )


def theme_index(themes: dict) -> dict[str, list[str]]:
    """종목코드 → 소속 테마 번호들."""
    index: dict[str, list[str]] = {}
    for no, theme in themes.items():
        for sym in theme.get("symbols", []):
            index.setdefault(sym["code"], []).append(no)
    return index


def hot_themes(themes: dict, title_matches: list[set[str]], limit: int) -> list[str]:
    """그날 기사에서 매칭된 종목들의 소속 테마를 집계해 상위 N.

    **1차 키는 특이도(매칭 종목 수 ÷ 테마 전체 종목 수), 절대 매칭 종목 수가
    아니다** (2026-08-16 EC2 e2e 실측 결함 수정). 절대 수로만 순위를 매기던
    첫 구현은 실제 `deepdive` 실행에서 결과가 "코리아 밸류업 지수"·
    "밸류업(제고계획)" 같은 대형 **우산 지수** 테마 2개로만 쏠렸다 — 그 편입
    사유는 수혜 근거가 아니라 회사 소개문("삼성그룹 계열의 세계적 전자
    업체...")이라 가이드 가치가 0이었다. 원인은 뉴스 대형주(삼성·하이닉스·
    현대차)를 많이 품은 대형 지수 테마가 절대 매칭 수로는 구조적으로 항상
    이기는 것.

    **크기 상한이나 블랙리스트는 쓰지 않는다.** EC2 `themes.json`(266개) 실측
    분포는 테마 크기 중앙값 17·p75 29·p90 50·최대 146이고, 큰 테마 중에도
    우산(밸류업지수 99·지주사 126)뿐 아니라 **진짜 섹터**(반도체 장비 93·
    2차전지 142·자동차부품 146)가 섞여 있다 — 크기로 자르면 진짜 섹터까지
    함께 잘린다. 대신 "테마 전체 종목 대비 매칭 비율" 인 특이도로 재면 우산
    테마는 분모가 커서 자연히 밀리고, 크기가 같아도 뉴스가 좁게 집중된
    테마가 이긴다.

    **자격 요건: 매칭 종목 수 ≥ 2.** 1개 매칭만으로 초소형 테마가 특이도
    1.0을 찍어 최상위를 차지하는 잡음을 막는다.

    **동률(특이도가 같음)은 매칭 종목 수 내림차순 → 언급 제목 수 내림차순 →
    테마 번호 오름차순으로 결정론적으로 해소한다** (2026-08-16 이전 리뷰
    수정 유지). `title_matches` 의 각 원소가 `set`이라 `for code in codes` 를
    그대로 순회하면 해시 랜덤화에 좌우돼 완전 동률 테마의 최종 순위가
    실행마다 달라질 수 있었다(Phase 8 하네스의 재현성 요구 위반) —
    `sorted(codes)` 로 순회 자체도 결정론으로 고정한다.
    """
    index = theme_index(themes)
    symbol_hits: dict[str, set[str]] = {}
    title_hits: dict[str, int] = {}
    for codes in title_matches:
        touched: set[str] = set()
        for code in sorted(codes):
            for no in index.get(code, []):
                symbol_hits.setdefault(no, set()).add(code)
                touched.add(no)
        for no in sorted(touched):
            title_hits[no] = title_hits.get(no, 0) + 1

    eligible = [no for no in symbol_hits if len(symbol_hits[no]) >= 2]

    def specificity(no: str) -> float:
        total = len(themes.get(no, {}).get("symbols", [])) or 1
        return len(symbol_hits[no]) / total

    ranked = sorted(
        eligible,
        key=lambda no: (
            -specificity(no), -len(symbol_hits[no]), -title_hits.get(no, 0), no,
        ),
    )
    return ranked[:limit]


def leaders(themes: dict, theme_no: str, top: int) -> list[dict]:
    """거래대금 상위 종목(대장주)."""
    symbols = themes.get(theme_no, {}).get("symbols", [])
    return _sort_by_value_traded(symbols)[:top]


def beneficiaries(themes: dict, theme_no: str, src_code: str, limit: int) -> list[dict]:
    """같은 테마의 다른 종목 + 편입사유(`reason`). `via_theme` 를 함께 싣는다.

    Task 1 에서 편입사유 없는 종목은 이미 걸러지지만, 빈 이유로 관계를 만들지
    않는다는 계약을 여기서도 방어적으로 한 번 더 지킨다.
    """
    theme = themes.get(theme_no)
    if not theme:
        return []
    candidates = [
        s for s in theme.get("symbols", [])
        if s["code"] != src_code and s.get("reason")
    ]
    out = []
    for s in _sort_by_value_traded(candidates)[:limit]:
        item = dict(s)
        item["via_theme"] = theme["name"]
        out.append(item)
    return out


def select_sources(
    themes: dict,
    title_matches: list[set[str]],
    max_themes: int,
    per_theme: int,
    market_leaders: list[dict] | None = None,
    leader_slots: int = 2,
) -> list[dict]:
    """최종 소스 목록. **한 테마가 전체를 먹지 못한다** — 테마 수 ≤ max_themes,
    테마당 종목 수 ≤ per_theme.

    `market_leaders` 를 주면(Task 1b 산출, 선택 인자) 그날 뉴스에도 매칭된
    (= `title_matches` 에 등장한) 거래상위 종목을 거래대금 순으로 최대
    `leader_slots` 개 보조로 합류시킨다. **전체 상한은
    `max_themes * per_theme + (leader_slots if market_leaders else 0)`.**
    없으면 테마만으로 동작(하위호환).

    **`leader_slots` 는 거래상위 전용 예약석이다** (2026-08-16 리뷰 판정으로
    계약 변경). 이전에는 `market_leaders` 를 테마 소스가 다 채우고 남는
    자리에만 끼워 넣었는데, 테마 소스가 `max_themes * per_theme` 를 거의
    항상 꽉 채우는 구조라(테마 하나만 있어도 `leaders()` 가 상한까지
    채운다) 실전에서 거래상위 보조 신호가 구조적으로 못 들어오는 문제가
    있었다. 별도 예약석으로 분리해 테마가 캡을 꽉 채워도 거래상위 신호가
    죽지 않게 한다.
    """
    hot = hot_themes(themes, title_matches, max_themes)
    sources: list[dict] = []
    seen: set[str] = set()
    for no in hot:
        for sym in leaders(themes, no, per_theme):
            if sym["code"] in seen:
                continue
            seen.add(sym["code"])
            item = dict(sym)
            item["via_theme"] = themes[no]["name"]
            sources.append(item)

    if market_leaders:
        matched_codes: set[str] = set().union(*title_matches) if title_matches else set()
        candidates = [
            m for m in market_leaders
            if m["code"] in matched_codes and m["code"] not in seen
        ]
        for m in _sort_by_value_traded(candidates)[:leader_slots]:
            seen.add(m["code"])
            item = dict(m)
            item["via_theme"] = None
            sources.append(item)

    return sources
