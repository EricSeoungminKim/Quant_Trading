"""전략 성적 보고서 — quant-expert §4 형식을 **코드가 강제한다** (2026-08-28).

## 왜

`.claude/skills/quant-expert/SKILL.md` §4는 성과를 보고할 때 반드시 함께 낼
항목을 정해 놓았다(구간·표본·검증 방식·OOS 성과·과최적화·비용 가정·탐색
횟수·최악 구간). 그런데 그건 **사람이 기억해야 하는 규칙**이었다. 사람이
기억하는 규칙은 바쁠 때 빠지고, 빠진 항목은 빠졌다는 사실조차 남기지 않는다 —
"샤프 1.8"만 적힌 한 줄은 탐색을 100번 했는지 1번 했는지 말하지 않는다.

이 모듈은 그 형식을 출력 코드로 굳힌다. 항목이 빠질 수 없고, 표본이 모자라면
빈칸을 그럴듯한 숫자로 채우는 대신 **"판단 불가"**가 찍힌다.

## 판정하지 않는다

여기서 "채택/기각"은 나오지 않는다. 숫자와 그 숫자를 얼마나 믿을 수 있는지를
내고, 판단은 사람이 한다(`forensics.py`·`walkforward.py`와 같은 계약).
"""
from __future__ import annotations

import math

import pandas as pd

from quant.backtest.engine import BacktestResult, _round_trip_pnl
from quant.backtest.fitness import MIN_ROUND_TRIPS, Fitness
from quant.backtest.statistics import (
    deflated_sharpe,
    expected_max_sharpe,
    min_track_record_length,
    probabilistic_sharpe,
)

__all__ = ["report_text", "trade_sharpe"]

_UNKNOWN = "판단 불가"

# 왜도/첨도 추정에 필요한 최소 표본. 4건 미만이면 pandas 도 NaN 을 낸다 —
# 그때는 정규 가정(0, 3)으로 물러서되 그 사실을 출력에 밝힌다.
_MIN_FOR_MOMENTS = 8


def trade_sharpe(pnl: pd.Series) -> dict:
    """왕복 손익 시계열 → **거래 1건당** 샤프와 모멘트.

    자산곡선(봉 단위) 샤프를 쓰지 않는 이유: 15분봉 자산곡선은 포지션이 없는
    동안 수익률이 0이라 관측 수가 수천 개로 부풀고, 그 n 이 PSR/DSR 분모에
    그대로 들어가면 무엇이든 유의해 보인다. **표본은 거래 수다** — quant-expert
    §2가 "거래 수를 빼고 샤프만 말하지 마라"고 하는 것과 같은 축이다.

    한계(숨기지 않는다): 손익을 KRW 절대액으로 재므로 사이징이 크게 변하면
    분산이 사이징을 반영한다. 우리 사이징은 현금 비율 고정이라 실무적으로
    문제되지 않지만, 사이징 실험 중이라면 이 값을 그대로 믿으면 안 된다.

    반환: `{"n": int, "sharpe": float|None, "skew": float, "kurtosis": float,
             "moments_estimated": bool}`
    """
    n = int(len(pnl))
    out = {"n": n, "sharpe": None, "skew": 0.0, "kurtosis": 3.0, "moments_estimated": False}
    if n < 2:
        return out
    std = float(pnl.std())
    if not std > 0:
        return out
    out["sharpe"] = float(pnl.mean()) / std
    if n >= _MIN_FOR_MOMENTS:
        skew, ex_kurt = float(pnl.skew()), float(pnl.kurt())
        if not (math.isnan(skew) or math.isnan(ex_kurt)):
            # pandas 의 kurt() 는 초과첨도(정규=0)다. statistics.py 는 첨도를
            # 받는다(정규=3) — 여기서 한 번만 변환한다.
            out["skew"], out["kurtosis"] = skew, ex_kurt + 3.0
            out["moments_estimated"] = True
    return out


def _fmt(value, spec: str = "+.2f") -> str:
    return _UNKNOWN if value is None else format(value, spec)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _period_line(result: BacktestResult, source: str) -> str:
    curve = result.equity_curve
    if curve is None or curve.empty:
        return f"구간:      데이터 없음 (source={source})"
    kind = "**합성 데이터(stub) — 성과 판단 불가**" if source == "stub" else f"실데이터({source})"
    return (
        f"구간:      {curve.index[0].date()} ~ {curve.index[-1].date()}  {kind}"
    )


