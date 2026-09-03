"""라이브와 동일한 run_cycle을 공유하는 리플레이 백테스트 엔진(ADR-4).

look-ahead 금지: DataFeed의 history()/quote()는 리플레이 시각(self._now)까지
**마감된** 봉만 반환한다 — 리플레이는 매 사이클 clock.set()/data.set_now()로 시계를
앞으로만 돌린다. 사이클 빈도는 `interval` 파라미터의 봉 마감 시각
(bar_close = open + interval) 기준 — predecessor(danta.py)의 "백테스트는 봉 마감마다
1회 체크" 관례를 그대로 따른다.

## 통화 규약 (하나만 기억할 것)

**BacktestResult의 모든 금액은 KRW다.** 기준 통화가 KRW인 이유는 계좌 현금
(`Portfolio.cash`)과 시작 자본(`backtest.start_cash_krw`)이 KRW이기 때문이다.

| 필드 | 통화 |
|---|---|
| `equity_curve`, `reconciliation.*`, `metrics` | KRW |
| `trades.realized_pnl_krw` / `trades.fee_krw` / `trades.pnl` | KRW |
| `trades.price` / `trades.fee` | 종목 표시 통화 (US=USD) |

`price`/`fee`만 표시 통화로 남기는 것은 도메인 원칙(models.py: "모든 가격은 종목의
표시 통화 그대로")을 따르기 위함이다 — 체결가를 KRW로 바꾸면 체결 로그가 시세와
대조 불가능해진다. **파생 손익(pnl)은 전부 KRW로 환산해 둔다.** 예전에는 손익이
USD, 자산곡선이 KRW라 둘을 비교하는 사람이 아무도 없었고, 그 틈에서 부호가 반대인
두 숫자가 몇 달간 공존했다. 환율은 백테스트 내내 브로커와 동일한 FxProvider
(기본 FixedFxProvider = 1500원 고정)를 쓴다 — 결정론 유지가 목적이다.

## 회계 항등식 (강제됨)

    최종자산 - 초기자산 == Σ실현손익 + 미실현평가손익 - Σ수수료      (전부 KRW)

`_reconcile()`이 매 백테스트마다 검산하고, 어긋나면 `ReconciliationError`를 던진다.
종목별로 `Σ매수수량 - Σ매도수량 == 최종 보유수량`도 함께 검산한다. 이 두 검산이
없으면 체결 로그·자산곡선·손익합계가 서로 다른 이야기를 해도 아무도 모른다.
"""
from __future__ import annotations

import copy
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from quant.apps.config import load_settings
from quant.backtest import roundtrips
from quant.core.models import Signal, SignalAction, market_of
# `_execute_signal`은 비공개지만 **의도적으로** 재사용한다. 봉내(intrabar) 청산이
# 승인(risk.approve) → 주문 → 체결 → 싱크 → 랏 정리를 거치는 경로가 전략이 낸
# EXIT 시그널과 **한 글자도 다르면 안 되기 때문**이다. 브로커를 직접 조작해
# 포지션을 깎는 지름길을 쓰면 수수료·원장·books·lots 정리가 정상 경로와 갈라지고,
# 그 갈라짐은 백테스트 숫자에만 나타나 아무도 못 본다. `quant/trade/loop.py`의
# `_hard_rail_exit`(엔진 레벨 하드레일)이 쓰는 것과 정확히 같은 패턴이다.
from quant.trade.loop import CycleTimings, _execute_signal, run_cycle
from quant.core.clock import SimClock
from quant.adapters.data.history import HistoryDataFeed
from quant.adapters.data.resample import resample_1m
from quant.core.session import BarSessionCalendar, MultiMarketBarSessionCalendar
from quant.adapters.data.stub import SESSION_ANCHOR, StubDataFeed
from quant.core.ports import Context
from quant.core.models import market_of_symbol
from quant.adapters.execution.paper import PaperBroker
from quant.adapters.persistence.sink import MultiSink
from quant.core.portfolio.portfolio import Portfolio, to_krw
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy import build_strategies

_BARS_PER_SESSION_1M = 390  # US 09:30-16:00 세션 분(feed의 세션 캘린더 기준)
_WARMUP_DAYS = 10  # lookback_bars 워밍업용 여유 거래일 — 리플레이 구간 앞에서 잘려나간다
_DEFAULT_START_CASH_KRW = 5_000_000.0

_TRADE_COLUMNS = [
    "ts", "symbol", "side", "qty", "price", "fee",
    "fee_krw", "realized_pnl_krw", "notional_krw", "pnl", "reason",
    # 이 체결이 어떤 체결 모델로 났는가 — "close"(봉 마감가) 또는 "intrabar"
    # (봉 안에서 손절/목표선이 닿아 그 가격으로 체결). 체결 로그를 나중에 읽는
    # 사람이 "이 손절은 15분 뒤 종가로 난 것"과 "봉 안에서 난 것"을 구분할 수
    # 없으면 비용 가정이 통째로 틀어진다.
    "fill_model",
]

FILL_MODELS = ("close", "intrabar")

# 항등식 허용 오차. 이 항등식은 대수적으로 **정확히** 성립하므로(아래 _reconcile
# 참고) 남는 것은 float64 누적 오차뿐이다. 체결 수천 건 x 자산 규모 1e7 KRW에서
# 이론적 누적 오차는 1e-6 KRW 수준이라, 초기자산의 1e-9(500만원 기준 0.005원)이면
# 잡음은 통과시키고 실제 회계 누락(최소 수십 원 단위)은 전부 잡는다.
_RECONCILE_REL_TOL = 1e-9
# 수량 항등식 허용 오차(주). PaperBroker가 전량청산 시 1e-9 미만 잔량을 0으로
# 스냅하므로 청산 1회당 최대 1e-9주가 소멸할 수 있다.
_QTY_ABS_TOL = 1e-6


class ReconciliationError(AssertionError):
    """백테스트 회계가 맞지 않는다 — 결과를 성과로 읽어선 안 된다.

    조용한 경고가 아니라 예외인 이유: 어긋난 백테스트 숫자는 "대략 맞는 숫자"가
    아니라 아무 의미도 없는 숫자다. 반환해 두면 누군가는 그것을 성과로 읽는다.
    """


