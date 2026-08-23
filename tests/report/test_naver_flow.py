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
