# 코드 투어 — 이 저장소는 어떻게 돌아가는가

`ARCHITECTURE.md`가 "왜 이런 구조인가"라면, 이 문서는 **"뭘 고치려면 어디를 열어야
하나"**다. 위에서 아래로 한 번 읽으면 전체 흐름이 잡히도록 썼다.

---

## 1. 30초 요약

서버에서 도는 것은 **세 개의 독립 프로세스**다. 서로를 부르지 않는다.

| 프로세스 | 실행 | 하는 일 | 주기 |
|---|---|---|---|
| **엔진** | `python -m quant.apps.cli paper` | 시세 읽고 → 판단 → 주문 → 알림 | 10초마다 (systemd 상주) |
| **뱅커 리포트** | `python -m quant.apps.cli report` | Toss 실계좌 진단 → Telegram | 매일 07:00 KST (cron) |
| **텔레그램 브리지** | `server/scripts/tg_bridge.py` | 내 질문 → Claude → 답변 | 상주 (systemd) |

**엔진 프로세스에는 LLM·네트워크 지연 요소가 없다.** Claude 호출은 브리지에만 있다
(ADR-0002). 장중에 API가 멈추면 그대로 손실이기 때문이다.

---

## 2. 판단 한 번이 흐르는 경로 (이것만 알면 절반은 안다)

10초마다 이 경로가 정확히 한 번 돈다.

```
run_paper_loop            quant/trade/loop.py:172
  └─ run_cycle            quant/trade/loop.py:30      ← 백테스트도 같은 함수를 쓴다
       │
       ├─ 1. strategy.on_cycle(ctx) ──────────► list[Signal]
       │     quant/trade/strategy/donchian.py / orb.py
       │     "지금 사야 하나?" 만 판단. 수량은 모른다.
       │
       ├─ 2. sinks.on_signal(signal)           quant/adapters/persistence/sink.py
       │     판단 자체를 먼저 기록 (주문이 거부돼도 남는다)
       │
       ├─ 3. control.is_halted() 체크          quant/trade/control.py
       │     킬 스위치. 신규 진입만 막고 청산은 통과시킨다.
       │
       ├─ 4. risk.approve(signal, ctx) ───────► Order | None
       │     quant/trade/risk/manager.py:110
       │     여기서 처음으로 **수량**이 정해진다. 회로차단기 전부 여기.
       │
       ├─ 5. broker.place_order(order) ───────► Fill | None
       │     quant/adapters/execution/paper.py  (모의체결)
       │     quant/adapters/brokers/toss/broker.py  (실주문, MODE=live일 때만)
       │
       └─ 6. sinks.on_fill(fill) + notifier.send()
             체결 기록 + Telegram 알림
```

**핵심 분업 — 이걸 헷갈리면 엉뚱한 파일을 고치게 된다:**

| 질문 | 답하는 곳 |
|---|---|
| "지금 살까 말까?" | `strategies/` |
| "몇 주 살까?" | `risk/manager.py` |
| "어디에 주문을 보낼까?" | `execution/` + `brokers/` |
| "얼마 갖고 있나?" | `portfolio/portfolio.py` |
| "지금 몇 시고 장이 열렸나?" | `data/clock.py` (세션 시각은 `data/session.py`) |
| "가격이 얼마지?" | `data/service.py` → `brokers/toss/datafeed.py` 또는 `data/history.py` |

전략은 `Signal`(목표 **비중**)만 낸다. 절대 수량을 내지 않는다. 그래야 사이징 정책을
전략마다 중복 구현하지 않고 `risk/` 한 곳에서 통제할 수 있다.

---

## 3. 계층 규칙 (어기면 되돌려야 하는 것)

```
        의존성 방향은 항상 안쪽 →
   어댑터                    코어
   brokers/  data/  notify/  execution/   →   strategies/  risk/  portfolio/   →   domain/
   (네트워크 I/O는 오직 여기)                  (순수 계산)                          (의존성 0)
```

- `domain/interfaces.py` 가 **모든 경계의 계약**이다. `Clock`, `DataFeed`, `Broker`,
  `RiskManager`, `Notifier`, `EventSink`, `Strategy` — 전부 Protocol(덕타이핑)이라
  상속이 필요 없다. 같은 메서드만 있으면 꽂힌다.
