# 원장 소급 판본 태깅 — 실측 (2026-09-07 17:5x, EC2 스냅샷 trades.jsonl 828행 + param_changes.jsonl 64행)

`docs/plans/evolvability-2026-09-07.md` 투자 #1(체결 행에 `params_fingerprint`)의 근거이자 소유자
결정 1("소급 태깅 vs 판정 불가 리셋")의 데이터. `param_changes.jsonl`은 전략별 파라미터 판본
(`fingerprint`, `recorded_at`, `params`)을 이미 기록하고 있어, **각 체결의 진입 시각 이전 마지막
판본**을 붙이는 소급 근사가 가능하다(fingerprint 자체는 미래 체결부터 정확).

| 전략 | 판본 수 | 현재 판본 트립 | 옛 판본 트립 | 현재 판본 승률 / 평균(세금 포함) |
|---|---:|---:|---:|---|
| scalp_1m | 6 | 2 | 167 | 0% / −87bp (n=2 — 판정 불가) |
| scalp_1m_cat | 2 | 6 | 19 | 50% / −16bp |
| pullback_impulse | 2 | 7 | 29 | 57% / +8bp |
| vol_breakout | 2 | 2 | 4 | 50% / +21bp |
| vol_breakout_cat | 1 | 4 | 9 | 100% / +51bp |
| llm_trader | 2 | 3 | 17 | 0% / −69bp |
| intraday_momentum | 4 | 2 | 7 | 0% / −18bp |
| gap_fade · news_momentum · news_scalp · frgn_accumulate 등 | 2~3 | 0 | 4~9 | — |

scalp_1m 판본별(오래된 → 최신, 순 bp 는 KR 세금 20bp + 수수료 반영):

| 판본 등록일 | fingerprint | n | 승률 | 평균 |
|---|---|---:|---:|---:|
| 08-23 | 0407e1d6… | 25 | 28% | −57bp |
| 08-26 | 455c2d0a… | 41 | 37% | −12bp |
| 08-28 | a898b4dc… | 46 | 39% | +6bp |
| 09-03 | e74cf711… | 9 | 22% | −62bp |
| 09-04 | e15ded71… | 8 | 25% | −11bp |
| 09-05 | 179b24d1… | 2 | 0% | −87bp |

읽는 법: `run scoreboard`가 하나의 트랙레코드로 보여주는 scalp_1m 169트립은 **여섯 개의 다른
규칙**에서 나왔고, 가장 오래 유지된 판본(08-28, 46트립)만이 유일하게 양수였다. 판본이 09-03 이후
사흘 간격으로 바뀌어 어느 판본도 판정 표본(30)에 못 미친다 — "데이터가 쌓일수록 무용지물"이
되는 정확한 메커니즘이다.

계산 스크립트: 이 세션의 scratchpad(재현: 같은 pairing 로직을 `quant/control/ledger.round_trips`
로 대체하고 `param_changes.jsonl`의 recorded_at 으로 join). 프로덕션 반영은 투자 #1(체결 행
스탬프) → #2(스코어보드 판본 필터) 순.