@dataclass
class BacktestResult:
    equity_curve: pd.Series  # KRW
    trades: pd.DataFrame
    metrics: dict
    # 회계 검산 내역(KRW). run_backtest이 이미 통과를 강제했으므로 여기 담긴 값은
    # "검산이 통과했다"는 증거이자, 성과를 손익 구성요소로 분해해 보기 위한 것이다.
    reconciliation: dict = field(default_factory=dict)
    # 벤치마크 비교(KRW): "buy_hold"(symbols[0] 전액 매수 후 보유),
    # "buy_hold_50pct"(50%만 투입, 나머지는 무이자 현금). 각각
    # total_return_pct/cagr_pct/mdd_pct/sharpe만 담는다 — 이 리포에 benchmark
    # 문자열이 하나도 없어서 10년 -34% 백테스트를 같은 기간 TQQQ 단순보유
    # +2,941%와 한 번도 나란히 못 본 적이 있다. 반드시 같이 봐야 한다.
    benchmark: dict = field(default_factory=dict)
    # 전략의 on_cycle이 예외로 죽어 스킵된 사이클(app.loop.CycleTimings.strategy_errors
    # 누적). "n_trades 0"이 조건 미충족 때문인지, 매 사이클 침묵 실패 때문인지 여기
    # 없이는 구분할 수 없다 — mean_reversion이 실측으로 이렇게 죽었었다(캘린더가
    # KR 봉을 모른다는 ValueError를 run_cycle이 삼킴). 키는 strategy_id, 값은
    # {"cycles_skipped": int, "last_error": str}.
    strategy_errors: dict = field(default_factory=dict)
    # 이 결과를 만든 체결 모델("close" | "intrabar", 2026-09-03). 성과 숫자와
    # **반드시 붙어 다녀야 하는 가정**이다 — 봉 마감 체결은 타이트한 손절을 쓰는
    # 일중 전략을 구조적으로 실제보다 좋게 만든다(손절선이 봉 안에서 닿아도 다음
    # 마감까지 못 본다). 두 숫자를 비교할 때 모델이 다르면 비교 자체가 무의미하다.
    fill_model: str = "close"
    # 봉내 체결 모델이 실제로 몇 번 발동했나. 0이면 "봉내 손절이 한 번도 없었다"는
    # 뜻이고, 그건 결과가 close 모델과 같아야 한다는 검증 가능한 주장이 된다.
    intrabar_stop_fills: int = 0
    intrabar_target_fills: int = 0
    # 같은 봉에서 손절선과 목표선이 **둘 다** 닿은 횟수. 어느 쪽이 먼저였는지는
    # 봉 데이터로 알 수 없으므로 보수적으로 손절을 택했다 — 이 횟수가 크면
    # 그 백테스트의 결과는 봉 간격을 줄이기 전까지 신뢰도가 낮다.
    both_touched_conservative: int = 0


def _interval_to_minutes(interval: str) -> int:
    """봉 간격 문자열 → 분. `HistoryDataFeed._interval_minutes` 와 같은 규약이다.

    **왜 별도 함수인가.** 엔진은 `int(interval.rstrip("m"))` 로 직접 파싱하다가
    `1d` 에서 ValueError 로 죽었다 — 피드는 이미 1d 를 알고 있었는데 엔진만
    몰랐다. 파싱 규칙이 두 곳에 흩어져 있으면 이런 식으로 갈린다.

    2026-08-15 실측에서 이 버그가 실제로 발목을 잡았다: US 수수료가 명목
    $201~$1,411 전 구간에서 19.85~20.56bp 로 **정률**이라, 명목을 키워도 비용
    bp 가 안 줄고 **보유기간을 늘려 gross 를 키우는 것만이 유일한 레버**다.
    그걸 시험하려면 굵은 봉으로 백테스트를 돌려야 했다.
    """
    if interval == "1d":
        return 24 * 60
    if interval.endswith("m") and interval[:-1].isdigit():
        return int(interval[:-1])
    raise ValueError(
        f"모르는 봉 간격: {interval!r} — 지원 형식은 '1d' 또는 '<분>m'(예: 5m, 15m). "
        "조용히 기본값으로 떨어뜨리면 다른 간격의 백테스트를 돌린 줄 모른다."
    )


_DAY_MINUTES = 24 * 60  # interval="1d" 봉 하나가 덮는 분 — _clock_now_for의 경계값


def _clock_now_for(bar_close: pd.Timestamp, minutes: int) -> pd.Timestamp:
    """`clock.set()`에 넘길 시각 — `data.set_now()`에 넘기는 봉 마감 시각과 다르다.

    Clock.now()는 세션 판정(is_market_open)뿐 아니라 전략의 "오늘 날짜" 판정(예:
    cross_momentum의 주간 리밸런싱 요일 게이트, orb_scan/intraday_scan의 세션 롤
    감지)에도 쓰인다. 봉 마감 시각(bar_close = open + interval)을 그대로 넘기면,
    interval이 하루 이상일 때 bar_close가 봉이 속한 날의 자정을 넘어 **다음 날
    자정**이 된다(월요일 봉의 마감 = 화요일 00:00) — Clock이 그 봉을 화요일 것으로
    오판한다.

    실측(2026-08-19): cross_momentum(rebalance_weekday 기본값 0=월요일)을
    interval="1d"로 돌리면, 거래일 봉의 마감 시각이 절대 월요일 날짜에 걸리지
    않는다(월+1일=화, 화+1일=수, ... 금+1일=토 — 주말은 봉이 없어 그 사이를 못
    메운다). 리밸런싱 조건 `today.weekday() == 0`이 수학적으로 성립 불가능해지고,
    거래 0건이 "결과"처럼 보였다. is_market_open 자체는 대부분의 요일에서 True였다
    (금요일 봉만 토요일로 밀려 세션이 없어 False) — 원인은 세션 판정이 아니라
    날짜 판정이었다.

    봉내(intraday) 간격에서는 이 오프셋이 하루를 넘지 않으므로 무해하다
    (09:30+15분=09:45, 같은 날) — 그래서 이 보정은 interval >= 1일에서만 켠다.
    look-ahead 방지가 걸린 DataFeed(`data.set_now`)는 건드리지 않는다: 시세/이력
    조회는 여전히 봉 마감 시각 기준으로 완성봉만 본다. Clock은 "이 봉이 대표하는
    거래일이 언제인가"를 답하고 DataFeed는 "어디까지 알려졌는가"를 답한다 —
    둘이 다른 시각을 보는 것은 계약이 다르기 때문이다.
    """
    if minutes >= _DAY_MINUTES:
        return bar_close - pd.Timedelta(minutes=minutes)
    return bar_close


def _union_bar_closes(indexes: list[pd.DatetimeIndex]) -> pd.DatetimeIndex:
    """여러 심볼의 봉 마감 시각을 하나의 정렬된 유니크 타임라인으로 합친다.

    단일 시장 다중 심볼(stub)에서는 심볼마다 타임스탬프가 이미 동일해 합집합이
    원래 타임라인과 같지만, 시장이 섞인 유니버스(예: KR ETF + US ETF)나
    history 소스처럼 심볼마다 가용 구간이 다를 수 있는 경우 이 합집합이 진짜
    리플레이 타임라인이다 — 한 심볼 것만 쓰면 다른 시장/심볼의 사이클이 통째로
    빠진다."""
    closes = indexes[0]
    for idx in indexes[1:]:
        closes = closes.union(idx)
    return closes


