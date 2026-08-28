"""눌림목(pullback) 임펄스 스캘프 — 5분봉. **순수 계약 전용 신규 전략**(레거시
쌍둥이 없음, 2026-08-28 소유자 지시 "스캘핑 전략을 여러 개 병렬로 돌려 실전에서
살아남는 것을 찾는다").

## 왜 이 전략인가 — 우리 자신의 실패 데이터가 근거다

기존 유일 스캘핑 전략 `scalp_1m`(돌파형)은 원장 평균 **-57bp**다. 원장 재생이
말한 것은 "전략이 틀렸다"가 아니라 **"진입 지점이 틀렸다"**이다:

- 손절당한 뒤 **76%(35/46)가 당일 진입가 위로 회복**, 회복 폭 중앙 **+105bp**
- 손실 건 보유시간 중앙 **5.9분** — 파동의 고점에서 사서 눌림에 털린다
- 거래량 급증 진입이 **오히려 나쁘다**(D+1 스피어만 -0.46; 장중 고RVOL -79.8bp
  vs 저RVOL -31.3bp) — 서지 순간이 곧 그 파동의 고점이라는 뜻이다

같은 신호를 **눌림 이후에** 사면 그 +105bp 가 손실이 아니라 수익 쪽에 붙는다.
회복폭 중앙 105bp 는 손익분기 목표(100~150bp)와 정확히 같은 스케일이다.

## 왜 5분봉인가 (비용 실측)

왕복 비용은 US **20bp**, KR 개별주 **30bp**(매도세 20bp). 1분 절대변동 중앙은
8.3bp — **1~2분 청산은 구조적으로 진다**(비용이 신호보다 크다). 일중 고저 중앙은
714bp 이므로 시간 축을 늘리면 먹을 것이 있다. 그래서 주력 타임프레임 5분,
목표 100~150bp, 보유 20~65분(`timeout_minutes` 기본 60)이다.

**5분봉을 어떻게 얻는가(추측이 아니라 코드 확인):** `DataFeed.history`의 계약이
`quant/core/ports.py:65-67`에 `interval: "1m" | "5m" | "15m" | "1d"`로 명시돼
있고, 실경로인 `TossDataFeed.history`(`quant/adapters/brokers/toss/datafeed.py:200`)
는 `"1d"`/`"1m"`이 아닌 interval 을 `resample_1m(bars_1m, _interval_minutes(interval))`
로 1분봉에서 리샘플해 돌려준다. `MarketDataService`(`quant/adapters/data/service.py`)
의 봉 경계 캐시·완성봉 필터도 `_interval_minutes("5m") == 5`로 정상 처리한다.
운영 중인 `intraday_scan`/`confluence`가 이미 `f"{bar_interval_minutes}m"` = `"5m"`
을 그대로 요청하고 있다. → **`DataNeeds`에 `"5m"`을 직접 선언한다**(1분봉을 받아
전략이 리샘플하지 않는다 — 그러면 어댑터가 이미 하는 일을 두 벌로 만든다).

## 규칙 (문헌 + 위 실측)

1. **임펄스**: 오늘 세션 5분봉에서 세션 신고가(prior 구간 최고가)와 그 직전
   저점 사이 폭이 `min_impulse_bp`(80) 이상.
2. **되돌림**: 임펄스 폭의 `pullback_min_pct`~`pullback_max_pct`(0.38~0.62)
   되돌림. **또는** 되돌림 저점이 세션 VWAP / EMA9 를 터치했으면 최소 깊이
   요건을 면제한다. 단 **상한(`pullback_max_pct`)은 두 경로 공통 하드 게이트**다
   — 과도한 되돌림은 임펄스 구조 자체가 깨진 것이고, VWAP 터치가 그것을 면제해
   주지 않는다(터치는 "얕아도 지지에 닿았다"는 근거이지 "깊어도 괜찮다"는
   근거가 아니다).
3. **되돌림 확인**: 되돌림 구간(고점→되돌림 저점) 봉당 평균 거래량 < 임펄스
   구간(임펄스 저점→고점) 봉당 평균 거래량. 매도 압력 소진의 코드화이자, 위
   "고RVOL 진입이 나쁘다" 실측과 부합하는 방향이다.
   **합이 아니라 봉당 평균으로 비교한다** — 두 구간의 봉 개수가 다르므로 합
   비교는 사실상 "어느 구간이 더 길었나"를 재게 된다(임펄스가 1봉이면 거의 항상
   통과, 되돌림이 길면 거의 항상 탈락).
4. **진입 트리거**: 되돌림 저점 **이후 첫 반등봉(종가>시가)의 고가**를 이후
   완성봉의 고가가 넘으면 진입.
5. **1일 1종목 1회**(`taken` 상태). 첫 눌림만 취한다 — 2·3차 눌림은 실패율이
   오른다는 게 실무 공통 규칙이고, 우리 원장에도 재진입 누적 손실이 있다.
6. **손절**: 되돌림 저점 − `atr_buffer_mult`(0.3) × ATR(5분봉).
   ATR 절대값은 기존 순수 지표 `atr_ratio`(Wilder 평활, `trend_gate.py`)에
   마지막 종가를 곱해 얻는다 — 같은 개념의 두 번째 구현을 만들지 않는다.
   ATR 이 계산 불가면(봉 부족) **진입하지 않는다** — 손절선을 정할 수 없는
   자리는 진입하지 않는다는 `quant/trade/structure.py`의 손절 철학 그대로다.
   `structure_bracket` 재사용도 검토했으나 쓰지 않았다: 그쪽은 (a) 손절 기준을
   "가장 가까운 스윙 저점"으로 잡는데 그것이 **이 임펄스의 되돌림 저점이라는
   보장이 없고**(다른 파동의 저점일 수 있다), (b) 버퍼가 ATR 이 아니라 %라
   변동성에 적응하지 않는다. 진입 근거와 손절 근거는 같은 구조여야 한다.
7. **목표**: 진입가 + 임펄스 폭 × `target_mult`(1.2).
8. **타임아웃**: `timeout_minutes`(60) 경과 시 청산 — 눌림목 되돌림 트레이드는
   빨리 가야 맞는 것이고, 안 가면 근거가 소멸한 것이다.
9. **EoD/오버나잇 금지**: 다른 일중 전략과 동일(세션 롤 강제청산 + 마감 전 청산).

## KR 동시호가 (실제로 돈을 잃은 적이 있는 자리)

KR 연속매매는 **15:20 종료**다(15:20~15:30 은 마감 동시호가 — 주문만 모아
15:30 종가로 일괄 체결). 신규 진입은 `quant.core.session.in_continuous_session`
으로 게이트한다 — 2026-08-26 에 `scalp_1m`이 동시호가/장전 구간에 "실재할 수
없는 체결"을 기록해 손절선을 2.8% 지나친 사고가 그 자리다. EoD 청산도 안전하다:
`Clock.minutes_to_close`가 이미 연속 거래 끝(15:20)까지의 잔여시간을 준다
(`quant/core/clock.py`의 `_effective_close`).

## 상태를 두 갈래로 흘린다 (재시작을 견디는 이유)

**장중 재시작은 가정이 아니라 사건이다** — 2026-08-28 에 실제로 포지션 8개를
연 채로 엔진이 재시작됐다. `next_state`는 껍질의 인스턴스 필드일 뿐이라
(`shell.py`) 재시작에 사라진다. 열린 랏의 방어선(손절/목표/타임아웃)을 거기
두면 **포지션은 브로커에 남는데 아무도 손절을 보지 않는 상태**가 된다. EoD
레일조차 못 잡는다 — 관리 루프가 그 랏을 모르기 때문이다.

그래서 `CloseBetPureStrategy`(`close_bet.py`)가 오버나잇 문제를 푼 것과 같은
방식으로, 상태를 **수명에 따라** 두 갈래로 나눈다:

| 키 | 무엇 | 어디로 흐르나 | 재시작 |
|---|---|---|---|
| `session_date: {market: "YYYY-MM-DD"}` | 세션 롤 감지 | `next_state` | 잃어도 무해(다음 사이클에 다시 채워진다) |
| `taken: {symbol: "YYYY-MM-DD"}` | 1일 1종목 1회 게이트 | `next_state` | 잃는다(아래 "아직 못 하는 것" 5번) |
| `pending: {symbol: lot}` | 신호는 냈고 **아직 체결 확인 전** | `next_state` | 잃어도 무해 — 체결되지 않았다면 방어할 포지션 자체가 없다 |
| `last_reject: {symbol: str}` | 진단용 거부 사유 | `next_state` | 잃어도 무해(진단) |
| **`entry`/`stop`/`target`/`entered_at`/`impulse_high`/`pullback_low`** | **열린 랏의 방어선** | **`Signal.state_update` → 루프가 체결 확인 후 `Position.meta["lots"]`에 기록 → 다음 사이클에 `snap.lots`로 회수** | **살아남는다** |

즉 **포지션이 살아 있는 한 필요한 값은 `next_state`에 두지 않는다.** 그 값들의
정본은 브로커 포지션의 lot 이고, 껍질이 매 사이클 `snap.lots[symbol]`로 돌려준다
(`shell.py`의 `needs_positions` 경로). 껍질 인스턴스를 통째로 버려도(=재시작)
다음 사이클에 같은 손절·목표·타임아웃이 그대로 나온다 —
`tests/test_pullback_impulse.py`의 `test_open_lot_survives_process_restart`가
그것을 고정한다.

덤으로 **Phase A 공통 한계가 이 값들에 한해 해소된다**: `next_state`는 체결
여부와 무관하게 적용되지만(`shell.py`), `state_update`는 `loop._execute_signal`이
**실제 Fill 을 받은 뒤에만** lot 에 적용한다(`loop.py:412-422`) — risk 거부나
미체결로 방어선이 오염되는 경로가 없다.

`pending`은 그래도 남긴다: 체결 직후 `state_update`가 lot 에 반영되기 전의 한
사이클 창(스냅샷의 `lots[symbol] == {}`)에서 관리가 끊기지 않게 하는 **같은
프로세스 안에서만 유효한 폴백**이다. lot 에 `entry`가 보이면 그때 버린다 —
정본이 둘이 되지 않도록.

`decide()`는 인자로 받은 `state`의 **사본**만 고쳐 반환한다(원본 in-place mutate
없음) — 같은 인스턴스를 여러 사이클에서 재진입 호출해도 상태가 오염되지 않는다.

## 아직 못 하는 것 (정직하게)

1. **성과 근거가 없다.** 이 전략이 이긴다는 증거는 아직 0이다. 근거는 "왜 지금
   지고 있는지"의 진단(위 원장 재생)뿐이고, 그 진단이 맞다는 것과 이 규칙이
   돈을 번다는 것은 다른 명제다. 백테스트 표본도 없다 — Toss 1분봉이 4거래일
   롤링만 주므로(`docs/data-availability.md`) 5분봉 표본도 그만큼이다. paper
   번인이 유일한 검증 경로다.
2. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 `self.symbols`만
   선언하므로 유니버스에서 빠진 뒤 남은 보유분은 보이지 않는다(순수 이관
   전략 공통 한계 — `Scalp1mPureStrategy` "아직 못 하는 것" 4번).
3. **손절 하드캡이 없다.** `scalp_1m`의 `stop_hard_cap_pct` 같은 바닥이 없다 —
   손실 폭은 "되돌림 깊이 + ATR 버퍼"에 구조적으로 묶여 있지만(되돌림 상한
   62% + ATR), 임펄스가 극단적으로 크면 1회 손실도 크다. 리스크 레이어의
   포지션 상한이 마지막 방어선이다.
4. **되돌림 저점 재경신을 다루지 않는다.** 첫 반등봉 이후 더 깊은 저점이
   나와도 setup 은 첫 저점 기준을 유지한다(첫 눌림만 취한다는 규칙 5의 연장) —
   그 경우 손절선이 현재 구조보다 위에 있을 수 있다.
5. **재시작이 "1일 1회" 게이트는 되돌린다.** 방어선은 살아남지만(위 "상태를 두
   갈래로" 절) `taken`은 `next_state`라 사라진다. 보유 중인 심볼은 안전하다 —
   진입 루프가 `snap.lots`에 있는 심볼을 건너뛰므로 중복 진입이 나지 않는다.
   구멍은 **"진입 → 청산 → 재시작"이 같은 날 일어난 경우**뿐이고, 그때는 같은
   셋업이 다시 성립하면 2차 눌림에 재진입할 수 있다(규칙 5 위반). 이것까지
   막으려면 청산된 랏의 흔적을 영속해야 하는데, 그 경로(`state_update`)는 전량
   청산 시 적용되지 않는다(`loop.py:414-417`의 의도적 동작) — 원장을 읽는 것은
   거래 평면에서 금지다. 손실이 아니라 **빈도** 문제라 여기서 멈춘다.
6. **조회 최적화가 없다.** 정적 `DataNeeds`라 시장이 닫혀 있어도 매 사이클 전
   심볼의 5분봉을 요청한다. `MarketDataService`의 봉 경계 캐시가 실제 API
   호출은 봉당 1회로 눌러주므로(같은 5분 경계 안에서는 캐시 히트) 운영 부담은
   1분봉 전략보다 오히려 작다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime, timedelta
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import ema
from quant.trade.indicators.trend_gate import atr_ratio
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "5m"

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분. lookback 하한 산정용.
_FULL_SESSION_MINUTES = 390


class PullbackImpulsePureStrategy:
    """모듈 docstring 참고. `decide()`는 `snap`/`state`만 본다."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "pullback_impulse",
    ):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Protocol 호환 — 실제 판정은 심볼별 시장 추론

        # 1) 임펄스 최소 폭(bp). 왕복 비용 20~30bp 대비 충분히 커야 목표
        #    (폭 x target_mult)가 비용을 넘는다 — 80bp x 1.2 = 96bp.
        self.min_impulse_bp: float = float(params.get("min_impulse_bp", 80))
        # 2) 되돌림 허용 구간(임펄스 폭 대비 비율). 문헌 표준 피보나치 38.2~61.8%.
        self.pullback_min_pct: float = float(params.get("pullback_min_pct", 0.38))
        self.pullback_max_pct: float = float(params.get("pullback_max_pct", 0.62))
        # 되돌림 최소 깊이를 면제하는 앵커(VWAP/EMA) 중 EMA 기간.
        self.ema_period: int = int(params.get("ema_period", 9))
        # 6) 손절 버퍼 — 되돌림 저점 아래 ATR 배수.
        self.atr_buffer_mult: float = float(params.get("atr_buffer_mult", 0.3))
        # 최소 손절폭(bp). 기본 40 = 왕복 비용(US 20bp)의 2배 — 손절폭이 비용과
        # 같은 자릿수면 진입 자체가 마이너스섬이다(2026-08-29 NOW 실사고 주석 참고).
        self.min_stop_bp: float = float(params.get("min_stop_bp", 40.0))
        self.atr_period: int = int(params.get("atr_period", 14))
        # 7) 목표 — 임펄스 폭 배수.
        self.target_mult: float = float(params.get("target_mult", 1.2))
        # 8) 타임아웃(분).
        self.timeout_minutes: float = float(params.get("timeout_minutes", 60))
        # 9) EoD 청산 — 다른 일중 전략과 동일한 이름/기본값.
        self.flatten_minutes: float = float(params.get("flatten_before_close_minutes", 1))
        # 사이징 비중. 1일 1종목 1회이므로 슬롯이 구조적으로 1개지만, 유니버스가
        # 여러 종목이라 전량 비중은 위험하다 — 보수적 기본값.
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.min_impulse_bp <= 0:
            raise ValueError("min_impulse_bp는 양수여야 합니다.")
        if not 0 < self.pullback_min_pct < 1:
            raise ValueError("pullback_min_pct는 0과 1 사이여야 합니다.")
        if not 0 < self.pullback_max_pct <= 1:
            raise ValueError("pullback_max_pct는 0 초과 1 이하여야 합니다.")
        if self.pullback_min_pct >= self.pullback_max_pct:
            raise ValueError("pullback_min_pct는 pullback_max_pct보다 작아야 합니다.")
        if self.ema_period < 2:
            raise ValueError("ema_period는 2 이상이어야 합니다.")
        if self.atr_buffer_mult < 0:
            raise ValueError("atr_buffer_mult는 0 이상이어야 합니다.")
        if self.min_stop_bp < 0:
            raise ValueError("min_stop_bp는 0(비활성) 이상이어야 합니다.")
        if self.atr_period < 2:
            raise ValueError("atr_period는 2 이상이어야 합니다.")
        if self.target_mult <= 0:
            raise ValueError("target_mult는 양수여야 합니다.")
        if self.timeout_minutes <= 0:
            raise ValueError("timeout_minutes는 양수여야 합니다.")
        if self.flatten_minutes <= 0:
            # clock._should_flatten docstring: 0이면 마지막 in-session 사이클에서도
            # 조건이 성립하지 않아 청산 창이 통째로 사라진다(실제 사고 이력).
            raise ValueError("flatten_before_close_minutes는 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 조회할 5분봉 개수 — 오늘 세션 전체(390/5=78봉) + ATR/EMA 워밍업을 덮는다.
        self._lookback_bars = max(
            int(params.get("lookback_bars", 120)),
            _FULL_SESSION_MINUTES // 5 + self.atr_period + self.ema_period + 1,
        )

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """5분봉 직접 요청 — 모듈 docstring "왜 5분봉인가" 절의 코드 근거 참고."""
        return DataNeeds(
            bars=tuple((s, _INTERVAL, self._lookback_bars) for s in self.symbols),
            quotes=tuple(self.symbols),
            needs_positions=True,
        )

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        taken: dict[str, str] = dict(state.get("taken", {}))
        pending: dict[str, dict] = {s: dict(v) for s, v in state.get("pending", {}).items()}
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))

        signals: list[Signal] = []
        markets_present = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 감지 + 리셋 — 반드시 관리(1단계)보다 먼저.
        for market in markets_present:
            if not snap.market_open.get(market, False):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            if session_date.get(market) == today_iso:
                continue
            session_date[market] = today_iso
            for s in [s for s in taken if market_of_symbol(s) == market]:
                taken.pop(s, None)
            for s in [s for s in last_reject if market_of_symbol(s) == market]:
                last_reject.pop(s, None)
            # 체결이 확인되지 않은 pending 은 하루를 넘기지 않는다.
            for s in [
                s for s in pending
                if market_of_symbol(s) == market and s not in snap.lots
            ]:
                pending.pop(s, None)

        # 1) 보유 관리 — 방어선의 정본은 **브로커 lot**(모듈 docstring "상태를 두
        #    갈래로" 절)이다. 껍질이 `snap.lots`로 돌려주므로 재시작해도 그대로다.
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not snap.market_open.get(market, False):
                continue
            lot = snap.lots.get(symbol)
            if lot is None:
                pending.pop(symbol, None)  # 신호를 냈지만 체결되지 않았다 — 정리.
                continue
            if lot.get("entry"):
                # lot 에 방어선이 실렸다 = 영속됐다. 로컬 폴백은 버린다(정본 하나).
                pending.pop(symbol, None)
            else:
                # 체결 직후 state_update 반영 전의 한 사이클 창 — 같은 프로세스
                # 안에서만 유효한 폴백. 재시작 뒤라면 여기도 비어 있고, 그때는
                # 방어선을 지어내지 않고 건너뛴다.
                lot = pending.get(symbol)
                if not lot:
                    last_reject[symbol] = "보유 중이나 랏 방어선 없음 — 관리 불가"
                    continue
            signal = self._manage(symbol, lot, market, snap)
            if signal is not None:
                signals.append(signal)

        # 2) 진입
        for market in markets_present:
            if not snap.market_open.get(market, False):
                continue
            # KR 15:20~15:30 동시호가에서는 현재가로 체결할 수 없다(모듈 docstring).
            if not in_continuous_session(market, snap.now):
                continue
            today = snap.now.astimezone(market_tz(market)).date()
            today_iso = today.isoformat()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                # 보유 중(`snap.lots`)이면 진입 평가 자체를 하지 않는다 — `taken`이
                # 재시작으로 날아가도 중복 진입이 나지 않는 이유다.
                if symbol in snap.lots or symbol in pending:
                    continue
                if taken.get(symbol) == today_iso:
                    last_reject[symbol] = "1일 1회 진입 소진"
                    continue
                signal = self._evaluate_entry(symbol, market, snap, today, pending, last_reject)
                if signal is not None:
                    taken[symbol] = today_iso
                    signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date, "taken": taken,
                "pending": pending, "last_reject": last_reject,
            },
        )

    # ------------------------------------------------------------------ 진입

    @staticmethod
    def _session_bars(bars: pd.DataFrame, market: str, today: dtdate) -> pd.DataFrame:
        """오늘 **연속 거래 시작 이후**의 봉만. 날짜만으로 거르면 프리마켓 봉이
        섞여 "세션 신고가"와 VWAP 앵커가 오염된다(`scalp_1m._session_bars`와 같은
        이유). 세션 시작 시각은 `quant.core.session.continuous_window`에서 가져온다
        — 세션 모델의 출처를 전략마다 새로 정의하지 않는다."""
        tz = market_tz(market)
        local = bars.index.tz_convert(tz)
        start_t, _ = continuous_window(market)
        return bars[(local.date == today) & (local.time >= start_t)]

    def _evaluate_entry(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        pending: dict[str, dict], last_reject: dict[str, str],
    ) -> Signal | None:
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            last_reject[symbol] = "5분봉 없음"
            return None
        session = self._session_bars(bars, market, today)
        if session.empty:
            last_reject[symbol] = "오늘 세션 5분봉 없음"
            return None

        setup, reject = self._detect_setup(session, bars)
        if setup is None:
            last_reject[symbol] = reject
            return None

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            last_reject[symbol] = "현재가 없음"
            return None
        entry = float(quote.price)

        ratio = atr_ratio(bars, self.atr_period)
        if ratio is None:
            # 손절선을 정할 수 없는 자리는 진입하지 않는다(structure.py 손절 철학).
            last_reject[symbol] = f"ATR 계산 불가(5분봉 {len(bars)}개)"
            return None
        atr_abs = ratio * float(bars["close"].iloc[-1])

        pullback_low = setup["pullback_low"]
        stop = pullback_low - self.atr_buffer_mult * atr_abs
        if stop >= entry:
            last_reject[symbol] = "손절가 계산 불가(진입가 이상)"
            return None
        # **최소 손절폭 게이트**(2026-08-29 실전 첫날 결함 수리): EMA9 터치의
        # 얕은 되돌림에서는 되돌림 저점이 현재가 바로 아래라 손절폭이 사실상
        # 0이 된다 — NOW 실사고: 진입 142.80 / 손절 142.75(**3.5bp**), 17초 만에
        # 손절. 왕복 비용 20bp+ 에 손절폭 3.5bp 는 진입 순간 지는 구조다.
        # 손절폭이 이 문턱에 못 미치는 자리는 "손절선을 정할 수 없는 자리"와
        # 같다 — 진입하지 않는다(structure.py 손절 철학).
        stop_bp = (entry - stop) / entry * 1e4
        if stop_bp < self.min_stop_bp:
            last_reject[symbol] = (
                f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp — "
                "되돌림 저점이 너무 가깝다(노이즈 안)"
            )
            return None
        target = entry + setup["width"] * self.target_mult

        # **열린 랏의 방어선** — 정본은 여기(`state_update`)로 나가 루프가 체결을
        # 확인한 뒤에만 `Position.meta["lots"]`에 쓴다(`loop.py:412-422`). 다음
        # 사이클부터 `snap.lots`로 회수되므로 프로세스 재시작을 견딘다(모듈
        # docstring "상태를 두 갈래로 흘린다" 절).
        defenses = {
            "entry": entry, "stop": stop, "target": target,
            "session": today.isoformat(), "entered_at": snap.now.isoformat(),
            "impulse_high": setup["impulse_high"], "pullback_low": pullback_low,
        }
        # 같은 값의 로컬 사본 — 체결 확인~lot 반영 사이 한 사이클의 폴백일 뿐이다.
        pending[symbol] = dict(defenses)
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"눌림목 임펄스 진입: {symbol} w={self.target_weight:.2f} "
                f"임펄스 {setup['impulse_bp']:.0f}bp "
                f"({fmt_price(setup['impulse_low'], symbol)}→"
                f"{fmt_price(setup['impulse_high'], symbol)}) "
                f"되돌림 {setup['retrace'] * 100:.0f}%{setup['anchor_note']} "
                f"손절={fmt_price(stop, symbol)} 목표={fmt_price(target, symbol)}"
            ),
            stop=stop,
            target=target,
            state_update={**defenses, "strategy": self.id},
        )

    def _detect_setup(
        self, session: pd.DataFrame, full: pd.DataFrame
    ) -> tuple[dict | None, str]:
        """임펄스→되돌림→반등 돌파 판정. 매치면 (setup dict, ""), 아니면 (None, 사유).

        상태를 읽지도 쓰지도 않는 순수 계산이다 — 마지막 봉(`session.iloc[-1]`)이
        돌파 판정 대상이고, 그 앞(`prior`)에서 임펄스와 되돌림을 찾는다."""
        if len(session) < 4:
            # 임펄스봉 + 되돌림봉 + 반등봉 + 돌파봉 = 최소 4봉.
            return None, f"세션 5분봉 부족({len(session)}개 < 4)"

        prior = session.iloc[:-1]
        last = session.iloc[-1]

        # 1) 임펄스 — 세션 신고가(prior 최고가)와 그 직전 저점.
        peak_pos = int(prior["high"].to_numpy().argmax())
        impulse_high = float(prior["high"].iloc[peak_pos])
        leg = prior.iloc[: peak_pos + 1]
        low_pos = int(leg["low"].to_numpy().argmin())
        impulse_low = float(leg["low"].iloc[low_pos])
        width = impulse_high - impulse_low
        if width <= 0 or impulse_low <= 0:
            return None, "임펄스 폭 0"
        impulse_bp = width / impulse_low * 1e4
        if impulse_bp < self.min_impulse_bp:
            return None, f"임펄스 부족({impulse_bp:.0f}bp < {self.min_impulse_bp:g}bp)"

        # 2) 되돌림 — 고점 이후 구간의 최저가.
        pb = prior.iloc[peak_pos + 1:]
        if pb.empty:
            return None, "되돌림 구간 없음(고점이 직전 봉)"
        pl_pos = int(pb["low"].to_numpy().argmin())
        pullback_low = float(pb["low"].iloc[pl_pos])
        if pullback_low <= impulse_low:
            return None, "되돌림이 임펄스 시작을 깼다(구조 무효)"
        retrace = (impulse_high - pullback_low) / width
        anchor_note = ""
        if retrace > self.pullback_max_pct:
            # 상한은 VWAP 터치로도 면제되지 않는다(모듈 docstring 규칙 2).
            return None, f"되돌림 과다({retrace:.2f} > {self.pullback_max_pct:g})"
        if retrace < self.pullback_min_pct:
            anchor = self._touched_anchor(session, full, pb.index[pl_pos], pullback_low)
            if anchor is None:
                return None, (
                    f"되돌림 부족({retrace:.2f} < {self.pullback_min_pct:g}) "
                    "· VWAP/EMA 터치 없음"
                )
            anchor_note = f"({anchor} 터치)"

        # 3) 거래량 소진 — 봉당 평균으로 비교(모듈 docstring 규칙 3).
        impulse_vol = float(leg.iloc[low_pos:]["volume"].mean())
        pullback_vol = float(pb.iloc[: pl_pos + 1]["volume"].mean())
        if not pullback_vol < impulse_vol:
            return None, f"되돌림 거래량 미소진({pullback_vol:.0f} ≥ {impulse_vol:.0f})"

        # 4) 첫 반등봉의 고가 돌파.
        after = pb.iloc[pl_pos + 1:]
        rebound = after[after["close"] > after["open"]]
        if rebound.empty:
            return None, "반등 5분봉 아직 없음"
        rebound_high = float(rebound["high"].iloc[0])
        if float(last["high"]) <= rebound_high:
            return None, f"반등봉 고가 미돌파(고가 {rebound_high:g})"

        return {
            "impulse_high": impulse_high, "impulse_low": impulse_low, "width": width,
            "impulse_bp": impulse_bp, "pullback_low": pullback_low, "retrace": retrace,
            "rebound_high": rebound_high, "anchor_note": anchor_note,
        }, ""

    def _touched_anchor(
        self, session: pd.DataFrame, full: pd.DataFrame, ts, pullback_low: float
    ) -> str | None:
        """되돌림 저점이 세션 VWAP 또는 EMA 를 터치했는가 — 터치한 앵커 이름,
        아니면 None. VWAP 은 정의상 세션 앵커라 오늘 봉만으로, EMA 는 워밍업이
        필요하므로 연속 시계열(`full`) 전체로 계산한다(`scalp_1m`의 MA60 과 같은
        이유)."""
        vwap = self._session_vwap(session, ts)
        if vwap is not None and pullback_low <= vwap:
            return "VWAP"
        ema_at = ema(full["close"], self.ema_period).get(ts)
        if ema_at is not None and pd.notna(ema_at) and pullback_low <= float(ema_at):
            return f"EMA{self.ema_period}"
        return None

    @staticmethod
    def _session_vwap(session: pd.DataFrame, ts) -> float | None:
        """`ts` 봉까지의 누적 세션 VWAP(전형가 기준). 거래량 0이면 None."""
        upto = session.loc[:ts]
        volume = float(upto["volume"].sum())
        if volume <= 0:
            return None
        typical = (upto["high"] + upto["low"] + upto["close"]) / 3.0
        return float((typical * upto["volume"]).sum() / volume)

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`Clock._should_flatten`(quant/core/clock.py) 재현 — 스냅샷이 주는
        원재료(`minutes_to_close`, `cadence_minutes`)로 같은 공식을 계산한다."""
        mtc = snap.minutes_to_close.get(market)
        if mtc is None or mtc <= 0:
            # mtc <= 0 = 연속 거래 종료(동시호가) — 원본과 동일하게 False.
            return False
        return mtc - snap.cadence_minutes < self.flatten_minutes

    def _manage(
        self, symbol: str, lot: Mapping[str, Any], market: str, snap: StrategySnapshot
    ) -> Signal | None:
        """`lot`은 브로커 포지션의 lot(`snap.lots[symbol]`)이거나, 체결 직후 한
        사이클의 로컬 폴백(`pending`)이다. **읽기만 한다** — 이 전략은 진입 후
        방어선을 움직이지 않으므로(트레일 없음) 쓰기 경로 자체가 필요 없다.

        `stop`/`target`이 없는 lot 도 방어적으로 다룬다: 그 판정만 건너뛰고
        세션 롤·EoD·타임아웃 레일은 계속 작동시킨다 — 값이 없다고 포지션을
        관리 밖으로 내보내는 것이 더 위험하다."""
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])
        stop = float(lot["stop"]) if lot.get("stop") is not None else None
        target = float(lot["target"]) if lot.get("target") is not None else None

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
        if lot.get("session") and lot["session"] != today_iso:
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): 진입 {lot['session']} "
                f"현재={fmt_price(price, symbol)}"
            )
        if self._should_flatten(market, snap):
            return _exit(
                f"EoD 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}"
            )
        if stop is not None and price <= stop:
            return _exit(
                f"손절(되돌림 저점 −{self.atr_buffer_mult:g}ATR): "
                f"entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        if target is not None and price >= target:
            return _exit(
                f"목표 도달(임펄스 폭 x{self.target_mult:g}): "
                f"entry={fmt_price(entry, symbol)} 목표={fmt_price(target, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )
        entered_at = lot.get("entered_at")
        if entered_at:
            held = snap.now - datetime.fromisoformat(entered_at)
            if held >= timedelta(minutes=self.timeout_minutes):
                return _exit(
                    f"타임아웃 청산({self.timeout_minutes:g}분): "
                    f"entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)} "
                    f"보유={held.total_seconds() / 60:.0f}분"
                )
        return None


class PullbackImpulseShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 다른 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `DonchianPureShell`/`Scalp1mPureShell`과 동일 패턴. 레지스트리 배선은
    이 파일 밖(`quant/trade/strategy/__init__.py`)에서 한다."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "pullback_impulse",
    ):
        super().__init__(PullbackImpulsePureStrategy(symbols, params, market=market, id=id))
