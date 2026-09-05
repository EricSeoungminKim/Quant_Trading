from quant.analyze.scoring import label_100, to_100
from quant.analyze.symbol_score import (
    CONFIRMATION_BONUS, NEWS_HOT_WEIGHT, SPAN, TRENDING_WEIGHT, score_all, score_symbol,
)


def _cont(today=1, streak=1):
    return {"name": "X", "days": 1, "articles": today, "today_articles": today,
            "streak_days": streak, "is_new": False, "history": [], "titles": []}


def _detail(f5=None, i5=None, fstreak=0, istreak=0, upside=None, opinion=None):
    return {
        "flow_summary": {"foreign_net_5d": f5, "inst_net_5d": i5,
                         "foreign_buy_streak": fstreak, "inst_buy_streak": istreak},
        "consensus": {"opinion_score": opinion} if opinion is not None else None,
        "upside_pct": upside,
    }


def _trending(score100=50, boards=None):
    return {"score100": score100, "boards": boards or {}}


def _keys(res):
    return {f["key"] for f in res["factors"]}


def test_no_data_is_neutral_with_no_factors():
    res = score_symbol(None, None)
    assert res["score"] == 0 and res["score100"] == 50
    assert res["label"] == "중립" and res["factors"] == []


def test_news_factors_need_thresholds():
    assert _keys(score_symbol(_cont(today=1, streak=1), None)) == set()
    assert _keys(score_symbol(_cont(today=5, streak=3), None)) == {"news_hot", "news_streak"}


def test_flow_direction_sets_sign():
    buy = score_symbol(None, _detail(f5=1000, i5=2000))
    sell = score_symbol(None, _detail(f5=-1000, i5=-2000))
    assert buy["score"] == 2 and sell["score"] == -2


def test_zero_net_flow_is_neutral_not_selling():
    """거래가 없던 종목을 순매도로 몰아 감점하면 안 된다."""
    res = score_symbol(None, _detail(f5=0, i5=0))
    assert res["score"] == 0 and _keys(res) == set()


def test_flow_streak_counts_the_better_of_two_actors():
    res = score_symbol(None, _detail(f5=1, i5=1, fstreak=1, istreak=4))
    assert "flow_streak" in _keys(res)


def test_short_flow_streak_is_not_a_factor():
    res = score_symbol(None, _detail(f5=1, i5=1, fstreak=1, istreak=2))
    assert "flow_streak" not in _keys(res)


def test_upside_positive_branch_is_zeroed_by_audit():
    """2026-09-06 감사: 목표주가 상향(>=20%) 근거는 D+1 방향 적중 53.5%였지만
    D+3 -88bp/D+5 -13bp로 역예측적이었다 — UPSIDE_POSITIVE_WEIGHT=0 이라
    아무리 큰 상향이어도 factor 자체가 안 생긴다(score 기여 0)."""
    assert "upside" not in _keys(score_symbol(None, _detail(upside=5.0)))
    assert "upside" not in _keys(score_symbol(None, _detail(upside=105.4)))
    assert score_symbol(None, _detail(upside=105.4))["score"] == 0


def test_upside_negative_branch_is_unchanged():
    """하향(<0%) 근거는 감사가 문제 삼지 않았다 — UPSIDE_NEGATIVE_WEIGHT=1 로
    그대로."""
    res = score_symbol(None, _detail(upside=-8.0))
    assert res["score"] == -1
    assert _keys(res) == {"upside"}


def test_opinion_thresholds():
    assert score_symbol(None, _detail(opinion=4.04))["score"] == 1
    assert score_symbol(None, _detail(opinion=2.5))["score"] == -1
    assert "opinion" not in _keys(score_symbol(None, _detail(opinion=3.5)))


def test_missing_consensus_does_not_break_scoring():
    res = score_symbol(_cont(today=5), {"flow_summary": {"foreign_net_5d": 100},
                                        "consensus": None, "upside_pct": None})
    assert res["score"] == 2 and _keys(res) == {"news_hot", "foreign_5d"}


def test_factors_always_sum_to_score():
    res = score_symbol(_cont(today=9, streak=4),
                       _detail(f5=1, i5=-1, istreak=5, upside=50.0, opinion=4.2))
    assert sum(f["delta"] for f in res["factors"]) == res["score"]


def test_score100_is_in_range():
    res = score_symbol(_cont(today=9, streak=4),
                       _detail(f5=1, i5=1, istreak=5, upside=50.0, opinion=4.2))
    assert 0 <= res["score100"] <= 100


def test_label_matches_score100_in_result():
    res = score_symbol(_cont(today=9, streak=4), _detail(f5=1, i5=1, istreak=5))
    assert res["label"] == label_100(res["score100"], "긍정 신호", "부정 신호")


def test_fewer_factors_stay_closer_to_neutral():
    """근거가 적을수록 50에 가까워야 한다 — 이게 고정 분모 설계의 핵심이다."""
    one_factor = score_symbol(_cont(today=9), None)  # news_hot 하나만
    many_factors = score_symbol(
        _cont(today=9, streak=4),
        _detail(f5=1, i5=1, istreak=5, upside=50.0, opinion=4.2),
    )
    assert abs(one_factor["score100"] - 50) < abs(many_factors["score100"] - 50)


