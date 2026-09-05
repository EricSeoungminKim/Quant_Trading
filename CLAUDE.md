# CLAUDE.md

이 저장소에서 작업하는 모든 에이전트(Claude Code 등)의 단일 진입점. 디렉토리별
세부 규칙은 하위 `CLAUDE.md`를 따르되, 충돌 시 더 구체적인(더 깊은 경로의) 파일이
우선한다.

## 이 시스템은 무엇인가

KR+US 정규장에서 11개 전략을 동시 운용하는 개인 자동매매 엔진이다 (2026-09-03
기준 EC2에서 paper 가동 중). 가동 목록의 진실은 config/settings.yaml `strategies:`
의 `enabled` 이다 — 이 문단은 그 요약일 뿐이다:

- **news_momentum** — 뉴스 EVENT 태그 종목 개장매수 후 사다리 청산, 롱 온리 (KR 0.1 / US 0.0)
- **news_scalp** — EVENT_SCALP 태그 종목 개장 즉시 1분봉 진입 (KR 0.04 / US 0.0) —
  news_momentum과 "같은 유니버스·다른 진입 시점"을 재는 A/B 짝
- **pullback_impulse** / **pullback_impulse_cat** — 5분봉 눌림목 임펄스 스캘프 (US 0.03 / 0.075,
  2026-09-05 base 하한 축소 — 36트립 −20.8bp)
- **mr_vwap_quiet** — 저거래량 종목 VWAP 평균회귀 스캘핑 (US 0.06)
- **vol_breakout** / **vol_breakout_cat** — 전일 레인지 기반 변동성 돌파(Larry Williams),
  마감 직전 청산 (각 KR 0.07 / US 0.05)
- ~~intraday_momentum~~ — 5분봉 일중 모멘텀. **2026-09-05 비활성**(원장 9트립 0승 −65bp,
  같은 계열 10년 walk-forward 전부 음수 — 변경기록 참고)
- **gap_fade** — 갭하락 되돌림 매수, 롱 온리 (US 0.03, 2026-09-05 하한 축소 — 9트립 −35bp)
- **scalp_1m** / **scalp_1m_cat** — 1분봉 조기 진입 스캘프 (각 KR 0.03 / US 0.03, 2026-09-05
  하한으로 축소 + KR 은 패턴B만 — 원장 149트립 −48.4bp)
- **llm_trader** — LLM이 직접 판단하는 실험 레인 (KR 0.08 / US 0.0)

`<id>_cat`(2026-09-03)은 파라미터가 아니라 유니버스(`universe_filter`)만 다른
A/B 실험 갈래다 — 뉴스·수급 촉매 태그가 붙은 종목만 보는 쪽인지를 잰다
(`quant/trade/strategy/CLAUDE.md`의 A/B 예외 참고, `run scoreboard --ab`로 판정).
frgn_accumulate/close_bet/overnight_drift/rsi2_dip은 같은 날 "자동매매는
단타·스캘핑만" 결정으로 비활성화됐다(오버나이트 아이디어는 manual_recs 레인으로
텔레그램 추천). donchian/orb/orb_scan/intraday_scan, 문헌 기반 신규 3종
(orb_rvol/eod_reversal/open_reversal)을 포함한 나머지는 `enabled: false`(코드·
원장은 남아 있다 — 측정 기준점 + 추후 복원용).

