"""전일 KR 세션 패턴 분류(`quant/analyze/kr_wrap.py`) — 손계산 픽스처 (2026-08-25).

세 패턴은 소유자 서술("초반부터 몰리고 안 빠짐 / 후반 회복 전고 돌파 / 후반
급 매수 파동")의 정량화다. 문턱값은 [미검증] 초기값 — 여기서는 분류기가 그
서술과 같은 모양을 정확히 잡고, 아닌 모양을 잡지 않는 것만 고정한다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from quant.analyze.kr_wrap import (
    build_kr_session_wrap, classify_session, flow_day_summary,
)

N = 381  # KR 정규장 분봉 수 (09:00~15:30)


def _bars(closes, volumes=None, highs=None, lows=None):
    n = len(closes)
    return pd.DataFrame({
        "open": [closes[0]] + closes[:-1],
        "high": highs if highs is not None else [c * 1.001 for c in closes],
        "low": lows if lows is not None else [c * 0.999 for c in closes],
        "close": closes,
        "volume": volumes if volumes is not None else [1000.0] * n,
    }, index=pd.RangeIndex(n))


def test_sustained_early_strength_detected():
    """개장 60분에 +2% 오르고 종일 고점 부근 유지 — '초반부터 몰리고 안 빠짐'."""
    closes = [100 + min(i, 60) * (2.0 / 60) for i in range(N)]  # 60분에 +2% 후 횡보
    assert "초반강세지속" in classify_session(_bars(closes))


def test_early_pop_that_faded_is_not_sustained():
    """초반 +2% 후 상승분을 다 반납 — '안 빠짐'이 아니다."""
    closes = [100 + min(i, 60) * (2.0 / 60) for i in range(120)] + [100.2] * (N - 120)
    assert "초반강세지속" not in classify_session(_bars(closes))


def test_late_breakout_after_midday_dip():
    """오전 고점 103 → 중반 -2% 눌림 → 마지막 90분에 104 돌파."""
    closes = ([100 + i * 0.1 for i in range(31)]        # ~103.0 고점
              + [101.0] * (N - 31 - 90)                 # 중반 눌림 (103 대비 -1.9%)
              + [104.0] * 90)                           # 후반 돌파
    assert "후반전고돌파" in classify_session(_bars(closes))


def test_no_late_breakout_without_dip():
    """눌림 없이 그냥 쭉 오른 건 '회복 후 돌파'가 아니다."""
    closes = [100 + i * 0.02 for i in range(N)]
    assert "후반전고돌파" not in classify_session(_bars(closes))


def test_late_volume_surge_detected():
    """마지막 60분 거래량 3배 + 가격 +1.5% — '후반 급 매수세 파동'."""
    closes = [100.0] * (N - 60) + [100 + i * (1.5 / 60) for i in range(60)]
    volumes = [1000.0] * (N - 60) + [3000.0] * 60
    assert "후반매수파동" in classify_session(_bars(closes, volumes))


def test_late_volume_without_price_is_not_a_buy_surge():
    """거래량만 터지고 가격이 안 오르면(분산 의심) 매수 파동이 아니다."""
    closes = [100.0] * N
    volumes = [1000.0] * (N - 60) + [3000.0] * 60
    assert "후반매수파동" not in classify_session(_bars(closes, volumes))


def test_half_day_data_refuses_to_classify():
    assert classify_session(_bars([100.0] * 100)) == []


def test_wrap_assembles_patterns_and_symbols():
    strong = [100 + min(i, 60) * (2.0 / 60) for i in range(N)]
    flat = [100.0] * N
    wrap = build_kr_session_wrap(
        {"005930": _bars(strong), "000660": _bars(flat)},
        names={"005930": "삼성전자"},
    )
    assert [e["symbol"] for e in wrap["patterns"]["초반강세지속"]] == ["005930"]
    assert wrap["patterns"]["초반강세지속"][0]["name"] == "삼성전자"
    assert wrap["symbols"] == ["005930"], "다음날 후보 합류용 심볼 목록"


def test_wrap_returns_none_when_nothing_to_say():
    assert build_kr_session_wrap({"005930": _bars([100.0] * N)}) is None


def test_flow_day_summary_totals_and_tops():
    rows = [
        {"date": "2026-08-25", "symbol": "005930", "foreign_net": 100.0, "inst_net": 50.0},
        {"date": "2026-08-25", "symbol": "000660", "foreign_net": -30.0, "inst_net": -20.0},
        {"date": "2026-08-24", "symbol": "005930", "foreign_net": 999.0, "inst_net": 0.0},
    ]
    s = flow_day_summary(rows, "2026-08-25")
    assert s["foreign_net_total"] == pytest.approx(70.0)
    assert s["inst_net_total"] == pytest.approx(30.0)
    assert s["top_buy"][0]["symbol"] == "005930"
    assert s["top_sell"][0]["symbol"] == "000660"
    assert flow_day_summary(rows, "2026-08-23") is None, "그날 데이터가 없으면 지어내지 않는다"
