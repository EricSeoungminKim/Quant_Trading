# 로컬 맥 원버튼 ML 파이프라인

**Cursor 터미널에서 `make ml` 한 줄이 전부다.**

## 언제 돌리나

하루 1회, 오전 7시 이후 아무 때나. 이유: 전날 16:00 KST 장마감 리포트가
EC2 MySQL `forward_return`(D+1)을 채우는 게 그 시점이라, 오전에 돌리면 전날
거래일까지 라벨이 확정된 최신 데이터를 본다. 24시간 상시 가동이 필요 없다 —
이 파이프라인은 서버가 아니라 사람이 손으로 누르는 배치 스크립트다.

## 명령

```bash
make ml
```

내부적으로 `local/ml/run.sh`를 실행한다. 순서:

1. **동기화** — EC2(`ubuntu@100.87.129.113:~/quant_trading_kiwoom`)에서 최신
   원장·파케이(`data/ledger/selections.jsonl`, `data/state/trades.jsonl`,
   `data/history/`)를 `data/ml/`로 rsync `--update` 증분 동기화한다. 실패하면
   중단한다 — "동기화 안 된 채 옛 데이터로 학습"을 조용히 하지 않는다.
   추가로 EC2 MySQL의 `selection ⋈ forward_return`(D+1) 조인 결과를 SSH로
   받아 `data/ml/train_labeled.json`에 저장한다 — **실제 학습 라벨은 이
   MySQL 조인에만 있다**(아래 "왜 MySQL인가" 참고).
2. **표본 게이트** — 시장(KR/US)별 독립 거래일 수가 30(운영
   `ml_scorer.MIN_TRAIN_DAYS`와 동일한 임계, 재정의하지 않고 그대로 재사용)
   미만이면 `표본 수집 중 KR n/30 · US m/30 — 학습 생략`을 출력하고 정상
   종료한다(exit 0, 에러 아님).
3. **학습** — 임계를 충족한 시장만 GradientBoosting을 purged walk-forward로
   학습·검증한다(`quant/research/ml_train.py`).
4. **산출** — `local/ml/out/YYYY-MM-DD/`에 `report.md`(타깃별 OOS 성적, 보정
   Brier, OOS 피처 중요도, 베이스라인 대결, 직전 실행 대비 델타, 다중검정
   신고), `model_<시장>_<타깃>.joblib`(3개, 참고용·미배포), `summary.json`을
   낸다. `local/ml/registry.jsonl`에도 이번 실행 한 줄이 추가된다(다음 실행의
   델타 계산 근거).

**자동 반영 없음.** `report.md` 맨 끝에 "참전 제안은 사람이 결정: 리포트를
보고 판단하라"가 항상 붙는다 — 이 파이프라인은 운영 판단(`ml_scorer.py`,
judgments 원장)에 자동으로 개입하지 않는다.

## 표본 임계 읽는 법

2026-08-30 기준 EC2 MySQL 실측: KR 10거래일, US 11거래일. 둘 다 30 미만이라
지금 `make ml`은 매일 "학습 생략" 경로로 정상 완주한다. 임계(30)를 채우려면
운영 `ml_scorer.sh`가 매일 아침/개장전 selection 행을 쌓는 것을 기다려야
한다 — 이 로컬 파이프라인이 표본을 만들어내지 않는다, 그냥 잰다.

## 옵션 환경변수

```bash
SKIP_SYNC=1 make ml     # 오프라인 재실행 — 이미 당겨온 data/ml/ 캐시로만 돈다
DRY_RUN=1 make ml       # rsync/ssh/scp 명령을 실제로 실행하지 않고 출력만 한다
PUSH=1 make ml          # 학습 결과 요약 json 을 EC2 data/ml_inbox/ 로 scp (서버 쪽 소비는 범위 밖 — 파일만 놓는다)
QT_SSH_HOST=ubuntu@<IP> make ml   # 기본값(ubuntu@100.87.129.113)과 다른 호스트
```

