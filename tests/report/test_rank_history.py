from datetime import date, datetime
from pathlib import Path

from quant.core.report_clock import KST
from quant.collect.snapshot import save_snapshot
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.rank_history import load_trading_amount_history

_AT = datetime(2026, 8, 12, 8, 0, tzinfo=KST)


def _rankings_snapshot(market: str, session_date: date, boards: dict) -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=session_date,
        generated_at=_AT,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=True, data={"boards": boards}, error=None,
                url="https://x", fetched_at=_AT, latency_ms=1,
            ),
        },
    )


def _item(rank, symbol, amount):
    return {"rank": rank, "symbol": symbol, "price": 100.0, "change_pct": 1.0,
            "trading_amount": amount}


def test_load_trading_amount_history_reads_past_snapshots(tmp_path: Path):
    for back, amount in [(1, 1000), (2, 2000), (3, 3000)]:
        d = date(2026, 8, 12) - __import__("datetime").timedelta(days=back)
        snap = _rankings_snapshot("KR", d, {"거래대금": [_item(1, "005930", amount)]})
        save_snapshot(snap, tmp_path)

    history = load_trading_amount_history("KR", "005930", date(2026, 8, 12), tmp_path)
    assert sorted(history) == [1000, 2000, 3000]


def test_load_trading_amount_history_skips_missing_files(tmp_path: Path):
    # 스냅샷 디렉터리가 통째로 비어 있어도 예외 없이 빈 리스트.
    assert load_trading_amount_history("KR", "005930", date(2026, 8, 12), tmp_path) == []


def test_load_trading_amount_history_skips_failed_ranking_source(tmp_path: Path):
    from datetime import timedelta

    d = date(2026, 8, 12) - timedelta(days=1)
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
    assert load_trading_amount_history("KR", "005930", date(2026, 8, 12), tmp_path) == []


def test_load_trading_amount_history_only_counts_target_symbol(tmp_path: Path):
    from datetime import timedelta

    d = date(2026, 8, 12) - timedelta(days=1)
    snap = _rankings_snapshot(
        "KR", d, {"거래대금": [_item(1, "005930", 1000), _item(2, "000660", 5000)]}
    )
    save_snapshot(snap, tmp_path)
    assert load_trading_amount_history("KR", "000660", date(2026, 8, 12), tmp_path) == [5000]


def test_load_trading_amount_history_respects_market(tmp_path: Path):
    from datetime import timedelta

    d = date(2026, 8, 12) - timedelta(days=1)
    save_snapshot(_rankings_snapshot("KR", d, {"거래대금": [_item(1, "AAPL", 999)]}), tmp_path)
    assert load_trading_amount_history("US", "AAPL", date(2026, 8, 12), tmp_path) == []
