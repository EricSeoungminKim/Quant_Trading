"""`quant.analyze.scalp_grade.grade_scalp` — 당일 단타 후보 3단계 행동 등급."""
from __future__ import annotations

from quant.analyze.scalp_grade import (
    GRADE_AGGRESSIVE,
    GRADE_ENTER,
    GRADE_HOLD,
    grade_scalp,
)


def test_all_signals_present_is_aggressive():
    result = grade_scalp(
        symbol_bullish=True, telegram_mentions=3, sector_bullish_hits=1,
        sector_rising=True, foreign_net_buying=True,
    )
    assert result.grade == GRADE_AGGRESSIVE
    assert "종목 호재 뉴스" in result.reasons
    assert any("텔레그램 언급" in r for r in result.reasons)


def test_aggressive_via_sector_hot_path_without_telegram():
    """텔레그램 언급이 없어도 섹터 호재 ≥3이면 적극 진입 대체 경로를 탄다."""
    result = grade_scalp(
        symbol_bullish=True, telegram_mentions=0, sector_bullish_hits=3,
        sector_rising=True, foreign_net_buying=True,
    )
    assert result.grade == GRADE_AGGRESSIVE
    assert any("섹터 호재" in r for r in result.reasons)


def test_aggressive_requires_symbol_bullish():
    """종목 자체 호재가 없으면 나머지 신호가 다 있어도 적극 진입이 아니다."""
    result = grade_scalp(
        symbol_bullish=False, telegram_mentions=5, sector_bullish_hits=5,
        sector_rising=True, foreign_net_buying=True,
    )
    assert result.grade != GRADE_AGGRESSIVE


def test_aggressive_requires_sector_rising():
    result = grade_scalp(
        symbol_bullish=True, telegram_mentions=3, sector_bullish_hits=0,
        sector_rising=False, foreign_net_buying=True,
    )
    assert result.grade != GRADE_AGGRESSIVE


def test_aggressive_requires_foreign_net_buying_strictly_true():
    """외국인 수급 데이터가 없으면(None) 적극 진입은 아니다 — 진입까지만."""
    result = grade_scalp(
        symbol_bullish=True, telegram_mentions=3, sector_bullish_hits=0,
        sector_rising=True, foreign_net_buying=None,
    )
    assert result.grade == GRADE_ENTER


def test_symbol_bullish_alone_with_no_foreign_selling_is_enter():
    result = grade_scalp(symbol_bullish=True, foreign_net_buying=None)
    assert result.grade == GRADE_ENTER
    assert "종목 호재 뉴스" in result.reasons


def test_sector_bullish_and_rising_without_symbol_news_is_enter():
    result = grade_scalp(
        symbol_bullish=False, sector_bullish_hits=1, sector_rising=True,
        foreign_net_buying=True,
    )
    assert result.grade == GRADE_ENTER


def test_enter_blocked_by_explicit_foreign_selling():
    """진입 조건을 만족해도 외국인이 명시적으로 순매도면 진입이 아니다(보류로 강등)."""
    result = grade_scalp(symbol_bullish=True, foreign_net_buying=False)
    assert result.grade == GRADE_HOLD
    assert "종목 호재 뉴스" in result.reasons
    assert "외국인 순매도" in result.reasons


def test_partial_signal_only_telegram_is_hold():
    result = grade_scalp(symbol_bullish=False, telegram_mentions=2)
    assert result.grade == GRADE_HOLD
    assert result.reasons == ["텔레그램 언급 2건"]


def test_partial_signal_only_sector_rising_is_hold():
    result = grade_scalp(symbol_bullish=False, sector_rising=True)
    assert result.grade == GRADE_HOLD


def test_no_signal_at_all_is_none_hidden():
    result = grade_scalp(symbol_bullish=False)
    assert result is None


def test_no_signal_with_explicit_negatives_is_none_hidden():
    result = grade_scalp(
        symbol_bullish=False, telegram_mentions=0, sector_bullish_hits=0,
        sector_rising=False, foreign_net_buying=False,
    )
    assert result is None
