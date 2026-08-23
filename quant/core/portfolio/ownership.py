"""엔진 소유 포지션 원장 — "이 계좌에서 **엔진이 진입한** 수량은 얼마인가".

왜 필요한가: 사용자가 같은 토스 계좌에서 손으로도 매매한다. 브로커 holdings()는
엔진 포지션과 사용자 수동 보유를 구분해 주지 않으므로, holdings()만 보고 청산하면
**사용자가 장기 보유하려던 종목을 엔진이 팔아버린다.** 그래서 엔진은 자기가 낸
체결만 여기에 누적해 두고, 매도 수량을 항상 이 원장으로 상한을 건다.

기록되지 않은 종목 = 사용자 수동 보유. 조회(equity 계산, 리포트)에는 포함해도
되지만 **주문 대상으로 삼지 않는다.**

조건주문(서버측 손절) id도 심볼별로 같이 들고 있는다 — 엔진이 스스로 청산했는데
서버측 손절이 남아 있으면, 나중에 사용자가 같은 종목을 손으로 산 순간 그 물량이
엔진이 걸어둔 손절에 팔린다. 청산 시 반드시 취소해야 하므로 id를 잊으면 안 된다.

Portfolio/TradingControl과 동일한 tmp-write-then-replace 원자적 쓰기를 쓴다 —
새 영속화 방식을 만들지 않는다. 네트워크/브로커 임포트 금지.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_PATH = Path("data/state/engine_positions.json")

# 부동소수 누적 오차로 0에 수렴한 잔량을 "아직 보유 중"으로 오인하지 않기 위한 임계.
# 절대값이 아니라 상대값이어야 하지만(가격이 높아 수량이 작은 종목), 여기서 다루는
# 것은 수량뿐이고 미국 분할주 최소 단위가 1e-6주라 그보다 두 자리 아래로 잡는다.
_ZERO_QTY = 1e-8


class EngineOwnership:
    """symbol -> {qty, conditional_order_ids}. 엔진 체결만 누적한다."""

    def __init__(self, state_path: Path | None = DEFAULT_STATE_PATH) -> None:
        """state_path=None이면 영속화하지 않는다(메모리 전용 — 테스트/백테스트)."""
        self.state_path = Path(state_path) if state_path is not None else None
        self._qty: dict[str, float] = {}
        self._protection: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------- 영속화
    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # 원장을 못 읽으면 "엔진 소유 0"으로 시작한다. 이 방향이 안전하다 —
            # 엔진은 아무것도 팔지 못하고, 대사(reconcile)가 불일치를 알린다.
            return
        for symbol, entry in (data.get("positions") or {}).items():
            self._qty[symbol] = float(entry.get("qty", 0.0))
            protection = entry.get("protection")
            if protection:
                self._protection[symbol] = protection

    def save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "positions": {
                symbol: {
                    "qty": qty,
                    "protection": self._protection.get(symbol, {}),
                }
                for symbol, qty in self._qty.items()
            }
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    # --------------------------------------------------------------- 조회
    def owned_qty(self, symbol: str) -> float:
        return self._qty.get(symbol, 0.0)

    def symbols(self) -> set[str]:
        return {s for s, q in self._qty.items() if q > _ZERO_QTY}

    def protection(self, symbol: str) -> dict:
        """{"ids": [conditionalOrderId...], "stop": float|None, "target": float|None}.
        기록이 없으면 빈 dict."""
        return dict(self._protection.get(symbol, {}))

    # --------------------------------------------------------------- 갱신
    def add(self, symbol: str, qty: float) -> None:
        """엔진 매수 체결분을 더한다."""
        if qty <= 0:
            return
        self._qty[symbol] = self.owned_qty(symbol) + qty
        self.save()

    def remove(self, symbol: str, qty: float) -> None:
        """엔진 매도 체결분을 뺀다. 0 이하가 되면 종목 자체를 원장에서 지운다
        (보호주문 기록도 같이 지운다 — 포지션이 없으면 걸어둘 손절도 없다)."""
        if qty <= 0:
            return
        remaining = self.owned_qty(symbol) - qty
        if remaining <= _ZERO_QTY:
            self._qty.pop(symbol, None)
            self._protection.pop(symbol, None)
        else:
            self._qty[symbol] = remaining
        self.save()

    def set_protection(
        self, symbol: str, ids: list[str],
        stop: float | None = None, target: float | None = None,
    ) -> None:
        if ids:
            self._protection[symbol] = {"ids": list(ids), "stop": stop, "target": target}
        else:
            self._protection.pop(symbol, None)
        self.save()
