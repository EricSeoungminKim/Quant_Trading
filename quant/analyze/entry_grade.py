"""진입 시점 5단계 결정론 등급(서브프로젝트 W part 3, 2026-08-17).

사용자 요구: 관심종목 추천마다 "지금 사도 되는지" 5단계(관망~적극 매수)를
붙인다. **LLM 금지** — 점수가 LLM에 의존하면 채점 루프(백테스트 재현, 회귀
테스트)가 무의미해진다. 순수 함수, 네트워크·파일 I/O 없음.

## 재사용 원칙

외국인 수급 반전 판정(연속 순매도/순매수 run 탐지)은 이미
`quant/analyze/foreign_trend.classify()`가 갖고 있다(사용자의 유입→일부이탈=
잔여관망 / 연속이탈=하락추세 / 재유입초과=매수시그널 시나리오 규칙을 그대로
구현한 것). 이 모듈은 그 판정을 5단계로 **매핑만** 한다 — 같은 판정 로직을 두
곳에 두지 않는다. 연속 streak 길이(등급 4/5의 "≥2일"/"≥3일" 임계값에 필요)는
`quant/analyze/foreign_flow_v2._trailing_run`을 그대로 가져다 쓴다(그 모듈이
이미 트레일링 run 길이·합계를 계산해 두었다).

## 등급 정의과 근거

1. **관망** — 악재 veto가 걸렸거나(최우선 차단), 외국인이 연속 ≥2일
   순매도 중(`foreign_trend.LABEL_OUTFLOW_TREND` — "지수·타 주체가 받쳐도
   하락 추세 가능성").
2. **기다림** — 데이터가 3일 미만(반전 판정에 최소 2일이 필요하다는
   `foreign_trend`의 원칙보다 한 단계 더 보수적으로, 3일 미만은 등급 산정을
   유보한다)이거나, 뚜렷한 유입 신호 없이 중립·단발 이탈(잔여 있음) 상태.
3. **관심** — 최근 순매수로 전환됐지만(유입 시작) 아직 분할 매수·적극 매수
   기준(연속 2일 이상 + 누적 정합)을 채우지 못한 상태.
4. **분할 매수** — 외국인이 연속 ≥2일 순매수 중이고, 1일·5일 누적 순매수가
   모두 양수(추세 정합 — `foreign_flow_v2`의 누적 정합 규칙과 같은 발상).
5. **적극 매수** — (외국인+기관 쌍끌이, 최근일) 또는 (외국인 연속 ≥3일
   순매수) 중 하나를 만족하고, **호재 마커가 1건 이상**(`bullish_hits >= 1`)
   있을 때만. 수급만으로는 5등급을 주지 않는다 — 뉴스 근거가 함께 있어야
   "적극"으로 올린다(사용자 원칙: 외국인+기관 쌍끌이가 최강 신호지만, 진입
   등급은 가격·수급뿐 아니라 재료도 봐야 한다).

`reasons`는 등급 산정에 실제로 쓰인 규칙만 담는다 — 트리거되지 않은 규칙은
언급하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from quant.analyze import foreign_trend
from quant.analyze.foreign_flow_v2 import _trailing_run

_LABELS: dict[int, str] = {
    1: "관망",
    2: "기다림",
    3: "관심",
    4: "분할 매수",
    5: "적극 매수",
}

# 등급 2(기다림)로 유보하는 최소 표본 일수 — foreign_trend의 반전 판정 최소치(2일)
# 보다 한 단계 보수적이다(모듈 docstring 참고).
_MIN_DAYS_FOR_GRADE = 3

# 등급 4(분할 매수) 임계값 — 외국인 연속 순매수 ≥2일.
_GRADE4_STREAK_DAYS = 2
# 등급 5(적극 매수) 임계값 — 외국인 연속 순매수 ≥3일.
_GRADE5_STREAK_DAYS = 3


@dataclass
class EntryGrade:
    """진입 등급 산정 결과. `grade` 1(관망)~5(적극 매수), `reasons`는 사람이
    읽는 근거 문자열 리스트(산정에 실제로 쓰인 규칙만)."""

    grade: int
    label: str
    reasons: list[str] = field(default_factory=list)


def _cumulative(rows: list[dict], window: int) -> float:
    return sum((r.get("foreign_net") or 0) for r in rows[-window:])


def entry_grade(
    foreign_rows: list[dict],
    bullish_hits: int = 0,
    bearish_veto: bool = False,
) -> EntryGrade:
    """진입 시점 5단계 등급. `foreign_rows`는 date-asc frgn_flow 원장 형식
    `[{date, symbol, foreign_net, inst_net}]` 최근 구간, `bullish_hits`는 호재
    마커 발견 건수, `bearish_veto`는 악재 veto 여부(True면 등급을 무조건
    1로 내린다 — 다른 신호를 보지 않는다)."""
    if bearish_veto:
        return EntryGrade(1, _LABELS[1], ["악재 veto"])

    days = len(foreign_rows)
    if days < _MIN_DAYS_FOR_GRADE:
        return EntryGrade(2, _LABELS[2], [f"데이터 부족(표본 {days}일 <{_MIN_DAYS_FOR_GRADE}일)"])

    classified = foreign_trend.classify(foreign_rows)
    label = classified["label"]

    if label == foreign_trend.LABEL_OUTFLOW_TREND:
        return EntryGrade(1, _LABELS[1], ["외국인 연속 순매도(이탈 추세)"])

    sign, streak, _streak_sum = _trailing_run(foreign_rows)
    sum1 = _cumulative(foreign_rows, 1)
    sum5 = _cumulative(foreign_rows, 5)

    last = foreign_rows[-1]
    tandem = (last.get("foreign_net") or 0) > 0 and (last.get("inst_net") or 0) > 0

    if bullish_hits >= 1 and (
        tandem or (sign > 0 and streak >= _GRADE5_STREAK_DAYS)
    ):
        reasons = [f"호재 마커 {bullish_hits}건"]
        if tandem:
            reasons.append("외국인+기관 쌍끌이(최근일)")
        if sign > 0 and streak >= _GRADE5_STREAK_DAYS:
            reasons.append(f"외국인 연속 순매수 {streak}일(≥{_GRADE5_STREAK_DAYS}일)")
        return EntryGrade(5, _LABELS[5], reasons)

    if sign > 0 and streak >= _GRADE4_STREAK_DAYS and sum1 > 0 and sum5 > 0:
        return EntryGrade(
            4,
            _LABELS[4],
            [
                f"외국인 연속 순매수 {streak}일(≥{_GRADE4_STREAK_DAYS}일)",
                "1일·5일 누적 순매수 양수",
            ],
        )

    if sign > 0:
        return EntryGrade(3, _LABELS[3], ["최근 순매수 전환(유입 시작)"])

    if label == foreign_trend.LABEL_WATCH_RESIDUAL:
        return EntryGrade(2, _LABELS[2], ["단발 이탈, 뚜렷한 유입 신호 없음"])

    return EntryGrade(2, _LABELS[2], ["뚜렷한 유입 신호 없음(중립)"])