- 이 덕분에 백테스트가 가능하다: `Clock`을 `SimClock`으로, `DataFeed`를
  `HistoryDataFeed`로, `Broker`를 `PaperBroker`로 갈아끼우면 **전략 코드는 한 줄도
  안 바뀌고** 과거를 다시 산다 (ADR-0004).
- 전략에서 `httpx`를 import하면 아키텍처 위반이다. 시세는 `ctx.data`로만 읽는다.

---

## 4. 조립은 한 군데서만 — `app/assembly.py`

어떤 어댑터를 코어에 꽂을지 결정하는 **유일한 장소**. `run.py`는 이걸 부르기만 한다.

```
build_paper_runtime(settings)          quant/apps/assembly.py:152
  ├─ WallClock                          실시간 시계
  ├─ build_toss_client()                자격증명 없으면 큰 소리로 실패 (stub 대체 금지)
  ├─ build_fx_provider()                환율: Toss 일일갱신, 실패 시 1500원 고정
  ├─ build_market_data()                Toss(실시세) > 로컬 Parquet(과거봉) 우선순위
  ├─ Portfolio.load_or_init()           data/state/portfolio.json 복원
  ├─ PaperBroker(...)                   MODE=live면 여기를 TossBroker로 교체
  ├─ RiskManagerImpl(cfg, ...)
  └─ TradingControl()                   킬 스위치 상태 로드
```

> 여기가 왜 중요한가: 예전에 `FxProvider`를 만들어 놓고 **아무 데서도 연결하지 않아**
> 전 계층이 조용히 고정환율로 돌았다. 조립 실수는 계층 구조가 아무리 깨끗해도 잡아주지
> 않는다. 그래서 조립을 한 파일에 모아 테스트 가능하게 만들었다.

---

## 5. 세 가지 실행 모드

```bash
# 1) 백테스트 — 과거 데이터로 리플레이
uv run python -m quant.apps.cli backtest --strategy orb --interval 5m --source history --days 2700

# 2) 페이퍼 — 실시세, 모의체결 (지금 AWS에서 돌 것)
uv run python -m quant.apps.cli paper

# 3) 뱅커 리포트 — Toss 실계좌 진단
uv run python -m quant.apps.cli report

# 부수: 과거 데이터 백필
uv run python -m quant.apps.cli fetch --symbol TQQQ --start 2016-01-01 --source alpaca --interval 5m
```

**백테스트와 페이퍼는 `run_cycle`을 공유한다.** 차이는 조립뿐이다:

| | 백테스트 | 페이퍼 |
|---|---|---|
| 시계 | `SimClock` — `set()`으로만 전진 | `WallClock` — 실제 시각 |
| 시세 | `HistoryDataFeed` (Parquet) | `MarketDataService` (Toss → Parquet 폴백) |
| 체결 | `PaperBroker` | `PaperBroker` (MODE=live면 `TossBroker`) |
| 반복 | `for ts in replay_closes` | `while True: await sleep(10)` |

백테스트 엔진은 `quant/backtest/engine.py`. 매 실행마다 **회계 항등식을 강제**한다:

```
최종자산 − 초기자산 == Σ실현손익 + 미실현손익 − Σ수수료
```

어긋나면 `ReconciliationError`를 던지고 결과를 반환하지 않는다. 틀린 백테스트 숫자는
"대충 맞는 숫자"가 아니라 아무 의미도 없는 숫자인데, 반환해 두면 누군가는 그걸 성과로
읽기 때문이다.

---

## 6. look-ahead 금지가 강제되는 지점

백테스트가 미래를 훔쳐보지 못하게 하는 장치는 **DataFeed 안**에 있다. 전략이 방어할
필요가 없다.

- `data/history.py` — `history()`/`quote()`는 리플레이 시각까지 **마감된** 봉만 준다.
  봉 인덱스는 봉 *시가* 시각이므로 기준은 `index + interval <= now`다.
  (`index <= now`로 쓰면 그 순간 막 열린 봉의 종가 = 미래 가격이 들어온다. 실제로
  15분치 미래를 주입하던 버그가 있었다.)
