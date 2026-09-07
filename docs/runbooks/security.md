# Runbook: 보안·설정·캘린더 견고성

2026-09-07 전사 검증 세션(오너 지시: SECURITY/CONFIG/SECRETS/CALENDAR/ROBUSTNESS
sweep)의 산출물. 시크릿 로테이션 절차 자체는 [`secrets-rotation.md`](secrets-rotation.md)를
따른다 — 이 문서는 **무엇을 확인했고, 무엇을 고쳤고, 무엇이 아직 구멍인지**를 담는다.

## 1. 시크릿 — 로그·백업 유출 경로

**확인됨(안전):**
- EC2 `.env.local` 권한은 `600`(rw-------)이다.
- `quant/control/backup.py`의 `ARTIFACTS = ("state", "ledger", "news")`는
  `data/` 아래로 스코프가 좁혀져 있어 저장소 루트의 `.env.local`/`*.pem`이
  구조적으로 백업 대상이 될 수 없고, `_SECRET_NAMES` 이름 차단목록(`.env`,
  `.pem`, `.key`, `id_rsa`, `id_ed25519`, `credentials`)이 이중 방어로 걸려 있다.
- `data/public/`(공개 리포트 사이트) grep 결과 토큰/키 패턴 없음.
- `journalctl -u quant-engine -u tg-bridge`(최근 14일) grep 결과 텔레그램 봇
  토큰(`/bot<숫자>:<문자열>` 형태)·`Bearer` 헤더 패턴 유출 없음.
- `quant/core/log_redact.py`(값 기반 + 형태 기반 2단 마스킹)가
  `quant/apps/cli.py`의 모든 진입점에 이미 배선돼 있다.

**고쳤음:**
- `server/scripts/tg_bridge.py`는 `.env.local`을 자체 파서(`read_env_file`)로
  읽어 `os.environ`을 채우지 않으므로 `log_redact.known_secrets()`(os.environ
  스캔)로는 `TELEGRAM_BRIDGE_BOT_TOKEN`/`TOSS_CLIENT_SECRET`을 못 잡았다. 지금까지는
  이 프로세스가 `logging.basicConfig()`를 부른 적이 없어 httpx의 INFO 요청 로그
  (`GET .../bot<TOKEN>/getUpdates`)가 어디에도 안 찍혀 우연히 안전했을 뿐이다 —
  누군가 나중에 `HTTPX_LOG_LEVEL=debug`를 걸거나 디버깅용으로 로깅을 켜는 순간
  `quant/core/log_redact.py` 모듈독스트링이 기록한 바로 그 유출(journalctl에
  봇 토큰 평문 381회)이 이 프로세스에서도 재현된다. `if __name__ == "__main__":`
  진입점에 `logging.basicConfig(level=logging.WARNING)`(로그 볼륨은 그대로) +
  `log_redact.install(extra_secrets=...)`을 추가해, 나중에 로그 레벨이 낮아져도
  토큰은 항상 가려지게 했다.

**남은 구멍(다른 담당 영역 — 보고만):**
- `server/scripts/lib/notify.sh`의 `_notify_send`는 크론 알림을 `parse_mode=HTML`로
  보낸다(엔진 알림기 `quant/adapters/notify/telegram.py`와 동일 관례, L1 서식
  지원 목적). `daily_brief.sh`/`own_brief.sh`처럼 **외부 소스(회사 시장 리포트,
  텔레그램 채널 포워딩)를 요약한 텍스트**를 그대로 이 경로로 보내는 호출부가
  있는데, 그 요약 텍스트에 완결된 HTML 태그(예: `<a href="...">`)가 섞여 있으면
  텔레그램이 이를 파싱해 오너의 텔레그램에 클릭 가능한 링크로 렌더링한다 —
  잘못된(닫히지 않은) 태그는 이미 평문 폴백으로 안전하지만, **문법적으로
  올바른** 악성 태그는 폴백을 타지 않는다. 이 크론 스크립트들은 `notify.sh`
  소유가 아니라 리포트/브리핑 담당 영역이므로, 외부 유래 텍스트를 이 경로로
  보내기 전에 `<`/`>`/`&`를 이스케이프하거나 `parse_mode`를 생략하는 처리가
  그쪽에서 필요하다.

## 2. 텔레그램 브리지 인증 모델 (`server/scripts/tg_bridge.py`)

`is_allowed_chat`이 판정하는 세 갈래(레거시 1:1 chat_id / `/here`로 바인딩된
슈퍼그룹 chat_id / 오너 본인의 `from.id`)와 그 근거는 함수 docstring에 있다.
2026-09-07 감사에서 추가로 확인·고정한 것:

- **채널 게시물(`channel_post`)과 수정된 메시지(`edited_message`)는 `message`
  키가 없어 자동으로 거부된다** — 별도 방어 코드가 필요했던 게 아니라 기존
  구조가 이미 안전했다. `tests/test_tg_bridge_auth_edgecases.py`가 회귀를 막는다.