## 왜 MySQL인가 (selections.jsonl이 아니라)

`quant/control/selections.py`는 원래 각 선정 행의 `outcome_*`를 사후에 채울
의도였지만, 2026-08-30 EC2 실측(`selections.jsonl` 전수 확인)으로는
`outcome_filled`가 단 한 건도 `true`가 아니다 — 전방 수익률은 실제로는 MySQL
`forward_return` 테이블에만 쌓인다. 그래서 이 파이프라인은
`local/ml/remote_dump.py`를 원격 파이썬 표준입력으로 흘려보내(EC2 파일시스템에
아무것도 남기지 않는다) MySQL 조인 결과를 직접 받아온다. 비밀번호는 원격
`.env.local`에서만 읽고 로컬 커맨드라인에 노출되지 않는다.

`data/ledger/selections.jsonl`/`data/state/trades.jsonl`/`data/history/`도
함께 동기화하지만, v1 학습(`quant/research/ml_train.py`)은 아직 MySQL 조인만
소비한다 — 나머지는 향후 피처 확장·수동 점검을 위해 로컬에 미리 놓아둔
것이다.

## 학습 하네스 설계 요약 v2 (`quant/research/ml_train.py`)

- **피처**: `quant.analyze.ml_scorer.FEATURE_NAMES`(운영 채점기와 동일한 13개)를
  그대로 재사용한다.
- **타깃 3개**: `d1_direction`(D+1 방향 분류), `d1_return_bps`(D+1 수익률 회귀),
  `d5_direction`(D+5 방향 분류, 라벨 성숙분만). "달성 가능 이익"(MFE) 라벨은
  `forward_return` 스키마에 구간 내 최고/최저가 컬럼이 없어 **구현하지
  않았다** — 없는 라벨을 지어내지 않는다.
- **검증**: `quant/backtest/purged_cv.py`의 purge+embargo를 **거래일 단위**로,
  타깃별 `label_horizon`(D+1=1, D+5=5)에 맞춰 적용한다.
- **모델**: `GradientBoostingClassifier`/`Regressor`. 하이퍼파라미터는
  `d1_direction`에서만 8콤보 그리드 탐색(outer purged OOS 폴드로 직접 선택,
  nested 아님 — 낙관 편향 있음을 신고), 나머지 두 타깃은 그 결과를 재사용한다.
- **확률 보정**: `CalibratedClassifierCV`(표본 크기에 따라 sigmoid/isotonic),
  Brier 점수로 보정 품질 신고.
- **피처 중요도**: 각 fold의 **OOS** 테스트 세트에서 permutation importance를
  구해 fold 평균(인샘플 impurity 중요도는 쓰지 않는다 — 오도 위험).
- **베이스라인 head-to-head**: `d1_return_bps` OOS 예측과 `selections`의
  규칙 채점기 점수(`baseline_score100`)를 같은 날·같은 종목에서
  `quant.control.leaderboard.daily_rank_ic`(리더보드와 같은 지표)로 순위상관
  비교 + 상위 N 평균 수익 비교. **2026-08-30 기준 MySQL `selection`
  테이블에 `baseline_score100` 컬럼이 없어** 이 비교는 매일 "데이터 없음"으로
  보고된다 — 스키마 마이그레이션 제안은 `ml_train.py` 모듈 docstring 참고.
- **모델 레지스트리**: `local/ml/registry.jsonl`에 실행마다 한 줄 append
  (ts/git_sha/시장별 표본·타깃별 성적/베이스라인 비교/하이퍼파라미터 탐색 수).
  직전 줄과 비교한 "직전 실행 대비 델타" 절이 매 리포트에 붙는다.
- **다중검정 신고**: 타깃 수 × 시장 수 + 하이퍼파라미터 탐색 콤보 수를 리포트
  맨 위에 명시하고, `leaderboard.required_t`로 참고용 요구 t도 함께 낸다.
