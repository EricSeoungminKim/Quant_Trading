"""quant.analyze.kr_sectors — Naver 업종→GICS-11 매핑 + 전일 US 섹터 신호
(2026-09-06). 순수 함수만, 네트워크/파일 I/O 없음(모듈 docstring과 같은 원칙)."""
from quant.analyze.kr_sectors import (
    GICS_TO_KR_LABEL, UPJONG_TO_GICS, US_SECTOR_ETF_TO_GICS,
    gics_for_upjong, us_sector_returns_from_source, us_sector_signal,
)
from quant.analyze.us_sector_map import KR_BENEFICIARIES

_KNOWN_GICS = set(KR_BENEFICIARIES.keys())


# --------------------------------------------------------------- 매핑 완전성

def test_every_upjong_maps_to_known_gics_sector_or_explicit_none():
    """UPJONG_TO_GICS의 79개 Naver 업종 전부 GICS-11(us_sector_map.
    KR_BENEFICIARIES와 같은 taxonomy) 중 하나이거나, 매핑이 없으면 명시적으로
    None이다 — 오타로 조용히 다른 문자열이 섞여 있지 않은지 확인한다."""
    bad = {
        upjong: gics for upjong, gics in UPJONG_TO_GICS.items()
        if gics is not None and gics not in _KNOWN_GICS
    }
    assert not bad, f"알 수 없는 GICS 섹터명: {bad}"


def test_upjong_to_gics_covers_at_least_the_eleven_gics_sectors():
    """11개 GICS 섹터 전부 최소 하나 이상의 Naver 업종에 연결돼 있다(커버리지
    누락 없음 — 특정 섹터가 통째로 비면 그 섹터는 절대 신호를 못 받는다)."""
    mapped = {gics for gics in UPJONG_TO_GICS.values() if gics is not None}
    assert mapped == _KNOWN_GICS


def test_upjong_with_no_gics_mapping_is_explicit_none():
    assert UPJONG_TO_GICS["기타"] is None


def test_gics_to_kr_label_covers_all_eleven_sectors():
    assert set(GICS_TO_KR_LABEL.keys()) == _KNOWN_GICS


def test_us_sector_etf_to_gics_covers_all_eleven_sectors():
    assert set(US_SECTOR_ETF_TO_GICS.values()) == _KNOWN_GICS


# --------------------------------------------------------------- gics_for_upjong

def test_gics_for_upjong_known():
    assert gics_for_upjong("반도체와반도체장비") == "Information Technology"
    assert gics_for_upjong("자동차") == "Consumer Discretionary"


def test_gics_for_upjong_unknown_returns_none():
    assert gics_for_upjong("존재하지않는업종") is None
    assert gics_for_upjong("기타") is None


# --------------------------------------------------------------- us_sector_signal

def test_us_sector_signal_passes_through_known_sectors():
    out = us_sector_signal({"Information Technology": 0.012, "Financials": -0.004})
    assert out == {"Information Technology": 0.012, "Financials": -0.004}


def test_us_sector_signal_filters_unknown_sector_keys():
    """오타·미지원 섹터명(GICS-11 밖)은 조용히 걸러진다 — 에러도 안 내지만
    섞이지도 않는다."""
    out = us_sector_signal({"Information Technology": 0.012, "Not A Sector": 0.5})
    assert out == {"Information Technology": 0.012}


def test_us_sector_signal_filters_none_values():
    out = us_sector_signal({"Energy": None, "Utilities": 0.01})
    assert out == {"Utilities": 0.01}


def test_us_sector_signal_empty_input():
    assert us_sector_signal({}) == {}
    assert us_sector_signal(None) == {}


# --------------------------------------------------------------- us_sector_returns_from_source

def test_us_sector_returns_from_source_converts_ticker_and_percent_to_fraction():
    """`quant.collect.sources.technical.fetch_sectors()`와 같은 모양의 리스트
    (퍼센트포인트 change_pct)를 GICS 영문 키·fraction으로 바꾼다."""
    sectors = [
        {"ticker": "XLK", "name": "기술", "change_pct": 1.2},
        {"ticker": "XLF", "name": "금융", "change_pct": -0.5},
    ]
    out = us_sector_returns_from_source(sectors)
    assert out == {"Information Technology": 0.012, "Financials": -0.005}


def test_us_sector_returns_from_source_ignores_unknown_ticker():
    sectors = [{"ticker": "ZZZZ", "name": "미상", "change_pct": 5.0}]
    assert us_sector_returns_from_source(sectors) == {}


def test_us_sector_returns_from_source_ignores_missing_change_pct():
    sectors = [{"ticker": "XLK", "name": "기술", "change_pct": None}]
    assert us_sector_returns_from_source(sectors) == {}


def test_us_sector_returns_from_source_handles_none_and_empty():
    assert us_sector_returns_from_source(None) == {}
    assert us_sector_returns_from_source([]) == {}