- **바인딩된 슈퍼그룹 안의 비오너 발신자는 실제로 `/halt` 같은 제어 명령을
  실행할 수 있다** — 이는 버그가 아니라 오너가 명시적으로 선택한 완화다
  (그룹에 오너 자신만/신뢰하는 사람만 있는 한 위험이 낮다는 전제). 이 사실이
  게이트 함수뿐 아니라 명령 처리 경로까지 실제로 관철되는지
  `test_halt_from_non_owner_member_of_bound_group_actually_halts`로 고정했다 —
  **그 그룹의 멤버 구성(관리자·봇 포함 여부)은 오너가 책임진다.**
- 포워드된 채널 게시물(`forward_origin`/`forward_from_chat`)의 진위는 텔레그램
  서버가 보증한다 — 클라이언트가 그 메타데이터를 조작해 등록되지 않은 채널을
  등록된 것처럼 속일 수 없다(채널 username은 텔레그램 전역에서 유일).
- `tg_bridge.TelegramClient.send_message`는 `parse_mode`를 지정하지 않는다
  (평문) — 위 §1의 HTML 인젝션 우려가 이 파일 자체의 응답 경로에는 해당하지
  않는다.

## 3. 설정 핫 리로드 — 문법은 유효하지만 의미가 깨진 파일

`quant/apps/config.py`의 `reload_if_changed()`는 2026-09-06 감사로 YAML **문법**
오류(`yaml.YAMLError`)는 이미 막아냈지만, **문법적으로 유효한 의미 오류**는
그대로 반영해버렸다 — 예: `capital_fraction: -1`(음수 자본 배분),
`poll_seconds: "다섯"`(숫자 자리에 문자열), `per_strategy_initial_krw: "많이"`.

`quant.apps.config._validate_semantics()`(2026-09-07 신규)가 이 필드들
(`capital_fraction`, `per_strategy_initial_krw`/`usd`, `poll_seconds`, 및
`strategies:`가 있는데 dict가 아닌 경우)을 검사한다. 실패 시:
- **핫 리로드(`reload_if_changed`)**: 마지막으로 성공한 설정을 유지, ERROR 로그
  + `on_error` 콜백 1회(엔진은 죽지 않는다 — YAML 문법 오류와 같은 계약).
- **부팅(`load_settings`)**: 기동 자체를 거부(raise) — 유지할 "마지막 성공한
  설정"이 없으므로 깨진 설정으로 뜨는 것보다 안전하다.

**의도적으로 빠진 것**: `strategies:` 키 자체의 부재, `governor.
protected_strategies`가 `strategies:`에 없는 id를 가리키는지. 둘 다 처음
넣었다가 뺐다 — `Settings.strategies`가 이미 `.get(key, {})`로 부재를 안전하게
흡수하고(전략 0개는 위험한 방향이 아니라 매매를 안 하는 안전한 방향이다),
`tests/test_backtest_intrabar.py`처럼 `config/settings.yaml`을 복사해
`strategies`만 단일 프로브로 갈아끼우는 정당한 테스트 패턴이 이 검사에
오탐으로 걸려 도입 직후 12개 테스트가 회귀했다(2026-09-07 실측). 커밋된
`config/settings.yaml`에 대해서는 `tests/test_governor_wiring.py::
test_protected_strategies_are_known_settings_yaml_strategies`(정적 검사)가
`protected_strategies` 참조 무결성을 별도로 지킨다.

`tests/test_settings_semantic_validation.py`가 양쪽 경계와 위 "의도적으로 빠진
것"을 모두 고정한다.

## 4. 캘린더/타임존 — 알려진 구멍과 다음 90일 오작동 날짜

### 4.1 라이브 세션 판정 (`quant/core/session.py`)

- **정상 경로**: `TossSessionCalendar`가 Toss `market-calendar` API를 매일
  조회해 그날의 실제 정규장 시각(휴장/조기폐장 포함)을 쓴다 — 이게 정답이다.
- **폴백(`StaticSessionCalendar`)**: 그 조회가 실패했을 때만 쓰인다. **2026-09-07
  이전에는 주말만 걸렀다** — 즉 Toss API가 마침 추석 당일에 장애를 내면
  엔진이 "장이 열려 있다"고 오판해 존재하지 않는 정규장에 주문을 낼 수 있었다.
  이제 `_STATIC_HOLIDAYS`/`_STATIC_EARLY_CLOSES` 정적 표를 추가해 최소한
  2026년 하반기 알려진 휴장일·조기폐장일은 폴백에서도 걸러진다
  (`tests/test_session.py`의 "StaticSessionCalendar 휴장일표" 절).
  **이 표는 유지보수 대상이다** — 대체휴일·임시공휴일·연도가 바뀌면 갱신
  해야 하고, 갱신을 놓치면 다시 조용히 틀린다. 정상 경로(Toss API)가 살아
  있는 한 이 표는 참조되지 않는다.
- `WallClock`/`SimClock`은 `zoneinfo`(tz 데이터베이스)로 KR/US 로컬 시각을
  계산하므로 서머타임(EDT/EST) 자체는 여기서 올바르게 처리된다 — 구멍은
  "그 시각 판정에 쓰는 캘린더가 휴장일을 아는가"에 있었다(위 내용).

