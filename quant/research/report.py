"""walk-forward 결과 렌더링 — 평문 요약(항상 동작) + quantstats HTML(선택,
설치된 경우에만). 평문 요약은 quantstats 없이도 always 동작해야 한다.

경로 통계(quant/research/pathstats.py) 렌더러도 여기 있다 — 끝점 지표가
숨기는 "겪어야 하는 과정"을 낙폭 -> 분포 -> 추세 -> 부트스트랩 -> 일관성 순으로
출력한다. 낙폭이 맨 위인 이유는 스킬 문서(§4)의 규칙이다: 소유자가 실제로 견뎌야
하는 것은 수익률이 아니라 낙폭이다.

신호 품질(quant/research/signalquality.py) 렌더러도 여기 있다 — 이쪽은
성과가 아니라 **진입 신호 자체**를 본다. 순서는 빈도·군집 -> 진입 품질(MAE/MFE) ->
엣지 감쇠 -> 정밀도 -> 강건성. 빈도가 맨 위인 이유: 거래할 수 없는 빈도라면 나머지
숫자가 아무리 좋아도 쓸 수 없기 때문이다.
"""
from __future__ import annotations

import logging

import pandas as pd

from quant.research.pathstats import PathStats, compute_path_stats
from quant.research.signalquality import (
    MIN_ENTRIES,
    MIN_YEAR_ENTRIES,
    SignalQuality,
    compute_signal_quality,
)
from quant.research.walkforward import WalkForwardResult

logger = logging.getLogger(__name__)

INSUFFICIENT = "표본 부족"


def render_text(result: WalkForwardResult, strategy_id: str = "") -> str:
    lines: list[str] = []
    lines.append(f"=== Walk-Forward 결과{f': {strategy_id}' if strategy_id else ''} ===")
    lines.append(f"windows: {len(result.window_results)}")
    lines.append("")

    check = result.sample_check
    if check is not None and not check.actionable:
        lines.append("!" * 60)
        lines.append("!! 표본 부족 — 아래 숫자는 NOT ACTIONABLE (권장 파라미터로 쓰지 말 것) !!")
        for reason in check.reasons:
            lines.append(f"!!   - {reason}")
        lines.append("!" * 60)
        lines.append("")

    lines.append("--- 윈도우별 train(in-sample) vs test(out-of-sample) ---")
    lines.append(
        f"{'window':>6} {'train_end':>12} {'test_end':>12} "
        f"{'train_sharpe':>13} {'test_sharpe':>12} {'gap':>7}  params"
    )
    for i, wr in enumerate(result.window_results):
        train_sh = float(wr.train_metrics.get("sharpe", 0.0))
        test_sh = float(wr.test_metrics.get("sharpe", 0.0))
        gap = train_sh - test_sh
        lines.append(
            f"{i:>6} {wr.window.train_end.date()!s:>12} {wr.window.test_end.date()!s:>12} "
            f"{train_sh:>13.2f} {test_sh:>12.2f} {gap:>7.2f}  {wr.best_params}"
        )
    lines.append("")

    lines.append("--- OOS(out-of-sample) 종합 (윈도우 test 구간 곱연결) ---")
    for key, value in result.oos_metrics.items():
        lines.append(f"{key:>18}: {value}")
    total_trades = sum(int(wr.test_metrics.get("n_trades", 0)) for wr in result.window_results)
    lines.append(f"{'total_test_trades':>18}: {total_trades}")
    lines.append("")

    lines.append("--- 파라미터 안정성 (윈도우 간 요동 — 크면 과최적화 신호) ---")
    for name, stats in result.param_stability.items():
        if "cv" in stats:
            flag = " <- 불안정" if stats["cv"] > 0.5 else ""
            lines.append(
                f"{name}: mean={stats['mean']:.4g} stdev={stats['stdev']:.4g} "
                f"cv={stats['cv']:.2f}{flag} values={stats['values']}"
            )
        else:
            flag = " <- 불안정" if stats["flip_rate"] > 0.5 else ""
            lines.append(f"{name}: flip_rate={stats['flip_rate']:.2f}{flag} values={stats['values']}")

    return "\n".join(lines)


