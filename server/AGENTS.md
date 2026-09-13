# server/ 운영 지침

현재 운영 지도와 인증·장애 이력은
[`docs/runbooks/codex-operations.md`](../docs/runbooks/codex-operations.md)를 먼저 읽는다.
`CLAUDE.md`의 초기 일정과 과거 Claude 기본값은 역사적 설명이다.

- EC2 접속: `ubuntu@100.87.129.113`(Tailscale).
  저장소는 `/home/ubuntu/quant_trading_kiwoom`, 서버 시간은 `Asia/Seoul`.
- systemd와 cron은 `.venv/bin/python`을 직접 호출한다. `uv run`이나 로그인 셸
  초기화에 의존하지 않는다.
- Codex는 `/home/ubuntu/.local/bin/codex`. `OPS_NARRATOR=codex`를 기본으로
  기존 OpenRouter 무료 폴백을 사용한다. Claude CLI는 운영에 필요하지 않다.
- 시크릿 파일과 인증 캐시의 값은 출력하지 않는다. API 상태 확인에는 필요한 키를
  프로그램으로 주입하고 HTTP/업무 상태·결과 건수만 표시한다. 예외 URL에도
  텔레그램 토큰이 포함될 수 있으므로 원문 예외를 출력하지 않는다.
- 리포트 서술은 도구 없는 Codex 실행이다. 거래 판단 보조 작업은 엔진 밖에서
  실행하고 기존 JSON 인박스 검증을 통과한다. 엔진 핫패스에 LLM 호출을 넣지 않는다.
- `tg-bridge`는 `MemoryMax=400M`, `CPUQuota=50%`를 유지한다. 거래 엔진이
  항상 우선이다. paper/live 모드를 운영 수리의 일부로 변경하지 않는다.
- 알림은 `scripts/lib/notify.sh`의 기존 함수와 주제 매핑을 사용한다.
  운영 봇과 충돌하는 별도 `getUpdates` 호출을 하지 않는다.
- 리포트 장애는 해당 실행 로그, 실제 HTML/엔진 JSON, 공개 URL, 발송 원장을
  함께 확인한다. 서비스가 `active`라는 이유만으로 발행 성공이라고 판단하지 않는다.
- 포트폴리오 공개 전 `validate-performance`와 `performance-xcheck`를 유지한다.
  원장이나 검증 규칙을 바꿔 불일치를 숨기지 않는다.
- 코드 배포는 [`deploy.md`](../docs/runbooks/deploy.md)를 따른다. 유닛 파일을
  바꿨으면 설치본을 복사하고 `daemon-reload`한다. 변경한 프로세스만 재시작한다.
- 검증과 실제 배포 결과를 같은 턴에 `docs/vault/변경기록.md`에 남긴다.
