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


# ── 직전 세션 급락 거부권 — ③(ranking_bullish) 전용 (2026-09-07) ───────────
#
# 최초 가설(제목에 악재가 없어도 직전 세션 -3% 급락이면 NEWS/STREAK/RANK
# 전부 거부)은 아카이브 538개 심볼의 실제 가격(yfinance)으로 검증한 결과
# 기각됐다 — 거부 대상이었던 139건 중 105/87건을 D+0/D+1 로 가격 매칭하니
# 그 외 승격 후보보다 **성과가 더 좋았다**(과매도 반등):
#   D+0 시가→종가: 거부 대상 평균 +107.5bp·적중 54.3%(n=105) vs
#                  그 외 평균 -17.3bp·적중 47.4%(n=932)
#   D+1 종가→종가: 거부 대상 평균 +88.6bp·적중 58.6%(n=87) vs
#                  그 외 평균 -33.6bp·적중 42.4%(n=713, 95%CI 비중첩)
# 그래서 NEWS/STREAK 는 급락 여부와 무관하게 그대로 승격한다. 다만
# `ranking_bullish=True`(랭킹 스냅샷 시점엔 상승)인데 이 리포트의 최종
# change_pct 가 급락인 사례(n=14, 예: SK스퀘어 402340 2026-08-20 -11.5%)는
# 표본이 작아 성과 판단은 못하지만(CI [45.4%,88.3%]) **"매수세가 실제로
# 몰린다"는 ③ 고유의 주장 자체가 같은 리포트 안에서 반박된 라벨 정합성
# 문제**라 그 태그만은 계속 막는다.

def test_price_crash_does_not_veto_news_or_streak_candidacy():
    """제목에 악재 표지가 없으면 직전 세션 급락도 NEWS/STREAK 후보 자격을
    막지 않는다(2026-09-07 실측 반영 — 급락 뒤 반등이 오히려 우세했다)."""
    c = _cont(REAL_NEUTRAL[:2], streak_days=3)
    quote = {"change_pct": -9.7}
    assert is_candidate(c) is True
    assert is_candidate(c, quote) is True
    line = candidates_line({"005930": c}, {}, sym_quotes={"005930": quote})
    assert "NEWS" in line


def test_price_crash_vetoes_ranking_bullish_only():
    """랭킹 편입 시점엔 상승이었어도(ranking_bullish=True), 이 리포트가 실제로
    보여주는 change_pct 가 급락이면 '매수세가 몰린다'는 ③의 주장이 깨진다 —
    ③ 단독 근거일 때만 후보 자격 자체가 막힌다(실측: SK스퀘어 -11.5%)."""
    c = _cont([], ranking_bullish=True)
    quote = {"change_pct": -11.5}
    assert is_candidate(c, quote) is False
    line = candidates_line({"402340": c}, {}, sym_quotes={"402340": quote})
    assert "402340" not in line


def test_price_crash_drops_only_the_rank_tag_when_news_also_qualifies():
    """③(RANK) 태그만 라벨 정합성 문제로 빠진다 — 뉴스 근거가 따로 있으면
    NEWS/STREAK 경로로는 그대로 후보가 된다(태그만 RANK 가 빠진다)."""
    c = _cont(REAL_NEUTRAL[:2], streak_days=3, ranking_bullish=True)
    quote = {"change_pct": -9.7}
    line = candidates_line({"005930": c}, {}, sym_quotes={"005930": quote})
    assert "005930" in line
    assert "RANK" not in line
    assert "NEWS" in line and "STREAK" in line


def test_mild_decline_does_not_veto():
    """-3%에 못 미치는 하락은 급락 거부권 대상이 아니다(경계값)."""
    c = _cont([], ranking_bullish=True)
    assert is_candidate(c, {"change_pct": -2.9}) is True
    assert is_candidate(c, {"change_pct": None}) is True
    assert is_candidate(c, None) is True


def test_bearish_markers_reports_the_price_crash():
    """payload 의 bearish_markers 필드에 급락 표지가 사람이 읽을 수 있게 남는다
    (정보 표시용 — NEWS/STREAK 후보 자격 판정에는 쓰이지 않는다, 위 참고)."""
    assert bearish_markers(_cont([]), {"change_pct": -9.747}) == ["직전 세션 급락 -9.7%"]
    # 제목 표지와 함께 있으면 둘 다 남는다(순서: 제목 → 가격).
    assert bearish_markers(_cont(["이마트 적자전환"]), {"change_pct": -5.0}) == [
        "적자", "직전 세션 급락 -5.0%",
    ]