def render_html(result: WalkForwardResult, output_path: str, strategy_id: str = "") -> bool:
    """quantstats로 OOS equity curve HTML tearsheet를 만든다. quantstats 미설치
    시 아무것도 쓰지 않고 False를 반환한다(호출부는 평문 요약만으로 계속 진행) —
    연구 스택이 선택 의존성이라는 계약(pyproject.toml)을 여기서도 지킨다."""
    try:
        import quantstats as qs
    except ImportError:
        logger.warning(
            "quantstats 미설치 — HTML 리포트 생략(평문 요약만 출력됨). "
            "`uv sync --group research`로 설치 가능."
        )
        return False

    if result.oos_equity.empty:
        logger.warning("OOS equity curve가 비어 있어 HTML 리포트를 생략함.")
        return False

    returns = result.oos_equity.pct_change().dropna()
    qs.reports.html(returns, output=output_path, title=f"Walk-Forward: {strategy_id}")
    return True


def _ts(value) -> str:
    """일봉이면 날짜만, 장중 봉이면 분까지 — 15분봉 자산곡선에서 낙폭 에피소드 세
    건이 전부 같은 날짜로 찍혀 구분이 안 되는 일을 막는다."""
    if value is None:
        return "-"
    ts = pd.Timestamp(value)
    return str(ts.date()) if (ts.hour, ts.minute, ts.second) == (0, 0, 0) else ts.strftime("%Y-%m-%d %H:%M")


