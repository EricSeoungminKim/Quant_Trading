"""합류 종목의 선정 원장 기록 — 2026-08-26 감사 수리.

감사 재현 내용: 거래량 감시(volume_watch)·전일 KR 세션 패턴(extra_watch)·점수
연속 강세(streak_watch)로 `AUTO_WATCH` 줄에 실려 **실제 매매 유니버스에 들어간**
종목이 `selections.jsonl` 에는 행 자체가 없었다 — `payload["symbols"]` 가
`cont`(오늘 뉴스 언급 종목)에서만 만들어지기 때문이다. 그래서 outcomes(전방
수익률)·리더보드·ai_trader 가 이 종목들을 **영원히** 볼 수 없었고, "조사한 것을
버리지 않는다"(2026-08-26 소유자)는 설계 의도가 조용히 깨져 있었다.

고정하는 계약:
① 합류 종목은 별도 producer 행으로 남는다(본선 행과 자연키 충돌 없이).
② 기준가(close)를 함께 남긴다 — 없으면 전방 수익률이 영영 계산 불가다.
③ 시세를 못 구하면 **키를 생략**한다(0 으로 위장 금지) — 행 자체는 남긴다.
④ 이미 본선 행이 있는 종목은 중복 기록하지 않는다.
⑤ 왜 합류했는지(join_reason)를 남긴다 — 나중에 축별 실효 검증의 입력.
⑥ 실패해도 리포트를 막지 않는다(원장은 부가 산출물).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quant.control import selections
from quant.control.selections import WATCH_JOIN_PRODUCER
from quant.report.collect.ledger import _record_watch_join_selections

DAY = date(2026, 8, 26)


def _payload(symbols=("005930",)):
    return {
        "session_date": DAY.isoformat(),
        "market": "KR",
        "symbols": [{"symbol": s, "name": "본선종목", "close": 1000.0} for s in symbols],
    }


def _origins(volume=(), wrap=(), streak=()):
    return {"volume": list(volume), "wrap": list(wrap), "streak": list(streak)}


def _patch_quotes(monkeypatch, quotes: dict, market_map: dict | None = None):
    import quant.report.collect.ledger as L

    monkeypatch.setattr(L, "fetch_symbol_quotes", lambda syms, **kw: quotes)
    monkeypatch.setattr(L, "load_market_map", lambda _cache: market_map or {})
    monkeypatch.setattr(L, "load_name_map", lambda _cache, _market: {"000660": "SK하이닉스"})


def test_joined_symbols_get_rows_with_price(tmp_path, monkeypatch):
    """① 합류 종목이 행으로 남고 ② 기준가가 실린다."""
    _patch_quotes(monkeypatch,
                  quotes={"000660.KS": {"close": 198500.0, "change_pct": 3.2}},
                  market_map={"000660": "000660.KS"})
    _record_watch_join_selections(_payload(), tmp_path, tmp_path / "cache",
                                  _origins(volume=["000660"]))

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "000660" and r["producer"] == WATCH_JOIN_PRODUCER
    assert r["close"] == 198500.0 and r["change_pct"] == 3.2
    assert r["name"] == "SK하이닉스"
    assert r["is_candidate"] is True, "AUTO_WATCH 줄에 실려 실제 유니버스에 들어갔다"
    assert r["join_reason"] == "volume"  # ⑤


def test_symbols_already_in_payload_are_not_duplicated(tmp_path, monkeypatch):
    """④ 본선 행이 있는 종목은 여기서 다시 기록하지 않는다."""
    _patch_quotes(monkeypatch, quotes={}, market_map={})
    _record_watch_join_selections(_payload(symbols=("005930",)), tmp_path,
                                  tmp_path / "cache", _origins(volume=["005930"]))
    assert selections.load(tmp_path / "data" / "ledger" / "selections.jsonl") == []


def test_missing_quote_omits_close_key(tmp_path, monkeypatch):
    """③ 시세를 못 구해도 행은 남기되 close 키를 만들지 않는다 —
    0 으로 위장하면 전방 수익률이 "본전"으로 영구히 굳는다."""
    _patch_quotes(monkeypatch, quotes={}, market_map={"000660": "000660.KS"})
    _record_watch_join_selections(_payload(), tmp_path, tmp_path / "cache",
                                  _origins(streak=["000660"]))

    [r] = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert "close" not in r and "change_pct" not in r
    assert r["join_reason"] == "streak"


def test_first_origin_wins_and_order_is_stable(tmp_path, monkeypatch):
    """같은 종목이 두 축에 걸리면 먼저 잡은 축을 사유로 남긴다(중복 행 금지)."""
    _patch_quotes(monkeypatch, quotes={}, market_map={})
    _record_watch_join_selections(
        _payload(), tmp_path, tmp_path / "cache",
        _origins(volume=["000660"], wrap=["000660"], streak=["000660"]))

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert len(rows) == 1 and rows[0]["join_reason"] == "volume"


def test_quote_failure_does_not_raise(tmp_path, monkeypatch):
    """⑥ 시세 조회가 터져도 리포트를 막지 않는다."""
    import quant.report.collect.ledger as L

    def boom(*_a, **_k):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(L, "fetch_symbol_quotes", boom)
    monkeypatch.setattr(L, "load_market_map", lambda _c: {"000660": "000660.KS"})
    monkeypatch.setattr(L, "load_name_map", lambda _c, _m: {})

    _record_watch_join_selections(_payload(), tmp_path, tmp_path / "cache",
                                  _origins(volume=["000660"]))
    # 시세 없이도 행은 남는다 — 속성은 썩고, 가격은 나중에도 조회된다.
    [r] = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert r["symbol"] == "000660" and "close" not in r


def test_append_is_idempotent(tmp_path, monkeypatch):
    _patch_quotes(monkeypatch, quotes={}, market_map={})
    for _ in range(2):
        _record_watch_join_selections(_payload(), tmp_path, tmp_path / "cache",
                                      _origins(wrap=["000660"]))
    assert len(selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")) == 1


def test_us_market_uses_ticker_directly(tmp_path, monkeypatch):
    """US 는 티커가 곧 야후 심볼 — 매핑 없이 조회된다(core._derive 와 같은 규칙)."""
    _patch_quotes(monkeypatch, quotes={"NVDA": {"close": 180.0, "change_pct": 1.1}})
    payload = {**_payload(symbols=()), "market": "US"}
    _record_watch_join_selections(payload, tmp_path, tmp_path / "cache",
                                  _origins(volume=["NVDA"]))

    [r] = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert r["symbol"] == "NVDA" and r["close"] == 180.0


def test_ai_trader_reads_watch_join_rows(tmp_path):
    """신입 AI 트레이더의 서류에도 합류 종목이 들어온다 — 매매 유니버스에
    있는 종목을 서류에서 빼면 그 종목에 대한 판단은 영원히 채점되지 않는다."""
    from quant.analyze.ai_trader import dossier_lines

    row = {"schema": 1, "date": DAY.isoformat(), "market": "KR",
           "producer": WATCH_JOIN_PRODUCER, "symbol": "000660", "name": "SK하이닉스",
           "is_candidate": True, "close": 198500.0, "change_pct": 3.2,
           "join_reason": "volume", "outcome_filled": False}
    [line] = dossier_lines([row])
    assert "000660" in line and "SK하이닉스" in line and "198,500" in line
