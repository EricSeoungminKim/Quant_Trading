"""`quant.analyze.us_sector_map` — US 뉴스 → GICS 섹터 → KR 수혜주(서브프로젝트 W part 2)."""
from __future__ import annotations

from quant.analyze.us_sector_map import (
    KR_BENEFICIARIES,
    classify_sector,
    map_us_news_to_kr,
)


def test_classify_sector_semiconductor_headline_is_information_technology():
    title = "NVIDIA UNVEILS NEW AI CHIP, SEMICONDUCTOR STOCKS RALLY"
    assert classify_sector(title) == "Information Technology"


def test_classify_sector_oil_headline_is_energy():
    title = "OPEC AGREES TO CUT CRUDE OIL PRODUCTION, WTI JUMPS"
    assert classify_sector(title) == "Energy"


def test_classify_sector_fed_headline_is_financials():
    title = "FEDERAL RESERVE SIGNALS INTEREST RATE HIKE, TREASURY YIELDS SURGE"
    assert classify_sector(title) == "Financials"


def test_classify_sector_korean_headline_matches_korean_keywords():
    title = "국제유가 급등, 정유주 강세 전망"
    assert classify_sector(title) == "Energy"


def test_classify_sector_no_keyword_match_returns_none():
    title = "COMPLETELY UNRELATED TEXT WITH NO SECTOR SIGNAL WHATSOEVER"
    assert classify_sector(title) is None


def test_classify_sector_empty_title_returns_none():
    assert classify_sector("") is None


def test_kr_beneficiaries_symbols_are_6_digit_codes():
    for sector, entries in KR_BENEFICIARIES.items():
        assert 3 <= len(entries) <= 6, f"{sector} has {len(entries)} entries"
        for entry in entries:
            assert entry["symbol"].isdigit() and len(entry["symbol"]) == 6
            assert entry["name"]
            assert entry["why"]


def test_map_us_news_to_kr_groups_by_sector_with_hits_and_beneficiaries():
    titles = [
        "NVIDIA STOCK SURGES ON AI CHIP DEMAND",
        "SEMICONDUCTOR EXPORTS HIT RECORD HIGH",
        "OPEC CUTS OIL PRODUCTION QUOTA",
        "COMPLETELY UNRELATED HEADLINE ABOUT NOTHING SECTOR-RELATED",
    ]
    result = map_us_news_to_kr(titles)

    assert result["Information Technology"]["hits"] == 2
    assert len(result["Information Technology"]["sample_headlines"]) == 2
    assert result["Information Technology"]["kr_beneficiaries"] == KR_BENEFICIARIES["Information Technology"]

    assert result["Energy"]["hits"] == 1
    assert result["Energy"]["sample_headlines"] == ["OPEC CUTS OIL PRODUCTION QUOTA"]

    # 매칭 안 된 헤드라인 때문에 새 섹터가 생기지 않는다
    assert set(result.keys()) == {"Information Technology", "Energy"}


def test_map_us_news_to_kr_empty_titles_returns_empty_dict():
    assert map_us_news_to_kr([]) == {}


def test_map_us_news_to_kr_all_unmatched_returns_empty_dict():
    titles = ["NOTHING SECTOR RELATED HERE", "ANOTHER VAGUE HEADLINE"]
    assert map_us_news_to_kr(titles) == {}
