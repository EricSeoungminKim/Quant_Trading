"""전략별 독립 명목계정 장부(StrategyBooks) 단위 테스트.

고정하는 것:
- 두 전략이 같은 종목을 사도 장부가 독립적으로 움직인다.
- 매도 시 실현손익이 그 전략 장부에만 반영된다.
- KR/US 혼합 시 avg_cost는 현지통화로 보존되고 KRW 환산은 조회 시점 fx를 쓴다.
- 장부 파일이 없을 때 초기화되고, 재시작 후(load) 복원된다.
- 저장은 원자적이다(tmp write + rename), 손상된 파일은 빈 장부로 안전하게 강등된다.
"""
from __future__ import annotations

import json

import pytest

from quant.core.fx import FixedFxProvider
from quant.core.models import Side
from quant.trade.risk.books import StrategyBooks

FX_RATE = 1500.0
INITIAL_KRW = 10_000_000.0


def _books(tmp_path, initial_krw: float = INITIAL_KRW) -> StrategyBooks:
    return StrategyBooks.load(tmp_path / "strategy_books.json", initial_krw=initial_krw)


# --------------------------------------------------------------- 초기화/복원

def test_missing_file_initializes_empty(tmp_path):
    books = _books(tmp_path)
    assert books.books == {}
    assert books.available_cash_krw("donchian") == pytest.approx(INITIAL_KRW)


def test_new_strategy_gets_initial_krw(tmp_path):
    books = _books(tmp_path)
    books.apply_fill("s", "TQQQ", Side.BUY, 1.0, 10.0, 0.0, "US", FixedFxProvider(FX_RATE))
    # 새 전략이 처음 등장하면 initial_krw에서 시작해 이번 매수만큼 차감된다.
    expected_cash = INITIAL_KRW - 1.0 * 10.0 * FX_RATE
    assert books.books["s"]["cash_krw"] == pytest.approx(expected_cash)


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "strategy_books.json"
    books = StrategyBooks.load(path, initial_krw=INITIAL_KRW)
    books.apply_fill("s", "TQQQ", Side.BUY, 2.0, 10.0, 1.0, "US", FixedFxProvider(FX_RATE))
    books.save()

    assert path.exists()
    restored = StrategyBooks.load(path, initial_krw=INITIAL_KRW)
    assert restored.books["s"]["positions"]["TQQQ"]["qty"] == pytest.approx(2.0)
    assert restored.books["s"]["cash_krw"] == books.books["s"]["cash_krw"]


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    path = tmp_path / "strategy_books.json"
    books = StrategyBooks.load(path, initial_krw=INITIAL_KRW)
    books.apply_fill("s", "TQQQ", Side.BUY, 1.0, 10.0, 0.0, "US", FixedFxProvider(FX_RATE))
    books.save()
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()


def test_corrupted_file_degrades_to_empty_books(tmp_path):
    path = tmp_path / "strategy_books.json"
    path.write_text("{not valid json", encoding="utf-8")
    books = StrategyBooks.load(path, initial_krw=INITIAL_KRW)
    assert books.books == {}


# ---------------------------------------------------------- 전략 간 독립성

def test_two_strategies_buying_same_symbol_stay_independent(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)

    books.apply_fill("a", "TQQQ", Side.BUY, 10.0, 50.0, 0.0, "US", fx)
    books.apply_fill("b", "TQQQ", Side.BUY, 3.0, 50.0, 0.0, "US", fx)

    assert books.books["a"]["positions"]["TQQQ"]["qty"] == pytest.approx(10.0)
    assert books.books["b"]["positions"]["TQQQ"]["qty"] == pytest.approx(3.0)
    # a의 대량 매수가 b의 현금에 영향을 주지 않는다.
    assert books.books["b"]["cash_krw"] == pytest.approx(INITIAL_KRW - 3.0 * 50.0 * FX_RATE)


def test_one_strategy_exhausting_cash_does_not_affect_other(tmp_path):
    """한 전략이 자기 1,000만원을 다 쓰는 것은 이 모듈의 책임 밖(risk.approve의
    현금 게이트가 막는다)이지만, 장부 자체는 마이너스로 가더라도 다른 전략
    장부에 영향을 주지 않아야 한다."""
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)

    books.apply_fill("a", "TQQQ", Side.BUY, 1000.0, 1000.0, 0.0, "US", fx)  # a: 현금 크게 소모

    assert books.available_cash_krw("b") == pytest.approx(INITIAL_KRW)