유니버스는 텔레그램 `/watch` + **자체 리포트**(2026-08-13 이 저장소로 흡수, 같은
EC2에서 KR 08:00 / US 20:00 발행)의 자동 후보로 채워진다 — `own_brief.sh {KR|US}`
(KR 08:12 / US 21:50)가 리포트의 엔진 JSON을 읽어 확신도 엔진 `watch-score`에
태우고 임계 통과분만 자동 등록한다. **이 경로에 LLM은 없다** (2026-08-13: 회사
리포트 의존을 끊으면서 산문 해석이 불필요해졌다). 유니버스는
KST 자정 + 08:27(KR 동시호가 전, 2026-08-17 전진) + 22:10(US 개장 전)에 리로드된다. 국면(regime)은 시장별로 분리돼 있다(US=QQQ,
KR=KODEX200+투자자 수급) — 방어 0.5x/중립 1.0x/공격 1.5x가 심볼 시장별로 사이징에
곱해진다. 시세는 키움 웹소켓(실시간) 우선, Toss REST 폴백. 주문 집행은 Toss 단일.
모든 체결은 거래 원장(`data/state/trades.jsonl`)에 영속화돼 전략별 승률·payoff
스코어보드(`run scoreboard`, 주간 크론)로 집계된다 — **숫자가 자본 배분을 결정한다**.

여기에 Toss 실계좌를 읽기 전용으로 진단하는 "Private Banker" 레이어가 얹혀 일일
리스크 리포트를 Telegram으로 보낸다. **실제 돈이 걸려 있다** — 이 저장소의 실수는
금전적 손실로 직결된다.

## 아키텍처 불변식 (Enforceable Invariants)

코드는 **4개 평면**으로 나뉜다. 기능이 아니라 *틀렸을 때 잃는 것*으로 나눈 것이다.

| 평면 | 디렉토리 | 틀리면 | 허용 |
|---|---|---|---|
| 수집 | `quant/collect/` | 데이터가 빈다 | 스크래핑, LLM, 실패, 재시도 |
| 분석 | `quant/analyze/` | 선정이 나빠진다 | LLM, 느린 배치 |
| **거래** | `quant/trade/` | **돈을 잃는다** | 결정론적 코드만 |
| 제어 | `quant/control/` | 다음 세션이 나빠진다 | 자동 조정, 실험, 롤백 |

그 아래 `quant/core/`(순수 도메인 — 외부 의존 0), `quant/adapters/`(네트워크·디스크
I/O 는 **오직 여기서만**), `quant/apps/`(진입점 3개)가 받친다.

의존 규칙 — **`tests/test_architecture.py`가 임포트 그래프로 강제한다:**

- `quant/core/`는 `quant` 안에서 자기 자신만 안다. httpx/yaml/jinja2/DB 드라이버 금지.
- `quant/trade/`는 `collect`·`analyze`·`adapters`·`apps`를 임포트하지 않는다.
  HTTP·DB 라이브러리도 금지 — 09:15 에 MySQL 이 딸꾹질했다고 매매가 멈추면 안 된다.
- **`quant/collect/`·`quant/analyze/`는 `quant/trade/`를 임포트하지 않는다.**
  스크래핑한 뉴스가 주문으로 이어지는 경로를 코드 수준에서 끊는다. 이게 가장
  중요한 규칙이다 — 뉴스는 *유니버스만* 편집하고, 진입은 전략이 가격으로 판단한다.
- `quant/control/`은 `quant/trade/`를 임포트하지 않는다. 거버너는 `settings.yaml`을
  쓰고, 엔진이 다음 리로드에 읽는다.
- 어댑터는 `quant/core/ports.py`의 Protocol (`Clock`, `DataFeed`, `Broker`,
  `RiskManager`, `Notifier`, `EventSink`, `Strategy`)을 구현한다. 어댑터의 네트워크
  예외는 어댑터 안에서 삼킨다 — raw 예외를 코어로 올리지 않는다.

아직 남은 위반 5건은 `tests/test_architecture.py`의 `KNOWN_DEBT`에 이유와 함께
등재돼 있다. **그 목록은 줄어들기만 한다** — 새 위반을 목록에 추가하는 게 아니라
고친다. 부채를 갚으면 목록에서도 지워야 한다(stale 테스트가 강제).

## 검증 커맨드 (완료 주장 전 반드시 실행)

```bash
uv run pytest                                                           # 전체 1,500+
uv run python -m quant.apps.cli backtest --strategy donchian --days 90  # 거래 스모크
uv run python -m quant.apps.report_cli --help                           # 리포트 스모크
```

