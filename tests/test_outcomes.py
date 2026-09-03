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

## 2026-09-03 감사 수리 (D1~D4)

`apply_outcome`의 `close_now` 인자가 `quote = (close, date)` 튜플로 바뀌었다 —
그 종가가 어느 거래일 것인지 모르면 같은 세션 재조회(D1의 가짜 0bp)를 구분할 수
없기 때문이다. `asof` 인자는 그대로 남는다(`close_report.matured_today`가
`outcome_dN_asof == today` 로 "오늘 채워졌나"를 판단하므로 계약을 안 바꿨다) —
`quote` 의 날짜는 D1 게이트에만 쓰인다.
"""
from __future__ import annotations

import pytest

from quant.control.outcomes import (
    apply_outcome,
    base_session_date,
    closes_from_quotes,
    due_horizons,
    horizon_status_counts,
    pending_symbols,
    split_for_quotes,
    to_yahoo_us_symbol,
)


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
    """grace 범위(기본 2영업일) 밖(age=4)이면 여전히 빈 목록 — grace_days 는
    무제한 재시도가 아니다."""
    assert due_horizons("2026-08-14", "2026-08-20") == []


def test_due_horizons_grace_covers_the_day_after_a_missed_horizon():
    """2026-08-26 감사 재현: D+1 시세 조회가 그날 실패해 다음날(age=2)이 됐어도
    grace_days=2 안이면 여전히 지평 1이 반환된다 — 그렇지 않으면 그 행의 D+1은
    영구히 못 채운다."""
    assert due_horizons("2026-08-14", "2026-08-18") == [1]


def test_due_horizons_grace_expires_after_configured_days():
    """grace_days=2 를 넘기면(age=4) 더 이상 지평 1을 반환하지 않는다 — 나흘
    지난 종가를 "D+1"이라 적는 건 근사가 아니라 오염이다."""
    assert due_horizons("2026-08-14", "2026-08-20") == []


def test_selection_day_itself_is_not_a_horizon():
    """선정 당일 종가는 **기준가**다 — 수익률이 아니다."""
    assert due_horizons("2026-08-14", "2026-08-14") == []


def test_past_dates_never_produce_horizons():
    assert due_horizons("2026-08-14", "2026-08-13") == []


# ── D2: 기준 세션 날짜 (close_date 우선, 없으면 date 폴백) ─────────────────

def test_base_session_date_prefers_close_date():
    """close_date(실제 시세 조회일)가 있으면 date(리포트 빌드일)보다 우선한다 —
    리포트가 휴장일 다음날 빌드를 돌면 date 는 거래일이 아닐 수 있다."""
    row = {"date": "2026-08-17", "close_date": "2026-08-14"}
    assert base_session_date(row) == "2026-08-14"


def test_base_session_date_falls_back_to_date_for_legacy_rows():
    """close_date 가 없는 옛 행은 기존 date 근사를 그대로 쓴다."""
    row = {"date": "2026-08-14"}
    assert base_session_date(row) == "2026-08-14"


# ── 어떤 종목의 시세가 필요한가 ───────────────────────────────────────────

def test_only_symbols_with_a_due_or_recently_missed_horizon_are_fetched():
    """시세 조회는 네트워크다 — 필요 없는 종목까지 부르면 레이트 리밋에 걸린다.

    2026-08-26: 유예(grace) 도입으로 "오늘이 정확히 만기"뿐 아니라 **최근에 놓친
    지평**도 대상이 된다 — D+1 당일 조회가 실패하면 예전엔 그 행의 D+1 이 영원히
    비었다(due_horizons docstring 참고). 8-11 선정분(D+3, 유예 안)은 다시 시도되고,
    유예를 넘긴 것(D+4 이상)은 대상이 아니다."""
    rows = [
        {"date": "2026-08-13", "symbol": "005930", "attributes": {"close": 71000.0}},
        {"date": "2026-08-11", "symbol": "000660", "attributes": {"close": 200000.0}},
    ]

    # 005930 = D+1(오늘 만기), 000660 = D+3(D+1 을 놓친 지 이틀 — 유예 안)
    assert pending_symbols(rows, today="2026-08-14") == {"005930", "000660"}


def test_pending_symbols_uses_close_date_when_present():
    """D2: date(리포트 빌드일)가 실제 거래일보다 하루 늦어도 close_date 가 있으면
    그걸로 세션을 센다 — 안 그러면 age 가 하루 부풀어 만기 판정이 틀린다."""
    # date 로 세면 아직 D+0(오늘 자신), close_date 로 세면 정확히 D+1.
    rows = [{"date": "2026-08-14", "close_date": "2026-08-13",
             "symbol": "005930", "attributes": {"close": 71000.0}}]

    assert pending_symbols(rows, today="2026-08-14") == {"005930"}


def test_horizon_missed_beyond_grace_is_not_refetched():
    """유예를 넘기면 포기한다 — 사흘 넘게 지난 종가를 D+1 이라 적는 건 오염이다."""
    rows = [{"date": "2026-08-10", "symbol": "000660", "attributes": {"close": 200000.0}}]

    assert pending_symbols(rows, today="2026-08-14") == set()


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
    한다. 근사를 쓰면서 그 사실을 기록하지 않으면 나중에 검산이 불가능하다.

    `asof` 는 호출부가 넘긴 값(명령이 돈 "오늘")을 그대로 적는다 —
    `close_report.matured_today` 가 이 필드를 `today` 와 비교해 "오늘 채워졌나"를
    판단하므로 바꾸지 않는다(quote 의 실제 시세 날짜는 D1 게이트에만 쓴다,
    아래 same-session 테스트 참고)."""
    row = apply_outcome(_row(), horizon=1, quote=(101.0, "2026-08-14"), asof="2026-08-14")

    assert row["outcome_d1_bps"] == pytest.approx(100.0)
    assert row["outcome_d1_asof"] == "2026-08-14"


