from datetime import date, datetime

from quant.analyze.briefing import build
from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult

_AT = datetime(2026, 8, 12, 8, 0, tzinfo=KST)


def _res(key, data=None, ok=True, error=None):
    return SourceResult(key, ok, data, error, "https://x.test", _AT, 10)


def _snap(**results) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, results)


def _quotes(**syms):
    return _res("market", {
        "quotes": {s: {"label": s, "close": v, "prev": v, "change_pct": 0.0}
                   for s, v in syms.items()},
        "crosscheck": {"checked": [], "warnings": []},
    })


def _flow(**actors):
    return _res("kospi_flow", {"market": "KOSPI", "unit": "억원",
                               "rows": [{"date": "2026-08-12", **actors}]})


def _levels(items):
    return [b["level"] for b in items]


def test_empty_snapshot_yields_placeholder_not_crash():
    out = build(_snap(), {}, {"quotes": {}, "flow": {}})
    assert len(out) == 1 and out[0]["level"] == "watch"


def test_imminent_high_impact_event_is_risk():
    cal = _res("calendar", {"horizon_days": 14, "events": [
        {"name": "Consumer Price Index", "date": "2026-08-13",
         "dday": "D-1", "days_ahead": 1, "high_impact": True}]})
    out = build(_snap(calendar=cal), {}, {"quotes": {}, "flow": {}})
    # 같은 사유의 이벤트는 한 항목으로 합치고 이름은 detail 에 나열한다
    assert out[0]["level"] == "risk"
    assert "CPI" in out[0]["detail"] and "D-1" in out[0]["detail"]


def test_multiple_imminent_events_collapse_into_one_item():
    cal = _res("calendar", {"horizon_days": 14, "events": [
        {"name": "Consumer Price Index", "date": "2026-08-12",
         "dday": "D-DAY", "days_ahead": 0, "high_impact": True},
        {"name": "Producer Price Index", "date": "2026-08-13",
         "dday": "D-1", "days_ahead": 1, "high_impact": True},
        {"name": "Advance Monthly Sales for Retail and Food Services",
         "date": "2026-08-14", "dday": "D-2", "days_ahead": 2, "high_impact": True}]})
    out = build(_snap(calendar=cal), {}, {"quotes": {}, "flow": {}})
    risks = [b for b in out if "주요 지표" in b["text"]]
    assert len(risks) == 1 and "3건" in risks[0]["text"]
    # 통용 표기로 바뀌어야 한다 — 정식 영문 릴리즈명이 그대로 남으면 안 된다
    assert "CPI" in risks[0]["detail"] and "PPI" in risks[0]["detail"]
    assert "Consumer Price Index" not in risks[0]["detail"]


def test_distant_event_is_not_raised():
    cal = _res("calendar", {"horizon_days": 14, "events": [
        {"name": "Consumer Price Index", "date": "2026-08-26",
         "dday": "D-14", "days_ahead": 14, "high_impact": True}]})
    out = build(_snap(calendar=cal), {}, {"quotes": {}, "flow": {}})
    assert not any("Consumer Price Index" in b["text"] for b in out)


def test_low_impact_event_is_not_raised():
    cal = _res("calendar", {"horizon_days": 14, "events": [
        {"name": "Wholesale Trade", "date": "2026-08-13",
         "dday": "D-1", "days_ahead": 1, "high_impact": False}]})
    out = build(_snap(calendar=cal), {}, {"quotes": {}, "flow": {}})
    assert not any("Wholesale" in b["text"] for b in out)


def test_high_vix_is_risk_and_low_vix_is_watch():
    hi = build(_snap(market=_quotes(**{"^VIX": 30.0})), {}, {"quotes": {}, "flow": {}})
    lo = build(_snap(market=_quotes(**{"^VIX": 12.0})), {}, {"quotes": {}, "flow": {}})
    assert "risk" in _levels(hi)
    assert "watch" in _levels(lo)


def test_mid_vix_produces_no_volatility_item():
    out = build(_snap(market=_quotes(**{"^VIX": 18.0})), {}, {"quotes": {}, "flow": {}})
    assert not any("VIX" in b["text"] for b in out)


def test_large_foreign_flow_is_signal():
    out = build(_snap(kospi_flow=_flow(외국인=26395, 기관계=100)), {},
                {"quotes": {}, "flow": {}})
    assert any(b["level"] == "signal" and "외국인" in b["text"] for b in out)


def test_small_flow_is_not_flagged():
    out = build(_snap(kospi_flow=_flow(외국인=120, 기관계=80)), {},
                {"quotes": {}, "flow": {}})
    assert not any("외국인" in b["text"] for b in out)


def test_flow_reversal_is_signal():
    delta = {"quotes": {}, "flow": {"외국인": {"current": -900, "previous": 500,
                                              "flipped": True}}}
    out = build(_snap(), {}, delta)
    assert any("순매도 전환" in b["text"] for b in out)


