"""전략별 독립 명목계정 장부 (`risk.capital_mode: per_strategy`).

각 전략에 동일한 시작 자본(기본 1,000만원)을 주고, 이후 그 전략의 체결만 그 전략의
장부에 반영한다 — 전체 계좌에서 지분을 나누는 것이 아니라 전략마다 완전히 분리된
가상 계좌다(2026-08-19 사용자 지시: "각 전략마다 따로 노는 것"). 목적은 모의투자
중 전략별 성장 곡선을 있는 그대로 비교하는 것이다.

**순수 로직 + JSON 영속화만** — 거래 평면(`quant/trade/`) 소속이라 네트워크/DB
금지(`quant/trade/CLAUDE.md` 상당 규칙, `tests/test_architecture.py` FORBIDDEN_EXTERNAL
로 강제됨). 통화 환산은 `quant.core.portfolio.portfolio.to_krw`만 쓴다 — 그 함수는
`quant/core/` 소속이라 `quant/trade/` → `quant/core/` 의존은 허용된 방향이다
(core/CLAUDE.md, test_architecture.py FORBIDDEN 표에 core→trade는 있어도 trade→core는
없다).

**통화 순수성**: `avg_cost`는 그 종목의 표시 통화 그대로 저장한다(KR=원, US=달러) —
`quant/core/models.py`가 이미 지키는 원칙과 동일하다. `cash_krw`/`fees_krw`/
`realized_pnl_krw`는 이름 그대로 항상 KRW다 — 체결 시점의 환율로 그때 한 번만
환산해 누적하고(PaperBroker.place_order와 동일 패턴), 이후 환율이 움직여도 과거
누적값은 바뀌지 않는다. `equity_krw()`만 "지금 이 순간"의 시세×환율로 평가한다.

파일 쓰기는 원자적이다(tmp write + rename) — `quant/trade/risk/manager.py`의
`_save_day_state`와 같은 패턴.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from quant.core.fx import FxProvider
from quant.core.models import Side
from quant.core.portfolio.portfolio import to_krw

logger = logging.getLogger(__name__)

_VERSION = 1
_QTY_EPSILON = 1e-9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StrategyBooks:
    """`{strategy_id: book}` 전체를 들고 있는 컨테이너. `book` 스키마는 모듈
    docstring의 JSON 예시와 동일하다: `cash_krw`/`realized_pnl_krw`/`fees_krw`(전부
    KRW)와 `positions: {symbol: {qty, avg_cost, market}}`(avg_cost는 현지통화)."""

    path: Path
    initial_krw: float
    books: dict[str, dict] = field(default_factory=dict)
    # 전략별 시작 명목자본 override (2026-09-03, `capital_policy: declared`).
    # fixed/equal_split은 전략마다 같은 금액이라 스칼라 `initial_krw` 하나면 됐지만,
    # declared는 전략마다 선언된 capital_fraction이 달라 금액도 다르다. 비어 있으면
    # (fixed/equal_split) 이 필드는 전혀 읽히지 않는다 — 기존 동작 100% 보존.
    # **영속화하지 않는다**: 매 기동에 정책이 다시 계산하고, 이미 만들어진 장부의
    # `book["initial_krw"]`는 생성 시점에 못박히므로(_ensure) 저장할 이유가 없다.
    initial_by_strategy: dict[str, float] = field(default_factory=dict)

    @classmethod
    def load(cls, path: "Path | str", initial_krw: float) -> "StrategyBooks":
        """파일이 없거나 깨졌으면 빈 장부로 시작한다 — 복원 실패가 기동을 막으면
        안 된다(risk/manager.py `_load_day_state`와 같은 원칙)."""
        p = Path(path)
        if not p.exists():
            return cls(path=p, initial_krw=initial_krw, books={})
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            logger.warning("전략별 장부 복원 실패 — 빈 장부로 시작: %s", e)
            return cls(path=p, initial_krw=initial_krw, books={})
        books = data.get("books")
        if not isinstance(books, dict):
            books = {}
        return cls(path=p, initial_krw=float(data.get("initial_krw", initial_krw)), books=books)

    def save(self) -> None:
        """원자적 tmp-replace. 쓰기 실패는 경고만 남기고 삼킨다 — 영속화 실패가
        거래 핫패스를 죽이면 안 된다."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": _VERSION,
                "initial_krw": self.initial_krw,
                "books": self.books,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:  # noqa: BLE001
            logger.warning("전략별 장부 저장 실패(거래는 계속): %s", e)

    def _ensure(self, strategy_id: str) -> dict:
        """strategy_id의 장부를 반환한다(없으면 지금의 self.initial_krw로 새로 만든다).

        `book["initial_krw"]`는 **생성 시점에 한 번만** 못박힌다 — 이후 컨테이너의
        `self.initial_krw`(예: `capital_policy: equal_split`에서 재기동마다 실제
        총현금 ÷ 활성 전략 수로 다시 계산되는 값, 2026-08-19 Phase B)가 바뀌어도
        이미 만들어진 장부의 기준선은 흔들리지 않는다 — 이미 손익이 쌓인 장부의
        출발선을 재기동이 재조정하면 그 손익 해석이 통째로 왜곡된다. 새로 추가된
        전략만 그 시점의 최신 self.initial_krw를 받는다(설계 질문 2 답: 기존
        전략은 재조정 안 함, 신규 전략만 새 분모로 받음).
        """
        book = self.books.get(strategy_id)
        if book is None:
            # declared 정책은 전략마다 시작금이 다르다 — 선언이 있으면 그 값을,
            # 없으면 지금까지처럼 컨테이너 스칼라를 쓴다.
            initial = float(self.initial_by_strategy.get(strategy_id, self.initial_krw))
            book = {
                "cash_krw": initial,
                "initial_krw": initial,
                "realized_pnl_krw": 0.0,
                "fees_krw": 0.0,
                "positions": {},
                "updated": _now_iso(),
            }
            self.books[strategy_id] = book
        return book

    def seed(self, strategy_ids: "Iterable[str]") -> int:
        """아직 없는 전략의 장부를 initial_krw로 미리 만든다. 새로 만든 개수를 반환.

        체결이 나야 장부가 생기면, 첫 거래 전까지 전략별 리포트가 "장부 없음"으로
        비어 보인다 — 지금 이 기능의 목적이 *전략마다 1,000만원이 어떻게 자라는지*를
        처음부터 보는 것이라, 출발선(1,000만원 / +0.00%)이 보이지 않으면 목적을
        절반 잃는다. 기동 시 한 번 불러 출발선을 세운다."""
        created = 0
        for sid in strategy_ids:
            if sid not in self.books:
                self._ensure(sid)
                created += 1
        return created

    def available_cash_krw(self, strategy_id: str) -> float:
        """그 전략이 지금 쓸 수 있는 현금(KRW). 아직 등장한 적 없는 전략은
        initial_krw 전액이 가용하다(첫 조회로 장부를 만든다)."""
        return float(self._ensure(strategy_id)["cash_krw"])

    def equity_krw(
        self, strategy_id: str, marks: "dict[str, float] | None", fx: FxProvider,
    ) -> float:
        """cash + 보유 포지션 평가액(KRW). `marks`(심볼→현지통화 현재가)에 없는
        종목은 avg_cost로 저하(degrade)한다 — risk/manager.py의 계좌 전체 equity
        계산과 같은 원칙(시세 하나 끊겼다고 승인 전체가 막히면 안 된다)."""
        book = self._ensure(strategy_id)
        marks = marks or {}
        total = float(book["cash_krw"])
        for symbol, pos in book["positions"].items():
            qty = float(pos.get("qty", 0.0))
            if qty <= 0:
                continue
            market = pos.get("market") or "US"
            price = marks.get(symbol)
            if price is None or not (price > 0):
                price = float(pos.get("avg_cost", 0.0))
            total += to_krw(qty * price, market, fx)
        return total

    def apply_fill(
        self,
        strategy_id: str,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        fee: float,
        market: str,
        fx: FxProvider,
    ) -> None:
        """체결 1건을 그 전략의 장부에만 반영한다. 원가/현금은 이 book 안에서 닫혀
        있다 — 다른 전략의 체결과 절대 섞이지 않는다(PaperBroker의 lot 분리와 같은
        원칙, 단 여기서는 애초에 전략마다 완전히 다른 장부라 lot 개념 자체가
        필요없다). 저장(`save()`)은 호출부(quant/trade/loop.py)의 몫이다 — 여기서는
        메모리만 갱신해, 한 사이클에 체결이 여러 건이어도 디스크 쓰기는 호출부가
        원하는 시점에 한 번만 하면 된다."""
        book = self._ensure(strategy_id)
        positions = book["positions"]
        pos = positions.get(symbol)
        notional_local = qty * price

        if side is Side.BUY:
            if pos is None:
                pos = {"qty": 0.0, "avg_cost": 0.0, "market": market}
                positions[symbol] = pos
            new_qty = pos["qty"] + qty
            if new_qty > 0:
                pos["avg_cost"] = (pos["avg_cost"] * pos["qty"] + notional_local) / new_qty
            pos["qty"] = new_qty
            pos["market"] = market
            book["cash_krw"] -= to_krw(notional_local + fee, market, fx)
            book["fees_krw"] += to_krw(fee, market, fx)
        else:
            if pos is None:
                # 이 장부가 모르는 매도 — 정상 경로라면 risk.approve()가 브로커
                # 실보유 기준으로 이미 걸러냈어야 한다(장부와 실보유가 어긋난
                # 신호). 조용히 무시하지 않고 원가 0으로 새로 만든다 — 실현손익은
                # 왜곡되지만(가격 전체가 이익으로 잡힘) 현금 누락보다는 드러나는
                # 실패가 낫다.
                logger.warning(
                    "전략별 장부에 없는 포지션 매도 — 장부-실보유 불일치 의심: %s %s",
                    strategy_id, symbol,
                )
                pos = {"qty": 0.0, "avg_cost": price, "market": market}
                positions[symbol] = pos
            cost_basis = float(pos["avg_cost"])
            realized_local = (price - cost_basis) * qty
            pos["qty"] = max(pos["qty"] - qty, 0.0)
            if pos["qty"] <= _QTY_EPSILON:
                positions.pop(symbol, None)
            book["cash_krw"] += to_krw(notional_local - fee, market, fx)
            book["fees_krw"] += to_krw(fee, market, fx)
            book["realized_pnl_krw"] += to_krw(realized_local, market, fx)

        book["updated"] = _now_iso()
