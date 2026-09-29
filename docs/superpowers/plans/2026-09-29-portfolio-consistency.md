# Portfolio consistency implementation plan

**Goal:** Publish a portfolio website whose current-period headline, curves, table and costs use the same paper-epoch population, with accurate methodological and operational descriptions.

**Approved scope:** The owner requested implementation and website improvement after reviewing the four defects in the preceding assessment. Preserve original ledgers and existing unrelated workspace changes.

**Design:** Add current-scope statistics and currency books to `paper_epoch`; keep historical fields for compatibility. Use only the epoch subtree for current presentation. Retain historical trade statistics in a clearly separate section. Label the existing Wilson test as a win-rate comparison with 50%, not profitability. Correct drawdown to include initial capital. Disclose realized-only P&L, idle allocated capital, paper costs and fixed FX.

- [x] Backend: add period, per-market books, strategy statistics, and costs; reproduce first-loss drawdown and scope regressions in `test_performance*.py`; strengthen public contract.
- [x] Frontend: use one current-period view in headline, charts and tables; remove lifetime curve fallback; expose separate historical stats and meaningful regression tests.
- [x] Content: correct overnight, external AI, observation lanes and paper execution descriptions in both languages; add submission-oriented engineering evidence and data provenance; document keyless demo.
- [x] Verification: focused tests, full repository pytest + required smoke commands, site data checks/typecheck/lint/build, independent numeric and code reviews, desktop/mobile UI checks.
- [ ] Deployment: commit only task changes, update EC2 reporting modules without restarting trading engine, regenerate from actual ledger and cross-check, publish current JSON + site and verify deployed text/data.