두 명령이 에러 없이 통과하지 않으면 "완료"라고 보고하지 않는다. 자신이 건드리지
않은 영역에서 실패가 났다면 그 실패를 그대로 보고한다 — 범위 밖 코드를 고쳐서
통과시키지 않는다.

## 절대 하지 말 것

- **시크릿 커밋 금지** (`.env.local`, `*.pem`). `.env.local`은 `.gitignore`에 있다 —
  절대 `git add -f`로 우회하지 않는다. 시크릿 키 목록은 `.env.example` 참고.
- **거래 핫패스에 LLM/네트워크 호출 금지.** 엔진(`quant.apps.cli paper`)은
  결정론적으로 돌아야 한다 — 장중에 API 호출이 멈추면 그대로 금전적 손실이다.
  Claude/LLM 호출은 `server/scripts/tg_bridge.py` 같은 별도 프로세스(리포팅
  레이어)에서만 허용된다 (ADR-0002 참고).
- **테스트를 약화시켜 통과시키지 않는다.** 실패하는 테스트는 프로덕션 코드의
  버그를 가리키는 신호다. `test.skip`/`.only`, assertion 삭제, mock으로 실패 원인
  가리기 금지.
- **시장 데이터를 조작하지 않는다.** `quant/adapters/data/stub.py`는 백테스트/스모크
  전용 합성 데이터(seed=42)로 명시돼 있다 — paper/live 코드 경로에서 사용하거나,
  실데이터인 것처럼 위장하지 않는다.

## 어디에 뭘 넣나 (라우팅 테이블)

| 하고 싶은 일       | 넣을 곳                                                                                                                                   |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| 새 전략 추가       | `quant/trade/strategy/<name>.py` + `quant/trade/strategy/__init__.py`의 `STRATEGY_REGISTRY`에 등록 + `config/settings.yaml`의 `strategies:` 블록 |
| 새 브로커 추가     | `quant/adapters/brokers/<name>/` — `quant.core.ports.Broker` Protocol 구현                                                                 |
| 새 알림 채널 추가  | `quant/adapters/notify/<name>.py` — `quant.core.ports.Notifier` Protocol 구현                                                              |
| 새 백테스트 실험   | `backtest/` (노트북, `quant.backtest` import) 또는 `quant/backtest/engine.py`                                               |
| 국면(regime) 지표 추가 | `quant/trade/regime/` — 시장별(`US`/`KR`) 분리 구조, ADR-0009                                                                        |
| 관심종목 채점 규칙 변경 | `quant/analyze/watch_scorer.py` (프리퍼시티 게이트 + 논지 태그별 증거점수) + 브리핑 스킬 `.claude/skills/daily-market-brief/`      |
| 성적 집계/스코어보드   | `quant/control/ledger.py` + `run scoreboard` CLI                                                                                    |
| 배포 관련 변경     | `server/` (systemd, crontab, scripts) — 절차는 `docs/runbooks/deploy.md`                                                                  |
| 전략 파라미터 변경 | `config/settings.yaml` (핫 리로드 대상, git 추적)                                                                                         |
| 시크릿 추가/변경   | `.env.example`에 키 이름만 추가 (값은 `.env.local`, git 미추적)                                                                           |

## 재설계를 이어서 할 때

이 저장소는 **Phase 1~8 재설계** 진행 중이다(현재 `redesign` 브랜치, Phase 1~3b 완료).
"다음 phase", "이어서 작업", "어디까지 했지"를 물으면 **`resume-redesign` 스킬이
자동으로 뜬다** — 문서를 읽으라고 시킬 필요가 없다.

- 🔴 **지금 우선순위**: [`docs/plans/개선-백로그-2026-08-15.md`](docs/plans/개선-백로그-2026-08-15.md)
  — Phase 1~7 배선은 끝났다. **다음 할 일은 다음 phase 가 아니다.** 원장으로 확인된
  사실은 *수수료가 엣지보다 크다*는 것이다(US 3개 전략 전부 수수료 전 양수인데 왕복
  20bp 가 음수로 뒤집었다). 실측 표 · 세션 로그 결함 7건 · 우선순위 P0~P3 가 거기 있다.
