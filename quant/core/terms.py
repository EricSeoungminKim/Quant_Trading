"""지표·이벤트 이름을 한국 시장에서 통용되는 표기로 바꾼다.

FRED 는 릴리즈명을 정식 영문으로 준다("Consumer Price Index"). 그대로 실으면
읽는 사람이 매번 번역하며 읽어야 하고, 정작 한국에서 쓰는 말은 "CPI" 다.
**통용어를 앞에 두고 우리말 설명을 괄호로 붙인다** — 검색·대화에서 쓰는 말이
그대로 리포트에 있어야 한다.

여기 없는 릴리즈는 고영향으로 분류되지 않아 리포트 전면에 나오지 않는다
(캘린더 '그 외 N건' 으로만 집계된다).

**왜 core 인가.** 순수 상수·순수 함수(외부 의존 0)인데 `quant/analyze/`에
있었다. `quant/collect/sources/calendar.py`(캘린더 수집)도 `event_freq`가
필요한데 collect → analyze는 금지 의존이다(4평면 표) — 순수 로직이라 core로
승격하면 양쪽 다 쓸 수 있다.
"""
from __future__ import annotations

# FRED 릴리즈명 → (통용 표기, 한 줄 설명, 발표 주기)
RELEASE_TERMS: dict[str, tuple[str, str, str]] = {
    "Consumer Price Index": ("CPI / Core CPI", "소비자물가", "월간"),
    "Producer Price Index": ("PPI", "생산자물가", "월간"),
    "Employment Situation": ("고용보고서", "비농업 고용·실업률", "월간"),
    "Personal Income and Outlays": ("Core PCE", "개인소비지출 물가", "월간"),
    "Gross Domestic Product": ("GDP 성장률", "국내총생산", "분기"),
    "Advance Monthly Sales for Retail and Food Services": ("소매판매", "미국 소매·외식 매출", "월간"),
    "Unemployment Insurance Weekly Claims Report": ("신규실업수당청구", "주간 실업수당 청구건수", "주간 (목요일)"),
    "Advance Report on Durable Goods": ("내구재 주문", "내구재 신규수주", "월간"),
    "FOMC Meeting": ("FOMC", "연준 통화정책회의", "회의"),
}


def event_term(name: str) -> str:
    """리포트에 찍을 짧은 표기. 예: 'Consumer Price Index' → 'CPI'."""
    term = RELEASE_TERMS.get(name.strip())
    return term[0] if term else name


def event_desc(name: str) -> str:
    """통용 표기 옆에 붙일 설명. 매핑에 없으면 빈 문자열."""
    term = RELEASE_TERMS.get(name.strip())
    return term[1] if term else ""


def event_full(name: str) -> str:
    """'CPI (소비자물가)' 형태. 설명이 없으면 원문 그대로."""
    term = RELEASE_TERMS.get(name.strip())
    return f"{term[0]} ({term[1]})" if term else name


def event_freq(name: str) -> str:
    """발표 주기. 예: 'Consumer Price Index' → '월간'. 매핑에 없으면 빈 문자열."""
    term = RELEASE_TERMS.get(name.strip())
    return term[2] if term else ""
