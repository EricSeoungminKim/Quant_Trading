"""호재(긍정 촉매) 마커 사전 + 분류기 — 서브프로젝트 P.

`quant/analyze/intraday_score.py`의 옛 뉴스 축("뉴스 모멘텀" 25 + "다매체" 15)은
"기사가 있다/여러 매체가 다뤘다"만 봤지 **그 기사가 호재인지**는 전혀 보지
않았다. `quant/backtest/catalyst_study.py`(서브프로젝트 M, `docs/plans/
촉매-연구-2026-08-17.md`)가 DART 공시 유형별 익일 수익률을 base rate 대비로
실측했고, 그 결과가 이 사전의 티어를 정한다:

- **상위 티어(`BULLISH_TIER_VALIDATED`)** — M 연구가 n≥30, base 대비 +0.5%p
  이상으로 실측 검증한 유형만: `수주/공급계약`(n=370, base 대비 +0.62%p),
  `자사주`(n=114, +0.83%p — 이번 창에서 가장 강한 신호).
- **하위 티어(`BULLISH_TIER_UNVALIDATED`)** — 나머지 전부. `흑자전환/최대실적`은
  M 연구의 `실적`(n=1,069, +0.48%p)에 대응하는데 문턱(+0.5%p)에 0.02%p
  모자라 "이 창에서는 base와 구분 안 됨"이었다 — 하향 배치. `FDA/품목허가/
  임상성공`은 M 연구의 `임상·승인`(n=26, +0.53%p — 방향은 긍정이지만 표본
  미달)에 대응, 판단 보류. `신제품/양산`·`특허`·`MOU/협력`은 M 연구가 아예
  다루지 않은 유형(DART 공시 분류 어휘에 없음) — 미실측이라 하위 티어가
  기본값이다. `무상증자`는 M 연구에서 n=21(표본 미달)에 평균 -2.97%로 오히려
  나빴지만(`docs/plans/촉매-연구-2026-08-17.md` Part A), 뉴스 제목에서는
  "무상증자 결정"이 시장에서 통상 호재로 읽히는 별개 사건(권리락 전
  단기 랠리)이라 사전에는 남기되 표본 미달로 하위 티어에 둔다 — 이 모듈의
  `quant/backtest/bullish_accuracy.py` 실측이 이 판단을 다시 검증한다.

## 재확인 실측 (P Part 2, `bullish_accuracy.py`, 2026-08-17 16시경 실행)

이 사전으로 직접 태깅해 다시 돌린 결과(`data/ledger/bullish_accuracy.jsonl`,
disclosures n=22,241 · 유효표본 9,310 · 봉 캐시 289종목):

| 유형 | n | 평균 익일% | base 대비 |
|---|---:|---:|---:|
| 자사주 | 64 | +0.00% | **+0.84%p** — 긍정 후보로 재확인(오히려 M 원 연구보다 강함) |
| 수주/공급계약 | 306 | -0.45% | +0.39%p — 방향은 여전히 긍정이나 문턱(+0.5%p) 미달 |
| 무상증자 | 15 | -3.83% | 표본 미달(n<30), 여전히 나쁨 |
| FDA/품목허가/임상성공 | 4 | +0.77% | 표본 미달(n<30) |

**`수주/공급계약`을 하향하지 않는다** — 방향은 이번에도 긍정이고(base보다
낫다), M 원 연구(같은 방법론, 더 넓은 60일 창, n=370)에서는 +0.62%p로 문턱을
넘었었다. 이번 재확인 창은 그보다 좁고(n=306, 종목 cap 300 재적용으로 봉 캐시
289종목만 확보) 시장 국면도 살짝 다르다 — "이 좁은 재확인 창에서는 마진이
얇아졌다"이지 "사전이 틀렸다"가 아니다. 표본이 계속 쌓이는 대로
`bullish_accuracy.py`를 다시 돌려 마진이 계속 얇으면 그때 재검토한다(M 연구의
"다른 국면에서 최소 1회 더 재현하기 전엔 확정 채택하지 않는다" 규율과 동일).
`자사주`는 이번에도(오히려 더 강하게) 확인돼 상위 티어를 유지한다.

Part B(뉴스 창, `mentions.jsonl`)는 이 실행에서도 n=0이었다 — `catalyst_study.py`
Part C와 같은 이유(크롤일이 아직 하루치뿐이고 그 거래일의 yfinance 확정 봉이
실행 시점에 없었다). 사전 조정 판단 재료가 아니다, 데이터가 더 쌓이면 채워진다.

## 왜 감성 분석이 아니라 사전 매칭인가

`quant/analyze/news_direction.py`(악재 거부권)와 같은 철학이다 — 애매한 어휘로
확대 해석하지 않는다. 키워드가 실제로 있으면 유형을 매기고, 없으면 아무 것도
주장하지 않는다.

## 악재 거부권과의 관계 — 비대칭은 그대로 유지한다

`news_direction.scan()`이 이미 "명백한 악재 표지 하나면 거부"라는 좁고 정밀한
규칙을 갖고 있다(대신증권 실측 사례 — 호재 기사 여섯 건보다 목표가 하향 한
건이 무겁다). 이 모듈은 그 판정을 **재구현하지 않고 그대로 재사용한다** —
호재 사전과 악재 거부권이 따로 놀면 "호재 마커가 있지만 사실은 악재"인 제목을
호재로 채점하는 사고가 난다.
"""
from __future__ import annotations

