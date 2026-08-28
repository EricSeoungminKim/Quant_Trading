"""세력 신호 수집(2026-08-28) — 파싱·적재·플래그 계약.

실 키움 서버 호출은 없다. 프레임은 전부 `docs/api/kiwoom/README.md` 5.5 의
verbatim 예시를 그대로 쓰거나(0B), 스펙에 매핑표가 없는 타입(0w/0F)은 FID 코드를
가진 가짜 프레임을 만든다 — **이 테스트가 FID 이름을 단정하지 않는 것 자체가 계약**
이다(스펙에 없는 필드를 추측하지 않는다).

검증 대상:
① 0B 프레임에서 FID 27/28 이 파싱되고, 기존 price 콜백(Quote)은 불변
② 0w/0F 파싱 — values 를 FID 코드 그대로 적재
③ 미지/깨진 형식은 버리지 않고 raw 로 보존
④ 버퍼 flush 원칙 (tests/test_tick_log.py 와 같은 계약)
⑤ 플래그 off 면 구독 등록 자체가 없다
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from quant.adapters.brokers.kiwoom.websocket import (
    REALTIME_TICK_TYPE,
    SMART_FLOW_TYPES,
    KiwoomRealtimeFeed,
)
from quant.adapters.smart_flow_log import SmartFlowLogger
from quant.apps.assembly import build_smart_flow

_T0 = datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc)


class RecordingSink:
    """websocket 이 싱크에 무엇을 넘기는지만 본다 — 디스크는 ④에서 따로 본다."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.raws: list[object] = []
        self.flushes = 0
        self.closed = 0

    def record(self, kind: str, symbol: str, ts: datetime, fields: dict) -> None:
        self.rows.append({"kind": kind, "symbol": symbol, "ts": ts, "fields": fields})

    def record_raw(self, payload: object, ts: datetime) -> None:
        self.raws.append(payload)

    def flush_if_due(self, now: datetime) -> int:
        self.flushes += 1
        return 0

    def close(self) -> int:
        self.closed += 1
        return 0


def _feed(sink=None) -> KiwoomRealtimeFeed:
    return KiwoomRealtimeFeed(access_token="tok", smart_flow_sink=sink)


def _read_jsonl(path):
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --------------------------------------------------------------------------
# ① 0B — FID 27/28 파싱 + 기존 price 콜백 불변
# --------------------------------------------------------------------------

# docs/api/kiwoom/README.md 5.5 [확인됨, verbatim] 을 그대로 옮긴 프레임.
_README_0B_FRAME = {
    "trnm": "REAL",
    "data": [
        {
            "type": "0B",
            "name": "주식체결",
            "item": "005930",
            "values": {
                "20": "165208",
                "10": "-20800",
                "11": "-50",
                "12": "-0.24",
                "27": "-20800",
                "28": "-20700",
            },
        }
    ],
}


def test_readme_0b_frame_still_yields_unchanged_quote_without_sink():
    """싱크가 없으면 이 어댑터의 동작은 한 바이트도 달라지지 않는다(회귀 방지)."""
    feed = _feed()
    feed._handle_real(_README_0B_FRAME)

    quote = feed.quote("005930")
    assert quote is not None
    assert quote.price == 20800.0  # 부호(-)는 등락 방향 — 절대값
    assert quote.ts.strftime("%H%M%S") == "165208"


def test_0b_frame_parses_best_bid_ask_into_sink():
    """② 27=최우선매도호가, 28=최우선매수호가 — 이미 오던 프레임에서 주워 담는다."""
    sink = RecordingSink()
    feed = _feed(sink)
    feed._handle_real(_README_0B_FRAME)

    l1 = [r for r in sink.rows if r["kind"] == "quote_l1"]
    assert len(l1) == 1
    assert l1[0]["symbol"] == "005930"
    assert l1[0]["fields"] == {"bid": 20700.0, "ask": 20800.0}
    # 체결시간(FID 20)을 그대로 쓴다 — 수신 시각이 아니라.
    assert l1[0]["ts"].strftime("%H%M%S") == "165208"


