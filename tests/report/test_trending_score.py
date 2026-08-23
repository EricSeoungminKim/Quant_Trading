from datetime import date, datetime, timedelta
from pathlib import Path

from quant.core.report_clock import KST
from quant.collect.snapshot import save_snapshot
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.trending_score import (
    relative_volume,
    score_all,
    symbol_ranking_info,
    trending_score,
)

_AT = datetime(2026, 8, 12, 8, 0, tzinfo=KST)


def _item(rank, symbol, amount=1000):
    return {"rank": rank, "symbol": symbol, "price": 100.0, "change_pct": 1.0,
            "trading_amount": amount}


# ── symbol_ranking_info ──────────────────────────────────────────────────


def test_symbol_ranking_info_collects_ranks_across_boards():
    boards = {
        "거래대금": [_item(1, "005930", 5000)],
        "상승률": [_item(4, "005930")],
    }
    ranks, amount, _ = symbol_ranking_info(boards, "005930")
    assert ranks == {"거래대금": 1, "상승률": 4}
    assert amount == 5000


def test_symbol_ranking_info_missing_symbol_returns_empty():
    ranks, amount, change = symbol_ranking_info({"거래대금": [_item(1, "005930")]}, "000660")
    assert ranks == {} and amount is None and change is None


def test_symbol_ranking_info_prefers_trading_amount_board_value():
    boards = {
        "상승률": [_item(1, "005930", 999)],
        "거래대금": [_item(5, "005930", 12345)],
    }
    _, amount, _ = symbol_ranking_info(boards, "005930")
    assert amount == 12345


def test_symbol_ranking_info_surfaces_change_pct():
    """방향을 버리면 아래 매수세 판정 자체를 할 수 없다 — 실제로 버리고 있었다."""
    boards = {"하락률": [{"rank": 2, "symbol": "058820", "price": 100.0,
                        "change_pct": -12.4, "trading_amount": 9000}]}
    _, _, change = symbol_ranking_info(boards, "058820")
    assert change == -12.4


# ── relative_volume ───────────────────────────────────────────────────────


def test_relative_volume_none_when_history_too_short():
    assert relative_volume(1000, [1000, 1000]) is None  # MIN_HISTORY_DAYS=3


def test_relative_volume_none_when_today_amount_missing():
    assert relative_volume(None, [1000, 1000, 1000]) is None


def test_relative_volume_computes_ratio_against_median():
    # 중앙값 1000, 오늘 3000 → 3.0배
    assert relative_volume(3000, [500, 1000, 1500]) == 3.0


def test_relative_volume_none_when_baseline_is_zero():
    assert relative_volume(1000, [0, 0, 0]) is None


# ── trending_score ────────────────────────────────────────────────────────


def test_no_evidence_is_neutral_with_no_factors():
    res = trending_score({}, None, [])
    assert res["score"] == 0 and res["score100"] == 50
    assert res["factors"] == [] and res["label"] == "중립"


def test_top_rank_scores_more_than_lower_rank():
    top = trending_score({"거래대금": 1}, None, [])
    lower = trending_score({"거래대금": 8}, None, [])
    assert top["score"] > lower["score"]


def test_multiple_boards_stack_additively():
    one_board = trending_score({"거래대금": 1}, None, [])
    two_boards = trending_score({"거래대금": 1, "상승률": 1}, None, [])
    assert two_boards["score"] == one_board["score"] * 2


def test_volume_surge_adds_points_and_is_reported():
    res = trending_score({}, 3000, [500, 1000, 1500])
    assert res["relative_volume"] == 3.0
    assert any(f["key"] == "rel_volume" for f in res["factors"])
    assert res["score"] > 0


def test_volume_below_baseline_adds_no_points():
    res = trending_score({}, 500, [1000, 1000, 1000])
    assert res["relative_volume"] == 0.5
    assert all(f["key"] != "rel_volume" for f in res["factors"])
    assert res["score"] == 0


def test_insufficient_baseline_omits_relative_volume_entirely():
    res = trending_score({}, 3000, [1000])
    assert res["relative_volume"] is None
    assert res["baseline_days"] == 1
    assert all(f["key"] != "rel_volume" for f in res["factors"])


def test_boards_field_echoes_input():
    res = trending_score({"거래대금": 2}, None, [])
    assert res["boards"] == {"거래대금": 2}


# ── 방향(매수세) ──────────────────────────────────────────────────────────
#
# 회귀: CMG제약이 하락률 보드에 오른 채 트렌딩 55점을 받았다. 롱 온리 전략에서
# 폭락 중인 종목에 가점을 주면, 리포트가 "지금 몰린다"며 매수 후보로 올린다.
# 거래가 몰리는 것과 **매수세**가 몰리는 것은 다르다 — 가격을 올리는 건 후자다.


