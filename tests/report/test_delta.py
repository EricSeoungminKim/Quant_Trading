from datetime import date, datetime
from pathlib import Path

from quant.core.report_clock import KST
from quant.collect.snapshot import save_snapshot
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.delta import compare, previous_snapshot


def _snap(day: int, kospi: float = 6345.0, foreign: int = 535,
          flow_date: str | None = None) -> Snapshot:
    at = datetime(2026, 8, day, 8, 0, tzinfo=KST)
    return Snapshot(
        SCHEMA_VERSION, "KR", date(2026, 8, day), at,
        {
            "market": SourceResult("market", True, {
                "quotes": {"^KS11": {"label": "KOSPI", "close": kospi,
                                     "prev": kospi, "change_pct": 0.0}},
                "crosscheck": {"checked": [], "warnings": []}},
                None, "https://x.test", at, 10),
            "kospi_flow": SourceResult("kospi_flow", True, {
                "market": "KOSPI", "unit": "억원",
                "rows": [{"date": flow_date or f"2026-08-{day:02d}",
                          "개인": -100, "외국인": foreign, "기관계": 200}]},
                None, "https://y.test", at, 10),
        },
    )


def test_no_previous_yields_empty():
    assert compare(_snap(12), None) == {"quotes": {}, "flow": {}}


def test_quote_change_is_computed():
    out = compare(_snap(12, kospi=6600.0), _snap(11, kospi=6345.0))
    assert round(out["quotes"]["^KS11"]["pct"], 2) == 4.02
    assert out["quotes"]["^KS11"]["prev_close"] == 6345.0


def test_flow_reversal_detected():
    out = compare(_snap(12, foreign=-700), _snap(11, foreign=535))
    assert out["flow"]["외국인"]["flipped"] is True


def test_same_sign_is_not_a_reversal():
    out = compare(_snap(12, foreign=300), _snap(11, foreign=535))
    assert out["flow"]["외국인"]["flipped"] is False


def test_identical_flow_date_is_skipped():
    """네이버는 전 거래일까지만 확정치를 준다 — 같은 날짜면 비교할 게 없다."""
    out = compare(_snap(12, flow_date="2026-08-11"), _snap(11, flow_date="2026-08-11"))
    assert out["flow"] == {}


def test_failed_section_is_ignored():
    cur = _snap(12)
    broken = SourceResult("market", False, None, "boom", "https://x.test",
                          cur.generated_at, 5)
    cur = Snapshot(cur.schema_version, cur.market, cur.session_date,
                   cur.generated_at, {**cur.results, "market": broken})
    assert compare(cur, _snap(11))["quotes"] == {}


def test_previous_snapshot_skips_weekend_gap(tmp_path: Path):
    """금요일 스냅샷이 월요일에도 직전으로 잡혀야 한다 — 주말에 비교가 끊기면 안 된다."""
    save_snapshot(_snap(14), tmp_path)  # 2026-08-14 (금)
    found = previous_snapshot("KR", date(2026, 8, 17), tmp_path)  # 월요일
    assert found is not None and found.session_date == date(2026, 8, 14)


def test_previous_snapshot_returns_none_beyond_lookback(tmp_path: Path):
    save_snapshot(_snap(1), tmp_path)
    assert previous_snapshot("KR", date(2026, 8, 31), tmp_path) is None


def test_previous_snapshot_picks_nearest(tmp_path: Path):
    save_snapshot(_snap(10), tmp_path)
    save_snapshot(_snap(11), tmp_path)
    found = previous_snapshot("KR", date(2026, 8, 12), tmp_path)
    assert found.session_date == date(2026, 8, 11)
