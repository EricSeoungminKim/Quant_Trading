"""quant.trade.regime — 지표 채점 규칙(결정론적), 소스 실패 시 중립 강제, 캐시
(같은 날 재계산 안 함/날 바뀌면 재계산), 핫패스 안전(risk_multiplier가 네트워크를
절대 안 부름)을 검증한다. 네트워크 호출 없음 — 전부 합성 데이터/페이크 클라이언트."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.regime.indicators import (
    bitcoin_score,
    bond_yield_score,
    kospi_score,
    qqq_trend_score,
    qqq_volatility_score,
)
from quant.trade.regime.provider import RegimeProvider

KST = ZoneInfo("Asia/Seoul")


# --------------------------------------------------------------------- helpers

def _write_qqq_daily(root: Path, closes: list[float], start: str = "2024-01-01") -> None:
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(closes),
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = root / "QQQ" / "1d" / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


class FakeIndicatorClient:
    """MarketIndicatorClient 페이크 — 호출 여부를 기록해 핫패스 검증에 쓴다."""

    def __init__(self, prices: dict[str, tuple[float, float]] | None = None, raise_on_call: bool = False):
        self._prices = prices or {}
        self.raise_on_call = raise_on_call
        self.calls: list[str] = []

    def indicator_price(self, symbol: str) -> float | None:
        self.calls.append(f"price:{symbol}")
        if self.raise_on_call:
            raise RuntimeError("network down")
        pair = self._prices.get(symbol)
        return pair[0] if pair else None

    def indicator_prev_close(self, symbol: str) -> float | None:
        self.calls.append(f"prev:{symbol}")
        if self.raise_on_call:
            raise RuntimeError("network down")
        pair = self._prices.get(symbol)
        return pair[1] if pair else None


class FakeBitcoinAdapter:
    def __init__(self, change_pct: float | None = None, raise_on_call: bool = False):
        self._change_pct = change_pct
        self.raise_on_call = raise_on_call
        self.calls = 0

    def price_change_pct(self) -> float | None:
        self.calls += 1
        if self.raise_on_call:
            raise RuntimeError("network down")
        return self._change_pct


# ----------------------------------------------------------------- indicators: qqq_trend

def test_qqq_trend_score_uptrend_above_band():
    closes = pd.Series([100.0] * 19 + [103.0])  # last vs 20d MA(~100.15) 크게 위
    result = qqq_trend_score(closes, ma_window=20, band_pct=1.0)
    assert result.score == 1
    assert "추세 상승" in result.reason


def test_qqq_trend_score_downtrend_below_band():
    closes = pd.Series([100.0] * 19 + [96.0])
    result = qqq_trend_score(closes, ma_window=20, band_pct=1.0)
    assert result.score == -1
    assert "추세 하락" in result.reason


def test_qqq_trend_score_within_band_is_neutral():
    closes = pd.Series([100.0] * 19 + [100.3])
    result = qqq_trend_score(closes, ma_window=20, band_pct=1.0)
    assert result.score == 0


def test_qqq_trend_score_insufficient_data_is_none():
    closes = pd.Series([100.0] * 5)
    result = qqq_trend_score(closes, ma_window=20)
    assert result.score is None
    assert "판단 불가" in result.reason


# ------------------------------------------------------------- indicators: qqq_volatility

def test_qqq_volatility_score_spike_is_risk_off():
    # 장기 구간은 완만한 변화, 최근 5일은 큰 진폭으로 튐
    long_part = [100.0 + 0.01 * i for i in range(56)]
    short_part = [long_part[-1], long_part[-1] * 1.05, long_part[-1] * 0.95, long_part[-1] * 1.06, long_part[-1] * 0.94]
    closes = pd.Series(long_part + short_part)
    result = qqq_volatility_score(closes, short_window=5, long_window=60)
    assert result.score == -1
    assert "변동성 급등" in result.reason


def test_qqq_volatility_score_calm_is_risk_on():
    # 장기 구간은 변동이 크고, 최근 5일은 거의 안 움직임
    import math
    long_part = [100.0 * (1 + 0.03 * math.sin(i)) for i in range(56)]
    short_part = [long_part[-1]] * 5
    closes = pd.Series(long_part + short_part)
    result = qqq_volatility_score(closes, short_window=5, long_window=60)
    assert result.score == 1
    assert "안정 국면" in result.reason


def test_qqq_volatility_score_insufficient_data_is_none():
    closes = pd.Series([100.0] * 10)
    result = qqq_volatility_score(closes, short_window=5, long_window=60)
    assert result.score is None


# --------------------------------------------------------------- indicators: bond/kospi/btc

def test_bond_yield_score_rising_is_risk_off():
    assert bond_yield_score(change_bp=5.0, band_bp=3.0).score == -1


def test_bond_yield_score_falling_is_risk_on():
    assert bond_yield_score(change_bp=-5.0, band_bp=3.0).score == 1


def test_bond_yield_score_flat_is_neutral():
    assert bond_yield_score(change_bp=0.5, band_bp=3.0).score == 0


def test_bond_yield_score_none_is_excluded():
    result = bond_yield_score(None)
    assert result.score is None
    assert "지표 제외" in result.reason


def test_kospi_score_up_is_risk_on():
    assert kospi_score(change_pct=1.0, band_pct=0.5).score == 1


def test_kospi_score_down_is_risk_off():
    assert kospi_score(change_pct=-1.0, band_pct=0.5).score == -1


def test_bitcoin_score_none_is_excluded():
    result = bitcoin_score(None)
    assert result.score is None
    assert "제외" in result.reason


def test_bitcoin_score_up_is_risk_on():
    assert bitcoin_score(change_pct=5.0, band_pct=2.0).score == 1


# ------------------------------------------------------------------------- provider: 소스 실패

def test_refresh_all_sources_failing_is_neutral_and_degraded(tmp_path):
    # QQQ 로컬 데이터 없음, indicator_client/bitcoin_adapter 없음 → 지표 전부 None
    provider = RegimeProvider(
        settings={},
        indicator_client=None,
        bitcoin_adapter=None,
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "neutral"
    assert state.risk_multiplier == 1.0
    assert state.degraded is True
    assert len(state.reasons) > 0


def test_refresh_partial_source_failure_still_computes(tmp_path):
    # start 를 now(2026-01-05) 바로 앞으로 둔다 — 기본값(2024-01-01)이면 봉이 2년
    # 낡아 신선도 가드에 걸리고, 이 테스트가 검증하려는 것("로컬 지표는 살아있다")이
    # 아니라 가드 동작을 시험하게 된다. 낡은 봉 쪽은 test_regime_freshness.py 가 본다.
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0], start="2025-12-08")
    client = FakeIndicatorClient(raise_on_call=True)  # 원격 지표는 전부 실패
    # 이 20봉 데이터는 qqq_trend만 계산되고 qqq_volatility는 61봉 미만이라 None —
    # 유효 지표가 1개뿐이라 min_valid_indicators(기본 2) 게이트에 걸린다. 이
    # 테스트가 보려는 건 그 게이트가 아니라 "로컬 지표는 원격과 무관하게 산다"는
    # 것이므로 게이트를 끈다(게이트 자체는 test_min_valid_indicators_gate_*가 본다).
    settings = {"regime": {"min_valid_indicators": 0}}
    provider = RegimeProvider(
        settings=settings,
        indicator_client=client,
        bitcoin_adapter=None,
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.degraded is False  # 로컬 지표는 살아있음
    assert any("판단 불가" in r or "실패" in r or "제외" in r for r in state.reasons)


# ------------------------------------------------------------------------------ provider: 캐시

def test_refresh_same_day_does_not_recompute(tmp_path):
    calls = {"n": 0}

    class CountingClient(FakeIndicatorClient):
        def indicator_price(self, symbol):
            calls["n"] += 1
            return super().indicator_price(symbol)

    client = CountingClient(prices={"KR_BOND_10Y": (3.0, 3.0), "KOSPI": (2800.0, 2800.0)})
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider = RegimeProvider(
        settings={}, indicator_client=client, history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json", now_fn=lambda: now,
    )
    state1 = provider.refresh()
    calls_after_first = calls["n"]
    assert calls_after_first > 0

    state2 = provider.refresh()  # 같은 날 — 재계산 안 함
    assert calls["n"] == calls_after_first
    assert state2.computed_at == state1.computed_at


def test_refresh_new_day_recomputes(tmp_path):
    calls = {"n": 0}

    class CountingClient(FakeIndicatorClient):
        def indicator_price(self, symbol):
            calls["n"] += 1
            return super().indicator_price(symbol)

    client = CountingClient(prices={"KR_BOND_10Y": (3.0, 3.0), "KOSPI": (2800.0, 2800.0)})
    day1 = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    day2 = day1 + timedelta(days=1)
    now = {"value": day1}
    provider = RegimeProvider(
        settings={}, indicator_client=client, history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json", now_fn=lambda: now["value"],
    )
    provider.refresh()
    calls_after_first = calls["n"]

    now["value"] = day2
    provider.refresh()
    assert calls["n"] > calls_after_first  # 날짜가 바뀌었으니 재계산됐어야 함


def test_refresh_force_recomputes_same_day(tmp_path):
    calls = {"n": 0}

    class CountingClient(FakeIndicatorClient):
        def indicator_price(self, symbol):
            calls["n"] += 1
            return super().indicator_price(symbol)

    client = CountingClient(prices={"KR_BOND_10Y": (3.0, 3.0), "KOSPI": (2800.0, 2800.0)})
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider = RegimeProvider(
        settings={}, indicator_client=client, history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json", now_fn=lambda: now,
    )
    provider.refresh()
    calls_after_first = calls["n"]
    provider.refresh(force=True)
    assert calls["n"] > calls_after_first


def test_new_provider_instance_reads_cache_from_disk(tmp_path):
    """캐시는 프로세스 메모리가 아니라 파일에 있으므로, 새 인스턴스도 오늘 계산분을
    재사용해야 한다(재계산 안 함)."""
    state_path = tmp_path / "state" / "regime.json"
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider1 = RegimeProvider(
        settings={}, history_dir=tmp_path / "history", state_path=state_path, now_fn=lambda: now,
    )
    state1 = provider1.refresh()

    provider2 = RegimeProvider(
        settings={}, history_dir=tmp_path / "history", state_path=state_path, now_fn=lambda: now,
    )
    state2 = provider2.current_state()
    assert state2 is not None
    assert state2.label == state1.label
    assert state2.computed_at == state1.computed_at


# ------------------------------------------------------------------------- provider: 핫패스 안전

def test_risk_multiplier_never_calls_network(tmp_path):
    client = FakeIndicatorClient(raise_on_call=True)
    bitcoin = FakeBitcoinAdapter(raise_on_call=True)
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider = RegimeProvider(
        settings={}, indicator_client=client, bitcoin_adapter=bitcoin,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: now,
    )
    provider.refresh()  # 여기서만 (실패하는) 네트워크 호출이 일어남
    client.calls.clear()
    bitcoin.calls = 0

    for _ in range(5):
        provider.risk_multiplier()

    assert client.calls == []
    assert bitcoin.calls == 0


def test_risk_multiplier_before_refresh_reads_cache_file_only(tmp_path):
    """refresh() 없이 새 프로세스가 뜬 상황(캐시 파일만 있는 상태)을 흉내낸다.
    risk_multiplier()가 파일은 읽어도 되지만 네트워크는 절대 부르면 안 된다."""
    state_path = tmp_path / "state" / "regime.json"
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    seeder = RegimeProvider(settings={}, history_dir=tmp_path / "history", state_path=state_path, now_fn=lambda: now)
    seeder.refresh()

    client = FakeIndicatorClient(raise_on_call=True)
    fresh = RegimeProvider(
        settings={}, indicator_client=client, history_dir=tmp_path / "history",
        state_path=state_path, now_fn=lambda: now,
    )
    multiplier = fresh.risk_multiplier()
    assert client.calls == []
    assert isinstance(multiplier, float)


def test_risk_multiplier_with_no_cache_and_no_refresh_is_neutral_default(tmp_path):
    provider = RegimeProvider(
        settings={}, history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
    )
    assert provider.risk_multiplier() == 1.0


# ------------------------------------------------------------- provider: 최소 유효 지표 게이트

def test_two_of_five_valid_hits_threshold_without_gate(tmp_path):
    """2026-08-18 실측 재현, 지금은 고정된 회귀 테스트: 로컬 지표 2개(qqq_trend/
    qqq_volatility)만 유효하고 원격 3개(bond/kospi/bitcoin)가 전부 실패한 채로
    유효 2개가 aggressive_min_score(기본 2)에 도달한 그 상황 자체다 — 이게
    2026-08-18에 US 국면이 하루 종일 aggressive(1.5x)를 유지한 실제 사고 재현.

    2026-08-19 이전에는 min_valid_indicators 게이트(기본 2)를 꺼도(=0) 이 상황이
    그대로 aggressive를 냈다(이 테스트가 그 사실을 규약으로 고정했었다). 이제는
    aggressive 전용 2단계 게이트(aggressive_min_valid 기본 3, aggressive_min_sources
    기본 2)가 추가돼, 유효 지표 2개가 전부 qqq_trend/qqq_volatility(같은 원천
    QQQ 1개)뿐이라 개수(2<3)·원천(1<2) 둘 다 미달 — neutral로 강등되고 사유에
    남는다. min_valid_indicators는 여전히 0으로 꺼 둔 채라(이 게이트가 아니라
    새 게이트가 잡는다는 걸 보여주려는 목적) 확인한다."""
    flat1 = [100.0] * 41
    ramp = [100.0 + (106.0 - 100.0) * i / 15 for i in range(1, 16)]
    flat2 = [ramp[-1]] * 5
    _write_qqq_daily(tmp_path / "history", flat1 + ramp + flat2, start="2025-10-13")
    client = FakeIndicatorClient(raise_on_call=True)  # 원격 지표 전부 실패
    settings = {"regime": {"min_valid_indicators": 0}}  # 옛 게이트만 비활성
    provider = RegimeProvider(
        settings=settings, indicator_client=client, bitcoin_adapter=None,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "neutral"
    assert state.risk_multiplier == 1.0
    assert state.degraded is True
    assert any("공격 강등" in r and "원천" in r for r in state.reasons)


def test_min_valid_indicators_gate_forces_neutral_when_below_floor(tmp_path):
    """qqq_volatility가 판단 불가(short_window 미만)라 로컬 지표가 1개(qqq_trend)만
    남고, 원격 3개도 전부 실패하면 유효 지표는 총 1개 — 기본 min_valid_indicators(2)
    미만이라 점수 합산 없이 강제로 neutral+degraded."""
    # 20영업일만 있으면 qqq_trend는 계산되지만 qqq_volatility는 long_window+1(61)
    # 미만이라 None이 된다.
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0], start="2025-12-08")
    client = FakeIndicatorClient(raise_on_call=True)
    provider = RegimeProvider(
        settings={},  # min_valid_indicators 기본값(2) 사용
        indicator_client=client, bitcoin_adapter=None,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "neutral"
    assert state.degraded is True
    assert state.risk_multiplier == 1.0


def test_min_valid_indicators_gate_allows_two_valid_through(tmp_path):
    """유효 지표가 정확히 min_valid_indicators(2)와 같으면 그 1단계 게이트는
    통과해 평소대로 점수 합산으로 판단한다 — 게이트는 "미만"만 막는다
    (2026-08-18 실측이 바로 이 경계값이었다: 딱 2개 유효 → aggressive_min_score
    도달. settings.yaml 주석에 이 한계를 적어 뒀다).

    이 테스트가 보려는 건 min_valid_indicators 경계값이지 2026-08-19에 추가된
    aggressive 전용 2단계 게이트(aggressive_min_valid/aggressive_min_sources)가
    아니다 — 그 게이트는 이 정확한 시나리오(유효 2개, 같은 원천 QQQ 1개)를 잡도록
    새로 설계된 것이라 기본값을 그대로 두면 여기서도 neutral로 강등돼 이 테스트가
    검증하려는 것과 다른 것을 검증하게 된다. 그래서 2단계 게이트만 명시적으로
    낮춰 격리한다(1단계 게이트는 여전히 기본값 2로 살아 있음).
    qqq_trend/qqq_volatility 둘 다 실제로 계산 가능하도록(61봉 이상 필요) 완만한
    상승 후 최근 5일 횡보 패턴을 쓴다 — 둘 다 +1로 합계 2에 도달."""
    flat1 = [100.0] * 41
    ramp = [100.0 + (106.0 - 100.0) * i / 15 for i in range(1, 16)]
    flat2 = [ramp[-1]] * 5
    _write_qqq_daily(tmp_path / "history", flat1 + ramp + flat2, start="2025-10-13")
    client = FakeIndicatorClient(raise_on_call=True)
    settings = {"regime": {"aggressive_min_valid": 2, "aggressive_min_sources": 1}}
    provider = RegimeProvider(
        settings=settings, indicator_client=client, bitcoin_adapter=None,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.degraded is False
    assert state.label == "aggressive"


# ------------------------------------------------------------------- provider: 설정 오버라이드

def test_custom_multipliers_and_thresholds_from_settings(tmp_path):
    settings = {
        "regime": {
            "risk_multipliers": {"defensive": 0.25, "neutral": 1.0, "aggressive": 1.5},
            "aggressive_min_score": 1,
            # 이 테스트는 커스텀 배수/임계 오버라이드를 보려는 것이지 min_valid_
            # indicators 게이트를 보려는 게 아니다 — KOSPI 하나만 유효한 채로도
            # 원래 의도(aggressive_min_score=1)대로 통과시키려면 게이트를 낮춘다.
            "min_valid_indicators": 1,
            # 2026-08-19: aggressive 전용 2단계 게이트(기본 3개/2원천)도 같은 이유로
            # 격리한다 — 이 테스트는 유효 지표 1개(KOSPI, 원천 1개)뿐이라 기본값이면
            # 항상 걸려 커스텀 배수 검증이라는 원래 목적과 무관한 이유로 실패한다.
            "aggressive_min_valid": 1,
            "aggressive_min_sources": 1,
        }
    }
    client = FakeIndicatorClient(prices={"KOSPI": (2828.0, 2800.0)})  # +1% → risk-on
    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider = RegimeProvider(
        settings=settings, indicator_client=client, history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json", now_fn=lambda: now,
    )
    state = provider.refresh()
    assert state.label == "aggressive"
    assert state.risk_multiplier == 1.5


# --------------------------------------------------- provider: aggressive 전용 정보 요건(비대칭)

def test_aggressive_demoted_when_valid_indicators_below_floor_but_defensive_unaffected(tmp_path):
    """2026-08-18 실측 사고의 정식 회귀 테스트(CLAUDE.md 지시 §검증 요구 1번) —
    qqq_trend +1, qqq_volatility +1, 나머지 3개(bond/kospi/bitcoin) 전부 실패인
    채로 min_valid_indicators(1단계, 기본 2)는 통과하지만, 유효 지표 2개가 전부
    같은 원천(QQQ) 뿐이라 aggressive 전용 2단계 게이트(aggressive_min_valid=3
    미달, aggressive_min_sources=2 미달)에 걸려 aggressive가 아니라 neutral로
    강등되고 degraded=True, 사유에 원천 부족이 남는다."""
    flat1 = [100.0] * 41
    ramp = [100.0 + (106.0 - 100.0) * i / 15 for i in range(1, 16)]
    flat2 = [ramp[-1]] * 5
    _write_qqq_daily(tmp_path / "history", flat1 + ramp + flat2, start="2025-10-13")
    client = FakeIndicatorClient(raise_on_call=True)  # bond/kospi 전부 실패
    provider = RegimeProvider(
        settings={}, indicator_client=client, bitcoin_adapter=None,  # bitcoin 도 미구현=실패
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "neutral"
    assert state.risk_multiplier == 1.0
    assert state.degraded is True
    assert any("원천" in r for r in state.reasons)


def test_defensive_still_fires_with_same_information_shortage_that_blocks_aggressive(tmp_path):
    """방어 비대칭 검증(CLAUDE.md 지시 §검증 요구 2번) — 위 테스트와 완전히 같은
    조건(유효 지표는 qqq_trend/qqq_volatility 2개뿐, 같은 원천 QQQ 1개, 나머지
    3개 실패)이지만 방향이 반대(둘 다 -1)라 defensive 임계(-2)에 닿으면, 정보
    부족을 이유로 강등되지 않고 그대로 defensive가 발동해야 한다 — "방어는 부분
    정보로도 그대로 발동한다"는 비대칭 설계(리스크 축소는 정보가 불완전해도
    안전측)를 확인한다.

    qqq_volatility_score의 계산 방식(직전 5일 변동성 vs 60일 변동성) 때문에 단순
    완만한 하락 램프는 항상 volatility=+1(안정)로 나온다 — 마지막 5일이 평탄하면
    short_vol이 0에 수렴해 방향과 무관하게 "안정 국면"이 된다. 대신
    test_qqq_volatility_score_spike_is_risk_off와 같은 급변동 패턴(마지막 값이
    직전 대비 -6% 급락)을 써서 qqq_trend와 qqq_volatility가 둘 다 -1이 되도록
    한다 — 이래야 "정보 부족(같은 원천 QQQ 1개, 유효 2개)" 조건이 동일하게 성립한다."""
    long_part = [100.0 + 0.01 * i for i in range(56)]
    last = long_part[-1]
    short_part = [last, last * 1.05, last * 0.95, last * 1.06, last * 0.94]
    _write_qqq_daily(tmp_path / "history", long_part + short_part, start="2025-10-13")
    client = FakeIndicatorClient(raise_on_call=True)
    provider = RegimeProvider(
        settings={}, indicator_client=client, bitcoin_adapter=None,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "defensive"
    assert state.risk_multiplier == 0.5
    assert state.degraded is False


def test_aggressive_still_blocked_when_three_valid_are_all_same_source(tmp_path):
    """가상 시나리오(2026-08-19 로스터 상대 비율 도입 시 요구된 검증) — 유효
    지표가 3개로 로스터 상대 개수 요건(예: US 로스터 5개면 ceil(5*0.6)=3)을
    충족해도, 그 3개가 전부 같은 원천(QQQ)이면 aggressive_min_sources(기본 2)
    요건에 걸려 여전히 차단돼야 한다 — 로스터 상대 "개수" 요건과 "원천 다양성"
    요건은 서로 다른 축이고, 이 케이스는 원천 요건이 잡는다. 실제 US 지표 풀에는
    QQQ 파생이 qqq_trend/qqq_volatility 2개뿐이라 이 정확한 3-동일원천 상황은
    나올 수 없으므로, _finalize를 직접 호출해 가상 IndicatorResult 3개(모두
    INDICATOR_SOURCE 상 QQQ로 매핑되는 이름)로 재현한다."""
    from quant.trade.regime.indicators import IndicatorResult

    provider = RegimeProvider(settings={}, state_path=tmp_path / "state" / "regime.json")
    results = [
        IndicatorResult("qqq_trend", 1, "가상: QQQ 파생 1"),
        IndicatorResult("qqq_volatility", 1, "가상: QQQ 파생 2"),
        IndicatorResult("qqq_trend", 1, "가상: QQQ 파생 3(동일 원천 재현용 중복)"),
    ]
    state = provider._finalize(results, datetime(2026, 1, 5, 8, 0, tzinfo=KST))
    assert state.label == "neutral"
    assert state.degraded is True
    assert any("원천" in r for r in state.reasons)


def test_aggressive_fires_normally_when_source_diversity_satisfied(tmp_path):
    """원천 다양성 요건 충족 시 정상 발동(CLAUDE.md 지시 §검증 요구 3번) —
    qqq_trend(QQQ) + kospi(KOSPI) + kr_bond_yield(KR_BOND_YIELD) 3개가 모두
    유효하고 서로 다른 원천 3개(요건 2개 이상 충족)라, 유효 지표 개수(3, 요건
    3)도 충족해 aggressive가 강등 없이 정상 발동한다."""
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0], start="2025-12-08")  # qqq_trend +1
    client = FakeIndicatorClient(prices={
        "KOSPI": (2828.0, 2800.0),  # +1% → +1
        "KR_BOND_10Y": (2.90, 2.95),  # -5bp → +1(위험선호)
    })
    provider = RegimeProvider(
        settings={}, indicator_client=client, bitcoin_adapter=None,
        history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 5, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()
    assert state.label == "aggressive"
    assert state.degraded is False


def test_save_cache_is_atomic_no_partial_file_on_crash(tmp_path, monkeypatch):
    """regime.json 은 7개 상태 파일 중 유일하게 write_text 직접이었다(2026-08-21).
    쓰다 죽으면 깨진 JSON 이 남고, 다음 부팅은 '캐시 파싱 실패'로 국면을 잃는다 —
    tmp write + rename(risk/manager._save_day_state 와 동일 패턴)으로 고친다.
    검증: 최종 단계(replace)를 죽여도 원본 파일은 이전 내용 그대로여야 한다."""
    import json as _json

    state_path = tmp_path / "state" / "regime.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"label": "이전값"}', encoding="utf-8")

    now = datetime(2026, 1, 5, 8, 0, tzinfo=KST)
    provider = RegimeProvider(
        settings={}, history_dir=tmp_path / "history", state_path=state_path, now_fn=lambda: now,
    )

    def _boom(self, target):
        raise OSError("디스크 가득참(주입)")

    monkeypatch.setattr(Path, "replace", _boom)
    try:
        provider.refresh(force=True)
    except OSError:
        pass  # 죽는 건 상관없다 — 원본이 살아 있어야 한다는 게 요지

    data = _json.loads(state_path.read_text(encoding="utf-8"))
    assert data == {"label": "이전값"}, "쓰기 실패가 기존 캐시를 깨뜨리면 안 된다"
