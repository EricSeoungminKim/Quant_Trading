# Runbook: 시크릿 로테이션

`.env.local`은 gitignore 대상이며 서버에는 `scp`로만 전달한다 — 절대 git에 커밋하지
않는다. 키 목록의 기준은 `.env.example` (값은 비어 있는 템플릿).

## 키 목록과 사용처

| 키                                                           | 사용처                                                                                                  |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `MODE`                                                       | 전역 실행 모드 (`paper`\|`live`) — `quant/apps/cli.py`, 각 브로커의 주문 가드                         |
| `TOSS_CLIENT_ID` / `TOSS_CLIENT_SECRET` / `TOSS_ACCOUNT_SEQ` | `quant/adapters/brokers/toss/client.py` (Toss Open API 인증), `quant/apps/cli.py`의 `report` 서브커맨드 |
| `KIWOOM_APP_KEY` / `KIWOOM_SECRET_KEY`                       | `quant/adapters/brokers/kiwoom/client.py` — 실전 앱(위탁 계좌). 웹소켓 시세 + ka10059 수급 (2026-08-10 실검증) |
| `KIWOOM_MOCK_APP_KEY` / `KIWOOM_MOCK_SECRET_KEY`             | 모의투자 앱 (모의서버 검증/재현용 — 실전과 분리 발급, 토큰 캐시도 base_url별 분리)                       |
| `KIWOOM_GLOBAL_APP_KEY` / `KIWOOM_GLOBAL_SECRET_KEY` / `KIWOOM_GLOBAL_ACCOUNT_NO` | 해외증권 계좌 앱 — 발급됨, 아직 코드 미사용(자리만)                                |
| `KIWOOM_ACCOUNT_NO` / `KIWOOM_BASE_URL`                      | Kiwoom 계좌·엔드포인트 선택 (mockapi ↔ 모의계좌, api ↔ 실계좌 — 반드시 짝 맞출 것)                      |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`                    | `quant/adapters/notify/telegram.py` (일방향 알림 봇)                                                      |
| `TELEGRAM_BRIDGE_BOT_TOKEN` / `TELEGRAM_BRIDGE_CHAT_ID`      | `server/scripts/tg_bridge.py` (양방향 Claude 브릿지 — 알림 봇과 별도 봇)                                |
| `START_CAPITAL_KRW`                                          | `quant/apps/cli.py`의 `cmd_paper` (paper 계좌 초기 자본, 시크릿은 아니지만 `.env.local`에 위치)       |

## 로테이션 절차 (일반)

1. **새 값 발급** — 해당 서비스(Toss 개발자센터, Kiwoom 개발자센터, Telegram
   BotFather)에서 새 키/토큰 발급. 기존 키를 먼저 폐기하지 않는다 (교체 창구
   확보 전까지 이중화).
2. **로컬 `.env.local` 갱신** — 해당 키만 교체, 나머지는 그대로.
3. **서버에 전달**:
   ```bash
   scp .env.local ubuntu@<ElasticIP>:~/quant_trading_kiwoom/
   ssh ubuntu@<ElasticIP> "sudo systemctl restart quant-engine tg-bridge"
   ```
4. **동작 확인** (`incident-engine-down.md` §4 참고):
   ```bash
   ssh ubuntu@<ElasticIP> "journalctl -u quant-engine -n 50"
   ```
   403/401 에러가 없으면 성공.
5. **이전 키 폐기** — 새 키가 정상 동작 확인된 후에만 발급처에서 이전 키를 revoke.

## Toss 전용 주의사항

Toss Open API는 **등록된 IP만 허용**한다. 키를 새로 발급해도 EC2 Elastic IP가
Toss 앱의 IP 허용목록에 등록돼 있어야 한다 (`server/README.md` §0) — IP 허용목록은
키 로테이션과 별개로 유지되지만, 인스턴스를 새로 띄우는 경우라면 반드시 함께
확인한다.

## Kiwoom 전용 주의사항

`KIWOOM_BASE_URL`은 계좌 종류와 반드시 짝이 맞아야 한다: `mockapi.kiwoom.com` ↔
모의계좌, `api.kiwoom.com` ↔ 실계좌. 로테이션 시 base URL과 계좌 번호를 따로
바꾸면 계좌 불일치로 인증이 실패하거나(더 나쁘게는) 잘못된 계좌를 조회할 수 있다.

## 절대 하지 말 것

- `.env.local`을 커밋하거나 `git add -f`로 강제 추가하지 않는다.
- 시크릿 값을 로그(`journalctl`, `data/*.log`)나 Telegram 메시지에 그대로 찍지
  않는다.
- 로테이션 중 이전 키를 먼저 폐기하지 않는다 — 서버 반영 확인 전에 폐기하면
  엔진이 인증 실패 상태로 멈춘다.
