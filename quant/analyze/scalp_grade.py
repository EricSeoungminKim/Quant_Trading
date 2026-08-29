"""당일 단타 후보 3단계 행동 등급 (2026-08-18, 사용자 지시).

`intraday_score.py`(v4)는 0~100 숫자만 낸다 — "그래서 지금 사도 되는지"가
바로 안 보인다는 사용자 피드백으로, 점수 위에 결정론 3단계 행동 등급을
얹는다: **단타 보류 / 단타 진입 / 단타 적극 진입**. 등급에도 못 미치는
후보는 **표시에서 뺀다**(사용자 원문: "3단계보다도 안 될 것 같은 건
보여주지도 말고") — `grade_scalp`가 `None`을 반환하면 호출부(`report_cli`)가
표시 목록에서 제외한다. 단, **원장 기록(선정 producer)은 건드리지 않는다**
— 숨김은 표시 계층의 일이지, 채점 연속성(intraday_verify 가 나중에 성적을
매길 표본)을 줄이는 일이 아니다.

`entry_grade.py`(중기 관심 종목 5단계)와 같은 원칙: 순수 함수, LLM 금지,
네트워크·파일 I/O 없음. 입력은 전부 호출부가 이미 계산해 둔 값을 받는다
(재수집 금지) — `bullish_markers`(호재 뉴스 축), `telegram_view`(언급),
`sector_map`/`naver_sector`(업종), `frgn_flow` 원장(외국인 수급).

## 등급 규칙 (초기값 — 튜닝 지점은 이 파일)

사용자가 준 예시 규칙을 그대로 코드화한다:

- **적극 진입**: 종목 호재 ≥1 **AND** (텔레그램 언급 ≥`TELEGRAM_MENTION_
  THRESHOLD` **OR** 섹터 호재 ≥`SECTOR_HOT_THRESHOLD`) **AND** 섹터 상승
  **AND** 외국인 순매수. 넷 다 있어야 한다 — 재료(종목+섹터/버즈)와
  수급(섹터 방향+외국인)이 동시에 맞아야 "적극"이다.
- **진입**: (종목 호재 **OR** (섹터 호재 ≥1 **AND** 섹터 상승)) **AND**
  외국인이 순매도는 아님(순매수 또는 데이터 없음 — "비매도"는 명시적
  매도만 배제한다, 모르는 것을 나쁘다고 위장하지 않는다는 이 저장소
  원장 규약과 같은 정신).
- **보류**: 위 둘 다 아니지만 신호가 하나라도 있음(종목 호재/텔레그램
  언급/섹터 호재/섹터 상승/외국인 순매수 중 하나).
- **미달(숨김)**: 그마저도 없음 — `None` 반환.

`sector_rising`/`foreign_net_buying`는 데이터가 없으면 `None`(모른다)을
받는다 — 0/False로 위장하지 않는다. `None`은 "적극 진입"의 필요조건은
채우지 못하지만(엄격), "진입"의 "비매도" 조건은 통과시킨다(관대) — 두
등급이 요구하는 확신 수준이 다르기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

GRADE_HOLD = "단타 보류"
GRADE_ENTER = "단타 진입"
GRADE_AGGRESSIVE = "단타 적극 진입"

TELEGRAM_MENTION_THRESHOLD = 2  # 텔레그램 언급 ≥2건 — 적극 진입의 대체 경로 중 하나
SECTOR_HOT_THRESHOLD = 3  # 섹터 호재 히트 ≥3건 — 적극 진입의 대체 경로 중 하나


@dataclass
class ScalpGrade:
    """`grade`: `GRADE_HOLD`/`GRADE_ENTER`/`GRADE_AGGRESSIVE` 중 하나.
    `reasons`: 등급 산정에 실제로 쓰인 근거만(트리거되지 않은 신호는 담지
    않는다 — `entry_grade.EntryGrade`와 같은 관례)."""

    grade: str
    reasons: list[str] = field(default_factory=list)


def grade_scalp(
    *,
    symbol_bullish: bool,
    telegram_mentions: int = 0,
    sector_bullish_hits: int = 0,
    sector_rising: bool | None = None,
    foreign_net_buying: bool | None = None,
) -> ScalpGrade | None:
    """당일 단타 후보 1종목의 행동 등급. 모듈 docstring "등급 규칙" 참고.

    `None`을 반환하면 미달(호출부가 표시에서 뺀다) — 점수로 위장하지 않는다.
    """
    telegram_hot = telegram_mentions >= TELEGRAM_MENTION_THRESHOLD
    sector_hot = sector_bullish_hits >= SECTOR_HOT_THRESHOLD
    sector_any = sector_bullish_hits >= 1

    if (
        symbol_bullish
        and (telegram_hot or sector_hot)
        and sector_rising is True
        and foreign_net_buying is True
    ):
        reasons = ["종목 호재 뉴스"]
        if telegram_hot:
            reasons.append(f"텔레그램 언급 {telegram_mentions}건(≥{TELEGRAM_MENTION_THRESHOLD})")
        if sector_hot:
            reasons.append(f"섹터 호재 {sector_bullish_hits}건(≥{SECTOR_HOT_THRESHOLD})")
        reasons.append("섹터 상승 중")
        reasons.append("외국인 순매수")
        return ScalpGrade(GRADE_AGGRESSIVE, reasons)

    if (symbol_bullish or (sector_any and sector_rising is True)) and foreign_net_buying is not False:
        reasons = []
        if symbol_bullish:
            reasons.append("종목 호재 뉴스")
        if sector_any and sector_rising is True:
            reasons.append(f"섹터 호재 {sector_bullish_hits}건 + 섹터 상승")
        reasons.append("외국인 순매수" if foreign_net_buying is True else "외국인 매도 없음(수급 데이터 부족 포함)")
        return ScalpGrade(GRADE_ENTER, reasons)

    # 미달(숨김) 게이트 — **양(+) 신호만** 센다. 섹터 하락/외국인 순매도 같은
    # 음(-) 신호는 그 자체로 "관심 근거"가 아니므로 이것만 있고 양 신호가
    # 하나도 없으면 보류조차 아니라 미달(None)이다 — 아래서 reasons 에는
    # 참고로 덧붙이되, 게이트 판정에는 넣지 않는다.
    signals: list[str] = []
    if symbol_bullish:
        signals.append("종목 호재 뉴스")
    if telegram_mentions:
        signals.append(f"텔레그램 언급 {telegram_mentions}건")
    if sector_bullish_hits:
        signals.append(f"섹터 호재 {sector_bullish_hits}건")
    if sector_rising is True:
        signals.append("섹터 상승 중")
    if foreign_net_buying is True:
        signals.append("외국인 순매수")
    if not signals:
        return None

    reasons = list(signals)
    if sector_rising is False:
        reasons.append("섹터 하락 중")
    if foreign_net_buying is False:
        reasons.append("외국인 순매도")
    return ScalpGrade(GRADE_HOLD, reasons)
