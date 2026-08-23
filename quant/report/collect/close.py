"""마감 포지션 리포트(서브프로젝트 R) 전용 뷰 조립 — 장중 신규 뉴스/당일
수급/랭킹.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

from quant.analyze.bullish_markers import classify_titles

# 랭킹 스냅샷(②)에서 재사용할 보드. "하락률"·"토스 사용자 거래대금"은 마감
# 포지션 판단(롱 온리)과 무관해 뺀다 — 아침 리포트의 "랭킹 스냅샷" 섹션과
# 달리 여긴 두 보드만 축약해서 보여준다(스펙 §내용 2 문구 그대로).
CLOSE_RANKING_BOARDS = ("거래대금", "상승률")


def _build_close_news_view(news_flow: list[dict]) -> list[dict]:
    """장중 신규 호재(①) — `_build_news_flow`가 이미 만든 사건 단위 뉴스
    목록(다매체 중복만 제거, 뉴스 창은 `news_since`로 이미 걸러짐)에 제목
    단위로 호재 마커/악재 거부권을 얹어 재정렬한다.

    `bullish_markers.classify_titles`는 원래 종목 단위(그 종목의 오늘치
    제목 여러 개)를 보고 호재 유형을 찾는 함수인데, 여기서는 사건(제목)
    하나짜리 리스트를 넘겨 "이 제목 자체가 호재/악재 마커를 담고 있는가"만
    독립적으로 판정한다 — 종목 태깅이 없는 시장 전체 뉴스 흐름이라
    `cont[symbol]["titles"]` 처럼 종목별로 묶을 입력이 없다.

    정렬: 호재 티어 내림차순 → 다매체(dup_count) 내림차순 → 기존 순서
    보존(안정 정렬 — `_build_news_flow`가 이미 정한 "증권 관련·최신" 우선
    순위가 동점 구간의 tie-break 로 남는다)."""
    out = []
    for item in news_flow:
        bull = classify_titles([item.get("title", "")])
        out.append({
            **item,
            "bullish_types": bull["bullish_types"],
            "bullish_tier": bull["tier"],
            "bearish": bull["bearish"],
        })
    out.sort(key=lambda it: (-it["bullish_tier"], -(it.get("dup_count") or 1)))
    return out


def _build_close_flow_view(payload: dict) -> dict:
    """당일 수급 — 시장 잠정 외국인/기관/개인(②). `machine_payload`가 이미
    계산한 시장 피처(`kospi_flow` 유래)를 그대로 재사용한다 — 종목별 당일
    잠정 수급 크롤은 신설하지 않는다(비목표, 스펙 §비목표 2번째 항). 결측은
    `None` 그대로(0으로 위장하지 않는다) — 템플릿이 "결측"으로 정직하게
    표시한다. "잠정"이라는 라벨은 템플릿이 고정 문구로 명시한다(장중 시점의
    같은 소스 값은 확정치가 아니다)."""
    features = payload.get("features") or {}
    return {
        "foreign_net": features.get("foreign_net_100m_krw"),
        "institution_net": features.get("institution_net_100m_krw"),
        "individual_net": features.get("individual_net_100m_krw"),
    }


def _build_close_ranking_view(snap, top: int = 8) -> dict[str, list[dict]]:
    """당일 랭킹 보드 — 거래대금/상승률(②). 이미 수집된 `toss_rankings`
    스냅샷에서 뽑을 뿐 새 크롤은 없다. `_derive`가 이미 종목명을 붙여
    두므로(그 함수의 "랭킹 종목명 매칭" 단계) 여기선 재사용만 한다 —
    이 함수가 `_derive` 이후에 호출돼야 이름이 채워진다(호출부 계약)."""
    ranking = snap.results.get("toss_rankings")
    if ranking is None or not ranking.ok or not ranking.data:
        return {}
    boards = ranking.data.get("boards") or {}
    out: dict[str, list[dict]] = {}
    for name in CLOSE_RANKING_BOARDS:
        items = boards.get(name) or []
        if items:
            out[name] = items[:top]
    return out
