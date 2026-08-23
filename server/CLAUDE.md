# CLAUDE.md — server/

## 여기 있는 것

EC2 상시 배포 관련 스크립트/설정 (Python 소스 아님):

- `README.md` — 전체 배포 절차 (콘솔 셋업 → 최초 셋업 → 시크릿 전달 → 이후 배포).
- `scripts/setup_ec2.sh` — 서버 최초 셋업 (idempotent).
- `scripts/deploy.sh` — 로컬에서 `git push` + 서버 `git pull && uv sync && systemctl
restart`. 사용법: `QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh`.
- `scripts/tg_bridge.py` — Telegram ↔ Claude 양방향 브리지 + 제어/관심종목 명령
  (`/halt` `/resume` `/flatten` `/status` `/watch` `/unwatch` `/watchlist`
  `/watchlist-reset`) + 즉시 조회 (`/balance`(/잔고,/자산) `/scoreboard`(/성적)
  `/help`(/명령어) — Claude 미경유, 로컬 상태+Toss 시세로 바로 응답). `/status`는
  "오늘의 전적"(금일 청산 실현 손익 + 보유 평가 손익 + 합계, 원장+시세 기반)을
  함께 보여준다. `watch-add` CLI 서브커맨드는 daily_brief의 자동 등록 경로가
  재사용한다(flock 공유). 별도 systemd 유닛으로 엔진과 격리.
- `scripts/own_brief.sh {KR|US}` — **현행 자동 편입 경로** (KR 08:12 / US 21:50).
  같은 박스의 자체 리포트(`~/market_report/out/YYYY/MM/DD/{KR,US}_engine.json`)를
  읽어 텔레그램 브리핑 + AUTO_WATCH → `watch-score` → 자동 등록. **LLM을 쓰지
  않는다** — 리포트가 후보를 이미 결정론적으로 계산해 내므로 해석할 산문이 없다.
  리포트가 없거나 낡으면 랭킹 발굴(`--discover-*`)만으로 계속 돌고 그 사실을 알린다.
  리포트 어휘(NEWS/RANK)를 엔진 어휘(EVENT/TREND)로 번역하는 지점이
  `quant/analyze/market_brief.py`다 — 번역이 끊기면 news_momentum이 리포트
  종목을 조용히 못 잡는다.
- `scripts/brief_from_report.py` — 위 스크립트가 쓰는 얇은 CLI(파일 I/O + 종료코드
  3=리포트 없음 / 4=낡음 / 5=파손).
- `scripts/session_pnl.sh {KR|US}` — 정규장 마감 후 그 세션의 **실화폐** 손익
  (KR 15:35 / US 06:10 화-토). 통화를 섞지 않는다.
- `scripts/daily_brief.sh` — **크론에서 제거됨(2026-08-13)**, 폴백용으로 보존.
  회사 리포트(13.209.240.206) → Claude 세션(도구 차단) → 브리핑 + 자동 등록.
  회사 리포트로 되돌려야 할 때만 쓴다.
