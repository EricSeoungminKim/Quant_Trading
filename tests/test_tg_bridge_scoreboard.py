"""tg_bridge.py `/scoreboard` — "에폭 이후" 절 (2026-09-06 live-readiness §1).

`quant.apps.cli scoreboard`가 얻은 새 절(에폭 이후 성적을 먼저, 누적(역사)을
그 아래)을 텔레그램 즉시조회 명령도 똑같이 보여줘야 한다 — 두 경로가 서로 다른
숫자를 보여주면 어느 쪽을 믿을지 알 수 없다. 판정 자체(`round_trips_since_epoch`
등)는 `tests/test_ledger.py`가 이미 다룬다 — 여기는 이 스크립트의 배선만 본다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import tg_bridge  # noqa: E402


def _write_ledger(tmp_path, rows: list[dict]) -> Path:
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_scoreboard_without_epoch_marker_is_unchanged(tmp_path, monkeypatch):
    ledger = _write_ledger(tmp_path, [
        {"ts": "2026-08-10T09:05:00+00:00", "strategy_id": "gap_fade", "symbol": "TQQQ",
         "side": "BUY", "qty": 1, "price": 70.0, "fee": 0.0, "realized_pnl": None, "market": "US"},
    ])
    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", ledger)

    text = tg_bridge.handle_scoreboard()
    assert "에폭 이후" not in text
    assert "📊 누적 스코어보드" in text


def test_scoreboard_shows_epoch_section_before_cumulative(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "quant.control.ledger.strategy_start_capital",
        lambda sid, *a, **k: {"KRW": 0.0, "USD": 10_000.0} if sid == "gap_fade" else {"KRW": 0.0, "USD": 0.0},
    )
    ledger = _write_ledger(tmp_path, [
        # 에폭 이전에 이미 닫힌 트립 — 누적(역사) 절에만.
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
    monkeypatch.setattr(tg_bridge, "LEDGER_PATH", ledger)

    text = tg_bridge.handle_scoreboard()
    assert "🆕 에폭 이후 (2026-09-07~)" in text
    assert "📚 누적(역사)" in text
    assert text.index("🆕 에폭 이후") < text.index("📚 누적(역사)")
    assert "🆕 에폭 이후 (2026-09-07~) (종결 1건)" in text
    assert "📚 누적(역사) (종결 2건)" in text
    assert "수익률 +5.0% (시작 $10,000.00)" in text
