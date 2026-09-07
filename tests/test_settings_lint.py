"""설정 린트(2026-09-07 팀장 결정 ⑦): NO_GO 근거를 단 활성 전략은 판정일(review_by)이 있어야 한다."""
import datetime as dt

import yaml


def _strategies():
    return yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))["strategies"]


def test_enabled_no_go_lanes_have_review_by():
    missing = []
    for sid, c in _strategies().items():
        if not (isinstance(c, dict) and c.get("enabled")):
            continue
        v = c.get("validation") or {}
        if "NO_GO" in str(v.get("evidence") or "") and not v.get("review_by"):
            missing.append(sid)
    assert missing == [], f"NO_GO 근거인데 review_by 없음: {missing}"


def test_review_by_dates_parse_and_are_future_or_recent():
    for sid, c in _strategies().items():
        rb = ((c or {}).get("validation") or {}).get("review_by")
        if rb:
            d = dt.date.fromisoformat(str(rb))
            assert d >= dt.date(2026, 9, 7), (sid, rb)


def test_scalp_trend_gate_is_block_on_both_ab_lanes():
    st = _strategies()
    assert st["scalp_1m"]["params"]["trend_gate_mode"] == "block"
    assert st["scalp_1m_cat"]["params"]["trend_gate_mode"] == "block"  # 앵커 공유
