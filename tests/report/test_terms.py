from quant.core.terms import RELEASE_TERMS, event_desc, event_full, event_term


def test_cpi_uses_common_abbreviation():
    # 한 릴리즈에 헤드라인 CPI 와 근원 CPI 가 함께 발표된다 — 둘 다 표기한다.
    assert event_term("Consumer Price Index") == "CPI / Core CPI"
    assert event_full("Consumer Price Index") == "CPI / Core CPI (소비자물가)"


def test_all_high_impact_releases_are_mapped():
    """캘린더가 전면에 세우는 릴리즈는 전부 통용 표기가 있어야 한다."""
    from quant.collect.sources.calendar import HIGH_IMPACT

    assert set(HIGH_IMPACT) <= set(RELEASE_TERMS)


def test_unmapped_name_passes_through_unchanged():
    assert event_term("Wholesale Trade") == "Wholesale Trade"
    assert event_full("Wholesale Trade") == "Wholesale Trade"
    assert event_desc("Wholesale Trade") == ""


def test_whitespace_is_tolerated():
    assert event_term("  Consumer Price Index  ") == "CPI / Core CPI"


def test_no_mapping_leaks_english_release_name():
    """매핑된 이름은 결과에 원문이 남지 않아야 한다 — 그래야 한글 패치가 완결된다."""
    for name in RELEASE_TERMS:
        assert name not in event_full(name)


def test_fomc_keeps_its_common_term():
    assert event_term("FOMC Meeting") == "FOMC"