def render_path_stats(stats: PathStats, title: str = "") -> str:
    """경로 통계를 평문 한 블록으로 렌더링한다.

    순서가 곧 주장이다: 낙폭 -> 롤링 분포 -> 추세 -> 부트스트랩 -> 일관성.
    수익률은 마지막에 온다 — 소유자가 먼저 봐야 하는 것은 견뎌야 할 하방이다.
    표본이 모자란 줄은 숫자 대신 "표본 부족"을 찍는다.
    """
    L: list[str] = []
    L.append(f"=== 경로 통계{f': {title}' if title else ''} ===")
    L.append(
        f"표본: {_ts(stats.start)} ~ {_ts(stats.end)} ({stats.span_years:.2f}년), "
        f"봉 {stats.n_bars}개, 라운드트립(매도) {stats.n_trades}건"
    )
    L.append("끝점 지표(총수익·CAGR)가 숨기는 '과정'을 보는 표다. 집계 점수는 없다 — 구성요소를 직접 본다.")
    L.append("")

    dd = stats.drawdown
    L.append("--- 1. 낙폭 · 하방 (먼저 보는 이유: 실제로 견뎌야 하는 것) ---")
    L.append(f"{'최대낙폭':<16} {dd.max_depth_pct:>8.2f}%   (표본 {dd.n_bars}봉)")
    L.append(f"{'Ulcer Index':<16} {dd.ulcer_index:>8.2f}    (깊이+길이 동시 반영; MDD는 최저점 하나만 본다)")
    L.append(f"{'수중 시간 비율':<15} {dd.time_under_water_frac:>8.1%}   (직전 고점 아래에 있던 봉의 비율)")
    L.append(
        f"{'최장 수중 구간':<15} {dd.longest_underwater_bars:>8}봉  "
        f"({dd.longest_underwater_days:.1f}일)"
    )
    L.append(
        f"{'낙폭 에피소드':<15} {dd.n_episodes:>8}건"
        + ("  <- 미회복 구간 있음" if dd.unrecovered else "")
    )
    if dd.episodes:
        L.append("  깊은 순 상위 5건 (미회복이면 회복=진행중):")
        L.append(
            f"  {'고점':>16} {'최저점':>16} {'회복':>16} {'깊이':>9} {'수중봉':>8} {'회복봉':>8}"
        )
        for ep in sorted(dd.episodes, key=lambda e: e.depth_pct)[:5]:
            recovery = _ts(ep.recovery) if ep.recovered else "진행중"
            rec_bars = f"{ep.recovery_bars}" if ep.recovery_bars is not None else "-"
            L.append(
                f"  {_ts(ep.start):>16} {_ts(ep.trough):>16} {recovery:>16} "
                f"{ep.depth_pct:>8.2f}% {ep.underwater_bars:>8} {rec_bars:>8}"
            )
    L.append("")

    L.append("--- 2. 롤링 창 수익 분포 ('아무 때나 들어가 N개월 들고 있었으면?') ---")
    L.append(
        f"  {'구간':>4} {'창길이':>8} {'창수':>8} {'p5':>9} {'p25':>9} {'중앙':>9} "
        f"{'p75':>9} {'p95':>9} {'양수비율':>8}"
    )
    for row in stats.rolling:
        if not row.sufficient:
            L.append(
                f"  {row.label:>4} {row.window_bars:>7}봉 {row.count:>8} "
                f"{INSUFFICIENT:>50}  (창 {row.count}개 < 최소 2개)"
            )
            continue
        L.append(
            f"  {row.label:>4} {row.window_bars:>7}봉 {row.count:>8} "
            f"{row.p5_pct:>8.2f}% {row.p25_pct:>8.2f}% {row.median_pct:>8.2f}% "
            f"{row.p75_pct:>8.2f}% {row.p95_pct:>8.2f}% {row.frac_positive:>8.1%}"
        )
    L.append("  주의: 겹치는 창이라 서로 독립이 아니다(자기상관). 기술통계이며 신뢰구간이 아니다.")
    L.append("")

    tr = stats.trend
    L.append("--- 3. 로그자산 추세 (끝점 2개가 아니라 전 구간 회귀) ---")
    if not tr.sufficient:
        L.append(f"  {INSUFFICIENT} (관측 {tr.n_obs}점 < 최소 3점)")
    else:
        L.append(f"  {'관측':<14} {tr.n_obs}점 / {tr.span_years:.2f}년")
        L.append(
            f"  {'추세 연성장률':<13} {tr.growth_pct_per_year:>8.2f}%/년  "
            f"[95% {tr.growth_pct_lo:.2f}% ~ {tr.growth_pct_hi:.2f}%]"
        )
        flag = "  <- 낮다: 추세가 아니라 몇 번의 점프에 의존" if tr.r_squared < 0.5 else ""
        L.append(f"  {'R^2':<14} {tr.r_squared:>8.4f}{flag}")
        L.append(
            f"  {'기울기 표준오차':<12} {tr.stderr_log_per_year:>8.2e} log/년   t={tr.t_stat:.2f}"
            + ("  <- |t|<2: 0과 구분 불가" if abs(tr.t_stat) < 2 else "")
        )
        L.append("  주의: OLS 표준오차는 잔차 i.i.d. 가정 — 자기상관 때문에 실제보다 작다(낙관적 하한).")
    L.append("")

    bs = stats.bootstrap
    L.append("--- 4. 거래손익 부트스트랩 (실제 경로가 운이었나) ---")
    if not bs.sufficient:
        L.append(f"  {INSUFFICIENT} (거래 {bs.n_trades}건 < 최소 30건)")
    else:
        L.append(f"  {'설정':<12} 원본 거래 {bs.n_trades}건, 경로 {bs.n_paths}개, seed={bs.seed}")
        L.append(
            f"  {'최종수익률':<11} p5 {bs.final_return_pct_p5:.2f}% | p25 {bs.final_return_pct_p25:.2f}% | "
            f"중앙 {bs.final_return_pct_median:.2f}% | p75 {bs.final_return_pct_p75:.2f}% | "
            f"p95 {bs.final_return_pct_p95:.2f}%"
        )
        L.append(
            f"  {'최대낙폭':<12} p5 {bs.mdd_pct_p5:.2f}% | 중앙 {bs.mdd_pct_median:.2f}% | "
            f"p95 {bs.mdd_pct_p95:.2f}%"
        )
        L.append(f"  {'음수 종료 비율':<10} {bs.frac_negative:>8.1%}")
        L.append(
            f"  {'관측 경로':<12} 최종 {bs.observed_final_return_pct:.2f}% "
            f"(백분위 {bs.observed_final_pct_rank:.0f} — 재추출은 평균을 보존하므로 구조적으로 50 근처), "
            f"MDD {bs.observed_mdd_pct:.2f}% (백분위 {bs.observed_mdd_pct_rank:.0f})"
        )
        L.append("  MDD 백분위가 높을수록 실제 경로가 분포보다 얕게 지나간 것 = 그만큼 운이 좋았다는 뜻.")
        L.append("  가정: 거래손익 i.i.d. — 계열 의존성·레짐 군집을 무시한다. 예측 분포가 아니라 반사실이다.")
        L.append("  주의: 매도 행 pnl 기준이라 진입 수수료가 빠져 있다(자산곡선 변화와 정확히 일치하지 않음).")
    L.append("")

    cs = stats.consistency
    L.append("--- 5. 월 · 연 일관성 ---")
    if not cs.months_sufficient:
        L.append(f"  월별: {INSUFFICIENT} ({cs.n_months}개월 < 최소 2개월)")
    else:
        L.append(
            f"  월별: {cs.n_months}개월, 양수 {cs.frac_positive_months:.1%}, "
            f"최고 {cs.best_month.label} {cs.best_month.return_pct:+.2f}%, "
            f"최악 {cs.worst_month.label} {cs.worst_month.return_pct:+.2f}%, "
            f"최장 연속손실 {cs.longest_losing_streak_months}개월"
        )
    if not cs.years_sufficient:
        L.append(f"  연도: {INSUFFICIENT} ({cs.n_years}년 < 최소 2년)")
    else:
        L.append(
            f"  연도: {cs.n_years}년, 양수 {cs.frac_positive_years:.1%}, "
            f"최고 {cs.best_year.label} {cs.best_year.return_pct:+.2f}%, "
            f"최악 {cs.worst_year.label} {cs.worst_year.return_pct:+.2f}%"
        )
        L.append("  " + " | ".join(f"{y.label} {y.return_pct:+.1f}%" for y in cs.yearly))

    return "\n".join(L)


