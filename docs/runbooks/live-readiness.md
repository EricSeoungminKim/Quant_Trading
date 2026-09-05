# 실전 준비 체크리스트 (live-readiness)

소유자 결정(2026-09-06): **전략 외의 모든 기능은 실전 배포 가능한 신뢰도로 먼저 완성한다.**
전략은 검증을 통과할 때까지 자유롭게 갈아끼우고, 통과하는 순간 이 체크리스트가 전부
"검증됨"이어야 바로 실전으로 전환한다. 항목마다 근거 테스트/드릴을 붙인다 — 테스트가
없는 "검증됨"은 없다. 기준선은 `.claude/skills/quant-expert/SKILL.md` §6 과
2026-09-06 안정성 감사(세션 스크래치패드 `stability_audit.md`, 요약은
`docs/vault/변경기록.md`).

상태 값: ✅ 검증됨(테스트/드릴 존재) · 🟡 부분(코드는 있으나 실운영/드릴 미실시) ·
❌ 미검증/결함 · ⏳ 진행 중

## A. 엔진 생존

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| A1 | 루프 예외 격리 — 한 사이클 실패가 프로세스를 죽이지 않는다 | ✅ | loop.py 사이클 try/except + `cycle_failure` 알림, tests/test_loop_* |
| A2 | 하트비트 + 워치독(5분 데드맨) | ✅ | data/state/heartbeat.json 5일 무실패, server/scripts/watchdog.sh |
| A3 | settings.yaml 핫리로드 파싱 오류에도 엔진이 죽지 않는다 | ✅ | quant/apps/config.py reload_if_changed(마지막 정상 설정 유지 + 운영 경보), tests/audit_2026_09_06/test_settings_hot_reload_yaml_error.py (2026-09-06) |
| A4 | 재시작 시 포지션·손절 메타 복원 | ✅ | `_persist_position_meta` 테스트, lot state_update |
| A5 | 재시작 시 일중 상태(쿨다운·일일 손실·주문 수) 복원 | 🟡 | `_save_day_state` 저장은 있으나 재시작 직후 공백 확인 필요 |
| A6 | 디스크 가득/저장 실패 시 장부-메모리 괴리 차단 | ✅ | Portfolio.save 원자화(fsync+rename) + PaperBroker 실패 시 롤백·재발생, tests/audit_2026_09_06/test_paper_broker_save_failure_divergence.py (2026-09-06) |
| A7 | OS 자동 재시작 우회(needrestart 제외) | ✅ | deploy.sh 주석 + /etc/needrestart/conf.d |

## B. 데이터

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| B1 | 라우트 폴백 — 짧은 봉 프레임도 다음 라우트로 | ✅ | MarketDataService._history_raw 요청의 절반 미만이면 다음 라우트·최장 프레임 채택, tests/audit_2026_09_06/test_data_service_short_frame_no_fallback.py (2026-09-06) |
| B2 | 키움 US 라우트 429 폭주 차단(회로차단기) | ✅ | us_datafeed 회로차단기 5회/60초→30분(settings datafeed.kiwoom_us_breaker), tests/test_kiwoom_us_datafeed.py (2026-09-06) |
| B3 | 유니버스 리로드 콜드 페치로 사이클 30초 정체 방지 | ✅ | service.prefetch + loop 리로드 배선(시간 예산), tests/test_universe_prefetch.py·tests/test_service.py — 실운영 사이클 시간은 월요일 08:27 리로드에서 확인 |
| B4 | 낡은 시세 감지(stale) + 폴백 | ✅ | kiwoom_rt stale 30초 → Toss, 스로틀 로그(09-05) |
| B5 | 휴장일 처리 — Toss 캘린더 실패 시 정적 폴백이 휴장일에 주문을 내지 않는다 | ✅ | tests/test_session.py 휴장일 폴백 테스트 (2026-09-06) |
| B6 | 분할·배당 조정 데이터 검증(백테스트) | ✅ | quant-backtest qb/letf/data_checks |

## C. 리스크 레일

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| C1 | 일중 하드 레일(손절 ≤5%, 목표 ≤10%, 사이클당 −5% 강제 청산) | ✅ | tests/test_risk_intraday_hardrail.py |
| C2 | 쿨다운 (symbol,strategy), 반복 진입 상한 | ✅ | tests |
| C3 | 동시 포지션 상한, 레버리지 노출 상한, 최소 주문금액 | ✅ | tests |
| C4 | 상한가 종목 진입 차단, 단일종목 레버리지 3천만원 미만 차단 | ✅ | tests |
| C5 | shared / per_strategy 두 경로의 레일 동일성 | ✅ | tests/test_risk_cross_mode_parity.py (2026-09-06) |
| C6 | 적립형(오버나이트) 레인의 가격 손절 부재 → 포트폴리오 수준 최대손실 레일 | ✅ | `risk.accumulate_max_loss_pct: 15`, loop `_accumulate_max_loss_check`, tests/test_accumulate_max_loss_rail.py (2026-09-06) |
| C7 | 마감 청산이 판단 주기와 무관하게 발동(§5 고전 결함) | ✅ | 감사: 활성 전략 전부 구조적으로 해결 |
| C8 | 미체결 청산 요청 영속 + 개장 재시도 | ✅ | pending_flatten (09-04) |

