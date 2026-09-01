"""quant.analyze.money_flow — 자금 흐름 판정 규칙 + 섹터 기울기 + 정합 테스트.
순수 함수만 다룬다(네트워크 없음)."""
from __future__ import annotations

import json

import pytest

from quant.adapters.macro.fred import DEFAULT_LEDGER_PATH as _ADAPTER_LEDGER_PATH
from quant.analyze.money_flow import (
    DEFAULT_LEDGER_PATH,
    SeriesSnapshot,
    analyze_money_flow,
    build_snapshots,
    format_money_flow_text,
    judge_cash_flow,
    judge_money_flow,
    load_ledger,
    sector_tilt,
    sector_tilt_for_symbol,
    series_snapshot,
)


def test_default_ledger_path_matches_adapter():
    """analyze는 adapters를 임포트할 수 없어(평면 규칙) 경로 문자열을 따로
    든다 — 값이 갈리면 여기서 잡는다."""
    assert DEFAULT_LEDGER_PATH == _ADAPTER_LEDGER_PATH


def _write_ledger(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------- load_ledger


def test_load_ledger_groups_by_series_sorted(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [
        {"date": "2026-08-25", "series": "us_10y", "value": 4.70},
        {"date": "2026-08-20", "series": "us_10y", "value": 4.60},
        {"date": "2026-08-24", "series": "vix", "value": 15.2},
    ])
    out = load_ledger(path)
    assert out["us_10y"] == [("2026-08-20", 4.60), ("2026-08-25", 4.70)]
    assert out["vix"] == [("2026-08-24", 15.2)]


def test_load_ledger_missing_file_returns_empty(tmp_path):
    assert load_ledger(tmp_path / "nope.jsonl") == {}


def test_load_ledger_skips_malformed_lines(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    path.write_text(
        "not json\n"
        '{"date": "2026-08-24", "series": "vix", "value": 15.2}\n'
        '{"date": "2026-08-25", "series": "vix"}\n',  # value 없음 — 건너뜀
        encoding="utf-8",
    )
    out = load_ledger(path)
    assert out["vix"] == [("2026-08-24", 15.2)]


# --------------------------------------------------------------------- series_snapshot (정합)


def test_series_snapshot_computes_1_5_20_obs_changes():
    rows = [(f"2026-08-{d:02d}", float(100 + d)) for d in range(1, 26)]  # 25 관측치
    snap = series_snapshot("us_10y", rows)
    assert snap.date == "2026-08-25"
    assert snap.value == pytest.approx(125.0)
    assert snap.chg_1d == pytest.approx(1.0)
    assert snap.chg_5d == pytest.approx(5.0)
    assert snap.chg_20d == pytest.approx(20.0)
    assert snap.direction_5d == "↑"


def test_series_snapshot_insufficient_history_returns_none_changes():
    rows = [("2026-08-24", 4.60), ("2026-08-25", 4.66)]
    snap = series_snapshot("us_10y", rows)
    assert snap.chg_1d == pytest.approx(0.06)
    assert snap.chg_5d is None
    assert snap.chg_20d is None


def test_series_snapshot_empty_rows_returns_none():
    assert series_snapshot("us_10y", []) is None


def test_build_snapshots_skips_missing_series(tmp_path):
    """발표 주기가 다른 시리즈들이 섞여도(2026-08-24 usdkrw만 있고 나머지는
    없음) 각 시리즈는 독립적으로, 있는 것만 계산된다 — 정합 문제로 전체가
    죽지 않는다."""
    path = tmp_path / "macro_rates.jsonl"
    _write_ledger(path, [{"date": "2026-08-24", "series": "usdkrw", "value": 1380.0}])
    out = build_snapshots(path)
    assert set(out.keys()) == {"usdkrw"}
    assert out["usdkrw"].value == 1380.0


# --------------------------------------------------------------------- judge_money_flow


def test_judge_money_flow_rate_up_equity_down():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "미국 10년물", "2026-08-25", 4.70,
                                          chg_1d=0.02, chg_5d=0.15, chg_20d=0.3, direction_5d="↑")}
    result = judge_money_flow(snapshots, equity_change_pct=-1.2, equity_label="QQQ")
    assert result["label"] == "긴축 부담 — 채권·주식 동반 이탈"
    assert any("10년물" in r for r in result["reasons"])
    assert any("QQQ" in r for r in result["reasons"])


