"""`quant.analyze.bullish_markers` — 호재 마커 사전 + 분류기 (서브프로젝트 P).

네트워크 없음 — 순수 함수만 테스트한다.
"""
from __future__ import annotations

from quant.analyze.bullish_markers import (
    BULLISH_TIER_UNVALIDATED,
    BULLISH_TIER_VALIDATED,
    BULLISH_TYPE_TIERS,
    NEWS_AXIS_MAX,
    classify_titles,
    classify_titles_dated,
    news_axis_v2,
)


# ------------------------------------------------------------------ classify_titles — 유형별

def test_classify_titles_finds_order_contract():
    result = classify_titles(["A사, 대규모 수주 공시"])
    assert "수주/공급계약" in result["bullish_types"]
    assert result["tier"] == BULLISH_TIER_VALIDATED
    assert result["bearish"] is False


def test_classify_titles_treasury_stock_is_validated_tier():
    result = classify_titles(["B사, 자사주 매입 결정"])
    assert result["bullish_types"] == ["자사주"]
    assert result["tier"] == BULLISH_TIER_VALIDATED


def test_classify_titles_earnings_turnaround_is_unvalidated_tier():
    result = classify_titles(["C사, 흑자전환 성공"])
    assert result["bullish_types"] == ["흑자전환/최대실적"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_classify_titles_fda_is_unvalidated_tier():
    result = classify_titles(["D사, FDA 품목허가 획득"])
    assert "FDA/품목허가/임상성공" in result["bullish_types"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_classify_titles_new_product_type():
    assert classify_titles(["E사, 신제품 출시"])["bullish_types"] == ["신제품/양산"]


def test_classify_titles_patent_type():
    assert classify_titles(["F사, 특허 취득"])["bullish_types"] == ["특허"]


def test_classify_titles_rights_issue_type():
    result = classify_titles(["G사, 무상증자 결정"])
    assert result["bullish_types"] == ["무상증자"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_classify_titles_mou_type():
    assert classify_titles(["H사, MOU 체결"])["bullish_types"] == ["MOU/협력"]


def test_classify_titles_no_match_is_empty_tier_zero():
    result = classify_titles(["오늘의 증시 브리핑"])
    assert result["bullish_types"] == []
    assert result["tier"] == 0


def test_classify_titles_empty_list():
    result = classify_titles([])
    assert result == {"bullish_types": [], "tier": 0, "bearish": False}


def test_classify_titles_dedupes_same_type_across_titles():
    result = classify_titles(["A사 수주 공시", "A사 대규모 공급계약 체결"])
    assert result["bullish_types"] == ["수주/공급계약"]


def test_classify_titles_multiple_distinct_types():
    result = classify_titles(["A사 수주 공시", "A사 자사주 매입 결정"])
    assert result["bullish_types"] == ["수주/공급계약", "자사주"]
    assert result["tier"] == BULLISH_TIER_VALIDATED  # max


def test_all_types_have_a_tier():
    """`BULLISH_MARKERS`가 가리키는 모든 유형이 `BULLISH_TYPE_TIERS`에 있어야
    `classify_titles`가 KeyError 없이 돈다."""
    from quant.analyze.bullish_markers import BULLISH_MARKERS

    for btype in BULLISH_MARKERS.values():
        assert btype in BULLISH_TYPE_TIERS


# ------------------------------------------------------------------ classify_titles — 악재 거부권 재사용

def test_classify_titles_bearish_veto_detected():
    result = classify_titles(["A사, 목표가 하향 조정"])
    assert result["bearish"] is True


def test_classify_titles_bullish_and_bearish_can_coexist():
    """호재 마커가 있어도 악재 표지가 같이 있으면 bearish=True — news_axis_v2가
    감점을 매기도록 두 정보를 모두 정직하게 돌려준다."""
    result = classify_titles(["A사 수주 공시", "A사, 목표가 하향 조정 - SK증권"])
    assert result["bullish_types"] == ["수주/공급계약"]
    assert result["bearish"] is True


# ------------------------------------------------------------------ news_axis_v2

def _bull(**overrides) -> dict:
    base = {"bullish_types": [], "tier": 0, "bearish": False}
    base.update(overrides)
    return base


def test_news_axis_v2_all_off_is_zero():
    pts, evidence = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=0, bull=_bull())
    assert pts == 0
    assert "호재 마커 없음" in evidence


def test_news_axis_v2_top_tier_points():
    pts, evidence = news_axis_v2(
        today_articles=0, news_z=0.0, max_dup_count=0,
        bull=_bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED),
    )
    assert pts == 15
    assert "호재:" in evidence


def test_news_axis_v2_low_tier_points():
    pts, _ = news_axis_v2(
        today_articles=0, news_z=0.0, max_dup_count=0,
        bull=_bull(bullish_types=["특허"], tier=BULLISH_TIER_UNVALIDATED),
    )
    assert pts == 8


def test_news_axis_v2_dup_high_and_medium():
    pts_high, _ = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=3, bull=_bull())
    assert pts_high == 10
    pts_med, _ = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=2, bull=_bull())
    assert pts_med == 5
    pts_single, _ = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=1, bull=_bull())
    assert pts_single == 0