def _interval_bars_of(data, symbol: str, interval: str, minutes: int) -> pd.DataFrame:
    """피드에서 `interval` 완성봉 OHLC 프레임(인덱스 = 봉 **시가** 시각)을 얻는다.

    `HistoryDataFeed`는 `interval_bars()`로 자기 선택 규칙(1분봉 리샘플 vs native
    저장소)을 캡슐화하고 있으므로 그걸 그대로 쓴다. `StubDataFeed`는 1분봉만
    합성하므로(`bars_1m` 공개) 엔진이 리플레이 타임라인을 뽑을 때와 **정확히 같은**
    리샘플을 여기서도 한다 — 타임라인과 봉 내용이 서로 다른 규칙에서 나오면
    "그 시각에 봉이 없다"는 판정이 조용히 어긋난다.

    `data.history()`를 쓰지 않는 이유: 그쪽은 리플레이 시각 기준 look-ahead 방지
    경계(마감된 봉만)를 적용하느라 세션 마지막 봉이 빠지는 등 경계가 다르다.
    체결 모델이 보는 것은 "지금 리플레이 중인 그 봉"이고 그 봉은 정의상 이미
    확정돼 있다(엔진이 그 마감 시각에 서 있다)."""
    getter = getattr(data, "interval_bars", None)
    if callable(getter):
        return getter(symbol, interval)
    bars_1m = getattr(data, "bars_1m", {}).get(symbol)
    if bars_1m is None or bars_1m.empty:
        return pd.DataFrame()
    return resample_1m(bars_1m, minutes)


def _open_lot_keys(broker) -> set[tuple[str, str]]:
    """지금 열려 있는 (심볼, 전략) 랏 키 집합. 봉내 체결의 look-ahead 방지에 쓴다."""
    keys: set[tuple[str, str]] = set()
    for symbol, pos in broker.positions().items():
        if not pos.is_open:
            continue
        for strategy_id, lot in (pos.meta.get("lots") or {}).items():
            try:
                if float(lot.get("qty", 0.0) or 0.0) > 0:
                    keys.add((symbol, strategy_id))
            except (TypeError, ValueError):
                continue
    return keys


def _lot_level(lot: dict, key: str) -> float | None:
    """랏의 stop/target 을 숫자로. 숫자가 아니면 **없는 것으로 친다** — 문자열
    "없음"이나 None을 0으로 떨어뜨리면 저가 0 비교가 항상 참이 돼 전 포지션이
    첫 봉에서 손절된다."""
    value = lot.get(key)
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


class _IntrabarFiller:
    """`fill_model="intrabar"` 의 전부 — 봉 **안에서** 닿은 손절/목표를 그 봉의
    마감 전에 체결시킨다.

    ## 왜 필요한가 (이 저장소 최대의 백테스트 정확도 결함)

    리플레이 엔진은 봉 마감마다 1회 판단한다. 그래서 15분봉 백테스트에서 손절선이
    봉 시작 1분 뒤에 닿아도 엔진은 **15분 뒤 종가**에서야 그것을 본다. 종가가
    손절선 위로 되돌아왔다면 그 손절은 아예 일어나지 않은 것이 된다. 타이트한
    손절을 쓰는 일중 전략일수록 이 왜곡이 크고, 방향은 항상 **낙관** 쪽이다.

    ## 규칙 (전부 보수적인 쪽)

    - 손절: `bar.low <= stop` 이면 `min(stop, bar.open)` 에 체결. 시가가 이미
      손절선 아래로 갭했으면 시가 체결이고, 그 외에는 손절선 체결이다 —
      **손절선보다 좋은 가격은 절대 주지 않는다**. KR 동시호가/세션 첫 봉의
      갭 관통도 이 한 줄이 그대로 덮는다.
    - 목표: `bar.high >= target` 이면 `max(target, bar.open)` 에 체결(같은 원리의 반대편).
    - 한 봉에서 **둘 다** 닿으면 손절이 이긴다. 봉 데이터로는 선후를 알 수 없고,
      모르는 것은 불리한 쪽으로 가정한다(`both_touched_conservative` 로 센다).
    - 부분청산 개념은 두지 않는다 — 해당 랏의 **잔량 전부**를 청산한다.
    - 진입은 여전히 봉 마감가다. 전략은 **완성된 봉**을 보고 판단하고 루프의
      시세는 그 마감가이기 때문이다 — 봉 안에서 진입가를 지어내면 그건 체결
      모델이 아니라 전략을 바꾸는 것이다.

    ## look-ahead 방지

    봉 t 의 OHLC 로 판정하는 대상은 **봉 t 이전에 이미 열려 있던 랏뿐**이다.
    직전 사이클이 끝난 뒤 스냅샷한 `_eligible` 이 그 경계다 — 봉 t 마감에
    진입한 랏은 봉 t 의 저가에 걸리지 않는다(그 저가는 진입 전에 이미 지나갔다).
    """

    def __init__(self, data, interval: str, minutes: int, ctx, risk, sinks, set_mode) -> None:
        self._data = data
        self._interval = interval
        self._minutes = minutes
        self._ctx = ctx
        self._risk = risk
        self._sinks = sinks
        self._set_mode = set_mode
        self._frames: dict[str, tuple | None] = {}
        self._eligible: set[tuple[str, str]] = set()
        self.stop_fills = 0
        self.target_fills = 0
        self.both_touched = 0

    def _bar(self, symbol: str, ts: pd.Timestamp):
        """그 심볼의 `ts` 마감 봉 (open, high, low). 없으면 None(휴장·상장 전 등)."""
        frame = self._frames.get(symbol, False)
        if frame is False:
            df = _interval_bars_of(self._data, symbol, self._interval, self._minutes)
            if df is None or df.empty:
                frame = None
            else:
                frame = (
                    df.index + pd.Timedelta(minutes=self._minutes),
                    df["open"].to_numpy(dtype=float),
                    df["high"].to_numpy(dtype=float),
                    df["low"].to_numpy(dtype=float),
                )
            self._frames[symbol] = frame
        if frame is None:
            return None
        closes, opens, highs, lows = frame
        i = int(closes.searchsorted(ts))
        if i >= len(closes) or closes[i] != ts:
            return None
        return opens[i], highs[i], lows[i]

    def snapshot(self, broker) -> None:
        """사이클 종료 시점의 열린 랏을 다음 봉의 판정 대상으로 굳힌다."""
        self._eligible = _open_lot_keys(broker)

    def scan(self, ts: pd.Timestamp, broker) -> None:
        """봉 `ts` 의 전략 사이클 **직전**에 호출한다."""
        if not self._eligible:
            return
        # list(): 청산이 체결되면 positions/lots 딕셔너리가 순회 중에 줄어든다
        # (PaperBroker가 전량 청산 랏을 pop 한다) — loop.py의 하드레일과 같은 이유.
        for symbol, pos in list(broker.positions().items()):
            if not pos.is_open:
                continue
            bar = self._bar(symbol, ts)
            if bar is None:
                continue
            bar_open, bar_high, bar_low = bar
            for strategy_id, lot in list((pos.meta.get("lots") or {}).items()):
                if (symbol, strategy_id) not in self._eligible:
                    continue  # 이 봉 마감에 새로 생긴 랏 — 이 봉의 고저로 판정하지 않는다
                try:
                    if float(lot.get("qty", 0.0) or 0.0) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                stop = _lot_level(lot, "stop")
                target = _lot_level(lot, "target")
                hit_stop = stop is not None and bar_low <= stop
                hit_target = target is not None and bar_high >= target
                if hit_stop and hit_target:
                    self.both_touched += 1
                if hit_stop:
                    price = min(stop, bar_open)
                    reason = f"봉내 손절(intrabar): stop={stop:g} low={bar_low:g}"
                    self.stop_fills += 1
                elif hit_target:
                    price = max(target, bar_open)
                    reason = f"봉내 익절(intrabar): target={target:g} high={bar_high:g}"
                    self.target_fills += 1
                else:
                    continue
                self._exit(symbol, strategy_id, price, reason, broker)

    def _exit(self, symbol: str, strategy_id: str, price: float, reason: str, broker) -> None:
        signal = Signal(
            strategy_id=strategy_id,
            symbol=symbol,
            action=SignalAction.EXIT_LONG,
            target_weight=0.0,
            exit_fraction=1.0,
            reason=reason,
        )
        self._sinks.on_signal(signal)
        previous = getattr(broker, "fill_price_override", None)
        broker.fill_price_override = price
        self._set_mode("intrabar")
        try:
            _execute_signal(signal, self._ctx, self._risk, self._sinks, None)
        finally:
            # try/finally 가 아니면 예외 한 번에 오버라이드가 남아 **그 뒤 모든
            # 체결이 손절가로 난다** — 조용히 전 구간이 오염된다.
            broker.fill_price_override = previous
            self._set_mode("close")


