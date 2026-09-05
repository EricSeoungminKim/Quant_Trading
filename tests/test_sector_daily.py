"""일일 주도 섹터 판정(quant.analyze.sector_daily) — 순수 함수 테스트.

`build_sector_daily_rows`/`rank_with_trend`/`scoring_context`는 전부 네트워크·
파일 I/O 없이 인자로만 판단한다(sector_view.py와 같은 원칙).
"""
from quant.analyze.sector_daily import (
    build_sector_daily_rows, composite_score, rank_with_trend, scoring_context,
)


def _sector_members():
    return {
        "반도체와반도체장비": [
            {"code": "005930", "name": "삼성전자"},
            {"code": "000660", "name": "SK하이닉스"},
        ],
        "자동차": [
            {"code": "005380", "name": "현대차"},
        ],
        "화학": [
            {"code": "051910", "name": "LG화학"},
        ],
    }


# --------------------------------------------------------------- build_sector_daily_rows

def test_turnover_summed_across_members_and_ranked_descending():
    rows = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 300, "000660": 200, "005380": 100},
        foreign_net_by_symbol={},
    )
    names = [r["sector"] for r in rows]
    assert names == ["반도체와반도체장비", "자동차"], "화학은 거래대금 관측이 없어 빠진다"
    semi = rows[0]
    assert semi["turnover_krw"] == 500
    assert semi["n_members"] == 2
    assert semi["top_members"][0]["code"] == "005930", "개별 거래대금 내림차순"


def test_sector_with_zero_observed_turnover_is_excluded_not_zeroed():
    """멤버 전원이 거래대금 원장에 없는 업종은 0으로 위장하지 않고 결과에서 빠진다."""
    rows = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 300},  # 000660/005380/051910은 없음
        foreign_net_by_symbol={},
    )
    names = {r["sector"] for r in rows}
    assert "자동차" not in names
    assert "화학" not in names


def test_foreign_net_none_when_no_member_observed():
    rows = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005380": 100},
        foreign_net_by_symbol={},  # 관측 없음
    )
    auto = next(r for r in rows if r["sector"] == "자동차")
    assert auto["foreign_net"] is None


def test_foreign_net_summed_across_observed_members_only():
    rows = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 300, "000660": 200},
        foreign_net_by_symbol={"005930": 1000, "000660": -400},  # 합산 +600
    )
    semi = next(r for r in rows if r["sector"] == "반도체와반도체장비")
    assert semi["foreign_net"] == 600


def test_empty_sector_members_returns_empty_rows():
    assert build_sector_daily_rows("2026-09-02", "KR", {}, {}, {}) == []


# --------------------------------------------------------------- rank_with_trend

def test_rank_assigned_by_turnover_descending():
    today = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 100, "005380": 900},
        foreign_net_by_symbol={},
    )
    ranked = rank_with_trend(today, [])
    by_sector = {r["sector"]: r["rank"] for r in ranked}
    assert by_sector["자동차"] == 1  # 900 > 100
    assert by_sector["반도체와반도체장비"] == 2


def test_trend_new_when_no_history():
    today = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 100},
        foreign_net_by_symbol={},
    )
    ranked = rank_with_trend(today, [])
    assert ranked[0]["trend"] == "신규"


def test_trend_up_when_rank_improves_from_yesterday():
    today = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 100, "005380": 900},  # 자동차 1위, 반도체 2위
        foreign_net_by_symbol={},
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 10, "foreign_net": None, "n_members": 2, "top_members": []},
        {"date": "2026-09-01", "market": "KR", "sector": "자동차",
         "turnover_krw": 900, "foreign_net": None, "n_members": 1, "top_members": []},
        # 어제는 반도체가 2위, 자동차가 1위 — 반도체 순위는 어제도 오늘도 2위(불변)
    ]
    ranked = rank_with_trend(today, history)
    semi = next(r for r in ranked if r["sector"] == "반도체와반도체장비")
    assert semi["trend"] == "="


