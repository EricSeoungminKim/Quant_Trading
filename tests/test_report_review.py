"""report_review 순수 함수 테스트 — 합성 데이터. 실가격 조회 없음."""
from __future__ import annotations

from quant.control import report_review as rr

CAL = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"]


def test_mentioned_candidates_filters_to_auto_watch_set():
    payload = {
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "ai_score100": 57,
             "trending_score100": 72, "origin": "news+anchor", "upside_pct": 91.4,
             "sector": "반도체", "change_pct": -8.7, "news_articles_today": 25},
            {"symbol": "000660", "name": "SK하이닉스", "ai_score100": 40,
             "origin": "news", "upside_pct": None},
        ],
    }
    out = rr.mentioned_candidates(payload, candidate_symbols={"005930"})
    assert [m["symbol"] for m in out] == ["005930"]
    assert out[0]["name"] == "삼성전자"
    assert out[0]["ai_score100"] == 57


def test_close_bet_candidates_joins_reasons():
    payload = {"close_bet_view": [
        {"symbol": "009150", "name": "삼성전기", "score": 3,
         "reasons": ["당일 +4.0%", "거래대금 3위"], "change_pct": 4.02,
         "trading_amount": 19532429000},
    ]}
    out = rr.close_bet_candidates(payload)
    assert out[0]["reason"] == "당일 +4.0% · 거래대금 3위"
    assert out[0]["trading_amount"] == 19532429000


def test_close_bet_candidates_empty_reasons_gives_none():
    payload = {"close_bet_view": [{"symbol": "X", "reasons": []}]}
    out = rr.close_bet_candidates(payload)
    assert out[0]["reason"] is None


def test_sector_focus_extracts_us_kr_bridge_and_empty_when_missing():
    payload = {"us_kr_bridge": {"focus": [
        {"us_name": "Tesla", "kr_sectors": ["2차전지"], "stocks": [{"code": "373220"}]},
    ]}}
    out = rr.sector_focus(payload)
    assert out[0]["us_name"] == "Tesla"
    assert rr.sector_focus({}) == []


def test_generation_timing_flag_on_time_late_and_unknown():
    assert rr.generation_timing_flag("2026-08-25T07:39:11+09:00", "KR") == "on_time"
    assert rr.generation_timing_flag("2026-08-25T17:54:23+09:00", "KR") == "late"
    assert rr.generation_timing_flag("2026-08-25T19:39:15+09:00", "US") == "on_time"
    assert rr.generation_timing_flag("2026-08-25T07:39:15+09:00", "US") == "late"
    assert rr.generation_timing_flag(None, "KR") == "unknown"
    assert rr.generation_timing_flag("not-a-date", "KR") == "unknown"


def test_index_moves_computes_oc0_cc0_cc1():
    prices = {
        "2026-08-12": (100.0, 98.0),   # prev close
        "2026-08-13": (99.0, 103.0),   # session_date: open 99, close 103
        "2026-08-14": (103.0, 106.0),  # next day
    }
    out = rr.index_moves(prices, CAL, "2026-08-13")
    assert out["oc0_bps"] == (103.0 - 99.0) / 99.0 * 10_000
    assert out["cc0_bps"] == (103.0 - 98.0) / 98.0 * 10_000
    assert out["cc1_bps"] == (106.0 - 103.0) / 103.0 * 10_000


def test_index_moves_missing_prices_are_none_not_zero():
    out = rr.index_moves({}, CAL, "2026-08-13")
    assert out["oc0_bps"] is None
    assert out["cc0_bps"] is None
    assert out["cc1_bps"] is None


def test_symbol_moves_same_shape_as_index_moves():
    prices = {"2026-08-12": (10.0, 10.0), "2026-08-13": (10.0, 11.0)}
    out = rr.symbol_moves(prices, CAL, "2026-08-13")
    assert out["oc0_bps"] == (11.0 - 10.0) / 10.0 * 10_000
    assert out["cc0_bps"] == (11.0 - 10.0) / 10.0 * 10_000
    assert out["cc1_bps"] is None  # no D+1 price


def test_rank_within_universe_orders_by_abs_desc_and_skips_missing():
    moves = {"A": 100.0, "B": -500.0, "C": None, "D": 10.0}
    ranks = rr.rank_within_universe(moves)
    assert ranks["B"] == (1, 3)
    assert ranks["A"] == (2, 3)
    assert ranks["D"] == (3, 3)
    assert "C" not in ranks


