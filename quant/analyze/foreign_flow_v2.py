"""외국인 수급 추종 v2 — KR 시장 실무 규칙 채점(서브프로젝트 O, 2026-08-17).

`foreign_trend.classify()`의 재유입/이탈/관망 5라벨 모델은 KR 시장 실무자들이
실제로 보는 신호(쌍끌이 동반매수, 연속 순매수일수, 1·5·20일 추세 정합, 거래량
대비 강도)를 라벨 하나로 뭉갠다. `quant/backtest/report_replay.py`의 4절 실측
(picks mover비율 24.0% vs base rate 25.0%, 우연 수준)이 그 뭉개짐 때문인지
확인하려고, 웹 리서치로 확인한 실무 규칙을 이 모듈이 독립 점수로 합산한다:

- **쌍끌이**(외국인+기관 동반 순매수, 최근 1일)가 최강 신호 — 더페어
  "수급이 곧 수익률" 동조화 분석, KB자산운용 "누가 사야 잘 오를까": 동반 매수
  종목 수익률이 시장 평균을 상회.
- **외국인 단독 > 기관 단독**(KB자산운용) — 이 모듈은 기관을 쌍끌이 보너스에만
  쓰고 독립 가점을 주지 않는다(`foreign_trend.classify`와 같은 원칙, 사용자
  확인).
- **연속 순매수일수**: 인포스탁 연속 순매수일 랭킹이 표준 지표 — 2일+/3일+
  단계 가점.
- **추세 정합**: 1·5·20일 누적이 모두 양수면 추세, 엇갈리면 일시 유입(실무
  요령).
- **강도**: |외국인 순매수(주식수)| / 당일 거래량 — 비중이 클수록 유의미
  (수급 강도-수익률 상관 연구).

## 왜 `quant/analyze/`인가 (평면 규칙)

이 함수는 처음엔 `quant/backtest/report_replay.py`(백테스트 재구성 전용)에
있었다 — A/B 실측(2026-08-17)에서 실전 규칙(변형 B)이 기존 라벨 기반(변형 A)을
movers precision·방향성 둘 다에서 이겼고, 라이브 단타 스코어러
(`quant/analyze/intraday_score.py`)에 그 승리 조합을 반영하게 됐다.
`quant/trade/strategy/CLAUDE.md`의 평면 규칙상 `quant/analyze/`는
`quant/backtest/`를 몰라야 하는데, `intraday_score.py`(analyze)가
`report_replay.py`(backtest 취급 경로)를 임포트하면 그 방향이 거꾸로 된다 —
그래서 순수 채점 로직만 이 모듈로 옮기고, `report_replay.py`는 이제 여기서
임포트한다(analyze → backtest 방향, 허용된 방향).

## 순수 함수 — 네트워크·LLM 없음

`quant/trade/`는 이 모듈을 전혀 모른다(임포트하지 않는다) — 이 스코어러는
진입을 결정하지 않는다, 사람이 볼 리포트/백테스트의 순위만 매긴다
(`intraday_score.py` 상단 주석과 동일한 원칙).
"""
from __future__ import annotations

from datetime import date

# `foreign_score_v2()`가 쓰는 배점·임계값 — 웹 리서치로 얻은 실무 규칙을
# 코드로 옮긴 초기값이다(검증되면 조정 근거가 생긴다는 전제는
# `intraday_score.py`의 "요인 설계는 초기 가중치다"와 동일).
FOREIGN_V2_TANDEM_POINTS = 12       # 쌍끌이(외국인+기관 동반 순매수, 최근일)
FOREIGN_V2_STREAK3_DAYS = 3         # 외국인 연속 순매수일수 ≥3일
FOREIGN_V2_STREAK3_POINTS = 10
FOREIGN_V2_STREAK2_DAYS = 2         # ==2일
FOREIGN_V2_STREAK2_POINTS = 6
FOREIGN_V2_TREND_WINDOWS = (1, 5, 20)   # 누적 정합 창(거래일)
FOREIGN_V2_TREND_ALIGN_POINTS = 8
FOREIGN_V2_STRENGTH_HIGH_PCT = 3.0  # 강도(|foreign_net|/거래량) ≥3%
FOREIGN_V2_STRENGTH_HIGH_POINTS = 6
FOREIGN_V2_STRENGTH_MED_PCT = 1.0   # ≥1%
FOREIGN_V2_STRENGTH_MED_POINTS = 3
FOREIGN_V2_EXIT_STREAK_DAYS = 2     # 이탈 중(연속 순매도) ≥2일
FOREIGN_V2_EXIT_PENALTY = 10
# 명목 상한 — 실제 달성 가능한 최댓값은 36점(쌍끌이12+연속≥3일10+추세정합8+
# 강도고6, 이탈 패널티는 부호상 연속매수와 동시 발생 불가)이지만, 이 축을 다른
# 배점(REPLAY_WEIGHTS_B/C, intraday_score.py의 FOREIGN_TREND_MAX)과 나누는
# 분모로 40을 그대로 쓴다 — 향후 규칙이 추가돼도 이 분모를 유지하면 기존
# 채점이 흔들리지 않는다.
FOREIGN_V2_MAX = 40


def _trailing_run(series: list[dict]) -> tuple[int, int, float]:
    """`series`(date-asc) 마지막 부호(양/음/0)가 같은 연속 구간의
    (부호, 길이, 구간합). 빈 시계열은 `(0, 0, 0.0)`."""
    if not series:
        return 0, 0, 0.0

    def _sign(v: float) -> int:
        return 1 if v > 0 else (-1 if v < 0 else 0)

    last_sign = _sign(series[-1].get("foreign_net") or 0)
    length = 0
    total = 0.0
    for row in reversed(series):
        v = row.get("foreign_net") or 0
        if _sign(v) != last_sign:
            break
        length += 1
        total += v
    return last_sign, length, total


