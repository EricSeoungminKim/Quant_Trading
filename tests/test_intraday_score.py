"""`quant.analyze.intraday_score` — 당일 단타 스코어러 (서브프로젝트 K).

2026-08-17: 외국인 추세 축이 라벨 기반(`foreign_trend.classify`)에서
`foreign_score_v2`(0~`FOREIGN_V2_MAX`, 서브프로젝트 O) 기반으로 바뀌어
`foreign_label` 입력이 `foreign_v2_score`로 교체됐다 — 이 파일의 모든
"외국인 추세" 테스트가 그 교체를 검증한다.

같은 날(서브프로젝트 P): 옛 "뉴스 모멘텀"(25) + "다매체 사건"(15) 두 축이
"호재 뉴스"(40, v3) 하나로 합쳐졌다 — `quant.analyze.bullish_markers.
news_axis_v2`를 감싸는 `_news_axis_factor`. 이 파일의 옛 "뉴스 모멘텀"/
"다매체 사건" 섹션 테스트는 "호재 뉴스" 섹션으로 교체됐다(세부 산식 자체의
단위 테스트는 `tests/test_bullish_markers.py`가 맡는다 — 이 파일은 스코어러
배선(팩터 이름/예산/합산)만 검증).

같은 날 다시(서브프로젝트 S part 2, v4): "텔레그램 시그널" 축(8점)이
신설되면서 기존 4축이 비례 축소됐다 — 호재 뉴스 40→38, 외국인 추세 30→28,
트렌딩 15→13, 촉매 15→13(intraday_score.py 모듈 docstring "2026-08-17 —
텔레그램" 절). 이 파일의 값들은 전부 v4 예산 기준이다.
"""
from __future__ import annotations

from quant.analyze.foreign_flow_v2 import FOREIGN_V2_MAX
from quant.analyze.intraday_score import (
    CATALYST_MAX,
    FOREIGN_TREND_MAX,
    NEWS_AXIS_BUDGET,
    SCORE_MAX,
    TELEGRAM_MAX,
    TRENDING_MAX,
    rank_intraday,
    score_intraday,
)


def _base_inputs(**overrides) -> dict:
    """모든 요인이 꺼진 기본 입력(None 이 하나도 없음) — 개별 요인만 켜서 테스트."""
    inputs = {
        "today_articles": 0,
        "news_z": 0.0,
        "max_dup_count": 0,
        "titles": [],
        "foreign_v2_score": 0,
        "trending_score100": 0,
        "dart_types": [],
        "theme_change_pct": 0.0,
        "telegram_mention": None,
    }
    inputs.update(overrides)
    return inputs


def _factor(result: dict, name: str) -> tuple[str, int, int, str]:
    for f in result["factors"]:
        if f[0] == name:
            return f
    raise KeyError(name)


# ------------------------------------------------------------------ 호재 뉴스 (v3 산식, v4 예산 38)

def test_news_axis_all_off():
    result = score_intraday(_base_inputs())
    name, pts, mx, evidence = _factor(result, "호재 뉴스")
    assert pts == 0
    assert mx == NEWS_AXIS_BUDGET == 38


def test_news_axis_articles_only():
    result = score_intraday(_base_inputs(today_articles=3, news_z=0.0))
    _, pts, _, evidence = _factor(result, "호재 뉴스")
    assert pts == 7  # round(38 * 7/40)
    assert "표본 부족" not in evidence


def test_news_axis_z_only():
    result = score_intraday(_base_inputs(today_articles=0, news_z=2.5))
    _, pts, _, _ = _factor(result, "호재 뉴스")
    assert pts == 8  # round(38 * 8/40)


def test_news_axis_z_none_does_not_fake_zero():
    """news_z=None 은 0점이지만, 0(실측 z=0)과 evidence 로 구분돼야 한다."""
    result = score_intraday(_base_inputs(today_articles=0, news_z=None))
    _, pts, _, evidence = _factor(result, "호재 뉴스")
    assert pts == 0
    assert "표본 부족" in evidence


def test_news_axis_dup_high_and_medium():
    result = score_intraday(_base_inputs(max_dup_count=3))
    assert _factor(result, "호재 뉴스")[1] == 10  # round(38 * 10/40)

    result_med = score_intraday(_base_inputs(max_dup_count=2))
    assert _factor(result_med, "호재 뉴스")[1] == 5  # round(38 * 5/40)

    result_single = score_intraday(_base_inputs(max_dup_count=1))
    assert _factor(result_single, "호재 뉴스")[1] == 0


