"""`report_cli._derive` 의 기준가 시세 대상 — 채점 루프(E) Task 2, §E-1.

`codes = [code for code, _ in rank(cont)]` 는 HTML 노출용 상위 10 제한을 시세
조회에도 그대로 적용해 버렸다 — 오늘 뉴스에 12개 종목이 잡혀도 시세는 10개만
채워지고, 나머지 2개는 `selections` 원장에 close 없는 행으로 영원히 남는다
(outcomes 가 채울 수 없다). 시세 대상은 **cont 전체**여야 한다. HTML 카드 노출
(`rank` 호출)과 `stock_detail`(limit 6)은 이 변경과 무관해야 한다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.analyze.opendays import anchor_dir_for
from quant.apps import report_cli
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.report.collect import core as report_core
from quant.report.collect import midterm as report_midterm

KST = ZoneInfo("Asia/Seoul")


def _cont_entry(rank: int) -> dict:
    return {
        "name": f"종목{rank}",
        "days": 1,
        "articles": 1,
        "today_articles": 12 - rank,  # 뚜렷한 순위 차 — rank() 정렬 확인용
        "streak_days": 1,
        "is_new": True,
        "history": [True],
        "titles": [],
    }


def _snap(market: str) -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=date(2026, 8, 15),
        generated_at=datetime(2026, 8, 15, 21, 50, tzinfo=KST),
        results={},
    )


def _wire_cont(monkeypatch, cont: dict) -> None:
    """뉴스 추출 파이프라인을 우회하고 `cont` 를 그대로 continuity() 결과로 만든다."""
    monkeypatch.setattr(report_core, "load_us_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_core, "collect_mentions", lambda snap, table, market: [])
    monkeypatch.setattr(report_core, "append_ledger", lambda mentions, path: 0)
    monkeypatch.setattr(report_core, "load_ledger", lambda path: [])
    monkeypatch.setattr(
        report_core, "continuity", lambda ledger, today, market=None: cont
    )


def test_derive_requests_quotes_for_the_entire_universe_not_just_top_10(
    monkeypatch, tmp_path
):
    codes = [f"SYM{i:02d}" for i in range(12)]
    cont = {c: _cont_entry(i) for i, c in enumerate(codes)}
    _wire_cont(monkeypatch, cont)

    requested: list[str] = []

    def fake_fetch(symbols):
        requested.extend(symbols)
        return {s: {"close": 100.0, "change_pct": 0.0, "history": [100.0]} for s in symbols}

    monkeypatch.setattr(report_core, "fetch_symbol_quotes", fake_fetch)

    snap = _snap("US")
    report_cli._derive(snap, tmp_path, tmp_path / "snapshots")

    assert len(requested) == 12, f"12개 전부가 아니라 {len(requested)}개만 시세 요청됨"
    assert set(requested) == set(codes)


def test_derive_mapping_and_lookup_failure_counts_cover_the_full_expanded_list(
    monkeypatch, tmp_path, capsys
):
    """시세 미확보 카운트 로그 회귀 — **조사 대상 전체(12개) 기준**으로 세야 한다
    (예전처럼 상위 10 만 셌다면 미확보가 실제보다 적게 잡힌다).

    2026-08-26: 문구가 "매핑실패 N / 조회실패 M"에서 "시세 미확보 K"로 바뀌었다 —
    KIND 403 폴백(.KS/.KQ 양쪽 조회, `quant/report/collect/quotes.py`)이 들어오면서
    '매핑' 단계 자체가 사라졌기 때문이다. 매핑에서 빠진 2개도 이제는 폴백으로
    조회를 시도하므로, 여기서는 **끝내 시세가 없는 종목 수**(12-8=4)를 센다.
    이 테스트가 지키는 성질(전체 목록 기준 집계)은 그대로다."""
    codes = [f"KR{i:02d}" for i in range(12)]
    cont = {c: _cont_entry(i) for i, c in enumerate(codes)}
    _wire_cont(monkeypatch, cont)

    # 12개 중 10개만 KIND 매핑 존재 — 나머지 2개는 .KS/.KQ 폴백으로 조회 시도된다
    mapped = codes[:10]
    market_map = {c: f"{c}.KS" for c in mapped}
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: market_map)
    monkeypatch.setattr(report_core, "fetch_many", lambda codes, limit=6: {})

    # 요청분 중 앞 8개만 시세가 온다 → 끝내 미확보 4건(12-8)
    def fake_fetch(symbols):
        ok = symbols[:8]
        return {s: {"close": 1.0, "change_pct": 0.0} for s in ok}

    monkeypatch.setattr(report_core, "fetch_symbol_quotes", fake_fetch)

    snap = _snap("KR")
    report_cli._derive(snap, tmp_path, tmp_path / "snapshots")

    err = capsys.readouterr().err
    assert "시세 미확보 4건 / 조사 12건" in err


# ── §E-2 실배선: _derive → baselines → machine_payload (2026-08-15 리뷰 M3) ──

def _uptrend_ohlcv(n: int = 60) -> pd.DataFrame:
    """`tests/test_baseline.py` 의 픽스처와 같은 모양 — 실제 `baseline_score`
    (mock 아님)가 None 이 아닌 점수를 내도록 충분한 행 수(>=30)·상승 추세."""
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    base = pd.Series(range(n), index=idx, dtype=float)
    close = 100 + base * 0.5
    return pd.DataFrame({"open": close - 0.2, "high": close + 0.5,
                         "low": close - 0.5, "close": close,
                         "volume": [1_000_000] * n}, index=idx)


def test_derive_wires_baseline_score_into_the_machine_payload(monkeypatch, tmp_path):
    """`_derive` 가 `sym_quotes` 의 실 ohlcv 로 `baseline_score`(mock 아님)를 돌려
    `machine_payload` 의 symbols 항목에 `baseline_score100` 으로 실어야 한다 —
    Task 5 진행 기록의 커버리지 공백(실배선이 단위 테스트로만 각각 확인됐고
    전체 파이프라인으로는 확인된 적이 없었다)."""
    codes = ["SYM00"]
    cont = {c: _cont_entry(i) for i, c in enumerate(codes)}
    _wire_cont(monkeypatch, cont)

    df = _uptrend_ohlcv()

    def fake_fetch(symbols):
        return {s: {"close": 100.0, "change_pct": 0.0, "ohlcv": df} for s in symbols}

    monkeypatch.setattr(report_core, "fetch_symbol_quotes", fake_fetch)

    snap = _snap("US")
    _, _, _, payload, _, _, _, _ = report_cli._derive(snap, tmp_path, tmp_path / "snapshots")

    entries = {e["symbol"]: e for e in payload["symbols"]}
    assert "SYM00" in entries
    assert entries["SYM00"]["baseline_score100"] is not None
    assert isinstance(entries["SYM00"]["baseline_score100"], int)


# ── §E-2 결함 방어: 오염된 ohlcv 한 개가 build 전체를 죽이면 안 된다 (I1) ──

def test_derive_baseline_loop_survives_one_corrupted_ohlcv_frame(
    monkeypatch, tmp_path, capsys
):
    """베이스라인 점수 루프(§228-235행)는 트렌딩(180행)·시세(210행) 루프와 같은
    관례로 try/except 로 감싸야 한다(2026-08-15 리뷰 I1) — object 인덱스처럼
    오염된 ohlcv 프레임 하나가 섞여도 그 심볼만 키가 빠지고 나머지는 살아야 한다.
    """
    codes = ["GOOD", "BAD"]
    cont = {c: _cont_entry(i) for i, c in enumerate(codes)}
    _wire_cont(monkeypatch, cont)

    good_df = _uptrend_ohlcv()
    # object 인덱스(문자열) — baseline_score 내부의 `d < today` 비교가 str vs
    # date 비교로 TypeError 를 던진다(_trend_score 호출 이전이라 baseline.py의
    # 자체 방어에 안 걸린다).
    bad_df = pd.DataFrame(
        {"open": [100.0] * 5, "high": [101.0] * 5, "low": [99.0] * 5,
         "close": [100.0] * 5, "volume": [1_000_000] * 5},
        index=["not-a-date"] * 5,
    )

    def fake_fetch(symbols):
        frames = {"GOOD": good_df, "BAD": bad_df}
        return {s: {"close": 100.0, "change_pct": 0.0, "ohlcv": frames[s]} for s in symbols}

    monkeypatch.setattr(report_core, "fetch_symbol_quotes", fake_fetch)

    snap = _snap("US")
    # 예외가 새면 이 호출 자체가 죽는다 — 죽지 않는 것 자체가 1차 검증.
    _, _, _, payload, _, _, _, _ = report_cli._derive(snap, tmp_path, tmp_path / "snapshots")

    err = capsys.readouterr().err
    assert "BAD" in err  # 오염된 심볼이 로그로 남는다(조용히 사라지지 않는다)

    entries = {e["symbol"]: e for e in payload["symbols"]}
    assert "baseline_score100" not in entries["BAD"]
    assert entries["GOOD"]["baseline_score100"] is not None  # 다른 심볼은 살아 있다


# ── 개장일 집계 배선(§G Task 3) — `_emit` 이 휴장 기간 engine.json 을 병합하는지 ──

def _write_qqq_anchor(root, dates: list[str]) -> None:
    """`opendays.last_open_day` 가 마지막 개장일을 판정하게 하는 최소 QQQ 앵커 픽스처
    (`tests/test_opendays.py::_write_daily` 와 같은 04:00 UTC 인덱스 관례)."""
    anchor = root / "data" / "history" / "QQQ" / "1d"
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d, tz="UTC") + pd.Timedelta(hours=4) for d in dates]
    )
    n = len(dates)
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1000.0] * n,
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = anchor / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _write_engine_json(out_root, market: str, d: date, payload: dict) -> None:
    path = out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_emit_merges_carryover_window_and_keeps_selections_ledger_free_of_carried_symbols(
    monkeypatch, tmp_path,
):
    """마지막 개장일 금(08-14) → 주말을 건너 월(08-17) 리포트가 08-15/08-16 을
    집계 창으로 잡아야 한다. 08-15 는 일부러 안 심어서 '누락 날짜는 건너뛴다'도
    같이 확인한다((f) 배선 + 누락 스킵).

    **§G Task 4 와의 상호작용**: 08-17(월)은 앵커 기준 `last_open+1`(토, 08-15)
    을 넘겨 원장 기록 게이트가 건너뛴다(Task 4) — 앵커 일봉은 그날 마감 후에야
    갱신되므로 개장 전 리포트 시점엔 항상 하루 지연이 있다. 병합 자체
    (engine.json/HTML)는 게이트와 무관하게 그대로 실행된다는 것도 이 테스트가
    같이 확인한다."""
    _write_qqq_anchor(tmp_path, ["2026-08-14"])

    cont = {"AAPL": _cont_entry(0)}
    _wire_cont(monkeypatch, cont)
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 100.0, "change_pct": 0.0} for s in syms},
    )
    monkeypatch.setattr(report_cli, "_fetch_youtube_briefs", lambda: {})
    # 텔레그램 인사이트(서브프로젝트 S part 2) — _emit 이 실제 네트워크를
    # 타지 않게 유튜브와 같은 이유로 막는다.
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})

    out_root = tmp_path / "out"
    prior_payload = {
        "schema": 1, "market": "US", "session_date": "2026-08-16",
        "symbols": [{"symbol": "MSFT", "name": "마이크로소프트", "trending_score100": 42}],
        "auto_watch": "AUTO_WATCH: MSFT:RANK",
    }
    _write_engine_json(out_root, "US", date(2026, 8, 16), prior_payload)  # 08-15 는 없음(스킵 확인)

    snap = Snapshot(
        schema_version=SCHEMA_VERSION, market="US",
        session_date=date(2026, 8, 17),
        generated_at=datetime(2026, 8, 17, 21, 50, tzinfo=KST),
        results={},
    )

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    engine_path = out_root / "2026" / "08" / "17" / "US_engine.json"
    merged = json.loads(engine_path.read_text(encoding="utf-8"))
    by_symbol = {s["symbol"]: s for s in merged["symbols"]}
    assert "AAPL" in by_symbol and "carried_from" not in by_symbol["AAPL"]
    assert by_symbol["MSFT"]["carried_from"] == "2026-08-16"
    assert "MSFT:RANK" in merged["auto_watch"]
    assert "AAPL:" in merged["auto_watch"]

    # 원장 기록 게이트(Task 4) — 08-17(월)은 last_open(금)+1 을 넘겨 선정 원장
    # 기록 자체를 건너뛴다. 게이트가 병합·렌더·write_machine 을 막지 않는다는
    # 건 위 engine.json/HTML 검증이 이미 확인했다.
    ledger_path = tmp_path / "data" / "ledger" / "selections.jsonl"
    assert not ledger_path.exists()

    html = (out_root / "2026" / "08" / "17" / "US_report.html").read_text(encoding="utf-8")
    assert "badge-carry" in html
    assert "08-16 발" in html
    assert "마이크로소프트" in html


def test_emit_skips_carryover_merge_when_anchor_data_is_absent(monkeypatch, tmp_path, capsys):
    """앵커(QQQ 1d) 데이터 자체가 없으면 개장일 판정이 안 된다 — 병합 없이
    기존 동작(오늘 payload 그대로) + stderr 로그 1줄(모듈 계약: 안전한 방향은
    무집계이지 억지 추정이 아니다)."""
    cont = {"AAPL": _cont_entry(0)}
    _wire_cont(monkeypatch, cont)
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 100.0, "change_pct": 0.0} for s in syms},
    )
    monkeypatch.setattr(report_cli, "_fetch_youtube_briefs", lambda: {})
    # 텔레그램 인사이트(서브프로젝트 S part 2) — _emit 이 실제 네트워크를
    # 타지 않게 유튜브와 같은 이유로 막는다.
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})

    out_root = tmp_path / "out"
    snap = Snapshot(
        schema_version=SCHEMA_VERSION, market="US",
        session_date=date(2026, 8, 17),
        generated_at=datetime(2026, 8, 17, 21, 50, tzinfo=KST),
        results={},
    )

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    err = capsys.readouterr().err
    assert "이월 병합 생략" in err

    merged = json.loads(
        (out_root / "2026" / "08" / "17" / "US_engine.json").read_text(encoding="utf-8")
    )
    by_symbol = {s["symbol"]: s for s in merged["symbols"]}
    assert "AAPL" in by_symbol
    assert not any("carried_from" in s for s in merged["symbols"])


# ── 원장 기록 게이트(§G Task 4) — 휴장일 재기록 차단 ──

def _write_daily_anchor(root, market: str, dates: list[str]) -> None:
    """`opendays.last_open_day` 가 마지막 개장일을 판정하게 하는 최소 앵커 픽스처
    (`_write_qqq_anchor` 와 같은 관례, market 을 인자로 받는 범용판)."""
    anchor = anchor_dir_for(market, root)
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz="UTC") for d in dates])
    n = len(dates)
    df = pd.DataFrame({
        "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
        "close": [100.5] * n, "volume": [1000.0] * n,
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = anchor / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _kr_snap_with_rankings(session_date: date) -> Snapshot:
    """KR 스냅샷 — `_record_flows`(KR 전용)·`_log_overlap`(토스 랭킹 필요)이
    둘 다 실행되는 조건을 만든다."""
    boards = {"거래상위": [{"rank": 1, "symbol": "005930", "price": 70000.0,
                           "change_pct": 1.0, "trading_amount": 1000}]}
    at = datetime(session_date.year, session_date.month, session_date.day, 8, 0, tzinfo=KST)
    return Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=session_date,
        generated_at=at,
        results={
            "toss_rankings": SourceResult(
                key="toss_rankings", ok=True, data={"boards": boards}, error=None,
                url="https://x", fetched_at=at, latency_ms=1,
            ),
        },
    )


def _wire_kr_ledger_pipeline(monkeypatch) -> None:
    cont = {"005930": _cont_entry(0)}
    _wire_cont(monkeypatch, cont)
    monkeypatch.setattr(report_core, "load_market_map", lambda cache_dir: {"005930": "005930.KS"})
    monkeypatch.setattr(
        report_core, "fetch_symbol_quotes",
        lambda syms: {s: {"close": 100.0, "change_pct": 0.0} for s in syms},
    )
    monkeypatch.setattr(
        report_core, "fetch_many",
        lambda codes, limit=6: {
            "005930": {"flow": [{"date": "2026-08-14", "foreign_net": 100, "inst_net": -50}]},
        },
    )
    # `_midterm_entities`(→ `_build_midterm_watch_view`, `_emit`이 KR 리포트에서
    # 부른다)는 `_derive`와 별개로 `load_table`을 자기 모듈에서 다시 임포트한다
    # (Phase D 엔진 분리) — 두 곳 다 막아야 이 테스트가 네트워크를 타지 않는다.
    monkeypatch.setattr(report_midterm, "load_table", lambda cache_dir: {})
    monkeypatch.setattr(report_cli, "_fetch_youtube_briefs", lambda: {})
    # 텔레그램 인사이트(서브프로젝트 S part 2) — _emit 이 실제 네트워크를
    # 타지 않게 유튜브와 같은 이유로 막는다.
    monkeypatch.setattr(report_cli, "_fetch_telegram_briefs", lambda root, getter=None: {})


def _ledger_paths(root) -> tuple:
    d = root / "data" / "ledger"
    return d / "flows.jsonl", d / "selections.jsonl", d / "overlap.jsonl"


def test_emit_skips_ledger_writes_on_sunday_after_friday_open(monkeypatch, tmp_path):
    """(a) 마지막 개장일 금(08-14) 다음 일요일(08-16) — 같은 금요일 데이터의
    재기록이므로 세 원장 모두 건너뛴다."""
    _write_daily_anchor(tmp_path, "KR", ["2026-08-14"])
    _wire_kr_ledger_pipeline(monkeypatch)
    out_root = tmp_path / "out"
    snap = _kr_snap_with_rankings(date(2026, 8, 16))

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    flows_p, sel_p, overlap_p = _ledger_paths(tmp_path)
    assert not flows_p.exists()
    assert not sel_p.exists()
    assert not overlap_p.exists()


def test_emit_records_ledger_on_saturday_first_capture_of_friday_close(monkeypatch, tmp_path):
    """(b) 토요일(08-15) — 금요일 마감 데이터의 첫 기록이므로 기록한다."""
    _write_daily_anchor(tmp_path, "KR", ["2026-08-14"])
    _wire_kr_ledger_pipeline(monkeypatch)
    out_root = tmp_path / "out"
    snap = _kr_snap_with_rankings(date(2026, 8, 15))

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    flows_p, sel_p, overlap_p = _ledger_paths(tmp_path)
    assert flows_p.exists() and sel_p.exists() and overlap_p.exists()


def test_emit_records_ledger_on_normal_weekday(monkeypatch, tmp_path):
    """(c) 정상 화요일(08-18), 마지막 개장일 월(08-17) — 기록한다."""
    _write_daily_anchor(tmp_path, "KR", ["2026-08-17"])
    _wire_kr_ledger_pipeline(monkeypatch)
    out_root = tmp_path / "out"
    snap = _kr_snap_with_rankings(date(2026, 8, 18))

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    flows_p, sel_p, overlap_p = _ledger_paths(tmp_path)
    assert flows_p.exists() and sel_p.exists() and overlap_p.exists()


def test_emit_records_ledger_when_anchor_missing(monkeypatch, tmp_path):
    """(d) 앵커 데이터 자체가 없음 — 판정 불가면 기존 동작(기록)을 유지한다."""
    _wire_kr_ledger_pipeline(monkeypatch)
    out_root = tmp_path / "out"
    snap = _kr_snap_with_rankings(date(2026, 8, 16))

    report_cli._emit(snap, tmp_path, out_root, tmp_path / "snapshots")

    flows_p, sel_p, overlap_p = _ledger_paths(tmp_path)
    assert flows_p.exists() and sel_p.exists() and overlap_p.exists()
