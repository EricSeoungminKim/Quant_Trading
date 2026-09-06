# Runbook: 텔레그램 발송 카탈로그

이 문서는 이 저장소가 텔레그램으로 내보내는 **모든** 메시지 종류를 코드에서
직접 추출해 한곳에 모은 것이다(2026-09-06, 리포트·텔레그램·사이트 실전화
4단계). 목적은 "매매 관련 알림이 알고 보니 새로운 형식으로 깨져서 나간다"나
"이 알림이 어디서 오는지 아무도 모른다" 같은 사고를 코드 대조 없이도 막는
것이다 — 발신자·트리거·레인·형식·이스케이프·중복·빈도가 표 하나에 있다.

**읽는 법:**
- **발신자**: 실제 소스 파일/모듈. 셸 크론은 전부 `server/scripts/lib/notify.sh`
  세 함수(`notify_now`/`notify_auto`/`notify_defer`) 중 하나를 거친다 — 그
  게이트의 규칙은 `notify.sh` 자체 주석과 [`docs/runbooks/telegram-rooms.md`]
  (telegram-rooms.md)에 있고 여기서 반복하지 않는다. 엔진은
  `quant.adapters.notify.telegram.TelegramNotifier`(`quant/trade/loop.py`가
  호출), 브리지는 `server/scripts/tg_bridge.py`가 직접 `sendMessage`를 친다
  (게이트를 거치지 않는다 — 명령 응답은 "지금 조치 안 하면 손해"류 분류가
  아니라 늘 즉시·동기 응답이라 세 함수의 분류 자체가 적용되지 않는다).
- **타이밍**: immediate(즉시 발송) / queued(장중이면 `data/notify_queue.jsonl`
  로 미뤄졌다가 장 마감 문서에 실림, 텔레그램 채팅으로는 절대 안 감) /
  deferred(문 자체가 즉시 발송을 안 한다 — 항상 큐로만 감).
- **HTML 엔티티**: `quant.core.tgfmt`(L1 서식, `<b>`/`<code>`/`<pre>` 등)를 쓰는지,
  평문인지. 엔진 메시지는 대부분 tgfmt, 크론 리포트는 스크립트마다 다르다
  (본문에 이미 tgfmt 스타일 태그가 섞인 것도 있다 — `notify.sh`가 `parse_mode=
  HTML`로 먼저 보내고 텔레그램이 거부하면 평문 폴백한다).
- **최대 길이**: 텔레그램 `sendMessage` 하드캡은 4096바이트(UTF-8). `notify.sh`
  는 하드캡에서 자르고(`text[:_MAX_LEN-1]+"…"` 는 파이썬 쪽, 셸 쪽은 자르지
  않고 텔레그램에 그대로 보내 400을 받을 수 있다 — 아래 "알려진 격차" 참고),
  다수 크론 스크립트는 관례적으로 **3900자**에서 미리 잘라 보낸다(`${OUT:0:3900}`)
  — 그 마진이 이 표의 "소프트 캡" 열이다.
- **중복 키**: 같은 알림이 반복 발송되지 않게 막는 장치(있으면). 없으면 "—".
- **빈도/일**: 정상 운영 시 실측 근사치(크론 스케줄 기준, 조건부 발송은 "조건부"로
  표시).
- **실패 시 보이는 곳**: 그 발송이 실패했을 때 무엇이 남는가/어디를 봐야 하는가.

---

## 0. 발신 경로 3갈래

| 경로 | 코드 | 실패 처리 |
|---|---|---|
| 엔진(파이썬, 장중 상시 프로세스) | `quant/trade/loop.py` → `quant.adapters.notify.telegram.TelegramNotifier.send()` | HTML 400 → 평문 1회 재시도, 5회 연속 실패 시 10분 뮤트. 모든 시도가 `data/ledger/notifications.jsonl`(성공/실패 전부)과, 실패만 `data/ledger/notify_failures.jsonl`(2026-09-06 신규)에 남는다. |
| 크론(셸, 배치 잡) | `server/scripts/*.sh` → `. lib/notify.sh` → `notify_now`/`notify_auto`/`notify_defer` | 같은 HTML→평문 폴백. 최종 실패는 `data/ledger/notify_failures.jsonl`(2026-09-06 신규)에 남는다. 큐 쓰기 실패는 반환값으로 호출부에 전달(스크립트 로그로 감). |
| 브리지(파이썬, 상시 프로세스) | `server/scripts/tg_bridge.py` → `TelegramBotClient.send_message()` | 게이트를 거치지 않는다 — 실패하면 `httpx` 예외가 브리지 프로세스 로그(`journalctl -u tg-bridge`)에 남을 뿐, 별도 원장이 없다(아래 "알려진 격차" 참고). |

