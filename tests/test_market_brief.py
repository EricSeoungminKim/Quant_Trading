"""자체 리포트 페이로드 → 브리핑/후보 추출 테스트 — 전부 오프라인 순수 함수."""
import pytest

from quant.analyze.market_brief import (
    MAX_CANDIDATES,
    auto_watch_tokens,
    brief_text,
    close_brief_text,
    engine_tokens,
    foreign_flow_candidate_symbols,
    foreign_flow_tokens,
    intraday_scalp_tokens,
    is_fresh,
    rejected_tokens,
    truncated_count,
)
from quant.analyze.watch_scorer import _VALID_TAGS, _parse_token


def _kr_payload(**over):
    p = {
        "schema": 1,
        "market": "KR",
        "session_date": "2026-08-13",
        "auto_watch": "AUTO_WATCH: 005930:NEWS+RANK+NEW 000660:NEWS+RANK 058820:NEWS",
        "stance": {
            "label": "약한 상승 신호", "score": 2, "score100": 70,
            "line": "외국인 +28,354억 순매수 — 발표를 확인한 뒤 진입한다.",
            "positives": ["외국인 +28,354억 순매수", "VIX 14.8"],
            "negatives": ["PPI 오늘"],
        },
        "features": {
            "kospi_change_pct": 0.42, "kosdaq_change_pct": -0.31,
            "foreign_net_100m_krw": 28354.0, "institution_net_100m_krw": 5279.0,
            "vix": 14.79, "usdkrw": 1416.97,
        },
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "ai_score100": 64,
             "news_articles_today": 9, "news_streak_days": 1,
             "in_ranking": True, "change_pct": 4.13},
            {"symbol": "000660", "name": "SK하이닉스", "ai_score100": 71,
             "news_articles_today": 6, "news_streak_days": 3,
             "in_ranking": True, "change_pct": 0.35},
        ],
        "missing": [],
    }
    p.update(over)
    return p


# --- 신선도 ---

def test_yesterdays_report_is_not_fresh():
    """어제 리포트로 오늘 종목을 편입하면 에러 없이 틀린 종목이 들어간다."""
    assert is_fresh(_kr_payload(), "2026-08-13")
    assert not is_fresh(_kr_payload(), "2026-08-14")


def test_missing_session_date_is_not_fresh():
    assert not is_fresh({"auto_watch": "AUTO_WATCH: 005930"}, "2026-08-13")


# --- 후보 추출 ---

def test_tokens_keep_their_tags():
    """태그는 그대로 실려야 한다 — news_momentum의 EVENT 게이트가 태그로 동작한다."""
    assert auto_watch_tokens(_kr_payload(), "KR") == [
        "005930:NEWS+RANK+NEW", "000660:NEWS+RANK", "058820:NEWS",
    ]


def test_us_tickers_in_a_kr_report_are_rejected():
    """리포트의 시장 필터가 깨져도 관심종목 파일까지 흘러들면 안 된다."""
    p = _kr_payload(auto_watch="AUTO_WATCH: 005930:NEWS NVDA:NEWS AAPL")
    assert auto_watch_tokens(p, "KR") == ["005930:NEWS"]
    assert rejected_tokens(p, "KR") == ["NVDA:NEWS", "AAPL"]


def test_kr_codes_in_a_us_report_are_rejected():
    p = {"auto_watch": "AUTO_WATCH: NVDA:NEWS+RANK 005930:NEWS BRK.B"}
    assert auto_watch_tokens(p, "US") == ["NVDA:NEWS+RANK", "BRK.B"]
    assert rejected_tokens(p, "US") == ["005930:NEWS"]


def test_malformed_tokens_are_dropped():
    """주입 형태(공백/세미콜론/경로)가 토큰으로 통과하면 안 된다."""
    p = {"auto_watch": "AUTO_WATCH: 005930:NEWS ../../etc 000|660 005930;rm 123456:BAD_TAG!"}
    assert auto_watch_tokens(p, "KR") == ["005930:NEWS"]


def test_duplicate_symbols_collapse_to_first():
    p = {"auto_watch": "AUTO_WATCH: 005930:NEWS 005930:RANK 000660:NEWS"}
    assert auto_watch_tokens(p, "KR") == ["005930:NEWS", "000660:NEWS"]