from quant.analyze.news_direction import scan as bearish_scan

# ------------------------------------------------------------------ 티어
# 상위 티어 = catalyst_study.py(M 연구)가 n≥30·base 대비 +0.5%p 이상으로 실측
# 검증한 유형. 하위 티어 = 미실측이거나 표본/마진이 문턱에 못 미친 유형(위
# 모듈 docstring에 유형별 근거).
BULLISH_TIER_VALIDATED = 2
BULLISH_TIER_UNVALIDATED = 1

BULLISH_TYPE_TIERS: dict[str, int] = {
    "수주/공급계약": BULLISH_TIER_VALIDATED,
    "자사주": BULLISH_TIER_VALIDATED,
    "흑자전환/최대실적": BULLISH_TIER_UNVALIDATED,
    "FDA/품목허가/임상성공": BULLISH_TIER_UNVALIDATED,
    "신제품/양산": BULLISH_TIER_UNVALIDATED,
    "특허": BULLISH_TIER_UNVALIDATED,
    "무상증자": BULLISH_TIER_UNVALIDATED,
    "MOU/협력": BULLISH_TIER_UNVALIDATED,
    # 2026-08-18 추가분(위 BULLISH_MARKERS 절 참고) — 전부 미실측이라 하위 티어.
    "목표가 상향": BULLISH_TIER_UNVALIDATED,
    "외국인 순매수 급증": BULLISH_TIER_UNVALIDATED,
}

# ------------------------------------------------------------------ 사전
# keyword → type. dart.py의 REPORT_TYPE_KEYWORDS와 같은 관례 — 순서가 매칭
# 우선순위(같은 유형이 이미 잡혔으면 그 유형의 다른 키워드는 더 보지 않는다,
# classify_titles 참고). 새 유형은 이 dict + BULLISH_TYPE_TIERS 에 키워드만
# 추가하면 된다(단일 튜닝 지점).
BULLISH_MARKERS: dict[str, str] = {
    "수주": "수주/공급계약",
    "공급계약": "수주/공급계약",
    "자사주 매입": "자사주",
    "자사주매입": "자사주",
    "자기주식 취득": "자사주",
    "자기주식취득": "자사주",
    "흑자전환": "흑자전환/최대실적",
    "최대실적": "흑자전환/최대실적",
    "역대 최대": "흑자전환/최대실적",
    # 2026-08-18(SK하이닉스 15점 사건 재점검) 추가: M 연구의 `실적`(n=1,069,
    # +0.48%p, 하위 티어)에 대응하는 흔한 헤드라인 표현인데 옛 사전에 "흑자전환/
    # 최대실적/역대 최대" 세 키워드뿐이라 "호실적"류 헤드라인을 놓쳤다(실측:
    # "삼성전자·SK하이닉스, 호실적에 상반기 현금 117조 증가"가 하나도 안 걸림).
    "호실적": "흑자전환/최대실적",
    "실적 개선": "흑자전환/최대실적",
    "실적 상향": "흑자전환/최대실적",
    "FDA": "FDA/품목허가/임상성공",
    "품목허가": "FDA/품목허가/임상성공",
    "임상 성공": "FDA/품목허가/임상성공",
    "임상시험 성공": "FDA/품목허가/임상성공",
    "신제품": "신제품/양산",
    "양산": "신제품/양산",
    # HBM(고대역폭메모리) 공급/증설 뉴스 — M 연구가 다루지 않은 유형(DART 공시
    # 분류 어휘에 없다)이라 미실측 하위 티어가 기본값이다(다른 미실측 유형과
    # 같은 원칙). 반도체 업종 리포트에서 실제로 반복 등장하는 촉매라 2026-08-18
    # 재점검에서 추가.
    "HBM": "신제품/양산",
    "특허": "특허",
    "무상증자": "무상증자",
    "MOU": "MOU/협력",
    "업무협약": "MOU/협력",
    # 2026-08-18 추가 — M 연구가 다루지 않은 시장 이벤트형 유형(애널리스트
    # 리포트/수급 뉴스). DART 공시가 아니라 미실측 하위 티어가 기본값이다.
    "목표가 상향": "목표가 상향",
    "목표주가 상향": "목표가 상향",
    "외국인 순매수": "외국인 순매수 급증",
}


