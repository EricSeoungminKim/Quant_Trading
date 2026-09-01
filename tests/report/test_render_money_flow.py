"""돈의 흐름 섹션(money_flow) — `render` 배선.

`{% if money_flow %}`는 값이 None이면 분기를 건너뛰므로, 실제로 채운 값으로
Jinja 블록이 문법 오류 없이 렌더되고 핵심 필드가 나타나는지를 본다
(`test_render_midterm.py`와 같은 관례). 판정 로직 자체는
`tests/test_money_flow.py`의 몫이다.
"""
from __future__ import annotations

from datetime import date, datetime

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.analyze.render import render

_AT = datetime(2026, 8, 31, 8, 0, tzinfo=KST)


def _snap(market: str = "KR") -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, date(2026, 8, 31), _AT, {})


def _money_flow_view() -> dict:
    return {
        "series": {
            "us_10y": {"label": "미국 10년물", "date": "2026-08-31", "value": 4.70,
                       "chg_1d": 0.02, "chg_5d": 0.15, "chg_20d": 0.3, "direction_5d": "↑"},
            "oil_wti": {"label": "WTI 유가", "date": "2026-08-31", "value": 84.0,
                        "chg_1d": 0.5, "chg_5d": 4.0, "chg_20d": 6.0, "direction_5d": "↑"},
        },
        "flow": {"label": "긴축 부담 — 채권·주식 동반 이탈",
                 "reasons": ["미국 10년물 5일 변화 +0.15%p", "KOSPI 당일 -0.80%"]},
        "cash": {"label": "VIX 안정 — 현금이 위험자산에 머문다", "reasons": ["VIX 14.0 (안정<15, 스트레스≥22)"]},
        "sector_tilt": {
            "석유와가스": {"score": 2, "why": ["정제마진 확대 — 원유는 이미 보유, 판가만 오른다"]},
            "항공사": {"score": -2, "why": ["항공유가 총원가의 20~30%를 차지 — 직접 비용 압박"]},
        },
        "prose": "유가와 금리 부담이 겹치며 리스크오프 흐름이 이어졌다.",
        "fallback_text": "긴축 부담 — 채권·주식 동반 이탈 · VIX 안정 — 현금이 위험자산에 머문다",
    }


def test_render_money_flow_section_appears_with_data():
    html = render(_snap(), money_flow=_money_flow_view())
    assert "돈의 흐름" in html
    assert "미국 10년물" in html
    assert "WTI 유가" in html
    assert "긴축 부담 — 채권·주식 동반 이탈" in html
    assert "석유와가스" in html
    assert "유가와 금리 부담이 겹치며 리스크오프 흐름이 이어졌다." in html


def test_render_money_flow_section_falls_back_to_deterministic_text_without_prose():
    view = _money_flow_view()
    view["prose"] = None
    html = render(_snap(), money_flow=view)
    assert view["fallback_text"] in html


def test_render_without_money_flow_omits_section():
    html = render(_snap(), money_flow=None)
    assert "돈의 흐름" not in html