def test_stance_verdict_hit_and_miss():
    idx = {"cc0_bps": 50.0}
    hit = rr.stance_verdict({"label": "강한 상승 신호", "score100": 80}, idx)
    assert hit["hit"] is True
    miss = rr.stance_verdict({"label": "강한 하락 신호", "score100": 10}, idx)
    assert miss["hit"] is False


def test_stance_verdict_none_when_no_direction_or_neutral_or_missing_index():
    idx = {"cc0_bps": None}
    assert rr.stance_verdict(None, idx)["hit"] is None
    assert rr.stance_verdict({"label": "중립", "score100": 50}, {"cc0_bps": 10.0})["hit"] is None
    assert rr.stance_verdict({"label": "강한 상승 신호", "score100": 80}, idx)["hit"] is None


def test_mention_verdicts_hit_rule_is_oc0_positive():
    mentions = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    moves = {"A": {"oc0_bps": 10.0}, "B": {"oc0_bps": -5.0}, "C": {"oc0_bps": None}}
    out = rr.mention_verdicts(mentions, moves)
    by_sym = {v["symbol"]: v for v in out}
    assert by_sym["A"]["hit"] is True
    assert by_sym["B"]["hit"] is False
    assert by_sym["C"]["hit"] is None


def test_score_ic_for_day_computes_spearman_and_n():
    verdicts = [
        {"ai_score100": 90, "oc0_bps": 100.0},
        {"ai_score100": 50, "oc0_bps": -50.0},
        {"ai_score100": 70, "oc0_bps": 20.0},
        {"ai_score100": None, "oc0_bps": 5.0},  # excluded — missing score
    ]
    ic, n = rr.score_ic_for_day(verdicts, "ai_score100")
    assert n == 3
    assert ic is not None and ic > 0


def test_score_ic_for_day_empty_pairs_returns_none():
    ic, n = rr.score_ic_for_day([{"ai_score100": None, "oc0_bps": None}], "ai_score100")
    assert ic is None
    assert n == 0


def test_missed_movers_excludes_mentioned_and_sorts_by_abs_move():
    universe = {
        "A": {"oc0_bps": 500.0}, "B": {"oc0_bps": -900.0},
        "C": {"oc0_bps": 50.0}, "D": {"oc0_bps": None},
    }
    out = rr.missed_movers(universe, mentioned_symbols={"A"}, names={"B": "비"})
    syms = [r["symbol"] for r in out["top_by_move"]]
    assert syms == ["B", "C"]  # A excluded (mentioned), D excluded (no price)
    assert out["top_by_move"][0]["name"] == "비"
    assert out["turnover_computed"] is False
    assert out["top_by_turnover"] == []


def test_missed_movers_turnover_ranking_when_provided():
    universe = {"A": {"oc0_bps": 10.0}, "B": {"oc0_bps": 20.0}}
    out = rr.missed_movers(universe, mentioned_symbols=set(),
                           turnover_by_symbol={"A": 100.0, "B": 500.0})
    assert out["turnover_computed"] is True
    assert [r["symbol"] for r in out["top_by_turnover"]] == ["B", "A"]


def test_known_signal_flags_unknown_when_symbol_row_missing():
    flags = rr.known_signal_flags(None)
    assert flags == {"foreign_flow": "unknown", "rvol": "unknown",
                     "news_count": "unknown", "sector_move": "unknown"}


def test_known_signal_flags_reads_from_symbol_row():
    row = {"foreign_buy_streak": 3, "relative_volume": 2.0, "news_articles_today": 5}
    flags = rr.known_signal_flags(row)
    assert flags["foreign_flow"] is True
    assert flags["rvol"] is True
    assert flags["news_count"] is True
    assert flags["sector_move"] == "unknown"


def test_known_signal_flags_false_below_threshold():
    row = {"foreign_buy_streak": 0, "relative_volume": 0.5, "news_articles_today": 0}
    flags = rr.known_signal_flags(row)
    assert flags["foreign_flow"] is False
    assert flags["rvol"] is False
    assert flags["news_count"] is False


def test_derive_lesson_mentions_stance_hitrate_and_missed():
    card = {
        "stance_verdict": {"hit": True},
        "mention_verdicts": [{"hit": True}, {"hit": False}],
        "missed": {"top_by_move": [{"symbol": "X", "oc0_bps": 300.0}]},
    }
    lesson = rr.derive_lesson(card)
    assert "스탠스 적중" in lesson
    assert "50%" in lesson
    assert "X" in lesson


