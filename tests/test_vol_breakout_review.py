import json
from datetime import datetime

import pytest

from quant.research.vol_breakout_review import build_review, main


def fills(symbol, day, gross, *, strategy="vol_breakout_cat", market="KR"):
    base = dict(symbol=symbol, strategy_id=strategy, market=market, qty=1, fee=1)
    return [dict(base, side="BUY", price=1000, ts=f"{day}T00:10:00+00:00"),
            dict(base, side="SELL", price=1000 + (gross or 0), realized_pnl=gross,
                 ts=f"{day}T06:00:00+00:00")]


def test_net_pf_concentration_and_extra_costs_use_closed_filtered_trips():
    rows = (fills("000001", "2026-09-07", 102) + fills("000002", "2026-09-07", -38)
            + fills("000001", "2026-09-08", -18)
            + fills("000001", "2026-09-06", 900)
            + fills("000001", "2026-09-09", 900, strategy="other")
            + fills("AAPL", "2026-09-09", 900, market="US")
            + fills("000003", "2026-09-09", None))
    result = build_review(rows, since=datetime.fromisoformat("2026-09-07T00:00:00+09:00"))
    assert result["overall"]["trips"] == 3
    assert result["overall"]["net_krw"] == 40
    assert result["overall"]["profit_factor"] == pytest.approx(100 / 60)
    assert result["overall"]["win_rate"] == pytest.approx(1 / 3)
    assert result["overall"]["mean_bp"] == pytest.approx(400 / 3)
    assert result["excluded_unknown_pnl_trips"] == 1
    assert result["without_best_trade_net_krw"] == -60
    assert result["without_best_day_net_krw"] == -20
    assert result["by_symbol"]["000001"]["net_krw"] == 80
    assert result["extra_one_way_slippage"]["5"]["net_krw"] == 37
    assert result["extra_one_way_slippage"]["10"]["net_krw"] == 34


def test_missing_sample_is_not_zero_risk_or_infinite_pf():
    result = build_review([], since=datetime.fromisoformat("2026-09-07T00:00:00+09:00"))
    assert result["overall"]["profit_factor"] is None
    assert result["overall"]["win_rate"] is None
    assert result["without_best_trade_net_krw"] is None
    assert result["eligible_for_live"] is False


def test_cutoff_applies_to_entry_after_pairing_and_days_use_kst():
    cross_cutoff = fills("000001", "2026-09-06", 102)
    cross_cutoff[1]["ts"] = "2026-09-07T01:00:00+00:00"
    current = fills("000002", "2026-09-07", 12)
    current[0]["ts"] = "2026-09-06T23:59:00+00:00"  # Sep 7 in KST
    current[1]["ts"] = "2026-09-07T06:00:00+00:00"
    result = build_review(cross_cutoff + current,
                          since=datetime.fromisoformat("2026-09-07T00:00:00+09:00"))
    assert result["overall"]["trips"] == 1
    assert result["overall"]["net_krw"] == 10
    assert list(result["by_day"]) == ["2026-09-07"]


def test_cli_writes_valid_json_without_changing_source(tmp_path):
    source = tmp_path / "trades.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in fills("000001", "2026-09-07", 12)))
    original = source.read_bytes()
    output = tmp_path / "review.json"
    main(["--ledger", str(source), "--since", "2026-09-07", "--out", str(output)])
    result = json.loads(output.read_text())
    assert result["overall"]["net_krw"] == 10
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert source.read_bytes() == original
    with pytest.raises(ValueError, match="source"):
        main(["--ledger", str(source), "--since", "2026-09-07", "--out", str(source)])
    assert source.read_bytes() == original


def test_cli_aborts_if_source_changes_while_loading(tmp_path, monkeypatch):
    import quant.research.vol_breakout_review as review

    source = tmp_path / "trades.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in fills("000001", "2026-09-07", 12)))
    output = tmp_path / "review.json"
    output.write_text("previous report")
    load = review.load_trades

    def append_during_load(path):
        rows = load(path)
        with path.open("a") as stream:
            stream.write("\n" + json.dumps(fills("000002", "2026-09-08", 20)[0]))
        return rows

    monkeypatch.setattr(review, "load_trades", append_during_load)
    with pytest.raises(RuntimeError, match="changed"):
        review.main(["--ledger", str(source), "--since", "2026-09-07", "--out", str(output)])
    assert output.read_text() == "previous report"
