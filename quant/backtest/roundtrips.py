"""체결 로그 → 라운드트립 FIFO 페어링 — 단일 정의(2026-09-03).

`engine._round_trip_pnl`(pnl 시계열만 필요한 호출부용)과 `analytics.
_round_trip_detail`(진입시각·보유시간·명목가까지 필요한 호출부용)이 같은
FIFO 매수-수수료 배분 루프를 각자 구현하고 있었다 — 반환 형태가 달라
"중복이 아니다"로 여겨졌지만, 매도 1건당 손익(`net_pnl_krw` = 실현손익 −
매도 수수료 − 배분된 매수 수수료)을 계산하는 핵심 로직은 완전히 같았다.
두 곳이 계산을 각자 하면 한쪽만 고치는 버그가 조용히 갈라진다. 이제 둘 다
이 모듈의 `round_trip_detail`을 부르고, 필요한 열만 골라 쓴다.

## 규약

한 행 = 매도 체결 1건. 매수 수수료는 그 매수를 청산한 매도들에 **수량
비율대로** 배분한다(선입선출 페어링, symbol별 독립). `trades["pnl"]`
(= 실현손익 − 매도 수수료)만 쓰면 매수 수수료가 반영되지 않아 왕복 비용의
절반이 사라진다 — 실측: 총수익 −51%인 백테스트에서 profit factor가 1.014로
나왔다(1을 넘으면 "이기는 전략"으로 읽힌다).
"""
from __future__ import annotations

import pandas as pd

_ROUND_TRIP_COLUMNS = [
    "symbol", "entry_ts", "exit_ts", "holding_minutes",
    "net_pnl_krw", "notional_krw", "net_bp", "reason",
]


def round_trip_detail(trades: pd.DataFrame) -> pd.DataFrame:
    """체결 로그 → 라운드트립 상세 테이블.

    열: symbol, entry_ts(그 포지션의 첫 매수 시각), exit_ts, holding_minutes,
    net_pnl_krw(실현손익-배분수수료), notional_krw(매도에 대응하는 매수 명목),
    net_bp, reason. mfe/mae는 여기 없다(현재 체결 로그엔 없어 호출부가 별도로
    채운다).
    """
    cols = _ROUND_TRIP_COLUMNS
    if trades is None or trades.empty:
        return pd.DataFrame(columns=cols)

    pending_fee: dict[str, float] = {}
    pending_qty: dict[str, float] = {}
    pending_notional: dict[str, float] = {}
    pending_entry_ts: dict[str, pd.Timestamp] = {}
    rows: list[dict] = []
    for row in trades.sort_values("ts").itertuples():
        symbol = row.symbol
        if row.side == "buy":
            pending_fee[symbol] = pending_fee.get(symbol, 0.0) + row.fee_krw
            pending_qty[symbol] = pending_qty.get(symbol, 0.0) + row.qty
            pending_notional[symbol] = pending_notional.get(symbol, 0.0) + row.notional_krw
            if symbol not in pending_entry_ts:
                pending_entry_ts[symbol] = row.ts
            continue

        held = pending_qty.get(symbol, 0.0)
        frac = min(row.qty / held, 1.0) if held > 0 else 1.0
        allocated_fee = pending_fee.get(symbol, 0.0) * frac
        allocated_notional = pending_notional.get(symbol, 0.0) * frac
        entry_ts = pending_entry_ts.get(symbol, row.ts)

        pending_fee[symbol] = pending_fee.get(symbol, 0.0) - allocated_fee
        pending_notional[symbol] = pending_notional.get(symbol, 0.0) - allocated_notional
        pending_qty[symbol] = max(held - row.qty, 0.0)

        net_pnl = row.realized_pnl_krw - row.fee_krw - allocated_fee
        notional = allocated_notional if allocated_notional > 0 else row.notional_krw
        holding_minutes = max((row.ts - entry_ts).total_seconds() / 60.0, 0.0)
        rows.append({
            "symbol": symbol, "entry_ts": entry_ts, "exit_ts": row.ts,
            "holding_minutes": holding_minutes, "net_pnl_krw": net_pnl,
            "notional_krw": notional,
            "net_bp": (net_pnl / notional * 1e4) if notional > 0 else 0.0,
            "reason": row.reason,
        })

        if pending_qty[symbol] <= 1e-9:
            pending_qty.pop(symbol, None)
            pending_fee.pop(symbol, None)
            pending_notional.pop(symbol, None)
            pending_entry_ts.pop(symbol, None)

    return pd.DataFrame(rows, columns=cols)


def round_trip_pnl(trades: pd.DataFrame) -> pd.Series:
    """왕복 순손익(KRW) 시계열 — `round_trip_detail`의 `net_pnl_krw` 열만.

    `engine._compute_metrics`/`strategy_report.trade_sharpe`처럼 pnl 값만
    필요한 호출부용 얇은 래퍼."""
    detail = round_trip_detail(trades)
    if detail.empty:
        return pd.Series(dtype=float)
    return pd.Series(detail["net_pnl_krw"].to_numpy(dtype=float), dtype=float)
