# Gap analysis: this system vs. `goldmansachs/gs-quant`

2026-08-24. Owner asked: *compare against the toolkit GS actually uses for quant work, diagnose what we lack, and adopt what fits a Kiwoom/Toss retail autonomous system.* This document records the comparison honestly — including what we deliberately did **not** adopt.

gs-quant is an institutional toolkit centred on derivative structuring, cross-asset risk, and access to GS's Marquee data/pricing APIs. We are a two-broker cash-equity system. A line-by-line feature copy would be cosplay; the useful question is which of their **disciplines** we are missing.

## What we already had (no action)

| gs-quant module | Our counterpart |
|---|---|
| `backtesting` | `quant/backtest/engine.py` — same code path as live, venue-accurate costs (KR sell tax 20bp, SEC fee, FINRA TAF), `fitness` machine-readable metrics |
| `risk` (limits sense) | `quant/trade/risk/manager.py` — hard rails, circuit breakers, regime-scaled sizing; per-strategy books |
| `datetime`/calendars | `quant/core/clock.py` + Toss market-calendar adapter (KR/US sessions, DST) |
| `tca` (partially) | `forensics` measures realized round-trip costs from the ledger; see gaps below |
| `event_study` (partially) | catalyst study ledger + judgment/leaderboard forward-return scoring |

## Gap adopted now: equity-curve analytics

gs-quant's `timeseries.econometrics` (`returns`, `volatility`, `sharpe_ratio`, `max_drawdown`) operates on **curves**. All our performance numbers were **per-trade** (win rate, payoff, bps). Nobody could answer "what is the realized Sharpe / max drawdown of strategy X's ₩10M book since inception?" because the curve itself was never recorded.

Adopted (2026-08-24):

- `quant/core/timeseries.py` — pure-stdlib `simple_returns` / `annualized_volatility` (√252, sample stdev — matches gs-quant defaults) / `sharpe_ratio_rf0` / `max_drawdown` / `cagr`.
  - **Deliberate deviation**: gs-quant pulls currency risk-free curves from Marquee; we have no such source, so Sharpe assumes rf=0 and says so in its name. In a ~3% KRW rate environment this *overstates* Sharpe — fine for comparing strategies against each other, not for absolute claims.
- `cli equity-snapshot --market {KR,US}` — appends one point per session close (total + per-strategy book equity, KRW) to `data/ledger/equity_curve.jsonl`; quote failures degrade to cost basis and are **counted**, never invented.
- `cli performance` — curve metrics with a hard "insufficient sample" floor (<5 points), wired into the weekly Telegram scoreboard.

## Gaps noted, deliberately deferred

- **Slippage TCA** (arrival-price vs fill). We already persist order intents; joining them to fills is the right next step. Deferred: needs a careful timestamp audit first, and our paper fills are model-generated — the measurement becomes meaningful at live conversion.
- **Scenario/what-if risk** (gs-quant `risk`/`scenarios`): built for derivatives Greeks. Our cash-equity exposure is already capped structurally (position % rails, no leverage in cash accounts). A KOSPI −5% what-if on current books would be honest to add later; low urgency at paper stage.
- **Portfolio optimisation**: our per-strategy capital is intentionally *not* optimised — equal ₩10M books exist to measure strategies, and the governor is forbidden from touching sizing. Optimising allocation before edges are proven positive would optimise noise.
- **Measure registry / composable Window API**: elegant at GS scale; over-engineering at 11 strategies. Revisit if analytics call-sites multiply.

## Disciplines borrowed rather than code

1. **Curves are first-class data** — hence the new ledger; a metric you didn't record the inputs for is a metric you'll never have.
2. **Annualisation and estimator conventions must be stated** (√252, N−1, rf) — ours now are, in the module docstring and output footnotes.
3. **Name the assumption in the API** (`sharpe_ratio_rf0`) so a future reader can't mistake it for the textbook quantity.
