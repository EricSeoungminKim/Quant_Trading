"""거래 다차원 분석 — `BacktestResult.trades`(체결 로그)를 사람이 판단할 수 있는
표로 바꾼다 (2026-09-03).

## 왜 여기 있나

`fitness.py`는 채택 여부를 논할 **자격**(sufficient)만 보고, `strategy_report.py`는
quant-expert §4 형식의 한 장짜리 요약만 낸다. 둘 다 "왜 이런 성적이 나왔나"에는
답하지 않는다 — 시간대별로 몰리는지, 특정 종목이 전체를 먹여 살리는지, 청산
사유별로 성격이 다른지는 트레이드를 쪼개봐야 보인다. 이 모듈은 그 쪼개는 일만
한다. **판정하지 않는다** — `gate.py`가 판정을 맡는다(같은 분업을
`ledger.py`/`walkforward.py`와 공유한다).

## 라운드트립 재구성

엔진의 `trades` 는 체결(fill) 단위 로그이고 라운드트립이 아니다. `engine._round_trip_pnl`
과 같은 규약(**매도 1건 = 1 트레이드**, 매수 수수료는 수량 비율로 배분)을 그대로
따르되, 이 모듈은 그 위에 진입 시각·보유시간·청산 사유까지 함께 들고 다녀야 해서
자체 페어링 함수(`_round_trip_detail`)를 둔다 — `engine._round_trip_pnl`은 pnl 값
하나만 내므로 재사용할 수 없다(중복이 아니라 반환 형태가 다르다).

## 표본 부족을 점수로 위장하지 않는다

`fitness.MIN_ROUND_TRIPS`(30)와 같은 기준을 그대로 쓴다 — 이 저장소의 표본선은
하나뿐이어야 한다. 미만이면 숫자를 내는 대신 `judgeable: False`를 세우고
호출부가 "판단 불가"를 찍는다.
"""
from __future__ import annotations

import math
import random
import statistics as _stats
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.backtest.fitness import MIN_ROUND_TRIPS
from quant.backtest.statistics import EULER_MASCHERONI
from quant.backtest.strategy_report import trade_sharpe as _trade_sharpe
from quant.control.ledger import _wilson_ci

__all__ = ["analyze_trades"]

_UNKNOWN = "판단 불가"

# ledger.py의 세션 시간대 맵과 같은 값 — hour-of-day/day-of-week 버킷을 시장
# 현지시간으로 매기기 위함. 새로 정의하지 않고 값만 복제하는 이유는 ledger.py의
# `_SESSION_TZ`가 모듈 전역 상수(비공개)라 임포트 안정성이 떨어지고, quant/backtest/
# 는 quant/control/을 몰라도 되는 게 원래 결합 방향(엔진 쪽이 더 안쪽 평면)이라서다.
_MARKET_TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}

_HOLDING_BUCKETS = [
    (0, 5, "<5분"), (5, 15, "5-15분"), (15, 60, "15-60분"),
    (60, 240, "60-240분"), (240, math.inf, ">240분"),
]

# 청산 사유 문자열에서 찾는 태그(우선순위 순 — 먼저 매치되는 것을 쓴다).
# quant/trade/strategy/*.py 의 실제 reason 문자열에서 뽑았다(예:
# "EoD 청산: ...", "손절: ...", "전량 익절(+...)", "LLM 트레이더 하드레일 손절(...)").
_EXIT_REASON_TAGS = ["EoD", "손절", "익절", "트레일", "하드", "세션 롤", "청산"]

_MC_ITERS = 2000
_MC_SEED = 42


def _classify_exit_reason(reason: str) -> str:
    r = str(reason or "")
    for tag in _EXIT_REASON_TAGS:
        if tag in r:
            return tag
    return "기타"


def _holding_bucket(minutes: float) -> str:
    for lo, hi, label in _HOLDING_BUCKETS:
        if lo <= minutes < hi:
            return label
    return ">240분"


def _to_market_tz(ts: pd.Timestamp, market: str) -> pd.Timestamp:
    tz = _MARKET_TZ.get(market)
    if tz is None:
        raise ValueError(f"모르는 시장: {market!r} (KR/US만 지원)")
    ts = pd.Timestamp(ts)
    return ts.tz_localize(tz) if ts.tzinfo is None else ts.tz_convert(tz)


