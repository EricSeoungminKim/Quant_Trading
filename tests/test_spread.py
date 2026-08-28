"""호가 스프레드 실측 수집(quant.collect.spread) 테스트.

`spread_row`는 순수 함수라 오프라인으로 전부 검증하고, `sample_spread`는 가짜
클라이언트 + 가짜 시계로 배선해 검증한다 — **네트워크는 절대 타지 않는다**
(실호출 테스트를 만들면 Toss MARKET_DATA 버킷을 CI 가 갉아먹는다).
"""
from __future__ import annotations

from datetime import datetime, timezone

from quant.collect.spread import MIN_CALL_INTERVAL, sample_spread, spread_row

NOW = datetime(2026, 8, 28, 4, 30, tzinfo=timezone.utc)


def _lv(price, volume):
    # Toss 응답의 price/volume 은 문자열이다 — 실제 형태로 테스트한다.
    return {"price": str(price), "volume": str(volume)}


# ------------------------------------------------------------------ spread_row


def test_spread_row_computes_bp_and_imbalance():
    row = spread_row(bids=[_lv(100.0, 300)], asks=[_lv(100.1, 100)],
                     symbol="TQQQ", ts=NOW.isoformat())
    assert row["symbol"] == "TQQQ"
    assert row["bid"] == 100.0
    assert row["ask"] == 100.1
    # mid = 100.05, spread = 0.1 → 0.1/100.05*10000 ≈ 9.995bp
    assert abs(row["spread_bp"] - 9.995) < 0.01
    # (300-100)/400 = 0.5 — 매수 우위
    assert abs(row["imbalance"] - 0.5) < 1e-9
    assert row["ts"] == NOW.isoformat()


def test_spread_row_picks_best_level_regardless_of_ordering():
    """호가 단계가 여러 개일 때 최우선 1단만 쓴다 — 정렬 방향에 기대지 않는다.

    openapi.json 의 KR 예시는 `asks`가 **내림차순**(72300, 72200, 72100)이라
    "첫 원소 = 최우선"이 성립하지 않는다. 최저가 ask / 최고가 bid 로 직접 고른다.
    """
    row = spread_row(
        bids=[_lv(72000, 5200), _lv(71900, 4100), _lv(71800, 2700)],
        asks=[_lv(72300, 1200), _lv(72200, 3400), _lv(72100, 8500)],
        symbol="005930", ts=NOW.isoformat(),
    )
    assert (row["bid"], row["ask"]) == (72000.0, 72100.0)
    assert (row["bid_size"], row["ask_size"]) == (5200.0, 8500.0)
    assert row["imbalance"] < 0  # 매도 잔량 우위


def test_spread_row_rejects_crossed_or_locked_book():
    ts = NOW.isoformat()
    assert spread_row([_lv(100, 1)], [_lv(100, 1)], "X", ts) is None   # locked (ask == bid)
    assert spread_row([_lv(101, 1)], [_lv(100, 1)], "X", ts) is None   # crossed (ask < bid)


def test_spread_row_rejects_empty_or_unparsable_book():
    ts = NOW.isoformat()
    assert spread_row([], [_lv(100, 1)], "X", ts) is None
    assert spread_row([_lv(100, 1)], [], "X", ts) is None
    assert spread_row(None, None, "X", ts) is None
    assert spread_row([{"price": "0"}], [_lv(100, 1)], "X", ts) is None      # 0원 호가
    assert spread_row([{"volume": "5"}], [_lv(100, 1)], "X", ts) is None     # price 키 없음


def test_spread_row_zero_volume_book_keeps_price_with_neutral_imbalance():
    row = spread_row([_lv(100, 0)], [_lv(101, 0)], "X", NOW.isoformat())
    assert row is not None and row["imbalance"] == 0.0


# ---------------------------------------------------------------- sample_spread


class _FakeClient:
    def __init__(self, books: dict):
        self.books = books
        self.calls: list[str] = []

    def orderbook(self, symbol: str) -> dict:
        self.calls.append(symbol)
        book = self.books[symbol]
        if isinstance(book, Exception):
            raise book
        return book


def _book(bid, ask):
    return {"timestamp": "2026-08-28T13:30:00+09:00", "currency": "KRW",
            "bids": [_lv(bid, 100)], "asks": [_lv(ask, 100)]}


def test_failed_symbol_does_not_block_the_rest():
    client = _FakeClient({
        "A": _book(100, 101),
        "B": RuntimeError("boom"),
        "C": _book(200, 201),
    })
    out = sample_spread(client, ["A", "B", "C"], now=NOW, sleep=lambda s: None,
                        monotonic=lambda: 0.0)
    assert [r["symbol"] for r in out.rows] == ["A", "C"]
    assert out.failed == [("B", "RuntimeError: boom")]
    assert client.calls == ["A", "B", "C"]  # 실패 후에도 계속 조회한다


def test_empty_and_outlier_books_are_reported_not_swallowed():
    client = _FakeClient({
        "OK": _book(100, 101),
        "EMPTY": {"timestamp": "t", "bids": [], "asks": []},   # US 무응답이 이 형태다
        "NONE": None,
        "CROSSED": {"bids": [_lv(101, 1)], "asks": [_lv(100, 1)]},
    })
    out = sample_spread(client, ["OK", "EMPTY", "NONE", "CROSSED"], now=NOW,
                        sleep=lambda s: None, monotonic=lambda: 0.0)
    assert [r["symbol"] for r in out.rows] == ["OK"]
    assert out.empty == ["EMPTY", "NONE"]
    assert out.dropped == ["CROSSED"]


def test_rate_limit_sleeps_between_calls():
    """호출 간 슬립이 실제로 들어가는지 — 가짜 시계로 검증(진짜로 기다리지 않는다)."""
    client = _FakeClient({s: _book(100, 101) for s in ["A", "B", "C"]})
    slept: list[float] = []
    out = sample_spread(
        client, ["A", "B", "C"], now=NOW,
        min_interval=1.0,
        sleep=slept.append,
        monotonic=lambda: 0.0,  # 시간이 멈춘 시계 — 매 호출이 최대치를 기다려야 한다
    )
    assert len(out.rows) == 3
    assert slept == [1.0, 1.0]  # 첫 호출은 즉시, 이후 2회는 간격만큼 대기


def test_rate_limit_floor_cannot_be_bypassed():
    """호출자가 0을 넘겨도 5 TPS(0.2초) 바닥값으로 잘린다 — 엔진 시세와 버킷을 나눈다."""
    client = _FakeClient({s: _book(100, 101) for s in ["A", "B"]})
    slept: list[float] = []
    sample_spread(client, ["A", "B"], now=NOW, min_interval=0.0,
                  sleep=slept.append, monotonic=lambda: 0.0)
    assert slept == [MIN_CALL_INTERVAL]


def test_no_sleep_when_enough_time_already_passed():
    client = _FakeClient({s: _book(100, 101) for s in ["A", "B"]})
    ticks = iter([0.0, 10.0, 10.0])  # 두 번째 호출 시점엔 이미 10초가 지났다
    slept: list[float] = []
    sample_spread(client, ["A", "B"], now=NOW, min_interval=1.0,
                  sleep=slept.append, monotonic=lambda: next(ticks))
    assert slept == []
