"""일중 모멘텀(intraday momentum) — Zarattini·Aziz·Barbon(2024) "Beat the Market:
An Effective Intraday Momentum Strategy" (SSRN 4824172) 기반 5분봉 전략.
**순수 계약 전용 신규 전략**(레거시 쌍둥이 없음).

## 원 논문 요지

개장가 기준 "노이즈 영역"(noise area)을 시각대별 평균 절대 이동폭으로 정의하고,
가격이 그 영역을 벗어나면 그 방향을 추종한다. 비용 차감 후 Sharpe 1.33
(SPY, 2007~2024)을 보고했다 — 방향성 매매(롱/숏 겸용)가 원 설계다.

## 롱 온리 계좌를 위한 각색 — 방향을 ETF 선택으로 변환

이 저장소의 계좌는 숏을 직접 못 판다(유니버스 전체가 롱 온리로 배선돼 있다).
그래서 신호와 체결을 분리한다:

- **신호 심볼**(`signal_symbol`, 기본 QQQ)의 5분봉으로만 노이즈 밴드·이탈을
  판정한다. 이 심볼 자체는 사고 팔지 않는다 — 순수하게 방향 판정용이다.
- 판정된 방향에 따라 **다른 심볼**을 매수한다: 상방 이탈 → `long_symbol`
  (기본 TQQQ, 3배 레버리지 롱) 매수, 하방 이탈 → `short_symbol`(기본 SQQQ,
  3배 인버스) 매수. 인버스 ETF를 사는 것 자체가 "매수"이므로 롱 온리 계좌
  제약과 충돌하지 않는다.

세 심볼은 **반드시 같은 시장이어야 한다**(생성자에서 검증) — 세션 롤/EoD
청산 타이밍을 신호 심볼의 시장 하나로 계산하기 때문이다. QQQ/TQQQ/SQQQ는
전부 US라 이 제약이 실사용에 문제가 되지 않는다.

## 노이즈 밴드 계산 (시각대별, 세션 앵커드 아님)

`noise_band()` 참고. 각 과거 거래일 d에 대해, **오늘 마지막 완성 5분봉과 같은
시각(time-of-day) t**의 종가로 `|close_t(d) / open(d) − 1|`을 구하고, 그 값들을
`lookback_days`(기본 14)일치 평균해 σ(t)를 얻는다. 상단 = 오늘 시가 ×
(1 + `band_mult` × σ(t)), 하단 = 오늘 시가 × (1 − `band_mult` × σ(t)).

세션 앵커드 누적(expanding) 방식이 아니라 **시각대별 횡단면 평균**인 이유 —
원 논문의 정의를 그대로 따른다: "오전 9:35에는 역사적으로 얼마나 움직였나"를
묻는 것이지 "오늘 09:30~09:35 구간 자체의 변동성"을 묻는 게 아니다. 시각이
같아야 비교가 성립하므로 과거 각 날의 **동일 시각** 봉만 골라 매칭한다
(달력일이 아니라 time-of-day 매칭 — 조기폐장/봉 결손이 있어도 있는 날짜만
쓰고 없는 시각은 그 날을 건너뛴다, 값을 지어내지 않는다).

## "확인 불가"는 통과가 아니라 거부다 — 그리고 이게 실제로 발동한다

`min_lookback_days`(기본 5) 미만의 과거 거래일만 확인되면 진입하지 않는다.
**이건 이론상의 방어선이 아니다.** `docs/data-availability.md` 실측(2026-07-28):
Toss 1분봉(따라서 이를 리샘플하는 5분봉도) 과거 데이터는 **약 4거래일**만
제공한다(`mr_vwap_quiet.py` 모듈 docstring도 같은 근거를 인용한다). 즉 이
전략을 지금 당장 라이브에 배선하면, Toss가 매일 쌓아가는 히스토리가
`min_lookback_days`를 채울 때까지 **조용히 0건**이 된다 — 버그가 아니라
설계다("확인 불가 = 거부"). 시간이 지나 히스토리가 쌓이면(또는 더 깊은 히스토리를
주는 소스로 바뀌면) 자연히 정상 작동한다.

## 청산 — 세 갈래

1. **VWAP 역크로스**(주 청산, 원 논문 VWAP 트레일의 단순화): 신호 심볼의
   **현재가**가 신호 심볼의 **현재 세션 VWAP**을 진입 방향과 반대로 넘으면
   청산한다. VWAP은 매 사이클 다시 계산한다(고정하지 않는다) — "트레일"이라는
   말 자체가 시간에 따라 움직이는 기준선을 뜻하기 때문이다(`mr_vwap_quiet`가
   목표를 진입 시점에 고정하는 것과 의도적으로 다르다 — 거기는 평균회귀 목표,
   여기는 추세 추종 트레일이라 기준선의 역할이 다르다).
2. **하드 손절**: 체결 ETF(`long_symbol`/`short_symbol`) 가격 기준
   `stop_pct`(기본 1.5%) 하락. `min_stop_bp`(기본 40bp) 게이트를 통과해야
   한다 — `stop_pct`가 지나치게 작게 설정되면(예: 0.3%) 왕복 비용(US 20bp)
   근처에서 손절선이 형성돼 진입 즉시 지는 구조가 된다
   (`pullback_impulse.py`의 2026-08-29 실사고와 같은 계열의 방어).
3. **세션 마감 5분 전 전량 청산**(`flatten_before_close_minutes`, 기본 5) +
   **오버나잇 강제청산**(세션 롤 시 보유 중이면 무조건 청산). 오버나잇 금지는
   이 전략의 설계 자체다 — 지수 갭 위험을 3배 레버리지로 들고 자는 것은
   범위 밖이다.

### 판단 주기와 EoD 청산의 상호작용 (과거 실사고 반영)

`_should_flatten()`은 `mr_vwap_quiet.py`의 검증된 이중 판정을 그대로 쓴다 —
(a) `Clock.minutes_to_close`(캘린더 기반, 조기폐장 인지) − `cadence_minutes`
< `flatten_before_close_minutes`, **또는** (b) 연속 거래 종료 시각까지 남은
벽시계 시간 − `cadence_minutes` < `flatten_before_close_minutes`.
`cadence_minutes`를 빼는 이유: 사이클이 5분 간격으로 도는데 "마감 5분 전"
경계를 정확히 그 순간에만 확인하면, 마침 그 순간이 사이클 사이 틈에 있을 때
청산 신호가 통째로 발동 못 하는 경우가 생긴다(`pullback_impulse.py` 모듈
docstring이 인용하는 `Clock._should_flatten` 재현 원칙과 같은 이유).
`flatten_before_close_minutes`가 0이면 이 창이 완전히 사라질 수 있어 생성자가
양수를 강제한다(같은 계열의 과거 실사고 방어).

## 재진입 규칙

- **반대 방향 재진입은 무제한 허용**: 기존 포지션이 VWAP 역크로스/손절/타임아웃
  성격의 청산으로 닫힌 뒤, 반대 방향 이탈이 다시 성립하면 그대로 반대 ETF에
  진입한다(같은 날 여러 번이어도 무관 — "반대 방향"이라는 것 자체가 앞선
  판단이 뒤집혔다는 뜻이다).
- **같은 방향 재진입은 하루 `max_same_direction_entries_per_day`(기본 2)회
  상한**: US 왕복 비용이 ~26bp(이 저장소 실측 20bp대)인데, 같은 방향으로
  반복 이탈-복귀를 쫓으면 손익분기를 비용이 먼저 갉아먹는다. 방향별로 독립
  카운트한다(`entries_today = {"long": n, "short": n}`).

## 상태를 두 갈래로 흘린다 (장중 재시작 생존)

`mr_vwap_quiet.py`/`pullback_impulse.py`와 같은 원칙 — **장중 재시작은 가정이
아니라 실제 사건**(2026-08-28)이다.

| 키 | 무엇 | 어디로 | 재시작 |
|---|---|---|---|
| `session_date` | 세션 롤 감지 | `next_state` | 잃어도 무해 |
| `entries_today` | 방향별 하루 진입 횟수 | `next_state` | 잃는다 — "아직 못 하는 것" 참고 |
| `entry`/`stop`/`direction`/`session`/`entered_at` | **열린 랏의 방어선(방향 포함)** | `Signal.state_update` → 루프가 체결 확인 후 `Position.meta["lots"]`에 기록 → 다음 사이클 `snap.lots`로 회수 | **살아남는다** |

`direction`을 lot에 싣는 이유: 재시작 후 어느 심볼(`long_symbol`/`short_symbol`)에
포지션이 열려 있는지는 `snap.lots`를 보면 알 수 있지만(그 심볼 자체가 키다),
VWAP 역크로스 판정에는 "지금 롱 레인인지 숏 레인인지"가 필요하다 — 이걸
lot 안에 명시적으로 저장해 판정 로직이 심볼 이름 문자열 비교(`sym ==
self.long_symbol`)에 암묵적으로 의존하지 않게 한다.

## 아직 못 하는 것 (정직하게)

1. **성과 근거가 없다.** 원 논문은 SPY 방향성 매매 결과이지, 이 각색(신호/체결
   심볼 분리 + 3배 레버리지 ETF)의 결과가 아니다. 백테스트 표본도 없다 —
   Toss 5분봉이 4거래일 롤링만 준다(위 "확인 불가" 절). paper 번인이 유일한
   검증 경로다.
2. **`entries_today`는 재시작으로 날아간다.** 열린 포지션은 `snap.lots`로
   보호되지만(중복 진입 불가 — 보유 중엔 진입 평가 자체를 안 한다), "청산 →
   재시작 → 같은 방향 3번째 진입"처럼 하루 안에 재시작이 끼면 방향별 2회
   상한이 리셋된다. 손실이 아니라 **빈도** 문제라 `pullback_impulse.py`와
   같은 판단으로 여기서 멈춘다.
3. **고아 포지션을 볼 수 없다.** `DataNeeds`가 정적으로 신호/롱/숏 3개 심볼만
   선언한다 — 이 전략은 애초에 유니버스 확장 대상이 아니라 고정 트리플이라
   해당 없음에 가깝지만, 파라미터를 바꿔 재배포하면 이전 심볼의 잔여 포지션은
   보이지 않는다.
4. **레버리지 ETF의 변동성 감쇠(decay)를 모델링하지 않는다.** TQQQ/SQQQ는
   일중에만 보유하므로(오버나잇 금지) 데일리 리밸런싱 감쇠의 영향은 제한적일
   것으로 기대하지만 검증되지 않았다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.strategy.mr_vwap_quiet import session_vwap_bands
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "5m"

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분. lookback 하한 산정용.
_FULL_SESSION_MINUTES = 390


# ---------------------------------------------------------------- 순수 지표

def _session_slice(bars: pd.DataFrame, market: str, day: dtdate) -> pd.DataFrame:
    """`day`의 **연속 거래 개장 이후** 봉만. 노이즈 밴드의 "당일 시가"·시각대
    매칭이 프리마켓 봉으로 오염되지 않게 한다(`mr_vwap_quiet`/`pullback_impulse`
    와 같은 이유)."""
    tz = market_tz(market)
    open_t, _ = continuous_window(market)
    local = bars.index.tz_convert(tz)
    return bars[(local.date == day) & (local.time >= open_t)]


def noise_band(
    bars: pd.DataFrame,
    market: str,
    today: dtdate,
    band_mult: float,
    lookback_days: int,
    min_lookback_days: int,
) -> tuple[float, float, float, int] | None:
    """오늘 마지막 완성 5분봉의 시각(time-of-day) 기준 노이즈 밴드.

    반환 `(day_open, upper, lower, days_used)`. 계산 불가(오늘 봉 없음, 과거
    거래일이 `min_lookback_days` 미만, 또는 그 시각에 매칭되는 과거 봉이
    `min_lookback_days` 미만) 시 **None** — "확인 불가는 통과가 아니라
    거부다"(모듈 docstring).

    `lookback_days`보다 오래된 과거 거래일은 쓰지 않는다(최근 N일만).
    `days_used`는 실제로 시각 매칭에 성공해 σ 계산에 들어간 과거 거래일 수다
    (달력상 존재하는 과거 거래일 수와 다를 수 있다 — 그 날 그 시각 봉이 없으면
    건너뛴다).
    """
    today_sess = _session_slice(bars, market, today)
    if today_sess.empty:
        return None
    day_open = float(today_sess["open"].iloc[0])
    if not (day_open > 0) or pd.isna(day_open):
        return None

    tz = market_tz(market)
    last_ts = today_sess.index[-1]
    target_time = last_ts.tz_convert(tz).time()

    local_dates = bars.index.tz_convert(tz).date
    hist_days = sorted({d for d in local_dates if d < today})
    if len(hist_days) < min_lookback_days:
        return None
    use_days = hist_days[-lookback_days:] if lookback_days > 0 else hist_days

    abs_rets: list[float] = []
    for d in use_days:
        day_bars = _session_slice(bars, market, d)
        if day_bars.empty:
            continue
        d_open = float(day_bars["open"].iloc[0])
        if not (d_open > 0) or pd.isna(d_open):
            continue
        match = day_bars[day_bars.index.tz_convert(tz).time == target_time]
        if match.empty:
            continue
        close_t = float(match["close"].iloc[-1])
        if pd.isna(close_t):
            continue
        abs_rets.append(abs(close_t / d_open - 1.0))

    if len(abs_rets) < min_lookback_days:
        return None

    sigma = sum(abs_rets) / len(abs_rets)
    upper = day_open * (1 + band_mult * sigma)
    lower = day_open * (1 - band_mult * sigma)
    return day_open, upper, lower, len(abs_rets)


# ---------------------------------------------------------------- 전략

class IntradayMomentumPureStrategy:
    """모듈 docstring 참고. `decide()`는 `snap`/`state`만 본다.

    **`symbols` 생성자 인자는 레지스트리 호출 시그니처(`cls(symbols=...,
    params=..., market=..., id=...)`) 호환용으로만 받는다** — 실제 거래
    심볼 3개(`signal_symbol`/`long_symbol`/`short_symbol`)는 `params`에서
    읽어 `self.symbols`를 직접 구성한다(신호 심볼은 조회만 하고 체결하지
    않지만, `PureStrategyShell._snapshot`이 `self.symbols`를 순회해 포지션
    lot을 채우므로 세 심볼 모두 포함해야 한다). settings.yaml 배선 시
    `symbols: []`로 둬도 무방하다.
    """

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "intraday_momentum",
    ):
        self.id = id
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 아래 self._market

        self.signal_symbol: str = str(params.get("signal_symbol", "QQQ")).strip()
        self.long_symbol: str = str(params.get("long_symbol", "TQQQ")).strip()
        self.short_symbol: str = str(params.get("short_symbol", "SQQQ")).strip()
        if not self.signal_symbol or not self.long_symbol or not self.short_symbol:
            raise ValueError("signal_symbol/long_symbol/short_symbol은 비어 있을 수 없습니다.")
        if self.long_symbol == self.short_symbol:
            raise ValueError("long_symbol과 short_symbol은 서로 달라야 합니다.")

        # 세 심볼은 같은 시장이어야 한다 — 세션 롤/EoD 청산 타이밍을 신호 심볼의
        # 시장 하나로 계산하기 때문이다(모듈 docstring).
        self._market = market_of_symbol(self.signal_symbol)
        if (market_of_symbol(self.long_symbol) != self._market
                or market_of_symbol(self.short_symbol) != self._market):
            raise ValueError(
                "signal_symbol/long_symbol/short_symbol은 같은 시장이어야 합니다 "
                "(세션 롤·EoD 청산 타이밍을 신호 심볼 시장 하나로 계산한다)."
            )

        # 순서 보존 중복 제거 — snap.lots 채움(PureStrategyShell._snapshot)이
        # self.symbols를 순회하므로 세 심볼 모두 있어야 한다.
        self.symbols = list(dict.fromkeys((self.signal_symbol, self.long_symbol, self.short_symbol)))

        # 노이즈 밴드 lookback. 기본 14일 — 원 논문 파라미터 스케일. 실측 데이터
        # 가용성(Toss 5분봉 ~4거래일)은 모듈 docstring "확인 불가" 절 참고.
        self.lookback_days: int = int(params.get("lookback_days", 14))
        # 최소 확인 가능 일수. 이 미만이면 진입하지 않는다("확인 불가 = 거부").
        self.min_lookback_days: int = int(params.get("min_lookback_days", 5))
        self.band_mult: float = float(params.get("band_mult", 1.0))
        # 하드 손절 — 체결 ETF 가격 기준 %.
        self.stop_pct: float = float(params.get("stop_pct", 1.5))
        # 손절폭 하한(bp). 0 = 비활성. stop_pct가 지나치게 좁으면 왕복 비용
        # 근처에서 손절선이 형성된다(`pullback_impulse.py` 2026-08-29 실사고와
        # 같은 계열의 방어).
        self.min_stop_bp: float = float(params.get("min_stop_bp", 40.0))
        # 같은 방향 하루 진입 상한 — 반대 방향 재진입은 이 상한에 걸리지 않는다.
        self.max_same_direction_entries_per_day: int = int(
            params.get("max_same_direction_entries_per_day", 2)
        )
        # EoD 청산 여유(분). 0이면 청산 창이 사라질 수 있어 생성자가 양수를
        # 강제한다(`pullback_impulse.py`와 같은 이유).
        self.flatten_before_close_minutes: float = float(
            params.get("flatten_before_close_minutes", 5.0)
        )
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.lookback_days < 1:
            raise ValueError("lookback_days는 1 이상이어야 합니다.")
        if self.min_lookback_days < 1:
            raise ValueError("min_lookback_days는 1 이상이어야 합니다.")
        if self.min_lookback_days > self.lookback_days:
            raise ValueError("min_lookback_days는 lookback_days 이하여야 합니다.")
        if self.band_mult <= 0:
            raise ValueError("band_mult는 양수여야 합니다.")
        if self.stop_pct <= 0:
            raise ValueError("stop_pct는 양수여야 합니다.")
        if self.min_stop_bp < 0:
            raise ValueError("min_stop_bp는 0(비활성) 이상이어야 합니다.")
        if self.max_same_direction_entries_per_day < 1:
            raise ValueError("max_same_direction_entries_per_day는 1 이상이어야 합니다.")
        if self.flatten_before_close_minutes <= 0:
            # 0이면 마지막 in-session 사이클에서도 조건이 성립하지 않아 청산
            # 창이 통째로 사라진다(pullback_impulse.py와 같은 실사고 이력 방어).
            raise ValueError("flatten_before_close_minutes는 양수여야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")

        # 조회할 5분봉 개수 — lookback_days + 오늘 세션 + 여유.
        self._lookback_bars = max(
            int(params.get("lookback_bars", 200)),
            (self.lookback_days + 1) * (_FULL_SESSION_MINUTES // 5) + 10,
        )

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """신호 심볼 5분봉 + 세 심볼 현재가 + 포지션."""
        return DataNeeds(
            bars=((self.signal_symbol, _INTERVAL, self._lookback_bars),),
            quotes=tuple(self.symbols),
            needs_positions=True,
        )

    def _held_lot(self, snap: StrategySnapshot) -> tuple[str, Mapping[str, Any]] | None:
        """지금 내가 방어선을 써 넣은 랏이 있는 심볼(`long_symbol` 또는
        `short_symbol`) — 없으면 None. `entry` 유무로 판정하는 이유는
        `mr_vwap_quiet.py`의 `_my_lot`과 같다: 빈 dict는 "다른 전략 보유" 또는
        "방금 체결, 아직 state_update 미반영" 둘 다일 수 있어 안전하게
        "관리 대상 아님"으로 떨어뜨린다."""
        for sym in (self.long_symbol, self.short_symbol):
            lot = snap.lots.get(sym)
            if lot and lot.get("entry") is not None:
                return sym, lot
        return None

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, str] = dict(state.get("session_date", {}))
        entries_today: dict[str, int] = dict(state.get("entries_today", {}))
        market = self._market

        if not snap.market_open.get(market, False):
            return Decision(
                signals=(),
                next_state={"session_date": session_date, "entries_today": entries_today},
            )

        tz = market_tz(market)
        today = snap.now.astimezone(tz).date()
        today_iso = today.isoformat()
        if session_date.get(market) != today_iso:
            session_date[market] = today_iso
            entries_today = {}

        signals: list[Signal] = []

        held = self._held_lot(snap)
        if held is not None:
            sym, lot = held
            signal = self._manage(snap, today, sym, lot)
            if signal is not None:
                signals.append(signal)
        else:
            signal = self._check_entry(snap, today, entries_today)
            if signal is not None:
                signals.append(signal)

        return Decision(
            signals=tuple(signals),
            next_state={"session_date": session_date, "entries_today": entries_today},
        )

    # ------------------------------------------------------------------ 진입

    def _check_entry(
        self, snap: StrategySnapshot, today: dtdate, entries_today: dict[str, int],
    ) -> Signal | None:
        market = self._market
        if not in_continuous_session(market, snap.now):
            return None
        bars = snap.bars.get((self.signal_symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None

        band = noise_band(
            bars, market, today, self.band_mult, self.lookback_days, self.min_lookback_days,
        )
        if band is None:
            return None  # 확인 불가 = 거부
        _day_open, upper, lower, days_used = band

        sess = _session_slice(bars, market, today)
        last_close = float(sess["close"].iloc[-1])
        if pd.isna(last_close):
            return None

        if last_close > upper:
            direction = "long"
        elif last_close < lower:
            direction = "short"
        else:
            return None  # 밴드 안 — 무진입

        if entries_today.get(direction, 0) >= self.max_same_direction_entries_per_day:
            return None

        exec_symbol = self.long_symbol if direction == "long" else self.short_symbol
        quote = snap.quotes.get(exec_symbol)
        if quote is None or quote.price <= 0:
            return None
        entry = float(quote.price)

        stop = entry * (1 - self.stop_pct / 100.0)
        if not (stop < entry):
            return None
        stop_bp = (entry - stop) / entry * 1e4
        if self.min_stop_bp and stop_bp < self.min_stop_bp:
            return None

        entries_today[direction] = entries_today.get(direction, 0) + 1
        return Signal(
            strategy_id=self.id,
            symbol=exec_symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"일중 모멘텀 이탈({direction}): {self.signal_symbol} "
                f"종가={fmt_price(last_close, self.signal_symbol)} "
                f"밴드=[{fmt_price(lower, self.signal_symbol)}, {fmt_price(upper, self.signal_symbol)}] "
                f"(σ표본 {days_used}일) 체결={exec_symbol} "
                f"진입={fmt_price(entry, exec_symbol)} 손절={fmt_price(stop, exec_symbol)}"
            ),
            stop=stop,
            state_update={
                "entry": entry, "stop": stop, "direction": direction,
                "session": today.isoformat(), "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    # ------------------------------------------------------------------ 관리

    def _current_signal_vwap(self, snap: StrategySnapshot, today: dtdate) -> float | None:
        """신호 심볼의 **현재 세션** VWAP — 매 사이클 다시 계산한다(트레일이므로
        고정하지 않는다, 모듈 docstring "청산" 절). `mr_vwap_quiet.session_vwap_bands`를
        재사용한다 — `band_k`는 여기서 안 쓰므로 임의값(1.0)을 넘긴다."""
        bars = snap.bars.get((self.signal_symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        sess = _session_slice(bars, self._market, today)
        if sess.empty:
            return None
        bands = session_vwap_bands(sess, band_k=1.0)
        if bands is None:
            return None
        vwap_series, _lower, _upper = bands
        vwap = float(vwap_series.iloc[-1])
        return None if pd.isna(vwap) else vwap

    def _should_flatten(self, snap: StrategySnapshot) -> bool:
        """EoD 강제청산 시점인가 — `mr_vwap_quiet._should_flatten`과 동일한
        이중 판정(캘린더 기반 + 연속거래종료 벽시계 기준의 논리합). 모듈
        docstring "판단 주기와 EoD 청산의 상호작용" 절 참고."""
        market = self._market
        mtc = snap.minutes_to_close.get(market)
        if mtc is not None and 0 < mtc and mtc - snap.cadence_minutes < self.flatten_before_close_minutes:
            return True
        tz = market_tz(market)
        now_local = snap.now.astimezone(tz)
        _, end_t = continuous_window(market)
        remaining = (
            datetime.combine(now_local.date(), end_t, tzinfo=tz) - now_local
        ).total_seconds() / 60
        return 0 < remaining and remaining - snap.cadence_minutes < self.flatten_before_close_minutes

    def _manage(
        self, snap: StrategySnapshot, today: dtdate, symbol: str, lot: Mapping[str, Any],
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])  # _held_lot이 None 아님을 이미 보장
        stop_raw = lot.get("stop")
        stop = float(stop_raw) if stop_raw is not None else None
        direction = lot.get("direction")
        tz = market_tz(self._market)

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        entry_session = lot.get("session")
        if entry_session and entry_session != snap.now.astimezone(tz).date().isoformat():
            return _exit(
                f"세션 롤 강제청산(오버나잇 금지): entry={fmt_price(entry, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )

        if self._should_flatten(snap):
            return _exit(
                f"EoD 청산(마감 {self.flatten_before_close_minutes:g}분 전): "
                f"entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}"
            )

        if stop is not None and price <= stop:
            return _exit(
                f"손절: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                f"현재={fmt_price(price, symbol)}"
            )

        vwap = self._current_signal_vwap(snap, today)
        if vwap is not None:
            signal_quote = snap.quotes.get(self.signal_symbol)
            if signal_quote is not None and signal_quote.price > 0:
                sp = float(signal_quote.price)
                if direction == "long" and sp < vwap:
                    return _exit(
                        f"VWAP 역크로스 청산(신호 하방): {self.signal_symbol}="
                        f"{fmt_price(sp, self.signal_symbol)} VWAP={fmt_price(vwap, self.signal_symbol)} "
                        f"체결={symbol} 현재={fmt_price(price, symbol)}"
                    )
                if direction == "short" and sp > vwap:
                    return _exit(
                        f"VWAP 역크로스 청산(신호 상방): {self.signal_symbol}="
                        f"{fmt_price(sp, self.signal_symbol)} VWAP={fmt_price(vwap, self.signal_symbol)} "
                        f"체결={symbol} 현재={fmt_price(price, symbol)}"
                    )
        return None


class IntradayMomentumShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 다른 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `MrVwapQuietShell`/`PullbackImpulseShell`과 동일 패턴. 레지스트리
    배선은 이 파일 밖(`quant/trade/strategy/__init__.py`)에서 한다."""

    def __init__(
        self, symbols: list[str], params: dict, market: str = "US",
        id: str = "intraday_momentum",
    ):
        super().__init__(IntradayMomentumPureStrategy(symbols, params, market=market, id=id))
