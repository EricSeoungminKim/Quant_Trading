"""`quant.report.lint.lint_trade_review_symbols` — 매매 리뷰/마감요약 7절 카드의
종목명 완비성. `_lint_symbol_completeness`(engine.json)와 같은 KR/US 비대칭
규율을 공유 스키마(`symbol`/`name`/`market`)로 적용한다."""
from __future__ import annotations

from quant.report.lint import WARN, lint_trade_review_symbols


def _group(symbol: str, name: str | None, market: str) -> dict:
    return {"symbol": symbol, "name": name, "market": market}


def test_kr_group_without_name_is_a_warning():
    out = lint_trade_review_symbols([_group("005930", None, "KR")])
    assert len(out) == 1
    assert out[0].severity == WARN
    assert "005930" in out[0].message


def test_kr_group_with_name_equal_to_code_is_a_warning():
    out = lint_trade_review_symbols([_group("005930", "005930", "KR")])
    assert len(out) == 1


def test_kr_group_with_real_name_is_clean():
    out = lint_trade_review_symbols([_group("005930", "삼성전자", "KR")])
    assert out == []


def test_us_group_without_name_is_a_warning():
    out = lint_trade_review_symbols([_group("IBM", None, "US")])
    assert len(out) == 1


def test_us_group_with_name_equal_to_ticker_is_not_flagged():
    """S&P500 원표 자체가 IBM 같은 종목은 티커를 정식 표시명으로 쓴다 —
    KR과 달리 이름=코드가 오탐이 아니다."""
    out = lint_trade_review_symbols([_group("IBM", "IBM", "US")])
    assert out == []


def test_empty_and_none_input_is_clean():
    assert lint_trade_review_symbols([]) == []
    assert lint_trade_review_symbols(None) == []


def test_mixed_groups_only_flags_the_bad_one():
    out = lint_trade_review_symbols([
        _group("005930", "삼성전자", "KR"),
        _group("000660", None, "KR"),
        _group("AAPL", "Apple", "US"),
    ])
    assert len(out) == 1
    assert "000660" in out[0].message