# ------------------------------------------------------------- 실현손익 분리

def test_realized_pnl_isolated_to_selling_strategy(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)

    books.apply_fill("a", "TQQQ", Side.BUY, 10.0, 100.0, 0.0, "US", fx)
    books.apply_fill("b", "TQQQ", Side.BUY, 10.0, 100.0, 0.0, "US", fx)
    books.apply_fill("a", "TQQQ", Side.SELL, 10.0, 150.0, 0.0, "US", fx)  # a만 익절

    assert books.books["a"]["realized_pnl_krw"] == pytest.approx(10.0 * 50.0 * FX_RATE)
    assert books.books["b"]["realized_pnl_krw"] == pytest.approx(0.0)


def test_sell_credits_cash_and_clears_position_on_full_exit(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "TQQQ", Side.BUY, 5.0, 100.0, 0.0, "US", fx)
    books.apply_fill("a", "TQQQ", Side.SELL, 5.0, 120.0, 1.0, "US", fx)

    assert "TQQQ" not in books.books["a"]["positions"]
    expected_cash = INITIAL_KRW - 5 * 100 * FX_RATE + (5 * 120 - 1) * FX_RATE
    assert books.books["a"]["cash_krw"] == pytest.approx(expected_cash)


def test_partial_sell_keeps_avg_cost(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "TQQQ", Side.BUY, 10.0, 100.0, 0.0, "US", fx)
    books.apply_fill("a", "TQQQ", Side.SELL, 4.0, 120.0, 0.0, "US", fx)

    pos = books.books["a"]["positions"]["TQQQ"]
    assert pos["qty"] == pytest.approx(6.0)
    assert pos["avg_cost"] == pytest.approx(100.0)  # 원가는 매도로 안 변한다


# -------------------------------------------------- KR/US 혼합 · 통화 순수성

def test_avg_cost_preserved_in_local_currency_across_fx_changes(tmp_path):
    """avg_cost는 현지통화 그대로 저장되고, 환율이 바뀌어도 과거 매입가는
    바뀌지 않는다 — KRW 환산은 equity_krw() 조회 시점에만 일어난다."""
    books = _books(tmp_path)
    books.apply_fill("a", "AAPL", Side.BUY, 10.0, 200.0, 0.0, "US", FixedFxProvider(1400.0))

    assert books.books["a"]["positions"]["AAPL"]["avg_cost"] == pytest.approx(200.0)

    # 환율이 크게 움직인 뒤에도 저장된 avg_cost(현지통화)는 그대로다.
    equity_low_fx = books.equity_krw("a", {"AAPL": 200.0}, FixedFxProvider(1400.0))
    equity_high_fx = books.equity_krw("a", {"AAPL": 200.0}, FixedFxProvider(1600.0))
    assert books.books["a"]["positions"]["AAPL"]["avg_cost"] == pytest.approx(200.0)
    assert equity_high_fx > equity_low_fx  # 평가액은 환율을 그때그때 반영한다


def test_kr_symbol_not_fx_converted(tmp_path):
    """KR 종목은 원화 그대로라 fx를 곱하지 않는다."""
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "005930", Side.BUY, 10.0, 70_000.0, 0.0, "KR", fx)

    expected_cash = INITIAL_KRW - 10 * 70_000.0
    assert books.books["a"]["cash_krw"] == pytest.approx(expected_cash)


def test_equity_krw_mixes_kr_and_us_positions(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "005930", Side.BUY, 10.0, 70_000.0, 0.0, "KR", fx)
    books.apply_fill("a", "AAPL", Side.BUY, 5.0, 200.0, 0.0, "US", fx)

    equity = books.equity_krw("a", {"005930": 70_000.0, "AAPL": 200.0}, fx)
    cash = books.books["a"]["cash_krw"]
    expected = cash + 10 * 70_000.0 + 5 * 200.0 * FX_RATE
    assert equity == pytest.approx(expected)


def test_equity_krw_falls_back_to_avg_cost_when_mark_missing(tmp_path):
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "AAPL", Side.BUY, 5.0, 200.0, 0.0, "US", fx)

    equity_no_marks = books.equity_krw("a", {}, fx)
    equity_with_marks = books.equity_krw("a", {"AAPL": 200.0}, fx)
    assert equity_no_marks == pytest.approx(equity_with_marks)


def test_equity_krw_new_strategy_equals_initial(tmp_path):
    books = _books(tmp_path)
    assert books.equity_krw("never-traded", {}, FixedFxProvider(FX_RATE)) == pytest.approx(INITIAL_KRW)


