"""CLI 엔트리포인트: python -m quant.apps.cli {backtest|paper|report}"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime

from quant.core import log_redact as _redact
from quant.adapters.env import load_env as _load_dotenv_secrets
from quant.apps.config import load_settings
from quant.trade.loop import run_paper_loop
from quant.backtest import run_backtest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 시크릿 마스킹은 로거가 만들어진 직후 걸어둔다. `os.environ` 기반 값(TOKEN/SECRET/
# APP_KEY/API_KEY/PASSWORD 이름 힌트)은 `SecretRedactingFilter`가 매 로그마다 새로
# 읽으므로 `load_settings()`가 나중에 `.env`/`.env.local`을 `os.environ`에 채워도
# 자동으로 잡힌다. 하지만 `quant.adapters.env.get_key()`(dart.py 등 수집기가 쓰는
# 경로)는 `.env.local`을 직접 읽을 뿐 `os.environ`을 채우지 않는다(실측: DART_API_KEY
# 가 dart-fundamentals 로그의 httpx GET 쿼리스트링에 평문으로 남았었다) — 그 값은
# `os.environ` 스캔으로 잡히지 않으므로 여기서 `.env.local`을 직접 읽어 주입한다.
# `quant/core/`는 `quant/adapters/`를 임포트할 수 없어(4평면 규칙) 스스로는 못 한다.
_redact.install(extra_secrets=_redact.known_secrets(env=_load_dotenv_secrets()))


def cmd_fitness(args: argparse.Namespace) -> None:
    """적합도 함수 — 에이전트가 부르는 진입점. JSON 한 덩어리를 stdout 으로 낸다.

    `backtest` 와 나눈 이유: `backtest` 는 사람이 읽는 표를 내고, 이건 **기계가
    비교할 수 있는 지표 묶음**을 낸다. 하네스(Phase 8)가 변형끼리 비교할 때
    사람용 출력을 파싱하게 두면 형식이 바뀌는 순간 조용히 틀린다.
    """
    import json as _json

    from quant.backtest.fitness import ZeroCostBacktest, evaluate

    symbols = args.symbols.split() if args.symbols else None
    result = run_backtest(
        strategy_id=args.strategy, days=args.days, interval=args.interval,
        source=args.source, symbols=symbols,
    )
    try:
        fit = evaluate(result, require_costs=not args.allow_zero_cost)
    except ZeroCostBacktest as e:
        # 실패도 JSON 으로 낸다 — 호출자가 stderr 를 파싱하게 두지 않는다.
        print(_json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(2)

    out = {
        "ok": True,
        "strategy": args.strategy,
        "days": args.days,
        "interval": args.interval,
        "source": args.source,
        **fit.to_dict(),
    }

    # 실데이터로 돌렸다면 **커버리지를 같이 낸다.** 봉이 빠진 구간에서 나온 숫자는
    # 멀쩡해 보이지만 근거가 없고, 하네스는 그걸 그대로 믿는다. 2026-08-13 실측:
    # 마지막 봉이 08-01 인데 그날은 08-13 이었다 — 최근 12일이 조용히 비어 있었다.
    # None 은 "커버리지 정상"이 아니라 **"모른다"**다(DuckDB 미설치·파일 없음).
    if args.source == "history":
        from quant.adapters.olap import coverage

        cov = {}
        for sym in (symbols or [args.strategy]):
            c = coverage(sym, args.interval)
            cov[sym] = None if c is None else c.to_dict()
        out["coverage"] = cov

    print(_json.dumps(out, ensure_ascii=False))


def cmd_walkforward(args: argparse.Namespace) -> None:
    """롤링 OOS 안정성 하네스 — 같은 설정(settings.yaml)을 여러 시간 창에서 돌려
    성과가 구간마다 안정적인지 본다. **파라미터 자동 탐색은 하지 않는다**
    (`optimize` 서브커맨드와는 목적이 다르다 — 거버너 층 0, 사이징·파라미터
    자동화 금지)."""
    import json as _json

    from quant.backtest.walkforward import run_walkforward, stability_summary

    symbols = args.symbols.split() if args.symbols else None
    fill_model = getattr(args, "fill_model", "close")
    folds = run_walkforward(
        strategy_id=args.strategy, total_days=args.days, window_days=args.window,
        step_days=args.step, interval=args.interval, source=args.source, symbols=symbols,
        history_dir=getattr(args, "history_dir", None), fill_model=fill_model,
    )
    out = {
        "strategy": args.strategy, "source": args.source, "interval": args.interval,
        # 체결 모델은 fold 숫자와 반드시 붙어 다녀야 한다 — 모델이 다른 두 JSON을
        # 나란히 놓고 비교하면 그 비교 자체가 틀린다.
        "fill_model": fill_model,
        "folds": folds, "summary": stability_summary(folds),
    }
    print(_json.dumps(out, ensure_ascii=False, indent=2))


def cmd_strategy_report(args: argparse.Namespace) -> None:
    """전략 성적표 — quant-expert §4 형식을 **코드가 강제하는** 단일 진입점.

    `backtest`는 인샘플 표를, `walkforward`는 fold JSON을, `scoreboard`는 원장
    결과를 낸다 — 세 개를 사람이 머릿속에서 합쳐야 "이 전략을 써도 되나"에
    답할 수 있었다. 합치는 과정에서 빠지는 게 항상 같았다: **탐색 횟수**와
    **비용이 실측인지 기본값인지**.

    여기서는 셋을 한 번에 돌리고, 다중검정 보정(deflated Sharpe)과 실측 비용
    (`control.cost_model`)을 붙여 한 장으로 낸다. 표본이 모자란 항목은
    "판단 불가"로 찍힌다 — 빈칸을 그럴듯한 숫자로 채우지 않는다.

    `--trials`는 **사람이 신고한다.** 이 전략을 채택하기까지 시험한 변형의 수를
    코드가 알 방법이 없고, 모르는 것을 1로 가정하면 보정이 조용히 꺼진다.
    """
    from quant.backtest.fitness import evaluate
    from quant.backtest.strategy_report import report_text
    from quant.backtest.walkforward import run_walkforward, stability_summary
    from quant.control.cost_model import by_strategy, effective_round_trip_bp
    from quant.control.ledger import load_trades, round_trips
    from quant.control.tca import join_intents_fills, slippage_bps
    from quant.control.warehouse import read_jsonl

    symbols = args.symbols.split() if args.symbols else None
    history_dir = getattr(args, "history_dir", None)
    fill_model = getattr(args, "fill_model", "close")
    result = run_backtest(
        strategy_id=args.strategy, days=args.days, interval=args.interval,
        source=args.source, symbols=symbols, history_dir=history_dir,
        fill_model=fill_model,
    )
    fit = evaluate(result)
    folds = run_walkforward(
        strategy_id=args.strategy, total_days=args.total_days, window_days=args.window,
        step_days=args.step, interval=args.interval, source=args.source, symbols=symbols,
        history_dir=history_dir, fill_model=fill_model,
    )

    # 비용은 **원장 실측**이 우선이다. 이 전략의 트립이 모자라면 cost_model이
    # None을 내고 effective_round_trip_bp가 기본값으로 물러서며 그 사실을
    # 라벨에 싣는다(백테스트 자체의 비용 가정은 settings.yaml이 따로 쓴다).
    from quant.adapters.env import REPO_ROOT

    raw_trades = load_trades(ledger_state_path())
    trips = round_trips(raw_trades)
    intents = read_jsonl(REPO_ROOT / "data" / "state" / "order_intents.jsonl")
    slips = slippage_bps(join_intents_fills(intents, raw_trades))
    cost = by_strategy(trips, slips).get(args.strategy)
    cost_bp, cost_label = effective_round_trip_bp(cost)

    print(report_text(
        result, fit, folds, stability_summary(folds),
        strategy=args.strategy, source=args.source, interval=args.interval,
        window_days=args.window, step_days=args.step, n_trials=args.trials,
        cost_bp=cost_bp, cost_label=cost_label,
    ))


def _render_gate_analytics_text(analytics: dict) -> str:
    """analyze_trades() 출력 → CLI용 텍스트. strategy_report.report_text의 §4
    형식을 보완하는 자리라 그 톤(빈칸을 숫자로 채우지 않는다)을 그대로 따른다."""
    lines = ["📐 트레이드 다차원 분석", ""]
    if not analytics.get("judgeable"):
        lines.append(analytics.get("note", "판단 불가"))
        return "\n".join(lines)

    ci_lo, ci_hi = analytics["win_rate_ci"]
    lines.append(
        f"승률 {analytics['win_rate']:.1%} (95% CI {ci_lo:.1%}~{ci_hi:.1%}) · "
        f"payoff {analytics['payoff_ratio']} · profit factor {analytics['profit_factor']} · "
        f"기대값 {analytics['expectancy_bp']:+.2f}bp"
    )
    st = analytics["streaks"]
    ew = st["expected_max_win_streak"]
    el = st["expected_max_loss_streak"]
    lines.append(
        f"연승/연패 최대 {st['max_consecutive_wins']}/{st['max_consecutive_losses']} "
        f"(독립가정 기대치 {ew:.1f}/{el:.1f})"
    )
    cs = analytics["cost_sensitivity"]
    lines.append(f"비용 민감도(bp): 1x {cs['1x']:+.2f} · 1.5x {cs['1.5x']:+.2f} · 2x {cs['2x']:+.2f}")
    mc = analytics["monte_carlo_max_dd"]
    lines.append(
        f"몬테카를로 최대낙폭(순서 셔플, seed={mc['seed']}, n={mc['n_iters']}): "
        f"평균 {mc['max_dd_bp_mean']:.1f}bp · p95 {mc['max_dd_bp_p95']:.1f}bp"
    )
    eq = analytics["equity_curve"]
    lines.append(
        f"트레이드 곡선 MDD {eq['mdd_bp']:.1f}bp · 회복 {eq['recovery_days']}일 · "
        f"underwater {eq['time_under_water_pct']}%"
    )
    lines.append(f"({analytics['mfe_mae']['note']})")

    def _fmt_bucket(title: str, bucket: dict) -> str:
        parts = ", ".join(
            f"{k}:{v['n']}건/{v['expectancy_bp']:+.1f}bp" for k, v in sorted(bucket.items())
        )
        return f"{title}: {parts}" if parts else f"{title}: (없음)"

    lines.append(_fmt_bucket("시간대별(현지)", analytics["by_hour_of_day"]))
    lines.append(_fmt_bucket("요일별", analytics["by_day_of_week"]))
    lines.append(_fmt_bucket("종목별", analytics["by_symbol"]))
    lines.append(_fmt_bucket("보유시간별", analytics["by_holding_bucket"]))
    lines.append(_fmt_bucket("청산사유별", analytics["by_exit_reason"]))
    return "\n".join(lines)


def cmd_backtest_gate(args: argparse.Namespace) -> None:
    """배포 게이트 — §4 리포트 + 트레이드 다차원 분석 + go/no-go/판단 불가 판정을
    한 번에 낸다.

    인샘플 백테스트(`--days`)는 트레이드 단위 분석(시간대·보유시간·청산사유 등)의
    재료이고, walk-forward(`--total-days`/`--window`/`--step`)는 게이트의 OOS
    판정 재료다 — 같은 `--strategy`/`--source`/`--symbols`로 둘 다 돌려야 판정이
    의미가 있다.

    `run_walkforward`은 다른 작업자가 동시에 수정 중이라 키워드 인자로만 호출하고,
    `history_dir` 파라미터가 있는지 `inspect.signature`로 확인한 뒤에만 넘긴다 —
    시그니처가 바뀌어도 위치인자 순서 어긋남으로 조용히 틀리지 않게 하기 위함.

    실데이터가 없으면(`--source history`) `run_backtest`/`run_walkforward`이 던지는
    `ValueError`를 사람이 읽는 메시지로 바꿔 비정상 종료한다(exit 1) — stub으로
    조용히 대체하지 않는다.
    """
    import inspect
    import json as _json
    import sys as _sys
    from datetime import datetime
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.backtest.analytics import analyze_trades
    from quant.backtest.fitness import evaluate
    from quant.backtest.gate import evaluate_gate, render_gate
    from quant.backtest.strategy_report import report_text
    from quant.backtest.walkforward import run_walkforward, stability_summary
    from quant.control.cost_model import by_strategy, effective_round_trip_bp
    from quant.control.ledger import load_trades, round_trips
    from quant.control.tca import join_intents_fills, slippage_bps
    from quant.control.warehouse import read_jsonl
    from quant.core.models import market_of_symbol

    symbols = args.symbols.split() if args.symbols else None
    history_dir = getattr(args, "history_dir", None)
    fill_model = getattr(args, "fill_model", "close")

    try:
        result = run_backtest(
            strategy_id=args.strategy, days=args.days, interval=args.interval,
            source=args.source, symbols=symbols, history_dir=history_dir,
            fill_model=fill_model,
        )
    except ValueError as e:
        print(f"오류: {e}", file=_sys.stderr)
        raise SystemExit(1)

    wf_kwargs = dict(
        strategy_id=args.strategy, total_days=args.total_days, window_days=args.window,
        step_days=args.step, interval=args.interval, source=args.source, symbols=symbols,
    )
    wf_params = inspect.signature(run_walkforward).parameters
    if "history_dir" in wf_params and history_dir is not None:
        wf_kwargs["history_dir"] = history_dir
    # history_dir 과 같은 이유로 시그니처를 먼저 확인한다(동시 수정 중인 모듈).
    if "fill_model" in wf_params:
        wf_kwargs["fill_model"] = fill_model
    try:
        folds = run_walkforward(**wf_kwargs)
    except ValueError as e:
        print(f"오류: {e}", file=_sys.stderr)
        raise SystemExit(1)
    stability = stability_summary(folds)

    fit = evaluate(result)

    raw_trades = load_trades(ledger_state_path())
    trips = round_trips(raw_trades)
    intents = read_jsonl(REPO_ROOT / "data" / "state" / "order_intents.jsonl")
    slips = slippage_bps(join_intents_fills(intents, raw_trades))
    cost = by_strategy(trips, slips).get(args.strategy)
    cost_bp, cost_label = effective_round_trip_bp(cost)

    universe_symbols = symbols or (
        sorted(result.trades["symbol"].unique().tolist()) if not result.trades.empty else []
    )
    inferred_markets = sorted({market_of_symbol(s) for s in universe_symbols}) or ["US"]
    market = inferred_markets[0]
    if len(inferred_markets) > 1:
        print(f"※ 혼합 시장 유니버스({inferred_markets}) — 시간대 분석은 {market} 기준으로 계산")

    analytics = analyze_trades(result.trades, market=market, cost_bp=cost_bp)
    gate = evaluate_gate(folds, analytics, trials=args.trials, cost_bp=cost_bp, market=market)

    print(report_text(
        result, fit, folds, stability,
        strategy=args.strategy, source=args.source, interval=args.interval,
        window_days=args.window, step_days=args.step, n_trials=args.trials,
        cost_bp=cost_bp, cost_label=cost_label,
    ))
    print()
    print(_render_gate_analytics_text(analytics))
    print()
    print(render_gate(gate))

    out_dir = Path("data") / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"gate_{args.strategy}_{datetime.now().strftime('%Y%m%d')}.json"
    out_path.write_text(_json.dumps({
        "strategy": args.strategy, "source": args.source, "interval": args.interval,
        "generated_at": datetime.now().isoformat(),
        # 2026-09-03: `promote` 가 fail-closed 로 요구하는 증거 3종 — 어느 구간·어떤
        # 체결 가정·어떤 비용으로 판정했는지가 JSON 에 없으면 승격 자체를 거부한다.
        "fill_model": getattr(result, "fill_model", fill_model),
        "data_range": {
            "start": str(result.equity_curve.index.min()) if len(result.equity_curve) else None,
            "end": str(result.equity_curve.index.max()) if len(result.equity_curve) else None,
            "days": args.days, "total_days": args.total_days, "window_days": args.window,
            "step_days": args.step, "interval": args.interval, "source": args.source,
            "history_dir": str(history_dir) if history_dir else None,
            "symbols": universe_symbols,
        },
        "cost_assumptions": {"round_trip_bp": cost_bp, "label": cost_label, "market": market},
        "backtest_metrics": result.metrics,
        "fitness": fit.to_dict(),
        "walkforward_folds": folds,
        "stability": stability,
        "analytics": analytics,
        "gate": gate,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n작성됨: {out_path}")


def cmd_kelly(args: argparse.Namespace) -> None:
    """원장 기반 부분 켈리 자문 — 승률·payoff로 켈리 비율을 **표시만** 한다.

    자본 배분에 자동 반영되지 않는다(거버너 층 0). settings.yaml의 capital_fraction
    변경은 사람이 이 출력을 보고 직접 한다.
    """
    import json as _json

    from quant.control.kelly import advisory
    from quant.control.ledger import load_trades, round_trips

    trips = round_trips(load_trades(ledger_state_path()))
    result = advisory(trips)
    print(_json.dumps(result, ensure_ascii=False, indent=2))
    print("※ 자문일 뿐 자동 반영되지 않는다 — 사이징 변경은 사람이 settings.yaml 로 한다(거버너 층 0).")


def cmd_backtest(args: argparse.Namespace) -> None:
    # --symbols "TQQQ SQQQ": 관심종목(watchlist) 전략(symbols: [])은 이게 없으면
    # settings.yaml의 빈 symbols로 돌아 symbols[0] 접근에서 IndexError로 죽는다 —
    # run_backtest이 그 대신 명확한 에러로 멈춘다.
    symbols = args.symbols.split() if args.symbols else None
    fill_model = getattr(args, "fill_model", "close")
    result = run_backtest(
        strategy_id=args.strategy, days=args.days, interval=args.interval, source=args.source,
        symbols=symbols, history_dir=getattr(args, "history_dir", None),
        fill_model=fill_model,
    )
    print(
        f"\n=== Backtest: {args.strategy} ({args.days}d, {args.interval}, {args.source}, "
        f"fill={result.fill_model}) ==="
    )
    for key, value in result.metrics.items():
        print(f"{key:>18}: {value}")
    print(f"{'n_bars':>18}: {len(result.equity_curve)}")

    # 전략이 on_cycle에서 예외로 죽어 스킵된 사이클 — "n_trades 0"이 조건 미충족인지
    # 침묵 실패인지 이게 없으면 구분할 수 없다(mean_reversion이 실측으로 이렇게
    # 죽었었다: run_cycle이 예외를 삼키고 "OK, n_trades 0"만 남겼다).
    if result.strategy_errors:
        print("\n--- 전략 사이클 에러 ---")
        for sid, info in result.strategy_errors.items():
            print(f"{sid:>18}: {info['cycles_skipped']}회 스킵 — {info['last_error']}")
    else:
        print(f"{'strategy_errors':>18}: 0")

    # 회계 검산 내역을 항상 함께 낸다. run_backtest이 이미 통과를 강제하지만, 성과
    # 숫자만 단독으로 떠다니면 그게 어떤 통화의 무엇을 합산한 것인지 아무도 모른다 —
    # 손익이 USD, 자산곡선이 KRW인 채로 몇 달을 보낸 적이 있다.
    # 봉내 체결 모델을 썼다면 **몇 번 발동했는지**를 성과 옆에 붙인다. 0이면
    # "봉 안에서 손절선이 닿은 적이 없다"는 뜻이고, 그건 close 모델과 결과가
    # 같아야 한다는 검증 가능한 주장이다.
    if result.fill_model == "intrabar":
        print(
            f"{'봉내 체결':>18}: 손절 {result.intrabar_stop_fills}건 · "
            f"익절 {result.intrabar_target_fills}건 · "
            f"동일봉 양쪽 터치(손절 우선) {result.both_touched_conservative}건"
        )

    rec = result.reconciliation
    ccy = rec["currency"]
    print(f"\n--- 회계 검산 ({ccy}) ---")
    print(f"{'초기자산':>18}: {rec['initial_equity']:>18,.0f}")
    print(f"{'최종자산':>18}: {rec['final_equity']:>18,.0f}")
    print(f"{'실현손익':>18}: {rec['realized_pnl']:>18,.0f}")
    print(f"{'미실현손익':>18}: {rec['unrealized_pnl']:>18,.0f}")
    print(f"{'수수료':>18}: {-rec['fees']:>18,.0f}")
    print(f"{'잔차':>18}: {rec['residual']:>18,.6f}  (허용 {rec['tolerance']:,.6f})")

    # 벤치마크(단순 매수보유) 비교를 전략 수익률과 항상 나란히 찍는다 — 이 비교가
    # 없으면 10년 -34% 백테스트를 같은 기간 TQQQ 단순보유 +2,941%와 한 번도
    # 나란히 못 본다.
    bench = result.benchmark
    print(f"\n--- 벤치마크 비교 (단순 매수보유, {ccy}) ---")
    for label, key in (("buy&hold 100%", "buy_hold"), ("buy&hold 50%", "buy_hold_50pct")):
        m = bench.get(key, {})
        print(
            f"{label:>18}: return {m.get('total_return_pct', 0):>8.2f}%  "
            f"cagr {m.get('cagr_pct', 0):>8.2f}%  mdd {m.get('mdd_pct', 0):>8.2f}%  "
            f"sharpe {m.get('sharpe', 0):>6.2f}"
        )


def cmd_paper(args: argparse.Namespace) -> None:
    """실시세 기반 모의매매 루프. 조립은 app/assembly.py가 담당한다."""
    from quant.apps.assembly import MissingCredentials, build_paper_runtime, rebuild_strategies

    settings = load_settings()
    _redact.install()
    try:
        rt = build_paper_runtime(settings)
    except MissingCredentials as e:
        logger.error("%s", e)
        raise SystemExit(2)

    def _rebuild():
        """세션 롤에서 전략을 다시 조립한다. settings.raw를 그때 다시 읽으므로 핫
        리로드된 설정이 반영되고, 열린 포지션 종목은 유니버스에서 빠졌어도 전략에
        남는다(유니버스는 신규 진입 후보를 고르는 장치일 뿐이다).

        rebuild_strategies가 돌려주는 markets는 매번 새로 만든 dict라 그냥
        버리면 유니버스 롤로 새로 들어온 심볼이 risk.market_of/broker.market_of에
        영영 반영되지 않는다(A-4). risk.market_of와 broker.market_of는 조립
        시점에 같은 dict 객체를 공유하므로(assembly.build_paper_runtime), 그
        dict를 **재할당하지 않고 in-place update**해야 양쪽에 다 반영된다.

        leverage_of는 rt.leverage_of(부팅 시점 스냅샷)를 그대로 다시 넘긴다 —
        재조회하지 않는다(market_of와 달리 여기서 갱신 배선을 하지 않기로 한
        판단은 assembly.rebuild_strategies의 docstring 참고). 넘기지 않으면
        MeanReversionStrategy가 매 세션 롤마다 leverage_of=None으로 재조립돼
        레버리지 금지 게이트가 첫 세션 이후로 조용히 꺼지는 회귀가 생긴다."""
        held = [sym for sym, pos in rt.ctx.broker.positions().items() if pos.is_open]
        # `_held_symbols`(2026-09-03): `universe_filter`(A/B 분할)가 보유 종목을
        # 버리지 않게 하는 경로. rebuild_strategies 는 held 를 심볼 목록에
        # **합치기만** 하고 build_strategies 로 전달하지 않아, 필터 단계에서는
        # 어느 것이 보유분인지 알 수 없다 — cfg 에 실어 보낸다(build_strategies
        # docstring 참고, rebuild_strategies 가 `{**cfg, ...}`로 통과시킨다).
        strategies, _markets, _active = rebuild_strategies(
            {**settings.raw, "_held_symbols": held}, rt.universe,
            held_symbols=held, leverage_of=rt.leverage_of,
        )
        rt.risk.market_of.update(_markets)
        return strategies

    logger.info("paper loop 시작 — poll_seconds=%s", settings.poll_seconds)
    asyncio.run(run_paper_loop(
        rt.strategies, rt.ctx, rt.risk, rt.sinks, settings, rt.notifier,
        control=rt.control, market_data=rt.data, active_markets=rt.active_markets,
        approval=rt.approval, approval_notifier=rt.approval_notifier,
        approval_cfg=rt.approval_cfg, reconciler=rt.reconciler, regime=rt.regime,
        universe=rt.universe, rebuild_strategies=_rebuild if rt.universe is not None else None,
        name_of=rt.name_of, books=rt.books, tick_logger=rt.tick_logger,
        exposure_check=rt.exposure_check,
    ))


def cmd_fetch(args: argparse.Namespace) -> None:
    """과거 데이터 백필 → data/history/{symbol}/{YYYY}/{MM}.parquet(1분봉) 또는
    data/history/{symbol}/{interval}/{YYYY}/{MM}.parquet(1분봉이 아닌 native interval)."""
    load_settings()  # .env/.env.local 로드
    from datetime import datetime
    from pathlib import Path

    from quant.collect.quotes.backfill import DEFAULT_HISTORY_DIR, backfill

    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end) if args.end else datetime.now()
    # 파티션 루트. 기본값은 이 저장소의 data/history — 별도 데이터 레이크로 받으려면
    # --history-dir 로 지정한다(레이아웃은 동일하므로 --source history 백테스트가
    # 같은 --history-dir 로 그대로 읽는다).
    history_dir = getattr(args, "history_dir", None) or DEFAULT_HISTORY_DIR

    if args.source == "toss":
        if args.interval not in ("1m", "1d"):
            raise ValueError("toss 소스는 1분봉(1m) 또는 일봉(1d)만 지원합니다")
        from quant.adapters.brokers.toss.client import TossClient
        from quant.collect.quotes.toss_source import TossCandleSource

        client = TossClient(
            client_id=os.environ.get("TOSS_CLIENT_ID", ""),
            client_secret=os.environ.get("TOSS_CLIENT_SECRET", ""),
            account_seq=os.environ.get("TOSS_ACCOUNT_SEQ", ""),
            mode="paper",
        )
        source = TossCandleSource(client)
        report = backfill(
            args.symbol, source, start, end, interval=args.interval, history_dir=history_dir,
        )
    elif args.source == "yfinance":
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        source = YFinanceCandleSource(args.interval)
        report = backfill(
            args.symbol, source, start, end, interval=args.interval, history_dir=history_dir,
        )
    elif args.source == "alpaca":
        from quant.collect.quotes.alpaca_source import AlpacaCandleSource

        # 일봉(1d) 타임스탬프는 세션 시작 시각(04:00~05:00 ET)이라 정규장
        # 09:30~15:45 between_time 필터를 통과하지 못해 전부 걸러진다(실측
        # 확인됨) — 1d에 한해 정규장 필터를 끈다. 분봉(1m/5m/15m/1h)은 기존
        # 동작(regular_session_only=True) 그대로 유지한다.
        regular_session_only = args.interval != "1d"
        source = AlpacaCandleSource(args.interval, regular_session_only=regular_session_only)
        report = backfill(
            args.symbol, source, start, end, interval=args.interval, history_dir=history_dir,
        )
    else:
        raise ValueError(f"지원하지 않는 데이터 소스: {args.source}")

    print(f"\n=== fetch: {args.symbol} ({args.start} ~ {args.end or 'now'}, source={args.source}, interval={args.interval}) ===")
    print(f"partitions_written: {report.partitions_written or '(none)'}")
    print(f"partitions_skipped: {report.partitions_skipped or '(none)'}")
    print(f"total_bars: {report.total_bars}")
    print(f"missing weekday sessions: {len(report.gaps)}")
    for g in report.gaps[:20]:
        print(f"  - {g}")
    print(f"written to: {Path(history_dir) / args.symbol}/")


def cmd_naver_fundamentals(args: argparse.Namespace) -> None:
    """네이버 거래상위(시가총액/PER/ROE) 스냅샷 → `data/ledger/fundamentals_naver.jsonl`.

    KR 마감(15:30) 이후 매일 돌린다 — 장중 값은 계속 바뀌지만 이 원장은 append_ledger가
    (date, code) 기준으로 그날의 첫 관측값만 남기므로 마감 직후든 몇 시간 뒤든 결과는
    같다. DART 재무제표(분기 단위로만 바뀜)와 갱신 주기가 달라 서브커맨드를 분리했다
    — 매일 도는 이쪽과 달리 dart-fundamentals는 주 1회면 충분하다."""
    from pathlib import Path

    from quant.collect.sources.naver_quant import fetch_and_persist

    root = Path(args.root)
    stat = fetch_and_persist(root)
    print(
        f"네이버 펀더멘털: 조회 {stat['fetched']}건, 신규 적재 {stat['added']}건 "
        f"(date={stat['date']})"
    )


def _kr_watchlist_symbols(root) -> list[str]:
    """watchlist.yaml의 KR 종목(6자리 숫자)만. `backfill_kr_stock_daily.sh`가 셸에서
    하는 것과 같은 필터를 파이썬 쪽에서도 하나 둔다(대상 종목 기본값 산출용) —
    `quant.trade.universe`의 파서는 비공개(`_parse_watchlist*`)라 여기서 얇게
    재구현한다(`cmd_health`의 `_watchlist_intake_tags`와 같은 관례)."""
    import yaml

    from quant.core.models import market_of_symbol

    path = root / "data" / "watchlist.yaml"
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    entries = raw.get("symbols") or []
    out: list[str] = []
    for e in entries:
        sym = e.get("symbol") if isinstance(e, dict) else e
        if sym and market_of_symbol(str(sym)) == "KR":
            out.append(str(sym))
    return out


def cmd_dart_fundamentals(args: argparse.Namespace) -> None:
    """DART 재무제표(자본총계/부채총계/순이익/발행주식수 → 부채비율/ROE/BPS) →
    `data/ledger/fundamentals_dart.jsonl`. 대상은 기본적으로 관심종목(watchlist.yaml)의
    KR 종목이고, `--symbols`로 직접 지정할 수도 있다.

    사업보고서(reprt_code=11011, 연간)만 자본총계/부채총계/당기순이익을 온전히
    담는다(dart_financials.py 모듈 docstring) — 그래서 기본 서브커맨드로 이걸 쓴다.
    사업연도 기본값: 사업보고서는 다음 해 3월 말까지 제출되므로, 4월 이후면 작년도가
    이미 나와 있고 1~3월엔 아직 재작년도까지만 확정이다(실측 2026-08-19,
    bsns_year=2025 정상 응답 — dart_financials.py 모듈 docstring 참고)."""
    from datetime import datetime
    from pathlib import Path

    from quant.collect.sources.dart_financials import fetch_and_persist

    root = Path(args.root)
    stock_codes = args.symbols.split() if args.symbols else _kr_watchlist_symbols(root)
    if not stock_codes:
        print("대상 종목 없음 — watchlist.yaml에 KR 종목이 없고 --symbols도 지정되지 않음")
        return

    if args.bsns_year:
        bsns_year = args.bsns_year
    else:
        now = datetime.now()
        bsns_year = str(now.year - 1) if now.month >= 4 else str(now.year - 2)

    stat = fetch_and_persist(stock_codes, bsns_year, args.reprt_code, root)
    print(
        f"DART 펀더멘털: 대상 {stat['requested']}종목, 신규 적재 {stat['added']}건 "
        f"(bsns_year={bsns_year}, reprt_code={args.reprt_code})"
    )
    if stat["errors"]:
        print(f"오류 {len(stat['errors'])}건:")
        for err in stat["errors"]:
            print(f"  - {err}")


def _load_param_space(strategy_id: str, path: str | None) -> dict:
    if path is not None:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    from quant.research.param_spaces import DEFAULT_PARAM_SPACES
    if strategy_id not in DEFAULT_PARAM_SPACES:
        raise SystemExit(
            f"'{strategy_id}'용 기본 param space가 없음 — --param-space로 YAML 경로를 지정하세요."
        )
    return DEFAULT_PARAM_SPACES[strategy_id]


def cmd_optimize(args: argparse.Namespace) -> None:
    """파라미터 최적화 + walk-forward 검증. 연구 스택(optuna/quantstats, `research`
    dependency group)이 필요 — 여기서만 지연 임포트한다."""
    from datetime import datetime
    from pathlib import Path

    from quant.research import report
    from quant.research.walkforward import split_windows, walk_forward

    param_space = _load_param_space(args.strategy, args.param_space)
    windows = split_windows(
        start=datetime.fromisoformat(args.start),
        end=datetime.fromisoformat(args.end),
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days or args.test_days,
        embargo_days=args.embargo_days,
    )
    if not windows:
        raise SystemExit(
            f"윈도우 0개 — start~end 구간이 train_days({args.train_days})+"
            f"test_days({args.test_days})(+embargo {args.embargo_days})를 못 채움"
        )

    result = walk_forward(
        strategy_id=args.strategy, param_space=param_space, windows=windows,
        n_trials=args.trials, source=args.source, seed=args.seed, interval=args.interval,
    )

    print()
    print(quant.analyze.render_text(result, strategy_id=args.strategy))

    out_dir = Path("data/research")
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{args.strategy}_walkforward.html"
    if report.render_html(result, str(html_path), strategy_id=args.strategy):
        print(f"\nHTML 리포트: {html_path}")


def cmd_report(args: argparse.Namespace) -> None:
    """Toss 실계좌 일일 진단 리포트 → Telegram (미설정 시 콘솔 출력)."""
    load_settings()  # .env/.env.local 로드
    from quant.control.banker import run_report
    from quant.adapters.brokers.toss.client import TossClient

    class _ConsoleNotifier:
        def send(self, text: str) -> None:
            print(text)

    notifier = _ConsoleNotifier()
    try:
        from quant.adapters.notify.telegram import TelegramNotifier
        tg = TelegramNotifier.from_env()
        if getattr(tg, "enabled", False):
            notifier = tg
    except Exception as e:
        logger.warning("텔레그램 미설정 — 콘솔로 출력: %s", e)

    mode = os.environ.get("MODE", "paper")
    if mode != "live":
        logger.info("banker report: MODE=%s — 읽기 전용 조회로 실행한다(주문은 어느 모드에서도 내지 않는다)", mode)
    client = TossClient(
        client_id=os.environ.get("TOSS_CLIENT_ID", ""),
        client_secret=os.environ.get("TOSS_CLIENT_SECRET", ""),
        account_seq=os.environ.get("TOSS_ACCOUNT_SEQ", ""),
        mode=mode,
    )
    run_report(client, notifier)


def cmd_watch_score(args: argparse.Namespace) -> None:
    """워치리스트 후보 종목 결정론적 채점 — 08:40 daily-brief cron 전용(리포팅 레이어).
    최종 줄 `PASS: ...` 포맷은 셸 스크립트가 파싱하므로 그대로 유지할 것.
    입력 토큰 포맷: `SYMBOL[:TAGS[:YYYYMMDD]]` (TAGS는 TREND/REBOUND/EVENT를
    '+'로 조합, 세 번째 필드는 리포트 발행일)."""
    import json
    from pathlib import Path

    settings = load_settings()
    _redact.install()  # .env/.env.local + settings.yaml 로드
    from quant.apps.assembly import MissingCredentials, build_toss_client
    from quant.analyze.watch_scorer import resolve_regime_label, run_watch_score

    auto_score_cfg = settings.universe.get("watchlist", {}).get("auto_score", {})
    enabled = auto_score_cfg.get("enabled", True)

    threshold = args.threshold
    if threshold is None:
        threshold = auto_score_cfg.get("threshold", 50)

    regime_state: dict | None = None
    # CWD가 아니라 저장소 루트 기준으로 고정 — cron이 어느 디렉토리에서 불려도 동일하게 찾는다.
    regime_path = regime_state_path()
    if regime_path.exists():
        try:
            regime_state = json.loads(regime_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("regime.json 파싱 실패 — neutral로 대체: %s", e)
    regime_label, stale_reason = resolve_regime_label(regime_state)
    if stale_reason:
        logger.warning("watch-score: %s", stale_reason)

    tokens = args.symbols.split()

    discover_markets = []
    if getattr(args, "discover_kr", False):
        discover_markets.append("KR")
    if getattr(args, "discover_us", False):
        discover_markets.append("US")

    if not tokens and not discover_markets:
        print("PASS: 없음")
        return

    if not enabled:
        results = run_watch_score(tokens, None, threshold, regime_label, enabled=False)
    else:
        try:
            client = build_toss_client()
        except MissingCredentials as e:
            logger.error("%s", e)
            raise SystemExit(2)
        # 발굴 후보(--discover-kr / --discover-us): 시장별 거래대금 랭킹 상위를
        # 리포트 후보와 합친다. 리포트 후보가 우선 — 같은 심볼이 양쪽에 있으면
        # 리포트 쪽 태그를 쓴다. US는 회사 리포트(한국 시황)에 안 나오므로 사실상
        # 이 발굴 경로가 유일한 자동 편입 수단이다(2026-08-11 사용자 요청).
        if discover_markets:
            from quant.analyze.watch_scorer import discover_candidates
            for market in discover_markets:
                have = {t.split(":")[0] for t in tokens}
                found = [
                    t for t in discover_candidates(client, market=market)
                    if t.split(":")[0] not in have
                ]
                if found:
                    logger.info("%s 발굴 후보 %d개 추가: %s", market, len(found), " ".join(found))
                    tokens = tokens + found

        # 종목별 수급(ka10059)용 키움 클라이언트 — 키가 있으면 붙인다. 서버 IP가
        # 아직 WAF에 안 풀렸거나 조회가 실패하면 scorer가 시장 조류로 폴백하므로
        # 여기서 실패해도 채점은 계속된다.
        kiwoom_client = None
        app_key = os.environ.get("KIWOOM_APP_KEY", "")
        secret_key = os.environ.get("KIWOOM_SECRET_KEY", "")
        if app_key and secret_key:
            try:
                from quant.adapters.brokers.kiwoom.client import KiwoomClient
                kiwoom_client = KiwoomClient(app_key=app_key, secret_key=secret_key)
            except Exception as e:  # noqa: BLE001 — 보조 데이터 소스 실패는 치명 아님
                logger.warning("키움 클라이언트 생성 실패 — 시장 조류로 폴백: %s", e)
        # 자금 흐름 섹터 기울기(§4, 2026-08-31 소유자 지시) — KR 종목의 네이버
        # 업종(sector_map.json)과 quant.analyze.money_flow 판정을 매칭해 증거
        # 점수 ±2를 가감한다. 둘 중 하나라도 없으면(원장 초기 배포 등)
        # run_watch_score/macro_sector_adjustment가 None으로 조용히 건너뛴다
        # — 매크로 데이터 없음이 채점 자체를 막지 않는다.
        from quant.adapters.env import REPO_ROOT
        from quant.adapters.macro.fred import DEFAULT_LEDGER_PATH as _MACRO_LEDGER_PATH
        from quant.analyze.money_flow import analyze_money_flow
        from quant.report.paths import _load_artifact

        sector_map = _load_artifact(REPO_ROOT / "data" / "ledger" / "sector_map.json")
        sector_tilt = None
        try:
            sector_tilt = analyze_money_flow(REPO_ROOT / _MACRO_LEDGER_PATH)["sector_tilt"].get("KR")
        except Exception as e:  # noqa: BLE001 — 매크로 판정 실패가 채점을 막지 않는다
            logger.warning("watch-score: 자금 흐름 섹터 기울기 생략: %s", e)

        # 주도 섹터(2026-09-03 소유자 철학 지시 B) — sector_daily.jsonl 최신일
        # 순위+외국인 수급으로 KR 증거점수 보너스/페널티(watch_scorer.
        # sector_daily_adjustment). 원장은 report_cli(08:00 아침 리포트 빌드)가
        # 이미 적재해 둔다 — 여기서는 읽기만 한다. 원장이 아직 없으면(리포트가
        # 한 번도 못 만들었거나 US만 도는 상황) 조용히 0 처리한다(§C).
        sector_daily_ctx = None
        sd_path = REPO_ROOT / "data" / "ledger" / "sector_daily.jsonl"
        if sd_path.exists():
            try:
                sd_rows = []
                for line in sd_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("date") and row.get("sector"):
                        sd_rows.append(row)
                if sd_rows:
                    from quant.analyze.sector_daily import rank_with_trend, scoring_context
                    sd_dates = sorted({r["date"] for r in sd_rows})
                    sd_latest = sd_dates[-1]
                    sd_today_rows = [r for r in sd_rows if r["date"] == sd_latest]
                    sd_history_rows = [r for r in sd_rows if r["date"] in sd_dates[-6:-1]]
                    sd_ranked = rank_with_trend(sd_today_rows, sd_history_rows)
                    sector_daily_ctx = scoring_context(sd_ranked)
            except Exception as e:  # noqa: BLE001 — 주도 섹터 컨텍스트 실패가 채점을 막지 않는다
                logger.warning("watch-score: 주도 섹터 컨텍스트 생략: %s", e)
        if sector_daily_ctx is None:
            logger.info("watch-score: 주도 섹터 데이터 없음 — 보너스 0")

        results = run_watch_score(
            tokens, client, threshold, regime_label, enabled=True, kiwoom_client=kiwoom_client,
            allow_kr_stocks=auto_score_cfg.get("allow_kr_stocks", False),
            sector_map=sector_map, sector_tilt=sector_tilt,
            sector_daily_ctx=sector_daily_ctx,
        )

    # 세분화 출력(2026-08-10 사용자 요청): 항목별 득점/만점 → 총점 → 임계값 구성.
    # 마지막 `PASS:` 줄만 기계 계약(daily_brief.sh 파싱) — 나머지는 텔레그램용.
    import re as _re

    passing = []
    for r in results:
        status = "✅ PASS" if r.passed else "❌ FAIL"
        profile = r.profile or ("+".join(r.tags) if r.tags else "무태그")
        thr = f"임계 {r.eff_threshold}" + (f" = {' '.join(r.threshold_notes)}" if r.threshold_notes else "")
        print(f"{r.symbol} [{profile}] 총 {r.score}/100 → {status} ({thr})")
        for name, earned, mx, detail in r.breakdown:
            mark = "●" if earned > 0 else "○"
            mx_str = f"/{mx}" if mx else ""
            print(f" {mark} {name}: {earned}{mx_str} — {detail}")
        # 게이트/수급/경고 등 점수 외 사유 — 증거 항목(끝이 "(+N)")은 breakdown이
        # 이미 보여줬으니 중복 출력하지 않는다.
        for reason in r.reasons:
            if not _re.search(r"\(\+\d+\)($|;)", reason):
                print(f" · {reason}")
        if r.passed:
            # 태그를 PASS 토큰에 실어보낸다(2026-08-12, news_momentum의 EVENT 게이트가
            # 필요로 함) — daily_brief.sh가 SYMBOL[:TAG[+TAG]] 형태로 파싱해
            # watch-add --tags로 그대로 넘긴다. r.tags는 입력 토큰(SYMBOL:TAGS:...)에서
            # 파싱된 태그다(무태그 best-of로 채점됐어도 원래 태그가 없었으면 빈 리스트 —
            # 채점에 쓰인 profile을 태그로 승격하지 않는다: EVENT는 뉴스 근거를 뜻하지
            # "EVENT 프로필로 가장 높은 점수가 나왔다"를 뜻하지 않는다).
            token = r.symbol + (":" + "+".join(r.tags) if r.tags else "")
            passing.append(token)

    # 시총 게이트 탈락 요약(2026-09-03 소유자 철학 지시 A) — 미확인("시총
    # 미확인")과 미달("시총 <3,000억")을 한 줄로 합산해 own_brief.sh 로그에서
    # 그날 몇 건이 시총 때문에 빠졌는지 바로 보이게 한다.
    cap_rejected = sum(
        1 for r in results
        if any(reason.startswith("시총 미확인") or reason.startswith("시총 <") for reason in r.reasons)
    )
    if cap_rejected:
        print(f"시총 <3,000억 탈락 {cap_rejected}건")

    print(f"PASS: {' '.join(passing) if passing else '없음'}")


def _news_scalp_verdict_line() -> str:
    """갈래 A(news_scalp) 승격 판정 줄 — intraday_verify 하네스가 남긴 원장
    (`data/ledger/intraday_verify.jsonl`)의 최신 `metrics`(=aggregate_metrics()
    출력 그대로)를 직접 읽는다. `quant.control.ledger`는 `quant.backtest`를
    임포트하지 않으므로(의도적 결합 축소, news_scalp_promotion_verdict docstring)
    aggregate 조립은 이 apps 계층(양쪽 평면을 다 아는 주입 지점)이 한다."""
    import json

    from quant.adapters.env import REPO_ROOT
    from quant.control.ledger import news_scalp_promotion_verdict

    path = REPO_ROOT / "data" / "ledger" / "intraday_verify.jsonl"
    if not path.exists():
        return "갈래 A(news_scalp) 승격 판정: intraday_verify 하네스 미실행 — 원장 없음"
    aggregate = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("metrics"), dict):
            aggregate = row["metrics"]
    if aggregate is None:
        return "갈래 A(news_scalp) 승격 판정: intraday_verify 원장이 비어 있음"

    settings = load_settings()
    execution_cfg = settings.raw.get("execution", {}) or {}
    fee_bps_cfg = execution_cfg.get("fee_bps", 0.0)
    fee_bps_kr = fee_bps_cfg.get("KR", 0.0) if isinstance(fee_bps_cfg, dict) else fee_bps_cfg
    # 왕복(진입+청산) 수수료 + KR 개별주 매도 거래세 — news_scalp 후보는 KR 전용.
    round_trip_fee_bps = float(fee_bps_kr) * 2 + float(execution_cfg.get("kr_stock_sell_tax_bps", 0.0))
    verdict = news_scalp_promotion_verdict(aggregate, round_trip_fee_bps=round_trip_fee_bps)
    return f"갈래 A(news_scalp) 승격 판정: {verdict['reason']}"


def _ab_arm_line(label: str, arm_id: str, arm: dict, market: str | None) -> str:
    """A/B 한 갈래 한 줄. 숫자가 없는 칸은 만들어 내지 않고 "-" 로 둔다."""
    wr = "     -" if arm["win_rate"] is None else f"{arm['win_rate'] * 100:5.0f}%"
    ci = ("" if arm["win_rate"] is None
          else f" (CI {arm['win_ci'][0] * 100:.0f}~{arm['win_ci'][1] * 100:.0f}%)")
    exp = ("        -" if arm["expectancy_bp"] is None
           else f"{arm['expectancy_bp']:+7.1f}bp")
    money = ("" if market is None
             else " · 순손익 " + (f"{arm['net_pnl']:,.0f}원" if market == "KR"
                                else f"${arm['net_pnl']:,.2f}"))
    return (f"  {label} {arm_id:<20} n={arm['n']:>4} · 승률 {wr}{ci}"
            f" · 기대값 {exp}{money}")


def _ab_report_lines(rows: list[dict]) -> list[str]:
    """`ledger.ab_compare` 결과 → 사람이 읽는 줄들. **계산은 하지 않는다**
    (수치는 전부 ledger 가 만든 것 그대로 — 포맷만 여기 있다)."""
    if not rows:
        return ["🧪 A/B 갈래 비교: 설정에 `<id>`/`<id>_cat` 쌍이 없다"]
    out = ["🧪 A/B 갈래 비교 — 촉매 태그(KR=외국인 수급 FRGN / US=뉴스 EVENT+추세 TREND)"
           " 유무로 갈라 놓은 같은 전략. 양쪽 n>=30 전에는 판단하지 않는다"]
    for r in rows:
        market = r["market"]
        out.append(f"[{r['base']} · {market or '표본 없음'}]")
        out.append(_ab_arm_line("기준", r["base"], r["baseline"], market))
        out.append(_ab_arm_line("촉매", r["catalyst_id"], r["catalyst"], market))
        if not r["judgeable"]:
            out.append(f"  → {r['reason']}")
            continue
        lo, hi = r["delta_ci"]
        out.append(
            f"  → 차이(촉매-기준) {r['delta_expectancy_bp']:+.1f}bp"
            f" (95% CI {lo:+.1f}~{hi:+.1f}) · 순열검정 p={r['p_value']:.3f}"
        )
    return out


def cmd_scoreboard(args: argparse.Namespace) -> None:
    """누적 거래 원장(data/state/trades.jsonl) → 전략별·종목별 스코어보드.

    승률·payoff·거래당 bps가 자본 배분 판단의 근거다(2026-08-10 사용자 원칙).
    출력은 stdout — 주간 크론(server/scripts/scoreboard_weekly.sh)이 텔레그램으로 쏜다.

    **3갈래 자동화(T) 승격 판정**(2026-08-17, spec §4) — 판정만 노출하고 자동
    승격은 하지 않는다(`quant.control.ledger`의 두 판정 함수 docstring 참고).
    스코어보드 표 자체(원장 raw trades 기준)와는 별도로, 갈래 A는 intraday_verify
    하네스 원장을, 갈래 B는 같은 raw trades를 재사용한다."""
    from pathlib import Path

    from quant.control.ledger import (
        ab_compare, ab_pairs_from_config, filter_recent,
        frgn_accumulate_promotion_verdict, load_trades, round_trips, scoreboard_text,
    )

    ledger_path = Path(args.ledger) if getattr(args, "ledger", None) else ledger_state_path()
    trades = load_trades(ledger_path)
    trips = round_trips(trades)
    title = "누적 스코어보드"
    if args.days:
        trips = filter_recent(trips, args.days)
        title = f"최근 {args.days}일 스코어보드"
    print(scoreboard_text(trips, title=title))

    # A/B 갈래 비교(2026-09-03) — 기본 출력에 섞지 않는다. 스코어보드는 "전략별
    # 성적"이고 이건 "같은 전략의 두 유니버스 중 어느 쪽이 나은가"라는 다른 질문이라,
    # 매주 텔레그램으로 나가는 본문을 두 배로 늘리지 않고 플래그로 연다.
    if getattr(args, "ab", False):
        rows = ab_compare(trips, bases=ab_pairs_from_config(load_settings().raw))
        print()
        print("\n".join(_ab_report_lines(rows)))

    print()
    frgn_verdict = frgn_accumulate_promotion_verdict(trades)
    print(f"갈래 B(frgn_accumulate) 승격 판정: {frgn_verdict['reason']}")
    print(_news_scalp_verdict_line())


def cmd_orders(args: argparse.Namespace) -> None:
    """주문 생애 원장(data/state/orders.jsonl) 조회 — 체결 원장(trades.jsonl)과
    달리 "시킨 것과 일어난 일의 차이"(거부·미체결 포함)를 담는다
    (`quant.control.ledger.TradeLedgerSink.on_order`).

    --rejected-funds: 자금 부족으로 못 산 시도만 필터한다. 소유자 지시(2026-08-31)
    "잔고 부족으로 못 산 경우, 시도 기록을 남겨 나중에 '안 산 게 아니라 못 샀던
    것'이 되게" 대응 — risk 레벨 예산 부족(quant/trade/risk/manager.py)과 브로커
    레벨 insufficient-buying-power(quant/adapters/brokers/toss/broker.py) 둘 다
    reason에 "자금 부족" 마커를 통일해뒀으므로 grep도, 이 필터도 같은 문자열 하나로
    양쪽을 다 잡는다."""
    from quant.control.ledger import load_orders

    orders_path = ledger_state_path().parent / "orders.jsonl"
    rows = load_orders(orders_path)
    if args.rejected_funds:
        rows = [r for r in rows if "자금 부족" in (r.get("reason") or "")]
    if args.limit:
        rows = rows[-args.limit:]
    if not rows:
        print("해당하는 주문 기록이 없음.")
        return
    for r in rows:
        print(
            f"{r.get('ts', '?')}  {r.get('market', '?'):>2}  "
            f"{r.get('strategy_id', '?'):<16}  {r.get('symbol', '?'):<8}  "
            f"{r.get('side', '?'):<4}  {r.get('status', '?'):<10}  {r.get('reason', '')}"
        )


def cmd_forensics(args: argparse.Namespace) -> None:
    """거래 부검 — 원장의 "졌다"를 "무엇 때문에 졌다"로 바꾼다.

    `scoreboard`가 결과(승률·payoff)를 낸다면 이건 **원인 후보**를 낸다:
    보유 중 최대유리(MFE) 대비 실현이 얼마였나(청산 효율), 진입 위치가 실제로
    승패를 갈랐나(대조군), 사전 지정 청산 규칙을 그 봉에 다시 돌리면 어땠나.

    2026-08-21에 이 분석을 손으로 했고 그때 나온 답이 "고칠 곳은 진입이 아니라
    청산"이었다(MFE 중앙 +113bp vs 실현 -47bp, 진입 위치 rho=+0.00). 1회용
    스크립트로 두면 다음에도 손으로 다시 해야 하므로 커맨드로 고정한다 —
    주간 크론이 `scoreboard` 옆에서 같이 돈다.

    청산 규칙은 **여기 하드코딩된 4종만** 재생한다(탐색 4회). 파라미터를
    탐색하지 않는 게 요점이다: 표본 수십 건에 규칙 수십 개를 시험하면 그중
    몇 개는 반드시 우연히 훌륭해 보인다."""
    from pathlib import Path

    import pandas as pd

    # 루트는 REPO_ROOT 하나만 센다 — `quant/data/state/...` 를 읽어 "거래 없음"을
    # 출력한 착오가 이 파일에서 이미 세 번 났다(ledger_state_path docstring).
    from quant.adapters.env import REPO_ROOT
    from quant.control.forensics import forensics_text, replay_all, simulate_exit_rules
    from quant.control.ledger import filter_recent, load_trades, round_trips

    root = REPO_ROOT
    trips = round_trips(load_trades(ledger_state_path()))
    title = "거래 부검"
    if args.days:
        trips = filter_recent(trips, args.days)
        title = f"거래 부검 — 최근 {args.days}일"
    if args.strategy:
        trips = [t for t in trips if t.get("strategy") == args.strategy]
        title += f" [{args.strategy}]"

    history_dir = root / "data" / "history"
    _cache: dict[tuple[str, str], object] = {}

    def load_bars(symbol: str, ts):
        """1분봉은 `data/history/{symbol}/{YYYY}/{MM}.parquet`(2단계 경로).
        간격 디렉토리가 있는 3단계 경로(1d/15m 등)와 구조가 다르다 —
        adapters/data/history.py `_load_1m` 과 같은 규칙이다.

        같은 (심볼, 월) 파티션을 종결마다 다시 읽지 않도록 캐시한다."""
        ts = pd.Timestamp(ts)
        key = (symbol, f"{ts.year}{ts.month:02d}")
        if key not in _cache:
            path = history_dir / symbol / str(ts.year) / f"{ts.month:02d}.parquet"
            if not path.exists():
                _cache[key] = None
            else:
                try:
                    df = pd.read_parquet(path)
                    if df.index.tz is None:
                        df.index = df.index.tz_localize("UTC")
                    _cache[key] = df
                except Exception as e:  # noqa: BLE001 — 봉 하나 못 읽는다고 부검 전체를 버리지 않는다
                    logger.warning("부검: %s 봉 읽기 실패(건너뜀): %s", symbol, e)
                    _cache[key] = None
        month = _cache[key]
        if month is None:
            return None
        day = month[month.index.normalize() == ts.normalize()]
        return day if len(day) else None

    rows, skipped = replay_all(trips, load_bars)

    # 사전 지정 청산 규칙 — 이 목록을 늘릴 때는 다중검정 편향을 함께 고지한다.
    # (익절 100 / 손절 100 은 2026-08-21 실측으로 채택돼 지금 배선된 값이다 —
    #  현행이 그 규칙대로 도는지 확인하는 기준선 역할도 한다.)
    RULES = [
        ("무규칙(마감까지)", None, None),
        ("익절+100bp", 100.0, None),
        ("익절+100/손절-100", 100.0, 100.0),
        ("손절-100bp만", None, 100.0),
    ]
    rules_result = simulate_exit_rules(trips, load_bars, RULES) if rows else None

    print(forensics_text(rows, skipped, title=title, rules_result=rules_result))


def cmd_daily_feedback(args: argparse.Namespace) -> None:
    """일일 피드백 — 오늘 진입한 체결의 **타이밍**을 규칙 기반으로 판정해
    전략별로 돌려준다 (2026-08-26 소유자 조직도 역할 5).

    forensics(`cli forensics`)와 역할이 다르다: forensics는 청산까지 포함한
    "왜 졌나"를 재생하고, 이건 "그 순간 진입이 나빴나"(고점매수/거래 소강
    진입/늦은 진입)만 본다. LLM 없음 — 전부 결정론, 임계는 [미검증 초기값]
    (`quant.control.daily_feedback` 모듈 docstring 참고).

    픽(진입 체결) 없으면 무출력 — experiments_daily.sh 관례. 같은 (날짜, 시장)
    은 `data/ledger/daily_feedback.jsonl`에 멱등 append(재실행해도 중복 안 남음)."""
    import json as _json
    from datetime import date as _date, datetime as _dt
    from zoneinfo import ZoneInfo

    import pandas as pd

    from quant.adapters.env import REPO_ROOT
    from quant.control.daily_feedback import (
        already_recorded, render_feedback_text, strategy_feedback, todays_round_trips,
    )
    from quant.control.ledger import load_trades
    from quant.control.warehouse import read_jsonl

    root = REPO_ROOT
    market = args.market
    tz = ZoneInfo("Asia/Seoul") if market == "KR" else ZoneInfo("America/New_York")
    on = args.date or _dt.now(tz).date().isoformat()

    trades = load_trades(ledger_state_path())
    trips = todays_round_trips(trades, market, on)
    if not trips:
        return  # 무출력 — experiments_daily.sh 관례(오늘 진입 없으면 조용히 대기)

    # 1분봉은 forensics의 load_bars와 같은 2단계 경로(`data/history/{symbol}/
    # {YYYY}/{MM}.parquet`) — 종목당 한 번만 읽는다.
    history_dir = root / "data" / "history"

    def _load_symbol_day(symbol: str, entry_ts: str):
        ts = pd.Timestamp(entry_ts)
        path = history_dir / symbol / str(ts.year) / f"{ts.month:02d}.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path)
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
        except Exception as e:  # noqa: BLE001 — 봉 하나 못 읽는다고 피드백 전체를 버리지 않는다
            logger.warning("일일 피드백: %s 봉 읽기 실패(건너뜀): %s", symbol, e)
            return None
        day = df[df.index.normalize() == ts.normalize()]
        return day if len(day) else None

    bars_by_symbol: dict[str, object] = {}
    for t in trips:
        sym = t["symbol"]
        if sym not in bars_by_symbol:
            bars_by_symbol[sym] = _load_symbol_day(sym, t["entry_ts"])

    feedback = strategy_feedback(trips, bars_by_symbol)
    target = _date.fromisoformat(on)
    print(render_feedback_text(target, market, feedback))

    out_path = root / "data" / "ledger" / "daily_feedback.jsonl"
    existing = read_jsonl(out_path)
    if not already_recorded(existing, on, market):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "date": on, "market": market, "feedback": feedback,
            "recorded_at": _dt.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")


def _daily_closes(history_dir, symbol: str) -> list[tuple[object, float]]:
    """`data/history/{symbol}/1d/{YYYY}/{MM}.parquet` → [(날짜, 종가)].

    빈 파티션은 버린다 — 백필은 데이터가 없는 달에도 0행 파일을 남기고, 빈
    DataFrame 은 DatetimeIndex 를 잃어 인덱스가 혼합 타입이 된다(regime provider
    가 같은 결함을 이미 겪었다). 파케이를 못 읽는 건 알파를 못 내는 사유일 뿐
    예외로 올릴 일이 아니다."""
    import pandas as pd

    base = history_dir / symbol / "1d"
    out: list[tuple[object, float]] = []
    for part in sorted(base.glob("*/*.parquet")) if base.exists() else []:
        try:
            df = pd.read_parquet(part)
        except Exception:  # noqa: BLE001 — 파티션 하나가 전체를 막지 않는다
            continue
        if df.empty or "close" not in df.columns:
            continue
        for ts, r in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else None
            if d is None:
                continue
            try:
                close = float(r["close"])
            except (TypeError, ValueError):
                continue
            if close > 0:
                out.append((d, close))
    out.sort(key=lambda t: t[0])
    return out


def _last_daily_close(history_dir, symbol: str) -> float | None:
    closes = _daily_closes(history_dir, symbol)
    return closes[-1][1] if closes else None


def cmd_alpha_report(args: argparse.Namespace) -> None:
    """지수 대비 초과수익(알파) 일일 추적 — 2026-08-28 소유자 지시.

    "지수가 빠지건 오르건 항상 지수 그래프 위에서 논다"가 목표다. 목표를
    보장할 수는 없어도 **측정은 할 수 있다** — 측정하지 않으면 알파가 있는지조차
    알 수 없다.

    지수 수익률의 출처는 두 갈래고 순서가 있다: ① 자본 곡선 행에 동반 기록된
    `benchmark_close`(2026-08-28~), ② 로컬 일봉 파케이. ①이 있는 날은 ①을 쓴다
    — 그날 마감 시점에 실제로 본 값이고, 백필 상태에 의존하지 않는다."""
    import json as _json
    from datetime import date as _date

    from quant.adapters.env import REPO_ROOT
    from quant.control.alpha import (
        BENCHMARKS, alpha_series, alpha_summary, benchmark_returns, daily_returns,
    )

    path = REPO_ROOT / "data" / "ledger" / "equity_curve.jsonl"
    if not path.exists():
        print("표본 없음 — 자본 곡선 원장이 없다(`cli equity-snapshot` 이 아직 안 돌았다)")
        return

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(_json.loads(line))
        except ValueError:
            continue

    for market in [args.market] if args.market else ["KR", "US"]:
        ours = [(d, r) for d, m, r in daily_returns(rows) if m == market]
        bench_symbol = BENCHMARKS[market]

        # 동반 기록 우선, 없는 날만 로컬 일봉으로 채운다.
        bars: dict[object, float] = {
            b[0]: b[1] for b in _daily_closes(REPO_ROOT / "data" / "history", bench_symbol)
        }
        for r in rows:
            if r.get("market") != market or r.get("benchmark_symbol") != bench_symbol:
                continue
            close = r.get("benchmark_close")
            try:
                d = _date.fromisoformat(str(r.get("date"))[:10])
            except ValueError:
                continue
            if close is not None and float(close) > 0:
                bars[d] = float(close)

        series = alpha_series(ours, benchmark_returns(sorted(bars.items())))
        if args.days and args.days > 0:
            series = series[-args.days:]
        if not series:
            print(f"[{market}] 표본 없음 — 자본 곡선 2점 이상 + {bench_symbol} 종가가 있어야 알파가 나온다")
            continue

        print(f"[{market}] 지수 대비 초과수익 — 벤치마크 {bench_symbol} ({len(series)}일)")
        print(f"{'날짜':<12}{'우리%':>10}{'지수%':>10}{'알파%p':>10}")
        for d, our, bench, alpha in series:
            print(f"{d.isoformat():<12}{our:>10.2f}{bench:>10.2f}{alpha:>10.2f}")

        s = alpha_summary(series)
        print(f"  누적: 우리 {s['cum_our_pct']:+.2f}% / 지수 {s['cum_bench_pct']:+.2f}% "
              f"→ 알파 {s['cum_alpha_pp']:+.2f}%p (이긴 날 {s['win_days']}/{s['n_days']})")
        if s["up_our_avg_pct"] is None:
            print(f"  지수 상승일 참여: 표본 부족({s['up_days']}일)")
        else:
            print(f"  지수 상승일 참여: 우리 {s['up_our_avg_pct']:+.2f}% vs 지수 "
                  f"{s['up_bench_avg_pct']:+.2f}% ({s['up_days']}일, {s['up_capture']:.2f}x — 높을수록 더 먹었다)")
        if s["down_our_avg_pct"] is None:
            print(f"  지수 하락일 방어: 표본 부족({s['down_days']}일)")
        else:
            print(f"  지수 하락일 방어: 우리 {s['down_our_avg_pct']:+.2f}% vs 지수 "
                  f"{s['down_bench_avg_pct']:+.2f}% ({s['down_days']}일, {s['down_capture']:.2f}x — 낮을수록 덜 잃었다)")
        print()


def cmd_equity_snapshot(args: argparse.Namespace) -> None:
    """자본 곡선 1점 기록 — 세션 마감 후 총자산·전략별 장부 평가액(KRW)을
    `data/ledger/equity_curve.jsonl` 에 덧붙인다 (gs-quant 대조 도입, 2026-08-24).

    거래 원장(bps)은 거래의 질문에 답하고, 이 곡선은 자본의 질문(변동성·샤프·
    MDD·CAGR — `cli performance`)에 답한다. 둘은 다른 데이터다 — 곡선이 안
    쌓이면 성과 분석은 영원히 거래 단위에 갇힌다.

    시세는 session-pnl 과 같은 경로(Toss /prices 배치). **시세가 없는 종목은
    평균단가로 저하하고 그 사실을 기록한다**(marked/degraded 카운트) — 없는
    시세를 지어내지 않되, 한 종목 시세 실패가 그날 곡선 점 자체를 잃게 하지도
    않는다(빠진 날은 나중에 복원할 수 없다).

    같은 (date, market) 은 마지막 기록이 이긴다 — 재실행은 덮어쓰기가 아니라
    append 이고, 읽는 쪽(`cli performance`)이 마지막 것만 쓴다(원장 관례).

    2026-08-28: **벤치마크 종가를 같이 적는다**(`benchmark_symbol`/`benchmark_close`,
    기존 필드는 그대로 — additive). 알파(`cli alpha-report`)는 우리 곡선과 지수
    곡선의 차이인데, 지수 쪽을 일봉 파케이에만 의존하면 백필이 끊기거나 파티션이
    유실된 날의 알파가 영원히 계산 불능이 된다. 자본 곡선 행이 스스로 비교 대상을
    들고 있으면 알파 계산이 자립한다."""
    import json as _json
    import sys
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from quant.adapters.env import REPO_ROOT
    from quant.control.alpha import BENCHMARKS
    from quant.core.models import market_of_symbol

    load_settings()
    market = args.market
    tz = ZoneInfo("Asia/Seoul") if market == "KR" else ZoneInfo("America/New_York")
    today = _dt.now(tz).date().isoformat()

    state = REPO_ROOT / "data" / "state"
    try:
        portfolio = _json.loads((state / "portfolio.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"자본 스냅샷 불가 — portfolio.json 읽기 실패: {type(e).__name__}: {e}")
        raise SystemExit(1)

    positions = {
        sym: p for sym, p in (portfolio.get("positions") or {}).items()
        if float(p.get("qty", 0) or 0) > 0
    }

    bench_symbol = BENCHMARKS[market]

    # 벤치마크는 포지션이 하나도 없는 날에도 조회한다 — 그날 지수 등락은 우리가
    # 쉬었다는 사실과 무관하게 기록돼야 알파 시계열에 구멍이 안 생긴다.
    quotes: dict[str, float] = {}
    live_usd_krw: float | None = None
    from quant.apps.assembly import MissingCredentials, build_toss_client
    try:
        client = build_toss_client()
        rows = client.prices(sorted({*positions, bench_symbol}))
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            price = row.get("price") or row.get("lastPrice") or row.get("close")
            try:
                if price is not None and float(price) > 0:
                    quotes[row["symbol"]] = float(price)
            except (TypeError, ValueError):
                continue
        # 환율도 같은 클라이언트로 실조회한다(cmd_strategy_pnl 과 동일 경로).
        # 별도 try — 환율 실패가 방금 받은 시세까지 "조회 실패"로 오인되게 하지 않는다.
        try:
            live_usd_krw = float(client.usd_krw())
        except Exception as e:  # noqa: BLE001 — 환율 실패는 폴백 사유일 뿐
            print(f"환율 조회 실패 — 고정 폴백: {type(e).__name__}: {e}", file=sys.stderr)
    except (MissingCredentials, Exception) as e:  # noqa: BLE001 — 시세 실패가 곡선 점을 잃게 하지 않는다(저하 기록)
        print(f"시세 조회 실패 — 전 종목 평균단가 저하: {type(e).__name__}: {e}", file=sys.stderr)

    # 환율: 실조회 우선, 실패 시 보수 고정값 — 장부 관례. 2026-09-02 이전엔 주석만
    # "실조회 우선"이라 적혀 있고 코드는 항상 고정 1,500원을 썼다. US 자산이 있는
    # 날의 총자산이 조용히 몇 % 틀어지므로 폴백을 썼으면 그 사실을 남긴다.
    from quant.core.fx import FixedFxProvider
    if live_usd_krw is not None and live_usd_krw > 0:
        fx = FixedFxProvider(live_usd_krw)
        fx_source = "live"
    else:
        fx = FixedFxProvider()
        fx_source = f"fallback:fixed:{fx.rate:g}"
        logger.warning(
            "USD/KRW 실조회 실패 — 고정 폴백 환율 %s 원 사용(자본 곡선 total_krw 가 "
            "그만큼 틀어진다, fx_source=%s)", format(fx.rate, ",.0f"), fx_source,
        )
    from quant.core.portfolio.portfolio import to_krw

    degraded = []
    # cash_usd(2026-09-01, PaperBroker 통화 분리 지갑): dual_currency=True에서
    # US 체결대금이 이 별도 필드에 쌓인다 — 안 더하면 총자산이 USD 현금만큼
    # 누락된다. 구버전 portfolio.json(필드 없음)은 0.0으로 안전하게 폴백된다
    # (Portfolio.load_or_init과 동일한 하위호환 원칙).
    total_krw = float(portfolio.get("cash", 0.0)) + to_krw(
        float(portfolio.get("cash_usd", 0.0)), "US", fx)
    for sym, p in positions.items():
        qty = float(p.get("qty", 0) or 0)
        price = quotes.get(sym)
        if price is None:
            price = float(p.get("avg_cost", 0) or 0)
            degraded.append(sym)
        total_krw += to_krw(qty * price, market_of_symbol(sym), fx)

    # 전략별 장부 평가액 — books 의 equity_krw 와 같은 산식(현금 + 마크 평가).
    books_equity: dict[str, float] = {}
    try:
        books = _json.loads((state / "strategy_books.json").read_text(encoding="utf-8"))
        for sid, book in (books.get("books") or {}).items():
            eq = float(book.get("cash_krw", 0.0))
            for sym, pos in (book.get("positions") or {}).items():
                qty = float(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                price = quotes.get(sym) or float(pos.get("avg_cost", 0) or 0)
                eq += to_krw(qty * price, pos.get("market") or market_of_symbol(sym), fx)
            books_equity[sid] = round(eq, 2)
    except (OSError, ValueError):
        pass  # 장부 없음(shared 모드) — 총자산만 기록

    # 벤치마크 종가 — 시세가 없으면 로컬 일봉의 마지막 종가로 저하한다. 둘 다
    # 없으면 None 을 적는다(알파는 그날을 건너뛴다 — 없는 지수를 지어내지 않는다).
    bench_close = quotes.get(bench_symbol)
    if bench_close is None:
        bench_close = _last_daily_close(REPO_ROOT / "data" / "history", bench_symbol)

    row = {
        "date": today,
        "market": market,
        "total_krw": round(total_krw, 2),
        "books": books_equity,
        "marked": sum(1 for s in positions if s in quotes),
        "degraded": degraded,
        # 2026-08-28 추가(additive) — 알파 계산의 자립을 위한 동반 기록.
        "benchmark_symbol": bench_symbol,
        "benchmark_close": None if bench_close is None else round(bench_close, 4),
        # 2026-09-02 추가(additive) — 이 점의 KRW 환산이 실환율인지 폴백인지.
        # 없으면(구행) 그 시절 고정 1,500원이라고 읽어야 한다.
        "fx_source": fx_source,
        "usd_krw": round(fx.usd_krw(), 4),
        "recorded_at": _dt.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
    }
    out = REPO_ROOT / "data" / "ledger" / "equity_curve.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(row, ensure_ascii=False) + "\n")
    print(f"자본 곡선 기록: {today} {market} 총 {total_krw:,.0f}원 "
          f"(마크 {row['marked']} / 저하 {len(degraded)}) 전략 장부 {len(books_equity)}개 "
          f"벤치마크 {bench_symbol}="
          f"{'없음' if bench_close is None else format(bench_close, ',.2f')}")

    # 하트비트 — cmd_experiments 와 같은 관례(job_findings 가 조용한 죽음을 잡게 함).
    try:
        from quant.adapters.kv import make_kv
        from quant.control.opstate import record_run

        record_run(make_kv(), "equity-snapshot", ok=True,
                  detail=f"{market} total_krw={total_krw:.0f} degraded={len(degraded)}")
    except Exception:  # noqa: BLE001 — 상태 기록 실패가 자본 곡선 기록을 막으면 안 된다
        pass


def cmd_performance(args: argparse.Namespace) -> None:
    """자본 곡선 → 성과 요약 (gs-quant 의 econometrics 상당, `core/timeseries`).

    **하루에 점 하나다**(2026-09-02 수정). 자본 곡선 원장은 KR·US 세션 마감마다
    행을 남기므로 같은 날짜에 두 행이 있고, 예전엔 그 둘을 각각 한 점으로 세어
    수익률 시계열의 길이가 실제 거래일의 2배가 됐다. 그 상태로 √252 연율화를
    하면 변동성·샤프가 √2 만큼 과소평가된다 — 같은 날짜는 **마지막 기록**만
    쓴다(원장 관례: 재실행은 append이고 읽는 쪽이 마지막만 쓴다).

    점 5개 미만이면 곡선별로 "표본 부족"을 출력한다 — 이 숫자로 아무것도
    판단하지 마라."""
    import json as _json
    from quant.adapters.env import REPO_ROOT
    from quant.core.timeseries import performance_summary

    path = REPO_ROOT / "data" / "ledger" / "equity_curve.jsonl"
    if not path.exists():
        print("자본 곡선 원장 없음 — `cli equity-snapshot` 이 아직 안 돌았다")
        return

    # 날짜 하나당 행 하나 — 원장에 쓰인 순서상 마지막 것이 이긴다(시장 구분 없이).
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = _json.loads(line)
        except ValueError:
            continue
        if not r.get("date"):
            continue
        latest[str(r["date"])] = r
    rows = sorted(latest.values(), key=lambda r: r["date"])

    def _curve(getter, name):
        vals = []
        for r in rows:
            v = getter(r)
            if v is not None and v > 0:
                vals.append(float(v))
        s = performance_summary(vals)
        if s["n_points"] < 5:
            print(f"[{name}] 점 {s['n_points']}개 — 표본 부족(5 미만), 판단 금지")
            return
        mdd = s["max_drawdown"]
        sharpe = s["sharpe_rf0"]
        vol = s["volatility"]
        print(f"[{name}] 점 {s['n_points']}개 · 누적 {s['total_return']*100:+.2f}% · "
              f"MDD {mdd*100:.1f}% · 변동성(연) {vol*100:.1f}% · "
              f"샤프(rf=0) {sharpe:+.2f}" if sharpe is not None else
              f"[{name}] 점 {s['n_points']}개 · 누적 {s['total_return']*100:+.2f}% · 샤프 계산 불가")

    _curve(lambda r: r.get("total_krw"), "총자산")
    sids = sorted({sid for r in rows for sid in (r.get("books") or {})})
    for sid in sids:
        _curve(lambda r, s=sid: (r.get("books") or {}).get(s), sid)
    print("\n※ 샤프는 무위험 이자율 0 가정 — 과대평가 방향. MDD 를 수익률보다 먼저 보라.")


def cmd_tearsheet(args: argparse.Namespace) -> None:
    """자본 곡선 원장(`data/ledger/equity_curve.jsonl`) → quantstats HTML 티어시트.

    `performance`(`cli performance`)가 gs-quant econometrics 상당의 숫자 요약을
    찍는다면, 이건 그 시각화 버전이다 — 어댑터(`quant.control.ledger.
    daily_equity_series_by_market`)로 시장별 일별 자본 시리즈를 뽑아
    `quant.research.report.render_html`(walk-forward 리포트가 이미 쓰는 그
    경로)에 태운다. `render_html`은 `WalkForwardResult.oos_equity`(pd.Series)만
    보므로, 다른 필드는 기본값(빈 값)인 채로 `oos_equity`만 채워 넘긴다.

    quantstats 미설치 시 `render_html`이 이미 우아하게 저하한다(경고 로그 +
    False 반환) — 여기서 별도 예외 처리를 하지 않는다."""
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.control.ledger import DEFAULT_EQUITY_CURVE_PATH, daily_equity_series_by_market
    from quant.research.report import render_html
    from quant.research.walkforward import WalkForwardResult

    path = REPO_ROOT / DEFAULT_EQUITY_CURVE_PATH
    series = daily_equity_series_by_market(path).get(args.market)
    if series is None or series.empty:
        print(f"[{args.market}] 표본 없음 — 자본 곡선 원장에 이 시장 행이 없다 "
              f"(`cli equity-snapshot` 이 아직 안 돌았다)")
        return

    n = len(series)
    # 임계 30: 연율화 지표(CAGR/샤프)는 짧은 표본에서 수학적으로 폭주한다 —
    # 실측: 5일 표본이 CAGR -98%/샤프 -8.7 로 나왔다(2026-08-29). 그 5일짜리가
    # 경고 없이 통과했던 임계(<5)를 문구("30일 이상 권장")와 일치시킨다.
    if n < 30:
        print(f"표본 부족({n}일) — 티어시트는 30일 이상 권장. 연율화 지표(CAGR/샤프)는 "
              f"이 표본에서 무의미하게 부풀려진다. 그래도 생성은 한다(정직 표기).")

    out_path = Path(args.out) if args.out else REPO_ROOT / "out" / f"tearsheet_{args.market}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    result = WalkForwardResult(oos_equity=series)
    ok = render_html(result, str(out_path), strategy_id=f"{args.market} 실전 자본곡선")
    if ok:
        print(f"티어시트 생성: {out_path} ({n}일 표본, {series.index.min().date()} ~ {series.index.max().date()})")
    else:
        print("quantstats 미설치 — HTML 생략(`uv sync --group research`로 설치)")


def _wrap_equity_points(path, market: str, on) -> list[dict]:
    """자본 곡선에서 `market`·`on 이하` 점만, (date) 중복은 마지막 기록이 이긴다
    (`cmd_performance` 와 같은 관례). 날짜 오름차순."""
    import json as _json

    if not path.exists():
        return []
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = _json.loads(line)
        except ValueError:
            continue
        d = str(r.get("date") or "")
        if not d or r.get("market") != market or d > on.isoformat():
            continue
        latest[d] = r
    return [latest[d] for d in sorted(latest)]


def _wrap_spread_rows(root, on) -> list[dict]:
    """6절(체결 비용) 재료 — `data/ledger/spread.jsonl`(spread_sample.sh가 10분마다
    수집) 중 `on` 날짜(ts 앞 10자리, ledger.py의 날짜 문자열 비교 관례와 동일)만.
    파일이 없으면 빈 리스트 — 6절이 "표본 없음"으로 정직하게 답한다."""
    import json as _json

    path = root / "data" / "ledger" / "spread.jsonl"
    if not path.exists():
        return []
    today = on.isoformat()
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = _json.loads(line)
        except ValueError:
            continue
        if str(r.get("ts") or "")[:10] == today:
            rows.append(r)
    return rows


def _wrap_issues(root, on) -> list[str]:
    """3절 재료 — **오늘 실제로 관측된 것만**. 없으면 빈 목록(호출부가 "없음").

    세 갈래를 본다: 운영 감시(ops_watch.log 판정 줄), 잡 하트비트(opstate →
    health.job_findings 의 alert), 워치독 상태 파일. 셋 다 이미 있는 계측이다 —
    이 리포트를 위해 새로 수집하지 않는다(수집이 늘면 리포트가 장애 원인이 된다).
    """
    today = on.isoformat()
    out: list[str] = []

    # (1) 운영 감시 — 오늘 날짜의 판정 줄만. 형식은 ops_watch.sh 의 log() 그대로.
    try:
        lines = (root / "data" / "ops_watch.log").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        if not ln.startswith(f"[{today} "):
            continue
        if "알림 전송 실패" in ln:
            out.append(f"운영 감시 이상 — 텔레그램 발송 실패(다음 주기 재시도): {ln.strip()}")
        elif "알림 전송" in ln:
            out.append(f"운영 감시 이상 판정 — 텔레그램 알림 전송됨: {ln.strip()}")

    # (2) 잡 하트비트 — alert 만(unknown 은 "모른다"라 이상으로 세지 않는다).
    try:
        from quant.adapters.kv import make_kv
        from quant.control import health as H
        from quant.control.opstate import snapshot

        jobs = ["collect:KR", "collect:US", "report:KR", "report:US", "ingest", "backup",
                "deepdive:KR", "deepdive:US", "close-report", "report_close:KR",
                "ops-judge", "experiments", "equity-snapshot"]
        for f in H.job_findings(snapshot(make_kv(), jobs)):
            if f.level == H.ALERT:
                out.append(f"잡 실패 — {f.detail} (조치: 자동 재시도 없음, 크론 로그 확인 필요)")
    except Exception:  # noqa: BLE001 — 상태 저장소 부재가 리포트를 막지 않는다
        pass

    # (3) 워치독 — 상태 파일이 있고 오늘 갱신됐으면 발동한 것이다(watchdog.sh 는
    # 회복하면 지운다). 파일 내용이 곧 장애 종류다.
    state = root / "data" / "state" / "watchdog.state"
    try:
        if state.exists():
            mtime = datetime.fromtimestamp(state.stat().st_mtime).date()
            if mtime == on:
                kind = state.read_text(encoding="utf-8").strip() or "종류 불명"
                out.append(f"워치독 발동 — {kind} (조치: 자동 알림 전송, 미해결 시 상태 파일 유지)")
    except OSError:
        pass

    return out[:10]


NOTIFY_QUEUE_PATH = ("data", "notify_queue.jsonl")
NOTIFY_ARCHIVE_PATH = ("data", "ledger", "notify_queue_archive.jsonl")


def _wrap_queue_parse(line: str) -> dict | None:
    """큐 한 줄 → dict, 못 읽으면 None. 깨진 줄이 리포트를 죽이지 않는다(원장 관례).

    **읽는 쪽과 비우는 쪽이 같은 판정을 써야 한다** — 유효 줄만 세서 읽고 원시
    줄 수만큼 지우면, 깨진 줄 하나가 끼는 순간 마지막 알림이 안 지워져 다음
    리포트에 또 나온다."""
    import json as _json

    try:
        row = _json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) and row.get("text") is not None else None


def _wrap_queue_rows(raw: str) -> list[dict]:
    """큐 원문 → 유효한 줄만."""
    return [r for r in (_wrap_queue_parse(ln) for ln in raw.splitlines() if ln.strip())
            if r is not None]


def _wrap_deferred(root, on, consume: bool) -> list[dict]:
    """장중에 미뤄진 알림 — 알림 게이트(`server/scripts/lib/notify.sh`)가 쌓은 큐.

    줄 계약은 그 셸 파일이 정의한다: `{ts, source, text, level}`, ts 는
    `%Y-%m-%dT%H:%M:%S%z`(로컬 KST). **이 파일을 읽는 곳은 여기 하나다** — 안
    읽으면 미뤄진 알림이 영영 소유자에게 닿지 않는다.

    `consume=True`(오늘치 정규 실행)면 **큐 전체**를 읽는다. 날짜로 거르지
    않는 이유: 큐의 의미는 "지난 리포트 이후 억눌린 것"이지 "오늘 억눌린 것"이
    아니다. 날짜로 걸렀더니 KR 리포트(16:55 KST)와 다음 날 US 리포트(06:55 KST,
    시장 기준일이 전날)가 **같은 KST 날짜를 둘 다 집어** 오전분이 두 번 나왔다.
    비우는 쪽(`_wrap_consume_queue`)이 중복을 없애는 유일한 장치다.

    `consume=False`(`--date` 백필)면 큐+아카이브에서 **그 날짜 줄만** 읽고
    비우지 않는다 — 과거를 다시 그리는 실행이 오늘 몫을 삼키면 안 된다.
    """
    queue = root.joinpath(*NOTIFY_QUEUE_PATH)
    if consume:
        try:
            return _wrap_queue_rows(queue.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return []

    prefix = on.isoformat()
    rows: list[dict] = []
    for path in (root.joinpath(*NOTIFY_ARCHIVE_PATH), queue):
        try:
            rows += _wrap_queue_rows(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return [r for r in rows if str(r.get("ts", "")).startswith(prefix)]


def _wrap_consume_queue(root, n_consumed: int) -> None:
    """읽은 줄 `n_consumed` 개를 아카이브로 옮기고 큐 앞에서 덜어낸다.

    **호출 시점이 계약이다** — HTML 을 성공적으로 쓴 **뒤**에만 부른다. 먼저
    비우면 렌더가 실패한 날 그날 알림이 통째로 증발한다.

    읽은 개수만큼만(앞에서부터) 덜어낸다: 읽은 뒤 이 순간까지 크론이 새로
    append 했을 수 있고, 파일을 통째로 비우면 그 줄들을 읽지도 않고 잃는다.

    락은 게이트(`_notify_enqueue`)와 같은 `flock` 을 같은 파일에 건다. 새
    inode 로 교체(tmp-replace)하지 **않는다** — 교체하면 락을 기다리던 appender
    가 사라질 옛 inode 에 쓰게 된다. 같은 inode 를 제자리에서 다시 쓴다.

    실패는 전부 삼킨다: 큐 정리 실패가 이미 만들어진 리포트를 되돌리지 않는다
    (최악의 경우 다음 리포트에 같은 줄이 한 번 더 나올 뿐이다).
    """
    if n_consumed <= 0:
        return
    queue = root.joinpath(*NOTIFY_QUEUE_PATH)
    archive = root.joinpath(*NOTIFY_ARCHIVE_PATH)
    try:
        import fcntl

        with queue.open("r+", encoding="utf-8", errors="replace") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass  # 락을 못 걸어도 진행 — 개인 서버의 크론은 초 단위로 겹치지 않는다
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
            # `n_consumed` 는 **유효 줄** 개수다 — 그 N번째 유효 줄까지 자른다
            # (사이에 낀 깨진 줄도 같이 아카이브로 간다).
            cut, seen = len(lines), 0
            for i, ln in enumerate(lines):
                if _wrap_queue_parse(ln) is not None:
                    seen += 1
                    if seen == n_consumed:
                        cut = i + 1
                        break
            moved, rest = lines[:cut], lines[cut:]
            if moved:
                archive.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("a", encoding="utf-8") as a:
                    a.write("\n".join(moved) + "\n")
            f.seek(0)
            f.write("\n".join(rest) + ("\n" if rest else ""))
            f.truncate()
    except (OSError, ImportError) as e:
        logger.warning("알림 큐 정리 실패(리포트는 이미 발행됨): %s: %s", type(e).__name__, e)


def _wrap_alpha_series(root, market: str, on) -> list[tuple]:
    """5절(지수 대비 성적) 재료 — 자본 곡선 원장 + 벤치마크 로컬 일봉을 읽어
    `control.alpha.alpha_series()`가 원하는 (날짜, 우리%, 지수%, 알파pp) 시퀀스로
    만든다. `on` 이후 미래 데이터는 쓰지 않는다(백필 재실행이 미래를 보면 안
    된다 — `_wrap_equity_points`와 같은 원칙).

    벤치마크 일봉은 `cmd_weekly_review._week_closes`와 같은 방식으로 로컬
    parquet(`data/history/<symbol>/1d/*/*.parquet`)만 읽는다 — 이 커맨드는
    "읽기만 한다"는 계약(`cmd_daily_wrap` docstring)이라 네트워크 조회를 새로
    추가하지 않는다."""
    import json as _json

    from quant.control import alpha as ALPHA

    eq_path = root / "data" / "ledger" / "equity_curve.jsonl"
    equity_rows: list[dict] = []
    if eq_path.exists():
        for line in eq_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if str(r.get("date") or "") <= on.isoformat():
                equity_rows.append(r)
    ours = [(d, ret) for d, m, ret in ALPHA.daily_returns(equity_rows) if m == market]

    bench_symbol = ALPHA.BENCHMARKS.get(market)
    bars: list[tuple] = []
    if bench_symbol:
        base = root / "data" / "history" / bench_symbol / "1d"
        if base.exists():
            import pandas as pd

            for part in sorted(base.glob("*/*.parquet")):
                try:
                    df = pd.read_parquet(part)
                except Exception:  # noqa: BLE001 — 깨진 파케이 하나가 리포트를 죽이지 않는다
                    continue
                for ts, row in df.iterrows():
                    d = ts.date() if hasattr(ts, "date") else None
                    if d and d.isoformat() <= on.isoformat():
                        bars.append((d, float(row["close"])))
    bench = ALPHA.benchmark_returns(bars)
    return ALPHA.alpha_series(ours, bench)


def _wrap_commits(root, on) -> list[str] | None:
    """4절 재료 — 그날 커밋 제목 줄(최대 10). git 을 못 읽으면 `None`(절 생략).

    빈 리스트("오늘 배포 없음")와 `None`("모른다")을 구분한다."""
    import shutil
    import subprocess
    from datetime import timedelta

    if not shutil.which("git") or not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "log", "--no-merges", "--format=%s",
             f"--since={on.isoformat()} 00:00",
             f"--until={(on + timedelta(days=1)).isoformat()} 00:00"],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()][:10]


def cmd_daily_wrap(args: argparse.Namespace) -> None:
    """장 마감 하루 요약 HTML 한 장 — 실적/지분/문제/변경/지수 대비 성적
    (2026-08-28 소유자 지시, 지수 대비 성적 절은 2026-08-29 통합).

    이 명령은 **읽기만** 한다: 거래 원장·자본 곡선·포트폴리오·종목명 캐시·ops
    로그·git. 시세 조회도 LLM 도 없다 — 마감 후 파일 하나를 만드는 일이
    네트워크에 의존하면 네트워크가 나쁜 날 하루가 통째로 기록되지 않는다.

    출력: `out/YYYY/MM/DD/{market}_wrap.html` 경로를 첫 줄에, 텔레그램 캡션을
    `CAPTION:` 접두로 둘째 줄에 찍는다(ai_trader.sh 의 `AI_WATCH:` 와 같은 관례)."""
    import json as _json
    from datetime import date as _date
    from zoneinfo import ZoneInfo

    from quant.adapters.env import REPO_ROOT
    from quant.apps.assembly import _load_kr_etf, _load_symbol_names
    from quant.control import daily_wrap as DW
    from quant.control.ledger import (
        ab_pairs_from_config, load_trades, round_trips, session_pnl_summary,
        session_window, trades_in_session,
    )
    from quant.core.models import market_of_symbol

    market = args.market
    tz = ZoneInfo("Asia/Seoul") if market == "KR" else ZoneInfo("America/New_York")
    on = _date.fromisoformat(args.date) if args.date else datetime.now(tz).date()
    root = REPO_ROOT

    trades = load_trades(root / "data" / "state" / "trades.jsonl")
    pnl = session_pnl_summary(trades, market, on)
    session_trades = trades_in_session(trades, market, on)
    start, end = session_window(market, on)
    all_trips = round_trips(trades)
    trips = DW.trips_closed_between(all_trips, start, end)

    try:
        portfolio = _json.loads(
            (root / "data" / "state" / "portfolio.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        portfolio = {}
    # 이 리포트는 시장 하나를 다룬다 — 다른 시장 보유를 섞으면 통화가 섞인다.
    positions = {
        sym: p for sym, p in (portfolio.get("positions") or {}).items()
        if market_of_symbol(sym) == market
    }

    # `--date` 백필은 큐를 소비하지 않는다 — 과거를 다시 그리는 실행이 오늘
    # 몫을 삼키면 그날 알림이 소유자에게 닿지 않는다.
    consume_queue = args.date is None
    deferred = _wrap_deferred(root, on, consume=consume_queue)

    sections = DW.build_sections(
        market=market, on=on, pnl=pnl, trips=trips,
        equity_points=_wrap_equity_points(
            root / "data" / "ledger" / "equity_curve.jsonl", market, on),
        positions=positions, session_trades=session_trades,
        names=_load_symbol_names(root / "data" / "state" / "symbol_names.json"),
        issues=_wrap_issues(root, on), commits=_wrap_commits(root, on),
        deferred=deferred, alpha_series=_wrap_alpha_series(root, market, on),
        spread_rows=_wrap_spread_rows(root, on),
        kr_etf=_load_kr_etf(root / "data" / "state" / "kr_etf.json"),
        # A/B 갈래(2026-09-03)는 **누적** 트립으로 잰다 — 하루치로는 양쪽 다
        # 30건에 한참 못 미쳐 매일 "판단 불가"만 찍힌다.
        all_trips=all_trips, ab_bases=ab_pairs_from_config(load_settings().raw),
    )

    out_path = (root / "out" / f"{on.year:04d}" / f"{on.month:02d}" / f"{on.day:02d}"
                / f"{market}_wrap.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(DW.render_html(sections), encoding="utf-8")

    # 큐 비우기는 **파일을 쓴 뒤**다 — 순서가 곧 계약이다(_wrap_consume_queue).
    if consume_queue:
        _wrap_consume_queue(root, len(deferred))

    print(out_path)
    print(f"CAPTION: {DW.caption_line(sections)}")


def cmd_weekly_review(args: argparse.Namespace) -> None:
    """주간 재검토 세션(2026-08-26 소유자 지시) — 토요일 아침, 양시장 마감 후.

    한 주의 종결 매매·손해 패턴·주간 장 흐름·점수 적중률(기록 vs 실제)·자본
    변화를 결정론으로 재계산해 출력한다. 발송은 쉘(weekly_review.sh) 몫.

    점수 적중률의 '다음 거래일 등락'은 로컬 일봉 파케이에서만 읽는다 —
    표본이 안 되는 종목은 정직하게 빠진다."""
    import json as _json
    from datetime import date as _date, timedelta
    from pathlib import Path

    import pandas as pd

    from quant.adapters.env import REPO_ROOT
    from quant.control.ledger import load_trades, round_trips
    from quant.control.symbol_log import accuracy_join, load_scores
    from quant.control.tca import join_intents_fills, slippage_bps, tca_summary
    from quant.control.warehouse import read_jsonl
    from quant.control.weekly_review import (
        loss_patterns, week_range, weekly_index_flow, weekly_review_text,
        weekly_strategy_stats,
    )

    root = REPO_ROOT
    today = _date.today()
    start, end = week_range(today - timedelta(days=1))  # 토요일 실행 → 막 끝난 주

    raw_trades = load_trades(ledger_state_path())
    trips = round_trips(raw_trades)
    stats = weekly_strategy_stats(trips, start, end)
    losses = loss_patterns(trips, start, end)

    # 슬리피지 TCA — 주문 의도(신호 시점 가격) vs 실제 체결가. intent 로그가
    # 오늘 가격을 안 남기므로 join_intents_fills가 표본 0을 내고 tca_summary가
    # None을 반환하는 게 정상(quant/control/tca.py 모듈 docstring 참고).
    intents = read_jsonl(root / "data" / "state" / "order_intents.jsonl")
    tca = tca_summary(slippage_bps(join_intents_fills(intents, raw_trades)), start, end)

    # 주간 지수 흐름 — 로컬 1d 파케이(069500=KOSPI200 프록시, QQQ).
    def _week_closes(symbol: str) -> list[float]:
        base = root / "data" / "history" / symbol / "1d"
        closes: list[float] = []
        for part in sorted(base.glob("*/*.parquet")) if base.exists() else []:
            try:
                df = pd.read_parquet(part)
            except Exception:  # noqa: BLE001
                continue
            for ts, row in df.iterrows():
                d = ts.date() if hasattr(ts, "date") else None
                if d and start <= d <= end:
                    closes.append(float(row["close"]))
        return closes

    index_flow = weekly_index_flow({
        "KOSPI200(069500)": _week_closes("069500"),
        "QQQ": _week_closes("QQQ"),
    })

    # 점수 적중률 — 지난주 기록 × 다음 거래일 등락(로컬 1d).
    score_rows = [r for r in load_scores(root / "data" / "ledger" / "symbol_scores.jsonl")
                  if start.isoformat() <= (r.get("date") or "") <= end.isoformat()]
    nxt: dict[tuple, float] = {}
    by_symbol: dict[str, list] = {}
    for r in score_rows:
        by_symbol.setdefault(r["symbol"], [])
    for symbol in by_symbol:
        base = root / "data" / "history" / symbol / "1d"
        seq: list[tuple[str, float]] = []
        for part in sorted(base.glob("*/*.parquet")) if base.exists() else []:
            try:
                df = pd.read_parquet(part)
            except Exception:  # noqa: BLE001
                continue
            for ts, row in df.iterrows():
                seq.append((str(ts)[:10], float(row["close"])))
        seq.sort()
        for i in range(1, len(seq)):
            prev_d, prev_c = seq[i - 1]
            d, c = seq[i]
            if prev_c > 0:
                nxt[(prev_d, symbol)] = (c / prev_c - 1) * 100
    score_accuracy = accuracy_join(score_rows, nxt)

    # 자본 주간 변화 — equity_curve 원장.
    equity_delta = None
    eq_path = root / "data" / "ledger" / "equity_curve.jsonl"
    if eq_path.exists():
        pts = []
        for line in eq_path.read_text(encoding="utf-8").splitlines():
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if start.isoformat() <= (r.get("date") or "") <= end.isoformat():
                pts.append(r)
        pts.sort(key=lambda r: (r["date"], r.get("recorded_at") or ""))
        if len(pts) >= 2:
            a, b = float(pts[0]["total_krw"]), float(pts[-1]["total_krw"])
            if a > 0:
                equity_delta = {"start": a, "end": b, "pct": (b / a - 1) * 100}

    print(weekly_review_text(start, end, index_flow, stats, losses,
                             score_accuracy, equity_delta, tca))

    try:
        from quant.adapters.kv import make_kv
        from quant.control.opstate import record_run

        record_run(make_kv(), "weekly-review", ok=True,
                  detail=f"trips={losses.get('n_week', 0)}")
    except Exception:  # noqa: BLE001
        pass


def cmd_experiments(args: argparse.Namespace) -> None:
    """자동 판정 — "바꿨다 → 쌓인다 → 판정이 온다"의 마지막 칸 (2026-08-24).

    매일 돈다. 하는 일 셋:
      1. `config/settings.yaml`의 전략 파라미터 지문을 찍어 바뀐 것만 원장에 남긴다
         (사람이 기록하지 않는다 — 자리를 비우면 사람 규율은 반드시 끊긴다).
      2. 표본이 찬 변경에 대해 이중차분(DiD) 판정을 낸다 — 대조군은 같은 기간
         파라미터가 안 바뀐 전략들이라 장세 몫이 상쇄된다.
      3. 표본이 충분한데 실현 엣지가 유의하게 음수인 전략을 경보한다.

    **판정할 게 없으면 아무것도 출력하지 않는다**(exit 0). 크론이 stdout 이
    비어 있으면 텔레그램을 보내지 않는다 — 매일 "아직 모릅니다"를 보내면 사람이
    안 읽고, 안 읽는 알림은 진짜 경보까지 같이 묻는다.

    설정 파일은 **읽기만 한다** — 판정만 하고 반영은 사람이 한다(거버너 층 0과
    같은 원칙: 사이징·전략 on/off 는 자동화하지 않는다).
    """
    import sys as _sys
    from datetime import date as _date

    from quant.adapters.env import REPO_ROOT
    from quant.control.experiments import (
        daily_report, load_changes, record_death_watch, record_fingerprints,
    )
    from quant.control.ledger import load_trades, round_trips

    settings = load_settings()
    changes_path = REPO_ROOT / "data" / "ledger" / "param_changes.jsonl"
    death_watch_path = REPO_ROOT / "data" / "ledger" / "death_watch.jsonl"
    today = _date.today()

    added = record_fingerprints(
        (settings.raw.get("strategies") or {}), today, changes_path,
    )
    if added and args.verbose:
        for a in added:
            kind = "기준선" if a["baseline"] else "변경 감지"
            print(f"[{kind}] {a['strategy']} {a['fingerprint']}", file=_sys.stderr)

    trips = round_trips(load_trades(ledger_state_path()))
    msg, settled = daily_report(trips, load_changes(changes_path), today)

    # 사망 판정 지속 감시 스냅샷(작업2, 2026-09-02) — 판정/반영과 분리된
    # 순수 기록 단계다. governor-apply(주간, --live 게이트)가 이 원장을 읽어
    # "K거래일 연속 사망"인 전략의 자동 비활성을 심사한다 — 이 커맨드는 여전히
    # 읽고 기록만 한다(위 docstring "판정만, 반영은 사람/governor" 원칙 그대로).
    record_death_watch(trips, today, death_watch_path)

    # 하트비트 — **이 잡이 멈춘 것과 "판정할 게 없는 것"은 겉보기가 같다**
    # (둘 다 텔레그램 무소식). 매 실행 기록해 규칙 기반 감시(cli health 의
    # job_findings)가 조용한 죽음을 대신 잡게 한다. ok=True 는 "판정이
    # 좋았다"가 아니라 "이 잡이 죽지 않고 끝냈다"다(ops-judge 와 같은 의미).
    try:
        from quant.adapters.kv import make_kv
        from quant.control.opstate import record_run

        record_run(make_kv(), "experiments", ok=True,
                  detail=f"verdicts={len(settled)} changes={len(added)}")
    except Exception:  # noqa: BLE001 — 상태 기록 실패가 판정 출력을 막으면 안 된다
        pass

    if msg is None:
        if args.verbose:
            print("판정할 것 없음 — 조용히 대기", file=_sys.stderr)
        return
    print(msg)
    if args.verbose and settled:
        print(f"확정된 실험: {', '.join(settled)}", file=_sys.stderr)


def cmd_session_pnl(args: argparse.Namespace) -> None:
    """세션(정규장) 마감 후 실화폐 손익 리포트 — 실현손익/수수료/전략별·종목별
    내역은 원장(오프라인)에서, 미실현손익(보유분 평가)은 현재 포트폴리오+실시세로.

    스코어보드(bps/승률, 통화 무관 축)와 달리 이건 "이번 세션에 실제 얼마"를
    시장별 통화(KR=원, US=달러)로 보여준다 — 절대 섞지 않는다(2026-08-13
    사용자 원칙). 출력은 stdout — server/scripts/session_pnl.sh가 텔레그램으로 쏜다."""
    import json as _json
    from datetime import date as _date
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from quant.control.ledger import load_trades, session_pnl_summary, session_pnl_text
    from quant.core.models import market_of_symbol

    load_settings()  # .env/.env.local 로드 — 미실현손익 조회용 Toss 자격증명이 여기서 온다

    market = args.market
    tz = ZoneInfo("Asia/Seoul") if market == "KR" else ZoneInfo("America/New_York")
    today = datetime.now(tz).date()
    on = _date.fromisoformat(args.date) if args.date else today

    # parents[1] 은 `quant/` 다 — 저장소 루트가 아니다. 이 착오로 세션 손익이
    # `quant/data/state/trades.jsonl`(존재하지 않는 경로)을 읽어, 08-18 KR 에
    # 실제 체결 16건이 있는데도 매일 15:35 텔레그램에 "이 세션에 체결된 거래 없음"
    # 을 보내고 있었다(2026-08-19 발견). regime_state_path/ledger_state_path 가
    # 같은 착오로 이미 두 번 고쳐졌다 — 루트를 세는 곳은 REPO_ROOT 하나뿐이다.
    from quant.adapters.env import REPO_ROOT

    repo_root = REPO_ROOT
    ledger_path = repo_root / "data" / "state" / "trades.jsonl"
    trades = load_trades(ledger_path)
    summary = session_pnl_summary(trades, market, on)
    print(session_pnl_text(summary))
    print()

    def _fmt(v: float) -> str:
        return f"{v:,.0f}원" if market == "KR" else f"${v:,.2f}"

    # --- 미실현손익(보유 중 평가) — 반드시 "지금" 포트폴리오+시세로만 계산할 수
    # 있다. 과거 --date 요청은 그 시점 포지션을 원장에서 재구성하지 않는 한
    # 알 수 없으므로 추측하지 않고 생략한다("모르면 0으로 위장하지 않는다" 원칙).
    if on != today:
        print("📦 보유 중 · 평가 손익: 과거 세션이라 생략 (현재 포트폴리오 상태만 확인 가능, 그 시점은 재구성 불가)")
        return

    portfolio_path = repo_root / "data" / "state" / "portfolio.json"
    if not portfolio_path.exists():
        print("📦 보유 중 · 평가 손익: 포트폴리오 상태 파일 없음 (data/state/portfolio.json)")
        return
    try:
        portfolio = _json.loads(portfolio_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        print(f"📦 보유 중 · 평가 손익: 포트폴리오 상태 읽기 실패 ({type(e).__name__}: {e})")
        return

    positions = {
        sym: p for sym, p in (portfolio.get("positions") or {}).items()
        if market_of_symbol(sym) == market and float(p.get("qty", 0) or 0) > 0
    }
    if not positions:
        print("📦 보유 중 · 평가 손익: 보유 종목 없음")
        return

    from quant.apps.assembly import MissingCredentials, build_toss_client

    try:
        client = build_toss_client()
    except MissingCredentials as e:
        print(f"📦 보유 중 · 평가 손익: 생략 — Toss 자격증명 없음 ({e})")
        return

    try:
        rows = client.prices(list(positions))
    except Exception as e:  # noqa: BLE001 — 시세 조회 실패는 "생략" 사유일 뿐, CLI를 죽이면 안 된다
        print(f"📦 보유 중 · 평가 손익: 생략 — 시세 조회 실패 ({type(e).__name__}: {e})")
        return

    quotes: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        price = row.get("price") or row.get("lastPrice") or row.get("close")
        try:
            if price is not None and float(price) > 0:
                quotes[row["symbol"]] = float(price)
        except (TypeError, ValueError):
            continue

    print("📦 보유 중 · 평가 손익 (현재 시점 마크 — 세션 마감 시점 마크가 아님)")
    total = 0.0
    missing: list[str] = []
    for sym, p in positions.items():
        qty = float(p.get("qty", 0) or 0)
        avg = float(p.get("avg_cost", 0) or 0)
        mark = quotes.get(sym)
        if mark is None:
            missing.append(sym)
            print(f"  {sym}: 시세 조회 실패 — 합계에서 제외")
            continue
        pnl = (mark - avg) * qty
        total += pnl
        pct = (mark / avg - 1) * 100 if avg else 0.0
        print(f"  {sym}: {_fmt(pnl)} ({pct:+.2f}%, 수량 {qty:g} · 평단 {avg:,.2f} · 현재가 {mark:,.2f})")
    suffix = f" (시세실패 {len(missing)}건 제외)" if missing else ""
    print(f"  평가손익 합계 {_fmt(total)}{suffix}")


def cmd_manual_recs(args: argparse.Namespace) -> None:
    """수동 계좌 추천 (2026-09-03 소유자 결정: 자동매매는 단타·스캘핑만).

    오버나이트/장기 보유가 전략 정의인 네 전략(frgn_accumulate/close_bet/
    overnight_drift/rsi2_dip)은 `config/settings.yaml`에서 비활성화됐다 — 그
    판단 로직은 `quant/analyze/manual_recs.py`(순수 analyze 평면, `quant/trade/`
    임포트 없음)로 옮겨져 여기서 텔레그램 추천으로만 나간다. 이 명령은 주문을
    내지 않는다.

    `--scorecard`면 원장 선정을 건너뛰고 producer `manual_rec_v1`의 D+5 적중률/
    평균bp만 찍는다(n<30이면 "판단 불가").

    출력은 stdout **하나**뿐이다 — `server/scripts/manual_recs.sh`가 그 전체를
    텔레그램 메시지로 그대로 보낸다(session_pnl.sh와 같은 관례: 텍스트 생성은
    여기서, 발송은 셸의 notify.sh에서). 그래서 진단 로그는 stderr로만 낸다."""
    import sys
    from datetime import date as _date

    from quant.adapters.env import REPO_ROOT
    from quant.analyze import manual_recs
    from quant.control import selections

    if args.scorecard:
        rows = selections.load(REPO_ROOT / "data" / "ledger" / "selections.jsonl")
        print(manual_recs.scorecard_text(rows))
        return

    if not args.market:
        print("--market {KR|US} 가 필요하다 (--scorecard 가 아니면)", file=sys.stderr)
        raise SystemExit(2)

    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Seoul") if args.market == "KR" else ZoneInfo("America/New_York")
    on = _date.fromisoformat(args.date) if args.date else datetime.now(tz).date()

    recs = manual_recs.build_recs(REPO_ROOT, args.market, on)
    print(manual_recs.render_telegram_message(recs, args.market))

    if args.dry_run:
        print(f"(dry-run — 선정 원장 기록 생략, 후보 {len(recs)}건)", file=sys.stderr)
        return

    added = manual_recs.write_recs(recs, REPO_ROOT, on.isoformat())
    print(f"선정 원장 {added}건 추가 (producer=manual_rec_v1, 후보 {len(recs)}건)", file=sys.stderr)


def cmd_strategy_pnl(args: argparse.Namespace) -> None:
    """전략별 독립 명목계좌(각 1,000만원) 성과 요약 — 평가금액·수익률·실현/미실현손익·
    보유종목·거래수/승률을 전략별로 나눠 보여준다.

    session-pnl/scoreboard는 계좌 전체를 하나로 뭉치지만, 이건 "어느 전략이
    이기고 있나"에 답한다(2026-08-19 사용자 요청). 장부(data/state/
    strategy_books.json)는 리스크 평면(별도 작업자)이 쓴다 — 여기선 읽기만.
    시세 조회는 session-pnl과 같은 패턴(Toss 실시세, 자격증명 없으면 생략).
    출력은 stdout — server/scripts/session_pnl.sh가 텔레그램으로 쏜다."""
    from pathlib import Path

    from quant.control.ledger import load_trades, round_trips
    from quant.control.strategy_books import (
        MESSAGE_SEPARATOR,
        load_strategy_books,
        strategy_books_messages,
    )

    load_settings()  # .env/.env.local — 미실현손익 조회용 Toss 자격증명

    # 루트는 REPO_ROOT 하나로만 센다 — parents[1] 은 `quant/` 라서 장부가 있는데도
    # "장부 파일 없음"이 찍혔다(2026-08-19 배포 검증에서 발견, session-pnl 과 동일 착오).
    from quant.adapters.env import REPO_ROOT

    repo_root = REPO_ROOT
    books_data = load_strategy_books(repo_root / "data" / "state" / "strategy_books.json")
    trades = load_trades(repo_root / "data" / "state" / "trades.jsonl")
    trips = round_trips(trades)

    quotes: dict[str, float] = {}
    usd_krw = 1500.0
    quote_error: str | None = None

    symbols = sorted({
        sym
        for book in books_data["books"].values()
        for sym, p in (book.get("positions") or {}).items()
        if float(p.get("qty", 0) or 0) != 0
    })
    if symbols:
        from quant.apps.assembly import MissingCredentials, build_toss_client

        try:
            client = build_toss_client()
            rows = client.prices(symbols)
            for row in rows or []:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                price = row.get("price") or row.get("lastPrice") or row.get("close")
                try:
                    if price is not None and float(price) > 0:
                        quotes[row["symbol"]] = float(price)
                except (TypeError, ValueError):
                    continue
            usd_krw = client.usd_krw()
        except MissingCredentials as e:
            quote_error = f"Toss 자격증명 없음 ({e}) — 미실현손익 생략"
        except Exception as e:  # noqa: BLE001 — 시세 조회 실패가 CLI를 죽이면 안 된다
            quote_error = f"시세 조회 실패 ({type(e).__name__}: {e})"

    # 텍스트 생성(순수)과 발송(I/O)을 분리한다(2026-08-19 Phase C) — 여기서는
    # 메시지 리스트를 만들 뿐이고, 전략마다 따로 보내는 건 호출부
    # (server/scripts/session_pnl.sh)의 몫이다. 메시지 사이는 MESSAGE_SEPARATOR로
    # 구분해 stdout에 한 번에 찍는다 — 그 스크립트가 다시 잘라 하나씩 보낸다.
    messages = strategy_books_messages(books_data, trips, quotes, usd_krw, quote_error=quote_error)
    print(MESSAGE_SEPARATOR.join(messages), end="")


def regime_state_path():
    """국면 캐시 경로. 저장소 루트 기준(CWD 무관 — cron 이 어디서 불려도 같아야 한다).

    **`parents[1]` 은 `quant/` 였다.** 그래서 `quant/data/state/regime.json` 을 찾고
    없으니 조용히 neutral 로 떨어졌다 — 실제 파일은 `data/state/regime.json` 에 있다
    (2026-08-14). 이제 루트를 세는 곳은 `adapters.env.REPO_ROOT` 하나뿐이다.
    """
    from quant.adapters.env import REPO_ROOT

    return REPO_ROOT / "data" / "state" / "regime.json"


def ledger_state_path():
    """체결 원장 경로. 저장소 루트 기준.

    **같은 착오가 여기서 제일 비쌌다**: 스코어보드가 `quant/data/state/trades.jsonl`
    을 읽어 "종결된 트레이드가 아직 없음"을 출력했다. 실제 원장에는 종결 26건이
    있었고, 루트 CLAUDE.md 는 "숫자가 자본 배분을 결정한다"고 못 박고 있다.
    """
    from quant.adapters.env import REPO_ROOT

    return REPO_ROOT / "data" / "state" / "trades.jsonl"


def cmd_publish_performance(args: argparse.Namespace) -> None:
    """공개 포트폴리오 사이트용 성과 JSON을 `--out`에 쓴다.

    입력은 거래 원장(`trades.jsonl`) 하나 + `execution` 설정 비용 상수뿐 —
    종목/포지션/계좌 잔고 절대값은 출력에 없다. 계산 로직은 순수 함수
    `quant.control.performance.build_performance_payload`에 있다(이 함수는
    파일 I/O만 한다)."""
    import json as _json
    from pathlib import Path

    from quant.control.ledger import load_trades
    from quant.control.performance import build_performance_payload

    trades = load_trades(ledger_state_path())
    settings = load_settings()

    # 이식 시점(2026-09-01) 이월 보유(005930)의 평가액을 시드에 반영하려면 이
    # 스냅샷이 필요하다 — 없어도(다른 환경/구버전 원장) 현금만으로 정상 동작한다
    # (build_performance_payload가 알아서 폴백한다, 여기선 있으면 읽어 넘길 뿐).
    snapshot_path = ledger_state_path().parent / "real_account_snapshot.json"
    real_account_snapshot = (
        _json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else None
    )

    payload = build_performance_payload(
        trades, settings.execution, real_account_snapshot=real_account_snapshot,
        # `strategies[].enabled`(지금 켜져 있나)를 JSON 에 싣기 위해 설정 블록을
        # 넘긴다 — 파라미터·종목은 안 나간다(공개 안전 규칙, performance.py 참고).
        strategies_cfg=settings.strategies,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"성과 JSON 기록: {out_path} (체결 {payload['period']['total_fills']}건, "
          f"거래일 {payload['period']['sessions']}일, 전략 {len(payload['strategies'])}개)")


def cmd_backup(args: argparse.Namespace) -> None:
    """백업 번들 생성/대조. JSON 을 stdout 으로 — 셸 스크립트와 감시가 파싱한다.

    문제가 있으면 **종료코드 1** 이다. 크론이 성공으로 읽고 넘어가면 안 된다.
    """
    import json as _json
    import sys
    from datetime import datetime as _dt
    from pathlib import Path

    from quant.control.backup import create, manifest, read_manifest, regressions, verify

    if args.verify:
        problems = verify(args.verify)
        print(_json.dumps({"bundle": args.verify, "problems": problems},
                          ensure_ascii=False, indent=2))
        raise SystemExit(1 if problems else 0)

    out_dir = Path(args.out)
    # 지난 번들을 먼저 집어둔다 — 새 번들을 쓰고 나면 "가장 최근"이 바뀐다.
    previous = sorted(out_dir.glob("quant-*.tar.gz"))
    prev_manifest = {}
    if previous:
        try:
            prev_manifest = read_manifest(previous[-1])
        except Exception as e:  # noqa: BLE001 — 지난 번들이 깨졌어도 새 백업은 만든다
            print(f"경고: 지난 번들을 읽지 못해 회귀 검사를 건너뛴다 ({type(e).__name__})",
                  file=sys.stderr)

    # 초까지 넣는다 — 분 단위면 같은 분에 두 번 돌 때 자기 자신을 덮어쓰고,
    # 그러면 "지난 번들"이 방금 만든 번들이 되어 회귀 검사가 무력해진다.
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    bundle = out_dir / f"quant-{stamp}.tar.gz"
    stats = create(Path(args.root), bundle, extra=[Path(p) for p in (args.include or [])])

    # 회귀는 **경고가 아니라 실패**다. 망가진 소스를 그대로 백업하면 지난 백업까지
    # 덮어쓴다(보관 개수가 유한하므로).
    problems = regressions(manifest(Path(args.root)), prev_manifest) if prev_manifest else []
    stats["compared_to"] = str(previous[-1]) if previous else None
    stats["problems"] = problems
    # 운영 상태 기록 — 감시의 `backup` 항목이 이걸 읽는다. 없으면 "기록이 없다"만
    # 영원히 답한다(2026-08-13 감시 배포 때 드러난 계측 공백).
    try:
        from quant.adapters.kv import make_kv
        from quant.control.opstate import record_run

        record_run(make_kv(), "backup", ok=not problems,
                   detail=f"{stats['files']}파일 {stats['bytes']}바이트"
                          + (f" 문제 {len(problems)}건" if problems else ""))
    except Exception:  # noqa: BLE001 — 기록 실패가 백업을 죽이지 않는다
        pass
    print(_json.dumps(stats, ensure_ascii=False, indent=2))
    raise SystemExit(1 if problems else 0)


def cmd_seed_real(args: argparse.Namespace) -> None:
    """실계좌 스냅샷을 모의(paper) 상태에 이식하는 **일회성 제어 도구**.

    **엔진(quant.apps.cli paper)이 꺼져 있을 때만 실행할 것.** 돌고 있는
    루프가 사이클마다 portfolio.json을 읽고/쓰는 도중 이 도구가 같은 파일을
    덮어쓰면, 엔진이 보는 상태와 실제 디스크 상태가 어긋나는 레이스 컨디션이
    난다 — 이 도구는 그 경합을 스스로 막지 않는다(호출부 책임).

    (2026-09-01 소유자 지시) "모의 포트폴리오를 완전히 초기화하고, 실제 토스
    계좌 스냅샷을 이어받아 모의투자로 진행하라. 원화는 원화로, 달러는 달러로만
    (환전 금지). 그 안에서 전략대로 지분을 나눠라." — 오케스트레이터가 정리한
    처분:

    - 005930(삼성전자)은 보유를 유지하고 `frgn_accumulate` 전략 lot으로
      이관한다(그 전략이 지금도 이 종목 매집 신호를 갖고 있어 청산 규칙이
      계속 자연스럽게 관리한다).
    - 나머지 종목(009150/012450/GOOGL/NVDA/TSLA/GLDM/SOXL)은 임시 `legacy`
      lot으로 세팅한 뒤, 스냅샷 가격으로 즉시 매도 체결을 기록한다 — 기존
      PaperBroker 수수료 모델(KR 개별주 매도세 20bp, US SEC Fee+TAF 포함)을
      그대로 통과시켜 trades.jsonl에 정식 행으로 남긴다. reason에 "실계좌
      이식 정리" 마커를 남긴다.

    매도 대금은 PaperBroker의 통화 분리 지갑(dual_currency=True, paper.py
    참고)을 통해 KR 매도→KRW 풀, US 매도→USD 풀로만 들어간다 — 환전 코드는
    어디에도 없다.

    **주의**: `strategy_books.json`(전략별 독립 명목계정, capital_mode:
    per_strategy)의 `frgn_accumulate` 장부는 이 도구가 건드리지 않는다 —
    거기 기록된 005930 포지션/현금은 이 이식 이전의 시뮬레이션 이력 그대로
    남는다. 이 장부와 새로 이식된 실제 보유(6주)를 맞출지는 별도 판단이
    필요하다(과도한 자동 조정 대신 백업만 남기고 사람 판단에 맡긴다).
    """
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    import pandas as pd

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.execution.paper import PaperBroker
    from quant.apps.config import load_settings
    from quant.control.ledger import TradeLedgerSink
    from quant.core.fx import FixedFxProvider
    from quant.core.models import Order, Position, Quote, Side, market_of_symbol
    from quant.core.portfolio.portfolio import Portfolio

    KEEP_SYMBOL = "005930"
    KEEP_STRATEGY = "frgn_accumulate"
    LEGACY_STRATEGY = "legacy"
    REASON = "실계좌 이식 정리 — 소유자 지시 2026-09-01: 005930만 보유 유지, 나머지 정리"

    snapshot_path = Path(args.snapshot)
    snapshot = _json.loads(snapshot_path.read_text(encoding="utf-8"))

    holdings = {h["symbol"]: h for h in snapshot["holdings"]}
    if KEEP_SYMBOL not in holdings:
        raise SystemExit(f"스냅샷에 {KEEP_SYMBOL}이 없다 — 이관 대상을 다시 확인하라")

    # 심볼 형태(6자리 숫자=KR)로 추론한 시장이 스냅샷의 통화 표기와 어긋나면
    # 스냅샷이 이상하다는 신호다 — 조용히 넘기지 않고 멈춘다.
    market_of: dict[str, str] = {}
    for sym, h in holdings.items():
        inferred = market_of_symbol(sym)
        expected = "KR" if h["currency"] == "KRW" else "US"
        if inferred != expected:
            raise SystemExit(
                f"{sym}: 심볼 추론 시장({inferred}) != 스냅샷 통화 기준 시장({expected}, "
                f"currency={h['currency']!r}) — 스냅샷을 확인하라"
            )
        market_of[sym] = inferred

    state_dir = REPO_ROOT / "data" / "state"
    portfolio_path = state_dir / "portfolio.json"
    books_path = state_dir / "strategy_books.json"
    risk_day_path = state_dir / "risk_day.json"
    ledger_path = state_dir / "trades.jsonl"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backed_up: list[str] = []
    if not args.dry_run:
        # 삭제가 아니라 보존 — 이 도구는 엔진이 꺼져 있을 때만 쓰는 전제이므로
        # 백업은 "되돌릴 방법"이지 "동시 접근 방지"가 아니다.
        for p in (portfolio_path, books_path, risk_day_path):
            if p.exists():
                bak = p.with_name(f"{p.name}.pre_seed.{stamp}.bak")
                bak.write_bytes(p.read_bytes())
                backed_up.append(str(bak))

    settings = load_settings()
    execution_cfg = settings.execution
    fx = FixedFxProvider(float(snapshot["fx_usd_krw"]))

    now = datetime.now(timezone.utc)
    portfolio = Portfolio(
        cash=float(snapshot["buying_power_KRW"]["cashBuyingPower"]),
        cash_usd=float(snapshot["buying_power_USD"]["cashBuyingPower"]),
        state_path=None if args.dry_run else portfolio_path,
    )
    for sym, h in holdings.items():
        qty = float(h["qty"])
        avg_cost = float(h["avg_cost"])
        pos = Position(symbol=sym, qty=qty, avg_cost=avg_cost, opened_at=now)
        portfolio.positions[sym] = pos
        lot = pos.ensure_lot(KEEP_STRATEGY if sym == KEEP_SYMBOL else LEGACY_STRATEGY)
        lot["qty"] = qty
        lot["avg_cost"] = avg_cost

    class _SnapshotDataFeed:
        """스냅샷 가격만 answer하는 정적 DataFeed — 이 도구는 실시세를 조회하지
        않는다(스냅샷 가격 그대로 체결시키는 것이 목적)."""

        def quote(self, symbol: str) -> Quote | None:
            h = holdings.get(symbol)
            return None if h is None else Quote(symbol=symbol, ts=now, price=float(h["price"]))

        def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    broker = PaperBroker(
        data=_SnapshotDataFeed(), portfolio=portfolio,
        fee_bps=execution_cfg.get("fee_bps", 0.0), market_of=market_of, fx=fx,
        # 정리 매도는 스냅샷에 이미 찍힌 실제 가격을 그대로 체결가로 쓴다 —
        # 슬리피지 가정을 얹지 않는다(추정치를 실측 위에 덧씌우지 않는다).
        slippage_bps=0.0,
        kr_stock_sell_tax_bps=execution_cfg.get("kr_stock_sell_tax_bps", 0.0),
        # 스냅샷에는 ETF 여부 정보가 없다 — "모르면 개별주"(paper.py 기존
        # 원칙: 과대 비용이 과소 비용보다 정직하다).
        kr_etf_symbols=set(),
        us_sec_fee_bps=execution_cfg.get("us_sec_fee_bps", 0.0),
        us_sec_fee_min_usd=execution_cfg.get("us_sec_fee_min_usd", 0.0),
        us_taf_per_share=execution_cfg.get("us_taf_per_share", 0.0),
        us_taf_cap_usd=execution_cfg.get("us_taf_cap_usd", 0.0),
        us_free_commission_notional_usd=execution_cfg.get("us_free_commission_notional_usd", 0.0),
        dual_currency=True,
    )

    class _NullSink:
        def on_signal(self, signal) -> None: ...
        def on_fill(self, fill) -> None: ...
        def on_order(self, state) -> None: ...

    ledger = None if args.dry_run else TradeLedgerSink(_NullSink(), path=ledger_path)

    sell_fills = []
    for sym, h in holdings.items():
        if sym == KEEP_SYMBOL:
            continue
        order = Order(symbol=sym, side=Side.SELL, qty=float(h["qty"]),
                       strategy_id=LEGACY_STRATEGY, reason=REASON)
        state = broker.place_order(order)
        if ledger is not None:
            ledger.on_order(state)
            if state.fill is not None:
                ledger.on_fill(state.fill)
        sell_fills.append({
            "symbol": sym, "status": state.status.value, "filled_qty": state.filled_qty,
            "price": state.fill.price if state.fill else None,
            "fee": state.fill.fee if state.fill else None,
        })

    if not args.dry_run:
        portfolio.save()

    result = {
        "dry_run": bool(args.dry_run),
        "backed_up": backed_up,
        "cash_krw_final": portfolio.cash,
        "cash_usd_final": portfolio.cash_usd,
        "positions_remaining": {
            sym: {"qty": pos.qty, "avg_cost": pos.avg_cost}
            for sym, pos in portfolio.positions.items() if pos.qty > 0
        },
        "sell_fills_recorded": sell_fills,
    }
    print(_json.dumps(result, ensure_ascii=False, indent=2))


def cmd_health(args: argparse.Namespace) -> None:
    """운영 이상 점검 — JSON 을 stdout 으로. Phase 5.3.

    판정과 종료코드가 **세 갈래**다: `ok`=0, `alert`=1, `unknown`=2.
    "모른다"를 정상으로 합산하지 않는다 — 그게 이 저장소가 반복해서 다친 모양이다.

    감지 규칙은 전부 `quant.control.health` 의 순수 함수다. 여기는 I/O 만 한다:
    읽기 실패는 예외로 올리지 않고 `None` 으로 내려보내 `unknown` 이 되게 한다.
    """
    import json as _json
    import shutil
    import subprocess
    import sys
    from datetime import timedelta, timezone
    from pathlib import Path

    from zoneinfo import ZoneInfo

    from quant.adapters import olap as OLAP
    from quant.adapters.kv import make_kv
    from quant.adapters.olap import coverage
    from quant.control import health as H
    from quant.control.ledger import load_trades
    from quant.control.opstate import llm_stats, snapshot, stale_feeds
    # 봉 신선도 임계값은 **거래 평면의 상수를 그대로 쓴다.** 여기 숫자를 따로 적으면
    # 언젠가 갈라지고, 갈라진 쪽이 조용한 쪽이 된다. (apps 는 두 평면을 다 안다 —
    # control 이 trade 를 임포트하는 건 아키텍처 위반이므로 주입 지점이 여기다.)
    from quant.trade.regime.provider import STALE_DAILY_BARS_AFTER

    load_settings()
    root = Path(args.root)
    now = datetime.now(timezone.utc)
    _KST_TZ = ZoneInfo("Asia/Seoul")

    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _json_file(path: Path) -> dict | None:
        raw = _read(path)
        if raw is None:
            return None
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            return None

    def _run(cmd: list[str]) -> str | None:
        """명령이 없거나 실패하면 None — "이상 없음"이 아니라 "모른다"로 흐른다."""
        if not shutil.which(cmd[0]):
            return None
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    findings: list[H.Finding] = []
    kv = make_kv()

    # 작업·피드 — Redis 가 죽으면 둘 다 unknown 이 된다(빈 목록이 아니라).
    # deepdive:KR/US·close-report 는 일 1회 잡이라 신선도 창이 다르다 —
    # opstate.JOB_HEARTBEAT_TTL 에서 30시간으로 오버라이드된다(I1c). ops-judge(판단
    # 워치독, 2026-08-19)는 하루 2회 잡이라 20시간으로 오버라이드된다(opstate.py) —
    # **이 감시 자체가 조용히 죽는 경로**(크론 삭제·LLM 자격증명 만료로 계속 서브셸
    # 실패 등)를 기존 규칙 기반 감시가 잡게 하려는 것이다(판단 레이어가 스스로를
    # 지키지 못하므로 밑단 규칙이 대신 지킨다).
    jobs = ["collect:KR", "collect:US", "report:KR", "report:US", "ingest", "backup",
            "deepdive:KR", "deepdive:US", "close-report", "report_close:KR", "ops-judge",
            "experiments", "equity-snapshot"]
    findings += H.job_findings(snapshot(kv, jobs))
    # 현재 설정된 피드 이름을 주입한다 — 없으면 개편으로 사라진 옛 이름이 영구히
    # "죽은 피드"로 경보된다(2026-08-13 실측: 연합뉴스·한경). 로스터를 못 구하면
    # None 을 넘겨 걸러내지 않는다.
    try:
        from quant.collect.sources.feeds import NEWS_FEEDS
        roster = {m: list(feeds) for m, feeds in NEWS_FEEDS.items()}
    except Exception:  # noqa: BLE001 — 로스터 부재가 점검을 죽이지 않는다
        roster = {}
    for market in ("KR", "US"):
        healthy = kv.healthy()
        findings += H.feed_findings(market, stale_feeds(kv, market, now) if healthy else [],
                                   healthy, configured=roster.get(market))

    # 봉 — 라이브 사이징에 걸린 파일. coverage() 는 못 읽으면 None 이다.
    cov = coverage("QQQ", "1d", root / "data" / "history")
    findings += H.bar_findings({"QQQ 1d": cov.last_ts if cov else None},
                               now, STALE_DAILY_BARS_AFTER)

    # 봉 값 자체의 타당성 — 신선도(위)와 다른 축. 앵커 2종(US=QQQ, KR=KODEX200)의
    # 마지막 2개 봉을 직접 읽는다. 디스크 포맷은 olap 밖에서 재구현하지 않고
    # olap.glob_for()/olap.query() 를 그대로 쓴다(둘 다 실패를 삼켜 None 을 낸다).
    def _last_bars(symbol: str, interval: str) -> list[dict] | None:
        pattern = OLAP.glob_for(symbol, interval, root / "data" / "history")
        rows = OLAP.query(
            "SELECT ts, open, high, low, close FROM "
            f"read_parquet('{pattern}', union_by_name=true) ORDER BY ts DESC LIMIT 2"
        )
        if rows is None:
            return None
        return [
            {
                "ts": r[0].isoformat() if hasattr(r[0], "isoformat") else r[0],
                "open": r[1], "high": r[2], "low": r[3], "close": r[4],
            }
            for r in reversed(rows)  # DESC 로 뽑았으니 오래된 것 → 최신 순으로 되돌린다
        ]

    findings += H.bar_sanity_findings(
        {"QQQ 1d": _last_bars("QQQ", "1d"), "069500 1d": _last_bars("069500", "1d")}, now)

    # 1분봉 적재 신선도 — scalp_1m 표본 축적(2026-08-18). data/history/*/1m/ 이
    # 하나도 없으면(백필 크론 05:40 첫 가동 전) 빈 dict를 그대로 넘긴다 —
    # intraday_history_findings가 그 경우 조용히 빈 목록을 낸다(소음 방지).
    history_root = root / "data" / "history"
    one_m_symbols = sorted(
        p.parent.name for p in history_root.glob("*/1m") if p.is_dir()
    ) if history_root.exists() else []
    last_1m_bars = {
        sym: (coverage(sym, "1m", history_root).last_ts
              if coverage(sym, "1m", history_root) else None)
        for sym in one_m_symbols
    }
    findings += H.intraday_history_findings(last_1m_bars, now)

    # 시계 — 호스트 TZ + 엔진 하트비트. heartbeat.ts 는 ISO 가 아니라 epoch 다.
    hb = _json_file(root / "data" / "state" / "heartbeat.json")
    hb_stamp = None
    if hb and isinstance(hb.get("ts"), (int, float)):
        hb_stamp = datetime.fromtimestamp(hb["ts"], tz=timezone.utc).isoformat()
    local_offset = datetime.now().astimezone().utcoffset()
    findings += H.clock_findings(
        None if local_offset is None else int(local_offset.total_seconds()), hb_stamp, now)

    # 원장 ↔ 포트폴리오
    findings += H.ledger_portfolio_findings(
        load_trades(root / "data" / "state" / "trades.jsonl"),
        _json_file(root / "data" / "state" / "portfolio.json"))

    # 원장 신선도 — DART 공시·텔레그램·외국인 수급·선정 4개 원장이 계속 쌓이나.
    # 타임스탬프 필드명은 writer마다 다르다: dart.py 는 rcept_dt(YYYYMMDD, 시각
    # 없음), telegram_channels.py 는 published(ISO, 시각 있음), frgn_flow.py·
    # selections.py 는 date(YYYY-MM-DD, 시각 없음) — 날짜만 있는 값은
    # fromisoformat 이 자정으로 해석해 그대로 UTC 자정 취급된다(임계가 일 단위라
    # 충분하다). 파일이 없거나 마지막 줄이 깨지면 None → unknown(_read/_json_file
    # 과 같은 관례) — "원장이 아예 없는 신규 설치"와 구분하지 않는다(지금 EC2엔
    # 4개 다 있다).
    def _ledger_last_ts(path: Path, field: str, date_fmt: str | None = None) -> str | None:
        raw = _read(path)
        if raw is None:
            return None
        lines = [ln for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            row = _json.loads(lines[-1])
        except _json.JSONDecodeError:
            return None
        value = row.get(field) if isinstance(row, dict) else None
        if value is None:
            return None
        if date_fmt is None:
            return str(value)
        try:
            return datetime.strptime(str(value), date_fmt).date().isoformat()
        except ValueError:
            return None

    def _ledger_max_date(path: Path, field: str) -> str | None:
        """전 행에서 field 최댓값(ISO 날짜는 사전순=시간순).

        frgn_flow 는 append-only 가 아니라 **upsert 재기록** 원장이라 파일의
        마지막 줄이 최신 날짜가 아니다 — 2026-08-17 EC2 첫 실행에서 '28일
        낡음' 거짓 경보가 그 가정에서 나왔다(마지막 줄은 심볼 블록의 옛 날짜).
        파일이 작아(심볼×최대 20일) 전체 스캔 비용은 무시할 수준이다.
        """
        raw = _read(path)
        if raw is None:
            return None
        best: str | None = None
        for ln in raw.splitlines():
            if not ln.strip():
                continue
            try:
                value = _json.loads(ln).get(field)
            except _json.JSONDecodeError:
                continue
            if isinstance(value, str) and (best is None or value > best):
                best = value
        return best

    ledger_root = root / "data" / "ledger"
    last_ts_by_ledger = {
        "disclosures": _ledger_last_ts(ledger_root / "disclosures.jsonl", "rcept_dt", "%Y%m%d"),
        "telegram_msgs": _ledger_last_ts(ledger_root / "telegram_msgs.jsonl", "published"),
        "frgn_flow": _ledger_max_date(ledger_root / "frgn_flow.jsonl", "date"),
        "selections": _ledger_last_ts(ledger_root / "selections.jsonl", "date"),
    }
    max_age_by_ledger = {
        # DART 수집 크론은 07:20 1일 1회 — 4일이면 주말+연휴를 넘겨도 안 울린다.
        "disclosures": timedelta(days=4),
        # 텔레그램은 리포트 빌드마다(하루 여러 번) 갱신된다 — 2일이면 하루 결항도 버틴다.
        "telegram_msgs": timedelta(days=2),
        # 외국인 수급도 리포트 빌드가 채운다(하루 여러 번) — disclosures 와 같은 여유.
        "frgn_flow": timedelta(days=4),
        # 선정 원장도 리포트 빌드마다 쌓인다 — 같은 여유.
        "selections": timedelta(days=4),
    }
    findings += H.ledger_findings(last_ts_by_ledger, now, max_age_by_ledger)

    def _tail_jsonl(path: Path, n: int) -> list[dict]:
        """마지막 n줄만 파싱한다 — 원장이 커져도 매시 크론이 파일 전체를 읽지 않게."""
        raw = _read(path)
        if raw is None:
            return []
        rows = []
        for ln in [ln for ln in raw.splitlines() if ln.strip()][-n:]:
            try:
                rows.append(_json.loads(ln))
            except _json.JSONDecodeError:
                continue
        return rows

    # 뉴스 발행량 이상 — KR/US 각각 오늘 건수 + 직전 7일. data/news/{market}/{day}.jsonl
    # 은 collector.py 가 하루치를 링크 기준으로 중복 제거해 재기록하므로 줄 수 =
    # 그날의 고유 기사 수다. 파일이 없는 날은 "그날 수집 0건"으로 0 취급한다.
    now_kst = now.astimezone(_KST_TZ)
    zero_check_active = now_kst.hour >= 14
    for market in ("KR", "US"):
        def _news_count(d) -> int:
            raw = _read(root / "data" / "news" / market / f"{d.isoformat()}.jsonl")
            return 0 if raw is None else len([ln for ln in raw.splitlines() if ln.strip()])

        today_kst = now_kst.date()
        today_count = _news_count(today_kst)
        trailing = [_news_count(today_kst - timedelta(days=i)) for i in range(1, 8)]
        findings += H.flow_anomaly_findings(today_count, trailing, market, zero_check_active)

    # 텔레그램 전채널 동시 침묵 — 원장 뒤쪽 5천 줄만 파싱해 채널별 최신 published 를
    # 뽑는다(읽기는 한 번에 하되 파싱 비용을 뒤쪽으로 한정 — 근거는 위 _tail_jsonl).
    # _read 를 따로 한 번 더 하는 이유: "파일이 없다"(None → UNKNOWN)와 "파싱할 줄이
    # 없다"(빈 dict)를 구분하기 위해서다.
    telegram_path = ledger_root / "telegram_msgs.jsonl"
    telegram_raw = _read(telegram_path)
    newest_by_channel: dict[str, str | None] | None
    if telegram_raw is None:
        newest_by_channel = None
    else:
        newest_by_channel = {}
        for row in _tail_jsonl(telegram_path, 5000):
            handle, published = row.get("handle"), row.get("published")
            if not handle or not published:
                continue
            if handle not in newest_by_channel or published > newest_by_channel[handle]:
                newest_by_channel[handle] = published
    findings += H.telegram_silence_findings(newest_by_channel, now)

    # 외국인 수급 원장 퇴화 — 최근 200행이 전부 foreign_net=0 인가.
    findings += H.frgn_flow_degenerate_findings(_tail_jsonl(ledger_root / "frgn_flow.jsonl", 200))

    # 선정 원장 중복 자연키 — 최근 500행.
    findings += H.selection_dup_findings(_tail_jsonl(ledger_root / "selections.jsonl", 500))

    # 시크릿 — 최근 로그. 값은 마스킹된 상태로만 경보에 실린다.
    journal = _run(["journalctl", "-u", "quant-engine", "--since", "1 hour ago", "--no-pager"])
    if journal is not None:
        findings += H.secret_findings(journal.splitlines())
    else:
        findings.append(H.Finding("secrets", H.UNKNOWN,
                                  "로그를 읽지 못했다 — 시크릿 유출 여부를 모른다"))

    # 타이머
    timers_out = _run(["systemctl", "list-timers", "--all", "--no-pager"])
    if timers_out is None:
        findings.append(H.Finding("timers", H.UNKNOWN, "systemd 타이머 목록을 읽지 못했다"))
    else:
        present = {tok for line in timers_out.splitlines() for tok in line.split()
                   if tok.endswith(".timer")}
        findings += H.timer_findings({u: None for u in present}, args.expect_timer or [])

    # 설치본 드리프트 — 저장소는 옳고 설치본만 낡는 부류(실측: EC2 crontab).
    findings += H.install_drift_findings(_run(["crontab", "-l"]),
                                        _read(root / "server" / "crontab.txt"))

    # 리포트 결측 — 리포트가 `engine.json` 의 `missing` 에 **이미 기록하는데** 아무도
    # 읽지 않았다. 2026-08-14: API 키를 못 읽어 소스 5개가 결측인 채로 발행됐고,
    # 사람이 빌드 출력의 "결측 5건"을 보고도 "장중이라 그런가"로 미뤘다.
    today = datetime.now(timezone.utc).astimezone(_KST_TZ).date()

    def _engine_json(market: str, d) -> dict | None:
        return _json_file(root / "out" / f"{d:%Y/%m/%d}" / f"{market}_engine.json")

    engine_payload_by_market: dict[str, dict | None] = {
        market: _engine_json(market, today) for market in ("KR", "US")
    }
    missing_by_market: dict[str, list[str] | None] = {
        market: (None if payload is None else list(payload.get("missing") or []))
        for market, payload in engine_payload_by_market.items()
    }
    findings += H.report_findings(missing_by_market, required=args.required_source or [])

    # 발행↔편입 정합 — 리포트는 발행됐는데 편입이 랭킹 폴백뿐인가(2026-08-14~17
    # 나흘간 own_brief 경로 기본값이 옛 체크아웃을 가리켜 실제로 이 모양이었다,
    # H.report_intake_findings docstring). 편입 결과는 `data/watchlist.yaml`
    # 에서 읽는다 — `data/own_brief.log`의 "편입 완료: $DISPLAY" 줄은 셸에서
    # 태그를 이미 벗겨내 심볼만 남기므로(own_brief.sh DISPLAY 조립부) 태그
    # 판정에 못 쓴다. watchlist.yaml 은 own_brief.sh → tg_bridge.py watch-add
    # 가 `source`/`tags`/`added_at`을 그대로 남긴다(server/scripts/
    # tg_bridge.py: _handle_watch_unlocked).
    def _auto_watch_token_count(auto_watch) -> int:
        """`report_cli.py: _auto_watch_count` 와 같은 로직 — 그 함수는 apps
        내부 비공개(`_` 접두)라 여기서 3줄을 다시 쓴다(임포트보다 저렴하고,
        형식(`"AUTO_WATCH: 없음"`)은 report_cli.py 가 이미 고정 계약으로 쓰고
        있어 갈라질 위험이 낮다)."""
        body = str(auto_watch or "").removeprefix("AUTO_WATCH:").strip()
        return 0 if not body or body == "없음" else len(body.split())

    def _watchlist_intake_tags(path: Path, session_date) -> dict[str, list[str] | None]:
        """오늘 자동 편입(`source=="auto"`, `added_at` 이 오늘)된 항목의
        태그를 시장별로 합친다. 태그만 갱신되고 신규 등록이 아닌 경우
        (FRGN_EXIT `--tags-only` 갱신)는 `added_at` 이 안 바뀌어 여기 안
        잡힌다 — 이 검사가 보려는 건 "오늘 신규 편입이 리포트를 실제로
        읽었나"이므로 신규 등록만으로 충분하다.
        """
        raw = _read(path)
        if raw is None:
            return {"KR": None, "US": None}
        try:
            import yaml
            data = yaml.safe_load(raw) or {}
        except Exception:  # noqa: BLE001 — 파싱 실패는 예외가 아니라 None
            return {"KR": None, "US": None}
        entries = data.get("symbols")
        if not isinstance(entries, list):
            return {"KR": None, "US": None}
        from quant.core.models import market_of_symbol

        tags_by_market: dict[str, set[str]] = {"KR": set(), "US": set()}
        for e in entries:
            if not isinstance(e, dict) or e.get("source") != "auto":
                continue
            added_at = e.get("added_at")
            if not isinstance(added_at, str):
                continue
            try:
                added_date = datetime.fromisoformat(added_at).date()
            except ValueError:
                continue
            if added_date != session_date:
                continue
            symbol = str(e.get("symbol") or "")
            if not symbol:
                continue
            tags_by_market[market_of_symbol(symbol)].update(e.get("tags") or [])
        return {m: sorted(tags) for m, tags in tags_by_market.items()}

    report_exists = {market: payload is not None
                     for market, payload in engine_payload_by_market.items()}
    intake_tags = _watchlist_intake_tags(root / "data" / "watchlist.yaml", today)
    findings += H.report_intake_findings(report_exists, intake_tags)

    # 리포트 품질 회귀 — 후보 수·AI 해석 상태가 어제보다 조용히 나빠졌나
    # (H.report_quality_findings docstring — 실측 확인한 engine.json 필드명 포함).
    def _report_summary(payload: dict | None) -> dict | None:
        if payload is None:
            return None
        return {
            "candidates": _auto_watch_token_count(payload.get("auto_watch")),
            "midterm": len(payload.get("midterm_watch") or []),
            "agent_interpret": payload.get("agent_interpret"),
            "missing": len(payload.get("missing") or []),
        }

    for market in ("KR", "US"):
        today_summary = _report_summary(engine_payload_by_market[market])
        trailing_summaries: list[dict] = []
        # 직전 최대 10 캘린더일을 훑어 실제로 발행된 개장일 것만 모은다(주말
        # ·휴장일은 engine.json 자체가 없어 자연히 빠진다) — flow_anomaly_
        # findings 처럼 "그 날은 trailing 에서 뺀다"관례.
        for i in range(1, 11):
            d = today - timedelta(days=i)
            payload = _engine_json(market, d)
            if payload is not None:
                trailing_summaries.append(_report_summary(payload))
            if len(trailing_summaries) >= 7:
                break
        findings += H.report_quality_findings(market, today_summary, trailing_summaries)

    # 필수 시크릿이 **앱이 실제로 쓰는 경로로** 읽히나. "파일에 있나"가 아니다 —
    # 2026-08-14 에 그 차이가 사고를 만들었다(검증 도구는 자기 로더로 읽어 "완료",
    # 앱은 DEFAULT_ENV 가 quant/ 를 봐서 전부 결측).
    try:
        from quant.adapters.env import load_env

        app_env = load_env()
    except Exception:  # noqa: BLE001
        app_env = None
    findings += H.secret_findings_for(app_env, required=args.required_secret or [])

    # 백업 — 번들 생성과 **오프사이트로 당겨간 것**을 따로 본다.
    bundles = sorted((root / "data" / "backups").glob("quant-*.tar.gz"))
    bundle_stamp = None
    if bundles:
        bundle_stamp = datetime.fromtimestamp(
            bundles[-1].stat().st_mtime, tz=timezone.utc).isoformat()
    pull_raw = _read(root / "data" / "backups" / "LAST_PULL")
    findings += H.backup_findings(bundle_stamp, pull_raw.strip() if pull_raw else None, now)

    # LLM 호출 계측 — narrate()/chat_with_tools() (OpenRouter 무료 레인)가
    # 기록한 최근 24시간 실패율. kv 는 위에서 이미 만든 것을 재사용한다.
    stats_by_lane = {lane: llm_stats(kv, lane, now) for lane in ("narrate", "tool")}
    findings += H.llm_health_findings(stats_by_lane)

    # 국면(regime) 강등 지속 — 유효 지표 부족으로 neutral 강등된 상태가 오래
    # (기본 2시간) 이어지면 ALERT. 2026-08-18~19 실측: US 국면이 지표 5개 중
    # 2개만으로 aggressive를 유지한 하루가 있었는데, provider가 이미 알고 있던
    # degraded 신호를 보는 사람이 없었다(H.regime_findings docstring).
    findings += H.regime_findings(
        _json_file(root / "data" / "state" / "regime.json"), now)

    summary = H.summarize(findings)
    summary["checked_at"] = now.isoformat(timespec="seconds")
    print(_json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit({"ok": 0, H.ALERT: 1, H.UNKNOWN: 2}[summary["verdict"]])


def cmd_ops_judge(args: argparse.Namespace) -> None:
    """판단하는 워치독 — `quant.control.health`(규칙) 위에 LLM 교차검증을 얹는다.

    규칙 기반 점검은 대체하지 않는다 — 이 명령은 규칙 기반 결과(호출부가
    `--rule-based-json`으로 넘긴다, 보통 `server/scripts/ops_judge.sh`가 먼저
    `cli health`를 부른 결과)를 도구 중 하나로 그대로 노출하고, 그 위에 서로 다른
    데이터 소스(포트폴리오·원장·전략 설정·리포트·지수 봉·운영 로그)를 대조하는
    LLM 판단을 얹는다. 판정과 종료코드는 세 갈래다: `ok`=0, `review`(확인 필요)=2,
    `alert`(이상)=1 — `health`의 0/1/2(ok/alert/unknown) 관례를 그대로 맞춘다.

    이 명령 자체가 LLM 때문에 죽으면 안 된다 — `quant.control.ops_judge.
    run_judgment`이 모든 LLM 실패(자격증명 없음/호출 실패/응답 없음/파싱 실패)를
    `review`로 흡수하므로, 여기서는 그 결과를 그대로 출력할 뿐이다.
    """
    import functools
    import json as _json
    import sys
    from datetime import timedelta, timezone
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT, get_key
    from quant.adapters.kv import make_kv
    from quant.adapters import olap as OLAP
    from quant.adapters.narrate import TOOL_MODEL, chat_with_tools
    from quant.control import ops_judge as J
    from quant.control.ledger import load_trades
    from quant.control.opstate import record_run
    from quant.control.strategy_books import load_strategy_books

    settings = load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    now = datetime.now(timezone.utc)

    def _read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _json_file(path: Path) -> dict | None:
        raw = _read(path)
        if raw is None:
            return None
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            return None

    # 규칙 기반 결과 — 대체가 아니라 도구 중 하나로 그대로 넘긴다. 호출부(셸)가
    # `cli health`를 먼저 부른 결과를 파일 또는 stdin으로 넘긴다. 안 넘기면
    # rule_based=None(도구가 "읽지 못했다"로 정직하게 답한다) — 이 명령 혼자서도
    # 죽지 않는다.
    rule_based: dict | None = None
    if args.rule_based_json:
        raw = sys.stdin.read() if args.rule_based_json == "-" else _read(Path(args.rule_based_json))
        if raw:
            try:
                rule_based = _json.loads(raw)
            except _json.JSONDecodeError:
                rule_based = None

    portfolio = _json_file(root / "data" / "state" / "portfolio.json")
    recent_trades = load_trades(root / "data" / "state" / "trades.jsonl")[-200:]
    strategy_books = load_strategy_books(root / "data" / "state" / "strategy_books.json")
    strategy_config = dict(settings.strategies)
    control_state = _json_file(root / "data" / "state" / "control.json")
    heartbeat = _json_file(root / "data" / "state" / "heartbeat.json")

    # 리포트 — 아침(KR/US) + KR 오후 마감. 오늘 발행 안 됐으면 None(도구가 "읽지
    # 못했다"로 정직하게 답한다 — cmd_health의 report_findings 관례와 동일).
    _KST = timezone(timedelta(hours=9))
    today = now.astimezone(_KST).date()

    def _engine_json(market: str) -> dict | None:
        return _json_file(root / "out" / f"{today:%Y/%m/%d}" / f"{market}_engine.json")

    def _close_engine_json(market: str) -> dict | None:
        return _json_file(root / "out" / f"{today:%Y/%m/%d}" / f"{market}_close_engine.json")

    reports = {
        "KR_am": _engine_json("KR"),
        "US_am": _engine_json("US"),
        "KR_close": _close_engine_json("KR"),
    }

    # 지수 봉 — cmd_health의 bar_sanity 조회와 같은 방식(olap.glob_for/query,
    # 실패는 None). 리포트가 말하는 지수 등락률을 실제 값과 대조하는 근거.
    def _last_bars(symbol: str, interval: str) -> list[dict] | None:
        pattern = OLAP.glob_for(symbol, interval, root / "data" / "history")
        rows = OLAP.query(
            "SELECT ts, open, high, low, close FROM "
            f"read_parquet('{pattern}', union_by_name=true) ORDER BY ts DESC LIMIT 5"
        )
        if rows is None:
            return None
        return [
            {"ts": r[0].isoformat() if hasattr(r[0], "isoformat") else r[0],
             "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
            for r in reversed(rows)
        ]

    bar_checks = {"QQQ 1d": _last_bars("QQQ", "1d"), "069500 1d": _last_bars("069500", "1d")}

    # 운영 로그 근사치 — 그 문자열을 만든 빌드/스크립트 로그(전송 문자열 자체가
    # 아니다 — sent_notifications 가 그쪽을 맡는다, 아래). 각 로그의 마지막
    # 400줄만 잡아 도구가 다시 자르게 한다.
    log_names = {
        "ops_watch": "ops_watch.log", "report": "report.log",
        "close_report": "close_report.log", "session_pnl": "session_pnl.log",
        "brief": "brief.log", "watchdog": "watchdog.log", "backup": "backup.log",
    }
    log_tails = {}
    for name, filename in log_names.items():
        raw = _read(root / "data" / filename)
        log_tails[name] = [ln for ln in raw.splitlines() if ln.strip()][-400:] if raw else []

    # 텔레그램 발송 원장 — `quant.adapters.notify.telegram.TelegramNotifier.send()`
    # 가 성공·실패 모두 실제 전송 문자열을 남긴다(2026-08-19). `_read()`가 `None`을
    # 주는 경우(파일 없음/OSError)와 파일은 읽었는데 유효한 행이 하나도 없는
    # 경우를 구분해서 넘긴다 — `None`="발송 기록을 모른다", `[]`="원장은 읽었는데
    # 발송 이력이 없다"(AgentData.sent_notifications 문서와 ops_judge 모듈
    # docstring "텔레그램 발송 원장" 절 — 둘을 합쳐 "없다"로 뭉개지 않는다).
    # 최근 300건만 잡아 도구(get_sent_notifications, 상한 50건)가 다시 자르게
    # 한다 — 원장 전체를 메모리에 올리지 않는다.
    def _read_notifications(path: Path, max_rows: int = 300) -> list[dict] | None:
        raw = _read(path)
        if raw is None:
            return None
        rows: list[dict] = []
        for ln in raw.splitlines():
            if not ln.strip():
                continue
            try:
                rows.append(_json.loads(ln))
            except _json.JSONDecodeError:
                continue
        return rows[-max_rows:]

    sent_notifications = _read_notifications(root / "data" / "ledger" / "notifications.jsonl")

    data = J.AgentData(
        rule_based=rule_based, portfolio=portfolio, recent_trades=recent_trades,
        strategy_books=strategy_books, strategy_config=strategy_config,
        control_state=control_state, heartbeat=heartbeat, reports=reports,
        bar_checks=bar_checks, log_tails=log_tails,
        sent_notifications=sent_notifications, label=args.label,
    )

    # LLM 백엔드 — chat_with_tools(OpenRouter 무료 레인, agent_interpret.py와 같은
    # 툴콜링 루프). 크론은 .env.local 을 export 하지 않으므로 os.environ 우선,
    # 없으면 get_key() 파일 직독 폴백(narrate.py `_make_openrouter_narrator`와
    # 같은 이유 — 2026-08-16 실측: 이 폴백이 없어 크론 경로의 LLM 이 조용히 죽어
    # 있었다).
    key = os.environ.get("OPENROUTER_API_KEY", "").strip() or (get_key("OPENROUTER_API_KEY") or "")
    narrator_name = "none"
    chat = None
    if key:
        # 단일 HTTP 콜 타임아웃을 예산의 일부로 줄인다 — chat_with_tools는 라운드당
        # (최대 5라운드 x 1순위/폴백 모델 = 최대 10콜) 재시도하므로, 콜 하나가
        # 예산 전체를 먹으면 안 된다. 실제 벽시계 상한은 그래도 셸의 `timeout`이
        # 진다(run_judgment 문서 — 이 함수는 단일 판단 호출이라 도중을 못 자른다).
        budget = args.time_budget if args.time_budget else 240
        timeout = max(10, min(60, int(budget / 4)))
        chat = functools.partial(chat_with_tools, api_key=key, model=TOOL_MODEL, timeout=timeout)
        narrator_name = f"openrouter:{TOOL_MODEL}"

    result = J.run_judgment(data, chat, time_budget_seconds=args.time_budget)

    out = dict(result)
    out["narrator"] = narrator_name
    out["label"] = args.label
    out["checked_at"] = now.isoformat(timespec="seconds")
    print(_json.dumps(out, ensure_ascii=False, indent=2))

    try:
        # ok=True는 "판정이 정상이었다"가 아니라 "이 잡이 죽지 않고 결과를 냈다"다
        # (close-report의 record_run과 같은 의미) — level=review 도 정상 실행의
        # 결과일 수 있으므로(자격증명 없음 등) 여기서 실패로 잘못 기록하면
        # job_findings 가 "이 잡이 최근에 실패했다"는 거짓 경보를 낸다.
        record_run(make_kv(), "ops-judge", ok=True,
                  detail=f"level={result['level']} narrator={narrator_name}")
    except Exception:  # noqa: BLE001 — 운영 상태 기록 실패가 판정 출력을 막으면 안 된다
        pass

    raise SystemExit({"ok": 0, "alert": 1, "review": 2}[result["level"]])


def cmd_outcomes(args: argparse.Namespace) -> None:
    """전방 수익률 채우기 + 결정론적 판단 기록 (Phase 7.2 / 7.3).

    매일 장 마감 후 돈다. **그날 만기가 된 지평만** 채우므로 과거 시세 조회가 필요
    없다 — 오늘 종가만 있으면 된다(자세한 근거는 `control/outcomes.py`).

    `pending_outcomes()` 는 원래부터 있었지만 **부르는 코드가 없었다** — 실측으로
    선정 원장 199행 중 outcome 이 0건이었다. 이 커맨드가 그 구멍이다.
    """
    import json as _json
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.control import outcomes as O
    from quant.control import selections
    from quant.control.judgment import HOLD_HORIZONS, selection_judgment

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    sel_path = root / "data" / "ledger" / "selections.jsonl"
    rows = selections.load(sel_path)
    today = args.date or _date.today().isoformat()

    need = O.pending_symbols(rows, today)
    quotes: dict[str, tuple[float, str]] = {}
    quote_error = None
    if need and not args.dry_run:
        from quant.analyze.entities import load_market_map
        from quant.collect.sources.market import fetch_symbol_quotes
        from quant.report.collect.quotes import fetch_kr_quotes

        us, kr = O.split_for_quotes(need)
        # US/KR 을 각자 try 로 격리한다 — 2026-08-26 실사고: KIND 403 이 KR 매핑
        # 에서 터지자 한 try 가 통째로 죽어 "US 83건만" 남았고, KR 322개 심볼의
        # D+1 채움이 그날 전부 밀렸다(유예 2거래일 안에 회복해야 하는 상태).
        try:
            if us:
                # D3(2026-09-03): 야후는 점(.) 이 든 종류주 심볼(BRK.B)을 못 받는다
                # — 대시로 바꿔 조회하고, 결과는 원래 심볼(선정 행의 symbol)로
                # 되돌려 매핑한다. 안 그러면 BRK.B 는 quotes 딕셔너리에서 "BRK-B"
                # 로만 남아 아래 조회(quotes.get(row["symbol"]))가 영원히 빈다.
                yahoo_map = {O.to_yahoo_us_symbol(s): s for s in us}
                us_raw = fetch_symbol_quotes(sorted(yahoo_map))
                us_renamed = {yahoo_map[sym]: q for sym, q in (us_raw or {}).items()
                             if sym in yahoo_map}
                quotes.update(O.closes_from_quotes(us_renamed))
        except Exception as e:  # noqa: BLE001 — 시세 실패가 판단 기록을 막지 않는다
            quote_error = f"US {type(e).__name__}: {e}"
            logger.warning("US 시세 조회 실패 — 이번 회차 US 채움 생략: %s", e)
        try:
            if kr:
                # KIND(시장구분)가 죽어도 KR 시세를 포기하지 않는다 — 리포트
                # 본선과 같은 .KS/.KQ 폴백 경로(quant/report/collect/quotes.py,
                # 같은 날 아침 리포트 수리와 동일 결함·동일 수리).
                kr_quotes, route = fetch_kr_quotes(
                    sorted(kr), root / "data" / "cache",
                    map_loader=load_market_map, quote_fetcher=fetch_symbol_quotes,
                )
                logger.info("KR 시세 경로: %s", route)
                quotes.update(O.closes_from_quotes(kr_quotes))
        except Exception as e:  # noqa: BLE001
            quote_error = (quote_error + " · " if quote_error else "") + f"KR {type(e).__name__}: {e}"
            logger.warning("KR 시세 조회 실패 — 이번 회차 KR 채움 생략: %s", e)

    filled = 0
    updated: list[dict] = []
    for row in rows:
        new = row
        # D2(2026-09-03): 세션을 close_date(있으면)로 센다 — 선정 행의 date 는
        # 리포트 빌드일일 뿐 실제 거래일이 아닐 수 있다(base_session_date 참고).
        for h in O.due_horizons(O.base_session_date(row), today):
            if new.get(f"outcome_d{h}_bps") is not None:
                continue
            before = new
            new = O.apply_outcome(new, h, quotes.get(str(row.get("symbol"))), today)
            if new is not before and new.get(f"outcome_d{h}_bps") is not None:
                filled += 1
        updated.append(new)

    # 결정론적 베이스라인 판단 — 리더보드에서 LLM 이 이겨야 할 상대(7.3).
    jpath = root / "data" / "ledger" / "judgments.jsonl"
    existing = {
        (r.get("producer"), r.get("producer_version"), r.get("input_hash"),
         r.get("symbol"), r.get("session_date"))
        for r in selections.load(jpath)
    }
    new_judgments = []
    for row in rows:
        j = selection_judgment(row, producer_version=str(args.scorer_version))
        if j.natural_key() not in existing:
            new_judgments.append(j)
            existing.add(j.natural_key())

    if not args.dry_run:
        if filled:
            selections.rewrite(updated, sel_path)
        if new_judgments:
            jpath.parent.mkdir(parents=True, exist_ok=True)
            with jpath.open("a", encoding="utf-8") as f:
                for j in new_judgments:
                    f.write(_json.dumps(j.__dict__, ensure_ascii=False) + "\n")

    print(_json.dumps({
        "today": today, "selection_rows": len(rows),
        "symbols_needing_quote": len(need), "quotes_fetched": len(quotes),
        # **"0건"과 "실패"를 구분한다.** 앞 버전은 예외를 삼키고 0 만 남겨
        # "채울 게 없었다"로 읽혔다(2026-08-14).
        "quote_error": quote_error,
        "quotes_missing": sorted(need - set(quotes))[:10] if need else [],
        "horizons_filled": filled, "judgments_appended": len(new_judgments),
        "horizons": list(HOLD_HORIZONS), "dry_run": bool(args.dry_run),
        # D4(2026-09-03): filled/due(진행 중)/lost(grace 넘겨 영구 유실)를 지평별로
        # 구분한다 — filled 만 보면 "표본이 아직 적다"와 "죽은 표본"을 못 가른다.
        "horizon_status": O.horizon_status_counts(updated, today),
    }, ensure_ascii=False, indent=2))


def cmd_ai_trader(args: argparse.Namespace) -> None:
    """신입사원 AI 트레이더(수습) — 오늘 selections 행(기존 직원과 같은 서류)을
    읽고 3역할 토론(애널리스트→리스크→트레이더)으로 판단을 남긴다 (2026-08-26).

    stdout = 텔레그램 카드 텍스트(experiments_daily.sh 관례 — 스크립트가 그대로
    발송). 픽 없음/결근/이미 기록됨이면 stdout 무출력 = 침묵. 판단은
    judgments.jsonl(producer="ai_trader")로 들어가 outcomes→리더보드가 채점한다.
    주문·워치리스트에는 닿지 않는다.
    """
    import json as _json
    from datetime import date as _date, datetime as _dt, timezone as _tz
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.narrate import make_json_narrator
    from quant.analyze import ai_trader
    from quant.control import selections

    settings = load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    today = args.date or _date.today().isoformat()
    market = args.market

    # 재실행 가드 — 같은 (날짜, 시장) 토론은 하루 한 번(LLM 호출 중복 방지).
    dpath = root / ai_trader.DEBATE_LEDGER
    if dpath.exists():
        for line in dpath.read_text(encoding="utf-8").splitlines():
            try:
                rec = _json.loads(line)
            except ValueError:
                continue
            if rec.get("date") == today and rec.get("market") == market:
                logger.info("ai-trader: %s %s 토론 기록이 이미 있다 — 건너뜀", today, market)
                return

    rows = []
    seen: set[str] = set()
    for r in selections.load(root / "data" / "ledger" / "selections.jsonl"):
        if str(r.get("date")) != today or str(r.get("market")) != market:
            continue
        # 본선 리포트 행 + 감시 축 합류 행(watch_join)만 서류로 본다. 단타
        # 스코어러 등 다른 생산자 행은 속성 모양이 달라 별도 서류다.
        if r.get("producer") not in (None, selections.WATCH_JOIN_PRODUCER):
            continue
        sym = str(r.get("symbol") or "")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        rows.append(r)
    if not rows:
        logger.info("ai-trader: %s %s 선정 원장 행 없음 — 서류가 없어 결근", today, market)
        return

    # 산문 서술기가 아니라 JSON 계약 변형 — 산문 가드(사고과정 유출 폐기)가
    # JSON 을 오탐하고 700 토큰 상한이 verdict 목록을 자른다(2026-08-26 실 E2E).
    # 8000: 추론 모델은 "생각"에도 max_tokens 를 쓴다 — 서류 80행 급에서 4000이
    # 완주하지 못해 결근한 실측(같은 날 EC2)의 여유분.
    narrator = make_json_narrator(max_tokens=8000)
    result = ai_trader.run_debate(rows, narrator.narrate)
    if result is None:
        logger.warning("ai-trader: 토론 실패(LLM) — 오늘 결근 (판단 미기록)")
        return

    judgments = ai_trader.to_judgments(result["final"], rows)
    added = ai_trader.append_judgments(judgments, root / "data" / "ledger" / "judgments.jsonl")

    dpath.parent.mkdir(parents=True, exist_ok=True)
    with dpath.open("a", encoding="utf-8") as f:
        f.write(_json.dumps({
            "date": today, "market": market, "final": result["final"],
            "transcript": [{"role": role, "raw": raw} for role, raw in result["transcript"]],
            "recorded_at": _dt.now(_tz.utc).isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n")

    logger.info("ai-trader: %s %s — 행 %d, 판단 %d(신규 %d)",
                today, market, len(rows), len(judgments), added)

    # 2단계(태그 소스 승격, 2026-08-26): 리더보드 promote 판정 후 **사람이**
    # settings 로 켠다. 켜지면 픽을 마커 줄로 내보내고, ai_trader.sh 가
    # watch-score 확신도 게이트(무태그 best-of)를 거쳐 편입한다 — own_brief 와
    # 같은 이중 게이트. 여기서는 워치리스트에 직접 쓰지 않는다.
    if (settings.raw.get("ai_trader") or {}).get("tag_source_enabled", False):
        wl = ai_trader.watch_line(result["final"])
        if wl:
            print(wl)

    names = {str(r.get("symbol")): r.get("name") for r in rows if r.get("name")}
    note = ai_trader.daily_note(result["final"], market, names)
    if note:
        print(note)


def cmd_promotion_debate(args: argparse.Namespace) -> None:
    """승격 토론(Bull/Bear) — 오늘 own_brief.sh 가 확신도 게이트를 통과시켜
    자동 편입한 종목을 Bull(찬성)/Bear(반대)/Judge(심판) 3역할 토론으로
    재검토한다(2026-09-02, 회사형 AI 에이전트 레이어 레인 1). **관심종목을
    바꾸지 않는다** — data/ledger/debate.jsonl 에 유지/보류 판정만 남긴다.

    stdout = 텔레그램 카드 텍스트(ai_trader.sh 관례 — 셸이 그대로 notify_auto
    로 넘긴다). 오늘 자동 편입이 없거나 LLM 결근이면 무출력(침묵)."""
    import json as _json
    from datetime import date as _date
    from pathlib import Path

    import yaml as _yaml

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.narrate import make_json_narrator
    from quant.analyze import promotion_debate as pd
    from quant.analyze.watch_scorer import resolve_regime_label, run_watch_score
    from quant.apps.assembly import MissingCredentials, _load_symbol_names, build_toss_client

    settings = load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    market = args.market
    today = args.date or _date.today().isoformat()

    watchlist_path = root / "data" / "watchlist.yaml"
    if not watchlist_path.exists():
        logger.info("promotion-debate: watchlist.yaml 없음 — 오늘 편입분 없음")
        return
    try:
        raw = _yaml.safe_load(watchlist_path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001 — 워치리스트 파싱 실패가 이 레인을 죽여도 매매엔 영향 없다
        logger.warning("promotion-debate: watchlist.yaml 파싱 실패: %s", e)
        return

    def _is_kr(sym: str) -> bool:
        return sym.isdigit() and len(sym) == 6

    tokens: list[str] = []
    for e in (raw.get("symbols") or []):
        if not isinstance(e, dict) or e.get("source") != "auto":
            continue
        if not str(e.get("added_at") or "").startswith(today):
            continue
        sym = str(e.get("symbol") or "")
        if not sym or _is_kr(sym) != (market == "KR"):
            continue
        tags = e.get("tags") or []
        tokens.append(sym + (":" + "+".join(tags) if tags else ""))

    if not tokens:
        logger.info("promotion-debate: %s %s 오늘 자동 편입 없음 — 토론 없음", today, market)
        return

    # 재실행 가드 — 같은 (날짜, 시장) 토론은 하루 한 번(LLM 호출 중복 방지).
    dpath = root / pd.DEBATE_LEDGER
    if dpath.exists():
        for line in dpath.read_text(encoding="utf-8").splitlines():
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if (r.get("date") == today and r.get("market") == market
                    and r.get("producer") == pd.PRODUCER):
                logger.info("promotion-debate: %s %s 이미 기록됨 — 건너뜀", today, market)
                return

    # 오늘 통과 종목의 점수 내역 — watch-score 를 재실행해 재구성한다(own_brief.sh
    # 가 오늘 아침 이미 계산했지만 결과를 persist 하지 않는다 — cmd_watch_score
    # 와 동일한 조립을 여기서도 한다).
    auto_score_cfg = settings.universe.get("watchlist", {}).get("auto_score", {})
    threshold = auto_score_cfg.get("threshold", 50)
    regime_state = None
    regime_path = regime_state_path()
    if regime_path.exists():
        try:
            regime_state = _json.loads(regime_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — regime 읽기 실패는 neutral 폴백으로 이미 처리됨
            pass
    regime_label, _stale = resolve_regime_label(regime_state)

    try:
        client = build_toss_client()
    except MissingCredentials as e:
        logger.warning("promotion-debate: Toss 클라이언트 구성 실패 — 결근: %s", e)
        return

    results = run_watch_score(
        tokens, client, threshold, regime_label, enabled=True,
        allow_kr_stocks=auto_score_cfg.get("allow_kr_stocks", False),
    )
    items = [
        {"symbol": r.symbol, "score": r.score, "eff_threshold": r.eff_threshold,
         "profile": r.profile, "breakdown": r.breakdown, "reasons": r.reasons}
        for r in results
    ]
    if not items:
        logger.info("promotion-debate: %s %s 채점 결과 없음 — 결근", today, market)
        return

    def _report_summary() -> str:
        try:
            d = _date.fromisoformat(today)
            payload = _json.loads(
                (root / "out" / f"{d:%Y/%m/%d}" / f"{market}_engine.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 리포트 요약은 있으면 좋은 참고자료일 뿐
            return ""
        exec_summary = payload.get("exec_summary")
        if not isinstance(exec_summary, dict):
            return ""
        parts = [exec_summary.get(k) for k in ("market", "flow", "catalyst")]
        return " ".join(p for p in parts if p)

    narrator = make_json_narrator(max_tokens=6000)
    result = pd.run_debate(items, _report_summary(), narrator.narrate)
    if result is None:
        logger.warning("promotion-debate: 토론 실패(LLM) — 오늘 결근")
        return

    records = pd.to_records(result["final"], items, market, today)
    pd.append_ledger(records, dpath)

    try:
        names = _load_symbol_names(root / "data" / "state" / "symbol_names.json")
    except Exception:  # noqa: BLE001 — 이름 캐시 없어도 심볼만으로 카드는 완전하다
        names = {}

    text = pd.notify_text(records, market, names)
    if text:
        print(text)


def cmd_ml_scorer(args: argparse.Namespace) -> None:
    """학습형 선정자 `ml_scorer` (2026-08-28) — 과거 `selection`⋈`forward_return`
    (D+1)으로 릿지 회귀를 학습해 오늘 선정 원장 후보를 채점한다. `ai_trader`와
    같은 계약: judgments 원장(producer="ml_scorer")에 판단만 남기고 주문·
    워치리스트에는 닿지 않는다.

    학습·후보 데이터 원천이 다르다 — 학습(과거, 전방수익률 필요)은 MySQL
    `selection`/`forward_return`(`quant/analyze/ml_scorer.py`가 DB 를 모르므로
    여기서 읽는다), 오늘 채점 대상은 `ai_trader`와 동일하게
    `data/ledger/selections.jsonl`에서 읽는다 — 그래야 `input_hash`가
    watch_scorer/ai_trader 와 같은 값이 된다(같은 서류 = 같은 해시 계약).

    표본(독립 거래일)이 `--min-train-days` 미만이면 학습도 예측도 하지 않고
    한 줄 stdout 을 남기고 exit 0 한다 — 2026-08-28 실측 기준(거래일 10일)
    지금은 이 경로만 동작하는 게 정상이다. DB 미접속도 같은 방식으로 정직하게
    알린다. `server/scripts/ml_scorer.sh` 가 이 두 "판단 없음" 메시지와 실제
    카드를 구분해 텔레그램 전송 여부를 정한다.
    """
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.db import connect
    from quant.adapters.env import REPO_ROOT
    from quant.analyze import ml_scorer
    from quant.analyze.ai_trader import append_judgments
    from quant.control import selections
    from quant.control.judgment import selection_attributes

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    today = args.date or _date.today().isoformat()
    market = args.market
    min_train_days = args.min_train_days
    lam = args.ridge_lambda

    conn = connect()
    if conn is None:
        print("MySQL 연결 없음 — 판단 없음")
        return

    try:
        import pymysql  # connect() 가 이미 성공했으므로 설치돼 있다

        cols_sql = ", ".join(f"s.{c}" for c in ml_scorer.FEATURE_NAMES)
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT s.session_date AS session_date, s.symbol AS symbol, "
                f"s.market AS market, {cols_sql}, fr.return_bps AS return_bps "
                "FROM selection s JOIN forward_return fr "
                "ON s.market = fr.market AND s.symbol = fr.symbol "
                "AND s.session_date = fr.session_date "
                "WHERE fr.horizon_days = 1 AND s.market = %s AND s.session_date < %s",
                (market, today),
            )
            train_recs = list(cur.fetchall())
    finally:
        conn.close()

    # 2차 방어선(워크포워드) — SQL WHERE 절이 이미 걸렀지만 코드 구조로 다시 강제한다.
    train_recs = ml_scorer.training_rows_before(train_recs, today)
    train_days = len({str(r["session_date"]) for r in train_recs})

    if not ml_scorer.enough_sample(train_days, min_train_days):
        print(f"표본 부족(거래일 {train_days}/{min_train_days}) — 판단 없음")
        return

    rows: list[dict] = []
    seen: set[str] = set()
    for r in selections.load(root / "data" / "ledger" / "selections.jsonl"):
        if str(r.get("date")) != today or str(r.get("market")) != market:
            continue
        # ai_trader 와 같은 서류만 본다 — 본선 리포트 행 + 감시 축 합류 행.
        if r.get("producer") not in (None, selections.WATCH_JOIN_PRODUCER):
            continue
        sym = str(r.get("symbol") or "")
        if not sym or sym in seen:
            continue
        seen.add(sym)
        rows.append(r)
    if not rows:
        print("선정 원장에 오늘 후보 없음 — 판단 없음")
        return

    X_train = ml_scorer.to_matrix(train_recs)
    y_train = [float(r["return_bps"]) for r in train_recs]
    X_train, medians = ml_scorer.impute_median(X_train)
    model = ml_scorer.fit_ridge(X_train, y_train, lam=lam)

    cand_attrs = [selection_attributes(r) for r in rows]
    X_cand = ml_scorer.fill_missing(ml_scorer.to_matrix(cand_attrs), medians)
    preds = ml_scorer.predict_scores(model, X_cand)
    pct = ml_scorer.to_percentile_scores(preds)
    scores = {str(r.get("symbol")): float(s) for r, s in zip(rows, pct)}

    judgments = ml_scorer.to_judgments(scores, rows)
    added = append_judgments(judgments, root / "data" / "ledger" / "judgments.jsonl")
    logger.info("ml-scorer: %s %s — 학습 거래일 %d, 후보 %d, 판단 %d(신규 %d)",
                today, market, train_days, len(rows), len(judgments), added)

    names = {str(r.get("symbol")): r.get("name") for r in rows if r.get("name")}
    print(ml_scorer.daily_note(scores, market, names))


def cmd_risk_review(args: argparse.Namespace) -> None:
    """독립 리스크 리뷰 — 드로다운·집중도·상쇄쌍 노출·연속 손실만 전담
    (2026-09-02, 회사형 AI 에이전트 레이어 레인 2). `ops-judge`와 분리된 별도
    프롬프트다(리스크는 트레이딩·보고 라인과 분리 — quant.control.risk_review
    모듈 docstring). 임계 초과 판정은 결정론이고, LLM 은 상위 3문제+권고
    서술만 맡는다 — LLM 이 결근해도 판정 자체는 흔들리지 않는다.

    stdout 첫 줄 `BREACH: yes|no`(셸이 notify_auto/notify_defer 를 가른다),
    이후 카드 본문."""
    import json as _json
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.narrate import make_json_narrator
    from quant.control import exposure as _exposure
    from quant.control import risk_review as rr
    from quant.control.ledger import load_trades, round_trips, scoreboard_text

    root = Path(args.root) if args.root else REPO_ROOT
    today = args.date or _date.today().isoformat()

    dpath = root / rr.RISK_LEDGER
    if dpath.exists():
        for line in dpath.read_text(encoding="utf-8").splitlines():
            try:
                r = _json.loads(line)
            except ValueError:
                continue
            if r.get("producer") == rr.PRODUCER and r.get("date") == today:
                logger.info("risk-review: %s 이미 기록됨 — 건너뜀", today)
                print(rr.format_card(r))
                return

    trades = load_trades(root / "data" / "state" / "trades.jsonl")
    trips = round_trips(trades)
    board_text = scoreboard_text(trips)

    try:
        portfolio = _json.loads(
            (root / "data" / "state" / "portfolio.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        portfolio = {}
    positions = portfolio.get("positions") or {}

    # portfolio.json → exposure.build_report 가 원하는 (lots, prices). 네트워크
    # 조회 없이 평단가로 저하한다 — daily_wrap.build_exposure_summary 와 동일한
    # "읽기만 한다" 원칙(quant.control.daily_wrap._lots_from_positions 참고).
    lots: dict[str, dict[str, float]] = {}
    prices: dict[str, float] = {}
    for symbol, p in positions.items():
        qty = float(p.get("qty", 0) or 0)
        if qty <= 0:
            continue
        meta = p.get("meta") or {}
        active = {
            sid: float(lot.get("qty", 0.0))
            for sid, lot in (meta.get("lots") or {}).items()
            if float(lot.get("qty", 0.0)) > 0
        }
        if not active:
            active = {str(meta.get("strategy") or "?"): qty}
        lots[symbol] = active
        prices[symbol] = float(p.get("avg_cost", 0) or 0)

    exposure_report = _exposure.build_report(lots=lots, prices=prices).to_dict() if lots else None

    consecutive = rr.strategy_consecutive_losses(trips)
    flags = rr.deterministic_flags(exposure_report, consecutive)
    dossier = rr.build_dossier(board_text, exposure_report, flags)

    narrator = make_json_narrator(max_tokens=1200)
    issues = rr.run_review(dossier, narrator.narrate)

    record = rr.to_record(today, flags, issues)
    rr.append_ledger(record, dpath)
    print(rr.format_card(record))


def cmd_pnl_attribution(args: argparse.Namespace) -> None:
    """PnL 귀속 요약 — [엣지(수수료 전 실현) − 수수료 − 세금] 결정론 분해 +
    전략별 상/하위 1개(2026-09-02, 회사형 AI 에이전트 레이어 레인 3). **LLM
    없음** — 숫자 요약에 환각 리스크를 질 이유가 없다(quant.control.
    pnl_attribution 모듈 docstring). 기존 session_pnl_summary/round_trips 를
    재사용할 뿐 새 통계를 만들지 않는다.

    stdout = 정확히 4줄 카드. 이 세션에 체결이 없으면 무출력(침묵) — 셸이
    notify_auto 로 넘긴다."""
    from datetime import date as _date
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from quant.adapters.env import REPO_ROOT
    from quant.control import pnl_attribution as pa
    from quant.control.ledger import load_trades, session_pnl_summary, trades_in_session

    settings = load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    market = args.market
    tz = ZoneInfo("Asia/Seoul") if market == "KR" else ZoneInfo("America/New_York")
    on = _date.fromisoformat(args.date) if args.date else datetime.now(tz).date()

    trades = load_trades(root / "data" / "state" / "trades.jsonl")
    session = session_pnl_summary(trades, market, on)
    if not session["has_trades"]:
        logger.info("pnl-attribution: %s %s 체결 없음 — 침묵", market, on)
        return
    session_trades = trades_in_session(trades, market, on)

    execution_cfg = settings.raw.get("execution") or {}
    tax_bps = float(execution_cfg.get("kr_stock_sell_tax_bps", 0.0))

    decomp = pa.decompose(session, session_trades, tax_bps)
    top, bottom = pa.top_bottom_strategies(session["by_strategy"])
    print(pa.format_summary(market, on.isoformat(), decomp, top, bottom))


def cmd_flow_scan(args: argparse.Namespace) -> None:
    """장중 거래대금 발굴 (2026-08-28 소유자 지시) — 아침 리포트가 못 잡은
    종목이라도 장중 거래대금이 쏠리면 워치리스트 후보로 뽑는다. 발굴만 한다
    — 편입은 own_brief.sh 와 같은 확신도 게이트(watch-score)를 반드시 거친다
    (`server/scripts/flow_scan.sh`가 이 커맨드의 출력을 watch-score 로 넘긴다).

    출력 계약: 후보가 있으면 정확히 한 줄 `FLOW: SYM1 SYM2 ...`(stdout). 없으면
    무출력, exit 0 — 셸이 grep으로 파싱하므로 형식을 바꾸지 말 것. 진단은
    stderr/logger로만 낸다.
    """
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.analyze.flow_scan import flow_candidates
    from quant.apps.assembly import MissingCredentials, build_toss_client
    from quant.trade.universe import FileWatchlistUniverse

    settings = load_settings()
    _redact.install()  # .env/.env.local + settings.yaml 로드

    root = Path(args.root) if args.root else REPO_ROOT
    watch = FileWatchlistUniverse(root / "data" / "watchlist.yaml")
    existing = set(watch.refresh())
    # 정적 앵커(TQQQ/SQQQ 등 settings.yaml 전략 고정 심볼) — 이미 전략이 고정으로
    # 다루는 심볼은 "발굴"할 새 정보가 아니다.
    for strat_cfg in settings.strategies.values():
        existing.update(strat_cfg.get("symbols", []) or [])

    try:
        client = build_toss_client()
    except MissingCredentials as e:
        logger.error("flow-scan: %s", e)
        raise SystemExit(2)

    try:
        result = client.rankings(
            type="MARKET_TRADING_AMOUNT", market_country=args.market,
            duration="realtime", count=args.top,
        )
    except Exception as e:  # noqa: BLE001 — 조회 실패는 발굴 실패일 뿐, 다른 잡을 막지 않는다
        logger.warning("flow-scan: 랭킹 조회 실패 — %s: %s", type(e).__name__, e)
        return

    rows = (result or {}).get("rankings", [])
    candidates = flow_candidates(rows, existing, args.market, top=args.top)
    if not candidates:
        logger.info("flow-scan: %s 신규 후보 없음", args.market)
        return

    print(f"FLOW: {' '.join(candidates)}")


def cmd_kr_flow(args: argparse.Namespace) -> None:
    """KR 마감 후 외국인·기관 수급 스냅샷 (2026-08-26 감사 수리, 크론 15:50).

    ## 왜 별도 잡인가 — 순서 결함

    수급 원장(`frgn_flow.jsonl`)은 그동안 **아침 리포트(07:30)만** 채웠다. 그런데
    아침 리포트가 보는 네이버 값은 전 거래일 종가 기준이라, D-1 세션의 수급은
    D일 07:30 에야 원장에 들어온다. 마감 종합(uswrap)은 D일 **05:50** 에 D-1
    수급을 읽으므로 **구조적으로 항상 비어 있었다**(2026-08-26 확인: 원장 최신
    날짜가 8/24 인데 8/26 05:50 wrap 이 8/25 수급을 찾고 있었다).

    이 잡은 KR 마감(15:30) 뒤에 돌아 **그날** 수급을 원장에 넣는다. 네이버
    `flow_daily` 행은 저마다 자기 날짜를 갖고 있으므로(리포트 날짜가 아니라)
    마감 후 실행이면 당일 행이 그대로 들어온다. 원장 append 는 멱등이라
    다음날 아침 리포트가 같은 행을 다시 넣어도 불어나지 않는다.

    실패해도 아무것도 막지 않는다 — 다음날 아침 리포트가 여전히 채운다(늦게).
    """
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.collect.sources.stock_detail import fetch_many
    from quant.report.collect.ledger import _record_flows, _record_frgn_flow
    from quant.trade.universe import FileWatchlistUniverse

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    watch = FileWatchlistUniverse(root / "data" / "watchlist.yaml")
    symbols = [s for s in watch.refresh() if s.isdigit() and len(s) == 6]
    if not symbols:
        logger.info("kr-flow: 워치리스트에 KR 종목이 없다 — 건너뜀")
        return
    symbols = symbols[: args.limit]
    try:
        details = fetch_many(symbols, limit=args.limit)
    except Exception as e:  # noqa: BLE001 — 수집 실패가 다른 잡을 막지 않는다
        logger.warning("kr-flow: 종목 상세 조회 실패 — %s: %s", type(e).__name__, e)
        return
    logger.info("kr-flow: 종목 상세 %d건 수집", len(details))
    _record_frgn_flow(details, root)
    _record_flows(details, root, args.date or _date.today().isoformat())


def cmd_delivery_check(args: argparse.Namespace) -> None:
    """소식통 배달 점검 — "대표님에게 오늘 산출물이 실제로 닿았는가"만 본다
    (2026-08-26, 소유자 조직도 역할 6). 크론 제안: 화~토 06:35 KST(US 마감
    정산 뒤, 하루 한 바퀴 완료 시점) — 날짜 계산 근거는
    `quant.control.delivery_check` docstring 참고(오늘이 아니라 대부분 전날
    기준).

    전부 정상이면 **침묵**(stdout 무출력, exit 0) — 매일 "정상" 알림은 사람이
    끄고, 끈 알림은 없는 알림이다. 종료코드는 health 관례와 동일: 0=정상 /
    1=미배달 있음 / 2=미배달은 없지만 확인 못 한 게 있음(모름을 정상으로
    합산하지 않는다).
    """
    from datetime import date as _date, timedelta as _timedelta, timezone as _timezone
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.control.delivery_check import (
        MISSING,
        ArtifactStatus,
        check_ai_trader,
        check_artifacts,
        check_log_traces,
        expected_artifacts,
    )

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    if args.date:
        today = _date.fromisoformat(args.date)
    else:
        today = datetime.now(_timezone(_timedelta(hours=9))).date()

    findings = []

    expected = expected_artifacts(today)
    if expected:
        statuses = {}
        for name, target_date in expected.items():
            path = root / "out" / f"{target_date:%Y/%m/%d}" / name
            try:
                size = path.stat().st_size
                statuses[name] = ArtifactStatus(exists=True, size=size)
            except OSError:
                statuses[name] = ArtifactStatus(exists=False, size=0)
        findings.extend(check_artifacts(statuses))

        def _read_lines(path: Path) -> list[str] | None:
            try:
                return path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return None

        target_kr = expected.get("KR_report.html", today)
        logs = {
            "own_brief_KR": _read_lines(root / "data" / "own_brief.log"),
            "own_brief_US": _read_lines(root / "data" / "own_brief.log"),
            "run_report_KR": _read_lines(root / "data" / "report.log"),
            "run_report_US": _read_lines(root / "data" / "report.log"),
        }
        findings.extend(check_log_traces(logs, target_kr))

        ai_trader_lines = _read_lines(root / "data" / "ai_trader.log")
        for market in ("KR", "US"):
            f = check_ai_trader(ai_trader_lines, market, target_kr)
            if f is not None:
                findings.append(f)

    if not findings:
        return

    missing = [f for f in findings if f.level == MISSING]
    lines = [f"📮 소식통 점검: {len(missing)}건 미배달" if missing else "📮 소식통 점검: 확인 못 한 항목 있음"]
    for f in findings:
        mark = "❌" if f.level == MISSING else "❔"
        lines.append(f"{mark} {f.detail}")
    print("\n".join(lines))
    if missing:
        raise SystemExit(1)
    raise SystemExit(2)


def cmd_macro_collect(args: argparse.Namespace) -> None:
    """매크로 금리·환율 시계열 수집(2026-08-28, 소유자 지시 — "시그널이 차트만
    보는 게 아니라 rate 를 함께 보고, 데이터를 미리 수집해 시기별로 ML 학습").

    `quant.adapters.macro.fred.SERIES` 전부를 FRED(fredgraph.csv, 인증 불필요)
    에서 받아 `data/ledger/macro_rates.jsonl`에 멱등 append(같은 (date, series)는
    최신값으로 갱신)한다. 국면(`quant.trade.regime`)의 US_BOND_10Y는 이 원장을
    파일로만 읽는다(`quant.adapters.regime_indicators.FileMacroIndicatorClient`)
    — 네트워크는 이 배치 커맨드에만 있고 거래 핫패스로는 새지 않는다.

    `--days 0`(기본)은 전체 과거 백필(첫 실행용). 크론(server/scripts/
    macro_collect.sh, 매일 1회)은 작은 `--days`로 최근분만 갱신해 매일 전체
    이력을 다시 받는 낭비를 줄인다. 일부 시리즈가 실패해도 나머지는 계속
    진행하고 실패분을 출력에 명시한다(정직하게 실패를 숨기지 않는다)."""
    from datetime import date as _date, timedelta as _timedelta
    from pathlib import Path

    from quant.adapters.macro.fred import DEFAULT_LEDGER_PATH, SERIES, append_macro_rows, fetch_series

    root = Path(args.root)
    ledger_path = root / DEFAULT_LEDGER_PATH
    cutoff = None
    if args.days > 0:
        cutoff = (_date.today() - _timedelta(days=args.days)).isoformat()

    parts: list[str] = []
    failed: list[str] = []
    for name, series_id in SERIES.items():
        points = fetch_series(series_id)
        if points is None:
            failed.append(name)
            continue
        if cutoff is not None:
            points = [(d, v) for d, v in points if d >= cutoff]
        rows = [{"date": d, "series": name, "value": v} for d, v in points]
        append_macro_rows(rows, path=ledger_path)
        parts.append(f"{name} {len(rows)}건")

    line = "수집: " + " · ".join(parts) if parts else "수집: 전체 실패"
    if failed:
        line += f" (실패: {', '.join(failed)})"
    print(line)
    if not parts:
        raise SystemExit(1)


def cmd_param_propose(args: argparse.Namespace) -> None:
    """전략 파라미터 제안 — AI 트레이더 3단계 (토 06:40 크론, 2026-08-26).

    주간 원장 요약 + 현재 파라미터를 LLM 에게 주고 변경 가설(최대 3건)을 받아
    **제안만** 기록·출력한다. stdout = 텔레그램 노트(무제안/결근이면 무출력).
    반영은 사람이 settings.yaml 로, 판정은 experiments 루프(16:30)가 한다.

    LLM 정책(소유자 2026-08-26): 논리가 중요한 작업 — Claude Code CLI 1순위,
    실패 시 OpenRouter 무료 레인 폴백.
    """
    import json as _json
    import os as _os
    from datetime import date as _date
    from pathlib import Path

    import yaml as _yaml

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.narrate import make_json_narrator, make_narrator
    from quant.analyze import param_proposer
    from quant.control.ledger import load_trades, round_trips
    from quant.control.weekly_review import (
        loss_patterns, week_range, weekly_review_text, weekly_strategy_stats,
    )

    settings = load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    today = _date.fromisoformat(args.date) if args.date else _date.today()
    start, end = week_range(today)
    week = f"{start.isocalendar().year}-W{start.isocalendar().week:02d}"

    trades = load_trades(root / "data" / "state" / "trades.jsonl")
    trips = round_trips(trades)
    stats = weekly_strategy_stats(trips, start, end)
    losses = loss_patterns(trips, start, end)
    review_text = weekly_review_text(start, end, index_flow=[], strategy_stats=stats,
                                     losses=losses, score_accuracy=None, equity_delta=None)

    active = {sid for sid, s in (settings.strategies or {}).items()
              if isinstance(s, dict) and s.get("enabled")}
    if not active:
        logger.info("param-propose: 활성 전략 없음 — 침묵")
        return
    params_yaml = _yaml.safe_dump(
        {sid: (settings.strategies[sid] or {}).get("params", {}) for sid in sorted(active)},
        allow_unicode=True, sort_keys=False)

    # Claude CLI 1순위(논리 중요) → OpenRouter 무료 폴백. 폴백 사용 여부를
    # 제안 원장에 남긴다(어느 모델의 제안이었는지가 나중의 메타 데이터다).
    claude = make_narrator(env={**_os.environ, "OPS_NARRATOR": "claude"})
    used = "claude-cli"

    def narrate(prompt: str) -> str | None:
        nonlocal used
        out = claude.narrate(prompt)
        if out is not None:
            return out
        used = "openrouter-free"
        logger.warning("param-propose: claude CLI 실패 — OpenRouter 무료 레인 폴백")
        return make_json_narrator(max_tokens=4000).narrate(prompt)

    result = param_proposer.propose(review_text, params_yaml, active, narrate)
    if result is None:
        logger.info("param-propose: %s 제안 없음/결근 — 침묵", week)
        return

    for p in result["proposals"]:
        p["llm"] = used
    added = param_proposer.append_proposals(
        result["proposals"], root / param_proposer.PROPOSALS_LEDGER, week)
    logger.info("param-propose: %s 제안 %d건(신규 %d, llm=%s)",
                week, len(result["proposals"]), added, used)

    # 거버너 형태로도 같은 원장에 남긴다 — governor-apply(2026-08-28 배선)가 읽는
    # 스키마는 {name, samples, expected_improvement, ...}인데, 위 append_proposals
    # 가 쓰는 스키마는 {param, risk, verify, ...}라 그대로는 못 쓴다. **LLM 응답에
    # samples/expected_improvement 가 실제로 있을 때만** 골라 담는다 — 못 뽑으면
    # 추측해서 채우지 않고 그 제안은 governor-apply 에 안 보인다(정량 근거 없는
    # 제안을 자동 반영 심사에 넣지 않는다는 뜻이라 옳은 동작이다).
    #
    # name = "strategies.<strategy>.params.<param>" — governor.ALLOWED*의 이름은
    # config/settings.yaml 의 점(.) 표기 전체 경로인데, LLM이 돌려주는 param은
    # 그 리프 이름뿐이다(예: "volume_surge_mult"). 2026-09-02까지 여기서 그냥
    # p["param"]을 name으로 썼는데, 그건 ALLOWED의 어떤 키와도 절대 일치하지
    # 않는다 — governor 형태로 남아도 _load_recent_governor_proposals가 읽어
    # decide()에 넣는 순간 항상 "0-blast-radius: 허용 목록에 없음"으로 거부됐다
    # (실측: EC2 data/ledger/param_proposals.jsonl 2026-W35 제안 2건은 애초에
    # samples/expected_improvement가 없어 governor_rows 자체가 비었지만, 있었더라도
    # 이 경로 결합 버그 때문에 반영될 수 없었다 — governor 자동 반영이 저장소
    # 역사상 한 번도 일어나지 않은 실제 원인 중 하나).
    import re as _re
    governor_rows: list[dict] = []
    raw_by_key: dict[tuple, dict] = {}
    m = _re.search(r"\{.*\}", result.get("raw") or "", _re.DOTALL)
    if m:
        try:
            raw_data = _json.loads(m.group(0))
        except ValueError:
            raw_data = {}
        for rp in raw_data.get("proposals", []) if isinstance(raw_data, dict) else []:
            if isinstance(rp, dict) and rp.get("strategy") and rp.get("param"):
                raw_by_key[(str(rp["strategy"]), str(rp["param"]))] = rp
    for p in result["proposals"]:
        rp = raw_by_key.get((p["strategy"], p["param"]), {})
        samples = rp.get("samples")
        improvement = rp.get("expected_improvement")
        if not isinstance(samples, (int, float)) or not isinstance(improvement, (int, float)):
            continue
        governor_rows.append({
            "date": today.isoformat(), "strategy": p["strategy"],
            "name": f"strategies.{p['strategy']}.params.{p['param']}",
            "current": p["current"], "proposed": p["proposed"], "samples": samples,
            "expected_improvement": improvement, "rationale": p["rationale"], "llm": used,
        })
    if governor_rows:
        gpath = root / param_proposer.PROPOSALS_LEDGER
        gpath.parent.mkdir(parents=True, exist_ok=True)
        with gpath.open("a", encoding="utf-8") as f:
            for row in governor_rows:
                f.write(_json.dumps(row, ensure_ascii=False) + "\n")
        logger.info("param-propose: 거버너 형태 제안 %d건 기록", len(governor_rows))

    print(result["note"])


# (2026-08-30) 예전엔 여기에 GOVERNOR_SETTINGS_PATH — governor.ALLOWED 의 이름
# (analyze 평면 모듈 상수)을 config/settings.yaml 경로로 잇는 매핑 테이블 — 이
# 있었지만 매핑이 채워진 적이 없었다(2026-08-28 실측: ALLOWED 7개 중 어느 것도
# settings.yaml에 없었다). governor.ALLOWED 재정의(quant/control/governor.py)로
# 이름 자체가 이제 settings.yaml 의 점(.) 표기 경로다 — 별도 매핑이 필요 없어
# 이 테이블은 삭제했다. `name.split(".")` 가 바로 `_write_overlay` 가 요구하는
# path_tuple 이다(아래 cmd_governor_apply).


def _load_recent_governor_proposals(path, today, window_days: int) -> list:
    """`data/ledger/param_proposals.jsonl` 에서 최근 `window_days`일치의
    governor.Proposal 형태 줄만 골라 변환한다.

    이 원장은 두 스키마가 섞여 있다: `cmd_param_propose`(기존)가 쓰는
    `{strategy, param, risk, verify, ...}`와, 같은 커맨드가 새로 추가한
    `{name, samples, expected_improvement, ...}`(governor 형태). name/samples/
    expected_improvement 가 전부 있는 줄만 governor.Proposal 이 될 수 있다 —
    없는 줄(구 스키마)은 자연히 건너뛴다.
    """
    import json as _json
    from datetime import date as _date, timedelta as _timedelta
    from pathlib import Path

    from quant.control import governor

    p = Path(path)
    if not p.exists():
        return []
    cutoff = today - _timedelta(days=window_days)
    out: list[governor.Proposal] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except ValueError:
            continue
        if any(row.get(k) is None for k in
               ("name", "current", "proposed", "samples", "expected_improvement")):
            continue
        try:
            d = _date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            continue
        if d < cutoff:
            continue
        out.append(governor.Proposal(
            name=row["name"], current=row["current"], proposed=row["proposed"],
            samples=row["samples"], expected_improvement=row["expected_improvement"],
            rationale=row.get("rationale", ""),
        ))
    return out


def _write_overlay(overlay_path, updates: dict[str, tuple]) -> None:
    """`{name: (path_tuple, value)}` 를 config/auto_params.yaml 에 깊은 병합으로
    적는다. 값이 None 이면 그 키를 오버레이에서 지운다(=원래 settings.yaml
    값으로 복귀 — 롤백에 쓴다)."""
    import yaml as _yaml

    overlay: dict = {}
    if overlay_path.exists():
        overlay = _yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    for _name, (path_tuple, value) in updates.items():
        node = overlay
        for key in path_tuple[:-1]:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(path_tuple[-1], None)
        else:
            node[path_tuple[-1]] = value
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(
        _yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _realized_performance_deltas(root: Path, decisions: list[dict], min_samples: int) -> dict[str, float]:
    """반영된 변경들의 "반영 전후 성과 변화율" — `governor.rollback_candidates`
    가 요구하는 `realized` 딕셔너리를 채운다.

    **판단이 아니라 근사다.** governor.ALLOWED 는 analyze 평면의 종목 선정
    문턱이라 거래 전략 하나에 귀속되지 않는다 — `quant.control.experiments`
    의 이중차분(DiD)은 `quant/trade` 전략별 파라미터 지문에 귀속되는 비교라
    여기 그대로 못 쓴다(대조군이 성립하지 않는다: 선정 문턱을 바꾸면 3개
    전략 전부의 유니버스가 같이 바뀐다). 그래서 반영일 기준 원장 전체
    (`trades.jsonl`)의 전/후 평균 bps 변화율로 근사한다 — 전략별 인과까지는
    못 잡지만, "이 변경 이후로 전체 성과가 눈에 띄게 나빠졌는가"는 잡는다.
    전/후 각각 `min_samples`건을 못 채운 이름은 realized 에서 빠지고,
    `rollback_candidates()` 가 그 경우 자동으로 후보에서 제외한다(None 처리)."""
    from quant.control.ledger import load_trades, round_trips

    trips = round_trips(load_trades(root / "data" / "state" / "trades.jsonl"))
    out: dict[str, float] = {}
    for row in decisions:
        if not row.get("accepted"):
            continue
        name = row.get("name")
        change_date = row.get("date")
        if not name or not change_date or name in out:
            continue
        before, after = [], []
        for t in trips:
            bps = t.get("bps")
            ts = t.get("exit_ts") or t.get("entry_ts")
            if bps is None or ts is None:
                continue
            (after if str(ts)[:10] >= change_date else before).append(float(bps))
        if len(before) < min_samples or len(after) < min_samples:
            continue
        mean_before = sum(before) / len(before)
        mean_after = sum(after) / len(after)
        if mean_before == 0:
            continue
        out[name] = (mean_after - mean_before) / abs(mean_before)
    return out


def _meta_update(name: str, today_iso: str | None) -> tuple[str, tuple]:
    """`config/auto_params.yaml` 의 `_meta.<name>.applied_at` 갱신용 update 엔트리.

    `_meta` 는 실제 settings 트리와 분리된 예약 최상위 키다 — 반영값 자체(리프)
    옆에 메타데이터를 끼워 넣으면 그 값이 그대로 전략 params 로 흘러들어가
    오염된다(예: `strategies.vol_breakout.params.applied_at` 같은 존재하지 않는
    키가 생김). `_meta` 는 `Settings` 의 어떤 접근자도 읽지 않으므로 병합돼도
    무해하다 — 사람이 auto_params.yaml 을 열어 "언제 반영됐나"를 바로 보라고
    두는 감사용 사이드카일 뿐이다. `today_iso=None` 이면 되돌리기(삭제)다."""
    value = {"applied_at": today_iso} if today_iso is not None else None
    return f"_meta:{name}", (("_meta", name), value)


def _apply_live_gate(decisions: list, live: bool) -> None:
    """`--live` 없이(기본값) 실행되면 governor 6~7층을 통과한 결정도 오버레이엔
    안 쓴다 — "자동 반영은 ALLOWED 범위 안만, 나머지는 전부 제안"(소유자 승인,
    2026-08-30)의 CLI 쪽 표현.

    accepted 를 실제로 내려야 하는 이유(단순히 쓰기를 건너뛰는 것으로는 부족한
    이유): `decisions.jsonl` 의 accepted=True 는 `governor.last_change()` 가
    "이 파라미터가 실제로 반영된 날"로 읽어 층 3(냉각)의 기준점이 된다. 미반영을
    accepted=True 로 남기면 다음 실행이 "이미 최근에 바뀌었다"고 오판해 근거
    없는 냉각을 건다 — 아무것도 안 바뀌었는데 말이다."""
    if live:
        return
    for d in decisions:
        if d.accepted:
            d.accepted = False
            d.applied_value = None
            d.layer = "not-live"
            d.reason = f"{d.reason} — 그러나 --live 없이 실행돼 제안만(실반영 보류)"


def _governor_revert(key: str, overlay_path: Path, decisions_path: Path, today,
                      *, dry_run: bool) -> None:
    """`governor-apply --revert <key>` — 자동 반영분을 사람이 한 줄로 되돌린다.

    `key` 는 `governor.ALLOWED` 의 이름(=settings.yaml 점 표기 경로)이어야 한다
    — 거버너가 애초에 건드릴 권한이 없던 키를 이 문으로 우회해 건드리게 하지
    않는다. 오버레이에서 그 키(와 `_meta` 의 applied_at)를 지우면 다음 핫
    리로드부터 settings.yaml 의 원래 값으로 자연 복귀한다 — `_write_overlay`
    의 "값이 None이면 삭제" 관례를 그대로 재사용한다(config.py 모듈 docstring
    "되돌리기는 그 파일에서 키를 지우는 것뿐" 원칙)."""
    from quant.control import governor

    if (key not in governor.ALLOWED and key not in governor.ALLOWED_ORDINAL
            and key not in governor.ALLOWED_KILL_SWITCH):
        logger.error(
            "governor-apply --revert: %s 는 governor.ALLOWED/ALLOWED_ORDINAL/"
            "ALLOWED_KILL_SWITCH 어디에도 없음 — 되돌릴 수 없음", key)
        return

    if not overlay_path.exists():
        logger.info("governor-apply --revert: 오버레이 파일 없음 — %s 는 이미 반영 상태가 아님", key)
        return

    import yaml as _yaml

    overlay = _yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
    node: object = overlay
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            break
    if node is None:
        logger.info("governor-apply --revert: %s 는 오버레이에 없음 — 이미 settings.yaml 기본값", key)
        return
    current_overlay_value = node

    if dry_run:
        print(f"[dry-run] governor-apply --revert: {key} = {current_overlay_value} "
              "→ 오버레이에서 제거(settings.yaml 기본값으로 복귀 예정)")
        return

    path_tuple = tuple(key.split("."))
    meta_name, meta_update = _meta_update(key, None)
    _write_overlay(overlay_path, {key: (path_tuple, None), meta_name: meta_update})

    decision = governor.Decision(
        proposal=governor.Proposal(
            name=key, current=current_overlay_value, proposed=0.0,
            samples=0, expected_improvement=0.0, rationale="사람이 --revert 로 수동 되돌림"),
        accepted=True,
        reason="수동 되돌림 — 오버레이에서 제거, settings.yaml 기본값으로 복귀",
        applied_value=None, layer="revert",
    )
    governor.record([decision], today, decisions_path)
    print(f"governor-apply --revert: {key} 제거 완료 (오버레이 값 {current_overlay_value} "
          "→ settings.yaml 기본값)")


def cmd_governor_apply(args: argparse.Namespace) -> None:
    """파라미터 자동 반영 심사 + 적용 — 거버너 배선 (2026-08-28, ALLOWED
    재정의·--live/--revert 2026-08-30).

    `quant/control/governor.py` 는 완성돼 있었지만 이걸 부르는 프로덕션 코드가
    없었다(제안은 나오는데 아무도 심사·반영하지 않는 열린 루프). 이 커맨드가
    그 마지막 칸을 채운다: 최근 제안(`data/ledger/param_proposals.jsonl`,
    기본 `--window-days`일)을 `governor.Proposal` 로 변환해 `governor.decide()`
    에 태우고, ALLOWED 범위(방향·봉투·보폭·냉각) 안에서 6층을 통과한 것만
    `config/auto_params.yaml` 오버레이에 반영한다. settings.yaml 은 절대 직접
    쓰지 않는다 — governor.ALLOWED 의 이름이 이제 그 경로 자체다
    (`name.split(".")` 가 곧 `_write_overlay` 의 path_tuple).

    **`--live` 없이는(기본값) 실반영하지 않는다** — 6층을 통과한 결정도
    decisions.jsonl 에 accepted=False, layer='not-live' 로 "제안"으로만 남는다
    (`_apply_live_gate` 참고). `--dry-run` 은 `--live` 보다 항상 우선한다 —
    심사만 하고 파일(오버레이·decisions.jsonl) 자체를 안 건드린다.

    `--revert <key>` 를 주면 위 심사를 건너뛰고 그 키 하나만 오버레이에서
    제거한다(`_governor_revert`).

    수락(그리고 실반영) 0건이면 조용히 종료한다 — 매일 조용한 게 기본값인 이
    저장소의 다른 크론과 같은 관례를 따른다.
    """
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.control import governor
    from quant.control.experiments import DEFAULT_DEATH_WATCH_PATH, consecutive_dead_candidates
    from quant.control.ledger import base_strategy_id

    root = Path(args.root) if args.root else REPO_ROOT
    today = _date.fromisoformat(args.date) if args.date else _date.today()
    decisions_path = root / "data" / "ledger" / "decisions.jsonl"
    overlay_path = root / "config" / "auto_params.yaml"

    if getattr(args, "revert", None):
        _governor_revert(args.revert, overlay_path, decisions_path, today, dry_run=args.dry_run)
        return

    proposals_path = root / "data" / "ledger" / "param_proposals.jsonl"
    death_watch_path = root / DEFAULT_DEATH_WATCH_PATH

    proposals = _load_recent_governor_proposals(proposals_path, today, args.window_days)

    # 사망 판정 지속 → 자동 비활성 후보(작업2, 2026-09-02). cmd_experiments가
    # 매일 쌓아 온 death_watch.jsonl(K거래일 연속 p<0.01)을 읽어, 여기서 이미
    # 도는 governor.decide() 파이프라인에 그대로 태운다 — 새 반영 경로를
    # 만들지 않는다(governor.ALLOWED_KILL_SWITCH가 방향·냉각·표본 심사를 한다).
    # 보호 목록(config/settings.yaml governor.protected_strategies — 2026-08-30
    # 소유자 지시 scalp_1m)은 제안 자체를 만들지 않고 별도 알림 문구로만 남긴다.
    settings = load_settings()
    protected = set((settings.raw.get("governor") or {}).get("protected_strategies") or [])
    protected_notes: list[str] = []
    for c in consecutive_dead_candidates(death_watch_path):
        sid = c["strategy"]
        # A/B 촉매 갈래(`<id>_cat`)는 기준 전략의 보호를 **상속**한다(2026-09-03).
        # 두 갈래는 같은 클래스이고, 한쪽만 자동 비활성되면 남은 쪽이 계속 돌면서
        # 비교 자체가 무의미해진다 — 실험을 끝내는 것은 사람의 판단이다.
        if base_strategy_id(sid) in protected:
            protected_notes.append(
                f"⚠️ [{sid}] 사망 경보 {c['streak_days']}거래일 연속 지속 "
                f"(평균 {c['mean_bp']:+.1f}bp/건, p={c['p_value']:.3f}, n={c['n']}) "
                "— 보호 목록이라 자동 비활성 대상에서 제외. 개선 실험 필요."
            )
            continue
        proposals.append(governor.Proposal(
            name=f"strategies.{sid}.enabled", current=True, proposed=False,
            samples=c["n"], expected_improvement=1.0,
            rationale=(f"{c['streak_days']}거래일 연속 사망 판정: 평균 {c['mean_bp']:+.1f}bp/건 "
                       f"(p={c['p_value']:.3f}, n={c['n']}, {c['since']}~{c['until']})"),
        ))

    if not proposals and not protected_notes:
        logger.info("governor-apply: 최근 %d일 governor 형태 제안 없음 — 침묵", args.window_days)
        return

    decisions = governor.decide(proposals, today, ledger_path=decisions_path) if proposals else []
    # 아래 _apply_live_gate 가 accepted 를 내리기 전에, "governor 가 실제로
    # 뭐라고 판단했는가"를 사람이 보는 요약용으로 먼저 굳혀 둔다 — --live 여부와
    # 무관하게 항상 진짜 판정을 보여준다.
    would_apply = any(d.accepted for d in decisions)
    report_lines = [governor.summary(decisions)]

    _apply_live_gate(decisions, args.live)

    updates: dict[str, tuple] = {}
    for d in decisions:
        if not d.accepted:
            continue
        path_tuple = tuple(d.proposal.name.split("."))
        updates[d.proposal.name] = (path_tuple, d.applied_value)
        meta_name, meta_update = _meta_update(d.proposal.name, today.isoformat())
        updates[meta_name] = meta_update

    if not args.dry_run:
        if updates:
            _write_overlay(overlay_path, updates)
        governor.record(decisions, today, decisions_path)

    # --- 자동 롤백: 과거 반영분 전체(이번 회차 포함) 중 성과가 나빠진 것 ---
    full_history = governor._history(decisions_path)
    realized = _realized_performance_deltas(root, full_history, governor.MIN_SAMPLES)
    candidates = governor.rollback_candidates(full_history, realized)
    rollback_decisions: list[governor.Decision] = []
    for c in candidates:
        name = c["name"]
        if (name not in governor.ALLOWED and name not in governor.ALLOWED_ORDINAL
                and name not in governor.ALLOWED_KILL_SWITCH):
            logger.warning("governor-apply: 롤백 대상 %s 가 ALLOWED 밖 — 되돌릴 곳이 없다", name)
            continue
        prev_value = c.get("current")
        rollback_decisions.append(governor.Decision(
            proposal=governor.Proposal(
                name=name, current=c.get("applied"), proposed=prev_value,
                samples=0, expected_improvement=0.0,
                rationale=f"자동 롤백: 반영 후 실현 변화율 {c['realized_change']:+.1%}"),
            accepted=True,
            reason=(f"자동 롤백 — 반영 후 {c['realized_change']:+.1%} 악화 "
                    f"(임계 {governor.ROLLBACK_DEGRADE:.0%})"),
            applied_value=prev_value, layer="6-rollback",
        ))

    if rollback_decisions:
        report_lines.append(governor.summary(rollback_decisions))
        _apply_live_gate(rollback_decisions, args.live)

        rollback_updates: dict[str, tuple] = {}
        for d in rollback_decisions:
            if not d.accepted:
                continue
            name = d.proposal.name
            path_tuple = tuple(name.split("."))
            rollback_updates[name] = (path_tuple, d.applied_value)
            meta_name, meta_update = _meta_update(name, None)  # 롤백=삭제라 메타도 지운다
            rollback_updates[meta_name] = meta_update

        if not args.dry_run:
            if rollback_updates:
                _write_overlay(overlay_path, rollback_updates)
            governor.record(rollback_decisions, today, decisions_path)

    if protected_notes:
        report_lines.append("\n".join(protected_notes))

    if not would_apply and not rollback_decisions and not protected_notes:
        if not decisions:
            logger.info("governor-apply: 판단할 제안 없음 — 침묵")
            return
        # 제안은 있었지만 전부 거부됐다 — "ALLOWED 밖/방향 위반/증거 부족이라
        # 제안만, 반영은 사람"이라는 사실 자체가 정보다(작업1.3, 소유자 지시:
        # 양방향 튜닝이라 자동 반영 대상이 아닌 제안은 그 사실을 로그·텔레그램에
        # 남겨라). report_lines[0]에 이미 governor.summary(decisions)의 거부
        # 사유별 목록이 있으므로 여기서는 조용히 반환하지 않고 그대로 출력한다.
        logger.info("governor-apply: 수락 0건 — 제안만 남음(반영은 사람)")

    if not args.live:
        report_lines.append("⚠️ --live 없이 실행 — 위 ✅ 는 반영 대기(제안) 상태다. "
                             "오버레이(config/auto_params.yaml)는 건드리지 않았다.")
    print("\n".join(report_lines))


def _capital_stats_from_trips(trips: list[dict]) -> list:
    """`ledger.round_trips()` 출력을 전략별 `allocator.StrategyStat` 목록으로.

    시장(KR/US)은 여기서 섞는다 — 강등 판단(`is_losing`)은 전략 단위 증거이고,
    `allocator.decide()`가 그 판단을 시장별 `capital_fraction` 각각에 적용한다.
    market 필드 자체는 `ledger._market_of`가 이미 채워 뒀으므로(round_trips 결과)
    여기서 새로 판정하지 않는다 — 그냥 쓰지 않을 뿐이다.
    """
    import statistics as _statistics

    from quant.control import allocator

    by_strategy: dict[str, list[float]] = {}
    for t in trips:
        if not t.get("pnl_known"):
            continue
        by_strategy.setdefault(str(t.get("strategy", "?")), []).append(float(t.get("bps", 0.0)))

    stats = []
    for strategy, bps_list in by_strategy.items():
        n = len(bps_list)
        mean_bp = sum(bps_list) / n
        stdev_bp = _statistics.stdev(bps_list) if n >= 2 else 0.0
        stats.append(allocator.StrategyStat(strategy=strategy, n=n, mean_bp=mean_bp, stdev_bp=stdev_bp))
    return stats


def _capital_current_fractions(settings) -> dict[tuple[str, str], float]:
    """settings(+오버레이 병합)의 `strategies.*.capital_fraction`을 `{(전략, 시장): 비율}`로.

    시장별로 나뉘지 않은(스칼라) capital_fraction 전략(예: orb)은 이 장치의
    대상이 아니다 — Demotion이 요구하는 (전략, 시장) 키를 만들 수 없다."""
    out: dict[tuple[str, str], float] = {}
    for name, block in (settings.strategies or {}).items():
        cf = block.get("capital_fraction") if isinstance(block, dict) else None
        if isinstance(cf, dict):
            for market, value in cf.items():
                try:
                    out[(name, str(market))] = float(value)
                except (TypeError, ValueError):
                    continue
    return out


def _capital_last_change_days(path, today) -> dict[str, int | None]:
    """`capital_decisions.jsonl`에서 전략별 마지막 **실제 강등**(applied=True) 이후
    경과일. 강등된 적 없으면 None(=냉각 대상 아님)."""
    from datetime import date as _date

    from quant.control import governor as _governor

    rows = _governor._history(path)
    best: dict[str, _date] = {}
    for row in rows:
        if not row.get("applied"):
            continue
        strategy = row.get("strategy")
        try:
            d = _date.fromisoformat(str(row.get("date", "")))
        except ValueError:
            continue
        if strategy and (strategy not in best or d > best[strategy]):
            best[strategy] = d
    return {s: (today - d).days for s, d in best.items()}


def _record_capital_decisions(demotions: list, today, path) -> None:
    """`Demotion` 목록을 append-only로 남긴다. 적용/스킵 모두 남긴다 — 오너 규율
    4 "거부·무변경도 기록한다"."""
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in demotions:
            f.write(_json.dumps({
                "date": today.isoformat(),
                "strategy": d.strategy,
                "market": d.market,
                "current": d.current,
                "proposed": d.proposed,
                "applied": d.applied,
                "reason": d.reason,
                "skip_reason": d.skip_reason,
            }, ensure_ascii=False) + "\n")


def _capital_review_summary(demotions: list) -> str:
    """텔레그램/stdout용 한 문단. 강등이 실제로 일어난 항목만 본문에 낸다 —
    스킵 사유는 decisions.jsonl에는 남지만 사람에게 매일 스팸으로 보내지 않는다."""
    applied = [d for d in demotions if d.applied]
    lines = [f"📉 자본 자동 강등 — {len(applied)}건"]
    for d in applied:
        lines.append(f"  {d.strategy}[{d.market}]: {d.current} → {d.proposed}")
        lines.append(f"    ↳ {d.reason}")
    lines.append("되돌리려면 config/auto_params.yaml 에서 해당 키를 지우십시오.")
    return "\n".join(lines)


def _promote_evidence_summary(evidence) -> str:
    """`promote --list`용 한 줄 요약. evidence는 backtest_pass/verified 승격
    시 dict(quant/control/promotion.py `_build_evidence`)거나, 전작에서 계승한
    구식 문자열(예: donchian)일 수 있다 — 둘 다 사람이 읽을 한 줄로 낮춘다."""
    if isinstance(evidence, dict) and evidence:
        return (
            f"verdict={evidence.get('verdict')} oos={evidence.get('oos_trades')} "
            f"exp={evidence.get('expectancy_bp')}bp dsr={evidence.get('deflated_sharpe')} "
            f"promoted_at={evidence.get('promoted_at')}"
        )
    if evidence:
        return str(evidence)
    return "(없음)"


def cmd_promote(args: argparse.Namespace) -> None:
    """백테스트 게이트 GO → config/settings.yaml 반영 (2026-09-03).

    로컬 워크플로: `backtest-gate`로 게이트 JSON을 만들고 → `promote --dry-run`으로
    정확히 무엇이 바뀔지 확인하고 → `promote`로 실제 반영한다. 심사(`check_promotable`)는
    `--dry-run` 여부와 무관하게 항상 먼저 돈다 — 막는 이유가 하나라도 있으면 그
    이유 전부를 출력하고 종료코드 2로 끝난다(파일은 건드리지 않는다). 통과했을 때만
    `--dry-run`이 "실제로 쓰지 않고 정확한 diff만 보여주기"와 "그대로 반영" 사이를
    가른다.

    `--capital-fraction KR=0.05,US=0.05`는 선택 — 안 주면 settings.yaml에 이미
    선언된 capital_fraction을 그대로 둔다(단순 승격은 enabled/validation만 바꾼다).

    settings.yaml에 직접 쓰는 이유·주석 보존 방식은 `quant/control/promotion.py`
    모듈 docstring 참고 — 오버레이(config/auto_params.yaml)가 아니라 대상 전략
    블록의 필드 줄만 텍스트 수준으로 교체한다.
    """
    import difflib
    import sys as _sys
    from pathlib import Path

    import yaml as _yaml

    from quant.apps.config import DEFAULT_SETTINGS_PATH
    from quant.control import promotion

    settings_path = Path(args.settings) if args.settings else Path(DEFAULT_SETTINGS_PATH)

    if args.list:
        settings = _yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        for sid, strat_cfg in sorted((settings.get("strategies") or {}).items()):
            v = (strat_cfg or {}).get("validation") or {}
            status = v.get("status", "burn_in")
            print(
                f"{sid}: enabled={strat_cfg.get('enabled', True)} status={status} "
                f"evidence={_promote_evidence_summary(v.get('evidence'))}"
            )
        return

    if not args.strategy or not args.gate:
        print("오류: --strategy 와 --gate 가 필요하다 (또는 --list)", file=_sys.stderr)
        raise SystemExit(2)

    gate = promotion.load_gate(args.gate)
    original_text = settings_path.read_text(encoding="utf-8")
    settings = _yaml.safe_load(original_text) or {}

    reasons = promotion.check_promotable(gate, strategy_id=args.strategy, settings=settings)
    if reasons:
        print(f"승격 불가 — {args.strategy}:")
        for r in reasons:
            print(f"  - {r}")
        raise SystemExit(2)

    capital_fraction = None
    if args.capital_fraction:
        capital_fraction = {}
        for kv in args.capital_fraction.split(","):
            key, _sep, value = kv.partition("=")
            capital_fraction[key.strip().upper()] = float(value)

    if args.dry_run:
        new_text = promotion.render_promoted_settings(
            original_text, args.strategy, gate, capital_fraction=capital_fraction,
        )
        diff = difflib.unified_diff(
            original_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=str(settings_path), tofile=f"{settings_path} (승격 후)",
        )
        _sys.stdout.writelines(diff)
        return

    promotion.apply_promotion(settings_path, args.strategy, gate, capital_fraction=capital_fraction)
    print(f"승격 완료: {args.strategy} → enabled=true, validation.status=backtest_pass ({settings_path})")
    print()
    print("다음 단계: 커밋 → make deploy (KR 마감 후) → scoreboard 30왕복 → 소유자 라이브 판단")


def cmd_capital_review(args: argparse.Namespace) -> None:
    """자본 자동 강등 장치 — "지는 곳에서 자본을 뺀다"(소유자 북극성, 2026-08-28).

    실측(원장 399건): 전략 7종 전부 수수료 전에도 음수. 어느 전략도 개선되지
    않아도 지는 곳의 배분을 줄이면 포트폴리오는 매일 나아진다. `quant/control/
    allocator.py`가 판단(6층은 아니고 4층: 증거·하한·냉각·한 방향)을 하고, 여기가
    원장을 읽고 `config/auto_params.yaml` 오버레이에 반영한다 — `governor.py`/
    `cmd_governor_apply`와 같은 분리(순수 로직 vs 배선), `_write_overlay`도 그대로
    재사용한다(복제하지 않는다).

    **한 방향만 자동이다 — 자본을 줄이는 것만.** 늘리는 것은 사람이
    `config/settings.yaml`을 직접 고쳐야 한다.

    강등 후보가 아예 없으면(증거 없음) 아무 파일도 건드리지 않고 조용히
    끝난다 — `cmd_governor_apply`가 제안 0건일 때 조용한 것과 같은 관례.
    후보는 있었지만 전부 스킵(냉각/하한)이어도 `capital_decisions.jsonl`에는
    남긴다(규율 4) — 단, `config/auto_params.yaml`은 실제 강등(applied=True)이
    최소 1건 있을 때만 쓴다.
    """
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.apps.config import load_settings
    from quant.control import allocator
    from quant.control.ledger import load_trades, round_trips

    root = Path(args.root) if args.root else REPO_ROOT
    today = _date.today()
    decisions_path = root / "data" / "ledger" / "capital_decisions.jsonl"
    overlay_path = root / "config" / "auto_params.yaml"

    trips = round_trips(load_trades(root / "data" / "state" / "trades.jsonl"))
    stats = _capital_stats_from_trips(trips)
    if not stats:
        logger.info("capital-review: 종결 트레이드 없음 — 침묵")
        return

    settings = load_settings(str(root / "config" / "settings.yaml"))
    current_fractions = _capital_current_fractions(settings)
    last_change_days = _capital_last_change_days(decisions_path, today)

    demotions = allocator.decide(
        stats, current_fractions, last_change_days,
        min_samples=args.min_samples,
    )
    if not demotions:
        logger.info("capital-review: 강등 후보 없음(증거 부족 또는 전부 양호) — 침묵")
        return

    applied = [d for d in demotions if d.applied]

    if not args.dry_run:
        if applied:
            updates = {
                f"{d.strategy}:{d.market}": (
                    ("strategies", d.strategy, "capital_fraction", d.market), d.proposed,
                )
                for d in applied
            }
            _write_overlay(overlay_path, updates)
        _record_capital_decisions(demotions, today, decisions_path)

    if not applied:
        logger.info("capital-review: 후보는 있었으나 전부 스킵(냉각/하한) — 조용히 종료")
        return

    print(_capital_review_summary(demotions))


def cmd_close_report(args: argparse.Namespace) -> None:
    """장마감 결과 리포트 — 그날 만기가 채워진 선정 원장 outcome + 리더보드 판정 +
    누적 스코어보드를 한 장으로 (§E-3). 매일 outcomes(16:00) 직후 크론.

    narrate는 선택이다 — 서술기(`OPS_NARRATOR`)가 죽거나 없어도 결정론 요약은
    그대로 나간다(ADR-0002: 발송이 서술기 때문에 죽으면 안 된다). 서술은 이미
    조립된 결정론 요약을 프롬프트로 "2문장 코멘트"만 요청한다 — 판단을 새로
    시키지 않는다.
    """
    import sys
    from datetime import date as _date
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.kv import make_kv
    from quant.adapters.narrate import make_narrator
    from quant.control import selections
    from quant.control.close_report import build_close_report, matured_today
    from quant.control.ledger import load_trades, round_trips, scoreboard_text
    from quant.control.leaderboard import verdicts_from_ledger
    from quant.control.opstate import record_run

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    today = args.date or _date.today().isoformat()

    sel = selections.load(root / "data" / "ledger" / "selections.jsonl")
    matured = matured_today(sel, today)

    # 리더보드 판정 — cmd_leaderboard 와 같은 계산(생산자별 일별 순위 IC → 승격
    # 판정, `leaderboard.verdicts_from_ledger` 로 추출됨). 판단 표본(judgments)이
    # 아직 없으면 verdicts 는 빈 dict 로 떨어진다.
    judgments = selections.load(root / "data" / "ledger" / "judgments.jsonl")
    producer_verdicts = verdicts_from_ledger(sel, judgments, args.horizon, args.trials)
    verdicts = {f"{who[0]}/{who[1]}": v for who, (v, _ic_by_day) in producer_verdicts.items()}

    ledger_path = root / "data" / "state" / "trades.jsonl"
    board = scoreboard_text(round_trips(load_trades(ledger_path)))

    report = build_close_report(matured, verdicts, board)

    # 결정론 요약을 **narrate 호출 전에** 찍고 flush 한다. narrate(로컬 Claude CLI
    # 기본 180s / 서브프로세스)가 셸 래퍼의 timeout 보다 오래 걸려 SIGTERM 으로
    # 죽어도, 명령 치환($())이 이미 flush 된 이 출력을 잡는다 — "서술기가 죽어도
    # 결정론 요약은 나간다"는 계약이 여기서 실제로 성립한다(2026-08-15 리뷰:
    # narrate 를 먼저 부르고 print 를 뒤에 두면 타임아웃 시 stdout 이 통째로
    # 비어 "생성 실패"로 오보됐다).
    print(report)
    sys.stdout.flush()

    # record_run 도 narrate **앞**에 둔다(2026-08-15 리뷰 M1) — narrate(로컬
    # Claude CLI, 기본 timeout 180s)가 셸 래퍼의 timeout 보다 오래 걸려 SIGTERM
    # 으로 죽으면, 뒤에 있던 record_run 이 실행되지 못해 opstate TTL 감시가
    # "오늘 안 돌았다"는 오탐을 낸다 — 실제로는 결정론 요약이 이미 발송됐는데도.
    try:
        record_run(make_kv(), "close-report", ok=True,
                  detail=f"만기 {len(matured)}건 · 판정 {len(verdicts)}건")
    except Exception:  # noqa: BLE001 — 운영 상태 기록 실패가 리포트 발행을 막으면 안 된다
        pass

    # narrate — 이미 나간 요약에 짧은 코멘트만 덧붙인다. 실패/미설정이면 조용히 생략.
    # 포트 계약(Narrator.narrate)은 "실패는 예외가 아니라 None"이지만, 여기서도
    # try/except 로 한 번 더 막는다.
    narrator = make_narrator()
    comment = None
    try:
        comment = narrator.narrate(
            "다음은 오늘의 장마감 결과 리포트다(결정론적으로 이미 조립됨). "
            "불필요한 서두 없이 2문장으로만 코멘트해줘:\n\n" + report
        )
    except Exception as e:  # noqa: BLE001 — narrate 실패가 이미 나간 리포트를 갉아먹으면 안 된다
        logger.warning("close-report narrate 실패(무시): %s: %s", type(e).__name__, e)
    if comment:
        print("\n💬 " + comment)


def cmd_shadow_judge(args: argparse.Namespace) -> None:
    """LLM 섀도우 판단 (Phase 7.4). **주문을 내지 않는다** — 판단만 기록한다.

    같은 입력(`attributes`)을 결정론적 스코어러와 **동일하게** 보고, 같은 방식으로
    `input_hash` 를 계산한다. 그래야 리더보드 비교가 실력 비교가 된다.

    모델을 못 부르거나 출력이 형식을 어기면 **아무것도 쓰지 않는다** — 0점을 주면
    "최하위로 평가했다"가 되어 IC 를 오염시킨다.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.adapters.narrate import make_narrator
    from quant.control import selections
    from quant.control.judgment import selection_attributes
    from quant.control.shadow import build_prompt, parse_scores
    from quant.core.models import Judgment, input_hash

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    rows = [r for r in selections.load(root / "data" / "ledger" / "selections.jsonl")
            if str(r.get("date")) == args.date and str(r.get("market")) == args.market]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(_json.dumps({"ok": False, "reason": "그 날짜·시장의 선정 행이 없다",
                           "date": args.date, "market": args.market}, ensure_ascii=False))
        raise SystemExit(2)

    narrator = make_narrator()
    text = narrator.narrate(build_prompt(rows))
    scores = parse_scores(text, allowed={str(r.get("symbol")) for r in rows})
    if not scores:
        # 모델이 죽었거나 형식을 어겼다. **판단을 지어내지 않는다.**
        print(_json.dumps({"ok": False, "reason": "모델 출력에서 점수를 얻지 못했다",
                           "producer": args.producer, "rows": len(rows)},
                          ensure_ascii=False))
        raise SystemExit(2)

    now = _dt.now(_tz.utc).isoformat(timespec="seconds")
    jpath = root / "data" / "ledger" / "judgments.jsonl"
    existing = {
        (r.get("producer"), r.get("producer_version"), r.get("input_hash"),
         r.get("symbol"), r.get("session_date"))
        for r in selections.load(jpath)
    }
    written = 0
    jpath.parent.mkdir(parents=True, exist_ok=True)
    with jpath.open("a", encoding="utf-8") as f:
        for r in rows:
            sym = str(r.get("symbol"))
            if sym not in scores:
                continue
            attrs = selection_attributes(r)
            j = Judgment(
                producer=args.producer, producer_version=args.producer_version,
                input_hash=input_hash(attrs), market=str(r.get("market")), symbol=sym,
                session_date=str(r.get("date")), score=scores[sym],
                # 척도가 달라도 순위로 비교하므로 판정 문턱은 중간값으로 둔다.
                verdict="pass" if scores[sym] >= 50 else "reject",
                rationale="shadow", ts=now,
            )
            if j.natural_key() in existing:
                continue
            f.write(_json.dumps(j.__dict__, ensure_ascii=False) + "\n")
            existing.add(j.natural_key())
            written += 1

    print(_json.dumps({"ok": True, "producer": args.producer, "date": args.date,
                       "market": args.market, "rows": len(rows),
                       "scored": len(scores), "judgments_written": written},
                      ensure_ascii=False, indent=2))


def cmd_leaderboard(args: argparse.Namespace) -> None:
    """생산자별 승격 판정 (Phase 7.5). **자동 적용하지 않는다** — 판정만 낸다.

    표본 수는 **거래일 수**로 센다(판단 수가 아니다). 근거는
    `control/leaderboard.py` docstring — 같은 날의 종목들은 같은 시장 움직임을 공유해
    독립 관측이 아니다.
    """
    import json as _json
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.control import selections
    from quant.control.leaderboard import verdicts_from_ledger

    load_settings()
    root = Path(args.root) if args.root else REPO_ROOT
    sel = selections.load(root / "data" / "ledger" / "selections.jsonl")
    judgments = selections.load(root / "data" / "ledger" / "judgments.jsonl")

    producer_verdicts = verdicts_from_ledger(sel, judgments, args.horizon, args.trials)
    out = []
    for who in sorted(producer_verdicts):
        v, ic_by_day = producer_verdicts[who]
        out.append({"producer": who[0], "version": who[1], **v.__dict__,
                    "ic_by_day": ic_by_day})

    print(_json.dumps({"horizon_days": args.horizon, "producers": out},
                      ensure_ascii=False, indent=2, default=str))
    # 승격 후보가 없으면 종료코드 2 — "판단 불가"를 성공으로 읽지 않는다.
    raise SystemExit(0 if any(p["promote"] for p in out) else 2)


def cmd_narrate(args: argparse.Namespace) -> None:
    """stdin 프롬프트 → 서술문을 stdout 으로. 서술하지 못하면 **출력 없이 종료코드 1**.

    서술기 선택(`OPS_NARRATOR`)은 `adapters.narrate.make_narrator()` 한 곳에만 있다 —
    셸에도 스위치를 두면 두 곳이 갈리고, 갈라진 쪽이 조용한 쪽이 된다.

    빈 출력 + 1 을 내는 이유: 호출자(`ops_watch.sh`)가 결정론적 형식으로 떨어질지를
    **문자열 내용이 아니라 종료코드로** 판단할 수 있어야 한다.
    """
    import sys

    from quant.adapters.narrate import make_narrator

    load_settings()
    narrator = make_narrator()
    text = narrator.narrate(sys.stdin.read())
    if not text:
        raise SystemExit(1)
    print(text)


def cmd_kiwoom_probe(args: argparse.Namespace) -> None:
    """키움 실키가 등록된 환경에서 사람이 돌려보는 웹소켓 스모크.

    토큰 발급 -> 웹소켓 접속/로그인 -> 종목 등록 -> N초간 수신 틱 출력 -> 종료.
    주문 기능은 포함하지 않는다 (읽기 전용 조회만). 해외주식(TQQQ) 실시간시세가
    이 경로로 실제 오는지는 미검증이라 기본 심볼은 국내 종목(005930)이다 —
    docs/api/kiwoom/README.md 5.1 참고.
    """
    load_settings()  # .env/.env.local 로드
    import contextlib

    # 키움 웹소켓은 계정당 세션이 하나다 — 엔진이 떠 있는 상태에서 프로브를 붙이면
    # **엔진의 실시간 시세가 끊긴다**(2026-08-11 실측: 엔진이 재접속 루프에 빠졌고
    # 프로브 종료 후에야 회복). 장중에는 절대 돌리지 말 것.
    logger.warning(
        "kiwoom-probe: 계정당 웹소켓 세션은 1개다 — quant-engine이 실행 중이면 "
        "그쪽 실시간 시세가 이 프로브 동안 끊긴다. 장중 실행 금지."
    )

    from quant.adapters.brokers.kiwoom.client import KiwoomClient
    from quant.adapters.brokers.kiwoom.websocket import DEFAULT_WS_URL, KiwoomRealtimeFeed

    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    secret_key = os.environ.get("KIWOOM_SECRET_KEY", "")
    if not app_key or not secret_key:
        raise SystemExit("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 미설정 — .env.local 확인")

    client = KiwoomClient(app_key=app_key, secret_key=secret_key)
    client.access_token()
    print(f"토큰 발급 성공 (base_url={client.base_url})")

    # WS 호스트는 **토큰이 발급된 서버와 짝이 맞아야 한다** — 실전 토큰을 모의 WS에
    # 물리면 8031("투자구분(실전/모의)이 달라서 Token를 사용할수가 없습니다")로
    # 끊긴다. assembly.build_kiwoom_realtime_route가 하던 치환이 여기엔 없어서,
    # 실전 환경에서 프로브만 모의 호스트로 붙어 **진단 도구가 거짓 실패를 보고**했다
    # (2026-08-11 실측). env 오버라이드는 그대로 최우선으로 남긴다.
    base_url = os.environ.get("KIWOOM_BASE_URL", "")
    _default_ws = (
        DEFAULT_WS_URL.replace("mockapi.kiwoom.com", "api.kiwoom.com")
        if "mockapi" not in base_url and "api.kiwoom.com" in base_url
        else DEFAULT_WS_URL
    )
    ws_url = os.environ.get("KIWOOM_WS_URL", _default_ws)
    feed = KiwoomRealtimeFeed(
        access_token=client.access_token, symbols=[args.symbol], ws_url=ws_url,
    )
    print(f"ws_url={ws_url}")

    async def _probe() -> None:
        run_task = asyncio.create_task(feed.run())
        try:
            seen = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + args.seconds
            print(f"웹소켓 접속/구독 중... ({args.symbol}, {args.seconds}초간 수신 대기)")
            while loop.time() < deadline:
                await asyncio.sleep(0.5)
                q = feed.quote(args.symbol)
                if q is not None and q != seen:
                    print(f"tick: {q}")
                    seen = q
        finally:
            await feed.close()
            run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task

        print(f"\n=== kiwoom-probe: {args.symbol} ({args.seconds}s) ===")
        print(f"health: {feed.health()}")
        print(f"last quote: {feed.quote(args.symbol)}")

    asyncio.run(_probe())


def cmd_spread_sample(args: argparse.Namespace) -> None:
    """호가창 스프레드 실측 수집 (2026-08-28) — 스캘핑 비용 가정의 검증.

    `slippage_bps: 2.5`도 "왕복 20bp"도 실측이 아니라 추정치다. 스캘핑은 비용이
    엣지보다 크면 전부 무의미하므로, 알파를 찾기 전에 이 숫자를 실측으로 바꾼다.
    측정 전용 — 거래 평면에 닿지 않고, 결과는 `data/ledger/spread.jsonl`에만 쌓인다.

    ## rate limit 방어 (이 잡의 핵심)

    Toss MARKET_DATA 그룹 상한은 10 TPS 이고 **엔진의 시세 폴링이 같은 버킷을
    쓴다**. 측정 잡이 상한을 다 쓰면 장중 시세가 429 로 밀린다 — 돈이 걸린 쪽이
    우선이다. 그래서 우리 몫을 절반 이하(5 TPS)로 스스로 자른다:

      - `collect.spread.sample_spread`가 호출 간 최소 0.2초(=5 TPS)를 강제한다.
        호출자가 더 촘촘한 값을 넘겨도 이 바닥값으로 잘린다.
      - 기본 `--interval-seconds 1.0`은 실제로는 1 TPS — 상한의 1/10 이다.
      - 한 라운드에서 심볼당 정확히 1회만 호출한다(중복 조회 없음).
      - 크론은 10분 간격이라 평균 부하는 무시할 수준이다.
    """
    import json
    import statistics
    import time as _time
    from datetime import datetime, timezone
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT
    from quant.apps.assembly import MissingCredentials, build_toss_client
    from quant.collect.spread import sample_spread
    from quant.core.models import market_of_symbol
    from quant.trade.universe import FileWatchlistUniverse

    settings = load_settings()
    _redact.install()

    root = Path(args.root) if args.root else REPO_ROOT

    symbols: list[str] = []
    seen: set[str] = set()

    def _add(sym: str) -> None:
        s = str(sym).strip()
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)

    if args.symbols:
        for s in args.symbols:
            _add(s)
    else:
        # 기본 대상: 워치리스트(그날 실제로 감시하는 종목) + 전략 앵커(TQQQ/SQQQ 등
        # settings.yaml 이 고정으로 든 심볼). 비용을 알아야 할 대상이 정확히 이 집합이다.
        for s in FileWatchlistUniverse(root / "data" / "watchlist.yaml").refresh():
            _add(s)
        for strat_cfg in settings.strategies.values():
            for s in strat_cfg.get("symbols") or []:
                _add(s)

    if args.market:
        symbols = [s for s in symbols if market_of_symbol(s) == args.market]

    if not symbols:
        why = f"--market {args.market} 필터에 남은 심볼이 없다" if args.market else (
            "--symbols 도 워치리스트도 비었다"
        )
        print(f"대상 심볼 없음 — {why}")
        return

    try:
        client = build_toss_client()
    except MissingCredentials as e:
        logger.error("spread-sample: %s", e)
        raise SystemExit(2)

    out_path = root / "data" / "ledger" / "spread.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    by_symbol: dict[str, list[float]] = {}
    dropped: list[str] = []
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    written = 0

    for round_no in range(args.rounds):
        if round_no:
            _time.sleep(args.interval_seconds)
        sample = sample_spread(
            client, symbols,
            now=datetime.now(timezone.utc),
            min_interval=args.interval_seconds,
        )
        if sample.rows:
            with out_path.open("a", encoding="utf-8") as f:
                for row in sample.rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += len(sample.rows)
        for row in sample.rows:
            by_symbol.setdefault(row["symbol"], []).append(row["spread_bp"])
        dropped += sample.dropped
        empty += sample.empty
        failed += sample.failed

    print(f"=== 호가 스프레드 실측 ({args.market or 'ALL'}, {args.rounds}라운드, {len(symbols)}종목) ===")
    if not by_symbol:
        print("표본 없음 — 아래 결측 내역 참고 (0bp 가 아니라 '못 쟀다'이다)")
    for symbol in sorted(by_symbol, key=lambda s: statistics.median(by_symbol[s])):
        spreads = by_symbol[symbol]
        median = statistics.median(spreads)
        print(f"{symbol:>8}  중앙값 {median:6.1f}bp  왕복 {median * 2:6.1f}bp  표본 {len(spreads)}")

    if by_symbol:
        overall = statistics.median([statistics.median(v) for v in by_symbol.values()])
        # 왕복 스프레드 = 중앙값 × 2 (즉시 사고 즉시 파는 스캘핑이 지불하는 스프레드).
        # 우리 가정은 편도 slippage_bps × 2 이고, 저장소가 인용하는 총 왕복 비용은 20bp다.
        assumed = float(settings.execution.get("slippage_bps", 2.5)) * 2
        print(
            f"\n왕복 스프레드 실측(전 종목 중앙값) {overall * 2:.1f}bp "
            f"vs 가정 slippage 왕복 {assumed:.1f}bp / 총비용 가정 20bp — "
            f"{'가정보다 비싸다' if overall * 2 > assumed else '가정 범위 안'}"
        )

    print(f"\n원장 기록: {written}줄 → {out_path}")
    if dropped:
        print(f"이상치 배제(ask<=bid 등): {len(set(dropped))}종목 {len(dropped)}건 {sorted(set(dropped))}")
    if empty:
        # US 심볼에서 Toss 호가가 실데이터를 주는지는 아직 미검증이다 — 빈 응답이면
        # 되는 척하지 않고 여기 그대로 드러낸다.
        us_empty = sorted({s for s in empty if market_of_symbol(s) == "US"})
        print(f"호가 응답 없음: {len(set(empty))}종목 {len(empty)}건 {sorted(set(empty))}")
        if us_empty:
            print(f"  ↳ US 응답 없음: {us_empty} — Toss 호가가 US 실데이터를 주는지 미확인")
    if failed:
        syms = sorted({s for s, _ in failed})
        print(f"조회 실패: {len(syms)}종목 {len(failed)}건 {syms} (사유 예: {failed[0][1]})")


