"""당일 단타(day-trading) 종목 스코어러 — 서브프로젝트 K.

**사용자 요구 원문 요지(2026-08-17)**: "국장 대표 뉴스 매체 기반 뉴스 스크레이핑
+ 외국인/기관 추세로 당일 단타 종목을 점수화해 따로 보여주고, 정확도는 과거
뉴스로 재현한 선정이 실제 그날 가격·거래량이 올랐는지로 검증해 계속 확신도
있게." 이 모듈은 앞부분(점수화)만 맡는다 — 검증은
`quant/backtest/intraday_verify.py`.

**자동매매 2원칙**(`docs/superpowers/specs/2026-08-17-foreign-flow-report-design.md`):
승률이 아니라 손익비, 감정 배제·수치만. 이 스코어러는 진입 여부를 결정하지
않는다(순수 함수, `quant/trade/` 미접촉) — 오늘 후보를 사람이 볼 수 있게 순위를
매길 뿐이다.

**메인 로직은 외국인 추세다** — 위 스펙의 사용자 원칙 1("한국주식은 기관
매수세는 노이즈 — 메인 리서치와 비리 없는 매수는 오로지 외국인 매수 추세")을
그대로 반영해 외국인 추세 요인에 가장 큰 배점(30/100)을 준다.

## 2026-08-17 — 외국인 축을 v2(실무 규칙)로 교체

`foreign_trend.classify()`의 재유입/이탈 5라벨 모델 대신
`quant.analyze.foreign_flow_v2.foreign_score_v2()`(쌍끌이·연속 순매수일수·
1·5·20일 추세 정합·강도·이탈 5개 실무 규칙 합산, 서브프로젝트 O)로 교체했다.
근거: `quant/backtest/report_replay.py`의 A/B/C 실측
(`docs/plans/리포트-재구성-백테스트-2026-08-17.md` 8~9절) — 라벨 기반(변형 A)
대비 v2(변형 C, 강도를 이중 계산하지 않는 조합)가 같은 30거래일 재구성 창에서
movers precision(26.5% vs 24.0%)과 방향성 적중률(55.0% vs 51.3%) 둘 다 더
좋았다. 100점 배점 자체는 안 바뀐다 — "외국인 추세" 축의 예산(30점)은 그대로,
그 30점을 채우는 **정의**만 라벨→점수 선형 스케일(`FOREIGN_TREND_MAX *
foreign_v2_score / FOREIGN_V2_MAX`)로 바뀌었다. `FOREIGN_LABEL_POINTS`는 이
모듈이 더 이상 쓰지 않지만 지우지 않는다 — `report_replay.py`의 변형 A가
"라이브에서 라벨 기반이었다면"의 베이스라인 재현에 그대로 임포트해 쓴다.

## 2026-08-17 — 뉴스 축을 v3(호재 탐지)로 교체 (서브프로젝트 P)

옛 "뉴스 모멘텀"(25) + "다매체 사건"(15) 두 축은 "기사가 있다/여러 매체가
다뤘다"만 봤지 **그 기사가 호재인지**는 전혀 보지 않았다 — 악재 기사가 3건
있어도 "뉴스 모멘텀 25점 만점"이 나올 수 있었다. `quant/backtest/
catalyst_study.py`(서브프로젝트 M, `docs/plans/촉매-연구-2026-08-17.md`)가
DART 공시 유형별 익일 수익률을 base rate 대비로 실측했고(수주 +0.62%p,
자사주 +0.83%p, 나머지는 이 창에서 구분 안 됨), 그 결과로 만든
`quant.analyze.bullish_markers`의 호재 마커 사전 + 악재 거부권 재사용
(`news_direction.scan`)이 두 축(합 40점)을 대체한다 — 예산은 그대로(25+15=40),
**정의**만 "기사량"에서 "호재 여부 + 기사량"으로 바뀌었다. 촉매 축(공시
화이트리스트)도 같은 M 연구 근거로 수주/자사주를 상위 가점, 실적을 하향했다
(아래 "촉매(15)" 절).

## 2026-08-17 — 텔레그램 시그널 축 신설, v4 (서브프로젝트 S part 2)

텔레그램 공개 채널(`quant.collect.sources.telegram_channels`, 12방 — 섹터/
매크로/뉴스 3개 tier)에서 종목이 언급됐는지를 새 축(`telegram_mentions`,
`quant.analyze.telegram_view`)으로 채점에 반영한다. 예산 8점을 새로 만들며,
기존 4축(합 100)에서 비례 축소해 총합을 100으로 유지한다 — "요인 설계는
초기 가중치다"(아래 절) 원칙 그대로, 텔레그램 신호가 실제로 유효한지는
검증 하네스가 나중에 실측한다.

배점 변화(구→신, 예산만 — 산식 자체는 그대로):
- 호재 뉴스 40 → 38 (`news_axis_v2` 원점수를 38/40 로 선형 축소)
- 외국인 추세 30 → 28
- 트렌딩 15 → 13 (고득점 15→13, 중간 8→7)
- 촉매 15 → 13 (공시 상위 티어 10→9, 표본부족 티어 5→4, 테마 등락 5→4)
- 텔레그램 시그널(신설) 0 → 8 (섹터방 언급 +8, 매크로/뉴스방 언급 +4)

38+28+13+13+8 = 100. `_INTRADAY_PRODUCER`/`_INTRADAY_PRODUCER_CLOSE`
(report_cli.py)도 `"intraday_scorer_v3"` → `"_v4"`로 같이 올라간다 — 팩터
구성이 바뀌었는데 리더보드가 v3/v4를 섞어 집계하면 조용히 틀린다(위
"2026-08-17" 절들과 같은 관례).

## 요인 설계는 초기 가중치다

아래 배점은 지어낸 확정치가 아니라 **검증 하네스(intraday_verify)가 조정
근거를 만든다는 전제 하의 초기값**이다 — 과거 재현 선정이 실제로 가격/거래량이
올랐는지를 실측해야 가중치를 고칠 근거가 생긴다.

## None 을 0으로 위장하지 않는다

`news_z`·`foreign_v2_score`·`trending_score100`·`theme_change_pct` 4개 입력은
데이터가 없으면 `None`이 온다. 이 저장소 전체의 원장 규약과 동일하게, "모른다"를
"0점(=나쁨)"으로 채우지 않는다 — evidence 문자열에 "표본 부족"/"수급 시계열
없음" 등으로 정직하게 남긴다. 이 4개 중 **3개 이상이 None**이면 근거 자체가
너무 얇아 점수를 매기는 게 오히려 과신을 만든다 — `score_intraday`는 `None`을
반환한다(데이터 부족을 점수로 위장하지 않는다). `foreign_v2_score` 자체는
`foreign_score_v2()`가 항상 int(하한 0)를 돌려주므로, 호출부가 수급 시계열이
아예 없을 때만 명시적으로 `None`을 넣어야 이 계약이 유지된다(호출부
`report_cli._build_intraday_view`/`intraday_verify.reconstruct_inputs`가
둘 다 이 규율을 따른다).
"""
from __future__ import annotations