def test_cap_keeps_the_best_corroborated_not_the_first_listed():
    """실측 2026-08-13: US 11개 중 랭킹에 오른 INTC·SMCI가 랭킹 없는
    AAPL·AMZN·BA에 밀려 잘렸다 — 리포트 순서대로 자르면 가장 좋은 걸 버린다."""
    p = {
        "auto_watch": ("AUTO_WATCH: NVDA:NEWS+RANK AAPL:NEWS AMZN:NEWS BA:NEWS "
                       "BAC:NEWS GS:NEWS INTC:NEWS+RANK SMCI:NEWS+RANK"),
        "symbols": [
            {"symbol": "NVDA", "trending_score100": 80, "news_articles_today": 5},
            {"symbol": "AAPL", "trending_score100": 50, "news_articles_today": 2},
            {"symbol": "AMZN", "trending_score100": 50, "news_articles_today": 2},
            {"symbol": "BA", "trending_score100": 50, "news_articles_today": 2},
            {"symbol": "BAC", "trending_score100": 50, "news_articles_today": 2},
            {"symbol": "GS", "trending_score100": 50, "news_articles_today": 2},
            {"symbol": "INTC", "trending_score100": 75, "news_articles_today": 4},
            {"symbol": "SMCI", "trending_score100": 70, "news_articles_today": 3},
        ],
    }
    kept = {t.split(":")[0] for t in auto_watch_tokens(p, "US")}
    assert {"NVDA", "INTC", "SMCI"} <= kept, "트렌딩 확인된 종목이 잘렸다"
    assert len(kept) == MAX_CANDIDATES["US"]


def test_kept_tokens_keep_report_order():
    """근거로 고르되 출력 순서는 리포트 순서를 유지한다 — 나란히 대조할 수 있게."""
    p = {
        "auto_watch": "AUTO_WATCH: AAA:NEWS BBB:NEWS CCC:NEWS DDD:NEWS EEE:NEWS FFF:NEWS",
        "symbols": [
            {"symbol": "AAA", "trending_score100": 10}, {"symbol": "BBB", "trending_score100": 90},
            {"symbol": "CCC", "trending_score100": 80}, {"symbol": "DDD", "trending_score100": 70},
            {"symbol": "EEE", "trending_score100": 60}, {"symbol": "FFF", "trending_score100": 95},
        ],
    }
    assert auto_watch_tokens(p, "US") == ["BBB:NEWS", "CCC:NEWS", "DDD:NEWS",
                                          "EEE:NEWS", "FFF:NEWS"]


def test_cap_ordering_is_deterministic_on_ties():
    p = {
        "auto_watch": "AUTO_WATCH: ZZZ:NEWS AAA:NEWS MMM:NEWS",
        "symbols": [{"symbol": s, "trending_score100": 50, "news_articles_today": 1}
                    for s in ("ZZZ", "AAA", "MMM")],
    }
    import dataclasses  # noqa: F401  (임포트 부작용 없음 — 순수 재호출 비교용)
    first = auto_watch_tokens({**p}, "US")
    assert first == auto_watch_tokens({**p}, "US")


@pytest.mark.parametrize("market", ["KR", "US"])
def test_cap_is_enforced_here_too(market):
    """캡을 셸 한 곳에만 두면 그 한 곳이 뚫렸을 때 막을 것이 없다."""
    syms = ([f"{i:06d}" for i in range(1, 40)] if market == "KR"
            else [f"SYM{c}" for c in "ABCDEFGHIJKLMNOP"])
    p = {"auto_watch": "AUTO_WATCH: " + " ".join(syms)}
    assert len(auto_watch_tokens(p, market)) == MAX_CANDIDATES[market]


def test_no_auto_watch_line_yields_nothing_without_crashing():
    assert auto_watch_tokens({}, "KR") == []
    assert auto_watch_tokens({"auto_watch": "AUTO_WATCH: "}, "KR") == []
    assert auto_watch_tokens({"auto_watch": "쓰레기"}, "KR") == []


def test_truncated_overflow_is_not_reported_as_rejected():
    """상한 절단은 '탈락'이 아니다 — 형식 위반과 섞어 세면 경보가 무의미해진다."""
    p = {"auto_watch": "AUTO_WATCH: " + " ".join(f"{i:06d}" for i in range(1, 40))}
    assert rejected_tokens(p, "KR") == []
    assert truncated_count(p, "KR") == 39 - MAX_CANDIDATES["KR"]