def test_quote_callback_contract_unchanged_when_sink_present():
    """① 싱크가 붙어도 Quote 계약(price)은 그대로다 — 소비자는 price 만 쓴다."""
    sink = RecordingSink()
    feed = _feed(sink)
    feed._handle_real(_README_0B_FRAME)

    quote = feed.quote("005930")
    assert quote is not None
    assert quote.price == 20800.0
    # bid/ask 가 코어 모델(Quote)로 새어나가지 않았다 — 별도 설계 결정이다.
    assert not hasattr(quote, "bid")
    assert not hasattr(quote, "ask")


def test_0b_without_bid_ask_records_no_l1_row():
    """27/28 이 없는 체결 프레임은 L1 행을 만들지 않는다(빈 행 오염 금지)."""
    sink = RecordingSink()
    feed = _feed(sink)
    feed._handle_real(
        {"trnm": "REAL", "data": [
            {"type": "0B", "item": "005930", "values": {"10": "70000", "20": "090001"}}
        ]}
    )
    assert feed.quote("005930").price == 70000.0
    assert sink.rows == []


def test_0b_with_missing_price_still_records_l1():
    """현재가가 깨져도 최우선호가는 살아 있을 수 있다 — 한쪽 실패로 다른 쪽을 버리지 않는다."""
    sink = RecordingSink()
    feed = _feed(sink)
    feed._handle_real(
        {"trnm": "REAL", "data": [
            {"type": "0B", "item": "005930", "values": {"10": "", "27": "70100", "28": "70000"}}
        ]}
    )
    assert feed.quote("005930") is None  # 종전대로 조용히 스킵
    assert sink.rows[0]["fields"] == {"bid": 70000.0, "ask": 70100.0}


# --------------------------------------------------------------------------
# ② 0w(프로그램매매) / 0F(거래원) 파싱
# --------------------------------------------------------------------------

def test_program_trade_frame_records_fid_codes_verbatim():
    """0w 의 FID 이름 매핑표는 우리 문서에 없다 — 이름을 붙이지 않고 코드 그대로 남긴다."""
    sink = RecordingSink()
    feed = _feed(sink)
    values = {"20": "090500", "202": "+120000", "204": "-3000"}
    feed._handle_real(
        {"trnm": "REAL", "data": [
            {"type": "0w", "name": "종목프로그램매매", "item": "005930", "values": values}
        ]}
    )

    assert len(sink.rows) == 1
    row = sink.rows[0]
    assert row["kind"] == "program"
    assert row["symbol"] == "005930"
    # 값도 형변환하지 않는다 — 부호/포맷 해석은 실프레임을 본 뒤에.
    assert row["fields"] == values


def test_broker_frame_records_fid_codes_verbatim():
    sink = RecordingSink()
    feed = _feed(sink)
    values = {"141": "키움증권", "142": "12345", "151": "미래에셋"}
    feed._handle_real(
        {"trnm": "REAL", "data": [
            {"type": "0F", "name": "주식당일거래원", "item": "000660", "values": values}
        ]}
    )

    assert len(sink.rows) == 1
    assert sink.rows[0]["kind"] == "broker"
    assert sink.rows[0]["symbol"] == "000660"
    assert sink.rows[0]["fields"] == values


def test_smart_flow_types_are_ignored_when_sink_absent():
    """싱크가 없으면 0w/0F 가 와도 아무 일도 일어나지 않는다(크래시 금지)."""
    feed = _feed()
    feed._handle_real(
        {"trnm": "REAL", "data": [{"type": "0w", "item": "005930", "values": {"202": "1"}}]}
    )
    assert feed.quote("005930") is None


# --------------------------------------------------------------------------
# ③ 미지/깨진 형식 → raw 보존
# --------------------------------------------------------------------------

