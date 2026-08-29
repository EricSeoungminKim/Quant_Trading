"""뉴스 방향 거부권 — 감성 분석이 아니라 **명백한 악재 하나에 대한 veto**.

회귀(2026-08-13 실측):
- 펄어비스 263750 — `'어닝쇼크' 펄어비스에 日 노무라 "목표가 59% 하향"` 기사가
  수집돼 있었는데 EVENT 태그가 붙어 개장 매수, -13,580원.
- 대신증권 003540 — 호재 5건 + 목표주가 하향 2건이 섞여 있었는데 매수, -14,904원.

원인: `render.candidates_line` 이 `today_articles > 0` 만 보고 NEWS 태그를 줬고,
그게 엔진 어휘의 EVENT 로 번역돼 `news_momentum` 이 개장 직후 샀다. 원래는
08:40 Claude 세션이 방향을 판정하는 전제였는데, `own_brief.sh` 로 갈아치우며
**판정 주체만 사라지고 태그 조건은 남았다.**
"""
from __future__ import annotations

import pytest

from quant.analyze.news_direction import bearish_marker, scan
from quant.analyze.render import bearish_markers, candidates_line, is_candidate

# 전부 우리 저장소에 실제로 들어온 제목이다 (2026-08-13 KR).
REAL_BEARISH = [
    # 표지가 둘(어닝쇼크 + 목표가 하향) 다 있다 — `_BEARISH` 순서상 앞의 것을 낸다.
    # 어느 쪽이든 거부 결과는 같지만, 반환값은 결정론적이어야 리포트가 흔들리지 않는다.
    ("'어닝쇼크' 펄어비스에 日 노무라 \"목표가 59% 하향\"", "목표가 하향"),
    ("대신증권, 유가증권 평가익 업고 2분기 '어닝 서프라이즈'… 목표주가는 4만원으로 하향 - SK증권", "목표가 하향"),
    ("대신증권, 어닝서프라이즈 달성했는데…목표주가 '하향' 이유는?", "목표가 하향"),
    ("키움증권 \"카카오게임즈, 신작 창의성 확인돼야…목표가 하향\"", "목표가 하향"),
    ("이마트, 2분기 영업손실 430억원…전년비 적자전환", "적자"),
    ("대동금속, 90억원 규모 유상증자 실시", "유상증자"),
    ("덴티스, 80억 원 규모 전환사채 발행", "전환사채"),
]

REAL_NEUTRAL = [
    "삼성전자, 인도에 플랙트그룹 신규 생산라인 준공…AI 데이터센터 공략 강화",
    "美 AI 반도체주 훈풍에…SK하이닉스 7%·삼성전자 5%↑[핫종목]",
    "신세계 오프프라이스 매장 ‘팩토리스토어’ 첫 해외 진출",
    "거래대금이 키운 '어닝 서프라이즈'...대신증권 상반기 순이익 165% 급증",
    "출시 3개월 만에 6만명 몰렸다…고객 씀씀이 10% 늘린 신세계",
]


@pytest.mark.parametrize("title,expected", REAL_BEARISH)
def test_real_bearish_headlines_are_detected(title, expected):
    assert bearish_marker(title) == expected


@pytest.mark.parametrize("title", REAL_NEUTRAL)
def test_real_neutral_headlines_are_not_flagged(title):
    """오탐이 비싸다 — 호재를 악재로 읽으면 살 수 있는 종목을 영영 못 산다."""
    assert bearish_marker(title) is None


def test_positive_earnings_wording_alone_does_not_trigger():
    """'어닝 서프라이즈'는 호재다. '어닝 쇼크'만 잡아야 한다."""
    assert bearish_marker("대신증권 2분기 '어닝 서프라이즈'") is None
    assert bearish_marker("펄어비스 어닝 쇼크") == "어닝쇼크"


def test_empty_and_none_titles_are_safe():
    assert bearish_marker("") is None
    assert scan([]) == [] and scan(None) == []


def test_scan_deduplicates_but_keeps_order():
    titles = ["A 목표가 하향", "B 어닝 쇼크", "C 목표주가 하향"]
    assert scan(titles) == ["목표가 하향", "어닝쇼크"]


# ── 태그 파이프라인 결합 ──────────────────────────────────────────────────

def _cont(titles: list[str], **kw):
    base = {"today_articles": len(titles), "streak_days": 0, "is_new": False,
            "in_ranking": False, "ranking_bullish": False,
            "titles": [{"title": t, "link": "", "feed": ""} for t in titles]}
    return {**base, **kw}


def test_bearish_symbol_loses_the_news_tag():
    """NEWS → 엔진 어휘 EVENT → news_momentum 개장 매수. 여기서 끊는다."""
    c = _cont(["'어닝쇼크' 펄어비스에 日 노무라 \"목표가 59% 하향\"", "펄어비스 신작 기대"])
    assert bearish_markers(c) == ["목표가 하향"]
    assert "NEWS" not in candidates_line({"263750": c}, {})


def test_clean_symbol_keeps_the_news_tag():
    c = _cont(REAL_NEUTRAL[:2])
    assert bearish_markers(c) == []
    assert "NEWS" in candidates_line({"005930": c}, {})


def test_bearish_news_does_not_grant_candidacy():
    """뉴스 근거로는 후보가 못 된다 — 기사 수가 문턱을 넘어도."""
    assert is_candidate(_cont(["A 목표가 하향", "B 목표주가 하향", "C 유상증자"])) is False


def test_ranking_evidence_survives_bearish_news():
    """매수세가 실제로 몰리는 것은 별개 사실이다 — 그쪽은 ranking_bullish 가 본다."""
    c = _cont(["목표가 하향"], ranking_bullish=True)
    assert is_candidate(c) is True
    line = candidates_line({"005930": c}, {})
    assert "RANK" in line and "NEWS" not in line


def test_payload_records_why_it_was_vetoed():
    """'점수가 낮아서'는 사람이 검증할 수 없다 — 표지 이름을 남긴다."""
    assert bearish_markers(_cont(["이마트 적자전환"])) == ["적자"]
    assert bearish_markers(_cont(REAL_NEUTRAL)) == []
