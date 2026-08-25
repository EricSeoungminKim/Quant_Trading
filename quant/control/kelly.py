"""부분 켈리 자문(advisory) — 원장의 승률·payoff로 켈리 비율을 **표시만** 한다.

**자본 배분에 자동 반영되지 않는다.** 이 저장소는 거버너 층 0에서 사이징 자동화를
금지한다(루트 CLAUDE.md: "숫자가 자본 배분을 결정한다"는 것은 사람이 숫자를 보고
`settings.yaml`을 고친다는 뜻이지, 코드가 고친다는 뜻이 아니다). 이 모듈은 그
숫자 중 하나(켈리 비율)를 계산해 보여줄 뿐이다.
"""
from __future__ import annotations

MIN_N_DEFAULT = 30


def kelly_fraction(win_rate: float, payoff: float) -> float | None:
    """켈리 비율 f* = win_rate - (1 - win_rate) / payoff.

    `payoff`는 평균이익/평균손실 비율(b). b<=0(손실 표본이 전혀 없어 정의 불가한
    경우는 표본이 만들어낸 `float("inf")`로 들어와도 정상 계산된다 — q/inf=0.0)
    이거나 `win_rate`가 [0, 1] 밖이면(입력 자체가 말이 안 됨) None을 반환한다 —
    억지로 숫자를 만들어내지 않는다.
    """
    if payoff <= 0:
        return None
    if not (0.0 <= win_rate <= 1.0):
        return None
    return win_rate - (1.0 - win_rate) / payoff


def advisory(trips: list[dict], min_n: int = MIN_N_DEFAULT) -> list[dict]:
    """전략별 부분 켈리 자문. `trips`는 `quant.control.ledger.round_trips()`의
    출력(라운드트립 목록, 키: strategy/pnl/bps/pnl_known 등)을 그대로 받는다.

    표본이 `min_n` 미만이면 켈리 값을 **지어내지 않고** 거부 사유만 낸다
    (`_strategy_block`의 표본 부족 경고와 같은 문턱 철학, MIN_TRIPS_FOR_JUDGEMENT=30).
    손익미상(`pnl_known=False`) 트립은 집계에서 제외한다 — ledger 원장의 기존
    관례와 동일.
    """
    by_strategy: dict[str, list[dict]] = {}
    for t in trips:
        if not t.get("pnl_known", True):
            continue
        by_strategy.setdefault(str(t.get("strategy", "?")), []).append(t)

    out: list[dict] = []
    for strategy in sorted(by_strategy):
        rows = by_strategy[strategy]
        n = len(rows)
        if n < min_n:
            out.append({
                "strategy": strategy, "n": n, "win_rate": None, "payoff": None,
                "full_kelly": None, "quarter_kelly": None,
                "note": f"표본 부족 ({n}/{min_n}건) — 산출 거부",
            })
            continue

        wins = [t for t in rows if t["pnl"] > 0]
        losses = [t for t in rows if t["pnl"] <= 0]
        win_rate = len(wins) / n
        avg_win_bps = sum(t["bps"] for t in wins) / len(wins) if wins else 0.0
        avg_loss_bps = abs(sum(t["bps"] for t in losses) / len(losses)) if losses else 0.0
        if avg_loss_bps > 0:
            payoff = avg_win_bps / avg_loss_bps
        else:
            payoff = float("inf") if avg_win_bps > 0 else 0.0

        full = kelly_fraction(win_rate, payoff)
        if full is None or full <= 0:
            note = "엣지 없음 — 배분 근거 없음"
            quarter = None if full is None else round(full / 4, 4)
        else:
            note = ""
            quarter = round(full / 4, 4)

        out.append({
            "strategy": strategy, "n": n,
            "win_rate": round(win_rate, 4),
            # payoff=inf(손실 표본이 하나도 없음)는 JSON에 그대로 못 실으므로 문자열로
            # 정직하게 표기한다(None은 "계산 안 됨"과 겹쳐 혼동을 만든다) — 그래도
            # full_kelly는 정상 계산된다(q/inf=0.0이 파이썬에서 그대로 성립).
            "payoff": "inf" if payoff == float("inf") else round(payoff, 3),
            "full_kelly": None if full is None else round(full, 4),
            "quarter_kelly": quarter,
            "note": note,
        })
    return out
