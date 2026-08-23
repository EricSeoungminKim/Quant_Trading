from datetime import date
from pathlib import Path

from quant.collect.snapshot import collect, load_snapshot, run_source, save_snapshot
from quant.collect.contracts import Snapshot


def test_run_source_captures_success():
    r = run_source("x", "https://example.test", lambda: {"v": 1})
    assert r.ok and r.data == {"v": 1} and r.error is None
    assert r.latency_ms >= 0


def test_run_source_converts_exception_to_failed_result():
    def boom():
        raise RuntimeError("네트워크 끊김")

    r = run_source("x", "https://example.test", boom)
    assert not r.ok and r.data is None
    assert "RuntimeError" in r.error and "네트워크 끊김" in r.error


def test_run_source_truncates_long_errors():
    def boom():
        raise RuntimeError("A" * 500)

    assert len(run_source("x", "u", boom).error) <= 160


def test_run_source_redacts_secret_query_params_in_error():
    """httpx 의 HTTPStatusError 는 URL 전체(키 포함)를 메시지에 담는다 — 그게
    그대로 error 필드로 스냅샷에 영속화되면 시크릿이 파일·백업 번들에 남는다.
    2026-08-16 실측: FRED 502 에러에 api_key=... 가 그대로 저장돼 있었다."""
    def boom():
        raise RuntimeError(
            "Server error '502 Bad Gateway' for url "
            "'https://api.stlouisfed.org/fred/series?series_id=X&api_key=SECRET123&file_type=json'"
        )

    err = run_source("macro", "https://api.stlouisfed.org/fred", boom).error
    assert "SECRET123" not in err
    assert "api_key=***" in err
    assert "series_id=X" in err  # 시크릿이 아닌 파라미터는 남긴다 — 디버깅 정보


def test_run_source_redacts_dart_and_generic_secret_params():
    def boom():
        raise RuntimeError("url 'https://x/api?crtfc_key=DARTKEY9&page_no=1&token=TOK5'")

    err = run_source("dart", "u", boom).error
    assert "DARTKEY9" not in err and "TOK5" not in err
    assert "crtfc_key=***" in err and "token=***" in err and "page_no=1" in err


def test_collect_isolates_failures():
    snap = collect(
        "KR",
        date(2026, 8, 12),
        {
            "good": ("https://example.test/g", lambda: {"a": 1}),
            "bad": (
                "https://example.test/b",
                lambda: (_ for _ in ()).throw(ValueError("nope")),
            ),
        },
    )
    assert snap.results["good"].ok
    assert not snap.results["bad"].ok
    assert snap.missing() == ["bad"]
    assert snap.market == "KR"


def test_collect_result_is_json_roundtrippable():
    snap = collect("US", date(2026, 8, 12), {"g": ("u", lambda: {"a": 1})})
    assert Snapshot.from_json(snap.to_json()) == snap


def test_save_and_load_roundtrip(tmp_path: Path):
    snap = collect("KR", date(2026, 8, 12), {"g": ("https://x.test", lambda: {"a": 1})})
    path = save_snapshot(snap, tmp_path)
    assert path.exists()
    assert load_snapshot(path) == snap


def test_save_path_layout(tmp_path: Path):
    snap = collect("US", date(2026, 8, 12), {"g": ("https://x.test", lambda: {"a": 1})})
    path = save_snapshot(snap, tmp_path)
    assert path.relative_to(tmp_path).as_posix() == "US/2026-08-12.json"
