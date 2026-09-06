"""report_accuracy 순수 함수 테스트 — 합성 데이터. 실가격 조회 없음."""
from __future__ import annotations

from quant.control import report_accuracy as ra


def test_direction_bucket_label_wins_over_score():
    assert ra.direction_bucket("약한 상승 신호", 60) == "bull"
    assert ra.direction_bucket("약한 하락 신호", 40) == "bear"
    assert ra.direction_bucket("중립", 50) == "neutral"


def test_direction_bucket_score_fallback_without_label_text():
    assert ra.direction_bucket(None, 70) == "bull"
    assert ra.direction_bucket(None, 20) == "bear"
    assert ra.direction_bucket(None, 50) == "neutral"


def test_direction_bucket_none_when_nothing_known():
    assert ra.direction_bucket(None, None) is None


def test_extract_open_claims_filters_to_candidates_only():
    payload = {
        "session_date": "2026-08-13", "market": "US", "generated_at": "t0",
        "stance": {"label": "약한 상승 신호", "score100": 60},
        "symbols": [
            {"symbol": "NVDA", "name": "Nvidia", "ai_score100": 57,
             "trending_score100": None, "origin": "news", "upside_pct": None},
            {"symbol": "AAPL", "name": "Apple", "ai_score100": 40,
             "trending_score100": 30, "origin": "news", "upside_pct": 5.0},
        ],
    }
    row = ra.extract_open_claims(payload, candidate_symbols={"NVDA"})
    assert row is not None
    assert row["market"] == "US"
    assert row["date"] == "2026-08-13"
    assert row["session"] == "open"
    assert row["direction"] == {"label": "약한 상승 신호", "score100": 60}
    assert [c["symbol"] for c in row["candidates"]] == ["NVDA"]
    assert row["candidates"][0]["score_kind"] == "ai_score100"


def test_extract_open_claims_none_when_empty():
    assert ra.extract_open_claims({"symbols": [], "stance": {}}, set()) is None


def test_extract_close_claims_from_close_bet_view():
    payload = {
        "session_date": "2026-09-03", "market": "KR", "generated_at": "t1",
        "close_bet_view": [
            {"symbol": "042700", "name": "한미반도체", "score": 6, "change_pct": 9.0},
        ],
    }
    row = ra.extract_close_claims(payload)
    assert row is not None
    assert row["session"] == "close"
    assert row["direction"] is None
    assert row["candidates"][0] == {
        "symbol": "042700", "name": "한미반도체", "score": 6,
        "score_kind": "close_bet_score", "trending_score100": None,
        "origin": "close_bet", "upside_pct": None,
    }


def test_extract_close_claims_none_when_no_close_bet():
    assert ra.extract_close_claims({"close_bet_view": []}) is None


def test_trading_day_plus_on_exact_and_holiday_dates():
    cal = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]
    assert ra.trading_day_plus(cal, "2026-08-10", 1) == "2026-08-11"
    assert ra.trading_day_plus(cal, "2026-08-10", 4) == "2026-08-14"
    assert ra.trading_day_plus(cal, "2026-08-10", 5) is None
    # 08-11이 휴장이라 달력에 없다 -> 다음 거래일(08-12)을 0번째로 본다
    cal_gap = ["2026-08-10", "2026-08-12", "2026-08-13"]
    assert ra.trading_day_plus(cal_gap, "2026-08-11", 0) == "2026-08-12"
    assert ra.trading_day_plus(cal_gap, "2026-08-11", 1) == "2026-08-13"


def test_prev_trading_day():
    cal = ["2026-08-10", "2026-08-11", "2026-08-12"]
    assert ra.prev_trading_day(cal, "2026-08-12") == "2026-08-11"
    assert ra.prev_trading_day(cal, "2026-08-10") is None
    # 주말을 건너뛴 리포트 빌드일(08-14, 월) -> 마지막 거래일은 08-12(금)
    assert ra.prev_trading_day(cal, "2026-08-14") == "2026-08-12"


def test_wilson_ci_bounds_and_empty():
    lo, hi = ra.wilson_ci(5, 10)
    assert 0.0 < lo < 0.5 < hi < 1.0
    lo0, hi0 = ra.wilson_ci(0, 0)
    assert lo0 != lo0  # nan
    assert hi0 != hi0


