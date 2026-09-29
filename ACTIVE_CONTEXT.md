# Active Project Context — 2026-09-30 KR briefing + ML observation

## Invariants
- Preserve four-plane boundaries; no model/network calls in trading hot path.
- Current deployment is paper. Do not change strategy, capital, orders or watchlist for this experiment.
- Preserve unrelated dirty research files and previous report-link fixes.

## KR briefing milestone (deployed d625f50)
- New report entry_environment: dated fixed heuristic, context evidence, keyword provenance, first-publication freeze.
- Score is unvalidated, not probability; Fed/news/sectors/BTC context never arbitrarily adds points.
- Independent review fixed temporal/core conflict/OHLC/corrupt row/partial result/frozen explanation issues.
- Exact-session Naver OHLC primary, Yahoo fallback. No retrospective forecasts; n=0 at deployment.
- Persistent feedback timer16:40/07:10KST installed; service success. Report07:30 starts,08:00 target.
- FRED bounds12994 rows backfilled with fetched_at. EnginePID624 unchanged.
- Public preview:/preview/entry-environment/2026/09/29/KR_report.html (historical, evaluation-excluded).
- Full suite7336pass/10skip/25deselect/1xfail; final44focusedpass; two smokes exit0; mobile390/desktop1280 verified.
- Heartbeat automation:KST08:15 daily; LA15:15/16:15 wakes with KST gate for DST; app availability required.
- First actual forecast9/30KR morning; evaluation9/30close; next trading-day report compares it.
- Next: confirm scheduled first publication/Telegram and forecast hash, then outcome population; do not invent past scores.
- Runbook:docs/runbooks/entry-environment.md. Preserve existing ML observation schedules below.

## Completed
- Portfolio current metrics fixed/deployed: engine cd3b01b, site28e29de.
- ML code77c4a79 pushed/deployed. Past-only evaluation, fixed settings, actual label availability gate.
- Tailscale authenticated snapshot:10,335 rows; KR33/US34 unique selection dates, including weekends.
- Same-input historical baseline partial restoration; no fabricated scores.
- Final run: local/ml/out/2026-09-29/report.md, summary.json, provenance.json, model manifests.
- Earlier evaluation before availability fix preserved in local/ml/out/2026-09-29-initial/.
- Full pytest7301 pass,10skip,25deselect,1xfail; focusedML53pass; both required smokes exit0.
- Known warnings: All-NaN, joblib/NumPy, existing Kiwoom WS reconnect after interpreter shutdown.

## Next session observation
- Host: tailscale ssh ubuntu@100.87.129.113; repo/home/ubuntu/quant_trading_kiwoom.
- Separate .venv-ml-shadow; no engine restart (PID624, started9/22).
- Frozen model dir:data/ml_models/2026-09-29; only d1_return_bps used.
- Joblib SHA prefixes: KR3c74941719c4; US99b8534574a0. Version hashes complete manifest too.
- One-time timers:quant-ml-shadow-kr-20260930 at9/30 08:24KST; us at22:05KST.
- Limits:CPU25%,Memory350M,nice10,timeout90; transient timers lost on server reboot.
- Output:data/ml_shadow/2026-09-30_{KR,US}.{json,md}, producer ml_shadow in judgments ledger.
- Codex heartbeat ml:9/30 08:40KST (LA9/29 16:40), one result report in this thread; no external messages.
- User asked to see change tomorrow. Application preference unanswered; recommended observation scope used.

## Material limitations / next actions
- Current models are data-quality diagnostic observations, not validated strategy improvements.
- US labels contain >1000% D1 outliers; source corporate actions/price basis must be verified, never clip to improve score.
- Report selection dates, reference-close dates and label horizons can differ; do not call these clean trading sessions.
- Final KR matched-subset top5 raw forward mean -6.30bp vs baseline-31.95bp (19dates/204rows); not cost-adjusted performance.
- US AUC~0.501; no adoption evidence. No change to trading based on this run.
- First confirm next-day timer actually ran; report candidate coverage and same-universe top5 changes.
- To evaluate improvement, collect prospective outcomes with auditable price dates and corporate-action treatment.