- 체크리스트: [`docs/plans/재설계-phase4-8.md`](docs/plans/재설계-phase4-8.md)
- 배경·결정: [`docs/vault/재설계-착수보고서.md`](docs/vault/재설계-착수보고서.md)

**문서보다 먼저 시스템에게 물어라** — `git log --oneline -8`(커밋 메시지에 '왜'가
있다), `uv run pytest -q tests/test_architecture.py -v`(평면 규칙 + 남은 부채).
문서는 낡을 수 있지만 이것들은 아니다.

## 퀀트 판단이 필요할 때

전략·백테스트 결과·리스크·실거래 전환을 다룰 때는 **`quant-expert` 스킬을 발동한다**
([`.claude/skills/quant-expert/SKILL.md`](.claude/skills/quant-expert/SKILL.md)).
구현자가 아니라 퀀트 실무자로 판단하고, 성과 주장·API 동작·수정 완료 주장을
적대적 서브에이전트로 교차검증하기 위한 것이다. 특히 백테스트 숫자를 해석하거나
실거래 전환을 논의할 때는 선택이 아니라 필수다.

## 지식 그래프 + 기억 Vault (질문 답할 때 여기부터)

이 저장소는 코드·문서·운영 절차 전체가 **graphify 지식 그래프**(3,841 노드 /
13,360 엣지)로 색인돼 있고, 그 위에 사람이 쓴 **기억 노트**가 얹혀 있다.

**저장소에 관한 질문을 받으면 순서는 이렇다:**

0. **처음 이 저장소를 맡았다면 [`docs/vault/착수보고서.md`](docs/vault/착수보고서.md)를
   먼저 읽는다.** 이 프로젝트가 무엇을 만드는지, 무엇이 끝났고 뭐가 남았는지,
   왜 그렇게 하는지(원칙과 그 원칙을 낳은 실제 실패들)가 한 파일에 있다.
   다른 세션·계정이 이어받을 수 있게 쓴 인수인계 문서다.
1. `graphify query "<질문>"` — 파일을 하나씩 열어보는 것보다 빠르고 정확하다.
   `graphify path "A" "B"`(두 개념 사이 경로), `graphify explain "<노드>"`도 있다.
2. [`docs/vault/00-START-HERE.md`](docs/vault/00-START-HERE.md) 아래 손으로 쓴 4개 노트
   — 시스템 작동방식 / 기능 목록 / 변경기록 / 운영 상수. 그래프가 모르는 것(왜 그렇게
   정했는지, 사용자 선호, 운영 사건)이 여기 있고, **그래프와 충돌하면 이쪽이 최신**이다.
3. 그래도 모르면 실제 코드.

**변경했으면 기록한다.** 코드나 운영 설정을 바꾼 턴에
[`docs/vault/변경기록.md`](docs/vault/변경기록.md) 맨 위에 항목을 추가한다 (형식은 그
노트 상단). git 로그는 *무엇을*, 이 노트는 ***왜*** + **어떻게 검증했는지**를 남긴다.
기능이 늘거나 동작이 바뀌었으면 `기능-목록.md`·`시스템-작동방식.md`도 같이 고친다.

그래프가 낡았으면 `/graphify . --update` 후
`graphify export obsidian --dir docs/vault/graph`. `graphify-out/`과
`docs/vault/graph/`는 재생성 가능하므로 git 미추적이고, `docs/vault/*.md`는 추적한다.

## 참고

- 아키텍처 결정의 배경: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) → [`docs/adr/`](docs/adr/)
- 운영 절차(배포/장애/시크릿 로테이션): [`docs/runbooks/`](docs/runbooks/)
- 디렉토리별 세부 규칙: `quant/core/CLAUDE.md`,
  `quant/trade/strategy/CLAUDE.md`, `quant/adapters/brokers/CLAUDE.md`,
  `quant/adapters/data/CLAUDE.md`, `server/CLAUDE.md`
