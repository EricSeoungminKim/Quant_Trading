# gs-quant Evaluation (2026-09-07)

Evaluator role only — no production code changed. Scope was revised twice
mid-task by the coordinator: (1) Marquee is institutional-only, so every
Marquee-gated feature is treated as unusable and capped at ~40% of effort;
(2) the real ask became "study gs-quant's repo as a quant-firm architecture
reference and derive a refactor/workflow-gap backlog for our system,"
including actually running what runs offline against our real data.

## Recommendation (top line)

**Do not adopt gs-quant as a dependency (A). Do not use it as a third
backtest engine (C) — proven impossible below, not just undesirable. Borrow
exactly one pattern (a scoped version of B): a typed Symbol/market value
object**, replacing a heuristic duplicated in **19 call sites** across the
repo. Everything else gs-quant offers either (a) needs a Marquee session we
will never have, (b) is pure-pandas math that already matches our own
conventions byte-for-byte once units are aligned (verified below, not
assumed), or (c) solves a portfolio shape (shared rebalanced capital) that
CLAUDE.md's `capital_policy: fixed_dual` deliberately does not have.

Ranking: **B (scoped) > no-op > C (impossible) > A**.

---

## Part 0 — Facts

- PyPI `gs-quant`: version **2.1.12**, released 2026-09-04, `requires_python
  >=3.10`, license Apache-2.0, 56 `requires_dist` entries (23 hard deps + 33
  extras across notebook/test/develop/mcp).
  (`curl -s https://pypi.org/pypi/gs-quant/json`)
- GitHub `goldmansachs/gs-quant`: pushed 2026-09-04T18:20:31Z, 73 open
  issues, 12,894 stars, not archived, Apache-2.0.
  (`gh api repos/goldmansachs/gs-quant`)
- Installs and imports cleanly on **both Python 3.12 and 3.14** (our repo
  pins `requires-python = ">=3.12"`; local interpreter is 3.14.6) — 46
  packages resolved either way, `uv pip install gs-quant` in a scratch venv,
  `python -c "import gs_quant"` succeeded on both.
  Notable resolved versions: `pandas==3.0.5`, `numpy==2.3.5`,
  `scipy==1.18.1` — our `pyproject.toml` pins `pandas>=2.2`; no conflict
  observed but not exercised together in one venv.
- Context7 docs + local introspection: `gs_quant.backtests` has three
  engines (`GenericEngine`, `PredefinedAssetEngine`, `EquityVolEngine`), all
  built on `Strategy(initial_portfolio, triggers)` where triggers carry
  actions (`AddTradeAction`, `SubmitOrderAction`, `HedgeAction`, ...).
  `gs_quant.timeseries` has ~647 public names across
  `econometrics`/`technicals`/`statistics`/`backtesting`/etc.

## Part 1 — Executed cross-checks (real data, real commands)

All scripts run from `/private/tmp/.../scratchpad/gsq/.venv` (Python 3.12,
`uv pip install gs-quant pyarrow`). Copies live in
`/Users/eric/Documents/GitHub/quant-backtest/qb/xcheck_gsquant/` (research
repo only, not a dependency, see its README).

### 1a. `gs_quant.timeseries` vs our own math (2025 real bars, TQQQ/SQQQ/SOXL/SOXS/QQQ/SOXX)

`python qb/xcheck_gsquant/compare_timeseries.py` — loads 2025 1-minute bars
from `data/lake/`, resamples to daily and 15m closes (250 daily bars, 6,500
15m bars per symbol), computes each function two ways and diffs the tails:

| Function | gs-quant call | Our convention | Max abs diff | Note |
|---|---|---|---|---|
| returns | `returns(close,1)` | `close.pct_change()` | **0.0** | identical |
| volatility (20d, annualized) | `volatility(close,Window(20,0),annualization_factor=252)` | `rets.rolling(20).std(ddof=1)*sqrt(252)*100` | **0.0** | identical once `annualization_factor` and ddof are made explicit |
| beta (TQQQ vs QQQ, 60d) | `beta(x,b,Window(60,0),prices=True)` | `rx.rolling(60).cov(rb)/rb.rolling(60).var()` | **0.0** | both ≈3.00 (correct: TQQQ is 3x QQQ) |
| correlation (SOXL vs SOXX, 60d) | `correlation(x,y,Window(60,0))` | `.rolling(60).corr()` | **0.0** | identical |
| max_drawdown (whole series) | `max_drawdown(close,Window(None,0))` | `((close-cummax)/cummax).min()` | matches **only after ×100** | gs-quant returns a raw fraction (-0.568), not percent — inconsistent with `volatility()`'s own percent convention in the same module |
| SMA (20d) | `moving_average` | `sma()` (`quant/trade/indicators/__init__.py`) | **0.0** | identical, trivial |
| EMA (20d, and 8/21 on 15m bars) | `exponential_moving_average(x,beta=1-alpha)` | `ewm(span=n,adjust=False)` (`ema()`) | **0.0** | identical once you convert `beta=1-2/(n+1)` — gs-quant parameterizes by retention weight, not span; a real "gotcha," not a bug |
| RSI (Wilder, 14d) | `relative_strength_index` (uses `smoothed_moving_average`, confirmed Wilder/SMMA recursion) | `rsi()` (Wilder, `quant/trade/indicators/__init__.py`) | **0.343** (out of 100, last 200 obs) | same algorithm family; residual traced to differing warm-up indexing (`diff(x,1)[1:]` vs `iloc[1:period+1]`), not a convention difference |
| Bollinger (20d, k=2) | `bollinger_bands` (ddof=1, confirmed by matching `mid±2*std(ddof=1)` exactly) | `bollinger()` (`quant/trade/indicators/__init__.py`, **ddof=0 by deliberate choice**, per its own docstring) | not comparable directly | genuine, already-documented convention difference on our side, not a defect |
| z-scores (20d) | `zscores` | `(x-rolling_mean)/rolling_std(ddof=1)` | 2e-13 | identical (float noise) |

**`gs_quant.timeseries.technicals` has no ATR function and no VWAP
function** (confirmed via `dir()`) — the two indicators that actually matter
for `letf_pair.py`'s stop sizing and session anchor are simply absent from
gs-quant's public API. There is nothing to adopt for our highest-value use
case.

**Verdict**: every function that exists in both places agrees to float
precision once conventions (annualization factor, ddof, EMA parameterization)
are made explicit. This is a clean bill of health for our own
`quant/trade/indicators/__init__.py` and `quant/backtest/analytics.py`
conventions — zero bugs found, zero unique capability gained. One
internal gs-quant inconsistency was found (volatility in percent, max_drawdown
in fraction, same module) worth knowing if anyone reads their docs literally.

### 1b. Backtest engines — proven, not assumed, to require Marquee

`GenericEngine`, minimal single-option strategy, no session:
```
FAILED: MqUninitialisedError : GsSession is not initialised
```
`PredefinedAssetEngine` — this is GS's own "bring your own price series"
engine (`DataManager.add_data_source(series, ...)` takes a plain
`pd.Series`, no Marquee data needed for the *price*). Its own official
example notebook
(`gs_quant/documentation/04_backtesting/examples/01_PredefinedAssetEngine/040100_simple_example.ipynb`)
— captioned "the asset defined here could be anything as we are going to
provide the valuation of that asset" — still opens with:
```python
GsSession.use(client_id=None, client_secret=None, scopes=('run_analytics',))
```
i.e. **even GS's own "no Marquee pricing needed" happy path requires an
authenticated session to run at all.** This settles the original ask's step
3 without needing to hand-implement the LETF F1 rule in gs-quant's DSL: the
framework cannot run — full stop, for us — regardless of whether the
strategy needs intrabar stops, session VWAP, or EoD flatten. Attempting the
full F1 rule would only reproduce the same `MqUninitialisedError` later.
Option C (independent backtest cross-check) is not merely undesirable, it is
**not executable** without institutional credentials.

### 1c. Calendar utilities

`gs_quant.datetime.date.is_business_day`:
- No named calendar (`calendars=()`): **works offline**, `False`/`True` for
  a plain Sat/Mon check — but this is just `numpy.is_busday` under the hood;
  zero incremental value over calling numpy ourselves.