def _round_trip_detail(trades: pd.DataFrame) -> pd.DataFrame:
    """체결 로그 → 라운드트립 상세 테이블.

    한 행 = 매도 체결 1건(engine._round_trip_pnl과 같은 규약). 열:
    symbol, entry_ts(그 포지션의 첫 매수 시각), exit_ts, holding_minutes,
    net_pnl_krw(실현손익-배분수수료), notional_krw(매도에 대응하는 매수 명목),
    net_bp, reason, mfe/mae(현재 체결 로그엔 없어 항상 NaN — analyze_trades가
    그 사실을 note로 밝힌다).
    """
    cols = ["symbol", "entry_ts", "exit_ts", "holding_minutes",
            "net_pnl_krw", "notional_krw", "net_bp", "reason"]
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


def _bucket_stats(rt: pd.DataFrame, key: pd.Series) -> dict:
    out: dict[str, dict] = {}
    for label, group in rt.groupby(key):
        bps = group["net_bp"]
        n = len(group)
        out[str(label)] = {
            "n": n,
            "expectancy_bp": round(float(bps.mean()), 3),
            "win_rate": round(float((bps > 0).mean()), 4),
        }
    return out


def _expected_max_streak(n: int, p: float) -> float | None:
    """독립 베르누이 n회에서 기대되는 최장 연속(run) 길이 — Schilling(1990) 근사.

        E[longest run] ≈ log_{1/p}(n·q) + γ/ln(1/p) − 1/2   (q = 1-p)

    실제 트레이드가 독립이라는 뜻이 아니다 — "만약 승패가 동전던지기처럼
    독립이라면 이 정도 길이의 연승/연패는 우연히도 나온다"는 **기준선**이다.
    실측 최장연패가 이 기준선보다 훨씬 길면 승패 사이에 상관(예: 레짐 지속)이
    있다는 신호다."""
    if n <= 0 or not (0.0 < p < 1.0):
        return None
    q = 1.0 - p
    if n * q < 1.0:
        return 0.0
    log_inv_p = math.log(1.0 / p)
    value = math.log(n * q) / log_inv_p + EULER_MASCHERONI / log_inv_p - 0.5
    return max(0.0, value)


def _streaks(net_bp: pd.Series) -> dict:
    n = len(net_bp)
    if n == 0:
        return {
            "max_consecutive_wins": 0, "max_consecutive_losses": 0,
            "expected_max_win_streak": None, "expected_max_loss_streak": None,
        }
    is_win = list(net_bp > 0)
    max_win_run = max_loss_run = 0
    cur_win = cur_loss = 0
    for w in is_win:
        if w:
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win_run = max(max_win_run, cur_win)
        max_loss_run = max(max_loss_run, cur_loss)
    win_rate = sum(is_win) / n
    return {
        "max_consecutive_wins": max_win_run,
        "max_consecutive_losses": max_loss_run,
        "expected_max_win_streak": _expected_max_streak(n, win_rate),
        "expected_max_loss_streak": _expected_max_streak(n, 1.0 - win_rate),
    }


def _monte_carlo_max_dd(net_bp: list[float], *, seed: int = _MC_SEED,
                         n_iters: int = _MC_ITERS) -> dict:
    """트레이드 **순서**만 셔플한(값은 그대로) 몬테카를로 — "이 트레이드들이 다른
    순서로 나왔다면 최대낙폭이 얼마나 더 나빴을 수 있나"에 답한다. 시드 고정 —
    같은 원장에서 같은 숫자가 나와야 리포트가 실행마다 흔들리지 않는다
    (control/ledger._permutation_p와 같은 이유)."""
    n = len(net_bp)
    if n < 2:
        return {
            "seed": seed, "n_iters": n_iters,
            "max_dd_bp_mean": None, "max_dd_bp_median": None, "max_dd_bp_p95": None,
        }
    rng = random.Random(seed)
    sample = list(net_bp)
    dds: list[float] = []
    for _ in range(n_iters):
        order = sample[:]
        rng.shuffle(order)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for v in order:
            cum += v
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        dds.append(max_dd)
    dds.sort()
    mean_dd = sum(dds) / n_iters
    mid = n_iters // 2
    median_dd = dds[mid] if n_iters % 2 else (dds[mid - 1] + dds[mid]) / 2
    p95_idx = min(n_iters - 1, math.ceil(0.95 * n_iters) - 1)
    return {
        "seed": seed, "n_iters": n_iters,
        "max_dd_bp_mean": round(mean_dd, 3),
        "max_dd_bp_median": round(median_dd, 3),
        "max_dd_bp_p95": round(dds[p95_idx], 3),
    }


