"""Read-only KR catalyst breakout diagnostics; no tuning or live promotion.

Run with --ledger trades.jsonl --since 2026-09-07 --out /tmp/review.json.
Reuses the deployed ledger's closed round-trip accounting, including fees.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from quant.control.ledger import load_trades, round_trips
from quant.core.report_clock import KST


def _stats(trips: list[dict]) -> dict:
    pnls = [t["pnl"] for t in trips]
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    return {
        "trips": len(trips), "net_krw": sum(pnls),
        "winning_pnl_krw": wins, "losing_pnl_krw": losses,
        "profit_factor": wins / losses if losses else None,
        "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
        "mean_bp": sum(t["bps"] for t in trips) / len(trips) if trips else None,
        "fees_krw": sum(t["fees"] for t in trips),
        "entry_notional_krw": sum(t["notional"] for t in trips),
    }


def build_review(trades: list[dict], *, since: datetime,
                 strategy: str = "vol_breakout_cat") -> dict:
    """Filter by entry time only after pairing full history, never orphan exits."""
    if since.tzinfo is None:
        raise ValueError("since must have a timezone")
    selected = [t for t in round_trips(trades)
                if t["strategy"] == strategy and t["market"] == "KR"
                and datetime.fromisoformat(t["entry_ts"]) >= since]
    known = [t for t in selected if t["pnl_known"]]
    by_day, by_symbol = defaultdict(list), defaultdict(list)
    for trip in known:
        day = datetime.fromisoformat(trip["exit_ts"]).astimezone(KST).date().isoformat()
        by_day[day].append(trip)
        by_symbol[trip["symbol"]].append(trip)
    overall = _stats(known)
    daily = {day: _stats(rows) for day, rows in sorted(by_day.items())}
    symbols = {symbol: _stats(rows) for symbol, rows in sorted(by_symbol.items())}
    best_trip = max(known, key=lambda t: t["pnl"], default=None)
    best_day = max(daily, key=lambda day: daily[day]["net_krw"], default=None)
    best_symbol = max(symbols, key=lambda symbol: symbols[symbol]["net_krw"], default=None)
    net = overall["net_krw"]
    return {
        "schema": 1, "strategy": strategy, "market": "KR", "currency": "KRW",
        "since_entry": since.isoformat(), "until_exit": max((t["exit_ts"] for t in known), default=None),
        "scope": "closed_round_trips_net_of_recorded_fees",
        "eligible_for_live": False,
        "limitations": [
            "Retrospective paper-ledger diagnosis, not unseen OOS or live execution evidence.",
            "Open positions and mark-to-market losses are excluded; this is not account NAV.",
            "PF is null without losses or trades, not evidence of infinite profitability.",
            "Extra slippage uses twice entry notional; not exact exit turnover or a fill/impact model.",
            "Many strategies were observed before selection; selection bias is not corrected.",
            "Missing parameter fingerprints cannot prove all trades used the current baseline.",
        ],
        "overall": overall, "closed_trade_days": len(daily),
        "excluded_unknown_pnl_trips": len(selected) - len(known),
        "parameter_fingerprints": dict(Counter(t.get("entry_params_fingerprint") or "unknown" for t in known)),
        "by_day": daily, "by_symbol": symbols,
        "best_trade": ({k: best_trip[k] for k in ("symbol", "entry_ts", "exit_ts", "pnl")} if best_trip else None),
        "without_best_trade_net_krw": net - best_trip["pnl"] if best_trip else None,
        "best_day": best_day,
        "without_best_day_net_krw": net - daily[best_day]["net_krw"] if best_day else None,
        "best_symbol": best_symbol,
        "without_best_symbol_net_krw": net - symbols[best_symbol]["net_krw"] if best_symbol else None,
        "extra_one_way_slippage": {
            str(bp): {"extra_cost_krw": 2 * overall["entry_notional_krw"] * bp / 10000,
                      "net_krw": net - 2 * overall["entry_notional_krw"] * bp / 10000}
            for bp in (0, 5, 10)
        },
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--strategy", default="vol_breakout_cat")
    parser.add_argument("--since", required=True, help="ISO date or aware datetime; date means KST midnight")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.ledger.resolve() == args.out.resolve():
        raise ValueError("output cannot overwrite source ledger")
    if not args.ledger.is_file():
        raise FileNotFoundError(args.ledger)
    since = datetime.fromisoformat(args.since)
    if len(args.since) == 10:
        since = since.replace(tzinfo=KST)
    source_hash = hashlib.sha256(args.ledger.read_bytes()).hexdigest()
    trades = load_trades(args.ledger)
    if hashlib.sha256(args.ledger.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("source ledger changed while loading; report not written")
    result = build_review(trades, since=since, strategy=args.strategy)
    result["generated_at"] = datetime.now(KST).isoformat()
    result["source"] = {"path": str(args.ledger.resolve()),
                        "sha256": source_hash}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(f"{args.out}: {result['overall']['trips']} closed trips; research only")


if __name__ == "__main__":
    main()
