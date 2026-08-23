"""파라미터 자동 반영 거버너 — "한 번에 크게 망하지 않게" 하는 방어층.

사용자 결정(2026-08-13): 하네스가 찾은 파라미터를 **자동 반영한다**. 단
"자동 반영 했을때 한번에 크게 망할 수 있으니깐 방어층을 두껍게".

## 층 0 — 폭발 반경 (다른 어떤 층보다 중요하다)

**자동 반영은 "무엇을 볼 것인가"만 건드린다. "얼마를 걸 것인가"는 절대 못 건드린다.**

    자동 허용 (선정 문턱)          자동 금지 (리스크)
    ─────────────────────         ─────────────────────
    min_articles                  capital_fraction
    min_streak                    stop_loss_pct
    news_hot                      max_entries_per_session
    rank_top                      risk_budget_pct
    vol_surge_ratio               max_leverage
    trending_min                  threshold(watch-score)

이유는 회복 가능성이 다르기 때문이다. 선정 문턱을 잘못 조이면 **그날 좋은 종목을
놓칠 뿐**이고 다음날 되돌리면 끝난다. 사이징을 잘못 키우면 **계좌가 날아가고**
되돌릴 자산이 남지 않는다. 실수의 비용이 비대칭이므로 권한도 비대칭이어야 한다.

`ALLOWED` 밖의 이름은 어떤 근거를 들고 와도 거부한다 — 근거의 품질을 심사하는
층이 아니라, **애초에 그 문을 열지 않는** 층이다.

## 층 1~6

1. **봉투(envelope)** — 각 파라미터의 하드 min/max. 밖이면 거부.
2. **보폭(step)** — 1회 변경 폭 상한. "3 → 30" 같은 도약을 막는다.
3. **냉각(cooldown)** — 같은 파라미터를 N일 안에 다시 못 바꾼다. 매일 흔들면
   그 파라미터의 성과를 영영 측정할 수 없다.
4. **증거(evidence)** — 최소 표본 수 + 개선 폭. 표본이 적으면 우연이다.
5. **동시 변경 상한** — 한 번에 하나만. 둘을 같이 바꾸면 어느 쪽이 효과였는지
   모른다(그리고 둘 다 나빴을 때 원인을 못 가린다).
6. **자동 롤백** — 반영 후 성과가 기준 이하로 나빠지면 되돌린다.

모든 결정은 `decisions.jsonl`에 append-only로 남는다 — 거부도 남긴다. 무엇이
거부됐는지가 방어층이 실제로 일하는지 보여주는 유일한 증거다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

# --- 층 0: 폭발 반경 -------------------------------------------------------
# 이름 → (하한, 상한, 1회 최대 보폭, 냉각일)
ALLOWED: dict[str, tuple[float, float, float, int]] = {
    "min_articles":      (1, 6, 1, 7),
    "min_streak":        (1, 5, 1, 7),
    "news_hot":          (2, 10, 1, 7),
    "rank_top":          (1, 10, 2, 7),
    "vol_surge_ratio":   (1.5, 6.0, 0.5, 14),
    "vol_elevated_ratio": (1.1, 3.0, 0.25, 14),
    "trending_min":      (0, 100, 5, 7),
}

# 자동 반영이 절대 닿으면 안 되는 이름. ALLOWED 화이트리스트만으로도 막히지만,
# **이름을 명시해 두면 실수로 ALLOWED 에 추가하는 것을 테스트가 잡는다.**
FORBIDDEN = frozenset({
    "capital_fraction", "stop_loss_pct", "max_entries_per_session",
    "risk_budget_pct", "max_leverage", "threshold", "partial_take_pct",
    "full_take_pct", "entry_window_seconds", "burn_in_max_capital_fraction",
})

MIN_SAMPLES = 30          # 이 저장소가 이미 쓰는 최소 표본선(ledger.MIN_TRIPS_FOR_JUDGEMENT)
MIN_IMPROVEMENT = 0.05    # 기대 개선이 5% 미만이면 흔들 가치가 없다
MAX_CHANGES_PER_RUN = 1   # 층 5
ROLLBACK_DEGRADE = -0.10  # 반영 후 10% 이상 악화되면 되돌린다


@dataclass
class Proposal:
    """하네스(LLM)가 내는 제안. **LLM 은 여기까지만 한다** — 채점과 반영은 코드다."""
    name: str
    current: float
    proposed: float
    samples: int
    expected_improvement: float   # 0.08 = 8% 개선 기대
    rationale: str = ""


@dataclass
class Decision:
    proposal: Proposal
    accepted: bool
    reason: str
    applied_value: float | None = None
    layer: str = ""               # 어느 층에서 걸렸나
    blocked_layers: list[str] = field(default_factory=list)


def _history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def last_change(name: str, decisions: list[dict]) -> date | None:
    """이 파라미터가 마지막으로 **실제 반영된** 날. 거부는 세지 않는다."""
    best: date | None = None
    for row in decisions:
        if row.get("name") != name or not row.get("accepted"):
            continue
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or d > best:
            best = d
    return best


def evaluate(proposal: Proposal, today: date, decisions: list[dict],
             changes_this_run: int = 0) -> Decision:
    """제안 하나를 모든 층에 통과시킨다. 하나라도 걸리면 거부."""
    p = proposal

    # 층 0 — 폭발 반경
    if p.name in FORBIDDEN:
        return Decision(p, False, f"리스크 파라미터는 자동 반영 금지: {p.name}", layer="0-blast-radius")
    if p.name not in ALLOWED:
        return Decision(p, False, f"자동 반영 허용 목록에 없음: {p.name}", layer="0-blast-radius")

    lo, hi, max_step, cooldown_days = ALLOWED[p.name]

    # 층 4 — 증거
    if p.samples < MIN_SAMPLES:
        return Decision(p, False, f"표본 부족 {p.samples}/{MIN_SAMPLES}", layer="4-evidence")
    if p.expected_improvement < MIN_IMPROVEMENT:
        return Decision(
            p, False,
            f"기대 개선 {p.expected_improvement:.1%} < {MIN_IMPROVEMENT:.0%}",
            layer="4-evidence",
        )

    # 층 5 — 동시 변경 상한
    if changes_this_run >= MAX_CHANGES_PER_RUN:
        return Decision(p, False, f"이번 회차 변경 상한 {MAX_CHANGES_PER_RUN}건 도달", layer="5-one-at-a-time")

    # 층 3 — 냉각
    prev = last_change(p.name, decisions)
    if prev is not None and (today - prev).days < cooldown_days:
        return Decision(
            p, False,
            f"냉각 중 — 마지막 변경 {prev} ({cooldown_days}일 필요)",
            layer="3-cooldown",
        )

    # 층 1 — 봉투
    if not (lo <= p.proposed <= hi):
        return Decision(p, False, f"봉투 밖: {p.proposed} ∉ [{lo}, {hi}]", layer="1-envelope")

    # 층 2 — 보폭 (봉투 안이라도 한 번에 크게 못 움직인다)
    step = abs(p.proposed - p.current)
    if step > max_step:
        clamped = p.current + max_step * (1 if p.proposed > p.current else -1)
        clamped = min(hi, max(lo, clamped))
        return Decision(
            p, True,
            f"보폭 제한으로 {p.proposed} → {clamped} 로 축소 반영",
            applied_value=clamped, layer="2-step-limit",
        )

    return Decision(p, True, "모든 층 통과", applied_value=p.proposed, layer="")


def decide(proposals: list[Proposal], today: date, ledger_path: Path) -> list[Decision]:
    """제안 묶음을 심사한다. 통과분은 층 5 때문에 최대 MAX_CHANGES_PER_RUN 건.

    **기대 개선이 큰 순으로 심사한다** — 상한이 1건이므로 순서가 곧 우선순위다.
    """
    decisions = _history(ledger_path)
    out: list[Decision] = []
    accepted = 0
    for p in sorted(proposals, key=lambda x: -x.expected_improvement):
        d = evaluate(p, today, decisions, changes_this_run=accepted)
        if d.accepted:
            accepted += 1
        out.append(d)
    return out


def record(decisions: list[Decision], today: date, path: Path) -> int:
    """결정을 append-only 로 남긴다. **거부도 남긴다** — 방어층이 실제로 일하는지는
    거부 기록으로만 확인할 수 있다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps({
                "date": today.isoformat(),
                "name": d.proposal.name,
                "current": d.proposal.current,
                "proposed": d.proposal.proposed,
                "applied": d.applied_value,
                "accepted": d.accepted,
                "reason": d.reason,
                "layer": d.layer,
                "samples": d.proposal.samples,
                "expected_improvement": d.proposal.expected_improvement,
                "rationale": d.proposal.rationale[:500],
            }, ensure_ascii=False) + "\n")
    return len(decisions)


