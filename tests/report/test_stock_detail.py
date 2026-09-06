import threading
import time
from pathlib import Path

import pytest

from quant.collect.sources.stock_detail import (
    OPINION_LABELS,
    fetch_many,
    opinion_label,
    parse_consensus,
    parse_investor_flow,
)

FRGN_FIXTURE = Path(__file__).parent / "fixtures" / "naver_frgn_005930.html"
CONSENSUS_FIXTURE = Path(__file__).parent / "fixtures" / "naver_consensus_005930.html"


@pytest.fixture
def flow_rows():
    return parse_investor_flow(FRGN_FIXTURE.read_bytes())


@pytest.fixture
def consensus_html():
    return CONSENSUS_FIXTURE.read_text(encoding="utf-8")


def test_parses_at_least_five_rows(flow_rows):
    assert len(flow_rows) >= 5


def test_latest_row_matches_known_values(flow_rows):
    # 2026-08-11 삼성전자 — 네이버 화면 표시값과 대조해 고정한 값이다
    r = flow_rows[0]
    assert r["date"] == "2026-08-11"
    assert r["close"] == 239500
    assert r["change_pct"] == 4.13
    assert r["volume"] == 23310969
    assert r["inst_net"] == 319290
    assert r["foreign_net"] == 2462529
    assert r["foreign_hold_pct"] == 46.60


def test_date_is_iso_normalized(flow_rows):
    assert all(len(r["date"]) == 10 and r["date"][4] == "-" for r in flow_rows)


def test_empty_flow_raises():
    with pytest.raises(ValueError):
        parse_investor_flow(b"<html><body>no table here</body></html>")


def test_parses_consensus(consensus_html):
    c = parse_consensus(consensus_html)
    assert c["opinion_score"] == 4.04
    assert c["opinion_label"] == "매수"
    assert c["target_price"] == 491875
    assert c["eps"] == 47821
    assert c["per"] == 5.01
    assert c["analyst_count"] == 24


def test_consensus_missing_table_raises():
    with pytest.raises(ValueError):
        parse_consensus("<html><body>no cTB15 here</body></html>")


@pytest.mark.parametrize(
    "score,label",
    [
        (1.0, "강력매도"),
        (2.0, "매도"),
        (3.0, "중립"),
        (4.04, "매수"),
        (4.8, "강력매수"),
    ],
)
def test_opinion_label_boundaries(score, label):
    assert opinion_label(score) == label
    assert label in OPINION_LABELS


def test_foreign_buy_streak_inline():
    # fetch_stock_detail 내부 로직과 동일한 계산을 인라인 flow 로 검증
    flow = [
        {"foreign_net": 100, "inst_net": -50},
        {"foreign_net": 200, "inst_net": 30},
        {"foreign_net": -10, "inst_net": 10},
        {"foreign_net": 50, "inst_net": 5},
    ]

    def streak(key):
        n = 0
        for r in flow:
            if r[key] <= 0:
                break
            n += 1
        return n

    assert streak("foreign_net") == 2  # 100, 200 이후 -10 에서 끊김
    assert streak("inst_net") == 0  # 첫 행이 음수라 즉시 끊김


def test_foreign_buy_streak_all_positive():
    flow = [{"foreign_net": 1}, {"foreign_net": 2}, {"foreign_net": 3}]

    def streak(key):
        n = 0
        for r in flow:
            if r[key] <= 0:
                break
            n += 1
        return n

    assert streak("foreign_net") == 3


@pytest.mark.live
def test_live_fetch_stock_detail_fills_flow_and_consensus():
    from quant.collect.sources.stock_detail import fetch_stock_detail

    d = fetch_stock_detail("005930")
    assert d["flow"]
    assert d["consensus"] is not None
    assert d["consensus"]["target_price"]
    assert d["upside_pct"] is not None
    assert d["upside_pct"] > 0


# --- flow_daily (서브프로젝트 I) — 5일 요약 폐기 전에 일별 원본을 보존한다 ---


def test_fetch_stock_detail_adds_flow_daily_additively(monkeypatch):
    """`flow_daily` 는 새 키다 — 기존 `flow`/`flow_summary` 는 그대로여야 한다
    (additive, backward compatible). 픽스처는 20행짜리라 최대 20행 상한도 같이
    검증된다."""
    import quant.collect.sources.stock_detail as sd

    raw = FRGN_FIXTURE.read_bytes()

    class FakeResp:
        def raise_for_status(self):
            pass

        @property
        def content(self):
            return raw

        @property
        def text(self):
            return raw.decode("euc-kr", "replace")

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(sd, "client", lambda: FakeClient())

    detail = sd.fetch_stock_detail("005930")
    parsed = parse_investor_flow(raw)

    assert detail["flow"] == parsed[:10]  # 기존 동작 그대로(변경 없음)
    assert set(detail["flow_summary"]) == {
        "foreign_net_5d", "inst_net_5d", "foreign_buy_streak", "inst_buy_streak",
    }

    assert len(detail["flow_daily"]) == 20  # 픽스처 전체 행 수 == 상한
    assert detail["flow_daily"][0] == {
        "date": "2026-08-11", "foreign_net": 2462529, "inst_net": 319290,
    }
    assert all(set(r) == {"date", "foreign_net", "inst_net"} for r in detail["flow_daily"])


# --- fetch_many (2026-09-07 Phase 2 §5) — 순차 + 0.3초 슬립 → 병렬 조회 ---