def test_missing_price_leaves_the_row_untouched_not_zero():
    """시세를 못 구했으면 **아무것도 쓰지 않는다.** 0을 쓰면 조회 실패가 "본전"으로
    영구히 굳고, 다시 시도할 기회도 사라진다."""
    row = apply_outcome(_row(), horizon=1, quote=None, asof="2026-08-14")

    assert "outcome_d1_bps" not in row
    assert not row.get("outcome_filled")


def test_outcome_filled_only_after_the_longest_horizon():
    """`outcome_filled` 은 "더 채울 게 없다"는 뜻이다 — D+1 만 채우고 세우면
    D+5·D+20 이 영영 안 채워진다(pending_outcomes 가 건너뛴다)."""
    row = apply_outcome(_row(), horizon=1, quote=(101.0, "2026-08-14"), asof="2026-08-14")
    assert not row.get("outcome_filled")

    row = apply_outcome(row, horizon=5, quote=(105.0, "2026-08-21"), asof="2026-08-21")
    assert not row.get("outcome_filled")

    row = apply_outcome(row, horizon=20, quote=(110.0, "2026-09-11"), asof="2026-09-11")
    assert row["outcome_filled"] is True


def test_apply_outcome_does_not_mutate_the_input():
    original = _row()

    apply_outcome(original, horizon=1, quote=(101.0, "2026-08-14"), asof="2026-08-14")

    assert "outcome_d1_bps" not in original


def test_zero_base_close_is_refused():
    """기준가 0 이면 수익률이 정의되지 않는다 — inf 를 쓰지 않는다."""
    row = apply_outcome({"date": "2026-08-13", "symbol": "X", "attributes": {"close": 0.0}},
                        horizon=1, quote=(101.0, "2026-08-14"), asof="2026-08-14")

    assert "outcome_d1_bps" not in row


# ── D1: 같은 세션 조회는 "시세 없음"과 동일하게 취급한다 ────────────────────

def test_same_session_quote_writes_nothing():
    """2026-08-26 감사 재현: 휴장일 다음날 "오늘 종가"를 조회했더니 선정일과 같은
    세션이라 (close_now-base)/base 가 정확히 0.0 이었다 — 그 가짜 0 이 그대로
    기록되면 재조회 필터(`is not None`)가 다시는 이 지평을 건드리지 않는다.
    quote 날짜가 기준 세션(2026-08-13)보다 뒤가 아니면 아무것도 쓰지 않는다."""
    row = apply_outcome(_row(), horizon=1, quote=(100.0, "2026-08-13"), asof="2026-08-14")

    assert "outcome_d1_bps" not in row


def test_next_session_quote_writes_the_return():
    """기준 세션 다음 거래일 종가면 정상적으로 채운다(회귀 방지 — 위 same-session
    테스트가 grace 재시도까지 막아버리지 않는지 확인)."""
    row = apply_outcome(_row(), horizon=1, quote=(101.0, "2026-08-14"), asof="2026-08-14")

    assert row["outcome_d1_bps"] == pytest.approx(100.0)


def test_same_session_quote_uses_close_date_as_base_when_present():
    """close_date 가 있으면 그걸 기준 세션으로 쓴다(date 가 아니라) — D2 와 같은
    필드를 D1 의 세션 판정에도 일관되게 쓴다."""
    row = {"date": "2026-08-17", "close_date": "2026-08-14", "symbol": "005930",
           "attributes": {"close": 100.0}}

    # date(08-17) 기준으로는 다음 세션이지만, close_date(08-14) 기준으로는 같은
    # 세션이다 — close_date 가 이겨야 한다.
    same_session = apply_outcome(row, horizon=1, quote=(100.0, "2026-08-14"), asof="2026-08-17")
    assert "outcome_d1_bps" not in same_session

    next_session = apply_outcome(row, horizon=1, quote=(101.0, "2026-08-17"), asof="2026-08-18")
    assert next_session["outcome_d1_bps"] == pytest.approx(100.0)


# ── 시세 추출 (실제 반환 형태) ────────────────────────────────────────────

