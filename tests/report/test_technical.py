import pandas as pd
import pytest

from quant.collect.sources import technical as technical_module
from quant.collect.sources.technical import (
    SECTORS,
    classify_52w,
    fetch_breadth_52w,
    fetch_sectors,
    fetch_vix_term,
    gics_to_kr,
    structure_label,
)

# 아래 기존 breadth 테스트는 naver_path_fetcher 를 주지 않으면 기본
# `_fetch_naver_path`(실네트워크)를 호출하므로 전부 명시적으로 None 을
# 돌려주는 페이크를 준다 — 네이버 경로 해석 자체는 별도 테스트 블록에서 검증한다.
def _no_naver_path(ticker: str) -> None:
    return None


def test_structure_label_contango():
    assert structure_label(near=15.0, far=19.0) == "콘탱고 (정상)"


def test_structure_label_backwardation():
    assert structure_label(near=25.0, far=18.0) == "백워데이션 (역전)"


def test_structure_label_flat():
    assert structure_label(near=15.0, far=15.3) == "평탄"


def test_structure_label_boundary_is_flat():
    # far - near == 0.5 는 "0.5 초과"가 아니므로 평탄
    assert structure_label(near=15.0, far=15.5) == "평탄"
    assert structure_label(near=15.5, far=15.0) == "평탄"


def test_sectors_has_11_entries_and_all_korean():
    assert len(SECTORS) == 11
    for name in SECTORS.values():
        assert any("가" <= ch <= "힣" for ch in name)


def test_gics_to_kr_covers_all_11_sectors():
    gics_sectors = [
        "Information Technology",
        "Financials",
        "Energy",
        "Health Care",
        "Industrials",
        "Consumer Staples",
        "Consumer Discretionary",
        "Communication Services",
        "Materials",
        "Utilities",
        "Real Estate",
    ]
    mapped = {gics_to_kr(s) for s in gics_sectors}
    assert mapped == set(SECTORS.values())


def test_gics_to_kr_unregistered_sector_falls_back_to_etc():
    assert gics_to_kr("Unknown Sector") == "기타"


def test_classify_52w_latest_is_high():
    assert classify_52w([10.0, 12.0, 9.0, 15.0]) == "high"


def test_classify_52w_latest_is_low():
    assert classify_52w([10.0, 12.0, 9.0, 5.0]) == "low"


def test_classify_52w_latest_is_neither():
    assert classify_52w([10.0, 12.0, 9.0, 11.0]) is None


def test_classify_52w_empty_returns_none():
    assert classify_52w([]) is None


@pytest.mark.live
def test_fetch_vix_term_live():
    data = fetch_vix_term()
    assert len(data["points"]) == 4
    assert data["structure"] in ("콘탱고 (정상)", "백워데이션 (역전)", "평탄")
    assert isinstance(data["spread"], float)
    assert data["move"] is not None
    assert data["move"]["value"] > 0


@pytest.mark.live
def test_fetch_sectors_live():
    data = fetch_sectors()
    assert len(data["sectors"]) == 11
    pct = [s["change_pct"] for s in data["sectors"]]
    assert pct == sorted(pct, reverse=True)


@pytest.mark.live
def test_fetch_breadth_52w_live():
    data = fetch_breadth_52w()
    assert data["universe"] > 450
    assert data["highs"]["count"] >= 0
    assert data["lows"]["count"] >= 0
    assert data["as_of"]
    for bucket in list(data["highs"]["by_sector"].values()) + list(data["lows"]["by_sector"].values()):
        for entry in bucket:
            assert "ticker" in entry and "name" in entry and "naver_path" in entry


# ── 52주 신고/신저 이름 병기 + 기준일 (리포트 UX 2차 L-3) ──────────────────
# "MRK·SOLV·TECH 는 실제 헬스케어 종목이나 티커만 보여서 TECH 가 섹터명처럼
# 읽히는 혼란" 피드백 — by_sector 값이 티커 문자열이 아니라
# {"ticker": ..., "name": ...} 로 바뀐다(계약 변경).