모든 경로가 공유하는 것: **레인(포럼 토픽) 라우팅**(`quant/core/tglanes.py`
단일 정의, 매핑은 `data/state/tg_lanes.json`) — 미바인딩이면 레거시 단일
채팅으로 폴백하고, 다른 레인이 하나라도 바인딩된 뒤라면 헤더(이모지+이름)를
붙인다. 브리지 명령 응답만 예외다 — 레인표를 보지 않고 **명령이 온 채팅/토픽에
그대로 답한다**(사용자가 어디서 물었는지가 곧 정체성이므로 재라우팅할 이유가
없다).

---

## 1. 엔진 실시간 알림 (`quant/trade/loop.py`)

전부 `TelegramNotifier.send(text, lane=...)`를 통과한다. 서식은 전부
`quant.core.tgfmt`(L1) 기반, 태그 불균형은 `tests/test_loop_html_formatting.py`
의 `_assert_balanced_html`(이 카탈로그의 계약 테스트가 재사용한다)이 고정한다.

| 메시지 | 트리거 | 레인 | 타이밍 | 최대 길이 | 중복 키 | 빈도/일 | 실패 시 |
|---|---|---|---|---|---|---|---|
| 🟢/🔴 매수·매도 체결 | 매 체결 (`_execute_signal`, loop.py:605) | `trades` | immediate | 4096(tgfmt.compose 없음, 직접 join) | — (체결마다 고유) | 전략·시장 활성도에 따라 0~수십 | `notify_failures.jsonl` + 로그 `Telegram 전송 실패` |
| ⚠️ 주문 실행 실패 | 주문 제출 예외 (loop.py:556) | `trades` | immediate | 평문 | — | 드묾(0~소수) | 동일 |
| ⏳/🚫 승인 요청 만료·취소 | 텔레그램 승인 버튼 타임아웃/청산 우선 (loop.py:764, 860) | (레인 미지정 → 레거시) | immediate | 평문 | — | approval 기능 활성 시에만 | 동일 |
| 🧹 보류됐던 청산 실행 | 장마감 미체결 flatten 이 개장 후 실행됨 (loop.py:1031) | `trades` | immediate | tgfmt | — | 드묾(pending_flatten 있을 때만) | 동일 |
| ⚠️ settings.yaml 리로드 실패 | 설정 파싱 실패, 직전 정상 설정 유지 (loop.py:2328) | `ops` | immediate | 평문 | — | 드묾 | 동일 |
| 🧭 오늘의 시장 국면 / ⚠️ 국면 판단 불가 | KST 거래일 경계 1회, regime.refresh() (loop.py:2368,2398) | (레인 미지정 → 레거시) | immediate | 평문(줄바꿈 join) | KST 거래일 1회(`regime_day`) | 1회/거래일 | 동일 |
| ⛔ 거래 자동 중단 (`_halt_notify_text`) | 연속 사이클 실패 / 브로커 대사 회로차단기 자동 halt (loop.py:2543,2578) | `ops` | immediate | tgfmt.compose, 태그 균형 테스트 있음 | — | 드묾(장애 시에만) | 동일 |
| ⚠️ settings 국면·기타 엔진 가드 실패 | 여러 방어 코드 경로(예외 삼킴 지점들) | 대부분 미지정(레거시) | immediate | 평문 | — | 드묾 | 동일 |
| 🚨 시세 끊김 — 손절이 작동하지 않는 상태 | 보유 종목 시세 unpriced 지속 (loop.py:2452) | `ops` | immediate | 평문 | 종목별 1회(`unpriced_alerted` set, 회복 시 리셋) | 조건부 | 동일 |
| 🚨 전략 오류 — 손절이 작동하지 않는 상태 | 전략 on_cycle 연속 예외 + 열린 포지션 존재 (loop.py:2565) | 미지정(레거시) | immediate | 평문 | 쿨다운(`failure_cooldown_sec`, 기본 설정값) | 조건부 | 동일 |
| 👁 워치 조건 히트 | `/watch` 등록 규칙 조건 충족 (loop.py:2615) | 미지정(레거시) | immediate | 평문 | 규칙별 쿨다운(`watch_cooldown_sec`) | 조건부 | 동일 |
| ⚠️ 시세 조회 연속 실패 | 데이터 피드 연속 stale (loop.py:2662) | `ops` | immediate | 평문 | 연속 카운터 리셋 시까지 반복 억제 없음(주의) | 조건부 | 동일 |
| 하트비트 (`_heartbeat_text`) | `engine.heartbeat_minutes` 주기, 장중만 | 미지정(레거시) | immediate | tgfmt | — | **기본 off**(`telegram_heartbeat` 설정, 소음 다이어트 2026-08-25) | 동일 |
| 포지션 현황 (`_position_report_text`) | `position_report_seconds` 주기, 보유 중일 때만, 장중만 | `trades` | immediate | tgfmt | — | 기본 off, 켜면 1분 주기 | 동일 |
| 🚨 합산 노출 경고 | 전략 간 중복 보유/상쇄 쌍 임계 초과 (loop.py:2720) | 미지정(레거시) | immediate | 평문 | 쿨다운(`exposure_alert_cooldown_sec`) | 조건부, 자동 차단 없음(가시화만) | 동일 |
| 세션 마감 요약 (`_session_summary_text`) | 시장 개장→마감 전환 시 정확히 1회 | `trades` | immediate | tgfmt | 시장별 1회/세션(`prev_market_open` 전이 감지) | KR 1회 + US 1회/일 | 동일 |

