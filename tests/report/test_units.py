from quant.analyze.units import (
    CATEGORY_ORDER,
    SYMBOL_META,
    fmt_fred,
    fmt_krw_eok,
    fmt_shares,
    fmt_value,
    group_quotes,
)


def test_group_quotes_orders_and_drops_empty_categories():
    quotes = {
        "^GSPC": {"label": "S&P500", "close": 7728.2},
        "^KS11": {"label": "KOSPI", "close": 6589.06},
        "BTC-USD": {"label": "비트코인", "close": 63607.48},
    }
    grouped = group_quotes(quotes)
    cats = [cat for cat, _ in grouped]
    # 순서는 CATEGORY_ORDER 를 따르고, 심볼이 없는 카테고리(아시아 증시,
    # 변동성·금리, 환율, 원자재)는 결과에서 빠진다.
    assert cats == ["한국 증시", "미국 증시", "디지털자산"]


def test_group_quotes_collects_unregistered_symbols_into_etc():
    quotes = {
        "^KS11": {"close": 6589.06},
        "NEWSYM": {"close": 1.0},
    }
    grouped = group_quotes(quotes)
    cats = [cat for cat, _ in grouped]
    assert cats == ["한국 증시", "기타"]
    etc_syms = dict(grouped[-1][1])
    assert "NEWSYM" in etc_syms


def test_fmt_value_pt():
    assert fmt_value("^KS11", 6589.06) == "6,589.06 pt"


def test_fmt_value_krw():
    assert fmt_value("KRW=X", 1415.54) == "1,415.54원"


def test_fmt_value_percent():
    assert fmt_value("^TNX", 4.72) == "4.72%"


def test_fmt_value_commodity_unit():
    assert fmt_value("CL=F", 83.92) == "83.92 달러/배럴"


def test_fmt_value_none_is_dash():
    assert fmt_value("^KS11", None) == "—"


def test_fmt_fred_scales_millions_to_trillions():
    assert fmt_fred("WALCL", 6748567) == "6.75조 달러"


def test_fmt_fred_percent():
    assert fmt_fred("DGS10", 4.72) == "4.72%"


def test_fmt_fred_unknown_series_returns_comma_number_without_error():
    result = fmt_fred("UNKNOWN", 1234.5)
    assert "1,234" in result


def test_fmt_krw_eok():
    assert fmt_krw_eok(26395) == "+2조 6,395억원"
    assert fmt_krw_eok(-708) == "-708억원"
    assert fmt_krw_eok(0) == "0억원"
    assert fmt_krw_eok(7887) == "+7,887억원"


def test_fmt_shares():
    assert fmt_shares(2462529) == "+246만주"
    assert fmt_shares(-4504862) == "-450만주"
    assert fmt_shares(12345) == "+1.2만주"
    assert fmt_shares(850) == "+850주"


def test_all_symbol_meta_categories_are_registered():
    for symbol, m in SYMBOL_META.items():
        assert m["category"] in CATEGORY_ORDER, (
            f"{symbol} 의 category {m['category']!r} 가 CATEGORY_ORDER 에 없다"
        )
