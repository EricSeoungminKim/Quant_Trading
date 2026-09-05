"""종목별 엔진 점수 — 우리가 모은 데이터만으로 매기는 근거 집계.

**아직 LLM 이 관여하지 않는다.** 전부 규칙이다. 무료 LLM 이 뉴스 본문을 읽고
판정을 붙이는 단계에서 "AI 애널리스트"로 승격한다 — 그 전에 AI 라고 부르면
증권사 컨센서스를 AI 라고 부르는 것과 같은 종류의 과장이다.

증권사 컨센서스 옆에 나란히 두는 게 목적이다. 사람 애널리스트는 실적·밸류에이션을
보고, 우리는 **뉴스 노출·수급·컨센서스 괴리**를 본다. 두 판단이 갈리는 지점이
곧 들여다볼 지점이다.

**매수/매도 권유가 아니라 근거 집계다.** 그래서 라벨도 긍정/부정 신호로 쓰고,
점수와 함께 **어떤 근거가 몇 점을 냈는지 전부 노출**한다. 근거를 못 펼치는 점수는
검증도 채점도 불가능하다.

각 요인은 이름이 안정적인 키를 갖는다 — 3층 채점에서 "어떤 요인이 실제로
맞혔나"를 요인별로 분해할 수 있게 하기 위해서다. **아래 요인별 가중치는 그
분해 결과다** — 2026-08-13~09-05 KR+US 오전판 리포트 실측(own recomputation,
yfinance 일봉, D+1 종가 기준)을 2026-09-06 감사가 재확인했다:

- `ai_score100`(이 모듈의 score100) Spearman IC = -0.082 (n=1,116, p=.006) —
  근거 없음, 오히려 역상관.
- `trending_score100`(quant.analyze.trending_score) IC = +0.186 (n=1,064,
  p<.0001) — 유의미하게 예측력 있음.
- origin "both"(뉴스+트렌딩 랭킹 동시 확인) D+1 평균 +278bp, 적중 56.0%
  (n=166) — news-only +28bp(n=920), watch_join +35bp(n=855)를 압도한다.
- 목표주가 괴리("upside") ≥20% 상향 근거는 D+1 방향 적중 53.5%(n=202)였지만
  D+3 -88bp(n=172)·D+5 -13bp(n=143)로 역예측적이었다 — 상향 근거는 짧게도
  못 믿을 신호였다. 하향(<0%) 근거는 표본이 따로 갈리지 않아 이 감사가
  손대지 않는다.

그래서 아래 가중치는 균등 ±1(2026-09-06 이전 값, 주석에 남긴다)을 걷어내고,
트렌딩 요인을 뉴스 건수 요인보다 무겁게, 뉴스+트렌딩 동시 확인에 별도
보너스를 준다.
"""
from __future__ import annotations

from quant.analyze.scoring import label_100, to_100

NEWS_HOT = 3          # 오늘 기사 이 건수 이상이면 관심 집중
STREAK_MIN = 2        # 연속 등장 이 일수 이상이면 근거 누적
FLOW_STREAK_MIN = 3   # 수급 연속 순매수 이 일수 이상이면 추세적
UPSIDE_BIG = 20.0     # 목표주가 괴리 이 % 이상이면 유의미(가중치는 아래 참고)
OPINION_GOOD = 4.0    # 증권사 의견 이 점 이상이면 우호
OPINION_BAD = 3.0     # 이 점 미만이면 비우호

# ── 요인별 가중치(2026-09-06 리포트 정확도 감사 반영, 모듈 docstring 근거) ──
#
# 예전 값(2026-09-06 이전, 전부 균등): NEWS_HOT_WEIGHT = NEWS_STREAK_WEIGHT =
# FLOW_WEIGHT = FLOW_STREAK_WEIGHT = UPSIDE_POSITIVE_WEIGHT =
# UPSIDE_NEGATIVE_WEIGHT = OPINION_WEIGHT = 1, TRENDING_WEIGHT/CONFIRMATION_BONUS
# 없음(트렌딩은 이 모듈이 아예 안 봤다), SPAN = 7.
NEWS_HOT_WEIGHT = 1
NEWS_STREAK_WEIGHT = 1
FLOW_WEIGHT = 1              # foreign_5d/inst_5d 공용(부호는 실제 순매수/도 방향)
FLOW_STREAK_WEIGHT = 1
# 목표주가 상향(≥20%) 근거 — 감사: D+1 방향 적중 53.5%지만 D+3 -88bp/D+5 -13bp로
# 역예측적(n=202/172/143). 가중치를 0으로 낮춰 사실상 반영하지 않는다(하향 근거는
# 그대로 둔다 — 아래 UPSIDE_NEGATIVE_WEIGHT). 상수로 남기는 이유: 표본이 다시
# 쌓여 결과가 뒤집히면 이 한 줄만 고치면 되게.
UPSIDE_POSITIVE_WEIGHT = 0
UPSIDE_NEGATIVE_WEIGHT = 1   # 하향 근거는 감사가 문제 삼지 않았다 — 그대로 유지.
OPINION_WEIGHT = 1
# 감사 IC(+0.186)가 ai_score100 자체의 IC(-0.082)보다 뚜렷이 높다 — 뉴스 건수
# 요인(NEWS_HOT_WEIGHT=1)보다 무겁게 잡는다.
TRENDING_WEIGHT = 2
# 뉴스+트렌딩 랭킹 동시 확인("origin=both") 보너스. 감사 실측 +278bp(n=166)는
# news-only(+28bp)·watch_join(+35bp)의 10배 안팎이라 단일 요인 중 가장 크게
# 잡는다 — SPAN(아래)의 절반이라 이 보너스 하나만으로 score100이 75(label_100의
# "강한 긍정 신호" 문턱)에 닿는다. 다른 요인이 더해지면 100에서 클립된다.
CONFIRMATION_BONUS = 4