def test_wilson_ci_narrows_with_more_data_same_rate():
    lo_small, hi_small = ra.wilson_ci(5, 10)
    lo_big, hi_big = ra.wilson_ci(500, 1000)
    assert (hi_big - lo_big) < (hi_small - lo_small)


def test_spearman_ic_perfect_monotonic():
    pairs = [(1, 10), (2, 20), (3, 30), (4, 40)]
    ic = ra.spearman_ic(pairs)
    assert ic == 1.0


def test_spearman_ic_perfect_inverse():
    pairs = [(1, 40), (2, 30), (3, 20), (4, 10)]
    ic = ra.spearman_ic(pairs)
    assert ic == -1.0


def test_spearman_ic_insufficient_or_constant():
    assert ra.spearman_ic([]) is None
    assert ra.spearman_ic([(1, 1)]) is None
    assert ra.spearman_ic([(1, 5), (1, 5), (1, 5)]) is None  # 분산 0


def test_score_direction_claims_hit_and_miss():
    claims = [
        {"market": "US", "date": "2026-08-13",
         "direction": {"label": "약한 상승 신호", "score100": 60}},
        {"market": "US", "date": "2026-08-14",
         "direction": {"label": "약한 하락 신호", "score100": 40}},
        {"market": "US", "date": "2026-08-17",
         "direction": {"label": "중립", "score100": 50}},  # neutral -> 제외
    ]
    cal = {"US": ["2026-08-12", "2026-08-13", "2026-08-14", "2026-08-17", "2026-08-18"]}
    # 08-13 청구: ref=08-12(100), D+1=08-13(110, 상승 -> bull 적중)
    # 08-14 청구: ref=08-13(110), D+1=08-14(90, 하락 -> bear 적중)
    idx = {("US", "2026-08-12"): 100.0, ("US", "2026-08-13"): 110.0,
           ("US", "2026-08-14"): 90.0, ("US", "2026-08-17"): 95.0}
    result = ra.score_direction_claims(claims, cal, idx)
    d1 = result[1]
    assert d1["n"] == 2
    assert d1["hits"] == 2
    assert d1["rate"] == 1.0


def test_score_direction_claims_excludes_missing_prices():
    claims = [{"market": "US", "date": "2026-08-13",
               "direction": {"label": "약한 상승 신호", "score100": 60}}]
    result = ra.score_direction_claims(claims, {"US": ["2026-08-12", "2026-08-13"]}, {})
    assert result[1]["n"] == 0
    assert result[1]["rate"] is None


def test_score_candidate_claims_forward_return_and_ic():
    cal = {"US": ["2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15", "2026-08-18"]}
    claims = [
        {"market": "US", "date": "2026-08-13", "direction": None,
         "candidates": [
             {"symbol": "AAA", "origin": "news", "score": 80},
             {"symbol": "BBB", "origin": "watch", "score": 40},
         ]},
    ]
    price = {
        ("US", "AAA", "2026-08-12"): (100.0, 100.0),
        ("US", "AAA", "2026-08-13"): (101.0, 110.0),  # D+1 close-to-close: +10%
        ("US", "BBB", "2026-08-12"): (50.0, 50.0),
        ("US", "BBB", "2026-08-13"): (49.0, 49.0),   # D+1: -2%
    }
    out = ra.score_candidate_claims(claims, cal, price)
    h1 = out["horizons"][1]
    assert h1["n"] == 2
    assert h1["hitrate"] == 0.5
    assert round(h1["mean_bps"]) == round((1000 + -200) / 2)
    # 점수 80(수익률 +) vs 40(수익률 -) -> 완전 단조 -> IC=1.0
    assert out["ic"][1] == 1.0
    assert out["by_tag"]["news"][1]["n"] == 1
    assert out["by_tag"]["watch"][1]["n"] == 1


def test_score_candidate_claims_missing_base_price_skips_symbol():
    cal = {"KR": ["2026-08-12", "2026-08-13"]}
    claims = [{"market": "KR", "date": "2026-08-13", "direction": None,
               "candidates": [{"symbol": "005930", "origin": "news", "score": 70}]}]
    out = ra.score_candidate_claims(claims, cal, {})
    assert out["horizons"][1]["n"] == 0
    assert out["horizons"][1]["mean_bps"] is None


def test_report_summary_none_latest_is_unmeasured():
    """원장이 아직 한 번도 안 돈 상태 — 지어내지 않고 "정확도 미측정"."""
    box = ra.report_summary(None)
    assert box["measured"] is False
    assert box["stance_line"] == "정확도 미측정"
    assert box["telegram_line"] == "정확도 미측정"
    for h in ra.HORIZONS:
        assert box["direction"][h] == {"n": 0, "measured": False}