- `data/resample.py` — 리샘플 시 마지막 미완성 bin을 항상 버린다.
- `data/service.py` — 소스별 **capability**로 라우팅한다. 실시세만 주는 소스에
  과거봉을 요청하지 않는다.

---

## 7. 상태는 어디에 사는가

| 상태 | 위치 | 살아남나 |
|---|---|---|
| 현금·보유수량·평균단가 | `data/state/portfolio.json` | 재시작 후에도 O |
| 포지션별 손절/목표가 | 같은 파일 안 `Position.meta` | O |
| 킬 스위치 (halt/flatten) | `data/state/control.json` | O |
| 체결·시그널 로그 | `data/events/*.jsonl` | O |
| 전략 인스턴스 필드 (`_session_date` 등) | 메모리만 | **X — 재시작 시 초기화** |

`data/` 디렉토리는 `.gitignore`에 있다 (런타임 상태, 커밋 대상 아님). `quant/adapters/data/`
(소스코드)와 이름이 같으니 헷갈리지 말 것.

---

## 8. 안전장치가 걸리는 지점

전부 `risk/manager.py:approve()` 안에 있고, **청산은 어느 것도 막지 않는다** — 청산을
막으면 손실 포지션을 가두는 꼴이라 방어하려던 리스크보다 나쁘다.

| 장치 | 설정 키 | 무엇을 막나 |
|---|---|---|
| 일일 손실 한도 | `risk.daily_loss_limit_pct` | 당일 신규 진입 |
| 일일 주문 수 상한 | `risk.max_orders_per_day` | 폭주 (10초마다 주문 내던 사고) |
| 손절 후 쿨다운 | `risk.cooldown_bars_after_stop` | 휩소 재진입 |
| 단일 주문 규모 상한 | `risk.max_order_notional_pct` | 사이징 산식 자체가 고장났을 때 |
| NaN/inf/음수 수량 가드 | (하드코딩) | 최종 방어선 |

루프 레벨(`app/loop.py`)에는 추가로:
- 사이클 예외 격리 → 연속 N회 실패 시 **자동 halt**
- 데이터 스테일 가드 → 장중 연속 조회 실패 시 알림
- 하트비트 → 5분마다 "살아 있음 + 포지션 + 자산" Telegram

수동 킬 스위치는 `app/control.py`. `data/state/control.json`을 원자적으로 쓴다.

---

## 9. 무엇을 하려면 어디를 여나

| 하고 싶은 일 | 여는 곳 |
|---|---|
| 전략 로직 수정 | `quant/trade/strategy/<name>.py` |
| 전략 파라미터만 조정 | `config/settings.yaml` — **핫 리로드된다 (재시작 불필요)** |
| 새 전략 추가 | 파일 생성 → `strategies/__init__.py`의 `STRATEGY_REGISTRY` 등록 → settings.yaml 블록 추가 |
| 사이징·리스크 한도 | `risk/manager.py` + `config/settings.yaml`의 `risk:` |
| 새 브로커 연결 | `brokers/<name>/` — `domain.interfaces.Broker` 구현 후 `assembly.py`에서 교체 |
| 알림 문구·채널 | `notify/telegram.py`, 호출부는 `app/loop.py` |
| 시세 소스 추가·우선순위 | `data/service.py`의 `SourceRoute`, 배선은 `assembly.py:build_market_data` |
| 백테스트 지표 추가 | `backtest/engine.py:_compute_metrics` |
| 최적화·워크포워드 | `research/` (`walkforward.py`, `optimize.py`, `signalquality.py`) |
| 배포·systemd·cron | `server/` + `docs/runbooks/deploy.md` |
| 시크릿 추가 | `.env.example`에 **키 이름만** (값은 `.env.local`, git 미추적) |

---

## 10. AWS에서 도는 모습