def test_fetch_many_respects_limit(monkeypatch):
    """`limit` 을 넘는 코드는 아예 조회하지 않는다(기존 `codes[:limit]` 계약)."""
    import quant.collect.sources.stock_detail as sd

    seen = []

    def fake_fetch(code):
        seen.append(code)
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    result = fetch_many(["a", "b", "c", "d"], limit=2)

    assert sorted(seen) == ["a", "b"]
    assert set(result) == {"a", "b"}


def test_fetch_many_preserves_input_order(monkeypatch):
    """스레드 완료 순서와 무관하게 반환 dict 키 순서는 입력 `codes` 순서다
    (순차 버전의 삽입 순서 계약을 유지)."""
    import quant.collect.sources.stock_detail as sd

    # 코드를 거꾸로 완료시켜 완료 순서 != 입력 순서가 되게 한다.
    delays = {"a": 0.06, "b": 0.04, "c": 0.02, "d": 0.0}

    def fake_fetch(code):
        time.sleep(delays[code])
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    result = fetch_many(["a", "b", "c", "d"], limit=8)

    assert list(result.keys()) == ["a", "b", "c", "d"]


def test_fetch_many_one_failure_does_not_kill_others(monkeypatch):
    """한 심볼의 예외가 나머지 결과를 막지 않는다(기존 순차 버전과 동일 계약)."""
    import quant.collect.sources.stock_detail as sd

    def fake_fetch(code):
        if code == "bad":
            raise ValueError("boom")
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    result = fetch_many(["good1", "bad", "good2"], limit=8)

    assert set(result) == {"good1", "good2"}


def test_fetch_many_runs_concurrently(monkeypatch):
    """병렬화가 실제로 일어난다 — N개를 각각 슬립하는 페이크로 감쌌을 때
    전체 소요시간이 순차 합보다 훨씬 짧아야 한다(요지: 더 이상 종목당
    0.3초씩 순차로 대기하지 않는다)."""
    import quant.collect.sources.stock_detail as sd

    n = 8
    sleep_s = 0.2

    def fake_fetch(code):
        time.sleep(sleep_s)
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    t0 = time.perf_counter()
    result = fetch_many([str(i) for i in range(n)], limit=n)
    elapsed = time.perf_counter() - t0

    assert len(result) == n
    # 순차라면 n * sleep_s = 1.6초. max_workers=4 병렬이면 이론상 2 배치 =~0.4초.
    # CI 노이즈를 감안해 넉넉히 순차 소요의 절반 미만이면 통과로 본다.
    assert elapsed < (n * sleep_s) / 2


def test_fetch_many_empty_codes_returns_empty_dict():
    assert fetch_many([], limit=8) == {}


def test_fetch_many_time_budget_returns_partial_and_warns(monkeypatch, capsys):
    """예산을 넘기면 그때까지 끝난 것만 돌려주고, 못 끝낸 종목은 결측으로
    남긴다(0/캐시로 위장하지 않는다) — stderr 경고도 함께 확인한다."""
    import quant.collect.sources.stock_detail as sd

    def fake_fetch(code):
        # "slow"만 예산을 넘기도록 오래 걸리게 한다.
        time.sleep(0.5 if code == "slow" else 0.0)
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    result = fetch_many(["fast1", "fast2", "slow"], limit=8, time_budget_s=0.1)

    assert set(result) == {"fast1", "fast2"}
    assert "slow" not in result
    err = capsys.readouterr().err
    assert "시간 예산" in err
    assert "2/3" in err


def test_fetch_many_no_budget_overrun_stays_silent(monkeypatch, capsys):
    """예산 안에 전부 끝나면 경고를 남기지 않는다."""
    import quant.collect.sources.stock_detail as sd

    monkeypatch.setattr(sd, "fetch_stock_detail", lambda code: {"code": code})

    result = fetch_many(["a", "b"], limit=8, time_budget_s=5.0)

    assert set(result) == {"a", "b"}
    assert capsys.readouterr().err == ""


def test_fetch_many_uses_bounded_worker_pool(monkeypatch):
    """동시 실행 스레드 수가 4를 넘지 않는지 확인한다(무제한 병렬 금지)."""
    import quant.collect.sources.stock_detail as sd

    lock = threading.Lock()
    concurrent = 0
    peak = 0

    def fake_fetch(code):
        nonlocal concurrent, peak
        with lock:
            concurrent += 1
            peak = max(peak, concurrent)
        time.sleep(0.05)
        with lock:
            concurrent -= 1
        return {"code": code}

    monkeypatch.setattr(sd, "fetch_stock_detail", fake_fetch)

    fetch_many([str(i) for i in range(10)], limit=10)

    assert peak <= 4


def test_fetch_stock_detail_flow_daily_caps_at_20_when_more_rows_parsed(monkeypatch):
    """일별 원장 상한은 20행 — 파서가 그 이상을 돌려줘도 잘라낸다."""
    import quant.collect.sources.stock_detail as sd

    rows = [
        {
            "date": f"2026-01-{i + 1:02d}",
            "close": 100, "change_pct": 0.0, "volume": 1,
            "inst_net": i, "foreign_net": i * 10, "foreign_hold_pct": 1.0,
        }
        for i in range(25)
    ]
    monkeypatch.setattr(sd, "parse_investor_flow", lambda raw: rows)

    class FakeResp:
        def raise_for_status(self):
            pass

        content = b""
        text = ""

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None):
            return FakeResp()

    monkeypatch.setattr(sd, "client", lambda: FakeClient())

    detail = sd.fetch_stock_detail("005930")

    assert len(detail["flow_daily"]) == 20
    assert detail["flow_daily"][0]["date"] == "2026-01-01"
    assert detail["flow_daily"][-1]["date"] == "2026-01-20"