def rollback_candidates(decisions: list[dict], realized: dict[str, float],
                        threshold: float = ROLLBACK_DEGRADE) -> list[dict]:
    """반영 후 성과가 나빠진 변경들 — 되돌릴 대상.

    `realized`: {파라미터명: 반영 전후 성과 변화율}. -0.12 면 12% 악화.
    **개선을 기대하고 바꿨는데 나빠졌으면 되돌린다** — 하네스의 가설이 틀릴 수
    있다는 전제가 이 층의 존재 이유다.
    """
    out = []
    for row in decisions:
        if not row.get("accepted"):
            continue
        delta = realized.get(row.get("name"))
        if delta is not None and delta <= threshold:
            out.append({**row, "realized_change": delta})
    return out


def summary(decisions: list[Decision]) -> str:
    """텔레그램용 한 문단. 무엇이 왜 거부됐는지가 본문이다."""
    ok = [d for d in decisions if d.accepted]
    no = [d for d in decisions if not d.accepted]
    lines = [f"🎛 파라미터 자동 반영 — 제안 {len(decisions)}건 · 반영 {len(ok)}건"]
    for d in ok:
        lines.append(f"  ✅ {d.proposal.name}: {d.proposal.current} → {d.applied_value}"
                     f" (표본 {d.proposal.samples}건)")
        if d.layer:
            lines.append(f"     ↳ {d.reason}")
    for d in no:
        lines.append(f"  ⛔ {d.proposal.name} [{d.layer}] {d.reason}")
    return "\n".join(lines)
