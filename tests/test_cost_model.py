"""실측 왕복 비용 모델 — **없는 정밀도를 지어내지 않는지**를 시험한다.

비용은 이 저장소에서 부호를 뒤집는 항이다(엣지 8~9bp vs 수수료 14~20bp). 그래서
이 파일이 지키는 것은 정확도만이 아니라 **정직성**이다:

① 표본이 모자라면 숫자가 아니라 None (호출부가 기본값을 쓰고 그걸 밝힌다)
② 슬리피지 표본만 없으면 fee_bp 는 내되 total_bp 는 None ("아는 절반"을 전부인 척 안 함)
③ 잔돈 트립은 제외하고 **몇 건 뺐는지 함께 낸다** (KR 최소수수료가 bp를 폭발시킨다)
④ 수수료 bp는 트립별 bp의 단순평균이 아니라 명목 가중평균
"""
from __future__ import annotations

from quant.control.cost_model import (
    FALLBACK_ROUND_TRIP_BP,
    MIN_LEGS_FOR_SLIPPAGE,
    MIN_TRIPS_FOR_FEE,
    by_market,
    by_strategy,
    effective_round_trip_bp,
    measure,
)


def _trip(notional=1_000_000.0, fees=2_000.0, market="US", strategy="donchian") -> dict:
    """`ledger.round_trips()` 출력의 최소 형태 — 이 모듈이 실제로 읽는 키만."""
    return {
        "strategy": strategy, "symbol": "TQQQ", "market": market,
        "notional": notional, "fees": fees,
    }


def _legs(n: int, bps: float, market="US", strategy="donchian") -> list[dict]:
    """`tca.slippage_bps()` 출력의 최소 형태 — 각 행이 한 leg."""
    return [
        {"bps": bps, "market": market, "strategy_id": strategy, "side": "BUY"}
        for _ in range(n)
    ]


# ── 표본 부족 → None ───────────────────────────────────────────────────────

def test_returns_none_when_not_enough_trips():
    """표본이 모자라면 그럴듯한 숫자 대신 None — 이게 이 모듈의 첫 계약이다."""
    assert measure([_trip() for _ in range(MIN_TRIPS_FOR_FEE - 1)]) is None


def test_returns_none_for_empty_ledger():
    assert measure([]) is None


def test_just_enough_trips_produces_a_number():
    cost = measure([_trip() for _ in range(MIN_TRIPS_FOR_FEE)])
    assert cost is not None and cost.n_trips == MIN_TRIPS_FOR_FEE


# ── 수수료 bp ──────────────────────────────────────────────────────────────

def test_fee_bp_is_notional_weighted_not_a_mean_of_ratios():
    """작은 트립 하나가 평균을 끌고 가면 안 된다.

    명목 100만 × 10건(각 2,000원 = 20bp) + 명목 1억 × 1건(20,000원 = 2bp).
    트립별 bp의 단순평균은 (20×10 + 2)/11 = 18.4bp지만, 실제로 문 비용은
    Σfee/Σnotional = 40,000/110,000,000 = 3.64bp다.
    """
    trips = [_trip(1_000_000.0, 2_000.0) for _ in range(10)]
    trips.append(_trip(100_000_000.0, 20_000.0))
    cost = measure(trips)
    assert cost is not None
    assert round(cost.fee_bp, 2) == 3.64


def test_fee_bp_matches_hand_computed_value():
    """왕복 20bp — 2026-08 원장에서 실제로 관측된 수준(하드코딩 20bp의 출처)."""
    cost = measure([_trip(1_000_000.0, 2_000.0) for _ in range(12)])
    assert cost is not None
    assert cost.fee_bp == 20.0


# ── 잔돈 제외 ──────────────────────────────────────────────────────────────

def test_dust_trips_are_excluded_and_counted():
    """KR 최소 수수료 탓에 잔돈 트립의 bp는 폭발한다 — 빼되 **뺀 사실을 남긴다**."""
    trips = [_trip(1_000_000.0, 2_000.0, market="KR") for _ in range(12)]
    trips += [_trip(1_000.0, 500.0, market="KR") for _ in range(3)]  # 5,000bp짜리 잔돈
    cost = measure(trips)
    assert cost is not None
    assert cost.n_trips == 12 and cost.n_dust_excluded == 3
    assert cost.fee_bp == 20.0  # 잔돈이 섞였다면 훨씬 커졌을 값