**알려진 격차**: 이 표의 "레인 미지정(레거시)" 행들은 포럼 토픽이 바인딩된
뒤에도 계속 레거시 단일 채팅(헤더만 붙어서)으로 간다 — `trades`/`ops`로
명시적으로 옮기는 것은 이 카탈로그 작업의 범위 밖(동작 변경) 이라 손대지
않았다. 다음에 레인을 손볼 때는 이 표를 근거로 시작하면 된다.

---

## 2. 브리지 명령 응답 (`server/scripts/tg_bridge.py`)

게이트(`notify.sh`)를 거치지 않는다 — 전부 **명령이 온 채팅/토픽에 즉시
동기 응답**(`TelegramBotClient.send_message(chat_id, text, message_thread_id)`,
tg_bridge.py:1351). 레인표를 보지 않는다.

| 명령 | 처리 함수 | 경로 | 형식 | 최대 길이 | 빈도 |
|---|---|---|---|---|---|
| `/balance` `/잔고` `/자산` | `handle_balance` | 로컬 상태 + Toss 시세 즉시 조회 — Claude 미경유 | 평문 | 길이 제한 없이 직접 전송(아래 격차 참고) | 온디맨드 |
| `/scoreboard` `/성적` | `handle_scoreboard` | 원장 집계 즉시 — Claude 미경유 | 평문 | 동일 | 온디맨드 |
| `/status` | `handle_query_command` 경로 밖, 제어 핸들러 | 거래 상태+현금+"오늘의 전적" | 평문 | 동일 | 온디맨드 |
| `/watchlist` `/watchlist-reset[-kr\|-us]` | `handle_watchlist_command`/`handle_watchlist_reset` | 로컬 상태 파일 | 평문 | 동일 | 온디맨드 |
| `/halt [사유]` `/rest` / `/resume` `/live` | 제어 명령 핸들러 → `TradingControl` | `data/state/control.json` 갱신 + 확인 응답 | 평문 | 동일 | 온디맨드(드묾) |
| `/flatten [all\|day]` | 동일 | 청산 요청 플래그 세팅 + 확인 응답 | 평문 | 동일 | 온디맨드(드묾) |
| `/watch <심볼...>` `/unwatch <심볼...>` | 관심종목 핸들러 | `watch-add` CLI 공유(daily_brief 자동편입과 flock 공유) | 평문 | 동일 | 온디맨드 |
| `/here [레인]` `/lanes` `/레인` | 레인 바인딩 핸들러 | `data/state/tg_lanes.json` 갱신 | 평문 | 동일 | 마이그레이션 시에만 |
| `/help` `/명령어` `/start` | `HELP_TEXT` 상수 반환 | 정적 텍스트 | 평문 | 고정 | 온디맨드 |
| 그 외 일반 문장 | Claude 세션 경유(`build_claude_prompt`) | 수십 초 소요, `truncate_reply(limit=4000)`로 소프트 캡 | 평문(LLM 출력 그대로) | **4000자** 소프트 캡 + "…(잘림)" | 온디맨드, 분당 한도(RateLimiter) 적용 — 초과 시 "잠시 후 다시 (분당 한도)" |

