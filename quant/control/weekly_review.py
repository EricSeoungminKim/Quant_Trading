"""주간 재검토 세션 — 토요일 아침, 양시장이 다 끝난 뒤 (2026-08-26 소유자 지시).

> "금요일에서 토요일로 넘어가는 6AM, 한 주의 한국장·미국장이 둘 다 끝났을 때 —
> 1주일 동안 기록하고 조사했던 것들과 진입했던 매매를 재검토하고, 문제는
> 없었는지, 이득/손해 패턴은 무엇이었는지, 전략이 이번 주 어떻게 반영됐는지."

전부 **결정론**(원장 재계산) — LLM 없음. 각 절이 실패해도 다른 절은 나간다.
판정하지 않는다: 숫자를 정리해 보여주고, 결론(전략 교체 등)은 사람이 낸다 —
자동 판정(개선/악화/사망)은 experiments 루프가 매일 별도로 한다.

순수 조립 + 텍스트 렌더 — 원장/가격 로딩은 호출부(cli) 몫.
"""
from __future__ import annotations

import statistics as st
from datetime import date, timedelta


def week_range(any_day: date) -> tuple[date, date]:
    """그 주의 (월요일, 일요일). 토 06:25 실행이면 '막 끝난 주'가 나온다."""
    monday = any_day - timedelta(days=any_day.weekday())
    return monday, monday + timedelta(days=6)


def weekly_strategy_stats(trips: list[dict], start: date, end: date) -> list[dict]:
    """전략별 주간 성적 — 종결 원장에서 그 주 청산분만."""
    s_iso, e_iso = start.isoformat(), end.isoformat() + "~"
    by: dict[str, list[dict]] = {}
    for t in trips:
        d = str(t.get("exit_ts") or "")[:10]
        if not (s_iso <= d <= e_iso):
            continue
        by.setdefault(t.get("strategy") or "?", []).append(t)
    out = []
    for sid, ts in sorted(by.items()):
        bps = [float(t.get("bps") or 0) for t in ts]
        wins = sum(1 for b in bps if b > 0)
        out.append({
            "strategy": sid, "n": len(ts), "wins": wins,
            "win_rate": round(wins / len(ts), 3) if ts else None,
            "avg_bps": round(sum(bps) / len(bps), 1) if bps else None,
            "total_pnl_bps": round(sum(bps), 1),
        })
    return sorted(out, key=lambda r: r["total_pnl_bps"])


def loss_patterns(trips: list[dict], start: date, end: date, top: int = 5) -> dict:
    """손해 패턴 — 상위 손실 목록 + 보유시간 버킷별 평균.

    청산 사유는 종결 원장에 없으므로 **추정하지 않는다** — 있는 축(전략·종목·
    보유시간·bp)만으로 패턴을 보인다."""
    from datetime import datetime

    s_iso = start.isoformat()
    e_iso = end.isoformat() + "~"
    week = [t for t in trips if s_iso <= str(t.get("exit_ts") or "")[:10] <= e_iso]
    losses = sorted((t for t in week if float(t.get("bps") or 0) < 0),
                    key=lambda t: float(t.get("bps") or 0))

    def _hold_min(t):
        try:
            a = datetime.fromisoformat(str(t["entry_ts"]))
            b = datetime.fromisoformat(str(t["exit_ts"]))
            return (b - a).total_seconds() / 60
        except Exception:  # noqa: BLE001
            return None

    buckets = {"<10분": [], "10~60분": [], "1시간~장중": [], "오버나이트+": []}
    for t in week:
        m = _hold_min(t)
        if m is None:
            continue
        b = ("<10분" if m < 10 else "10~60분" if m < 60
             else "1시간~장중" if m < 390 else "오버나이트+")
        buckets[b].append(float(t.get("bps") or 0))

    return {
        "n_week": len(week),
        "n_losses": len(losses),
        "worst": [{
            "strategy": t.get("strategy"), "symbol": t.get("symbol"),
            "bps": round(float(t.get("bps") or 0), 1),
            "hold_min": round(_hold_min(t) or -1),
        } for t in losses[:top]],
        "hold_buckets": {
            k: {"n": len(v), "avg_bps": round(sum(v) / len(v), 1)}
            for k, v in buckets.items() if v
        },
    }


