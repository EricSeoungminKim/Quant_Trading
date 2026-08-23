"""판단 귀속 + 전방 수익률 — Phase 7.1/7.2. 순수 함수만(네트워크·DB 없음).

## 왜 `input_hash` 가 핵심인가

리더보드의 목적은 "결정론적 스코어러 vs LLM" 을 **공정하게** 비교하는 것이다. 그런데
두 생산자가 서로 **다른 입력**을 보고 낸 판단을 나란히 세우면 그건 실력 비교가 아니다 —
정보량 비교다. 그래서 판단마다 "이 판단이 본 입력"의 해시를 함께 남기고,
**같은 해시끼리만** 비교한다.

그래서 해시에 들어가면 안 되는 것이 분명하다: **생산자 이름도, 그 판단의 출력도**.
그게 섞이면 같은 입력을 본 두 판단이 서로 다른 해시를 갖게 되고 비교가 불가능해진다.

## 왜 전방 수익률이 먼저인가

`selections.py` 가 이미 매일 속성 벡터를 남기고 `outcome_*` 를 비워 둔다. 그런데
`pending_outcomes()` 를 **부르는 코드가 없다** — 며칠째 속성만 쌓이고 채점의 나머지
절반이 비어 있다. 입력이 이미 있는 조각이라 여기부터 채운다.

**속성은 썩고 가격은 안 썩는다**(selections 모듈의 판단). 그래서 속성은 그날 남기고
수익률은 나중에 계산한다 — 사후 갱신이라 append-only JSONL 이 아니라 DB 가 맡는다.
"""
from __future__ import annotations

import pytest

from quant.control.judgment import (
    HOLD_HORIZONS,
    forward_returns_bps,
    judgment_row,
    selection_judgment,
)
from quant.core.models import Judgment, input_hash


# ── input_hash ────────────────────────────────────────────────────────────

def test_hash_is_stable_across_key_order():
    """dict 순서가 해시를 바꾸면 같은 입력이 매번 다른 해시가 된다."""
    a = input_hash({"trending_score100": 68, "news_articles_today": 13})
    b = input_hash({"news_articles_today": 13, "trending_score100": 68})

    assert a == b


def test_hash_changes_when_any_input_value_changes():
    base = {"trending_score100": 68, "news_articles_today": 13}
    changed = {"trending_score100": 69, "news_articles_today": 13}

    assert input_hash(base) != input_hash(changed)


def test_hash_distinguishes_missing_from_zero():
    """**"트렌딩 0점"과 "트렌딩을 계산 못 했다"는 다른 사건이다**(selections 원칙).

    둘이 같은 해시가 되면 결측이 있던 날의 비교가 조용히 오염된다.
    """
    assert input_hash({"trending_score100": 0}) != input_hash({"trending_score100": None})


def test_hash_ignores_producer_and_output():
    """**이 테스트가 리더보드를 가능하게 한다.**

    같은 입력을 본 두 생산자는 같은 해시를 가져야 한다. 생산자 이름이나 판단 결과가
    해시에 섞이면 같은 입력끼리 묶을 수 없고, 비교는 실력이 아니라 정보량 비교가 된다.
    """
    features = {"trending_score100": 68, "news_articles_today": 13}

    scorer = Judgment(producer="watch_scorer", producer_version="3",
                      input_hash=input_hash(features), market="KR", symbol="005930",
                      session_date="2026-08-14", score=71.0, verdict="pass",
                      rationale="", ts="2026-08-14T08:00:00+09:00")
    llm = Judgment(producer="nemotron-3-ultra", producer_version="free",
                   input_hash=input_hash(features), market="KR", symbol="005930",
                   session_date="2026-08-14", score=40.0, verdict="reject",
                   rationale="", ts="2026-08-14T08:00:01+09:00")

    assert scorer.input_hash == llm.input_hash


def test_hash_is_a_sha256_hex():
    assert len(input_hash({"a": 1})) == 64
    assert all(c in "0123456789abcdef" for c in input_hash({"a": 1}))


# ── 선정 원장 → 판단 ──────────────────────────────────────────────────────

