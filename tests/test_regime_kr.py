"""KR 국면 분리(2026-08-10) — KR 세션은 KOSPI 프록시 추세 + 투자자 수급으로 판단."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from quant.trade.loop import _execute_signal
from quant.core.ports import Context
from quant.core.models import Quote, Signal, SignalAction
from quant.trade.regime.provider import RegimeProvider


class _FlowClient:
    def __init__(self, *, uptrend=True, net_per_market=int(5e11), candles_raise=False, flow_raise=False):
        self._uptrend = uptrend
        self._net = net_per_market
        self._candles_raise = candles_raise
        self._flow_raise = flow_raise

    def candles(self, symbol, interval="day", count=30):
        if self._candles_raise:
            raise RuntimeError("down")
        base = list(range(100, 130)) if self._uptrend else list(range(130, 100, -1))
        return pd.DataFrame({"close": [float(x) for x in base[:count]]})

    def investor_trading(self, symbol="KOSPI", interval="1d", count=2):
        if self._flow_raise:
            raise RuntimeError("down")
        buy = max(self._net, 0)
        sell = max(-self._net, 0)
        return {"records": [{
            "foreigner": {"buyAmount": str(buy), "sellAmount": str(sell)},
            "institution": {"buyAmount": "0", "sellAmount": "0"},
        }]}


def _provider(tmp_path, client):
    return RegimeProvider(
        settings={"regime": {"risk_multipliers": {"defensive": 0.5, "neutral": 1.0, "aggressive": 1.5},
                             "aggressive_min_score": 2, "defensive_max_score": -2}},
        state_path=tmp_path / "regime.json",
        flow_client=client,
    )


def test_kr_uptrend_and_inflow_is_aggressive(tmp_path):
    """2026-08-19 수정 경위: 이 테스트는 한때
    `test_kr_uptrend_and_inflow_is_defaults_to_neutral_pending_min_valid_override`로
    이름이 바뀌어 "기본 설정으로는 KR이 neutral로 강등된다"를 검증했었다 — 그
    시점의 aggressive 전용 게이트(aggressive_min_valid)가 절대값 3(US 5지표 풀
    기준)이었는데, KR은 지표가 구조적으로 kr_trend/kr_flow 2개뿐이라 3을 영원히
    못 채워 기본 설정에서 KR을 영구 봉쇄하는 부작용이 있었다. 원래 의도(KR
    지표 2개가 둘 다 +1이면 aggressive 발동)를 되돌리기 위해 절대값을 **로스터
    상대 비율**(aggressive_min_valid_ratio, 기본 0.6)로 바꿨다 — KR(로스터 2)은
    ceil(2*0.6)=2 이므로 이제 오버라이드 없이 기본 설정만으로 aggressive가
    정상 발동한다. 원천 요건(kr_trend=KR_TREND, kr_flow=KR_FLOW, 기본 2)도
    이미 서로 다른 원천이라 항상 충족된다. 이름과 단언을 원래 의도로 되돌린다."""
    p = _provider(tmp_path, _FlowClient(uptrend=True, net_per_market=int(5e11)))
    kr = p._compute_kr()
    assert kr.label == "aggressive" and kr.risk_multiplier == 1.5
    assert kr.degraded is False


def test_kr_aggressive_reachable_with_lowered_aggressive_min_valid(tmp_path):
    """2026-08-19: 기본 설정(비율 기반, aggressive_min_valid_ratio=0.6)만으로도
    KR은 이미 aggressive에 도달한다(test_kr_uptrend_and_inflow_is_aggressive 참고).
    이 테스트는 하위호환 경로 — settings에 옛 절대값 aggressive_min_valid를 명시
    하면 비율 대신 그 값을 그대로 하한으로 쓰는 오버라이드 — 가 여전히 동작함을
    확인한다(운영자가 특정 시장만 조이거나 특정 값으로 고정하고 싶을 때의 탈출구)."""
    settings = {"regime": {"risk_multipliers": {"defensive": 0.5, "neutral": 1.0, "aggressive": 1.5},
                           "aggressive_min_score": 2, "defensive_max_score": -2,
                           "aggressive_min_valid": 2}}
    p = RegimeProvider(
        settings=settings, state_path=tmp_path / "regime.json",
        flow_client=_FlowClient(uptrend=True, net_per_market=int(5e11)),
    )
    kr = p._compute_kr()
    assert kr.label == "aggressive" and kr.risk_multiplier == 1.5
    assert not kr.degraded


def test_kr_downtrend_and_outflow_is_defensive(tmp_path):
    p = _provider(tmp_path, _FlowClient(uptrend=False, net_per_market=int(-5e11)))
    kr = p._compute_kr()
    assert kr.label == "defensive" and kr.risk_multiplier == 0.5


def test_kr_mixed_signals_neutral(tmp_path):
    p = _provider(tmp_path, _FlowClient(uptrend=True, net_per_market=int(-5e11)))
    assert p._compute_kr().label == "neutral"


def test_only_one_of_two_kr_indicators_succeeding_now_degrades(tmp_path):
    """2026-08-19 추가: 이전엔 지표 1개(추세)만 성공해도 합계가 aggressive_min(2)
    미만이라 "판단해서 중립"(degraded=False)으로 조용히 넘어갔다. min_valid_
    indicators(기본 2) 게이트로 이제 이 경우를 명시적으로 degraded=True 로 표시한다
    — risk_multiplier 값은 바뀌지 않는다(둘 다 neutral/1.0), 신호(alert 가능 여부)만
    정직해진다."""
    p = _provider(tmp_path, _FlowClient(uptrend=True, net_per_market=int(5e11), flow_raise=True))
    kr = p._compute_kr()
    assert kr.label == "neutral" and kr.risk_multiplier == 1.0
    assert kr.degraded is True


def test_no_flow_client_or_all_failures_degrade_to_neutral(tmp_path):
    p_none = _provider(tmp_path, None)
    kr = p_none._compute_kr()
    assert kr.label == "neutral" and kr.degraded

    p_fail = _provider(tmp_path, _FlowClient(candles_raise=True, flow_raise=True))
    kr2 = p_fail._compute_kr()
    assert kr2.label == "neutral" and kr2.degraded


def test_kr_state_survives_cache_roundtrip_and_us_label_stays_top_level(tmp_path):
    """캐시 라운드트립 배선을 보려는 테스트지 aggressive 게이트를 보려는 게
    아니다 — 2026-08-19 게이트(§검증 요구, aggressive_min_valid 기본 3)가 KR
    2지표 풀에 걸리지 않도록 명시적으로 낮춰 격리한다(위
    test_kr_aggressive_reachable_with_lowered_aggressive_min_valid와 동일 근거)."""
    settings = {"regime": {"risk_multipliers": {"defensive": 0.5, "neutral": 1.0, "aggressive": 1.5},
                           "aggressive_min_score": 2, "defensive_max_score": -2,
                           "aggressive_min_valid": 2}}

    def provider_with(client):
        return RegimeProvider(settings=settings, state_path=tmp_path / "regime.json", flow_client=client)

    p = provider_with(_FlowClient(uptrend=True, net_per_market=int(5e11)))
    p.refresh()
    # 새 인스턴스가 캐시에서 KR 배수 복원 (watch-score 호환: top-level label 유지)
    p2 = provider_with(None)
    assert p2.risk_multiplier("KR") == 1.5
    import json
    data = json.loads((tmp_path / "regime.json").read_text())
    assert "label" in data and data["markets"]["KR"]["label"] == "aggressive"


class _CaptureRisk:
    def __init__(self):
        self.seen_multiplier = None

    def approve(self, signal, ctx, risk_multiplier=1.0, marks=None):
        self.seen_multiplier = risk_multiplier
        return None  # 주문 없음 — 배수 캡처만


class _NullSink:
    def on_signal(self, s): ...
    def on_fill(self, f): ...


def test_execute_signal_resolves_kr_multiplier_by_symbol():
    risk = _CaptureRisk()
    sig_kr = Signal(strategy_id="t", symbol="069500", action=SignalAction.ENTER_LONG, target_weight=1.0)
    _execute_signal(sig_kr, None, risk, _NullSink(), None, mult_by_market={"KR": 0.5, "US": 1.5})
    assert risk.seen_multiplier == 0.5

    sig_us = Signal(strategy_id="t", symbol="TQQQ", action=SignalAction.ENTER_LONG, target_weight=1.0)
    _execute_signal(sig_us, None, risk, _NullSink(), None, mult_by_market={"KR": 0.5, "US": 1.5})
    assert risk.seen_multiplier == 1.5