def classify_titles(titles: list[str]) -> dict:
    """제목 목록(오늘치, 종목 하나) → 호재 유형/티어/악재 여부.

    반환: `{"bullish_types": [발견된 유형들, 우선순위 순], "tier":
    max(발견된 유형들의 티어), "bearish": bool}`. 호재 유형이 하나도 없으면
    `tier`는 0(어느 티어에도 속하지 않는다는 뜻 — 0점으로 위장하는 게
    아니라 "발견된 게 없다"의 정직한 표기, `intraday_score.py`의 None 계약과
    같은 정신).

    `bearish`는 `news_direction.scan()`(기존 악재 거부권 메커니즘)을 그대로
    재사용한다 — 새로 만들지 않는다(위 모듈 docstring).
    """
    types_found: list[str] = []
    for keyword, btype in BULLISH_MARKERS.items():
        if btype in types_found:
            continue
        if any(keyword in (t or "") for t in titles or []):
            types_found.append(btype)
    tier = max((BULLISH_TYPE_TIERS[t] for t in types_found), default=0)
    return {
        "bullish_types": types_found,
        "tier": tier,
        "bearish": bool(bearish_scan(titles)),
    }


# ------------------------------------------------------------------ 감쇠 가중 판정 (2026-08-18)
# 배경(SK하이닉스 15점 사건 재점검): `classify_titles`는 "오늘치 제목만"이라는
# 전제로 호출돼 왔다 — `mentions.continuity()`의 `titles` 필드가 원장에서
# `date == 오늘` 행만 골라 만들기 때문이다. 그래서 직전 개장일(주말 포함)에
# 이미 터진 호재가 다음 채점에서 "호재 마커 없음"으로 잡혔다. 재료의
# 신선도(오늘 기사가 더 강한 신호)와 전일·주말 축적(하루 이틀 전 기사도
# 완전히 버리면 안 된다)을 절충해, `mentions.continuity()`가 만드는
# `recent_titles`(최근 3개장일, `{title, weight}` — 원장에 실제로 존재하는
# 날짜만 세므로 휴장일은 자동으로 빠진다, `mentions._recent_session_dates`
# docstring 참고)를 감쇠 가중으로 판정한다.
def classify_titles_dated(dated_titles: list[dict]) -> dict:
    """`classify_titles`의 감쇠 가중 버전. `dated_titles`는
    `mentions.continuity()`가 만드는 `recent_titles` — `[{"title", "weight",
    ...}, ...]`(weight: 당일 1.0/전일 0.6/전전일 0.3, `mentions.
    RECENT_TITLES_DECAY`). 반환값은 `classify_titles`와 같은 키 +
    `tier_weight`(발견된 마커 중 가장 신선한(=가중치가 큰) 기사의 가중치 —
    마커가 없으면 1.0, `news_axis_v2`의 곱셈 항등원이라 무해하다).

    tier/bullish_types/bearish 판정 자체는 `classify_titles`를 그대로
    재사용한다(제목 전체의 합집합을 본다 — 재구현하지 않는다). 이 함수는
    "그 판정에 쓰인 제목이 얼마나 신선했는가"만 추가로 계산한다."""
    titles = [d.get("title", "") for d in dated_titles or []]
    bull = classify_titles(titles)
    if not bull["bullish_types"]:
        bull["tier_weight"] = 1.0
        return bull
    matched_keywords = {
        kw for kw, btype in BULLISH_MARKERS.items() if btype in bull["bullish_types"]
    }
    weights = [
        d.get("weight", 1.0) for d in dated_titles or []
        if any(kw in (d.get("title") or "") for kw in matched_keywords)
    ]
    bull["tier_weight"] = max(weights) if weights else 1.0
    return bull