class _FakeResp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    def __init__(self, text):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        return _FakeResp(self._text)


_SP500_TABLE_HTML = """
<table>
<tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
<tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td></tr>
<tr><td>JPM</td><td>JPMorgan Chase &amp; Co.</td><td>Financials</td></tr>
</table>
"""


def _mock_breadth_deps(monkeypatch):
    monkeypatch.setattr(technical_module, "client", lambda timeout=20.0: _FakeClient(_SP500_TABLE_HTML))
    idx = pd.date_range("2025-08-01", periods=3, freq="D")
    close = pd.DataFrame({"AAPL": [10.0, 12.0, 15.0], "JPM": [50.0, 45.0, 40.0]}, index=idx)
    frame = pd.concat({"Close": close}, axis=1)
    monkeypatch.setattr(technical_module.yf, "download", lambda *a, **k: frame)


def test_fetch_breadth_52w_attaches_company_name_per_displayed_ticker(monkeypatch):
    _mock_breadth_deps(monkeypatch)
    names = {"AAPL": "Apple Inc.", "JPM": "JPMorgan Chase & Co."}

    data = fetch_breadth_52w(
        name_fetcher=lambda t: names.get(t), sleep=lambda s: None,
        naver_path_fetcher=_no_naver_path,
    )

    assert data["highs"]["by_sector"]["기술"] == [
        {"ticker": "AAPL", "name": "Apple Inc.", "naver_path": None}
    ]
    assert data["lows"]["by_sector"]["금융"] == [
        {"ticker": "JPM", "name": "JPMorgan Chase & Co.", "naver_path": None}
    ]


def test_fetch_breadth_52w_as_of_reflects_last_data_date(monkeypatch):
    _mock_breadth_deps(monkeypatch)

    data = fetch_breadth_52w(
        name_fetcher=lambda t: None, sleep=lambda s: None, naver_path_fetcher=_no_naver_path,
    )

    assert data["as_of"] == "2025-08-03"


def test_fetch_breadth_52w_name_lookup_failure_falls_back_to_ticker_only(monkeypatch):
    """기본 name_fetcher(yf.Ticker(...).info)가 실패해도 전체 조회는 죽지
    않고 이름만 None 으로 빠진다 — 표는 티커만으로도 그대로 나간다."""
    _mock_breadth_deps(monkeypatch)

    def boom(ticker):
        raise RuntimeError("network down")

    monkeypatch.setattr(technical_module.yf, "Ticker", boom)
    data = fetch_breadth_52w(sleep=lambda s: None, naver_path_fetcher=_no_naver_path)

    assert data["highs"]["by_sector"] == {
        "기술": [{"ticker": "AAPL", "name": None, "naver_path": None}]
    }


def test_fetch_ticker_name_swallows_errors(monkeypatch):
    def boom(ticker):
        raise RuntimeError("network down")

    monkeypatch.setattr(technical_module.yf, "Ticker", boom)
    assert technical_module._fetch_ticker_name("AAPL") is None


def test_fetch_ticker_name_returns_short_name(monkeypatch):
    class _FakeTicker:
        info = {"shortName": "Apple Inc."}

    monkeypatch.setattr(technical_module.yf, "Ticker", lambda ticker: _FakeTicker())
    assert technical_module._fetch_ticker_name("AAPL") == "Apple Inc."


def test_fetch_breadth_52w_uses_default_name_fetcher_when_not_provided(monkeypatch):
    _mock_breadth_deps(monkeypatch)
    monkeypatch.setattr(technical_module, "_fetch_ticker_name", lambda t: f"{t} Corp")

    data = fetch_breadth_52w(sleep=lambda s: None, naver_path_fetcher=_no_naver_path)

    assert data["highs"]["by_sector"]["기술"] == [
        {"ticker": "AAPL", "name": "AAPL Corp", "naver_path": None}
    ]


