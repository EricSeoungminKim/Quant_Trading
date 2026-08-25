"""미국장 마감 직후 종합 리포트(uswrap, 2026-08-25 소유자 지시).

핵심 계약: (1) 없는 데이터를 지어내지 않는다 — sectors/market/vix_term 중
빠진 소스는 그 부분만 생략. (2) tone/섹터 판정은 `us_kr_bridge`를 재사용할
뿐 여기서 다시 채점하지 않는다. (3) `uswrap` CLI 는 이미 저장된 그날 US
스냅샷이 있으면 재사용하고, 없을 때만 sectors/market/vix_term 3개만
최소 수집한다 — 다른 소스(news/toss_rankings 등)는 절대 건드리지 않는다.
(4) `load_latest_us_wrap`은 조회 시작일(`before_date`) 당일 파일부터 찾는다
— KR 아침판과 그 전날 새벽 uswrap 은 같은 KST 달력 날짜에 일어나므로
시작일을 제외하면 매번 하루 어긋난다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.collect.snapshot import save_snapshot
from quant.report.collect.uswrap import build_us_wrap, load_latest_us_wrap, write_us_wrap

_AT = date(2026, 8, 25)

_SECTORS = {
    "sectors": [
        {"ticker": "XLV", "name": "헬스케어", "change_pct": 3.5},
        {"ticker": "XLE", "name": "에너지", "change_pct": 1.2},
        {"ticker": "XLK", "name": "기술", "change_pct": 0.4},
        {"ticker": "XLF", "name": "금융", "change_pct": -1.1},
    ],
}
_MEMBERS = {
    "생물공학": [{"code": "207940", "name": "삼성바이오로직스", "change_pct": 4.2}],
    "석유와가스": [{"code": "010950", "name": "S-Oil", "change_pct": 0.9}],
}
_MARKET = {
    "quotes": {
        "^GSPC": {"label": "S&P500", "close": 5900.0, "change_pct": 0.85},
        "^IXIC": {"label": "NASDAQ", "close": 19000.0, "change_pct": 1.2},
        "^DJI": {"label": "다우", "close": 42000.0},  # change_pct 없음 — 첫 거래일 등
        "KRW=X": {"label": "USD/KRW", "close": 1350.0, "change_pct": -0.1},  # 지수 아님
    },
}
_VIX = {
    "points": [
        {"label": "VIX 9D", "symbol": "^VIX9D", "value": 12.0, "change_pct": -1.0},
        {"label": "VIX", "symbol": "^VIX", "value": 14.5, "change_pct": 2.1},
    ],
    "structure": "콘탱고 (정상)",
    "spread": 1.3,
}


# ------------------------------------------------------------------ build_us_wrap


def test_build_us_wrap_combines_bridge_indices_and_vix():
    out = build_us_wrap(_SECTORS, _MARKET, _VIX, _MEMBERS)
    assert out["tone"] in ("상승 우위", "하락 우위", "혼조")
    assert out["us_sectors"][0]["name"] == "헬스케어"  # 등락순 1위
    assert out["kr_focus"][0]["us_name"] == "헬스케어"
    assert out["kr_focus"][0]["stocks"][0]["code"] == "207940"
    assert out["vix"] == {"value": 14.5, "change_pct": 2.1, "structure": "콘탱고 (정상)"}
    idx_symbols = {i["symbol"] for i in out["indices"]}
    assert idx_symbols == {"^GSPC", "^IXIC"}  # DJI 는 change_pct 없어 제외, KRW 는 지수 아님


def test_build_us_wrap_missing_sectors_omits_bridge_fields_but_keeps_rest():
    out = build_us_wrap(None, _MARKET, _VIX, _MEMBERS)
    assert "tone" not in out and "us_sectors" not in out and "kr_focus" not in out
    assert out["indices"] and out["vix"]


def test_build_us_wrap_missing_market_omits_indices():
    out = build_us_wrap(_SECTORS, None, _VIX, _MEMBERS)
    assert "indices" not in out
    assert out["tone"]


def test_build_us_wrap_missing_vix_omits_vix():
    out = build_us_wrap(_SECTORS, _MARKET, None, _MEMBERS)
    assert "vix" not in out
    assert out["indices"]


def test_build_us_wrap_all_inputs_missing_returns_none():
    assert build_us_wrap(None, None, None, None) is None
    assert build_us_wrap({}, {}, {}, None) is None


def test_build_us_wrap_missing_sector_members_still_returns_tone_with_empty_focus():
    # sector_members 없이도(kr_focus 계산 불가) tone/us_sectors 는 나온다 —
    # build_us_kr_bridge 의 "멤버 없음 → 빈 stocks" 계약(사용자 지시: 없는 데이터로
    # 고르는 척하지 않는다)을 그대로 물려받는다.
    out = build_us_wrap(_SECTORS, None, None, None)
    assert out["tone"]
    assert out["kr_focus"][0]["stocks"] == []


# ------------------------------------------------------------------ write_us_wrap / load_latest_us_wrap


def test_write_us_wrap_saves_dated_json_with_date_field(tmp_path: Path):
    payload = build_us_wrap(_SECTORS, _MARKET, _VIX, _MEMBERS)
    path = write_us_wrap(payload, tmp_path / "out", _AT)
    assert path.relative_to(tmp_path / "out").as_posix() == "2026/08/25/US_wrap.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["date"] == "2026-08-25"
    assert saved["tone"] == payload["tone"]


def test_load_latest_us_wrap_finds_file_for_before_date_itself(tmp_path: Path):
    """KR 아침판과 그 전날 새벽 uswrap 은 같은 KST 달력 날짜 — before_date 당일
    파일을 반드시 찾아야 한다(시작일 제외 시 매번 하루 어긋나는 결함)."""
    out_root = tmp_path / "out"
    write_us_wrap({"tone": "상승 우위"}, out_root, _AT)
    found = load_latest_us_wrap(out_root, _AT)
    assert found is not None and found["tone"] == "상승 우위"


def test_load_latest_us_wrap_looks_back_within_max_days(tmp_path: Path):
    out_root = tmp_path / "out"
    stale_date = date(2026, 8, 22)  # 3일 전(토요일치, 예: 금요일 마감분)
    write_us_wrap({"tone": "혼조"}, out_root, stale_date)
    found = load_latest_us_wrap(out_root, _AT, max_back_days=4)
    assert found is not None and found["tone"] == "혼조"


def test_load_latest_us_wrap_returns_none_beyond_max_back_days(tmp_path: Path):
    out_root = tmp_path / "out"
    too_old = date(2026, 8, 19)  # 6일 전
    write_us_wrap({"tone": "혼조"}, out_root, too_old)
    assert load_latest_us_wrap(out_root, _AT, max_back_days=4) is None


def test_load_latest_us_wrap_returns_none_when_nothing_saved(tmp_path: Path):
    assert load_latest_us_wrap(tmp_path / "out", _AT) is None


# ------------------------------------------------------------------ CLI: `report uswrap`


def _source_result(key: str, data: dict | None) -> SourceResult:
    return SourceResult(
        key=key, ok=data is not None, data=data, error=None if data is not None else "x",
        url="u", fetched_at=report_cli.datetime.now(report_cli.KST), latency_ms=1,
    )


def test_uswrap_cli_reuses_existing_snapshot_without_collecting(tmp_path, monkeypatch, capsys):
    """그날 US 스냅샷이 이미 있으면 재사용 — `collect`/`build_sources` 를 호출하면
    테스트가 실패하도록 막아서 "새로 수집하지 않는다"를 실측으로 강제한다."""
    def _boom(*a, **k):
        raise AssertionError("기존 스냅샷이 있으면 재수집하면 안 된다")

    monkeypatch.setattr(report_cli, "build_sources", _boom)
    monkeypatch.setattr(report_cli, "collect", _boom)

    snap_root = tmp_path / "data" / "snapshots"
    snap = Snapshot(SCHEMA_VERSION, "US", _AT, report_cli.datetime.now(report_cli.KST), {
        "sectors": _source_result("sectors", _SECTORS),
        "market": _source_result("market", _MARKET),
        "vix_term": _source_result("vix_term", _VIX),
    })
    save_snapshot(snap, snap_root)

    rc = report_cli.main(["uswrap", "--date", _AT.isoformat(), "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "기존 US 스냅샷 재사용" in out

    saved = json.loads(
        (tmp_path / "out" / "2026" / "08" / "25" / "US_wrap.json").read_text(encoding="utf-8")
    )
    assert saved["date"] == "2026-08-25"
    assert saved["tone"]


def test_uswrap_cli_collects_minimal_sources_when_no_snapshot(tmp_path, monkeypatch, capsys):
    """스냅샷이 없으면 sectors/market/vix_term 만 최소 수집 — build_sources 가
    돌려주는 다른 소스(news 등)는 절대 안 부른다."""
    called = []

    def _fake_build_sources(market_code, session_date, news_since=None):
        assert market_code == "US"

        def _track(key, data):
            def _fn():
                called.append(key)
                return data
            return _fn

        return {
            "sectors": ("u", _track("sectors", _SECTORS)),
            "market": ("u", _track("market", _MARKET)),
            "vix_term": ("u", _track("vix_term", _VIX)),
            "news": ("u", _track("news", {"feeds": {}})),
            "toss_rankings": (
                "u",
                lambda: (_ for _ in ()).throw(AssertionError("news/toss_rankings 는 부르면 안 된다")),
            ),
        }

    monkeypatch.setattr(report_cli, "build_sources", _fake_build_sources)

    rc = report_cli.main(["uswrap", "--date", _AT.isoformat(), "--root", str(tmp_path)])
    assert rc == 0
    assert set(called) == {"sectors", "market", "vix_term"}  # news 는 호출 안 됨

    assert (tmp_path / "out" / "2026" / "08" / "25" / "US_wrap.json").exists()
    assert not (tmp_path / "data" / "snapshots" / "US" / "2026-08-25.json").exists()


def test_uswrap_cli_no_data_writes_nothing_and_prints_skip_message(tmp_path, monkeypatch, capsys):
    def _fake_build_sources(market_code, session_date, news_since=None):
        def _fail():
            raise RuntimeError("network down")
        return {
            "sectors": ("u", _fail), "market": ("u", _fail), "vix_term": ("u", _fail),
        }

    monkeypatch.setattr(report_cli, "build_sources", _fake_build_sources)

    rc = report_cli.main(["uswrap", "--date", _AT.isoformat(), "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "US wrap 생략" in out
    assert not (tmp_path / "out" / "2026" / "08" / "25" / "US_wrap.json").exists()


def test_uswrap_cli_no_market_flag_needed():
    """uswrap 은 US 전용이라 --market 없이도 인자 파싱이 성공해야 한다."""
    with pytest.raises(SystemExit) as exc:
        report_cli.main(["uswrap", "--date", "2026-08-25", "--root", ".", "--market", "US"])
    # --market 은 정의돼 있지 않으므로 argparse 가 에러(exit code 2)로 거부해야
    # 정상 — "US 전용이라 시장 인자가 없다"는 설계를 실측으로 고정한다.
    assert exc.value.code == 2
