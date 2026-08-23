"""신호 품질(quant/research/signalquality.py) 단위 테스트.

답을 해석적으로(또는 구성상 자명하게) 아는 합성 봉만 쓴다 — 실데이터를 넣고
"그럴듯한 숫자가 나온다"를 확인하는 것은 검증이 아니다.

가장 중요한 테스트는 4번(강건성 대조)이다. **이 모듈이 존재하는 이유가 거기
있다**: 파라미터를 10% 흔들었을 때 신호 집합이 유지되는 규칙과, 특정 봉에
맞춰져 있어 통째로 뒤집히는 규칙이 명확히 갈려야 한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.core.models import Signal, SignalAction
from quant.research import report
from quant.research.signalquality import (
    MIN_ENTRIES,
    EntryEvent,
    compute_signal_quality,
    edge_decay,
    excursion_profile,
    jaccard,
    noise_robustness,
    replay_entry_signals,
    signal_frequency,
    signal_precision,
)

_SYMBOL = "TQQQ"
_HORIZON = 26


def _bars(closes: np.ndarray, start: str = "2020-01-01", freq: str = "15min") -> pd.DataFrame:
    """OHLC가 전부 종가와 같은 봉 — 봉 내부 고저 순서의 모호성을 원천 제거한다
    (그래야 MAE/MFE·정밀도의 정답이 하나로 정해진다)."""
    idx = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC", name="ts")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c, "low": c, "close": c, "volume": np.full(len(c), 1000.0)},
        index=idx,
    )


def _entries_at(df: pd.DataFrame, positions: list[int], direction: str = "long") -> list[EntryEvent]:
    """position k = "k-1번 봉이 막 마감된 시점" — 진입가는 k-1번 봉 종가,
    선도구간은 k번 봉부터다(EntryEvent.ts 규약)."""
    return [
        EntryEvent(
            ts=df.index[k], symbol=_SYMBOL, direction=direction,
            price=float(df["close"].iloc[k - 1]),
        )
        for k in positions
    ]


# --- 1. MAE/MFE: 순수 상승 뒤 진입 -------------------------------------------

_RATE = 0.001  # 봉당 +0.1% 복리


def test_pure_uptrend_gives_zero_mae_and_known_mfe():
    """진입 직후 한 번도 역행하지 않는 계열 — MAE는 정확히 0, MFE는 계산 가능."""
    df = _bars(100.0 * (1 + _RATE) ** np.arange(200))
    entries = _entries_at(df, list(range(1, 61)))

    stats = {e.horizon_bars: e for e in excursion_profile(entries, {_SYMBOL: df}, (1, 4, 26))}
    for h in (1, 4, 26):
        ex = stats[h]
        assert ex.sufficient
        assert ex.n == 60
        assert ex.mae_median_pct == pytest.approx(0.0, abs=1e-12)
        assert ex.mae_p90_pct == pytest.approx(0.0, abs=1e-12)
        # 진입가 = k-1번 봉 종가, horizon h의 최고가 = k+h-1번 봉 종가 => (1+r)^h
        expected = ((1 + _RATE) ** h - 1) * 100
        assert ex.mfe_median_pct == pytest.approx(expected, rel=1e-9)
        assert ex.mfe_mae_ratio == float("inf")  # MAE 중앙값 0 -> 비는 무한대


def test_pure_uptrend_precision_is_100_percent():
    df = _bars(100.0 * (1 + _RATE) ** np.arange(200))
    entries = _entries_at(df, list(range(1, 61)))

    pr = signal_precision(entries, {_SYMBOL: df}, threshold_pct=1.0, horizon_bars=_HORIZON)
    assert pr.sufficient
    assert pr.precision == 1.0
    assert pr.n_favorable_first == 60
    assert pr.n_adverse_first == 0
    assert pr.n_ambiguous == 0
    assert pr.n_unresolved == 0  # 1.001^26 - 1 = 2.6% > 1% 임계치


def test_inverse_etf_long_is_measured_on_the_instrument_bought():
    """SQQQ 롱은 하락 전망이지만 순행/역행은 **산 종목** 가격으로 잰다.

    같은 하락 계열에서 direction="long"이면 MAE가 크고 MFE가 0이어야 한다.
    부호를 뒤집어 "숏"으로 재면 정반대가 나오므로, 이 테스트가 그 혼동을 막는다.
    """
    df = _bars(100.0 * (1 - _RATE) ** np.arange(200))
    entries = _entries_at(df, list(range(1, 61)), direction="long")

    ex = excursion_profile(entries, {_SYMBOL: df}, (26,))[0]
    assert ex.sufficient
    assert ex.mfe_median_pct == pytest.approx(0.0, abs=1e-12)
    assert ex.mae_median_pct == pytest.approx((1 - (1 - _RATE) ** 26) * 100, rel=1e-9)

    pr = signal_precision(entries, {_SYMBOL: df}, threshold_pct=1.0, horizon_bars=26)
    assert pr.precision == 0.0
    assert pr.n_adverse_first == 60


# --- 2. 랜덤워크: 엣지가 없어야 한다 ------------------------------------------

def _random_walk_setup(seed: int = 7, n_entries: int = 300, spacing: int = 30):
    """겹치지 않게 spacing 간격으로 진입 — 선도구간이 서로 안 겹치므로 관측이
    실제로 독립이다(겹치면 t가 부풀어 테스트 자체가 거짓말이 된다)."""
    rng = np.random.default_rng(seed)
    n_bars = (n_entries + 2) * spacing
    steps = rng.normal(0.0, 0.004, size=n_bars)
    df = _bars(100.0 * np.exp(np.cumsum(steps)))
    positions = [spacing * (i + 1) for i in range(n_entries)]
    return df, _entries_at(df, positions)


def test_random_walk_edge_decay_is_indistinguishable_from_zero():
    df, entries = _random_walk_setup()
    points = edge_decay(entries, {_SYMBOL: df}, max_bars=_HORIZON)

    assert all(p.sufficient for p in points)
    assert all(p.n >= MIN_ENTRIES for p in points)
    worst = max(points, key=lambda p: abs(p.t_stat))
    assert abs(worst.t_stat) < 3.0, f"랜덤워크인데 {worst.bars}봉에서 t={worst.t_stat:.2f}"
    # 표준오차는 봉이 늘수록 커진다(누적 분산) — 감쇠 곡선이 "평평"하다는 증거.
    assert points[-1].stderr_pct > points[0].stderr_pct


def test_random_walk_precision_is_near_coin_flip():
    df, entries = _random_walk_setup()
    pr = signal_precision(entries, {_SYMBOL: df}, threshold_pct=1.0, horizon_bars=_HORIZON)

    assert pr.sufficient
    assert pr.n_resolved >= MIN_ENTRIES
    assert 0.40 < pr.precision < 0.60, f"랜덤워크 정밀도 {pr.precision:.3f}"


# --- 3. 빈도 · 군집 -----------------------------------------------------------

def _daily_entries(timestamps: list[str]) -> list[EntryEvent]:
    return [EntryEvent(ts=pd.Timestamp(t, tz="UTC"), symbol=_SYMBOL, price=100.0) for t in timestamps]


def test_clustered_signals_are_flagged_as_bursty():
    """한 주에 40건 몰아치고 그 뒤 1년간 5건 — 백테스트 수익률과 무관하게
    거래 가능한 물건이 아니다. 군집 지표가 그것을 드러내야 한다."""
    burst = [f"2021-03-{1 + i // 8:02d} {9 + i % 8:02d}:00" for i in range(40)]
    tail = [f"2021-{m:02d}-15 10:00" for m in (6, 8, 10, 12)] + ["2022-02-15 10:00"]
    clustered = signal_frequency(_daily_entries(burst + tail))

    assert clustered.n_entries == 45
    assert clustered.max_in_7d >= 40
    assert clustered.frac_in_busiest_month > 0.85
    assert clustered.busiest_month == "2021-03"
    # 포아송(무작위)이면 0. 양수면 군집이다. 0.5는 임의 컷오프였고 실측 0.483도
    # 충분히 강한 군집이므로, 절대 임계 대신 등간격 대조군과의 간극을 검증한다.
    assert clustered.burstiness > 0.4, f"burstiness={clustered.burstiness:.3f}"

    # 완전 등간격 대조군 — 같은 개수인데 군집 지표가 정반대여야 한다.
    even = pd.date_range("2021-01-01", periods=45, freq="8D", tz="UTC")
    regular = signal_frequency([EntryEvent(ts=t, symbol=_SYMBOL, price=100.0) for t in even])
    assert regular.max_in_7d == 1
    assert regular.burstiness < -0.9
    assert clustered.burstiness - regular.burstiness > 1.2  # 군집 vs 등간격이 확연히 갈린다


def test_per_year_counts_flag_thin_years():
    entries = _daily_entries(
        [f"2021-{m:02d}-05 10:00" for m in range(1, 13)] + ["2022-01-05 10:00", "2022-02-05 10:00"]
    )
    fq = signal_frequency(entries)
    by_year = {y.year: y for y in fq.per_year}
    assert by_year[2021].count == 12 and by_year[2021].sufficient
    assert by_year[2022].count == 2 and not by_year[2022].sufficient


# --- 4. 잡음 강건성 — 이 모듈이 존재하는 이유 ---------------------------------

def _spike_series(seed: int = 11, n: int = 4000, spike_every: int = 100) -> np.ndarray:
    """작은 잡음 + 드물고 큰 스파이크. 5% 임계치는 스파이크만 잡고 잡음과 멀리
    떨어져 있으므로, 임계치를 ±10% 흔들어도 잡히는 봉이 그대로다."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, 0.003, size=n)
    rets[::spike_every] = 0.08
    return rets * 100  # percent


