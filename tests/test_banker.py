"""build_daily_report: 집중도/레버리지 경고, 손실 단계별 진단이 올바르게 플래그되는지 검증.
순수 함수 — 네트워크 없이 테스트한다.
"""
from __future__ import annotations

from quant.control.banker import build_daily_report


def test_empty_holdings_reports_zero_assets():
    report = build_daily_report([], fx_rate=1300.0)
    assert "₩0" in report
    assert "보유 종목 없음" in report


def test_flags_concentration_leverage_and_loss_thresholds():
    holdings = [
        # 비중 66% (>40% 집중도 경고), 손익 -22% (<=-20% 청산 검토)
        {"symbol": "TQQQ", "currency": "USD", "qty": 10, "avg_cost": 50.0, "price": 39.0},
        # 손익 정확히 -10% (<=-10% 검토 필요, but not exit)
        {"symbol": "SQQQ", "currency": "USD", "qty": 5, "avg_cost": 20.0, "price": 18.0},
        # 무손실 포지션, 워닝/진단 없어야 함
        {"symbol": "AAPL", "currency": "USD", "qty": 1, "avg_cost": 100.0, "price": 110.0},
    ]
    report = build_daily_report(holdings, fx_rate=1300.0)

    # 집중도 경고: TQQQ 비중이 40% 초과
    assert "집중도 경고" in report
    assert "TQQQ" in report.split("[경고]")[1].split("[손실")[0]

    # 레버리지 ETF(TQQQ+SQQQ) 노출 합계 경고
    assert "레버리지 ETF 노출" in report

    # 손실 종목 진단: TQQQ는 청산 검토, SQQQ는 검토 필요
    diagnosis_block = report.split("[손실 종목 진단]")[1]
    assert "TQQQ" in diagnosis_block and "청산 검토" in diagnosis_block
    assert "SQQQ" in diagnosis_block and "검토 필요" in diagnosis_block

    # AAPL은 이익 포지션 — 진단 블록에 등장하지 않아야 함
    assert "AAPL" not in diagnosis_block


def test_no_warnings_or_diagnoses_when_balanced_and_profitable():
    holdings = [
        {"symbol": "AAPL", "currency": "USD", "qty": 1, "avg_cost": 100.0, "price": 105.0},
        {"symbol": "MSFT", "currency": "USD", "qty": 1, "avg_cost": 100.0, "price": 102.0},
        {"symbol": "GOOG", "currency": "USD", "qty": 1, "avg_cost": 100.0, "price": 101.0},
    ]
    report = build_daily_report(holdings, fx_rate=1300.0)
    assert "[경고]" not in report
    assert "[손실 종목 진단]" not in report


def test_krw_holdings_not_converted_by_fx_rate():
    holdings = [{"symbol": "005930", "currency": "KRW", "qty": 10, "avg_cost": 70000.0, "price": 70000.0}]
    report = build_daily_report(holdings, fx_rate=1300.0)
    assert "₩700,000" in report


def _distressed_holdings() -> list[dict]:
    return [
        {"symbol": "TQQQ", "currency": "USD", "qty": 120, "avg_cost": 88.0, "price": 63.2},
        {"symbol": "SOXL", "currency": "USD", "qty": 60, "avg_cost": 32.0, "price": 28.9},
        {"symbol": "005930", "currency": "KRW", "qty": 30, "avg_cost": 71000, "price": 79800},
    ]


def test_leverage_exposure_uses_broker_factors_not_a_hardcoded_list():
    """실계좌엔 SOXL/UPRO 등 무엇이든 있을 수 있다 — 하드코딩 목록에 없다고
    레버리지가 아닌 것으로 처리하면 위험이 과소보고된다."""
    report = build_daily_report(
        _distressed_holdings(), 1387.5,
        leverage_factors={"TQQQ": 3.0, "SOXL": 3.0, "005930": 1.0},
    )
    assert "레버리지 ETF 노출 합계 84.4%" in report
    assert "확인 불가" not in report


def test_inverse_leverage_counts_as_leverage():
    """인버스(-3x)도 레버리지다 — 부호 때문에 빠지면 안 된다."""
    holdings = [
        {"symbol": "SQQQ", "currency": "USD", "qty": 100, "avg_cost": 40.0, "price": 40.0},
        {"symbol": "GLD", "currency": "USD", "qty": 10, "avg_cost": 100.0, "price": 100.0},
    ]
    report = build_daily_report(holdings, 1400.0, leverage_factors={"SQQQ": -3.0, "GLD": 1.0})
    assert "레버리지 ETF 노출 합계 80.0%" in report


def test_unknown_leverage_is_surfaced_not_silently_assumed_safe():
    """배수를 모르는 종목을 조용히 1배로 간주하면 안 된다 — 명시적으로 드러낸다."""
    # 조회를 시도했으나 SOXL/005930은 실패해 dict에서 빠진 상황 (fetch_leverage_factors의
    # 실제 반환 형태). 조회 자체를 안 한 경우(None)와 구분된다.
    report = build_daily_report(_distressed_holdings(), 1387.5, {"TQQQ": 3.0})
    assert "레버리지 배수 확인 불가" in report
    assert "SOXL" in report.split("확인 불가")[1]
    assert "실제 노출은 더 클 수 있음" in report


def test_fetch_leverage_factors_skips_failures_instead_of_defaulting():
    """조회 실패를 1.0으로 메우면 '레버리지 아님'으로 잘못 단정하게 된다."""
    from quant.control.banker import fetch_leverage_factors

    class _Client:
        def stock_info(self, symbol):
            if symbol == "SOXL":
                raise RuntimeError("네트워크 오류")
            if symbol == "005930":
                return {"symbol": symbol}  # leverageFactor 필드 없음
            return {"symbol": symbol, "leverageFactor": "3"}

    factors = fetch_leverage_factors(_Client(), _distressed_holdings())
    assert factors == {"TQQQ": 3.0}
    assert "SOXL" not in factors and "005930" not in factors
