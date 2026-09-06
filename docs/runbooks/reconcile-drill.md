# Runbook: 브로커 대사 드릴 (reconcile-drill)

`quant/trade/reconcile.py`의 `Reconciler`(엔진 원장 ↔ 브로커 실보유 대조, 불일치
시 신규 진입 halt)는 실전에서 지금까지 단 한 번도 불일치를 만나본 적이 없다 —
paper는 대사 대상이 아니었고(2026-09-06 이전, `build_reconciler`가 PaperBroker에
항상 None을 돌려줬다), 실계좌(TossBroker)에서도 아직 실제 불일치가 난 적이 없다.
"불일치가 나면 정말 감지하고, 정말 halt하고, 정말 알림이 나가는가"를 사람이 직접
확인하지 않은 채 실전으로 가지 않는다. 이 드릴이 그 확인 절차다.

이 드릴은 **paper 상태를 대상으로 하되 실 파일을 절대 건드리지 않는다** —
`data/state/portfolio.json`을 읽기 전용으로 복사만 하고, halt는 드릴 전용
`TradingControl` 인스턴스에서만 일어난다. 대사가 조회 이상의 일(특히 주문)을
하지 않는다는 것도 매 실행마다 재확인한다(가짜 브로커가 `place_order`를 호출하면
그 자리에서 예외를 던진다).

## 언제 실행하나

- **월 1회** (정기 드릴 — `docs/runbooks/live-readiness.md` D5의 백업 복원
  리허설과 같은 주기 감각으로).
- **MODE를 바꾸기 전** (paper → live 전환, 또는 live 전환 후 브로커/계좌를
  바꿀 때) — 반드시.
- reconcile.py, paper.py의 `engine_owned_*`, 또는 assembly.py의 대사 조립
  코드를 고친 직후.

## 명령

```bash
# 불일치 없음 — 기준선(반드시 '일치' + halt 없음이어야 한다)
uv run python -m quant.apps.cli reconcile-drill --inject none

# 네 가지 불일치 종류 — 전부 감지 + halt돼야 한다
uv run python -m quant.apps.cli reconcile-drill --inject qty
uv run python -m quant.apps.cli reconcile-drill --inject missing
uv run python -m quant.apps.cli reconcile-drill --inject extra
uv run python -m quant.apps.cli reconcile-drill --inject cash
```

옵션:

- `--dry-run` — 스냅샷/제어 파일을 `data/state/drill/`이 아니라 임시 디렉터리에
  만들고 실행 후 지운다(흔적을 전혀 안 남긴다). 기본 모드는 `data/state/drill/`
  아래에 이번 드릴의 스냅샷·제어 상태를 감사 흔적으로 남긴다(다음 사람이 "정말
  돌렸나"를 파일로 확인할 수 있게).
- `--send` — 불일치 알림을 캡처만 하지 않고 실제 텔레그램(ops 레인)으로도
  보낸다. 기본은 캡처만 하고 화면에 출력한다 — 드릴이 실제 알림 소음을 만들지
  않는다.
- `--root` — 저장소 루트(기본 `.`). 다른 체크아웃/스테이징 경로를 대상으로 할 때.

각 실행은 종료코드로도 판정한다: **감지(또는 `none`의 무감지)가 기대대로면 0,
아니면 1**. 크론/CI에 넣을 때는 종료코드만 봐도 된다.

## 기대 출력

`--inject qty` 예시(실제 paper 포트폴리오가 비어 있으면 내장 fixture로 대체된다는
안내가 한 줄 더 붙는다):

```
=== 브로커 대사 드릴 ===
주입 종류: qty
대상 심볼: 005930
검사 실행됨: True
판정: 불일치 — 005930: 엔진 원장 10주 > 브로커 보유 5주
제어 상태: halted — 브로커 대사 불일치 — 신규 진입 중단 (005930: 엔진 원장 10주 > 브로커 보유 5주)
운영 알림(ops 레인, 캡처됨 — 실제로 보내지 않았다):
브로커 대사 불일치 — 신규 진입을 중단했다(청산은 계속 동작한다).
005930: 엔진 원장 10주 > 브로커 보유 5주
원인 확인 후 /resume 할 것.

예상대로 'qty' 불일치를 감지해 신규 진입을 halt했다. 청산 주문은 이 상태에서도 계속 나간다(reconcile.py의 정책).
```

`--inject none` 예시:

```
=== 브로커 대사 드릴 ===
주입 종류: none
대상 심볼: (해당 없음)
검사 실행됨: True
판정: 일치
제어 상태: 정상(halt 아님)
운영 알림: 없음(불일치가 없거나 이미 알림을 보낸 뒤 재확인 사이클)

예상대로 '일치' — 신규 진입 halt 없음.
```

**"실패"로 봐야 하는 출력**: `판정: 일치`인데 `--inject`가 `none`이 아니거나,
`제어 상태: 정상(halt 아님)`인데 불일치를 주입했다면 대사 코드가 깨진 것이다 —
종료코드도 1이다. 이 상태로는 절대 라이브 전환하지 않는다.

## 실 라이브 모드에서 진짜 불일치가 나면 무슨 일이 일어나는가

이 드릴이 재현하는 것과 정확히 같은 코드 경로가 실전에서도 그대로 돈다
(`quant/trade/reconcile.py`의 같은 `Reconciler.check()`, 드릴이 재구현하지
않는다):

1. **신규 진입만 halt** — `TradingControl.halt(reason, by="auto")`. 열린
   포지션의 손절·익절·마감청산은 그대로 동작한다(halt는 ENTER/SCALE_IN만
   막는다) — 불일치 상황에서 청산까지 막으면 방어하려던 리스크보다 더 나쁜
   상태가 된다.
2. **텔레그램 ops 레인으로 알림** — "브로커 대사 불일치 — 신규 진입을
   중단했다(청산은 계속 동작한다)." + 불일치 상세 + "원인 확인 후 /resume 할
   것." 같은 불일치가 반복돼도 알림은 최초 1회만 간다(로그는 매 사이클 남는다).
3. **자동 resume은 없다** — 원인을 사람이 확인하고 `/resume`해야 한다. 원인 모른
   채 자동 복구하면 같은 사고가 조용히 반복된다는 것이 이 모듈의 설계 원칙이다.
4. `cli health`의 "reconcile" 잡이 "마지막 대조 성공 시각"을 보여준다 —
   불일치가 계속 halt로 남아 있으면(성공=불일치 없음이 아니라 "검사가
   실행됐는가" 기준이므로, 검사 자체는 계속 성공으로 기록된다는 점에 유의)
   운영자가 halt 여부는 `/status`로, 검사 생존 여부는 `cli health`로 각각
   확인한다.

paper 모드에서는 매 사이클(기본 5분 간격, `engine.reconcile_interval_minutes`)
같은 `Reconciler.check()`가 **항등 대사**(엔진 소유 원장 = paper 포트폴리오
자신)로 계속 실행된다 — 불일치가 날 수는 없지만, 코드 경로 자체가 죽어 있지
않다는 것을 매일 확인해준다. 이 드릴은 그 코드 경로가 **불일치가 실제로 있을
때** 기대대로 반응하는지를 확인한다.

## 실전 전환 전 체크리스트

- [ ] `--inject none`이 '일치' + halt 없음을 낸다
- [ ] `--inject qty` / `missing` / `extra` / `cash` 네 가지 전부 감지 + halt를 낸다
- [ ] 출력의 "제어 상태"에 halt 사유가 사람이 읽고 원인을 좁힐 수 있는 문장으로
      나온다
- [ ] `--send`로 한 번 실제 텔레그램 ops 레인까지 확인(월 1회 정기 드릴 때 1회면
      충분 — 매번 보낼 필요는 없다)
- [ ] `cli health`의 "reconcile" 잡이 UNKNOWN이 아니라 최근 성공을 보여준다(엔진이
      한동안 돌고 있었다는 뜻)
- [ ] 이 문서와 `docs/runbooks/live-readiness.md` D1 상태가 최신이다

## 관련

- `docs/runbooks/live-readiness.md` D1
- `tests/test_reconcile.py`, `tests/test_reconcile_drill.py`,
  `tests/test_paper_broker_engine_owned.py`
- `quant/trade/reconcile.py`, `quant/adapters/execution/paper.py`,
  `quant/apps/assembly.py` (`build_reconciler`/`build_reconcile_heartbeat`)
