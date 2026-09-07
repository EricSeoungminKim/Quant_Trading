"""`run_paper_loop`의 설정 핫 리로드 훅이 원장 sink의 파라미터 지문 맵을
갱신하는지 확인한다 (2026-09-07, 진화가능성 평가 투자 #1~#2).

배경: `docs/plans/evolvability-2026-09-07.md`가 지적한 문제 — 장중
`config/settings.yaml`의 전략 파라미터가 바뀌어도 원장 행에는 아무 표시가
안 남아, `scalp_1m`처럼 옛 판본과 새 판본의 체결이 하나의 트랙레코드로
계속 풀렸다(6판본/169트립). `quant/trade/loop.py`가 이미 갖고 있던
`settings.reload_if_changed()` 훅(사이클마다 확인) 직후에, 원장 sink가
노출하는 `refresh_params_fingerprints(strategies_cfg)`를 duck-typing으로
불러 그 시점부터의 체결에 새 지문이 찍히게 한다.

`quant/trade/`는 `quant/control/`을 임포트할 수 없으므로(아키텍처 규칙,
`tests/test_architecture.py`) loop.py 자체는 지문을 계산하지 않는다 — 이
테스트는 그 "부르기만 한다" 계약만 검증한다. 실제 계산 로직(`quant.control.
experiments.strategy_fingerprints`)은 `tests/test_experiments.py`가, 원장
행에 찍히는 것은 `tests/test_ledger.py`가 이미 커버한다.

Fakes는 `tests/test_loop_resilience.py`의 것을 재사용한다(그 파일의 컨벤션,
`tests/test_watchlist_universe.py`가 이미 같은 패턴을 쓴다)."""
from __future__ import annotations

import asyncio
import os
import time

from quant.apps.config import Settings, _read_merged
from quant.core.ports import Context
from quant.trade.control import TradingControl
from tests.test_loop_resilience import (  # noqa: E402  (fakes 재사용)
    FakeBroker,
    FakeClock,
    FakeDataFeed,
    FakeNotifier,
    FakeRisk,
    FakeStrategy,
    _drive_n_cycles,
)

_INITIAL_YAML = (
    "engine:\n  poll_seconds: 5\n"  # _validate_semantics는 poll_seconds<=0을 거부한다
    "strategies:\n"
    "  gap_fade:\n"
    "    class: gap_fade\n"
    "    enabled: true\n"
    "    params:\n"
    "      dip_bps: 30\n"
)
_CHANGED_YAML = (
    "engine:\n  poll_seconds: 5\n"
    "strategies:\n"
    "  gap_fade:\n"
    "    class: gap_fade\n"
    "    enabled: true\n"
    "    params:\n"
    "      dip_bps: 45\n"  # 파라미터가 바뀌었다 — 지문도 바뀌어야 한다
)


class _FingerprintTrackingSink:
    """`TradeLedgerSink`의 지문 관련 표면만 흉내낸 최소 더블 — refresh 호출
    인자를 그대로 기록해 훅이 무엇을 넘겼는지 검증한다."""

    def __init__(self):
        self.signals = []
        self.fills = []
        self.refresh_calls: list[dict] = []

    def on_signal(self, signal) -> None:
        self.signals.append(signal)

    def on_fill(self, fill) -> None:
        self.fills.append(fill)

    def refresh_params_fingerprints(self, strategies_cfg: dict) -> None:
        self.refresh_calls.append(strategies_cfg)


def _touch_future(path) -> None:
    future = time.time() + 5
    os.utime(path, (future, future))


def _make_real_settings(tmp_path, yaml_text: str) -> Settings:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return Settings(_read_merged(path), path)


def test_settings_reload_refreshes_the_ledger_sinks_fingerprint_map(tmp_path):
    settings = _make_real_settings(tmp_path, _INITIAL_YAML)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = _FingerprintTrackingSink()
    strat = FakeStrategy()
    notifier = FakeNotifier()
    control = TradingControl(state_path=tmp_path / "control.json")

    def _after_cycle(n: int) -> None:
        if n == 1:
            # 1사이클 종료 후 파라미터를 바꾸고 mtime을 미래로 밀어 다음 사이클의
            # reload_if_changed()가 변경을 감지하게 한다.
            settings.path.write_text(_CHANGED_YAML, encoding="utf-8")
            _touch_future(settings.path)

    asyncio.run(_drive_n_cycles(
        2, after_cycle=_after_cycle, strategies=[strat], ctx=ctx, risk=risk,
        sinks=sinks, settings=settings, notifier=notifier, control=control,
    ))

    assert sinks.refresh_calls, "설정이 바뀌었는데 원장 sink의 지문 맵이 갱신되지 않음"
    latest = sinks.refresh_calls[-1]
    assert latest["gap_fade"]["params"]["dip_bps"] == 45, (
        "훅이 리로드 *이전* strategies_cfg를 넘겼다 — settings.raw가 이미 "
        "새 값으로 교체된 뒤에 불러야 한다"
    )


def test_no_reload_means_no_refresh_call(tmp_path):
    """설정이 안 바뀌면(mtime 그대로) 매 사이클 재계산하지 않는다 — 쓸데없는
    비용을 사이클마다 물지 않는다."""
    settings = _make_real_settings(tmp_path, _INITIAL_YAML)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = _FingerprintTrackingSink()
    strat = FakeStrategy()
    notifier = FakeNotifier()
    control = TradingControl(state_path=tmp_path / "control.json")

    asyncio.run(_drive_n_cycles(
        3, strategies=[strat], ctx=ctx, risk=risk, sinks=sinks, settings=settings,
        notifier=notifier, control=control,
    ))

    assert sinks.refresh_calls == []


def test_refresh_hook_is_a_noop_when_sink_does_not_expose_it(tmp_path):
    """`refresh_params_fingerprints`를 노출하지 않는 sink(테스트 더블, 구버전
    구성)도 훅이 조용히 건너뛰어야 한다 — duck-typing 계약."""
    from tests.test_loop_resilience import FakeSink

    settings = _make_real_settings(tmp_path, _INITIAL_YAML)
    ctx = Context(clock=FakeClock(), data=FakeDataFeed(), broker=FakeBroker())
    risk = FakeRisk()
    sinks = FakeSink()
    strat = FakeStrategy()
    notifier = FakeNotifier()
    control = TradingControl(state_path=tmp_path / "control.json")

    def _after_cycle(n: int) -> None:
        if n == 1:
            settings.path.write_text(_CHANGED_YAML, encoding="utf-8")
            _touch_future(settings.path)

    # 예외 없이 2 사이클을 완주하면 충분하다 — refresh_params_fingerprints가
    # 없어도 훅이 터지지 않는다는 뜻.
    asyncio.run(_drive_n_cycles(
        2, after_cycle=_after_cycle, strategies=[strat], ctx=ctx, risk=risk,
        sinks=sinks, settings=settings, notifier=notifier, control=control,
    ))