def test_zero_notional_trips_do_not_divide_by_zero():
    trips = [_trip() for _ in range(12)] + [_trip(notional=0.0, fees=100.0)]
    cost = measure(trips)
    assert cost is not None and cost.n_trips == 12


# ── 슬리피지 ───────────────────────────────────────────────────────────────

def test_slippage_is_none_when_legs_are_scarce():
    """paper 구간에는 intent 기록이 없다 — 표본 0이 정상이고, 0bp가 아니다."""
    cost = measure([_trip() for _ in range(12)], _legs(MIN_LEGS_FOR_SLIPPAGE - 1, 5.0))
    assert cost is not None
    assert cost.slippage_bp is None
    assert cost.total_bp is None      # 아는 절반(수수료)을 전부인 척하지 않는다
    assert cost.fee_bp == 20.0        # 아는 절반은 그대로 낸다


def test_slippage_doubles_the_leg_average_for_a_round_trip():
    """왕복은 진입 1 leg + 청산 1 leg — leg 평균 × 2."""
    cost = measure([_trip() for _ in range(12)], _legs(MIN_LEGS_FOR_SLIPPAGE, 5.0))
    assert cost is not None
    assert cost.slippage_bp == 10.0
    assert cost.total_bp == 30.0      # 수수료 20 + 슬리피지 10
    assert cost.n_slippage_legs == MIN_LEGS_FOR_SLIPPAGE


# ── 그룹별 ─────────────────────────────────────────────────────────────────

def test_by_market_keeps_kr_and_us_apart():
    """"왕복 20bp" 하나로 뭉치면 두 시장 어디에도 맞지 않을 수 있다."""
    trips = [_trip(1_000_000.0, 2_000.0, market="US") for _ in range(12)]
    trips += [_trip(1_000_000.0, 500.0, market="KR") for _ in range(12)]
    costs = by_market(trips)
    assert costs["US"].fee_bp == 20.0
    assert costs["KR"].fee_bp == 5.0


def test_by_market_reports_none_for_the_thin_side():
    trips = [_trip(market="US") for _ in range(12)]
    trips += [_trip(market="KR") for _ in range(2)]
    costs = by_market(trips)
    assert costs["US"] is not None
    assert costs["KR"] is None  # 표본 2건짜리 시장에 비용을 매기지 않는다


def test_by_strategy_matches_tca_strategy_id_key():
    """트립은 `strategy`, TCA 행은 `strategy_id` — 키 이름이 다르다(붙이는 게 일이다)."""
    trips = [_trip(strategy="orb_scan") for _ in range(12)]
    trips += [_trip(strategy="donchian") for _ in range(12)]
    legs = _legs(MIN_LEGS_FOR_SLIPPAGE, 4.0, strategy="orb_scan")
    costs = by_strategy(trips, legs)
    assert costs["orb_scan"].slippage_bp == 8.0
    assert costs["donchian"].slippage_bp is None  # 이 전략의 leg는 하나도 없다


# ── 호출부 계약 ────────────────────────────────────────────────────────────

def test_effective_bp_falls_back_and_says_so():
    bp, label = effective_round_trip_bp(None)
    assert bp == FALLBACK_ROUND_TRIP_BP
    assert "기본값" in label  # 숫자만 돌려주면 읽는 사람이 실측으로 오해한다


def test_effective_bp_labels_fee_only_measurement_as_incomplete():
    cost = measure([_trip() for _ in range(12)])
    bp, label = effective_round_trip_bp(cost)
    assert bp == 20.0
    assert "슬리피지 표본 없음" in label and "이보다 크다" in label


def test_effective_bp_labels_full_measurement():
    cost = measure([_trip() for _ in range(12)], _legs(MIN_LEGS_FOR_SLIPPAGE, 5.0))
    bp, label = effective_round_trip_bp(cost)
    assert bp == 30.0
    assert label.startswith("실측")
