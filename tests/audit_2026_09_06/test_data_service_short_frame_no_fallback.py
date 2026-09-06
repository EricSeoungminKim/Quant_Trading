"""2026-09-06 안정성 감사 — P1. `MarketDataService._history_raw()`(quant/adapters/
data/service.py)는 소스가 **예외 없이** 요청보다 적은 봉을 돌려줘도 그걸 성공으로
받아들이고 다음 우선순위 소스로 폴백하지 않는다.

`_history_raw`(현재 감사 시점 기준 quant/adapters/data/service.py:342-370, 특히
루프 본문은 350-367 — 감사 지시서에 적힌 329-344는 그 사이 다른 커밋(봉 내부 체결
모델 등)으로 줄 번호가 밀린 구버전 참조다)는 각 라우트에 대해:

    result = route.source.history(symbol, interval, n + 1)
    ...
    self._record_success(route.name)
    return _normalize_frame(result)

처럼 **예외가 나지 않으면 그 즉시 반환**한다. `len(result)`가 요청한 `n + 1`보다
훨씬 적어도(예: Toss가 신규 상장·거래정지·일시적 응답 절단 등으로 30개 요청에 5개만
반환) 그건 "실패"가 아니라 "성공"으로 기록되고, 낮은 우선순위 소스(예: 로컬 Parquet
히스토리)로 폴백을 시도하지 않는다. `health()`의 `degraded` 플래그도 켜지지 않는다
— 순전히 조용한 데이터 저하다.

파급: 이 결과는 `_finalize_bars`가 `tail(n)`으로 자르기 전에 이미 부족하므로,
ATR/RSI/눌림목 폭 등 창(window) 기반 지표를 쓰는 전략(pullback_impulse/vol_breakout/
gap_fade/letf_pair 등 다수)이 표본 부족으로 계산을 왜곡하거나(예: ATR을 실제보다
좁게 추정 → 손절폭 과소 산정), 손절 쿨다운의 "N봉 경과" 판정(`RiskManagerImpl.
approve()`의 `_bar_ts`)이 실제보다 적은 봉으로 세져 쿨다운이 의도보다 일찍 풀릴 수
있다. 어느 경로든 사용자에게 보이는 신호가 전혀 없다.

**2026-09-06 수정 완료**: `_history_raw`는 이제 반환된 봉 수가
`expected_floor = max(2, ceil(n*0.5))` 미만이면 "성공했지만 부족함"으로 보고
다음 라우트를 계속 시도하고, 모든 라우트가 부족해도 그중 가장 긴 프레임을 쓴다
(예외를 던지지 않는다). 아래 테스트가 그 계약을 고정한다(과거엔 xfail이었다 —
이제 통과한다). "오늘은 폴백이 아예 시도되지 않는다"를 고정하던 특성화 테스트는
그 버그 자체가 없어졌으므로 제거했다."""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from quant.adapters.data.service import Capability, MarketDataService, SourceRoute

_OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


class _FakeClock:
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now


class _FakeSource:
    def __init__(self, history_df: pd.DataFrame):
        self._history_df = history_df
        self.history_calls: list[tuple[str, str, int]] = []

    def quote(self, symbol: str):
        return None

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        self.history_calls.append((symbol, interval, n))
        return self._history_df  # 예외 없음 — 그냥 짧게 돌려준다(오늘의 시나리오)


def _bars(start: str, periods: int) -> pd.DataFrame:
    idx = pd.date_range(start, periods=periods, freq="15min", tz="UTC")
    prices = [100.0 + i for i in range(periods)]
    return pd.DataFrame({
        "open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
        "close": prices, "volume": [10.0] * periods,
    }, index=idx)


def _service(short_source: _FakeSource, full_source: _FakeSource) -> MarketDataService:
    # short_source가 더 높은 우선순위(리스트 앞) — Toss처럼 1순위지만 짧은 히스토리만
    # 있는 소스를 흉내낸다. full_source는 로컬 Parquet 히스토리처럼 깊은 과거를 가진
    # 낮은 우선순위 폴백 소스.
    return MarketDataService(
        routes=[
            SourceRoute(name="short_primary", source=short_source, capabilities=frozenset({Capability.BARS})),
            SourceRoute(name="deep_fallback", source=full_source, capabilities=frozenset({Capability.BARS})),
        ],
        clock=_FakeClock(datetime(2024, 1, 3, 0, 0, tzinfo=UTC)),
    )


def test_short_frame_from_primary_falls_back_to_deeper_source():
    """바람직한 계약(2026-09-06 수정 완료): 1순위 소스가 예외 없이 30개 요청에
    5개만 돌려주면, 더 깊은 히스토리를 가진 2순위 소스로 폴백해 요청한 개수를
    채운다."""
    short_source = _FakeSource(_bars("2024-01-02T09:30", 5))
    full_source = _FakeSource(_bars("2023-12-01T09:30", 500))

    svc = _service(short_source, full_source)
    result = svc.history("TQQQ", "15m", 30)

    assert short_source.history_calls == [("TQQQ", "15m", 31)]
    assert full_source.history_calls != []  # 짧은 응답도 폴백을 유발한다
    assert len(result) == 30
    assert svc.health().degraded is False  # 폴백이 결국 성공했으므로 degraded 아님


def test_all_routes_short_returns_the_longest_without_raising():
    """모든 라우트가 부족해도 예외를 던지지 않고 그중 가장 긴 프레임을 쓴다 —
    데이터 없음보다 짧은 데이터가 낫다."""
    shorter = _FakeSource(_bars("2024-01-02T09:30", 3))
    longer = _FakeSource(_bars("2024-01-02T08:00", 10))

    svc = _service(shorter, longer)
    result = svc.history("TQQQ", "15m", 30)

    assert len(result) == 10  # 둘 다 짧지만 longer(10개)가 shorter(3개)보다 낫다


def test_primary_long_enough_is_used_without_calling_fallback():
    """1순위 소스가 이미 충분히 길면(임계 이상) 2순위는 아예 호출되지 않는다 —
    폴백 로직이 불필요한 네트워크 호출을 만들면 안 된다."""
    long_source = _FakeSource(_bars("2024-01-01T09:30", 31))
    full_source = _FakeSource(_bars("2023-12-01T09:30", 500))

    svc = _service(long_source, full_source)
    result = svc.history("TQQQ", "15m", 30)

    assert full_source.history_calls == []
    assert len(result) == 30
