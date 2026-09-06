"""`data/state/regime.json`에서 시장별 국면(regime) sub-dict를 읽는 순수 함수.

세 곳에서 토씨 하나 다르지 않게 복제돼 있던 로직을 여기 하나로 합친다
(`quant/report/collect/tg_digest_section.py::_load_regime_for_report`,
`quant/report/collect/core.py::_load_regime_for_stance`,
`quant/apps/cli.py::_tg_digest_load_regime`, 2026-09-07 통합). `quant.trade.
regime.provider._save_cache`가 쓰는 스키마 그대로 읽는다(최상위=US 하위호환,
`markets.{KR,US}`=현행). 실패는 예외가 아니라 `None`이다 — 호출부가 정직하게
"판정 불가"를 보여줄 수 있게.
"""
from __future__ import annotations

import json
from pathlib import Path


def load_regime_for_market(root: Path, market: str) -> dict | None:
    """`root/data/state/regime.json`에서 `market`(KR/US) sub-dict(label/
    risk_multiplier/reasons)만 뽑는다. 파일이 없거나, JSON 파싱에 실패하거나,
    해당 시장 sub-dict가 없으면 `None`."""
    path = root / "data" / "state" / "regime.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = (payload.get("markets") or {}).get(market)
    if not isinstance(state, dict) and market == "US":
        state = payload if "label" in payload else None
    return state if isinstance(state, dict) else None