def test_trend_up_and_down_reflect_rank_change():
    today = build_sector_daily_rows(
        "2026-09-02", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100},  # 오늘: 반도체 1위, 자동차 2위
        foreign_net_by_symbol={},
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 10, "foreign_net": None, "n_members": 2, "top_members": []},
        {"date": "2026-09-01", "market": "KR", "sector": "자동차",
         "turnover_krw": 900, "foreign_net": None, "n_members": 1, "top_members": []},
        # 어제: 자동차 1위, 반도체 2위
    ]
    ranked = rank_with_trend(today, history)
    semi = next(r for r in ranked if r["sector"] == "반도체와반도체장비")
    auto = next(r for r in ranked if r["sector"] == "자동차")
    assert semi["trend"] == "↑"  # 2위 → 1위
    assert auto["trend"] == "↓"  # 1위 → 2위


def test_history_only_looks_at_most_recent_date_for_trend():
    """trend 비교는 히스토리 중 가장 최근 날짜와만 비교한다."""
    today = build_sector_daily_rows(
        "2026-09-03", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100},
        foreign_net_by_symbol={},
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": None, "n_members": 2, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 10, "foreign_net": None, "n_members": 2, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "자동차",
         "turnover_krw": 900, "foreign_net": None, "n_members": 1, "top_members": []},
    ]
    ranked = rank_with_trend(today, history)
    semi = next(r for r in ranked if r["sector"] == "반도체와반도체장비")
    assert semi["trend"] == "↑"  # 09-02 기준 2위 → 오늘 1위 (09-01은 무시)


def test_foreign_net_negative_streak_counts_consecutive_days_including_today():
    today = build_sector_daily_rows(
        "2026-09-03", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900},
        foreign_net_by_symbol={"005930": -100},  # 오늘도 음수
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": -50, "n_members": 2, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": -20, "n_members": 2, "top_members": []},
    ]
    ranked = rank_with_trend(today, history)
    semi = ranked[0]
    assert semi["foreign_net_negative_streak"] == 3


def test_foreign_net_negative_streak_breaks_on_positive_day():
    today = build_sector_daily_rows(
        "2026-09-03", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900},
        foreign_net_by_symbol={"005930": -100},
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": -50, "n_members": 2, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": 20, "n_members": 2, "top_members": []},  # 양수 — 끊긴다
    ]
    ranked = rank_with_trend(today, history)
    assert ranked[0]["foreign_net_negative_streak"] == 1


def test_foreign_net_negative_streak_breaks_on_missing_data():
    """결측(그 업종 그날 데이터 없음)은 이탈로 단정하지 않고 스트릭을 끊는다."""
    today = build_sector_daily_rows(
        "2026-09-03", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900},
        foreign_net_by_symbol={"005930": -100},
    )
    history = [
        {"date": "2026-09-01", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": -50, "n_members": 2, "top_members": []},
        {"date": "2026-09-02", "market": "KR", "sector": "반도체와반도체장비",
         "turnover_krw": 900, "foreign_net": None, "n_members": 2, "top_members": []},  # 결측
    ]
    ranked = rank_with_trend(today, history)
    assert ranked[0]["foreign_net_negative_streak"] == 1


# --------------------------------------------------------------- scoring_context

def test_scoring_context_top3_positive_requires_both_rank_and_sign():
    ranked = [
        {"sector": "A", "rank": 1, "foreign_net": 100, "foreign_net_negative_streak": 0},
        {"sector": "B", "rank": 2, "foreign_net": -50, "foreign_net_negative_streak": 0},
        {"sector": "C", "rank": 4, "foreign_net": 200, "foreign_net_negative_streak": 0},
    ]
    ctx = scoring_context(ranked)
    assert ctx["top3_positive"] == {"A"}, "B는 상위3이지만 순매수 음수, C는 순매수 양수지만 4위"


def test_scoring_context_negative_streak3_needs_at_least_3_days():
    ranked = [
        {"sector": "A", "rank": 1, "foreign_net": -10, "foreign_net_negative_streak": 3},
        {"sector": "B", "rank": 2, "foreign_net": -10, "foreign_net_negative_streak": 2},
    ]
    ctx = scoring_context(ranked)
    assert ctx["negative_streak3"] == {"A"}


# --------------------------------------------------------------- composite_score
# (전일 US 섹터 링크 신호, 2026-09-06 — quant-backtest results/sector_link/
# SUMMARY.md S1 근거. §sector_daily.US_LINK_WEIGHT 주석 참고.)

def test_composite_score_weight_zero_ignores_us_link_ret():
    """weight=0(기본값)이면 us_link_ret이 무엇이든(None 포함) 결과가 같다 —
    재현성의 핵심 불변식."""
    base = composite_score(1, 4, None, weight=0.0)
    assert composite_score(1, 4, 0.05, weight=0.0) == base
    assert composite_score(1, 4, -0.9, weight=0.0) == base


def test_composite_score_matches_turnover_rank_ordering_at_weight_zero():
    """weight=0일 때 composite_score로 정렬해도 rank(=거래대금) 순서와 같다."""
    scores = {r: composite_score(r, 5, None) for r in range(1, 6)}
    ranks_sorted_by_score = sorted(scores, key=lambda r: -scores[r])
    assert ranks_sorted_by_score == [1, 2, 3, 4, 5]


def test_composite_score_nonzero_weight_adds_us_link_contribution():
    base = composite_score(2, 4, None, weight=0.5)
    assert composite_score(2, 4, 0.1, weight=0.5) == base + 0.05
    assert composite_score(2, 4, -0.1, weight=0.5) == base - 0.05


def test_composite_score_top_rank_is_one_point_zero():
    assert composite_score(1, 4, None, weight=0.0) == 1.0


# --------------------------------------------------------------- rank_with_trend
# us_sector_returns (전일 US 섹터 링크)

def test_rank_with_trend_without_us_sector_returns_leaves_link_fields_none():
    """기존 호출부 하위호환 — us_sector_returns 생략 시 전부 None, weight=0
    이므로 composite_score는 rank만의 함수."""
    today = build_sector_daily_rows(
        "2026-09-06", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100},
        foreign_net_by_symbol={},
    )
    ranked = rank_with_trend(today, [])
    semi = next(r for r in ranked if r["sector"] == "반도체와반도체장비")
    assert semi["us_link_ret"] is None
    assert semi["us_link_gics_kr"] is None
    assert semi["composite_score"] == composite_score(semi["rank"], len(ranked), None)


