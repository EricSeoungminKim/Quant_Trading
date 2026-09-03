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

## 6. 백테스트 → 모의투자 승격 → 라이브 (개별 전략, 2026-09-03)

위 1~5절은 **엔진 전체**를 paper에서 live로 넘기는 1회성 절차다. 이 절은
**전략 하나**를 새로 켜거나(burn_in) 정식 검증 단계(backtest_pass)로 올릴 때
매번 반복하는 절차다 — `quant/control/promotion.py` + `quant.apps.cli promote`가
그 반영을 강제한다(증거 없이는 `enabled: true`가 안 된다).

로컬(개발 머신)에서:

```bash
# 1. 백테스트 게이트 — walk-forward OOS + deflated Sharpe + 비용 2배 생존 + fold 안정성
uv run python -m quant.apps.cli backtest-gate --strategy <전략id> --source history \
    --days 90 --total-days 360 --window 90 --step 90 --trials <탐색한 변형 수>
# → data/backtest/gate_<전략id>_<YYYYMMDD>.json 에 GO/NO_GO/판단불가 판정 저장

# 2. dry-run — 정확히 무엇이 바뀔지 diff로 먼저 본다 (파일 안 씀)
uv run python -m quant.apps.cli promote --strategy <전략id> \
    --gate data/backtest/gate_<전략id>_<YYYYMMDD>.json --dry-run

# 3. 반영 — config/settings.yaml 의 해당 전략 블록만 바뀐다
#    (enabled: true, validation.status: backtest_pass, validation.evidence)
uv run python -m quant.apps.cli promote --strategy <전략id> \
    --gate data/backtest/gate_<전략id>_<YYYYMMDD>.json
# 필요하면 --capital-fraction KR=0.05,US=0.05 로 배분도 같이 조정

# 4. 커밋 + 배포
git add config/settings.yaml
git commit -m "promote: <전략id> 백테스트 게이트 GO → backtest_pass"
git push
# EC2 배포는 KR 정규장 마감 후에만 (docs/runbooks/deploy.md) — 장중 재기동 금지
make deploy
```

승격 직후 확인 — `promote --list`로 모든 전략의 status/evidence 요약을 한눈에:

```bash
uv run python -m quant.apps.cli promote --list
```

- [ ] `backtest-gate` 판정이 GO (NO_GO/판단불가면 승격 자체가 `check_promotable`에서
      막힌다 — 종료코드 2 + 막힌 이유 전부 출력)
- [ ] `promote --dry-run` diff가 의도한 전략 블록의 `enabled`/`validation`(그리고
      지정했다면 `capital_fraction`)만 바꾼다 — 다른 전략 블록이 diff에 없어야 함
- [ ] 커밋 메시지에 게이트 파일 경로가 남아 있다(추적 가능성)

**모의투자(paper) 관찰 — 30 라운드트립까지:**

```bash
uv run python -m quant.apps.cli scoreboard --ab      # A/B 갈래를 함께 채점 중이면
uv run python -m quant.apps.cli scoreboard            # 아니면 이것만
```

- [ ] 종결 라운드트립 ≥ 30건 (그 전엔 어떤 숫자도 "판단 불가")
- [ ] 승률 95% 신뢰구간이 50%를 위로 벗어남(ledger.py 판정 기준과 동일)
- [ ] 위 두 조건을 만족하면 사람이 `validation.status: backtest_pass → verified`로
      직접 승격(evidence를 스코어보드 근거로 갱신) — **코드가 자동 승격하지 않는다**

**라이브 전환 여부는 여전히 소유자 판단이다** — 위 paper 30 라운드트립 통과는
"실거래로 넘길 근거가 갖춰졌다"일 뿐, 그 자체가 자동으로 MODE=live를 트리거하지
않는다. 실제 전환은 1~5절 절차를 그대로 따른다.

각 산출물이 증명하는 것:

| 산출물 | 증명하는 것 |
|---|---|
| `data/backtest/gate_*.json` | walk-forward OOS로 본 기대값·샤프·비용 생존성 — **인샘플 성과 아님** |
| `promote --dry-run` diff | 이 반영이 대상 전략 블록 밖을 건드리지 않는다는 것(다른 전략 오염 없음) |
| `config/settings.yaml`의 `validation.evidence` | 어느 게이트 파일·언제·무슨 판정으로 승격했는지(사후 추적) |
| `scoreboard` 30 라운드트립 | 실거래 슬리피지·체결·유니버스 변화를 반영한 **실측** — 백테스트가 라이브를 예측한다고 가정하지 않는다(§0) |
- 브로커에 걸린 조건주문은 엔진 정지와 무관하게 살아 있다 — 반드시 별도 확인.