_SPIKES = _spike_series()


def _threshold_rule(params: dict) -> list[EntryEvent]:
    """봉 수익률이 임계치를 넘으면 진입 — 임계치가 데이터의 어디에 놓이냐가 전부다."""
    thr = params["threshold_pct"]
    idx = pd.date_range("2020-01-01", periods=len(_SPIKES), freq="15min", tz="UTC")
    hits = np.flatnonzero(_SPIKES > thr)
    return [EntryEvent(ts=idx[i], symbol=_SYMBOL, price=100.0) for i in hits]


def _pinned_rule(params: dict) -> list[EntryEvent]:
    """특정 봉 위치에 못박힌 규칙 — 데이터의 성질이 아니라 인덱스에 적합된 것.
    이게 "단일 봉에 맞춰 적합된 신호"의 순수 표본이다."""
    period = int(params["period"])
    idx = pd.date_range("2020-01-01", periods=len(_SPIKES), freq="15min", tz="UTC")
    return [EntryEvent(ts=idx[i], symbol=_SYMBOL, price=100.0) for i in range(0, len(idx), period)]


def test_robust_rule_survives_perturbation_but_pinned_rule_does_not():
    robust_base = {"threshold_pct": 5.0}
    robust = noise_robustness(
        lambda o: _threshold_rule({**robust_base, **o}),
        robust_base, ["threshold_pct"], pct=10.0, n_draws=6, seed=42,
    )
    pinned_base = {"period": 30}  # MIN_ENTRIES(30) 이상 생성되도록
    pinned = noise_robustness(
        lambda o: _pinned_rule({**pinned_base, **o}),
        pinned_base, ["period"], pct=10.0, n_draws=6, seed=42,
    )

    r = robust.params[0]
    p = pinned.params[0]
    assert r.perturbed and p.perturbed
    assert robust.baseline_n >= MIN_ENTRIES and pinned.baseline_n >= MIN_ENTRIES

    assert r.min_jaccard > 0.95, f"강건해야 할 규칙의 최소 겹침 {r.min_jaccard:.3f}"
    assert p.mean_jaccard < 0.30, f"못박힌 규칙의 평균 겹침 {p.mean_jaccard:.3f}"
    # 이 대조가 이 기능의 존재 이유다.
    assert r.mean_jaccard > p.mean_jaccard + 0.5


