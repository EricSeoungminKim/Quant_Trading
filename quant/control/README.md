# quant/control/

## 한 줄 정의

**제어 평면** — 실적을 집계하고, 판정을 내리고, 파라미터를 조정한다. "숫자가
자본 배분을 결정한다"는 이 시스템의 핵심 원칙이 구현되는 곳. 틀리면 잃는 것:
**다음 세션이 나빠진다**(잘못된 자동 강등/승격, 틀린 스코어보드) — 오늘의 거래를
직접 막지는 않지만, 내일의 자본 배분과 파라미터를 그르친다. 자동 조정·실험·
롤백이 이 평면의 일이다.

## 주요 파일 지도 (30여 개 파일, 주제별)

- **원장/스코어보드**: `ledger.py`(거래 원장 + 전략별 승률·payoff 스코어보드,
  이 디렉터리 최대 파일), `strategy_books.py`(전략별 독립 명목계좌 1,000만원
  성과 요약), `symbol_log.py`(종목 점수 일일 원장), `selections.py`(리포트가
  올린 종목의 속성 벡터 영속화), `flows.py`/`frgn_flow.py`(수급 원장).
- **자본 배분/판단**: `allocator.py`(자본 자동 강등 — "오늘 잃어도 되지만 내일은
  줄인다"), `governor.py`(파라미터 자동 반영 거버너, 방어층), `kelly.py`(부분
  켈리 비율 **표시만**, 자문용), `leaderboard.py`(리더보드 승격 규칙),
  `alpha.py`(지수 대비 초과수익 추적).
- **실측 원가/평가**: `cost_model.py`(실측 왕복 비용 모델), `tca.py`(Transaction
  Cost Analysis, 신호가 대비 슬리피지), `forensics.py`(거래 부검 — "졌다"를
  "무엇 때문에 졌다"로).
- **판단 루프**: `experiments.py`(자동 판정 루프), `judgment.py`(판단 귀속 +
  전방 수익률), `outcomes.py`(전방 수익률 채우기), `shadow.py`(LLM 섀도우 판단),
  `ops_judge.py`(판단하는 워치독, LLM 판단 레이어).
- **운영 감시**: `health.py`(운영 이상 감지, 감시 에이전트 입구), `opstate.py`
  (무엇이 마지막으로 언제 성공했나), `delivery_check.py`(소식통 배달 점검),
  `backup.py`(백업 번들 생성 + 대조).
- **리포트 배선**: `daily_feedback.py`(오늘 진입 타이밍 판정), `daily_wrap.py`
  (장 마감 하루 요약 HTML), `close_report.py`(장마감 결과 리포트), `weekly_review.py`
  (주간 재검토 세션).
- **인프라**: `banker.py`(Private Banker, 읽기 전용 계좌 진단), `warehouse.py`
  (아티팩트→분석 저장소 적재), `relation_store.py`(관계 사전 MySQL 색인).

## 핵심 불변식

- **`quant/control/`은 `quant/trade/`를 임포트하지 않는다**(`tests/test_architecture.py`
  의 `FORBIDDEN`). 거버너는 `config/settings.yaml`(정확히는 오버레이 `auto_params.yaml`)
  을 쓸 뿐이고, 엔진이 다음 리로드에 읽는다 — 자동 튜닝이 돌아가는 엔진을
  직접 건드리지 못하게 한다.
- 판단/원장 관련 다수 모듈이 "순수 함수만, I/O는 호출부(`apps.cli`)"로 스스로를
  제한한다(`judgment.py`, `outcomes.py`, `leaderboard.py`, `close_report.py` 등
  docstring에 명시).
- `banker.py`는 읽기 전용·규칙 기반·LLM 없음 — Private Banker는 진단만 하고
  아무것도 실행하지 않는다.

## 데이터 흐름

**상류**: `data/state/trades.jsonl`(거래 원장, `quant/trade/loop.py`가 씀),
`quant/adapters/db.py`(MySQL 분석 저장소), 리포트 아티팩트. **하류**:
`config/settings.yaml`/`auto_params.yaml`(다음 사이클에 `quant/trade/`가 읽음),
Telegram 알림(`quant/adapters/notify/`), `run scoreboard`/`run health` CLI 출력.

## 손대기 전에

- `uv run pytest -q tests/test_architecture.py -v` — `control → trade` 임포트가
  생기지 않았는지.
- `ledger.py`를 건드렸다면 `run scoreboard` 계열 커맨드로 스모크(성적 집계는
  이 파일이 기준점).
- 자동 강등/거버너(`allocator.py`, `governor.py`) 관련 변경은 실거래 전환/리스크
  판단이므로 `quant-expert` 스킬 발동이 필수(루트 CLAUDE.md).