def test_top_news_symbol_is_reported():
    cont = {"005930": {"name": "삼성전자", "days": 3, "articles": 11,
                       "today_articles": 11, "streak_days": 3, "is_new": False,
                       "history": [], "titles": []}}
    out = build(_snap(), cont, {"quotes": {}, "flow": {}})
    assert any("삼성전자" in b["text"] and "11건" in b["text"] for b in out)


def test_long_streak_is_reported_separately():
    cont = {"005930": {"name": "삼성전자", "days": 4, "articles": 4,
                       "today_articles": 1, "streak_days": 4, "is_new": False,
                       "history": [], "titles": []}}
    out = build(_snap(), cont, {"quotes": {}, "flow": {}})
    assert any("4일 연속" in b["text"] for b in out)


def test_crosscheck_warning_becomes_risk():
    mkt = _res("market", {"quotes": {}, "crosscheck": {
        "checked": ["^GSPC"], "warnings": ["^GSPC: yfinance 1 vs fred 2 (50% 괴리)"]}})
    out = build(_snap(market=mkt), {}, {"quotes": {}, "flow": {}})
    assert any(b["level"] == "risk" and "괴리" in b["text"] for b in out)


def test_missing_sources_are_surfaced_not_hidden():
    broken = _res("macro", ok=False, error="RuntimeError: FRED_API_KEY 미설정")
    out = build(_snap(macro=broken), {}, {"quotes": {}, "flow": {}})
    assert any("결측" in b["text"] and "macro" in b["detail"] for b in out)


def test_failed_source_data_is_never_read():
    """ok=False 인데 data 가 남아 있어도 읽으면 안 된다 — 오염된 값이다."""
    poisoned = SourceResult(
        "kospi_flow", False, {"rows": [{"date": "x", "외국인": 99999}]},
        "boom", "https://x.test", _AT, 10,
    )
    out = build(_snap(kospi_flow=poisoned), {}, {"quotes": {}, "flow": {}})
    assert not any("99,999" in b["text"] for b in out)


# ── 스탠스 한 줄 ──────────────────────────────────────────────

from quant.analyze.briefing import stance  # noqa: E402


def _cal(name="Consumer Price Index", days=0, hi=True):
    return _res("calendar", {"horizon_days": 14, "events": [
        {"name": name, "date": "2026-08-12", "dday": "D-DAY" if days == 0 else f"D-{days}",
         "days_ahead": days, "high_impact": hi}]})


def _anchors(**pcts):
    return _res("market", {
        "quotes": {}, "anchors": {
            f"{c}.KS": {"label": n, "symbol": c, "close": 1000.0, "prev": 1000.0,
                        "change_pct": p}
            for c, (n, p) in pcts.items()},
        "crosscheck": {"checked": [], "warnings": []}})


def test_stance_is_neutral_when_nothing_crosses_threshold():
    s = stance(_snap(), {}, {"quotes": {}, "flow": {}})
    assert s["label"] == "중립" and s["score"] == 0 and s["score100"] == 50


def test_stance_turns_aggressive_on_stacked_positives():
    s = stance(_snap(market=_quotes(**{"^KS11": 0.0, "^VIX": 12.0}),
                     kospi_flow=_flow(외국인=26395, 기관계=7887)),
               {}, {"quotes": {}, "flow": {}})
    assert s["label"] in ("약한 상승 신호", "강한 상승 신호") and s["score"] >= 2


def test_stance_turns_defensive_on_stacked_negatives():
    mkt = _res("market", {"quotes": {
        "^KS11": {"label": "KOSPI", "close": 1.0, "prev": 1.0, "change_pct": -3.0},
        "^VIX": {"label": "VIX", "close": 30.0, "prev": 30.0, "change_pct": 0.0}},
        "crosscheck": {"checked": [], "warnings": []}})
    s = stance(_snap(market=mkt, kospi_flow=_flow(외국인=-26395, 기관계=-100)),
               {}, {"quotes": {}, "flow": {}})
    assert s["label"] in ("약한 하락 신호", "강한 하락 신호") and s["score"] <= -2


def test_imminent_event_overrides_action_even_when_aggressive():
    s = stance(_snap(market=_quotes(**{"^KS11": 0.0, "^VIX": 12.0}),
                     kospi_flow=_flow(외국인=26395, 기관계=7887), calendar=_cal()),
               {}, {"quotes": {}, "flow": {}})
    assert "발표를 확인한 뒤" in s["line"] and "CPI" in s["line"]


def test_distant_event_does_not_force_wait():
    s = stance(_snap(market=_quotes(**{"^KS11": 0.0, "^VIX": 12.0}),
                     kospi_flow=_flow(외국인=26395, 기관계=7887), calendar=_cal(days=5)),
               {}, {"quotes": {}, "flow": {}})
    assert "발표를 확인한 뒤" not in s["line"]


def test_stance_line_cites_actual_numbers():
    s = stance(_snap(kospi_flow=_flow(외국인=26395, 기관계=100)), {},
               {"quotes": {}, "flow": {}})
    assert "26,395" in s["line"]