_PEEK_MAX_N = 200


def cmd_peek(args: argparse.Namespace) -> None:
    """읽기 전용 시세 조회 — 현재가 + 최근 봉 n개를 JSON으로 stdout에 낸다.

    2026-08-30 소유자 지시: LLM 판단 프로세스(server/ 소관, 이 저장소 밖)가
    "우리가 만든 데이터 API를 다른 전략들과 공정하게 활용"할 수 있게 하는 읽기
    전용 진입점. **주문 경로가 전혀 없다** — `quote()`/`history()` 조회뿐이고,
    브로커/리스크/체결 어느 것도 조립하지 않는다.

    ## 왜 `build_market_data`(엔진과 동일 라우팅)를 그대로 쓰지 않았나

    Kiwoom 실시간 웹소켓은 **계정당 세션이 하나**다(`cmd_kiwoom_probe` 경고 —
    "quant-engine이 실행 중이면 그쪽 실시간 시세가 이 프로브 동안 끊긴다").
    `peek`는 LLM 판단 루프가 장중 반복 호출할 것을 전제하는데, `build_market_data`
    가 여는 Kiwoom 라우트를 그대로 썼다가는 매 호출이 라이브 엔진의 웹소켓을
    끊을 위험이 있다. 그래서 이 명령은 **Toss REST 단독**(`TossDataFeed`)만
    쓴다 — 기본 설정(`kiwoom.realtime.enabled: false`)에서는 엔진 자신도 이
    경로로 폴백하므로, 대부분의 시간에 엔진이 실제로 보는 것과 같은 소스다.

    `--n`은 `_PEEK_MAX_N`(200)으로 상한한다(남용 방지) — 넘겨도 에러 없이 잘라서
    진행하고 경고만 남긴다.
    """
    import json as _json

    from quant.adapters.brokers.toss.datafeed import TossDataFeed
    from quant.apps.assembly import MissingCredentials, build_toss_client

    load_settings()  # .env/.env.local 로드(TOSS_CLIENT_ID 등)

    n = args.n
    if n <= 0:
        raise SystemExit("--n은 1 이상이어야 합니다.")
    if n > _PEEK_MAX_N:
        logger.warning("peek: --n %d은 상한 %d으로 잘린다", n, _PEEK_MAX_N)
        n = _PEEK_MAX_N

    try:
        client = build_toss_client()
    except MissingCredentials as e:
        logger.error("peek: %s", e)
        raise SystemExit(2)

    feed = TossDataFeed(client, symbols=[args.symbol])
    quote = feed.quote(args.symbol)

    out: dict = {
        "symbol": args.symbol,
        "interval": args.interval,
        "quote": (
            {"price": quote.price, "ts": quote.ts.isoformat()} if quote is not None else None
        ),
    }
    try:
        bars = feed.history(args.symbol, args.interval, n)
    except Exception as e:  # noqa: BLE001 — 조회 실패도 "결과"다, JSON으로 그대로 드러낸다
        out["bars"] = None
        out["bars_error"] = f"{type(e).__name__}: {e}"
    else:
        out["bars"] = [
            {
                "ts": ts.isoformat(),
                "open": float(row["open"]), "high": float(row["high"]),
                "low": float(row["low"]), "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            for ts, row in bars.iterrows()
        ]

    print(_json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="quant")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fit = sub.add_parser(
        "fitness",
        help="적합도 지표를 JSON 으로 (기계용). 사람이 읽을 표는 backtest 를 쓴다.",
    )
    p_fit.add_argument("--strategy", default="donchian")
    p_fit.add_argument("--days", type=int, default=90)
    p_fit.add_argument("--interval", default="15m")
    p_fit.add_argument("--source", default="stub")
    p_fit.add_argument("--symbols", default=None)
    p_fit.add_argument(
        "--allow-zero-cost", action="store_true",
        help="비용 0 백테스트를 허용한다 — **비용 모델 자체를 시험할 때만**. "
             "하네스 경로에서 쓰면 확실한 손실 전략이 최고점으로 나온다 "
             "(실측 엣지 왕복 8~9bp vs 수수료 14bp).",
    )
    p_fit.set_defaults(func=cmd_fitness)

    p_wf = sub.add_parser(
        "walkforward",
        help="롤링 OOS 안정성 하네스 — 같은 설정을 여러 시간 창에서 반복 실행(파라미터 탐색 없음)",
    )
    p_wf.add_argument("--strategy", default="donchian")
    p_wf.add_argument("--days", type=int, default=360, help="전체 관찰 기간(달력일)")
    p_wf.add_argument("--window", type=int, default=90, help="창 크기(거래일)")
    p_wf.add_argument("--step", type=int, default=45, help="창 간격(달력일)")
    p_wf.add_argument("--interval", default="15m")
    p_wf.add_argument("--source", default="stub")
    p_wf.add_argument("--symbols", default=None)
    p_wf.add_argument(
        "--history-dir", default=None,
        help="파티션 루트(기본: data/history). 별도 데이터 레이크를 같은 레이아웃으로 "
             "두고 그쪽을 읽거나 쓰려면 지정한다 — --source history 에서만 의미가 있다.",
    )
    p_wf.add_argument(
        "--fill-model", default="close", choices=["close", "intrabar"],
        help="close(기본, 봉 마감가 체결) | intrabar(봉 안에서 닿은 손절/목표를 그 봉 안에서 체결 — 봉 마감 체결이 타이트한 손절 전략을 실제보다 좋게 보이게 하는 왜곡을 제거한다. 보수적: 같은 봉에서 둘 다 닿으면 손절이 이기고, 갭 관통은 시가 체결)",
    )
    p_wf.set_defaults(func=cmd_walkforward)

    p_sr = sub.add_parser(
        "strategy-report",
        help="전략 성적표 한 장 — 인샘플+OOS+deflated Sharpe(탐색 횟수 보정)+실측 비용 "
             "(quant-expert §4 형식, 표본 부족 항목은 '판단 불가')",
    )
    p_sr.add_argument("--strategy", default="donchian")
    p_sr.add_argument("--days", type=int, default=90, help="인샘플 백테스트 기간(거래일)")
    p_sr.add_argument("--total-days", type=int, default=360, help="walk-forward 전체 관찰 기간(달력일)")
    p_sr.add_argument("--window", type=int, default=90, help="walk-forward 창 크기(거래일)")
    p_sr.add_argument("--step", type=int, default=90, help="walk-forward 창 간격(달력일)")
    p_sr.add_argument("--interval", default="15m")
    p_sr.add_argument("--source", default="stub")
    p_sr.add_argument("--symbols", default=None)
    p_sr.add_argument(
        "--trials", type=int, default=1,
        help="이 전략을 채택하기까지 시험한 변형의 수. deflated Sharpe 가 이 값으로 "
             "샤프를 깎는다 — **축소해 신고하면 그만큼 관대한 판정이 나온다**. "
             "기본 1은 '한 번도 탐색하지 않았다'는 뜻이다.",
    )
    p_sr.add_argument(
        "--history-dir", default=None,
        help="파티션 루트(기본: data/history). 별도 데이터 레이크를 같은 레이아웃으로 "
             "두고 그쪽을 읽거나 쓰려면 지정한다 — --source history 에서만 의미가 있다.",
    )
    p_sr.add_argument(
        "--fill-model", default="close", choices=["close", "intrabar"],
        help="close(기본, 봉 마감가 체결) | intrabar(봉 안에서 닿은 손절/목표를 그 봉 안에서 체결 — 봉 마감 체결이 타이트한 손절 전략을 실제보다 좋게 보이게 하는 왜곡을 제거한다. 보수적: 같은 봉에서 둘 다 닿으면 손절이 이기고, 갭 관통은 시가 체결)",
    )
    p_sr.set_defaults(func=cmd_strategy_report)

    p_bg = sub.add_parser(
        "backtest-gate",
        help="배포 게이트 — §4 리포트 + 트레이드 다차원 분석 + go/no-go/판단 불가 판정 "
             "(walk-forward OOS + deflated Sharpe + 비용 2배 생존 + fold 안정성)",
    )
    p_bg.add_argument("--strategy", default="donchian")
    p_bg.add_argument("--days", type=int, default=90, help="인샘플 백테스트 기간(거래일)")
    p_bg.add_argument("--total-days", type=int, default=360, help="walk-forward 전체 관찰 기간(달력일)")
    p_bg.add_argument("--window", type=int, default=90, help="walk-forward 창 크기(거래일)")
    p_bg.add_argument("--step", type=int, default=90, help="walk-forward 창 간격(달력일)")
    p_bg.add_argument("--interval", default="15m")
    p_bg.add_argument("--source", default="stub")
    p_bg.add_argument("--symbols", default=None)
    p_bg.add_argument(
        "--trials", type=int, default=1,
        help="이 전략을 채택하기까지 시험한 변형의 수 — deflated Sharpe 기준에 반영된다.",
    )
    p_bg.add_argument(
        "--history-dir", default=None,
        help="파티션 루트(기본: data/history). --source history 에서만 의미가 있다.",
    )
    p_bg.add_argument(
        "--fill-model", default="close", choices=["close", "intrabar"],
        help="close(기본, 봉 마감가 체결) | intrabar(봉 안에서 닿은 손절/목표를 그 봉 안에서 체결 — 봉 마감 체결이 타이트한 손절 전략을 실제보다 좋게 보이게 하는 왜곡을 제거한다. 보수적: 같은 봉에서 둘 다 닿으면 손절이 이기고, 갭 관통은 시가 체결)",
    )
    p_bg.set_defaults(func=cmd_backtest_gate)

    p_kelly = sub.add_parser(
        "kelly", help="원장 기반 부분 켈리 자문(표시만, 자동 반영 없음)",
    )
    p_kelly.set_defaults(func=cmd_kelly)

    p_bt = sub.add_parser("backtest")
    p_bt.add_argument("--strategy", default="donchian")
    p_bt.add_argument("--days", type=int, default=90)
    p_bt.add_argument("--interval", default="15m")
    p_bt.add_argument("--source", default="stub")
    p_bt.add_argument(
        "--symbols", default=None,
        help='공백 구분 심볼 목록(예: "TQQQ SQQQ") — settings.yaml의 symbols: []를 '
             "덮어쓴다. 관심종목(watchlist) 전략(orb_scan/intraday_scan/cross_momentum/"
             "confluence)은 이게 없으면 명확한 에러로 멈춘다.",
    )
    p_bt.add_argument(
        "--history-dir", default=None,
        help="파티션 루트(기본: data/history). 별도 데이터 레이크를 같은 레이아웃으로 "
             "두고 그쪽을 읽거나 쓰려면 지정한다 — --source history 에서만 의미가 있다.",
    )
    p_bt.add_argument(
        "--fill-model", default="close", choices=["close", "intrabar"],
        help="close(기본, 봉 마감가 체결) | intrabar(봉 안에서 닿은 손절/목표를 그 봉 안에서 체결 — 봉 마감 체결이 타이트한 손절 전략을 실제보다 좋게 보이게 하는 왜곡을 제거한다. 보수적: 같은 봉에서 둘 다 닿으면 손절이 이기고, 갭 관통은 시가 체결)",
    )
    p_bt.set_defaults(func=cmd_backtest)

    p_paper = sub.add_parser("paper")
    p_paper.set_defaults(func=cmd_paper)

    p_report = sub.add_parser("report")
    p_report.set_defaults(func=cmd_report)

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--symbol", required=True)
    p_fetch.add_argument("--start", required=True)
    p_fetch.add_argument("--end", default=None)
    p_fetch.add_argument("--source", default="toss", choices=["toss", "yfinance", "alpaca"])
    p_fetch.add_argument("--interval", default="1m")
    p_fetch.add_argument(
        "--history-dir", default=None,
        help="파티션 저장 루트(기본: data/history). 별도 데이터 레이크로 받으려면 지정한다.",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    p_naver_fund = sub.add_parser(
        "naver-fundamentals",
        help="네이버 거래상위 펀더멘털(시가총액/PER/ROE) 스냅샷 적재 (매일, KR 마감 후)",
    )
    p_naver_fund.add_argument("--root", default=".", help="저장소 루트")
    p_naver_fund.set_defaults(func=cmd_naver_fundamentals)

    p_dart_fund = sub.add_parser(
        "dart-fundamentals",
        help="DART 재무제표(부채비율/ROE/BPS) 적재 (분기 단위 변동 — 주 1회면 충분)",
    )
    p_dart_fund.add_argument("--root", default=".", help="저장소 루트")
    p_dart_fund.add_argument(
        "--symbols", default=None,
        help='공백 구분 종목코드 목록(예: "005930 000660") — 생략 시 watchlist.yaml의 KR 종목',
    )
    p_dart_fund.add_argument(
        "--bsns-year", default=None,
        help="사업연도(YYYY). 생략 시 4월 이후=작년도, 1~3월=재작년도로 추정",
    )
    p_dart_fund.add_argument(
        "--reprt-code", default="11011",
        help="DART 보고서 코드. 기본 11011=사업보고서(연간, 계정이 온전함)",
    )
    p_dart_fund.set_defaults(func=cmd_dart_fundamentals)

    p_opt = sub.add_parser("optimize")
    p_opt.add_argument("--strategy", default="donchian")
    p_opt.add_argument("--start", required=True)
    p_opt.add_argument("--end", required=True)
    p_opt.add_argument("--train-days", type=int, default=60)
    p_opt.add_argument("--test-days", type=int, default=20)
    p_opt.add_argument("--step-days", type=int, default=None, help="기본값: --test-days와 동일")
    p_opt.add_argument("--embargo-days", type=int, default=0)
    p_opt.add_argument("--trials", type=int, default=50)
    p_opt.add_argument("--source", default="stub", choices=["stub", "history"])
    # interval을 넘기지 않으면 walk_forward가 기본 15m으로 리플레이한다. 5분봉
    # 전략(orb)을 그 케이던스로 돌리면 진입 창 판정이 통째로 어긋나 에러 없이
    # 엉뚱한 최적 파라미터가 나온다.
    p_opt.add_argument("--interval", default="15m")
    p_opt.add_argument("--seed", type=int, default=42)
    p_opt.add_argument("--param-space", default=None, help="YAML 경로 (생략 시 전략별 기본 param space 사용)")
    p_opt.set_defaults(func=cmd_optimize)

    p_watch_score = sub.add_parser("watch-score", help="워치리스트 후보 종목 결정론적 채점 (리포팅 레이어, 08:40 cron 전용)")
    p_watch_score.add_argument("--symbols", default="", help="공백 구분 심볼 목록 (예: \"TQQQ 005930\") — --discover-kr만으로도 실행 가능")
    p_watch_score.add_argument(
        "--discover-kr", action="store_true",
        help="Toss 거래대금 랭킹 상위에서 KR 후보를 추가 발굴해 함께 채점 (TREND 태그)",
    )
    p_watch_score.add_argument(
        "--discover-us", action="store_true",
        help="Toss 거래대금 랭킹 상위에서 US 후보를 추가 발굴해 함께 채점 (TREND 태그)",
    )
    p_watch_score.add_argument(
        "--threshold", type=int, default=None,
        help="기본값: config universe.watchlist.auto_score.threshold (없으면 50)",
    )
    p_watch_score.set_defaults(func=cmd_watch_score)

    p_scoreboard = sub.add_parser("scoreboard", help="누적 거래 원장 기반 전략별·종목별 성적표 (승률/payoff/bps)")
    p_scoreboard.add_argument("--days", type=int, default=None, help="최근 N일만 (기본: 전체 누적)")
    p_scoreboard.add_argument(
        "--ab", action="store_true",
        help="A/B 갈래 비교 추가 — `<id>`(촉매 제외) vs `<id>_cat`(촉매만) 기대값 차이",
    )
    p_scoreboard.add_argument(
        "--ledger", default=None,
        help="원장 경로 재지정 (기본: data/state/trades.jsonl) — 운영 원장 사본 점검용",
    )
    p_scoreboard.set_defaults(func=cmd_scoreboard)

    p_orders = sub.add_parser("orders", help="주문 생애 원장(orders.jsonl) 조회 — 거부/미체결 포함")
    p_orders.add_argument(
        "--rejected-funds", action="store_true",
        help="자금 부족으로 거부된 시도만 (\"안 산 게 아니라 못 샀던 것\")",
    )
    p_orders.add_argument("--limit", type=int, default=50, help="최근 N건만 (기본 50, 0=전체)")
    p_orders.set_defaults(func=cmd_orders)

    p_eq = sub.add_parser("equity-snapshot", help="자본 곡선 1점 기록 — 세션 마감 후 총자산·전략별 장부 평가액(KRW)")
    p_eq.add_argument("--market", required=True, choices=["KR", "US"])
    p_eq.set_defaults(func=cmd_equity_snapshot)

    p_perf = sub.add_parser("performance", help="자본 곡선 성과 — CAGR/변동성/샤프(rf=0)/MDD (gs-quant econometrics 상당)")
    p_perf.set_defaults(func=cmd_performance)

    p_alpha = sub.add_parser(
        "alpha-report",
        help="지수 대비 초과수익(알파) — 날짜별 우리/지수/알파 + 상승일 참여율·하락일 방어율",
    )
    p_alpha.add_argument("--market", choices=["KR", "US"], default=None,
                         help="생략하면 KR·US 둘 다")
    p_alpha.add_argument("--days", type=int, default=30, help="최근 N 거래일 (0=전체)")
    p_alpha.set_defaults(func=cmd_alpha_report)

    p_tearsheet = sub.add_parser(
        "tearsheet",
        help="자본 곡선 → quantstats HTML 티어시트 (performance 의 시각화 버전, quantstats 미설치 시 우아하게 저하)",
    )
    p_tearsheet.add_argument("--market", required=True, choices=["KR", "US"])
    p_tearsheet.add_argument("--out", default=None, help="기본값: out/tearsheet_{market}.html")
    p_tearsheet.set_defaults(func=cmd_tearsheet)

    p_wrap = sub.add_parser(
        "daily-wrap",
        help="장 마감 하루 요약 HTML 한 장 (실적·지분 변경·문제와 조치·배포된 커밋) — 경로를 stdout 에 낸다",
    )
    p_wrap.add_argument("--market", required=True, choices=["KR", "US"], help="KR 또는 US")
    p_wrap.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 그 시장 기준 오늘)")
    p_wrap.set_defaults(func=cmd_daily_wrap)

    p_weekly = sub.add_parser("weekly-review", help="주간 재검토 — 전략별 성적·손해 패턴·주간 장 흐름·점수 적중률 (토 06:25)")
    p_weekly.set_defaults(func=cmd_weekly_review)

    p_experiments = sub.add_parser("experiments", help="자동 판정 — 파라미터 변경 감지 + 이중차분 효과 판정 + 전략 사망 경보 (판정 없으면 무출력)")
    p_experiments.add_argument("--verbose", action="store_true", help="지문/대기 상태를 stderr 로")
    p_experiments.set_defaults(func=cmd_experiments)

    p_forensics = sub.add_parser("forensics", help="거래 부검 — MFE/MAE·청산 효율·진입 위치 대조군·청산 규칙 재생 (왜 졌나)")
    p_forensics.add_argument("--days", type=int, default=None, help="최근 N일만 (기본: 전체 누적)")
    p_forensics.add_argument("--strategy", default=None, help="특정 전략만")
    p_forensics.set_defaults(func=cmd_forensics)

    p_daily_fb = sub.add_parser("daily-feedback", help="일일 피드백 — 오늘 진입 타이밍 규칙 판정(고점매수/거래소강/늦은진입), 전략별 (픽 없으면 무출력)")
    p_daily_fb.add_argument("--market", required=True, choices=["KR", "US"], help="KR 또는 US")
    p_daily_fb.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 그 시장 기준 오늘)")
    p_daily_fb.set_defaults(func=cmd_daily_feedback)

    p_session_pnl = sub.add_parser("session-pnl", help="세션(정규장) 마감 후 실화폐 손익 리포트 (실현+미실현, 시장별 통화)")
    p_session_pnl.add_argument("--market", required=True, choices=["KR", "US"], help="KR 또는 US")
    p_session_pnl.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 그 시장 기준 오늘)")
    p_session_pnl.set_defaults(func=cmd_session_pnl)

    p_manual_recs = sub.add_parser(
        "manual-recs",
        help="수동 계좌 추천(자동매매 아님) — 외국인 적립/종가배팅/RSI(2) 눌림(KR)·"
             "오버나이트 드리프트(US)를 선정 원장에 남기고 텔레그램용 메시지를 stdout에 낸다",
    )
    p_manual_recs.add_argument("--market", default=None, choices=["KR", "US"],
                                help="KR 또는 US (--scorecard 가 아니면 필수)")
    p_manual_recs.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 그 시장 기준 오늘)")
    p_manual_recs.add_argument("--dry-run", action="store_true",
                                help="선정 원장에 쓰지 않고 메시지만 stdout에 출력")
    p_manual_recs.add_argument("--scorecard", action="store_true",
                                help="선정 대신 producer manual_rec_v1의 D+5 적중률/평균bp를 출력")
    p_manual_recs.set_defaults(func=cmd_manual_recs)

    p_strategy_pnl = sub.add_parser(
        "strategy-pnl",
        help="전략별 독립 명목계좌(각 1천만원) 성과 — 평가금액/수익률/실현·미실현손익/거래수·승률",
    )
    p_strategy_pnl.set_defaults(func=cmd_strategy_pnl)

    p_backup = sub.add_parser(
        "backup",
        help="아티팩트 백업 번들 생성/대조 (JSON). 문제가 있으면 종료코드 1.",
    )
    p_backup.add_argument("--out", default="data/backups", help="번들을 둘 디렉토리")
    p_backup.add_argument("--root", default=".", help="저장소 루트 (data/ 의 부모)")
    p_backup.add_argument(
        "--include", action="append", default=None,
        help="data/ 밖의 파일을 함께 담는다 (MySQL 덤프). 여러 번 쓸 수 있다.",
    )
    p_backup.add_argument(
        "--verify", default=None,
        help="이 번들을 매니페스트와 대조만 하고 끝낸다 (생성하지 않음)",
    )
    p_backup.set_defaults(func=cmd_backup)

    p_publish_perf = sub.add_parser(
        "publish-performance",
        help="공개 포트폴리오 사이트용 성과 JSON 생성 (거래 원장 하나만 입력, 종목/잔고 비노출)",
    )
    p_publish_perf.add_argument("--out", required=True, help="출력 JSON 경로")
    p_publish_perf.set_defaults(func=cmd_publish_performance)

    p_seed_real = sub.add_parser(
        "seed-real",
        help="실계좌 스냅샷을 모의(paper) 상태로 이식 (일회성 제어 도구 — 반드시 엔진 정지 중에 실행)",
    )
    p_seed_real.add_argument(
        "--snapshot", default="data/state/real_account_snapshot.json",
        help="실계좌 스냅샷 JSON 경로",
    )
    p_seed_real.add_argument(
        "--dry-run", action="store_true",
        help="파일을 쓰지 않고 결과만 미리 본다(기본은 실제로 씀)",
    )
    p_seed_real.set_defaults(func=cmd_seed_real)

    p_health = sub.add_parser(
        "health",
        help="운영 이상 점검 (JSON). 종료코드 0=정상 / 1=이상 / 2=모름.",
    )
    p_health.add_argument("--root", default=".", help="저장소 루트")
    p_health.add_argument(
        "--expect-timer", action="append", default=None,
        help="있어야 하는 systemd 타이머 유닛. 여러 번 쓸 수 있다.",
    )
    p_health.add_argument(
        "--required-source", action="append", default=None,
        help="리포트에서 빠지면 안 되는 소스 이름(engine.json 의 missing 과 대조). "
             "after_hours 처럼 시간대에 따라 정상적으로 빠지는 건 넣지 않는다.",
    )
    p_health.add_argument(
        "--required-secret", action="append", default=None,
        help="앱 경로로 읽혀야 하는 시크릿 키 이름. 값은 출력하지 않는다.",
    )
    p_health.set_defaults(func=cmd_health)

    p_oj = sub.add_parser(
        "ops-judge",
        help="판단하는 워치독 — 규칙 기반 health 위에 LLM 교차검증(JSON). 종료코드 0=정상 / 1=이상 / 2=확인 필요.",
    )
    p_oj.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_oj.add_argument(
        "--rule-based-json", default=None,
        help="`cli health`의 JSON 출력 파일 경로, 또는 '-'(stdin). "
             "생략하면 규칙 기반 결과 없이 판단한다(도구가 '읽지 못했다'로 정직하게 답한다).",
    )
    p_oj.add_argument(
        "--label", default="manual",
        help="호출 컨텍스트 라벨(예: kr-midday, us-midsession) — 프롬프트에 그대로 노출된다.",
    )
    p_oj.add_argument(
        "--time-budget", type=float, default=240.0,
        help="LLM 판단 벽시계 예산(초). 0 이하면 LLM 을 호출하지 않고 즉시 'review'로 떨어진다. "
             "실제 강제 중단은 호출부(셸)의 timeout 이 진다 — 이 값은 시작 전 게이트 + HTTP 타임아웃 축소에만 쓰인다.",
    )
    p_oj.set_defaults(func=cmd_ops_judge)

    p_out = sub.add_parser(
        "outcomes",
        help="전방 수익률 채우기 + 결정론적 판단 기록 (매일 장 마감 후). JSON 출력.",
    )
    p_out.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_out.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_out.add_argument("--scorer-version", default="3",
                       help="결정론적 스코어러 버전 — 규칙을 바꾸면 올린다(표본이 섞이지 않게). "
                            "기본값 3 = 선정 속성에 news_z 추가(H-2 Task 4, 2026-08-16). "
                            "2 = baseline_score100 산식(§E-2, 2026-08-15 v1 상수 50 문제로 교체)")
    p_out.add_argument("--dry-run", action="store_true")
    p_out.set_defaults(func=cmd_outcomes)

    p_ai = sub.add_parser(
        "ai-trader",
        help="신입사원 AI 트레이더(수습) — 오늘 선정 원장으로 3역할 토론, 판단만 기록(주문 없음). "
             "stdout = 텔레그램 카드(픽 없으면 무출력).",
    )
    p_ai.add_argument("--market", required=True, choices=["KR", "US"])
    p_ai.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_ai.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_ai.set_defaults(func=cmd_ai_trader)

    p_ml = sub.add_parser(
        "ml-scorer",
        help="학습형 선정자 — 과거 selection⋈forward_return(D+1)로 릿지 회귀를 학습해 "
             "오늘 선정 원장 후보를 채점, 판단만 기록(주문 없음). "
             "stdout = 표본부족/DB없음/판단없음 한 줄 또는 텔레그램 카드.",
    )
    p_ml.add_argument("--market", required=True, choices=["KR", "US"])
    p_ml.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_ml.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_ml.add_argument("--min-train-days", type=int, default=30,
                      help="학습에 요구하는 최소 독립 거래일 (기본 30 — 근거는 "
                           "quant/analyze/ml_scorer.py 모듈 docstring)")
    p_ml.add_argument("--ridge-lambda", type=float, default=10.0,
                      help="릿지 정규화 강도 (기본 10.0 — 소표본 과최적합 억제)")
    p_ml.set_defaults(func=cmd_ml_scorer)

    p_pd = sub.add_parser(
        "promotion-debate",
        help="승격 토론(Bull/Bear/Judge) — 오늘 own_brief 자동 편입분을 재검토, "
             "유지/보류 판정만 기록(관심종목은 바꾸지 않는다). "
             "stdout = 텔레그램 카드(편입 없음/LLM 결근이면 무출력).",
    )
    p_pd.add_argument("--market", required=True, choices=["KR", "US"])
    p_pd.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_pd.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_pd.set_defaults(func=cmd_promotion_debate)

    p_rr = sub.add_parser(
        "risk-review",
        help="독립 리스크 리뷰 — 드로다운·집중도·상쇄쌍 노출·연속 손실만 전담, "
             "ops-judge 와 분리된 별도 프롬프트. 판정은 결정론, LLM 은 상위 3문제+"
             "권고만. stdout 첫 줄 `BREACH: yes|no` + 카드.",
    )
    p_rr.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_rr.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_rr.set_defaults(func=cmd_risk_review)

    p_pa = sub.add_parser(
        "pnl-attribution",
        help="PnL 귀속 요약 — [엣지 − 수수료 − 세금] 결정론 분해 + 전략별 상/하위 1개. "
             "LLM 없음. stdout = 4줄 카드(체결 없으면 무출력).",
    )
    p_pa.add_argument("--market", required=True, choices=["KR", "US"])
    p_pa.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_pa.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_pa.set_defaults(func=cmd_pnl_attribution)

    p_fs = sub.add_parser(
        "flow-scan",
        help="장중 거래대금 발굴 — 랭킹 상위 신규 후보를 뽑아 `FLOW: ...` 한 줄로 낸다 "
             "(편입은 watch-score 게이트를 거친 뒤 별도 셸이 한다).",
    )
    p_fs.add_argument("--market", required=True, choices=["KR", "US"])
    p_fs.add_argument("--top", type=int, default=30, help="랭킹 상위 몇 개까지 볼지 (기본 30)")
    p_fs.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_fs.set_defaults(func=cmd_flow_scan)

    p_kf = sub.add_parser(
        "kr-flow",
        help="KR 마감 후 외국인·기관 수급 스냅샷 (크론 15:50) — 마감 종합(05:50)이 "
             "전일 수급을 읽을 수 있게 그날 안에 원장을 채운다.",
    )
    p_kf.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_kf.add_argument("--date", default=None, help="수급 스냅샷 날짜 (YYYY-MM-DD)")
    p_kf.add_argument("--limit", type=int, default=20,
                      help="조회 종목 수 상한 — 네이버 요청 예산(기본 20, 아침 리포트와 동일)")
    p_kf.set_defaults(func=cmd_kr_flow)

    p_dc = sub.add_parser(
        "delivery-check",
        help="소식통 배달 점검 — 오늘 리포트/브리핑이 실제로 닿았는가만 본다 "
             "(크론 제안: 화~토 06:35, US 마감 정산 뒤). 정상이면 무출력. "
             "종료코드 0=정상 / 1=미배달 있음 / 2=미배달은 없지만 확인 못 함.",
    )
    p_dc.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_dc.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD, 기본: KST 오늘)")
    p_dc.set_defaults(func=cmd_delivery_check)

    p_macro = sub.add_parser(
        "macro-collect",
        help="매크로 금리·환율(FRED) 시계열 수집 — data/ledger/macro_rates.jsonl에 "
             "멱등 append. 국면(US_BOND_10Y)의 원천. --days 0(기본)=전체 백필.",
    )
    p_macro.add_argument("--root", default=".", help="저장소 루트")
    p_macro.add_argument("--days", type=int, default=0,
                          help="최근 N일만 기록 (기본 0=전체 백필, 크론은 소량만 지정)")
    p_macro.set_defaults(func=cmd_macro_collect)

    p_pp = sub.add_parser(
        "param-propose",
        help="전략 파라미터 제안(AI 리뷰, 주간) — 제안만 기록·출력, 반영은 사람이 "
             "settings.yaml 로(판정은 experiments 루프). stdout = 텔레그램 노트.",
    )
    p_pp.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_pp.add_argument("--date", default=None, help="이 날짜가 속한 주를 리뷰 (YYYY-MM-DD)")
    p_pp.set_defaults(func=cmd_param_propose)

    p_ga = sub.add_parser(
        "governor-apply",
        help="파라미터 자동 반영 심사(6층 방어 + 방향 제약) — ALLOWED 범위 안(리스크를 "
             "줄이는 방향)만 config/auto_params.yaml 오버레이에 실반영, 범위 밖은 전부 "
             "제안만. 기본은 제안만(파일 미변경) — 실반영은 --live.",
    )
    p_ga.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_ga.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_ga.add_argument("--window-days", type=int, default=7, help="최근 N일 제안만 심사 (기본 7)")
    p_ga.add_argument("--dry-run", action="store_true",
                       help="심사만 하고 파일(오버레이·decisions.jsonl)을 전혀 쓰지 않는다. --live 보다 항상 우선한다")
    p_ga.add_argument("--live", action="store_true",
                       help="ALLOWED 범위 안에서 통과한 결정을 실제로 config/auto_params.yaml에 반영한다. "
                            "기본(미지정)은 심사·기록만 하고 오버레이는 건드리지 않는다(제안만)")
    p_ga.add_argument("--revert", default=None, metavar="KEY",
                       help="governor.ALLOWED 의 이름(예: strategies.vol_breakout.params.min_stop_bp) "
                            "하나를 오버레이에서 제거해 settings.yaml 기본값으로 되돌린다. 지정하면 "
                            "그 외 심사는 건너뛴다")
    p_ga.set_defaults(func=cmd_governor_apply)

    p_promote = sub.add_parser(
        "promote",
        help="백테스트 게이트 GO → config/settings.yaml 승격 반영 "
             "(enabled: true + validation.status: backtest_pass + evidence)",
    )
    p_promote.add_argument("--strategy", default=None, help="승격 대상 전략 id (settings.yaml strategies 키)")
    p_promote.add_argument("--gate", default=None,
                            help="backtest-gate CLI가 쓴 게이트 JSON (data/backtest/gate_<전략>_<날짜>.json)")
    p_promote.add_argument("--capital-fraction", default=None, metavar="KR=0.05,US=0.05",
                            help="선택. 주지 않으면 settings.yaml의 기존 capital_fraction을 그대로 둔다")
    p_promote.add_argument("--dry-run", action="store_true",
                            help="심사만 하고 정확한 YAML diff를 출력 — 파일은 쓰지 않는다")
    p_promote.add_argument("--settings", default=None, help="기본: config/settings.yaml")
    p_promote.add_argument("--list", action="store_true",
                            help="모든 전략의 enabled/validation.status/evidence 요약만 출력하고 종료")
    p_promote.set_defaults(func=cmd_promote)

    p_cap = sub.add_parser(
        "capital-review",
        help="자본 자동 강등 — 지는 전략의 capital_fraction 을 반감(하한 있음). "
             "config/auto_params.yaml 오버레이에 반영, 늘리는 방향은 절대 자동 아님.",
    )
    p_cap.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_cap.add_argument("--dry-run", action="store_true", help="심사만 하고 파일을 쓰지 않는다")
    p_cap.add_argument("--min-samples", type=int, default=20, help="강등 판단 최소 종결 표본 (기본 20)")
    p_cap.set_defaults(func=cmd_capital_review)

    p_cr = sub.add_parser(
        "close-report",
        help="장마감 결과 리포트 — 그날 만기된 outcome + 리더보드 판정 + 누적 스코어보드 (텍스트).",
    )
    p_cr.add_argument("--root", default=None, help="기본: 저장소 루트")
    p_cr.add_argument("--date", default=None, help="오늘로 볼 날짜 (YYYY-MM-DD)")
    p_cr.add_argument("--horizon", type=int, default=5, help="리더보드 판정에 쓸 지평(거래일). 기본 5")
    p_cr.add_argument("--trials", type=int, default=1,
                      help="리더보드 다중검정 보정 시행 횟수. 프롬프트·모델을 K개 시험했으면 K")
    p_cr.set_defaults(func=cmd_close_report)

    p_sj = sub.add_parser(
        "shadow-judge",
        help="LLM 섀도우 판단 기록 (주문 없음). 점수를 못 얻으면 종료코드 2.",
    )
    p_sj.add_argument("--root", default=None)
    p_sj.add_argument("--date", required=True, help="선정 원장의 날짜 (YYYY-MM-DD)")
    p_sj.add_argument("--market", required=True, choices=["KR", "US"])
    p_sj.add_argument("--producer", default="nemotron-3-ultra",
                      help="생산자 이름. 리더보드에서 이 이름으로 채점된다")
    p_sj.add_argument("--producer-version", default="free",
                      help="프롬프트를 바꾸면 올린다 — 안 올리면 변경 전후가 한 표본에 섞인다")
    p_sj.add_argument("--limit", type=int, default=60,
                      help="한 번에 채점할 종목 수 상한 (컨텍스트·비용 보호)")
    p_sj.set_defaults(func=cmd_shadow_judge)

    p_lb = sub.add_parser(
        "leaderboard",
        help="생산자별 승격 판정 (JSON). 승격 후보가 없으면 종료코드 2.",
    )
    p_lb.add_argument("--root", default=None)
    p_lb.add_argument("--horizon", type=int, default=5, help="지평(거래일). 기본 5")
    p_lb.add_argument("--trials", type=int, default=1,
                      help="시행 횟수(다중검정 보정). 프롬프트·모델을 K개 시험했으면 K")
    p_lb.set_defaults(func=cmd_leaderboard)

    p_narrate = sub.add_parser(
        "narrate",
        help="stdin 을 사람이 읽을 문장으로 (OPS_NARRATOR). 서술 불가 시 출력 없이 종료코드 1.",
    )
    p_narrate.set_defaults(func=cmd_narrate)

    p_kiwoom_probe = sub.add_parser("kiwoom-probe", help="키움 실키 등록 후 웹소켓 실시간시세 수동 스모크 (주문 없음)")
    p_kiwoom_probe.add_argument("--symbol", default="005930", help="기본: 삼성전자 (해외주식 실시간 지원 여부는 미검증)")
    p_kiwoom_probe.add_argument("--seconds", type=int, default=10)
    p_kiwoom_probe.set_defaults(func=cmd_kiwoom_probe)

    p_spread = sub.add_parser(
        "spread-sample",
        help="호가창 스프레드 실측 수집 (측정 전용, 원장 data/ledger/spread.jsonl)",
    )
    p_spread.add_argument("--market", choices=["KR", "US"], default=None,
                          help="심볼 시장 필터. 미지정이면 전부")
    p_spread.add_argument("--symbols", nargs="*", default=None,
                          help="기본: 워치리스트 + 전략 앵커 심볼")
    p_spread.add_argument("--rounds", type=int, default=1, help="반복 라운드 수 (심볼당 라운드당 1회 조회)")
    p_spread.add_argument("--interval-seconds", type=float, default=1.0,
                          help="호출 간 최소 간격(초). 0.2 미만은 5 TPS 상한으로 잘린다")
    p_spread.add_argument("--root", default=None)
    p_spread.set_defaults(func=cmd_spread_sample)

    p_peek = sub.add_parser(
        "peek",
        help="읽기 전용 시세 조회(현재가+최근 봉 n개, JSON) — 주문 경로 없음. LLM 판단 프로세스용",
    )
    p_peek.add_argument("--symbol", required=True, help="예: 005930 (KR), TQQQ (US)")
    p_peek.add_argument("--interval", default="5m", help="1m|5m|15m|1d (기본 5m)")
    p_peek.add_argument("--n", type=int, default=40, help=f"봉 개수 (상한 {_PEEK_MAX_N})")
    p_peek.set_defaults(func=cmd_peek)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
