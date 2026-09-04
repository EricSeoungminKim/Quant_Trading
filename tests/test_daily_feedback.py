"""일일 피드백(`quant/control/daily_feedback.py`) — 2026-08-26 소유자 조직도 역할 5.

> "오늘 만든 거래에 당시 들어갈 때 트레이더가 남겨둔 진입 시그널이 어디에서
> 나온 건지 당시 상황을 보면서... 진입을 너무 늦게 함 고점매수, 거래량이
> 높았던 종목인데 막상 거래가 끊겼을 때 들어감."

여기서 지키는 계약:
1. **문제를 지어내지 않는다** — 표본(전 봉 5개 미만)이 적으면 판정하지 않는다.
2. **look-ahead 없음** — "진입 시점까지"만 보는 규칙(고점매수·늦은진입)은 진입
   이후 봉을 쓰지 않는다. "이후 세션" 규칙(고점매수의 MFE)은 사후 피드백이라
   의도적으로 미래 봉을 본다(forensics의 mfe_session_bp와 같은 성격).
3. **임계는 [미검증 초기값]** — 렌더 텍스트 푸터에 그 사실을 명시한다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from quant.control.daily_feedback import (
    already_recorded,
    entry_timing_findings,
    render_feedback_text,
    strategy_feedback,
    todays_round_trips,
)

T0 = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)  # 세션 시작(예: KR 09:00 KST)


def _bars(closes, volumes=None, highs=None, lows=None, start=T0):
    n = len(closes)
    idx = pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(n)])
    return pd.DataFrame({
        "open": closes,
        "high": highs if highs is not None else closes,
        "low": lows if lows is not None else closes,
        "close": closes,
        "volume": volumes if volumes is not None else [100.0] * n,
    }, index=idx)


# ── entry_timing_findings: 고점매수 ─────────────────────────────────────

def test_high_range_entry_that_stalls_is_flagged():
    # 0~9분: 90~100 레인지, 10분째 진입가 100(레인지 위치 1.0).
    # 이후(after) 최고가 100.2 -> MFE = 20bp < 30bp 문턱 -> 고점매수 플래그.
    closes = [90.0, 92.0, 95.0, 91.0, 98.0, 93.0, 96.0, 94.0, 99.0, 100.0]
    after = [100.0, 100.1, 100.2, 100.1, 100.0]
    bars = _bars(closes + after)
    entry_ts = T0 + timedelta(minutes=9)
    findings = entry_timing_findings(entry_ts, 100.0, bars)
    assert any("고점매수" in f for f in findings)


def test_high_range_entry_that_keeps_running_is_not_flagged():
    """레인지 위치는 높지만 이후 계속 올랐다 — 고점매수로 오판하면 안 된다."""
    closes = [90.0, 92.0, 95.0, 91.0, 98.0, 93.0, 96.0, 94.0, 99.0, 100.0]
    after = [101.0, 105.0, 110.0, 115.0, 120.0]  # 진입가의 +0.3% 훌쩍 초과
    bars = _bars(closes + after)
    entry_ts = T0 + timedelta(minutes=9)
    findings = entry_timing_findings(entry_ts, 100.0, bars)
    assert not any("고점매수" in f for f in findings)


def test_low_range_entry_is_not_flagged_even_if_it_stalls():
    """레인지 하단에서 진입했다면 정체돼도 "고점매수"는 아니다."""
    closes = [100.0, 105.0, 108.0, 106.0, 110.0, 107.0, 109.0, 108.0, 111.0, 90.0]
    after = [90.1, 90.0, 90.1, 90.0, 90.0]
    bars = _bars(closes + after)
    entry_ts = T0 + timedelta(minutes=9)
    findings = entry_timing_findings(entry_ts, 90.0, bars)
    assert not any("고점매수" in f for f in findings)


# ── entry_timing_findings: 거래 소강 진입 ───────────────────────────────

def test_volume_lull_before_entry_is_flagged():
    """소유자 예시 그대로: 거래량 높았던 종목인데 진입 직전 5분에 거래가 끊겼다."""
    closes = [100.0] * 11
    # 세션 초반 거래량 높음(200), 진입 직전 5분은 뚝 끊김(20) — 세션평균의 50% 미만.
    volumes = [200.0] * 5 + [20.0] * 5 + [100.0]
    bars = _bars(closes, volumes=volumes)
    entry_ts = T0 + timedelta(minutes=10)
    findings = entry_timing_findings(entry_ts, 100.0, bars)
    assert any("거래 소강" in f for f in findings)


def test_steady_volume_before_entry_is_not_flagged():
    closes = [100.0] * 11
    volumes = [100.0] * 11
    bars = _bars(closes, volumes=volumes)
    entry_ts = T0 + timedelta(minutes=10)
    findings = entry_timing_findings(entry_ts, 100.0, bars)
    assert not any("거래 소강" in f for f in findings)


# ── entry_timing_findings: 늦은 진입 ─────────────────────────────────────

def test_entry_long_after_the_session_high_is_flagged():
    # 5분째 고점(110) 형성, 진입은 40분째 -> 35분 경과 >= 30분 문턱.
    closes = [100.0, 105.0, 108.0, 109.0, 110.0] + [105.0] * 36
    bars = _bars(closes)
    entry_ts = T0 + timedelta(minutes=40)
    findings = entry_timing_findings(entry_ts, 105.0, bars)
    assert any("늦은 진입" in f for f in findings)


def test_entry_soon_after_the_session_high_is_not_flagged():
    closes = [100.0, 105.0, 108.0, 109.0, 110.0, 109.5, 109.0]
    bars = _bars(closes)
    entry_ts = T0 + timedelta(minutes=6)
    findings = entry_timing_findings(entry_ts, 109.0, bars)
    assert not any("늦은 진입" in f for f in findings)


# ── 표본 부족 ─────────────────────────────────────────────────────────

def test_too_few_bars_before_entry_yields_no_findings():
    """진입 전 봉이 5개 미만이면 세 규칙 다 판정하지 않는다 — 문제를 지어내지 않는다."""
    closes = [100.0, 101.0, 102.0]
    bars = _bars(closes)
    entry_ts = T0 + timedelta(minutes=2)
    assert entry_timing_findings(entry_ts, 102.0, bars) == []


def test_missing_bars_yields_no_findings():
    assert entry_timing_findings(T0, 100.0, None) == []


# ── todays_round_trips ──────────────────────────────────────────────────

def _fill(ts, strategy_id, symbol, side, qty, price, market="KR", reason="", realized_pnl=None, fee=0.0):
    return {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "reason": reason, "market": market,
    }


def test_todays_round_trips_attaches_entry_price_and_reason():
    trades = [
        _fill("2026-08-24T00:05:00+00:00", "donchian", "005930", "BUY", 10, 100.0,
              reason="패턴A 채널 상단 돌파"),
        _fill("2026-08-24T01:00:00+00:00", "donchian", "005930", "SELL", 10, 105.0,
              realized_pnl=50.0),
    ]
    trips = todays_round_trips(trades, "KR", "2026-08-24")
    assert len(trips) == 1
    t = trips[0]
    assert t["entry_price"] == pytest.approx(100.0)
    assert t["reason"] == "패턴A 채널 상단 돌파"


def test_todays_round_trips_filters_other_market_and_date():
    trades = [
        _fill("2026-08-24T00:05:00+00:00", "donchian", "005930", "BUY", 10, 100.0, market="KR"),
        _fill("2026-08-24T01:00:00+00:00", "donchian", "005930", "SELL", 10, 105.0, market="KR", realized_pnl=50.0),
        _fill("2026-08-24T00:05:00+00:00", "donchian", "QQQ", "BUY", 10, 100.0, market="US"),
        _fill("2026-08-24T01:00:00+00:00", "donchian", "QQQ", "SELL", 10, 105.0, market="US", realized_pnl=50.0),
        _fill("2026-08-23T00:05:00+00:00", "donchian", "005930", "BUY", 10, 100.0, market="KR"),
        _fill("2026-08-23T01:00:00+00:00", "donchian", "005930", "SELL", 10, 105.0, market="KR", realized_pnl=50.0),
    ]
    trips = todays_round_trips(trades, "KR", "2026-08-24")
    assert len(trips) == 1
    assert trips[0]["symbol"] == "005930"


def test_todays_round_trips_missing_entry_fill_leaves_price_and_reason_blank():
    """방어적 — 매칭되는 원본 체결을 못 찾으면 지어내지 않는다."""
    trips = todays_round_trips([], "KR", "2026-08-24")
    assert trips == []


# ── strategy_feedback ────────────────────────────────────────────────────

def _trip(strategy, symbol, entry_min, exit_min, entry_price, reason="", bps=0.0):
    return {
        "strategy": strategy, "symbol": symbol, "bps": bps,
        "entry_ts": (T0 + timedelta(minutes=entry_min)).isoformat(),
        "exit_ts": (T0 + timedelta(minutes=exit_min)).isoformat(),
        "entry_price": entry_price, "reason": reason,
    }


def test_strategy_feedback_groups_by_strategy_and_counts_findings():
    closes = [90.0, 92.0, 95.0, 91.0, 98.0, 93.0, 96.0, 94.0, 99.0, 100.0]
    after = [100.0, 100.1, 100.2, 100.1, 100.0]
    bars = _bars(closes + after)
    trips = [_trip("donchian", "005930", 9, 13, 100.0, reason="패턴A")]
    out = strategy_feedback(trips, {"005930": bars})
    assert "donchian" in out
    d = out["donchian"]
    assert d["n_entries"] == 1
    assert any("고점매수" in tag for tag in d["finding_counts"])
    assert d["finding_counts"][next(iter(d["finding_counts"]))] == 1


def test_strategy_feedback_no_findings_reports_clean():
    closes = [100.0] * 11
    bars = _bars(closes)
    trips = [_trip("orb_scan", "QQQ", 6, 9, 100.0, reason="개장 5분 돌파")]
    out = strategy_feedback(trips, {"QQQ": bars})
    assert out["orb_scan"]["finding_counts"] == {}
    assert out["orb_scan"]["n_entries"] == 1


def test_strategy_feedback_tracks_missing_bars_honestly():
    trips = [_trip("donchian", "TQQQ", 0, 5, 50.0)]
    out = strategy_feedback(trips, {"TQQQ": None})
    assert out["donchian"]["n_entries"] == 1
    assert out["donchian"]["bars_missing"] == 1
    assert out["donchian"]["finding_counts"] == {}


# ── 부검 제외분의 원장 실현 합 (선택 편향 방지) ──────────────────────────

def test_strategy_feedback_reports_ledger_bps_of_skipped_trips():
    """보유가 1분봉 해상도 미만(hold 봉 <2)인 초단타는 부검에서 빠지는데,
    2026-08-27 실측에서 빠진 5건에 -195bp 가 숨어 부검 요약(MFE 중앙 +105bp)이
    합 -215bp 의 하루를 미화했다 — 제외분의 원장 실현 합을 함께 내야 한다."""
    closes = [100.0] * 11
    bars = _bars(closes)
    trips = [
        _trip("scalp_1m", "005930", 6, 9, 100.0, bps=50.0),        # 재생 가능(hold 4봉)
        _trip("scalp_1m", "005930", 6.2, 6.5, 100.0, bps=-120.0),  # 봉 사이 18초 → 스킵
    ]
    out = strategy_feedback(trips, {"005930": bars})
    d = out["scalp_1m"]
    assert d["forensics_skipped"] == 1
    assert d["skipped_ledger_bp"] == pytest.approx(-120.0)


def test_strategy_feedback_skipped_ledger_none_when_nothing_skipped():
    closes = [100.0] * 11
    bars = _bars(closes)
    trips = [_trip("scalp_1m", "005930", 6, 9, 100.0, bps=50.0)]
    out = strategy_feedback(trips, {"005930": bars})
    assert out["scalp_1m"]["forensics_skipped"] == 0
    assert out["scalp_1m"]["skipped_ledger_bp"] is None


def test_render_shows_skipped_ledger_sum():
    feedback = {"scalp_1m": {"n_entries": 8, "finding_counts": {}, "examples": {},
                             "forensics": {"n": 0}, "forensics_skipped": 5,
                             "bars_missing": 0, "skipped_ledger_bp": -195.4}}
    out = render_feedback_text(date(2026, 8, 27), "KR", feedback)
    assert "제외" in out
    assert "-195.4bp" in out


# ── already_recorded (멱등 append 판정) ──────────────────────────────────

def test_already_recorded_true_when_date_and_market_match():
    existing = [{"date": "2026-08-24", "market": "KR"}, {"date": "2026-08-23", "market": "US"}]
    assert already_recorded(existing, "2026-08-24", "KR") is True


def test_already_recorded_false_when_no_match():
    existing = [{"date": "2026-08-23", "market": "KR"}]
    assert already_recorded(existing, "2026-08-24", "KR") is False
    assert already_recorded([], "2026-08-24", "KR") is False


# ── render_feedback_text ──────────────────────────────────────────────────

def test_render_shows_no_entries_when_feedback_empty():
    out = render_feedback_text(date(2026, 8, 24), "KR", {})
    assert "진입 체결 없음" in out


def test_render_shows_no_issues_line_for_clean_strategy():
    feedback = {"orb_scan": {"n_entries": 2, "finding_counts": {}, "examples": {},
                              "forensics": {"n": 0}, "forensics_skipped": 0, "bars_missing": 0}}
    out = render_feedback_text(date(2026, 8, 24), "KR", feedback)
    assert "특이사항 없음" in out
    assert "orb_scan" in out


def test_render_includes_finding_example_and_reason_quote():
    feedback = {
        "donchian": {
            "n_entries": 1,
            "finding_counts": {"고점매수": 1},
            "examples": {"고점매수": {"symbol": "005930", "entry_ts": "2026-08-24T00:09:00+00:00",
                                      "reason": "패턴A 채널 상단 돌파",
                                      "finding": "고점매수 — 레인지 위치 100%"}},
            "forensics": {"n": 0}, "forensics_skipped": 0, "bars_missing": 0,
        }
    }
    out = render_feedback_text(date(2026, 8, 24), "KR", feedback)
    assert "고점매수: 1건" in out
    assert "패턴A 채널 상단 돌파" in out
    assert "005930" in out


def test_render_includes_unverified_threshold_footer():
    out = render_feedback_text(date(2026, 8, 24), "KR", {})
    assert "미검증 초기값" in out


# ── render_feedback_text HTML 서식 (2026-09-04, tgfmt) ────────────────────

import re as _re


def _assert_balanced_html(text: str) -> None:
    stack: list[str] = []
    for m in _re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            assert stack and stack[-1] == name, f"짝이 안 맞는 태그 </{name}> in: {text!r}"
            stack.pop()
    assert not stack, f"닫히지 않은 태그 {stack} in: {text!r}"


def test_render_feedback_text_html_is_balanced_empty_and_nonempty():
    _assert_balanced_html(render_feedback_text(date(2026, 8, 24), "KR", {}))

    feedback = {
        "donchian": {
            "n_entries": 1,
            "finding_counts": {"고점매수": 1},
            "examples": {"고점매수": {"symbol": "005930", "entry_ts": "2026-08-24T00:09:00+00:00",
                                      "reason": "패턴A 채널 상단 돌파",
                                      "finding": "고점매수 — 레인지 위치 100%"}},
            "forensics": {"n": 0}, "forensics_skipped": 0, "bars_missing": 0,
        }
    }
    text = render_feedback_text(date(2026, 8, 24), "KR", feedback)
    _assert_balanced_html(text)
    assert "<blockquote expandable>" in text


def test_render_feedback_text_escapes_reason_and_report_link():
    feedback = {
        "donchian": {
            "n_entries": 1,
            "finding_counts": {"고점매수": 1},
            "examples": {"고점매수": {"symbol": "005930", "entry_ts": "2026-08-24T00:09:00+00:00",
                                      "reason": "A&B<x> 채널 상단 돌파",
                                      "finding": "고점매수 — 레인지 위치 100%"}},
            "forensics": {"n": 0}, "forensics_skipped": 0, "bars_missing": 0,
        }
    }
    text = render_feedback_text(
        date(2026, 8, 24), "KR", feedback, report_url="https://example.com/r?a=1&b=2",
    )
    _assert_balanced_html(text)
    assert "A&B<x>" not in text and "A&amp;B&lt;x&gt;" in text
    assert '<a href="https://example.com/r?a=1&amp;b=2">전체 리포트</a>' in text