**알려진 격차**: `/balance`/`/scoreboard`/`/status`/`/watchlist` 류 즉시조회
응답은 `truncate_reply`를 거치지 않고 그대로 전송된다 — 보유 종목이나 전략
수가 크게 늘면 4096자 하드캡을 넘겨 텔레그램이 400으로 거부할 수 있다(브리지는
게이트의 HTML→평문 폴백도, 실패 원장도 갖고 있지 않다 — 실패하면 사용자는
그냥 응답을 못 받는다). 이 카탈로그 작성 시점엔 실측으로 넘긴 사례가 없어
"확인된 버그"는 아니지만, 포지션·전략 수가 늘어나는 방향인 이 저장소에서는
잠재적 위험으로 기록해 둔다.

---

## 3. 크론 스크립트 (`server/scripts/lib/notify.sh` 게이트)

세 함수의 뜻: `notify_now`=즉시(긴급), `notify_auto`=장중이면 큐/장외면 즉시,
`notify_defer`=항상 큐. 모든 스크립트가 `NOTIFY_LANE`을 소스 상단에 박아둔다
(레인 값은 열에 표기). 아래 표는 스크립트당 대표 메시지 종류를 담는다 — 거의
모든 스크립트가 공통으로 갖는 "호스트 TZ 가 KST 가 아님" 가드 메시지는 한 번만
각주로 남기고 표에서 반복하지 않는다.[^tz]

[^tz]: `daily_brief.sh`/`us_watch_discover.sh`/`own_brief.sh`/`manual_recs.sh`/
`session_pnl.sh`/`market_pulse.sh`/`close_report.sh`/`tg_digest.sh` 등 시각에
민감한 스크립트 다수가 시작 시 `date +%z != +0900`를 확인해 그 스크립트의 원래
문(`notify_auto`/`notify_now`/`notify_defer`)으로 경고 한 줄을 낸다 — 서버
타임존 드리프트를 사람이 우연히 발견하기 전에 잡기 위함(2026-08 결함 재발 방지).