from quant.analyze.bullish_markers import (
    NEWS_AXIS_MAX,
    classify_titles,
    classify_titles_dated,
    news_axis_v2,
)
from quant.analyze.foreign_flow_v2 import FOREIGN_V2_MAX

# `LABEL_*` 임포트는 이 모듈 자신은 더 이상 채점에 쓰지 않지만
# `FOREIGN_LABEL_POINTS`(아래) 정의에 남아 있다 — 그 상수의 존재 이유는
# 위 "2026-08-17" 절 참고.
from quant.analyze.foreign_trend import (
    LABEL_INFLOW,
    LABEL_NEUTRAL,
    LABEL_OUTFLOW_TREND,
    LABEL_WATCH_ANOTHER_DAY,
    LABEL_WATCH_RESIDUAL,
)

# ------------------------------------------------------------------ 호재 뉴스 (v3 예산 40 → v4 38, 위 "2026-08-17" 절)
# 산식 자체는 `quant.analyze.bullish_markers.news_axis_v2`(호재 마커 티어 +
# 다매체 + news_z + 기사량, 악재 거부권 60% 감점)에 있다 — 그 모듈의 내부
# 배점(15+10+8+7=`NEWS_AXIS_MAX`)은 건드리지 않고, 이 축의 **예산**만
# `NEWS_AXIS_BUDGET`으로 선형 축소한다(`_news_axis_factor`가 스케일한다) —
# `bullish_markers.py`를 고치면 그 모듈을 쓰는 다른 소비자(있다면)까지 흔든다.
NEWS_AXIS_BUDGET = 38