def _selection_row(**over) -> dict:
    row = {
        "schema": 1, "date": "2026-08-14", "market": "KR", "symbol": "005930",
        "is_candidate": True,
        "attributes": {"trending_score100": 68, "news_articles_today": 13,
                       "close": 71_000.0, "ai_score100": None},
        "params": {"threshold": 50},
    }
    row.update(over)
    return row


def test_deterministic_scorer_judgment_is_derived_from_the_selection_row():
    """7.3 — 결정론적 베이스라인이 리더보드에 선다. 이게 LLM 이 이겨야 할 상대다.

    점수 출처는 baseline_score100 뿐이다(trending 폴백은 §C1 리뷰로 없앴다) —
    그래서 픽스처에 baseline_score100 을 명시로 넣는다.
    """
    row = _selection_row()
    row["attributes"]["baseline_score100"] = 68
    j = selection_judgment(row, producer_version="3")

    assert j.producer == "watch_scorer"
    assert j.producer_version == "3"
    assert j.symbol == "005930"
    assert j.market == "KR"
    assert j.verdict == "pass"          # is_candidate=True
    assert j.score == 68


def test_rejected_symbol_is_also_recorded():
    """**떨어뜨린 종목도 판단이다.** 통과분만 남기면 문턱이 너무 높은지 낮은지
    영영 알 수 없다(selections 모듈이 같은 이유로 탈락분을 남긴다)."""
    j = selection_judgment(_selection_row(is_candidate=False), producer_version="3")

    assert j.verdict == "reject"


def test_same_attributes_yield_the_same_input_hash_across_days():
    """입력이 같으면 날짜가 달라도 해시가 같다 — 해시는 *입력*의 지문이다."""
    a = selection_judgment(_selection_row(date="2026-08-14"), producer_version="3")
    b = selection_judgment(_selection_row(date="2026-08-15"), producer_version="3")

    assert a.input_hash == b.input_hash


def test_score_missing_is_none_not_zero():
    row = _selection_row(attributes={"trending_score100": None, "close": 1.0})

    assert selection_judgment(row, producer_version="3").score is None


# ── 전방 수익률 ───────────────────────────────────────────────────────────

def test_forward_returns_are_bps_against_the_selection_close():
    """bps 로 재는 이유: 종목 가격대가 달라도 비교 가능해야 한다
    (적합도 함수가 명목 대비 bps 를 쓰는 것과 같은 이유)."""
    got = forward_returns_bps(base_close=100.0, closes={1: 101.0, 5: 98.0, 20: 110.0})

    assert got[1] == pytest.approx(100.0)     # +1% = 100bp
    assert got[5] == pytest.approx(-200.0)
    assert got[20] == pytest.approx(1000.0)


def test_unknown_future_close_is_none_not_zero():
    """**"수익률 0"과 "가격을 못 구했다"는 다른 사건이다.** 0 으로 위장하면
    리더보드가 조회 실패를 "본전"으로 집계한다."""
    got = forward_returns_bps(base_close=100.0, closes={1: 101.0, 5: None})

    assert got[1] == pytest.approx(100.0)
    assert got[5] is None
    assert got[20] is None                     # 아예 안 넘긴 지평도 None


def test_zero_or_negative_base_close_yields_all_none():
    """기준가가 0이면 나눗셈이 폭발한다 — 조용히 inf 를 넣지 않는다."""
    for bad in (0.0, -1.0, None):
        got = forward_returns_bps(base_close=bad, closes={1: 101.0})
        assert all(v is None for v in got.values())


def test_horizons_are_the_documented_ones():
    """계획서가 T+1/5/20 이라고 못 박았다."""
    assert HOLD_HORIZONS == (1, 5, 20)


# ── DB 행 변환 (멱등성) ───────────────────────────────────────────────────

def test_judgment_row_is_ordered_to_match_the_columns():
    j = selection_judgment(_selection_row(), producer_version="3")

    row = judgment_row(j)

    assert isinstance(row, tuple)
    assert row[0] == "watch_scorer"
    assert len(row) == 10


