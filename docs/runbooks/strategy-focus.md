# 한국장 촉매형 변동성 돌파 집중 운영

## 결정과 운영 경계

2026-09-30 소유자가 병렬 전략 운용을 중단하고 KR `vol_breakout_cat` 하나에 모의 검증·데이터 수집을 집중하도록 지시했다. 실계좌 전환이 아니다. 기존 `k=0.5`, `min_stop_bp=40`, `eod_exit_min=5`, `target_weight=0.5`, FRGN 태그 조건과 비용 가정은 동결한다. 선정 이전 36거래의 수익은 다음 기간 성과가 아니다.

`enabled`는 cat만 true, `markets: [KR]`가 조립 단계에서 US 심볼을 제외한다. 기존 capital_fraction과 독립 계좌 기록은 과거 손실·분모 보존을 위해 유지한다. US 자본 선언이 남아 있어도 전략 감시·신호 유니버스는 KR만이다. 설정만 핫리로드하면 옛 객체가 남으므로 재시작 검증이 필요하다.

## 전환 절차

1. 실제 프로세스 인자가 paper인지 확인한다.
2. `TradingControl.halt`로 신규 진입 중단 후 기존 엔진의 정상 flatten 경로로 남은 모의 포지션을 정리한다. 폐장 중이면 다음 개장 시 재시도한다.
3. 포트폴리오 lot 수량과 미체결 상태가 0인지 확인한다. 청산 요청/완료 알림 자체로 성공을 판단하지 않는다.
4. 신규 설정·필터 배포 후 paper 엔진 재시작, KRcat 단일 구성·심볼시장 KR·제어 상태를 확인하고 resume한다.
5. 원장/계좌를 리셋하거나 과거 손실을 지우지 않는다. 기존 전략 정리 매도도 원래 전략의 성과에 남긴다.

## 자료와 반복 검증

- `strategy-focus-capture.timer`: 평일 KST08:28/16:45. 지연 실행은 실제 시각으로 기록한다.
- `data/research/vol_breakout_cat/inputs/YYYY-MM-DD/HHMMSSffffff.json`: 파일 기준 후보/태그 원문, 설정 원문과 SHA, 전략·비용·리스크, 관측시각, KR 틱 파일 존재/크기. 이는 엔진이 앞서 읽은 유니버스 캐시와 일치한다는 증거가 아니다.
- 설정 캡처는 기본 `settings.yaml` 범위다. 전환 전 EC2에 `config/auto_params.yaml`이 없음을 확인했다. 향후 overlay를 도입하면 별도 보존·병합 증거가 필요하며 이 파일만으로 유효 설정 전체가 재현된다고 해석하지 않는다.
- 기존 엔진 `tick_log.enabled: true`로 `data/ticks/KR/YYYY-MM-DD.jsonl`이 계속 누적된다. 틱은 관측 자료이며 전체 종목·전 세션 분봉 완전성을 보장하지 않는다.
- 같은 서비스가 `quant.research.vol_breakout_review`를 실행한다. `review-history.json`은9/7 이후, `review-focus.json`은9/30 이후 진입한 거래를 별도로 집계한다. 두 파일은 연구용이며 공개 사이트의 과거 계좌 곡선과 구간이 다르다.
- 보고 항목: 거래 수·승률·PF·평균 순손익·종목/일 집중·최대 거래/일/종목 제외·추가 편도슬립5/10bp 민감도·파라미터 지문 누락. 미청산 위험은 별도 포트폴리오 확인이 필요하다.
- Codex heartbeat `automation-2`: KST17:00 점검. LA00:00/01:00 두 기동 + KST시간/중복 게이트로 DST 대응. 신규 거래·중요 변화·오류에만 이 작업에 알림, 앱 환경 가용성 필요. 주문/설정/원본 수정 없음.

## 파라미터 개선 규칙

[연구 프로토콜](../research/vol-breakout-focus-2026-09-30.md)을 따른다. 과거 후보 인지 시각을 모르면 과거 가격 리플레이를 촉매 전략 OOS라고 부르지 않는다. 제한된 사전등록 후보를 기간 분리·비용 스트레스·수익 집중도 기준으로 비교한다. 새 표본과 실제 데이터 완전성 확인 없이 최적값을 배포하지 않는다. 승률50% 검정은 순기대수익 검정이 아니다.

## 확인 명령

```bash
systemctl status strategy-focus-capture.timer
journalctl -u strategy-focus-capture.service -n 20
.venv/bin/python -m quant.apps.strategy_focus --root .
.venv/bin/python -m quant.research.vol_breakout_review --ledger data/state/trades.jsonl --since 2026-09-30 --out data/research/vol_breakout_cat/review-focus.json
```