# ── 네이버 상세 링크 해석(리포트 UX 3차, 2026-08-17) ─────────────────────
# 실측(curl "https://ac.stock.naver.com/ac?q=MRK&target=stock,index,
# marketindicator"): {"query":"MRK","items":[{"code":"MRK",
# "name":"머크 앤 코","typeCode":"NYSE","url":"/worldstock/stock/MRK/total",
# "reutersCode":"MRK",...},{"code":"MRKR","url":"/worldstock/stock/
# MRKR.O/total","reutersCode":"MRKR.O",...},{"code":"9980",
# "url":"/worldstock/stock/9980.T/total","reutersCode":"9980.T",...}]}


class _FakeAcResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAcClient:
    def __init__(self, get_fn):
        self._get_fn = get_fn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        return self._get_fn(url, params)


def _patch_ac_client(monkeypatch, get_fn):
    monkeypatch.setattr(technical_module, "client", lambda timeout=5.0: _FakeAcClient(get_fn))


def test_fetch_naver_path_picks_exact_code_match_and_strips_total_suffix(monkeypatch):
    def fake_get(url, params):
        assert params["q"] == "MRK"
        return _FakeAcResp({
            "items": [
                {"code": "MRK", "url": "/worldstock/stock/MRK/total", "reutersCode": "MRK"},
                {"code": "MRKR", "url": "/worldstock/stock/MRKR.O/total", "reutersCode": "MRKR.O"},
            ]
        })

    _patch_ac_client(monkeypatch, fake_get)
    assert technical_module._fetch_naver_path("MRK") == "/worldstock/stock/MRK"


def test_fetch_naver_path_matches_via_reuters_code_base_when_code_differs(monkeypatch):
    """일부 종목은 code 필드 자체에 거래소 접미사가 없어도 값이 티커와 다를 수
    있다 — reutersCode 에서 거래소 접미사(.K 등)를 뗀 값으로도 매칭을 시도한다."""
    def fake_get(url, params):
        return _FakeAcResp({
            "items": [
                {"code": "FOOX", "url": "/worldstock/stock/FOO.K/total", "reutersCode": "FOO.K"},
            ]
        })

    _patch_ac_client(monkeypatch, fake_get)
    assert technical_module._fetch_naver_path("FOO") == "/worldstock/stock/FOO.K"


def test_fetch_naver_path_returns_none_when_no_exact_match(monkeypatch):
    """자동완성이 유사 티커(MRKR)만 돌려주고 정확히 일치하는 항목이 없으면
    fuzzy 로 아무거나 고르지 않고 None."""
    def fake_get(url, params):
        return _FakeAcResp({
            "items": [
                {"code": "MRKR", "url": "/worldstock/stock/MRKR.O/total", "reutersCode": "MRKR.O"},
            ]
        })

    _patch_ac_client(monkeypatch, fake_get)
    assert technical_module._fetch_naver_path("MRK") is None


def test_fetch_naver_path_returns_none_on_http_error(monkeypatch):
    def fake_get(url, params):
        raise RuntimeError("network down")

    _patch_ac_client(monkeypatch, fake_get)
    assert technical_module._fetch_naver_path("MRK") is None


def test_fetch_naver_path_returns_none_when_items_empty(monkeypatch):
    def fake_get(url, params):
        return _FakeAcResp({"items": []})

    _patch_ac_client(monkeypatch, fake_get)
    assert technical_module._fetch_naver_path("ZZZZ") is None


def test_fetch_breadth_52w_attaches_naver_path_per_displayed_ticker(monkeypatch):
    _mock_breadth_deps(monkeypatch)
    paths = {"AAPL": "/worldstock/stock/AAPL.O", "JPM": None}

    data = fetch_breadth_52w(
        name_fetcher=lambda t: None, sleep=lambda s: None,
        naver_path_fetcher=lambda t: paths.get(t),
    )

    assert data["highs"]["by_sector"]["기술"][0]["naver_path"] == "/worldstock/stock/AAPL.O"
    assert data["lows"]["by_sector"]["금융"][0]["naver_path"] is None