def _sample_line(result: BacktestResult, fit: Fitness) -> str:
    curve = result.equity_curve
    days = 0 if curve is None or curve.empty else len({ts.date() for ts in curve.index})
    warn = "" if fit.n_round_trips >= MIN_ROUND_TRIPS else (
        f"  ⚠️ 왕복 {MIN_ROUND_TRIPS}건 미만 — 아래 성과 지표는 전부 참고용이다"
    )
    return (
        f"표본:      {days} 거래일 · 체결 {fit.n_fills}건 · 왕복 {fit.n_round_trips}건{warn}"
    )


def _oos_block(folds: list[dict], stability: dict, window_days: int, step_days: int) -> list[str]:
    lines = [
        f"검증 방식: walk-forward — 창 {window_days}거래일 / 간격 {step_days}달력일, "
        f"fold {len(folds)}개 (파라미터 탐색 없음, 각 창 OOS)",
    ]
    if not folds:
        lines.append(f"성과(OOS): {_UNKNOWN} — fold가 없다")
        return lines

    trips = sum(int(f.get("n_round_trips") or 0) for f in folds)
    net = [float(f["net_bps"]) for f in folds if f.get("net_bps") is not None]
    sharpes = [float(f["sharpe"]) for f in folds if f.get("sharpe") is not None]
    wins = [float(f["win_rate_pct"]) for f in folds if f.get("win_rate_pct") is not None]
    mdds = [float(f["mdd_pct"]) for f in folds if f.get("mdd_pct") is not None]

    if trips == 0:
        lines.append(f"성과(OOS): {_UNKNOWN} — fold 전체 왕복 0건 (거래가 없었다)")
    else:
        lines.append(
            f"성과(OOS): 왕복 {trips}건 · 순bp 중앙 {_fmt(_median(net))} "
            f"(최소 {_fmt(min(net) if net else None)} / 최대 {_fmt(max(net) if net else None)})"
        )
        lines.append(
            f"           샤프(연율,자산곡선) 중앙 {_fmt(_median(sharpes))} · "
            f"승률 중앙 {_fmt(_median(wins), '.1f')}% · MDD 최악 {_fmt(min(mdds) if mdds else None, '.2f')}%"
        )
    lines.append(f"           {stability.get('verdict_hint', '')}")
    return lines


def _overfit_block(
    is_sharpe_annual: float, folds: list[dict], trade_stat: dict, n_trials: int,
) -> list[str]:
    """과최적화 진단 — 인샘플/OOS 격차 + **탐색 횟수를 반영한 deflated Sharpe**."""
    oos_sharpes = [float(f["sharpe"]) for f in folds if f.get("sharpe") is not None]
    oos_median = _median(oos_sharpes)
    gap = None if oos_median is None else is_sharpe_annual - oos_median
    lines = [
        f"과최적화:  인샘플 샤프 {is_sharpe_annual:+.2f} vs OOS 중앙 {_fmt(oos_median)}"
        + ("" if gap is None else f" (격차 {gap:+.2f})"),
    ]

    n, sr = trade_stat["n"], trade_stat["sharpe"]
    if sr is None:
        lines.append(
            f"           거래당 샤프: {_UNKNOWN} — 거래 {n}건(2건 미만이거나 분산 0)"
        )
        lines.append(f"           탐색 {n_trials}회 반영 deflated Sharpe: {_UNKNOWN} (표본 없음)")
        return lines

    skew, kurt = trade_stat["skew"], trade_stat["kurtosis"]
    moments = "실측 왜도/첨도" if trade_stat["moments_estimated"] else "정규 가정(표본<8)"
    e_max = expected_max_sharpe(n_trials, n)
    dsr = deflated_sharpe(sr, n_trials, n, skew, kurt)
    psr = probabilistic_sharpe(sr, 0.0, n, skew, kurt)
    trl = min_track_record_length(sr, e_max, skew, kurt, 0.95)

    lines.append(
        f"           거래당 샤프 {sr:+.3f} (n={n} 왕복, {moments}) · PSR(vs 0) {psr:.2f}"
    )
    lines.append(
        f"           탐색 {n_trials}회 → 우연의 기준선 E[max SR] {e_max:+.3f} · "
        f"**deflated Sharpe {dsr:.2f}**"
    )
    if math.isinf(trl):
        lines.append(
            "           MinTRL: ∞ — 이 샤프는 우연의 기준선 이하다. "
            "표본을 더 모아도 유의해지지 않는다(전략을 바꿔야 한다)."
        )
    else:
        need = math.ceil(trl)
        short = max(need - n, 0)
        lines.append(
            f"           MinTRL: 95% 신뢰로 믿으려면 왕복 {need}건 필요 — "
            + ("충족" if short == 0 else f"{short}건 부족")
        )
    lines.append(
        f"           ※ 탐색 {n_trials}회는 **호출자가 신고한 값**이다. 실제로 시험한 "
        "변형이 더 많으면 위 숫자는 그만큼 관대하다."
    )
    return lines


