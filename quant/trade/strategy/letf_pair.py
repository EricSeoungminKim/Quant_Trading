"""레버리지 ETF 페어 전환(LETF pair trend switch, Family F1) — 지수/섹터 3배
ETF 페어(TQQQ/SQQQ, SOXL/SOXS 등)를 신호 심볼(U)의 추세 방향으로 갈아타는
15분봉 전략. **순수 계약 전용 신규 전략**(레거시 쌍둥이 없음).

이 파일은 `scratchpad/letf_spec.md`의 Family F1("trend switch v1")을 **한 글자도
바꾸지 않고 그대로** 구현한다 — 독립 백테스터(`quant-backtest` 저장소)가 같은
규칙을 별도로 구현해 거래 단위로 교차검증하기 때문이다(스펙 "Cross-check
protocol"). 규칙을 바꾸고 싶으면 스펙 문서를 먼저 바꾸고 두 구현을 함께 고칠 것.

## 규칙

세 심볼 — `signal_symbol`(U, 신호 판정 전용, 매매하지 않음), `long_symbol`(L, 상승
시 매수하는 3배 롱 ETF), `short_symbol`(S, 하락 시 매수하는 3배 인버스 ETF). 롱 온리
계좌이므로 "숏"은 인버스 ETF를 **매수**하는 것으로 표현한다(`intraday_momentum.py`와
같은 신호/체결 분리 패턴). 세 심볼은 반드시 같은 시장이어야 한다(생성자 검증).

U의 `interval_minutes`(M)분봉(연속 세션만, 날짜를 넘겨 이어지는 하나의 시계열)에서:

- `ema_f = EMA(close, n_fast)`, `ema_s = EMA(close, n_slow)` (n_fast < n_slow)
- `atr = ATR(n_atr)`(Wilder 평활, 가격 단위) — `quant.trade.indicators.trend_gate.
  atr_ratio`(ATR/종가 비율)를 재사용해 종가를 곱해 되돌린다(코드 중복 방지, 정의는
  동일한 Wilder 재귀).
- `vwap` = **세션 앵커드** VWAP(오늘 세션 봉만 누적, typical=(고+저+종)/3) — EMA/ATR과
  달리 날짜를 넘기지 않는다.
- `strength = (ema_f − ema_s) / atr`

방향(완성봉 t 기준): `UP`(ema_f>ema_s, close>vwap, strength≥k_min) /
`DOWN`(ema_f<ema_s, close<vwap, −strength≥k_min) / 그 외 `NEUTRAL`.

**진입**(무포지션일 때만, 진입창 `[session_open+warmup_min, session_close−no_entry_min]`
안에서만): UP→L 매수, DOWN→S 매수. `day_filter=true`면 추가로 그날 갭(`|시가/전일종가
−1|≥gap_min`) 또는 개장 30분 레인지가 일봉 ATR14의 `or_atr_min`배 이상이어야 한다.
`max_entries_per_day`로 하루 진입 횟수를 제한한다.

**손절**(`stop_mode`, 기본 `"atr"`, 2026-09-05 소유자 청산 규칙 확장으로 선택형이
됨): `"atr"`이면 `stop_X = entry_X × (1 − 3×stop_atr_mult×atr/close_U)`(U의 ATR/종가
비를 3배 레버리지로 환산, 기존 산식과 100% 동일 — 건드리지 않으면 동작이 안 바뀐다).
`"pct"`면 `stop_X = entry_X × (1 − stop_pct)`(기본 3%, 진입가 대비 고정 비율 — 리서치
시뮬레이터 정의 그대로). 두 모드 다 `(entry−stop)/entry < min_stop_bp/1e4`면 진입
거부("손절폭 미달"). 매 사이클 held ETF의 현재가와 비교해 재확인한다(엔진의 기존
손절 시맨틱과 동일 — `price <= stop`). `trail_atr_mult>0`이면 완성봉마다
`stop_X = max(stop_X, price_X×(1−3×trail_atr_mult×atr/close_U))`로 갱신을 시도한다
(영속화 한계는 "아직 못 하는 것" 참고).

**익절 사다리**(2026-09-05 소유자 청산 규칙 확장, 전부 기본 0/false=비활성 — 건드리지
않으면 동작이 안 바뀐다): 1단계 `tp1_pct`(예 0.03) 도달 시 `SCALE_OUT`(비중
`tp1_fraction`, 기본 0.5), 2단계 `tp2_pct`(예 0.05) 도달 시 잔량 전량 `EXIT`. 목표가는
퍼센트 대신 ATR 스케일로도 정의할 수 있다(`tp_atr_mult`, 기본 0) —
`target1_X = entry_X × (1 + 3×tp_atr_mult×atr/close_U)`, `target2`는 그 거리의 2배.
`tp1_pct`와 `tp_atr_mult`는 같은 1단계를 두 방식으로 정의하는 것이라 **동시에 설정하면
생성자가 거부한다**(`ValueError`). `tp2_pct`만 켜고 `tp1_pct`/`tp_atr_mult`를 다
꺼두면 사다리 없이 2단계가 곧장 전량 청산이 된다 — 이 경우에만 `ENTER` 신호의
`Signal.target`에 그 목표가를 실어 백테스트 봉내 체결기(`--fill-model intrabar`)가
그 자리에서 체결시킬 수 있게 한다. 사다리가 있으면(1단계가 존재) `target`을 비워
둔다 — 봉내 체결기는 단일 stop/target 쌍만 알아 부분청산을 표현할 수 없기 때문이다
(**정직한 한계**: 1단계 부분청산과 그 이후 판정은 사이클마다 재평가되는 시세 기반
경로에만 의존하고, 봉내 정밀 체결의 혜택을 받지 못한다).

`tp_floor_exit`(사다리가 켜져 있으면 기본 `true`, 명시적으로 끄면 존중): 1단계가 체결된
뒤(`SCALE_OUT`의 `state_update`로 `tp1_price`를 랏에 기록 — **체결 확인 후에만** 반영,
`entry`/`stop`과 같은 영속화 경로) 매 사이클 잔량 ETF의 **현재가(quote)**가
`tp1_price` 아래로 떨어지면 잔량을 전량 청산한다(사유 "부분익절 가격 이탈"). **이건
시세 기반이지 봉 종가 기반이 아니다** — 리서치 시뮬레이터는 완성봉 종가로 판정하므로
작은 괴리가 생긴다(아래 "백테스터와의 합의점"에 명시). `tp_floor_to_entry`(기본
`false`): 켜면 1단계 체결과 같은 사이클에 손절선을 진입가로 올린다(같은
`state_update`로 `stop`을 갱신).

**신호 청산/전환**: 보유 방향과 반대 방향이 뜨면 청산, `switch=true`면 같은 `decide()`
호출에서 반대 ETF 진입도 함께 낸다(진입 게이트를 그대로 통과해야 한다 — 아래
"백테스터와의 합의점" 참고). `exit_on_neutral=true`면 NEUTRAL에서도 청산(전환 없음).

**재진입 쿨다운**: 손절로 청산된 뒤 **같은 방향** 재진입은 `cooldown_bars`개의 완성봉이
지나야 허용(그 시점에 방향 조건이 다시 성립해야 함 — 매 사이클 새로 판정하므로 자동
충족). **반대 방향**은 쿨다운 없이 즉시 허용(하루 진입 상한만 적용).

**마감 처리**(`overnight=false`, 기본): `kernel.should_flatten_dual`로 `eod_exit_min`
전 강제청산 + 세션 롤 시 오버나잇 잔여 랏 강제청산(`kernel.is_overnight_carry`).
`overnight=true`(연구용, 기본 미배포)면 두 청산 모두 건너뛴다.

**승률 게이트**(`win_table`, 선택): 조립 평면(`quant/apps/assembly.py`)이 워크포워드
JSON을 읽어 생성자에 주입한다(이 파일은 파일을 열지 않는다). 버킷 =
`{regime(above/below, U 일봉종가 vs SMA20 through yesterday)}|{entry_hour_bucket}|
{|strength| tercile}`. 버킷이 있고 `n≥30`이고 `mean_bp≤0`이면 진입 거부
("win-table: bucket negative"). 버킷이 없으면(테이블 자체가 없거나, 그 조합의 표본이
없거나, 지표 계산 불가) 통과(로그만) — 스펙 "missing bucket → allow"과 동일.

## 데이터

- `(signal_symbol, f"{M}m", ≥3×n_slow)` — EMA/ATR/VWAP/OR30 전부 이 한 시계열에서
  계산한다. 3×n_slow는 EMA(n_slow) 워밍업 + 개장 레인지·세션 슬라이싱 여유를 넉넉히
  덮는다(기본 M=15/n_slow=21 → 63봉 ≈ 2.4세션).
- `(signal_symbol, "1d", 30)` — `day_filter=true` **또는** `win_table`이 주입됐을 때만
  요청한다(day_filter의 ATR14·전일종가, win_table의 SMA20 국면 판정 둘 다 일봉이
  필요하다). 둘 다 꺼져 있으면 요청하지 않는다(콜드 페치 절약).
- `quotes`: `symbols`(signal/long/short 중복 제거) 전부. L/S는 진입가·손절 재확인에
  실제로 쓰고, U는 스펙이 명시한 대로 요청하되(향후 관측성/디버그용) 방향 판정
  자체는 U의 **봉** 종가를 쓴다(라이브 사이클 시점의 U 호가가 아니다).
- `needs_positions=True`.

## 상태가 두 갈래로 흐른다

| 키 | 무엇 | 어디로 | 재시작 |
|---|---|---|---|
| `session_date`/`entries_today`/`last_reject` | 하루짜리 카운터·사유 | `next_state` | 잃어도 무해(다음 사이클 하루 안에서 재구성) |
| `last_stop` | `{"long"/"short": 마지막 손절 시점 U 봉 타임스탬프}` — 쿨다운 판정용 | `next_state` | **잃는다**(재시작하면 쿨다운이 리셋된다 — 아래 한계 참고) |
| `entry`/`stop`/`direction`/`session`/`entered_at` | 보유 랏의 방어선 | `Signal.state_update` → 체결 후 `Position.meta["lots"]` → 다음 사이클 `snap.lots` | **살아남는다** |

## 아직 못 하는 것 (정직하게)

1. **성과 근거가 없다.** 이 파일은 스펙을 코드로 옮긴 것이고, walk-forward/deflated
   Sharpe/비용 2배 생존 검증은 별도(`quant-backtest`) 저장소의 몫이다. `settings.yaml`의
   두 블록 모두 `enabled: false`, `validation.status: burn_in`으로 들어간다.
2. **트레일 스탑은 재시작에서 살아남지 않는다.** 순수 계약에서 `Signal.state_update`는
   **체결(fill)이 실제로 난 신호에만** 적용된다(`quant/trade/loop.py
   _execute_signal` — "체결 확인 후에만 전략 상태를 적용한다"). 트레일 갱신은 새 주문을
   내지 않으므로 이 경로를 못 탄다 — 그래서 `trail_atr_mult>0`일 때 매 사이클
   `max(저장된 stop, 즉석 계산한 트레일 후보)`를 **그 사이클의 손절 판정에만** 쓰고,
   랏에는 쓰지 않는다. 재시작하면 트레일 이력이 사라지고 entry 시점 손절로 되돌아간다
   (다른 순수 전략들의 "재시작으로 날아가는 상태"와 같은 종류의 한계). 기본값
   `trail_atr_mult=0`(꺼짐)이라 배포 초기 설정에서는 발동하지 않는다.
3. **트레일/재확인 손절의 `close_X`는 봉 종가가 아니라 현재가(quote)다.** `DataNeeds`가
   L/S의 **봉**은 요청하지 않는다(요청하면 콜드 페치가 3배로 는다 — 국내 반영 판단은
   quote로 충분하다는 다른 전략들의 관례를 그대로 따른다). 스펙의 `close_X`는 독립
   백테스터에서는 실제 완성봉 종가지만, 이 라이브 구현에서는 그 근사인 사이클 시점
   현재가를 쓴다 — 아래 "백테스터와의 합의점" 참고.
4. **`last_stop`(쿨다운 타이머)은 재시작으로 날아간다.** `next_state`에만 있고 랏에
   실을 수 없다(청산된 뒤에는 `snap.lots`에 그 심볼이 더 이상 없다 — 랏은 "열린
   포지션"에만 존재). 재시작 직후에는 쿨다운이 없던 것처럼 동작한다(안전측 — 재진입을
   추가로 막는 게 아니라 덜 막는 방향의 오차다).
5. **`day_filter`의 OR30(개장 30분 레인지)은 `interval_minutes`가 30을 나누어떨어지지
   않으면 근사가 깨진다.** 봉 개수 기준(`round(30/M)`)으로 잘라서, M=5/15/30이면 정확히
   30분이지만 M=60이면 첫 60분봉 하나(OR60)를 대신 쓴다 — 스펙 그리드에 M=60이
   있으므로 실제로 일어날 수 있는 근사다.
6. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 신호/롱/숏 3개 심볼만
   선언한다 — 파라미터를 바꿔 재배포하면 이전 심볼의 잔여 포지션은 보이지 않는다
   (`intraday_momentum.py`와 같은 한계).
7. **익절 사다리(1단계)의 부분청산은 봉내 체결기의 혜택을 받지 못한다.** `Signal.target`
   은 2단계가 곧장 전량 청산일 때만 실린다(`_tp2_is_full_exit`) — 사다리가 있으면
   `target`이 비고, 1단계 부분청산·플로어 청산 전부 매 사이클 재평가되는 시세 기반
   경로에만 의존한다. 봉 안에서 목표가를 스치고 되돌아온 경우를 놓칠 수 있다(2번
   항목의 트레일 스탑과 같은 종류의 한계).
8. **`tp_floor_exit`는 완성봉 종가가 아니라 시세(quote) 기반이다.** 아래 "백테스터와의
   합의점"에 그 괴리를 명시한다.

## 백테스터와의 합의점

독립 백테스터(`quant-backtest`)가 트레이드 단위로 교차검증하므로, 각 규칙이 이 라이브
구현에서 **정확히 무엇으로 실현되는지** 여기 적는다 — 백테스터는 이 목록과 같게 맞출 것.

- **"완성봉 t에서 판단, 체결은 bar t+1의 open"**: 라이브 엔진은 봉이 새로 생길 때까지
  기다리지 않는다 — `decide()`는 10초 주기 사이클마다 불리고, 그 시점에 U의 **가장 최근
  완성봉**(=봉 t)으로 방향을 판단해 신호를 낸다. 신호가 나가면 엔진이 즉시 시장가
  주문을 내고 **다음 시세(quote)**로 체결된다 — 이건 이 저장소의 다른 모든 순수 전략과
  동일한 근사다(`trend_day.py`/`intraday_momentum.py` 등도 "봉 t 완성 → 신호 →
  다음 사이클 체결"). 봉 t가 막 완성된 시점의 `snap.now`는 봉 t+1의 open 시각과
  사실상 같으므로(주기가 봉 간격보다 훨씬 짧다), **entry_X/stop_X는 봉 t+1의 open이
  아니라 신호가 나간 사이클의 quote 가격**으로 실현된다. 백테스터는 이를 "bar t+1의
  open"으로 정확히 재현하면 되고, 두 값의 괴리는 사이클 지연(수 초) 수준이라
  스펙의 tolerance(±1bp) 안에 들어온다.
- **EMA/ATR는 날짜를 넘겨 이어지는 연속 봉 시계열**(`snap.bars[(U, f"{M}m")]`)에서
  계산한다 — 세션 경계에서 리셋하지 않는다. VWAP만 오늘 세션 봉으로 리셋한다
  (`_session_slice` + `_session_vwap`).
- **ATR은 Wilder**: `trend_gate.atr_ratio`(TR을 첫 `n_atr`개 단순평균으로 시드한 뒤
  `(이전×(n_atr−1)+현재)/n_atr` 재귀) × 마지막 종가. `n_atr+1`개 미만이면 `None`
  (지표 계산 불가 → 진입 거부, 관리 중이면 신호 기반 청산만 보류 — 손절/EoD는 영향
  없음).
- **손절 재확인은 봉 내부 워크가 아니라 "quote ≤ stop"**: 스펙의 "intrabar 워크"(open
  갭 우선, 다음 low 확인)는 백테스터의 `--fill-model intrabar` 몫이다. 라이브는 10초
  주기로 quote를 보므로 사실상 같은 보수적 결과로 수렴한다(갭이 발생하면 다음 사이클
  quote가 이미 stop 아래일 것이고, 그 즉시 청산된다 — "open ≤ stop → open에서 청산"과
  같은 방향의 근사).
- **스위치(반대 진입)도 신규 진입과 동일한 게이트를 전부 통과해야 한다** — 이건 스펙이
  명시하지 않은 부분이라 이 구현이 내린 결정이다: 진입창(`warmup_min`/`no_entry_min`),
  `day_filter`, `min_stop_bp`, `win_table` 전부 스위치의 반대편 진입에도 적용한다.
  근거: 스펙이 "same bar; counts as an entry"라고 명시했고("진입으로 센다"), 마감
  직전 스위치를 예외로 두면 진입 직후 바로 EoD 청산되는 무의미한 왕복이 생긴다.
  게이트 중 하나라도 막히면 **청산은 그대로 나가고 반대 진입만 생략**한다(전량
  현금화, 스위치가 아니라 단순 청산이 된다) — `last_reject`에 사유가 남는다.
  백테스터는 이 규칙을 그대로 재현해야 한다.
- **쿨다운은 방향별로, "완성봉 개수"로 센다**: `last_stop[direction]`에 손절이 난 시점의
  U 봉 타임스탬프를 저장하고, 그보다 늦은(strictly after) 완성봉 개수를 센다. 세션
  경계에서 리셋하지 않는다(EMA/ATR과 같은 "날짜를 넘겨 이어지는 시계열" 전제).
- **day_filter의 ATR14는 별도 지표다** — F1의 `n_atr`(손절폭용, M분봉)과 무관하게
  **항상 14, 일봉** 기준이며 같은 Wilder 정의(`_wilder_atr(daily, 14)`)를 쓴다. 이는
  스펙이 "ATR14_daily"라고 명시적으로 이름 붙인 것을 그대로 따른 것이다.
- **win_table의 지역 시각 버킷은 그 심볼 시장의 로컬 타임존**(U가 US면 America/New_York)
  이다 — `market_tz(market)`.
- **`tp_floor_exit`(1단계 이후 플로어 청산)는 완성봉 종가가 아니라 매 사이클의
  시세(quote)로 판정한다** — 다른 손절/트레일 재확인과 같은 관례(quote가 완성봉
  종가의 근사)를 그대로 따른 것이다. 리서치 시뮬레이터가 "그 뒤 완성된 M분봉의
  종가가 `tp1_price` 아래면 청산"으로 판정한다면, 봉 안에서 잠깐 `tp1_price`
  아래로 찍혔다가 그 봉 종가는 다시 위로 회복하는 경우 두 구현의 판정이 갈린다
  (라이브는 quote 저점에서 즉시 청산, 시뮬레이터는 봉 종가 기준이라 청산 안 함) —
  **작지만 실재하는 괴리**로 알려둔다(모듈 docstring "아직 못 하는 것" 8번).
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from datetime import time as dtime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import ema, sma
from quant.trade.indicators.trend_gate import atr_ratio
from quant.trade.strategy import kernel
from quant.trade.strategy.shell import PureStrategyShell

_DEFAULT_INTERVAL_MINUTES = 15
_OR30_MINUTES = 30.0
_WIN_TABLE_REGIME_SMA_DAYS = 20
_DAY_FILTER_ATR_PERIOD = 14

# 승률 게이트 진입 시각대 버킷(스펙 "Win table" 절, 문자 그대로) — 그 심볼 시장의
# 로컬 시각으로 비교한다.
_WIN_TABLE_HOUR_BUCKETS = (
    (dtime(9, 30), dtime(10, 30), "09:30-10:30"),
    (dtime(10, 30), dtime(12, 0), "10:30-12:00"),
    (dtime(12, 0), dtime(14, 0), "12:00-14:00"),
    (dtime(14, 0), dtime(15, 30), "14:00-15:30"),
)


# ---------------------------------------------------------------- 순수 헬퍼

def _session_slice(bars: pd.DataFrame, market: str, day: dtdate) -> pd.DataFrame:
    """`day`의 연속 거래 개장 이후 봉만 — VWAP·개장 레인지가 프리마켓 봉으로
    오염되지 않게 한다(`trend_day.py`/`intraday_momentum.py`와 같은 필터)."""
    tz = market_tz(market)
    open_t, _ = continuous_window(market)
    local = bars.index.tz_convert(tz)
    return bars[(local.date == day) & (local.time >= open_t)]


def _session_vwap(session_bars: pd.DataFrame) -> float | None:
    """세션 시작부터의 누적 VWAP(마지막 값). 전형가=(고+저+종)/3, 룩어헤드 없음
    (`trend_day.session_vwap`과 동일 정의)."""
    if session_bars is None or session_bars.empty:
        return None
    tp = (session_bars["high"].astype(float) + session_bars["low"].astype(float)
          + session_bars["close"].astype(float)) / 3.0
    vol = session_bars["volume"].astype(float)
    total = float(vol.sum())
    if not (total > 0):
        return None
    value = float((tp * vol).sum() / total)
    return None if pd.isna(value) else value


def _wilder_atr(bars: pd.DataFrame, period: int) -> float | None:
    """Wilder 평활 ATR(단일 값, 절대 가격 단위). `trend_gate.atr_ratio`(ATR/종가
    비율)를 재사용해 마지막 종가를 곱해 되돌린다 — Wilder 재귀 정의를 중복
    구현하지 않는다. 데이터 부족/NaN이면 `None`(모듈 docstring "데이터" 절)."""
    ratio = atr_ratio(bars, period)
    if ratio is None:
        return None
    if bars is None or bars.empty:
        return None
    last_close = float(bars["close"].astype(float).iloc[-1])
    if pd.isna(last_close) or last_close <= 0:
        return None
    return float(ratio) * last_close


def _entry_hour_bucket(local_time: dtime) -> str | None:
    """win_table 진입 시각대 버킷 — 어느 창에도 안 들면 `None`(버킷 없음=통과)."""
    for start, end, label in _WIN_TABLE_HOUR_BUCKETS:
        if start <= local_time < end:
            return label
    return None


def _strength_tercile(abs_strength: float, edges: Any) -> str | None:
    """`|strength|`의 tercile("t1"/"t2"/"t3") — `edges`(2개 경계값) 형식이 아니면
    `None`(버킷 없음=통과)."""
    if not edges:
        return None
    try:
        edges_list = list(edges)
        e1, e2 = float(edges_list[0]), float(edges_list[1])
    except (TypeError, ValueError, IndexError):
        return None
    if abs_strength < e1:
        return "t1"
    if abs_strength < e2:
        return "t2"
    return "t3"


# ---------------------------------------------------------------- 전략

class LetfPairPureStrategy:
    """모듈 docstring 참고. `decide()`는 `snap`/`state`만 본다.

    `win_table`은 조립 평면(`quant/apps/assembly.py`)이 `win_table_path` 설정을
    읽어 JSON을 파싱한 뒤 넘기는 값이다 — 이 전략은 파일을 열지 않는다(플레인
    규칙, 모듈 docstring "승률 게이트" 절)."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "letf_pair", win_table: dict | None = None,
    ):
        self.id = id
        self.market = market  # Protocol 호환용 — 실제 판정은 아래 self._market

        self.signal_symbol: str = str(params.get("signal_symbol", "QQQ")).strip()
        self.long_symbol: str = str(params.get("long_symbol", "TQQQ")).strip()
        self.short_symbol: str = str(params.get("short_symbol", "SQQQ")).strip()
        if not self.signal_symbol or not self.long_symbol or not self.short_symbol:
            raise ValueError("signal_symbol/long_symbol/short_symbol은 비어 있을 수 없습니다.")
        if self.long_symbol == self.short_symbol:
            raise ValueError("long_symbol과 short_symbol은 서로 달라야 합니다.")

        # 세 심볼은 같은 시장이어야 한다 — 세션 롤/EoD 청산 타이밍을 신호 심볼의
        # 시장 하나로 계산하기 때문이다(intraday_momentum.py와 같은 이유·검증).
        self._market = market_of_symbol(self.signal_symbol)
        if (market_of_symbol(self.long_symbol) != self._market
                or market_of_symbol(self.short_symbol) != self._market):
            raise ValueError(
                "signal_symbol/long_symbol/short_symbol은 같은 시장이어야 합니다 "
                "(세션 롤·EoD 청산 타이밍을 신호 심볼 시장 하나로 계산한다)."
            )
        self.symbols = list(dict.fromkeys((self.signal_symbol, self.long_symbol, self.short_symbol)))

        self.interval_minutes: int = int(params.get("interval_minutes", _DEFAULT_INTERVAL_MINUTES))
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes는 양수여야 합니다.")
        self._interval = f"{self.interval_minutes}m"

        # bar_interval_minutes는 risk/manager.py의 쿨다운 봉 간격 선언용 미러다
        # (2026-09-03 감사 C4 관례) — 선언됐다면 interval_minutes와 반드시 같아야
        # 한다(둘이 갈라지면 리스크 레일의 쿨다운 분 환산이 조용히 틀어진다).
        declared = params.get("bar_interval_minutes")
        if declared is not None and int(declared) != self.interval_minutes:
            raise ValueError(
                "bar_interval_minutes는 interval_minutes(M)와 같아야 합니다 "
                f"(interval_minutes={self.interval_minutes}, bar_interval_minutes={declared})."
            )

        self.n_fast: int = int(params.get("n_fast", 8))
        self.n_slow: int = int(params.get("n_slow", 21))
        if self.n_fast < 1 or self.n_slow < 1:
            raise ValueError("n_fast/n_slow는 1 이상이어야 합니다.")
        if self.n_fast >= self.n_slow:
            raise ValueError("n_fast는 n_slow보다 작아야 합니다.")

        self.n_atr: int = int(params.get("n_atr", 14))
        if self.n_atr < 1:
            raise ValueError("n_atr은 1 이상이어야 합니다.")

        self.k_min: float = float(params.get("k_min", 0.25))
        if self.k_min < 0:
            raise ValueError("k_min은 0 이상이어야 합니다.")

        self.stop_atr_mult: float = float(params.get("stop_atr_mult", 1.5))
        if self.stop_atr_mult <= 0:
            raise ValueError("stop_atr_mult는 양수여야 합니다.")

        self.trail_atr_mult: float = float(params.get("trail_atr_mult", 0.0))
        if self.trail_atr_mult < 0:
            raise ValueError("trail_atr_mult는 0(비활성) 이상이어야 합니다.")

        # 손절 산식 선택(2026-09-05, 소유자 청산 규칙 확장). 기본 "atr"는 기존
        # 산식(stop_atr_mult 기반)과 100% 동일 — 이 파라미터를 건드리지 않으면
        # 동작이 바뀌지 않는다. "pct"는 진입가 대비 고정 비율 손절
        # (stop_pct, 기본 3%) — 리서치 시뮬레이터의 정의를 그대로 따른다.
        # min_stop_bp 게이트는 두 모드 모두에 동일하게 적용된다(아래 `_try_entry`).
        self.stop_mode: str = str(params.get("stop_mode", "atr"))
        if self.stop_mode not in ("atr", "pct"):
            raise ValueError("stop_mode는 'atr' 또는 'pct'여야 합니다.")
        self.stop_pct: float = float(params.get("stop_pct", 0.03))
        if self.stop_pct <= 0:
            raise ValueError("stop_pct는 양수여야 합니다.")

        self.min_stop_bp: float = kernel.parse_min_stop_bp(params, default=40.0)

        self.warmup_min: float = float(params.get("warmup_min", 30.0))
        if self.warmup_min < 0:
            raise ValueError("warmup_min은 0 이상이어야 합니다.")
        self.no_entry_min: float = float(params.get("no_entry_min", 30.0))
        if self.no_entry_min < 0:
            raise ValueError("no_entry_min은 0 이상이어야 합니다.")
        self.eod_exit_min: float = float(params.get("eod_exit_min", 5.0))
        if self.eod_exit_min <= 0:
            # 0이면 마지막 in-session 사이클에서도 조건이 성립하지 않아 청산
            # 창이 통째로 사라진다(다른 순수 전략들과 같은 실사고 이력 방어).
            raise ValueError("eod_exit_min은 양수여야 합니다.")

        self.cooldown_bars: int = int(params.get("cooldown_bars", 2))
        if self.cooldown_bars < 0:
            raise ValueError("cooldown_bars는 0 이상이어야 합니다.")

        self.max_entries_per_day: int = int(params.get("max_entries_per_day", 3))
        if self.max_entries_per_day < 1:
            raise ValueError("max_entries_per_day는 1 이상이어야 합니다.")

        self.switch: bool = bool(params.get("switch", True))
        self.exit_on_neutral: bool = bool(params.get("exit_on_neutral", False))
        self.day_filter: bool = bool(params.get("day_filter", False))

        self.gap_min: float = float(params.get("gap_min", 0.005))
        if self.gap_min < 0:
            raise ValueError("gap_min은 0 이상이어야 합니다.")
        self.or_atr_min: float = float(params.get("or_atr_min", 0.8))
        if self.or_atr_min < 0:
            raise ValueError("or_atr_min은 0 이상이어야 합니다.")

        self.overnight: bool = bool(params.get("overnight", False))

        self.target_weight: float = float(params.get("target_weight", 0.5))
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 익절 사다리(2026-09-05, 소유자 청산 규칙 확장). 전부 기본 0(비활성) —
        # 건드리지 않으면 동작이 바뀌지 않는다. 모듈 docstring "익절 사다리" 절.
        self.tp1_pct: float = float(params.get("tp1_pct", 0.0))
        if self.tp1_pct < 0:
            raise ValueError("tp1_pct는 0(비활성) 이상이어야 합니다.")
        self.tp1_fraction: float = float(params.get("tp1_fraction", 0.5))
        if not 0 < self.tp1_fraction < 1:
            raise ValueError("tp1_fraction은 0 초과 1 미만이어야 합니다(부분청산).")
        self.tp2_pct: float = float(params.get("tp2_pct", 0.0))
        if self.tp2_pct < 0:
            raise ValueError("tp2_pct는 0(비활성) 이상이어야 합니다.")
        self.tp_atr_mult: float = float(params.get("tp_atr_mult", 0.0))
        if self.tp_atr_mult < 0:
            raise ValueError("tp_atr_mult는 0(비활성) 이상이어야 합니다.")
        # tp1_pct(퍼센트 사다리)와 tp_atr_mult(ATR 스케일 사다리)는 같은 1단계
        # 목표를 두 가지 다른 방식으로 정의하는 것이라 동시에 켤 수 없다 —
        # 어느 쪽이 이기는지 모호해지는 대신 여기서 명확히 거부한다.
        if self.tp1_pct > 0 and self.tp_atr_mult > 0:
            raise ValueError("tp1_pct와 tp_atr_mult는 동시에 설정할 수 없습니다.")
        # 사다리가 켜져 있으면(둘 중 하나라도 활성) tp_floor_exit 기본값이 True로
        # 바뀐다 — 명시적으로 껐다면(false) 그 값을 존중한다. 사다리가 꺼져
        # 있으면(기본) 이 값 자체가 쓰이지 않으므로 기존 동작에 영향 없다.
        _ladder_active = self.tp1_pct > 0 or self.tp2_pct > 0 or self.tp_atr_mult > 0
        self.tp_floor_exit: bool = bool(params.get("tp_floor_exit", _ladder_active))
        self.tp_floor_to_entry: bool = bool(params.get("tp_floor_to_entry", False))

        self.win_table: dict | None = win_table if isinstance(win_table, dict) else None

        # 개장 30분 레인지 봉 개수 — M이 30을 나누어떨어지지 않으면 근사(모듈
        # docstring "아직 못 하는 것" 5번).
        self._or30_bars = max(1, round(_OR30_MINUTES / self.interval_minutes))
        # 조회할 M분봉 개수 — 6×n_slow(기본 126봉 ≈ 15m 기준 4.8세션). 3×n_slow(63봉)로
        # 시작했다가 교차검증(2026-09-05)에서 EMA(adjust=False)·Wilder ATR 의 시드
        # 편향이 k_min 문턱 근처 신호를 뒤집는 것이 확인돼 넓혔다 — 63봉이면
        # strength 가 ±0.01 어긋나 백테스터(전체 이력)와 DOWN↔NEUTRAL 이 갈렸다.
        # 라이브 Toss 15m 히스토리는 실측 200봉(2026-09-05)이라 요청 가능한 범위다.
        # 봉이 덜 오면 있는 만큼으로 계산한다(거부하지 않는다 — 편향만 커진다).
        self._bar_count = max(6 * self.n_slow, self.n_slow + self.n_atr + 10)

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """모듈 docstring "데이터" 절 참고."""
        bars: tuple[tuple[str, str, int], ...] = (
            (self.signal_symbol, self._interval, self._bar_count),
        )
        if self.day_filter or self.win_table is not None:
            bars = bars + ((self.signal_symbol, "1d", 30),)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: int = int(state.get("entries_today", 0))
        last_reject: dict[str, str] = dict(state.get("last_reject", {}))
        last_stop: dict[str, str] = dict(state.get("last_stop", {}))

        market = self._market
        if not snap.market_open.get(market, False):
            return Decision(
                signals=(),
                next_state={
                    "session_date": session_date, "entries_today": entries_today,
                    "last_reject": last_reject, "last_stop": last_stop,
                },
            )

        tz = market_tz(market)
        today = snap.now.astimezone(tz).date()
        today_iso = today.isoformat()
        if kernel.session_rolled(session_date.get(market), today_iso):
            session_date[market] = today_iso
            entries_today = 0
            last_reject = {}
            # last_stop(쿨다운 타이머)은 세션을 넘겨 유지한다 — EMA/ATR과 같은
            # "날짜를 넘겨 이어지는 연속 시계열" 전제(모듈 docstring "백테스터와의
            # 합의점" 절).

        bars = snap.bars.get((self.signal_symbol, self._interval))
        ind = self._indicators(bars, market, today)

        signals: list[Signal] = []
        held = kernel.held_lot(snap.lots, (self.long_symbol, self.short_symbol))
        if held is not None:
            symbol, lot = held
            manage_signals, entries_today = self._manage(
                snap, symbol, lot, ind, market, today, today_iso,
                entries_today, last_stop, last_reject,
            )
            signals.extend(manage_signals)
        elif in_continuous_session(market, snap.now):
            entry_signal, entries_today = self._try_entry(
                snap, ind, market, today, today_iso, entries_today, last_stop, last_reject,
            )
            if entry_signal is not None:
                signals.append(entry_signal)

        return Decision(
            signals=tuple(signals),
            next_state={
                "session_date": session_date, "entries_today": entries_today,
                "last_reject": last_reject, "last_stop": last_stop,
            },
        )

    # ------------------------------------------------------------------ 지표

    def _indicators(
        self, bars: pd.DataFrame | None, market: str, today: dtdate,
    ) -> dict[str, Any] | None:
        """완성봉 t 기준 방향 판정 원재료. 계산 불가(데이터 부족/NaN)면 `None`
        — 모듈 docstring "규칙" 절의 EMA/ATR/VWAP/strength 정의 그대로."""
        if bars is None or bars.empty:
            return None
        closes = bars["close"].astype(float)
        last_ema_f = float(ema(closes, self.n_fast).iloc[-1])
        last_ema_s = float(ema(closes, self.n_slow).iloc[-1])
        if pd.isna(last_ema_f) or pd.isna(last_ema_s):
            return None
        atr = _wilder_atr(bars, self.n_atr)
        if atr is None or not (atr > 0):
            return None
        session = _session_slice(bars, market, today)
        if session.empty:
            return None
        vwap = _session_vwap(session)
        if vwap is None:
            return None
        close_t = float(closes.iloc[-1])
        if pd.isna(close_t):
            return None

        strength = (last_ema_f - last_ema_s) / atr
        if last_ema_f > last_ema_s and close_t > vwap and strength >= self.k_min:
            direction = "UP"
        elif last_ema_f < last_ema_s and close_t < vwap and -strength >= self.k_min:
            direction = "DOWN"
        else:
            direction = "NEUTRAL"
        return {
            "direction": direction, "atr": atr, "close": close_t, "vwap": vwap,
            "strength": strength, "bar_ts": bars.index[-1], "session": session,
        }

    # ------------------------------------------------------------------ 익절 사다리

    def _tp_ladder_active(self) -> bool:
        """1단계(퍼센트/ATR) 또는 2단계 중 하나라도 설정됐는가."""
        return self.tp1_pct > 0 or self.tp2_pct > 0 or self.tp_atr_mult > 0

    def _tp2_is_full_exit(self) -> bool:
        """2단계가 1단계 없이 **곧장 전량 청산**인가 — 이때만 `ENTER` 신호에
        `target`을 실어 백테스트 봉내 체결기가 그 자리에서 체결시킬 수 있다
        (모듈 docstring "백테스터와의 합의점" — 봉내 체결기는 단일 stop/target
        쌍만 알아 사다리의 부분청산은 표현할 수 없다)."""
        return self.tp1_pct <= 0 and self.tp_atr_mult <= 0 and self.tp2_pct > 0

    def _tp_targets(
        self, entry: float, ind: Mapping[str, Any] | None,
    ) -> tuple[float | None, float | None]:
        """(tp1_target, tp2_target) — 설정 안 됐거나(0) ATR 모드인데 지표를 계산할
        수 없으면 그 자리는 `None`(판정 보류, 모듈 docstring "규칙" 절 산식)."""
        if self.tp_atr_mult > 0:
            if ind is None:
                return None, None
            distance = 3 * self.tp_atr_mult * ind["atr"] / ind["close"]
            return entry * (1 + distance), entry * (1 + 2 * distance)
        tp1 = entry * (1 + self.tp1_pct) if self.tp1_pct > 0 else None
        tp2 = entry * (1 + self.tp2_pct) if self.tp2_pct > 0 else None
        return tp1, tp2

    # ------------------------------------------------------------------ 진입

    @staticmethod
    def _minutes_since_open(market: str, now: datetime) -> float:
        tz = market_tz(market)
        local = now.astimezone(tz)
        open_t, _ = continuous_window(market)
        open_dt = datetime.combine(local.date(), open_t, tzinfo=tz)
        return (local - open_dt).total_seconds() / 60

    def _day_filter_ok(
        self, snap: StrategySnapshot, market: str, today: dtdate,
    ) -> tuple[bool, str]:
        """스펙 "day_filter" 절 — 갭 또는 개장 30분 레인지/ATR14 중 하나만 통과하면
        된다. 확인 불가는 거부다(넓은 손절 전략을 국면 모르는 채로 켜지 않는다,
        `trend_day.py`와 같은 원칙)."""
        daily = snap.bars.get((self.signal_symbol, "1d"))
        if daily is None or daily.empty:
            return False, "day_filter: 일봉 확인 불가"
        prev_close = float(daily["close"].astype(float).iloc[-1])
        if not (prev_close > 0) or pd.isna(prev_close):
            return False, "day_filter: 전일 종가 확인 불가"

        bars = snap.bars.get((self.signal_symbol, self._interval))
        session = _session_slice(bars, market, today) if bars is not None else pd.DataFrame()
        if session.empty:
            return False, "day_filter: 당일 봉 확인 불가"
        day_open = float(session["open"].iloc[0])
        gap = abs(day_open / prev_close - 1.0) if day_open > 0 and not pd.isna(day_open) else None

        if len(session) < self._or30_bars:
            return False, "day_filter: 개장 레인지 미형성"
        opening = session.iloc[: self._or30_bars]
        or_high = float(opening["high"].max())
        or_low = float(opening["low"].min())
        if pd.isna(or_high) or pd.isna(or_low):
            return False, "day_filter: 개장 레인지 값 결손"

        atr14 = _wilder_atr(daily, _DAY_FILTER_ATR_PERIOD)
        if atr14 is None or not (atr14 > 0):
            return False, "day_filter: ATR14(일봉) 계산 불가"
        or_ratio = (or_high - or_low) / atr14

        gap_ok = gap is not None and gap >= self.gap_min
        or_ok = or_ratio >= self.or_atr_min
        if gap_ok or or_ok:
            return True, ""
        gap_str = f"{gap * 100:.2f}%" if gap is not None else "N/A"
        return False, (
            f"day_filter: 갭 {gap_str} < {self.gap_min * 100:.2f}% & "
            f"OR/ATR14 {or_ratio:.2f} < {self.or_atr_min:.2f}"
        )

    def _cooldown_ok(
        self, direction_key: str, snap: StrategySnapshot, last_stop: Mapping[str, str],
    ) -> tuple[bool, str]:
        """스펙 "재진입 쿨다운" 절 — 같은 방향의 마지막 손절 이후 완성봉 개수를
        센다(세션 경계 무시, 모듈 docstring "합의점" 절)."""
        if self.cooldown_bars <= 0:
            return True, ""
        stop_ts_iso = last_stop.get(direction_key)
        if not stop_ts_iso:
            return True, ""
        bars = snap.bars.get((self.signal_symbol, self._interval))
        if bars is None or bars.empty:
            return True, ""
        try:
            stop_ts = pd.Timestamp(stop_ts_iso)
        except (ValueError, TypeError):
            return True, ""
        elapsed = int((bars.index > stop_ts).sum())
        if elapsed < self.cooldown_bars:
            return False, f"쿨다운: 손절 후 {elapsed}/{self.cooldown_bars}봉 경과"
        return True, ""

    def _win_table_ok(
        self, snap: StrategySnapshot, market: str, ind: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """스펙 "Win table" 절 — 버킷이 있고 n≥30·mean_bp≤0이면 거부, 그 밖(테이블
        없음/버킷 없음/지표 계산 불가)은 전부 통과(로그만)."""
        if self.win_table is None:
            return True, ""
        daily = snap.bars.get((self.signal_symbol, "1d"))
        if daily is None or len(daily) < _WIN_TABLE_REGIME_SMA_DAYS:
            return True, ""
        closes = daily["close"].astype(float)
        sma_series = sma(closes, _WIN_TABLE_REGIME_SMA_DAYS)
        last_close, last_sma = float(closes.iloc[-1]), float(sma_series.iloc[-1])
        if pd.isna(last_sma):
            return True, ""
        regime = "above" if last_close > last_sma else "below"

        local_time = snap.now.astimezone(market_tz(market)).time()
        hour_bucket = _entry_hour_bucket(local_time)
        if hour_bucket is None:
            return True, ""

        edges = (self.win_table.get("edges") or {}).get("strength")
        tercile = _strength_tercile(abs(ind["strength"]), edges)
        if tercile is None:
            return True, ""

        key = f"{regime}|{hour_bucket}|{tercile}"
        bucket = (self.win_table.get("buckets") or {}).get(key)
        if not bucket:
            return True, ""
        n = bucket.get("n", 0) or 0
        mean_bp = bucket.get("mean_bp", 0) or 0
        if n >= 30 and mean_bp <= 0:
            return False, f"win-table: bucket negative ({key}, n={n}, mean={mean_bp:.1f}bp)"
        return True, ""

    def _try_entry(
        self, snap: StrategySnapshot, ind: Mapping[str, Any] | None, market: str,
        today: dtdate, today_iso: str, entries_today: int,
        last_stop: dict[str, str], last_reject: dict[str, str],
    ) -> tuple[Signal | None, int]:
        """무포지션 진입과 스위치의 반대편 진입이 공유하는 단일 게이트 파이프라인
        (모듈 docstring "백테스터와의 합의점" — 스위치도 이 전부를 통과해야 한다)."""
        if ind is None:
            last_reject[self.signal_symbol] = "지표 계산 불가(데이터 부족)"
            return None, entries_today
        direction = ind["direction"]
        if direction == "NEUTRAL":
            return None, entries_today  # 정상 대기 — 사유를 남기지 않는다

        elapsed = self._minutes_since_open(market, snap.now)
        if elapsed < self.warmup_min:
            return None, entries_today  # 워밍업 구간 — 정상 대기

        mtc = snap.minutes_to_close.get(market)
        if mtc is not None and mtc < self.no_entry_min:
            last_reject[self.signal_symbol] = f"진입창 종료(마감 {self.no_entry_min:g}분 전)"
            return None, entries_today

        if entries_today >= self.max_entries_per_day:
            last_reject[self.signal_symbol] = f"하루 진입 상한 {self.max_entries_per_day}건 소진"
            return None, entries_today

        if self.day_filter:
            ok, reason = self._day_filter_ok(snap, market, today)
            if not ok:
                last_reject[self.signal_symbol] = reason
                return None, entries_today

        direction_key = "long" if direction == "UP" else "short"
        cd_ok, cd_reason = self._cooldown_ok(direction_key, snap, last_stop)
        if not cd_ok:
            last_reject[self.signal_symbol] = cd_reason
            return None, entries_today

        exec_symbol = self.long_symbol if direction == "UP" else self.short_symbol
        quote = snap.quotes.get(exec_symbol)
        if quote is None or quote.price <= 0:
            last_reject[exec_symbol] = "현재가 없음"
            return None, entries_today
        entry = float(quote.price)

        # 손절 산식 — stop_mode="pct"면 진입가 대비 고정 비율(리서치 시뮬레이터
        # 정의), 기본("atr")은 기존 산식과 100% 동일(모듈 docstring "규칙" 절).
        if self.stop_mode == "pct":
            stop = entry * (1 - self.stop_pct)
        else:
            stop = entry * (1 - 3 * self.stop_atr_mult * ind["atr"] / ind["close"])
        if not (0 < stop < entry):
            last_reject[exec_symbol] = "손절가 계산 불가"
            return None, entries_today
        stop_bp = (entry - stop) / entry * 1e4
        if not kernel.stop_bp_gate_ok(stop_bp, self.min_stop_bp):
            last_reject[exec_symbol] = f"손절폭 {stop_bp:.0f}bp < 최소 {self.min_stop_bp:g}bp"
            return None, entries_today

        wt_ok, wt_reason = self._win_table_ok(snap, market, ind)
        if not wt_ok:
            last_reject[exec_symbol] = wt_reason
            return None, entries_today

        # 2단계 익절이 1단계 없이 곧장 전량 청산이면(=단일 stop/target 쌍) 봉내
        # 체결기가 그 자리에서 체결시킬 수 있게 `target`을 싣는다 — 1단계가
        # 있는 사다리는 `target`을 비워 둔다(모듈 docstring "익절 사다리" 절,
        # `_tp2_is_full_exit`).
        target = None
        if self._tp2_is_full_exit():
            _, target = self._tp_targets(entry, ind)

        entries_today += 1
        last_reject.pop(exec_symbol, None)
        signal = Signal(
            strategy_id=self.id,
            symbol=exec_symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"LETF 페어 전환({direction}): {self.signal_symbol} "
                f"strength={ind['strength']:.2f} 체결={exec_symbol} "
                f"진입={fmt_price(entry, exec_symbol)} 손절={fmt_price(stop, exec_symbol)}"
            ),
            stop=stop,
            target=target,
            state_update={
                "entry": entry, "stop": stop, "direction": direction_key,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )
        return signal, entries_today

    # ------------------------------------------------------------------ 관리

    def _should_flatten(self, snap: StrategySnapshot) -> bool:
        market = self._market
        mtc = snap.minutes_to_close.get(market)
        return kernel.should_flatten_dual(
            market, snap.now, mtc, snap.cadence_minutes, self.eod_exit_min,
        )

    def _manage(
        self, snap: StrategySnapshot, symbol: str, lot: Mapping[str, Any],
        ind: Mapping[str, Any] | None, market: str, today: dtdate, today_iso: str,
        entries_today: int, last_stop: dict[str, str], last_reject: dict[str, str],
    ) -> tuple[list[Signal], int]:
        """판정 순서: 오버나잇 안전망 → EoD → 손절(트레일 포함) → 익절 사다리
        (2단계 전량 → 1단계 부분 → 플로어) → 신호 반전/스위치 → NEUTRAL 청산."""
        signals: list[Signal] = []
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return signals, entries_today
        price = float(quote.price)
        entry = float(lot["entry"])
        stop_raw = lot.get("stop")
        stop = float(stop_raw) if stop_raw is not None else None
        direction = lot.get("direction")  # "long" | "short"

        def _exit(reason: str) -> Signal:
            return kernel.exit_signal(self.id, symbol, reason)

        if not self.overnight and kernel.is_overnight_carry(lot, today_iso):
            signals.append(_exit(
                f"세션 롤 강제청산(오버나잇 금지): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            ))
            return signals, entries_today

        if not self.overnight and self._should_flatten(snap):
            signals.append(_exit(
                f"EoD 청산(마감 {self.eod_exit_min:g}분 전): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            ))
            return signals, entries_today

        # 트레일(있으면) — 매 사이클 즉석 재계산해 이번 사이클 손절 판정에만
        # 쓴다(모듈 docstring "아직 못 하는 것" 2번 — 랏에는 영속화하지 않는다).
        effective_stop = stop
        if self.trail_atr_mult > 0 and ind is not None and stop is not None:
            candidate = price * (1 - 3 * self.trail_atr_mult * ind["atr"] / ind["close"])
            effective_stop = max(stop, candidate)

        if effective_stop is not None and price <= effective_stop:
            if ind is not None and direction is not None:
                last_stop[direction] = ind["bar_ts"].isoformat()
            signals.append(_exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(effective_stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            ))
            return signals, entries_today

        # 익절 사다리(2026-09-05, 소유자 청산 규칙 확장). 손절 다음, 신호 기반
        # 청산보다 먼저 본다 — 목표가에 닿았으면 방향 판정과 무관하게 이익을
        # 실현한다. 손절 판정과 달리 `ind`가 필요 없는 경로(퍼센트 사다리)도
        # 있으므로 아래 `if ind is None: return` 보다 앞에 둔다.
        if self._tp_ladder_active():
            tp1_done = bool(lot.get("tp1_done"))
            tp1_price_raw = lot.get("tp1_price")
            tp1_price = float(tp1_price_raw) if tp1_price_raw is not None else None
            tp1_target, tp2_target = self._tp_targets(entry, ind)

            if tp2_target is not None and price >= tp2_target:
                signals.append(_exit(
                    f"익절(2단계): entry={fmt_price(entry, symbol)} "
                    f"목표={fmt_price(tp2_target, symbol)} 현재={fmt_price(price, symbol)}"
                ))
                return signals, entries_today

            if not tp1_done and tp1_target is not None and price >= tp1_target:
                # tp1_price는 **체결 확인 후에만**(state_update가 SCALE_OUT의
                # 실제 fill을 거쳐야 랏에 반영된다) 랏에 실린다 — 재시작해도
                # 살아남는다(모듈 docstring "상태가 두 갈래로 흐른다" 절과 같은
                # 원칙, `_try_entry`의 entry/stop과 동급).
                tp1_state_update: dict[str, Any] = {"tp1_done": True, "tp1_price": price}
                if self.tp_floor_to_entry:
                    tp1_state_update["stop"] = entry
                signals.append(Signal(
                    strategy_id=self.id,
                    symbol=symbol,
                    action=SignalAction.SCALE_OUT,
                    target_weight=0.0,
                    exit_fraction=self.tp1_fraction,
                    reason=(
                        f"부분 익절(1단계, {self.tp1_fraction:.0%}): "
                        f"entry={fmt_price(entry, symbol)} 목표={fmt_price(tp1_target, symbol)} "
                        f"현재={fmt_price(price, symbol)}"
                    ),
                    state_update=tp1_state_update,
                ))
                return signals, entries_today

            if tp1_done and self.tp_floor_exit and tp1_price is not None and price < tp1_price:
                # 시세(quote) 기반 플로어다 — 리서치 시뮬레이터는 **완성봉 종가**로
                # 판정한다는 점에서 작은 괴리가 있다(모듈 docstring "백테스터와의
                # 합의점" 절에 명시).
                signals.append(_exit(
                    f"부분익절 가격 이탈: 1단계가={fmt_price(tp1_price, symbol)} "
                    f"현재={fmt_price(price, symbol)}"
                ))
                return signals, entries_today

        if ind is None:
            return signals, entries_today  # 방향 확인 불가 — 신호 기반 청산 보류

        if direction == "long" and ind["direction"] == "DOWN":
            signals.append(_exit(
                f"신호 반전 청산(하방): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            ))
            if self.switch:
                new_signal, entries_today = self._try_entry(
                    snap, ind, market, today, today_iso, entries_today, last_stop, last_reject,
                )
                if new_signal is not None:
                    signals.append(new_signal)
            return signals, entries_today

        if direction == "short" and ind["direction"] == "UP":
            signals.append(_exit(
                f"신호 반전 청산(상방): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            ))
            if self.switch:
                new_signal, entries_today = self._try_entry(
                    snap, ind, market, today, today_iso, entries_today, last_stop, last_reject,
                )
                if new_signal is not None:
                    signals.append(new_signal)
            return signals, entries_today

        if self.exit_on_neutral and ind["direction"] == "NEUTRAL":
            signals.append(_exit(
                f"중립 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}"
            ))
            return signals, entries_today

        return signals, entries_today


class LetfPairShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 다른 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=..., win_table=...)`) 생성할 수
    있게 하는 얇은 팩토리 — `TrendDayShell`과 동일 패턴 + `win_table` 전달."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "letf_pair", win_table: dict | None = None,
    ):
        super().__init__(
            LetfPairPureStrategy(symbols, params, market=market, id=id, win_table=win_table)
        )
