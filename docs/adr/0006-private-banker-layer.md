# ADR-0006: Private Banker 레이어 (banker/)

- Status: Accepted
- Date: 2026-07-28

## Context

거래 엔진과 계좌 진단/리포팅 기능이 한 프로세스·한 실패 도메인으로 묶이면, 리포팅
로직의 버그나 장애가 실거래 루프까지 끌고 내려갈 위험이 있다 (전작 Iron Rule #1의
교훈).

## Decision

거래 엔진과 분리된 읽기 전용 분석 레이어로 `banker/`를 둔다:

- Toss 실계좌 holdings 조회 → 일일 포트폴리오 진단 (집중도, 손익, MDD, 리스크 노출)
- 규칙 기반 진단 + 매도/보유 사유서 생성 → Telegram 발송
- 엔진 핫패스와 분리: banker가 죽어도 거래는 계속된다.

## Consequences

- `banker/`는 `quant.apps.cli report` 서브커맨드로 독립 실행되며, `paper`/`live`
  루프(`quant.apps.cli paper`)와 별개의 프로세스 실행이다.
- banker 코드의 장애는 트레이딩 엔진 가용성에 영향을 주지 않는다.