- `scripts/us_watch_discover.sh` — **크론에서 제거됨(2026-08-13)**, 폴백용으로 보존.
  리포트 없이 Toss 랭킹 발굴만으로 US를 편입한다. (파일 상단 주석이 "자정 롤에서
  흡수된다"고 말하지만 지금은 22:10 경계가 있다 — `_universe_roll_bucket` 참고.)
- `scripts/backfill_us_daily.sh` — 화-토 07:00 KST QQQ 일봉 백필 + **커버리지 되짚기**.
  `data/history/QQQ/1d/` 는 백테스트용이 아니다 — `quant/trade/regime/provider.py` 가
  직접 읽어 라이브 US 국면(사이징 배수)을 계산한다. 2026-08-13 까지 이 스케줄이
  **아예 없어서** 마지막 봉이 07-31 에 멈춘 채 돌았다. 소스는 **yfinance** 다
  (alpaca 무료 구독은 최근 구간을 403 으로 막으면서 `rc=0` 을 낸다 — 실측으로
  겹치는 22봉 종가 괴리 0.0bp 확인 후 교체). fetch 성공을 신뢰하지 않고
  `olap.coverage()` 로 되짚어 여전히 낡으면 알린다.
- `scripts/backup.sh` — 매일 03:30 KST 아티팩트(`data/{state,ledger,news}`) + MySQL
  덤프를 번들로 (`data/backups/quant-*.tar.gz`, 14개 보관). **번들을 만들 뿐 전송하지
  않는다** — 전송은 받는 쪽이 당긴다(아래). EC2 가 털렸을 때 백업까지 지울 수 있는
  키를 같은 박스에 두지 않으려는 것이다. 번들 안 매니페스트 대조·회귀(줄어듦) 검사는
  `quant.apps.cli backup` 이 하고, 문제가 있으면 **종료코드 1**.
- `scripts/backup_pull.sh` — **Mac 에서 실행.** Tailscale 로 번들을 당겨오고 받은
  것을 전부 대조한 뒤, 성공 시각을 로컬과 **EC2 양쪽**에 `LAST_PULL` 로 남긴다
  (EC2 쪽에 없으면 감시가 "오프사이트 사본이 있나"를 영원히 모른다).
  S3 는 IAM 역할이 생기면 여기(또는 제3의 러너)에 붙인다.
- `scripts/backup_restore_check.sh` — 복원 리허설. **안 해본 백업은 백업이 아니다.**
  풀어서 jsonl 을 실제로 파싱하고, MySQL 덤프를 스크래치 DB 에 적재해 행 수를 라이브와
  대조한 뒤 지운다. 종료코드 **0=전부 확인 / 1=문제 / 2=부분 통과**(확인 못 한 항목이
  있으면 "리허설했다"고 기록하지 않는다).
- `scripts/ops_watch.sh` — 매시 05분 운영 감시(Phase 5.2). `watchdog.sh` 가 **엔진의
  침묵**(다운/행)을 본다면 이쪽은 **엔진이 잘 도는데 틀린 것**을 본다: 낡은 봉·죽은
  피드·원장 불일치·로그에 남은 시크릿·설치본 드리프트·백업 부재. 감지는 전부
  `quant.control.health` 의 결정론적 순수 함수(`cli health`, 종료코드 0/1/2)고,
  **LLM 은 그 JSON 을 한국어로 바꾸는 데만 쓰인다** — 서술기가 죽어도 경보는 나간다.
  **서술기 선택은 셸에 없다** — `quant/adapters/narrate.py: make_narrator()` 한 곳이
  `OPS_NARRATOR`(`claude` 기본 / `openrouter` / `none`)를 읽고, 셸은 `cli narrate` 의
  종료코드만 본다(스위치를 양쪽에 두면 갈라진다). Claude 는 **도구를 전면 차단**해
  부르고(텍스트→텍스트뿐이라 주문 경로에 닿을 방법이 없다), OpenRouter 는 무료 레인
  기본값(`nvidia/nemotron-3-ultra-550b-a55b:free`)을 쓴다. 정상이면 아무 말도 하지
  않고, 같은 발견 집합은 한 번만 알린다.
- `scripts/watchdog.sh` — 5분마다 데드맨 스위치 (서비스 다운/행 감지, 장애당 1회 알림).
- `scripts/scoreboard_weekly.sh` — 금 16:10 KST 주간+누적 전략 성적표 텔레그램.
- `scripts/kiwoom_ws_check.sh` — 키움 웹소켓 수동 재검증 도구 (크론에서는 제거됨 —
  2026-08-10 검증 통과로 `kiwoom.realtime.enabled: true`).
- `systemd/quant-engine.service`, `systemd/tg-bridge.service` — 데몬 유닛 정의.
- `crontab.txt` — banker 리포트(07:00) · 관심종목 초기화(KR 08:05 / US 21:40) ·
  자동 편입(KR 08:12 / US 21:50, `own_brief.sh`) · 세션 손익(KR 15:35 / US 06:10
  화-토) · scoreboard(금 16:10) · watchdog(5분) · 주간 로그/캐시 정리(일 03:00).
  전부 KST. 리포트 생성(`market-report@.timer`, KR 07:50 / US 19:50 기상 → 08:00 /
  20:00 발행)과 뉴스 누적 수집(`news-collect@.timer`, 30분마다)도 이제 여기 있다 —
  2026-08-13 `market_report` 저장소를 흡수하면서 유닛과 경로를 함께 옮겼다.
  **배포 시 주의:** EC2 에는 아직 `~/market_report` 체크아웃이 남아 있고 기존 유닛이
  그쪽을 가리킨다. 새 유닛으로 갈아끼우기 전에 옛 타이머를 먼저 멈춰야 두 벌이
  동시에 도는 사고를 피한다(`docs/runbooks/deploy.md`).

## 로컬 불변식

- **systemd/cron은 `.venv/bin/python`을 직접 호출한다 — `uv run`이 아니다.** 데몬/크론
  환경은 로그인 셸이 아니라 PATH가 비어 있어 `uv`가 조용히 실패하는 사례가 있었다
  (전작 교훈). 새 유닛/크론 항목을 추가할 때도 이 패턴을 따른다.
- **`TZ=Asia/Seoul`을 모든 곳에 명시한다** — systemd 유닛의 `Environment=TZ=...`,
  서버 타임존, crontab 시각 기준까지 전부 KST로 통일. US 세션(장 마감/개장) 관련
  크론을 추가할 때는 서머타임(EDT/EST) 전환을 반드시 감안한다.
- **SIGTERM(exit 143)은 정상 종료로 취급한다** (`SuccessExitStatus=143`) — 재배포
  때마다 재시작이 'Failed'로 오경보되는 것을 방지.
- **`tg-bridge`는 `MemoryMax=400M`, `CPUQuota=50%`로 격리한다** — 브리지가 Claude
  서브프로세스를 띄우다 폭주해도 `quant-engine.service`(실거래 엔진)의 자원을
  빼앗지 않도록. 엔진이 항상 우선이다.
- **Toss Open API는 등록된 IP만 허용한다** (403 otherwise) — EC2 Elastic IP를
  Toss 허용목록에 등록하지 않으면 엔진은 뜨지만 주문/조회가 전부 실패한다.
- `.env.local`은 git에 올라가지 않는다 — 서버 전달은 `scp`로만 한다
  (`server/README.md` §3).

## 배포/장애 절차

체크리스트 형태의 실행 절차는 `docs/runbooks/`에 있다:
[`deploy.md`](../docs/runbooks/deploy.md),
[`incident-engine-down.md`](../docs/runbooks/incident-engine-down.md),
[`secrets-rotation.md`](../docs/runbooks/secrets-rotation.md).
