"""봉내(intrabar) 체결 모델 — `run_backtest(fill_model="intrabar")`.

## 이 테스트가 지키는 것

리플레이 엔진은 봉 마감마다 1회 판단한다. 그래서 15분봉 백테스트에서 손절선이 봉
시작 1분 뒤에 닿아도 엔진은 **15분 뒤 종가**에서야 본다 — 종가가 손절선 위로
되돌아왔다면 그 손절은 아예 없었던 일이 된다. 타이트한 손절을 쓰는 일중 전략일수록
왜곡이 크고 방향은 **항상 낙관**이다. 이게 이 저장소 백테스트의 1순위 정확도 결함이다.

`fill_model="intrabar"`는 그 왜곡을 제거하되 **모르는 것은 전부 불리한 쪽으로**
가정한다(같은 봉 양쪽 터치 → 손절, 갭 관통 → 시가). 아래 테스트들은 그 규칙 하나
하나를 합성 봉으로 고정한다.

## 왜 합성 parquet 인가

`StubDataFeed`는 seed 고정 난수라 "이 봉의 저가를 손절선 1틱 아래로" 같은 조건을
만들 수 없다. `HistoryDataFeed`가 읽는 파티션 레이아웃(data/history/{symbol}/{YYYY}/
{MM}.parquet, 1분봉)을 tmp에 직접 써서 봉 하나하나를 손으로 짓는다 — 합성이라는
사실은 여기 명시돼 있고, 이 숫자들은 성과가 아니라 **체결 규칙의 증거**다.
"""
from __future__ import annotations

import copy
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import yaml

from quant.adapters.data.history import clear_partition_cache
from quant.backtest.engine import run_backtest
from quant.core.models import Signal, SignalAction
from quant.trade.strategy import STRATEGY_REGISTRY

NY = ZoneInfo("America/New_York")
SESSION_DAY = datetime(2026, 1, 5).date()  # 월요일
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
SYMBOL = "PROBE"          # 6자리가 아니므로 market_of_symbol → "US"
INTERVAL = "5m"
BASE = 100.0
STOP = 99.0
TARGET = 102.0
PROBE_ID = "intrabar_probe"


# --------------------------------------------------------------------- 픽스처 도구


def _session_1m(overrides: dict[int, tuple[float, float, float, float]]) -> pd.DataFrame:
    """정규장 1세션(390분) 1분봉. 기본은 전부 평탄한 BASE 가격이고, `overrides`가
    지정한 **5분 버킷 번호**만 (open, high, low, close)가 되도록 그 버킷의 1분봉
    다섯 개를 짓는다.

    리샘플 규칙(resample_1m: open=first, high=max, low=min, close=last)을 그대로
    역산한다 — 첫 분에 open, 둘째 분에 high, 셋째 분에 low, 마지막 분에 close를
    싣는다. 호출자는 high >= max(open, close), low <= min(open, close)를 지켜야
    버킷의 고저가 의도한 값이 된다(그렇지 않으면 봉이 스스로 모순된다).
    """
    start = datetime.combine(SESSION_DAY, SESSION_OPEN, tzinfo=NY)
    end = datetime.combine(SESSION_DAY, SESSION_CLOSE, tzinfo=NY)
    rows: list[dict] = []
    index: list[datetime] = []
    ts = start
    minute = 0
    while ts < end:
        bucket, within = divmod(minute, 5)
        o = h = l = c = BASE
        spec = overrides.get(bucket)
        if spec is not None:
            b_open, b_high, b_low, b_close = spec
            if within == 0:
                o = h = l = c = b_open
            elif within == 1:
                o, h, l, c = b_open, b_high, b_open, b_open
            elif within == 2:
                o, h, l, c = b_open, b_open, b_low, b_open
            elif within == 4:
                o = h = l = c = b_close
            else:
                o = h = l = c = b_open
        index.append(ts)
        rows.append({"open": o, "high": h, "low": l, "close": c, "volume": 1000.0})
        ts += timedelta(minutes=1)
        minute += 1
    df = pd.DataFrame(rows, index=pd.DatetimeIndex(index, name="ts"))
    df.index = df.index.tz_convert("UTC")  # 파티션 규약: tz-aware(UTC 저장)
    return df


