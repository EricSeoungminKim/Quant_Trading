import json
from pathlib import Path

import pytest

from quant.collect.sources.sentiment import (
    fetch_aaii,
    fetch_cnn_fear_greed,
    fetch_naaim,
    fetch_sentiment,
    fg_rating_ko,
    naaim_label,
    parse_aaii_table,
    parse_cnn_fear_greed,
    parse_naaim_table,
)

FIXTURES = Path(__file__).parent / "fixtures"


# --- fg_rating_ko: CNN 공식 5구간 경계 ---


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "극단적 공포"),
        (24, "극단적 공포"),
        (25, "공포"),
        (44, "공포"),
        (45, "중립"),
        (55, "중립"),
        (56, "탐욕"),
        (74, "탐욕"),
        (75, "극단적 탐욕"),
        (100, "극단적 탐욕"),
    ],
)
def test_fg_rating_ko_boundaries(value, expected):
    assert fg_rating_ko(value) == expected


# --- naaim_label: 4구간 경계 ---


@pytest.mark.parametrize(
    "value,expected",
    [
        (19.9, "방어적"),
        (20.0, "중립 이하"),
        (49.9, "중립 이하"),
        (50.0, "강세"),
        (79.9, "강세"),
        (80.0, "적극 강세"),
    ],
)
def test_naaim_label_boundaries(value, expected):
    assert naaim_label(value) == expected


# --- CNN Fear & Greed: 픽스처 파싱 ---


def test_parse_cnn_fear_greed():
    data = json.loads((FIXTURES / "cnn_feargreed.json").read_text())
    out = parse_cnn_fear_greed(data)
    assert out["value"] == 61
    assert out["rating"] == "Greed"
    assert out["rating_ko"] == "탐욕"
    assert out["prev_close"] == 61
    assert out["prev_week"] == 60


# --- NAAIM: 픽스처 파싱 ---


def test_parse_naaim_table():
    text = (FIXTURES / "naaim_table.html").read_text()
    rows = parse_naaim_table(text)
    assert len(rows) >= 2
    assert rows[0] == {"date": "2026-04-29", "value": 93.79}
    assert rows[1] == {"date": "2026-04-22", "value": 94.15}


# --- AAII: 픽스처 파싱 + spread 계산 ---


def test_parse_aaii_table():
    text = (FIXTURES / "aaii_sentiment.html").read_text()
    rows = parse_aaii_table(text)
    assert len(rows) >= 2
    assert rows[0] == {
        "date_raw": "Aug 5",
        "bull_pct": 37.0,
        "neutral_pct": 25.0,
        "bear_pct": 38.0,
    }


def test_aaii_spread_and_changes():
    text = (FIXTURES / "aaii_sentiment.html").read_text()
    rows = parse_aaii_table(text)
    latest, prev = rows[0], rows[1]
    spread = round(latest["bull_pct"] - latest["bear_pct"], 1)
    bull_change = round(latest["bull_pct"] - prev["bull_pct"], 1)
    bear_change = round(latest["bear_pct"] - prev["bear_pct"], 1)
    assert spread == -1.0
    assert bull_change == 6.0
    assert bear_change == -4.1


# --- fetch_* : 픽스처를 응답으로 삼아 오프라인으로 검증 (monkeypatch) ---


def test_fetch_naaim_uses_top_two_rows(monkeypatch):
    text = (FIXTURES / "naaim_table.html").read_text()

    class FakeResp:
        def raise_for_status(self):
            pass

        @property
        def text(self):
            return text

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    import quant.collect.sources.sentiment as sentiment_mod

    monkeypatch.setattr(sentiment_mod, "client", lambda: FakeClient())
    out = fetch_naaim()
    assert out == {
        "value": 93.79,
        "label": "적극 강세",
        "prev": 94.15,
        "as_of": "2026-04-29",
    }