def test_derive_lesson_handles_empty_card():
    # 빈 카드도 스탠스 판정 불가 사실 자체는 말할 게 있다 — "완전히 빈 문자열"이
    # 아니라 "판정 불가"를 정직하게 낸다(지어내지 않되, 침묵하지도 않는다).
    assert rr.derive_lesson({}) == "스탠스 판정 불가(중립 또는 지수 결측)"


def test_build_card_open_session_end_to_end():
    payload = {
        "session_date": "2026-08-13", "market": "KR",
        "generated_at": "2026-08-13T07:39:00+09:00",
        "stance": {"label": "강한 상승 신호", "score100": 80, "line": "테스트"},
        "symbols": [
            {"symbol": "A", "name": "에이", "ai_score100": 90, "origin": "news"},
            {"symbol": "B", "name": "비", "ai_score100": 40, "origin": "news"},
        ],
        "us_kr_bridge": {"focus": []},
    }
    prices = {
        "A": {"2026-08-12": (10.0, 10.0), "2026-08-13": (10.0, 12.0)},
        "B": {"2026-08-12": (20.0, 20.0), "2026-08-13": (20.0, 19.0)},
    }
    index_prices = {"2026-08-12": (100.0, 100.0), "2026-08-13": (100.0, 105.0)}
    card = rr.build_card(
        date="2026-08-13", market="KR", session="open",
        open_payload=payload, close_payload=None,
        candidate_symbols={"A", "B"}, calendar=CAL, index_prices=index_prices,
        universe_symbols=payload["symbols"], price_by_symbol=prices,
        names={"A": "에이", "B": "비"},
    )
    assert card["stance_verdict"]["hit"] is True
    assert card["generation_timing"] == "on_time"
    verdicts = {v["symbol"]: v for v in card["mention_verdicts"]}
    assert verdicts["A"]["hit"] is True
    assert verdicts["B"]["hit"] is False
    assert verdicts["A"]["rank"] == (1, 2)
    md = rr.render_markdown(card)
    assert "2026-08-13 KR 리포트 회고" in md
    assert "적중" in md


def test_build_card_close_session_has_no_stance():
    close_payload = {
        "close_bet_view": [{"symbol": "C", "name": "씨", "score": 5,
                            "reasons": ["당일 +5%"]}],
    }
    prices = {"C": {"2026-08-12": (10.0, 10.0), "2026-08-13": (10.0, 11.0)}}
    index_prices = {"2026-08-12": (100.0, 100.0), "2026-08-13": (100.0, 101.0)}
    card = rr.build_card(
        date="2026-08-13", market="KR", session="close",
        open_payload=None, close_payload=close_payload,
        candidate_symbols=set(), calendar=CAL, index_prices=index_prices,
        universe_symbols=[], price_by_symbol=prices,
    )
    assert card["stance"] is None
    assert card["stance_verdict"]["hit"] is None
    md = rr.render_markdown(card)
    assert "마감판" in md
    assert "방향콜을 내지 않는다" in md


# ── Phase 6 재발 방지 루프 — 원장 압축 행 / 텔레그램 포맷 / 주간 집계 ────────

def _sample_card(*, stance_hit=False, stance_bucket="bear"):
    return {
        "date": "2026-09-05", "market": "KR", "session": "open",
        "stance_verdict": {"hit": stance_hit, "bucket": stance_bucket},
        "mention_verdicts": [
            {"symbol": f"S{i}", "hit": (i % 2 == 0), "origin": "news"} for i in range(10)
        ] + [{"symbol": "S10", "hit": True, "origin": "watch_join"},
             {"symbol": "S11", "hit": None, "origin": "news"}],  # 가격 결측 — 판정 제외
        "ic_by_score": {"ai_score100": {"ic": 0.02, "n": 25, "measured": True},
                        "trending_score100": {"ic": None, "n": 5, "measured": False}},
        "missed": {
            "universe_n": 45,
            "top_by_move": [
                {"symbol": "005930", "name": "삼성전자", "oc0_bps": 320,
                 "signals": {"rvol": True}},
                {"symbol": "000660", "name": "SK하이닉스", "oc0_bps": -210,
                 "signals": {"rvol": False}},
                {"symbol": "042700", "name": "한미반도체", "oc0_bps": 180,
                 "signals": {"rvol": True}},
            ],
        },
    }