## D. 브로커·회계

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| D1 | 브로커 잔고 대조 + 불일치 자동 정지 | 🟡 | quant/trade/reconcile.py — 페이퍼에서는 no-op, 실운영 실행 이력 0 → 실전 전 스테이징 드릴 필수 |
| D2 | 원장 손상 내성(줄 단위 스킵) | ✅ | ledger.load_trades |
| D3 | 전략별 독립 계좌(fixed_dual) 사이징·현금 게이트 | ✅ | tests/test_risk_fixed_dual.py (2026-09-06) |
| D4 | 에폭 리셋 절차(엔진 정지 확인, 장부·포트폴리오 보관) | ✅ | cli paper-epoch, tests/test_cli_paper_epoch.py — 정지 확인은 systemctl 우선(하트비트는 systemctl 없을 때만 대체 신호, 2026-09-06 수리: 정지 직후 하트비트가 여전히 "최근"으로 보여 오판하던 결함) |
| D5 | 백업 번들 + 오프사이트 풀 + 복원 리허설 | ✅ | backup.sh 매일, backup_restore_check.sh 매월 첫째 일요일 03:50 KST(server/crontab.txt) — 성공 시 `data/backups/LAST_REHEARSAL` 기록, `health.backup_findings`가 40일 초과 시 경보(ops_watch.sh), tests/test_health.py — 실제 EC2 리허설 실행 이력은 아직 0회 |
| D6 | 수수료·세금·슬리피지 실측 대비 | ✅ | 요율표 반영(08-19) + `cli slippage-report`(quant/control/slippage.py, 체결↔스프레드 ±5분 최근접 매칭 → market/strategy별 중앙값·p90·왕복환산 vs slippage_bps 가정)·spread_sample_us.sh가 TQQQ/SQQQ/SOXL/SOXS 앵커 강제, tests/test_slippage.py·tests/test_cli_slippage_report.py — 실측 표본은 크론 가동 후 축적(현재 0건) |

## E. 제어·알림

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| E1 | 킬스위치 /halt·/resume·/flatten 코드 경로 | ✅ | tg_bridge 테스트 |
| E2 | 킬스위치 실전 드릴(제어실 토픽에서 사장님 계정으로) | ❌ | 실사용 이력 0 — 월 1회 드릴 필요(소유자 작업) |
| E3 | 텔레그램 레인 5개 라우팅 | ✅ | 2026-09-05 바인딩·테스트 발송 확인 |
| E4 | 배포 하드가드(장중 재시작 거부) | ✅ | deploy.sh |
| E5 | 배포 드리프트 감지(EC2 HEAD vs origin/main) | ✅ | deploy.sh → data/state/last_deploy_sha.txt, ops_watch.sh 비교·1회 경보 (2026-09-06) |
| E6 | 운영 감시(ops_watch) 오경보 억제·발견 단위 지문 | ✅ | 09-04 |

## F. 리포트·사이트

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| F1 | 리포트 주장 원장 + 정확도 스코어카드(방향·섹터·종목·점수) | ✅ | quant/control/report_accuracy.py, data/ledger/report_claims.jsonl, `report accuracy` 크론 06:40, 리포트 정확도 상자, tests/test_report_accuracy.py (2026-09-06) — 섹터 픽 평가는 sector_daily 이력 축적 후 |
| F2 | 사이트 데이터 발행 게이트(스키마·결측·연속성·거래수 비감소) | ✅ | quant/control/performance_contract.py, publish-perf/validate-performance, publish_portfolio.sh 푸시 중단+경보, tests/test_performance_contract.py (2026-09-06) |
| F3 | 사이트 빌드 시 데이터 검증 훅 | ✅ | quant-portfolio scripts/check-data.mjs prebuild (2026-09-06, Vercel 반영) |

## G. 전략 사망 판정·승격

| # | 항목 | 상태 | 근거 |
|---|---|---|---|
| G1 | 게이트(OOS n·CI·DSR·비용×2·순열·룩어헤드) → promote 만 승격 | ✅ | quant/backtest/gate.py + quant-backtest qb/letf/validate.py |
| G2 | 라이브 표본 n≥30 Wilson CI 판정 → 축소·비활성 | ✅ | scoreboard, 2026-09-05 결정 |
| G3 | 페이퍼 슬리피지·승률 대 백테스트 괴리 대조 | 🟡 | 절차만 정의, 자동화 미완 |

**실전 전환 조건**: A~G 전부 ✅ 또는 🟡→✅, 그리고 어느 전략이 G1 GO + 페이퍼 100건 이상
CI 하한 > 0. 이 문서는 항목이 바뀔 때마다 `docs/vault/변경기록.md` 와 함께 갱신한다.
