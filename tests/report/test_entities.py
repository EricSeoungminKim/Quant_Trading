from pathlib import Path

from quant.analyze.entities import (
    build_market_map,
    build_table,
    build_us_table,
    extract_us,
    load_market_map,
    load_table,
    load_us_table,
    parse_corp_list,
    parse_symbol_dir,
    parse_us_list,
)

SP500_FIXTURE = Path(__file__).parent / "fixtures" / "sp500_list.html"

# KIND corpList.do 다운로드 포맷을 그대로 흉내낸 최소 표 — parse_corp_list 가
# 실제로 파싱하는 열 구조(회사명, 시장구분, 종목코드)만 맞추면 된다.
_SAMPLE_HTML = """
<table>
<tr><th>a</th><th>b</th><th>c</th></tr>
<tr><td>삼성전자</td><td>유가</td><td>005930</td></tr>
<tr><td>코스맥스</td><td>코스닥</td><td>192820</td></tr>
</table>
""".encode("euc-kr")


def test_build_market_map_maps_kospi_and_kosdaq_suffixes():
    recs = parse_corp_list(_SAMPLE_HTML)
    m = build_market_map(recs)
    assert m["005930"] == "005930.KS"
    assert m["192820"] == "192820.KQ"


def test_load_market_map_reuses_load_table_cache(tmp_path: Path):
    cache = tmp_path / "kind_corplist.html"
    cache.write_bytes(_SAMPLE_HTML)

    # 캐시가 이미 있으니 네트워크를 타지 않는다 — 타면 client() 가 예외를 낸다.
    table = load_table(tmp_path)
    market_map = load_market_map(tmp_path)

    assert table == build_table(parse_corp_list(_SAMPLE_HTML))
    assert market_map == {"005930": "005930.KS", "192820": "192820.KQ"}


# ---------------------------------------------------------------------------
# US (S&P500) — 티커가 일반 단어와 충돌하는 문제(IT=Gartner, ALL=Allstate 등)를
# 막는 게 핵심이라 회귀 테스트로 명시한다.
# ---------------------------------------------------------------------------


def _us_table() -> list[tuple[str, str]]:
    return build_us_table(parse_us_list(SP500_FIXTURE.read_text(encoding="utf-8")))


def test_parse_us_list_extracts_name_and_ticker_from_fixture():
    recs = parse_us_list(SP500_FIXTURE.read_text(encoding="utf-8"))
    assert ("Nvidia", "NVDA") in recs
    assert ("Microsoft", "MSFT") in recs
    assert ("Apple Inc.", "AAPL") in recs


def test_build_us_table_strips_corp_suffix_but_keeps_glued_names():
    table = _us_table()
    assert ("Apple", "AAPL") in table  # "Inc." 접미사 제거
    assert ("KeyCorp", "KEY") in table  # 붙여쓴 사명은 "Corp"를 잘라내면 안 된다


def test_extract_us_matches_company_names_case_insensitive():
    table = _us_table()
    hits = extract_us("NVIDIA and Microsoft rallied", table)
    symbols = {h["symbol"] for h in hits}
    assert symbols == {"NVDA", "MSFT"}


def test_extract_us_rejects_it_ticker_false_positive():
    """'IT spending' 의 IT 를 Gartner 로 잡으면 안 된다 (2글자 티커 제외)."""
    table = _us_table()
    assert extract_us("IT spending rose in Q3", table) == []


def test_extract_us_rejects_all_ticker_false_positive():
    """'ALL of the gains' 의 ALL 을 Allstate 로 잡으면 안 된다 (제외목록)."""
    table = _us_table()
    assert extract_us("ALL of the gains", table) == []


def test_extract_us_matches_uppercase_ticker():
    table = _us_table()
    hits = extract_us("AAPL beat estimates", table)
    assert hits == [{"name": "Apple", "symbol": "AAPL"}]


def test_extract_us_lowercase_ticker_not_matched():
    table = _us_table()
    assert extract_us("aapl beat estimates", table) == []


def test_extract_us_excludes_tickers_shorter_than_min_len():
    """GE 처럼 2글자 티커는 오탐이 압도적이라 기본 제외한다."""
    table = _us_table()
    assert extract_us("GE reported earnings", table) == []


def test_extract_us_dedupes_same_symbol_matched_twice():
    table = _us_table()
    hits = extract_us("Nvidia (NVDA) hit a new high on NVDA strength", table)
    assert len(hits) == 1
    assert hits[0]["symbol"] == "NVDA"


