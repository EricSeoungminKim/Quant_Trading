# Quant Trading

**An autonomous, self-auditing quantitative trading system for Korean (KRX) and US equities** — running live in paper mode on AWS EC2, 24/7, across both market sessions.

Built solo as a long-running engineering project: 11 strategies, a dual-market research-report pipeline, and an operations layer that detects its own defects, measures its own edge, and reports over Telegram — designed so the system keeps improving from its own trade ledger even when nobody is watching.

> ⚠️ **Disclaimer** — This is a personal research/portfolio project. It trades a paper account. Nothing here is investment advice, and past simulated performance implies nothing about future results.

## Why this project is different

Most hobby trading bots are a strategy script with an exchange API. This repository is organized around a harder question: **what does it take to run trading software you can trust while you sleep?**

The answers shaped the architecture:

| Problem | Answer in this repo |
|---|---|
| A scraping bug should never place an order | 4-plane architecture where the *import graph itself* is tested — news code physically cannot reach order code |
| Backtests lie | Costs modeled to the venue's actual fee schedule (KR transaction tax 20bp, SEC fee, FINRA TAF); walk-forward OOS only; sample sizes and multiple-testing counts reported with every number |
| "It works" isn't evidence | 3,600+ tests, plus a forensics layer that replays every closed trade against 1-minute bars to measure *why* it won or lost (MFE/MAE, exit efficiency, entry-position control groups) |
| Parameter changes go unevaluated | An experiments loop fingerprints strategy configs daily, waits for sample size, then judges each change with **difference-in-differences** against unchanged strategies as market controls — and messages the verdict |
| Silent failure is the default failure mode | Heartbeats on every job, a rules-based watchdog for staleness/drift, an LLM ops judge that cross-checks data sources for contradictions, and alert dedup so real alerts never drown |

## Architecture

Code is partitioned by **cost of failure**, not by feature. Boundaries are enforced by `tests/test_architecture.py`, which parses the import graph and fails CI on violations.

```
┌────────────────────── information layer (failures lose data, not money) ─────┐
│  quant/collect/   scrapers, feeds, Telegram channels, FRED, DART   (LLM ok)  │
│  quant/analyze/   scoring, views, prose  →  edits the *watchlist file only*  │
└──────────────────────────────────────────────┬───────────────────────────────┘
                                     watchlist.yaml (a file, not an import)
┌──────────────────────────────────────────────▼───────────────────────────────┐
│  quant/trade/     strategies · risk · regime · loop     DETERMINISTIC ONLY   │
│                   no network, no DB, no LLM — enforced by tests              │
└──────────────────────────────────────────────┬───────────────────────────────┘
                              Protocol ports (quant/core/ports.py)
┌──────────────────────────────────────────────▼───────────────────────────────┐
│  quant/adapters/  ALL I/O lives here: Kiwoom WS/REST · Toss REST · Telegram  │
│  quant/apps/      assembly & CLI — the only place every plane meets          │
│  quant/control/   ledgers · scoreboard · governor · forensics · experiments  │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **`quant/core/`** — pure domain (models, ports, portfolio math). Zero external dependencies; stdlib + pandas type hints only.
- **`quant/trade/`** — the money path. Strategies emit intent (`Signal`), a risk manager owns sizing and hard rails (position caps, daily-loss circuit breaker, consecutive-stop cooldown), per-strategy virtual books track each strategy's own ₩10M account.
- **`quant/adapters/`** — hexagonal adapters implementing the core Protocols. Market data routes by priority (Kiwoom realtime WS → Kiwoom US REST → Toss REST) with loud, logged fallback.
- **`quant/control/`** — the part that makes it autonomous: trade forensics, config-change experiments (DiD + permutation tests), a parameter governor with hard envelopes (selection thresholds may auto-adjust; **sizing may never**), backups with restore rehearsal.

## The autonomous loop

```
trade → append-only ledger (trades.jsonl)
      → weekly scoreboard        win rate · payoff · Wilson CI      (Telegram)
      → weekly forensics         MFE/MAE replay on 1-minute bars    (Telegram)
      → daily experiments        config-change verdicts via DiD,
                                 strategy death alerts (permutation p ≤ .05)
      → human decides            governor applies only within envelopes
```

The loop found real things. Example from the ledger (August 2026): strategies were entering fine — 71% of closed trades touched +50bp — but median exit efficiency was **−0.44**: profits were round-tripped into losses. Entry position in the day's range showed **ρ ≈ 0** against outcomes (winners bought at 0.86 of range, losers at 0.88), so the popular "we buy tops" hypothesis was measurably *not* the problem. Exit discipline was. Pre-registered exit-rule replay (4 rules, no tuning) moved the simulated per-trade mean from −77bp to −14bp; the change shipped behind config flags and is now being judged by the experiments loop with unchanged strategies as controls.

## Reports

A second pipeline (same repo, separate processes) publishes research reports before each session — Korean morning/close reports and a US pre-open report — combining Naver market data, 12 Telegram channels, FRED macro, DART filings, and an overnight **US→KR sector bridge** (S&P sector ETFs mapped to KRX industries via their shared GICS taxonomy). Deterministic scoring picks candidates; an LLM lane (Claude CLI with OpenRouter fallback) writes prose *about* already-computed numbers and is forbidden from creating facts. Reports feed the watchlist through a no-LLM confidence gate.

## Honesty rules (encoded, not aspirational)

- Every performance number ships with sample size; below threshold the system prints *"do not allocate on this"* — literally.
- Missing data is reported as missing, never silently defaulted (`None` means *unknown*, not *fine*).
- Search/tuning attempts are counted and disclosed (multiple-testing bias).
- Failed sends, parse failures, and skipped jobs must leave a trace — the costliest bugs found here were silent ones (a token cache that outlived its token, a truncation that ate report sections for five days).

## Stack

Python 3.12 · pandas · httpx · websockets · Jinja2 · DuckDB/Parquet · MySQL · Redis · systemd + cron on EC2 · pytest (3,600+ tests) · uv

## Running

```bash
uv sync
cp .env.example .env.local        # fill in your own keys — nothing runs without them
uv run pytest                     # full suite
uv run python -m quant.apps.cli backtest --strategy donchian --days 90
uv run python -m quant.apps.cli paper          # live paper loop (needs broker keys)
uv run python -m quant.apps.report_cli build --market KR
```

Key CLI surfaces: `paper` `backtest` `fitness` `scoreboard` `forensics` `experiments` `health` `ops-judge` `session-pnl`.

Secrets live only in `.env.local` (git-ignored; see `.env.example` for the required names). The deploy story, runbooks, and incident procedures are in [`server/`](server/) and [`docs/runbooks/`](docs/runbooks/).

## Documentation

Most design documentation is in Korean — it doubles as the operational log of a real running system.

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) + [`docs/adr/`](docs/adr/) — decisions and their reasons
- [`docs/vault/`](docs/vault/) — handwritten system notes: how it works, what changed, and *why*, including honest post-mortems
- [`CLAUDE.md`](CLAUDE.md) — the working agreement for AI-assisted development on this codebase (plane rules, verification gates, forbidden actions)

## License

[MIT](LICENSE)