def test_judge_money_flow_rate_down_equity_up():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "미국 10년물", "2026-08-25", 4.40,
                                          chg_1d=-0.02, chg_5d=-0.15, chg_20d=-0.3, direction_5d="↓")}
    result = judge_money_flow(snapshots, equity_change_pct=1.0)
    assert result["label"] == "위험자산 선호(리스크온)"


def test_judge_money_flow_rate_up_equity_up_growth_optimism():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "", "d", 4.70,
                                          chg_1d=0.02, chg_5d=0.15, chg_20d=0.3, direction_5d="↑")}
    result = judge_money_flow(snapshots, equity_change_pct=0.8)
    assert result["label"] == "성장 기대 우위(경기 낙관)"


def test_judge_money_flow_rate_down_equity_down_growth_worry():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "", "d", 4.40,
                                          chg_1d=-0.02, chg_5d=-0.15, chg_20d=-0.3, direction_5d="↓")}
    result = judge_money_flow(snapshots, equity_change_pct=-0.9)
    assert result["label"] == "경기 둔화 우려(안전자산 선호)"


def test_judge_money_flow_no_equity_data_defers():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "", "d", 4.70,
                                          chg_1d=0.02, chg_5d=0.15, chg_20d=0.3, direction_5d="↑")}
    result = judge_money_flow(snapshots, equity_change_pct=None)
    assert "보류" in result["label"]
    assert result["equity_direction"] is None


def test_judge_money_flow_missing_rate_series():
    result = judge_money_flow({}, equity_change_pct=1.5)
    assert result["rate_direction"] == "→"
    assert "없음" in result["reasons"][0]


# --------------------------------------------------------------------- judge_cash_flow


def test_judge_cash_flow_calm_vix_and_dollar_weak():
    snapshots = {
        "vix": SeriesSnapshot("vix", "VIX", "d", 13.0, 0, 0, 0, "→"),
        "dollar_index": SeriesSnapshot("dollar_index", "달러", "d", 99.0, chg_1d=0, chg_5d=-2.0,
                                       chg_20d=0, direction_5d="↓"),
    }
    result = judge_cash_flow(snapshots)
    assert "안정" in result["label"]
    assert "약세" in result["label"]


def test_judge_cash_flow_stress_vix():
    snapshots = {"vix": SeriesSnapshot("vix", "VIX", "d", 25.0, 0, 0, 0, "→")}
    result = judge_cash_flow(snapshots)
    assert "스트레스" in result["label"]
    assert "25.0" in result["reasons"][0]


def test_judge_cash_flow_inverted_curve():
    snapshots = {"term_spread_10y2y": SeriesSnapshot("term_spread_10y2y", "", "d", -0.2, 0, 0, 0, "→")}
    result = judge_cash_flow(snapshots)
    assert "역전" in result["label"]


def test_judge_cash_flow_no_data():
    result = judge_cash_flow({})
    assert "판정 불가" in result["label"]


# --------------------------------------------------------------------- sector_tilt


def test_sector_tilt_oil_up_activates_kr_and_us():
    snapshots = {"oil_wti": SeriesSnapshot("oil_wti", "WTI", "d", 103.0, chg_1d=0, chg_5d=6.0,
                                           chg_20d=0, direction_5d="↑")}  # 100→103 = +3%
    tilt = sector_tilt(snapshots)
    assert tilt["KR"]["석유와가스"]["score"] == 2
    assert tilt["KR"]["항공사"]["score"] == -2
    assert tilt["US"]["XLE(에너지)"]["score"] == 2


def test_sector_tilt_oil_down_flips_signs():
    snapshots = {"oil_wti": SeriesSnapshot("oil_wti", "WTI", "d", 94.0, chg_1d=0, chg_5d=-6.0,
                                           chg_20d=0, direction_5d="↓")}  # 100→94 = -6%
    tilt = sector_tilt(snapshots)
    assert tilt["KR"]["석유와가스"]["score"] == -2
    assert tilt["KR"]["항공사"]["score"] == 2