def test_silent_truncation_is_made_visible():
    """US 캡은 5인데 리포트가 6개를 내는 일이 실제로 있었다(2026-08-13 실측).

    잘린 사실이 안 보이면 사용자는 리포트가 5개만 냈다고 오해한다.
    """
    p = {"auto_watch": "AUTO_WATCH: BA:NEWS INTC:NEWS LITE:NEWS NDAQ:NEWS NVDA:NEWS AAPL:NEWS"}
    assert truncated_count(p, "US") == 1
    assert "1개 잘림" in brief_text(p, "US")


def test_no_truncation_notice_when_nothing_was_cut():
    assert truncated_count(_kr_payload(), "KR") == 0
    assert "잘림" not in brief_text(_kr_payload(), "KR")


# --- 리포트 어휘 → 엔진 어휘 번역 ---
# 이 블록이 지키는 것: 번역이 끊기면 news_momentum이 리포트 종목을 영영 진입시키지
# 못한다 — 에러도 경고도 없이. 2026-08-13에 실제로 그 상태였다.

def test_report_tags_are_translated_to_engine_profiles():
    assert engine_tokens(_kr_payload(), "KR") == [
        "005930:TREND+EVENT:20260813",
        "000660:TREND+EVENT:20260813",
        "058820:EVENT:20260813",
    ]


def test_translated_tags_survive_the_engine_parser():
    """번역 결과가 watch_scorer._parse_token에서 무태그로 강등되면 안 된다.

    이 회귀가 원래 버그였다 — 리포트의 NEWS/RANK/NEW는 _VALID_TAGS에 없어서
    '알 수 없는 태그'로 전체가 강등됐다.
    """
    for token in engine_tokens(_kr_payload(), "KR"):
        symbol, tags, report_date, reasons = _parse_token(token)
        assert tags, f"{token} 이 무태그로 강등됐다"
        assert all(t in _VALID_TAGS for t in tags)
        assert "알 수 없는 태그" not in reasons
        assert report_date is not None


def test_raw_report_tokens_would_be_demoted_by_the_engine():
    """번역이 필요한 이유를 고정한다 — 리포트 원문 토큰은 실제로 강등된다."""
    _, tags, _, reasons = _parse_token("005930:NEWS+RANK+NEW")
    assert tags == [] and "알 수 없는 태그" in reasons


def test_event_carries_the_session_date_but_trend_alone_does_not():
    """EVENT 프로필은 신선도로 30/100점을 매기므로 날짜가 없으면 구조적으로 손해다.

    TREND 프로필은 날짜를 안 보므로 의미 없는 필드를 실어보내지 않는다.
    """
    p = _kr_payload(auto_watch="AUTO_WATCH: 005930:RANK 000660:NEWS")
    assert engine_tokens(p, "KR") == ["005930:TREND", "000660:EVENT:20260813"]


def test_evidence_only_tags_produce_an_untagged_token():
    """STREAK/NEW 는 증거일 뿐 채점 프로필이 아니다 — 무태그 best-of로 보낸다."""
    p = _kr_payload(auto_watch="AUTO_WATCH: 005930:STREAK+NEW")
    assert engine_tokens(p, "KR") == ["005930"]


def test_translation_is_deterministic_in_tag_order():
    a = engine_tokens(_kr_payload(auto_watch="AUTO_WATCH: 005930:RANK+NEWS"), "KR")
    b = engine_tokens(_kr_payload(auto_watch="AUTO_WATCH: 005930:NEWS+RANK"), "KR")
    assert a == b == ["005930:TREND+EVENT:20260813"]


def test_missing_session_date_omits_the_date_suffix():
    p = {"auto_watch": "AUTO_WATCH: 005930:NEWS"}
    assert engine_tokens(p, "KR") == ["005930:EVENT"]


def test_brief_shows_the_translated_tokens():
    """번역이 조용히 끊기지 않게 알림에 남긴다."""
    assert "엔진 토큰: 005930:TREND+EVENT:20260813" in brief_text(_kr_payload(), "KR")


# --- 브리핑 본문 ---

def test_brief_has_score_stance_and_evidence():
    t = brief_text(_kr_payload(), "KR", url="http://x/KR_report.html")
    assert "70점/100점" in t and "약한 상승 신호" in t
    assert "PPI 오늘" in t
    assert "삼성전자(005930)" in t
    assert "http://x/KR_report.html" in t