def run_backtest(
    strategy_id: str = "donchian",
    days: int = 90,
    interval: str = "15m",
    source: str = "stub",
    settings_path: str = "config/settings.yaml",
    end: datetime | pd.Timestamp | None = None,
    param_overrides: dict | None = None,
    symbols: list[str] | None = None,
    history_dir: str | Path | None = None,
    fill_model: str = "close",
) -> BacktestResult:
    """`end`/`param_overrides`는 quant/research/(walk-forward·파라미터 탐색)가
    임의의 과거 구간·파라미터 조합으로 이 함수를 반복 호출하기 위해 추가된 것 —
    둘 다 기본값(None)에서는 기존 동작(가용 데이터 끝에서부터 마지막 `days`일,
    settings.yaml 파라미터 그대로)과 100% 동일하다. paper/live 경로는 이 함수를
    호출하지 않으므로 거래 판단에는 영향이 없다.

    end: 리플레이가 이 시각 이전 봉까지만 쓰도록 자른다(그 뒤로 마지막 `days`일).
    param_overrides: strat_cfg["params"]의 일부 키를 settings.yaml을 건드리지 않고
    덮어쓴다(탐색 중인 파라미터 조합을 주입하기 위함).
    symbols: settings.yaml의 strat_cfg["symbols"]를 덮어쓴다. 관심종목(watchlist)
    전략(orb_scan/intraday_scan/cross_momentum/confluence)은 settings.yaml에
    `symbols: []`로 선언돼 있다 — 라이브에서는 세션 롤(app/assembly.rebuild_strategies)이
    그날의 관심종목으로 채우지만, 백테스트는 그 조립을 재현하지 않으므로 호출자가
    직접 지정해야 한다. 지정하지 않았는데 전략의 symbols가 비어 있으면(watchlist
    전략을 심볼 없이 돌리면) 명확한 에러로 멈춘다 — symbols[0] 접근이 IndexError로
    죽게 두면 원인을 알 수 없다.

    fill_model: "close"(기본) = 지금까지의 동작 그대로 — 모든 체결이 봉 마감가다.
    "intrabar" = 봉 안에서 닿은 손절/목표를 그 봉 안에서 체결시킨다(규칙은
    `_IntrabarFiller` docstring). 기본값이 "close"인 이유는 **기존 결과를 한 글자도
    바꾸지 않기 위해서**다 — 두 모델의 숫자를 나란히 봐야 봉 마감 체결이 얼마나
    낙관적이었는지 알 수 있고, 기본값을 바꿔 버리면 그 비교 기준선이 사라진다."""
    if source not in ("stub", "history"):
        raise ValueError(f"지원하지 않는 데이터 소스: {source}")
    if fill_model not in FILL_MODELS:
        raise ValueError(
            f"모르는 체결 모델: {fill_model!r} — 지원값은 {list(FILL_MODELS)}. "
            "조용히 기본값으로 떨어뜨리지 않는다(어느 모델로 돈 결과인지 모르게 된다)."
        )

    settings = load_settings(settings_path)
    cfg = settings.raw
    if strategy_id not in cfg.get("strategies", {}):
        raise ValueError(
            f"settings.yaml에 '{strategy_id}' 전략이 없다 "
            f"(정의된 전략: {sorted(cfg.get('strategies', {}))})"
        )
    # `enabled`는 **실거래 활성화** 플래그다. 백테스트는 전략을 이름으로 지목해
    # 호출하므로 그 플래그와 무관하게 돌아야 한다. 이 복사를 안 하면 enabled:false인
    # 전략은 build_strategies에서 걸러져 "거래 0건 / 수익률 0%"라는, 에러도 아니고
    # 결과도 아닌 숫자가 조용히 나온다 — 실제로 그렇게 한 번 속았다.
    cfg = copy.deepcopy(cfg)
    cfg["strategies"][strategy_id]["enabled"] = True
    if param_overrides:
        cfg["strategies"][strategy_id]["params"].update(param_overrides)
    strat_cfg = cfg["strategies"][strategy_id]
    if symbols is not None:
        strat_cfg["symbols"] = list(symbols)
    symbols = strat_cfg["symbols"]
    if not symbols:
        raise ValueError(
            f"'{strategy_id}' 전략은 관심종목 유니버스를 쓴다(settings.yaml에 "
            "symbols: [])— run_backtest(symbols=[...]) 또는 CLI --symbols로 "
            "구체 심볼을 지정해야 한다(예: --symbols \"TQQQ SQQQ\")"
        )
    markets = market_of(cfg.get("universe", {}))
    # settings.yaml의 universe.us/kr에 없는 심볼(관심종목으로 새로 들어온 것,
    # --symbols로 임의 지정한 것)은 market_of_symbol로 채운다 — app/assembly.py의
    # rebuild_strategies와 동일한 패턴. 기본값 "US" 폴백은 쓰지 않는다: KR 6자리
    # 코드가 US로 떨어지면 세션 판정이 미국 시간 기준으로 어긋나고 원화 환산도
    # 1,500배 어긋난다(2026-08-11 058610 0.0015주 매수 사고).
    for sym in symbols:
        if sym not in markets:
            markets[sym] = market_of_symbol(sym)

    minutes = _interval_to_minutes(interval)
    if source == "history":
        # 실데이터: data/history/의 파티션 전체를 로드 — 리플레이 구간 앞의 워밍업은
        # 디스크에 이미 있는 과거 데이터가 자연히 커버한다(StubDataFeed의 합성
        # _WARMUP_DAYS 개념이 필요 없다).
        # history_dir: 파티션 루트를 호출자가 지정할 수 있게 한다(기본값은 이 저장소의
        # data/history). 별도 데이터 레이크(quant-backtest 저장소의 data/bars 등)를
        # 같은 레이아웃으로 두고 그쪽을 리플레이하기 위한 것 — **경로만 바뀌고
        # 나머지 동작은 100% 동일하다**(None이면 HistoryDataFeed의 기존 기본값).
        data = (
            HistoryDataFeed(symbols, history_dir) if history_dir is not None
            else HistoryDataFeed(symbols)
        )
        # 1분봉이 있으면 리샘플, 없으면(예: yfinance native 15m 전용 백필) native
        # interval 저장소를 그대로 쓴다 — HistoryDataFeed.bar_closes가 그 선택을
        # 캡슐화한다(quant/adapters/data/history.py 참고).
        symbol_bar_closes = {s: data.bar_closes(s, interval) for s in symbols}
    else:
        # stub은 심볼과 무관하게 US 세션(09:30-16:00 America/New_York) 봉만 합성한다
        # (data/stub.py) — KR 심볼로 stub을 돌리면 세션 캘린더가 US 시간대를 KR로
        # 잘못 읽어 "그날 존재하는 봉" 자체가 실제 KR 개장 시간과 무관해진다. 되는
        # 척하지 않고 여기서 멈춘다 — 조용히 도는 것보다 명확히 막는 게 낫다.
        kr_symbols = sorted(s for s in symbols if markets[s] == "KR")
        if kr_symbols:
            raise ValueError(
                f"stub 데이터는 US 세션(09:30-16:00 America/New_York) 봉만 합성한다 "
                f"(data/stub.py) — KR 심볼({', '.join(kr_symbols)})은 stub으로 의미 "
                "있는 백테스트를 만들 수 없다. `run fetch`로 KR 데이터를 먼저 "
                "백필하고 --source history를 써라."
            )
        stub_days = days + _WARMUP_DAYS
        if end is not None:
            # end가 합성 타임라인(SESSION_ANCHOR부터 시작)의 뒤쪽을 가리키면 그
            # 지점까지 커버할 만큼 더 길게 생성한다 — walk-forward가 여러 윈도우를
            # 연속된 하나의 합성 타임라인 위에서 앞으로 굴릴 수 있게 하기 위함.
            elapsed_bdays = len(pd.bdate_range(SESSION_ANCHOR, end))
            stub_days = max(stub_days, elapsed_bdays + _WARMUP_DAYS)
        data = StubDataFeed(symbols, days=stub_days)
        symbol_bar_closes = {
            s: resample_1m(data.bars_1m[s], minutes).index + pd.Timedelta(minutes=minutes)
            for s in symbols
        }

    empty = [s for s in symbols if len(symbol_bar_closes[s]) == 0]
    if empty:
        avail = {}
        for s in empty:
            d = (data.root / s) if hasattr(data, "root") else Path("data/history") / s
            avail[s] = sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []
        raise ValueError(
            f"{interval} 봉이 없는 심볼: "
            + ", ".join(f"{s}(보유: {', '.join(avail[s]) or '없음'})" for s in empty)
            + " — 없는 간격을 리샘플로 지어내지 않는다(docs/data-availability.md). "
              "--interval 을 맞추거나 먼저 fetch 한다."
        )

    bar_closes = _union_bar_closes(list(symbol_bar_closes.values()))

    if end is not None:
        end_ts = pd.Timestamp(end)
        if bar_closes.tz is not None and end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize(bar_closes.tz)
        bar_closes = bar_closes[bar_closes <= end_ts]

    bars_per_day = max(_BARS_PER_SESSION_1M // minutes, 1)
    replay_closes = bar_closes[-(days * bars_per_day):]

    # cadence = 봉 간격. 리플레이는 봉 마감마다 판단하므로 이게 "다음 판단까지의
    # 간격"이고, 마감 전 강제청산이 창을 건너뛰지 않으려면 필수다 (Clock.should_flatten).
    #
    # 세션(개장~마감)은 **리플레이할 봉 자체에서 유도**한다. 09:30~16:00으로 고정하면
    # 조기폐장일(연 수 회, 13:00 마감)에 엔진이 장이 닫힌 뒤에도 3시간 동안 포지션을
    # 관리하고 시간외 봉으로 청산가를 계산한다(실측: 2016~2026에 14세션, ORB가 그중
    # 13세션에서 거래). 그날 존재하는 봉이 곧 그날의 세션이므로 거래소 캘린더를
    # 따로 들일 필요가 없다 — 라이브는 TossSessionCalendar가 같은 일을 한다.
    #
    # 관심종목 유니버스는 시장이 섞일 수 있다(KR ETF + US ETF) — 전략들은 보유·평가
    # 종목마다 market_of_symbol()로 시장을 추론해 개별적으로 is_market_open을 묻는다
    # (mean_reversion/cross_momentum/orb_scan/intraday_scan/confluence, app/loop.py의
    # _build_marks). 그래서 심볼 하나의 시장만 아는 단일 BarSessionCalendar로는
    # 부족하다 — 심볼들의 시장별로 캘린더를 만들어 MultiMarketBarSessionCalendar로
    # 묶는다. 한 시장의 봉만 있으면 그 시장만 지원하고, 다른 시장을 물으면 명시적으로
    # 실패한다(추측해서 답하지 않는다 — run_cycle이 조용히 삼키면 "거래 0건"이라는
    # 가짜 성공으로 보인다. mean_reversion이 실측으로 이렇게 죽었다).
    # **없는 간격을 요청했으면 여기서 명확히 멈춘다.** 빈 봉 인덱스는 tz 가 없어서
    # 그대로 두면 한참 뒤 BarSessionCalendar 의 `tz_convert` 가
    # `TypeError: Cannot convert tz-naive timestamps` 로 죽는다 — 원인이 "데이터가
    # 없다"인데 메시지는 타임존을 가리켜 진단이 엉뚱한 데로 샌다.
    # 2026-08-13 실측: QQQ 는 1d·5m 만 있는데 기본값 15m 로 돌려 이 함정에 빠졌고,
    # 그걸 데이터 손상으로 오진할 뻔했다.
    present_markets = sorted({markets[s] for s in symbols})
    calendars: dict[str, BarSessionCalendar] = {}
    for m in present_markets:
        market_closes = _union_bar_closes(
            [symbol_bar_closes[s] for s in symbols if markets[s] == m]
        )
        calendars[m] = BarSessionCalendar(
            market_closes - pd.Timedelta(minutes=minutes),
            interval_minutes=minutes,
            market=m,
        )
    calendar = MultiMarketBarSessionCalendar(calendars)
    clock = SimClock(
        now=_clock_now_for(replay_closes[0], minutes), cadence_minutes=minutes, calendar=calendar,
    )
    capital_fraction = {sid: c.get("capital_fraction", 1.0) for sid, c in cfg["strategies"].items()}
    start_cash = float(cfg.get("backtest", {}).get("start_cash_krw", _DEFAULT_START_CASH_KRW))
    # state_path=None: 백테스트는 절대 디스크 상태를 건드리지 않는다 — 라이브
    # paper 상태를 덮어쓰거나 동시 실행끼리 결과를 오염시키는 것을 막는다.
    portfolio = Portfolio(cash=start_cash, state_path=None)
    broker = PaperBroker(
        data=data, portfolio=portfolio, fee_bps=cfg["execution"]["fee_bps"], market_of=markets,
        # .get(): 일부 테스트가 execution 블록에 slippage_bps 없이 최소 설정을 직접
        # 주입한다(ex: 사다리 계측 테스트) — 없으면 슬리피지 0(PaperBroker 자체 기본값과 동일).
        slippage_bps=cfg["execution"].get("slippage_bps", 0.0),
        # 2026-08-19: 백테스트가 paper/live와 같은 비용 모델을 써야 한다 — 이 인자들이
        # 빠져 있어서 KR 개별주 매도세·미국 SEC Fee가 백테스트에서만 통째로 0으로
        # 취급되던 결함이었다(paper 루프는 assembly.py에서 이미 넘기고 있었다).
        # kr_etf_symbols는 넘기지 않는다 — 백테스트엔 Toss securityType 조회(네트워크)가
        # 없어 ETF 여부를 알 수 없고, paper.py의 기존 원칙대로 "모르면 개별주로
        # 취급"(과대 비용이 과소 비용보다 정직하다)이 기본값(빈 집합)만으로 이미 적용된다.
        kr_stock_sell_tax_bps=cfg["execution"].get("kr_stock_sell_tax_bps", 0.0),
        us_sec_fee_bps=cfg["execution"].get("us_sec_fee_bps", 0.0),
        us_sec_fee_min_usd=cfg["execution"].get("us_sec_fee_min_usd", 0.0),
        us_taf_per_share=cfg["execution"].get("us_taf_per_share", 0.0),
        us_taf_cap_usd=cfg["execution"].get("us_taf_cap_usd", 0.0),
        us_free_commission_notional_usd=cfg["execution"].get(
            "us_free_commission_notional_usd", 0.0),
    )
    risk = RiskManagerImpl(cfg, capital_fraction=capital_fraction, market_of=markets)
    strategies = [s for s in build_strategies(cfg) if s.id == strategy_id]
    if not strategies:
        raise ValueError(
            f"'{strategy_id}' 전략 인스턴스를 만들지 못했다 — 빈 전략으로 백테스트를 "
            "돌리면 거래 0건이 '결과'처럼 보인다"
        )
    ctx = Context(clock=clock, data=data, broker=broker)

    fx = broker.fx
    trade_rows: list[dict] = []
    # 지금 집행 중인 체결이 어떤 모델로 나는가. 평소에는 "close"(봉 마감가)이고,
    # `_IntrabarFiller`가 봉내 청산을 낼 때만 그 한 건 동안 "intrabar"로 바뀐다 —
    # 리스트에 담는 이유는 클로저에서 재바인딩 없이 갈아끼우기 위해서다.
    fill_mode = ["close"]

    def _set_fill_mode(mode: str) -> None:
        fill_mode[0] = mode

    class _TradeCapture:
        def on_signal(self, signal) -> None:
            return None

        def on_fill(self, fill) -> None:
            # 실현손익은 브로커가 체결 시점의 실제 평균단가로 계산해 Fill에 실어
            # 보낸 값을 그대로 쓴다 — 체결 로그에서 원가를 재구성하지 않는다.
            if fill.realized_pnl is None:
                raise ReconciliationError(
                    f"브로커가 실현손익을 제공하지 않는다({type(broker).__name__}): "
                    f"{fill.symbol} {fill.side.value} — 회계 검산이 불가능하므로 중단한다"
                )
            market = markets.get(fill.symbol, "US")
            fee_krw = to_krw(fill.fee, market, fx)
            realized_krw = to_krw(fill.realized_pnl, market, fx)
            trade_rows.append({
                "ts": fill.ts, "symbol": fill.symbol, "side": fill.side.value,
                "qty": fill.qty, "price": fill.price, "fee": fill.fee,
                "fee_krw": fee_krw, "realized_pnl_krw": realized_krw,
                # 명목가(KRW). **`fee_krw / fee` 로 역산하지 않는다** — 수수료가
                # 0인 체결(면제·테스트)에서 0으로 나누고, 그러면 명목 대비 bps 가
                # 조용히 무한대가 된다. 환산은 여기서 한 번만 한다.
                "notional_krw": to_krw(abs(fill.qty) * fill.price, market, fx),
                "pnl": realized_krw - fee_krw, "reason": fill.reason,
                "fill_model": fill_mode[0],
            })

    sinks = MultiSink([_TradeCapture()])

    # 자산곡선의 첫 점은 "아무 것도 하기 전"의 시작 자본이다. 첫 사이클에서 이미
    # 체결이 날 수 있으므로 리플레이 첫 봉의 사후 자산을 시작점으로 삼으면 그 거래의
    # 손익이 수익률에서 통째로 빠진다.
    equity_ts: list[pd.Timestamp] = [replay_closes[0] - pd.Timedelta(minutes=minutes)]
    equity_vals: list[float] = [start_cash]
    prices: dict[str, float] = {}
    # 사이클마다 새 CycleTimings로 run_cycle을 호출해 strategy_errors를 관찰한다.
    # 예전에는 timings를 안 넘겨서 전략이 on_cycle에서 예외로 죽어도(예: 이 캘린더는
    # KR 봉으로 만들어졌다는 ValueError) run_cycle이 조용히 삼키고 다음 전략으로
    # 넘어가, "거래 0건"이 조건 미충족인지 매 사이클 침묵 실패인지 구분할 수 없었다.
    strategy_error_counts: dict[str, int] = {}
    strategy_error_last: dict[str, str] = {}
    # fill_model="close"면 filler 자체를 만들지 않는다 — 봉 프레임 로딩도, 랏
    # 스냅샷도 일어나지 않으므로 기존 경로가 문자 그대로 그대로다.
    filler = (
        _IntrabarFiller(data, interval, minutes, ctx, risk, sinks, _set_fill_mode)
        if fill_model == "intrabar" else None
    )
    for ts in replay_closes:
        clock.set(_clock_now_for(ts, minutes))
        data.set_now(ts)
        # 봉내 체결 판정은 **전략 사이클보다 먼저** 온다. 손절선은 봉 안에서
        # 닿았으므로 그 봉의 마감 판단(신규 진입·추가매수 포함)보다 시간상 앞선다.
        if filler is not None:
            filler.scan(ts, broker)
        timings = CycleTimings()
        run_cycle(strategies, ctx, risk, sinks, notifier=None, timings=timings)
        if filler is not None:
            # 이 사이클이 끝난 시점의 열린 랏 = 다음 봉의 판정 대상(look-ahead 경계).
            filler.snapshot(broker)
        for sid, err in timings.strategy_errors.items():
            strategy_error_counts[sid] = strategy_error_counts.get(sid, 0) + 1
            strategy_error_last[sid] = err
        prices = {s: q.price for s in symbols if (q := data.quote(s)) is not None}
        equity_ts.append(ts)
        equity_vals.append(portfolio.equity(prices, markets, fx))

    equity_curve = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_ts, name="ts"), name="equity")
    trades = pd.DataFrame(trade_rows, columns=_TRADE_COLUMNS)
    strategy_errors = {
        sid: {"cycles_skipped": count, "last_error": strategy_error_last[sid]}
        for sid, count in strategy_error_counts.items()
    }

    reconciliation = _reconcile(
        equity_curve=equity_curve, trades=trades, positions=portfolio.positions,
        last_prices=prices, market_of=markets, fx=fx,
    )
    metrics = _compute_metrics(equity_curve, trades)

    # 벤치마크(단순 매수보유) 가격 시계열 — 리플레이와 정확히 같은 시각에 같은
    # DataFeed.quote()를 재호출해, 전략이 실제로 본 가격과 다른 숫자를 비교하는
    # 일을 막는다. data.set_now()는 이 시점 이후 더 쓰이지 않으므로 되감아도 안전.
    bench_market = markets.get(symbols[0], "US")
    bench_prices_krw: list[float] = []
    for ts in replay_closes:
        data.set_now(ts)
        q = data.quote(symbols[0])
        price = q.price if q is not None else float("nan")
        bench_prices_krw.append(to_krw(price, bench_market, fx))
    bench_price_series = pd.Series(
        [bench_prices_krw[0]] + bench_prices_krw, index=equity_curve.index, name="price",
    )
    benchmark = _compute_benchmark(bench_price_series, start_cash)

    return BacktestResult(
        equity_curve=equity_curve, trades=trades, metrics=metrics, reconciliation=reconciliation,
        benchmark=benchmark, strategy_errors=strategy_errors,
        fill_model=fill_model,
        intrabar_stop_fills=(filler.stop_fills if filler is not None else 0),
        intrabar_target_fills=(filler.target_fills if filler is not None else 0),
        both_touched_conservative=(filler.both_touched if filler is not None else 0),
    )


