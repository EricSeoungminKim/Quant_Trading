---
type: assessment
tags: [vault/handwritten, evolvability, 2026-09-07]
---

# 진화 가능성 평가 (2026-09-07)

> 질문: "지금 프로그램이 계속 발전할 수 있는 구성인가? 계속 돌릴수록 데이터가
> 무용지물이 되는 게 아니라 의미가 있어야 하고, 그걸 토대로 발전해야 한다."
> 범위: 읽기 전용 분석. 코드 변경 없음(이 문서 + 아래 소규모 PoC 후보 제안만).
> HEAD: eff9b75. 근거: 저장소 코드, `docs/vault/변경기록.md`, EC2 원장 스냅샷
> (`ec2_snapshot/`), 리서치 저장소 `quant-backtest`.

---

## 한 줄 평결

**뼈대는 진화할 수 있게 짜여 있다(4평면 + A/B 레인 + 승격 게이트 + 결정론적
governor). 그러나 지금은 그 뼈대가 만든 데이터를 그 뼈대 스스로 배신하고 있다.**
전략 파라미터가 바뀔 때마다 원장 행에는 아무 표시도 남지 않는다 — 그 결과
`scalp_1m`은 09-05 이전 원장의 **99%**(316/320건)가 오늘과 다른 규칙에서 나온
체결인데도 `run scoreboard`는 그걸 하나의 연속된 트랙레코드로 취급한다.
"발전"의 인프라(governor, `_cat` A/B, 승격 게이트, `변경기록.md`)는 실재하고
꽤 정교하지만, **그 인프라가 먹는 원재료(원장)가 판본 표시 없이 풀링**되고
있어 숫자가 쌓일수록 오히려 더 오염된다. 지금 당장 필요한 건 새 전략이나 새
지표가 아니라 **원장 행 하나에 "이 체결이 어떤 파라미터 판본 아래서 났는가"를
적는 필드 하나**다 — 이게 없으면 다른 모든 되먹임 루프(governor 승격, A/B
판정, 비용모델 재추정)가 전부 섞인 표본으로 판단하게 된다.

---

## 스코어카드 (0-10, 근거 포함)