def test_every_number_carries_a_unit():
    """단위 없는 숫자는 신뢰할 수 없다(2026-08-12 사용자 원칙)."""
    t = brief_text(_kr_payload(), "KR")
    assert "코스피 +0.42%" in t and "코스닥 -0.31%" in t
    assert "외국인 +28,354억" in t and "기관 +5,279억" in t
    assert "VIX 14.8" in t and "원/달러 1,417원" in t


def test_trending_shows_which_board_and_what_rank():
    """'언급됐다'와 '실제로 거래가 몰린다'는 다르다(2026-08-13 사용자 지시)."""
    p = _kr_payload(symbols=[{
        "symbol": "005930", "name": "삼성전자", "ai_score100": 64,
        "trending_score100": 68, "news_articles_today": 9,
        "trending_boards": {"거래대금": 1, "토스 사용자 거래대금": 3},
        "relative_volume": 2.4,
    }])
    t = brief_text(p, "KR")
    assert "트렌딩 68점" in t
    assert "거래대금 1위" in t and "토스 사용자 거래대금 3위" in t
    assert "RVOL 2.4배" in t


def test_symbol_without_trending_data_still_renders():
    p = _kr_payload(symbols=[{"symbol": "003540", "name": "대웅", "ai_score100": 50,
                              "news_articles_today": 2, "trending_boards": {}}])
    t = brief_text(p, "KR")
    assert "대웅(003540)" in t and "위" not in t.split("🔎")[1].split("\n")[1]


def test_outlet_concentration_is_visible():
    """한 매체가 대부분이면 '여러 곳이 다뤘다'가 아니라 '한 곳이 여러 번 썼다'다."""
    p = _kr_payload(news_diversity={
        "total_articles": 100, "distinct_outlets": 12,
        "by_outlet": {"매일경제": 60, "뉴시스": 40},
    })
    t = brief_text(p, "KR")
    assert "뉴스 100건 · 매체 12곳" in t
    assert "최다 매일경제 60건 · 60%" in t


def test_missing_news_diversity_omits_the_line():
    assert "📰" not in brief_text(_kr_payload(), "KR")


def test_missing_sources_are_surfaced_not_hidden():
    t = brief_text(_kr_payload(missing=["naver_flow", "calendar"]), "KR")
    assert "결측 소스 2개" in t and "naver_flow" in t


def test_missing_features_drop_the_line_without_crashing():
    """리포트 스키마가 바뀌어도 브리핑이 죽지 않는다 — 없으면 그 줄만 빠진다."""
    t = brief_text({"session_date": "2026-08-13", "stance": {}, "features": {}}, "KR")
    assert "한국장" in t and "자동 편입 후보: 없음" in t


def test_us_brief_uses_us_indicators():
    p = {
        "session_date": "2026-08-13", "market": "US",
        "stance": {"score100": 50, "label": "중립", "line": "VIX 14.8로 우호적이다."},
        "features": {"sp500_change_pct": 0.281, "net_liquidity_musd": 5841241.35,
                     "vix": 14.79, "usdkrw": 1416.97},
        "auto_watch": "AUTO_WATCH: NVDA:NEWS+RANK",
    }
    t = brief_text(p, "US")
    assert "미국장" in t and "S&P500 +0.28%" in t
    assert "순유동성 5.84조 달러" in t
    assert "코스피" not in t


def test_symbols_are_ranked_by_score_before_truncation():
    """상위 N만 자르는 이상 정렬을 리포트의 계약에 의존하지 않는다."""
    t = brief_text(_kr_payload(), "KR", max_symbols=1)
    assert "SK하이닉스" in t and "삼성전자(" not in t


def test_rejected_tokens_are_reported_in_the_brief():
    p = _kr_payload(auto_watch="AUTO_WATCH: 005930:NEWS NVDA:NEWS")
    t = brief_text(p, "KR")
    assert "제외 1개" in t and "NVDA:NEWS" in t


def test_brief_fits_in_a_telegram_message():
    big = _kr_payload(
        symbols=[{"symbol": f"{i:06d}", "name": "종목" * 10, "ai_score100": 50,
                  "news_articles_today": 3} for i in range(200)],
        missing=[f"source_{i}" for i in range(50)],
    )
    assert len(brief_text(big, "KR")) <= 3500


