"""전략 간 합산 노출 감시 — 관측+경고, 자동 차단 아님 (2026-08-30).

## 왜

`risk.capital_mode: per_strategy`에서는 `RiskManagerImpl`의 모든 노출 상한
(`max_total_exposure_pct`/`max_leveraged_exposure_pct`)이 **그 전략의 독립
장부(`quant.trade.risk.books.StrategyBooks`) 기준**으로만 계산된다
(`quant/trade/risk/manager.py:643-661`). 두 전략이 각자 자기 한도 안에서 같은
심볼을 중복 보유하거나(예: donchian과 mean_reversion이 둘 다 TQQQ 롱), 레버리지
ETF 롱과 그 인버스 롱을 동시에 들고 있어도(TQQQ 롱 + SQQQ 롱 — 두 전략이 정반대
베팅을 하며 자본만 묶어 두는 상태) 상한 계산에는 "다른 전략도 이미 들고 있다"는
사실이 전혀 들어가지 않는다. 계좌 전체로 보면 레버리지가 실질 상한을 넘거나
자본이 상쇄로 놀고 있는데도 아무 경보가 없다.

이 모듈은 그 사각지대를 **드러내기만** 한다. 대회 설계(전략마다 독립적으로
진입 판단)를 존중해 자동으로 주문을 막지 않는다 — 사람이 합산 그림을 보고
개입할지 정한다.

## 입력 — 왜 브로커/전략을 모르는가

`quant/trade/`를 임포트하면 안 된다(`tests/test_architecture.py` FORBIDDEN:
`quant.trade → quant.control`도 막혀 있어 반대 방향도 이미 끊겨 있다 — 이
모듈은 애초에 거래 평면의 어떤 타입도 몰라야 양방향이 성립한다). 그래서
`build_report()`는 `quant.core.models.Position`도, `StrategyBooks`도 받지
않고 이미 눌러진 값만 받는다:

- `lots: {symbol: {strategy_id: qty}}` — `Position.meta["lots"]`을 호출부가
  평평하게 만든 것. `StrategyBooks`(per_strategy 전용 가상 장부)가 아니라
  `Position.meta["lots"]`를 원천으로 쓰는 이유: 후자는 `capital_mode`와 무관하게
  **항상** 채워지고(체결마다 `Position.ensure_lot()`), 실제 브로커 보유와
  1:1이라 "가상 장부상 숫자"가 아니라 "지금 진짜 들고 있는 수량"을 본다.
- `prices: {symbol: 현재가}` — 표시 통화 그대로(호출부가 marks 우선, 없으면
  평단가로 저하한다).
- `leverage_of` — 없으면(오프라인 리포트 등, 네트워크로 조회한 leverageFactor가
  없는 호출부) 알려진 상쇄 쌍 심볼만 `_KNOWN_PAIR_LEVERAGE`로 보강하고, 그 외
  심볼은 1.0(비레버리지 가정)으로 저하한다 — 없는 정밀도를 지어내지 않는다.
- `capital_krw` — 없으면(브로커가 `portfolio.cash`를 노출하지 않는 실거래 경로 등)
  레버리지 비율(pct) 판단을 보류한다(`leveraged_exposure_pct=None`) — 0%로
  위장하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass

from quant.core.fx import FxProvider
from quant.core.models import market_of_symbol
from quant.core.portfolio.portfolio import to_krw

__all__ = [
    "DEFAULT_ALERT_PCT",
    "KNOWN_OFFSETTING_PAIRS",
    "ExposureReport",
    "OffsettingPair",
    "SymbolExposure",
    "build_report",
]

# 소유자가 지정한 알려진 상쇄 쌍 — 레버리지 ETF 롱 vs 그 인버스 롱을 동시에
# 들고 있으면(둘 다 qty>0) 두 전략이 정반대로 베팅하며 자본을 묶어 두는 상태다.
# (long, inverse) 순서 — summary_line()의 표기 순서로만 쓰인다.
KNOWN_OFFSETTING_PAIRS: tuple[tuple[str, str], ...] = (
    ("TQQQ", "SQQQ"),
    ("SOXL", "SOXS"),
    ("122630", "252670"),
)

# 알려진 쌍의 배수 — daily_wrap처럼 네트워크 없이(leverage_of 미제공) 도는
# 호출부를 위한 최소 보강이다. 실측(stock_info)이 있으면 그게 항상 우선한다 —
# 이 표는 그게 없을 때만 쓰는 바닥값이다.
_KNOWN_PAIR_LEVERAGE: dict[str, float] = {
    "TQQQ": 3.0, "SQQQ": 3.0,
    "SOXL": 3.0, "SOXS": 3.0,
    "122630": 2.0, "252670": 2.0,
}

# 합산 레버리지가중 노출이 총자본의 이 비율을 넘으면 경고 대상 — settings.yaml
# risk.cross_strategy_leverage_alert_pct(퍼센트, 100 = 여기 1.0)가 실제 배선값.
DEFAULT_ALERT_PCT = 1.0


def _leverage_of(symbol: str, leverage_of: dict[str, float] | None) -> float:
    if leverage_of is not None and symbol in leverage_of:
        try:
            lev = abs(float(leverage_of[symbol]))
            if lev > 0:
                return lev
        except (TypeError, ValueError):
            pass
    return _KNOWN_PAIR_LEVERAGE.get(symbol, 1.0)


@dataclass(frozen=True)
class SymbolExposure:
    """심볼 하나의 합산 노출 — 몇 개 전략이 얼마씩 들고 있는지 포함."""

    symbol: str
    market: str
    notional_krw: float
    leveraged_notional_krw: float
    leverage: float
    strategies: dict[str, float]  # strategy_id -> qty (qty>0만)

    @property
    def n_strategies(self) -> int:
        return len(self.strategies)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "notional_krw": round(self.notional_krw),
            "leveraged_notional_krw": round(self.leveraged_notional_krw),
            "leverage": self.leverage,
            "strategies": dict(self.strategies),
        }


@dataclass(frozen=True)
class OffsettingPair:
    """레버리지 롱 vs 인버스 롱을 동시에 들고 있는 알려진 쌍."""

    long_symbol: str
    inverse_symbol: str
    long_notional_krw: float
    inverse_notional_krw: float

    def to_dict(self) -> dict:
        return {
            "long_symbol": self.long_symbol,
            "inverse_symbol": self.inverse_symbol,
            "long_notional_krw": round(self.long_notional_krw),
            "inverse_notional_krw": round(self.inverse_notional_krw),
        }


@dataclass(frozen=True)
class ExposureReport:
    total_notional_krw: float
    total_leveraged_notional_krw: float
    capital_krw: float | None
    leveraged_exposure_pct: float | None  # None = capital_krw를 모른다
    by_symbol: tuple[SymbolExposure, ...]  # notional 내림차순
    duplicates: tuple[SymbolExposure, ...]  # 2개 이상 전략이 든 심볼만
    offsetting_pairs: tuple[OffsettingPair, ...]
    alert_threshold_pct: float
    alert: bool  # 임계 초과 또는 상쇄 쌍 존재

    def summary_line(self) -> str:
        """1줄 요약 — daily_wrap 2절 옆, 로그(평시), 알림(임계 초과 시) 공통 재료."""
        if not self.by_symbol:
            return "보유 없음"
        parts = [f"합산 명목 {self.total_notional_krw:,.0f}원"]
        if self.leveraged_exposure_pct is not None:
            parts.append(
                f"레버리지가중 {self.leveraged_exposure_pct * 100:.0f}%"
                f"(상한 {self.alert_threshold_pct * 100:.0f}%)"
            )
        else:
            parts.append(
                f"레버리지가중 {self.total_leveraged_notional_krw:,.0f}원"
                "(총자본 모름 — 비율 판단 불가)"
            )
        if self.duplicates:
            dups = ", ".join(f"{s.symbol}({s.n_strategies}개 전략)" for s in self.duplicates)
            parts.append(f"중복 보유 {dups}")
        if self.offsetting_pairs:
            pairs = ", ".join(f"{p.long_symbol}/{p.inverse_symbol}" for p in self.offsetting_pairs)
            parts.append(f"상쇄 쌍 보유 {pairs}")
        return " · ".join(parts)

    def alert_text(self) -> str | None:
        """임계 초과·상쇄 쌍 존재가 아니면 None — 호출부가 그때만 알림을 보낸다."""
        if not self.alert:
            return None
        return f"⚠️ 전략 간 합산 노출 경고 — {self.summary_line()}"

    def to_dict(self) -> dict:
        return {
            "total_notional_krw": round(self.total_notional_krw),
            "total_leveraged_notional_krw": round(self.total_leveraged_notional_krw),
            "capital_krw": None if self.capital_krw is None else round(self.capital_krw),
            "leveraged_exposure_pct": (
                None if self.leveraged_exposure_pct is None
                else round(self.leveraged_exposure_pct * 100, 1)
            ),
            "by_symbol": [s.to_dict() for s in self.by_symbol],
            "duplicates": [s.to_dict() for s in self.duplicates],
            "offsetting_pairs": [p.to_dict() for p in self.offsetting_pairs],
            "alert_threshold_pct": self.alert_threshold_pct * 100,
            "alert": self.alert,
            "summary": self.summary_line(),
            "alert_text": self.alert_text(),
        }


def build_report(
    lots: dict[str, dict[str, float]],
    prices: dict[str, float],
    leverage_of: dict[str, float] | None = None,
    capital_krw: float | None = None,
    alert_threshold_pct: float = DEFAULT_ALERT_PCT,
    fx: FxProvider | None = None,
) -> ExposureReport:
    """`lots`(symbol -> {strategy_id: qty}) + `prices`(symbol -> 현재가) →
    합산 노출 리포트.

    가격이 없는 심볼(`prices`에 키가 없거나 0 이하)은 노출 계산에서 조용히
    제외한다 — 0으로 지어내지 않는다. qty<=0인 전략 항목도 제외한다(청산된
    레거시 lot 흔적 방어).
    """
    by_symbol: list[SymbolExposure] = []
    total_notional = 0.0
    total_leveraged = 0.0

    for symbol, strat_qty in lots.items():
        active = {sid: float(q) for sid, q in strat_qty.items() if float(q) > 0}
        if not active:
            continue
        price = prices.get(symbol)
        if price is None or price <= 0:
            continue
        total_qty = sum(active.values())
        market = market_of_symbol(symbol)
        notional_krw = to_krw(total_qty * price, market, fx)
        lev = _leverage_of(symbol, leverage_of)
        leveraged_notional = notional_krw * lev

        by_symbol.append(SymbolExposure(
            symbol=symbol, market=market, notional_krw=notional_krw,
            leveraged_notional_krw=leveraged_notional, leverage=lev,
            strategies=active,
        ))
        total_notional += notional_krw
        total_leveraged += leveraged_notional

    by_symbol.sort(key=lambda s: s.notional_krw, reverse=True)
    duplicates = tuple(s for s in by_symbol if s.n_strategies >= 2)

    notional_by_symbol = {s.symbol: s.notional_krw for s in by_symbol}
    offsetting: list[OffsettingPair] = []
    for long_sym, inv_sym in KNOWN_OFFSETTING_PAIRS:
        if long_sym in notional_by_symbol and inv_sym in notional_by_symbol:
            offsetting.append(OffsettingPair(
                long_symbol=long_sym, inverse_symbol=inv_sym,
                long_notional_krw=notional_by_symbol[long_sym],
                inverse_notional_krw=notional_by_symbol[inv_sym],
            ))

    pct = (total_leveraged / capital_krw) if capital_krw and capital_krw > 0 else None
    alert = bool(offsetting) or (pct is not None and pct > alert_threshold_pct)

    return ExposureReport(
        total_notional_krw=total_notional,
        total_leveraged_notional_krw=total_leveraged,
        capital_krw=capital_krw,
        leveraged_exposure_pct=pct,
        by_symbol=tuple(by_symbol),
        duplicates=duplicates,
        offsetting_pairs=tuple(offsetting),
        alert_threshold_pct=alert_threshold_pct,
        alert=alert,
    )
