"""전략별 독립 명목계좌 성과 요약(quant.control.strategy_books) 테스트 — 전부 오프라인.

장부 파일은 병렬 작업자(리스크 평면, quant/trade/risk/books.py)가 쓴다 — 여기선
없음/비어있음/정상 3가지 상태를 픽스처로 만들어 읽기 전용 함수만 검증한다.

(파일명 주의: `tests/test_strategy_books.py`는 리스크 평면의 `StrategyBooks` 클래스
단위 테스트가 이미 그 이름을 쓰고 있어 충돌을 피해 이 파일명을 쓴다.)
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from quant.control.strategy_books import (
    load_strategy_books,
    strategy_books_messages,
    strategy_equity,
    strategy_records,
    strategy_trade_stats,
)
from quant.core.fx import FixedFxProvider
from quant.core.models import Side
from quant.trade.risk.books import StrategyBooks


def _write_books(tmp_path, books: dict, initial_krw: float = 10_000_000.0):
    p = tmp_path / "strategy_books.json"
    p.write_text(json.dumps({"version": 1, "initial_krw": initial_krw, "books": books},
                             ensure_ascii=False), encoding="utf-8")
    return p


# --- load_strategy_books -----------------------------------------------------

def test_load_missing_file_is_not_an_error(tmp_path):
    data = load_strategy_books(tmp_path / "does_not_exist.json")
    assert data["exists"] is False
    assert data["books"] == {}
    assert data["error"] is None


def test_load_empty_json_object(tmp_path):
    p = tmp_path / "strategy_books.json"
    p.write_text("{}", encoding="utf-8")
    data = load_strategy_books(p)
    assert data["exists"] is True
    assert data["books"] == {}
    assert data["error"] is None


def test_load_corrupt_json_reports_error_not_crash(tmp_path):
    p = tmp_path / "strategy_books.json"
    p.write_text("{not valid json", encoding="utf-8")
    data = load_strategy_books(p)
    assert data["exists"] is True
    assert data["error"] is not None
    assert data["books"] == {}


def test_load_normal_file(tmp_path):
    p = _write_books(tmp_path, {
        "donchian": {"cash_krw": 9_500_000.0, "realized_pnl_krw": 100_000.0,
                     "fees_krw": 5_000.0, "positions": {}, "updated": "2026-08-19T00:00:00+09:00"},
    })
    data = load_strategy_books(p)
    assert data["exists"] is True
    assert data["error"] is None
    assert data["initial_krw"] == 10_000_000.0
    assert "donchian" in data["books"]


# --- strategy_equity ----------------------------------------------------------

def test_equity_cash_only_no_positions():
    book = {"cash_krw": 10_000_000.0, "positions": {}}
    eq = strategy_equity(book, quotes={}, usd_krw=1500.0)
    assert eq["equity_krw"] == 10_000_000.0
    assert eq["unrealized_pnl_krw"] == 0.0
    assert eq["positions"] == []
    assert eq["missing_quotes"] == []


def test_equity_kr_position_priced_in_won():
    book = {
        "cash_krw": 5_000_000.0,
        "positions": {"069500": {"qty": 100.0, "avg_cost": 40000.0, "market": "KR"}},
    }
    eq = strategy_equity(book, quotes={"069500": 41000.0}, usd_krw=1500.0)
    # 평가금액 = 5,000,000 + 100*41000 = 9,100,000
    assert eq["equity_krw"] == 9_100_000.0
    # 미실현손익 = (41000-40000)*100 = 100,000
    assert eq["unrealized_pnl_krw"] == 100_000.0
    assert eq["missing_quotes"] == []


def test_equity_us_position_converted_to_krw():
    book = {
        "cash_krw": 5_000_000.0,
        "positions": {"TQQQ": {"qty": 10.0, "avg_cost": 70.0, "market": "US"}},
    }
    eq = strategy_equity(book, quotes={"TQQQ": 75.0}, usd_krw=1500.0)
    # 평가금액 = 5,000,000 + 10*75*1500 = 6,125,000
    assert eq["equity_krw"] == 6_125_000.0
    assert eq["unrealized_pnl_krw"] == (75.0 - 70.0) * 10 * 1500.0


def test_equity_missing_quote_excluded_not_zeroed():
    book = {
        "cash_krw": 5_000_000.0,
        "positions": {"005930": {"qty": 10.0, "avg_cost": 70000.0, "market": "KR"}},
    }
    eq = strategy_equity(book, quotes={}, usd_krw=1500.0)
    # 시세 없으면 그 포지션은 평가금액에 안 들어간다 — 0으로 위장하지 않는다
    assert eq["equity_krw"] == 5_000_000.0
    assert eq["missing_quotes"] == ["005930"]
    assert eq["positions"][0]["market_value_krw"] is None


# --- strategy_trade_stats -------------------------------------------------

def test_trade_stats_zero_trades_is_none_win_rate_not_zero_percent():
    stats = strategy_trade_stats([], "donchian")
    assert stats["n_trades"] == 0
    assert stats["win_rate"] is None, "표본 0건은 0%가 아니라 None(아직 거래 없음)이어야 오해가 없다"


def test_trade_stats_filters_by_strategy_and_computes_win_rate():
    trips = [
        {"strategy": "donchian", "pnl": 100.0, "pnl_known": True},
        {"strategy": "donchian", "pnl": -50.0, "pnl_known": True},
        {"strategy": "orb_scan", "pnl": 999.0, "pnl_known": True},
    ]
    stats = strategy_trade_stats(trips, "donchian")
    assert stats["n_trades"] == 2
    assert stats["wins"] == 1
    assert stats["win_rate"] == 0.5


def test_trade_stats_excludes_unknown_pnl():
    trips = [
        {"strategy": "donchian", "pnl": 100.0, "pnl_known": True},
        {"strategy": "donchian", "pnl": None, "pnl_known": False},
    ]
    stats = strategy_trade_stats(trips, "donchian")
    assert stats["n_trades"] == 1
    assert stats["n_unknown"] == 1


# --- strategy_records / strategy_books_messages ----------------------------

def test_messages_no_file_is_explicit_not_silent():
    data = load_strategy_books(  # 존재하지 않는 경로
        "definitely/does/not/exist/strategy_books.json"
    )
    messages = strategy_books_messages(data, trips=[], quotes={}, usd_krw=1500.0)
    assert len(messages) == 1
    assert "장부 파일 없음" in messages[0]


def test_messages_empty_books_is_explicit():
    data = {"exists": True, "books": {}, "initial_krw": 10_000_000.0, "error": None}
    messages = strategy_books_messages(data, trips=[], quotes={}, usd_krw=1500.0)
    assert len(messages) == 1
    assert "등록된 전략 없음" in messages[0]


def test_summary_ranks_by_return_descending():
    """두 전략 모두 거래·보유가 없어(2026-08-19 Phase C 접힘 규칙) 상세 메시지는
    안 나가지만, 순위는 요약 메시지 하나에 항상 전부 나온다."""
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {
            "loser": {"cash_krw": 9_000_000.0, "realized_pnl_krw": -1_000_000.0,
                      "fees_krw": 0.0, "positions": {}},
            "winner": {"cash_krw": 11_000_000.0, "realized_pnl_krw": 1_000_000.0,
                       "fees_krw": 0.0, "positions": {}},
        },
    }
    messages = strategy_books_messages(data, trips=[], quotes={}, usd_krw=1500.0)
    assert len(messages) == 1, "둘 다 거래·보유가 없으므로 상세 메시지 없이 요약만"
    summary = messages[0]
    winner_pos = summary.index("winner")
    loser_pos = summary.index("loser")
    assert winner_pos < loser_pos, "수익률 높은 전략이 먼저 나와야 한다"
    rank_line = next(line for line in summary.splitlines() if line.startswith("순위:"))
    assert "winner" in rank_line and "loser" in rank_line


def test_detail_zero_trades_shows_no_trades_not_zero_percent():
    """보유 종목이 있어(접히지 않음) 상세 메시지가 나가는 전략 — 체결이 아직
    없으면 '아직 거래 없음'이지 '승률 0%'가 아니어야 한다."""
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {"fresh": {"cash_krw": 6_500_000.0, "realized_pnl_krw": 0.0,
                             "fees_krw": 0.0,
                             "positions": {"TQQQ": {"qty": 5.0, "avg_cost": 70.0, "market": "US"}}}},
    }
    messages = strategy_books_messages(data, trips=[], quotes={"TQQQ": 70.0}, usd_krw=1500.0)
    assert len(messages) == 2
    detail = messages[1]
    assert "아직 거래 없음" in detail
    assert "승률 0%" not in detail


def test_detail_shows_holdings_with_pnl():
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {
            "donchian": {
                "cash_krw": 5_000_000.0, "realized_pnl_krw": 0.0, "fees_krw": 0.0,
                "positions": {"TQQQ": {"qty": 10.0, "avg_cost": 70.0, "market": "US"}},
            },
        },
    }
    messages = strategy_books_messages(data, trips=[], quotes={"TQQQ": 75.0}, usd_krw=1500.0)
    assert len(messages) == 2
    detail = messages[1]
    assert "TQQQ" in detail
    assert "10" in detail


def test_idle_strategy_folds_into_summary_line():
    """거래도 보유도 없는 전략은 상세 메시지를 받지 않고 요약에 이름만 남는다
    (2026-08-19 Phase C, 사용자 지침: "이해 안 되면 필요없는 정보")."""
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {
            "idle": {"cash_krw": 10_000_000.0, "realized_pnl_krw": 0.0,
                     "fees_krw": 0.0, "positions": {}},
            "active": {"cash_krw": 5_000_000.0, "realized_pnl_krw": 0.0, "fees_krw": 0.0,
                       "positions": {"TQQQ": {"qty": 10.0, "avg_cost": 70.0, "market": "US"}}},
        },
    }
    messages = strategy_books_messages(data, trips=[], quotes={"TQQQ": 70.0}, usd_krw=1500.0)
    assert len(messages) == 2, "active만 상세 메시지, idle은 요약에 접힌다"
    assert "idle" in messages[0] and "거래·보유 없음" in messages[0]
    assert all("idle" not in m for m in messages[1:]), "idle의 상세 메시지가 따로 나가면 안 된다"
    assert "active" in messages[1]


def test_records_return_pct_matches_equity_over_initial():
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {"donchian": {"cash_krw": 11_000_000.0, "realized_pnl_krw": 1_000_000.0,
                                "fees_krw": 0.0, "positions": {}}},
    }
    records = strategy_records(data, trips=[], quotes={}, usd_krw=1500.0)
    assert abs(records[0]["return_pct"] - 10.0) < 1e-9


# --- 항등식 불변 테스트 (2026-08-19, P0 코디네이터 지적) ------------------------
#
# equity_krw == initial_krw + realized_pnl_krw - fees_krw + unrealized_krw
#
# 손으로 만든 픽스처(cash_krw를 독립적으로 골라 씀)로는 이 항등식이 우연히
# 깨질 수 있다 — 실제로 그런 샘플 메시지가 나갔었다(2026-08-19 코디네이터
# 발견). 여기서는 픽스처를 손으로 짜지 않고 **진짜 생산자**
# (`quant.trade.risk.books.StrategyBooks.apply_fill`)로 체결을 시뮬레이션해
# 나온 book을 그대로 읽어, `strategy_equity()`(cash + 포지션 평가, 실제
# equity_krw()와 동일한 정의)가 이 항등식과 일치하는지 검증한다.
#
# 전제: fx가 매수~지금 사이 변하지 않아야 정확히 맞는다(원금의 환율 환산
# 시점이 매수 시점 vs 현재로 갈리면 그 차이만큼 어긋난다 — 이건 버그가 아니라
# 실제 통화환산 원가의 성격이다, `apply_fill` docstring 참고). 페이퍼 루프의
# `DailyFxProvider`는 거래일 안에서는 캐시된 고정 환율을 쓰므로 이 전제가
# 일상적인 케이스다. `FixedFxProvider`로 전 구간 고정해 이 전형적 케이스를 검증한다.

_FX = FixedFxProvider(1500.0)


def _identity_check(sb: StrategyBooks, strategy_id: str, marks: dict[str, float]) -> None:
    book = sb.books[strategy_id]
    real_equity = sb.equity_krw(strategy_id, marks=marks, fx=_FX)
    eq = strategy_equity(book, quotes=marks, usd_krw=_FX.usd_krw())
    assert abs(eq["equity_krw"] - real_equity) < 1e-6, "strategy_equity()가 진짜 equity_krw()와 어긋남"
    naive = (
        sb.initial_krw + book["realized_pnl_krw"] - book["fees_krw"] + eq["unrealized_pnl_krw"]
    )
    assert abs(eq["equity_krw"] - naive) < 1e-6, (
        f"항등식 깨짐: equity={eq['equity_krw']} vs "
        f"initial+realized-fees+unrealized={naive}"
    )


def test_identity_kr_only(tmp_path):
    sb = StrategyBooks(path=tmp_path / "b.json", initial_krw=10_000_000.0, books={})
    sb.apply_fill("s", "069500", Side.BUY, 10, 40000.0, 100.0, "KR", _FX)
    sb.apply_fill("s", "069500", Side.SELL, 10, 41000.0, 100.0, "KR", _FX)  # 종결 트립
    sb.apply_fill("s", "122630", Side.BUY, 5, 20000.0, 50.0, "KR", _FX)     # 미종결 보유
    _identity_check(sb, "s", marks={"122630": 20500.0})


def test_identity_us_only(tmp_path):
    sb = StrategyBooks(path=tmp_path / "b.json", initial_krw=10_000_000.0, books={})
    sb.apply_fill("s", "TQQQ", Side.BUY, 20, 68.0, 4.0, "US", _FX)
    sb.apply_fill("s", "TQQQ", Side.SELL, 20, 70.0, 4.0, "US", _FX)  # 종결 트립
    sb.apply_fill("s", "SQQQ", Side.BUY, 10, 10.0, 2.0, "US", _FX)   # 미종결 보유
    _identity_check(sb, "s", marks={"SQQQ": 10.5})


def test_identity_mixed_kr_and_us(tmp_path):
    sb = StrategyBooks(path=tmp_path / "b.json", initial_krw=10_000_000.0, books={})
    sb.apply_fill("s", "069500", Side.BUY, 10, 40000.0, 100.0, "KR", _FX)
    sb.apply_fill("s", "069500", Side.SELL, 8, 39000.0, 80.0, "KR", _FX)  # 부분 청산(손실) + 잔여 보유
    sb.apply_fill("s", "TQQQ", Side.BUY, 15, 70.0, 3.0, "US", _FX)
    sb.apply_fill("s", "TQQQ", Side.SELL, 15, 72.0, 3.0, "US", _FX)       # 종결 트립(이익)
    sb.apply_fill("s", "SQQQ", Side.BUY, 5, 12.0, 1.0, "US", _FX)         # 미종결 보유
    _identity_check(sb, "s", marks={"069500": 38500.0, "SQQQ": 11.5})


def test_identity_holds_across_multiple_strategies_independently(tmp_path):
    """전략 A의 체결이 전략 B의 장부에 절대 새지 않는지 — 항등식과 별개로,
    strategy_records()가 여러 전략을 한 번에 다룰 때도 각자 항등식을 지키는지."""
    sb = StrategyBooks(path=tmp_path / "b.json", initial_krw=10_000_000.0, books={})
    sb.apply_fill("winner", "TQQQ", Side.BUY, 10, 60.0, 2.0, "US", _FX)
    sb.apply_fill("winner", "TQQQ", Side.SELL, 10, 65.0, 2.0, "US", _FX)
    sb.apply_fill("loser", "005930", Side.BUY, 5, 70000.0, 100.0, "KR", _FX)
    sb.apply_fill("loser", "005930", Side.SELL, 5, 68000.0, 100.0, "KR", _FX)

    marks: dict[str, float] = {}
    books_data = {"exists": True, "error": None, "initial_krw": sb.initial_krw, "books": sb.books}
    records = strategy_records(books_data, trips=[], quotes=marks, usd_krw=_FX.usd_krw())
    for r in records:
        naive = sb.initial_krw + r["realized_pnl_krw"] - r["fees_krw"] + r["unrealized_pnl_krw"]
        assert abs(r["equity_krw"] - naive) < 1e-6, f"{r['strategy']}: 항등식 깨짐"
    winner = next(r for r in records if r["strategy"] == "winner")
    loser = next(r for r in records if r["strategy"] == "loser")
    assert winner["realized_pnl_krw"] > 0
    assert loser["realized_pnl_krw"] < 0


# --- 표본 작을 때 승률 노이즈 억제 (2026-08-19, 코디네이터 지적) --------------
#
# n=1~4건에서 승률 0%/100%는 정보가 아니라 잡음이다. 임계는 기존
# `ledger.MIN_TRIPS_FOR_JUDGEMENT`(=30, 누적 스코어보드가 이미 쓰는 값 —
# "이항비율 CI 폭이 1/sqrt(n) 규모"라는 같은 근거)를 재사용해 새 임계를
# 발명하지 않는다.

def test_detail_small_sample_win_rate_flagged_as_insufficient():
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {"news_scalp": {"cash_krw": 9_842_500.0, "realized_pnl_krw": -150_000.0,
                                  "fees_krw": 7_500.0, "positions": {}}},
    }
    trips = [{"strategy": "news_scalp", "pnl": -150_000.0, "pnl_known": True}]
    messages = strategy_books_messages(data, trips, quotes={}, usd_krw=1500.0)
    detail = messages[1]
    assert "표본 부족" in detail
    assert "승률 0%" in detail  # 승률 자체는 여전히 보여주되, 표본 부족 표시를 곁들인다


def test_detail_large_sample_win_rate_not_flagged():
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {"donchian": {"cash_krw": 10_000_000.0, "realized_pnl_krw": 0.0,
                                "fees_krw": 0.0, "positions": {}}},
    }
    trips = [{"strategy": "donchian", "pnl": 100.0, "pnl_known": True} for _ in range(31)]
    messages = strategy_books_messages(data, trips, quotes={}, usd_krw=1500.0)
    assert "표본 부족" not in messages[1]


def test_message_shows_cash_and_stock_split_so_the_10m_start_is_visible():
    """사용자 피드백(2026-08-19): 평가금액만 찍히니 "전략마다 1,000만원"이라는
    구조가 메시지에서 보이지 않았다. 시작금·현금·주식이 모두 드러나야 한다."""
    books_data = {
        "exists": True,
        "initial_krw": 10_000_000.0,
        "books": {
            "a": {
                "cash_krw": 4_000_000.0,
                "realized_pnl_krw": 0.0,
                "fees_krw": 0.0,
                "positions": {"005930": {"qty": 100.0, "avg_cost": 60_000.0, "market": "KR"}},
            },
        },
        "error": None,
    }
    messages = strategy_books_messages(books_data, [], {"005930": 60_000.0}, 1480.0)
    detail = messages[1]

    assert "시작 10,000,000원" in detail
    assert "현금 4,000,000원 + 주식 6,000,000원" in detail
    assert "평가 10,000,000원" in detail


def test_cash_plus_stock_equals_equity():
    """현금 + 주식 = 평가금액이 깨지면 사용자가 숫자를 검산할 수 없다."""
    book = {
        "cash_krw": 1_234_567.0,
        "positions": {
            "005930": {"qty": 10.0, "avg_cost": 70_000.0, "market": "KR"},
            "AAPL": {"qty": 5.0, "avg_cost": 200.0, "market": "US"},
        },
    }
    eq = strategy_equity(book, {"005930": 71_000.0, "AAPL": 210.0}, 1480.0)
    assert eq["cash_krw"] + eq["stock_value_krw"] == pytest.approx(eq["equity_krw"])


# --- 표시된 텍스트만으로 검산 (2026-08-19, 코디네이터 확증 결함) -------------
#
# EC2 실측: orb_scan 상세 메시지가 "시작 10,000,000원 → 평가 9,937,933원 ·
# 실현손익 -402,453원 · 미실현손익 +365,556원"만 보여줬다. 장부 원본에는 수수료
# 25,169원이 있었는데 메시지에 그 줄이 없어서, 독자가 "시작+실현-수수료+미실현"을
# 계산하면 평가금액과 정확히 수수료만큼 어긋나 보였다(계산 자체는 항상 맞았다 —
# 표시 누락이었다). 내부 값(`equity_krw` 등)으로 재는 위쪽 identity 테스트들은
# 이 결함을 못 잡는다 — 여기서는 **메시지에 실제로 찍힌 숫자를 정규식으로 파싱**해
# 독자가 손으로 하는 것과 같은 검산을 재현한다.

def _num_after_label(label: str, text: str) -> float:
    m = re.search(rf"{re.escape(label)} ([+-]?[\d,]+)원", text)
    assert m, f"'{label}' 뒤의 금액을 메시지에서 찾을 수 없다:\n{text}"
    return float(m.group(1).replace(",", ""))


def test_detail_message_realized_fee_identity_from_displayed_numbers():
    """실제로 화면에 찍히는 숫자만으로 "시작 + 실현손익(수수료 전) - 수수료 합계
    + 미실현손익 = 평가"가 성립해야 한다. 수수료 줄이 메시지에서 빠지면 이
    파싱 자체가 실패해 테스트가 즉시 잡는다."""
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {
            "orb_scan": {
                "cash_krw": 5_572_378.0,
                "realized_pnl_krw": -402_453.0,  # 장부 스키마: 수수료 전(gross)
                "fees_krw": 25_169.0,
                "positions": {"005930": {"qty": 40.0, "avg_cost": 100_000.0, "market": "KR"}},
            },
        },
    }
    quotes = {"005930": 109_139.0}
    messages = strategy_books_messages(data, trips=[], quotes=quotes, usd_krw=1500.0)
    detail = messages[1]

    start = _num_after_label("시작", detail)
    equity = _num_after_label("평가", detail)
    gross = _num_after_label("실현손익(수수료 전)", detail)
    fees = _num_after_label("수수료 합계", detail)
    net = _num_after_label("실현손익(순, 수수료 차감)", detail)
    unrealized = _num_after_label("미실현손익", detail)

    assert gross == pytest.approx(-402_453.0)
    assert fees == pytest.approx(25_169.0)
    assert net == pytest.approx(gross - fees, abs=1)
    assert equity == pytest.approx(9_937_938.0, abs=1)
    assert equity == pytest.approx(start + gross - fees + unrealized, abs=2), (
        "메시지에 찍힌 숫자만으로 검산했을 때 항등식이 깨진다 — "
        "이번 결함이 정확히 이 틈으로 빠져나갔다"
    )


def test_summary_message_totals_are_labeled_gross_and_include_fees():
    """요약의 합계도 같은 이름 문제를 가진다 — "총 실현손익"이라고만 쓰면
    session-pnl의 순손익과 헷갈린다. "(수수료 전)" 표시와 총 수수료가 요약에도
    있어야 독자가 합계 수준에서도 검산할 수 있다."""
    data = {
        "exists": True, "error": None, "initial_krw": 10_000_000.0,
        "books": {
            "orb_scan": {
                "cash_krw": 5_572_378.0, "realized_pnl_krw": -402_453.0, "fees_krw": 25_169.0,
                "positions": {"005930": {"qty": 40.0, "avg_cost": 100_000.0, "market": "KR"}},
            },
            "donchian": {
                "cash_krw": 9_980_000.0, "realized_pnl_krw": -20_000.0, "fees_krw": 1_000.0,
                "positions": {},
            },
        },
    }
    messages = strategy_books_messages(data, trips=[], quotes={"005930": 109_139.0}, usd_krw=1500.0)
    summary = messages[0]

    assert "총 실현손익(수수료 전)" in summary
    total_realized = _num_after_label("총 실현손익(수수료 전)", summary)
    total_fees = _num_after_label("총 수수료", summary)
    assert total_realized == pytest.approx(-402_453.0 + -20_000.0)
    assert total_fees == pytest.approx(25_169.0 + 1_000.0)