def test_news_axis_bullish_titles_add_points():
    result = score_intraday(_base_inputs(titles=["A사, 대규모 수주 공시"]))
    _, pts, _, evidence = _factor(result, "호재 뉴스")
    assert pts == 14  # round(38 * 15/40) — 상위 티어(실측 유효 유형)
    assert "호재:" in evidence

    result_low = score_intraday(_base_inputs(titles=["B사, 특허 취득"]))
    assert _factor(result_low, "호재 뉴스")[1] == 8  # round(38 * 8/40) — 하위 티어


def test_news_axis_bearish_marker_vetoes_60pct():
    result_clean = score_intraday(_base_inputs(
        today_articles=5, news_z=3.0, max_dup_count=5, titles=["A사, 대규모 수주 공시"],
    ))
    assert _factor(result_clean, "호재 뉴스")[1] == 38

    result_vetoed = score_intraday(_base_inputs(
        today_articles=5, news_z=3.0, max_dup_count=5,
        titles=["A사, 대규모 수주 공시", "A사, 목표가 하향 조정 - SK증권"],
    ))
    _, pts, _, evidence = _factor(result_vetoed, "호재 뉴스")
    assert pts == 15  # round(38 * round(40*0.4)/40) == round(38*16/40)
    assert "악재 마커" in evidence


def test_news_axis_all_on_hits_full_budget():
    result = score_intraday(_base_inputs(
        today_articles=5, news_z=3.0, max_dup_count=5, titles=["A사, 대규모 수주 공시"],
    ))
    assert _factor(result, "호재 뉴스")[1] == NEWS_AXIS_BUDGET == 38


# ------------------------------------------------------------------ 외국인 추세 (v2 산식, v4 예산 28)

def test_foreign_trend_v2_max_score_is_full_points():
    result = score_intraday(_base_inputs(foreign_v2_score=FOREIGN_V2_MAX))
    _, pts, mx, _ = _factor(result, "외국인 추세")
    assert pts == FOREIGN_TREND_MAX == 28 and mx == 28


def test_foreign_trend_v2_zero_score_is_zero_points():
    result = score_intraday(_base_inputs(foreign_v2_score=0))
    assert _factor(result, "외국인 추세")[1] == 0


def test_foreign_trend_v2_scales_linearly():
    # foreign_v2_score=20/40 → 28 * 20/40 = 14.
    result = score_intraday(_base_inputs(foreign_v2_score=20))
    assert _factor(result, "외국인 추세")[1] == 14

    # foreign_v2_score=12/40 → round(28 * 12/40) = round(8.4) = 8.
    result2 = score_intraday(_base_inputs(foreign_v2_score=12))
    assert _factor(result2, "외국인 추세")[1] == 8


def test_foreign_trend_v2_none_is_zero_with_honest_evidence():
    # foreign_v2_score=None 은 nullable 4개 중 1개뿐이므로 여전히 채점된다.
    result = score_intraday(_base_inputs(foreign_v2_score=None))
    assert result is not None
    _, pts, _, evidence = _factor(result, "외국인 추세")
    assert pts == 0
    assert "수급 시계열 없음" in evidence


# ------------------------------------------------------------------ 트렌딩 (v4 예산 13)

def test_trending_high():
    result = score_intraday(_base_inputs(trending_score100=70))
    assert _factor(result, "트렌딩")[1] == TRENDING_MAX == 13
    result2 = score_intraday(_base_inputs(trending_score100=99))
    assert _factor(result2, "트렌딩")[1] == 13


def test_trending_medium():
    result = score_intraday(_base_inputs(trending_score100=55))
    assert _factor(result, "트렌딩")[1] == 7


def test_trending_low():
    result = score_intraday(_base_inputs(trending_score100=54))
    assert _factor(result, "트렌딩")[1] == 0


# ------------------------------------------------------------------ 촉매 (v4 예산 13)