def test_seed_creates_starting_line_for_untraded_strategies(tmp_path):
    """기동 시 출발선을 세운다. 체결이 나야 장부가 생기면 첫 거래 전까지 전략별
    리포트가 '장부 없음'으로 비어 보이고, '전략마다 1,000만원이 어떻게 자라는지'를
    처음부터 본다는 목적이 절반 사라진다."""
    books = _books(tmp_path)
    created = books.seed(["a", "b", "c"])

    assert created == 3
    assert set(books.books) == {"a", "b", "c"}
    for sid in ("a", "b", "c"):
        assert books.books[sid]["cash_krw"] == pytest.approx(INITIAL_KRW)
        assert books.books[sid]["positions"] == {}


def test_seed_does_not_touch_existing_books(tmp_path):
    """재기동 때 이미 굴러간 장부를 초기화하면 그날 손익이 통째로 사라진다."""
    books = _books(tmp_path)
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("a", "005930", Side.BUY, 10.0, 70_000.0, 100.0, "KR", fx)
    cash_before = books.books["a"]["cash_krw"]

    created = books.seed(["a", "b"])

    assert created == 1  # b 만 새로 생긴다
    assert books.books["a"]["cash_krw"] == pytest.approx(cash_before)
    assert books.books["a"]["positions"]["005930"]["qty"] == pytest.approx(10.0)


# ============================================== capital_policy: fixed_dual (2026-09-06)

def _dual_books(tmp_path, initial_by_krw=None, initial_by_usd=None) -> StrategyBooks:
    books = StrategyBooks.load(tmp_path / "strategy_books.json", initial_krw=0.0)
    books.dual_currency = True
    books.initial_by_strategy = dict(initial_by_krw or {})
    books.initial_by_strategy_usd = dict(initial_by_usd or {})
    return books


def test_dual_currency_seeds_krw_and_usd_independently(tmp_path):
    """양 시장 모두 계좌가 있는 전략은 두 화폐 필드를 각자 시작금으로 받는다."""
    books = _dual_books(tmp_path, {"scalp_1m": 10_000_000.0}, {"scalp_1m": 10_000.0})
    book = books.books.setdefault("scalp_1m", books._ensure("scalp_1m"))
    assert book["cash_krw"] == pytest.approx(10_000_000.0)
    assert book["cash_usd"] == pytest.approx(10_000.0)
    assert book["initial_krw"] == pytest.approx(10_000_000.0)
    assert book["initial_usd"] == pytest.approx(10_000.0)


def test_dual_currency_us_only_strategy_has_zero_krw_book(tmp_path):
    """KR 참여가 없는 전략(예: letf_pair_qqq)은 cash_krw가 0으로 남는다 —
    KR 지갑 자체가 없다는 뜻이지, 잔고가 부족하다는 뜻이 아니다."""
    books = _dual_books(tmp_path, {}, {"letf_pair_qqq": 10_000.0})
    book = books._ensure("letf_pair_qqq")
    assert book["cash_krw"] == pytest.approx(0.0)
    assert book["cash_usd"] == pytest.approx(10_000.0)


def test_dual_currency_us_fill_moves_usd_wallet_only_no_fx_conversion(tmp_path):
    """dual_currency=True에서 US 체결은 cash_usd만 달러 그대로 움직이고
    cash_krw는 건드리지 않는다 — 환전 코드가 없다는 것의 직접 증거."""
    books = _dual_books(tmp_path, {"scalp_1m": 10_000_000.0}, {"scalp_1m": 10_000.0})
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("scalp_1m", "TQQQ", Side.BUY, 10.0, 70.0, 1.0, "US", fx)

    book = books.books["scalp_1m"]
    assert book["cash_usd"] == pytest.approx(10_000.0 - 10 * 70.0 - 1.0)
    assert book["cash_krw"] == pytest.approx(10_000_000.0), "US 체결이 KRW 지갑을 건드렸다"
    assert book["fees_usd"] == pytest.approx(1.0)
    assert book["fees_krw"] == pytest.approx(0.0)


def test_dual_currency_kr_fill_moves_krw_wallet_only(tmp_path):
    books = _dual_books(tmp_path, {"scalp_1m": 10_000_000.0}, {"scalp_1m": 10_000.0})
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("scalp_1m", "005930", Side.BUY, 10.0, 70_000.0, 100.0, "KR", fx)

    book = books.books["scalp_1m"]
    assert book["cash_krw"] == pytest.approx(10_000_000.0 - 10 * 70_000.0 - 100.0)
    assert book["cash_usd"] == pytest.approx(10_000.0), "KR 체결이 USD 지갑을 건드렸다"


