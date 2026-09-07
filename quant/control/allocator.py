"""자본 자동 강등 장치 — 소유자 북극성(2026-08-28): "오늘 잃어도 되지만 내일은
벌어야 한다. 내가 참견하지 않아도 하루하루 나아져야 한다."

실측(원장 399건): 전략 7종 전부 수수료 전에도 음수. 어느 전략도 개선되지
않아도 **지는 곳에서 자본을 빼면 포트폴리오는 매일 나아진다** — 그게 이
장치의 존재 이유다.

**이 모듈은 "덜 잃게" 할 뿐 "벌게" 하지 않는다.** 지는 전략의 배분을 줄여
그 전략이 계좌를 갉아먹는 속도를 늦출 뿐, 남은 전략이 돈을 벌 것이라는
보장은 어디에도 없다. 성과를 주장하지 마라.

## 설계 원칙 (`quant/control/governor.py` 층 0과 같은 철학)

**한 방향만 자동이다 — 자본을 줄이는 것만 자동, 늘리는 것은 사람만.**
실수의 비용이 비대칭이기 때문이다: 과소 배분은 놓친 기회일 뿐이지만 과대
배분은 원금 손실이다. 회복 가능성이 다르면 권한도 비대칭이어야 한다.
`decide()`가 증가 방향 결과를 만들어내면 그건 버그이지 기능이 아니다 —
그 경우 항목을 skip하고 사유를 남긴다(방어적 코딩, 발생해선 안 되는
경로도 침묵시키지 않는다).

이 파일은 순수 로직이다. DB/파일 I/O는 호출부(`quant/apps/cli.py`)의
책임이다 — `quant/control/`은 결정론적 판단만 하고, 원장을 읽고 오버레이에
쓰는 것은 배선 층의 일이다(governor.py/cmd_governor_apply와 같은 분리).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

# 2026-09-07(진화가능성 평가 투자 #3): 이 파일은 독립적으로 20을 최소 표본선으로
# 써 왔는데, `quant/control/ledger.py`(스코어보드 판정)·`quant/control/governor.py`
# 는 이미 30(`MIN_TRIPS_FOR_JUDGEMENT`)을 쓴다 — 같은 질문("판단할 만큼 쌓였나")에
# 파일마다 다른 답을 하고 있었다. ledger의 상수를 그대로 재사용해 하나로 합친다.
from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT
from quant.control.multiple_testing import benjamini_hochberg

# 2026-09-07(결정 ⑧, 다중검정 보정): 자동 강등 후보가 같은 회차에 여럿이면
# 개별 검정(90% 신뢰상한)만으로는 우연히 유의해 보이는 전략이 섞인다 —
# `quant/control/multiple_testing.py` 모듈 docstring 참고. FDR(q) 기본값.
FDR_Q = 0.10


@dataclass
class StrategyStat:
    """전략 하나의 종결 트레이드 통계 (bps 축, 통화 무관)."""
    strategy: str
    n: int
    mean_bp: float
    stdev_bp: float


def is_losing(stat: StrategyStat, *, min_samples: int = MIN_TRIPS_FOR_JUDGEMENT,
              confidence: float = 0.90) -> tuple[bool, str]:
    """이 전략이 "지고 있다고 말할 만한 증거"가 있는가.

    단순히 `mean_bp < 0`이면 우연한 손실 구간(운 나쁜 20건)에도 강등된다 —
    동전을 던져도 절반은 마이너스 구간을 지난다. 그래서 **평균의 단측 신뢰
    상한**을 쓴다: `mean + z * stdev/sqrt(n)`이 여전히 0 미만이어야 "우연이
    아니라 진짜 지고 있다"고 본다. z는 하드코딩하지 않고 정규분포 역함수로
    구한다 — confidence를 바꾸면 z도 따라 바뀌어야 하는데 하드코딩하면
    둘이 따로 논다.
    """
    z = statistics.NormalDist().inv_cdf(confidence)
    if stat.n < min_samples:
        return False, f"표본 부족 {stat.n}/{min_samples}건 — 판단 보류"

    se = stat.stdev_bp / (stat.n ** 0.5)
    upper = stat.mean_bp + z * se
    detail = (f"n={stat.n} 평균={stat.mean_bp:+.1f}bp "
              f"신뢰상한({confidence:.0%})={upper:+.1f}bp")
    if upper < 0:
        return True, f"{detail} — 지고 있다는 근거 충분(상한도 0 미만)"
    return False, f"{detail} — 우연한 손실 구간일 수 있어 무변경"


def p_value_losing(stat: StrategyStat) -> float:
    """단측 p값 — "이 전략의 참 평균이 0 미만"이라는 귀무가설 하에서 관측된
    평균(혹은 그보다 더 극단적인 값)이 나올 확률.

    `is_losing()`이 90% 신뢰상한을 만들 때 쓴 것과 **같은 정규근사 가정**을
    쓴다(z, t분포 아님 — 표준 라이브러리에 t분포 역함수가 없고, 기존 코드가
    이미 z를 쓰고 있어 하나의 가정으로 통일한다): 검정통계량
    z = mean_bp / se (se = stdev_bp / sqrt(n))는 귀무가설(참 평균=0) 하에서
    표준정규를 근사한다. p = P(Z <= z) = Φ(z) — z가 많이 음수일수록 p가
    작아진다(평균이 우연히 이렇게 낮게 나올 확률이 작다 = 진짜 지고 있다는
    근거가 강하다).

    n<2나 stdev_bp<=0(표본 분산을 계산할 수 없음)이면 se가 0이 되어 나눗셈이
    안 된다 — 이 경우 부호만으로 극단값 취급한다(평균<0 이면 p=0.0, 아니면
    p=1.0). 실제로는 `decide()`가 이 함수를 부르기 전에 `is_losing()`의
    `min_samples`(기본 30) 문턱을 이미 통과한 후보에만 적용하므로 거의 발생하지
    않는다."""
    se = stat.stdev_bp / (stat.n ** 0.5) if stat.n > 0 else 0.0
    if se <= 0:
        return 0.0 if stat.mean_bp < 0 else 1.0
    z = stat.mean_bp / se
    return statistics.NormalDist().cdf(z)


def next_fraction(current: float, *, factor: float = 0.5, floor: float = 0.05) -> float:
    """반감, 하한 클램프. 이미 하한이면 그대로 — 완전 정지는 사람의 결정이다
    (전략이 죽었는지 판단하려면 최소한의 표본이 계속 나와야 한다)."""
    if current <= floor:
        return current
    return max(floor, current * factor)


@dataclass
class Demotion:
    """강등 판단 하나. `applied=False`인 항목(냉각/하한/증거 부족)도 그대로
    기록된다 — 방어층이 실제로 일하는지 보여주는 유일한 증거는 거부 기록이다."""
    strategy: str
    market: str
    current: float
    proposed: float
    reason: str
    applied: bool
    skip_reason: str = ""
    # 2026-09-07(결정 ⑧, 다중검정 보정) — 이 후보의 단측 p값(`p_value_losing`)과
    # 그 회차의 BH 문턱·통과 여부. 기존 90% 신뢰상한 프리필터를 통과한 후보에만
    # 채워진다(표본 부족/신뢰상한 양수로 애초에 후보가 아니었던 전략은 이 필드가
    # 없는 Demotion 자체가 안 만들어진다). `capital_decisions.jsonl`에 원시 p와
    # BH 판정을 같이 남겨 "우연이 아니라는 근거"를 사람이 재검증할 수 있게 한다.
    p_value: float = 0.0
    bh_threshold: float = 0.0
    passed_fdr: bool = False


def decide(stats: list[StrategyStat],
           current_fractions: dict[tuple[str, str], float],
           last_change_days: dict[str, int | None],
           *, min_samples: int = MIN_TRIPS_FOR_JUDGEMENT, cooldown_days: int = 5,
           factor: float = 0.5, floor: float = 0.05, fdr_q: float = FDR_Q) -> list[Demotion]:
    """전략별 통계를 심사해 강등 후보 목록을 만든다.

    "지고 있다"는 증거가 없는 전략(표본 부족 포함)은 애초에 후보가 아니다 —
    아무 항목도 만들지 않는다(원칙 2: 증거 없이 움직이지 않는다).

    **다중검정 보정(BH, q=`fdr_q`)은 이 회차에 평가된 전략 `stats` 전부를
    가족(family)으로 삼는다** — 신뢰상한 프리필터를 통과한 것만 가족으로
    좁히면 안 된다(그러면 이미 유의해 보이는 것들끼리만 비교하게 되어 보정이
    사실상 무력화된다: 17개 중 5개가 우연히 유의해 보였다면 그 5개만으로
    보정해선 안 되고 원래 17개 전부를 분모로 써야 FDR이 의미가 있다).
    `quant/control/multiple_testing.py` 참고. 프리필터를 통과한 전략만 BH
    판정과 무관하게 **후보로서 기록**되고(Demotion row 생성), 그중에서도 BH를
    통과한 것만 실제로 냉각/하한/보폭을 검사해 강등을 적용한다 — BH를 통과
    못 한 후보도 스킵으로 기록은 남긴다(원장 규율: 거부도 남긴다).
    """
    pvalues = [p_value_losing(stat) for stat in stats]
    passed_flags = benjamini_hochberg(pvalues, q=fdr_q)
    bh_threshold = max((p for p, ok in zip(pvalues, passed_flags) if ok), default=0.0)

    out: list[Demotion] = []
    for stat, p, passed_fdr in zip(stats, pvalues, passed_flags):
        losing, reason = is_losing(stat, min_samples=min_samples)
        if not losing:
            continue

        markets = sorted(m for (s, m) in current_fractions if s == stat.strategy)
        for market in markets:
            current = current_fractions[(stat.strategy, market)]
            if current <= 0:
                # 이 시장엔 애초에 배분이 없다(구조적 0, 예: donchian의 KR) —
                # 강등 대상이 아니다. 기록할 "결정"도 아니다.
                continue

            if not passed_fdr:
                # 개별로는 신뢰상한이 0 미만이었지만, 같은 회차에 검정한 다른
                # 후보들과 함께 보면 다중검정 보정 후엔 우연일 가능성을
                # 배제하지 못한다 — 자동 강등은 하지 않는다.
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False,
                    skip_reason=(f"FDR(BH q={fdr_q:.2f}) 미통과 — p={p:.4f} > "
                                 f"문턱 {bh_threshold:.4f} (다중검정 보정 후 우연일 가능성 배제 못함)"),
                    p_value=p, bh_threshold=bh_threshold, passed_fdr=False,
                ))
                continue

            days = last_change_days.get(stat.strategy)
            if days is not None and days < cooldown_days:
                # 왜 냉각인가: 줄인 직후 또 줄이면 새 크기에서의 성과를 측정할
                # 기회가 없다 — 판단이 계속 그때그때 흔들리는 파라미터를
                # 만든다.
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False,
                    skip_reason=f"냉각 중 — 마지막 강등 {days}일 전 ({cooldown_days}일 필요)",
                    p_value=p, bh_threshold=bh_threshold, passed_fdr=True,
                ))
                continue

            if current <= floor:
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False,
                    skip_reason=f"이미 하한({floor}) — 자동으로는 더 줄이지 않는다",
                    p_value=p, bh_threshold=bh_threshold, passed_fdr=True,
                ))
                continue

            proposed = next_fraction(current, factor=factor, floor=floor)
            if proposed > current:
                # 여기 도달하면 next_fraction이나 호출부의 버그다 — 증가
                # 방향은 절대 자동 반영하지 않는다(설계 타협 금지 원칙).
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False, skip_reason="증가 방향은 자동 금지",
                    p_value=p, bh_threshold=bh_threshold, passed_fdr=True,
                ))
                continue

            out.append(Demotion(
                stat.strategy, market, current, proposed, reason, applied=True,
                p_value=p, bh_threshold=bh_threshold, passed_fdr=True,
            ))

    return out
