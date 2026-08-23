from quant.analyze.scoring import label_100
from quant.analyze.symbol_score import score_all, score_symbol


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


def test_upside_is_positive_only_when_meaningful():
    assert "upside" not in _keys(score_symbol(None, _detail(upside=5.0)))
    assert score_symbol(None, _detail(upside=105.4))["score"] == 1
    assert score_symbol(None, _detail(upside=-8.0))["score"] == -1


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