def test_closes_from_quotes_reads_the_close_and_date_fields():
    """**`fetch_symbol_quotes` 는 `{심볼: {"close": ..., "date": ...}}` 를 돌려준다.**

    2026-08-14 실측 버그: 내 배선이 `float(v)` 로 dict 를 변환하려다 예외가 났고,
    내 except 가 그걸 삼켜 `quotes_fetched: 0` 만 남았다 — 돌았는데 아무 일도 안 하는
    그 패턴이다. 반환 형태를 테스트로 못 박는다.

    2026-09-03 (D1): `date` 도 함께 남긴다 — 버리면 `apply_outcome` 이 같은 세션
    재조회를 구분할 방법이 없다.
    """
    got = closes_from_quotes({
        "AAPL": {"close": 305.26, "prev": 302.25, "date": "2026-08-14"},
        "MSFT": {"close": 496.88, "date": "2026-08-14"},
    })

    assert got == {"AAPL": (305.26, "2026-08-14"), "MSFT": (496.88, "2026-08-14")}


def test_quotes_without_a_close_are_dropped():
    """close 가 없으면 그 종목은 시세를 못 구한 것이다 — 0 으로 위장하지 않는다."""
    assert closes_from_quotes({"AAPL": {"prev": 1.0, "date": "2026-08-14"},
                               "MSFT": {}, "X": None}) == {}


def test_quotes_without_a_date_are_dropped():
    """날짜 없는 종가는 D1 의 같은-세션 판정이 불가능하다 — 가짜 0bp 위험을 그대로
    재현하므로 통째로 뺀다(방어적: `fetch_symbol_quotes` 는 실제로는 항상 날짜를
    준다)."""
    assert closes_from_quotes({"AAPL": {"close": 305.26}}) == {}


def test_kr_symbols_need_a_yahoo_mapping():
    """KR 6자리 코드는 야후 심볼이 아니다 — 매핑 없이 부르면 조용히 빈 결과가 온다.

    내일 KR 선정의 D+1 이 만기가 되면 같은 방식으로 실패할 예정이었다.
    """
    us, kr = split_for_quotes({"AAPL", "005930"})

    assert us == {"AAPL"}
    assert kr == {"005930"}


# ── D3: 미국 종류주(점 표기) 심볼은 야후에 대시로 보낸다 ────────────────────

def test_dotted_us_symbol_maps_to_dash_for_yahoo():
    """야후는 BRK.B 를 조용히 못 받는다 — 대시(BRK-B)로 바꿔야 한다. 감사 재현:
    이 변환이 없어서 BRK.B 는 한 번도 채점되지 않았다."""
    assert to_yahoo_us_symbol("BRK.B") == "BRK-B"


def test_plain_us_symbol_is_unchanged_by_yahoo_mapping():
    assert to_yahoo_us_symbol("AAPL") == "AAPL"


# ── D4: 지평별 filled/due/lost 카운트 ───────────────────────────────────────

def test_horizon_status_counts_distinguishes_filled_due_and_lost():
    """filled(이미 채움) / due(진행 중 — 아직 채울 기회가 있다) / lost(grace 를
    넘겨 영구 유실)를 지평별로 센다. 아직 만기가 안 된 지평(D+5/D+20)은 셋 중
    어디에도 안 들어간다 — "아직 안 물어본 질문"이다."""
    today = "2026-08-14"
    rows = [
        # D+1 채워짐(filled).
        {"date": "2026-08-13", "symbol": "A", "outcome_d1_bps": 10.0},
        # D+1 진행 중(만기 당일, 아직 못 채움 — due).
        {"date": "2026-08-13", "symbol": "B"},
        # D+1 grace(기본 2일) 를 넘겨 영구 유실(D+13 선정 → age 이틀이 아니라
        # 나흘 지난 걸 만들려면 today 를 훨씬 뒤로 잡아야 한다 — 별도 today 사용).
    ]
    counts = horizon_status_counts(rows, today)

    assert counts[1]["filled"] == 1
    assert counts[1]["due"] == 1
    assert counts[1]["lost"] == 0
    # D+5/D+20 은 아직 만기가 안 됐다 — age(1) < 5, < 20.
    assert counts[5] == {"filled": 0, "due": 0, "lost": 0}
    assert counts[20] == {"filled": 0, "due": 0, "lost": 0}


def test_horizon_status_counts_marks_past_grace_as_lost():
    """grace_days(기본 2)를 넘긴 미채움 행은 lost 다 — due 에 잡히면 "언젠가 채워질
    수 있다"로 오독된다."""
    # 2026-08-14(금) 선정, age=4영업일 뒤(2026-08-20) → D+1 은 grace(1..3) 밖.
    rows = [{"date": "2026-08-14", "symbol": "A"}]

    counts = horizon_status_counts(rows, today="2026-08-20")

    assert counts[1] == {"filled": 0, "due": 0, "lost": 1}


def test_horizon_status_counts_uses_close_date_when_present():
    """D2 와 같은 필드로 세션을 센다 — close_date 가 있으면 date 대신 그걸 쓴다."""
    rows = [{"date": "2026-08-17", "close_date": "2026-08-13", "symbol": "A",
             "outcome_d1_bps": 10.0}]

    counts = horizon_status_counts(rows, today="2026-08-14")

    assert counts[1]["filled"] == 1
