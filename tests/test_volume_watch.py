from datetime import date, timedelta
from pathlib import Path

from quant.core.report_clock import KST
from quant.collect.snapshot import save_snapshot
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.volume_watch import recurring_volume_symbols

_AT = __import__("datetime").datetime(2026, 8, 24, 8, 0, tzinfo=KST)
_TODAY = date(2026, 8, 25)


def _rankings_snapshot(market: str, session_date: date, board: list[dict]) -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=session_date,
        generated_at=_AT,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=True, data={"boards": {"거래대금": board}},
                error=None, url="https://x", fetched_at=_AT, latency_ms=1,
            ),
        },
    )


def _item(rank, symbol):
    return {"rank": rank, "symbol": symbol, "price": 100.0, "change_pct": 1.0,
            "trading_amount": 1000}


def test_recurring_volume_symbols_counts_repeat_appearances(tmp_path: Path):
    for back in range(1, 4):  # 3일 전부 005930 등장
        d = _TODAY - timedelta(days=back)
        save_snapshot(_rankings_snapshot("KR", d, [_item(1, "005930")]), tmp_path)
    result = recurring_volume_symbols(tmp_path, "KR", _TODAY, days=5, min_appearances=2)
    assert result == ["005930"]


def test_recurring_volume_symbols_excludes_single_appearance(tmp_path: Path):
    d = _TODAY - timedelta(days=1)
    save_snapshot(_rankings_snapshot("KR", d, [_item(1, "005930")]), tmp_path)
    assert recurring_volume_symbols(tmp_path, "KR", _TODAY, days=5, min_appearances=2) == []


def test_recurring_volume_symbols_skips_missing_files(tmp_path: Path):
    assert recurring_volume_symbols(tmp_path, "KR", _TODAY) == []


def test_recurring_volume_symbols_skips_failed_ranking_source(tmp_path: Path):
    d = _TODAY - timedelta(days=1)
    snap = Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=d, generated_at=_AT,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=False, data=None, error="403",
                url="https://x", fetched_at=_AT, latency_ms=1,
            ),
        },
    )
    save_snapshot(snap, tmp_path)
    assert recurring_volume_symbols(tmp_path, "KR", _TODAY) == []


def test_recurring_volume_symbols_respects_board_top(tmp_path: Path):
    # 11위 종목은 board_top=10 이면 잡히지 않는다.
    board = [_item(r, f"{100000+r:06d}") for r in range(1, 12)]
    for back in range(1, 3):
        save_snapshot(_rankings_snapshot("KR", _TODAY - timedelta(days=back), board), tmp_path)
    result = recurring_volume_symbols(tmp_path, "KR", _TODAY, days=5, min_appearances=2,
                                       board_top=10)
    assert "100011" not in result
    assert "100001" in result


def test_recurring_volume_symbols_excludes_non_kr_codes(tmp_path: Path):
    # US 심볼(문자)은 6자리 숫자가 아니므로 제외.
    for back in range(1, 3):
        save_snapshot(_rankings_snapshot("KR", _TODAY - timedelta(days=back), [_item(1, "AAPL")]),
                      tmp_path)
    assert recurring_volume_symbols(tmp_path, "KR", _TODAY, days=5, min_appearances=2) == []


def test_recurring_volume_symbols_orders_by_count_desc(tmp_path: Path):
    # 005930 은 4일, 000660 은 그중 최근 2일에만 같이 등장 — 파일은 하루 1개이므로
    # 겹치는 날은 두 심볼을 한 보드에 같이 담는다.
    for back in range(1, 5):
        board = [_item(1, "005930")]
        if back <= 2:
            board.append(_item(2, "000660"))
        save_snapshot(_rankings_snapshot("KR", _TODAY - timedelta(days=back), board), tmp_path)
    result = recurring_volume_symbols(tmp_path, "KR", _TODAY, days=5, min_appearances=2)
    assert result == ["005930", "000660"]


def test_candidates_line_appends_rank_tokens_without_duplicates():
    """감시 메모리 심볼은 RANK 로 덧붙되, 이미 토큰이 있는 심볼은 중복 금지."""
    from quant.analyze.render import candidates_line

    cont = {"005930": {"today_articles": 1, "streak_days": 0, "is_new": False}}
    line = candidates_line(cont, {}, volume_watch=["005930", "035720"])
    assert line.count("005930") == 1, "이미 후보인 종목에 RANK 토큰을 또 붙이면 안 된다"
    assert "035720:RANK" in line


def test_candidates_line_default_is_byte_identical():
    """volume_watch 미지정이면 기존 출력과 완전 동일 — 무동작 보장."""
    from quant.analyze.render import candidates_line

    cont = {"005930": {"today_articles": 1, "streak_days": 0, "is_new": False}}
    assert candidates_line(cont, {}) == candidates_line(cont, {}, volume_watch=None)
