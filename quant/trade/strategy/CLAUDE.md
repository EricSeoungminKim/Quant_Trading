# CLAUDE.md — quant/trade/strategy/

## 여기 있는 것

전략 구현체. 각 전략은 `quant.core.ports.Strategy` Protocol을 만족하는 클래스 —
`id: str`, `symbols: list[str]`, `on_cycle(ctx: Context) -> list[Signal]`.

**활성 목록의 진실은 `config/settings.yaml`의 `strategies:` 블록 `enabled`
필드다** — 아래는 2026-09-03 기준 요약일 뿐이고 낡을 수 있다. 현재 활성(12개,
루트 `CLAUDE.md` "이 시스템은 무엇인가" 참고): `news_momentum.py`/`news_scalp.py`,
`pullback_impulse.py`(+`_cat` 갈래), `mr_vwap_quiet.py`, `vol_breakout.py`
(+`_cat` 갈래), `intraday_momentum.py`, `gap_fade.py`, `scalp_1m.py`(+`_cat`
갈래), `llm_trader.py`. `<id>_cat`은 별도 파일이 아니라 같은 클래스를 다른
유니버스로 돌리는 A/B 갈래다(아래 "로컬 불변식" 참고). `donchian.py`/
`orb_scan.py`/`intraday_scan.py`를 포함해 나머지는 현재 `enabled: false`(코드는
남아 있다 — 측정 기준점 + 추후 복원용). 비활성이지만 유지: `orb.py` — orb_scan의
규격 출처(Zarattini & Aziz 논문판)이자 in-sample 측정 기록의 기준점.

공통 시맨틱(2026-08-10): 유효한 진입 신호를 '이미 보유/이미 진입'으로 막지 않는다.
같은 완성봉 1회 가드(`_last_entry_bar`)와 리스크 레일이 폭주를 막고, 사이징은
리스크 레이어가 잔여룸 기준 증분으로 계산한다.

## 절대 여기 임포트하지 말 것

- `quant.adapters.brokers.*`, `httpx` 등 네트워크/브로커 코드 — 전략은
  `ctx.data`(DataFeed)와 `ctx.clock`(Clock)만 읽는다. `ctx.broker`는 포지션 조회
  (`ctx.broker.positions()`)에만 쓰고, 직접 주문을 내지 않는다 — 사이징/주문 생성은
  `risk/`의 소관이다.
- `quant.apps.config` — 파라미터는 생성자 인자(`params: dict`)로 주입받는다.

## 로컬 불변식

- **전략 1개 = 파일 1개.** config 별칭으로 같은 클래스를 다중 인스턴스화하지 않는다
  (전작 `donchian_apex_vm17...` 같은 패턴 재발 금지 — ADR-0007).
  - **유일한 예외: A/B 갈래 `<id>_cat`**(2026-09-03). 촉매 태그 유무로 유니버스만
    가른 같은 전략의 두 갈래이고, `params` 는 YAML 앵커로 **공유**한다(파라미터가
    다르면 그건 A/B 가 아니다). ADR-0007 이 막으려던 것은 파라미터만 다른 전략
    동물원이고, 이건 **기한이 있는 실험**이다 — 판정(`scoreboard --ab`, 양쪽
    n>=30)이 나면 진 갈래를 지운다. 새 갈래를 늘리려면 그 조건을 먼저 읽어라.
- `on_cycle`은 `Signal`(목표 비중 기반, 수량 아님)만 반환한다 — 실제 주문
  수량/승인은 `risk.manager.RiskManagerImpl.approve()`가 결정한다.
- 포지션당 부가 상태(entry/stop/target 등)는 `Position.meta`에 저장한다 — 이는
  `Portfolio` 영속화에 얹히므로 재시작에도 살아남는다.
- `DataFeed.history()`가 반환하는 봉은 완성봉뿐이라는 전제로 로직을 짠다
  (look-ahead 없음이 이미 보장됨 — 전략에서 추가로 방어할 필요 없음).

## 새 전략을 추가하는 법 (recipe)

1. `quant/trade/strategy/<name>.py`에 클래스 작성. `donchian.py`를 참고해
   `__init__(self, symbols, params, market="US", id="<name>")`와
   `on_cycle(self, ctx: Context) -> list[Signal]` 시그니처를 맞춘다.
2. `quant/trade/strategy/__init__.py`의 `STRATEGY_REGISTRY` dict에 등록:
   `STRATEGY_REGISTRY = {"donchian": DonchianStrategy, "<name>": NewStrategy}`.
3. `config/settings.yaml`의 `strategies:` 블록에 항목 추가 (`class`는
   registry의 key, `symbols`, `params`, `capital_fraction`, `enabled`).
4. `tests/`에 단위 테스트 추가 — 기존 전략은 `tests/test_donchian.py`를 패턴으로
   참고 (stub `DataFeed`/`Context`로 `on_cycle` 직접 호출).
5. `uv run python -m quant.apps.cli backtest --strategy <name>`으로 스모크 확인.
