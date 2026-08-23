"""포지션/현금 상태 + JSON 영속화. PaperBroker가 사용한다.

통화: Position.avg_cost/시세는 종목의 표시 통화(USD/KRW) 그대로 저장한다(도메인 원칙).
현금(cash)은 기준 통화 KRW. USD/KRW 환산은 FxProvider(quant.core.fx)를 주입받아
수행한다 — fx 인자를 생략하면 고정 환율(fallback) 프로바이더로 대체된다.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from quant.core.fx import FixedFxProvider, FxProvider
from quant.core.models import Position, market_of_symbol

DEFAULT_STATE_PATH = Path("data/state/portfolio.json")

_DEFAULT_FX = FixedFxProvider()


def to_krw(amount: float, market: str, fx: FxProvider | None = None) -> float:
    """market이 US(표시 통화 USD)이면 fx로 KRW 환산, KR이면 그대로."""
    fx = fx or _DEFAULT_FX
    return amount * fx.usd_krw() if market == "US" else amount


def from_krw(amount_krw: float, market: str, fx: FxProvider | None = None) -> float:
    fx = fx or _DEFAULT_FX
    return amount_krw / fx.usd_krw() if market == "US" else amount_krw


class Portfolio:
    def __init__(
        self,
        cash: float,
        positions: dict[str, Position] | None = None,
        state_path: Path | None = DEFAULT_STATE_PATH,
    ):
        """state_path=None이면 영속화하지 않는다(save()가 no-op).

        백테스트는 반드시 None으로 만든다. 기본 경로를 쓰면 백테스트가 라이브
        paper 거래의 상태 파일을 덮어쓰고, 동시에 돌린 백테스트끼리 서로의
        상태를 밟아 결과가 비결정적이 된다(2026-08-06 실측: 동일 조건에서
        승률 90.3% vs 87.3%). 6.9만 사이클 × 파일 쓰기라 속도 손해도 크다.
        """
        self.cash = cash
        self.positions: dict[str, Position] = positions or {}
        self.state_path = Path(state_path) if state_path is not None else None

    def equity(
        self,
        prices: dict[str, float],
        market_of: dict[str, str] | None = None,
        fx: FxProvider | None = None,
    ) -> float:
        """KRW 기준 총자산 = 현금 + 각 포지션의 KRW 환산 평가액."""
        market_of = market_of or {}
        total = self.cash
        for symbol, pos in self.positions.items():
            if pos.qty <= 0:
                continue
            price = prices.get(symbol, pos.avg_cost)
            total += to_krw(pos.qty * price, market_of.get(symbol) or market_of_symbol(symbol), fx)
        return total

    def save(self) -> None:
        if self.state_path is None:
            return  # 영속화 비활성 (백테스트)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "cash": self.cash,
            "positions": {
                sym: {
                    "symbol": pos.symbol,
                    "qty": pos.qty,
                    "avg_cost": pos.avg_cost,
                    "opened_at": pos.opened_at.isoformat() if pos.opened_at else None,
                    "meta": pos.meta,
                }
                for sym, pos in self.positions.items()
            },
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    @classmethod
    def load_or_init(cls, start_cash: float, state_path: Path = DEFAULT_STATE_PATH) -> "Portfolio":
        state_path = Path(state_path)
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            positions = {
                sym: Position(
                    symbol=p["symbol"],
                    qty=p["qty"],
                    avg_cost=p["avg_cost"],
                    opened_at=datetime.fromisoformat(p["opened_at"]) if p.get("opened_at") else None,
                    meta=p.get("meta", {}),
                )
                for sym, p in data.get("positions", {}).items()
            }
            return cls(cash=data.get("cash", start_cash), positions=positions, state_path=state_path)
        portfolio = cls(cash=start_cash, state_path=state_path)
        portfolio.save()
        return portfolio