def render_path_report(
    equity: pd.Series,
    trades: pd.DataFrame | None = None,
    title: str = "",
    seed: int = 42,
) -> str:
    """계산 + 렌더링 한 번에 — 호출부(노트북/CLI)에서 가장 흔한 사용 형태."""
    return render_path_stats(compute_path_stats(equity, trades, seed=seed), title=title)


def _bars_label(bars: int, interval_minutes: int) -> str:
    """봉 수를 사람이 읽는 시간으로도 같이 찍는다 — 26봉이 몇 시간인지 매번
    암산하게 만들면 읽는 사람이 그냥 안 읽는다."""
    minutes = bars * interval_minutes
    if minutes < 60:
        return f"{bars}봉({minutes}분)"
    hours = minutes / 60
    return f"{bars}봉({hours:.1f}h)" if hours < 6.5 else f"{bars}봉({minutes / 390:.1f}세션)"


def render_signal_quality(stats: SignalQuality, title: str = "") -> str:
    """신호 품질을 평문 한 블록으로 렌더링한다.

    순서가 곧 주장이다: 빈도·군집 -> 진입 품질(MAE/MFE) -> 엣지 감쇠 -> 정밀도 ->
    강건성. 빈도를 먼저 보는 이유는 거래 불가능한 빈도면 나머지가 무의미하기
    때문이고, MAE/MFE가 그 다음인 이유는 **역행폭이 작다는 것이 "확신 있는
    진입"의 정의**이기 때문이다.

    모든 블록은 표본 수 n을 함께 찍는다. 모자라면 숫자 대신 "표본 부족(n=...)".
    여기 숫자는 전부 **신호에 대한 것이지 거래 가능한 손익이 아니다** — 청산
    규칙·수수료·슬리피지·사이징이 하나도 들어가 있지 않다.
    """
    im = stats.interval_minutes
    L: list[str] = []
    L.append(f"=== 신호 품질{f': {title}' if title else ''} ===")
    L.append(
        f"표본: {_ts(stats.first)} ~ {_ts(stats.last)}, 진입신호 {stats.n_entries}건, "
        f"종목 {', '.join(stats.symbols) if stats.symbols else '-'}, {im}분봉"
    )
    L.append(
        "청산 규칙과 무관한 '진입 신호 자체'의 품질이다. "
        "**손익이 아니다** — 수수료·슬리피지·사이징·청산이 들어가 있지 않다. 집계 점수는 없다."
    )
    if stats.n_dropped_no_bars:
        L.append(f"  주의: 봉 데이터가 없거나 기준가가 0인 신호 {stats.n_dropped_no_bars}건은 측정에서 제외됨.")
    L.append("")

    fq = stats.frequency
    L.append("--- 1. 빈도 · 군집 (먼저 보는 이유: 거래 불가능한 빈도면 나머지가 무의미) ---")
    if fq.n_entries == 0:
        L.append(f"  {INSUFFICIENT} (신호 0건)")
    else:
        L.append(
            f"  {'신호 수':<12} {fq.n_entries:>6}건 / {fq.span_days:.0f}일 "
            f"= 월 {fq.signals_per_month:.2f}건"
            + ("" if fq.sufficient else f"   <- {INSUFFICIENT}(분포 판단 최소 30건)")
        )
        L.append("  " + f"{'종목별':<12} " + ", ".join(f"{s} {c}건" for s, c in sorted(fq.per_symbol.items())))
        if not fq.gaps_sufficient:
            L.append(f"  {'신호 간격':<11} {INSUFFICIENT} (간격 {fq.n_gaps}개 < 최소 10개)")
        else:
            L.append(
                f"  {'신호 간격(일)':<10} 중앙 {fq.gap_days_median:.2f} | p25 {fq.gap_days_p25:.2f} | "
                f"p75 {fq.gap_days_p75:.2f} | 최대 {fq.gap_days_max:.1f} (간격 {fq.n_gaps}개)"
            )
            burst_flag = "  <- 군집적" if fq.burstiness > 0.3 else ("  <- 규칙적" if fq.burstiness < -0.3 else "")
            L.append(
                f"  {'군집성':<12} burstiness {fq.burstiness:+.3f} (0=포아송, +1=군집), "
                f"간격 CV {fq.gap_cv:.2f}{burst_flag}"
            )
        L.append(
            f"  {'최대 밀집':<11} 임의의 7일 창에 최대 {fq.max_in_7d}건 | "
            f"최다월 {fq.busiest_month} {fq.busiest_month_count}건 (전체의 {fq.frac_in_busiest_month:.1%})"
        )
        if fq.per_year:
            L.append(f"  연도별 (한 해 {MIN_YEAR_ENTRIES}건 미만은 빈도 해석 근거 부족):")
            for yc in fq.per_year:
                mark = "" if yc.sufficient else f"  <- {INSUFFICIENT}"
                L.append(f"    {yc.year} {yc.count:>4}건{mark}")
    L.append("")

    L.append("--- 2. 진입 품질 MAE/MFE (역행폭이 작다 = 확신 있는 진입) ---")
    L.append(
        f"  {'선도구간':>12} {'n':>6} {'MAE중앙':>9} {'MAE p75':>9} {'MAE p90':>9} "
        f"{'MFE중앙':>9} {'MFE p75':>9} {'MFE/MAE':>8}"
    )
    for ex in stats.excursions:
        label = _bars_label(ex.horizon_bars, im)
        if not ex.sufficient:
            L.append(f"  {label:>12} {ex.n:>6}  {INSUFFICIENT} (최소 {MIN_ENTRIES}건, 구간부족 제외 {ex.n_truncated}건)")
            continue
        ratio = "inf" if ex.mfe_mae_ratio == float("inf") else f"{ex.mfe_mae_ratio:.2f}"
        L.append(
            f"  {label:>12} {ex.n:>6} {ex.mae_median_pct:>8.2f}% {ex.mae_p75_pct:>8.2f}% "
            f"{ex.mae_p90_pct:>8.2f}% {ex.mfe_median_pct:>8.2f}% {ex.mfe_p75_pct:>8.2f}% {ratio:>8}"
        )
    L.append("  MAE/MFE는 크기(양수%)다. 매수한 종목 기준 — SQQQ 롱은 SQQQ 가격으로 잰다.")
    L.append("  MFE/MAE는 중앙값끼리의 비(개별 비율 평균이 아님). 겹치는 진입이라 사분위는 기술통계다.")
    L.append("")

    L.append("--- 3. 엣지 감쇠 (진짜 엣지는 일찍 나타나 유지된다; 잡음은 평평하다) ---")
    shown = [d for d in stats.decay if d.bars in _decay_ticks(stats.decay)]
    if not any(d.sufficient for d in stats.decay):
        n_max = max((d.n for d in stats.decay), default=0)
        L.append(f"  {INSUFFICIENT} (최대 {n_max}건 < 최소 {MIN_ENTRIES}건)")
    else:
        L.append(f"  {'선도':>12} {'n':>6} {'평균수익':>9} {'중앙':>9} {'표준오차':>9} {'t':>7}")
        for d in shown:
            label = _bars_label(d.bars, im)
            if not d.sufficient:
                L.append(f"  {label:>12} {d.n:>6}  {INSUFFICIENT}")
                continue
            flag = "" if abs(d.t_stat) >= 2 else "  <- 0과 구분 불가"
            L.append(
                f"  {label:>12} {d.n:>6} {d.mean_pct:>8.3f}% {d.median_pct:>8.3f}% "
                f"{d.stderr_pct:>8.3f}% {d.t_stat:>7.2f}{flag}"
            )
    L.append("  주의: 겹치는 진입은 독립 관측이 아니다 — 표준오차는 하한, t는 과대평가다.")
    L.append("")

    pr = stats.precision
    L.append("--- 4. 신호 정밀도 (청산 규칙 없는 '먼저 우리 쪽으로 갔나') ---")
    L.append(
        f"  설정: 임계치 ±{pr.threshold_pct:g}%, 선도구간 {_bars_label(pr.horizon_bars, im)}, "
        f"신호 {pr.n_entries}건"
    )
    if not pr.sufficient:
        L.append(f"  {INSUFFICIENT} (판정된 신호 {pr.n_resolved}건 < 최소 {MIN_ENTRIES}건)")
    else:
        L.append(
            f"  {'정밀도':<12} {pr.precision:>8.1%}  "
            f"(순행선착 {pr.n_favorable_first} / 판정 {pr.n_resolved})"
        )
    L.append(
        f"  {'미판정':<12} 임계치 미도달 {pr.n_unresolved}건, "
        f"같은 봉에서 양쪽 도달 {pr.n_ambiguous}건(봉 내부 순서는 OHLC로 알 수 없어 분모 제외)"
    )
    if pr.n_entries and pr.n_ambiguous / pr.n_entries > 0.2:
        L.append("  <- 모호 비율 20% 초과: 임계치가 봉 변동폭 대비 너무 좁다. 정밀도를 신뢰하지 말 것.")
    L.append("")

    rb = stats.robustness
    L.append("--- 5. 잡음 강건성 (파라미터 ±10%에 신호 집합이 살아남는가) ---")
    if rb is None:
        L.append("  미측정 (noise_robustness를 실행해 넘기지 않았음 — 전략 재실행이 필요하다)")
    else:
        L.append(
            f"  설정: baseline 신호 {rb.baseline_n}건, ±{rb.pct:g}%, draw {rb.n_draws}회/파라미터, "
            f"seed={rb.seed}"
        )
        if not rb.sufficient:
            L.append(f"  주의: baseline {rb.baseline_n}건 < {MIN_ENTRIES}건 — Jaccard가 소수 신호에 좌우된다.")
        L.append(f"  {'파라미터':>18} {'기준값':>10} {'평균 Jaccard':>13} {'최소':>8}  draw별(값→겹침)")
        for p in rb.params:
            if not p.perturbed:
                L.append(f"  {p.name:>18} {str(p.base_value):>10} {'건너뜀':>13} {'-':>8}  {p.skip_reason}")
                continue
            flag = "  <- 취약" if p.mean_jaccard < 0.7 else ""
            detail = ", ".join(
                f"{d.value:g}→{d.jaccard:.2f}({d.n_signals})" if isinstance(d.value, (int, float))
                else f"{d.value}→{d.jaccard:.2f}({d.n_signals})"
                for d in p.draws
            )
            L.append(
                f"  {p.name:>18} {p.base_value:>10} {p.mean_jaccard:>13.3f} "
                f"{p.min_jaccard:>8.3f}  {detail}{flag}"
            )
        L.append("  Jaccard는 (종목,시각) 집합 기준 — 개수가 같아도 시점이 다르면 0이다.")
        L.append("  낮다 = 그 파라미터 근방에서 신호가 데이터 잡음에 적합돼 있다는 뜻.")

    return "\n".join(L)


def _decay_ticks(decay: list) -> set[int]:
    """감쇠 곡선을 전부 찍으면 26줄이 된다 — 앞쪽은 촘촘히, 뒤는 듬성듬성.
    앞 4봉은 전부, 그 뒤는 4의 배수, 마지막 봉은 항상 포함."""
    if not decay:
        return set()
    last = decay[-1].bars
    return {d.bars for d in decay if d.bars <= 4 or d.bars % 4 == 0 or d.bars == last}


def render_signal_quality_report(
    entries,
    bars,
    interval_minutes: int = 15,
    title: str = "",
    robustness=None,
) -> str:
    """계산 + 렌더링 한 번에 — 호출부(노트북/CLI)에서 가장 흔한 사용 형태."""
    return render_signal_quality(
        compute_signal_quality(
            entries, bars, interval_minutes=interval_minutes, robustness=robustness,
        ),
        title=title,
    )
