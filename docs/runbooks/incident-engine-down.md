# Runbook: quant-engine 다운 대응

장중에 `quant-engine` systemd 유닛이 죽었거나, 사이클이 멈춘 것으로 의심될 때.

## 1. 살아있는지 확인

```bash
ssh $QT_SSH_HOST "sudo systemctl status quant-engine"
```

- `Active: active (running)`이면 프로세스는 살아있다 → §3(사이클 확인)으로.
- `Active: failed`/`inactive`면 §2(재시작)로.

## 2. 재시작

```bash
ssh $QT_SSH_HOST "sudo systemctl restart quant-engine"
ssh $QT_SSH_HOST "sudo systemctl status quant-engine"
```

재시작 직후 `Active: active (running)`인지 다시 확인. `SuccessExitStatus=143`이
설정돼 있으므로 SIGTERM에 의한 종료(exit 143)는 'Failed'로 오경보되지 않는다 —
그 외 종료 코드로 반복 실패한다면 §4(로그 분석)로 바로 넘어간다.

## 3. 마지막 사이클 확인 (프로세스는 살아있는데 멈춘 것 같을 때)

```bash
ssh $QT_SSH_HOST "journalctl -u quant-engine --since '10 min ago'"
```

`paper loop 시작 — poll_seconds=N` 로그 이후 주기적으로 사이클 로그가 찍혀야 한다
(poll_seconds는 `config/settings.yaml`의 `engine.poll_seconds`, 기본 10초). 로그가
멈춰 있으면 재시작(§2)하고 §4로 원인을 확인한다.

## 4. 로그 분석

```bash
ssh $QT_SSH_HOST "journalctl -u quant-engine -n 200 --no-pager"
ssh $QT_SSH_HOST "journalctl -u quant-engine -f"   # 실시간 tail
```

흔한 원인:

- **Toss API 403** — Elastic IP가 Toss Open API 허용목록에서 빠졌을 가능성
  (`server/README.md` §0). 토스증권 앱 → 설정 → Open API → IP 허용목록 확인.
- **`.env.local` 누락/오타** — 시크릿 값이 비어 있으면 브로커 초기화가 실패할 수
  있다. `secrets-rotation.md` 참고해 서버의 `.env.local` 내용을 점검.
- **`uv sync` 미실행 상태로 코드만 pull됨** — 의존성 버전 불일치. 수동으로
  `ssh $QT_SSH_HOST "cd quant_trading_kiwoom && uv sync"` 후 재시작.

## 5. banker 리포트(cron)가 안 왔을 때

거래 엔진과 무관한 별도 프로세스(cron)다. 엔진이 죽어도 영향받지 않고, 반대도
마찬가지다 (ADR-0006).

```bash
ssh $QT_SSH_HOST "crontab -l"                       # 크론 설치 확인
ssh $QT_SSH_HOST "cd quant_trading_kiwoom && tail -50 data/report.log"
```

## 6. tg-bridge(Telegram↔Claude 브릿지) 문제

엔진과 격리된 별도 유닛 — 이게 죽어도 거래 엔진은 영향받지 않는다.

```bash
ssh $QT_SSH_HOST "sudo systemctl status tg-bridge"
ssh $QT_SSH_HOST "journalctl -u tg-bridge -n 100"
```

`MemoryMax=400M`/`CPUQuota=50%`로 격리돼 있으므로, 브리지가 폭주해도 엔진 자원을
빼앗지 않는다 — 브리지만 재시작하면 된다: `sudo systemctl restart tg-bridge`.
