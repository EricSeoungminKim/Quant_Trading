"""전역 공유 픽스처. e2e 스위트(tests/e2e/)가 반복적으로 필요로 하는 것만 담는다 —
개별 유닛 테스트 전용 Fake는 각 테스트 파일에 그대로 둔다(기존 관례,
tests/test_donchian.py 등 참고).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

NY = ZoneInfo("America/New_York")


@pytest.fixture(autouse=True)
def isolate_heartbeat_file(tmp_path, monkeypatch):
    """run_paper_loop의 하트비트 상태 파일 기본 경로를 tmp로 돌린다(전 테스트 자동 적용).

    전역인 이유: 이 파일을 격리하지 않으면 루프를 구동하는 아무 테스트나 리포의 실제
    data/state/heartbeat.json에 **방금 시각의 ts**를 남긴다. 외부 워치독은 그걸 보고
    "엔진 살아 있음"으로 판단한다 — 죽은 엔진을 살아 있다고 속이는 파일을 테스트가
    만들어내는 셈이라, 데드맨 스위치가 정확히 막으려던 상황을 테스트가 재현한다."""
    from quant.control import ledger as ledger_module
    from quant.trade import loop as loop_module

    monkeypatch.setattr(loop_module, "DEFAULT_HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    # 같은 이유로 거래 원장도 격리 — 테스트 체결이 실제 누적 스코어보드 데이터를
    # 오염시키면 "측정된 성적이 자본을 결정한다"는 원칙 자체가 무너진다.
    monkeypatch.setattr(ledger_module, "DEFAULT_LEDGER_PATH", tmp_path / "trades.jsonl")


@pytest.fixture
def ny_tz():
    return NY


@pytest.fixture
def fake_clock_cls():
    """Clock Protocol을 만족하는 최소 페이크. now/minutes_to_close/should_flatten을
    생성자 인자로 고정할 수 있다 — tests/test_donchian.py의 FakeClock과 동일한 계약."""

    class FakeClock:
        def __init__(
            self,
            now: datetime,
            minutes_to_close: float | None = 120.0,
            cadence_minutes: float = 15.0,
            market_open: bool = True,
        ):
            self._now = now
            self._minutes_to_close = minutes_to_close
            self._cadence = cadence_minutes
            self._market_open = market_open

        def now(self) -> datetime:
            return self._now

        def is_market_open(self, market: str) -> bool:
            return self._market_open

        def minutes_to_close(self, market: str) -> float | None:
            return self._minutes_to_close

        def cadence_minutes(self) -> float:
            return self._cadence

        def should_flatten(self, market: str, flatten_minutes: float) -> bool:
            mtc = self.minutes_to_close(market)
            return mtc is not None and mtc - self._cadence < flatten_minutes

    return FakeClock


@pytest.fixture
def donchian_params():
    """DonchianStrategy 기본 파라미터 팩토리. tests/test_donchian.py의 _params와
    동일한 기본값 — position_mgmt 없이 진입/청산 핵심 로직만 켠다."""

    def _params(**overrides) -> dict:
        p = dict(
            interval_minutes=15,
            lookback_bars=40,
            volume_mult=1.5,
            stop_fallback_pct=1.5,
            risk_reward=2.0,
            max_concurrent_names=1,
            flatten_before_close_minutes=10,
            min_session_bars_before_entry=0,
        )
        p.update(overrides)
        return p

    return _params


@pytest.fixture
def make_breakout_bars():
    """`interval_minutes` 간격의 완성봉 n개를 만든다. breakout=True면 마지막 봉이
    직전 lookback 고가를 강하게 돌파하고 거래량이 튄다(volume_mult 조건 통과).
    tests/test_donchian.py의 _make_bars를 e2e에서 재사용하기 위해 일반화한 버전."""

    def _make(
        n: int,
        base: float = 100.0,
        breakout: bool = False,
        low_volume: bool = False,
        interval_minutes: int = 15,
        start: datetime | None = None,
    ) -> pd.DataFrame:
        rows = []
        ts0 = start or datetime(2026, 1, 5, 9, 30, tzinfo=NY)
        for i in range(n):
            price = base + i * 0.01
            rows.append({
                "ts": ts0 + timedelta(minutes=interval_minutes * i),
                "open": price, "high": price + 0.05, "low": price - 0.05,
                "close": price, "volume": 1000.0,
            })
        if breakout:
            last_close = rows[-1]["close"] + 5.0
            rows[-1].update(
                close=last_close, high=last_close + 0.1, low=last_close - 1.0,
                volume=1.0 if low_volume else 5000.0,
            )
        return pd.DataFrame(rows).set_index("ts")

    return _make