```
EC2 (Ubuntu, TZ=Asia/Seoul)
├── systemd: quant-engine.service   → .venv/bin/python -m quant.apps.cli paper
│                                     Restart=always, RestartSec=30
├── systemd: tg-bridge.service      → Claude 브리지 (MemoryMax/CPUQuota로 상한)
│                                     엔진의 자원을 절대 빼앗지 않게 격리
└── cron: 07:00 KST                 → quant.apps.cli report
        일 03:00 (일요일)            → 로그·캐시 정리
```

배포는 `QT_SSH_HOST=ubuntu@<ElasticIP> ./server/scripts/deploy.sh`
(git push → ssh → git pull → uv sync → systemctl restart).

> systemd/cron에서 `uv run`을 쓰지 않고 `.venv/bin/python`을 직접 가리키는 이유:
> 데몬 환경은 로그인 셸이 아니라 PATH가 비어 있고, `uv`가 셸 초기화에 의존해 조용히
> 실패한 전례가 있다 (전작 `stock-algo-trade`).

---

## 11. 지금 상태에서 ORB를 페이퍼로 돌리려면 걸리는 것

읽다 보면 마주칠 실제 미해결 지점들이다. 숨기지 않고 적어 둔다.

1. ~~`assembly.py:_primary_interval_minutes`가 ORB의 `bar_interval_minutes`를 못 읽는다~~
   → 수정됨 (두 키를 모두 읽는다).
2. ~~`risk.cooldown_bar_interval_minutes`가 15로 고정이라 5분봉 전략에서 쿨다운이
   조용한 no-op이 된다~~ → 수정됨 (전략별 봉 간격을 따라간다).
3. **실시세 소스가 Toss뿐**이다. 키움 모의투자로 옮기려면
   `brokers/kiwoom/datafeed.py`를 채우고 `assembly.py:build_market_data`의 라우트에
   추가해야 한다.
4. **Toss API는 IP 화이트리스트**다. 등록되지 않은 곳에서 호출하면
   `403 access_denied [IP address not allowed]`가 난다 — 로컬에서는 실시세를 볼 수
   없고, EC2의 등록된 IP에서만 동작한다. 키움 실전 키도 마찬가지로 지정단말기
   인증(`8050`)이 걸려 있다.
5. **`config/settings.yaml`에서 `orb.enabled: false`** — 검증 전 실거래 활성화 금지.
   백테스트는 `enabled`와 무관하게 돈다 (`backtest/engine.py`가 명시적으로 켠다).
6. **사이징 3중 캡이 서로를 무력화한다.** ORB의 `risk_budget_pct`(논문 1%)와
   `max_leverage`가 만든 목표 비중을 `risk.max_position_pct: 50`이 다시 자른다 —
   실측 결과 **거래의 97.4%가 정확히 비중 0.50**으로 수렴해 리스크 기반 사이징이
   사실상 상수가 된다. 논문 규격대로 돌리려면 세 값을 함께 재보정해야 한다.

## 12. 이 시스템이 "정직한 측정기"이기 위해 갖춘 것

숫자를 만들어내기는 쉽고, 틀린 숫자를 눈치채기는 어렵다. 그래서 다음 장치들이 있다.

| 장치 | 위치 | 막는 것 |
|---|---|---|
| 회계 항등식 강제 | `backtest/engine.py:_reconcile` | 손익·자산곡선·체결로그가 서로 다른 이야기를 하는 것 |
| look-ahead 차단 | `data/history.py`, `data/resample.py` | 미완성 봉·미래 가격 주입 |
| 세션 유도 | `data/session.py` | 조기폐장일에 시간외 체결을 만들어내는 것 |
| 슬리피지·수수료 | `execution/paper.py` + `execution.slippage_bps` | "체결은 공짜"라는 가정 |
| 왕복 손익 배분 | `backtest/engine.py:_round_trip_pnl` | 매수 수수료가 빠져 profit factor가 부풀려지는 것 |
| **벤치마크 병기** | `backtest/engine.py:_compute_benchmark` | 전략 수익률만 보고 "단순 보유가 더 나았다"를 놓치는 것 |
| 빈 전략 거부 | `backtest/engine.py` | 거래 0건이 '결과'처럼 보이는 것 |

이 중 어느 하나라도 없던 시절에 실제로 틀린 결론을 냈다. 끄거나 우회하지 말 것.
