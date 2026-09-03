"""롤링 OOS 안정성 하네스(quant.backtest.walkforward) 단위 테스트.

**주의**: 이 모듈은 `quant.research.walkforward`(optuna 파라미터 탐색,
`tests/test_walkforward.py`가 이미 시험 중)와 이름이 비슷하지만 다른 모듈이다 —
여기는 파라미터를 탐색하지 않고 같은 설정을 시간 창만 바꿔가며 반복 실행한다.
파일명을 `test_backtest_walkforward.py`로 분리한 이유는 기존 `test_walkforward.py`
(다른 기능의 이미 존재하던 커버리지)를 덮어쓰지 않기 위함이다.
"""
from __future__ import annotations

import pandas as pd

from quant.backtest.walkforward import rolling_windows, run_walkforward, stability_summary


# ── rolling_windows: 순수 수학, run_backtest 불필요 ──────────────────────────

def test_rolling_windows_non_overlapping_step_equals_window():
    windows = rolling_windows(total_days=360, window_days=90, step_days=90)
    assert windows == [(90, 0), (180, 90), (270, 180), (360, 270)]


def test_rolling_windows_newest_first():
    windows = rolling_windows(total_days=200, window_days=90, step_days=45)
    # end_offset(두 번째 값)이 단조 증가 — 창이 최신에서 과거로 간다
    ends = [end for _, end in windows]
    assert ends == sorted(ends)
    assert ends[0] == 0


def test_rolling_windows_overlap_when_step_smaller_than_window():
    windows = rolling_windows(total_days=180, window_days=90, step_days=45)
    for (start_a, end_a), (start_b, end_b) in zip(windows, windows[1:]):
        assert end_b < start_a  # 다음(더 과거) 창의 end가 이전 창의 start보다 이전


def test_rolling_windows_empty_when_total_shorter_than_window():
    assert rolling_windows(total_days=30, window_days=90, step_days=45) == []


def test_rolling_windows_exact_fit_no_partial_window():
    """total_days가 window_days의 배수가 아니면 마지막 부분 창은 버린다(억지로
    짧은 창을 만들지 않는다)."""
    windows = rolling_windows(total_days=100, window_days=90, step_days=90)
    assert windows == [(90, 0)]  # 두 번째 창(180,90)은 100일을 넘어서 제외


# ── stability_summary: 순수 계산, run_backtest 불필요 ────────────────────────

def _fold(net_bps: float, n_round_trips: int = 30) -> dict:
    return {"net_bps": net_bps, "n_round_trips": n_round_trips}


def test_stability_summary_empty_folds():
    s = stability_summary([])
    assert s["folds"] == 0
    assert s["net_bps_median"] is None
    assert "표본 부족" in s["verdict_hint"]


def test_stability_summary_all_positive():
    s = stability_summary([_fold(5.0), _fold(10.0), _fold(15.0)])
    assert s["folds"] == 3
    assert s["n_positive"] == 3
    assert s["net_bps_median"] == 10.0
    assert s["net_bps_min"] == 5.0
    assert s["net_bps_max"] == 15.0
    assert "안정적일 가능성" in s["verdict_hint"]


def test_stability_summary_all_negative():
    s = stability_summary([_fold(-5.0), _fold(-2.0)])
    assert s["n_positive"] == 0
    assert "엣지 없음 가능성" in s["verdict_hint"]


def test_stability_summary_mixed_flags_instability():
    s = stability_summary([_fold(10.0), _fold(-10.0), _fold(5.0)])
    assert s["n_positive"] == 2
    assert "불안정" in s["verdict_hint"]


def test_stability_summary_flags_thin_folds_without_hiding_median():
    """왕복 10건 미만 fold가 섞이면 sufficient_folds가 그걸 드러내되, 숫자 자체는
    감추지 않는다(지어내지 않는 것과 숨기지 않는 것은 다른 문제)."""
    s = stability_summary([_fold(10.0, n_round_trips=30), _fold(20.0, n_round_trips=2)])
    assert s["sufficient_folds"] == 1
    assert s["folds"] == 2
    assert s["net_bps_median"] is not None
    assert "표본 부족" in s["verdict_hint"]


# ── run_walkforward: run_backtest 를 실제로 호출(stub 소스, 느릴 수 있음) ────

def test_run_walkforward_produces_one_fold_per_window(monkeypatch):
    """run_backtest 를 스텁으로 교체해 엔진을 실제로 돌리지 않고 배선만 검증한다."""
    import quant.backtest.walkforward as wf_mod

    calls = []

    class _FakeResult:
        def __init__(self):
            self.trades = None
            self.metrics = {}
            self.benchmark = {}
            self.strategy_errors = {}

    # **kwargs: run_backtest 에 인자가 늘어도(history_dir 등) 이 가짜가 배선
    # 테스트를 깨뜨리지 않게 한다 — 여기서 보는 계약은 창(days/end) 계산이지
    # 시그니처가 아니다.
    def _fake_run_backtest(strategy_id, days, interval, source, settings_path, end,
                           symbols, **kwargs):
        calls.append({"days": days, "end": end})
        return _FakeResult()

    monkeypatch.setattr(wf_mod, "run_backtest", _fake_run_backtest)

    folds = run_walkforward(
        "donchian", total_days=180, window_days=90, step_days=90,
        anchor=pd.Timestamp("2024-06-01"),
    )
    assert len(folds) == 2
    assert len(calls) == 2
    # 최신 창(end_offset=0)이 anchor 그대로, 다음 창은 그보다 90일 이전
    assert calls[0]["end"] == pd.Timestamp("2024-06-01")
    assert calls[1]["end"] == pd.Timestamp("2024-06-01") - pd.Timedelta(days=90)
    for f in folds:
        assert f["n_fills"] == 0  # _FakeResult는 거래 없음 → fitness.evaluate가 0으로 낸다


def test_run_walkforward_is_deterministic_given_fixed_anchor(monkeypatch):
    import quant.backtest.walkforward as wf_mod

    class _FakeResult:
        def __init__(self):
            self.trades = None
            self.metrics = {}
            self.benchmark = {}
            self.strategy_errors = {}

    def _fake_run_backtest(strategy_id, days, interval, source, settings_path, end,
                           symbols, **kwargs):
        return _FakeResult()

    monkeypatch.setattr(wf_mod, "run_backtest", _fake_run_backtest)

    r1 = run_walkforward("donchian", total_days=90, window_days=90, step_days=90,
                          anchor=pd.Timestamp("2024-06-01"))
    r2 = run_walkforward("donchian", total_days=90, window_days=90, step_days=90,
                          anchor=pd.Timestamp("2024-06-01"))
    assert r1 == r2

# 실제 run_backtest(source="stub")를 돌리는 종단 검증은 pytest 스위트에 넣지
# 않는다 — 이 저장소에 "느린 테스트"용 마커가 없고(등록된 건 `live`뿐, 의미가
# 다르다), 위 모킹 테스트가 배선을 이미 검증한다. 실제 엔진 연동 확인은
# `uv run python -m quant.apps.cli walkforward --source stub`로 수동 실행해 본다
# (CLAUDE.md 완료 기준).