def test_news_axis_v2_news_z_threshold():
    pts, _ = news_axis_v2(today_articles=0, news_z=2.5, max_dup_count=0, bull=_bull())
    assert pts == 8
    pts_below, _ = news_axis_v2(today_articles=0, news_z=1.9, max_dup_count=0, bull=_bull())
    assert pts_below == 0


def test_news_axis_v2_news_z_none_does_not_fake_zero():
    pts, evidence = news_axis_v2(today_articles=0, news_z=None, max_dup_count=0, bull=_bull())
    assert pts == 0
    assert "표본 부족" in evidence


def test_news_axis_v2_articles_threshold():
    pts, _ = news_axis_v2(today_articles=3, news_z=0.0, max_dup_count=0, bull=_bull())
    assert pts == 7
    pts_below, _ = news_axis_v2(today_articles=2, news_z=0.0, max_dup_count=0, bull=_bull())
    assert pts_below == 0


def test_news_axis_v2_max_score_is_40():
    pts, _ = news_axis_v2(
        today_articles=5, news_z=3.0, max_dup_count=5,
        bull=_bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED),
    )
    assert pts == NEWS_AXIS_MAX == 40


def test_news_axis_v2_bearish_veto_cuts_60pct():
    bull = _bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED)
    pts_clean, _ = news_axis_v2(today_articles=5, news_z=3.0, max_dup_count=5, bull=bull)
    assert pts_clean == 40

    bull_bearish = _bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED, bearish=True)
    pts_vetoed, evidence = news_axis_v2(today_articles=5, news_z=3.0, max_dup_count=5, bull=bull_bearish)
    assert pts_vetoed == round(40 * 0.4) == 16
    assert "악재 마커 존재" in evidence


def test_news_axis_v2_bearish_veto_on_zero_stays_zero():
    pts, _ = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=0, bull=_bull(bearish=True))
    assert pts == 0


# ------------------------------------------------------------------ classify_titles — 2026-08-18 추가 마커
# SK하이닉스 15점 사건 재점검(로컬 실측: "삼성전자·SK하이닉스, 호실적에 상반기
# 현금 117조 증가"가 옛 사전(흑자전환/최대실적/역대 최대)에 하나도 안 걸렸다)
# 으로 추가된 키워드 4종. 사전 매칭·티어 배정만 검증한다 — 점수 산식은
# 안 바뀌었다(news_axis_v2 회귀는 위 섹션들이 이미 고정한다).

