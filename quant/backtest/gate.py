"""배포 게이트 — walk-forward + 트레이드 분석을 **go/no-go/판단 불가** 한 마디로
좁힌다 (2026-09-03).

## 왜 여기가 마지막 문일 필요가 있나

`walkforward.stability_summary`는 힌트 문자열을 낸다("불안정해 보인다" 류).
`strategy_report.report_text`는 사람이 읽는 한 장짜리 표를 낸다. 둘 다 **판정하지
않는다** — 이 저장소의 원칙(`walkforward.py`/`ledger.py`와 같은 계약)은 "숫자를
내고 판단은 사람이 한다"이지만, 그 판단에 쓰는 기준선 자체가 매번 사람 기억에
의존하면 이번엔 이 기준을 봤는데 저번엔 저 기준을 봤다는 일이 생긴다. 여기서는
**기준선을 코드로 고정**하고 그 기준선 대비 pass/fail만 낸다 — "GO/NO_GO"는 여전히
소유자가 최종 결정할 문제고, 이 함수는 "그 결정에 필요한 체크리스트를 빠짐없이
계산해서 보여주는 것"까지만 한다.

## 판단 불가 vs NO_GO

**판단 불가**는 "계산할 데이터 자체가 없다"(fold 0개, OOS 트레이드 0건) — 데이터가
있는데 기준 미달인 것과는 다르다. 표본이 있는데 기준(예: OOS 100건)에 못 미치면
그건 **NO_GO**다(그 자체가 실제로 채택하지 말아야 할 이유이지, "몰라서 답을 못
하는 것"이 아니다). 이 구분이 없으면 표본 부족을 "아직 판단 안 함"으로 착각해
계속 진행하게 된다.
"""
from __future__ import annotations

import statistics as _stats
from dataclasses import asdict, dataclass

from quant.backtest.statistics import deflated_sharpe

__all__ = ["GateThresholds", "evaluate_gate", "render_gate"]

_UNKNOWN = "판단 불가"

# fitness.MIN_ROUND_TRIPS와 같은 값 — "이 아래로는 채점표 자체를 계산할 수 없다"는
# 표본선. GateThresholds.min_oos_trades(100, "GO를 주려면 이만큼 필요")와는 다른
# 축이다 — 이건 "판단 불가 문턱", 저건 "합격선"이다.
_MIN_JUDGEABLE_OOS_TRADES = 30

_Z_95 = 1.959963984540054


@dataclass(frozen=True)
class GateThresholds:
    """go/no-go 판정 기준선. 전부 소유자가 조정 가능하지만 기본값은 quant-expert
    §2/§6·`fitness.MIN_ROUND_TRIPS`·`ledger.MIN_TRIPS_FOR_JUDGEMENT`와 정합적으로
    잡았다 — 이 파일 밖의 기존 기준선과 숫자가 달라지면 "왜 저기는 30인데 여기는
    50이냐"는 질문에 답할 수 없다."""

    min_oos_trades: int = 100
    min_expectancy_ci_lower_bp: float = -5.0
    min_deflated_sharpe: float = 0.95
    min_fold_positive_share: float = 0.60
    max_worst_fold_mdd_pct: float = 15.0  # 절대값 비교(부호 무시)
    min_worst_fold_expectancy_bp: float = -30.0
    min_plateau_ratio: float = 0.50  # 이웃 파라미터 median >= center * 이 비율


def _fold_net_bps(folds: list[dict]) -> list[float]:
    return [float(f["net_bps"]) for f in folds if f.get("net_bps") is not None]


def _oos_expectancy_ci(net_bps: list[float]) -> tuple[float | None, float | None, float | None]:
    """OOS 기대값(bp)과 95% CI — **fold를 관측 단위로 삼은 정규근사**다.

    walk-forward는 fold별 집계(net_bps 등)만 밖으로 내보내고 fold 안의 개별
    트레이드는 노출하지 않는다(walkforward.py가 의도적으로 그렇게 설계됨 —
    `run_backtest`이 반환하는 BacktestResult 전체를 fold마다 들고 있는 건 메모리
    낭비이자 이 모듈의 책임 밖이다). 그래서 트레이드 단위 CI 대신 fold 단위
    표본(전형적으로 4~8개)으로 정규근사 CI를 낸다 — fold 수가 적어 이 CI는
    넓다. 표본이 적다는 사실 자체가 "판단 불가"로 이어지도록 fold < 2면
    CI를 못 낸다(표준편차 정의 불가)."""
    if not net_bps:
        return None, None, None
    mean_bp = _stats.mean(net_bps)
    if len(net_bps) < 2:
        return mean_bp, None, None
    se = _stats.stdev(net_bps) / (len(net_bps) ** 0.5)
    return mean_bp, mean_bp - _Z_95 * se, mean_bp + _Z_95 * se


