from pathlib import Path

import pytest

from quant.collect.sources.naver_flow import FLOW_COLUMNS, INSTITUTION_SUBS, parse_flow

FIXTURE = Path(__file__).parent / "fixtures" / "naver_kospi_20260811.html"


@pytest.fixture
def rows():
    return parse_flow(FIXTURE.read_bytes())


def test_parses_rows(rows):
    assert len(rows) >= 5
    assert set(rows[0]) == {"date", *FLOW_COLUMNS}


def test_latest_row_matches_known_values(rows):
    # 2026-08-11 KOSPI, 억원 단위 — 네이버 화면 표시값과 대조해 고정한 값이다
    r = next(r for r in rows if r["date"] == "2026-08-11")
    assert r["개인"] == -708
    assert r["외국인"] == 535
    assert r["기관계"] == 212
    assert r["금융투자"] == -2089
    assert r["기타법인"] == -40


def test_institution_subtotals_sum_to_total(rows):
    """컬럼 밀림 탐지 — 기관계는 6개 세부항목의 합이어야 한다(반올림 오차 허용 ±2)."""
    for r in rows:
        assert abs(sum(r[s] for s in INSTITUTION_SUBS) - r["기관계"]) <= 2, r


def test_date_is_iso_normalized(rows):
    assert all(len(r["date"]) == 10 and r["date"][4] == "-" for r in rows)


@pytest.mark.live
def test_live_fetch_kospi_and_kosdaq():
    from quant.collect.sources.naver_flow import fetch_flow

    for sosok in ("01", "02"):
        d = fetch_flow(sosok, "20260811")
        assert d["rows"], sosok


def test_json_flow_preserves_krw_units_and_combines_official_investor_groups():
    import json

    fixture = FIXTURE.with_name("naver_index_flow_20261022.json")
    raw = fixture.read_bytes()
    rows = parse_flow(raw)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-10-22"
    assert rows[0]["개인"] == 3666.5
    assert rows[0]["외국인"] == 504.61  # 9000 + 기타외국인 9001
    assert rows[0]["투신"] == -2907.51  # 3000 + 사모 3100
    assert rows[0]["연기금등"] == 1313.09  # 6000 + 국가 7000
    for row in rows:
        assert sum(row[name] for name in INSTITUTION_SUBS) == pytest.approx(row["기관계"])
        assert sum(row[name] for name in ("개인", "외국인", "기관계", "기타법인")) == pytest.approx(0, abs=0.02)
    data = json.loads(raw)
    data["content"][0]["netAmounts"] = data["content"][0]["netAmounts"][1:]
    assert len(parse_flow(json.dumps(data).encode())) == 1  # 결측을 0으로 위장하지 않는다


def test_fetch_uses_new_json_api_and_requested_cutoff(monkeypatch):
    from contextlib import contextmanager

    from quant.collect.sources import naver_flow

    urls = []
    class Client:
        def get(self, url):
            urls.append(url)
            class Response:
                content = FIXTURE.with_name("naver_index_flow_20261022.json").read_bytes()
                def raise_for_status(self):
                    pass
            return Response()
    @contextmanager
    def client():
        yield Client()
    monkeypatch.setattr(naver_flow, "client", client)
    result = naver_flow.fetch_flow("02", "20261021")
    assert urls == ["https://stock.naver.com/api/domestic/market/trend/daily?tradeType=KRX&marketType=KOSDAQ&bizdate=20261021&startIdx=0&pageSize=20"]
    assert result["unit"] == "억원"
    assert [row["date"] for row in result["rows"]] == ["2026-10-21"]