def test_unknown_type_is_preserved_as_raw():
    """모르는 타입을 버리면 파서를 고칠 근거가 사라진다."""
    sink = RecordingSink()
    feed = _feed(sink)
    item = {"type": "0D", "item": "005930", "values": {"41": "70000"}}
    feed._handle_real({"trnm": "REAL", "data": [item]})

    assert sink.rows == []
    assert sink.raws == [item]


def test_malformed_smart_flow_item_is_preserved_as_raw():
    """type 은 아는데 모양이 어긋난 프레임(values 누락)도 raw 로 남긴다."""
    sink = RecordingSink()
    feed = _feed(sink)
    item = {"type": "0w", "item": "005930"}  # values 없음
    feed._handle_real({"trnm": "REAL", "data": [item]})

    assert sink.rows == []
    assert sink.raws == [item]


def test_non_dict_item_is_preserved_as_raw():
    sink = RecordingSink()
    feed = _feed(sink)
    feed._handle_real({"trnm": "REAL", "data": ["뜻밖의문자열"]})
    assert sink.raws == ["뜻밖의문자열"]


def test_sink_exception_never_escapes_and_warns_once(caplog):
    """수집기가 죽어도 시세 수신은 멈추지 않는다 — 예외를 삼키고 1회만 경고."""

    class ExplodingSink(RecordingSink):
        def record(self, **kwargs):
            raise RuntimeError("디스크 폭발")

    feed = _feed(ExplodingSink())
    with caplog.at_level("WARNING"):
        feed._handle_real(_README_0B_FRAME)
        feed._handle_real(_README_0B_FRAME)

    # Quote 는 정상적으로 갱신됐다 — 수집 실패가 시세를 막지 않았다.
    assert feed.quote("005930").price == 20800.0
    assert sum("세력 신호 싱크" in r.message for r in caplog.records) == 1


# --------------------------------------------------------------------------
# ④ 버퍼링 / flush 원칙 (tests/test_tick_log.py 와 같은 계약)
# --------------------------------------------------------------------------

def test_no_file_before_flush(tmp_path):
    sink = SmartFlowLogger(tmp_path / "smart_flow.jsonl", tmp_path / "raw", flush_seconds=30)
    sink.record("program", "005930", _T0, {"202": "1"})
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_writes_only_after_flush_seconds_elapsed(tmp_path):
    ledger = tmp_path / "smart_flow.jsonl"
    sink = SmartFlowLogger(ledger, tmp_path / "raw", flush_seconds=30)
    sink.record("program", "005930", _T0, {"202": "1"})

    assert sink.flush_if_due(_T0 + timedelta(seconds=10)) == 0
    assert not ledger.exists()

    assert sink.flush_if_due(_T0 + timedelta(seconds=31)) == 1
    assert _read_jsonl(ledger) == [
        {"ts": _T0.isoformat(), "symbol": "005930", "kind": "program", "fields": {"202": "1"}}
    ]


def test_append_only_accumulates_across_flushes(tmp_path):
    ledger = tmp_path / "smart_flow.jsonl"
    sink = SmartFlowLogger(ledger, tmp_path / "raw", flush_seconds=30)
    sink.record("program", "005930", _T0, {"202": "1"})
    sink.flush_if_due(_T0 + timedelta(seconds=31))
    sink.record("broker", "005930", _T0 + timedelta(seconds=40), {"141": "키움증권"})
    sink.flush_if_due(_T0 + timedelta(seconds=62))

    rows = _read_jsonl(ledger)
    assert [r["kind"] for r in rows] == ["program", "broker"]


def test_close_flushes_remaining_buffer(tmp_path):
    ledger = tmp_path / "smart_flow.jsonl"
    sink = SmartFlowLogger(ledger, tmp_path / "raw", flush_seconds=30)
    sink.record("program", "005930", _T0, {"202": "1"})
    assert sink.close() == 1
    assert len(_read_jsonl(ledger)) == 1


def test_raw_goes_to_dated_file(tmp_path):
    raw_dir = tmp_path / "raw"
    sink = SmartFlowLogger(tmp_path / "smart_flow.jsonl", raw_dir, flush_seconds=30)
    sink.record_raw({"type": "0D"}, _T0)
    sink.close()

    path = raw_dir / f"{_T0.date().isoformat()}.jsonl"
    assert _read_jsonl(path) == [{"ts": _T0.isoformat(), "raw": {"type": "0D"}}]