def evaluate_gate(
    walkforward_result: list[dict],
    analytics: dict,
    *,
    trials: int,
    cost_bp: float,
    market: str,
    thresholds: GateThresholds | None = None,
    param_plateau: dict[str, float] | None = None,
) -> dict:
    """walk-forward fold 목록 + `analytics.analyze_trades()` 출력 → go/no-go 판정.

    `walkforward_result`: `quant.backtest.walkforward.run_walkforward()`가 내는
    fold dict 목록(`net_bps`/`n_round_trips`/`mdd_pct`/... — `fitness.Fitness.to_dict()`
    키 그대로).

    `analytics`: 같은 전략의 (통상 인샘플 전체 구간) `analyze_trades()` 출력.
    deflated Sharpe·비용 2배 생존 여부는 여기서 가져온다 — walk-forward fold는
    트레이드 단위 분포를 안 주므로 이 두 기준은 analytics 쪽 표본으로 판정한다.
    **analytics와 walkforward_result가 같은 전략·같은 기간 근방이 아니면 이 판정은
    의미가 없다** — 호출부(CLI)가 같은 `--strategy`로 둘 다 돌려야 한다.

    `param_plateau`: 선택. `{"파라미터라벨": OOS 기대값bp, ...}` — 이웃 파라미터의
    OOS 성과. 중앙값이 이 전략의 OOS 기대값(위에서 계산한 `mean_bp`, "center")의
    `min_plateau_ratio`(기본 50%) 이상이면 통과. 안 주면 이 기준은 채점에서
    빠지고("평가 안 함") 다른 기준만으로 판정한다.

    반환: `{"verdict": "GO"|"NO_GO"|"판단 불가", "criteria": {...}, "rationale": str,
    "thresholds": {...}}`."""
    th = thresholds or GateThresholds()
    folds = walkforward_result or []
    net_bps = _fold_net_bps(folds)
    oos_trades = sum(int(f.get("n_round_trips") or 0) for f in folds)

    if not folds or oos_trades < _MIN_JUDGEABLE_OOS_TRADES:
        return {
            "verdict": _UNKNOWN,
            "criteria": {},
            "rationale": (
                f"{_UNKNOWN} — walk-forward fold {len(folds)}개, OOS 라운드트립 "
                f"{oos_trades}건(최소 {_MIN_JUDGEABLE_OOS_TRADES}건 미만) — 판정 자체를 "
                "계산할 표본이 없다."
            ),
            "thresholds": asdict(th),
        }

    mean_bp, ci_lower, ci_upper = _oos_expectancy_ci(net_bps)

    criteria: dict[str, dict] = {}

    criteria["oos_n_trades"] = {
        "pass": oos_trades >= th.min_oos_trades,
        "value": oos_trades, "threshold": th.min_oos_trades,
        "label": "OOS 라운드트립 수",
    }

    ci_pass = mean_bp is not None and mean_bp > 0 and ci_lower is not None and ci_lower > th.min_expectancy_ci_lower_bp
    criteria["oos_expectancy"] = {
        "pass": ci_pass,
        "value": None if mean_bp is None else round(mean_bp, 3),
        "ci_lower": None if ci_lower is None else round(ci_lower, 3),
        "ci_upper": None if ci_upper is None else round(ci_upper, 3),
        "threshold": f"> 0, CI하한 > {th.min_expectancy_ci_lower_bp}bp",
        "label": "OOS 기대값(bp, fold 단위 정규근사 CI)",
    }

    ts = (analytics or {}).get("trade_sharpe") or {}
    n_obs, sr = ts.get("n"), ts.get("sharpe")
    dsr = None
    if n_obs is not None and n_obs >= 2 and sr is not None:
        dsr = deflated_sharpe(
            sr, max(int(trials), 1), int(n_obs),
            ts.get("skew", 0.0), ts.get("kurtosis", 3.0),
        )
    criteria["deflated_sharpe"] = {
        "pass": dsr is not None and dsr >= th.min_deflated_sharpe,
        "value": None if dsr is None else round(dsr, 3),
        "threshold": th.min_deflated_sharpe,
        "label": f"deflated Sharpe (trials={trials})",
    }

    cs = (analytics or {}).get("cost_sensitivity") or {}
    exp_2x = cs.get("2x")
    criteria["survives_2x_cost"] = {
        "pass": exp_2x is not None and exp_2x >= 0,
        "value": exp_2x, "threshold": 0.0,
        "label": "2배 비용 시나리오 기대값(bp)",
    }

    mdds = [abs(float(f["mdd_pct"])) for f in folds if f.get("mdd_pct") is not None]
    worst_mdd = max(mdds) if mdds else None
    criteria["worst_fold_mdd"] = {
        "pass": worst_mdd is not None and worst_mdd <= th.max_worst_fold_mdd_pct,
        "value": worst_mdd, "threshold": th.max_worst_fold_mdd_pct,
        "label": "최악 fold MDD(%, 절대값)",
    }

    worst_exp = min(net_bps) if net_bps else None
    criteria["worst_fold_expectancy"] = {
        "pass": worst_exp is not None and worst_exp >= th.min_worst_fold_expectancy_bp,
        "value": worst_exp, "threshold": th.min_worst_fold_expectancy_bp,
        "label": "최악 fold 기대값(bp)",
    }

    n_positive = sum(1 for v in net_bps if v > 0)
    share = (n_positive / len(folds)) if folds else None
    criteria["fold_stability"] = {
        "pass": share is not None and share >= th.min_fold_positive_share,
        "value": None if share is None else round(share, 3),
        "threshold": th.min_fold_positive_share,
        "label": f"양(+) net_bps fold 비율 ({n_positive}/{len(folds)})",
    }

    if param_plateau:
        neighbor_vals = list(param_plateau.values())
        median_neighbor = _stats.median(neighbor_vals) if neighbor_vals else None
        plateau_pass = None
        if median_neighbor is not None and mean_bp is not None:
            plateau_pass = (
                median_neighbor >= th.min_plateau_ratio * mean_bp if mean_bp > 0
                else median_neighbor >= mean_bp
            )
        criteria["param_plateau"] = {
            "pass": plateau_pass,
            "value": None if median_neighbor is None else round(median_neighbor, 3),
            "threshold": f">= center × {th.min_plateau_ratio}",
            "label": "이웃 파라미터 median OOS 기대값(bp)",
        }

    required = [c for name, c in criteria.items() if name != "param_plateau"]
    all_required_pass = all(c["pass"] for c in required)
    plateau_c = criteria.get("param_plateau")
    plateau_ok = plateau_c is None or plateau_c["pass"] in (True, None)
    verdict = "GO" if all_required_pass and plateau_ok else "NO_GO"

    failed = [c["label"] for c in required if not c["pass"]]
    if plateau_c is not None and plateau_c["pass"] is False:
        failed.append(plateau_c["label"])

    if verdict == "GO":
        rationale = (
            f"OOS 라운드트립 {oos_trades}건, 기대값 {mean_bp:+.2f}bp(95% CI 하한 "
            f"{ci_lower:+.2f}bp), deflated Sharpe {dsr:.2f}(trials={trials})가 전부 "
            "기준선을 통과했고 2배 비용·최악 fold·fold 안정성도 버틴다 — 배포를 "
            "논의할 근거는 갖춰졌다(최종 결정은 소유자 몫)."
        )
    else:
        rationale = (
            f"다음 기준 미달로 NO_GO: {', '.join(failed)}. "
            "이 중 하나라도 통과 못 하면 실거래 손실로 직결될 수 있는 지표라 "
            "완화하지 않는다 — 기준을 낮추기보다 전략·파라미터를 바꿔야 한다."
        )

    return {
        "verdict": verdict, "criteria": criteria, "rationale": rationale,
        "thresholds": asdict(th),
    }


def render_gate(gate: dict) -> str:
    """`evaluate_gate()` 출력 → CLI/노트북용 텍스트."""
    lines = [f"🚦 배포 게이트 판정: {gate['verdict']}", ""]
    for name, c in gate.get("criteria", {}).items():
        mark = "✅" if c["pass"] is True else ("⚠️ 평가 안 함" if c["pass"] is None else "❌")
        lines.append(f"  [{mark}] {c['label']}: {c['value']} (기준: {c['threshold']})")
    lines.append("")
    lines.append(gate["rationale"])
    return "\n".join(lines)