def weekly_index_flow(closes_by_label: dict[str, list[float]]) -> list[dict]:
    """주간 지수 흐름 — {라벨: 그 주의 일봉 종가 리스트(월→금)}. 2개 미만이면 생략."""
    out = []
    for label, closes in closes_by_label.items():
        if not closes or len(closes) < 2 or closes[0] <= 0:
            continue
        rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        out.append({
            "label": label,
            "week_pct": round((closes[-1] / closes[0] - 1) * 100, 2),
            "best_day_pct": round(max(rets), 2),
            "worst_day_pct": round(min(rets), 2),
        })
    return out


def weekly_review_text(
    start: date, end: date,
    index_flow: list[dict],
    strategy_stats: list[dict],
    losses: dict,
    score_accuracy: dict | None,
    equity_delta: dict | None,
    tca: dict | None = None,
) -> str:
    lines = [f"🗓 주간 재검토 — {start.isoformat()} ~ {end.isoformat()}"]

    if index_flow:
        lines.append("\n[주간 장 흐름]")
        for i in index_flow:
            lines.append(f"  {i['label']}: 주간 {i['week_pct']:+.2f}% "
                         f"(최고일 {i['best_day_pct']:+.2f}% / 최악일 {i['worst_day_pct']:+.2f}%)")

    if equity_delta:
        lines.append(f"\n[자본] 주초 {equity_delta['start']:,.0f}원 → 주말 "
                     f"{equity_delta['end']:,.0f}원 ({equity_delta['pct']:+.2f}%)")

    if strategy_stats:
        lines.append("\n[전략별 이번 주]")
        for r in strategy_stats:
            lines.append(f"  {r['strategy']}: {r['n']}건 승률 "
                         f"{r['win_rate']*100:.0f}% 평균 {r['avg_bps']:+.1f}bp "
                         f"합계 {r['total_pnl_bps']:+.0f}bp")
    else:
        lines.append("\n[전략별 이번 주] 종결 거래 없음")

    if losses.get("n_week"):
        lines.append(f"\n[손해 패턴] 주간 {losses['n_week']}건 중 손실 {losses['n_losses']}건")
        for w in losses["worst"]:
            lines.append(f"  최악: [{w['strategy']}] {w['symbol']} "
                         f"{w['bps']:+.1f}bp (보유 {w['hold_min']}분)")
        for k, v in losses.get("hold_buckets", {}).items():
            lines.append(f"  보유 {k}: {v['n']}건 평균 {v['avg_bps']:+.1f}bp")

    lines.append("\n[기록 vs 실제 — 점수 적중률(다음 거래일)]")
    if score_accuracy:
        for bucket, v in score_accuracy.items():
            lines.append(f"  {bucket}: {v['n']}건 · 다음날 평균 {v['avg_next_pct']:+.2f}% "
                         f"· 양봉률 {v['hit_rate']*100:.0f}%")
        lines.append("  ※ 점수가 높을수록 다음날이 좋아야 시스템이 장을 읽고 있는 것")
    else:
        lines.append("  표본 부족 — 점수 원장이 쌓이면 여기서 적중률이 나온다")

    lines.append("\n[슬리피지 TCA — 의도 가격 vs 실제 체결가]")
    if tca:
        o = tca["overall"]
        lines.append(f"  전체 {o['n']}건 평균 {o['avg_bps']:+.1f}bp (p90 {o['p90_bps']:+.1f}bp)")
        for mkt, v in tca.get("by_market", {}).items():
            lines.append(f"  {mkt}: {v['n']}건 평균 {v['avg_bps']:+.1f}bp (p90 {v['p90_bps']:+.1f}bp)")
        lines.append("  ※ paper 체결가는 브로커 모델값 — 지금은 모델 슬리피지 가정 검증, "
                     "실거래 전환 시 실측 대조가 진가")
    else:
        lines.append("  표본 없음")

    lines.append("\n※ 결론은 내지 않는다 — 개선/악화/사망 판정은 매일 16:30 "
                 "자동 판정 루프가, 전략 교체 결정은 사람이 한다.")
    return "\n".join(lines)