def foreign_intensity_ratio(series: list[dict], bars_by_date: dict[date, dict]) -> float | None:
    """마지막 관찰일(`series[-1]`, D-1 이하)의 |외국인 순매수(주식수)| /
    그날 거래량. `series`가 이미 D 미만으로 필터된 값이므로 이 함수는 그
    범위 밖 날짜를 절대 보지 않는다(look-ahead 없음 — `bars_by_date`에 미래
    날짜가 섞여 있어도 참조하지 않는다). 거래량·순매수 결측이면 `None`
    (0으로 위장하지 않는다). `bars_by_date`가 빈 dict(봉 캐시 없는 호출부 —
    `intraday_verify.py`처럼 pre-open 재구성에 봉을 안 쓰는 하네스)여도
    `None`을 정직하게 돌려줄 뿐 예외를 내지 않는다."""
    if not series:
        return None
    last = series[-1]
    try:
        d_last = date.fromisoformat(str(last.get("date")))
    except (TypeError, ValueError):
        return None
    bar = bars_by_date.get(d_last)
    f_net = last.get("foreign_net")
    if not bar or not bar.get("volume") or f_net is None:
        return None
    return abs(f_net) / bar["volume"]


def foreign_score_v2(series: list[dict], bars_by_date: dict[date, dict]) -> tuple[int, list[str]]:
    """외국인 수급 추종 v2 채점. 모듈 docstring의 5개 실무 규칙을 합산한다.

    `series`: date-asc `[{date, foreign_net, inst_net}]`, 호출자가 이미 D
    **미만**(strictly before)으로 필터링한 값(look-ahead 없음, ≤D-1만 본다 —
    `quant/backtest/report_replay.prior_flow_rows`,
    `quant/backtest/intraday_verify._foreign_label_for`와 동일 계약).
    `bars_by_date`: `{date: {..., "volume":...}}` 같은 종목의 봉 캐시 — 없으면
    (빈 dict) 강도 축만 조용히 0점 처리된다(다른 4개 규칙은 봉 없이도 전부
    계산 가능).

    반환 `(score, evidence)`. `score`는 0 이상(하한 0 — floor), 명목 상한
    `FOREIGN_V2_MAX`(실제 달성 가능 최댓값은 36, 상수 정의 옆 주석 참고).
    `evidence`는 트리거된 규칙만 사람이 읽는 문자열로(트리거 없으면
    "트리거된 규칙 없음" 한 줄)."""
    if not series:
        return 0, ["수급 시계열 없음"]

    evidence: list[str] = []
    score = 0

    last = series[-1]
    last_foreign = last.get("foreign_net") or 0
    last_inst = last.get("inst_net") or 0

    if last_foreign > 0 and last_inst > 0:
        score += FOREIGN_V2_TANDEM_POINTS
        evidence.append(f"쌍끌이(외국인+기관 동반 순매수, 최근일) +{FOREIGN_V2_TANDEM_POINTS}")

    sign, length, _run_sum = _trailing_run(series)

    if sign > 0:
        if length >= FOREIGN_V2_STREAK3_DAYS:
            score += FOREIGN_V2_STREAK3_POINTS
            evidence.append(f"외국인 연속 순매수 {length}일(≥{FOREIGN_V2_STREAK3_DAYS}일) +{FOREIGN_V2_STREAK3_POINTS}")
        elif length == FOREIGN_V2_STREAK2_DAYS:
            score += FOREIGN_V2_STREAK2_POINTS
            evidence.append(f"외국인 연속 순매수 {length}일 +{FOREIGN_V2_STREAK2_POINTS}")
    elif sign < 0 and length >= FOREIGN_V2_EXIT_STREAK_DAYS:
        score -= FOREIGN_V2_EXIT_PENALTY
        evidence.append(f"이탈 중(연속 순매도 {length}일) -{FOREIGN_V2_EXIT_PENALTY}")

    window_sums = {w: sum(r.get("foreign_net") or 0 for r in series[-w:]) for w in FOREIGN_V2_TREND_WINDOWS}
    if all(window_sums[w] > 0 for w in FOREIGN_V2_TREND_WINDOWS):
        used = "·".join(f"{min(w, len(series))}일" for w in FOREIGN_V2_TREND_WINDOWS)
        score += FOREIGN_V2_TREND_ALIGN_POINTS
        evidence.append(f"누적 정합({used} 모두 양수) +{FOREIGN_V2_TREND_ALIGN_POINTS}")

    ratio = foreign_intensity_ratio(series, bars_by_date)
    if ratio is not None and ratio >= FOREIGN_V2_STRENGTH_HIGH_PCT / 100:
        score += FOREIGN_V2_STRENGTH_HIGH_POINTS
        evidence.append(f"강도 {ratio * 100:.1f}%(≥{FOREIGN_V2_STRENGTH_HIGH_PCT:.0f}%) +{FOREIGN_V2_STRENGTH_HIGH_POINTS}")
    elif ratio is not None and ratio >= FOREIGN_V2_STRENGTH_MED_PCT / 100:
        score += FOREIGN_V2_STRENGTH_MED_POINTS
        evidence.append(f"강도 {ratio * 100:.1f}%(≥{FOREIGN_V2_STRENGTH_MED_PCT:.0f}%) +{FOREIGN_V2_STRENGTH_MED_POINTS}")

    score = max(0, min(score, FOREIGN_V2_MAX))
    if not evidence:
        evidence.append("트리거된 규칙 없음")
    return score, evidence
