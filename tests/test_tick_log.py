"""TickLogger — 버퍼링/flush 주기/append-only/실패 격리 계약을 검증한다.

quant/adapters/tick_log.py 참고: 거래 핫패스에서 record()는 메모리 버퍼 append만
하고, 디스크 쓰기는 flush_if_due가 flush_seconds 경과 시에만 한 번(append-only)
한다. 실패는 절대 예외로 새어나가지 않는다."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

from quant.adapters.tick_log import TickLogger

_T0 = datetime(2026, 8, 28, 9, 0, 0, tzinfo=timezone.utc)


def _read_jsonl(path):
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_no_file_before_flush(tmp_path):
    """① 버퍼링: flush 전에는 파일이 없다."""
    logger = TickLogger(tmp_path, flush_seconds=30)
    logger.record("TQQQ", 50.0, _T0)
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_writes_only_after_flush_seconds_elapsed(tmp_path):
    """② flush_seconds 경과 후에만 쓴다."""
    logger = TickLogger(tmp_path, flush_seconds=30)
    logger.record("TQQQ", 50.0, _T0)

    written = logger.flush_if_due(_T0 + timedelta(seconds=10))
    assert written == 0
    assert list(tmp_path.rglob("*.jsonl")) == []

    written = logger.flush_if_due(_T0 + timedelta(seconds=31))
    assert written == 1
    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert rows == [{"ts": _T0.isoformat(), "symbol": "TQQQ", "price": 50.0}]


def test_append_only_accumulates_across_flushes(tmp_path):
    """③ append-only: 두 번 flush 하면 행이 누적된다."""
    logger = TickLogger(tmp_path, flush_seconds=30)
    logger.record("TQQQ", 50.0, _T0)
    logger.flush_if_due(_T0 + timedelta(seconds=31))

    logger.record("TQQQ", 51.0, _T0 + timedelta(seconds=35))
    logger.flush_if_due(_T0 + timedelta(seconds=62))

    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert [r["price"] for r in rows] == [50.0, 51.0]


def test_write_failure_does_not_raise(tmp_path):
    """④ 쓰기 실패(권한 없는 경로 등)해도 예외가 새어나오지 않는다."""
    root = tmp_path / "readonly_root"
    root.mkdir()
    os.chmod(root, stat.S_IREAD | stat.S_IEXEC)  # 쓰기 금지 — mkdir/open이 실패한다
    try:
        logger = TickLogger(root, flush_seconds=30)
        logger.record("TQQQ", 50.0, _T0)
        # 예외가 새어나오면 이 테스트는 실패한다(assertRaises 없이 그냥 호출).
        written = logger.flush_if_due(_T0 + timedelta(seconds=31))
        assert written == 0
    finally:
        os.chmod(root, stat.S_IRWXU)  # tmp_path 정리를 위해 권한 복구


def test_close_flushes_remaining_buffer(tmp_path):
    """⑤ close 가 flush_seconds 미도달이어도 남은 버퍼를 쓴다."""
    logger = TickLogger(tmp_path, flush_seconds=30)
    logger.record("TQQQ", 50.0, _T0)
    logger.close()

    files = list(tmp_path.rglob("*.jsonl"))
    assert len(files) == 1
    rows = _read_jsonl(files[0])
    assert rows == [{"ts": _T0.isoformat(), "symbol": "TQQQ", "price": 50.0}]


def test_date_boundary_uses_separate_files(tmp_path):
    """⑥ 일자 경계에서 다른 파일로 간다."""
    logger = TickLogger(tmp_path, flush_seconds=0)
    day1 = _T0
    day2 = _T0 + timedelta(days=1)
    logger.record("TQQQ", 50.0, day1)
    logger.flush_if_due(day1)  # flush_seconds=0 — 즉시 due, day1 파일에 쓴다

    logger.record("TQQQ", 55.0, day2)
    logger.flush_if_due(day2)  # day2 파일로 분리된다

    us_dir = tmp_path / "US"
    files = sorted(us_dir.glob("*.jsonl"))
    assert [f.name for f in files] == [
        f"{day1.date().isoformat()}.jsonl",
        f"{day2.date().isoformat()}.jsonl",
    ]


def test_disabled_does_nothing(tmp_path):
    """⑦ enabled=False 면 아무 일도 안 한다."""
    logger = TickLogger(tmp_path, flush_seconds=0, enabled=False)
    logger.record("TQQQ", 50.0, _T0)
    assert logger.flush_if_due(_T0 + timedelta(seconds=100)) == 0
    logger.close()
    assert list(tmp_path.rglob("*.jsonl")) == []


def test_same_second_dedup_keeps_last_value(tmp_path):
    """같은 (symbol, 초)로 두 번 기록하면 마지막 값으로 덮는다."""
    logger = TickLogger(tmp_path, flush_seconds=0)
    logger.record("TQQQ", 50.0, _T0)
    logger.record("TQQQ", 50.5, _T0.replace(microsecond=500_000))
    logger.flush_if_due(_T0)

    files = list(tmp_path.rglob("*.jsonl"))
    rows = _read_jsonl(files[0])
    assert len(rows) == 1
    assert rows[0]["price"] == 50.5


def test_market_routing_splits_kr_and_us(tmp_path):
    """market_of_symbol 재사용 — KR(6자리 숫자)/US 심볼이 다른 디렉터리로 간다."""
    logger = TickLogger(tmp_path, flush_seconds=0)
    logger.record("TQQQ", 50.0, _T0)
    logger.record("005930", 70000.0, _T0)
    logger.flush_if_due(_T0)

    assert (tmp_path / "US" / f"{_T0.date().isoformat()}.jsonl").exists()
    assert (tmp_path / "KR" / f"{_T0.date().isoformat()}.jsonl").exists()