# ------------------------------------------------------------------ 외국인 추세 (v3 예산 30 → v4 28, 메인)
# 2026-08-17: 이 축의 정의를 라벨(`foreign_trend.classify`) 기반에서
# `foreign_score_v2`(쌍끌이·연속일·정합·강도·이탈, quant/analyze/foreign_flow_v2.py)
# 기반으로 교체했다(위 "2026-08-17" 절 참고). 예산은 v4 에서 30→28로 줄었다
# (텔레그램 시그널 축 신설에 맞춘 비례 축소, 위 "2026-08-17 — 텔레그램" 절).
FOREIGN_TREND_MAX = 28
# `report_replay.py`의 변형 A(라이브 라벨 기반이었다면의 베이스라인 재현)가
# 여전히 이 매핑을 쓴다 — 이 모듈 자신의 채점 경로에서는 더 이상 참조하지
# 않지만, 그 이유로 지우지 않는다(모듈 docstring "2026-08-17" 절). 옛 v1
# 배점(30점 만점) 그대로 둔다 — 재현 베이스라인은 당시 정의를 보존해야 한다.
FOREIGN_LABEL_POINTS: dict[str, int] = {
    LABEL_INFLOW: 30,
    LABEL_WATCH_RESIDUAL: 15,
    LABEL_WATCH_ANOTHER_DAY: 8,
    LABEL_OUTFLOW_TREND: 0,
    LABEL_NEUTRAL: 0,
}

# ------------------------------------------------------------------ 트렌딩 (v3 예산 15 → v4 13)
TRENDING_HIGH_THRESHOLD = 70
TRENDING_HIGH_POINTS = 13
TRENDING_MED_THRESHOLD = 55
TRENDING_MED_POINTS = 7
TRENDING_MAX = 13

# ------------------------------------------------------------------ 촉매 (v3 예산 15 → v4 13)
# 2026-08-17 (서브프로젝트 P): `catalyst_study.py`(M 연구) 실측 근거로 화이트리스트를
# 두 티어로 나눴다 — n≥30·base 대비 +0.5%p 이상으로 실측 검증된 유형(`수주`
# n=370 +0.62%p, `자사주` n=114 +0.83%p)만 상위 가점을 받는다. `실적`은
# n=1,069(표본 충분)인데 +0.48%p로 문턱에 0.02%p 모자라 "이 창에서는 base와
# 구분 안 됨"이라 화이트리스트에서 뺐다(하향) — `docs/plans/
# 촉매-연구-2026-08-17.md` 제안 3. `임상·승인`은 n=26(표본 미달)이지만 방향이
# 긍정(+0.53%p)이라 판단 보류로 낮은 티어에 남긴다(제안 4 — 빼지 않고 표본이
# 쌓이길 기다린다). 같은 날(서브프로젝트 S part 2) 텔레그램 축 신설로 예산이
# 15→13으로 줄면서 상위/표본부족/테마 세 값도 비례 축소했다(10→9, 5→4, 5→4).
CATALYST_DART_TOP_TYPES = frozenset({"수주", "자사주"})  # 실측 유효
CATALYST_DART_TOP_POINTS = 9
CATALYST_DART_WATCH_TYPES = frozenset({"임상·승인"})  # 표본 부족·판단 보류
CATALYST_DART_WATCH_POINTS = 4
CATALYST_THEME_THRESHOLD_PCT = 3.0  # 테마 등락 +3%p 이상(상승만 — 롱 온리)
CATALYST_THEME_POINTS = 4
CATALYST_MAX = CATALYST_DART_TOP_POINTS + CATALYST_THEME_POINTS  # 13

