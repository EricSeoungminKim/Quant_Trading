"""Reports must not turn stale/provisional flow into current evidence."""
import json
from datetime import date
from types import SimpleNamespace

import pytest

from quant.report.collect.agent_interpret import _build_agent_foreign_flow
from quant.report.collect.close import _build_close_bet_view
from quant.report.collect.intraday import _build_intraday_view
from quant.report.collect.sector import _build_foreign_view


def _write(root, dates):
    path = root / "data/ledger/frgn_flow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({
        "date": day, "symbol": "005930", "foreign_net": 100, "inst_net": 10,
    }) + "\n" for day in dates))


@pytest.mark.parametrize("dates", [
    ["2026-09-08", "2026-09-09"],
    ["2026-10-23", "2026-10-26"],
])
def test_stale_or_unfinished_flow_is_absent_from_all_report_consumers(tmp_path, monkeypatch, dates):
    _write(tmp_path, dates)
    session = date(2026, 10, 23)
    snap = SimpleNamespace(market="KR", session_date=session, results={})
    assert _build_foreign_view(tmp_path, snap, {"005930": {}}, {}, {}) is None
    assert _build_agent_foreign_flow(tmp_path, {"005930"}, session)["005930"] == []
    captured = {}
    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", lambda values, top: captured.update(values) or [])
    _build_intraday_view(tmp_path, "KR", {
        "session_date": session.isoformat(), "auto_watch": "AUTO_WATCH: 005930:NEWS",
    }, {}, None, None, None, {})
    assert captured["005930"]["foreign_v2_score"] is None
    snap.results["toss_rankings"] = SimpleNamespace(ok=True, data={"boards": {
        "상승률": [{"symbol": "005930", "change_pct": 4.0, "rank": 1}],
    }})
    assert _build_close_bet_view(snap, tmp_path, {})[0]["score"] == 2


def test_report_replay_uses_previous_completed_session_and_ignores_later_rows(tmp_path):
    _write(tmp_path, ["2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26"])
    session = date(2026, 10, 23)
    snap = SimpleNamespace(market="KR", session_date=session, results={})
    view = _build_foreign_view(tmp_path, snap, {"005930": {}}, {}, {})
    assert [row["date"] for row in view["sectors"][0]["rows"][0]["series"]] == ["2026-10-21", "2026-10-22"]
    rows = _build_agent_foreign_flow(tmp_path, {"005930"}, session)["005930"]
    assert [row["date"] for row in rows] == ["2026-10-21", "2026-10-22"]