def test_sector_tilt_rate_up_banks_positive_reits_negative():
    snapshots = {"us_10y": SeriesSnapshot("us_10y", "", "d", 4.70, chg_1d=0, chg_5d=0.15,
                                          chg_20d=0, direction_5d="↑")}
    tilt = sector_tilt(snapshots)
    assert tilt["KR"]["은행"]["score"] == 2
    assert tilt["US"]["XLRE(리츠)"]["score"] == -2


def test_sector_tilt_below_threshold_stays_empty():
    """5일 변화가 임계값 미만이면(노이즈) 어느 드라이버도 활성화되지 않는다."""
    snapshots = {"oil_wti": SeriesSnapshot("oil_wti", "WTI", "d", 100.5, chg_1d=0, chg_5d=0.5,
                                           chg_20d=0, direction_5d="↑")}  # +0.5%, 임계 3% 미달
    assert sector_tilt(snapshots) == {}


def test_sector_tilt_combined_drivers_sum_and_clip():
    """유가↑(반도체 언급 없음)와 금리↑(반도체 -1) 둘 다 활성화되면 반도체
    점수가 합산되고 -2..2로 잘린다."""
    snapshots = {
        "us_10y": SeriesSnapshot("us_10y", "", "d", 4.70, chg_1d=0, chg_5d=0.15, chg_20d=0, direction_5d="↑"),
        "dollar_index": SeriesSnapshot("dollar_index", "", "d", 102.0, chg_1d=0, chg_5d=2.0,
                                       chg_20d=0, direction_5d="↑"),  # 100→102 = +2%
    }
    tilt = sector_tilt(snapshots)
    # 금리↑: 반도체 -1, 달러↑: 반도체 +1 → 합산 0
    assert tilt["KR"]["반도체와반도체장비"]["score"] == 0
    assert len(tilt["KR"]["반도체와반도체장비"]["why"]) == 2


# --------------------------------------------------------------------- sector_tilt_for_symbol (§4 연동)


def test_sector_tilt_for_symbol_known_sector():
    tilt_kr = {"은행": {"score": 2, "why": ["예대마진 확대"]}}
    result = sector_tilt_for_symbol("은행", tilt_kr)
    assert result == (2, "예대마진 확대")


def test_sector_tilt_for_symbol_unknown_sector_returns_none_not_penalized():
    """섹터를 모르는 종목(sector_map.json에 없음)은 None — 호출부가 0점(불이익
    없음)으로 취급해야 한다."""
    assert sector_tilt_for_symbol(None, {"은행": {"score": 2, "why": []}}) is None
    assert sector_tilt_for_symbol("모르는업종", {"은행": {"score": 2, "why": []}}) is None


# --------------------------------------------------------------------- analyze_money_flow + format


def test_analyze_money_flow_end_to_end(tmp_path):
    path = tmp_path / "macro_rates.jsonl"
    rows = []
    for d in range(1, 26):
        rows.append({"date": f"2026-08-{d:02d}", "series": "us_10y", "value": 4.50 + d * 0.01})
        rows.append({"date": f"2026-08-{d:02d}", "series": "vix", "value": 14.0})
        rows.append({"date": f"2026-08-{d:02d}", "series": "oil_wti", "value": 80.0 + d * 0.5})
    _write_ledger(path, rows)

    result = analyze_money_flow(path, equity_change_pct=-0.8, equity_label="KODEX200")
    assert "us_10y" in result["series"]
    assert result["flow"]["label"]
    assert result["cash"]["label"]
    assert "KR" in result["sector_tilt"]

    text = format_money_flow_text(result)
    assert result["flow"]["label"] in text
    assert result["cash"]["label"] in text


def test_analyze_money_flow_missing_ledger_returns_empty_series(tmp_path):
    result = analyze_money_flow(tmp_path / "nope.jsonl")
    assert result["series"] == {}
    assert result["sector_tilt"] == {}