def test_fetch_aaii_builds_expected_dict(monkeypatch):
    text = (FIXTURES / "aaii_sentiment.html").read_text()

    class FakeResp:
        def raise_for_status(self):
            pass

        @property
        def text(self):
            return text

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return FakeResp()

    import quant.collect.sources.sentiment as sentiment_mod

    monkeypatch.setattr(sentiment_mod, "client", lambda: FakeClient())
    out = fetch_aaii()
    assert out == {
        "bull_pct": 37.0,
        "bear_pct": 38.0,
        "neutral_pct": 25.0,
        "bull_change": 6.0,
        "bear_change": -4.1,
        "spread": -1.0,
        "as_of": "2026-08-05",
    }


def test_fetch_cnn_fear_greed_builds_expected_dict(monkeypatch):
    data = json.loads((FIXTURES / "cnn_feargreed.json").read_text())

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return data

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return FakeResp()

    import quant.collect.sources.sentiment as sentiment_mod

    monkeypatch.setattr(sentiment_mod, "client", lambda: FakeClient())
    out = fetch_cnn_fear_greed()
    assert out == {
        "value": 61,
        "rating": "Greed",
        "rating_ko": "탐욕",
        "prev_close": 61,
        "prev_week": 60,
    }


# --- fetch_sentiment: 부분 실패 허용, 전체 실패시에만 raise ---


def test_fetch_sentiment_all_fail_raises(monkeypatch):
    import quant.collect.sources.sentiment as sentiment_mod

    monkeypatch.setattr(sentiment_mod, "fetch_cnn_fear_greed", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sentiment_mod, "fetch_naaim", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sentiment_mod, "fetch_aaii", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(ValueError, match="시장 심리 지표를 하나도 못 가져왔다"):
        sentiment_mod.fetch_sentiment()


def test_fetch_sentiment_partial_failure_keeps_others(monkeypatch):
    import quant.collect.sources.sentiment as sentiment_mod

    monkeypatch.setattr(sentiment_mod, "fetch_cnn_fear_greed", lambda: {"value": 1})
    monkeypatch.setattr(sentiment_mod, "fetch_naaim", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sentiment_mod, "fetch_aaii", lambda: {"bull_pct": 1})

    out = sentiment_mod.fetch_sentiment()
    assert out["cnn_fear_greed"] == {"value": 1}
    assert out["naaim"] is None
    assert out["aaii"] == {"bull_pct": 1}


# --- 실전 네트워크 호출: 최소 1개 지표는 채워져야 한다 ---


@pytest.mark.live
def test_live_fetch_sentiment_fills_at_least_one():
    out = fetch_sentiment()
    assert any(v is not None for v in out.values()), out


# ── 낡은 값 표시 ─────────────────────────────────────────────

from datetime import date  # noqa: E402

from quant.collect.sources.sentiment import STALE_DAYS, mark_stale  # noqa: E402


def test_recent_entry_is_not_stale():
    out = mark_stale({"value": 1.0, "as_of": "2026-08-10"}, date(2026, 8, 12))
    assert out["stale"] is False and out["age_days"] == 2


def test_old_entry_is_flagged_stale():
    """NAAIM 이 몇 달 묵은 값을 서빙하는 사례가 실제로 있다(2026-08-12 실측)."""
    out = mark_stale({"value": 93.79, "as_of": "2026-04-29"}, date(2026, 8, 12))
    assert out["stale"] is True and out["age_days"] > 100


def test_stale_boundary():
    on = mark_stale({"as_of": "2026-07-22"}, date(2026, 8, 12), days=STALE_DAYS)
    assert on["age_days"] == 21 and on["stale"] is False
    over = mark_stale({"as_of": "2026-07-21"}, date(2026, 8, 12), days=STALE_DAYS)
    assert over["stale"] is True


def test_missing_or_bad_as_of_is_passed_through():
    assert mark_stale(None, date(2026, 8, 12)) is None
    assert mark_stale({"value": 1}, date(2026, 8, 12)) == {"value": 1}
    assert "stale" not in mark_stale({"as_of": "not-a-date"}, date(2026, 8, 12))