# ---------------------------------------------------------------------------
# 서브프로젝트 T (2026-08-17) — EVENT_SCALP/FRGN/FRGN_EXIT 태그 배선
# ---------------------------------------------------------------------------

def test_intraday_scalp_tokens_reads_close_payload_intraday_view():
    payload = {"intraday_view": [
        {"symbol": "005930", "name": "삼성전자", "score100": 72},
        {"symbol": "000660", "name": "SK하이닉스", "score100": 65},
    ]}
    assert intraday_scalp_tokens(payload) == ["005930:EVENT_SCALP", "000660:EVENT_SCALP"]


def test_intraday_scalp_tokens_is_a_safe_noop_on_morning_payload():
    """아침 payload는 intraday_view 키 자체가 없다(실제 경로 추적 결과, market_brief.py
    "서브프로젝트 T" 절 참고) — 빈 리스트로 안전하게 저하돼야 한다."""
    assert intraday_scalp_tokens(_kr_payload()) == []
    assert intraday_scalp_tokens({}) == []


def test_intraday_scalp_tokens_ignores_malformed_items():
    assert intraday_scalp_tokens({"intraday_view": [{"score100": 50}, "005930", None]}) == []


def test_foreign_flow_tokens_maps_inflow_to_frgn_and_outflow_to_frgn_exit():
    from quant.analyze.foreign_trend import LABEL_INFLOW, LABEL_NEUTRAL, LABEL_OUTFLOW_TREND

    frgn, frgn_exit = foreign_flow_tokens({
        "005930": LABEL_INFLOW,
        "000660": LABEL_OUTFLOW_TREND,
        "058820": LABEL_NEUTRAL,
    })
    assert frgn == ["005930:FRGN"]
    assert frgn_exit == ["000660"]


def test_foreign_flow_tokens_watch_labels_trigger_neither():
    """단발 이탈(잔여 있음)/관망(하루 더)은 매수도 이탈도 아니다 — 신규/갱신 어느
    쪽도 트리거하지 않는다(foreign_trend.py 모듈 docstring)."""
    from quant.analyze.foreign_trend import LABEL_WATCH_ANOTHER_DAY, LABEL_WATCH_RESIDUAL

    frgn, frgn_exit = foreign_flow_tokens({
        "005930": LABEL_WATCH_RESIDUAL, "000660": LABEL_WATCH_ANOTHER_DAY,
    })
    assert frgn == [] and frgn_exit == []


def test_foreign_flow_tokens_output_is_sorted_for_determinism():
    from quant.analyze.foreign_trend import LABEL_INFLOW

    frgn, _ = foreign_flow_tokens({"005930": LABEL_INFLOW, "000660": LABEL_INFLOW})
    assert frgn == ["000660:FRGN", "005930:FRGN"]


def test_foreign_flow_candidate_symbols_is_kr_only():
    p = {"auto_watch": "AUTO_WATCH: NVDA:NEWS"}
    assert foreign_flow_candidate_symbols(p, "US") == set()


def test_foreign_flow_candidate_symbols_unions_auto_watch_and_intraday_view():
    p = {
        "auto_watch": "AUTO_WATCH: 005930:NEWS 000660:RANK",
        "intraday_view": [{"symbol": "058820"}],
    }
    assert foreign_flow_candidate_symbols(p, "KR") == {"005930", "000660", "058820"}


def test_close_brief_text_shows_intraday_candidates_not_auto_watch_none():
    """close payload에는 auto_watch가 없다 — brief_text()를 그대로 쓰면 후보가 있어도
    '없음'이 찍힌다. close_brief_text()는 실제 스키마(intraday_view)를 읽어야 한다."""
    payload = {
        "session_date": "2026-08-17",
        "intraday_view": [{"symbol": "005930", "name": "삼성전자", "score100": 72}],
        "market_flow": {"foreign_net": 120.5, "institution_net": -30.0},
        "missing": [],
    }
    t = close_brief_text(payload, "KR")
    assert "마감 포지션 브리핑" in t
    assert "삼성전자(005930) 72점" in t
    assert "외국인 +120억" in t and "기관 -30억" in t


def test_close_brief_text_no_candidates_says_so_not_empty_auto_watch():
    t = close_brief_text({"session_date": "2026-08-17", "missing": []}, "KR")
    assert "당일 단타 후보 없음" in t