def _reconcile(
    equity_curve: pd.Series,
    trades: pd.DataFrame,
    positions: dict,
    last_prices: dict[str, float],
    market_of: dict[str, str],
    fx,
) -> dict:
    """회계 항등식(전부 KRW)을 검산한다. 어긋나면 ReconciliationError.

        최종자산 - 초기자산 == Σ실현손익 + 미실현평가손익 - Σ수수료

    **이 항등식은 근사가 아니라 정확히 성립한다.** 증명은 PaperBroker의 산식에서
    바로 나온다. 원가기준 B = Σ(수량 x 평균단가)를 KRW로 환산한 값이라 하면

      매수: 현금 -= (수량x가격 + 수수료)xfx,  B += 수량x가격xfx
            => d(현금+B) = -수수료xfx
      매도: 현금 += (수량x가격 - 수수료)xfx,  B -= 수량x평균단가xfx
            => d(현금+B) = (가격-평균단가)x수량xfx - 수수료xfx = 실현손익 - 수수료

    즉 (현금+B)의 총 변화 = Σ실현손익 - Σ수수료 이고, 자산 = 현금 + B + 미실현이며
    시작 시점엔 포지션이 없어 미실현 = 0이다. 따라서 남는 잔차는 float64 누적 오차뿐.

    함께 검산하는 수량 항등식(종목별 Σ매수수량 - Σ매도수량 == 최종 보유수량)은
    체결 로그가 포트폴리오와 같은 이야기를 하는지 보는 독립 레일이다 — 금액 항등식은
    로그와 포트폴리오가 **같이** 틀리면 통과할 수 있지만 이쪽은 통과하지 못한다.
    """
    initial_equity = float(equity_curve.iloc[0])
    final_equity = float(equity_curve.iloc[-1])
    realized_pnl = float(trades["realized_pnl_krw"].sum()) if not trades.empty else 0.0
    fees = float(trades["fee_krw"].sum()) if not trades.empty else 0.0

    unrealized_pnl = 0.0
    for symbol, pos in positions.items():
        if pos.qty <= 0:
            continue
        # 시세가 없으면 평균단가로 마킹한다 — Portfolio.equity()와 반드시 같은
        # 폴백을 써야 한다(그쪽은 원가로 잡는데 여기서 시세로 잡으면 잔차가 난다).
        price = last_prices.get(symbol, pos.avg_cost)
        unrealized_pnl += to_krw(pos.qty * (price - pos.avg_cost), market_of.get(symbol) or ("KR" if symbol.isdigit() and len(symbol) == 6 else "US"), fx)

    residual = (final_equity - initial_equity) - (realized_pnl + unrealized_pnl - fees)
    tolerance = _RECONCILE_REL_TOL * max(abs(initial_equity), abs(final_equity), 1.0)

    positions_check = {}
    qty_violations = []
    symbols = sorted(set(trades["symbol"]) | set(positions)) if not trades.empty else sorted(positions)
    for symbol in symbols:
        rows = trades[trades["symbol"] == symbol] if not trades.empty else trades
        buy_qty = float(rows[rows["side"] == "buy"]["qty"].sum()) if not rows.empty else 0.0
        sell_qty = float(rows[rows["side"] == "sell"]["qty"].sum()) if not rows.empty else 0.0
        pos = positions.get(symbol)
        final_qty = float(pos.qty) if pos is not None else 0.0
        qty_residual = buy_qty - sell_qty - final_qty
        positions_check[symbol] = {
            "buy_qty": buy_qty, "sell_qty": sell_qty,
            "net_qty": buy_qty - sell_qty, "final_qty": final_qty, "residual": qty_residual,
        }
        if abs(qty_residual) > _QTY_ABS_TOL:
            qty_violations.append(
                f"{symbol}: 매수 {buy_qty:.9f} - 매도 {sell_qty:.9f} = {buy_qty - sell_qty:.9f} "
                f"!= 최종 보유 {final_qty:.9f} (차이 {qty_residual:.9f}주)"
            )

    result = {
        "currency": "KRW",
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "equity_change": final_equity - initial_equity,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "fees": fees,
        "residual": residual,
        "tolerance": tolerance,
        "positions": positions_check,
    }

    problems = []
    if abs(residual) > tolerance:
        problems.append(
            f"자산 항등식 불일치: 자산변화 {final_equity - initial_equity:,.4f} != "
            f"실현 {realized_pnl:,.4f} + 미실현 {unrealized_pnl:,.4f} - 수수료 {fees:,.4f} "
            f"(잔차 {residual:,.6f}원, 허용 {tolerance:,.6f}원)"
        )
    problems.extend(qty_violations)
    if problems:
        raise ReconciliationError(
            "백테스트 회계 검산 실패 — 이 결과는 성과로 읽을 수 없다:\n  "
            + "\n  ".join(problems)
        )
    return result