def test_rank_with_trend_attaches_us_link_ret_via_gics_mapping():
    """반도체와반도체장비 → GICS Information Technology. us_sector_returns에
    그 섹터 수익률이 있으면 행에 붙는다."""
    today = build_sector_daily_rows(
        "2026-09-06", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100},
        foreign_net_by_symbol={},
    )
    ranked = rank_with_trend(
        today, [], us_sector_returns={"Information Technology": 0.02, "Financials": -0.01},
    )
    semi = next(r for r in ranked if r["sector"] == "반도체와반도체장비")
    auto = next(r for r in ranked if r["sector"] == "자동차")
    assert semi["us_link_ret"] == 0.02
    assert semi["us_link_gics_kr"] == "기술"
    # 자동차 → Consumer Discretionary, us_sector_returns에 없음 → None
    assert auto["us_link_ret"] is None
    assert auto["us_link_gics_kr"] is None


def test_rank_with_trend_sector_with_no_gics_mapping_gets_none_link():
    """kr_sectors.UPJONG_TO_GICS에 없는 업종명은 us_sector_returns가 있어도
    None(있는 걸 지어내지 않는다)."""
    members = {"존재하지않는업종": [{"code": "999999", "name": "테스트"}]}
    today = build_sector_daily_rows(
        "2026-09-06", "KR", members, turnover_by_symbol={"999999": 100},
        foreign_net_by_symbol={},
    )
    ranked = rank_with_trend(
        today, [], us_sector_returns={"Information Technology": 0.02},
    )
    assert ranked[0]["us_link_ret"] is None


def test_rank_with_trend_composite_score_identical_regardless_of_us_sector_returns_at_default_weight():
    """모듈 기본 weight(0)에서는 us_sector_returns를 넘기든 안 넘기든
    composite_score가 완전히 같다 — 요구된 재현성 테스트."""
    today_a = build_sector_daily_rows(
        "2026-09-06", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100, "051910": 50},
        foreign_net_by_symbol={},
    )
    today_b = build_sector_daily_rows(
        "2026-09-06", "KR", _sector_members(),
        turnover_by_symbol={"005930": 900, "005380": 100, "051910": 50},
        foreign_net_by_symbol={},
    )
    ranked_without = rank_with_trend(today_a, [])
    ranked_with = rank_with_trend(
        today_b, [],
        us_sector_returns={"Information Technology": 0.05, "Consumer Discretionary": -0.03,
                            "Materials": 0.01},
    )
    scores_without = {r["sector"]: r["composite_score"] for r in ranked_without}
    scores_with = {r["sector"]: r["composite_score"] for r in ranked_with}
    assert scores_without == scores_with