def _write_lake(root, bars: pd.DataFrame) -> str:
    part = root / SYMBOL / f"{SESSION_DAY.year:04d}"
    part.mkdir(parents=True, exist_ok=True)
    bars.to_parquet(part / f"{SESSION_DAY.month:02d}.parquet")
    # 파티션 캐시는 (절대경로, 심볼) 키라 tmp마다 다르지만, 같은 tmp를 두 번
    # 쓰는 파라미터화 테스트가 stale 프레임을 보지 않도록 명시적으로 버린다.
    clear_partition_cache()
    return str(root)


class _ProbeStrategy:
    """봉 1(첫 리플레이 사이클) 마감에 1회 진입하고, 그 뒤로는 아무 것도 하지 않는
    전략. **스스로 청산하지 않는다** — 청산이 일어난다면 그것은 전적으로 체결
    모델(봉내 손절/익절)이 낸 것이라, 테스트가 두 원인을 헷갈릴 수 없다."""

    def __init__(self, symbols, params, market, id):  # noqa: A002 — 팩토리 규약
        self.id = id
        self.symbols = list(symbols)
        self.params = dict(params or {})
        self.market = market
        self._cycles = 0

    def on_cycle(self, ctx):
        self._cycles += 1
        if self._cycles != int(self.params.get("entry_cycle", 1)):
            return []
        symbol = self.symbols[0]
        pos = ctx.broker.positions().get(symbol)
        if pos is not None and pos.lot_qty(self.id) > 0:
            return []
        stop = float(self.params["stop_price"])
        target = float(self.params["target_price"])
        return [Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=float(self.params.get("target_weight", 0.5)),
            reason="probe 진입",
            stop=stop,
            target=target,
            state_update={"entry": None, "stop": stop, "target": target},
        )]


def _write_settings(tmp_path, params: dict) -> str:
    """저장소 설정을 그대로 베끼되 전략 블록만 probe 하나로 갈아끼운다.

    수수료·슬리피지는 0으로 둔다 — 체결 **가격 규칙**을 시험하는 자리라 비용이
    섞이면 `min(stop, open)`을 소수점으로 대조할 수 없다. 비용 모델 자체는
    tests/test_backtest.py와 e2e 사다리 테스트가 따로 지킨다.
    """
    raw = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))
    cfg = copy.deepcopy(raw)
    cfg["strategies"] = {
        PROBE_ID: {
            "enabled": True,
            "class": PROBE_ID,
            "symbols": [SYMBOL],
            "capital_fraction": 1.0,
            "params": params,
        }
    }
    execution = cfg.setdefault("execution", {})
    execution["fee_bps"] = 0.0
    execution["slippage_bps"] = 0.0
    for key in (
        "kr_stock_sell_tax_bps", "us_sec_fee_bps", "us_sec_fee_min_usd",
        "us_taf_per_share", "us_taf_cap_usd", "us_free_commission_notional_usd",
    ):
        execution[key] = 0.0
    # per_strategy(명목계정 분리)는 books 주입이 전제라 백테스트에서는 shared로
    # 고정한다 — 사이징 모드를 시험하는 자리가 아니다.
    cfg.setdefault("risk", {})["capital_mode"] = "shared"
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return str(path)


@pytest.fixture()
def probe(monkeypatch):
    monkeypatch.setitem(STRATEGY_REGISTRY, PROBE_ID, _ProbeStrategy)
    yield


