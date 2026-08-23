from datetime import date, datetime

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult


def _snap() -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market="KR",
        session_date=date(2026, 8, 12),
        generated_at=datetime(2026, 8, 12, 8, 0, tzinfo=KST),
        results={
            "market": SourceResult(
                key="market", ok=True, data={"KOSPI": 6345.5}, error=None,
                url="https://example.test/a",
                fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST),
                latency_ms=120,
            ),
            "flow": SourceResult(
                key="flow", ok=False, data=None, error="HTTP 500",
                url="https://example.test/b",
                fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST),
                latency_ms=8000,
            ),
        },
    )


def test_roundtrip_preserves_everything():
    snap = _snap()
    assert Snapshot.from_json(snap.to_json()) == snap


def test_missing_lists_failed_sources_only():
    assert _snap().missing() == ["flow"]


def test_json_is_stable_for_same_snapshot():
    # 같은 스냅샷은 항상 같은 바이트여야 재현성 검증이 가능하다
    assert _snap().to_json() == _snap().to_json()