| 스크립트 | 트리거/스케줄(KST) | 레인 | 문 | 대표 메시지 | 소프트 캡 | 중복 키 | 빈도/일 |
|---|---|---|---|---|---|---|---|
| `own_brief.sh KR` | 평일 08:12 | `briefs` | `notify_auto` | 🤖 확신도 엔진 통과 → 자동 편입 / 🛑 엔진 오류 / 🗞 리포트 없음 | 3900 | — | 1 |
| `own_brief.sh KR close` | 평일 14:52 | `briefs` | `notify_auto` | 상동(마감 편입 변형) | 3900 | — | 1 |
| `own_brief.sh US` | 평일 21:50 | `briefs` | `notify_auto` | 상동 | 3900 | — | 1 |
| `promotion_debate.sh KR/US` | 평일 08:15 / 21:53 | `briefs` | `notify_auto` | 승격 토론 판정 | — | — | 2 |
| `flow_scan.sh KR` | 09:30 + 30분마다 10:00-14:30 | `briefs` | `notify_auto` | 🌊 장중 거래대금 편입(확신도 게이트 통과분) | — | — | 조건부, 최대 ~10 |
| `flow_scan.sh US` | 23:00,23:30 + 00:00-04:30 30분마다 | `briefs` | `notify_auto` | 상동 | — | — | 조건부, 최대 ~12 |
| `tg_digest.sh KR/US` | 09:35+ / 22:35+ 30분마다 | `intel` | `notify_now` | 채널 다이제스트 + CANDS 후보 | — | — | KR ~9, US ~13 |
| `ai_trader.sh KR/US` | 08:20 / 20:10 | `briefs` | `notify_auto` | LLM 실험 레인 카드 | — | — | 2 |
| `ml_scorer.sh KR/US` | 08:22 / 20:12 | `briefs` | `notify_auto` | 스코어러 픽 | — | — | 2 |
| `market_pulse.sh KR` | 09:40, 12:00, 14:30 | `briefs` | `notify_now` | 지표 표(펄스) | — | — | 3 |
| `market_pulse.sh US` | 23:00, 01:00, 03:00, 05:00 | `briefs` | `notify_now` | 상동 | — | — | 4 |
| `manual_recs.sh KR/US` | 15:50 / 06:30 | `briefs` | `notify_auto`(장외 실질 즉시) | 오버나이트/스윙 추천 | 3900 | — | 2 |
| `report_accuracy.sh` | 화-토 06:40 | `briefs` | `notify_auto` | 리포트 정확도 채점 요약 | 3900 | — | 1 |
| `session_pnl.sh KR/US` | 15:35 / 06:10 | `trades` | `notify_defer`만 | 세션 실화폐 손익 | 3900 | — | 큐만(문서로만 노출) 2 |
| `capital_review.sh` | 평일 16:50 | `briefs` | `notify_auto` | 자본 배분 재검토 | — | — | 1 |
| `daily_feedback.sh KR/US` | 16:45 / 06:25 | `briefs` | `notify_defer` | 일일 피드백 요약 | 3900 | — | 큐만 2 |
| `pnl_attribution.sh KR/US` | 16:58 / 06:58 | `trades` | `notify_auto`(장외라 실질 즉시) | 손익 귀속 리포트 | — | — | 2 |
| `close_report.sh` | 매일 16:20 | `briefs` | `notify_defer`만 | 마감 리포트 요약 | 3900 | — | 큐만 1 |
| `scoreboard_weekly.sh` | 금 16:10 | `trades` | `notify_defer`만 | 주간+누적 스코어보드/부검/자본곡선/슬리피지 4건 | — | — | 큐만 4/주 |
| `weekly_review.sh` | 토 06:25 | `briefs` | `notify_defer`만 | 주간 재검토 | — | — | 큐만 1/주 |
| `param_propose.sh` | 토 06:40 | `briefs` | `notify_defer`만 | 파라미터 제안 | — | — | 큐만 1/주 |
| `governor.sh` | 토 06:50 | `briefs` | `notify_auto` | 거버너 결정(설정 반영) | — | — | 1/주 |
| `experiments_daily.sh` | 매일 16:30 | `briefs` | `notify_defer`만 | 자동 판정 잡 요약/실패 | — | — | 큐만 1 |
| `capital_review.sh`/`ml_scorer.sh`/`ai_trader.sh` 등 위 다수는 `|| true`로 실패를 삼키되 반환값은 로그에 남는다 — 텔레그램 발송 자체의 실패는 §4 원장을 본다. |||||||
| `backfill_1m.sh` | 평일 16:38 + 매일 05:40 | `ops` | `notify_defer`만 | 🚨 1분봉 백필 부분 실패 | — | — | 큐만, 조건부 |
| `backfill_kr_daily.sh` | 매일 16:30 | `ops` | `notify_defer`만 | 069500 일봉 백필 낡음/검증 불가 | — | — | 큐만, 조건부 |
| `backfill_kr_stock_daily.sh` | 매일 16:35 | `ops` | `notify_defer`만 | 개별종목 일봉 백필 부분 실패 | — | — | 큐만, 조건부 |
| `backfill_kr_largecap_daily.sh` | 평일 15:36 | `ops` | `notify_defer`만 | 대형주 일봉 백필 실패 | — | — | 큐만, 조건부 |
| `backfill_us_daily.sh` | 화-토 06:42 | `ops` | `notify_defer`만 | QQQ/VIX 일봉 백필 낡음/검증 불가 | — | — | 큐만, 조건부 |
| `kr_minute_backfill.sh` | 토 05:00 | `ops` | `notify_now`(가드) + `notify_defer`(결과) | 🚨 정규장 요일 트리거 감지(가드) / 결과 요약 | — | — | 1/주(정상 시 결과만 큐) |
| `macro_collect.sh` | 화-토 06:15 | `ops` | `notify_defer`만 | 🚨 FRED 수집 실패 | — | — | 큐만, 조건부 |
| `delivery_check.sh` | 화-토 06:35 | `ops` | `notify_defer`만 | 배송/전달 점검 결과 | — | — | 큐만 1 |
| `risk_review.sh` | 화-토 06:37 | `briefs` | `notify_auto`(BREACH) / `notify_defer`(정상) | 리스크 임계 초과 카드 | — | — | 조건부 |
| `ops_judge.sh kr-midday/us-midsession` | 평일 13:10 / 화-토 01:30 | `ops` | `notify_defer`만 | 판단 워치독 결과 | — | — | 큐만 2 |
| `experiments_daily.sh`/`weekly_review.sh` 등은 위에 이미 있음. |||||||
| `backup.sh` | 매일 03:30 | `ops` | `notify_now` | ⚠️ MySQL 덤프 0바이트 / 🚨 백업 실패 | — | — | 정상 시 무발송, 실패 시 즉시 |
| `backup_restore_check.sh` | 매월 첫 일 03:50 | `ops` | `notify_now` | ✅/🚨/❔ 복원 리허설 결과 | — | — | 1/월 |
| `ops_watch.sh` | 매시 05분 | `ops` | `notify_now` | 🚨/❔ 운영 감시 이상 (§4의 `notify_failure_findings` 포함) + 배포 드리프트 별도 경로 | 2500(형식화 실패 폴백) | Finding 지문 24h 억제(`dedupe_repeat_alerts`) + 배포드리프트는 SHA당 1회 | 정상 시 무발송, 초당 최대 24회 실행 |
| `watchdog.sh` | 5분마다 | `ops` | `notify_now` | 🚨 엔진 다운/행 / ⚠️ 브리지 다운 / ✅ 회복 | 상태 파일 기반(장애당 1회) | 정상 시 무발송 |
| `publish_portfolio.sh` | 평일 16:25 + 화-토 06:25 | `ops` | `notify_now` | 🚨 공개 성과 JSON 검증 실패 | — | — | 정상 시 무발송 |
| `daily_wrap.sh KR/US` | 평일 16:55 / 화-토 06:55 | `briefs` | `notify_now`(서술만) + `sendDocument`(문서, 게이트 밖) | 자연어 서술 1통 + 마감 요약 HTML 파일 | 서술은 4096 하드캡(tgfmt 아님) | 없음(1회/세션) | KR/US 각 1(서술+문서 2통) |

