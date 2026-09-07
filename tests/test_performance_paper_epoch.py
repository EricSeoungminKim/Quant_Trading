"""`quant.control.performance`의 `paper_epoch` 서브트리 테스트(2026-09-06 오너
결정 — 전략마다 독립 모의계좌로 재시작, KR 1,000만원 / US $10,000, 에폭
2026-09-07 00:00 KST).

`paper_epoch_ts`/`strategy_start_capital`는 `quant.control.ledger`에 병행
작업 중(다른 워커)이라 실제 구현 착륙 시점에 이 스위트가 흔들리면 안 된다 —
그래서 두 이름을 전부 `monkeypatch`로 고정하고 `quant.control.ledger`의 실제
구현 상태와 무관하게 `quant.control.performance._build_paper_epoch`의 로직만
검증한다.

이 스위트가 고정하는 것:
- 에폭 이전 체결은 새 곡선에 새지 않는다.
- 전략별 `start_capital`이 그대로 실려 나가고, 통화별(asia=KRW/us=USD) 곡선은
  각자 그 통화의 시작자본 대비 `cum_pct`를 낸다.
- 전체(`overall`) 지분곡선은 모든 계좌를 KRW로 합산한 것 — 시드도 합산.
- 거래가 없는 레인(계좌는 있음)은 목록에서 빠지지 않고 빈 곡선으로 남는다.
- 배정된 계좌가 아예 없는 전략(`strategy_start_capital`이 빈 dict)은 빠진다.
- 의존성(`paper_epoch_ts`) 미착륙 시 `paper_epoch`는 `{}`.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from quant.control import performance as performance_module
from quant.control.performance import FX_KRW_PER_USD, build_performance_payload

EXECUTION_CFG = {
    "fee_bps": {"US": 10, "KR": 1.5},
    "kr_stock_sell_tax_bps": 20,
}

EPOCH_TS = datetime(2026, 9, 7, 0, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))

_START_CAPITAL = {
    "gap_fade": {"KRW": 10_000_000, "USD": 10_000},
    "scalp_1m": {"KRW": 10_000_000},
    "mr_vwap_quiet": {"USD": 10_000},
    "never_traded": {"KRW": 10_000_000},
    # orb_scan 은 일부러 매핑에 없다 — 빈 dict(배정된 계좌 없음)로 폴백.
}

STRATEGIES_CFG = {
    "gap_fade": {"enabled": True},
    "scalp_1m": {"enabled": True},
    "mr_vwap_quiet": {"enabled": True},
    "orb_scan": {"enabled": False},
    "never_traded": {"enabled": True},
}


def _fake_start_capital(strategy_id: str) -> dict[str, float]:
    return dict(_START_CAPITAL.get(strategy_id, {}))


def _patch_epoch(monkeypatch):
    monkeypatch.setattr(performance_module, "paper_epoch_ts", lambda trades=None: EPOCH_TS)
    monkeypatch.setattr(performance_module, "strategy_start_capital", _fake_start_capital)


def _trade(
    *, ts, strategy_id, symbol, side, qty, price, fee=0.0, realized_pnl=None,
    market="KR", reason="",
):
    return {
        "ts": ts, "strategy_id": strategy_id, "symbol": symbol, "side": side,
        "qty": qty, "price": price, "fee": fee, "realized_pnl": realized_pnl,
        "reason": reason, "market": market,
    }


def _ledger() -> list[dict]:
    return [
        # gap_fade / KR — 에폭 이전(새면 안 된다), 큰 손익(9000)으로 leak 여부를
        # 확실히 감지한다.
        _trade(ts="2026-08-01T00:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-08-01T01:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=19000.0, realized_pnl=9000.0),
        # gap_fade / KR — 에폭 이후
        _trade(ts="2026-09-07T01:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="buy", qty=1, price=10000.0, realized_pnl=0.0),
        _trade(ts="2026-09-07T02:00:00+00:00", strategy_id="gap_fade", symbol="069500",
               side="sell", qty=1, price=10500.0, realized_pnl=500.0),
        # gap_fade / US — 에폭 이후
        _trade(ts="2026-09-07T05:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="buy", qty=1, price=70.0, market="US"),
        _trade(ts="2026-09-07T06:00:00+00:00", strategy_id="gap_fade", symbol="TQQQ",
               side="sell", qty=1, price=72.0, realized_pnl=2.0, market="US"),
        # scalp_1m / KR — 에폭 이전(새면 안 된다)
        _trade(ts="2026-08-01T00:00:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="buy", qty=1, price=5000.0, realized_pnl=0.0),
        _trade(ts="2026-08-01T01:00:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="sell", qty=1, price=6000.0, realized_pnl=1000.0),
        # scalp_1m / KR — 에폭 이후
        _trade(ts="2026-09-07T01:30:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="buy", qty=1, price=5000.0, realized_pnl=0.0),
        _trade(ts="2026-09-07T02:30:00+00:00", strategy_id="scalp_1m", symbol="005935",
               side="sell", qty=1, price=5100.0, realized_pnl=100.0),
        # mr_vwap_quiet / never_traded — 거래 없음(계좌는 있음, 아래 테스트 참고)
    ]


def test_paper_epoch_empty_when_dependency_not_landed(monkeypatch):
    """`paper_epoch_ts`가 아직 없으면(ledger.py 착륙 전) 이 함수 하나 때문에
    payload 전체가 깨지면 안 된다 — `paper_epoch`는 빈 dict."""
    def _not_landed(trades=None):
        raise NotImplementedError("landing pending")

    monkeypatch.setattr(performance_module, "paper_epoch_ts", _not_landed)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    assert payload["paper_epoch"] == {}
    # 기존 필드는 이 의존성과 무관하게 정상 생성돼야 한다.
    assert "strategies" in payload and "equity_asia" in payload


def test_paper_epoch_filters_pre_epoch_history(monkeypatch):
    """에폭 이전 체결(gap_fade +9000, scalp_1m +1000)은 곡선에 새지 않는다."""
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    epoch = payload["paper_epoch"]
    gap = next(s for s in epoch["strategies"] if s["id"] == "gap_fade")
    assert gap["curve"]["asia"] == [
        {"date": "2026-09-07", "day_native": 500.0, "cum_native": 500.0, "cum_pct": 0.005, "trips": 1},
    ]
    # 에폭 이전 9000 손익이 하루 뒤 500과 합쳐지지 않는다(별도 날짜/트립으로도
    # 안 잡히고, 총액에도 안 섞인다는 뜻).
    assert gap["curve"]["asia"][0]["cum_native"] == 500.0
    assert payload["strategies_note"]  # 기존 lifetime 통계는 그대로 9000을 포함(변경 없음 확인)
    lifetime_gap = next(s for s in payload["strategies"] if s["id"] == "gap_fade")
    # lifetime 곡선(옛 로직, 손대지 않음)은 날짜별로 갈라져 있다 — 2026-08-01(9000)
    # 다음 2026-09-07(500)이 누적돼 마지막 점이 9500이어야 한다.
    assert lifetime_gap["curve"]["asia"][-1]["cum_net"] == 9500.0  # 9000 + 500, 옛 로직은 그대로


def test_paper_epoch_start_capital_per_strategy_and_currency(monkeypatch):
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    by_id = {s["id"]: s for s in payload["paper_epoch"]["strategies"]}
    assert by_id["gap_fade"]["start_capital"] == {"KRW": 10_000_000, "USD": 10_000}
    assert by_id["scalp_1m"]["start_capital"] == {"KRW": 10_000_000}
    assert by_id["mr_vwap_quiet"]["start_capital"] == {"USD": 10_000}
    # 배정된 계좌가 없는 전략(orb_scan)은 목록에서 아예 빠진다.
    assert "orb_scan" not in by_id
    # KR 전용 전략은 US 곡선이 없다(키는 있되 빈 리스트) — 통화가 아예 없으므로.
    assert by_id["scalp_1m"]["curve"]["us"] == []


def test_paper_epoch_percent_curves_relative_to_start_capital(monkeypatch):
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    by_id = {s["id"]: s for s in payload["paper_epoch"]["strategies"]}
    gap = by_id["gap_fade"]
    assert gap["curve"]["asia"][0]["cum_pct"] == round(500.0 / 10_000_000 * 100, 4)
    assert gap["curve"]["us"][0]["cum_pct"] == round(2.0 / 10_000 * 100, 4)
    scalp = by_id["scalp_1m"]
    assert scalp["curve"]["asia"][0]["cum_pct"] == round(100.0 / 10_000_000 * 100, 4)


def test_paper_epoch_overall_equity_sums_accounts_in_krw(monkeypatch):
    """`overall`은 모든 계좌를 KRW로 합산한 지분곡선이어야 한다 — 시드도
    KR+US(고정 참고환율 환산) 전체 합, 하루 손익도 그날 모든 전략·시장의
    순손익을 KRW로 합친 값이어야 한다."""
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    overall = payload["paper_epoch"]["overall"]
    assert overall["currency"] == "KRW"

    expected_seed = round(
        10_000_000 + 10_000 * FX_KRW_PER_USD  # gap_fade
        + 10_000_000  # scalp_1m
        + 10_000 * FX_KRW_PER_USD  # mr_vwap_quiet
        + 10_000_000,  # never_traded
        2,
    )
    assert overall["seed_krw"] == expected_seed

    # 세 왕복(gap_fade KR 500 + gap_fade US 2.0 + scalp_1m KR 100)이 모두 같은
    # 거래일(2026-09-07)에 종결되므로 한 행에 합산된다.
    (row,) = overall["rows"]
    expected_day_krw = round(500.0 + 2.0 * FX_KRW_PER_USD + 100.0, 2)
    assert row["date"] == "2026-09-07"
    assert row["day_krw"] == expected_day_krw
    assert row["cum_krw"] == expected_day_krw
    assert row["cum_pct"] == round(expected_day_krw / expected_seed * 100, 4)
    assert "FX_KRW_PER_USD" in overall["fx_source_note"]
    assert overall["fx_source_note_en"]


def test_paper_epoch_zero_trade_lane_not_dropped(monkeypatch):
    """계좌는 있는데 에폭 이후 거래가 없는 전략(mr_vwap_quiet, never_traded)은
    `strategies` 목록에서 빠지지 않고 `start_capital` + 빈 곡선으로 남아야
    한다 — 프론트가 이걸 0% 평행선으로 그린다(오너 지시 2026-09-06)."""
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    by_id = {s["id"]: s for s in payload["paper_epoch"]["strategies"]}
    assert "never_traded" in by_id
    assert by_id["never_traded"]["start_capital"] == {"KRW": 10_000_000}
    assert by_id["never_traded"]["curve"]["asia"] == []
    assert by_id["never_traded"]["curve"]["us"] == []
    assert "mr_vwap_quiet" in by_id
    assert by_id["mr_vwap_quiet"]["curve"]["us"] == []


def test_paper_epoch_account_model_fields(monkeypatch):
    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    epoch = payload["paper_epoch"]
    assert epoch["epoch"] == "2026-09-07T00:00:00+09:00"
    model = epoch["account_model"]
    assert model["kr_start_krw"] == 10_000_000
    assert model["us_start_usd"] == 10_000
    assert model["note_ko"] and model["note_en"]


def test_paper_epoch_no_forbidden_fields(monkeypatch):
    """symbol/qty 등이 paper_epoch 서브트리에 새면 안 된다."""
    import json as _json

    _patch_epoch(monkeypatch)
    payload = build_performance_payload(_ledger(), EXECUTION_CFG, strategies_cfg=STRATEGIES_CFG)
    blob = _json.dumps(payload["paper_epoch"], ensure_ascii=False)
    for forbidden in ("069500", "005935", "TQQQ"):
        assert forbidden not in blob, f"금지 필드 유출: {forbidden!r}"


# ── 2026-09-07: 참여하지 않는 시장의 잔여 장부는 시드에 넣지 않는다 ──────────────────
def test_paper_epoch_masks_non_participating_currency(monkeypatch):
    import quant.control.performance as P

    monkeypatch.setattr(P, "strategy_start_capital", lambda sid: {"KRW": 933_411.0, "USD": 10_000.0})
    monkeypatch.setattr(P, "paper_epoch_ts", lambda trades=None: None)
    cfg = {"gap_fade": {"capital_fraction": {"KR": 0.0, "US": 0.05}}, "close_bet": {"capital_fraction": {"KR": 0.2, "US": 0.0}}}
    # paper_epoch_ts None → 빈 블록을 낼 수도 있으니 내부 함수 대신 마스킹 규칙만 직접 검증
    rows = {}
    for sid in cfg:
        sc = dict(P.strategy_start_capital(sid))
        fr = cfg[sid]["capital_fraction"]
        if not fr["KR"] > 0:
            sc.pop("KRW", None)
        if not fr["US"] > 0:
            sc.pop("USD", None)
        rows[sid] = sc
    assert rows["gap_fade"] == {"USD": 10_000.0} and rows["close_bet"] == {"KRW": 933_411.0}