def test_review_summary_row_computes_judged_mention_stats():
    card = _sample_card()
    row = rr.review_summary_row(card, generated_at="2026-09-06T06:44:00+09:00")
    assert row["date"] == "2026-09-05"
    assert row["market"] == "KR"
    assert row["session"] == "open"
    assert row["generated_at"] == "2026-09-06T06:44:00+09:00"
    assert row["stance_hit"] is False
    assert row["stance_bucket"] == "bear"
    # 12개 언급 중 1개(S11)는 가격 결측 -> 판정 대상 11건.
    assert row["mentions_total_n"] == 12
    assert row["mentions_n"] == 11
    # S0,S2,S4,S6,S8(짝수 5개) + S10 = 6건 적중.
    assert row["mentions_hit_n"] == 6
    assert row["mentions_hit_rate"] == 6 / 11
    assert row["mentions_by_origin"]["news"] == {"n": 10, "hit_n": 5}
    assert row["mentions_by_origin"]["watch_join"] == {"n": 1, "hit_n": 1}
    assert row["missed_n"] == 3
    assert row["missed_universe_n"] == 45
    assert row["missed_rvol_n"] == 2
    assert [s["symbol"] for s in row["missed_symbols"]] == ["005930", "000660", "042700"]


def test_review_summary_row_ic_by_score_passes_through_measured_flag():
    card = _sample_card()
    row = rr.review_summary_row(card)
    assert row["ic_by_score"]["ai_score100"] == {"ic": 0.02, "n": 25, "measured": True}
    assert row["ic_by_score"]["trending_score100"]["measured"] is False


def test_format_daily_telegram_matches_owner_example_shape():
    card = _sample_card()
    row = rr.review_summary_row(card)
    line = rr.format_daily_telegram(row)
    assert line == "🔁 회고 KR 09-05: 스탠스 ✗ · 언급 11건 적중 55% · 놓친 급등 3/10 (RVOL 신호 2)"
    # 태그 균형 — 이 줄은 평문이라 텔레그램 HTML 파서를 걸 태그가 없어야 한다.
    assert line.count("<") == line.count(">") == 0
    assert len(line.encode("utf-8")) <= 4096


def test_format_daily_telegram_stance_hit_and_close_session_and_unjudged():
    hit_row = rr.review_summary_row(_sample_card(stance_hit=True, stance_bucket="bull"))
    assert "스탠스 ✓" in rr.format_daily_telegram(hit_row)

    close_card = dict(_sample_card(), session="close", stance_verdict={"hit": None, "bucket": None})
    close_row = rr.review_summary_row(close_card)
    assert "스탠스 없음(마감판)" in rr.format_daily_telegram(close_row)
    assert " 마감:" in rr.format_daily_telegram(close_row)

    empty_row = rr.review_summary_row({
        "date": "2026-09-05", "market": "US", "session": "open",
        "stance_verdict": {}, "mention_verdicts": [], "ic_by_score": {}, "missed": {},
    })
    line = rr.format_daily_telegram(empty_row)
    assert "언급 0건" in line
    assert "판정불가" in line  # 스탠스 판정불가


def test_append_review_ledger_writes_and_skips_duplicate(tmp_path):
    from quant.apps.report_cli import _append_review_ledger

    path = tmp_path / "report_review.jsonl"
    row = {"date": "2026-09-05", "market": "KR", "session": "open", "stance_hit": False}
    assert _append_review_ledger(path, row) is True
    assert path.read_text(encoding="utf-8").count("\n") == 1

    # 같은 (date, market, session) 재시도 -> 멱등, 두번째 줄이 생기지 않는다.
    assert _append_review_ledger(path, {**row, "stance_hit": True}) is False
    assert path.read_text(encoding="utf-8").count("\n") == 1

    # 다른 session -> 새 행.
    assert _append_review_ledger(path, {**row, "session": "close"}) is True
    assert path.read_text(encoding="utf-8").count("\n") == 2


