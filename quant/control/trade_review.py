"""데일리 매매 리뷰 — "오늘 어떤 전략이 무슨 생각으로 언제 사고 팔았나"를
(전략, 종목) 카드 단위로 재구성한다 (2026-09-07 오너 요청).

세션 마감 요약(`ledger.session_pnl_summary`)이 "얼마 벌었나"를 답한다면, 이
모듈은 "왜 그 가격에 샀고, 전략이 그린 손절/목표 범위 안에서 실제로 어떻게
움직였나"를 답한다 — 진입 사유(`reason`) 문자열을 구조화 필드로 파싱하고,
전략 파라미터로 손절/목표 밴드를 보강하고, 봉 데이터로 MFE/MAE를 계산한다.

## 순수성

`quant.core`·`quant.control`만 의존한다(`quant/control/ledger.py`의 마커 판별
헬퍼를 재사용). `quant/trade/`는 절대 임포트하지 않는다(루트 CLAUDE.md 아키텍처
불변식) — 전략별 `reason` 문자열 포맷은 각 `quant/trade/strategy/<id>.py`
docstring/코드를 사람이 읽고 이 모듈의 정규식으로 옮겨 적은 것일 뿐, 코드
의존은 없다.

## reason 파싱 — 왜 정규식인가

전략마다 다른 클래스지만 `reason` 문자열은 공통 어휘를 쓴다: `w=`(비중),
`손절=`/`목표=`/`기준=`/`트리거=`(가격, KR은 콤마 천단위·US는 소수점),
`손절 -N%`/`익절 +N%`(비율, close_bet/news_momentum), `[...]`(게이트 태그).
전략별 파서를 17개 쓰는 대신 이 어휘를 한 정규식 세트로 뽑는다 — 새 전략이
같은 어휘를 쓰면 파서 수정 없이 바로 동작한다(실측: scalp_1m/vol_breakout/
pullback_impulse/mr_vwap_quiet/gap_fade/letf_pair가 전부 `손절=`/`목표=` 어휘를
공유한다). 어휘를 안 쓰는 소수(frgn_accumulate류)는 stop/target이 `None`으로
빠지고 `compute_band`가 전략 파라미터로 보강한다 — 없는 걸 0으로 위장하지 않는다.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from quant.control.ledger import (
    is_paper_epoch_marker,
    is_seeding_carry,
    is_seeding_liquidation,
    trades_in_session,
)
from quant.core.strategy_ids import base_strategy_id

_NUM = r"[+-]?\d[\d,]*\.?\d*"

# 시장별 로컬 표시 시간대(2026-09-07 오너 지적 수리) — Plotly.js 날짜축이
# 오프셋 붙은 ISO 문자열의 오프셋을 무시하고 시:분 숫자를 리터럴로 읽는 알려진
# 결함이 있다. 원장 체결(`ts`)은 UTC 오프셋으로 기록되는데 야후 봉은 거래소
# 로컬 tz(오프셋이 다름)로 온다 — 렌더러에 그대로 섞어 넘기면 캔들과 BUY/SELL
# 마커가 차트에서 서로 다른 위치에 찍힌다(실측: 캔들 09:00대, 마커 00:30대).
# `to_market_local_iso()`를 거쳐 **오프셋을 뗀 로컬 벽시계 문자열**로 통일하면
# 렌더러가 뭘 쓰든(Plotly, 손으로 그린 SVG) 같은 기준으로 나란히 찍힌다.
MARKET_TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}
MARKET_TZ_LABEL = {"KR": "KST", "US": "ET"}


def to_market_local_iso(dt: datetime | None, market: str) -> str | None:
    """`dt`(임의 tz, 대개 원장의 UTC 오프셋)를 `market`의 로컬 벽시계로 바꿔
    **오프셋 없이** ISO 문자열로 돌려준다. `market`을 모르면(`MARKET_TZ`에 없는
    값) 변환 없이 오프셋만 뗀다 — 그래도 최소한 렌더러 간 일관성은 지킨다.
    `dt`가 tz 정보가 없으면(방어적 — 원장은 항상 오프셋을 남긴다) 그대로 둔다."""
    if dt is None:
        return None
    tz = MARKET_TZ.get(market)
    if tz is not None and getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(tz)
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.replace(tzinfo=None)
    return dt.isoformat()


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _search(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def parse_entry_reason(reason: str) -> dict[str, Any]:
    """진입 `reason` 문자열을 구조화 필드로. 모르는 필드는 `None`/빈 리스트 —
    없는 정보를 지어내지 않는다."""
    text = str(reason or "")
    pattern = text.split(":", 1)[0].strip() if ":" in text else text.strip()
    gates = re.findall(r"\[([^\]]+)\]", text)
    return {
        "pattern": pattern,
        "gates": gates,
        "weight": _num(_search(rf"w=({_NUM})", text)),
        "stop_price": _num(_search(rf"손절=({_NUM})", text)) or _num(_search(rf"\bstop=({_NUM})", text)),
        "stop_pct": _num(_search(rf"손절\s*-({_NUM})%", text)),
        "target_price": (
            _num(_search(rf"목표(?:\([^)]*\))?=({_NUM})", text))
            or _num(_search(rf"\btarget=({_NUM})", text))
        ),
        "target_pct": _num(_search(rf"익절\s*\+({_NUM})%", text)),
        "basis_price": _num(_search(rf"기준=({_NUM})", text)),
        "trigger_price": _num(_search(rf"트리거=({_NUM})", text)),
    }


def parse_exit_reason(reason: str) -> dict[str, Any]:
    """청산 `reason` 문자열의 가벼운 파싱 — 원문은 항상 그대로 보존하고(카드에
    verbatim 표시), 여기서는 종류 라벨과 진입/현재가만 뽑는다."""
    text = str(reason or "")
    kind = text.split(":", 1)[0].strip() if ":" in text else text.strip()
    return {
        "kind": kind,
        "entry_price": _num(_search(rf"entry=({_NUM})", text)) or _num(_search(rf"진입\s*({_NUM})", text)),
        "price": _num(_search(rf"현재=({_NUM})", text)) or _num(_search(rf"→\s*({_NUM})", text)),
    }


def compute_band(
    strategy_id: str,
    entry_price: float,
    parsed: dict[str, Any],
    strategy_params: dict[str, Any] | None = None,
    risk_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """진입 시점 손절/목표 밴드 — 1순위 `reason`(가격 또는 비율), 2순위 전략
    파라미터, 3순위(적립 계열) 리스크 레일. `band_source`에 실제 쓰인 경로를
    남긴다 — 나중에 "이 밴드 어디서 왔지"를 감사할 수 있게. `stop_note`/
    `target_note`(2026-09-07 추가)는 파생값에만 붙는 사람이 읽는 한 줄 설명
    (예: "+1.5R 부분익절") — 값 자체가 아니라 왜 그 값인지를 보여준다.

    **`trail_bp`는 목표를 만들지 않는다** — 진입 시점에 고정 가격을 못 박는
    `take_profit_bps`/`partial_take_r`와 달리, 트레일링 스탑은 보유 중 고점을
    따라가는 경로 의존적 규칙이라 진입 시점의 "목표가"로 환산하면 숫자를
    지어내는 것이 된다(README 원칙 — 모르는 것을 지어내지 않는다).
    """
    strategy_params = strategy_params or {}
    risk_params = risk_params or {}
    sources: list[str] = []
    stop_note: str | None = None
    target_note: str | None = None

    stop = parsed.get("stop_price")
    if stop is not None:
        sources.append("reason:손절")
    elif parsed.get("stop_pct") is not None:
        stop = entry_price * (1 - parsed["stop_pct"] / 100)
        sources.append("reason:손절%")

    target = parsed.get("target_price")
    if target is not None:
        sources.append("reason:목표")
    elif parsed.get("target_pct") is not None:
        target = entry_price * (1 + parsed["target_pct"] / 100)
        sources.append("reason:익절%")

    base_id = base_strategy_id(strategy_id)
    cfg = strategy_params.get(strategy_id) or strategy_params.get(base_id) or {}
    params = cfg.get("params", cfg) if isinstance(cfg, dict) else {}

    if stop is None:
        stop_pct = params.get("stop_pct")
        if stop_pct:
            stop = entry_price * (1 - float(stop_pct) / 100)
            sources.append("params:stop_pct")
            stop_note = f"-{float(stop_pct):g}% 손절"
        elif base_id in ("frgn_accumulate", "news_accumulate"):
            max_loss = risk_params.get("accumulate_max_loss_pct")
            if max_loss:
                stop = entry_price * (1 - float(max_loss) / 100)
                sources.append("params:risk.accumulate_max_loss_pct")
                stop_note = f"최대손실 -{float(max_loss):g}%"

    if target is None:
        # 고정 bp 익절(scalp_1m류 `take_profit_bps` — 0/미설정이면 꺼진 것).
        take_profit_bps = params.get("take_profit_bps")
        if take_profit_bps:
            target = entry_price * (1 + float(take_profit_bps) / 1e4)
            sources.append("params:take_profit_bps")
            target_note = f"+{float(take_profit_bps):g}bp 익절"
        else:
            # R배수 부분익절(scalp_1m 패턴A/B 공통 — `partial_take_r` × R,
            # R = entry − stop). 손절이 아직 안 잡혀 있으면(위 단계 전부 실패)
            # R을 정의할 수 없어 건너뛴다 — 지어내지 않는다.
            partial_take_r = params.get("partial_take_r")
            if partial_take_r and stop is not None and entry_price > stop:
                target = entry_price + float(partial_take_r) * (entry_price - stop)
                sources.append("params:partial_take_r")
                target_note = f"+{float(partial_take_r):g}R 부분익절"

    if target is None:
        take_profit_pct = params.get("take_profit_pct")
        if take_profit_pct:
            target = entry_price * (1 + float(take_profit_pct) / 100)
            sources.append("params:take_profit_pct")
            target_note = f"+{float(take_profit_pct):g}% 익절"

    return {
        "stop": stop,
        "target": target,
        "band_source": "+".join(sources) if sources else "unknown",
        "stop_note": stop_note,
        "target_note": target_note,
    }


def _ts(row: dict) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(row.get("ts")))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _is_marker(row: dict) -> bool:
    return is_paper_epoch_marker(row) or is_seeding_liquidation(row) or is_seeding_carry(row)


def _mfe_mae_bp(bars: pd.DataFrame | None, entry_price: float, start: datetime, end: datetime) -> dict[str, float | None]:
    if bars is None or bars.empty or entry_price <= 0:
        return {"mfe_bp": None, "mae_bp": None, "mfe_price": None, "mae_price": None}
    try:
        window = bars.loc[(bars.index >= start) & (bars.index <= end)]
    except TypeError:
        return {"mfe_bp": None, "mae_bp": None, "mfe_price": None, "mae_price": None}
    if window.empty:
        return {"mfe_bp": None, "mae_bp": None, "mfe_price": None, "mae_price": None}
    high = float(window["high"].max())
    low = float(window["low"].min())
    return {
        "mfe_bp": (high - entry_price) / entry_price * 1e4,
        "mae_bp": (low - entry_price) / entry_price * 1e4,
        "mfe_price": high,
        "mae_price": low,
    }


def _chart_bars(
    bars: pd.DataFrame | None, start: datetime, end: datetime, market: str, pad_bars: int = 15,
) -> list[dict[str, Any]]:
    """차트용 봉 슬라이스 — entry~exit 구간에 앞뒤 여유(`pad_bars`)를 더해 진입
    직전 맥락과 청산 직후 여진을 함께 보여준다. 캔들 차트는 렌더러(HTML/노트북)
    둘 다 이 필드만 보면 되게 review dict 안에 통째로 담는다 — 렌더 쪽이 별도로
    봉 데이터를 다시 조회할 필요가 없다. 못 찾으면 빈 리스트(차트 생략 근거).

    `ts`는 `to_market_local_iso()`를 거친 **로컬 벽시계·오프셋 없음** 문자열이다
    — 야후가 주는 봉의 tz(거래소 로컬)와 체결(`entries`/`exits`의 `ts`, 원장의
    UTC 오프셋)이 서로 다른 채로 렌더러에 넘어가면 Plotly.js가 오프셋을 무시
    하고 캔들과 BUY/SELL 마커를 차트에서 서로 다른 위치에 찍힌다(2026-09-07
    실측 결함) — 이 함수와 `entries`/`exits` 둘 다 같은 변환을 거쳐야 한다."""
    if bars is None or bars.empty:
        return []
    try:
        sorted_bars = bars.sort_index()
        # 전체 시계열 기준으로 미리 60선을 잡아둔다 — 슬라이스 후에 계산하면
        # 창 시작 부근 60개가 워밍업 부족으로 NaN이 된다(scalp_1m 청산 규칙
        # 자체가 60선을 보므로, 카드에서도 전략이 실제로 본 값과 같아야 한다).
        ma60 = sorted_bars["close"].rolling(60, min_periods=1).mean()
        mask = (sorted_bars.index >= start) & (sorted_bars.index <= end)
        positions = sorted_bars.index[mask]
        if len(positions) == 0:
            return []
        idx_list = sorted_bars.index
        start_pos = idx_list.get_indexer([positions[0]])[0]
        end_pos = idx_list.get_indexer([positions[-1]])[0]
        lo = max(0, start_pos - pad_bars)
        hi = min(len(sorted_bars), end_pos + pad_bars + 1)
        window = sorted_bars.iloc[lo:hi]
        ma60_window = ma60.iloc[lo:hi]
    except TypeError:
        return []
    return [
        {
            "ts": to_market_local_iso(idx, market),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "ma60": (float(m) if pd.notna(m) else None),
        }
        for (idx, row), m in zip(window.iterrows(), ma60_window, strict=True)
    ]


def _group_key(row: dict) -> tuple[str, str]:
    return (str(row.get("strategy_id", "?")), str(row.get("symbol")))


def build_trade_review(
    fills: list[dict],
    bars_by_symbol: dict[str, pd.DataFrame],
    strategy_params: dict[str, Any],
    market: str,
    date: Any,
    risk_params: dict[str, Any] | None = None,
    bar_meta_by_symbol: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """그날(`date`, `market` 세션 기준) 체결을 (전략, 종목) 카드로 묶는다.

    `fills`는 원장 전체(또는 그 이상)를 넘겨도 된다 — 내부에서
    `ledger.trades_in_session`으로 그 시장·그 날짜 세션 구간만 골라내고
    에폭/이식 마커 행을 뺀다(둘 다 프로그램의 매매 판단이 아니다).

    한 (전략, 종목) 그룹 안에서 매수가 수량을 늘리고 매도가 줄인다 — 누적
    수량이 0으로 돌아오면 "closed"(그날 안에 종결), 그렇지 않으면
    "open"(오버나이트 보유 등, 그날 안에서는 미종결). 선행 매수 없이 시작하는
    매도(전날 진입한 오버나이트 포지션의 오늘 청산)는 이 리뷰의 범위 밖이라
    건너뛴다 — "오늘 진입한 전략들의 생각"이 요청의 초점이다.
    """
    bar_meta_by_symbol = bar_meta_by_symbol or {}
    session_fills = [f for f in trades_in_session(fills, market, date) if not _is_marker(f)]
    session_fills.sort(key=lambda x: str(x.get("ts", "")))

    by_key: dict[tuple[str, str], list[dict]] = {}
    for f in session_fills:
        by_key.setdefault(_group_key(f), []).append(f)

    groups: list[dict[str, Any]] = []
    for (strategy_id, symbol), rows in by_key.items():
        qty = 0.0
        cur: list[dict] = []
        started = False
        meta = bar_meta_by_symbol.get(symbol, {})
        for f in rows:
            side = str(f.get("side", "")).upper()
            f_qty = float(f.get("qty", 0) or 0)
            if not started:
                if side != "BUY":
                    continue  # 선행 매수 없는 매도 — 오늘 진입이 아니다
                started = True
            qty += f_qty if side == "BUY" else -f_qty
            cur.append(f)
            if cur and abs(qty) < 1e-9:
                groups.append(_build_group(strategy_id, symbol, cur, bars_by_symbol.get(symbol), strategy_params, risk_params, meta))
                cur, qty, started = [], 0.0, False
        if cur:
            groups.append(_build_group(strategy_id, symbol, cur, bars_by_symbol.get(symbol), strategy_params, risk_params, meta))

    groups.sort(key=lambda g: str(g["entries"][0]["ts"]) if g["entries"] else "")
    summary = _day_summary(groups)
    return {
        "market": market,
        "date": date.isoformat() if hasattr(date, "isoformat") else str(date),
        "groups": groups,
        "summary": summary,
    }


def _build_group(
    strategy_id: str,
    symbol: str,
    fills: list[dict],
    bars: pd.DataFrame | None,
    strategy_params: dict[str, Any],
    risk_params: dict[str, Any] | None,
    bar_meta: dict[str, str] | None = None,
) -> dict[str, Any]:
    market = str(fills[0].get("market") or "")
    buys = [f for f in fills if str(f.get("side", "")).upper() == "BUY"]
    sells = [f for f in fills if str(f.get("side", "")).upper() != "BUY"]
    buy_qty = sum(float(f.get("qty", 0) or 0) for f in buys)
    notional = sum(float(f.get("qty", 0) or 0) * float(f.get("price", 0) or 0) for f in buys)
    entry_price = (notional / buy_qty) if buy_qty > 0 else float(buys[0].get("price", 0) or 0)

    status = "closed" if sells and abs(buy_qty - sum(float(f.get("qty", 0) or 0) for f in sells)) < 1e-9 else "open"
    pnl_known = status == "closed" and all(f.get("realized_pnl") is not None for f in sells)
    gross = sum(float(f.get("realized_pnl") or 0) for f in sells) if pnl_known else None
    fees = sum(float(f.get("fee", 0) or 0) for f in fills)
    pnl = (gross - fees) if gross is not None else None
    pnl_bp = (pnl / notional * 1e4) if (pnl is not None and notional > 0) else None

    entry_ts = _ts(buys[0])
    exit_ts = _ts(sells[-1]) if sells else None
    holding_minutes = None
    if entry_ts is not None and exit_ts is not None:
        holding_minutes = (exit_ts - entry_ts).total_seconds() / 60.0

    mfe_mae = {"mfe_bp": None, "mae_bp": None, "mfe_price": None, "mae_price": None}
    bars_window: list[dict[str, Any]] = []
    bars_available = bars is not None and not bars.empty
    if entry_ts is not None:
        window_end = exit_ts or (bars.index.max() if bars_available else entry_ts)
        mfe_mae = _mfe_mae_bp(bars, entry_price, entry_ts, window_end)
        bars_window = _chart_bars(bars, entry_ts, window_end, market)

    entry_reason = str(buys[0].get("reason") or "")
    parsed_entry = parse_entry_reason(entry_reason)
    band = compute_band(strategy_id, entry_price, parsed_entry, strategy_params, risk_params)

    base_id = base_strategy_id(strategy_id)
    cfg = strategy_params.get(strategy_id) or strategy_params.get(base_id) or {}
    key_params = cfg.get("params", cfg) if isinstance(cfg, dict) else {}

    exit_reason = str(sells[-1].get("reason") or "") if sells else None

    def _local_fill_ts(f: dict) -> str | None:
        # `_ts(f)` 실패(파싱 불가, 원장 손상 등)는 원문 그대로 보여준다 —
        # 없는 값을 지어내거나 조용히 버리지 않는다.
        converted = to_market_local_iso(_ts(f), market)
        return converted if converted is not None else f.get("ts")

    return {
        "strategy_id": strategy_id,
        "symbol": symbol,
        "market": market,
        "tz_label": MARKET_TZ_LABEL.get(market, market),
        "status": status,
        "entries": [
            {"ts": _local_fill_ts(f), "price": float(f.get("price", 0) or 0), "qty": float(f.get("qty", 0) or 0), "reason": f.get("reason")}
            for f in buys
        ],
        "exits": [
            {"ts": _local_fill_ts(f), "price": float(f.get("price", 0) or 0), "qty": float(f.get("qty", 0) or 0), "reason": f.get("reason")}
            for f in sells
        ],
        "entry_price": entry_price,
        "holding_minutes": holding_minutes,
        "pnl": pnl,
        "pnl_bp": pnl_bp,
        "pnl_known": pnl_known,
        "fees": fees,
        "notional": notional,
        **mfe_mae,
        "band": band,
        "bars_available": bars_available,
        "bars_source": (bar_meta or {}).get("source") if bars_available else None,
        "bars_interval": (bar_meta or {}).get("interval") if bars_available else None,
        "bars_window": bars_window,
        "thinking": {
            "entry_reason": entry_reason,
            "entry_parsed": parsed_entry,
            "exit_reason": exit_reason,
            "exit_parsed": parse_exit_reason(exit_reason) if exit_reason else None,
            "strategy_params": key_params,
        },
    }


def _day_summary(groups: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, list[dict]] = {}
    for g in groups:
        by_strategy.setdefault(g["strategy_id"], []).append(g)

    per_strategy: dict[str, Any] = {}
    for strategy_id, gs in by_strategy.items():
        known = [g for g in gs if g["pnl_known"]]
        n = len(known)
        wins = [g for g in known if g["pnl"] > 0]
        per_strategy[strategy_id] = {
            "n": n,
            "n_open": sum(1 for g in gs if g["status"] == "open"),
            "win_rate": (len(wins) / n) if n else None,
            "net_bp": sum(g["pnl_bp"] for g in known) if n else None,
            "best": max(known, key=lambda g: g["pnl_bp"])["symbol"] if known else None,
            "best_bp": max((g["pnl_bp"] for g in known), default=None),
            "worst": min(known, key=lambda g: g["pnl_bp"])["symbol"] if known else None,
            "worst_bp": min((g["pnl_bp"] for g in known), default=None),
        }

    all_known = [g for g in groups if g["pnl_known"]]
    n_total = len(all_known)
    totals = {
        "n": n_total,
        "n_open": sum(1 for g in groups if g["status"] == "open"),
        "win_rate": (sum(1 for g in all_known if g["pnl"] > 0) / n_total) if n_total else None,
        "net_bp": sum(g["pnl_bp"] for g in all_known) if n_total else None,
    }
    return {"per_strategy": per_strategy, "totals": totals}


def format_telegram_line(review: dict[str, Any], url: str | None = None) -> str:
    """텔레그램 한 줄 요약 — "📈 매매 리뷰 KR 09-07: 11트립 · 승률 45% · 순 -12bp
    · 최고 scalp_1m 105560 +38bp" 형식. 오늘 체결이 아예 없으면 빈 문자열
    (호출부가 조용히 스킵하는 근거) — "0트립"을 발송해 소음을 늘리지 않는다."""
    if not review.get("groups"):
        return ""
    t = review["summary"]["totals"]
    mmdd = "-".join(str(review["date"]).split("-")[1:])
    parts = [f"📈 매매 리뷰 {review['market']} {mmdd}: {t['n']}트립"]
    if t["n_open"]:
        parts[-1] += f"(+{t['n_open']} 보유중)"
    if t["win_rate"] is not None:
        parts.append(f"승률 {t['win_rate']:.0%}")
    if t["net_bp"] is not None:
        parts.append(f"순 {t['net_bp']:+.0f}bp")
    best_sid, best_symbol, best_bp = None, None, None
    for sid, s in review["summary"]["per_strategy"].items():
        if s["best_bp"] is not None and (best_bp is None or s["best_bp"] > best_bp):
            best_sid, best_symbol, best_bp = sid, s["best"], s["best_bp"]
    if best_sid:
        parts.append(f"최고 {best_sid} {best_symbol} {best_bp:+.0f}bp")
    line = " · ".join(parts)
    if url:
        line += f"\n{url}"
    return line
