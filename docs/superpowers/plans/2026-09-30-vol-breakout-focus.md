# KR catalyst breakout focus implementation plan

Goal: User-authorized sole KR vol_breakout_cat paper operation, auditable research and portfolio decision timeline.
Architecture: Preserve four planes. Optional strategy market filter is deterministic; research reads ledger and never promotes parameters. Existing performance and closed-account histories remain intact.
Decisions: Preserve k0.5/min_stop_bp40/eod5/target_weight0.5, fees/slippage and paper mode. Disable other strategies without deleting capital assignments or data. Historical allocation metadata must not erase past US losses. markets:[KR] limits future symbols. Focus date2026-09-30; historical return is not a prospective focus-period result.

- [x] Halt new entries, normal paper flatten of old positions, verify zero lots/orders. Existing KRcat holdings absent before flatten.
- [x] Test optional market filter, disable all strategies except KRcat, restart paper after flatten and verify/resume.
- [x] Reproducible concentration/cost diagnostic and preregistered parameter-validation protocol; never label historical winner selection as OOS.
- [x] Portfolio main-strategy curve plus dated research timeline, reasons for retiring other lanes, archived comparisons and honest uncertainty.
- [x] Independent review, full tests and required smoke commands; publish backend data with both existing gates, frontend build and deployed UI verification.
- [x] Record deployment facts in vault/context. No live funds, no destructive account resets, no profit guarantee.