# 왕복 순손익(KRW) 시계열. 매도 1건 = 1 트레이드.
#
# `trades["pnl"]`(= 실현손익 − **매도** 수수료)를 그대로 쓰면 안 된다. 매수
# 수수료가 어디에도 반영되지 않아 왕복 비용의 절반이 사라지고, 승률·profit
# factor·기대값이 전부 낙관 쪽으로 치우친다 — 실측: 총수익 −51%인 백테스트에서
# profit factor가 1.014로 나왔다(1을 넘으면 "이기는 전략"으로 읽힌다).
#
# 매수 수수료는 그 매수를 청산한 매도에 수량 비율대로 배분한다. 자산곡선 기반
# 지표(total_return/CAGR/MDD)는 원래부터 모든 수수료를 반영하므로 영향 없다.
#
# FIFO 배분 자체는 `quant.backtest.roundtrips`(단일 정의, 2026-09-03 부채
# 상환) — `analytics._round_trip_detail`이 진입시각·명목가까지 곁들여 같은
# 루프를 따로 구현하고 있었다.
_round_trip_pnl = roundtrips.round_trip_pnl


def _compute_metrics(equity: pd.Series, trades: pd.DataFrame) -> dict:
    if equity.empty:
        return {
            "total_return_pct": 0.0, "cagr_pct": 0.0, "mdd_pct": 0.0,
            "sharpe": 0.0, "win_rate": 0.0, "n_trades": 0,
        }
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    total_return_pct = (end / start - 1) * 100 if start else 0.0

    years = max((equity.index[-1] - equity.index[0]).total_seconds() / (365.25 * 24 * 3600), 1e-9)
    cagr_pct = ((end / start) ** (1 / years) - 1) * 100 if start > 0 else 0.0

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    mdd_pct = float(drawdown.min()) * 100 if not drawdown.empty else 0.0

    rets = equity.pct_change().dropna()
    sharpe = 0.0
    if len(rets) > 1 and rets.std() > 0:
        periods_per_year = len(rets) / years
        sharpe = float((rets.mean() / rets.std()) * (periods_per_year ** 0.5))

    pnl = _round_trip_pnl(trades)
    n_trades = len(pnl)
    win_rate = float((pnl > 0).mean() * 100) if n_trades else 0.0

    # 승률만으로 전략을 판정하면 "타이트한 손절 + 큰 우측 꼬리" 계열을 구조적으로
    # 탈락시킨다. ORB 원논문(SSRN 4729284 Table 2/4)의 hit ratio는 최고 성과
    # 버전조차 48.4%이고, 누적 R 상위 종목들의 승률은 17~24%다 — 손익비가 전부인
    # 전략이다. profit_factor(총이익/총손실)와 payoff_ratio(평균이익/평균손실)가
    # 없으면 그 구조를 볼 수 없어, 승률이 높지만 지는 전략과 승률이 낮지만 이기는
    # 전략을 구분하지 못한다.
    profit_factor = 0.0
    payoff_ratio = 0.0
    expectancy = 0.0
    if n_trades:
        wins, losses = pnl[pnl > 0], pnl[pnl < 0]
        gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
        expectancy = float(pnl.mean())
        # 손실이 하나도 없으면 비율이 정의되지 않는다 — 0으로 두면 "최악"으로
        # 읽히므로 무한대를 뜻하는 inf로 정직하게 표기한다.
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        if len(losses) and len(wins):
            payoff_ratio = float(wins.mean() / -losses.mean())

    return {
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "mdd_pct": round(mdd_pct, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3),
        "payoff_ratio": round(payoff_ratio, 3),
        "expectancy_krw": round(expectancy, 1),
        "n_trades": n_trades,
    }