def test_catalyst_dart_top_type_match():
    """2026-08-17(서브프로젝트 P): M 연구 실측(수주 +0.62%p, 자사주 +0.83%p,
    n≥30·base 대비 +0.5%p 이상)으로 화이트리스트 상위 티어가 바뀌었다.
    v4(서브프로젝트 S part 2)에서 텔레그램 축 신설로 예산이 15→13(상위
    티어 10→9)로 줄었다."""
    result = score_intraday(_base_inputs(dart_types=["수주"]))
    assert _factor(result, "촉매")[1] == 9

    result2 = score_intraday(_base_inputs(dart_types=["자사주"]))
    assert _factor(result2, "촉매")[1] == 9


def test_catalyst_dart_watch_type_lower_points():
    """`임상·승인`은 M 연구에서 표본 미달(n=26)로 판단 보류 — 상위 티어보다
    낮은 배점을 유지한다(화이트리스트에서 빼지는 않는다). v4에서 5→4."""
    result = score_intraday(_base_inputs(dart_types=["임상·승인"]))
    assert _factor(result, "촉매")[1] == 4


def test_catalyst_dart_earnings_no_longer_top_tier():
    """`실적`은 M 연구에서 n=1,069(표본 충분)인데 +0.48%p로 문턱(+0.5%p) 미달
    — "이 창에서는 base와 구분 안 됨"이라 화이트리스트에서 뺐다(하향)."""
    result = score_intraday(_base_inputs(dart_types=["실적"]))
    assert _factor(result, "촉매")[1] == 0


def test_catalyst_dart_type_no_match():
    result = score_intraday(_base_inputs(dart_types=["유상증자"]))
    assert _factor(result, "촉매")[1] == 0


def test_catalyst_theme_change():
    result = score_intraday(_base_inputs(theme_change_pct=3.0))
    assert _factor(result, "촉매")[1] == 4  # v4: 5→4

    result_below = score_intraday(_base_inputs(theme_change_pct=2.9))
    assert _factor(result_below, "촉매")[1] == 0

    # 롱 온리 — 하락(-3%)은 촉매로 세지 않는다.
    result_down = score_intraday(_base_inputs(theme_change_pct=-5.0))
    assert _factor(result_down, "촉매")[1] == 0


def test_catalyst_both_dart_and_theme():
    result = score_intraday(_base_inputs(dart_types=["수주"], theme_change_pct=4.0))
    assert _factor(result, "촉매")[1] == CATALYST_MAX == 13


# ------------------------------------------------------------------ 텔레그램 시그널 (v4 신설, 예산 8)

def test_telegram_no_mention_is_zero():
    result = score_intraday(_base_inputs(telegram_mention=None))
    _, pts, mx, evidence = _factor(result, "텔레그램 시그널")
    assert pts == 0 and mx == TELEGRAM_MAX == 8
    assert "언급 없음" in evidence


def test_telegram_empty_channels_is_zero():
    result = score_intraday(_base_inputs(telegram_mention={"channels": [], "sector_room": False}))
    assert _factor(result, "텔레그램 시그널")[1] == 0


def test_telegram_sector_room_mention_gets_full_points():
    mention = {
        "channels": [{"handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector"}],
        "sector_room": True,
    }
    result = score_intraday(_base_inputs(telegram_mention=mention))
    _, pts, mx, evidence = _factor(result, "텔레그램 시그널")
    assert pts == 8 and mx == 8
    assert "전력 섹터" in evidence and "pikachu_aje" in evidence


def test_telegram_news_or_macro_room_mention_gets_half_points():
    mention = {
        "channels": [{"handle": "tazastock", "분류": "장중 시황·국내 시황", "tier": "news"}],
        "sector_room": False,
    }
    result = score_intraday(_base_inputs(telegram_mention=mention))
    assert _factor(result, "텔레그램 시그널")[1] == 4


def test_telegram_mixed_channels_prefers_sector_points():
    """섹터방과 뉴스방 둘 다 언급했으면 더 강한 신호(섹터, +8)를 쓴다."""
    mention = {
        "channels": [
            {"handle": "tazastock", "분류": "장중 시황·국내 시황", "tier": "news"},
            {"handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector"},
        ],
        "sector_room": True,
    }
    result = score_intraday(_base_inputs(telegram_mention=mention))
    assert _factor(result, "텔레그램 시그널")[1] == 8


# ------------------------------------------------------------------ None 3개 이상 → None

def test_three_nullable_none_returns_none():
    result = score_intraday(_base_inputs(
        news_z=None, foreign_v2_score=None, trending_score100=None,
    ))
    assert result is None


