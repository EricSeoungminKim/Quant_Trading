"""`run scoreboard` 출력에 실린 3갈래 자동화(T) 승격 판정 줄 — 서브프로젝트 T
Task 3. 판정 자체(`quant.control.ledger`의 두 함수)는 `tests/test_ledger.py`가
이미 다룬다 — 여기는 apps 계층의 배선(원장 읽기 → 판정 함수 호출 → 출력 줄)만
검증한다."""
from __future__ import annotations

import json

import pytest


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
