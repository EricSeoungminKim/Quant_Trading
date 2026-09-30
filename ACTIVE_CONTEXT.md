# Active Project Context — 2026-09-30 KR catalyst focus

## Invariants / authorization
- Four planes remain isolated; no LLM/network calls in trading hot path.
- Owner explicitly requested only KR vol_breakout_cat PAPER, no real funds; supersedes earlier no-settings-change ML observation restriction.
- Preserve all historical capital assignments, ledger and losses. No paper-epoch reset.
- Keep k0.5/min_stop_bp40/eod5/target_weight0.5 and FRGN gate; markets:[KR] excludes US without erasing historical US account.
- Preserve unrelated dirty history/resample/research files and earlier mixed vault edits.

## Completed focus transition
- Backend0e901ed deployed EC2; portfolio2ef843f+6a343a2 published https://quant-portfolio-eta.vercel.app.
- Host: tailscale ssh ubuntu@100.87.129.113, /home/ubuntu/quant_trading_kiwoom.
- Old PID624 PAPER halted; normal flatten closed all13 old-strategy symbols at09:00. Verified every lot0 and pending_flatten null.
- New PID834717 MODE=paper restarted; assembled only KRcat, active_markets KR,1 eligible candidate. Resumed09:01; heartbeat market_open=true/halted=false.
- All historical strategy_books identical before/after restart; account/ledger not reset. No existing cat holding during transition.
- Input capture timer installed08:28/16:45KST; service success/exit0. Actual first capture09:01:37, not backdated to pre-open.
- Capture: raw base settings/watchlist, hashes/code4hashes, tick path/size. Does not prove engine cache or historical tag knowledge; EC2 auto_params absent.
- Review: whole-ledger pairing then entry cutoff, source hash checked around read, generated_at; concentration/slippage, no NAV/OOS/live claim.
- Artifacts:data/research/vol_breakout_cat/{inputs/YYYY-MM-DD/,review-history.json,review-focus.json}. History36trades; focus0 initially.
- KR ticks continued2128→4096bytes. Parameter search not executed; first collect valid prospective coverage.
- Portfolio mainKRcat curve + decision story + deferral evidence; earlier aggregate losses archived visibly, KO/EN current roster aligned.
- Public09:02:06JSON enabled_count1,1075closed trips; validate-performance and performance-xcheck pass with date-gap warnings only.
- KRcat36trades/+9.3594%; historical UScat57trades/-1.0742% retained. Main curve includes preselection, not focus-period result.
- Full7357pass/10skip/25deselect/1xfail; final29focused; required2smokes pass. Website16tests/type/lint/build pass;390/1280 no overflow.
- Runbooks:docs/runbooks/strategy-focus.md, docs/research/vol-breakout-focus-2026-09-30.md.
- Automation automation-2 ACTIVE: KST17:00 review, LA00/01 gate; notify only meaningful changes/errors. Read-only, no Telegram duplicate or auto-tuning.

## Next observations
- Check16:45 capture/reviews and17:00 heartbeat, fresh ticks/known-time candidates and first focus trades. Zero trips means insufficient evidence.
- Compare new samples to frozen baseline; require data coverage and cost/concentration tests before the preregistered7candidate temporal study.
- Website currently tracks original KR account; separate focus-period entry-cutoff statistics are research files, not a published new baseline curve.
- Preserve existing macro/ML read-only observations below; no other trading strategy reactivation without owner instruction.

## Existing report / ML observations (keep)
- Backend1861719 includes d625f50 entry_environment; first9/30 morning published, frozen49.7 neutral coverage100, first prospective outcome pending.
- Report07:30 build,08:00 target; environment feedback16:40/07:10; automation briefing08:15KST.
- ML77c4a79 is observation only, not strategy improvement evidence. .venv-ml-shadow separate; models data/ml_models/2026-09-29.
- One-time ML timers9/30 KR08:24 andUS22:05KST; transient/reboot-sensitive. automation ml08:40 result report.
- ML limitations: US extreme forward labels/corporate actions unverified, retrospective selection dates differ from price dates; never clip to improve results.
- Historical KRcat36trades/15exitdays: +935935KRW, PF1.6309, win30.56%; largest trade excluded -76224KRW. Not OOS/live validation.
- Fresh post-selection samples and historical catalyst availability evidence required before limited preregistered parameter comparison; no parameter sweep executed.
