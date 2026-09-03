"""거래 부검 — 원장의 "졌다"를 "무엇 때문에 졌다"로 바꾸는 층 (2026-08-22).

## 왜 이 파일이 생겼나

`ledger.scoreboard_text`는 전략별 승률·payoff·기대값을 낸다. 그건 **결과**다.
2026-08-21에 그 결과가 "7개 전략 중 6개가 수수료 전에도 음수"라고 말했지만,
거기서 무엇을 고쳐야 하는지는 나오지 않았다 — 진입이 나쁜 건지, 청산이 나쁜
건지, 종목 선정이 나쁜 건지 스코어보드는 구분하지 못한다.

그날 답을 준 건 손으로 쓴 1회용 스크립트였다: 체결 시각을 1분봉에 얹어 보유 중
최대유리(MFE)/최대불리(MAE)를 재니 **MFE 중앙 +113bp인데 실현은 -47bp** —
이익 구간에 들어갔다가 되돌림을 그대로 맞고 있었다. 그리고 "고점 진입"은
사실이지만(레인지 위치 중앙 0.89) 승패를 가르지 못했다(rho=+0.00, 승 0.84 vs
패 0.89). 즉 **고칠 곳은 진입이 아니라 청산**이었다.

**그 분석이 1회용이면 다음에도 손으로 다시 해야 한다.** 이 파일은 그걸 매주
같은 방식으로 돌 수 있게 고정한 것이다 — 데이터가 쌓이는 것과 개선점이 보이는
것은 다르고, 그 사이를 잇는 게 여기다.

## 정직성 규칙

- **커버리지를 먼저 보고한다.** 1분봉이 없는 종결 건은 재생할 수 없다. 몇 건
  중 몇 건을 봤는지 말하지 않는 숫자는 근거가 아니다(2026-08-21 실측: 106건 중
  74건, 70%).
- **레인지 위치는 진단이지 게이트가 아니다.** 진입 시점까지의 봉만 써서
  look-ahead를 피하지만, 그래도 이걸 진입 필터로 쓰려면 승/패 대조가 먼저다 —
  그래서 `entry_range_position`이 승패를 나눠서 함께 낸다.
- **표본이 적으면 적다고 쓴다.** 여기서는 판정하지 않는다. 숫자를 내고, 판단은
  사람이 한다.

## 평면

`quant/control/` 소속(제어 평면) — 원장을 읽어 다음 세션을 낫게 하는 층이다.
거래 평면을 임포트하지 않는다. 봉 로딩은 **주입**받는다(`load_bars`) — 디스크
접근을 이 모듈에 박지 않아야 테스트가 봉을 만들어 넣을 수 있고, 나중에 소스가
바뀌어도(예: OLAP) 이 파일은 안 바뀐다.
"""
from __future__ import annotations

import statistics as st
from typing import Callable

from quant.control.cost_model import FALLBACK_ROUND_TRIP_BP

# 왕복 비용(bp) 기본값. 재생 결과에서 이 값을 빼 순bp로 비교한다. 호출부가
# 설정값으로 덮을 수 있다.
#
# 2026-09-02: 여기 20.0을 따로 적어 두는 대신 `cost_model`의 설정 유도값을
# 그대로 쓴다 — 같은 원장을 읽는 세 리포트(부검·비용모델·공개 성과)가 서로 다른
# 왕복 비용으로 "엣지가 남았나"를 판정하고 있었다(cost_model 상단 주석 참고).
DEFAULT_ROUND_TRIP_BP = FALLBACK_ROUND_TRIP_BP


def _pct_bp(now: float, base: float) -> float:
    return (now - base) / base * 1e4


