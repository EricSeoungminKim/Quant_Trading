"""진입 **신호 자체**의 품질 — 청산 규칙과 완전히 분리해서 본다.

## 왜 이게 필요한가 (목적함수를 바꾸는 것이 요점이다)

백테스트 최종 수익률을 목적함수로 두고 파라미터를 고르면, 전략이 아니라 **그
백테스트에 맞춰진 것**이 나온다. 최종 수익률은 진입 규칙·손절·목표·부분청산·
사이징·비용이 전부 곱해진 하나의 숫자라, 어디서 나온 성과인지 분해되지 않는다.
진입이 좋아서 번 건지, 손절 폭을 우연히 잘 맞춘 건지 구분할 수 없다.

여기 있는 지표들은 **청산 규칙과 무관하다**. 오직 "진입 시점이 좋았는가"만 본다:

1. `excursion_profile` — MAE/MFE. 진입 후 얼마나 역행했고 얼마나 순행했는가.
   **역행폭(MAE)이 작다 = 확신 있는 진입.** 이것이 "확신 있는 진입"의 조작적
   정의다. 횡보 한가운데서 동전 던지듯 들어간 신호는 MAE가 크다.
2. `edge_decay` — 진입 후 N봉 각각의 선도수익. 진짜 엣지는 **일찍 나타나서
   유지된다**. 잡음은 평평하고 0과 구분되지 않는다.
3. `signal_precision` — MFE가 임계치에 먼저 닿았는가 vs MAE가 먼저 닿았는가.
   청산 규칙이 개입하지 않는 "승률".
4. `noise_robustness` — 파라미터를 ±10% 흔들었을 때 신호 집합이 얼마나 유지되나
   (Jaccard). 10% 넛지에 신호가 뒤집히는 전략은 잡음에 적합된 것이다.
5. `signal_frequency` — 빈도·간격·군집. 한 주에 50번 터지고 그 뒤 1년간 조용한
   전략은 거래할 수 있는 물건이 아니다.

이렇게 분리해 두면 **진입 품질과 포지션 관리를 따로 설계하고 따로 검증**할 수
있다. 하나의 수익률 숫자 안에서 둘을 뭉개지 않는다.

## 입력이 BacktestResult가 아닌 이유

이 모듈은 완성된 `BacktestResult`를 받지 않는다. 실제 백테스트는 포지션이 이미
열려 있거나 예산이 없으면 신호를 **억제**하므로, 체결된 거래만 보면 신호 모집단이
편향된다. 그래서 `collect_entry_signals`는 브로커·리스크매니저·포지션 상태 없이
전략을 리플레이해서 **발생한 모든 ENTER_LONG을** 모은다 (아래 그 함수의 독스트링에
남는 차이를 정확히 적어 두었다).

## 규약

- 집계 점수(signal score)는 만들지 않는다. 구성요소를 직접 본다.
- 모든 통계는 표본 수 n을 함께 낸다. 모자라면 값을 지어내지 않고
  `sufficient=False`로 표시하고, 렌더러가 "표본 부족(n=...)"으로 찍는다.
- 시드 고정 시 완전 결정론적이다.
- numpy/pandas만 쓴다 (scipy는 선택 의존성 그룹이라 기댈 수 없다 — t-통계량은
  손으로 짠다).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from quant.apps.config import load_settings
from quant.core.models import market_of
from quant.core.clock import SimClock
from quant.adapters.data.history import HistoryDataFeed
from quant.core.ports import Context
from quant.core.models import SignalAction
from quant.trade.strategy import STRATEGY_REGISTRY

# 분포(중앙값·사분위)를 말하려면 최소 이 정도는 있어야 한다. 근거는
# research/sample_guard.MIN_TRADES와 같다 — n<30이면 이항 승률의 표준오차가
# 너무 커서 동전던지기와 구분되지 않는다. 적용 대상만 다르다(여긴 신호 수).
MIN_ENTRIES = 30

# 연도별 행을 "그 해의 신호 빈도"로 읽으려면 최소 이 정도. 한 해 3건짜리 행은
# 레짐 판단 근거가 아니라 그냥 카운트다(카운트 자체는 항상 출력한다).
MIN_YEAR_ENTRIES = 12

# 신호 간격 분포(CV·burstiness)를 말하려면 간격이 최소 이 개수는 있어야 한다.
MIN_GAPS = 10

DEFAULT_EXCURSION_HORIZONS: tuple[int, ...] = (1, 4, 8, 26)
DEFAULT_DECAY_BARS = 26
DEFAULT_PRECISION_PCT = 1.0
DEFAULT_PERTURB_PCT = 10.0
DEFAULT_PERTURB_DRAWS = 4
DEFAULT_SEED = 42

_BAR_COLUMNS = ["open", "high", "low", "close", "volume"]
_DAY_SECONDS = 86400.0
_DAYS_PER_MONTH = 365.25 / 12


# --- 입력 타입 ----------------------------------------------------------------

@dataclass(frozen=True)
class EntryEvent:
    """진입 신호 하나. 체결됐다는 뜻이 아니라 **신호가 발생했다**는 뜻이다.

    ts: 판단 시각 = 봉 마감 시각(리플레이 사이클 시각). 봉 인덱스는 봉 시가
        시각이므로, 이 신호의 선도 구간은 `index >= ts`인 봉들이다.
    direction: 실제로 **매수한 종목** 기준 방향. 이 저장소는 전량 롱이다 —
        SQQQ 진입은 하락 전망을 표현하지만 SQQQ를 사는 것이므로 `"long"`이고,
        순행/역행도 SQQQ 가격 기준으로 잰다. 역방향 ETF를 "숏"으로 적으면
        MFE/MAE 부호가 통째로 뒤집힌다.
    price: 신호 시점의 기준가(마지막 완성봉 종가). 실제 체결가가 아니다.
    """

    ts: pd.Timestamp
    symbol: str
    direction: str = "long"
    price: float = 0.0


@dataclass
class SignalCollection:
    """`collect_entry_signals`의 산출물 — 신호 + 그 신호를 재는 데 쓸 봉 전체."""

    entries: list[EntryEvent]
    bars: dict[str, pd.DataFrame]
    symbols: list[str]
    interval_minutes: int
    n_cycles: int
    first_bar: pd.Timestamp | None
    last_bar: pd.Timestamp | None


# --- 결과 타입 ----------------------------------------------------------------

@dataclass
class ExcursionStat:
    """한 선도구간(horizon_bars)에서의 MAE/MFE 분포.

    MAE/MFE 모두 **크기(양수 %)** 로 보고한다: MAE는 진입가 대비 최대 역행폭,
    MFE는 최대 순행폭. 부호를 섞으면 사분위 읽기가 헷갈린다.
    """

    horizon_bars: int
    n: int
    sufficient: bool
    mae_median_pct: float = 0.0
    mae_p25_pct: float = 0.0
    mae_p75_pct: float = 0.0
    mae_p90_pct: float = 0.0
    mfe_median_pct: float = 0.0
    mfe_p25_pct: float = 0.0
    mfe_p75_pct: float = 0.0
    mfe_p90_pct: float = 0.0
    # 중앙값의 비 (개별 진입 비율의 평균이 아니다 — MAE=0인 진입에서 무한대가
    # 나오는 것을 피하려고 중앙값끼리 나눈다).
    mfe_mae_ratio: float = 0.0
    # 선도봉이 horizon만큼 없어서 제외된 신호 수 (표본 끝자락).
    n_truncated: int = 0


@dataclass
class DecayPoint:
    bars: int
    n: int
    sufficient: bool
    mean_pct: float = 0.0
    median_pct: float = 0.0
    stderr_pct: float = 0.0
    t_stat: float = 0.0


@dataclass
class PrecisionStats:
    """청산 규칙 없는 "승률" — 순행 임계치와 역행 임계치 중 무엇이 먼저 닿았나."""

    threshold_pct: float
    horizon_bars: int
    n_entries: int
    sufficient: bool
    n_favorable_first: int = 0
    n_adverse_first: int = 0
    # 같은 봉 안에서 양쪽 임계치를 모두 건드린 경우. OHLC만으로는 봉 내부 순서를
    # 알 수 없으므로 어느 쪽이 먼저인지 **모른다**. 지어내지 않고 따로 센다.
    n_ambiguous: int = 0
    # 선도구간 안에서 어느 쪽도 닿지 않은 경우.
    n_unresolved: int = 0
    n_resolved: int = 0
    precision: float = 0.0  # favorable / (favorable + adverse)


@dataclass
class YearCount:
    year: int
    count: int
    sufficient: bool


@dataclass
class FrequencyStats:
    n_entries: int
    span_days: float
    sufficient: bool
    signals_per_month: float = 0.0
    per_symbol: dict[str, int] = field(default_factory=dict)
    per_year: list[YearCount] = field(default_factory=list)
    n_gaps: int = 0
    gaps_sufficient: bool = False
    gap_days_median: float = 0.0
    gap_days_p25: float = 0.0
    gap_days_p75: float = 0.0
    gap_days_max: float = 0.0
    gap_days_mean: float = 0.0
    gap_days_std: float = 0.0
    gap_cv: float = 0.0
    # Burstiness B = (sd-mean)/(sd+mean). 0 = 포아송(무기억), 1에 가까울수록 군집,
    # -1에 가까울수록 규칙적. 간격 분포 하나로 군집성을 보는 표준적인 지표다.
    burstiness: float = 0.0
    max_in_7d: int = 0
    busiest_month: str = ""
    busiest_month_count: int = 0
    frac_in_busiest_month: float = 0.0


@dataclass
class RobustnessDraw:
    value: object
    n_signals: int
    jaccard: float


@dataclass
class ParamRobustness:
    name: str
    base_value: object
    perturbed: bool
    skip_reason: str = ""
    draws: list[RobustnessDraw] = field(default_factory=list)
    mean_jaccard: float = 0.0
    min_jaccard: float = 0.0


@dataclass
class RobustnessStats:
    baseline_n: int
    pct: float
    n_draws: int
    seed: int
    sufficient: bool
    params: list[ParamRobustness] = field(default_factory=list)


@dataclass
class SignalQuality:
    """신호 품질 묶음. 집계 점수는 없다 — 구성요소를 직접 본다."""

    n_entries: int
    symbols: list[str]
    interval_minutes: int
    first: pd.Timestamp | None
    last: pd.Timestamp | None
    frequency: FrequencyStats
    excursions: list[ExcursionStat]
    decay: list[DecayPoint]
    precision: PrecisionStats
    robustness: RobustnessStats | None = None
    n_dropped_no_bars: int = 0


# --- 내부 헬퍼 ----------------------------------------------------------------

def _validate_bars(bars: dict[str, pd.DataFrame]) -> None:
    for symbol, df in bars.items():
        if not isinstance(df, pd.DataFrame):
            raise TypeError(f"bars[{symbol!r}]는 pd.DataFrame이어야 한다")
        if df.empty:
            continue
        if not isinstance(df.index, pd.DatetimeIndex):
            raise TypeError(f"bars[{symbol!r}]의 인덱스는 DatetimeIndex여야 한다")
        if not df.index.is_monotonic_increasing:
            raise ValueError(f"bars[{symbol!r}] 인덱스가 시간순이 아니다")
        missing = [c for c in ("high", "low", "close") if c not in df.columns]
        if missing:
            raise ValueError(f"bars[{symbol!r}]에 필요한 컬럼이 없다: {missing}")


def _forward(bars: pd.DataFrame, entry_ts: pd.Timestamp, n: int | None) -> pd.DataFrame:
    """진입 판단 시각 **이후** 봉들. entry_ts는 봉 마감 시각이고 인덱스는 봉 시가
    시각이므로, `index >= entry_ts`가 곧 "그 판단 이후에 형성된 봉"이다."""
    start = int(bars.index.searchsorted(entry_ts, side="left"))
    return bars.iloc[start:] if n is None else bars.iloc[start:start + n]


def _pctile(values: np.ndarray, q: float) -> float:
    return float(np.percentile(values, q)) if values.size else 0.0


def _t_stat(values: np.ndarray) -> tuple[float, float, float]:
    """(mean, stderr, t). scipy 없이 손으로 — 표본평균의 t = mean/(sd/sqrt(n))."""
    n = values.size
    if n < 2:
        return (float(values.mean()) if n else 0.0, 0.0, 0.0)
    mean = float(values.mean())
    stderr = float(values.std(ddof=1) / np.sqrt(n))
    t = mean / stderr if stderr > 0 else 0.0
    return mean, stderr, float(t)


def _pair(entries: Iterable[EntryEvent], bars: dict[str, pd.DataFrame]) -> list[tuple[EntryEvent, pd.DataFrame]]:
    out: list[tuple[EntryEvent, pd.DataFrame]] = []
    for e in entries:
        df = bars.get(e.symbol)
        if df is None or df.empty or e.price <= 0:
            continue
        out.append((e, df))
    return out


# --- 1. MAE / MFE -------------------------------------------------------------

def excursion_profile(
    entries: Sequence[EntryEvent],
    bars: dict[str, pd.DataFrame],
    horizons: Sequence[int] = DEFAULT_EXCURSION_HORIZONS,
) -> list[ExcursionStat]:
    """진입 후 고정 선도구간에서의 최대역행(MAE)·최대순행(MFE) 분포.

    **역행폭(MAE)이 작다는 것이 "확신 있는 진입"의 조작적 정의다.** 좋은 진입은
    들어가자마자 거의 아프지 않다. 횡보 한가운데서 들어간 신호는 순행하든 말든
    먼저 크게 역행하고, 그 역행폭이 곧 손절이 뜯기는 폭이며 포지션 크기의 상한이다.
    MFE가 커도 MAE가 같이 크면 그것은 "확신"이 아니라 변동성이다.

    측정 대상은 **실제로 매수한 종목**이다. SQQQ 롱은 하락 전망을 표현하지만
    순행/역행은 SQQQ 가격으로 잰다 (`EntryEvent.direction` 독스트링 참고).

    MAE/MFE 모두 크기(양수 %)로 낸다. `mfe_mae_ratio`는 개별 비율의 평균이 아니라
    **중앙값끼리의 비**다 — MAE=0인 진입에서 개별 비율이 무한대가 되기 때문이다.

    선도봉이 horizon만큼 남아 있지 않은 신호(표본 끝자락)는 그 horizon에서
    제외하고 `n_truncated`로 센다. horizon마다 n이 다를 수 있다는 뜻이다.

    한계: 겹치는 진입(같은 구간을 공유하는 신호들)은 서로 독립이 아니다. 여기
    사분위는 기술통계지 추론통계가 아니다. 또 봉 내부의 고가/저가 도달 **순서**는
    OHLC로 알 수 없으므로, MAE와 MFE가 같은 봉에서 나왔을 때 무엇이 먼저였는지는
    이 함수가 답하지 않는다 (`signal_precision`이 그 모호성을 따로 센다).
    """
    _validate_bars(bars)
    pairs = _pair(entries, bars)
    out: list[ExcursionStat] = []

    for h in horizons:
        h = int(h)
        mae_list: list[float] = []
        mfe_list: list[float] = []
        truncated = 0
        for e, df in pairs:
            fwd = _forward(df, e.ts, h)
            if len(fwd) < h:
                truncated += 1
                continue
            hi = float(fwd["high"].to_numpy().max())
            lo = float(fwd["low"].to_numpy().min())
            if e.direction == "short":
                mfe = (e.price / lo - 1.0) * 100 if lo > 0 else 0.0
                mae = (hi / e.price - 1.0) * 100
            else:
                mfe = (hi / e.price - 1.0) * 100
                mae = (1.0 - lo / e.price) * 100
            mae_list.append(max(mae, 0.0))
            mfe_list.append(max(mfe, 0.0))

        mae_arr = np.asarray(mae_list, dtype=float)
        mfe_arr = np.asarray(mfe_list, dtype=float)
        n = mae_arr.size
        if n < MIN_ENTRIES:
            out.append(ExcursionStat(horizon_bars=h, n=n, sufficient=False, n_truncated=truncated))
            continue
        mae_med = _pctile(mae_arr, 50)
        mfe_med = _pctile(mfe_arr, 50)
        out.append(ExcursionStat(
            horizon_bars=h, n=n, sufficient=True, n_truncated=truncated,
            mae_median_pct=mae_med,
            mae_p25_pct=_pctile(mae_arr, 25), mae_p75_pct=_pctile(mae_arr, 75),
            mae_p90_pct=_pctile(mae_arr, 90),
            mfe_median_pct=mfe_med,
            mfe_p25_pct=_pctile(mfe_arr, 25), mfe_p75_pct=_pctile(mfe_arr, 75),
            mfe_p90_pct=_pctile(mfe_arr, 90),
            mfe_mae_ratio=float(mfe_med / mae_med) if mae_med > 0 else float("inf"),
        ))
    return out


# --- 2. 엣지 감쇠 -------------------------------------------------------------

def edge_decay(
    entries: Sequence[EntryEvent],
    bars: dict[str, pd.DataFrame],
    max_bars: int = DEFAULT_DECAY_BARS,
) -> list[DecayPoint]:
    """진입 후 1..max_bars봉 각각의 선도수익 평균·중앙값·표준오차·t.

    읽는 법: **진짜 엣지는 일찍 나타나서 유지된다.** 1~4봉에서 이미 0과 구분되는
    양의 평균이 나오고 그 뒤로 꺾이지 않는 모양. 잡음은 어느 봉에서도 |t|가 2를
    넘지 못하고 부호가 오락가락한다. 뒤쪽 봉에서만 유의해지면 그건 진입 신호의
    엣지가 아니라 그냥 자산의 표류(drift)일 가능성이 높다.

    **경고: 이 t-통계량은 낙관적(과대)이다.** 신호가 겹치면(같은 선도구간을
    공유하면) 관측치가 독립이 아니라서 유효표본이 n보다 작다. 자기상관을 보정하지
    않았으므로 표준오차는 하한이다. |t|가 2를 겨우 넘는 정도면 "유의하다"고 읽지
    말 것. 방향과 감쇠 **모양**을 보는 용도지 유의성 검정이 아니다.

    각 봉의 n을 따로 낸다 — 표본 끝자락 신호는 뒤쪽 봉에서 빠지므로 n이 줄어든다.
    """
    _validate_bars(bars)
    pairs = _pair(entries, bars)
    max_bars = int(max_bars)

    # 진입별 선도수익 행렬(모자란 뒤쪽은 NaN) — 봉마다 다시 슬라이싱하지 않는다.
    matrix = np.full((len(pairs), max_bars), np.nan, dtype=float)
    for i, (e, df) in enumerate(pairs):
        fwd = _forward(df, e.ts, max_bars)
        closes = fwd["close"].to_numpy(dtype=float)
        if closes.size == 0:
            continue
        rets = (closes / e.price - 1.0) * 100
        if e.direction == "short":
            rets = -rets
        matrix[i, :closes.size] = rets

    out: list[DecayPoint] = []
    for k in range(1, max_bars + 1):
        col = matrix[:, k - 1]
        vals = col[np.isfinite(col)]
        n = vals.size
        if n < MIN_ENTRIES:
            out.append(DecayPoint(bars=k, n=n, sufficient=False))
            continue
        mean, stderr, t = _t_stat(vals)
        out.append(DecayPoint(
            bars=k, n=n, sufficient=True,
            mean_pct=mean, median_pct=float(np.median(vals)),
            stderr_pct=stderr, t_stat=t,
        ))
    return out


# --- 3. 신호 정밀도 -----------------------------------------------------------

def signal_precision(
    entries: Sequence[EntryEvent],
    bars: dict[str, pd.DataFrame],
    threshold_pct: float = DEFAULT_PRECISION_PCT,
    horizon_bars: int = DEFAULT_DECAY_BARS,
) -> PrecisionStats:
    """"먼저 우리 쪽으로 갔는가" — 청산 규칙이 없는 승률.

    진입가 기준 +threshold_pct와 -threshold_pct를 놓고, 선도구간 안에서 어느 쪽에
    **먼저** 닿았는지 센다. 손절폭·목표가·보유기간 같은 청산 파라미터가 하나도
    들어가지 않으므로, 이 숫자는 진입 규칙만의 성질이다. (threshold를 그 전략의
    1R에 해당하는 %로 주면 "1R 먼저 먹었나"가 된다.)

    **봉 내부 순서는 모른다.** 한 봉이 고가로 +임계치를, 저가로 -임계치를 동시에
    건드리면 OHLC만으로는 어느 쪽이 먼저인지 알 수 없다. 지어내지 않고
    `n_ambiguous`로 따로 센 뒤 분모에서 제외한다. 이 값이 크면 임계치가 봉
    변동폭에 비해 너무 좁다는 뜻이므로 precision 자체를 신뢰하면 안 된다.

    `precision = 순행선착 / (순행선착 + 역행선착)`. 어느 쪽도 안 닿은 신호는
    `n_unresolved`로 세고 분모에서 뺀다 — "아무 일도 없었다"는 승도 패도 아니다.
    """
    _validate_bars(bars)
    pairs = _pair(entries, bars)
    horizon_bars = int(horizon_bars)
    thr = float(threshold_pct) / 100.0

    fav = adv = amb = unres = 0
    for e, df in pairs:
        fwd = _forward(df, e.ts, horizon_bars)
        if fwd.empty:
            unres += 1
            continue
        highs = fwd["high"].to_numpy(dtype=float)
        lows = fwd["low"].to_numpy(dtype=float)
        if e.direction == "short":
            fav_mask = lows <= e.price * (1 - thr)
            adv_mask = highs >= e.price * (1 + thr)
        else:
            fav_mask = highs >= e.price * (1 + thr)
            adv_mask = lows <= e.price * (1 - thr)
        fav_i = np.flatnonzero(fav_mask)
        adv_i = np.flatnonzero(adv_mask)
        f = int(fav_i[0]) if fav_i.size else -1
        a = int(adv_i[0]) if adv_i.size else -1
        if f < 0 and a < 0:
            unres += 1
        elif a < 0 or (f >= 0 and f < a):
            fav += 1
        elif f < 0 or a < f:
            adv += 1
        else:
            amb += 1

    resolved = fav + adv
    n = len(pairs)
    return PrecisionStats(
        threshold_pct=float(threshold_pct),
        horizon_bars=horizon_bars,
        n_entries=n,
        sufficient=resolved >= MIN_ENTRIES,
        n_favorable_first=fav,
        n_adverse_first=adv,
        n_ambiguous=amb,
        n_unresolved=unres,
        n_resolved=resolved,
        precision=float(fav / resolved) if resolved else 0.0,
    )


# --- 4. 잡음 강건성 -----------------------------------------------------------

def _signal_key(entries: Iterable[EntryEvent]) -> set[tuple[str, pd.Timestamp]]:
    return {(e.symbol, e.ts) for e in entries}


def jaccard(a: set, b: set) -> float:
    """|교집합| / |합집합|. 둘 다 비면 1.0(변화 없음)으로 정의한다."""
    union = a | b
    return 1.0 if not union else len(a & b) / len(union)


def noise_robustness(
    generate: Callable[[dict], Sequence[EntryEvent]],
    base_params: dict,
    param_names: Sequence[str],
    pct: float = DEFAULT_PERTURB_PCT,
    n_draws: int = DEFAULT_PERTURB_DRAWS,
    seed: int = DEFAULT_SEED,
) -> RobustnessStats:
    """파라미터를 ±pct% 흔들어 신호 집합을 다시 만들고 baseline과의 Jaccard를 잰다.

    **10% 넛지에 신호 집합이 확 바뀌는 전략은 그 데이터의 잡음에 적합된 것이다.**
    파라미터를 한 번에 **하나씩만** 흔든다 — 그래야 어느 파라미터에 취약한지
    보인다. 여러 개를 동시에 흔들면 원인이 섞여서 "불안정하다"는 사실만 남는다.

    baseline은 `generate({})`, 즉 파라미터 오버라이드 없는 원본 신호 집합이다.
    비교 대상은 `(symbol, ts)` 집합이며, 신호 **개수**가 아니라 **어떤 시점에
    터졌는가**를 본다 (개수만 같고 전부 다른 시점이면 Jaccard는 0이다).

    불리언/문자열 같은 비수치 파라미터는 "±10%"가 정의되지 않으므로 흔들지 않고
    `perturbed=False` + 사유를 남긴다 (조용히 건너뛰면 검사한 줄 안다).
    정수 파라미터는 반올림 후 최소 1로 클립하며, 반올림 결과가 원값과 같으면
    그 draw는 Jaccard 1.0이 나온다 — 이는 버그가 아니라 "그 파라미터는 이
    스케일에서 10% 흔들어도 값이 안 변한다"는 사실 그대로다.

    시드 고정 시 완전 결정론적이다(파라미터는 주어진 순서대로 소비한다).
    """
    baseline = _signal_key(generate({}))
    rng = np.random.default_rng(seed)
    results: list[ParamRobustness] = []

    for name in param_names:
        base = base_params.get(name)
        if isinstance(base, bool) or not isinstance(base, (int, float)):
            results.append(ParamRobustness(
                name=name, base_value=base, perturbed=False,
                skip_reason=f"수치형이 아님({type(base).__name__}) — ±{pct:g}% 정의 불가",
            ))
            continue

        draws: list[RobustnessDraw] = []
        for factor in 1.0 + rng.uniform(-pct, pct, size=n_draws) / 100.0:
            value = base * factor
            value = max(1, int(round(value))) if isinstance(base, int) else float(value)
            perturbed_set = _signal_key(generate({name: value}))
            draws.append(RobustnessDraw(
                value=value, n_signals=len(perturbed_set),
                jaccard=jaccard(baseline, perturbed_set),
            ))
        overlaps = np.asarray([d.jaccard for d in draws], dtype=float)
        results.append(ParamRobustness(
            name=name, base_value=base, perturbed=True, draws=draws,
            mean_jaccard=float(overlaps.mean()) if overlaps.size else 0.0,
            min_jaccard=float(overlaps.min()) if overlaps.size else 0.0,
        ))

    return RobustnessStats(
        baseline_n=len(baseline), pct=float(pct), n_draws=int(n_draws), seed=int(seed),
        sufficient=len(baseline) >= MIN_ENTRIES,
        params=results,
    )


# --- 5. 빈도 · 군집 -----------------------------------------------------------

def signal_frequency(entries: Sequence[EntryEvent]) -> FrequencyStats:
    """신호가 얼마나 자주, 얼마나 고르게 터지는가.

    한 주에 50번 터지고 그 뒤 1년간 조용한 전략은 백테스트 수익률이 뭐가 나오든
    거래할 수 있는 물건이 아니다 — 자본이 놀고, 그 한 주의 레짐에 전 재산이
    걸린다. 그래서 개수만이 아니라 **간격의 분포**를 본다:

    - `gap_cv` = 간격의 변동계수. 포아송(무기억) 과정이면 1 근처.
    - `burstiness` = (sd-mean)/(sd+mean) ∈ [-1,1]. 0=포아송, +1에 가까울수록 군집,
      -1에 가까울수록 규칙적. 간격 하나로 군집성을 보는 표준 지표다.
    - `max_in_7d` = 임의의 7일 창에 들어간 최대 신호 수.
    - `frac_in_busiest_month` = 가장 바빴던 한 달에 전체의 몇 %가 몰렸나.

    연도별 개수를 따로 내는 것은 레짐 의존성을 드러내기 위함이다 — 2020년에만
    터지는 전략은 2020년 전략이지 전략이 아니다. 신호가 적은 해도 개수는 그대로
    출력하되 `sufficient=False`로 표시한다(개수는 사실, 빈도 해석은 근거 부족).
    """
    events = sorted(entries, key=lambda e: (e.ts, e.symbol))
    n = len(events)
    if n == 0:
        return FrequencyStats(n_entries=0, span_days=0.0, sufficient=False)

    ts = pd.DatetimeIndex([e.ts for e in events])
    span_days = float((ts[-1] - ts[0]).total_seconds() / _DAY_SECONDS)

    per_symbol: dict[str, int] = {}
    for e in events:
        per_symbol[e.symbol] = per_symbol.get(e.symbol, 0) + 1

    year_counts: dict[int, int] = {}
    for t in ts:
        year_counts[int(t.year)] = year_counts.get(int(t.year), 0) + 1
    per_year = [
        YearCount(year=y, count=c, sufficient=c >= MIN_YEAR_ENTRIES)
        for y, c in sorted(year_counts.items())
    ]

    month_labels = ts.strftime("%Y-%m")
    month_counts: dict[str, int] = {}
    for label in month_labels:
        month_counts[label] = month_counts.get(label, 0) + 1
    busiest_month, busiest_count = max(month_counts.items(), key=lambda kv: (kv[1], kv[0]))

    # 단위 안전 변환. pandas 3에서 DatetimeIndex.unit이 'us'일 수 있어 asi8이
    # 나노초가 아니다(실측: pandas 3.0.5에서 unit='us'). 반면 Timedelta.value는
    # 항상 나노초라, 둘을 섞으면 1000배 어긋난다 — max_in_7d가 항상 전체 개수를
    # 반환하고 간격(일)이 1000분의 1로 나온다. Timedelta로 나눠 단위를 없앤다.
    days_from_start = np.asarray((ts - ts[0]) / pd.Timedelta(days=1), dtype=float)
    max_in_7d = int(max(
        np.searchsorted(days_from_start, days_from_start + 7.0, side="right") - np.arange(n)
    ))

    gaps = np.diff(days_from_start)
    n_gaps = int(gaps.size)
    gaps_ok = n_gaps >= MIN_GAPS
    mean_gap = float(gaps.mean()) if n_gaps else 0.0
    std_gap = float(gaps.std(ddof=1)) if n_gaps > 1 else 0.0
    denom = std_gap + mean_gap

    return FrequencyStats(
        n_entries=n,
        span_days=span_days,
        sufficient=n >= MIN_ENTRIES,
        signals_per_month=float(n / (span_days / _DAYS_PER_MONTH)) if span_days > 0 else 0.0,
        per_symbol=per_symbol,
        per_year=per_year,
        n_gaps=n_gaps,
        gaps_sufficient=gaps_ok,
        gap_days_median=_pctile(gaps, 50),
        gap_days_p25=_pctile(gaps, 25),
        gap_days_p75=_pctile(gaps, 75),
        gap_days_max=float(gaps.max()) if n_gaps else 0.0,
        gap_days_mean=mean_gap,
        gap_days_std=std_gap,
        gap_cv=float(std_gap / mean_gap) if mean_gap > 0 else 0.0,
        burstiness=float((std_gap - mean_gap) / denom) if denom > 0 else 0.0,
        max_in_7d=max_in_7d,
        busiest_month=busiest_month,
        busiest_month_count=busiest_count,
        frac_in_busiest_month=float(busiest_count / n),
    )


# --- 신호 수집 (전략 리플레이, 체결 없음) --------------------------------------

class _NoPositionBroker:
    """포지션이 **영원히 비어 있는** 브로커. 전략의 `ctx.broker.positions()`만 만족시킨다.

    주문을 받지 않는다 — 실수로 체결 경로가 붙으면 조용히 무시되는 대신 터지도록
    `place_order`가 예외를 던진다.
    """

    def positions(self) -> dict:
        return {}

    def cash(self) -> float:
        return 0.0

    def place_order(self, order):  # pragma: no cover - 방어용
        raise RuntimeError("신호 수집 리플레이는 주문을 내지 않는다 — 배선이 잘못됐다")


def replay_entry_signals(
    strategy,
    data,
    bar_closes: pd.DatetimeIndex,
    interval_minutes: int,
) -> tuple[list[EntryEvent], int]:
    """전략을 리플레이하며 ENTER_LONG 신호를 **체결 없이** 모은다. (신호, 사이클수).

    look-ahead 금지는 백테스트와 동일하게 지킨다: 매 사이클 `clock.set()` /
    `data.set_now()`로 시각을 앞으로만 돌리고, 전략은 `ctx.data.history()`가
    돌려주는 완성봉만 본다.

    **백테스트와 의도적으로 다른 점 (이게 이 함수의 존재 이유다):**

    - 브로커·리스크매니저·포트폴리오가 없다. 포지션이 절대 열리지 않으므로
      "이미 보유 중이라 진입 신호를 내지 않는다"는 억제가 사라진다. 백테스트의
      체결 로그만 보면 신호 모집단이 **보유 중이 아니었던 시점**으로 편향된다.
    - 예산·사이징·주문 거부(risk.approve)가 개입하지 않는다.
    - `EntryEvent.price`는 신호 시점의 마지막 완성봉 종가이며 체결가가 아니다
      (슬리피지·수수료 없음).

    **남아 있는 게이팅 (전략 내부라 여기서 제거할 수 없다):**

    - `max_concurrent_names`는 전략이 한 사이클 안에서 세는 값이라, 같은 사이클에
      여러 종목이 동시에 돌파하면 뒤 종목이 잘린다. 포지션은 안 열리므로 사이클
      간에는 영향이 없다. 종목별로 완전히 독립된 모집단이 필요하면 심볼 하나짜리로
      나눠 호출하거나 `param_overrides={"max_concurrent_names": len(symbols)}`로
      중화한다.
    - `allow_same_day_reentry` 같은 **신호 규칙 자체**의 상태는 그대로 둔다 —
      그건 포지션 게이팅이 아니라 진입 규칙의 일부다.
    """
    if len(bar_closes) == 0:
        return [], 0
    clock = SimClock(now=bar_closes[0], cadence_minutes=interval_minutes)
    ctx = Context(clock=clock, data=data, broker=_NoPositionBroker())

    entries: list[EntryEvent] = []
    for ts in bar_closes:
        clock.set(ts)
        data.set_now(ts)
        for signal in strategy.on_cycle(ctx):
            if signal.action is not SignalAction.ENTER_LONG:
                continue
            quote = data.quote(signal.symbol)
            if quote is None:
                continue
            entries.append(EntryEvent(
                ts=pd.Timestamp(ts), symbol=signal.symbol,
                direction="long", price=float(quote.price),
            ))
    return entries, len(bar_closes)


def load_full_bars(data, symbols: Sequence[str], interval: str) -> dict[str, pd.DataFrame]:
    """리플레이가 끝난 뒤 **사후 측정용**으로 전 구간 봉을 뽑는다(look-ahead 무관).

    DataFeed의 공개 표면만 쓴다: `set_now`를 마지막 봉 마감으로 옮기고 history를
    크게 긁는다. 신호 생성 중에는 절대 호출하면 안 된다 — 시계를 미래로 옮긴다.
    """
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        closes = data.bar_closes(symbol, interval)
        if len(closes) == 0:
            out[symbol] = pd.DataFrame(columns=_BAR_COLUMNS)
            continue
        data.set_now(closes[-1])
        out[symbol] = data.history(symbol, interval, len(closes))
    return out


def collect_entry_signals(
    strategy_id: str = "donchian",
    interval: str = "15m",
    settings_path: str = "config/settings.yaml",
    history_dir: str | Path = "data/history",
    param_overrides: dict | None = None,
    start: datetime | pd.Timestamp | None = None,
    end: datetime | pd.Timestamp | None = None,
) -> SignalCollection:
    """실데이터(`data/history/`) 위에서 전략을 리플레이해 진입 신호를 모은다.

    `enabled: false`인 전략도 수집한다 — 신호 품질 조사는 채택 **전에** 하는
    일이므로 활성화 여부로 막지 않는다(그래서 `build_strategies`를 쓰지 않는다).
    억제/차이는 `replay_entry_signals` 독스트링에 적혀 있다.
    """
    settings = load_settings(settings_path)
    cfg = settings.raw
    strat_cfg = dict(cfg["strategies"][strategy_id])
    params = dict(strat_cfg["params"])
    if param_overrides:
        params.update(param_overrides)
    symbols = list(strat_cfg["symbols"])
    markets = market_of(cfg.get("universe", {}))

    data = HistoryDataFeed(symbols, history_dir=history_dir)
    bar_closes = data.bar_closes(symbols[0], interval)
    if start is not None:
        bar_closes = bar_closes[bar_closes >= _align_tz(start, bar_closes)]
    if end is not None:
        bar_closes = bar_closes[bar_closes <= _align_tz(end, bar_closes)]

    strategy = STRATEGY_REGISTRY[strat_cfg["class"]](
        symbols=symbols, params=params,
        market=markets.get(symbols[0], "US"), id=strategy_id,
    )
    minutes = 24 * 60 if interval == "1d" else int(interval.rstrip("m"))
    entries, n_cycles = replay_entry_signals(strategy, data, bar_closes, minutes)
    bars = load_full_bars(data, symbols, interval)

    return SignalCollection(
        entries=entries, bars=bars, symbols=symbols, interval_minutes=minutes,
        n_cycles=n_cycles,
        first_bar=pd.Timestamp(bar_closes[0]) if len(bar_closes) else None,
        last_bar=pd.Timestamp(bar_closes[-1]) if len(bar_closes) else None,
    )


def _align_tz(value, index: pd.DatetimeIndex) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if index.tz is not None and ts.tzinfo is None:
        ts = ts.tz_localize(index.tz)
    return ts


# --- 묶음 계산 ----------------------------------------------------------------

def compute_signal_quality(
    entries: Sequence[EntryEvent],
    bars: dict[str, pd.DataFrame],
    interval_minutes: int = 15,
    horizons: Sequence[int] = DEFAULT_EXCURSION_HORIZONS,
    decay_bars: int = DEFAULT_DECAY_BARS,
    precision_pct: float = DEFAULT_PRECISION_PCT,
    precision_horizon: int | None = None,
    robustness: RobustnessStats | None = None,
) -> SignalQuality:
    """다섯 블록을 한 번에 계산한다. 렌더링은 report.render_signal_quality.

    `robustness`는 전략 재실행이 필요해 순수하지 않으므로 여기서 만들지 않고
    바깥에서 `noise_robustness`로 만들어 넘긴다(없으면 그 블록은 생략된다).
    """
    _validate_bars(bars)
    entries = list(entries)
    dropped = len(entries) - len(_pair(entries, bars))
    ts_all = sorted(e.ts for e in entries)
    return SignalQuality(
        n_entries=len(entries),
        symbols=sorted({e.symbol for e in entries}) or sorted(bars),
        interval_minutes=int(interval_minutes),
        first=pd.Timestamp(ts_all[0]) if ts_all else None,
        last=pd.Timestamp(ts_all[-1]) if ts_all else None,
        frequency=signal_frequency(entries),
        excursions=excursion_profile(entries, bars, horizons),
        decay=edge_decay(entries, bars, decay_bars),
        precision=signal_precision(
            entries, bars, precision_pct,
            decay_bars if precision_horizon is None else precision_horizon,
        ),
        robustness=robustness,
        n_dropped_no_bars=dropped,
    )