**빈도 합계 감**: 정상 운영일 기준 즉시/큐 합쳐 대략 KR 세션 15~25통 +
US 세션 20~30통 + 시간당 ops_watch 24회(대부분 무발송) + watchdog 288회/일
(대부분 무발송) — 실제 눈에 보이는 텔레그램 메시지는 소유자 지시(2026-08-28)
대로 "장중은 매매만" 원칙에 따라 이보다 훨씬 적다(`trades`/`briefs` 즉시분 +
장 마감 문서 1~2통).

---

## 4. 마감 문서 (deferred 큐 → `daily_wrap.sh`)

`notify_defer`로 쌓인 모든 알림은 텔레그램으로 **직접 나가지 않는다** — 유일한
소비자는 `quant.apps.cli daily-wrap`(`_wrap_deferred`/`_wrap_consume_queue`,
`quant/apps/cli.py`)이고, `server/scripts/daily_wrap.sh`가 그 출력 HTML 파일을
`sendDocument`로 전송한다(이 저장소 최초의 문서 전송 — 메시지 4096자 제한을
피하려고 장 마감 후 "하루 요약 HTML 파일 1장"으로 통합하는 것이 2026-08-28
소유자 지시의 핵심이다).

- 큐 파일: `data/notify_queue.jsonl` → 소비 후 `data/ledger/notify_queue_archive.jsonl`.
- 소비 규칙: 큐 **전체**를 읽는다(날짜로 거르지 않음 — KR 16:55/US 06:55 마감이
  겹치는 걸 막기 위해 "지난 리포트 이후 전부"가 계약). `--date` 백필은 아카이브+
  큐에서 그 날짜분만 읽고 소비하지 않는다.