- Named calendar (`calendars=('NYSE',)` or `('KRX',)`) — the only part that
  would actually help (real exchange-holiday awareness for our KR/US session
  calendars): **`MqUninitialisedError: GsSession is not initialised`**. The
  one useful capability is exactly the one gated by Marquee.

### 1d. `gs_quant.timeseries.backtesting.backtest_basket` on our own paper ledger

Pure-pandas, no session (confirmed by reading its body — no `GsSession`
import used). Ran it (`qb/xcheck_gsquant/compare_basket.py`) on real
per-strategy NAV curves built from
`Quant_Trading/data/state/trades.jsonl` (local paper ledger, 461 rows;
`scalp_1m` n=107, `intraday_scan` n=42, `confluence` n=35 closed round
trips). Naive NAV construction (`100 + cumsum(realized_pnl)`, mixed
KRW/USD, no capital normalization) went **non-positive for the KRW
strategies**, and `backtest_basket`'s per-step `units = capital*weight/price`
division blew the equal-weight basket from 100 to 6,330 in 8 days — a real,
reproducible trap: the function silently assumes each input series behaves
like a positive price/NAV index, and our raw-PnL curves don't satisfy that
without first normalizing to each strategy's actual allocated capital
(10,000,000 KRW / $10,000 per `capital_policy: fixed_dual`). Even fixed,
this tool solves "rebalance N strategies inside one shared, drifting
capital pool" — the opposite of our deliberate design (`quant.apps.cli
paper-epoch` gives each strategy its own **fixed, segregated** book
precisely so one strategy's drawdown can't eat another's capital). No
adoption value; the mismatch is architectural, not a missing feature.

---

## Part 2 — gs-quant repo as an architecture reference

Shallow-cloned to `/private/tmp/.../scratchpad/gsq-repo` (`git clone
--depth 1`). Package layout under `gs_quant/`: `api/ analytics/ backtests/
common.py base.py context_base.py datetime/ documentation/ entities/
errors.py instrument/ interfaces/ json*.py markets/ mcp/ models/
priceable.py risk/ session.py target/ test/ timeseries/ tracing/ workflow/`.

| Pattern | Where in gs-quant | What we have | Gap real? |
|---|---|---|---|
| **Typed instrument/symbol model** | `base.py` (`dataclass_json`-based `Priceable`/instrument classes with `camelize`/`decode_instrument`), `markets/securities.py` (`Asset`, `AssetClass`, `AssetIdentifier`) | `quant/core/models.py:market_of_symbol` (single canonical def) — but the exact heuristic `symbol.isdigit() and len(symbol)==6` is **independently re-implemented in 19 other files** (`quant/collect/quotes/yf_source.py`, `quant/trade/loop.py`, `quant/trade/strategy/{orb_scan,confluence,llm_trader,intraday_scan,close_bet}.py`×2, `quant/adapters/brokers/kiwoom/datafeed.py`, `quant/analyze/watch_scorer.py`, `quant/report/collect/uswrap.py`, `quant/apps/{assembly,cli}.py`×2, `quant/control/{tca,warehouse,ledger,daily_wrap}.py`) — confirmed live via `grep -rn` today, not from memory. `quant/backtest/engine.py:758` even has an inline copy inside `_reconcile()`'s fallback. The 2026-08-10 changelog entry already flagged this exact duplication ("저장소 4곳" — it has since grown to 19). | **Yes — highest-confidence gap found in this whole evaluation.** |
| PricingContext / HistoricalPricingContext batching | `markets/core.py:PricingContext`, `markets/historical.py:HistoricalPricingContext` — context-manager batches N pricing requests into 1 round trip | No batching layer; `quant/core/ports.py:ColdFetchBudgetExceeded` exists specifically because cold cache misses are throttled **per-symbol, per-cycle**, not batched into one multi-symbol call | Plausible, unverified — worth a dedicated look, not designed here (adapter-level, higher risk) |
| `RiskMeasure` as first-class object | `risk/measures.py` (`PnlExplain`, `PnlExplainClose`, ... — many measures against one priceable) | `quant/core/ports.py:RiskManager.approve()` — one boolean gate per signal | **No gap** — this pattern only pays off with N measures per instrument (derivatives greeks); we have one approve/reject decision. Reject. |
| Strategy = triggers + actions, engine separate | `backtests/strategy.py:Strategy(initial_portfolio, triggers)`, 3 interchangeable engines | `Strategy.on_cycle()` fuses "when" and "what" per file (`quant/trade/strategy/*.py`), one path (live=backtest, ADR-4) | Legit architecture difference, not a defect — CLAUDE.md's "1 strategy = 1 file" (ADR-0007) plus determinism already gives auditability; splitting adds 2 layers of indirection for 17 strategies with no observed bug it would have caught. **Reject rewrite**, per the original brief's own instruction. |
| Typed backtest result object | `backtests/backtest_objects.py:BackTest`/`PredefinedAssetBacktest` (`.performance`, `.trade_ledger()`) | `quant/backtest/engine.py:BacktestResult` — **already a `@dataclass`** with named fields (`equity_curve`, `trades`, `metrics`, `reconciliation`, `fill_model`, ...) | **No gap** — we already do this; note it so it isn't miscounted as missing. |
| Errors hierarchy | `errors.py`: `MqError`→`{MqValueError, MqTypeError, MqRequestError→{Auth,RateLimited,Timeout,InternalServerError}}` + `error_builder(status)` mapping HTTP codes uniformly | Scattered domain exceptions (`ReconciliationError` in `engine.py`, `DataSourceError`/`ColdFetchBudgetExceeded` in `core/ports.py`), no common base, no shared HTTP-status→exception builder — each adapter (Toss/Kiwoom) hand-rolls its own status-code handling | Real but low-severity gap — convention-enforced today ("어댑터의 네트워크 예외는 어댑터 내부에서 처리"), works, but a shared base + builder would remove duplicated branching per adapter |
| Business-day/holiday calendar object | `datetime/gscalendar.py:GsCalendar` | Session boundary = "does a bar exist for this timestamp" (`quant/core/session.py:BarSessionCalendar`) — deliberate, per `engine.py`'s own docstring, and already handles early-close days correctly | **No gap, and nothing to adopt** — the one thing gs-quant's calendar does that ours doesn't (named exchange holiday tables) needs Marquee (proven in Part 1c). Our simpler approach is the only one of the two that actually runs. |
| Retry/backoff/tracing layer | `session.py` (httpx + `backoff` decorators), `tracing/` (OpenTelemetry) | `quant/collect/`/`quant/analyze/` (where retries are explicitly allowed per the 4-plane table) — checked: only 2 files reference retry/backoff, 1 hand-rolled retry loop | Not enough duplication to justify a shared helper today (would be premature abstraction for 1 call site) |
| Test fixtures (mocked session) | `gs_quant/test/` shared session-mock fixtures | Protocol-boundary fakes (`quant/core/ports.py` Clock/DataFeed/Broker fakes used across 1,500+ tests) | **No gap** — same idea, different name (fake the Protocol, not the HTTP client) |
| CI/versioning/deprecation (`versioneer.py`, `deprecation` pkg) | external semver + deprecation warnings for library consumers | N/A — single-deployment personal system, no external API consumers | **N/A, reject** |

---

## Part 3 — Prioritized backlog (ranked, top choice implemented in detail)

**#1 — Typed `Symbol`/market value object** (top choice; borrows gs-quant's
typed-instrument pattern without the dependency)
- **Change**: keep `quant/core/models.py:market_of_symbol()` as the single
  source of truth (already correct); replace the 19 duplicated inline
  heuristics with calls to it. Where a call site currently branches on the
  string shape *and* needs the market for something else (e.g.
  `close_bet.py`, `apps/assembly.py`), import and call the function instead
  of re-deriving it.
- **Guardrail**: extend `tests/test_architecture.py` with a regex grep test
  that fails if `r"isdigit\(\)\s+and\s+len\("` appears anywhere under
  `quant/` outside `quant/core/models.py` — the same "shrinks only"
  KNOWN_DEBT discipline the repo already uses for import-graph violations.
- **Files touched**: the 19 files listed in Part 2's table.
- **Tests**: the new architecture guard, plus re-run existing suites
  (`uv run pytest`) to confirm behavior-preserving (all replacements are
  identical boolean logic, not new logic).
- **Effort**: 4–6 hours (mechanical replacement + one new test + full
  suite run).
- **Risk to trade plane**: low — pure refactor of a pure function call,
  no new dependency, no behavior change, deterministic.
- **Timing**: before real-money (P1) — this exact class of bug already cost
  real money once (058610 traded as USD at 1/1500th scale, per
  `docs/vault/변경기록.md`); 19 independent copies of the rule is 19 places
  a future edit can silently diverge from the canonical one.

**#2 — Small domain error hierarchy + adapter error builder**
Borrow: `errors.py`'s `MqError` tree + `error_builder(status)`. Add
`quant/core/errors.py` with `QuantError` as a common base, reparent
`DataSourceError`/`ColdFetchBudgetExceeded` under it, and add one
`http_error_builder(status) -> QuantError` helper for Toss/Kiwoom adapters
to stop each one hand-rolling status-code branches. Files: `quant/core/errors.py`
(new), `quant/adapters/brokers/{toss,kiwoom}/*.py`. Tests: unit tests for
the builder's status-code mapping. Effort: 6–10h. Risk: low, additive.
Timing: P2 (after live-readiness P0/P1 items).

**#3 — Reject: Strategy = triggers+actions rewrite.** No defect found that
it would fix (see Part 2); adds indirection for 17 files. Do not do this to
the trade plane, per the original brief's own constraint.

**#4 — Reject: adopt gs-quant as an analyze/backtest dependency (Option A).**
Zero unique capability for our actual indicator set (no ATR, no VWAP);
100% overlap elsewhere already matches our own math; backtest engines
unusable without Marquee; `pandas 3.0.5` resolution is untested against our
pin and adds 46 packages for nothing used.

**#5 — Reject: gs-quant as a third independent backtest engine (Option C).**
Proven impossible in Part 1b (`MqUninitialisedError` even in GS's own
"bring your own data" example) — not a cost/benefit call, it cannot run.

**#6 — No action: calendar/holiday table.** Checked and rejected in Part
1c/2 — the one useful part (named exchange holidays) is Marquee-gated; our
existing bar-presence approach is strictly better for us because it runs.

**#7 — No action: typed backtest result object.** Already have it
(`BacktestResult` dataclass). Verified, not assumed.

**#8 — Watch, don't build: PricingContext-style batching for cold fetches.**
Plausible workflow gap (`ColdFetchBudgetExceeded` exists because misses are
per-symbol) but unverified without a dedicated adapter-level investigation;
scope was out of budget for this evaluation. Flagged for a follow-up
session, not designed here. Effort if pursued: 12–20h, medium risk
(must preserve per-symbol failure isolation that adapters currently rely on).

**#9 — No action: retry/backoff helper in collect/analyze.** Only 1
hand-rolled retry loop found — insufficient duplication to justify
abstraction today (would be speculative, against CLAUDE.md's "no
abstractions for single-use code").

**#10 — No action: RiskMeasure-as-object, test-fixture pattern, CI/versioning.**
All checked in Part 2 and found to be either already-equivalent or
not applicable to a single-deployment, non-derivatives, personal system.

---

## Verification commands (every number above)

```
curl -s https://pypi.org/pypi/gs-quant/json | python3 -c "..."
gh api repos/goldmansachs/gs-quant
uv venv --python 3.12 .venv && uv pip install gs-quant   # + repeated on --python 3.14
git clone --depth 1 https://github.com/goldmansachs/gs-quant.git
grep -rn "isdigit() and len(symbol) == 6" quant/            # 19 hits, Part 2
python qb/xcheck_gsquant/compare_timeseries.py               # Part 1a table
python -c "...GenericEngine().run_backtest(...)"              # Part 1b, MqUninitialisedError
python -c "...is_business_day(..., calendars=('NYSE',))"      # Part 1c, MqUninitialisedError
python qb/xcheck_gsquant/compare_basket.py                    # Part 1d
grep -rn "retry\|backoff" quant/collect/ quant/analyze/       # Part 2, retry row
sed -n '58p' tests/test_architecture.py                       # KNOWN_DEBT: set() — empty, confirmed
```
