"""리플레이 일관성 — 같은 원장을 세 소비자가 세면 같은 숫자가 나와야 한다.

2026-09-07 데이터 계보 감사(오너 지시: "그 데이터를 토대로 발전해야 한다" —
전제는 그 데이터가 여러 소비자 사이에서 서로 모순되지 않는다는 것)의 일환.

라운드트립을 세는 소비자가 최소 세 갈래다:
- `quant.control.ledger.round_trips` — 원장(lifetime, 시장 무관 lot 재생).
- `quant.control.performance.build_performance_payload` → `strategies[].total.trips`
  — 공개 사이트 payload. `_strategy_stats`가 내부에서 `round_trips`를 그대로
  다시 부른다(같은 함수) 그리고 **pnl_known 트립만** 센다.
- `quant.control.trade_review.build_trade_review` → 그날 세션 안에서 완전히
  열리고 닫힌 그룹 수(`status == "closed"`). 스코프가 "하루·그 시장"뿐이라
  `round_trips`(lifetime)와 원래도 다른 모집단이다 — 그래서 이 테스트는 **모든
  트립이 하루 세션 안에서 열리고 닫히는 합성 원장**을 써서 세 소비자의 모집단을
  일치시킨다. 그 조건에서도 숫자가 갈리면 진짜 버그다.

세 함수 다 실제 프로덕션 함수를 그대로 부른다 — 재구현이나 근사가 아니다.
"""
from __future__ import annotations

from datetime import date

from quant.control.ledger import round_trips
from quant.control.performance import build_performance_payload
from quant.control.trade_review import build_trade_review

EXECUTION_CFG = {
    "fee_bps": {"US": 10, "KR": 1.5},
    "kr_stock_sell_tax_bps": 20,
}


def _trade(*, ts, strategy_id, symbol, side, qty, price, fee=0.0, realized_pnl=None,
           market="KR", reason=""):
    return {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "reason": reason, "market": market,
    }


def _intraday_ledger() -> list[dict]:
    """단일 KR 세션(2026-08-10) 안에서 완전히 열리고 닫히는 라운드트립 3건 —
    전략 2개(orb_scan 2건, scalp_1m 1건), 종목 2개. 오버나이트 보유·이식/에폭
    마커·손익미상(realized_pnl=None) 행 없음 — 세 소비자의 모집단이 정확히
    같아지도록 만든 조건이다."""
    return [
        # orb_scan / 069500 — 트립 1
        _trade(ts="2026-08-10T00:05:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="BUY", qty=10, price=10000.0, fee=15.0),
        _trade(ts="2026-08-10T01:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="SELL", qty=10, price=10100.0, fee=15.2, realized_pnl=1000.0),
        # orb_scan / 069500 — 트립 2 (같은 종목, 그날 안에서 재진입)
        _trade(ts="2026-08-10T02:00:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="BUY", qty=5, price=10050.0, fee=7.5),
        _trade(ts="2026-08-10T02:30:00+00:00", strategy_id="orb_scan", symbol="069500",
               side="SELL", qty=5, price=9950.0, fee=7.5, realized_pnl=-500.0),
        # scalp_1m / 005930 — 트립 3
        _trade(ts="2026-08-10T03:00:00+00:00", strategy_id="scalp_1m", symbol="005930",
               side="BUY", qty=20, price=70000.0, fee=21.0),
        _trade(ts="2026-08-10T03:10:00+00:00", strategy_id="scalp_1m", symbol="005930",
               side="SELL", qty=20, price=70200.0, fee=21.1, realized_pnl=4000.0),
    ]


def test_round_trips_vs_performance_payload_agree():
    trades = _intraday_ledger()
    ledger_trips = round_trips(trades)
    assert len(ledger_trips) == 3
    assert all(t["pnl_known"] for t in ledger_trips), "합성 원장은 전부 손익 확정이어야 함"

    payload = build_performance_payload(trades, EXECUTION_CFG)
    payload_trip_total = sum(s["total"]["trips"] for s in payload["strategies"])
    assert payload_trip_total == len(ledger_trips), (
        f"performance 페이로드 trips 합({payload_trip_total}) != "
        f"ledger.round_trips 트립 수({len(ledger_trips)})"
    )


def test_round_trips_vs_trade_review_agree():
    trades = _intraday_ledger()
    ledger_trips = round_trips(trades)

    review = build_trade_review(
        trades, bars_by_symbol={}, strategy_params={}, market="KR",
        date=date(2026, 8, 10),
    )
    closed = [g for g in review["groups"] if g["status"] == "closed"]
    assert len(closed) == len(ledger_trips), (
        f"trade_review 종결 그룹 수({len(closed)}) != "
        f"ledger.round_trips 트립 수({len(ledger_trips)})"
    )
    assert review["summary"]["totals"]["n_open"] == 0, "합성 원장은 오버나이트 보유가 없어야 함"


def test_all_three_consumers_agree_on_trip_count():
    """세 소비자를 한 자리에서 직접 비교하는 매트릭스 — 하나라도 갈리면 실패."""
    trades = _intraday_ledger()

    n_ledger = len(round_trips(trades))

    payload = build_performance_payload(trades, EXECUTION_CFG)
    n_performance = sum(s["total"]["trips"] for s in payload["strategies"])

    review = build_trade_review(
        trades, bars_by_symbol={}, strategy_params={}, market="KR",
        date=date(2026, 8, 10),
    )
    n_trade_review = sum(1 for g in review["groups"] if g["status"] == "closed")

    matrix = {
        "ledger.round_trips": n_ledger,
        "performance.build_performance_payload": n_performance,
        "trade_review.build_trade_review": n_trade_review,
    }
    assert len(set(matrix.values())) == 1, f"라운드트립 수 불일치: {matrix}"
    assert n_ledger == 3