def _weekly_rows():
    return [
        {"date": "2026-09-01", "market": "KR", "session": "open", "stance_hit": True,
         "mentions_n": 10, "mentions_hit_n": 4,
         "mentions_by_origin": {"news": {"n": 10, "hit_n": 4}},
         "ic_by_score": {"ai_score100": {"ic": 0.05, "n": 22, "measured": True}},
         "missed_n": 3, "missed_universe_n": 40, "missed_rvol_n": 1,
         "missed_symbols": [{"symbol": "005930", "name": "삼성전자", "oc0_bps": 300}]},
        {"date": "2026-09-02", "market": "KR", "session": "open", "stance_hit": False,
         "mentions_n": 8, "mentions_hit_n": 2,
         "mentions_by_origin": {"news": {"n": 8, "hit_n": 2}},
         "ic_by_score": {"ai_score100": {"ic": -0.03, "n": 21, "measured": True}},
         "missed_n": 2, "missed_universe_n": 38, "missed_rvol_n": 0,
         "missed_symbols": [{"symbol": "005930", "name": "삼성전자", "oc0_bps": -120}]},
        {"date": "2026-09-02", "market": "US", "session": "open", "stance_hit": None,
         "mentions_n": 5, "mentions_hit_n": 3,
         "mentions_by_origin": {"watch_join": {"n": 5, "hit_n": 3}},
         "ic_by_score": {}, "missed_n": 0, "missed_universe_n": 20, "missed_rvol_n": 0,
         "missed_symbols": []},
    ]


def test_aggregate_weekly_stance_only_counts_open_session_with_verdict():
    agg = rr.aggregate_weekly(_weekly_rows())
    assert agg["n_cards"] == 3
    # US 행은 stance_hit=None -> 스탠스 표본에서 빠진다(KR 2건만).
    assert agg["stance"] == {"n": 2, "hit_n": 1, "hit_rate": 0.5}


def test_aggregate_weekly_mentions_and_tags_sum_across_all_sessions():
    agg = rr.aggregate_weekly(_weekly_rows())
    assert agg["mentions"]["n"] == 23
    assert agg["mentions"]["hit_n"] == 9
    assert agg["mentions_by_tag"]["news"] == {"n": 18, "hit_n": 6, "hit_rate": 6 / 18}
    assert agg["mentions_by_tag"]["watch_join"] == {"n": 5, "hit_n": 3, "hit_rate": 0.6}


def test_aggregate_weekly_ic_distribution_and_recall_and_recurring_misses():
    agg = rr.aggregate_weekly(_weekly_rows())
    ic = agg["ic_distribution"]["ai_score100"]
    assert ic["n_days"] == 2 and ic["positive_days"] == 1 and ic["negative_days"] == 1
    assert agg["recall"] == {"missed_n": 5, "universe_n": 98, "rvol_signal_n": 1}
    assert len(agg["top_recurring_misses"]) == 1
    top = agg["top_recurring_misses"][0]
    assert top["symbol"] == "005930" and top["days"] == 2
    assert top["avg_oc0_bps"] == 90.0  # (300 + -120) / 2


def test_aggregate_weekly_empty_rows_gives_unmeasured_defaults():
    agg = rr.aggregate_weekly([])
    assert agg["n_cards"] == 0
    assert agg["stance"] == {"n": 0, "hit_n": 0, "hit_rate": None}
    assert agg["mentions"] == {"n": 0, "hit_n": 0, "hit_rate": None}
    assert agg["ic_distribution"] == {}
    assert agg["top_recurring_misses"] == []


def test_render_weekly_markdown_and_telegram_smoke():
    agg = rr.aggregate_weekly(_weekly_rows())
    md = rr.render_weekly_markdown(agg, "2026-08-30 ~ 2026-09-05")
    assert "주간 집계" in md
    assert "005930" in md
    assert "Phase 2" in md  # 착수 전 — 자동으로 채워질 자리라는 안내

    line = rr.format_weekly_telegram(agg, "2026-08-30 ~ 2026-09-05")
    assert line.startswith("📅 주간 회고 2026-08-30 ~ 2026-09-05:")
    assert "005930" in line


def test_render_weekly_markdown_handles_no_data_gracefully():
    agg = rr.aggregate_weekly([])
    md = rr.render_weekly_markdown(agg, "빈 주")
    assert "판정 불가" in md
    line = rr.format_weekly_telegram(agg, "빈 주")
    assert "판정불가" in line


def test_build_report_review_block_empty_when_no_rows():
    assert rr.build_report_review_block(None) == {}
    assert rr.build_report_review_block([]) == {}


def test_build_report_review_block_summarizes_latest_and_totals():
    block = rr.build_report_review_block(_weekly_rows())
    assert block["as_of"] == "2026-09-02"  # 가장 최근 날짜
    assert block["n_cards"] == 3
    assert block["stance"]["n"] == 2
    assert block["mentions"]["n"] == 23
    assert block["recall"]["missed_n"] == 5
