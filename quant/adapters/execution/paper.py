"""paper 브로커: 현재가로 즉시 체결. Broker Protocol 구현.

fee_bps는 config(execution.fee_bps)에서. 숫자 하나면 전 시장 공통(하위호환),
`{US: 10, KR: 1.5}` 형태의 dict면 **시장별**로 적용한다. cash/positions는 Portfolio가
들고 있고, 체결마다 즉시 JSON으로 영속화한다.

slippage_bps는 config(execution.slippage_bps)에서 오며, 체결가에 항상 **불리한**
방향으로 적용된다(매수는 더 비싸게, 매도는 더 싸게) — "슬리피지 0"은 낙관적 가정이지
현실이 아니다. Fill.price는 슬리피지 적용 후 실제 체결가이므로 회계 항등식은 그대로
성립한다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.core.fx import FixedFxProvider, FxProvider
from quant.core.ports import DataFeed
from quant.core import oms
from quant.core.models import (
    Fill, OpenOrder, Order, OrderState, Position, Side, market_of_symbol,
)
from quant.core.portfolio.portfolio import Portfolio, to_krw


class PaperBroker:
    def __init__(
        self,
        data: DataFeed,
        portfolio: Portfolio,
        fee_bps: float | dict[str, float] = 0.0,
        market_of: dict[str, str] | None = None,
        kr_stock_sell_tax_bps: float = 0.0,
        kr_etf_symbols: set[str] | frozenset = frozenset(),
        fx: FxProvider | None = None,
        slippage_bps: float | dict[str, float] = 0.0,
        us_sec_fee_bps: float = 0.0,
        us_sec_fee_min_usd: float = 0.0,
        us_free_commission_notional_usd: float = 0.0,
        us_taf_per_share: float = 0.0,
        us_taf_cap_usd: float = 0.0,
    ):
        self.data = data
        self.portfolio = portfolio
        self.fee_bps = fee_bps
        self.market_of = market_of or {}
        # KR 개별주 매도 거래세(bp). ETF 목록에 없는 KR 심볼의 **매도**에만 붙는다 —
        # ETF 여부를 모르면 개별주로 취급(과대 비용이 과소 비용보다 정직하다:
        # 비용 과소평가는 일중 전략에서 그대로 가짜 엣지가 된다).
        self.kr_stock_sell_tax_bps = kr_stock_sell_tax_bps
        self.kr_etf_symbols = set(kr_etf_symbols)
        self.fx = fx or FixedFxProvider()
        self.slippage_bps = slippage_bps
        # 토스증권 미국주식 실제 요율(2026-08-19 소유자 제공, config/settings.yaml
        # execution 블록에서 주입된다). 기본값 0.0은 하위호환 — 이 인자들을 넘기지
        # 않는 호출부(기존 테스트 등)는 예전 그대로(SEC Fee 없음, $10 이하 면제
        # 없음) 동작한다.
        self.us_sec_fee_bps = us_sec_fee_bps
        self.us_sec_fee_min_usd = us_sec_fee_min_usd
        self.us_free_commission_notional_usd = us_free_commission_notional_usd
        # FINRA TAF(2026-08-21): 미국 **매도** 전용, 금액이 아니라 **주수** 기준
        # 주당 $0.000166, 주문당 상한 $8.30. SEC Fee 와 별개로 둘 다 붙는다 —
        # 저가·대량 주문에서는 이 주수 기준 요금이 SEC Fee 를 지배한다.
        # 기본값 0.0 = 하위호환(기존 호출부 동작 불변).
        self.us_taf_per_share = us_taf_per_share
        self.us_taf_cap_usd = us_taf_cap_usd

    def _fee_bps_for(self, market: str) -> float:
        """시장별 수수료(bp, 편도). dict인데 해당 시장이 없으면 0이 아니라 명시된 값 중
        **최댓값**을 쓴다 — 모르는 시장을 수수료 0으로 취급하면 비용 과소평가고,
        일중 전략에서 그 과소평가는 그대로 가짜 엣지가 된다(_slippage_bps_for와 동일 원칙)."""
        if isinstance(self.fee_bps, dict):
            if market in self.fee_bps:
                return self.fee_bps[market]
            return max(self.fee_bps.values()) if self.fee_bps else 0.0
        return self.fee_bps

    def _commission(self, market: str, notional_local: float) -> float:
        """중개 수수료(bp 기준, 편도). 미국주식은 **주문당 체결금액이
        `us_free_commission_notional_usd`(토스 실제 정책: $10) 이하면 면제** —
        매수·매도 공통. SEC Fee(`_us_sec_fee`)는 이 면제와 별개로 매도에 항상
        붙는다(둘을 섞으면 안 된다 — 소유자가 준 자료는 "수수료 무료"라고만
        했지 SEC Fee까지 면제라고 확인해주지 않았고, 여기서는 비용을 낙관하지
        않는 쪽(SEC Fee는 부과)을 택했다)."""
        if market == "US" and notional_local <= self.us_free_commission_notional_usd:
            return 0.0
        return notional_local * self._fee_bps_for(market) / 10_000

    def _us_sec_fee(self, notional_usd: float) -> float:
        """SEC Fee(미국 **매도** 전용, 2026-08-19 소유자 제공 요율): 매도금액의
        0.00206%, **최소 $0.01**. 소액 주문에서는 요율보다 최소액이 지배한다
        (`max()`가 그 지배 관계를 그대로 표현한다) — $10 이하 커미션 면제 대상
        에도 예외 없이 부과한다."""
        if notional_usd <= 0:
            return 0.0
        return max(notional_usd * self.us_sec_fee_bps / 10_000, self.us_sec_fee_min_usd)

    def _us_taf(self, qty: float) -> float:
        """FINRA TAF(미국 매도 전용): 주당 `us_taf_per_share`, 주문당 상한
        `us_taf_cap_usd`. **주수 기준**이라 SEC Fee(금액 기준)와 지배 영역이
        다르다 — $1짜리 10만 주 매도면 SEC $2.06 vs TAF $8.30(상한)."""
        if qty <= 0 or self.us_taf_per_share <= 0:
            return 0.0
        fee = qty * self.us_taf_per_share
        return min(fee, self.us_taf_cap_usd) if self.us_taf_cap_usd > 0 else fee

    def _slippage_bps_for(self, symbol: str) -> float:
        """종목별 슬리피지(bp). dict인데 종목이 없으면 0이 아니라 dict에 명시된
        값 중 최댓값을 쓴다 — 모르는 종목을 슬리피지 0으로 취급하면 과소평가다."""
        if isinstance(self.slippage_bps, dict):
            if symbol in self.slippage_bps:
                return self.slippage_bps[symbol]
            return max(self.slippage_bps.values()) if self.slippage_bps else 0.0
        return self.slippage_bps

    def place_order(self, order: Order) -> OrderState:
        """즉시 체결 시뮬레이터. **왜 체결되지 않았는지**까지 상태로 돌려준다.

        paper 에는 브로커가 없으므로 못 낸 주문은 전부 `not_submitted`(주문번호 없음)
        다 — 실계좌의 "브로커가 거부했다"와 구분된다.
        """
        quote = self.data.quote(order.symbol)
        if quote is None or quote.price <= 0:
            # quote 가 없어 quote.ts 를 못 쓴다 — 2026-08-31 실측(EC2 orders.jsonl,
            # "ts": null 110건): 이 아래 not_submitted 호출들이 at= 을 안 넘겨
            # oms.accept()의 updated_at 이 그대로 None 으로 남았다. 벽시계로 채운다.
            return oms.not_submitted(
                order, "시세 없음/0 이하 — 주문 생성 불가", at=datetime.now(timezone.utc))
        slippage_bps = self._slippage_bps_for(order.symbol)
        if order.side is Side.BUY:
            price = quote.price * (1 + slippage_bps / 10_000)
        else:
            price = quote.price * (1 - slippage_bps / 10_000)
        # dict는 힌트일 뿐 — 없으면 "US"로 떨어뜨리지 않고 심볼에서 계산한다.
        # market_of는 부팅 시점 스냅샷이라 장중에 편입된 관심종목이 빠져 있고,
        # KR 종목이 "US"로 오인되면 to_krw가 현금에 환율을 곱해 **장부가 통째로
        # 틀어진다** (2026-08-11: 명목 181원짜리 058610 매수가 현금 256,000원을
        # 차감). 수량 오류보다 이쪽이 더 위험하다.
        market = self.market_of.get(order.symbol) or market_of_symbol(order.symbol)
        pos = self.portfolio.positions.get(order.symbol)

        realized_pnl = 0.0
        if order.side is Side.BUY:
            qty = order.qty
            if qty <= 0:
                return oms.not_submitted(
                    order, f"매수 수량이 0 이하 (qty={qty})", at=quote.ts)
            # 매수에는 세금·SEC Fee가 붙지 않는다 — 둘 다 매도 전용(토스 실제
            # 요율표). $10 이하 미국 커미션 면제는 매수·매도 공통이라 _commission이
            # 알아서 처리한다.
            fee = self._commission(market, qty * price)
            if pos is None:
                pos = Position(symbol=order.symbol, qty=0.0, avg_cost=0.0, opened_at=quote.ts)
                self.portfolio.positions[order.symbol] = pos
            notional = qty * price
            new_qty = pos.qty + qty
            if new_qty > 0:
                pos.avg_cost = (pos.avg_cost * pos.qty + notional) / new_qty
            pos.qty = new_qty
            if pos.opened_at is None:
                pos.opened_at = quote.ts
            # 전략별 lot도 함께 갱신한다 — 심볼 합산(pos.qty/avg_cost)은 지금처럼
            # 진실로 유지하고, lot은 그중 이 전략 몫만 별도로 블렌딩한다(2026-08-11
            # 사용자 지시: 여러 전략이 같은 심볼을 각자 몫만 관리).
            lot = pos.ensure_lot(order.strategy_id)
            lot_qty_before = float(lot.get("qty", 0.0))
            lot_avg_before = float(lot.get("avg_cost", 0.0))
            lot_new_qty = lot_qty_before + qty
            if lot_new_qty > 0:
                lot["avg_cost"] = (lot_avg_before * lot_qty_before + notional) / lot_new_qty
            lot["qty"] = lot_new_qty
            self.portfolio.cash -= to_krw(notional + fee, market, self.fx)
        else:
            if pos is None or pos.qty <= 0:
                return oms.not_submitted(order, "보유 수량 없음 — 매도 불가", at=quote.ts)
            # 자기 lot에서만 판다 — 다른 전략의 몫은 절대 건드리지 않는다(2026-08-11
            # 사용자 지시). lot이 없으면(수동 매수/순수 레거시) 기존 동작(pos 전체
            # 기준)으로 폴백하되, lot들이 존재하면 그 lot들 밖의 잔량(고아분)까지만
            # 판다 — 어느 전략도 추적하지 않는 물량을 다른 전략의 lot으로 오인해
            # 팔지 않기 위함이다.
            lots = pos.meta.get("lots")
            lot = lots.get(order.strategy_id) if lots else None
            if lot is not None:
                sellable = float(lot.get("qty", 0.0))
            elif lots:
                tracked = sum(float(l.get("qty", 0.0)) for l in lots.values())
                sellable = max(pos.qty - tracked, 0.0)
            else:
                sellable = pos.qty
            qty = min(order.qty, sellable)
            if qty <= 0:
                return oms.not_submitted(
                    order, f"매도 가능 수량 0 (요청 {order.qty}, 이 전략 몫 {sellable})",
                    at=quote.ts)
            notional = qty * price
            fee = self._commission(market, notional)
            if (
                market == "KR"
                and self.kr_stock_sell_tax_bps > 0
                and order.symbol not in self.kr_etf_symbols
            ):
                # KR 개별주 매도세 반영(2026-08-10 사용자 결정, 2026-08-19 요율
                # 15→20bp 정정 — 코스피/코스닥 공통 증권거래세+농특세 0.2%) —
                # ETF는 거래세 면제
                fee += notional * self.kr_stock_sell_tax_bps / 10_000
            if market == "US":
                # SEC Fee(2026-08-19 소유자 제공 요율) — $10 이하 커미션 면제와
                # 무관하게 매도에 항상 붙는다.
                fee += self._us_sec_fee(notional)
                fee += self._us_taf(qty)
            # 원가는 내 lot의 avg_cost 기준(없으면 심볼 합산 avg_cost로 폴백) —
            # 다른 전략이 더 비싸게/싸게 산 몫이 내 실현손익에 섞이지 않게 한다.
            # 평균단가는 이 매도로 바뀌지 않는다(전량 청산 시에만 0으로 리셋). 리셋
            # 전에 읽어 둬야 부분청산/전량청산이 같은 원가를 쓴다.
            cost_basis = float(lot["avg_cost"]) if lot is not None else pos.avg_cost
            realized_pnl = (price - cost_basis) * qty
            pos.qty -= qty
            self.portfolio.cash += to_krw(notional - fee, market, self.fx)
            if lot is not None:
                remaining = float(lot.get("qty", 0.0)) - qty
                if remaining <= qty * 1e-9:
                    lots.pop(order.strategy_id, None)
                else:
                    lot["qty"] = remaining
            # 잔여 청산 임계는 반드시 체결 수량 대비 "상대"여야 한다. 절대값
            # 1e-9를 쓰면 가격이 높아 수량이 작은 종목에서 정상 잔여가 임계
            # 아래로 떨어지고, 아래 분기가 현금을 넣어주지 않고 포지션만
            # 지워 자산이 증발한다(사다리 테스트 실측: 수수료 0 조건에서
            # 1,000,000 → 985,150원, 잔여 전액 소멸).
            if pos.qty <= qty * 1e-9:
                pos.qty = 0.0
                pos.avg_cost = 0.0
                pos.opened_at = None
                pos.meta = {}  # lots를 포함해 전량 초기화

        self.portfolio.save()
        fill = Fill(
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            ts=quote.ts,
            strategy_id=order.strategy_id,
            fee=fee,
            reason=order.reason,
            realized_pnl=realized_pnl,
            cash_after=self.portfolio.cash,
        )
        # qty 가 order.qty 보다 작을 수 있다(매도 가능 수량 클램프) — 그러면
        # filled_from 이 자동으로 부분체결로 만들고 잔량을 남긴다.
        return oms.report_fill(order, fill, at=quote.ts)

    def positions(self) -> dict[str, Position]:
        return self.portfolio.positions

    def cash(self) -> float:
        return self.portfolio.cash

    def cancel_order(self, order_id: str) -> bool:
        """paper는 즉시 체결 모델이라 미체결 주문이 존재할 수 없다 — 취소할 대상이
        없으므로 항상 False(가짜 성공 금지)."""
        return False

    def open_orders(self) -> list[OpenOrder]:
        """paper는 place_order가 그 자리에서 체결/거부로 끝나 미체결 상태를 만들지
        않는다 — 항상 빈 리스트다."""
        return []
