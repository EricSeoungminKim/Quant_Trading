# Runbook: 실계좌 전환 (paper → MODE=live)

"코드가 완성됐다"와 "실제 돈을 맡긴다" 사이의 절차. 순서를 건너뛰지 않는다 —
각 단계는 앞 단계가 실측으로 통과했을 때만 진행한다.

## 0. 전제: 왜 이 절차가 있나

- 이 계좌는 **사용자가 수동으로도 매매하는 실계좌**다. 엔진 결함은 엔진 돈만
  날리는 게 아니라 사용자의 수동 포지션 관리까지 오염시킬 수 있다.
- 10년 백테스트 기준, 토스 US 수수료(왕복 20bp)에서 순마진이 0 근처다
  (`docs/CODE-TOUR.md` §12, 메모리 `intraday-cost-ceiling`). **미장 실전은 검증
  장치이지 수익 장치가 아니라는 전제로 소액에서 시작한다.** 경제성이 열려 있는
  쪽은 국내 상장 ETF(거래세 면제, 왕복 3~6bp)다.

## 1. 구 시스템 정지 (가장 먼저 — 건너뛰면 이중 주문)

stock-algo-trade가 같은 EC2에서 **같은 토스 계좌**로 돌고 있다. 새 엔진을 먼저
올리면 두 엔진이 같은 계좌에 동시 주문한다.

```bash
ssh $QT_SSH_HOST "sudo systemctl disable --now stock-algo-trade 2>/dev/null; \
                  crontab -l | grep -v stock-algo-trade | crontab -"
ssh $QT_SSH_HOST "ps aux | grep -i [s]tock-algo"   # 결과가 비어야 함
```

- [ ] 구 엔진 프로세스 0개 확인
- [ ] 구 crontab 항목 제거 확인
- [ ] 토스 앱에서 구 엔진의 미체결 주문·조건주문 잔존 여부 확인 후 정리

## 2. 새 엔진 배포 (paper)

`deploy.md` 절차 + 이 저장소 특이사항:

- [ ] `server/systemd/quant-engine.service`의 `Environment=MODE=paper` 확인
      (MODE는 systemd가 단일 진실 소스 — `.env.local`이 덮어쓸 수 없다)
- [ ] `.env.local` scp — `TELEGRAM_BOT_TOKEN ≠ TELEGRAM_BRIDGE_BOT_TOKEN` 확인
      (같으면 getUpdates 폴러 둘이 서로의 업데이트를 훔친다)
- [ ] `data/state/portfolio.json` 초기 상태 확인 (잔존 상태로 시작 금지)
- [ ] 기동 로그에서: 데이터 라우트, 환율 소스, 킬 스위치 상태, 사이클 지연 확인
- [ ] **EC2에서만 가능한 실측 3종**:
      - `GET /api/v1/commissions` — 실제 수수료율을 확인해 `execution.fee_bps`와 대조
      - 조건주문(OCO) 등록/취소 1회 왕복 — [미검증] 스키마 확정
      - `python -m quant.apps.cli kiwoom-probe` — 키움 키 등록 후 웹소켓 수신 확인

## 3. Paper 번인 (미국장 최소 3세션, 한국장 병행 시 KR도 3세션)

매 세션 종료 후 확인:

- [ ] 브로커 대사(reconciliation) 불일치 0건
- [ ] 하트비트 공백 없음 (장중 5분 간격)
- [ ] 사이클 지연 경고(slow_cycle_warn) 0건
- [ ] 체결 로그의 모든 진입에 대응하는 청산 존재 (오버나잇 0건)
- [ ] 라이브 신호와 같은 날 백테스트 리플레이의 신호 대조 — 불일치는 결함
- [ ] 수수료 합계가 `commissions` API 실측 요율과 일치

하나라도 실패하면 원인 수정 후 번인 3세션을 다시 센다.

## 4. MODE=live 전환 (사람이 직접)

```bash
ssh $QT_SSH_HOST "sudo sed -i 's/MODE=paper/MODE=live/' /etc/systemd/system/quant-engine.service && \
                  sudo systemctl daemon-reload && sudo systemctl restart quant-engine"
ssh $QT_SSH_HOST "journalctl -u quant-engine -n 20 | grep MODE"
```

- [ ] 첫 세션은 `risk_budget_pct`를 평소의 절반으로 (소액 검증)
- [ ] 첫 실체결 후: 토스 앱 체결내역 vs 엔진 Fill 로그 대조 (가격·수량·수수료)
- [ ] 조건주문(손절)이 브로커에 실제로 걸렸는지 토스 앱에서 육안 확인
- [ ] 첫 세션 동안 사람이 상시 대기 (킬 스위치: `/halt`, `/flatten`)

## 5. 롤백

문제 발생 시:

```bash
ssh $QT_SSH_HOST "sudo sed -i 's/MODE=live/MODE=paper/' /etc/systemd/system/quant-engine.service && \
                  sudo systemctl daemon-reload && sudo systemctl restart quant-engine"
```

- 열린 포지션은 자동 청산되지 않는다 — `/flatten`으로 정리하거나 토스 앱에서 수동 정리.
- 브로커에 걸린 조건주문은 엔진 정지와 무관하게 살아 있다 — 반드시 별도 확인.