def test_declining_stock_gets_no_attention_credit():
    """같은 거래대금 1위라도 하락 중이면 가점이 아니라 감점이다."""
    rising = trending_score({"거래대금": 1}, None, [], change_pct=+5.0)
    falling = trending_score({"거래대금": 1}, None, [], change_pct=-5.0)
    assert rising["score"] > 0
    assert falling["score"] < 0
    assert falling["score"] == -rising["score"]


def test_decliner_board_subtracts():
    res = trending_score({"하락률": 2}, None, [], change_pct=-12.4)
    assert res["score"] < 0
    assert res["score100"] < 50


def test_heavy_volume_while_declining_is_evidence_against():
    """하락 중 거래량 급증은 매도세다 — 관심의 크기가 감점을 키운다."""
    res = trending_score({"하락률": 1}, 3000, [500, 1000, 1500], change_pct=-15.0)
    assert res["relative_volume"] == 3.0
    assert res["score"] < trending_score({"하락률": 1}, None, [], change_pct=-15.0)["score"]


def test_cmg_regression_declining_stock_is_not_a_long_candidate():
    """실제 사례 재현 — 하락률 상위 + 거래대금 상위 + 거래 급증."""
    res = trending_score(
        {"하락률": 1, "거래대금": 3}, 4000, [500, 1000, 1500], change_pct=-18.0
    )
    assert res["score100"] < 50, "폭락 종목이 중립 이상을 받으면 매수 후보로 올라간다"
    assert "관심 저조" in res["label"] or res["label"] == "중립"


def test_gain_and_loss_of_equal_attention_are_symmetric():
    """부호만 다르고 크기는 같다 — 방향이 관심의 부호를 정한다는 규칙 그대로."""
    up = trending_score({"거래대금": 1}, 3000, [500, 1000, 1500], change_pct=+9.0)
    down = trending_score({"거래대금": 1}, 3000, [500, 1000, 1500], change_pct=-9.0)
    assert up["score"] == -down["score"]
    assert up["score100"] + down["score100"] == 100


def test_unknown_direction_keeps_attention_positive():
    """방향 미상은 하락의 증거가 아니다 — 랭킹 밖 종목은 change_pct 자체가 없다."""
    known = trending_score({"거래대금": 1}, None, [], change_pct=+1.0)
    unknown = trending_score({"거래대금": 1}, None, [], change_pct=None)
    assert unknown["score"] == known["score"] > 0


def test_flat_price_is_not_treated_as_declining():
    res = trending_score({"거래대금": 1}, None, [], change_pct=0.0)
    assert res["score"] > 0


def test_direction_is_reported_for_audit():
    res = trending_score({"하락률": 1}, None, [], change_pct=-7.5)
    assert res["change_pct"] == -7.5
    assert any("하락" in f["text"] for f in res["factors"])


def test_deterministic_same_inputs_same_output():
    a = trending_score({"거래대금": 1, "상승률": 5}, 3000, [500, 1000, 1500])
    b = trending_score({"거래대금": 1, "상승률": 5}, 3000, [500, 1000, 1500])
    assert a == b


# ── score_all (integration with rank_history) ──────────────────────────────


def _rankings_snapshot(market, session_date, boards):
    return Snapshot(
        schema_version=SCHEMA_VERSION, market=market, session_date=session_date,
        generated_at=_AT,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=True, data={"boards": boards}, error=None,
                url="https://x", fetched_at=_AT, latency_ms=1,
            ),
        },
    )


def test_score_all_pulls_baseline_from_saved_snapshots(tmp_path: Path):
    today = date(2026, 8, 12)
    for back, amount in [(1, 1000), (2, 1000), (3, 1000)]:
        save_snapshot(
            _rankings_snapshot("KR", today - timedelta(days=back), {"거래대금": [_item(1, "005930", amount)]}),
            tmp_path,
        )
    cont = {"005930": {"name": "삼성전자"}}
    today_boards = {"거래대금": [_item(1, "005930", 5000)]}
    result = score_all(cont, today_boards, "KR", today, tmp_path)
    assert result["005930"]["relative_volume"] == 5.0
    assert result["005930"]["baseline_days"] == 3


def test_score_all_symbol_absent_from_ranking_is_neutral(tmp_path: Path):
    cont = {"003540": {"name": "대신증권"}}
    result = score_all(cont, {}, "KR", date(2026, 8, 12), tmp_path)
    assert result["003540"]["score100"] == 50
    assert result["003540"]["factors"] == []
