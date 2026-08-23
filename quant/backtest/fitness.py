"""적합도 함수 — 자동 개선(Phase 8)이 최적화할 대상.

**여기가 거짓말하면 그 위에 쌓은 모든 자동 개선이 조용히 틀린다.** 하네스는
당신이 측정하는 것을 최적화한다. 백테스트에 룩어헤드가 있으면 하네스는 그
버그를 찾아내 아주 훌륭하게 착취하고, 자신만만한 숫자를 들고 온다.

## 왜 총수익률이 아니라 명목 대비 bps 인가

`total_return_pct` 는 **사이징과 계좌 크기에 오염된다.** 같은 전략을 두 배로
태우면 수익률이 두 배로 보이지만 엣지는 그대로다. 반대로 자본의 1%만 태우면
훌륭한 엣지도 초라해 보인다.

명목 대비 bps 는 **거래 1원당 얼마를 벌었나**다. 그래서 비용과 같은 단위로
직접 비교된다:

    우리가 이미 아는 숫자 — TQQQ 일중 엣지 왕복 8~9bp, 키움 수수료 14bp.

비용을 빼고 최적화하면 하네스는 **확실한 손실 전략을 최고점으로** 들고 온다.
그래서 이 모듈은 `gross_bps`(비용 전)와 `net_bps`(비용 후)를 나눠 내고,
`edge_covers_cost` 로 그 관계를 한 눈에 답한다.

## 표본 부족을 점수로 위장하지 않는다

왕복 12번짜리 표본에 변형 100개를 시험하면 그중 몇 개는 **반드시** 훌륭해
보인다 — 우연히. 이건 위험이 아니라 산수다. 그래서 `sufficient` 플래그를 내고,
부족하면 하네스가 채택하지 못하게 한다. 점수를 깎는 게 아니라 **자격을 뺏는다** —
깎으면 언젠가 문턱을 넘고, 그때 근거가 없다는 사실은 사라져 있다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# 채택을 논하려면 최소 이만큼의 왕복이 필요하다. 근거: 승률 추정의 표준오차가
# sqrt(p(1-p)/n) 이므로 n=30 에서도 ±9%p 다. 30 은 "충분"이 아니라 "이 아래로는
# 논의 자체가 무의미"한 선이다.
MIN_ROUND_TRIPS = 30

# 비용이 이 아래면 비용 모델이 꺼진 것으로 본다. 실제 최소 비용은 KR ETF 편도
# 1.5bp 수준이라 0.5bp 왕복은 물리적으로 불가능하다.
MIN_PLAUSIBLE_COST_BPS = 0.5


class ZeroCostBacktest(RuntimeError):
    """비용 없이 돌린 백테스트를 적합도로 쓰려 할 때.

    **경고가 아니라 예외인 이유**: 비용 0 백테스트는 우리가 아는 가장 비싼
    실패 모드다(엣지 8~9bp, 수수료 14bp). 경고는 로그에 묻히고 하네스는 계속
    돈다. 여기서 멈춰야 한다.
    """


@dataclass(frozen=True)
class Fitness:
    """비교 가능한 지표 묶음. 하네스와 리더보드가 이것만 본다."""

    # --- 엣지 (비용과 같은 단위) ---
    gross_bps: float          # 비용 전, 명목 대비
    net_bps: float            # 비용 후, 명목 대비 — **이게 진짜 성적이다**
    cost_bps: float           # 명목 대비 실제 부담한 비용
    edge_covers_cost: bool    # gross_bps > cost_bps
    # --- 규모 ---
    total_notional_krw: float
    total_cost_krw: float
    net_pnl_krw: float
    # --- 분포 ---
    win_rate_pct: float
    payoff_ratio: float
    mdd_pct: float
    sharpe: float
    # --- 표본 ---
    n_fills: int
    n_round_trips: int
    sufficient: bool          # 채택을 논할 자격이 있나
    # --- 맥락 ---
    benchmark_return_pct: float | None = None
    strategy_errors: int = 0

    def to_dict(self) -> dict:
        return {
            "gross_bps": round(self.gross_bps, 3),
            "net_bps": round(self.net_bps, 3),
            "cost_bps": round(self.cost_bps, 3),
            "edge_covers_cost": self.edge_covers_cost,
            "total_notional_krw": round(self.total_notional_krw),
            "total_cost_krw": round(self.total_cost_krw),
            "net_pnl_krw": round(self.net_pnl_krw),
            "win_rate_pct": round(self.win_rate_pct, 4),
            "payoff_ratio": round(self.payoff_ratio, 3),
            "mdd_pct": round(self.mdd_pct, 3),
            "sharpe": round(self.sharpe, 3),
            "n_fills": self.n_fills,
            "n_round_trips": self.n_round_trips,
            "sufficient": self.sufficient,
            "benchmark_return_pct": (
                None if self.benchmark_return_pct is None
                else round(self.benchmark_return_pct, 3)
            ),
            "strategy_errors": self.strategy_errors,
        }


def count_round_trips(trades: pd.DataFrame) -> int:
    """(종목별) 누적 수량이 0으로 돌아온 횟수.

    체결 수가 아니라 왕복 수가 표본 크기다 — 매수 1건은 아직 결과가 없다.
    """
    if trades is None or trades.empty:
        return 0
    trips = 0
    for _, group in trades.groupby("symbol", sort=False):
        pos = 0.0
        opened = False
        for _, row in group.iterrows():
            qty = float(row.get("qty") or 0)
            pos += qty if str(row.get("side")) == "buy" else -qty
            if not opened and abs(pos) > 1e-9:
                opened = True
            elif opened and abs(pos) <= 1e-9:
                trips += 1
                opened = False
    return trips


def _cost_krw(trades: pd.DataFrame) -> float:
    if trades is None or trades.empty:
        return 0.0
    return float(trades.get("fee_krw", pd.Series(dtype=float)).fillna(0).sum())


def _notional_krw(trades: pd.DataFrame) -> float:
    if trades is None or trades.empty:
        return 0.0
    if "notional_krw" not in trades.columns:
        raise ZeroCostBacktest(
            "체결에 notional_krw 가 없다 — 명목 대비 bps 를 계산할 수 없다. "
            "engine._TRADE_COLUMNS 를 확인한다."
        )
    return float(trades["notional_krw"].fillna(0).abs().sum())


def evaluate(result, *, require_costs: bool = True) -> Fitness:
    """`BacktestResult` → 비교 가능한 지표 묶음.

    `require_costs=False` 는 **테스트 전용**이다. 하네스 경로에서는 절대 끄지
    않는다 — 그러려고 만든 게 아니라, 비용 모델 자체를 시험하려고 둔 구멍이다.
    """
    trades = result.trades
    metrics = result.metrics or {}

    notional = _notional_krw(trades)
    cost = _cost_krw(trades)
    realized = (
        0.0 if trades is None or trades.empty
        else float(trades.get("realized_pnl_krw", pd.Series(dtype=float)).fillna(0).sum())
    )
    net_pnl = realized - cost

    cost_bps = (cost / notional * 10_000) if notional > 0 else 0.0
    gross_bps = (realized / notional * 10_000) if notional > 0 else 0.0
    net_bps = (net_pnl / notional * 10_000) if notional > 0 else 0.0

    n_fills = 0 if trades is None else int(len(trades))
    if require_costs and n_fills > 0 and cost_bps < MIN_PLAUSIBLE_COST_BPS:
        raise ZeroCostBacktest(
            f"비용이 명목 대비 {cost_bps:.3f}bp 다 — 비용 모델이 꺼진 백테스트를 "
            f"적합도로 쓸 수 없다. 우리 실측 엣지는 왕복 8~9bp, 수수료는 14bp 라 "
            f"비용을 빼고 최적화하면 확실한 손실 전략이 최고점으로 나온다. "
            f"config/settings.yaml 의 execution.fee_bps / slippage_bps 를 확인한다."
        )

    trips = count_round_trips(trades)
    bench = (result.benchmark or {}).get("buy_hold", {}).get("total_return_pct")
    errors = sum(
        int(v.get("cycles_skipped", 0)) for v in (result.strategy_errors or {}).values()
    )

    return Fitness(
        gross_bps=gross_bps,
        net_bps=net_bps,
        cost_bps=cost_bps,
        edge_covers_cost=gross_bps > cost_bps,
        total_notional_krw=notional,
        total_cost_krw=cost,
        net_pnl_krw=net_pnl,
        win_rate_pct=float(metrics.get("win_rate") or 0.0),
        payoff_ratio=float(metrics.get("payoff_ratio") or 0.0),
        mdd_pct=float(metrics.get("mdd_pct") or 0.0),
        sharpe=float(metrics.get("sharpe") or 0.0),
        n_fills=n_fills,
        n_round_trips=trips,
        sufficient=trips >= MIN_ROUND_TRIPS,
        benchmark_return_pct=None if bench is None else float(bench),
        strategy_errors=errors,
    )
