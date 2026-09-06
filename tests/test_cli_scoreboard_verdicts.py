"""`run scoreboard` 출력에 실린 3갈래 자동화(T) 승격 판정 줄 — 서브프로젝트 T
Task 3. 판정 자체(`quant.control.ledger`의 두 함수)는 `tests/test_ledger.py`가
이미 다룬다 — 여기는 apps 계층의 배선(원장 읽기 → 판정 함수 호출 → 출력 줄)만
검증한다."""
from __future__ import annotations

import json


def _write_verify_ledger(tmp_path, rows: list[dict]) -> None:
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "intraday_verify.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_news_scalp_verdict_line_reports_missing_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import _news_scalp_verdict_line

    line = _news_scalp_verdict_line()
    assert "미실행" in line or "원장 없음" in line


def test_news_scalp_verdict_line_reports_empty_ledger_file(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    _write_verify_ledger(tmp_path, [])
    from quant.apps.cli import _news_scalp_verdict_line

    assert "비어" in _news_scalp_verdict_line()


def test_news_scalp_verdict_line_uses_latest_row_and_applies_kr_round_trip_fee(tmp_path, monkeypatch):
    """execution.fee_bps.KR(1.5) x2 + kr_stock_sell_tax_bps(20) = 23bp 왕복비용을
    적용해 순 bp를 판정한다.

    2026-08-19 교정: 이 테스트는 왕복 18bp(매도세 15bp)를 하드코딩하고 있었는데,
    토스증권 실제 요율은 KR 개별주 **매도 제세금 0.2%**(증권거래세 0.05% + 농어촌
    특별세 0.15%)라 20bp 다 — 설정 주석이 합계를 부분으로 착각해 15bp 로 적혀 있었다.
    즉 **승격 판정이 5bp 만큼 후했다**: 같은 원장이 이전엔 +12.0bp 로 보였는데
    실제 비용을 물리면 +7.0bp 다. 낡은 비용을 고정한 기대값을 실제 요율로 바꾼다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    _write_verify_ledger(tmp_path, [
        {"date": "2026-08-10", "metrics": {"n_symbol_days": 5, "avg_open_close_bp": 5.0}},
        # 최신 행(마지막 줄)만 써야 한다 — 오래된 표본 부족 행에 덮이면 안 된다.
        {"date": "2026-08-17", "metrics": {"n_symbol_days": 40, "avg_open_close_bp": 30.0}},
    ])
    from quant.apps.cli import _news_scalp_verdict_line

    line = _news_scalp_verdict_line()
    assert "승격 판정 가능" in line
    assert "수수료(23bp)" in line  # 1.5 x2 + 20(매도 제세금)
    assert "+7.0bp" in line  # 30.0 - 23.0


def test_news_scalp_verdict_line_insufficient_sample_from_latest_row(tmp_path, monkeypatch):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    _write_verify_ledger(tmp_path, [
        {"date": "2026-08-17", "metrics": {"n_symbol_days": 3, "avg_open_close_bp": 30.0}},
    ])
    from quant.apps.cli import _news_scalp_verdict_line

    assert "표본 부족" in _news_scalp_verdict_line()


def test_scoreboard_command_prints_both_promotion_verdicts(tmp_path, monkeypatch, capsys):
    """run scoreboard CLI 출력에 갈래 A/B 판정 줄이 둘 다 실려야 한다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    (tmp_path / "data" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "state" / "trades.jsonl").write_text("", encoding="utf-8")

    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse_namespace())
    out = capsys.readouterr().out
    assert "갈래 A(news_scalp) 승격 판정" in out
    assert "갈래 B(frgn_accumulate) 승격 판정" in out


def argparse_namespace():
    import argparse

    return argparse.Namespace(days=None)


# ---------------------------------------------------------------------------
# "에폭 이후" 절 (2026-09-06 live-readiness §1)
# ---------------------------------------------------------------------------