def test_report_summary_below_min_n_is_unmeasured():
    """표본이 min_n 미만이면(감사 사례: D+3 n=27이 min_n=20을 넘긴 케이스와
    반대로, 여기선 넘기지 못한 값) "정확도 미측정" — 지어내지 않는다."""
    latest = {
        "as_of": "2026-09-05", "n_claims": 10, "min_n": 20,
        "direction": {1: {"n": 15, "rate": 0.6, "ci_lo": 0.4, "ci_hi": 0.8}},
        "candidates": {"horizons": {}, "ic": {}, "ic_n": {}},
    }
    box = ra.report_summary(latest)
    assert box["direction"][1] == {"n": 15, "measured": False}
    assert box["stance_line"] == "정확도 미측정"  # STANCE_HORIZON=3 은 아예 없음(n=0)
    assert box["telegram_line"] == "정확도 미측정"


def test_report_summary_stance_line_matches_owner_example():
    """소유자 예시 문구를 그대로 재현: "방향 판정 정확도(최근 n=27, D+3): 26%
    — 참고용" (감사 실측 D+3 25.9%를 반올림하면 26%)."""
    latest = {
        "as_of": "2026-09-06", "n_claims": 500, "min_n": 20,
        "direction": {
            "1": {"n": 310, "rate": 0.61, "ci_lo": 0.55, "ci_hi": 0.66},
            "3": {"n": 27, "rate": 0.259, "ci_lo": 0.132, "ci_hi": 0.447},
            "5": {"n": 24, "rate": 0.458, "ci_lo": 0.27, "ci_hi": 0.66},
        },
        "candidates": {
            "horizons": {"1": {"n": 300, "mean_bps": 14.0, "hitrate": 0.547}},
            "ic": {"1": -0.082}, "ic_n": {"1": 1116},
        },
    }
    box = ra.report_summary(latest)
    assert box["stance_line"] == "방향 판정 정확도(최근 n=27, D+3): 26% — 참고용"
    assert box["measured"] is True
    assert box["direction"][1]["rate"] == 0.61 and box["direction"][1]["measured"] is True
    assert box["ic"][1] == {"n": 1116, "measured": True, "value": -0.082}
    assert box["candidates"][1]["mean_bps"] == 14.0
    # 3/5 지평 키가 str(JSON 라운드트립)이어도 int 로 조회된다
    assert box["direction"][3]["n"] == 27 and box["direction"][5]["n"] == 24


def test_report_summary_telegram_line_skips_unmeasured_horizons():
    latest = {
        "as_of": "2026-09-06", "n_claims": 50, "min_n": 20,
        "direction": {
            1: {"n": 40, "rate": 0.55, "ci_lo": 0.4, "ci_hi": 0.7},
            3: {"n": 5, "rate": 0.4, "ci_lo": 0.1, "ci_hi": 0.8},  # n<20 -> 미측정
            5: {"n": 22, "rate": 0.45, "ci_lo": 0.25, "ci_hi": 0.65},
        },
        "candidates": {"horizons": {}, "ic": {}, "ic_n": {}},
    }
    box = ra.report_summary(latest)
    assert box["telegram_line"] == "리포트 정확도 방향 D+1 55%(n=40) · D+5 45%(n=22)"


def test_build_scorecard_and_render_markdown_smoke():
    cal = {"US": ["2026-08-12", "2026-08-13", "2026-08-14"]}
    claims = [
        {"market": "US", "date": "2026-08-13",
         "direction": {"label": "약한 상승 신호", "score100": 60},
         "candidates": [{"symbol": "AAA", "origin": "news", "score": 80}]},
    ]
    idx = {("US", "2026-08-12"): 100.0, ("US", "2026-08-13"): 105.0}
    price = {("US", "AAA", "2026-08-12"): (10.0, 10.0), ("US", "AAA", "2026-08-13"): (10.5, 11.0)}
    card = ra.build_scorecard(claims, cal, idx, price, as_of="2026-08-14")
    assert card["n_claims"] == 1
    md = ra.render_markdown(card)
    assert "리포트 정확도 스코어카드" in md
    assert "판단 불가" in md  # 표본이 20 미만이라 모든 지평이 판단 불가여야 한다