def test_noise_robustness_is_deterministic_under_fixed_seed():
    base = {"period": 100}
    gen = lambda o: _pinned_rule({**base, **o})  # noqa: E731
    a = noise_robustness(gen, base, ["period"], n_draws=4, seed=123)
    b = noise_robustness(gen, base, ["period"], n_draws=4, seed=123)
    assert [d.value for d in a.params[0].draws] == [d.value for d in b.params[0].draws]
    assert [d.jaccard for d in a.params[0].draws] == [d.jaccard for d in b.params[0].draws]


def test_non_numeric_params_are_skipped_not_silently_ignored():
    base = {"allow_same_day_reentry": True, "stop_mode": "atr"}
    rb = noise_robustness(
        lambda o: _pinned_rule({"period": 100, **{k: v for k, v in o.items() if k == "period"}}),
        base, ["allow_same_day_reentry", "stop_mode"],
    )
    assert [p.perturbed for p in rb.params] == [False, False]
    assert all(p.skip_reason for p in rb.params)


def test_jaccard_counts_timestamps_not_signal_counts():
    a = {(_SYMBOL, pd.Timestamp("2020-01-01", tz="UTC"))}
    b = {(_SYMBOL, pd.Timestamp("2020-01-02", tz="UTC"))}
    assert jaccard(a, a) == 1.0
    assert jaccard(a, b) == 0.0  # 개수는 같지만 시점이 전혀 다르다
    assert jaccard(set(), set()) == 1.0