def _run(tmp_path, overrides, fill_model, *, entry_cycle=1, stop=STOP, target=TARGET):
    lake = tmp_path / "lake"
    history_dir = _write_lake(lake, _session_1m(overrides))
    settings_path = _write_settings(tmp_path, {
        "entry_cycle": entry_cycle, "stop_price": stop, "target_price": target,
        "target_weight": 0.5,
    })
    return run_backtest(
        strategy_id=PROBE_ID, days=1, interval=INTERVAL, source="history",
        settings_path=settings_path, history_dir=history_dir, fill_model=fill_model,
    )


def _sells(result) -> pd.DataFrame:
    return result.trades[result.trades["side"] == "sell"]


# ---------------------------------------------------------------- 규칙별 고정 테스트


def test_intrabar_stop_fills_at_the_stop_when_the_bar_only_dips_through_it(tmp_path, probe):
    """봉 2의 저가가 손절선 아래를 찍었지만 시가는 위 — 체결은 손절선 그대로.

    close 모델이었다면 봉 2의 종가(=100, 손절선 위)로 되돌아왔으므로 이 손절은
    **일어나지 않았다**. 그게 정확히 이 기능이 없앤 낙관이다."""
    result = _run(tmp_path, {1: (100.0, 100.5, 98.5, 100.0)}, "intrabar")

    sells = _sells(result)
    assert len(sells) == 1, f"봉내 손절 1건이어야 한다: {sells}"
    row = sells.iloc[0]
    assert row["price"] == pytest.approx(STOP)
    assert "봉내 손절(intrabar)" in row["reason"]
    assert row["fill_model"] == "intrabar"
    assert result.intrabar_stop_fills == 1
    assert result.intrabar_target_fills == 0
    assert result.both_touched_conservative == 0
    assert result.fill_model == "intrabar"


def test_intrabar_stop_fills_at_the_open_when_the_bar_gaps_through_it(tmp_path, probe):
    """시가가 이미 손절선 아래로 갭했으면 손절선이 아니라 **시가** 체결이다 —
    실제로는 그 가격에서야 팔 수 있기 때문이다. 손절선 체결로 적으면 갭 손실을
    통째로 감추게 된다(세션 첫 봉·KR 동시호가 갭이 정확히 이 경우다)."""
    result = _run(tmp_path, {1: (98.0, 98.5, 97.5, 98.0)}, "intrabar")

    sells = _sells(result)
    assert len(sells) == 1
    assert sells.iloc[0]["price"] == pytest.approx(98.0)
    assert result.intrabar_stop_fills == 1


def test_intrabar_target_fills_at_the_target(tmp_path, probe):
    result = _run(tmp_path, {1: (100.0, 102.5, 99.5, 100.0)}, "intrabar")

    sells = _sells(result)
    assert len(sells) == 1
    row = sells.iloc[0]
    assert row["price"] == pytest.approx(TARGET)
    assert "봉내 익절(intrabar)" in row["reason"]
    assert result.intrabar_target_fills == 1
    assert result.intrabar_stop_fills == 0


def test_intrabar_target_fills_at_the_open_when_the_bar_gaps_above_it(tmp_path, probe):
    """익절도 대칭이다 — 시가가 이미 목표 위면 `max(target, open)`."""
    result = _run(tmp_path, {1: (103.0, 103.5, 102.5, 103.0)}, "intrabar")

    assert _sells(result).iloc[0]["price"] == pytest.approx(103.0)
    assert result.intrabar_target_fills == 1


def test_intrabar_stop_wins_when_both_are_touched_in_the_same_bar(tmp_path, probe):
    """같은 봉에서 손절선과 목표선이 둘 다 닿으면 선후를 알 수 없다. 모르는 것은
    불리한 쪽(손절)으로 가정하고, 그 사실을 `both_touched_conservative`로 센다 —
    이 숫자가 크면 그 백테스트는 봉을 잘게 쪼개기 전까지 신뢰도가 낮다."""
    result = _run(tmp_path, {1: (100.0, 102.5, 98.5, 100.0)}, "intrabar")

    sells = _sells(result)
    assert len(sells) == 1
    assert sells.iloc[0]["price"] == pytest.approx(STOP)
    assert "손절" in sells.iloc[0]["reason"]
    assert result.intrabar_stop_fills == 1
    assert result.intrabar_target_fills == 0
    assert result.both_touched_conservative == 1


