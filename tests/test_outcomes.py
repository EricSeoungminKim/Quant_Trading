"""전방 수익률 채우기 — Phase 7.2 배선. 순수 함수, 네트워크 없음.

## 왜 "과거 가격 조회"가 아니라 "매일 오늘 종가로 채우기"인가

D+1 수익률을 계산하려면 D+1 의 종가가 필요하다. 그걸 **나중에 과거 시세로 조회**하려면
일봉 히스토리 API 가 종목마다 필요하고, 우리 히스토리는 몇 종목만 덮는다(실측: EC2
parquet 4종목).

대신 **매일 돌면서 그날 만기가 된 지평만 채운다.** D+1 은 D 다음 거래일에 채우고,
D+5 는 5거래일 뒤에 채운다 — 그때는 **오늘 종가**만 있으면 되고, 그건 이미 리포트가
매일 받는 값이다. 과거 조회가 필요 없어진다.

## 거래일 근사와 그 한계

공휴일 달력이 없으므로 **평일 수로 센다.** 한국·미국 공휴일에 하루씩 밀릴 수 있다.
그 사실을 숨기지 않고, 채운 행에 **실제 기준 날짜(`asof`)를 함께 남긴다** — 나중에
"D+5 라고 적힌 게 실제로 며칠 뒤였나"를 되짚을 수 있어야 한다.
(`backfill._find_gaps` 도 같은 근사를 쓰고 같은 이유로 주석에 밝혀 두었다.)
"""
from __future__ import annotations

import pytest

from quant.control.outcomes import apply_outcome, due_horizons, pending_symbols


# ── 오늘 만기가 된 지평 ───────────────────────────────────────────────────

def test_next_business_day_is_horizon_one():
    # 2026-08-13(목) 선정 → 2026-08-14(금) 이 D+1
    assert due_horizons("2026-08-13", "2026-08-14") == [1]


def test_weekend_is_skipped():
    """금요일 선정의 D+1 은 토요일이 아니라 **다음 월요일**이다.

    달력일로 세면 가격이 없는 날을 기준으로 삼아 수익률이 통째로 빈다.
    """
    assert due_horizons("2026-08-14", "2026-08-15") == []   # 토
    assert due_horizons("2026-08-14", "2026-08-16") == []   # 일
    assert due_horizons("2026-08-14", "2026-08-17") == [1]  # 월


def test_five_business_days_later_is_horizon_five():
    # 2026-08-14(금) + 5영업일 = 2026-08-21(금)
    assert due_horizons("2026-08-14", "2026-08-21") == [5]


def test_no_horizon_due_returns_empty():
    assert due_horizons("2026-08-14", "2026-08-18") == []


def test_selection_day_itself_is_not_a_horizon():
    """선정 당일 종가는 **기준가**다 — 수익률이 아니다."""
    assert due_horizons("2026-08-14", "2026-08-14") == []


def test_past_dates_never_produce_horizons():
    assert due_horizons("2026-08-14", "2026-08-13") == []


# ── 어떤 종목의 시세가 필요한가 ───────────────────────────────────────────

def test_only_symbols_with_a_due_horizon_are_fetched():
    """시세 조회는 네트워크다 — 필요 없는 종목까지 부르면 레이트 리밋에 걸린다."""
    rows = [
        {"date": "2026-08-13", "symbol": "005930", "attributes": {"close": 71000.0}},
        {"date": "2026-08-11", "symbol": "000660", "attributes": {"close": 200000.0}},
    ]

    assert pending_symbols(rows, today="2026-08-14") == {"005930"}


def test_rows_already_filled_for_that_horizon_are_not_refetched():
    rows = [{"date": "2026-08-13", "symbol": "005930",
             "attributes": {"close": 71000.0}, "outcome_d1_bps": 120.0}]

    assert pending_symbols(rows, today="2026-08-14") == set()


# ── 행 갱신 ───────────────────────────────────────────────────────────────

def _row() -> dict:
    return {"date": "2026-08-13", "market": "KR", "symbol": "005930",
            "attributes": {"close": 100.0}}


