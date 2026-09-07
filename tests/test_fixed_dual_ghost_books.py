"""2026-09-07 — fixed_dual 기동 시 미참여 통화의 유령 장부 치유(미국 전용 레인의 KRW 933,411원)."""
import json

from quant.apps.assembly import _heal_ghost_currency_books
from quant.trade.risk.books import StrategyBooks


def _books(tmp_path, data):
    p = tmp_path / "strategy_books.json"
    p.write_text(json.dumps({"version": 2, "initial_krw": 933411.0, "books": data}), encoding="utf-8")
    return StrategyBooks.load(p, initial_krw=0.0)


def test_ghost_krw_book_on_us_only_lane_is_zeroed(tmp_path):
    b = _books(tmp_path, {
        "gap_fade": {"cash_krw": 933411.0, "initial_krw": 933411.0, "realized_pnl_krw": 0.0, "fees_krw": 0.0,
                     "cash_usd": 10000.0, "initial_usd": 10000.0, "realized_pnl_usd": 0.0, "fees_usd": 0.0, "positions": {}},
        "scalp_1m": {"cash_krw": 9800000.0, "initial_krw": 10000000.0, "realized_pnl_krw": -130000.0, "fees_krw": 45000.0,
                     "cash_usd": 10000.0, "initial_usd": 10000.0, "realized_pnl_usd": 0.0, "fees_usd": 0.0, "positions": {}},
    })
    healed = _heal_ghost_currency_books(b, ["gap_fade", "scalp_1m"], {"scalp_1m": 1e7}, {"gap_fade": 1e4, "scalp_1m": 1e4})
    assert healed == ["gap_fade:KRW"]
    assert b.books["gap_fade"]["cash_krw"] == 0.0 and b.books["gap_fade"]["initial_krw"] == 0.0
    assert b.books["gap_fade"]["cash_usd"] == 10000.0
    assert b.books["scalp_1m"]["cash_krw"] == 9800000.0  # 참여 레인은 그대로


def test_ghost_book_with_activity_is_left_alone(tmp_path):
    b = _books(tmp_path, {
        "gap_fade": {"cash_krw": 900000.0, "initial_krw": 933411.0, "realized_pnl_krw": -33411.0, "fees_krw": 0.0,
                     "cash_usd": 10000.0, "initial_usd": 10000.0, "realized_pnl_usd": 0.0, "fees_usd": 0.0, "positions": {}},
    })
    assert _heal_ghost_currency_books(b, ["gap_fade"], {}, {"gap_fade": 1e4}) == []
    assert b.books["gap_fade"]["cash_krw"] == 900000.0


def test_load_fallback_no_longer_seeds_ghost_books(tmp_path):
    """load() 가 파일의 initial_krw 를 되살려도 initial_krw=0 + 명시 0.0 항목이면 새 장부는 0."""
    b = _books(tmp_path, {})
    b.initial_krw = 0.0
    b.dual_currency = True
    b.initial_by_strategy = {"gap_fade": 0.0}
    b.initial_by_strategy_usd = {"gap_fade": 10000.0}
    b.seed(["gap_fade"])
    assert b.books["gap_fade"]["initial_krw"] == 0.0 and b.books["gap_fade"]["initial_usd"] == 10000.0