### 4.2 크론(KST 고정 시각) — DST 전환 시 어긋나는 것 (다른 담당 영역, 보고만)

미국 서머타임(EDT)은 **2026-11-01(일) 종료** → 그 이후 미국 정규장은 KST로
1시간 밀린다(09:30 ET 개장이 22:30 KST에서 23:30 KST로, 16:00 ET 마감이 05:00
KST에서 06:00 KST로). `server/crontab.txt`의 대다수 US 관련 항목은 이미 양쪽
서머타임 상태를 다 덮는 넉넉한 창으로 설계돼 있다(주석에 "서머타임 무관"이라고
명시된 것들 — 예: `session_pnl.sh US`는 06:10, `market_pulse.sh`류는 22:xx~04:xx
30분 간격). 그러나 다음 하나는 **EDT 마감(05:00 KST) 기준으로만 여유를 뒀다**:

| 크론 | 현재 스케줄 | 문제 |
|---|---|---|
| `report_cli uswrap` | `50 5 * * 2-6` (05:50 KST, 화~토) | EST(11-01 이후)에는 미국 정규장이 **06:00 KST**에 닫힌다 — 05:50은 마감 10분 **전**이라, 장이 아직 열려 있는데 "마감 종합"을 만든다. 그 산출물(`US_wrap.json`)을 다음날 KR 아침 리포트(07:30 빌드)가 그대로 읽으므로, 그날 미국장 카드가 마감 전 스냅샷으로 굳는다. |

**첫 오작동 예상일**: 2026-11-03(화) 05:50 KST(11-02 월요일 미국 세션이 첫
EST 마감). 이후 DST가 다시 시작되는 2027년 3월 둘째 일요일 전까지 매 화~토
아침 반복된다.

**권장 조치(택1, `server/crontab.txt` 담당 영역)**:
1. 가장 간단: `uswrap` 스케줄을 `50 5,6 * * 2-6`처럼 05:50과 06:50 두 번 돌리고
   `uswrap` 자체가 멱등이면(이미 결정론 재계산이라 멱등으로 보인다) 늦은 실행이
   최신값으로 덮어쓰게 둔다.
2. 더 정확함: 크론을 `06:10`으로 옮겨 항상 마감 후이게 만든다(단, 그러면
   `macro_collect.sh`(06:15)와의 "05:50 발행 뒤" 순서 가정이 깨지므로 그쪽도
   같이 밀어야 한다 — 이 파일이 담당 영역이 아니므로 조율 필요).
3. 근본적: `quant.core.clock.WallClock.is_market_open("US")`을 먼저 확인하고
   아직 열려 있으면 대기/재시도하는 얇은 래퍼로 크론 스크립트를 감싼다 — 이
   방식은 이후 어떤 시각에 크론을 걸어도 DST에 흔들리지 않는다.

### 4.3 다음 90일(2026-09-07~2026-12-06) 휴장일 체크리스트

| 날짜 | 시장 | 종류 | Toss API 장애 시 폴백 상태(2026-09-07 수정 후) |
|---|---|---|---|
| 2026-09-24/25 | KR | 추석 연휴 | 커버됨(`_STATIC_HOLIDAYS`) |
| 2026-10-03 | KR | 개천절(토요일과 겹침 — 요일상 원래도 휴장) | 커버됨(중복 방어) |
| 2026-10-09 | KR | 한글날 | 커버됨 |
| 2026-11-01 | US | DST 종료(휴장일 아님, §4.2 참고) | 해당 없음 — 크론 문제 |
| 2026-11-26 | US | Thanksgiving | 커버됨 |
| 2026-11-27 | US | 조기폐장(13:00 ET) | 커버됨(`_STATIC_EARLY_CLOSES`) |
| 2026-12-25 | KR/US | 크리스마스(90일 범위 밖이지만 표에 선반영) | 커버됨 |

## 5. 참고 — 이번 세션에서 발견했지만 다른 담당 영역인 것

- **디스크 증가**: EC2 `data/ledger/smart_flow.jsonl`이 2026-08-31 생성 이후
  8일 만에 295MB(~37MB/일)로 자라는데, 주간 정리 크론(`find data -name "*.log"
  -mtime +7 -delete`)은 `.jsonl`을 건드리지 않는다 — **무제한 증가**. 현재 EC2
  가용 디스크는 7.6GB뿐이라 이 파일 하나만으로도 90일 내 +3GB 안팎이 더해진다.
  담당: 이 파일을 쓰는 `quant/adapters/smart_flow_log.py`/크론 보유 영역.
- **의존성**: `uv pip list --outdated` 기준 치명적 CVE 신호는 없음(전부
  patch/minor 지연). `websockets`(16.1.1→17.1)·`yfinance`(1.5.2→1.7.0)는
  메이저/여러 마이너 뒤처져 있어 다음 정기 업그레이드 때 회귀 테스트와 함께
  검토 권장. `pip-audit`은 이 환경에 `pip` 자체가 없어 실행 불가 — 네트워크
  있는 환경에서 별도 실행 필요.