def test_apply_outcome_writes_bps_and_the_asof_date():
    """**`asof` 를 남기는 이유**: 평일 근사라 "D+1"이 실제로 며칠 뒤였는지 되짚어야
    한다. 근사를 쓰면서 그 사실을 기록하지 않으면 나중에 검산이 불가능하다."""
    row = apply_outcome(_row(), horizon=1, close_now=101.0, asof="2026-08-14")

    assert row["outcome_d1_bps"] == pytest.approx(100.0)
    assert row["outcome_d1_asof"] == "2026-08-14"


def test_missing_price_leaves_the_row_untouched_not_zero():
    """시세를 못 구했으면 **아무것도 쓰지 않는다.** 0을 쓰면 조회 실패가 "본전"으로
    영구히 굳고, 다시 시도할 기회도 사라진다."""
    row = apply_outcome(_row(), horizon=1, close_now=None, asof="2026-08-14")

    assert "outcome_d1_bps" not in row
    assert not row.get("outcome_filled")


def test_outcome_filled_only_after_the_longest_horizon():
    """`outcome_filled` 은 "더 채울 게 없다"는 뜻이다 — D+1 만 채우고 세우면
    D+5·D+20 이 영영 안 채워진다(pending_outcomes 가 건너뛴다)."""
    row = apply_outcome(_row(), horizon=1, close_now=101.0, asof="2026-08-14")
    assert not row.get("outcome_filled")

    row = apply_outcome(row, horizon=5, close_now=105.0, asof="2026-08-21")
    assert not row.get("outcome_filled")

    row = apply_outcome(row, horizon=20, close_now=110.0, asof="2026-09-11")
    assert row["outcome_filled"] is True


def test_apply_outcome_does_not_mutate_the_input():
    original = _row()

    apply_outcome(original, horizon=1, close_now=101.0, asof="2026-08-14")

    assert "outcome_d1_bps" not in original


def test_zero_base_close_is_refused():
    """기준가 0 이면 수익률이 정의되지 않는다 — inf 를 쓰지 않는다."""
    row = apply_outcome({"date": "2026-08-13", "symbol": "X", "attributes": {"close": 0.0}},
                        horizon=1, close_now=101.0, asof="2026-08-14")

    assert "outcome_d1_bps" not in row


# ── 시세 추출 (실제 반환 형태) ────────────────────────────────────────────

def test_closes_from_quotes_reads_the_close_field():
    """**`fetch_symbol_quotes` 는 `{심볼: {"close": ...}}` 를 돌려준다.**

    2026-08-14 실측 버그: 내 배선이 `float(v)` 로 dict 를 변환하려다 예외가 났고,
    내 except 가 그걸 삼켜 `quotes_fetched: 0` 만 남았다 — 돌았는데 아무 일도 안 하는
    그 패턴이다. 반환 형태를 테스트로 못 박는다.
    """
    from quant.control.outcomes import closes_from_quotes

    got = closes_from_quotes({"AAPL": {"close": 305.26, "prev": 302.25},
                              "MSFT": {"close": 496.88}})

    assert got == {"AAPL": 305.26, "MSFT": 496.88}


def test_quotes_without_a_close_are_dropped():
    """close 가 없으면 그 종목은 시세를 못 구한 것이다 — 0 으로 위장하지 않는다."""
    from quant.control.outcomes import closes_from_quotes

    assert closes_from_quotes({"AAPL": {"prev": 1.0}, "MSFT": {}, "X": None}) == {}


def test_kr_symbols_need_a_yahoo_mapping():
    """KR 6자리 코드는 야후 심볼이 아니다 — 매핑 없이 부르면 조용히 빈 결과가 온다.

    내일 KR 선정의 D+1 이 만기가 되면 같은 방식으로 실패할 예정이었다.
    """
    from quant.control.outcomes import split_for_quotes

    us, kr = split_for_quotes({"AAPL", "005930"})

    assert us == {"AAPL"}
    assert kr == {"005930"}