def test_a_lot_opened_at_bar_t_is_not_checked_against_bar_t(tmp_path, probe):
    """look-ahead(정확히는 back-look) 방지: 진입 봉의 저가는 진입 **전에** 지나간
    가격이다. 그 봉으로 진입 직후의 랏을 손절시키면 존재하지 않는 손실을 만든다.

    봉 0(진입 봉)의 저가를 손절선 한참 아래(95)로 두고, 그 뒤 봉은 전부 평탄하게
    둔다 — 체결 모델이 경계를 지키면 청산은 한 건도 나오지 않아야 한다."""
    result = _run(tmp_path, {0: (100.0, 100.0, 95.0, 100.0)}, "intrabar")

    assert result.intrabar_stop_fills == 0
    assert _sells(result).empty, "진입 봉의 저가로 그 봉의 진입분을 손절시켰다"
    assert (result.trades["side"] == "buy").sum() == 1


def test_close_model_reproduces_the_old_path_exactly(tmp_path, probe):
    """`fill_model="close"`(기본값)는 이 기능이 없던 시절과 **완전히 같은** 체결
    목록을 낸다. 기본값이 바뀌면 과거의 모든 성과 숫자가 조용히 재정의된다."""
    overrides = {1: (100.0, 100.5, 98.5, 100.0)}
    explicit = _run(tmp_path, overrides, "close")
    default_run = _run(tmp_path, overrides, "close")

    pd.testing.assert_frame_equal(explicit.trades, default_run.trades)
    assert explicit.fill_model == "close"
    assert explicit.intrabar_stop_fills == 0
    assert explicit.intrabar_target_fills == 0
    assert explicit.both_touched_conservative == 0
    assert (explicit.trades["fill_model"] == "close").all()
    # 봉 2의 종가는 손절선 위로 되돌아왔다 — close 모델은 그 손절을 못 본다.
    assert _sells(explicit).empty


def test_default_fill_model_is_close(tmp_path, probe):
    """기본 인자가 "close"임을 시그니처가 아니라 **결과로** 고정한다."""
    overrides = {1: (100.0, 100.5, 98.5, 100.0)}
    lake = tmp_path / "lake"
    history_dir = _write_lake(lake, _session_1m(overrides))
    settings_path = _write_settings(tmp_path, {
        "entry_cycle": 1, "stop_price": STOP, "target_price": TARGET, "target_weight": 0.5,
    })
    default_result = run_backtest(
        strategy_id=PROBE_ID, days=1, interval=INTERVAL, source="history",
        settings_path=settings_path, history_dir=history_dir,
    )
    assert default_result.fill_model == "close"
    assert _sells(default_result).empty


def test_reconciliation_passes_under_intrabar(tmp_path, probe):
    """봉내 청산도 정상 체결 경로(risk.approve → place_order → sinks)를 그대로
    타므로 회계 항등식이 성립해야 한다. `run_backtest`은 어긋나면 예외를 던지지만,
    잔차를 여기서 한 번 더 명시적으로 본다 — 이 경로가 브로커를 직접 조작하는
    지름길로 바뀌는 순간 이 테스트가 먼저 깨진다."""
    result = _run(tmp_path, {1: (100.0, 100.5, 98.5, 100.0)}, "intrabar")

    rec = result.reconciliation
    assert abs(rec["residual"]) <= rec["tolerance"]
    for check in rec["positions"].values():
        assert abs(check["residual"]) < 1e-6


def test_unknown_fill_model_fails_loudly(tmp_path, probe):
    with pytest.raises(ValueError, match="모르는 체결 모델"):
        _run(tmp_path, {}, "봉내처럼보이는오타")