def _worst_block(result: BacktestResult, folds: list[dict]) -> list[str]:
    lines = []
    mdd = (result.metrics or {}).get("mdd_pct")
    lines.append(
        f"최악 구간: 인샘플 MDD {_fmt(None if mdd is None else float(mdd), '.2f')}%"
    )
    scored = [f for f in folds if f.get("net_bps") is not None]
    if scored:
        worst = min(scored, key=lambda f: f["net_bps"])
        lines.append(
            f"           최악 fold: {worst.get('end')} 종료 창 — 순bp {worst['net_bps']:+.1f}"
            f" (왕복 {worst.get('n_round_trips', 0)}건, MDD {float(worst.get('mdd_pct') or 0):.2f}%)"
        )
    else:
        lines.append(f"           최악 fold: {_UNKNOWN} — fold 없음")
    return lines


def report_text(
    result: BacktestResult,
    fit: Fitness,
    folds: list[dict],
    stability: dict,
    *,
    strategy: str,
    source: str,
    interval: str,
    window_days: int,
    step_days: int,
    n_trials: int,
    cost_bp: float,
    cost_label: str,
) -> str:
    """quant-expert §4 형식 그대로의 사람용 리포트.

    `n_trials`는 **이 전략을 채택하기까지 시험한 변형의 수**다. 호출부(CLI)가
    사람에게 받는다 — 코드가 알 방법이 없고, 모르는 것을 1로 가정하면 이
    리포트에서 가장 중요한 보정이 조용히 꺼진다.

    `cost_bp`/`cost_label`은 `quant.control.cost_model.effective_round_trip_bp()`의
    반환값을 그대로 받는다(실측인지 기본값인지가 라벨에 실려 있다).
    """
    trade_stat = trade_sharpe(_round_trip_pnl(result.trades))
    is_sharpe = float((result.metrics or {}).get("sharpe") or 0.0)

    lines = [f"🧪 전략 성적표 — {strategy} ({interval})", ""]
    lines.append(_period_line(result, source))
    lines.append(_sample_line(result, fit))
    lines += _oos_block(folds, stability, window_days, step_days)
    lines += _overfit_block(is_sharpe, folds, trade_stat, n_trials)
    lines.append(
        f"비용 가정: 왕복 {cost_bp:.1f}bp — {cost_label}"
    )
    lines.append(
        f"           이번 백테스트가 실제 부담한 비용 {fit.cost_bps:.1f}bp · "
        f"총bp {fit.gross_bps:+.1f} → 순bp {fit.net_bps:+.1f} "
        f"(엣지가 비용을 덮는가: {'예' if fit.edge_covers_cost else '아니오'})"
    )
    lines += _worst_block(result, folds)

    bench = (result.benchmark or {}).get("buy_hold") or {}
    if bench:
        lines.append(
            f"벤치마크:  단순보유 총수익 {float(bench.get('total_return_pct', 0)):+.2f}% · "
            f"MDD {float(bench.get('mdd_pct', 0)):.2f}% · 샤프 {float(bench.get('sharpe', 0)):+.2f}"
        )
    if fit.strategy_errors:
        lines.append(
            f"⚠️ 전략 예외로 스킵된 사이클 {fit.strategy_errors}회 — "
            "거래 0건이 조건 미충족인지 침묵 실패인지 확인해야 한다"
        )
    return "\n".join(lines)
