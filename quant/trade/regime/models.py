"""국면 판단 결과 모델. stdlib only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RegimeState:
    """RegimeProvider.refresh()의 산출물이자 data/state/regime.json 캐시 스키마.

    degraded: 지표를 하나도 못 구해 강제로 neutral 처리됐다는 표시. 정상적으로
    점수 합산 결과가 중립 구간에 떨어진 것과 구분해야 호출부가 "판단했더니
    중립"과 "판단을 못 했다"를 구별해 알림을 걸 수 있다(조용한 기본값 금지).
    """

    label: str  # "defensive" | "neutral" | "aggressive"
    risk_multiplier: float
    reasons: list[str]
    computed_at: datetime
    degraded: bool = False

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "risk_multiplier": self.risk_multiplier,
            "reasons": self.reasons,
            "computed_at": self.computed_at.isoformat(),
            "degraded": self.degraded,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeState":
        return cls(
            label=d["label"],
            risk_multiplier=d["risk_multiplier"],
            reasons=list(d["reasons"]),
            computed_at=datetime.fromisoformat(d["computed_at"]),
            degraded=d.get("degraded", False),
        )
