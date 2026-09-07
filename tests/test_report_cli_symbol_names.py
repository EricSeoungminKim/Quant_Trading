"""`report_cli symbol-names` — 셸(flow_scan.sh)이 쓰는 라벨 출력(2026-09-07)."""

from __future__ import annotations

import json
from pathlib import Path

from quant.apps import report_cli


def test_symbol_names_prints_labels_in_input_order(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "data" / "state").mkdir(parents=True)
    (tmp_path / "data" / "state" / "symbol_names.json").write_text(
        json.dumps({"TSLA": "Tesla Inc", "005930": "삼성전자"}), encoding="utf-8"
    )
    # 네트워크·LLM 없이 캐시만 — 리졸버 조립 함수를 캐시 읽기로 대체한다.
    monkeypatch.setattr(
        report_cli, "_trade_review_symbol_names",
        lambda root, cache_dir, symbols, market: json.loads(
            (Path(root) / "data" / "state" / "symbol_names.json").read_text(encoding="utf-8")
        ),
    )
    rc = report_cli.main(["symbol-names", "--market", "US", "--root", str(tmp_path), "TSLA", "CRCL", "AAPL"])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "Tesla Inc(TSLA) CRCL AAPL", "모르는 티커는 코드 그대로, 순서 유지"


def test_symbol_names_empty_input_prints_empty_line(tmp_path: Path, capsys):
    (tmp_path / "data" / "state").mkdir(parents=True)
    rc = report_cli.main(["symbol-names", "--market", "KR", "--root", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""