- **크래시 안전성(2026-09-06 신규, 이 작업)**: 이전엔 같은 fd 를
  `seek(0)+write+truncate`로 제자리에서 고쳐 썼다 — `write()` 도중 프로세스가
  죽으면 파일이 반토막나 줄이 깨지거나 유실될 수 있었다. 이제 새 내용을 임시
  파일에 통째로 쓰고 `os.replace()`로 원자적으로 바꿔친다(POSIX rename 원자성 —
  중간 상태가 관측되지 않는다). 락도 데이터 파일 자신이 아니라 전용
  `notify_queue.jsonl.lock` 파일에 건다 — 데이터 파일을 치환하면서 그 파일 자신에
  락을 걸면 치환 직전 이미 그 이름을 열어 대기 중이던 appender가 사라질 옛
  inode에 쓰게 되기 때문이다(`server/scripts/lib/notify.sh`의
  `_notify_enqueue`가 같은 `.lock` 파일을 잠근다 — 짝을 반드시 맞춰야 한다).
  `quant/apps/cli.py::_wrap_consume_queue`, `tests/test_daily_wrap.py`의
  `test_crash_during_replace_leaves_original_queue_intact` 참고.

---

## 5. 발송 실패 처리 (2026-09-06 신규)

### 5.1 공유 실패 원장

셸 게이트와 엔진 노티파이어가 **같은 파일·같은 스키마**를 쓴다 —
`data/ledger/notify_failures.jsonl`, 각 줄 `{ts, source, lane, text, [error]}`.

- 셸: `server/scripts/lib/notify.sh`의 `_notify_record_failure()` — `_notify_send()`가
  HTML 시도와 평문 재시도 **둘 다** 실패했을 때만 한 줄 남긴다. `source`는
  호출한 크론 스크립트의 basename(`$0`), 실패 원인 자체는 셸에서 응답 바디를
  분류하지 않으므로 담지 않는다(응답 원문은 크론 자체 로그에 있다).
- 엔진: `quant/adapters/notify/telegram.py`의 `TelegramNotifier._record_failure()`
  — `send()`의 except 블록에서 `_record()`(전체 발송 원장)와 함께 호출된다.
  `source="engine"`, `error`에 예외 타입+메시지를 남긴다.
- 브리지는 이 원장에 쓰지 않는다(§2 "알려진 격차").

### 5.2 감지 → 경보

`quant.control.health.notify_failure_findings(today_failure_count, threshold=3)`
(순수 함수, `tests/test_health.py`)가 오늘(KST) 치 행 수를 판정한다 —
`quant.apps.cli cmd_health`가 `data/ledger/notify_failures.jsonl`을 읽어 오늘
날짜로 시작하는 줄만 세어 넘긴다. 3건 초과면 `alert` Finding 하나가 생기고,
`server/scripts/ops_watch.sh`(매시 05분, 이미 `cli health`를 호출하는 기존
파이프라인)가 그대로 집어 경보로 낸다 — **ops_watch.sh 자체는 한 줄도 바뀌지
않았다**, 감지는 전부 `quant.control.health`의 결정론적 순수 함수라는 이 저장소
불변식을 그대로 지킨 것이다.

### 5.3 레인별 레이트 리밋