def _cost_sensitivity(mean_net_bp: float, cost_bp: float) -> dict:
    """비용을 1x/1.5x/2x로 올렸을 때의 기대값(bp). `net_bp`는 이미 실제 부담한
    비용(대략 `cost_bp` 가정)을 뺀 값이라, k배 시나리오는 추가로 `(k-1)*cost_bp`만
    더 빼면 된다 — 비용 구조 자체를 다시 시뮬레이션하지 않는다(왕복당 정률
    가정 하의 근사)."""
    return {
        "1x": round(mean_net_bp, 3),
        "1.5x": round(mean_net_bp - 0.5 * cost_bp, 3),
        "2x": round(mean_net_bp - 1.0 * cost_bp, 3),
    }


def _equity_curve_stats(rt: pd.DataFrame) -> dict:
    """트레이드 단위 누적곡선(bp)의 MDD·회복일수·water-under 비중.

    **한계(숨기지 않는다)**: 이건 일별 자산곡선이 아니라 트레이드 사이 간격을
    그대로 시간축으로 쓴 근사다 — 하루에 트레이드가 없는 구간은 이 곡선에
    아예 나타나지 않는다. 봉 단위 정밀도가 필요하면 `engine.BacktestResult
    .equity_curve`를 따로 봐야 한다. 여기서는 "트레이드 관점에서" 낙폭이
    얼마나 오래갔는지를 본다."""
    if rt.empty:
        return {"mdd_bp": None, "recovery_days": None,
                "time_under_water_days": None, "time_under_water_pct": None}
    ordered = rt.sort_values("exit_ts").reset_index(drop=True)
    cum = ordered["net_bp"].cumsum()
    running_max = cum.cummax()
    drawdown = cum - running_max
    mdd_bp = float(drawdown.min())

    ts = ordered["exit_ts"]
    trough_pos = int(drawdown.values.argmin())
    trough_ts = ts.iloc[trough_pos]
    peak_before = running_max.iloc[trough_pos]

    recovery_days = None
    after = cum.iloc[trough_pos + 1:]
    recovered = after[after >= peak_before]
    if not recovered.empty:
        recovery_ts = ts.loc[recovered.index[0]]
        recovery_days = round((recovery_ts - trough_ts).total_seconds() / 86400.0, 2)

    underwater = drawdown < 0
    if len(ts) > 1:
        gaps_days = ts.diff().dt.total_seconds().fillna(0.0) / 86400.0
        tuw_days = float(gaps_days[underwater].sum())
        total_days = float((ts.iloc[-1] - ts.iloc[0]).total_seconds() / 86400.0)
        tuw_pct = (tuw_days / total_days * 100) if total_days > 0 else None
    else:
        tuw_days, tuw_pct = 0.0, 0.0

    return {
        "mdd_bp": round(mdd_bp, 3),
        "recovery_days": recovery_days,
        "time_under_water_days": round(tuw_days, 2),
        "time_under_water_pct": None if tuw_pct is None else round(tuw_pct, 2),
    }