def test_unchanged_l1_is_not_rewritten(tmp_path):
    """최우선호가는 체결마다 따라오지만 대부분 직전과 같다 — 바뀐 순간만 남긴다."""
    ledger = tmp_path / "smart_flow.jsonl"
    sink = SmartFlowLogger(ledger, tmp_path / "raw", flush_seconds=0)
    for _ in range(3):
        sink.record("quote_l1", "005930", _T0, {"bid": 70000.0, "ask": 70100.0})
    sink.record("quote_l1", "005930", _T0, {"bid": 70000.0, "ask": 70200.0})
    sink.close()

    rows = _read_jsonl(ledger)
    assert [r["fields"]["ask"] for r in rows] == [70100.0, 70200.0]


def test_disabled_logger_leaves_no_trace(tmp_path):
    sink = SmartFlowLogger(tmp_path / "smart_flow.jsonl", tmp_path / "raw", enabled=False)
    sink.record("program", "005930", _T0, {"202": "1"})
    sink.record_raw({"x": 1}, _T0)
    assert sink.flush_if_due(_T0 + timedelta(hours=1)) == 0
    assert sink.close() == 0
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_write_failure_is_swallowed_and_warned_once(tmp_path, caplog):
    """쓰기 실패가 시세 수신을 막으면 안 된다 — 삼키고 1회만 경고."""
    blocked = tmp_path / "file.txt"
    blocked.write_text("나는 디렉토리가 아니다", encoding="utf-8")
    sink = SmartFlowLogger(blocked / "smart_flow.jsonl", tmp_path / "raw", flush_seconds=0)

    with caplog.at_level("WARNING"):
        sink.record("program", "005930", _T0, {"202": "1"})
        assert sink.flush_if_due(_T0 + timedelta(seconds=1)) == 0
        sink.record("program", "005930", _T0, {"202": "2"})
        assert sink.flush_if_due(_T0 + timedelta(seconds=2)) == 0

    assert sum("세력 신호 로그 쓰기 실패" in r.message for r in caplog.records) == 1


# --------------------------------------------------------------------------
# ⑤ 플래그 off → 구독 등록 자체가 없다
# --------------------------------------------------------------------------

def test_flag_off_registers_no_smart_flow_types():
    """기본값은 false — 실서버 검증 전이다. 구독 타입이 늘지 않아야 한다."""
    sink, types = build_smart_flow({})
    assert sink is None
    assert types == [REALTIME_TICK_TYPE]
    assert not set(SMART_FLOW_TYPES) & set(types)


def test_flag_explicitly_false_registers_no_smart_flow_types():
    sink, types = build_smart_flow({"kiwoom": {"realtime": {"smart_flow_enabled": False}}})
    assert sink is None
    assert types == [REALTIME_TICK_TYPE]


def test_flag_on_adds_types_to_the_existing_subscription():
    """키움 WS 세션은 계정당 1개 — 새 연결이 아니라 기존 REG 에 타입을 얹는다."""
    sink, types = build_smart_flow({"kiwoom": {"realtime": {"smart_flow_enabled": True}}})
    assert isinstance(sink, SmartFlowLogger)
    assert types == [REALTIME_TICK_TYPE, *SMART_FLOW_TYPES]


def test_feed_reg_payload_carries_configured_types():
    """구독 타입이 실제로 REG 프레임에 실린다(배선 회귀 방지)."""
    _, types = build_smart_flow({"kiwoom": {"realtime": {"smart_flow_enabled": True}}})
    feed = KiwoomRealtimeFeed(access_token="tok", symbols=["005930"], types=types)
    assert feed._types == ["0B", "0w", "0F"]

    feed_off = KiwoomRealtimeFeed(access_token="tok", symbols=["005930"])
    assert feed_off._types == ["0B"]
