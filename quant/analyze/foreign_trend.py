"""외국인 수급 추종 라벨러(서브프로젝트 I) — 순수 함수, 네트워크·LLM 없음.

**사용자가 정한 원칙** (`docs/superpowers/specs/2026-08-17-foreign-flow-report-design.md`):
"한국주식은 기관 매수세는 노이즈 — 메인 리서치와 비리 없는 매수는 오로지
외국인 매수 추세". 기관은 표시는 남기되(`inst_follows`) 라벨 판단에는
쓰지 않는다 — **기관 단독 유입은 어떤 라벨도 올리지 않는다.**

## 판단 규칙(사용자 원문, 2026-08-17)

종목별 일별 외국인 순매수 시계열에서(누적 순잔여 = 관찰 창 내 순매수 합):

- 유입 후 일부 이탈(예: +100억 → 익일 -50억): "잔여 자본이 남아 있다" —
  부분 익절/조정 노림 가능성. 라벨 `관망(잔여 있음)`.
- 이탈이 연달아 이어짐(2일 이상 연속 순매도): 지수·타 주체가 받쳐도
  하락 추세 가능성 — 라벨 `이탈 추세(부분 매도/중립 고려)`.
- 이탈 후 재유입, 특히 이전 이탈분을 넘는 재유입: 매수 시그널 —
  라벨 `매수 시그널(재유입)`. 하루 유입·분할 유입 모두 인정.
- 이탈→재유입 반복(하루 단위 교차, 재유입이 직전 이탈을 못 넘음):
  `관망(하루 더)` — 다음날 추세로 스탠스 조정.
- 외국인 유입에 기관이 따라붙으면 강화 신호(참고 표기, `inst_follows`).

## 판정 알고리즘

시계열을 부호(양/음/0)가 같은 연속 구간(run)으로 나눈 뒤, **가장 최근 run**
과 그 바로 앞 run 만 본다 — 사용자 원칙("다음날 추세로 스탠스 조정")이
최신 반전을 최우선으로 두기 때문이다:

- 최근 run 이 음수(순매도)면: 길이 ≥2 → `이탈 추세`, 길이 1(단발 이탈) →
  `관망(잔여 있음)`.
- 최근 run 이 양수(순매수)면: 바로 앞 run 이 음수였을 때만 "재유입" 로
  판정한다 — 이번 유입 합이 직전 이탈 절대값을 넘으면 `매수 시그널(재유입)`,
  못 넘으면 `관망(하루 더)`. 앞에 이탈 run 이 없으면(처음부터 계속 유입)
  `매수 시그널(재유입)`.

## 데이터 부족

날짜 반전을 판정하려면 최소 2일치가 필요하다(예: `+100억 → 익일 -50억`은
2일짜리 예시지만 명확한 판정이 나온다). **1일 이하**는 반전 여부를 전혀
알 수 없으므로 `중립` — `days` 는 실제 표본 수를 정직하게 돌려준다(0 으로
위장하지 않는 원장 계약과 동일한 정신).
"""
from __future__ import annotations

LABEL_INFLOW = "매수 시그널(재유입)"
LABEL_OUTFLOW_TREND = "이탈 추세(부분 매도/중립 고려)"
LABEL_WATCH_RESIDUAL = "관망(잔여 있음)"
LABEL_WATCH_ANOTHER_DAY = "관망(하루 더)"
LABEL_NEUTRAL = "중립"


def _sign(v: float) -> int:
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def _runs(series: list[dict]) -> list[tuple[int, int, float]]:
    """[(부호, 길이, 구간합)] 시간순. 부호가 바뀔 때마다 새 구간을 연다."""
    runs: list[tuple[int, int, float]] = []
    for row in series:
        v = row.get("foreign_net") or 0
        s = _sign(v)
        if runs and runs[-1][0] == s:
            prev_s, prev_len, prev_sum = runs[-1]
            runs[-1] = (prev_s, prev_len + 1, prev_sum + v)
        else:
            runs.append((s, 1, v))
    return runs


def classify(series: list[dict]) -> dict:
    """`series`(date-asc [{date, foreign_net, inst_net}]) → 외국인 수급 라벨.

    반환: {"label", "residual"(창 내 순매수 합), "inst_follows"(최근 유입일에
    기관도 순매수했는지), "days"(실제 표본 수)}.
    """
    n = len(series)
    residual = sum(r.get("foreign_net") or 0 for r in series)

    last_day = series[-1] if series else None
    inst_follows = bool(
        last_day
        and (last_day.get("foreign_net") or 0) > 0
        and (last_day.get("inst_net") or 0) > 0
    )

    if n < 2:
        # 반전을 판정하려면 최소 2일치가 필요하다 — 이보다 짧으면 정직하게 모른다고 답한다.
        return {
            "label": LABEL_NEUTRAL,
            "residual": residual,
            "inst_follows": inst_follows,
            "days": n,
        }

    runs = _runs(series)
    last_sign, last_len, last_sum = runs[-1]
    prev = runs[-2] if len(runs) >= 2 else None

    if last_sign < 0:
        label = LABEL_OUTFLOW_TREND if last_len >= 2 else LABEL_WATCH_RESIDUAL
    elif last_sign > 0:
        if prev is not None and prev[0] < 0:
            prior_outflow = abs(prev[2])
            label = LABEL_INFLOW if last_sum > prior_outflow else LABEL_WATCH_ANOTHER_DAY
        else:
            # 이탈 없이 처음부터 계속 유입 — 재유입할 대상이 없으므로 순매수
            # 추세 자체를 매수 시그널로 본다.
            label = LABEL_INFLOW
    else:
        # 최근 구간이 순매수 0(보합) — 방향이 없으므로 중립.
        label = LABEL_NEUTRAL

    return {
        "label": label,
        "residual": residual,
        "inst_follows": inst_follows,
        "days": n,
    }
