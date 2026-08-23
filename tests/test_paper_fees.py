"""KR 개별주 매도 거래세(paper) — 2026-08-10 사용자 결정: 개별주 편입 허용의 전제.

2026-08-19 소유자가 준 토스증권 실제 요율표 대조 후 추가:
- KR 개별주 매도 거래세는 15bp가 아니라 **20bp**였다(코스피·코스닥 공통 증권거래세
  0.05% + 농어촌특별세 0.15% = 0.2%). "농특세"를 코스피만의 몫으로, "0.2%"를
  코스닥만의 몫으로 잘못 나눠 읽은 착오 — KR 개별주 매도마다 5bp씩 비용을
  낙관해왔다.
- 미국주식 매도에는 SEC Fee(매도금액의 0.00206%, 최소 $0.01)가 별도로 붙는다.
- 미국주식은 주문당 체결금액 $10 이하면 커미션(fee_bps)이 면제된다(매수·매도
  공통) — 단 SEC Fee는 이 면제와 무관하게 매도에 항상 붙는다(불확실하면
  보수적으로: 비용을 낙관하지 않는다).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant.apps.config import load_settings
from quant.core.models import Order, Quote, Side
from quant.adapters.execution.paper import PaperBroker
from quant.core.portfolio.portfolio import Portfolio


class _Feed:
    def __init__(self, price):
        self._price = price

    def quote(self, symbol):
        return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=self._price)


def _broker(**kw):
    return PaperBroker(
        data=_Feed(10000.0),
        portfolio=Portfolio(cash=10_000_000.0, positions={}),
        fee_bps={"KR": 1.5, "US": 10},
        market_of={"005930": "KR", "069500": "KR", "TQQQ": "US"},
        kr_stock_sell_tax_bps=15,
        kr_etf_symbols={"069500"},
        **kw,
    )


def _roundtrip_fees(broker, symbol):
    buy = broker.place_order(Order(symbol=symbol, side=Side.BUY, qty=10, strategy_id="t")).fill
    sell = broker.place_order(Order(symbol=symbol, side=Side.SELL, qty=10, strategy_id="t")).fill
    return buy.fee, sell.fee


def test_kr_stock_sell_pays_tax_but_buy_does_not():
    buy_fee, sell_fee = _roundtrip_fees(_broker(), "005930")
    notional = 10 * 10000.0
    assert abs(buy_fee - notional * 1.5 / 1e4) < 1e-6, "매수는 기본 수수료만"
    assert abs(sell_fee - notional * (1.5 + 15) / 1e4) < 1e-6, "매도는 수수료 + 거래세 15bp"


def test_kr_etf_sell_is_tax_exempt():
    _, sell_fee = _roundtrip_fees(_broker(), "069500")
    assert abs(sell_fee - 10 * 10000.0 * 1.5 / 1e4) < 1e-6


def test_us_symbol_unaffected():
    _, sell_fee = _roundtrip_fees(_broker(), "TQQQ")
    assert abs(sell_fee - 10 * 10000.0 * 10 / 1e4) < 1e-6


def test_unclassified_kr_symbol_treated_as_stock():
    """ETF 목록에 없는 KR 심볼(조회 실패 등) → 과세 — 보수적 방향."""
    b = _broker()
    b.market_of["999999"] = "KR"
    _, sell_fee = _roundtrip_fees(b, "999999")
    assert abs(sell_fee - 10 * 10000.0 * 16.5 / 1e4) < 1e-6


def test_fill_records_cash_after_snapshot_matching_portfolio(tmp_path):
    """체결 직후 현금 스냅샷 — 원장↔현금 갭의 발생 지점을 기록으로 특정하기 위함
    (2026-08-11 160,974원 미설명 갭에서 시점별 기록 부재로 원인 특정 실패)."""
    from quant.core.models import Order, Quote, Side
    from quant.adapters.execution.paper import PaperBroker
    from quant.core.portfolio.portfolio import Portfolio

    class _Data:
        def quote(self, symbol):
            from datetime import datetime, timezone
            return Quote(symbol=symbol, ts=datetime.now(timezone.utc), price=10_000.0)

    portfolio = Portfolio(cash=1_000_000.0, state_path=None)
    broker = PaperBroker(data=_Data(), portfolio=portfolio, fee_bps={"KR": 0.0, "US": 0.0},
                         market_of={"069500": "KR"}, slippage_bps=0.0)
    fill = broker.place_order(Order(symbol="069500", side=Side.BUY, qty=10,
                                    strategy_id="donchian")).fill
    assert fill is not None
    assert fill.cash_after == portfolio.cash == 900_000.0

    sell = broker.place_order(Order(symbol="069500", side=Side.SELL, qty=10,
                                    strategy_id="donchian")).fill
    assert sell.cash_after == portfolio.cash == 1_000_000.0


# --- config/settings.yaml 실제 값 대조 (2026-08-19 소유자 제공 토스 요율표) ---

def test_settings_kr_stock_sell_tax_matches_actual_krx_rate():
    """15bp는 착오였다 — 코스피·코스닥 공통 증권거래세(0.05%)+농특세(0.15%)=0.2%
    (20bp)가 실제 값이다. 이 테스트가 다시 15로 되돌아가면 즉시 잡는다."""
    settings = load_settings("config/settings.yaml")
    assert settings.raw["execution"]["kr_stock_sell_tax_bps"] == 20


def test_settings_us_sec_fee_and_free_commission_are_configured():
    settings = load_settings("config/settings.yaml")
    execution = settings.raw["execution"]
    assert execution["us_sec_fee_bps"] == pytest.approx(0.206)
    assert execution["us_sec_fee_min_usd"] == pytest.approx(0.01)
    assert execution["us_free_commission_notional_usd"] == pytest.approx(10)


# --- 미국주식 SEC Fee + $10 이하 커미션 면제 (2026-08-19) --------------------

_US_KW = dict(
    fee_bps={"US": 10, "KR": 1.5},
    market_of={"TQQQ": "US"},
    us_sec_fee_bps=0.206,
    us_sec_fee_min_usd=0.01,
    us_free_commission_notional_usd=10.0,
)


def _us_broker(cash: float = 10_000_000.0, price: float = 100.0) -> PaperBroker:
    return PaperBroker(
        data=_Feed(price), portfolio=Portfolio(cash=cash, positions={}), **_US_KW,
    )


def test_us_buy_never_pays_sec_fee():
    """SEC Fee는 매도 전용이다 — 매수엔 절대 안 붙는다."""
    broker = _us_broker(price=100.0)  # notional = 100*10=1,000 > $10, 커미션은 정상 부과
    buy = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t")).fill
    assert buy.fee == pytest.approx(10 * 100.0 * 10 / 10_000)  # 커미션만


def test_us_sell_sec_fee_minimum_dominates_for_small_notional():
    """요율(0.00206%)이 아니라 **최소 $0.01**이 지배하는 소액 매도. notional=$50이면
    요율 기준 SEC Fee=0.00103달러로 최소액보다 작다 — max()가 최소액을 골라야 한다."""
    broker = _us_broker(price=5.0)  # 매수 notional=50 > $10, 커미션 부과
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t"))
    sell = broker.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=10, strategy_id="t")).fill
    notional = 10 * 5.0
    commission = notional * 10 / 10_000
    assert notional * 0.206 / 10_000 < 0.01, "이 테스트의 전제(요율<최소액)가 깨졌다"
    assert sell.fee == pytest.approx(commission + 0.01)


def test_us_sell_sec_fee_rate_dominates_for_large_notional():
    """큰 매도금액에서는 요율이 최소액보다 커야 한다."""
    broker = _us_broker(price=500.0, cash=100_000_000.0)
    broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=100, strategy_id="t"))
    sell = broker.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=100, strategy_id="t")).fill
    notional = 100 * 500.0
    commission = notional * 10 / 10_000
    sec_fee_by_rate = notional * 0.206 / 10_000
    assert sec_fee_by_rate > 0.01, "이 테스트의 전제(요율>최소액)가 깨졌다"
    assert sell.fee == pytest.approx(commission + sec_fee_by_rate)


def test_us_order_at_or_under_10_usd_has_no_commission_but_sec_fee_still_applies():
    """$10 이하 주문 — 커미션은 면제, SEC Fee는 매도에 여전히 붙는다(불확실하면
    보수적으로: SEC Fee 면제 여부가 확인되지 않았으므로 부과 유지)."""
    broker = _us_broker(price=1.0)  # notional = 1.0*10 = $10, 경계값(이하 포함)
    buy = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t")).fill
    assert buy.fee == pytest.approx(0.0), "$10 이하 매수는 커미션 면제"

    sell = broker.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=10, strategy_id="t")).fill
    notional = 10 * 1.0
    assert sell.fee == pytest.approx(max(notional * 0.206 / 10_000, 0.01)), (
        "$10 이하 매도는 커미션은 면제되지만 SEC Fee는 그대로 부과돼야 한다"
    )


def test_us_order_just_over_10_usd_pays_commission():
    broker = _us_broker(price=10.01)  # notional = $10.01, 경계값 초과
    buy = broker.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=1, strategy_id="t")).fill
    assert buy.fee == pytest.approx(10.01 * 10 / 10_000)
    assert buy.fee > 0.0


def test_kr_market_unaffected_by_us_sec_fee_and_commission_waiver():
    """US 전용 규칙(SEC Fee, $10 면제)이 KR 심볼에 새면 안 된다."""
    broker = PaperBroker(
        data=_Feed(5.0), portfolio=Portfolio(cash=10_000_000.0, positions={}),
        fee_bps={"US": 10, "KR": 1.5}, market_of={"069500": "KR"},
        us_sec_fee_bps=0.206, us_sec_fee_min_usd=0.01, us_free_commission_notional_usd=10.0,
    )
    buy = broker.place_order(Order(symbol="069500", side=Side.BUY, qty=10, strategy_id="t")).fill
    # notional = 5.0*10 = 50 KRW, US $10 면제 임계와 무관하게 KR은 항상 커미션 부과.
    assert buy.fee == pytest.approx(50 * 1.5 / 10_000)
    assert buy.fee > 0.0


# --- 미국주식 FINRA TAF (2026-08-21) ----------------------------------------
# Trading Activity Fee: 매도 **주수** 기준 주당 $0.000166, 주문당 상한 $8.30.
# SEC Fee(금액 기준)와 달리 주수 기준이라 저가·대량 주문에서 지배적이 된다.
# 기본값 0.0 = 하위호환(이 인자를 넘기지 않는 기존 호출부는 동작 불변).

_TAF_KW = dict(_US_KW, us_taf_per_share=0.000166, us_taf_cap_usd=8.30)


def _taf_broker(price: float = 100.0) -> PaperBroker:
    return PaperBroker(
        data=_Feed(price), portfolio=Portfolio(cash=100_000_000.0, positions={}), **_TAF_KW,
    )


def test_us_sell_pays_taf_per_share():
    b = _taf_broker(price=100.0)
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t"))
    sell = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=10, strategy_id="t")).fill
    # 커미션 1000*10bp=1.0 + SEC max(1000*0.206bp, 0.01)=0.0206 + TAF 10*0.000166
    assert sell.fee == pytest.approx(1.0 + 0.0206 + 10 * 0.000166)


def test_us_sell_taf_capped_per_order():
    b = _taf_broker(price=1.0)  # 저가 대량 — 주수 기준 요금이 상한에 걸리는 영역
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=100_000, strategy_id="t"))
    sell = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=100_000, strategy_id="t")).fill
    # TAF 미상한 100000*0.000166=$16.6 → 상한 $8.30
    commission = 100_000 * 1.0 * 10 / 10_000
    sec = 100_000 * 1.0 * 0.206 / 10_000
    assert sell.fee == pytest.approx(commission + sec + 8.30)


def test_us_buy_never_pays_taf():
    b = _taf_broker(price=100.0)
    buy = b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t")).fill
    assert buy.fee == pytest.approx(1.0)  # 커미션만


def test_taf_default_zero_keeps_today_behaviour():
    b = _us_broker()
    b.place_order(Order(symbol="TQQQ", side=Side.BUY, qty=10, strategy_id="t"))
    sell = b.place_order(Order(symbol="TQQQ", side=Side.SELL, qty=10, strategy_id="t")).fill
    assert sell.fee == pytest.approx(1.0 + 0.0206)  # TAF 없음 — 기존 그대로


def test_settings_us_taf_is_configured():
    settings = load_settings("config/settings.yaml")
    execution = settings.raw["execution"]
    assert execution["us_taf_per_share"] == pytest.approx(0.000166)
    assert execution["us_taf_cap_usd"] == pytest.approx(8.30)