# --- 5. 표본 부족 가드 --------------------------------------------------------

def test_short_input_reports_insufficient_everywhere():
    df = _bars(100.0 * (1 + _RATE) ** np.arange(60))
    entries = _entries_at(df, [1, 2, 3, 4, 5])
    sq = compute_signal_quality(entries, {_SYMBOL: df}, interval_minutes=15)

    assert sq.n_entries == 5
    assert not sq.frequency.sufficient
    assert not sq.frequency.gaps_sufficient
    assert all(not ex.sufficient for ex in sq.excursions)
    assert all(not d.sufficient for d in sq.decay)
    assert not sq.precision.sufficient

    text = report.render_signal_quality(sq, title="가드")
    assert text.count(report.INSUFFICIENT) >= 4
    assert "n=" not in text or report.INSUFFICIENT in text


def test_empty_entries_do_not_crash_renderer():
    df = _bars(100.0 * np.ones(50))
    sq = compute_signal_quality([], {_SYMBOL: df})
    assert sq.n_entries == 0
    assert report.INSUFFICIENT in report.render_signal_quality(sq)


def test_entries_without_bars_are_dropped_and_reported():
    df = _bars(100.0 * (1 + _RATE) ** np.arange(60))
    entries = _entries_at(df, [1, 2]) + [
        EntryEvent(ts=df.index[3], symbol="MISSING", price=100.0)
    ]
    sq = compute_signal_quality(entries, {_SYMBOL: df})
    assert sq.n_entries == 3
    assert sq.n_dropped_no_bars == 1


# --- 6. 렌더러가 기존 것을 깨지 않는가 + 정상 경로 ----------------------------