def test_natural_key_makes_reingest_idempotent():
    """같은 판단을 두 번 적재해도 행이 두 배가 되면 안 된다.

    자연키는 `(producer, producer_version, input_hash, symbol, session_date)` 다 —
    같은 생산자가 같은 입력을 같은 날 두 번 봐도 판단은 하나다.
    """
    j = selection_judgment(_selection_row(), producer_version="3")

    assert j.natural_key() == ("watch_scorer", "3", j.input_hash, "005930", "2026-08-14")


# ============ 선정 원장의 실제 형태 (2026-08-14 실측 버그) ============
#
# `selections.build_rows` 는 속성을 **최상위에 펼쳐** 넣는다(`**attrs`) — `attributes`
# 라는 중첩 키는 **없다.** 내가 `row["attributes"]` 를 읽는 코드를 써서:
#
#   - 판단 199건이 전부 **같은 input_hash**(빈 dict 의 해시)를 가졌다 → 리더보드의
#     전제("같은 입력을 본 판단끼리만 비교")가 조용히 파괴됐다.
#   - score 가 전부 None 이라 순위를 만들 수 없었다.
#
# 형태를 테스트로 못 박는다. 스키마를 추측하면 이런 일이 또 난다.

def _flat_selection_row(**over) -> dict:
    """`build_rows` 가 실제로 만드는 모양 — 속성이 최상위에 있다."""
    row = {
        "schema": 1, "date": "2026-08-14", "market": "KR", "symbol": "005930",
        "is_candidate": True,
        "trending_score100": 68, "news_articles_today": 13, "close": 71000.0,
        "ai_score100": None, "trending_label": "관심",
        "market_stance100": 55, "params": {"threshold": 50},
        "outcome_filled": False,
    }
    row.update(over)
    return row


def test_judgment_reads_attributes_from_the_flat_row():
    """점수 출처는 baseline_score100 뿐이다(§C1 리뷰로 trending 폴백 제거) —
    이 픽스처 행에 그걸 명시로 얹어야 score 단언이 의미가 있다."""
    j = selection_judgment(_flat_selection_row(baseline_score100=68), producer_version="3")

    assert j.score == 68
    assert j.symbol == "005930"
    assert j.verdict == "pass"


def test_different_symbols_get_different_input_hashes():
    """**이게 리더보드의 최소 조건이다.**

    실측 버그에서는 199건이 단일 해시였다 — 그러면 "같은 입력을 본 판단"이 전부
    같은 것으로 묶여 비교가 무의미해진다.
    """
    a = selection_judgment(_flat_selection_row(symbol="005930", trending_score100=68),
                           producer_version="3")
    b = selection_judgment(_flat_selection_row(symbol="000660", trending_score100=41),
                           producer_version="3")

    assert a.input_hash != b.input_hash


def test_bookkeeping_fields_are_not_part_of_the_input_hash():
    """`outcome_*`·`schema`·`date` 는 **입력이 아니다.** 사후에 채워지는 값이 해시에
    들어가면 같은 판단의 해시가 나중에 바뀐다."""
    before = selection_judgment(_flat_selection_row(), producer_version="3")
    after = selection_judgment(
        _flat_selection_row(outcome_d1_bps=120.0, outcome_filled=True), producer_version="3")

    assert before.input_hash == after.input_hash


def test_verdict_is_not_in_the_input_hash():
    """결정론적 판정이 해시에 들어가면 같은 입력을 본 LLM 과 묶이지 않는다."""
    a = selection_judgment(_flat_selection_row(is_candidate=True), producer_version="3")
    b = selection_judgment(_flat_selection_row(is_candidate=False), producer_version="3")

    assert a.input_hash == b.input_hash
    assert a.verdict != b.verdict


# ============ 점수 출처: baseline_score100 뿐 (§E-2, 폴백은 §C1 리뷰로 제거) ==

def test_judgment_prefers_baseline_score_over_trending():
    """산식 교체(v2) — trending_score100 은 상수 50 이 대부분이라 baseline 이 이겨야
    한다(2026-08-14 실측: 판단 254건 중 59건이 상수 50)."""
    row = _flat_selection_row(baseline_score100=62, trending_score100=68)

    j = selection_judgment(row, producer_version="2")

    assert j.score == 62