# 이 모듈 자신의 촉매 채점에는 더 이상 쓰이지 않지만(위 실측으로 TOP/WATCH
# 두 티어로 교체) `report_replay.py`의 재구성 변형이 "촉매 화이트리스트가
# (실측 반영 전) 이랬다면"의 과거 정의를 그대로 참조한다 — `FOREIGN_LABEL_
# POINTS`(위 "2026-08-17" 절)와 같은 관례로 지우지 않는다.
CATALYST_DART_TYPES = frozenset({"수주", "임상·승인", "실적"})

# ------------------------------------------------------------------ 텔레그램 시그널 (v4 신설, 8 — 위 "2026-08-17 — 텔레그램" 절)
# 입력은 `quant.analyze.telegram_view.telegram_mentions()`의 출력 한 종목분
# (`{"channels": [...], "sector_room": bool}` | None). 섹터방(tier=sector)
# 언급이 매크로/뉴스방 언급보다 강한 신호라고 본다(2026-08-17 사용자 지시) —
# 전력·바이오처럼 좁은 주제방의 언급은 우연히 스쳐가는 매크로/뉴스 언급보다
# 종목 특정성이 높다.
TELEGRAM_SECTOR_POINTS = 8
TELEGRAM_OTHER_POINTS = 4
TELEGRAM_MAX = TELEGRAM_SECTOR_POINTS

SCORE_MAX = (
    NEWS_AXIS_BUDGET + FOREIGN_TREND_MAX + TRENDING_MAX + CATALYST_MAX + TELEGRAM_MAX
)
assert SCORE_MAX == 100  # noqa: S101 — 배점 설계가 어긋나면 임포트 시점에 바로 드러난다

# None 이 몇 개 이상이면 근거 부족으로 채점 자체를 포기하는지.
MIN_NULLABLE_FOR_NONE_RESULT = 3
_NULLABLE_KEYS = ("news_z", "foreign_v2_score", "trending_score100", "theme_change_pct")


def _foreign_strength(foreign_v2_score: int | None) -> int:
    """랭킹 tie-break 전용 강도. `foreign_v2_score` 자체가 이미 신호 강도라
    (0~`FOREIGN_V2_MAX`) 라벨 순위표가 더 필요 없다 — 그대로 쓴다. None(수급
    시계열 없음)은 0점보다도 약한 신호로 최하위 취급."""
    return foreign_v2_score if foreign_v2_score is not None else -1


def _news_axis_factor(
    today_articles: int, news_z: float | None, max_dup_count: int, titles: list[str],
    recent_titles: list[dict] | None = None,
) -> tuple[str, int, int, str]:
    """호재 뉴스 축(v3 산식, v4 예산) — `bullish_markers.classify_titles` +
    `news_axis_v2`가 0~`NEWS_AXIS_MAX`(40)로 채점한 원점수를 이 축의 예산
    (`NEWS_AXIS_BUDGET`, 38)으로 선형 축소한다(위 "2026-08-17 — 텔레그램" 절).
    `titles`가 비어 있으면(과거 재구성 등 제목을 복원할 수 없는 호출부) 호재
    마커는 "없음"으로 정직하게 채점된다 — 0으로 위장하는 게 아니라 실제로
    마커가 발견되지 않았다는 뜻이다.

    `recent_titles`(2026-08-18, SK하이닉스 15점 사건 재점검 — `mentions.
    continuity()`의 `recent_titles`, `[{"title","weight"}, ...]`)가 있으면
    `classify_titles_dated`로 최근 3개장일(당일 1.0/전일 0.6/전전일 0.3)을
    감쇠 가중으로 본다. 없으면(과거 재구성 등 하루 단위 가중 데이터가 없는
    호출부) `titles`만으로 `classify_titles`를 쓰는 옛 동작 그대로
    (`tier_weight=1.0`, 하위 호환)."""
    if recent_titles:
        bull = classify_titles_dated(recent_titles)
        tier_weight = bull.get("tier_weight", 1.0)
    else:
        bull = classify_titles(titles)
        tier_weight = 1.0
    raw_pts, evidence = news_axis_v2(
        today_articles, news_z, max_dup_count, bull, tier_weight=tier_weight,
    )
    pts = round(NEWS_AXIS_BUDGET * raw_pts / NEWS_AXIS_MAX)
    return "호재 뉴스", pts, NEWS_AXIS_BUDGET, evidence


