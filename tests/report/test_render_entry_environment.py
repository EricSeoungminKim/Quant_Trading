"""Entry context stays distinct from proven returns and preserves expandable evidence."""
from datetime import date, datetime
from html.parser import HTMLParser

from quant.analyze.render import render
from quant.analyze.tg_digest import Digest, NumberClaim
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.core.report_clock import KST
from quant.report.render.telegram import _format_summary


def snapshot():
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 9, 30), datetime(2026, 9, 30, 8, tzinfo=KST), {})


def environment(**overrides):
    return {
        "score": 63.5, "coverage": 80, "label": "중립 관찰", "exposure_band": "25-50%",
        "session_date": "2026-09-30", "version": "entry-v1", "note": "확률 아님 · 미검증",
        "factors": [{"id": "flow", "label": "외국인 수급", "weight": 15, "value": None,
                     "score": None, "status": "missing", "reason": "시세 없음", "date": None}],
        "risks": [{"code": "fx", "label": "원화 약세 확인 필요", "severity": "high"}],
        "keywords": {"status": "ok", "accepted_messages": 30, "excluded_messages": 4,
                     "topics": [{"keyword": "반도체", "count": 8, "channels": ["channel-a"], "urls": []}],
                     "missing_channels": []},
        "news": {"status": "ok", "items": [{"title": "<script>not executable</script>",
                     "url": "https://example.com/news", "classification": "주장·확인 필요"}]},
        "feedback": {"session_date": "2026-09-29", "summary": "지난 세션: 평가 대기"},
        **overrides,
    }


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.nodes[attrs["id"]] = (tag, attrs)


def test_entry_context_precedes_summary_and_exposes_evidence_without_html_injection():
    html = render(snapshot(), entry_environment=environment(),
                  exec_summary={"market": "기존 시장 설명", "flow": "수급 설명", "catalyst": "재료 설명"})
    assert html.index('id="entry-environment"') < html.index("기존 시장 설명")
    assert "63.5" in html and "80%" in html and "25-50%" in html
    assert "확률 아님" in html and "미검증" in html
    assert "시세 없음" in html and "원화 약세 확인 필요" in html and "반도체" in html
    assert "지난 세션: 평가 대기" in html
    assert "&lt;script&gt;not executable&lt;/script&gt;" in html
    assert "<script>not executable</script>" not in html
    tag, attrs = Elements(html).nodes["executive-summary"]
    assert tag == "details" and "open" not in attrs


def test_missing_score_is_hold_not_zero_and_missing_feedback_is_explicit():
    html = render(snapshot(), entry_environment=environment(score=None, coverage=40,
                   label="판단 보류", exposure_band="판단 보류", feedback=None))
    assert "판단 보류" in html and "40%" in html
    assert "이 점수의 이전 세션 평가 없음" in html
    section = html.split('id="entry-environment"', 1)[1].split("</section>", 1)[0]
    assert "0 / 100" not in section


def test_optional_entry_context_preserves_legacy_layout_and_telegram():
    html = render(snapshot(), exec_summary={"market": "기존 설명", "flow": "", "catalyst": ""})
    assert 'id="entry-environment"' not in html
    assert Elements(html).nodes["executive-summary"][0] == "section"
    assert _format_summary({"auto_watch": "AUTO_WATCH: 없음", "symbols": []}) == "후보 0개"


def test_telegram_prepends_environment_and_risk_without_probability_claim():
    text = _format_summary({"auto_watch": "AUTO_WATCH: 없음", "entry_environment": environment()})
    lines = text.splitlines()
    assert "63.5/100" in lines[0] and "80%" in lines[0] and "중립 관찰" in lines[0]
    assert "확률 아님" in lines[0] and "미검증" in lines[0]
    assert "원화 약세 확인 필요" in lines[1]
    assert "후보 0개" in text
    held = _format_summary({"entry_environment": environment(score=None, label="판단 보류")})
    assert "판단 보류" in held.splitlines()[0] and "None" not in held