def test_dual_currency_realized_pnl_tracked_in_native_currency(tmp_path):
    books = _dual_books(tmp_path, {}, {"gap_fade": 10_000.0})
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("gap_fade", "TQQQ", Side.BUY, 10.0, 70.0, 0.0, "US", fx)
    books.apply_fill("gap_fade", "TQQQ", Side.SELL, 10.0, 75.0, 0.0, "US", fx)

    book = books.books["gap_fade"]
    assert book["realized_pnl_usd"] == pytest.approx(50.0)
    assert book["realized_pnl_krw"] == pytest.approx(0.0)


def test_available_cash_krw_for_market_returns_native_wallet_converted(tmp_path):
    """fixed_dual: 시장별 지갑만 KRW로 환산해 돌려준다 — 다른 시장 지갑은
    절대 섞이지 않는다(risk/manager.py 현금 게이트가 이 값을 그대로 쓴다)."""
    books = _dual_books(tmp_path, {"scalp_1m": 10_000_000.0}, {"scalp_1m": 10_000.0})
    fx = FixedFxProvider(FX_RATE)

    assert books.available_cash_krw_for_market("scalp_1m", "KR", fx) == pytest.approx(10_000_000.0)
    assert books.available_cash_krw_for_market("scalp_1m", "US", fx) == pytest.approx(10_000.0 * FX_RATE)

    # US 지갑을 다 써도 KR 지갑은 전혀 줄지 않는다.
    books.apply_fill("scalp_1m", "TQQQ", Side.BUY, 140.0, 70.0, 20.0, "US", fx)  # ~9,820 USD 소진
    assert books.available_cash_krw_for_market("scalp_1m", "KR", fx) == pytest.approx(10_000_000.0)


def test_available_cash_krw_for_market_matches_available_cash_krw_when_not_dual(tmp_path):
    """dual_currency=False(fixed/equal_split/declared)에서는 market 인자와
    무관하게 기존 available_cash_krw와 완전히 같은 값 — 동작 불변."""
    books = _books(tmp_path)  # dual_currency 기본값 False
    fx = FixedFxProvider(FX_RATE)
    books.apply_fill("s", "TQQQ", Side.BUY, 1.0, 10.0, 0.0, "US", fx)

    expected = books.available_cash_krw("s")
    assert books.available_cash_krw_for_market("s", "KR", fx) == pytest.approx(expected)
    assert books.available_cash_krw_for_market("s", "US", fx) == pytest.approx(expected)


def test_equity_krw_sums_both_wallets_under_dual_currency(tmp_path):
    """equity는 두 통화 장부의 합(성과는 합쳐서 본다) — 현금 게이트만 분리된다."""
    books = _dual_books(tmp_path, {"scalp_1m": 10_000_000.0}, {"scalp_1m": 10_000.0})
    fx = FixedFxProvider(FX_RATE)
    equity = books.equity_krw("scalp_1m", {}, fx)
    assert equity == pytest.approx(10_000_000.0 + 10_000.0 * FX_RATE)


def test_loading_a_pre_dual_currency_books_file_backfills_usd_fields(tmp_path):
    """구버전 books.json(달러 필드 없음)을 fixed_dual로 불러와도 크래시하지
    않고 0.0으로 채워진다 — 하위호환 로딩."""
    path = tmp_path / "strategy_books.json"
    path.write_text(json.dumps({
        "version": 1, "initial_krw": 10_000_000.0,
        "books": {
            "scalp_1m": {
                "cash_krw": 9_500_000.0, "initial_krw": 10_000_000.0,
                "realized_pnl_krw": -500_000.0, "fees_krw": 1000.0,
                "positions": {}, "updated": "2026-09-01T00:00:00+00:00",
            },
        },
    }), encoding="utf-8")

    books = StrategyBooks.load(path, initial_krw=10_000_000.0)
    books.dual_currency = True
    book = books._ensure("scalp_1m")
    assert book["cash_usd"] == pytest.approx(0.0)
    assert book["initial_usd"] == pytest.approx(0.0)
    assert book["cash_krw"] == pytest.approx(9_500_000.0), "기존 KRW 값은 보존돼야 한다"
