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


@dataclass
class StrategyStat:
    """전략 하나의 종결 트레이드 통계 (bps 축, 통화 무관)."""
    strategy: str
    n: int
    mean_bp: float
    stdev_bp: float


def is_losing(stat: StrategyStat, *, min_samples: int = 20,
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


def decide(stats: list[StrategyStat],
           current_fractions: dict[tuple[str, str], float],
           last_change_days: dict[str, int | None],
           *, min_samples: int = 20, cooldown_days: int = 5,
           factor: float = 0.5, floor: float = 0.05) -> list[Demotion]:
    """전략별 통계를 심사해 강등 후보 목록을 만든다.

    "지고 있다"는 증거가 없는 전략(표본 부족 포함)은 애초에 후보가 아니다 —
    아무 항목도 만들지 않는다(원칙 2: 증거 없이 움직이지 않는다). 후보로
    떠오른 전략만 시장별로 냉각/하한/보폭을 검사한다.
    """
    out: list[Demotion] = []
    for stat in stats:
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

            days = last_change_days.get(stat.strategy)
            if days is not None and days < cooldown_days:
                # 왜 냉각인가: 줄인 직후 또 줄이면 새 크기에서의 성과를 측정할
                # 기회가 없다 — 판단이 계속 그때그때 흔들리는 파라미터를
                # 만든다.
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False,
                    skip_reason=f"냉각 중 — 마지막 강등 {days}일 전 ({cooldown_days}일 필요)",
                ))
                continue

            if current <= floor:
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False,
                    skip_reason=f"이미 하한({floor}) — 자동으로는 더 줄이지 않는다",
                ))
                continue

            proposed = next_fraction(current, factor=factor, floor=floor)
            if proposed > current:
                # 여기 도달하면 next_fraction이나 호출부의 버그다 — 증가
                # 방향은 절대 자동 반영하지 않는다(설계 타협 금지 원칙).
                out.append(Demotion(
                    stat.strategy, market, current, current, reason,
                    applied=False, skip_reason="증가 방향은 자동 금지",
                ))
                continue

            out.append(Demotion(stat.strategy, market, current, proposed, reason, applied=True))

    return out