텔레그램 실측 한도(~20건/분/챗)에 안전마진을 두고, **같은 레인**으로 최근
60초 안에 15건을 넘겨 보내면 짧게 쉰다(막지 않고 늦출 뿐 — 발송 자체는 결국
전부 나간다):

- 셸: `_notify_rate_limit()`(`notify.sh`) — 레인별 상태 파일
  `data/state/tg_rate_<레인>.log`에 최근 전송 시각(epoch)을 쌓고, 60초 밖은
  버린다. 15건 이상이면 `sleep "${NOTIFY_RATE_SLEEP:-4}"`.
- 엔진: **레이트 리밋 없음(의도)**. 워커가 넣었던 `TelegramNotifier._throttle()`(15건 초과 시 `time.sleep(4)`)은 2026-09-07 통합 검토에서 제거했다 — 알림기는 거래 루프(10초 주기) 안에서 동기 호출되므로 sleep 한 번이 곧 루프 정지다(개장 직후 체결 폭주 = 가장 sleep 하면 안 되는 순간). 텔레그램이 429를 돌려주면 그 실패가 `notify_failures.jsonl`에 남고 `cli health`가 집계한다 — 늦추는 대신 드러낸다.

---

## 6. 킬스위치 드릴 절차 (`server/scripts/halt_drill.sh`)

**목적**: "/halt·/resume 버튼이 있다"와 "버튼을 눌렀을 때 실제로 매매가
멈춘다"는 다른 주장이다. 이 드릴은 텔레그램 앱 → `tg-bridge` 프로세스 →
`data/state/control.json` 쓰기 → 엔진이 다음 사이클에 그 파일을 읽는 경로
**전체**가 실제로 동작하는지 사람이 눈으로 확인하는 것을 자동화한다.

**중요: 이 스크립트는 `/halt`·`/resume`을 절대 스스로 보내지 않는다.**
스크립트가 파일을 직접 써서 흉내 내면 "브리지가 실제로 명령을 받는가"라는
경로의 절반을 건너뛰고 확인했다는 착각만 남기 때문이다 — 오직 `control.json`을
읽고 기다릴 뿐이다.

**절차**:

```bash
./server/scripts/halt_drill.sh
```

1. 시작 전 `control.json` 상태(halted/reason/by)를 보여준다. 이미 halted라면
   먼저 `/resume`으로 정상화한 뒤 다시 실행하라는 경고가 뜬다.
2. "지금 `/halt 드릴테스트`를 보내세요" 안내 — 소유자가 텔레그램에서
   (레인을 묶어뒀다면 🎛 제어실에서, 아니면 브리지와의 1:1 채팅에서) 직접
   보낸다. 스크립트는 최대 `HALT_DRILL_TIMEOUT_SECONDS`(기본 300초) 동안
   `HALT_DRILL_POLL_SECONDS`(기본 2초)마다 `control.json`을 다시 읽는다.
   `halted:true`가 보이면 걸린 시간·사유·주체(manual/auto)를 찍는다.
3. "이어서 `/resume`을 보내세요" 안내 — 같은 방식으로 `halted:false` 복귀를
   기다린다.
4. 두 구간 모두 반영되면 `exit 0`("통과"). 1구간에서 타임아웃이면 `exit 1`
   (브리지가 명령을 못 받았거나 죽은 것으로 의심 — `systemctl status
   tg-bridge` 확인), 2구간에서 타임아웃이면 `exit 2`(halt는 반영됐지만 resume
   경로만 막힌 특이 케이스).

**언제 돌리나**: 배포 직후 1회, 그리고 `tg-bridge.service`/`quant-engine.service`
유닛 정의나 `data/state/control.json` 스키마를 건드린 뒤. 크론에 등록하지
않는다 — 사람이 실제로 텔레그램을 조작해야 하는 절차라 자동화 대상이 아니다.

---

## 7. 계약 테스트 (`tests/test_telegram_contracts.py`)

이 카탈로그가 문서로 약속한 것(레인 배정, 형식, 길이, 이스케이프, 숫자 서식,
한국어 가독성)을 코드로 고정한다 — 실제 실행/렌더 가능한 함수만 대상이며,
목록과 실행 방법은 그 파일 상단 docstring에 있다.
