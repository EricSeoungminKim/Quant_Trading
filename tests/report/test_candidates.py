

# ── 근거 임계 (2026-08-12 실측: 1건짜리 잡음이 후보에 올랐다) ──────────

from quant.analyze.render import (  # noqa: E402
    MIN_ARTICLES, MIN_STREAK, candidates_line, is_candidate,
)


def _c(articles=1, streak=1, new=False, in_ranking=False):
    return {"today_articles": articles, "streak_days": streak,
            "is_new": new, "in_ranking": in_ranking}


def test_single_mention_is_noise_not_a_candidate():
    """대신증권·신세계·에이스침대가 각 1건으로 후보에 올랐던 실측 회귀."""
    assert not is_candidate(_c(articles=1, streak=1))


def test_multiple_mentions_qualify():
    assert is_candidate(_c(articles=MIN_ARTICLES))


def test_streak_qualifies_even_with_one_article_today():
    assert is_candidate(_c(articles=1, streak=MIN_STREAK))


def test_ranking_qualifies_without_news():
    """뉴스 언급과 거래 관심은 다르다 — 돈이 몰린 종목은 그 자체로 근거다."""
    assert is_candidate(_c(articles=0, streak=0, in_ranking=True))


def test_noise_is_excluded_from_auto_watch():
    cont = {
        "005930": {"name": "삼성전자", "today_articles": 18, "streak_days": 1,
                   "is_new": False, "in_ranking": True},
        "003540": {"name": "대신증권", "today_articles": 1, "streak_days": 1,
                   "is_new": True, "in_ranking": False},
    }
    line = candidates_line(cont, {})
    assert "005930" in line and "003540" not in line


def test_rank_tag_is_emitted():
    cont = {"005930": {"name": "삼성전자", "today_articles": 3, "streak_days": 1,
                       "is_new": False, "in_ranking": True}}
    assert "005930:NEWS+RANK" in candidates_line(cont, {})


def test_anchors_still_added_regardless_of_threshold():
    line = candidates_line({}, {"005930.KS": {"symbol": "005930", "label": "삼성전자"}})
    assert "005930:ANCHOR" in line