# 가점 가능한 요인(news_hot, news_streak, foreign_5d, inst_5d, flow_streak,
# opinion, trending)의 고정 분모 — CONFIRMATION_BONUS는 "보너스"라 분모에
# 넣지 않는다(위 상수 주석 참고). upside는 양의 가중치가 0이라 분모 계산에서
# 뺐다(하향만 있는 요인은 이론상 최대 양의 기여가 없다). 실제 발동 요인 수로
# 나누지 않는다 — to_100 참고.
SPAN = (
    NEWS_HOT_WEIGHT + NEWS_STREAK_WEIGHT + FLOW_WEIGHT * 2 + FLOW_STREAK_WEIGHT
    + OPINION_WEIGHT + TRENDING_WEIGHT
)


def score_symbol(cont: dict | None, detail: dict | None, trending: dict | None = None) -> dict:
    """`{"score", "label", "factors": [{"key","delta","text"}]}`.

    cont     — mentions.continuity 의 종목 항목 (없으면 뉴스 요인 생략)
    detail   — stock_detail.fetch_stock_detail 결과 (없으면 수급·컨센서스 요인 생략)
    trending — quant.analyze.trending_score.trending_score() 결과(없으면
        호출부 하위호환 — 트렌딩/확인 요인 생략). `score100`이 중립(50)이 아니면
        방향에 따라 ±TRENDING_WEIGHT를 더하고, `cont`도 있고 `boards`도 있으면
        (뉴스+트렌딩 랭킹 동시 확인) CONFIRMATION_BONUS를 더한다 — 모듈 docstring
        감사 근거 참고.
    """
    factors: list[dict] = []

    def add(key: str, delta: int, text: str) -> None:
        factors.append({"key": key, "delta": delta, "text": text})

    if cont:
        today = cont.get("today_articles") or 0
        if today >= NEWS_HOT:
            add("news_hot", NEWS_HOT_WEIGHT, f"오늘 뉴스 {today}건 집중")
        streak = cont.get("streak_days") or 0
        if streak >= STREAK_MIN:
            add("news_streak", NEWS_STREAK_WEIGHT, f"{streak}일 연속 노출")

    if detail:
        fs = detail.get("flow_summary") or {}
        # 순매매 0 은 중립이다 — 순매도로 취급하면 거래가 없던 종목이 부당하게
        # 감점된다. 부호가 있을 때만 요인으로 센다.
        for key, label, value in (
            ("foreign_5d", "외국인", fs.get("foreign_net_5d")),
            ("inst_5d", "기관", fs.get("inst_net_5d")),
        ):
            if not value:
                continue
            add(key, FLOW_WEIGHT if value > 0 else -FLOW_WEIGHT,
                f"{label} 5일 {'순매수' if value > 0 else '순매도'}")
        best_streak = max(fs.get("foreign_buy_streak") or 0, fs.get("inst_buy_streak") or 0)
        if best_streak >= FLOW_STREAK_MIN:
            add("flow_streak", FLOW_STREAK_WEIGHT, f"연속 순매수 {best_streak}일")

        up = detail.get("upside_pct")
        if up is not None:
            if up >= UPSIDE_BIG and UPSIDE_POSITIVE_WEIGHT:
                add("upside", UPSIDE_POSITIVE_WEIGHT, f"목표주가 대비 여력 {up:+.0f}%")
            elif up < 0:
                add("upside", -UPSIDE_NEGATIVE_WEIGHT, f"목표주가 하회 {up:+.0f}%")

        cs = detail.get("consensus") or {}
        op = cs.get("opinion_score")
        if op is not None:
            if op >= OPINION_GOOD:
                add("opinion", OPINION_WEIGHT, f"증권사 의견 {op}")
            elif op < OPINION_BAD:
                add("opinion", -OPINION_WEIGHT, f"증권사 의견 {op}")

    if trending:
        t100 = trending.get("score100")
        if t100 is not None and t100 != 50:
            add("trending", TRENDING_WEIGHT if t100 > 50 else -TRENDING_WEIGHT,
                f"트렌딩 점수 {t100}점 반영(가중치 {TRENDING_WEIGHT})")
        if cont and trending.get("boards"):
            add("confirmation", CONFIRMATION_BONUS,
                "뉴스+트렌딩 랭킹 동시 확인 — 감사 근거 최상위 등급")

    score = sum(f["delta"] for f in factors)
    score100 = to_100(score, SPAN)
    label = label_100(score100, "긍정 신호", "부정 신호")
    return {"score": score, "score100": score100, "label": label, "factors": factors}


def score_all(
    cont: dict[str, dict], details: dict[str, dict], trending: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """뉴스에 잡힌 종목 전부에 대해 점수를 매긴다. 상세·트렌딩이 없어도
    뉴스만으로 매긴다(둘 다 호출부 하위호환 — score_symbol 참고)."""
    trending = trending or {}
    return {
        code: score_symbol(c, details.get(code), trending.get(code))
        for code, c in cont.items()
    }