def test_market_evidence_retains_dates_and_distinguishes_btc_reference_from_score():
    context = {
        "quotes": {"QQQ": {"label": "나스닥", "date": "2026-09-29", "value": -0.5, "status": "ok"}},
        "btc": {"date": "2026-09-30", "change_pct": 1.3, "status": "provisional", "score_included": False},
        "fed": {"fed_target_upper": {"status": "ok", "value": 4.5, "date": "2026-09-29",
                    "last_change_bp": -25, "last_change_date": "2026-09-17"}},
        "sectors": {"status": "ok", "positive_breadth_pct": 60, "n_dated": 10,
                    "leaders": [{"name": "반도체", "change_pct": 2.1, "date": "2026-09-29"}]},
    }
    html = render(snapshot(), entry_environment=environment(market_context=context))
    assert "나스닥" in html and "-0.5%" in html and "2026-09-29" in html
    assert "비트코인" in html and "+1.3%" in html and "점수 제외" in html
    assert "4.5%" in html and "-25bp" in html and "2026-09-17" in html
    assert "상승 업종 비중 60%" in html and "반도체 +2.1%" in html


def test_review_evidence_and_keyword_sources_are_available_in_expanded_details():
    env = environment(forecast_status="retrospective", feedback={
        "summary": "이전 점수는 장중 재생성", "review": ["예측 성과에서 제외"],
        "factor_changes": [{"label": "변동성", "previous": 50, "current": 30, "change": -20}],
    })
    env["keywords"]["topics"][0]["urls"] = ["https://example.com/original"]
    html = render(snapshot(), entry_environment=env)
    assert "예측 성과에서 제외" in html and "변동성" in html and "-20" in html
    assert "channel-a" in html and 'href="https://example.com/original"' in html
    assert "사후 재생성" in html


def test_new_overview_caps_repetitive_channel_claims_and_discloses_total():
    snap = snapshot()
    digest = Digest("KR", snap.generated_at, snap.generated_at,
                    channel_entries={"channel": []},
                    number_claims=[NumberClaim("005930", f"claim-{i:02d}", "channel", "원문", "미확인") for i in range(25)])
    html = render(snap, channel_digest=digest, entry_environment=environment())
    assert "claim-19" in html and "claim-20" not in html
    assert "전체 25건 중 20건 표시" in html
    legacy = render(snap, channel_digest=digest)
    assert "claim-24" in legacy


def test_feedback_sample_sizes_and_baselines_remain_visible_and_telegram_is_complete():
    env = environment(feedback={"summary": "관측 3일 — 미검증", "stats": {"benchmarks": {
        "KOSPI": {"n_outcomes": 3, "n_forecasts": 2, "hits": 1, "accuracy_pct": 50,
                  "accuracy_ci": [0.1, 0.9], "rank_ic": None, "always_up_hits": 2, "always_flat_hits": 0},
        "KOSDAQ": {"n_outcomes": 0, "n_forecasts": 0, "hits": 0, "accuracy_pct": None,
                  "accuracy_ci": None, "rank_ic": None, "always_up_hits": 0, "always_flat_hits": 0},
    }}})
    html = render(snapshot(), entry_environment=env)
    assert "실제값 3 / 방향 판정 2" in html and "10%–90%" in html
    assert "항상 상승 2/2" in html and "항상 보합 0/2" in html
    assert "실제값 0 / 방향 판정 0" in html and "표본 없음" in html
    text = _format_summary({"entry_environment": env})
    assert "반도체" in text and "관측 3일 — 미검증" in text


def test_mobile_overview_shows_hold_once_and_keeps_model_metadata_in_details():
    env = environment(score=None, label="판단 보류", exposure_band="판단 보류",
                      generated_at="2026-09-30T08:00:00+09:00", forecast_status="장전 최초 기록 고정")
    html = render(snapshot(), entry_environment=env)
    panel = html.split('id="entry-environment"', 1)[1].split("</section>", 1)[0]
    overview, evidence = panel.split('<details class="entry-evidence">', 1)
    assert "entry-v1" not in overview and "2026-09-30T08:00:00+09:00" not in overview
    assert "entry-v1" in evidence and "2026-09-30T08:00:00+09:00" in evidence
    assert overview.count("판단 보류") == 1
