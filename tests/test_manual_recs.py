"""수동 계좌 추천 레인(quant/analyze/manual_recs.py) 회귀 테스트.

설계는 그 모듈 docstring 참고. 여기서는:
- RSI(2) 재구현이 quant.trade.indicators.rsi()와 같은 값을 내는지(중복 구현 대조)
- 외국인 적립 추세(a)/종가배팅(b)/RSI(2) 눌림(c)/오버나이트 드리프트(d) 각 생산자가
  올바른 후보만 뽑는지
- 선정 원장 행 변환(close/close_date 포함 여부)과 append()를 통한 기록
- 텔레그램 메시지 렌더링(8건 상한)
- 성적표 표본 부족 가드(n<30)
를 검증한다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant.analyze import foreign_trend, manual_recs
from quant.control import selections
from quant.trade.indicators import rsi as _trade_rsi


# --------------------------------------------------------------------------- RSI(2) 중복 구현 대조


def test_wilder_rsi_matches_trade_indicators_rsi():
    """analyze 는 trade 를 임포트할 수 없어 RSI 를 재구현했다(모듈 docstring) —
    두 구현이 같은 값을 내는지 여기서 대조한다."""
    closes = pd.Series([100.0, 102.0, 101.0, 99.0, 98.0, 103.0, 105.0, 104.0, 107.0, 106.0])
    got = manual_recs._wilder_rsi(closes, period=2)
    want = _trade_rsi(closes, period=2)
    pd.testing.assert_series_equal(got, want, check_names=False)


# --------------------------------------------------------------------------- (a) 외국인 적립 추세


def _write_flow(path: Path, symbol: str, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        import json
        for r in rows:
            f.write(json.dumps({"symbol": symbol, **r}, ensure_ascii=False) + "\n")


def test_foreign_accumulate_recs_only_includes_inflow_label(tmp_path):
    flow_path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    # 이탈 후 재유입, 재유입 합이 이탈을 넘음 → LABEL_INFLOW
    _write_flow(flow_path, "000001", [
        {"date": "2026-08-25", "foreign_net": -100},
        {"date": "2026-08-26", "foreign_net": -50},
        {"date": "2026-08-27", "foreign_net": 200},
    ])
    # 연속 순매도만 2일 이상 → LABEL_OUTFLOW_TREND (추천 대상 아님)
    _write_flow(flow_path, "000002", [
        {"date": "2026-08-25", "foreign_net": 100},
        {"date": "2026-08-26", "foreign_net": -50},
        {"date": "2026-08-27", "foreign_net": -60},
    ])
    _write_daily_closes(tmp_path / "data" / "history" / "000001" / "1d", [70000.0])

    recs = manual_recs.foreign_accumulate_recs(tmp_path)

    assert {r["symbol"] for r in recs} == {"000001"}
    rec = recs[0]
    assert rec["kind"] == "frgn_accumulate"
    assert rec["market"] == "KR"
    assert rec["horizon"] == "D+20"
    assert "FRGN_EXIT" in rec["invalidation"]
    # F3(2026-09-03) — 누적(residual)만이 아니라 classify()가 실제로 쓴 최근
    # run(재유입 200주, run 길이 1일)의 숫자가 문구에 그대로 나와야 한다.
    assert "최근 1일 순매수 +200주" in rec["reason"]
    assert "3일 누계 +50주" in rec["reason"]  # -100-50+200=+50
    assert rec["ref_price"] == pytest.approx(70000.0)  # 로컬 일봉에서 조회
    assert rec["price_source"] == "history"


def test_foreign_accumulate_recs_drops_symbol_without_any_price(tmp_path):
    """F1(2026-09-03, 감사 B8) — INFLOW 라벨이어도 기준가를 못 구하면(로컬
    일봉도 없고 price_of도 못 찾으면) 추천 자체를 드롭한다. 채점 불가능한
    추천을 텔레그램으로 보내지 않는다는 원칙."""
    flow_path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    _write_flow(flow_path, "000001", [
        {"date": "2026-08-25", "foreign_net": -100},
        {"date": "2026-08-26", "foreign_net": 200},
    ])

    recs = manual_recs.foreign_accumulate_recs(tmp_path)

    assert recs == []


def test_foreign_accumulate_recs_falls_back_to_injected_price_of(tmp_path):
    """F1 — 로컬 일봉이 없어도 주입된 `price_of`(cli.py가 Toss 배치 시세로
    조립하는 것의 대역)가 가격을 주면 드롭되지 않고 price_source가 그 출처를
    그대로 반영한다."""
    flow_path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    _write_flow(flow_path, "000001", [
        {"date": "2026-08-25", "foreign_net": -100},
        {"date": "2026-08-26", "foreign_net": 200},
    ])

    def _toss_only(sym: str):
        return (12345.0, "2026-09-03", "toss") if sym == "000001" else None

    recs = manual_recs.foreign_accumulate_recs(tmp_path, price_of=_toss_only)

    assert len(recs) == 1
    assert recs[0]["ref_price"] == pytest.approx(12345.0)
    assert recs[0]["price_source"] == "toss"


def test_foreign_accumulate_recs_respects_explicit_symbol_list(tmp_path):
    flow_path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    _write_flow(flow_path, "000001", [
        {"date": "2026-08-25", "foreign_net": -100},
        {"date": "2026-08-26", "foreign_net": 200},
    ])
    _write_flow(flow_path, "000003", [
        {"date": "2026-08-25", "foreign_net": -100},
        {"date": "2026-08-26", "foreign_net": 200},
    ])
    _write_daily_closes(tmp_path / "data" / "history" / "000001" / "1d", [70000.0])
    _write_daily_closes(tmp_path / "data" / "history" / "000003" / "1d", [1000.0])

    recs = manual_recs.foreign_accumulate_recs(tmp_path, symbols=["000001"])

    assert {r["symbol"] for r in recs} == {"000001"}


# --------------------------------------------------------------------------- (b) 종가배팅


def _write_watchlist(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"symbols": entries}, allow_unicode=True), encoding="utf-8")


def test_close_bet_recs_filters_by_tag_and_falls_back_without_report(tmp_path):
    _write_watchlist(tmp_path / "data" / "watchlist.yaml", [
        {"symbol": "066570", "name": "LG전자", "tags": ["CLOSE_BET", "EVENT"]},
        {"symbol": "000660", "name": "SK하이닉스", "tags": ["EVENT"]},  # 태그 없음 — 제외
    ])
    _write_daily_closes(tmp_path / "data" / "history" / "066570" / "1d", [95000.0])

    recs = manual_recs.close_bet_recs(tmp_path, date(2026, 9, 2))

    assert {r["symbol"] for r in recs} == {"066570"}
    rec = recs[0]
    assert rec["name"] == "LG전자"
    assert rec["horizon"] == "D+5"
    assert "리포트 근거 조회 불가" in rec["reason"]
    assert "-1%" in rec["invalidation"]
    assert rec["price_source"] == "history"


def test_close_bet_recs_drops_symbol_without_any_price(tmp_path):
    """F1(2026-09-03) — close_bet도 frgn_accumulate와 같은 결함(감사 B8)을
    공유한다: CLOSE_BET 태그가 있어도 로컬 일봉이 없으면 드롭한다."""
    _write_watchlist(tmp_path / "data" / "watchlist.yaml", [
        {"symbol": "066570", "name": "LG전자", "tags": ["CLOSE_BET"]},
    ])

    assert manual_recs.close_bet_recs(tmp_path, date(2026, 9, 2)) == []


def test_close_bet_recs_uses_report_reasons_when_available(tmp_path):
    _write_watchlist(tmp_path / "data" / "watchlist.yaml", [
        {"symbol": "066570", "name": "LG전자", "tags": ["CLOSE_BET"]},
    ])
    _write_daily_closes(tmp_path / "data" / "history" / "066570" / "1d", [95000.0])
    out_dir = tmp_path / "out" / "2026" / "09" / "02"
    out_dir.mkdir(parents=True, exist_ok=True)
    import json
    (out_dir / "KR_close_engine.json").write_text(json.dumps({
        "close_bet_view": [
            {"symbol": "066570", "reasons": ["당일 +4.2%", "외국인 매수 시그널(재유입)"]},
        ],
    }), encoding="utf-8")

    recs = manual_recs.close_bet_recs(tmp_path, date(2026, 9, 2))

    assert len(recs) == 1
    assert "당일 +4.2%" in recs[0]["reason"]
    assert "외국인 매수 시그널(재유입)" in recs[0]["reason"]


def test_close_bet_recs_empty_watchlist_returns_empty(tmp_path):
    assert manual_recs.close_bet_recs(tmp_path, date(2026, 9, 2)) == []


# --------------------------------------------------------------------------- 일봉 parquet 픽스처


def _write_daily_closes(history_dir: Path, closes: list[float], start: str = "2025-11-01") -> None:
    """`data/history/{symbol}/1d/YYYY/MM.parquet` 형태로 종가만 있는 일봉을 쓴다
    (tests/test_opendays.py의 _write_daily와 같은 관례 — 04:00 UTC 인덱스,
    영업일 근사로 순차 날짜를 매긴다)."""
    dates = pd.bdate_range(start=start, periods=len(closes), tz="UTC") + pd.Timedelta(hours=4)
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(closes),
    }, index=dates)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = history_dir / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


# --------------------------------------------------------------------------- (c) RSI(2) 눌림


def test_rsi2_dip_recs_fires_on_oversold_above_trend(tmp_path):
    # 250 영업일 완만한 상승(추세 위 확보) 뒤 마지막 이틀 급락(RSI(2) 과매도 유도).
    closes = [100.0 + 0.5 * i for i in range(248)]
    closes += [closes[-1] - 5, closes[-1] - 10]
    _write_daily_closes(tmp_path / "data" / "history" / "069500" / "1d", closes)

    recs = manual_recs.rsi2_dip_recs(tmp_path, ["069500"])

    assert len(recs) == 1
    rec = recs[0]
    assert rec["symbol"] == "069500"
    assert rec["kind"] == "rsi2_dip"
    assert rec["horizon"] == "D+5"
    assert rec["ref_price"] == pytest.approx(closes[-1])
    assert "RSI(2)" in rec["reason"]
    assert "5거래일" in rec["invalidation"]


def test_rsi2_dip_recs_skips_flat_series_without_signal(tmp_path):
    closes = [100.0] * 210
    _write_daily_closes(tmp_path / "data" / "history" / "069500" / "1d", closes)

    assert manual_recs.rsi2_dip_recs(tmp_path, ["069500"]) == []


def test_rsi2_dip_recs_skips_symbol_with_insufficient_history(tmp_path):
    closes = [100.0 + i for i in range(20)]  # 200일 SMA 워밍업에 한참 못 미침
    _write_daily_closes(tmp_path / "data" / "history" / "069500" / "1d", closes)

    assert manual_recs.rsi2_dip_recs(tmp_path, ["069500"]) == []


def test_rsi2_dip_recs_skips_symbol_with_no_history_directory(tmp_path):
    assert manual_recs.rsi2_dip_recs(tmp_path, ["999999"]) == []


# --------------------------------------------------------------------------- (d) 오버나이트 드리프트


def test_overnight_drift_recs_is_qqq_only_and_always_fires(tmp_path):
    _write_daily_closes(tmp_path / "data" / "history" / "QQQ" / "1d", [500.0, 501.0, 502.5])

    recs = manual_recs.overnight_drift_recs(tmp_path)

    assert [r["symbol"] for r in recs] == ["QQQ"]
    rec = recs[0]
    assert rec["market"] == "US"
    assert rec["ref_price"] == pytest.approx(502.5)
    assert "-3%" in rec["invalidation"]
    assert rec["horizon"] == "D+5"


def test_overnight_drift_recs_excludes_tqqq_even_without_data():
    """TQQQ 는 레버리지 갭 리스크로 의도적 제외(overnight_drift.py) — 데이터 유무와
    무관하게 절대 후보에 없다."""
    assert manual_recs._OVERNIGHT_DRIFT_SYMBOLS == ("QQQ",)


# --------------------------------------------------------------------------- 선정 원장 기록


def test_to_selection_rows_includes_close_only_when_ref_price_present():
    recs = [
        manual_recs._rec_row(
            symbol="005930", name="삼성전자", market="KR", kind="frgn_accumulate",
            reason="근거", ref_price=70000.0, ref_date="2026-09-01",
            invalidation="FRGN_EXIT", horizon="D+20",
        ),
        manual_recs._rec_row(
            symbol="000001", name=None, market="KR", kind="close_bet",
            reason="근거", ref_price=None, ref_date=None,
            invalidation="-1%", horizon="D+5",
        ),
    ]

    rows = manual_recs.to_selection_rows(recs, "2026-09-03")

    priced, unpriced = rows
    assert priced["close"] == 70000.0
    assert priced["close_date"] == "2026-09-01"
    assert priced["producer"] == manual_recs.PRODUCER
    assert priced["is_candidate"] is True
    assert priced["outcome_filled"] is False
    assert "close" not in unpriced
    assert "close_date" not in unpriced


def test_write_recs_appends_via_canonical_selections_writer(tmp_path):
    recs = [manual_recs._rec_row(
        symbol="005930", name="삼성전자", market="KR", kind="frgn_accumulate",
        reason="근거", ref_price=70000.0, ref_date="2026-09-01",
        invalidation="FRGN_EXIT", horizon="D+20",
    )]

    added = manual_recs.write_recs(recs, tmp_path, "2026-09-03")
    assert added == 1

    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    loaded = selections.load(path)
    assert len(loaded) == 1
    assert loaded[0]["producer"] == "manual_rec_v1"

    # 재실행해도 같은 (날짜,시장,종목,producer)는 중복되지 않는다(append() 자연키).
    added_again = manual_recs.write_recs(recs, tmp_path, "2026-09-03")
    assert added_again == 0
    assert len(selections.load(path)) == 1


def test_written_row_base_session_date_uses_close_date(tmp_path):
    """outcomes.base_session_date 가 close_date 를 우선한다 — D+N 채점이 실제
    기준가 날짜로 세션을 세는지 배선 확인(quant/control/outcomes.py)."""
    from quant.control.outcomes import base_session_date

    recs = [manual_recs._rec_row(
        symbol="005930", name=None, market="KR", kind="frgn_accumulate",
        reason="근거", ref_price=70000.0, ref_date="2026-08-28",
        invalidation="FRGN_EXIT", horizon="D+20",
    )]
    row = manual_recs.to_selection_rows(recs, "2026-09-01")[0]

    assert base_session_date(row) == "2026-08-28"


# --------------------------------------------------------------------------- 텔레그램 메시지


def test_render_telegram_message_empty_recs():
    msg = manual_recs.render_telegram_message([], "KR")
    assert "📌 수동 계좌 추천 (자동매매 아님)" in msg
    assert "후보 없음" in msg


def test_render_telegram_message_caps_at_eight_and_notes_the_rest():
    recs = [
        manual_recs._rec_row(
            symbol=f"{i:06d}", name=None, market="KR", kind="frgn_accumulate",
            reason="근거", ref_price=None, ref_date=None,
            invalidation="FRGN_EXIT", horizon="D+20",
        )
        for i in range(10)
    ]

    msg = manual_recs.render_telegram_message(recs, "KR")

    assert msg.count("[frgn_accumulate]") == 8
    assert "외 2건 생략" in msg


def test_render_telegram_message_no_price_text_is_not_duplicated():
    """F2(2026-09-03) — "기준가 {price}"의 {price} 자리에 "기준가 없음..."을
    또 넣어 "기준가 기준가 없음(...)"으로 중복 렌더링되던 결함."""
    recs = [manual_recs._rec_row(
        symbol="000001", name=None, market="KR", kind="frgn_accumulate",
        reason="근거", ref_price=None, ref_date=None,
        invalidation="FRGN_EXIT", horizon="D+20",
    )]

    msg = manual_recs.render_telegram_message(recs, "KR")

    assert "기준가 기준가" not in msg
    assert "기준가 없음(기준가 조회 실패)" in msg


def test_render_telegram_message_labels_toss_sourced_price_as_live():
    """F1 — price_source가 "toss"면 "종가"가 아니라 "실시간"으로 표시해
    로컬 일봉 종가와 혼동되지 않게 한다."""
    recs = [manual_recs._rec_row(
        symbol="000001", name=None, market="KR", kind="frgn_accumulate",
        reason="근거", ref_price=12345.0, ref_date="2026-09-03",
        invalidation="FRGN_EXIT", horizon="D+20", price_source="toss",
    )]

    msg = manual_recs.render_telegram_message(recs, "KR")

    assert "12,345(2026-09-03 실시간)" in msg
    assert "종가" not in msg


# --------------------------------------------------------------------------- 성적표


def test_scorecard_text_reports_insufficient_sample_below_30():
    rows = [
        {"producer": "manual_rec_v1", "outcome_d5_bps": 10.0}
        for _ in range(29)
    ]
    text = manual_recs.scorecard_text(rows)
    assert "판단 불가" in text
    assert "n=29" in text


def test_scorecard_text_reports_hit_rate_and_mean_bp_at_or_above_30():
    rows = (
        [{"producer": "manual_rec_v1", "outcome_d5_bps": 100.0} for _ in range(20)]
        + [{"producer": "manual_rec_v1", "outcome_d5_bps": -50.0} for _ in range(10)]
        # 다른 producer/미채움 행은 표본에서 제외된다.
        + [{"producer": "watch_scorer", "outcome_d5_bps": 999.0}]
        + [{"producer": "manual_rec_v1", "outcome_d5_bps": None}]
    )

    stats = manual_recs.scorecard_stats(rows)
    assert stats["n"] == 30
    assert stats["hit_rate"] == pytest.approx(20 / 30)
    assert stats["mean_bp"] == pytest.approx((100.0 * 20 - 50.0 * 10) / 30)

    text = manual_recs.scorecard_text(rows)
    assert "판단 불가" not in text
    assert "n=30" in text
    assert "67%" in text