def test_judgment_score_is_none_when_baseline_absent_even_with_trending():
    """2026-08-15 리뷰 C1 — trending_score100 폴백을 없앴다. 남겨두면
    `cmd_outcomes`(날짜 필터 없이 selections 전체를 재판단)가 `--scorer-version`
    기본값 "2" 로 옛 v1 행(trending 만 있고 baseline 은 없음)을 v2 버킷에 v1
    산식 점수로 채워, 같은 날 v2 표본과 뒤섞여 리더보드 IC 를 오염시킨다.

    옛 행은 이미 v1 시절에 기록된 판단이 있으므로 여기서 잃는 것이 없다 —
    score=None 은 "채점 못 함"으로 리더보드가 자동 제외한다(0 과 다르다)."""
    row = _flat_selection_row(trending_score100=68)
    assert "baseline_score100" not in row

    j = selection_judgment(row, producer_version="2")

    assert j.score is None


def test_judgment_score_is_none_when_both_absent():
    row = _flat_selection_row()
    row.pop("trending_score100", None)

    j = selection_judgment(row, producer_version="2")

    assert j.score is None


def test_judgment_baseline_score_zero_is_not_overridden_by_trending():
    """0 은 유효한 baseline 점수다 — 있는데 trending 으로 덮이면 안 된다."""
    row = _flat_selection_row(baseline_score100=0, trending_score100=68)

    j = selection_judgment(row, producer_version="2")

    assert j.score == 0


# ============ news_z 추가 (H-2 Task 4, producer_version "2"→"3") ============

def test_news_z_changes_the_input_hash():
    """news_z 유무가 속성 벡터를 바꾸므로 입력 해시도 달라야 한다 — 그래야
    news_z 이전/이후 행이 같은 입력으로 잘못 묶이지 않는다."""
    with_z = _flat_selection_row(baseline_score100=62, news_z=1.5)
    without_z = _flat_selection_row(baseline_score100=62)

    a = selection_judgment(with_z, producer_version="3")
    b = selection_judgment(without_z, producer_version="3")

    assert a.input_hash != b.input_hash


def test_news_z_does_not_change_the_score():
    """news_z 는 아직 점수 산식의 입력이 아니다 — baseline_score100 만 본다.
    있든 없든 score 는 그대로여야, 이 필드 추가가 기존 채점을 조용히 바꾸지
    않았다는 게 확인된다."""
    with_z = _flat_selection_row(baseline_score100=62, news_z=1.5)
    without_z = _flat_selection_row(baseline_score100=62)

    a = selection_judgment(with_z, producer_version="3")
    b = selection_judgment(without_z, producer_version="3")

    assert a.score == b.score == 62


def test_old_rows_without_news_z_do_not_leak_into_the_new_producer_version_bucket():
    """**v2→v3 버킷 오염 방지 (§C1 리뷰와 같은 종류의 결함).**

    `cmd_outcomes` 는 날짜 필터 없이 selections 전체를 재판단하고
    `--scorer-version` 기본값이 이제 "3"이다. news_z 가 없던 옛 행이 그 기본값을
    타면 producer_version="3" 버킷에 들어가되, 여전히 news_z 없는 입력 해시를
    갖는다 — v3 신규 행(news_z 있음)과 절대 같은 input_hash 로 묶이지 않는다.
    자연키 전체가 다르므로 리더보드가 서로 다른 표본으로 정확히 구분한다."""
    old_row = _flat_selection_row(baseline_score100=62)  # news_z 없음(v2 시절 행)
    new_row = _flat_selection_row(baseline_score100=62, news_z=1.5)  # v3 신규 행

    old_j = selection_judgment(old_row, producer_version="3")
    new_j = selection_judgment(new_row, producer_version="3")

    assert old_j.producer_version == new_j.producer_version == "3"
    assert old_j.natural_key() != new_j.natural_key()
    assert old_j.input_hash != new_j.input_hash
    # 점수 산식 자체는 news_z 유무와 무관 — 폴백으로 슬쩍 바뀌지 않는다.
    assert old_j.score == new_j.score == 62