| 축 | 점수 | 근거 |
|---|---:|---|
| **데이터 의미 보존** | **2/10** | 원장 행(`trades.jsonl`)에 params_hash/schema_version 없음(`quant/control/ledger.py` 스키마: `ts, strategy_id, symbol, side, qty, price, fee, realized_pnl, market` 뿐). `scalp_1m` 320건 중 316건(99%), `gap_fade`·`pullback_impulse` 각 100%가 최신 파라미터 편집 이전 체결. `PAPER_EPOCH_MARKER`(ledger.py:150-184)는 **자본 리셋만** 표시하고 파라미터 판본은 표시 안 함. `report_claims.jsonl`의 `schema=1` 필드는 존재하지만 `score_direction_claims` 등 어디서도 읽지 않는 죽은 필드(report_accuracy.py). |
| **되먹임 루프 폐쇄도** | **6/10** | (b) 리포트 정확도→가중치: 실제로 한 번 닫혔다(2026-09-06 `symbol_score.py` IC 감사 → 가중치 수정, 사람 매개). (a) governor/promotion: 코드 경로는 끝까지 있지만(`governor.py` evaluate→record, `promotion.py` check_promotable→apply_promotion) 실사용 증거는 `param_proposals.jsonl` 단 4행 — 사실상 아직 안 돈다. (c) 리서치→설정: NO_GO 판정도 소유자가 뒤집어 재활성(letf_pair_qqq/sox, intraday_momentum) — 검증 결과가 설정을 강제하지 못함. (d)(e)는 설계상 open(사람이 마지막 단계). |
| **회귀 방지(테스트/게이트)** | **7/10** | pytest 6,954건 수집(0.95초), `test_architecture.py`가 4평면 임포트 규칙을 강제, `KNOWN_DEBT` 비어 있음. backtest gate(`quant/backtest/gate.py`)가 walk-forward+deflated Sharpe+비용 2배 스트레스로 실질적 holdout. 약점: 이 방대한 테스트가 "원장 데이터의 의미"는 전혀 지키지 않는다 — 아키텍처 회귀는 막아도 통계적 회귀(파라미터 바뀐 걸 모르고 풀링)는 못 막음. |
| **실험 위생** | **5/10** | A/B(`_cat`) 2/2, holdout(backtest gate) 2/2, 최소표본(`MIN_TRIPS_FOR_JUDGEMENT=30`) 2/2(단 `allocator.decide(min_samples=20)`과 불일치), 다중검정 보정 0/2(17개 전략을 개별 p<0.05/p<0.01로 판정, 상관 없음), 파라미터 판본화 1/2(`experiments.params_fingerprint()`는 있지만 원장 행에 안 걸림), 커밋→지표 연결 registry 0/2(`변경기록.md`는 사람이 쓰는 산문, 질의 불가), 사전등록 1/2(리서치 저장소엔 있음, 라이브 규칙 변경엔 없음), 결정 저널 2/2(`변경기록.md` 6,281줄, 매 턴 강제). |
| **구조적 확장 여력** | **5/10** | `quant/apps/cli.py` 7,439줄·61개 서브커맨드(1:1 `cmd_*`/`add_parser`) — 신규 기능마다 계속 여기 쌓임. `server/crontab.txt` 93개 활성 job, 자체 주석이 "2GB 박스에서 자원 경합 피하려고 2-6분 간격으로 수동 스태거링" 중이라고 인정 — 수동 스케줄링의 한계에 근접. `docs/vault/변경기록.md` 6,281줄, 무한 증가, 아카이브 메커니즘 없음. `params_fingerprint()`(experiments.py)가 runs 테이블의 씨앗이지만 단일 슬롯 상태 파일일 뿐 조회 가능한 테이블이 아님(`grep params_hash\|experiment_id\|run_id` 전체 0건 in trades.jsonl). |

**총점 25/50** — 뼈대(회귀 방지·실험 위생)는 준수한 수준이지만, 핵심 질문인
"데이터가 의미를 유지하는가"에서 2/10으로 바닥을 찍어 전체 평균을 끌어내린다.

---

## Top-8 투자, (가치÷비용) 순, 비용은 시간(hr)·건드리는 평면 표기