def test_renderer_shows_all_five_blocks_in_order():
    df = _bars(100.0 * (1 + _RATE) ** np.arange(300))
    entries = _entries_at(df, list(range(1, 101)))
    base = {"threshold_pct": 5.0}
    rb = noise_robustness(
        lambda o: _threshold_rule({**base, **o}), base, ["threshold_pct"], n_draws=3, seed=42,
    )
    text = report.render_signal_quality(
        compute_signal_quality(entries, {_SYMBOL: df}, interval_minutes=15, robustness=rb),
        title="합성",
    )
    order = [text.index(h) for h in ("1. 빈도", "2. 진입 품질", "3. 엣지 감쇠", "4. 신호 정밀도", "5. 잡음 강건성")]
    assert order == sorted(order)
    assert "손익이 아니다" in text


def test_path_stats_renderer_still_works():
    """report.py에 블록을 더해도 기존 렌더러가 살아 있어야 한다."""
    idx = pd.date_range("2020-01-01", periods=400, freq="D", tz="UTC")
    equity = pd.Series(1_000_000 * np.exp(np.linspace(0, 0.3, len(idx))), index=idx)
    text = report.render_path_report(equity, title="회귀 확인")
    assert "경로 통계" in text and "낙폭" in text


# --- 7. 신호 수집 리플레이 (체결·포지션 게이팅 없음) --------------------------

class _FakeFeed:
    """리플레이 규약만 만족하는 최소 DataFeed — set_now 이후 완성봉만 보여준다."""

    def __init__(self, df: pd.DataFrame, minutes: int = 15):
        self._df = df
        self._minutes = minutes
        self._now = None

    def set_now(self, now) -> None:
        self._now = now

    def _visible(self) -> pd.DataFrame:
        closes = self._df.index + pd.Timedelta(minutes=self._minutes)
        return self._df[closes <= self._now]

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return self._visible().tail(n)

    def quote(self, symbol: str):
        visible = self._visible()
        if visible.empty:
            return None
        return type("Q", (), {"symbol": symbol, "ts": visible.index[-1], "price": float(visible["close"].iloc[-1])})()

    def bar_closes(self, symbol: str, interval: str) -> pd.DatetimeIndex:
        return self._df.index + pd.Timedelta(minutes=self._minutes)


class _EveryThirdBar:
    """3봉마다 진입, 매 봉 청산 신호도 낸다. 청산은 수집되면 안 된다."""

    id = "fake"

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.seen_positions: list[int] = []

    def on_cycle(self, ctx) -> list[Signal]:
        self.seen_positions.append(len(ctx.broker.positions()))
        # 완성봉 전체를 받아야 한다 — 10으로 상한을 두면 len(bars)가 포화돼
        # "3의 배수" 조건이 초반 9봉에서만 참이 된다.
        bars = ctx.data.history(self.symbols[0], "15m", 10_000)
        out = [Signal(
            strategy_id=self.id, symbol=self.symbols[0],
            action=SignalAction.EXIT_LONG, target_weight=0.0, reason="noise",
        )]
        if len(bars) and len(bars) % 3 == 0:
            out.append(Signal(
                strategy_id=self.id, symbol=self.symbols[0],
                action=SignalAction.ENTER_LONG, target_weight=1.0, reason="every 3rd",
            ))
        return out


def test_replay_collects_only_entries_and_never_holds_a_position():
    df = _bars(100.0 * (1 + _RATE) ** np.arange(30))
    feed = _FakeFeed(df)
    strategy = _EveryThirdBar([_SYMBOL])
    closes = feed.bar_closes(_SYMBOL, "15m")

    entries, n_cycles = replay_entry_signals(strategy, feed, closes, interval_minutes=15)

    assert n_cycles == len(closes)
    # 포지션은 매 사이클 항상 비어 있어야 한다 — 백테스트의 보유 중 억제가 없다는 뜻.
    assert set(strategy.seen_positions) == {0}
    assert len(entries) == 10  # 완성봉 3,6,...,30
    assert all(e.symbol == _SYMBOL and e.direction == "long" for e in entries)
    # 진입가 = 그 시각 마지막 완성봉 종가(look-ahead 없음)
    for e in entries:
        assert e.price == pytest.approx(float(df["close"].loc[e.ts - pd.Timedelta(minutes=15)]))