def test_score_all_covers_every_news_symbol_even_without_detail():
    cont = {"005930": _cont(today=5), "000660": _cont(today=1)}
    out = score_all(cont, {"005930": _detail(f5=100)})
    assert set(out) == {"005930", "000660"}
    assert out["000660"]["factors"] == []


def test_every_factor_carries_a_stable_key_and_text():
    """3층 채점에서 요인별로 분해하려면 키가 안정적이어야 한다."""
    res = score_symbol(_cont(today=9, streak=4),
                       _detail(f5=1, i5=1, istreak=5, upside=50.0, opinion=4.2))
    for f in res["factors"]:
        assert f["key"] and isinstance(f["key"], str)
        assert f["text"] and f["delta"] in (1, -1)


# ── 트렌딩 가중치 + 뉴스/트렌딩 동시 확인 보너스 (2026-09-06 감사 반영) ──────

def test_no_trending_input_is_backward_compatible():
    """trending 인자를 생략하면(호출부 하위호환) 트렌딩/확인 요인이 아예 안
    생긴다 — 기존 2-인자 호출부(전부)가 바이트 단위로 그대로다."""
    res = score_symbol(_cont(today=9), None)
    assert _keys(res) == {"news_hot"}


def test_trending_weight_exceeds_news_hot_weight():
    """감사 근거: trending_score100 IC(+0.186) > ai_score100 IC(-0.082) —
    뉴스 건수 요인(news_hot)보다 트렌딩 요인의 가중치가 커야 한다."""
    assert TRENDING_WEIGHT > NEWS_HOT_WEIGHT


def test_trending_factor_scaled_by_weight_and_direction():
    """trending_score100 이 중립(50)보다 높으면 +TRENDING_WEIGHT, 낮으면
    -TRENDING_WEIGHT. news_hot 하나(가중치 1)만 있는 종목에 트렌딩을 얹으면
    raw = NEWS_HOT_WEIGHT(1) + TRENDING_WEIGHT(2) = 3, SPAN=8이므로
    score100 = 50 + 3/8*50 = 68.75 → 69."""
    bullish = score_symbol(_cont(today=9), None, _trending(score100=80))
    assert "trending" in _keys(bullish)
    assert bullish["score"] == NEWS_HOT_WEIGHT + TRENDING_WEIGHT
    assert bullish["score100"] == to_100(NEWS_HOT_WEIGHT + TRENDING_WEIGHT, SPAN) == 69

    bearish = score_symbol(_cont(today=9), None, _trending(score100=20))
    assert bearish["score"] == NEWS_HOT_WEIGHT - TRENDING_WEIGHT


def test_trending_neutral_score_adds_no_factor():
    """score100=50(중립)은 근거가 없다는 뜻이지 부정이 아니다 — factor 자체를
    안 남긴다(trending_score.py 의 같은 원칙)."""
    res = score_symbol(_cont(today=9), None, _trending(score100=50))
    assert "trending" not in _keys(res)


def test_trending_alone_without_ranking_boards_does_not_confirm():
    """트렌딩 점수는 있어도(예: 감시 리스트 캐시) 실제로 랭킹 보드에 없으면
    (`boards`가 비어 있으면) 뉴스+트렌딩 동시 확인이 아니다."""
    res = score_symbol(_cont(today=9), None, _trending(score100=80, boards={}))
    assert "confirmation" not in _keys(res)


def test_confirmation_bonus_alone_reaches_top_tier():
    """뉴스 origin(cont 있음) + 랭킹 보드 등재(boards 있음) 동시 확인이면
    CONFIRMATION_BONUS(4)가 붙는다. SPAN=8이므로 이 보너스 하나만으로도
    score100 = 50 + 4/8*50 = 75 — label_100의 "강한" 문턱(>=75)에 정확히
    닿는다(감사: origin=both D+1 +278bp, n=166 — 최상위 등급으로 취급할
    근거)."""
    res = score_symbol(_cont(today=0), None, _trending(score100=50, boards={"거래대금": [1]}))
    assert "confirmation" in _keys(res)
    assert res["score"] == CONFIRMATION_BONUS
    assert res["score100"] == 75 == to_100(CONFIRMATION_BONUS, SPAN)
    assert res["label"] == "강한 긍정 신호"


def test_confirmation_requires_both_news_and_ranking():
    """cont가 없으면(뉴스 origin이 아니면) boards가 있어도 확인 보너스가
    없다 — "둘 다"가 조건이다."""
    res = score_symbol(None, None, _trending(score100=80, boards={"거래대금": [1]}))
    assert "confirmation" not in _keys(res)
    # trending 요인(뉴스 origin과 무관)은 그대로 붙는다.
    assert "trending" in _keys(res)


def test_score_all_threads_trending_through():
    cont = {"005930": _cont(today=9), "000660": _cont(today=1)}
    trending = {"005930": _trending(score100=80, boards={"거래대금": [1]})}
    out = score_all(cont, {}, trending)
    assert "confirmation" in _keys(out["005930"])
    assert out["000660"]["factors"] == []  # 트렌딩 없음 + 뉴스 문턱 미달
