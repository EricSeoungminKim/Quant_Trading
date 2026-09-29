# KR premarket briefing and prospective evaluation

Goal: Extend the existing KR morning report with an auditable entry-environment score, concise evidence, channel keyword aggregation, and next-session feedback.
Architecture: existing collect snapshots → pure analyze score → report DTO/HTML/Telegram. Adapter stores immutable pre-open forecasts and obtains exact-session benchmark OHLC. Pure control evaluation compares open-to-close direction; report displays previous evaluation. No trading imports or allocation mutations.
Tech stack: existing Python/Jinja2/JSON artifacts/systemd; existing daily report and Telegram lane.
Spec: User request 2026-09-29. Score is an uncalibrated environment index, never a probability. Exposure bands are observation scenarios, not broker allocation. Freeze inputs/version before KR open; late rebuilds cannot create retrospective forecasts. Missing/stale inputs reduce coverage and can abstain. Keyword repetition is attention, not corroboration. Feedback identifies evidence and hypotheses, never claims causal proof or tunes on one outcome.

- [x] Pure score and strict dated input/keyword tests (entry_environment.py).
- [x] Immutable forecast adapter, exact-session outcomes and pure evaluation tests.
- [x] Morning assembly, concise collapsible section, Telegram summary and mobile checks.
- [x] Persistent daily outcome schedule plus in-session daily briefing automation.
- [x] Independent numeric/code review, full tests and smoke commands.
- [x] Deploy only report components, verify public artifacts and schedules, record evidence.

Ownership: numeric agent entry_environment + its tests; UI agent rendering files after contract; parent persistence/control/assembly/deployment. Preserve unrelated working tree edits.
