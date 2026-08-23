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

각 요인은 ±1. 나중에 3층 채점에서 "어떤 요인이 실제로 맞혔나"를 요인별로 분해해
가중치를 조정할 수 있도록, 요인 이름을 안정적인 키로 남긴다.
"""
from __future__ import annotations

from quant.analyze.scoring import label_100, to_100

NEWS_HOT = 3          # 오늘 기사 이 건수 이상이면 관심 집중
STREAK_MIN = 2        # 연속 등장 이 일수 이상이면 근거 누적
FLOW_STREAK_MIN = 3   # 수급 연속 순매수 이 일수 이상이면 추세적
UPSIDE_BIG = 20.0     # 목표주가 괴리 이 % 이상이면 유의미
OPINION_GOOD = 4.0    # 증권사 의견 이 점 이상이면 우호
OPINION_BAD = 3.0     # 이 점 미만이면 비우호

# 가점 가능한 요인 7개(news_hot, news_streak, foreign_5d, inst_5d, flow_streak,
# upside, opinion)의 고정 분모. 실제 발동 요인 수로 나누지 않는다 — to_100 참고.
SPAN = 7


def score_symbol(cont: dict | None, detail: dict | None) -> dict:
    """`{"score", "label", "factors": [{"key","delta","text"}]}`.

    cont  — mentions.continuity 의 종목 항목 (없으면 뉴스 요인 생략)
    detail— stock_detail.fetch_stock_detail 결과 (없으면 수급·컨센서스 요인 생략)
    """
    factors: list[dict] = []

    def add(key: str, delta: int, text: str) -> None:
        factors.append({"key": key, "delta": delta, "text": text})

    if cont:
        today = cont.get("today_articles") or 0
        if today >= NEWS_HOT:
            add("news_hot", 1, f"오늘 뉴스 {today}건 집중")
        streak = cont.get("streak_days") or 0
        if streak >= STREAK_MIN:
            add("news_streak", 1, f"{streak}일 연속 노출")

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
            add(key, 1 if value > 0 else -1,
                f"{label} 5일 {'순매수' if value > 0 else '순매도'}")
        best_streak = max(fs.get("foreign_buy_streak") or 0, fs.get("inst_buy_streak") or 0)
        if best_streak >= FLOW_STREAK_MIN:
            add("flow_streak", 1, f"연속 순매수 {best_streak}일")

        up = detail.get("upside_pct")
        if up is not None:
            if up >= UPSIDE_BIG:
                add("upside", 1, f"목표주가 대비 여력 {up:+.0f}%")
            elif up < 0:
                add("upside", -1, f"목표주가 하회 {up:+.0f}%")

        cs = detail.get("consensus") or {}
        op = cs.get("opinion_score")
        if op is not None:
            if op >= OPINION_GOOD:
                add("opinion", 1, f"증권사 의견 {op}")
            elif op < OPINION_BAD:
                add("opinion", -1, f"증권사 의견 {op}")

    score = sum(f["delta"] for f in factors)
    score100 = to_100(score, SPAN)
    label = label_100(score100, "긍정 신호", "부정 신호")
    return {"score": score, "score100": score100, "label": label, "factors": factors}


def score_all(cont: dict[str, dict], details: dict[str, dict]) -> dict[str, dict]:
    """뉴스에 잡힌 종목 전부에 대해 점수를 매긴다. 상세가 없어도 뉴스만으로 매긴다."""
    return {
        code: score_symbol(c, details.get(code))
        for code, c in cont.items()
    }