| # | 투자 | 가치/비용 | 시간 | 평면 | 왜 |
|---|---|---:|---:|---|---|
| 1 | **원장 행에 `params_fingerprint` 필드 추가** — `TradeLedgerSink.on_fill`(`ledger.py`)이 체결 시점 해당 전략의 `experiments.params_fingerprint()` 값을 그대로 찍는다. 기존 행은 소급 불가(정직하게 `null`로 남긴다) | ★★★★★ | 3 | trade(원장 스키마만, 결정 로직 불변) | 지금 유일하게 없는, 가장 싼, 가장 큰 레버. 이거 하나로 scoreboard/governor/A-B가 전부 "판본별로 잘라서" 볼 수 있게 됨. 코드가 아니라 필드 추가라 리스크 최소 |
| 2 | **`scoreboard`/`ab_compare`에 "params_fingerprint 변경 이후만" 필터 옵션** — #1이 있어야 의미 있음 | ★★★★★ | 4 | control | 지금 governor·A/B 판정이 섞인 표본으로 이뤄지는 걸 구조적으로 막음. `MIN_TRIPS_FOR_JUDGEMENT=30`도 "판본 내 30건"으로 재정의해야 진짜 유의미 |
| 3 | **`allocator.decide(min_samples=20)`을 `ledger.MIN_TRIPS_FOR_JUDGEMENT`(30)로 통일** | ★★★★☆ | 0.5 | control | 이미 발견된 불일치(같은 판단 기준이 파일마다 다른 숫자). 상수 하나 참조만 바꾸면 됨 |
| 4 | **`report_claims.jsonl`의 죽은 `schema=1` 필드를 실제로 쓴다** — 스코어링 규칙(가중치·게이트 로직)이 바뀔 때 스키마를 증분하고, `score_direction_claims`가 최신 스키마만(또는 스키마별로 분리 집계) 사용 | ★★★★☆ | 5 | control(report_accuracy.py) | 2026-09-06 가중치 개정처럼 사람이 규칙을 손으로 바꾸는 일이 계속 일어날 것 — `late_regeneration`처럼 나중에 발견되는 대신 지금 필드를 살리면 예방됨 |
| 5 | **경량 `runs.jsonl`(또는 SQLite 1테이블) — 커밋 SHA + params_fingerprint + affected 전략 + 변경 사유** — `experiments.py` 저장 시점에 `git rev-parse HEAD`를 같이 찍기만 하면 됨 | ★★★☆☆ | 6 | control | 지금 커밋→지표 연결이 사람이 `변경기록.md`를 읽어야만 되는데, 이 파일 하나면 "이 커밋 이후 scalp_1m 승률이 어떻게 변했나"를 질의 가능하게 만듦. `변경기록.md`는 계속 남기되, 이건 그 옆의 기계가 읽는 인덱스 |
| 6 | **다중검정 보정 — 17개 전략 동시 판정 시 Benjamini-Hochberg(FDR) 한 줄 추가** — `experiments.consecutive_dead_candidates()`/governor 킬스위치 p값 계산부에 삽입 | ★★★☆☆ | 4 | control | quant-expert 스킬이 이미 "다중검정 편향 무시"를 금기로 명시 — 알고 있는데 코드엔 없음. 17개를 동시에 굴리는 지금 구조에서 우연한 "유의미"가 나올 확률이 실제로 큼 |
| 7 | **`cli.py` 61개 서브커맨드를 3-4개 파일로 분리**(예: `cli_paper.py`/`cli_backtest.py`/`cli_governor.py`, 공유 파서 유틸만 남기고) | ★★☆☆☆ | 10 | apps | 지금 당장 깨진 건 아니지만 7,439줄 단일 파일은 이미 리뷰 비용을 올리고 있고 새 기능마다 계속 커짐. 급하진 않으나 가장 높은 구조적 이자를 물고 있음 |
| 8 | **`변경기록.md` 월 단위 아카이브 규칙**(예: `변경기록-2026-09.md`로 롤오버, `00-START-HERE.md`에서 링크) | ★★☆☆☆ | 2 | control/docs | 6,281줄이 계속 늘기만 함. 검색은 되지만(grep) 세션마다 "맨 위에 추가"하려면 파일이 무거워짐. 값싸고 안전한 정리 |

**PoC 후보(문서 범위 내 소규모 스크립트, 프로덕션 미변경)**: #1의 스키마 변경이
얼마나 파급되는지 보여주는 `docs/plans/`용 더미 스크립트(EC2 스냅샷의
`trades.jsonl` 828행을 읽어 `params_fingerprint` 컬럼을 소급 근사 — 각 전략의
`param_changes.jsonl` 64건 타임스탬프와 조인해 "판본 A/B/C별 승률"을 실제로
갈라 보여주는 것)를 원하면 별도로 만들 수 있음. 이번 평가에는 포함하지 않음
(요청 범위: 분석 + 메모).

---

## 소유자가 결정해야 할 3가지

1. **소급 데이터를 어떻게 할 것인가.** `param_changes.jsonl`(64건, EC2)이 각
   파라미터 변경 시각을 갖고 있으므로, 원하면 과거 원장도 "판본 근사"로
   **소급 태깅**할 수 있다(느슨한 근사 — fingerprint는 미래분만 정확). 안 하면
   scalp_1m/gap_fade/pullback_impulse의 현재 트랙레코드는 "판정 불가"로 리셋해야
   정직하다. 어느 쪽?