def analyze_trades(trades: pd.DataFrame, *, market: str, cost_bp: float) -> dict:
    """`BacktestResult.trades`(체결 로그) → 다차원 트레이드 분석 dict. 순수 함수.

    `market`은 hour-of-day/day-of-week 버킷을 어느 시간대로 계산할지("KR"→Asia/Seoul,
    "US"→America/New_York) 정한다 — 여러 시장이 섞인 유니버스라면 호출부가 시장별로
    trades를 나눠 각각 부른다(이 함수는 단일 시간대만 안다).

    `cost_bp`는 비용 민감도 계산에 쓰는 왕복 비용 가정(bp) — `net_bp`가 이미 반영한
    비용 수준을 호출부가 명시적으로 알려준다(fitness.evaluate가 실제 부담 비용을
    `cost_bps`로 내는 것과 같은 정보를, 여기서는 시나리오 배율의 기준으로 쓴다).

    `n < fitness.MIN_ROUND_TRIPS`(30)면 `judgeable: False`이고 나머지 수치 필드는
    전부 None — 표본 부족을 그럴듯한 숫자로 덮지 않는다."""
    rt = _round_trip_detail(trades)
    n = len(rt)

    out: dict = {"n": n, "judgeable": n >= MIN_ROUND_TRIPS, "market": market}
    if n < MIN_ROUND_TRIPS:
        out.update({
            "note": f"{_UNKNOWN} — 라운드트립 {n}건 (최소 {MIN_ROUND_TRIPS}건 필요)",
            "win_rate": None, "win_rate_ci": None, "payoff_ratio": None,
            "expectancy_bp": None, "profit_factor": None,
            "by_hour_of_day": {}, "by_day_of_week": {}, "by_symbol": {},
            "by_holding_bucket": {}, "by_exit_reason": {},
            "streaks": _streaks(rt["net_bp"]) if n else {
                "max_consecutive_wins": 0, "max_consecutive_losses": 0,
                "expected_max_win_streak": None, "expected_max_loss_streak": None,
            },
            "mfe_mae": {"available": False,
                        "note": "체결 로그에 mfe/mae 필드가 없다 — 봉내 최대유리/불리 "
                                "가격이 기록되지 않는다"},
            "monte_carlo_max_dd": _monte_carlo_max_dd(list(rt["net_bp"])),
            "cost_sensitivity": None,
            "equity_curve": {"mdd_bp": None, "recovery_days": None,
                              "time_under_water_days": None, "time_under_water_pct": None},
            "net_bp_series": list(rt["net_bp"]),
        })
        return out

    net_bp = rt["net_bp"]
    wins_mask = net_bp > 0
    wins_n = int(wins_mask.sum())
    win_rate = wins_n / n
    lower, upper = _wilson_ci(wins_n, n)

    wins, losses = net_bp[wins_mask], net_bp[~wins_mask]
    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0
    payoff_ratio = (avg_win / -avg_loss) if avg_loss < 0 else (float("inf") if avg_win > 0 else 0.0)
    gross_profit = float(wins.sum())
    gross_loss = float(-losses.sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    expectancy_bp = float(net_bp.mean())

    entry_local = rt["entry_ts"].apply(lambda ts: _to_market_tz(ts, market))
    hour_key = entry_local.apply(lambda ts: ts.hour)
    dow_key = entry_local.apply(lambda ts: ts.strftime("%a"))
    holding_key = rt["holding_minutes"].apply(_holding_bucket)
    reason_key = rt["reason"].apply(_classify_exit_reason)

    out.update({
        "win_rate": round(win_rate, 4),
        "win_rate_ci": (round(lower, 4), round(upper, 4)),
        "payoff_ratio": round(payoff_ratio, 3) if math.isfinite(payoff_ratio) else payoff_ratio,
        "expectancy_bp": round(expectancy_bp, 3),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else profit_factor,
        "by_hour_of_day": _bucket_stats(rt, hour_key),
        "by_day_of_week": _bucket_stats(rt, dow_key),
        "by_symbol": _bucket_stats(rt, rt["symbol"]),
        "by_holding_bucket": _bucket_stats(rt, holding_key),
        "by_exit_reason": _bucket_stats(rt, reason_key),
        "streaks": _streaks(net_bp),
        "mfe_mae": {"available": False,
                    "note": "체결 로그에 mfe/mae 필드가 없다 — 봉내 최대유리/불리 "
                            "가격이 기록되지 않는다"},
        "monte_carlo_max_dd": _monte_carlo_max_dd(list(net_bp)),
        "cost_sensitivity": _cost_sensitivity(expectancy_bp, cost_bp),
        "equity_curve": _equity_curve_stats(rt),
        # trade_sharpe: 거래당(관측당) 샤프 — gate.py의 deflated Sharpe 입력.
        # strategy_report.trade_sharpe()를 그대로 재사용(중복 구현 금지) — bp 단위
        # pnl 시리즈를 넣으면 KRW 절대액 대신 사이징에 무관한 샤프가 나온다.
        "trade_sharpe": _trade_sharpe(net_bp),
        "net_bp_series": list(net_bp),
    })
    return out