def _seed_us_caches(tmp_path: Path) -> None:
    """S&P500 + 나스닥 심볼 디렉터리 캐시를 모두 깔아 네트워크를 타지 않게 한다."""
    for name in ("sp500_list.html", "symdir_nasdaqlisted.txt", "symdir_otherlisted.txt"):
        (tmp_path / name).write_text(
            (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def test_load_us_table_reuses_cache(tmp_path: Path):
    _seed_us_caches(tmp_path)

    # 캐시가 이미 있으니 네트워크를 타지 않는다 — 타면 client() 가 예외를 낸다.
    table = load_us_table(tmp_path)

    # S&P500 항목은 그대로 앞쪽에 있고 이름도 위키피디아 표기를 유지한다
    assert table[: len(_us_table())] == _us_table()


def test_us_table_is_extended_beyond_sp500(tmp_path: Path):
    """S&P500 밖 종목(CoreWeave·SpaceX)이 사전에 들어와야 뉴스에서 잡힌다.

    2026-08-13 실측: CoreWeave 는 하루치 헤드라인에 6번 나오는데 사전에 없어
    "뉴스 노출 상위 종목"에 영영 못 올라왔다.
    """
    _seed_us_caches(tmp_path)
    names = {sym: name for name, sym in load_us_table(tmp_path)}

    assert names["CRWV"] == "CoreWeave"          # 증권 종류 접미사가 벗겨졌다
    assert names["SPCX"] == "Space Exploration Technologies"
    assert names["NVDA"] == "Nvidia"             # S&P500 이름이 우선(나스닥 표기 아님)
    assert "ZTEST" not in names                  # Test Issue=Y 는 제외


def test_extended_table_extracts_a_non_sp500_company(tmp_path: Path):
    _seed_us_caches(tmp_path)
    table = load_us_table(tmp_path)
    hits = {h["symbol"] for h in extract_us("Nvidia and CoreWeave surge on AI demand", table)}
    assert hits == {"NVDA", "CRWV"}


def test_symbol_dir_rejects_a_changed_format():
    """형식이 바뀌면 조용히 0건이 아니라 예외여야 한다."""
    with pytest.raises(ValueError):
        parse_symbol_dir("Foo|Bar\nA|B\n")


# ── 회사명 오탐 방어 (2026-08-12 실측 회귀) ──────────────────

import pytest  # noqa: E402

from quant.analyze.entities import AMBIGUOUS_NAMES, MIN_NAME_LEN  # noqa: E402


@pytest.fixture(scope="module")
def us_table():
    from pathlib import Path

    from quant.analyze.entities import build_us_table, parse_us_list

    fixture = Path(__file__).parent / "fixtures" / "sp500_list.html"
    return build_us_table(parse_us_list(fixture.read_text(encoding="utf-8")))


@pytest.mark.parametrize(
    "headline,note",
    [
        ("Duolingo started at buy with $222 price target at Seaport", "price target → Target"),
        ("DynaResource launches $3M private offering", "$3M → 3M"),
        ("Super Micro's earnings report brings more good news", "good news → News Corp"),
    ],
)
def test_common_words_are_not_company_names(us_table, headline, note):
    """미국 원문 97건 중 실제로 나왔던 오탐들 — 다시 잡히면 안 된다."""
    assert extract_us(headline, us_table) == [], note


def test_dow_index_is_not_dow_inc(us_table):
    """'Dow Dips' 는 다우존스 지수다. Dow Inc 로 잡으면 원장이 오염된다."""
    hits = {h["symbol"] for h in extract_us("Stock Market Today: Dow Dips, Nvidia Falls", us_table)}
    assert "DOW" not in hits
    assert "NVDA" in hits  # 같은 문장의 진짜 종목은 살아야 한다


def test_lowercase_company_name_is_rejected(us_table):
    """전부 소문자면 보통명사로 쓰인 것이다."""
    assert extract_us("shares of microsoft rose", us_table) == []
    assert [h["symbol"] for h in extract_us("Microsoft rose", us_table)] == ["MSFT"]


def test_all_caps_company_name_still_matches(us_table):
    """헤드라인은 'NVIDIA' 처럼 전부 대문자로 쓰기도 한다."""
    assert [h["symbol"] for h in extract_us("NVIDIA surged", us_table)] == ["NVDA"]


def test_short_names_are_excluded(us_table):
    assert all(len(n) >= MIN_NAME_LEN for n, _ in us_table if n)


def test_ambiguous_names_are_not_in_table(us_table):
    assert not ({n for n, _ in us_table} & AMBIGUOUS_NAMES)


# ---------------------------------------------------------------------------
# 종목코드 → 회사명 (실시간 랭킹 표시용)
# 토스 랭킹 API는 symbol·price·change_pct·rank만 내려주고 **이름이 없다** —
# 그대로 두면 랭킹 표가 종목코드로만 보인다(2026-08-13 사용자 지적).
# ---------------------------------------------------------------------------

from quant.analyze.entities import _preferred_share_names


def test_preferred_share_code_follows_the_krx_convention():
    """구형 우선주는 보통주 코드의 끝자리 0을 5로 바꾼 코드를 쓴다."""
    base = {"002990": "금호건설", "005930": "삼성전자"}
    out = _preferred_share_names(base)
    assert out["002995"] == "금호건설우"
    assert out["005935"] == "삼성전자우"


def test_preferred_name_is_not_invented_when_the_common_share_is_unknown():
    """끝자리가 5인 아무 코드에나 이름을 붙이지 않는다 — 규약이지 유추가 아니다."""
    assert _preferred_share_names({}) == {}
    # 보통주가 사전에 없으면 우선주도 만들지 않는다
    assert "123455" not in _preferred_share_names({"999990": "어떤회사"})


def test_existing_preferred_entry_is_not_overwritten():
    """사전에 이미 실명이 있으면 규약 유도본이 덮어쓰지 않는다."""
    base = {"002990": "금호건설", "002995": "금호건설우선주(실명)"}
    assert "002995" not in _preferred_share_names(base)


def test_non_zero_ending_codes_are_left_alone():
    """2우선주(끝자리 7)·신형우선주(문자 포함)는 규약이 갈려 손대지 않는다."""
    out = _preferred_share_names({"002997": "어떤2우B", "0193L0": "무언가"})
    assert out == {}