2. **NO_GO 판정을 뒤집는 재활성 정책을 명문화할 것인가.** `letf_pair_qqq/sox`,
   `intraday_momentum`은 리서치 저장소 검증에서 NO_GO/음수였지만 소유자 결정으로
   페이퍼 관찰 중이다. 이게 반복되는 패턴이라면(이미 2건), "판정 철회 아님"이라는
   문구만으로는 이 전략들이 다음에 언제·어떤 조건으로 재평가되는지가 코드에
   없다 — review_by 날짜 필드를 `settings.yaml`에 강제할지 결정 필요(투자 #2와 연동).
3. **다중검정 보정을 도입할지, 아니면 "17개 독립 실험이 아니라 사람이 최종
   승인하니 괜찮다"는 현재 암묵적 입장을 유지할지.** 지금은 governor가
   자동으로 자본을 줄이고(`allocator`) 실험이 자동으로 킬스위치를 당기는
   부분(`death_watch.jsonl`)이 있어 "사람이 다 본다"는 전제가 이미 깨져
   있다 — 자동 결정 부분만이라도 FDR을 넣을지는 소유자 리스크 선호의 문제다.

---

## 부록 — 평가에 쓴 핵심 근거 (재확인용)

- `quant/control/ledger.py`: `TradeLedgerSink.on_fill` 스키마, `MIN_TRIPS_FOR_JUDGEMENT=30`, `PAPER_EPOCH_MARKER`/`paper_epoch_ts()`(150-184), `ab_compare()`(693).
- `quant/control/experiments.py`: `params_fingerprint()`(sha256, 16-hex), `consecutive_dead_candidates()`(K=5, p<0.01), `MIN_SAMPLE=30`.
- `quant/control/governor.py`: `evaluate()`/`record()`, `MIN_SAMPLES=30`(ledger 상수 재사용), 주석에 "param_propose.sh가 실제로 낸 건 2026-W35 1주뿐".
- `quant/control/allocator.py`: `decide(min_samples=20)` — ledger 표준(30)과 불일치.
- `quant/control/report_accuracy.py`: `SCHEMA=1`(54), `score_direction_claims` 등이 schema 필터 없이 전량 풀링, `late_regeneration` 플래그(2026-09-06/07 추가)만 예외.
- `quant/control/cost_model.py`: `measure()`가 넘겨받은 trips 전체를 현재 `execution:` 설정 기준으로 재평가 — 개별 행에 당시 수수료율 스냅샷 없음.
- EC2 스냅샷(`ec2_snapshot/data/state/trades.jsonl`, 828행; `data/ledger/param_changes.jsonl`, 64행; `death_watch.jsonl`, 6행; `debate.jsonl`, 19행; `param_proposals.jsonl`, 4행): scalp_1m 320건(39%), 그중 99%가 최신 파라미터 편집(09-05) 이전 체결.
- `config/settings.yaml` git 이력(2026-08-13 이후 32 커밋): scalp_1m 9회, gap_fade/close_bet 각 6회, pullback_impulse/vol_breakout 각 4회 파라미터 편집(토글 제외).
- `quant/apps/cli.py`(7,439줄, 61 서브커맨드), `server/crontab.txt`(93 활성 job), `docs/vault/변경기록.md`(6,281줄), pytest 6,954건 collect(0.95초).
- `.claude/skills/quant-expert/SKILL.md:31` — "다중검정 편향 무시"를 금기로 명시(코드엔 보정 없음, `grep` 전체 0건).
- `/Users/eric/Documents/GitHub/quant-backtest/docs/validation-stack.md` Part 3 — 사전등록 Go/No-Go 프로토콜(리서치 전용, 라이브 파라미터 변경엔 미적용).