_BENCHMARK_METRIC_KEYS = ("total_return_pct", "cagr_pct", "mdd_pct", "sharpe")


def _buy_hold_curve(price_series: pd.Series, start_cash: float, fraction: float) -> pd.Series:
    """`fraction`만큼의 자본을 최초 가격에 투입해 끝까지 보유하는 자산곡선(KRW).
    나머지(1-fraction)는 무이자 현금으로 그대로 남는다 — 전략이 실제로 쓰는 노출
    수준(예: capital_fraction 50%)과 비교하기 위한 것."""
    shares = (start_cash * fraction) / price_series.iloc[0]
    return shares * price_series + start_cash * (1 - fraction)


def _compute_benchmark(price_series: pd.Series, start_cash: float) -> dict:
    """단순 매수보유 벤치마크(KRW). `_compute_metrics`를 그대로 재사용해 지표
    정의가 백테스트 성과와 갈라지지 않게 한다. 벤치마크는 매수 1회뿐이라
    수수료를 적용하지 않는다(무시 가능한 크기) — win_rate/profit_factor 같은
    거래 지표는 벤치마크에 의미가 없으므로 반환하지 않는다."""
    empty_trades = pd.DataFrame(columns=_TRADE_COLUMNS)

    def _summarize(curve: pd.Series) -> dict:
        full = _compute_metrics(curve, empty_trades)
        return {key: full[key] for key in _BENCHMARK_METRIC_KEYS}

    return {
        "buy_hold": _summarize(_buy_hold_curve(price_series, start_cash, 1.0)),
        "buy_hold_50pct": _summarize(_buy_hold_curve(price_series, start_cash, 0.5)),
    }