def replay_trip(
    trip: dict,
    load_bars: Callable[[str, object], object | None],
) -> dict | None:
    """종결 1건을 1분봉에 얹어 부검한다. 봉이 없거나 너무 짧으면 `None`
    (없는 걸 지어내지 않는다 — 호출부가 커버리지로 센다).

    반환:
      entry_bp_realized  진입~청산 봉 종가 기준 실현(bp) — 원장 bps와 교차검증용
      mfe_bp / mae_bp    보유 중 최대유리 / 최대불리(bp)
      mfe_session_bp     진입~장 마감까지의 최대유리(bp) — 더 들고 있었으면?
      to_close_bp        마감까지 보유했을 때(bp)
      range_pos          진입 시점 **까지의** 당일 레인지 위치(0=저가, 1=고가).
                         봉이 5개 미만이면 None — 표본이 적으면 위치가 소음이다.
      exit_efficiency    실현 / MFE. 1.0 = 최고점에 팔았다, 0 이하 = 이익 구간을
                         전부 반납했다. **MFE<=0이면 None**(잡을 이익이 애초에
                         없었던 거래에 효율을 매기면 청산 탓으로 오독된다).
      hold_min           보유 분
    """
    entry_ts, exit_ts = trip.get("entry_ts"), trip.get("exit_ts")
    symbol = trip.get("symbol")
    if not (entry_ts and exit_ts and symbol):
        return None
    day = load_bars(symbol, entry_ts)
    if day is None or len(day) < 2:
        return None

    hold = day[(day.index >= entry_ts) & (day.index <= exit_ts)]
    if len(hold) < 2:
        return None
    entry_px = float(hold.iloc[0]["close"])
    if entry_px <= 0:
        return None

    mfe = _pct_bp(float(hold["high"].max()), entry_px)
    mae = _pct_bp(float(hold["low"].min()), entry_px)
    realized = _pct_bp(float(hold.iloc[-1]["close"]), entry_px)

    before = day[day.index <= entry_ts]
    range_pos = None
    if len(before) >= 5:
        hi, lo = float(before["high"].max()), float(before["low"].min())
        if hi > lo:
            range_pos = (entry_px - lo) / (hi - lo)

    after = day[day.index >= entry_ts]
    to_close = _pct_bp(float(after.iloc[-1]["close"]), entry_px) if len(after) else None
    mfe_session = _pct_bp(float(after["high"].max()), entry_px) if len(after) else None

    return {
        "strategy": trip.get("strategy"),
        "symbol": symbol,
        "ledger_bps": trip.get("bps"),
        "realized_bp": realized,
        "mfe_bp": mfe,
        "mae_bp": mae,
        "mfe_session_bp": mfe_session,
        "to_close_bp": to_close,
        "range_pos": range_pos,
        "exit_efficiency": (realized / mfe) if mfe > 0 else None,
        "hold_min": len(hold),
    }


def replay_all(trips: list[dict], load_bars) -> tuple[list[dict], int]:
    """재생 가능한 것만 부검한다. `(rows, skipped)` — skipped 가 커버리지다."""
    rows, skipped = [], 0
    for t in trips:
        r = replay_trip(t, load_bars)
        if r is None:
            skipped += 1
        else:
            rows.append(r)
    return rows, skipped