def _write_ledger(tmp_path, rows: list[dict]) -> None:
    ledger_dir = tmp_path / "data" / "state"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with (ledger_dir / "trades.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_scoreboard_has_no_epoch_section_without_a_marker(tmp_path, monkeypatch, capsys):
    """paper-epoch를 한 번도 안 돌린 원장은 기존과 동일하게 "누적 스코어보드"뿐이다."""
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T09:05:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
    ])
    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse_namespace())
    out = capsys.readouterr().out
    assert "에폭 이후" not in out
    assert "📊 누적 스코어보드" in out
    assert "📚 누적(역사)" not in out


def test_scoreboard_prints_epoch_section_first_then_cumulative(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "quant.control.ledger.strategy_start_capital",
        lambda sid, *a, **k: {"KRW": 0.0, "USD": 10_000.0} if sid == "gap_fade" else {"KRW": 0.0, "USD": 0.0},
    )
    _write_ledger(tmp_path, [
        # 에폭 이전에 이미 닫힌 트립 — 누적(역사) 절에만 나와야 한다.
        {"ts": "2026-08-10T09:05:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
        {"ts": "2026-08-10T10:05:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "SELL", "qty": 1, "price": 71.0, "fee": 0.0, "realized_pnl": 100.0, "market": "US"},
        {"ts": "2026-09-07T00:00:00+09:00", "strategy_id": "epoch", "symbol": "_EPOCH_",
         "side": "buy", "qty": 0.0, "price": 0.0, "fee": 0.0, "realized_pnl": 0.0,
         "reason": "페이퍼 에폭 리셋 — capital_policy=fixed_dual, at 2026-09-07T00:00:00+09:00",
         "market": "US"},
        # 에폭 이후 정상 왕복.
        {"ts": "2026-09-08T02:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 5, "price": 60.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
        {"ts": "2026-09-08T03:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "SELL", "qty": 5, "price": 62.0, "fee": 0.0, "realized_pnl": 500.0, "market": "US"},
    ])
    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse_namespace())
    out = capsys.readouterr().out

    assert "🆕 에폭 이후 (2026-09-07~)" in out
    assert "📚 누적(역사)" in out
    # 에폭 이후 절이 누적(역사) 절보다 먼저 나와야 한다.
    assert out.index("🆕 에폭 이후") < out.index("📚 누적(역사)")
    # 에폭 이후 절엔 1건(종결 1건)만, 누적 절엔 에폭 이전 트립까지 포함해 2건.
    assert "🆕 에폭 이후 (2026-09-07~) (종결 1건)" in out
    assert "📚 누적(역사) (종결 2건)" in out
    assert "수익률 +5.0% (시작 $10,000.00)" in out


def test_scoreboard_days_flag_skips_the_epoch_section(tmp_path, monkeypatch, capsys):
    """`--days`(주간 크론의 "최근 N일" 호출)는 에폭 이후 절을 건너뛴다 — 그
    스코프가 이미 "최근"이라 중복되면 scoreboard_weekly.sh 텔레그램 본문만
    두 배로 늘어난다."""
    import argparse

    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    _write_ledger(tmp_path, [
        {"ts": "2026-09-07T00:00:00+09:00", "strategy_id": "epoch", "symbol": "_EPOCH_",
         "side": "buy", "qty": 0.0, "price": 0.0, "fee": 0.0, "realized_pnl": 0.0,
         "reason": "페이퍼 에폭 리셋 — capital_policy=fixed_dual, at 2026-09-07T00:00:00+09:00",
         "market": "US"},
        {"ts": "2026-09-08T02:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 5, "price": 60.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
        {"ts": "2026-09-08T03:00:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "SELL", "qty": 5, "price": 62.0, "fee": 0.0, "realized_pnl": 500.0, "market": "US"},
    ])
    from quant.apps.cli import cmd_scoreboard

    cmd_scoreboard(argparse.Namespace(days=7))
    out = capsys.readouterr().out
    assert "에폭 이후" not in out
    assert "📚 누적(역사)" not in out
    assert "최근 7일 스코어보드" in out
