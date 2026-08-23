"""실데이터(data/history 파티션) 백테스트의 회계 검산 + 결정론.

stub 백테스트에서 항등식이 성립하는 것만으로는 부족하다. 실제로 모순이 터진 것은
실데이터 쪽이었고, 그 이유가 실데이터에만 있었기 때문이다:

- 가격 스케일이 극단적이다. 역분할 소급조정으로 SQQQ 2016년 가격은 $165,930까지
  올라가고 TQQQ는 $1.75까지 내려간다 — 같은 종목의 수량이 10년에 걸쳐 50배 넘게
  달라진다. 체결 로그를 나중에 재구성해 원가를 계산하는 방식은 여기서 무너진다.
- 부분청산(scale-out)/추가매수(scale-in)로 포지션이 여러 번 열리고 닫힌다.
- 체결 건수가 수천 건이라 float64 누적 오차가 실제로 관측 가능한 크기가 된다.

data/history는 git 미추적(용량)이라 없으면 스킵한다 — 조용히 통과시키지 않고
스킵 사유를 남긴다.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from quant.backtest.engine import run_backtest

_HISTORY_DIR = Path("data/history")
_SYMBOLS = ("TQQQ", "SQQQ")
# 결정론 비교를 위해 구간을 고정한다 — end를 비우면 디스크 데이터가 늘어날 때마다
# 구간이 움직여 회귀 테스트가 아니게 된다.
_END = pd.Timestamp("2019-01-01", tz="UTC")
_DAYS = 120

pytestmark = pytest.mark.skipif(
    not all((_HISTORY_DIR / s).exists() for s in _SYMBOLS),
    reason="data/history 파티션 없음 (git 미추적) — 실데이터 백테스트 스킵",
)


@pytest.fixture(scope="module")
def real_result():
    return run_backtest(
        strategy_id="donchian", days=_DAYS, interval="15m", source="history", end=_END,
    )


def test_real_data_backtest_actually_trades(real_result):
    """아래 검산들이 빈 장부끼리의 비교가 아님을 보증한다."""
    assert real_result.metrics["n_trades"] > 20
    assert len(real_result.trades) > 40


def test_real_data_backtest_satisfies_equity_identity(real_result):
    """최종자산 - 초기자산 == Σ실현손익 + 미실현평가손익 - Σ수수료 (전부 KRW)."""
    rec = real_result.reconciliation

    assert rec["currency"] == "KRW"
    lhs = rec["final_equity"] - rec["initial_equity"]
    rhs = rec["realized_pnl"] + rec["unrealized_pnl"] - rec["fees"]
    assert lhs == pytest.approx(rhs, abs=rec["tolerance"])


def test_real_data_backtest_satisfies_per_symbol_quantity_identity(real_result):
    """종목별 Σ매수수량 - Σ매도수량 == 최종 보유수량.

    실제로 깨졌던 형태가 바로 이것이다: 로그상 TQQQ 758.57주가 남아 있는데
    (2026년 가격 기준 약 $65,000) 자본은 $3,333뿐이었다.

    2026-08-10 정수 수량 사이징(QUICKREF:207) 도입 이후 이 구간(2018-09~2019-01,
    120일)에서는 SQQQ 예산이 1주 미만이라 아예 체결되지 않는다 — reconciliation에
    SQQQ 항목 자체가 없다. 이 테스트의 의도는 "SQQQ가 반드시 거래된다"가 아니라
    "체결된 종목의 회계 항등식"이므로, 실제로 체결된 종목에 대해서만 항등식을
    검사한다. 검사가 공허해지지 않도록 최소 1종목은 거래됐음을 별도로 단언한다."""
    trades = real_result.trades
    checks = real_result.reconciliation["positions"]
    assert checks, "체결된 종목이 하나도 없다 — 항등식 검산이 공허하다"

    for symbol, check in checks.items():
        rows = trades[trades["symbol"] == symbol]
        buy = float(rows[rows["side"] == "buy"]["qty"].sum())
        sell = float(rows[rows["side"] == "sell"]["qty"].sum())
        assert buy > 0, f"{symbol} 매수가 없다 — 검산이 공허하다"
        assert buy - sell == pytest.approx(check["final_qty"], abs=1e-6)


def test_real_data_equity_change_is_not_absurd_relative_to_capital(real_result):
    """손익이 자본 규모와 같은 자릿수인지 본다.

    통화가 섞였을 때(손익 USD / 자산 KRW) 정확히 이 검사가 깨졌다: 자본 500만원
    (약 $3,333)에 대해 손익 합계가 +$31,839로 보고됐다. 항등식이 통과하면 이건
    자동으로 만족되지만, 사람이 읽고 바로 이상함을 알아챌 수 있는 형태로 하나 더 둔다."""
    rec = real_result.reconciliation
    gross = abs(rec["realized_pnl"]) + abs(rec["unrealized_pnl"])

    # 10년도 아닌 수개월 구간에서 누적 실현손익이 초기자본의 50배를 넘으면 회계 오류다.
    assert gross < 50 * rec["initial_equity"]


def test_real_data_backtest_is_deterministic():
    """같은 입력 -> 완전히 동일한 출력. 실데이터 경로는 디스크 I/O와 리샘플이 끼어
    stub 경로보다 비결정성이 스며들 여지가 크다."""
    a = run_backtest(strategy_id="donchian", days=_DAYS, interval="15m", source="history", end=_END)
    b = run_backtest(strategy_id="donchian", days=_DAYS, interval="15m", source="history", end=_END)

    pd.testing.assert_series_equal(a.equity_curve, b.equity_curve)
    pd.testing.assert_frame_equal(a.trades, b.trades)
    assert a.metrics == b.metrics
    assert a.reconciliation == b.reconciliation