def _med(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return st.median(vals) if vals else None


def summarize(rows: list[dict]) -> dict:
    """부검 결과 집계. **판정하지 않는다** — 숫자만 낸다."""
    if not rows:
        return {"n": 0}
    reached = sum(1 for r in rows if r["mfe_bp"] >= 50)
    effs = [r["exit_efficiency"] for r in rows if r["exit_efficiency"] is not None]
    return {
        "n": len(rows),
        "realized_bp_median": _med(rows, "realized_bp"),
        "mfe_bp_median": _med(rows, "mfe_bp"),
        "mae_bp_median": _med(rows, "mae_bp"),
        "mfe_session_bp_median": _med(rows, "mfe_session_bp"),
        "to_close_bp_median": _med(rows, "to_close_bp"),
        "reached_50bp_pct": reached / len(rows) * 100,
        "exit_efficiency_median": st.median(effs) if effs else None,
        "hold_min_median": _med(rows, "hold_min"),
    }


def entry_range_control(rows: list[dict]) -> dict:
    """진입 레인지 위치가 **실제로 승패를 가르는가** — 대조군 없이 "고점에서
    샀다"만 보면 원인으로 오독한다(2026-08-21: 승 0.84 vs 패 0.89, rho=+0.00).

    `rho`는 레인지 위치 vs 원장 bps의 피어슨 상관. 표본 8건 미만이면 None."""
    rs = [r for r in rows if r.get("range_pos") is not None and r.get("ledger_bps") is not None]
    if not rs:
        return {"n": 0}
    win = [r for r in rs if r["ledger_bps"] > 0]
    lose = [r for r in rs if r["ledger_bps"] <= 0]
    out = {
        "n": len(rs),
        "n_win": len(win),
        "n_lose": len(lose),
        "range_pos_median_win": st.median([r["range_pos"] for r in win]) if win else None,
        "range_pos_median_lose": st.median([r["range_pos"] for r in lose]) if lose else None,
        "rho": None,
    }
    if len(rs) >= 8:
        xs = [r["range_pos"] for r in rs]
        ys = [r["ledger_bps"] for r in rs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        den = (sum((a - mx) ** 2 for a in xs) * sum((b - my) ** 2 for b in ys)) ** 0.5
        out["rho"] = (num / den) if den else None
    return out


def simulate_exit_rules(
    trips: list[dict],
    load_bars,
    rules: list[tuple[str, float | None, float | None]],
    cost_bp: float = DEFAULT_ROUND_TRIP_BP,
) -> list[dict]:
    """사전 지정 청산 규칙을 실제 봉에 재생한다.

    `rules`: `[(이름, take_profit_bp|None, stop_bp|None), ...]` — **호출부가
    미리 정한 목록만** 받는다. 이 함수가 파라미터를 탐색하지 않는 게 요점이다:
    27건에 규칙 20개를 시험하면 그중 몇 개는 반드시 우연히 훌륭해 보인다.
    탐색 횟수는 호출부가 `len(rules)`로 밝힌다.

    보수적 가정: 봉 **종가**로만 판단·체결한다(장중 고가 터치로 익절됐다고 치지
    않는다). 미청산은 그날 마지막 봉 종가로 청산(엔진의 마감 청산과 동일).
    결과에서 `cost_bp`를 뺀다.
    """
    acc: dict[str, list[float]] = {name: [] for name, _, _ in rules}
    for t in trips:
        entry_ts, symbol = t.get("entry_ts"), t.get("symbol")
        if not (entry_ts and symbol):
            continue
        day = load_bars(symbol, entry_ts)
        if day is None:
            continue
        after = day[day.index >= entry_ts]
        if len(after) < 5:
            continue
        closes = [float(v) for v in after["close"].tolist()]
        if closes[0] <= 0:
            continue
        for name, tp, sl in rules:
            acc[name].append(_sim_one(closes, tp, sl) - cost_bp)

    out = []
    for name, _, _ in rules:
        v = acc[name]
        if not v:
            out.append({"rule": name, "n": 0})
            continue
        out.append({
            "rule": name, "n": len(v),
            "median_bp": st.median(v), "mean_bp": sum(v) / len(v),
            "win_pct": sum(1 for x in v if x > 0) / len(v) * 100,
            "total_bp": sum(v),
        })
    return out


def _sim_one(closes: list[float], tp: float | None, sl: float | None) -> float:
    entry = closes[0]
    for px in closes[1:]:
        bp = _pct_bp(px, entry)
        if sl is not None and bp <= -sl:
            return bp
        if tp is not None and bp >= tp:
            return bp
    return _pct_bp(closes[-1], entry)


def forensics_text(
    rows: list[dict], skipped: int, *, title: str = "거래 부검",
    rules_result: list[dict] | None = None, by_strategy: bool = True,
) -> str:
    """사람이 읽는 리포트. 커버리지를 **맨 위**에 둔다 — 그게 이 숫자들이 얼마나
    믿을 만한지를 정한다."""
    total = len(rows) + skipped
    lines = [f"🔬 {title}"]
    if total == 0:
        return lines[0] + "\n원장에 종결 거래가 없다."
    pct = len(rows) / total * 100
    lines.append(f"재생 {len(rows)}/{total}건 ({pct:.0f}%) — 1분봉 없는 {skipped}건 제외")
    if len(rows) < 30:
        lines.append("⚠️ 표본 부족 — 이 숫자로 파라미터를 바꾸지 마라")
    if not rows:
        return "\n".join(lines)

    groups = [("전체", rows)]
    if by_strategy:
        seen: dict[str, list[dict]] = {}
        for r in rows:
            seen.setdefault(r.get("strategy") or "?", []).append(r)
        groups += sorted(seen.items())

    for name, g in groups:
        s = summarize(g)
        lines.append(f"\n[{name}] {s['n']}건")
        lines.append(
            f"  실현 중앙 {s['realized_bp_median']:+.1f}bp · "
            f"MFE 중앙 {s['mfe_bp_median']:+.1f}bp · MAE 중앙 {s['mae_bp_median']:+.1f}bp"
        )
        eff = s["exit_efficiency_median"]
        lines.append(
            f"  +50bp 도달 {s['reached_50bp_pct']:.0f}% · "
            f"청산 효율 중앙 {('%.2f' % eff) if eff is not None else '측정 불가'} "
            f"(1.0=최고점 매도, 0 이하=이익 전부 반납)"
        )
        if s["to_close_bp_median"] is not None:
            lines.append(f"  마감까지 보유했다면 중앙 {s['to_close_bp_median']:+.1f}bp")

    ctl = entry_range_control(rows)
    if ctl.get("n"):
        lines.append(f"\n[진입 위치 대조군] 승 {ctl['n_win']}건 / 패 {ctl['n_lose']}건")
        w, lo = ctl["range_pos_median_win"], ctl["range_pos_median_lose"]
        lines.append(
            f"  당일 레인지 위치 중앙 — 승 {('%.2f' % w) if w is not None else '-'} vs "
            f"패 {('%.2f' % lo) if lo is not None else '-'} (1.0=당시 고가)"
        )
        rho = ctl["rho"]
        if rho is None:
            lines.append("  상관 rho: 표본 부족(8건 미만)")
        else:
            verdict = "진입 위치가 승패를 가르지 못한다" if abs(rho) < 0.2 else "관련 있을 수 있다 — 추가 검증 필요"
            lines.append(f"  상관 rho = {rho:+.2f} → {verdict}")

    if rules_result:
        lines.append(f"\n[청산 규칙 재생] 사전 지정 {len(rules_result)}종 (탐색 {len(rules_result)}회)")
        for r in rules_result:
            if not r.get("n"):
                lines.append(f"  {r['rule']:22s} 표본 없음")
                continue
            lines.append(
                f"  {r['rule']:22s} n={r['n']:3d} 중앙 {r['median_bp']:+7.1f} "
                f"평균 {r['mean_bp']:+7.1f} 승률 {r['win_pct']:3.0f}% 합계 {r['total_bp']:+8.0f}"
            )
        lines.append("  ※ 비용 차감 후. 봉 종가 체결 가정이라 손절 쪽이 낙관적이다.")

    return "\n".join(lines)
