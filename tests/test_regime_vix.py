"""VIX 스트레스 게이트(2026-09-06, 방어 전용) — quant-backtest
results/letf/SUMMARY_vix_regime.md(사전등록 리서치, report-only)를 근거로 추가한
US 전용 방어 게이트. 이 파일이 검증하는 계약:

- `vix_stress()`(quant/trade/regime/indicators.py): 순수 함수, 사전등록 임계
  (level>=25 OR ratio>=1.20)로 stress bool을 낸다. 룩어헤드 없음 — 함수는 주어진
  시리즈의 마지막 값만 "가장 최근 완결 종가"로 쓰고, 그 이후 값을 스스로 지어내지
  않는다.
- `RegimeProvider._apply_vix_gate()`: 점수 합산(_finalize) **뒤에** 적용하는
  후처리 게이트. risk_multiplier를 절대 올리지 않는다 — aggressive를 neutral로
  상한하거나(공격 금지), 기존 변동성 위험회피 신호(qqq_volatility_score==-1,
  연구가 "기존 vol 플래그"라 부르는 그 지표)와 함께 뜨면 defensive로 강등한다.
- 데이터 없음/낡음(3세션 초과)/비활성화 → 거래를 막지 않는다(건너뛰고 사유만
  남긴다).
- 백필: server/scripts/backfill_us_daily.sh가 VIX도 QQQ와 같은 패턴으로 받는다.
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.trade.regime.indicators import IndicatorResult, vix_stress
from quant.trade.regime.models import RegimeState
from quant.trade.regime.provider import (
    DEFAULT_VIX_LEVEL_MAX,
    DEFAULT_VIX_SMA20_RATIO_MAX,
    STALE_VIX_SESSIONS_AFTER,
    RegimeProvider,
)

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- helpers

def _write_vix_daily(root: Path, closes: list[float], last_bar: str, tz: str | None = "UTC") -> None:
    """마지막 봉이 `last_bar`인 VIX 일봉 파티션 — test_regime_freshness의
    `_write_qqq_daily`와 같은 관례."""
    idx = pd.bdate_range(end=last_bar, periods=len(closes), tz=tz)
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [0.0] * len(closes),
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = root / "VIX" / "1d" / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _write_qqq_daily(root: Path, closes: list[float], last_bar: str) -> None:
    idx = pd.bdate_range(end=last_bar, periods=len(closes))
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(closes),
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = root / "QQQ" / "1d" / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


# ======================================================================= vix_stress (순수 함수)

def test_vix_stress_calm_is_not_stress():
    closes = pd.Series([20.0] * 19 + [20.5])  # level 20.5, sma20 ~20.03 -> ratio ~1.02
    result = vix_stress(closes)
    assert result["stress"] is False
    assert result["level"] == pytest.approx(20.5)
    assert "정상" in result["reason"]
    assert "20일선 대비" in result["reason"]


def test_vix_stress_level_threshold_alone_triggers():
    """레벨만 25 이상이면 비율과 무관하게 스트레스 — OR 조건."""
    closes = pd.Series([25.0] * 20)  # ratio == 1.0 (비율은 정상권), level == 25 (경계)
    result = vix_stress(closes)
    assert result["stress"] is True
    assert "스트레스" in result["reason"]
    assert "공격 금지" in result["reason"]


def test_vix_stress_ratio_threshold_alone_triggers():
    """레벨은 25 미만이어도 20일선 대비 1.20배 이상이면 스트레스 — OR 조건."""
    closes = pd.Series([15.0] * 19 + [20.0])  # level=20 (<25), ratio=20/15.25≈1.31 (>=1.20)
    result = vix_stress(closes)
    assert result["level"] == 20.0
    assert result["vs_sma20"] >= 1.20
    assert result["stress"] is True
    assert "배" in result["reason"]  # 스트레스 문구는 "n배" 표기(정상 문구의 "20일선 대비"와 다름)


def test_vix_stress_below_both_thresholds_is_normal():
    closes = pd.Series([20.0] * 19 + [22.0])  # level=22 (<25), ratio=22/20.1≈1.09 (<1.20)
    result = vix_stress(closes)
    assert result["stress"] is False
    assert "정상" in result["reason"]


def test_vix_stress_insufficient_data_is_none_not_stress():
    closes = pd.Series([30.0] * 5)  # sma_window(기본 20) 미만
    result = vix_stress(closes)
    assert result["level"] is None
    assert result["vs_sma20"] is None
    assert result["stress"] is False  # 판단 불가 ≠ 스트레스(억지로 단정하지 않는다)
    assert "판단 불가" in result["reason"]


def test_vix_stress_none_series_is_insufficient_data():
    result = vix_stress(None)
    assert result["stress"] is False
    assert result["level"] is None


def test_vix_stress_respects_custom_thresholds():
    closes = pd.Series([20.0] * 20)  # level=20, ratio=1.0 — 기본 임계로는 정상
    assert vix_stress(closes)["stress"] is False
    assert vix_stress(closes, level_max=15.0)["stress"] is True  # 레벨 임계를 낮추면 걸린다
    assert vix_stress(closes, sma20_ratio_max=0.5)["stress"] is True  # 비율 임계를 낮춰도 걸린다


def test_vix_stress_default_thresholds_match_pre_registered_study():
    """settings.yaml regime.us.vix의 코드 기본값 — 25 / 1.20."""
    assert DEFAULT_VIX_LEVEL_MAX == 25.0
    assert DEFAULT_VIX_SMA20_RATIO_MAX == 1.20


# ------------------------------------------------------------ no look-ahead

def test_vix_stress_series_through_yesterday_does_not_see_todays_spike():
    """룩어헤드 없음 계약: 시리즈의 **마지막 값**만 "가장 최근 완결 종가"로 쓴다.

    어제까지의(오늘 스파이크를 제외한) 시리즈를 넘기면 오늘 스파이크는 반영되지
    않는다 — 함수 자신은 미래를 지어내지 않는다. "어제까지만 넘긴다"는 책임은
    호출부(RegimeProvider._load_vix_daily_closes, 로컬 파티션은 백필이 완결한
    세션만 담는다)에 있다.
    """
    calm_through_yesterday = pd.Series([15.0] * 20)
    with_todays_spike = pd.concat([calm_through_yesterday, pd.Series([31.0])], ignore_index=True)

    result_yesterday = vix_stress(calm_through_yesterday)
    assert result_yesterday["stress"] is False

    result_with_today = vix_stress(with_todays_spike)
    assert result_with_today["stress"] is True  # 오늘 값을 실수로 얹으면 결과가 바뀐다는 대조군


# ======================================================================= provider: 백필/신선도

def test_stale_vix_data_is_skipped_with_warning_never_blocks(tmp_path, caplog):
    """3세션 초과로 낡은 VIX는 건너뛴다 — 절대 거래(국면 계산 자체)를 막지 않는다."""
    now = datetime(2026, 1, 12, 8, 0, tzinfo=KST)  # 월요일 아침
    # QQQ는 신선하게 둬서(aggressive 게이트를 낮춰) refresh()가 정상 완주하는지 본다.
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0] * 20, last_bar="2026-01-09")
    # VIX는 1/2(금)이 마지막 — 1/12(월) 아침이면 1/5,1/6,1/7,1/8,1/9 5세션 경과(>3, STALE)
    _write_vix_daily(tmp_path / "history", [15.0] * 20, last_bar="2026-01-02")

    provider = RegimeProvider(
        settings={"regime": {"min_valid_indicators": 0}},
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: now,
    )
    with caplog.at_level("WARNING"):
        state = provider.refresh()

    assert any("VIX" in r and "낡음" in r for r in state.reasons)
    assert any("VIX" in r.getMessage() and "낡음" in r.getMessage() for r in caplog.records)
    # 낡은 VIX 하나만으로 판단 자체가 막히지 않는다(degraded는 다른 지표 부족 여부로만 결정).
    assert state.label in {"defensive", "neutral", "aggressive"}


def test_missing_vix_data_is_skipped_not_an_error(tmp_path):
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0] * 20, last_bar="2026-01-09")
    provider = RegimeProvider(
        settings={"regime": {"min_valid_indicators": 0}},
        history_dir=tmp_path / "history",  # VIX 파티션 자체가 없음
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 12, 8, 0, tzinfo=KST),
    )
    state = provider.refresh()  # 예외 없이 완주해야 한다
    assert any("VIX" in r and ("없음" in r or "제외" in r) for r in state.reasons)


def test_stale_threshold_is_three_sessions():
    assert STALE_VIX_SESSIONS_AFTER == 3


def test_vix_disabled_via_settings_is_skipped(tmp_path):
    _write_vix_daily(tmp_path / "history", [15.0] * 19 + [40.0], last_bar="2026-01-09")  # 스트레스감
    provider = RegimeProvider(
        settings={"regime": {"us": {"vix": {"enabled": False}}}},
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST),
    )
    reason, stress = provider._vix_indicator()
    assert stress is None
    assert "비활성화" in reason


def test_vix_settings_override_thresholds(tmp_path):
    _write_vix_daily(tmp_path / "history", [20.0] * 20, last_bar="2026-01-09")  # 기본 임계로는 정상
    provider = RegimeProvider(
        settings={"regime": {"us": {"vix": {"level_max": 15}}}},
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST),
    )
    _reason, stress = provider._vix_indicator()
    assert stress is True  # settings가 기본값(25) 대신 15를 강제


def test_vix_default_settings_are_25_and_1_2(tmp_path):
    """settings 없이 만들면 코드 기본값(레벨 25 / 비율 1.20)을 쓴다."""
    _write_vix_daily(tmp_path / "history", [24.0] * 20, last_bar="2026-01-09")  # 24 < 25, ratio~1.0
    provider = RegimeProvider(
        settings={},
        history_dir=tmp_path / "history",
        state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST),
    )
    _reason, stress = provider._vix_indicator()
    assert stress is False


# ======================================================================= provider: 방어 전용 게이트

def _base_state(label: str, computed_at: datetime) -> RegimeState:
    return RegimeState(label=label, risk_multiplier={"defensive": 0.5, "neutral": 1.0, "aggressive": 1.5}[label],
                       reasons=["(기존 지표 사유)"], computed_at=computed_at, degraded=False)


def _bare_provider(tmp_path: Path) -> RegimeProvider:
    return RegimeProvider(
        settings={}, history_dir=tmp_path / "history", state_path=tmp_path / "state" / "regime.json",
        now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST),
    )


def test_stress_caps_aggressive_to_neutral_without_vol_flag(tmp_path, monkeypatch):
    provider = _bare_provider(tmp_path)
    monkeypatch.setattr(provider, "_vix_indicator", lambda: ("VIX 30.0 (1.40배) — 스트레스: 공격 금지", True))
    results = [IndicatorResult("qqq_trend", 1, "추세 상승"), IndicatorResult("qqq_volatility", 1, "안정 국면")]
    state = _base_state("aggressive", datetime(2026, 1, 9, 8, 0, tzinfo=KST))

    gated = provider._apply_vix_gate(state, results)

    assert gated.label == "neutral"  # 공격 금지, 방어까지는 아니다(기존 vol 플래그가 위험선호였으므로)
    assert gated.risk_multiplier == 1.0
    assert any(r.startswith("VIX ") for r in gated.reasons)


def test_stress_plus_existing_vol_flag_forces_defensive(tmp_path, monkeypatch):
    provider = _bare_provider(tmp_path)
    monkeypatch.setattr(provider, "_vix_indicator", lambda: ("VIX 30.0 (1.40배) — 스트레스: 공격 금지", True))
    results = [IndicatorResult("qqq_trend", 0, "횡보"), IndicatorResult("qqq_volatility", -1, "변동성 급등")]
    state = _base_state("neutral", datetime(2026, 1, 9, 8, 0, tzinfo=KST))

    gated = provider._apply_vix_gate(state, results)

    assert gated.label == "defensive"
    assert gated.risk_multiplier == 0.5


def test_stress_plus_vol_flag_never_raises_from_defensive(tmp_path, monkeypatch):
    """방어 전용 — 이미 defensive면 그대로다(올라갈 일이 없다는 걸 확인)."""
    provider = _bare_provider(tmp_path)
    monkeypatch.setattr(provider, "_vix_indicator", lambda: ("VIX 30.0 (1.40배) — 스트레스: 공격 금지", True))
    results = [IndicatorResult("qqq_trend", -1, "추세 하락"), IndicatorResult("qqq_volatility", -1, "변동성 급등")]
    state = _base_state("defensive", datetime(2026, 1, 9, 8, 0, tzinfo=KST))

    gated = provider._apply_vix_gate(state, results)

    assert gated.label == "defensive"
    assert gated.risk_multiplier == 0.5


def test_no_stress_leaves_label_unchanged(tmp_path, monkeypatch):
    provider = _bare_provider(tmp_path)
    monkeypatch.setattr(provider, "_vix_indicator", lambda: ("VIX 15.0 (20일선 대비 0.97) — 정상", False))
    results = [IndicatorResult("qqq_trend", 1, "추세 상승"), IndicatorResult("qqq_volatility", 1, "안정 국면")]
    state = _base_state("aggressive", datetime(2026, 1, 9, 8, 0, tzinfo=KST))

    gated = provider._apply_vix_gate(state, results)

    assert gated.label == "aggressive"
    assert gated.risk_multiplier == 1.5
    assert gated.reasons[-1] == "VIX 15.0 (20일선 대비 0.97) — 정상"


def test_vix_skip_reason_still_appended_and_never_gates(tmp_path, monkeypatch):
    """VIX 판단 불가(None)는 stress=False와 동일하게 취급 — 게이트를 절대 걸지 않는다."""
    provider = _bare_provider(tmp_path)
    monkeypatch.setattr(provider, "_vix_indicator", lambda: ("VIX 일봉 데이터 없음 — 지표 제외", None))
    results = [IndicatorResult("qqq_trend", 1, "추세 상승"), IndicatorResult("qqq_volatility", -1, "변동성 급등")]
    state = _base_state("aggressive", datetime(2026, 1, 9, 8, 0, tzinfo=KST))

    gated = provider._apply_vix_gate(state, results)

    assert gated.label == "aggressive"  # None(판단 불가)은 게이트를 걸지 않는다
    assert gated.reasons[-1] == "VIX 일봉 데이터 없음 — 지표 제외"


# ======================================================================= regime.json 렌더링

def test_regime_json_fixture_includes_vix_line(tmp_path, monkeypatch):
    """캐시 파일(regime.json) 왕복 후에도 VIX 사유 줄이 살아남는다 —
    quant/analyze/tg_digest.py의 program_stance_display()·daily_brief.sh가
    reasons를 그대로 읽어 텔레그램/리포트에 노출한다."""
    _write_qqq_daily(tmp_path / "history", [100.0] * 19 + [103.0] * 20, last_bar="2026-01-09")
    _write_vix_daily(tmp_path / "history", [15.0] * 19 + [16.0], last_bar="2026-01-09")
    state_path = tmp_path / "state" / "regime.json"
    provider = RegimeProvider(
        settings={"regime": {"min_valid_indicators": 0}},
        history_dir=tmp_path / "history", state_path=state_path,
        now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST),
    )
    provider.refresh()

    import json
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert any(r.startswith("VIX ") for r in payload["reasons"])  # 최상위(=US, 하위호환)
    assert any(r.startswith("VIX ") for r in payload["markets"]["US"]["reasons"])

    # 새 RegimeProvider(캐시만 읽음)로도 그대로 복원된다.
    fresh = RegimeProvider(history_dir=tmp_path / "history", state_path=state_path,
                            now_fn=lambda: datetime(2026, 1, 9, 8, 0, tzinfo=KST))
    cached = fresh.current_state()
    assert any(r.startswith("VIX ") for r in cached.reasons)


# ======================================================================= 백필 스크립트

def test_backfill_us_daily_script_is_syntactically_valid():
    script = REPO_ROOT / "server" / "scripts" / "backfill_us_daily.sh"
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_backfill_us_daily_script_fetches_vix_with_yahoo_ticker():
    script = REPO_ROOT / "server" / "scripts" / "backfill_us_daily.sh"
    text = script.read_text(encoding="utf-8")
    assert "--symbol VIX" in text
    assert "STALE_VIX_SESSIONS_AFTER" in text
    assert "notify_defer" in text


def test_backfill_us_daily_script_uses_only_defer_gate():
    """quant/adapters(등) 규칙과 별개로, 이 스크립트가 이미 지켜야 하는
    tests/test_notify_gate.py의 분류(backfill_us_daily=notify_defer)를 VIX 블록도
    어기지 않는지 재확인 — notify_now/notify_auto를 섞어 쓰면 안 된다."""
    script = REPO_ROOT / "server" / "scripts" / "backfill_us_daily.sh"
    text = script.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "notify_now " not in body
    assert "notify_auto " not in body


def test_yahoo_ticker_override_maps_vix_to_caret_vix():
    from quant.collect.quotes.yf_source import _YAHOO_TICKER_OVERRIDES
    assert _YAHOO_TICKER_OVERRIDES["VIX"] == "^VIX"