def test_anchor_strength_counts_toward_stance():
    s = stance(_snap(market=_anchors(**{"005930": ("삼성전자", 7.1),
                                        "000660": ("SK하이닉스", 5.5)})),
               {}, {"quotes": {}, "flow": {}})
    assert s["score"] == 1 and any("삼성전자" in p for p in s["positives"])


def test_stance_never_reads_failed_sources():
    poisoned = SourceResult("kospi_flow", False,
                            {"rows": [{"date": "x", "외국인": 99999, "기관계": 99999}]},
                            "boom", "https://x.test", _AT, 10)
    s = stance(_snap(kospi_flow=poisoned), {}, {"quotes": {}, "flow": {}})
    assert s["score"] == 0 and "99,999" not in s["line"]


# ── 시계열 이상치 가드 ────────────────────────────────────────

from quant.analyze.briefing import INDEX_DAILY_LIMIT_PCT, implausible_moves  # noqa: E402


def _q(sym, label, hist):
    return {sym: {"label": label, "close": hist[-1] if hist else None,
                  "prev": hist[-2] if len(hist) > 1 else None,
                  "change_pct": 0.0, "history": hist}}


def test_normal_index_series_is_clean():
    assert implausible_moves(_q("^KS11", "KOSPI", [100.0, 102.0, 101.0, 103.0])) == []


def test_impossible_daily_move_is_flagged():
    out = implausible_moves(_q("^KS11", "KOSPI", [100.0, 118.0, 117.0]))
    assert len(out) == 1 and "KOSPI" in out[0] and "+18.0%" in out[0]


def test_flag_reports_the_largest_move():
    out = implausible_moves(_q("^KS11", "KOSPI", [100.0, 112.0, 90.0]))
    assert "-19.6%" in out[0]


def test_just_under_threshold_is_not_flagged():
    """임계 바로 아래는 통과. 정확히 임계값은 부동소수 오차 영역이라 시험하지 않는다."""
    hist = [100.0, 100.0 * (1 + (INDEX_DAILY_LIMIT_PCT - 0.5) / 100)]
    assert implausible_moves(_q("^KS11", "KOSPI", hist)) == []


def test_just_over_threshold_is_flagged():
    hist = [100.0, 100.0 * (1 + (INDEX_DAILY_LIMIT_PCT + 0.5) / 100)]
    assert len(implausible_moves(_q("^KS11", "KOSPI", hist))) == 1


def test_non_index_symbols_are_ignored():
    """개별 종목·원자재·크립토는 하루 10% 이상 움직일 수 있다 — 지수만 본다."""
    assert implausible_moves(_q("BTC-USD", "비트코인", [100.0, 130.0])) == []


def test_short_or_missing_history_is_ignored():
    assert implausible_moves(_q("^KS11", "KOSPI", [100.0])) == []
    assert implausible_moves({"^KS11": {"label": "KOSPI"}}) == []


def test_zero_price_does_not_divide_by_zero():
    assert implausible_moves(_q("^KS11", "KOSPI", [0.0, 100.0, 101.0])) == []


def test_label_100_covers_full_ordinal_scale_for_stance():
    from quant.analyze.scoring import label_100
    assert label_100(100, "상승 신호", "하락 신호") == "강한 상승 신호"
    assert label_100(60, "상승 신호", "하락 신호") == "약한 상승 신호"
    assert label_100(50, "상승 신호", "하락 신호") == "중립"
    assert label_100(40, "상승 신호", "하락 신호") == "약한 하락 신호"
    assert label_100(0, "상승 신호", "하락 신호") == "강한 하락 신호"


def test_stance_label_matches_label_100_for_score100():
    from quant.analyze.scoring import label_100
    s = stance(_snap(kospi_flow=_flow(외국인=26395, 기관계=7887)), {},
               {"quotes": {}, "flow": {}})
    assert s["label"] == label_100(s["score100"], "상승 신호", "하락 신호")


# ── 조사 처리 ────────────────────────────────────────────────

from quant.analyze.briefing import josa  # noqa: E402


def test_josa_picks_by_final_consonant():
    assert josa("오늘", "이", "가") == "이"      # ㄹ 받침
    assert josa("모레", "이", "가") == "가"      # 받침 없음
    assert josa("순매수", "이", "가") == "가"


def test_josa_treats_non_hangul_as_open_syllable():
    """'D-8이'보다 'D-8가'가 자연스럽다."""
    assert josa("D-8", "이", "가") == "가"
    assert josa("CPI", "이", "가") == "가"
    assert josa("", "이", "가") == "가"


def test_stance_line_uses_correct_particle():
    cal = _res("calendar", {"horizon_days": 14, "events": [
        {"name": "Consumer Price Index", "date": "2026-08-12",
         "dday": "오늘", "days_ahead": 0, "high_impact": True}]})
    s = stance(_snap(kospi_flow=_flow(외국인=26395, 기관계=7887), calendar=cal),
               {}, {"quotes": {}, "flow": {}})
    assert "오늘이 걸린다" in s["line"] and "오늘가" not in s["line"]