def _telegram_factor(mention: dict | None) -> tuple[str, int, int, str]:
    """텔레그램 시그널 축(v4 신설) — `telegram_mentions()`이 종목별로 만든
    `{"channels": [...], "sector_room": bool}`을 채점한다. `channels`는
    `[{"handle", "분류", "tier"}, ...]`. 언급이 없으면(`None`/빈 리스트) 0점 —
    다른 nullable 4개 입력과 달리 "모른다"가 아니라 "언급이 실제로 없다"는
    뜻이라 `_NULLABLE_KEYS`에 넣지 않는다(catalyst 의 dart_types 와 같은 결)."""
    channels = (mention or {}).get("channels") or []
    if not channels:
        return "텔레그램 시그널", 0, TELEGRAM_MAX, "텔레그램 언급 없음"

    sector_channels = [c for c in channels if c.get("tier") == "sector"]
    if sector_channels:
        c = sector_channels[0]
        pts = TELEGRAM_SECTOR_POINTS
    else:
        c = channels[0]
        pts = TELEGRAM_OTHER_POINTS
    evidence = f"텔레그램: {c['분류']} 채널({c['handle']}) 언급 (+{pts})"
    return "텔레그램 시그널", pts, TELEGRAM_MAX, evidence


def _foreign_trend_factor(foreign_v2_score: int | None) -> tuple[str, int, int, str]:
    """`foreign_v2_score`(0~`FOREIGN_V2_MAX`, `foreign_score_v2()`의 원점수)를
    이 축의 예산(`FOREIGN_TREND_MAX`)으로 선형 스케일한다. `None`(수급 시계열
    없음)은 0점으로 위장하지 않고 evidence 로 정직하게 남긴다."""
    if foreign_v2_score is None:
        return "외국인 추세", 0, FOREIGN_TREND_MAX, "수급 시계열 없음"
    pts = round(FOREIGN_TREND_MAX * foreign_v2_score / FOREIGN_V2_MAX)
    return "외국인 추세", pts, FOREIGN_TREND_MAX, f"foreign_score_v2 {foreign_v2_score}/{FOREIGN_V2_MAX} (+{pts})"


def _trending_factor(trending_score100: float | None) -> tuple[str, int, int, str]:
    if trending_score100 is None:
        return "트렌딩", 0, TRENDING_MAX, "트렌딩 데이터 없음"
    if trending_score100 >= TRENDING_HIGH_THRESHOLD:
        pts = TRENDING_HIGH_POINTS
        note = f"트렌딩 {trending_score100:.0f}≥{TRENDING_HIGH_THRESHOLD} (+{pts})"
    elif trending_score100 >= TRENDING_MED_THRESHOLD:
        pts = TRENDING_MED_POINTS
        note = f"트렌딩 {trending_score100:.0f}≥{TRENDING_MED_THRESHOLD} (+{pts})"
    else:
        pts = 0
        note = f"트렌딩 {trending_score100:.0f}<{TRENDING_MED_THRESHOLD}"
    return "트렌딩", pts, TRENDING_MAX, note


def _catalyst_factor(dart_types: list[str], theme_change_pct: float | None) -> tuple[str, int, int, str]:
    pts = 0
    notes: list[str] = []

    top_matched = [t for t in dart_types if t in CATALYST_DART_TOP_TYPES]
    watch_matched = [t for t in dart_types if t in CATALYST_DART_WATCH_TYPES]
    if top_matched:
        pts += CATALYST_DART_TOP_POINTS
        notes.append(f"공시 촉매(실측 유효) {top_matched} (+{CATALYST_DART_TOP_POINTS})")
    elif watch_matched:
        pts += CATALYST_DART_WATCH_POINTS
        notes.append(f"공시 촉매(표본 부족·판단 보류) {watch_matched} (+{CATALYST_DART_WATCH_POINTS})")
    else:
        notes.append("공시 촉매 없음")

    if theme_change_pct is None:
        notes.append("테마 등락 데이터 없음")
    elif theme_change_pct >= CATALYST_THEME_THRESHOLD_PCT:
        pts += CATALYST_THEME_POINTS
        notes.append(f"테마 등락 {theme_change_pct:+.1f}%≥{CATALYST_THEME_THRESHOLD_PCT:.0f}% (+{CATALYST_THEME_POINTS})")
    else:
        notes.append(f"테마 등락 {theme_change_pct:+.1f}%<{CATALYST_THEME_THRESHOLD_PCT:.0f}%")

    return "촉매", pts, CATALYST_MAX, "; ".join(notes)