def test_all_four_nullable_none_returns_none():
    result = score_intraday(_base_inputs(
        news_z=None, foreign_v2_score=None, trending_score100=None, theme_change_pct=None,
    ))
    assert result is None


def test_two_nullable_none_still_scores():
    result = score_intraday(_base_inputs(news_z=None, theme_change_pct=None))
    assert result is not None


def test_telegram_mention_absent_does_not_count_toward_none_gate():
    """텔레그램은 nullable 4종(_NULLABLE_KEYS)에 없다 — telegram_mention=None
    이 다른 3개 None 과 함께 있어도 게이트 카운트에 더해지지 않는다(이미
    3개만으로 게이트가 걸리는지는 위 테스트가 확인한다 — 여기선 4번째로
    telegram_mention=None 을 얹어도 여전히 같은 이유로 None 이 되는지만
    확인, 즉 텔레그램이 카운트를 5개로 부풀리지 않는다는 것)."""
    result = score_intraday(_base_inputs(
        news_z=None, foreign_v2_score=None, telegram_mention=None,
    ))
    assert result is not None  # nullable 2개(news_z, foreign_v2_score)뿐 — 게이트 미달


# ------------------------------------------------------------------ breakdown 합산

def test_breakdown_sums_to_score100():
    mention = {
        "channels": [{"handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector"}],
        "sector_room": True,
    }
    result = score_intraday(_base_inputs(
        today_articles=5, news_z=3.0, max_dup_count=5, titles=["A사, 대규모 수주 공시"],
        foreign_v2_score=FOREIGN_V2_MAX, trending_score100=80,
        dart_types=["수주"], theme_change_pct=4.0, telegram_mention=mention,
    ))
    assert result["score100"] == sum(f[1] for f in result["factors"])
    assert result["score100"] == SCORE_MAX == 100
    assert len(result["factors"]) == 5
    for name, pts, mx, evidence in result["factors"]:
        assert 0 <= pts <= mx
        assert isinstance(evidence, str) and evidence


def test_breakdown_all_off_sums_to_zero():
    result = score_intraday(_base_inputs())
    assert result["score100"] == 0


# ------------------------------------------------------------------ rank_intraday

def test_rank_orders_by_score_desc():
    candidates = {
        "LOW": _base_inputs(),
        "HIGH": _base_inputs(foreign_v2_score=FOREIGN_V2_MAX, today_articles=5, news_z=3.0),
    }
    ranked = rank_intraday(candidates, top=8)
    assert [sym for sym, _ in ranked] == ["HIGH", "LOW"]


def test_rank_tie_break_by_foreign_v2_score_strength():
    """동점이면 외국인 신호(foreign_v2_score)가 더 뚜렷한 쪽이 우선 —
    symbol 알파벳 순서보다 우선한다."""
    strong_foreign = _base_inputs(foreign_v2_score=FOREIGN_V2_MAX, trending_score100=0)  # 28
    weak_foreign_but_trending = _base_inputs(
        foreign_v2_score=30, trending_score100=55,
    )  # round(28*30/40)=21 + 7 = 28
    assert score_intraday(strong_foreign)["score100"] == score_intraday(weak_foreign_but_trending)["score100"]

    # "AAA"가 알파벳상 먼저지만 약한 신호, "ZZZ"가 알파벳상 나중이지만 강한 신호.
    candidates = {"AAA": weak_foreign_but_trending, "ZZZ": strong_foreign}
    ranked = rank_intraday(candidates, top=8)
    assert [sym for sym, _ in ranked] == ["ZZZ", "AAA"]


def test_rank_tie_break_falls_back_to_symbol():
    candidates = {"BBB": _base_inputs(), "AAA": _base_inputs()}
    ranked = rank_intraday(candidates, top=8)
    assert [sym for sym, _ in ranked] == ["AAA", "BBB"]


def test_rank_excludes_none_results():
    candidates = {
        "OK": _base_inputs(),
        "INSUFFICIENT": _base_inputs(news_z=None, foreign_v2_score=None, trending_score100=None),
    }
    ranked = rank_intraday(candidates, top=8)
    assert [sym for sym, _ in ranked] == ["OK"]


def test_rank_respects_top():
    candidates = {f"S{i}": _base_inputs(today_articles=i) for i in range(10)}
    ranked = rank_intraday(candidates, top=3)
    assert len(ranked) == 3
