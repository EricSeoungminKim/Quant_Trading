"""`quant.apps.cli seed-real` — 실계좌 스냅샷을 모의(paper) 상태로 이식하는
일회성 제어 도구(2026-09-01 소유자 지시: "모의 포트폴리오를 완전히 초기화하고,
실제 토스 계좌 스냅샷을 이어받아 모의투자로 진행하라. 원화는 원화로, 달러는
달러로만(환전 금지)").

이 스위트가 고정하는 것:
- 기존 portfolio.json/strategy_books.json/risk_day.json은 지워지지 않고
  *.pre_seed.<날짜>.bak으로 보존된다.
- 정리 대상 7종목이 스냅샷 가격으로 매도 체결되어 trades.jsonl에 정식 행으로
  남는다(paper 수수료 모델 그대로 통과 — KR 매도세, US SEC Fee/TAF 포함).
- 매도 대금은 통화별 풀에만 들어간다(KR→KRW, US→USD) — 환전 없음.
- 005930은 남아서 frgn_accumulate lot으로 이관된다.
- --dry-run은 아무 파일도 쓰지 않는다.
- 구버전 portfolio.json(cash_usd 필드 없음)도 seed-real 실행 자체에는
  영향이 없다(새로 통째로 구성하므로) — 다만 백업은 그 구버전 내용 그대로 남는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

SNAPSHOT = {
    "ts": "2026-09-01T22:33:08.088844+09:00",
    "fx_usd_krw": 1376.7,
    "holdings": [
        {"symbol": "009150", "currency": "KRW", "qty": 1.0, "avg_cost": 1435000.0, "price": 1401000.0},
        {"symbol": "005930", "currency": "KRW", "qty": 6.0, "avg_cost": 263416.666666, "price": 255000.0},
        {"symbol": "012450", "currency": "KRW", "qty": 1.0, "avg_cost": 1163000.0, "price": 1060000.0},
        {"symbol": "GOOGL", "currency": "USD", "qty": 1.0, "avg_cost": 388.71, "price": 334.07},
        {"symbol": "NVDA", "currency": "USD", "qty": 5.0, "avg_cost": 214.27, "price": 215.55},
        {"symbol": "TSLA", "currency": "USD", "qty": 5.831316, "avg_cost": 414.8, "price": 357.53},
        {"symbol": "GLDM", "currency": "USD", "qty": 15.537861, "avg_cost": 87.179951, "price": 86.1},
        {"symbol": "SOXL", "currency": "USD", "qty": 13.0, "avg_cost": 160.01, "price": 105.67},
    ],
    "buying_power_KRW": {"currency": "KRW", "cashBuyingPower": "523860"},
    "buying_power_USD": {"currency": "USD", "cashBuyingPower": "0"},
}


def _write_snapshot(tmp_path: Path) -> Path:
    p = tmp_path / "snapshot.json"
    p.write_text(json.dumps(SNAPSHOT), encoding="utf-8")
    return p


def _seed_old_state(tmp_path: Path) -> None:
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "portfolio.json").write_text(
        json.dumps({"cash": 999.0, "positions": {}}), encoding="utf-8")  # 구버전(cash_usd 없음)
    (state / "strategy_books.json").write_text(
        json.dumps({"version": 1, "initial_krw": 1.0, "books": {}}), encoding="utf-8")
    (state / "risk_day.json").write_text(json.dumps({"day": "2026-01-01"}), encoding="utf-8")


def _run(monkeypatch, tmp_path, capsys, dry_run: bool) -> dict:
    # REPO_ROOT만 격리한다 — cwd는 그대로 둬야 load_settings()의 상대경로
    # "config/settings.yaml"(실제 수수료율)이 그대로 풀린다(pytest는 저장소
    # 루트에서 돈다는 이 저장소의 기존 관례, test_paper_fees.py와 동일).
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_real

    args = argparse.Namespace(snapshot=str(_write_snapshot(tmp_path)), dry_run=dry_run)
    cmd_seed_real(args)
    out = capsys.readouterr().out
    return json.loads(out)


def test_backs_up_existing_state_files_without_deleting_them(tmp_path, monkeypatch, capsys):
    _seed_old_state(tmp_path)
    result = _run(monkeypatch, tmp_path, capsys, dry_run=False)

    state = tmp_path / "data" / "state"
    assert (state / "portfolio.json").exists(), "구버전 파일이 사라지면 안 된다(덮어써지되 원본은 백업됨)"
    assert len(result["backed_up"]) == 3
    for bak in result["backed_up"]:
        assert Path(bak).exists()
        assert ".pre_seed." in Path(bak).name
    old_portfolio_bak = next(b for b in result["backed_up"] if "portfolio.json" in b)
    assert json.loads(Path(old_portfolio_bak).read_text())["cash"] == 999.0


def test_sells_seven_legacy_positions_and_keeps_005930(tmp_path, monkeypatch, capsys):
    result = _run(monkeypatch, tmp_path, capsys, dry_run=False)

    assert len(result["sell_fills_recorded"]) == 7
    assert all(f["status"] == "filled" for f in result["sell_fills_recorded"])
    sold_symbols = {f["symbol"] for f in result["sell_fills_recorded"]}
    assert sold_symbols == {"009150", "012450", "GOOGL", "NVDA", "TSLA", "GLDM", "SOXL"}

    assert result["positions_remaining"] == {
        "005930": {"qty": 6.0, "avg_cost": 263416.666666},
    }


def test_currency_pools_are_never_mixed(tmp_path, monkeypatch, capsys):
    """KR 매도(009150/012450) 대금은 KRW 풀에, US 매도 대금은 USD 풀에만 들어간다 —
    시작 KRW(523,860)보다 커진 만큼이 KR 매도 순대금과 일치해야 하고, 시작 USD(0)에서
    커진 만큼이 US 매도 순대금과 일치해야 한다(환전 없음 검증)."""
    result = _run(monkeypatch, tmp_path, capsys, dry_run=False)

    kr_fills = [f for f in result["sell_fills_recorded"] if f["symbol"] in ("009150", "012450")]
    us_fills = [f for f in result["sell_fills_recorded"] if f["symbol"] not in ("009150", "012450")]

    kr_net = sum(f["price"] * qty - f["fee"] for f, qty in zip(
        kr_fills, [1.0, 1.0]))
    us_qtys = {"GOOGL": 1.0, "NVDA": 5.0, "TSLA": 5.831316, "GLDM": 15.537861, "SOXL": 13.0}
    us_net = sum(f["price"] * us_qtys[f["symbol"]] - f["fee"] for f in us_fills)

    assert result["cash_krw_final"] == pytest.approx(523860.0 + kr_net, abs=0.01)
    assert result["cash_usd_final"] == pytest.approx(0.0 + us_net, abs=0.01)


def test_trades_ledger_gets_seven_new_rows_with_transfer_reason_marker(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, capsys, dry_run=False)

    ledger_path = tmp_path / "data" / "state" / "trades.jsonl"
    assert ledger_path.exists()
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    sell_rows = [r for r in rows if r["symbol"] != "005930"]
    assert len(sell_rows) == 7
    for row in sell_rows:
        assert row["side"] == "sell"
        assert row["strategy_id"] == "legacy"
        assert "실계좌 이식 정리" in row["reason"]
        assert row["symbol"] != "005930"


def test_005930_gets_a_carry_over_row_so_ledger_reconstruction_starts_from_real_qty(
    tmp_path, monkeypatch, capsys,
):
    """D3: 유지 종목(005930)은 정리매도가 없으니 원장 행이 하나도 안 남아, 이관
    이후 이 종목을 조금이라도 팔면 quant.control.health.positions_from_trades가
    시작 잔량을 0으로 재구성해 영구히 오탐이 났다(실측). 캐리오버 합성 buy
    행이 원장에 정확히 하나 남아야 한다."""
    result = _run(monkeypatch, tmp_path, capsys, dry_run=False)

    ledger_path = tmp_path / "data" / "state" / "trades.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    carry_rows = [r for r in rows if r["symbol"] == "005930"]
    assert len(carry_rows) == 1
    carry = carry_rows[0]
    assert carry["side"] == "buy"
    assert carry["qty"] == 6.0
    assert carry["price"] == pytest.approx(263416.666666)
    assert carry["strategy_id"] == "seed"
    assert "실계좌 이식 이월" in carry["reason"]

    # cmd_seed_real이 stdout에 찍는 결과에도 그대로 드러나야 한다(수동 점검용).
    assert result["carry_row_recorded"]["symbol"] == "005930"
    assert result["carry_row_recorded"]["qty"] == 6.0


def test_dry_run_does_not_report_a_carry_row_write(tmp_path, monkeypatch, capsys):
    """dry-run은 아무 파일도 쓰지 않는다 — 캐리 행도 예외가 아니다."""
    result = _run(monkeypatch, tmp_path, capsys, dry_run=True)

    state = tmp_path / "data" / "state"
    assert not (state / "trades.jsonl").exists()
    # 미리보기 자체는 여전히 계산돼 나와야 한다.
    assert result["carry_row_recorded"]["symbol"] == "005930"


def test_005930_position_carries_frgn_accumulate_lot(tmp_path, monkeypatch, capsys):
    _run(monkeypatch, tmp_path, capsys, dry_run=False)

    portfolio = json.loads((tmp_path / "data" / "state" / "portfolio.json").read_text())
    pos = portfolio["positions"]["005930"]
    assert pos["qty"] == 6.0
    lot = pos["meta"]["lots"]["frgn_accumulate"]
    assert lot["qty"] == 6.0
    assert lot["avg_cost"] == pytest.approx(263416.666666)


def test_dry_run_writes_no_files(tmp_path, monkeypatch, capsys):
    result = _run(monkeypatch, tmp_path, capsys, dry_run=True)

    assert result["dry_run"] is True
    assert result["backed_up"] == []
    state = tmp_path / "data" / "state"
    assert not (state / "portfolio.json").exists()
    assert not (state / "trades.jsonl").exists()
    # 그럼에도 결과 계산은 실제로 이뤄져서 미리보기가 나와야 한다
    assert len(result["sell_fills_recorded"]) == 7


def test_snapshot_missing_keep_symbol_aborts(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("quant.adapters.env.REPO_ROOT", tmp_path)
    from quant.apps.cli import cmd_seed_real

    bad_snapshot = dict(SNAPSHOT, holdings=[h for h in SNAPSHOT["holdings"] if h["symbol"] != "005930"])
    p = tmp_path / "bad_snapshot.json"
    p.write_text(json.dumps(bad_snapshot), encoding="utf-8")
    args = argparse.Namespace(snapshot=str(p), dry_run=True)

    with pytest.raises(SystemExit):
        cmd_seed_real(args)