# ------------------------------------------------------------------ 뉴스 축 v2 (0~40)
BULLISH_TIER_TOP_POINTS = 15
BULLISH_TIER_LOW_POINTS = 8
DUP_HIGH_THRESHOLD = 3
DUP_HIGH_POINTS = 10
DUP_MED_THRESHOLD = 2
DUP_MED_POINTS = 5
NEWS_Z_THRESHOLD = 2.0
NEWS_Z_POINTS = 8
ARTICLES_THRESHOLD = 3
ARTICLES_POINTS = 7
NEWS_AXIS_MAX = (
    BULLISH_TIER_TOP_POINTS + DUP_HIGH_POINTS + NEWS_Z_POINTS + ARTICLES_POINTS
)  # 40 — 상위 티어 + 다매체 고 + news_z + 기사 건수가 전부 켜졌을 때
assert NEWS_AXIS_MAX == 40  # noqa: S101 — 배점 설계가 어긋나면 임포트 시점에 바로 드러난다

# 악재 마커가 하나라도 있으면 총점의 이만큼을 깎는다(candidates_line 철학 —
# 호재 여섯 건보다 목표가 하향 한 건이 무겁다. 완전히 0으로 죽이지 않는 이유는
# 다매체·news_z 같은 다른 근거까지 통째로 지우면 "왜 이 종목이 후보에도
# 안 올랐는지"가 안 보이기 때문 — 40% 만 남겨 랭킹에서는 사실상 밀려나되
# evidence 는 남는다).
BEARISH_VETO_DISCOUNT = 0.6


def news_axis_v2(
    today_articles: int, news_z: float | None, max_dup_count: int, bull: dict,
    tier_weight: float = 1.0,
) -> tuple[int, str]:
    """단타 스코어러의 호재 뉴스 축(0~`NEWS_AXIS_MAX`). `bull`은 `classify_titles()`
    결과. 반환 `(pts, evidence)` — evidence는 사람이 읽는 한 줄 문자열.

    `tier_weight`(기본 1.0, 하위 호환): `classify_titles_dated`가 매긴
    마커 발견 시점의 신선도 가중(당일 1.0/전일 0.6/전전일 0.3, 위 "감쇠 가중"
    절). 호재 티어 점수에만 곱한다 — 다매체/news_z/기사량은 이미 "오늘"만
    보는 별개 축이라 건드리지 않는다."""
    pts = 0
    notes: list[str] = []

    tier = bull.get("tier", 0)
    bullish_types = bull.get("bullish_types") or []
    if tier == BULLISH_TIER_VALIDATED:
        add = round(BULLISH_TIER_TOP_POINTS * tier_weight)
        pts += add
        weight_note = f", 최근성 가중 {tier_weight:.1f}" if tier_weight < 1.0 else ""
        notes.append(f"호재: {', '.join(bullish_types)} (+{add}{weight_note})")
    elif tier == BULLISH_TIER_UNVALIDATED:
        add = round(BULLISH_TIER_LOW_POINTS * tier_weight)
        pts += add
        weight_note = f", 최근성 가중 {tier_weight:.1f}" if tier_weight < 1.0 else ""
        notes.append(f"호재(미실측 유형): {', '.join(bullish_types)} (+{add}{weight_note})")
    else:
        notes.append("호재 마커 없음")

    if max_dup_count >= DUP_HIGH_THRESHOLD:
        pts += DUP_HIGH_POINTS
        notes.append(f"다매체 {max_dup_count}건≥{DUP_HIGH_THRESHOLD} (+{DUP_HIGH_POINTS})")
    elif max_dup_count == DUP_MED_THRESHOLD:
        pts += DUP_MED_POINTS
        notes.append(f"다매체 {max_dup_count}건 (+{DUP_MED_POINTS})")
    else:
        notes.append(f"다매체 {max_dup_count}건 — 단일 매체 수준")

    if news_z is None:
        notes.append("news_z 표본 부족 — 0 위장 금지")
    elif news_z >= NEWS_Z_THRESHOLD:
        pts += NEWS_Z_POINTS
        notes.append(f"news_z {news_z:.2f}≥{NEWS_Z_THRESHOLD:.0f} (+{NEWS_Z_POINTS})")
    else:
        notes.append(f"news_z {news_z:.2f}<{NEWS_Z_THRESHOLD:.0f}")

    if today_articles >= ARTICLES_THRESHOLD:
        pts += ARTICLES_POINTS
        notes.append(f"오늘 기사 {today_articles}건≥{ARTICLES_THRESHOLD} (+{ARTICLES_POINTS})")
    else:
        notes.append(f"오늘 기사 {today_articles}건<{ARTICLES_THRESHOLD}")

    if bull.get("bearish"):
        pre_veto = pts
        pts = round(pts * (1 - BEARISH_VETO_DISCOUNT))
        notes.append(f"악재 마커 존재 — 총점 {int(BEARISH_VETO_DISCOUNT * 100)}% 감점({pre_veto}→{pts})")

    return pts, "; ".join(notes)