def score_intraday(inputs: dict) -> dict | None:
    """단타 후보 1종목 채점. `inputs` 키:

    - today_articles: int (기본 0)
    - news_z: float | None
    - max_dup_count: int (기본 0)
    - titles: list[str] (기본 [] — 오늘 이 종목 기사 제목들. 호재 마커/악재
      거부권 판정에 쓴다. 과거 재구성처럼 제목을 복원할 수 없는 호출부는
      빈 리스트로 정직하게 degrade한다 — `_news_axis_factor` docstring)
    - recent_titles: list[dict] | None (기본 None — `mentions.continuity()`의
      `recent_titles`, `[{"title","weight"}, ...]`. 있으면 호재 마커 판정이
      최근 3개장일을 감쇠 가중으로 본다(2026-08-18, SK하이닉스 15점 사건
      재점검). 없으면 `titles`만으로 옛 동작(당일만) 그대로 — 하위 호환)
    - foreign_v2_score: int | None (`foreign_flow_v2.foreign_score_v2()`의
      원점수 — 수급 시계열이 아예 없으면 `None`, 있으면 항상 int)
    - trending_score100: float | None
    - dart_types: list[str] (기본 [])
    - theme_change_pct: float | None
    - telegram_mention: dict | None (기본 None — `telegram_view.
      telegram_mentions()`이 만드는 `{"channels": [...], "sector_room": bool}`
      한 종목분. nullable 4개와 달리 "모른다"가 아니라 "언급이 없다"는
      뜻이라 아래 3개-None 게이트 대상이 아니다)

    반환: `{"score100": int, "factors": [(name, pts, max, evidence)]}`.
    `news_z`/`foreign_v2_score`/`trending_score100`/`theme_change_pct` 중
    `MIN_NULLABLE_FOR_NONE_RESULT`개 이상이 None이면 `None`을 반환한다 —
    근거가 너무 얇을 때 점수를 매기지 않는다(데이터 부족을 점수로 위장 금지).
    """
    none_count = sum(1 for k in _NULLABLE_KEYS if inputs.get(k) is None)
    if none_count >= MIN_NULLABLE_FOR_NONE_RESULT:
        return None

    today_articles = inputs.get("today_articles") or 0
    max_dup_count = inputs.get("max_dup_count") or 0
    titles = inputs.get("titles") or []
    recent_titles = inputs.get("recent_titles") or None
    dart_types = inputs.get("dart_types") or []

    factors = [
        _news_axis_factor(
            today_articles, inputs.get("news_z"), max_dup_count, titles, recent_titles,
        ),
        _foreign_trend_factor(inputs.get("foreign_v2_score")),
        _trending_factor(inputs.get("trending_score100")),
        _catalyst_factor(dart_types, inputs.get("theme_change_pct")),
        _telegram_factor(inputs.get("telegram_mention")),
    ]
    score100 = sum(f[1] for f in factors)
    return {"score100": score100, "factors": factors}


def rank_intraday(candidates: dict[str, dict], top: int = 8) -> list[tuple[str, dict]]:
    """`candidates`(symbol → `score_intraday` 의 `inputs`)를 전부 채점해 상위
    `top`개를 돌려준다. `score_intraday`가 `None`을 낸 종목(근거 부족)은
    제외한다. 정렬: score100 내림차순 → 외국인 신호 강도 내림차순(같은 점수면
    수급 신호(`foreign_v2_score`)가 더 뚜렷한 쪽을 우선) → symbol 오름차순
    (결정론)."""
    scored: list[tuple[str, dict, int]] = []
    for symbol, inputs in candidates.items():
        result = score_intraday(inputs)
        if result is None:
            continue
        scored.append((symbol, result, _foreign_strength(inputs.get("foreign_v2_score"))))

    scored.sort(key=lambda item: (-item[1]["score100"], -item[2], item[0]))
    return [(symbol, result) for symbol, result, _ in scored[:top]]