def test_classify_titles_good_earnings_headline_matches_added_keyword():
    """실측 사건의 원문 헤드라인 — 옛 사전으로는 tier=0(호재 없음)이었다."""
    result = classify_titles(["삼성전자·SK하이닉스, 호실적에 상반기 현금 117조 증가"])
    assert result["bullish_types"] == ["흑자전환/최대실적"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_classify_titles_earnings_improvement_synonyms():
    assert classify_titles(["A사, 실적 개선 뚜렷"])["bullish_types"] == ["흑자전환/최대실적"]
    assert classify_titles(["A사, 실적 상향 조정"])["bullish_types"] == ["흑자전환/최대실적"]


def test_classify_titles_hbm_supply_news():
    result = classify_titles(["SK하이닉스, HBM 공급 확대 전망"])
    assert "신제품/양산" in result["bullish_types"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_classify_titles_target_price_upgrade():
    result = classify_titles(["SK하이닉스, 목표주가 상향 조정 - 증권사 리포트"])
    assert "목표가 상향" in result["bullish_types"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED
    assert classify_titles(["A사, 목표가 상향"])["bullish_types"] == ["목표가 상향"]


def test_classify_titles_foreign_net_buying_surge():
    result = classify_titles(["A사, 외국인 순매수 급증세"])
    assert "외국인 순매수 급증" in result["bullish_types"]
    assert result["tier"] == BULLISH_TIER_UNVALIDATED


def test_all_added_types_have_a_tier():
    from quant.analyze.bullish_markers import BULLISH_MARKERS
    for btype in BULLISH_MARKERS.values():
        assert btype in BULLISH_TYPE_TIERS


# ------------------------------------------------------------------ classify_titles_dated — 감쇠 가중(2026-08-18)

def test_classify_titles_dated_no_titles_is_tier_zero_with_weight_one():
    result = classify_titles_dated([])
    assert result["bullish_types"] == []
    assert result["tier"] == 0
    assert result["tier_weight"] == 1.0


def test_classify_titles_dated_todays_match_keeps_full_weight():
    result = classify_titles_dated([{"title": "A사, 대규모 수주 공시", "weight": 1.0}])
    assert result["tier"] == BULLISH_TIER_VALIDATED
    assert result["tier_weight"] == 1.0


def test_classify_titles_dated_only_older_match_gets_decayed_weight():
    """마커가 전일치(가중 0.6) 기사에만 있으면 tier_weight가 그 가중치를
    반영한다 — 창 확장(최근 3개장일) 없이는 아예 tier=0이었을 신호다."""
    result = classify_titles_dated([
        {"title": "A사, 대규모 수주 공시", "weight": 0.6},
        {"title": "오늘의 증시 브리핑", "weight": 1.0},
    ])
    assert result["tier"] == BULLISH_TIER_VALIDATED
    assert result["tier_weight"] == 0.6


def test_classify_titles_dated_prefers_freshest_matching_weight():
    """같은 유형이 여러 날짜에 걸쳐 매칭되면 가장 신선한(가중치가 큰) 기사의
    가중치를 쓴다 — 오늘도 다시 언급됐으면 굳이 감쇠할 이유가 없다."""
    result = classify_titles_dated([
        {"title": "A사, 수주 공시(전일)", "weight": 0.6},
        {"title": "A사, 수주 공시 후속 보도", "weight": 1.0},
    ])
    assert result["tier"] == BULLISH_TIER_VALIDATED
    assert result["tier_weight"] == 1.0


def test_classify_titles_dated_ignores_bearish_only_titles_for_weight():
    """호재 마커가 전혀 없으면(악재만 있어도) tier_weight는 항등원 1.0 —
    곱해질 점수 자체가 0이라 값이 무의미하지만 계약은 지킨다."""
    result = classify_titles_dated([{"title": "A사, 목표가 하향 조정", "weight": 0.3}])
    assert result["tier"] == 0
    assert result["bearish"] is True
    assert result["tier_weight"] == 1.0


# ------------------------------------------------------------------ news_axis_v2 — tier_weight(2026-08-18)

def test_news_axis_v2_tier_weight_default_matches_old_behavior():
    """`tier_weight`를 안 주면(기본 1.0) 감쇠 가중 도입 전과 점수가 완전히
    같아야 한다 — 하위 호환 계약."""
    bull = _bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED)
    pts, evidence = news_axis_v2(today_articles=0, news_z=0.0, max_dup_count=0, bull=bull)
    assert pts == 15
    assert "최근성 가중" not in evidence


def test_news_axis_v2_tier_weight_scales_only_the_tier_points():
    bull = _bull(bullish_types=["수주/공급계약"], tier=BULLISH_TIER_VALIDATED)
    pts, evidence = news_axis_v2(
        today_articles=0, news_z=0.0, max_dup_count=0, bull=bull, tier_weight=0.6,
    )
    assert pts == round(15 * 0.6) == 9
    assert "최근성 가중 0.6" in evidence


def test_news_axis_v2_tier_weight_applies_to_low_tier_too():
    bull = _bull(bullish_types=["특허"], tier=BULLISH_TIER_UNVALIDATED)
    pts, _ = news_axis_v2(
        today_articles=0, news_z=0.0, max_dup_count=0, bull=bull, tier_weight=0.3,
    )
    assert pts == round(8 * 0.3) == 2


# ------------------------------------------------------------------ 회귀: SK하이닉스 15점 사건(2026-08-18)
# 재현: 종목 호재가 전일(가중 0.6)에만 터지고, 오늘 제목엔 마커가 없다.
# `mentions.continuity()`가 만드는 `recent_titles`(최근 3개장일 감쇠)를 그대로
# 써서, 옛 경로(오늘치 titles만)와 새 경로(recent_titles)를 나란히 비교한다
# — 옛 경로는 tier=0(호재 없음), 새 경로는 tier>0 + 감쇠 점수가 나와야 한다.

def test_hynix_weekend_catalyst_regression():
    from datetime import date
    from quant.analyze.mentions import continuity
    from quant.analyze.intraday_score import NEWS_AXIS_BUDGET

    ledger = [
        {"date": "2026-08-17", "symbol": "000660", "name": "SK하이닉스",
         "title": "SK하이닉스, HBM 공급 확대에 목표주가 상향", "link": "a1",
         "feed": "F", "market": "KR"},
        {"date": "2026-08-18", "symbol": "000660", "name": "SK하이닉스",
         "title": "오늘의 코스피 시황 브리핑", "link": "a2", "feed": "F", "market": "KR"},
    ]
    cont = continuity(ledger, date(2026, 8, 18), market="KR")
    c = cont["000660"]

    # 옛 경로 — 오늘치 titles만(창 문제 재현): 마커 없음.
    old_titles = [t["title"] for t in c["titles"]]
    old_bull = classify_titles(old_titles)
    old_pts, _ = news_axis_v2(today_articles=len(old_titles), news_z=None, max_dup_count=1, bull=old_bull)
    assert old_bull["bullish_types"] == []
    assert old_pts == 0

    # 새 경로 — recent_titles(최근 3개장일 감쇠): 전일 마커를 0.6 가중으로 잡는다.
    new_bull = classify_titles_dated(c["recent_titles"])
    new_pts, evidence = news_axis_v2(
        today_articles=len(old_titles), news_z=None, max_dup_count=1,
        bull=new_bull, tier_weight=new_bull["tier_weight"],
    )
    assert new_bull["bullish_types"]  # 신제품/양산·목표가 상향 둘 다 잡힌다
    assert new_bull["tier_weight"] == 0.6
    assert new_pts > old_pts
    assert "최근성 가중 0.6" in evidence

    scaled_before = round(NEWS_AXIS_BUDGET * old_pts / NEWS_AXIS_MAX)
    scaled_after = round(NEWS_AXIS_BUDGET * new_pts / NEWS_AXIS_MAX)
    assert scaled_before == 0
    assert scaled_after > 0
