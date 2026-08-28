"""오버나이트 드리프트 — 마감 직전에 사서 다음날 개장 직후에 판다.

**US ETF 전용 의도**로 만들었다(코드는 시장·유형을 가정하지 않는다 — 심볼은
설정에서 주입받고 판정은 `market_of_symbol` 로 심볼별 시장을 추론한다). 왜
그 의도인지는 아래 "왜 US ETF 만인가" 절에 비용 실측으로 적어 뒀다.

## 논지 — 하루의 수익은 대부분 장이 닫혀 있는 동안 난다

문헌(Lachance, *Review of Financial Economics* 2023; Elm Wealth; STOXX)이
보고하는 것: 특정 ETF 표본에서 연 8% 총수익이 **오버나이트 +12.9%/yr,
인트라데이 −4.3%/yr** 로 갈린다. 원인 설명은 ETF 시장 성장에 따른 **개장 시
주문불균형**이고, 문헌은 이 이상현상이 **비용을 물고도 수익 가능**하다고 본다.

이 저장소에서 이 전략이 하는 일은 두 가지다.

1. **비용 대비 엣지가 가장 좋다.** 왕복 **1회**로 하루치 드리프트를 통째로
   먹는다. 이 저장소의 중심 사실은 "수수료가 엣지보다 크다"인데
   (`docs/plans/개선-백로그-2026-08-15.md`: US 3개 전략이 전부 수수료 전
   양수인데 왕복 20bp 가 음수로 뒤집었다), 왕복 횟수를 줄이는 것이 그 사실에
   대한 가장 직접적인 답이다.
2. **벤치마크다.** 일중 스캘핑 전략들이 이걸 못 이기면 일중 매매를 할 이유가
   없다. "우리 스캘핑이 의미가 있나"를 판정할 기준선이 있어야 한다.

**우리 데이터로는 아직 미검증이다** — validation.status: burn_in. 위 숫자는
문헌 인용이지 우리 원장의 실측이 아니다. 표본이 쌓이면 `run scoreboard` 와
experiments 루프가 판정한다.

## 왜 US ETF 만인가 (비용 실측)

| 시장·유형 | 왕복 비용 | 판정 |
|---|---|---|
| US ETF | 수수료 10bp×2 + SEC fee + 스프레드(TQQQ 편도 2.8bp 실측) ≈ **26bp** | 이 크기 엣지로 넘을 수 있다 |
| KR 개별주 | 매도세 20bp + 수수료 ≈ **왕복 30bp** | 넘지 못한다. 게다가 개별주 갭 하방 위험이 ETF 와 비교가 안 된다 |

그래서 **심볼 목록으로 그 의도를 강제한다**(설정에서 US ETF 만 주입한다).
코드에 `"US"` 나 ETF 판별을 하드코딩하지 않는 이유는 두 가지다 — (a) 심볼로
유형을 판별할 방법이 없고(추측이 된다), (b) 시장 가정은 이 저장소에서 이미
사고를 냈다(`market_of_symbol` docstring 의 0.0015주 매수 사고).

## 규칙

1. **진입**: 정규장 마감 `entry_before_close_minutes`(기본 5)분 전. 시장이
   열려 있고 **연속 거래 구간**일 것(`quant.core.session`).
2. **진입 필터**: `max_gap_up_pct` / `min_close_vs_open_pct` — **기본은 둘 다
   꺼져 있다**. 이유는 아래 "왜 필터 기본값이 전부 비활성인가".
3. **청산**: 익일 **개장 후 `exit_after_open_minutes`(기본 5)분 이내** 전량.
   문헌의 엣지가 *개장 시* 주문불균형이므로 오래 들고 있으면 인트라데이 음수
   구간에 그대로 노출된다.
4. **보호 레일**: `stop_pct`(기본 3.0) — 익일 시가가 이 이상 갭다운이면
   개장 후 첫 판단에서 즉시 청산. **목표가는 두지 않는다** — 드리프트를
   자르면 이 전략이 먹으려는 것 자체가 없어진다.
5. **1일 1회**, 심볼당 1포지션.

## 왜 필터 기본값이 전부 비활성인가

문헌의 근거는 **"무조건 오버나이트 보유"** 이지 조건부가 아니다. 갭업 제한이나
종가/시가 하한을 기본으로 켜는 순간, 우리가 검증한 적 없는 가설(예: "갭업한
날 밤에는 드리프트가 약하다")을 문헌의 결과에 몰래 섞는 것이 된다. 그러면
성과가 나빠도 좋아도 **원인이 어느 쪽인지 영원히 모른다** — 검증하려던 대상이
사라진다.

필터는 **표본이 쌓인 뒤 experiments 루프가 판정할 대상**으로만 존재한다.
켤 수 있게 만들어 둔 것이지, 켜 두라고 만든 것이 아니다.

관련해서, 필터가 켜져 있는데 계산에 필요한 데이터가 없으면 **진입하지 않는다**
("확인 불가는 통과가 아니라 거부다" — `mr_vwap_quiet` 와 같은 원칙). 운영자가
켜 둔 전제를 확인하지 못한 채 진입하는 것은 다른 전략을 실행하는 것이다.

## 오버나이트 — 배선할 때 반드시 함께 할 일

이 전략은 **밤을 넘겨 포지션을 들고 있는 것이 설계**다. 다른 일중 전략들의
EoD 강제청산 레일에 걸리면 진입한 날 마감에 그대로 털려 전략이 통째로
무효가 된다. 배선 시 `quant/trade/loop.py` 의 `_OVERNIGHT_STRATEGIES` 에
`"overnight_drift"` 를 반드시 추가해야 한다(`_is_overnight` 가 `_pure` 접미사를
벗겨 보므로 A/B id 는 자동으로 따라온다).

`tests/test_position_report_wording.py` 는 **전략 모듈 소스에 EoD 청산 판정
함수 이름이 등장하는지**로 오버나이트 여부를 유도해 그 목록과 대조한다. 그래서
이 파일은 그 함수 이름을 어디에도 문자열로 적지 않는다 — 적으면 대조가 조용히
무력화되고, 리포트가 "장 마감까지 보유"라고 거짓말하게 된다.

## 상태가 두 갈래로 흐른다 (이 전략에서는 선택이 아니다)

| # | 값 | 어디로 | 왜 |
|---|---|---|---|
| 1 | `entered_date` = `{symbol: "YYYY-MM-DD"}` | `next_state` | "1일 1회" 게이트. 하루 안에서만 의미가 있다 |
| 2 | `entry`/`stop`/`session`/`entered_at` | **`Signal.state_update` → `Position.meta["lots"]` → `snap.lots`** | **밤을 넘겨야 한다** |

2번을 `next_state` 에 두면 안 되는 이유는 `CloseBetPureStrategy` docstring
"세션 경계를 넘는 state" 절이 이미 적어 둔 그대로다: 껍질(`shell.py`)은
`next_state` 를 인스턴스 필드로만 들고 있어 **프로세스 재시작에 증발한다**.
이 전략은 정의상 밤을 넘기고, 엔진은 하루 한 번 이상 재시작될 수 있다(핫
리로드·배포·크래시, 그리고 2026-08-28 실제 사건: 소유자가 포지션 8개를 보유한
채 장중에 재시작했다). 진입가·방어선을 거기 두면 **아침에 청산 기준을 잃은 채
포지션만 남는다** — 그대로 손실 경로다.

반면 `Signal.state_update` 는 루프가 **체결을 확인한 뒤에만** lot 에 쓰고
(`loop.py` `_execute_signal`), lot 은 포지션과 함께 영속되며, 다음날 껍질이
`snap.lots` 로 되돌려준다. `tests/test_overnight_drift.py` 의 재시작 생존
테스트가 이 설계를 고정한다 — 껍질과 인스턴스를 통째로 버려도 익일 청산이
나와야 한다.

## 아직 못 하는 것 (정직하게)

1. **우리 원장 실측이 없다.** 위 성과 숫자는 전부 문헌 인용이다. 백테스트도
   아직 돌리지 않았다.
2. **고아 포지션을 볼 수 없다.** `DataNeeds` 는 정적으로 `self.symbols` 만
   선언하므로, 밤새 유니버스가 갈려 보유 심볼이 목록에서 빠지면 아침에 청산
   주체가 사라진다. 오버나이트 전략에서 특히 아픈 한계라
   **배선 전에 유니버스 리로드가 보유 심볼을 유지하는지 확인해야 한다**
   (`CloseBetPureStrategy` "아직 못 하는 것" 3번과 같은 문제).
3. **KR 심볼을 주입하면 진입이 조용히 0 건이 된다.** KR 은 연속 거래가
   15:20 에 끝나는데 `minutes_to_close` 는 정규장 마감(15:30)까지를 센다 —
   기본 5분 창(15:25~15:30)은 통째로 동시호가 구간이라 연속 거래 가드에
   걸린다. **의도한 대로다**(위 비용표: KR 개별주는 이 엣지로 못 넘는다).
   조용한 0 건이 조용한 손실보다 낫다는 판단이고, 여기 적어 뒀으니 조용하지도
   않다.
4. **진입 시각의 하한이 없다.** 마감 5분 전이면 언제든 진입한다 — "가능한 한
   종가에 가깝게"를 더 좁히지 않았다. 좁히면 체결 실패 위험이 커지고, 그
   교환비를 우리 데이터로 아직 모른다.
5. **`lot` 에 `session` 이 없으면 그 포지션을 관리하지 않는다.** 진입 당일인지
   익일인지 구분할 근거가 없는 상태에서 청산하면 진입 당일에 팔아 전략을
   자기 손으로 무효화할 수 있다. 정상 경로에서는 발생하지 않는다(진입
   `state_update` 가 항상 싣는다).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

import pandas as pd

from quant.core.models import Signal, SignalAction, market_of_symbol
from quant.core.session import continuous_window, in_continuous_session, market_tz
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.strategy.shell import PureStrategyShell

# 필터가 켜졌을 때만 쓰는 봉. 세션 시가(당일 첫 봉의 시가)를 얻는 용도라
# 정밀도가 필요 없고, 이미 라이브에서 쓰이는 간격이다(orb_scan 기본값).
_INTERVAL = "5m"
# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분.
_FULL_SESSION_MINUTES = 390
# 당일 세션 전체(78봉) + 여유. 마감 직전에 판단하므로 세션 전체가 필요하다.
_SESSION_BARS = _FULL_SESSION_MINUTES // 5 + 2
# 갭 계산용 일봉. 전일 종가 하나만 쓰지만 휴장일/결손을 감안해 여유를 둔다.
_DAILY_COUNT = 5


class OvernightDriftStrategy:
    """오버나이트 드리프트 — `PureStrategy`(quant.core.strategy_api) 구현.

    **"Pure" 접미사가 없는 이유**: 신규 전략이라 이전할 레거시 쌍둥이가 없다
    (`MrVwapQuietStrategy` 와 같다). 순수 구현이 유일한 구현이고, 껍질
    (`OvernightDriftShell`)만 `Strategy` Protocol 을 만족한다.

    설계 근거는 전부 모듈 docstring 에 있다 — 특히 "왜 필터 기본값이 전부
    비활성인가", "오버나이트 — 배선할 때 반드시 함께 할 일", "상태가 두 갈래로
    흐른다" 세 절.
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "overnight_drift"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 판정은 심볼별 시장 추론

        # 진입: 정규장 마감 N분 전부터. 기준은 `snap.minutes_to_close`(캘린더
        # 경유라 **조기폐장을 안다**) — 시각 하드코딩은 미국 서머타임 전환에
        # 그대로 깨진다.
        self.entry_before_close_minutes: float = float(
            params.get("entry_before_close_minutes", 5))
        # 청산: 익일 개장 후 N분 이내. 문헌의 엣지는 개장 주문불균형이다.
        self.exit_after_open_minutes: float = float(
            params.get("exit_after_open_minutes", 5))
        # 갭다운 방어선(진입가 대비 %). 목표가는 두지 않는다 — 드리프트를 자르면
        # 먹으려는 것 자체가 없어진다(모듈 docstring 규칙 4).
        self.stop_pct: float = float(params.get("stop_pct", 3.0))
        # 진입 필터 — **기본 0 = 비활성**(모듈 docstring "왜 필터 기본값이 전부
        # 비활성인가"). 0 이 아닐 때만 계산하고, 계산 불가면 진입하지 않는다.
        self.max_gap_up_pct: float = float(params.get("max_gap_up_pct", 0.0))
        self.min_close_vs_open_pct: float = float(params.get("min_close_vs_open_pct", 0.0))
        # 전략 배정 자본 대비 비중. 왕복 1회짜리 전략이라 회전이 없고 동시
        # 보유 종목 수도 심볼 수를 넘지 않는다 — 하드레일은 risk 레이어가 건다.
        self.target_weight: float = float(params.get("target_weight", 0.5))

        if self.entry_before_close_minutes <= 0:
            raise ValueError("entry_before_close_minutes는 양수여야 합니다.")
        if self.exit_after_open_minutes <= 0:
            raise ValueError("exit_after_open_minutes는 양수여야 합니다.")
        if self.stop_pct <= 0:
            raise ValueError("stop_pct는 양수여야 합니다.")
        if self.max_gap_up_pct < 0:
            raise ValueError("max_gap_up_pct는 0(비활성) 이상이어야 합니다.")
        if not 0 < self.target_weight <= 1:
            raise ValueError("target_weight는 0 초과 1 이하여야 합니다.")
        # `min_close_vs_open_pct` 는 음수도 유효하다("−1% 까지는 허용"). 그래서
        # 부호 검증을 하지 않는다. 대신 **정확히 0 은 비활성**이라, "종가 ≥ 시가"
        # 를 표현하려면 아주 작은 양수(예: 0.001)를 써야 한다 — 0 을 비활성으로
        # 쓰기로 한 대가이고, 여기 적어 두는 것 말고 더 나은 방법이 없다.

        self._filters_on = bool(self.max_gap_up_pct > 0 or self.min_close_vs_open_pct != 0)

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """현재가 + 포지션이 전부다. **봉은 필터가 켜졌을 때만** 선언한다 —
        기본 구성(필터 전부 꺼짐)에서는 진입 판정에 봉이 아예 필요 없다
        (마감 N분 전인가 + 현재가가 있는가). 정적 선언이라 조건부 조회를 할 수
        없으므로, 조건을 **생성 시점**에 확정해 선언 자체를 줄인다.
        """
        bars: tuple[tuple[str, str, int], ...] = ()
        if self._filters_on:
            bars += tuple((s, _INTERVAL, _SESSION_BARS) for s in self.symbols)
        if self.max_gap_up_pct > 0:
            bars += tuple((s, "1d", _DAILY_COUNT) for s in self.symbols)
        return DataNeeds(bars=bars, quotes=tuple(self.symbols), needs_positions=True)

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        # 입력 state 는 절대 in-place 로 건드리지 않는다 — 중첩 dict 까지 복사.
        entered_date: dict[str, str] = dict(state.get("entered_date", {}))

        signals: list[Signal] = []
        markets = sorted({market_of_symbol(s) for s in self.symbols})

        # 1) 보유 관리 — 진입보다 먼저. 보유의 진실은 `snap.lots` 하나뿐이라
        #    (모듈 docstring "상태가 두 갈래로") 인스턴스 장부와 어긋날 수 없고
        #    프로세스를 재시작해도 그대로 이어진다.
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not self._tradable(market, snap):
                continue
            lot = self._my_lot(snap, symbol)
            if lot is None:
                continue
            signal = self._manage(symbol, lot, market, snap)
            if signal is not None:
                signals.append(signal)

        # 2) 진입 — 마감 N분 전 창.
        for market in markets:
            if not self._tradable(market, snap):
                continue
            if not self._in_entry_window(market, snap):
                continue
            today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if self._my_lot(snap, symbol) is not None:
                    continue  # 심볼당 1포지션
                if entered_date.get(symbol) == today_iso:
                    continue  # 1일 1회
                signal = self._check_entry(symbol, market, snap, today_iso)
                if signal is not None:
                    entered_date[symbol] = today_iso
                    signals.append(signal)

        return Decision(signals=tuple(signals), next_state={"entered_date": entered_date})

    # ------------------------------------------------------------------ 게이트

    @staticmethod
    def _tradable(market: str, snap: StrategySnapshot) -> bool:
        """시장이 열려 있고, **호가가 실시간으로 체결되는** 구간인가.

        둘 다 필요하다: `market_open` 은 휴장/조기폐장을 알고
        (`SessionCalendar`), `in_continuous_session` 은 하루 안에서 동시호가·
        시간외를 걸러낸다. 후자를 빼면 실재할 수 없는 가격으로 체결이 모델링된다
        (2026-08-26 실사고 — `quant/core/session.py` 참고).
        """
        return bool(snap.market_open.get(market, False)) and in_continuous_session(market, snap.now)

    def _in_entry_window(self, market: str, snap: StrategySnapshot) -> bool:
        """정규장 마감까지 남은 시간이 `entry_before_close_minutes` 이하인가.

        `snap.minutes_to_close` 는 `SessionCalendar` 를 통해 계산되므로
        **조기폐장일에도 맞는다**. 시각을 직접 비교하지 않는 이유가 이것과
        미국 서머타임이다. `None`(그 시장 세션을 모른다)은 진입하지 않는다.
        """
        mtc = snap.minutes_to_close.get(market)
        if mtc is None:
            return False
        return 0 < mtc <= self.entry_before_close_minutes

    @staticmethod
    def _minutes_since_open(market: str, snap: StrategySnapshot) -> float:
        """연속 거래 개장 이후 경과 분. tz 변환으로 서머타임이 자동 반영된다."""
        tz = market_tz(market)
        now_local = snap.now.astimezone(tz)
        open_t, _ = continuous_window(market)
        opened = datetime.combine(now_local.date(), open_t, tzinfo=tz)
        return (now_local - opened).total_seconds() / 60

    @staticmethod
    def _my_lot(snap: StrategySnapshot, symbol: str) -> Mapping[str, Any] | None:
        """내가 **진입가를 써 넣은** 열린 랏만 돌려준다.

        `snap.lots[symbol]` 이 빈 dict 인 두 경우(남의 포지션 / 방금 체결돼
        아직 lot 필드가 없음)를 `entry` 유무로 안전하게 걸러낸다 — 남의 포지션을
        내 것으로 오인해 청산 주문을 내는 사고가 구조적으로 불가능해진다
        (`MrVwapQuietStrategy._my_lot` 과 같은 판정).
        """
        lot = snap.lots.get(symbol)
        if not lot or lot.get("entry") is None:
            return None
        return lot

    # ------------------------------------------------------------------ 진입

    def _check_entry(self, symbol: str, market: str, snap: StrategySnapshot,
                     today_iso: str) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)

        gap_up: float | None = None
        close_vs_open: float | None = None
        if self._filters_on:
            session_open = self._session_open(symbol, market, snap)
            if session_open is None or session_open <= 0:
                return None  # 필터가 켜졌는데 전제를 확인할 수 없다 → 거부
            if self.max_gap_up_pct > 0:
                prev_close = self._prev_close(symbol, snap)
                if prev_close is None:
                    return None
                gap_up = (session_open / prev_close - 1.0) * 100.0
                if gap_up >= self.max_gap_up_pct:
                    return None
            if self.min_close_vs_open_pct != 0:
                close_vs_open = (price / session_open - 1.0) * 100.0
                if close_vs_open < self.min_close_vs_open_pct:
                    return None

        stop = price * (1 - self.stop_pct / 100)
        detail = ""
        if gap_up is not None:
            detail += f" 갭업={gap_up:+.2f}%"
        if close_vs_open is not None:
            detail += f" 종가/시가={close_vs_open:+.2f}%"
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=self.target_weight,
            reason=(
                f"오버나이트 드리프트 진입: {symbol} w={self.target_weight:.2f} "
                f"현재={fmt_price(price, symbol)} 손절={fmt_price(stop, symbol)} "
                f"(마감 {self.entry_before_close_minutes:g}분 전 · 익일 개장 "
                f"{self.exit_after_open_minutes:g}분 내 청산){detail}"
            ),
            stop=stop,
            # 목표가 없음 — 드리프트를 자르지 않는다(모듈 docstring 규칙 4).
            # **밤을 넘는 값은 여기로만 나간다**: 루프가 체결을 확인한 뒤에만
            # lot 에 쓰고, 다음날 껍질이 `snap.lots` 로 돌려준다.
            state_update={
                "entry": price, "stop": stop,
                "session": today_iso, "entered_at": snap.now.isoformat(),
                "strategy": self.id,
            },
        )

    def _session_open(self, symbol: str, market: str,
                      snap: StrategySnapshot) -> float | None:
        """오늘 **연속 거래 개장 이후** 첫 봉의 시가. 프리마켓 봉이 섞이면
        "당일 시가"가 아니게 되므로 시각으로 잘라낸다."""
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        tz = market_tz(market)
        open_t, _ = continuous_window(market)
        local = bars.index.tz_convert(tz)
        session = bars[(local.date == snap.now.astimezone(tz).date()) & (local.time >= open_t)]
        if session.empty:
            return None
        value = float(session["open"].iloc[0])
        return None if pd.isna(value) else value

    @staticmethod
    def _prev_close(symbol: str, snap: StrategySnapshot) -> float | None:
        """전일 종가 = 마지막 **완성** 일봉의 종가.

        장중에는 데이터 서비스가 미완성 일봉(오늘)을 잘라내므로 마지막 행이
        전일이 된다(`quant/adapters/data/service.py` 의 완성봉 필터 —
        `mr_vwap_quiet.gap_pct` 가 같은 전제 위에 있다).
        """
        daily = snap.bars.get((symbol, "1d"))
        if daily is None or daily.empty:
            return None
        value = float(daily["close"].iloc[-1])
        if pd.isna(value) or value <= 0:
            return None
        return value

    # ------------------------------------------------------------------ 관리

    def _manage(self, symbol: str, lot: Mapping[str, Any], market: str,
                snap: StrategySnapshot) -> Signal | None:
        """익일 개장 창에서의 청산. `lot` 은 껍질이 `Position.meta["lots"][id]`
        에서 순수 조회해 채운 사본이라 여기서 고쳐도 반영되지 않는다(읽기 전용).

        **세 갈래가 전부 청산이다.** 익일 정규장 중에 이 포지션을 계속 들고 있을
        이유가 없기 때문이다 — 문헌의 엣지는 개장 순간에 있고 그 뒤는 인트라데이
        음수 구간이다. 갈래를 나누는 것은 **원장에 남는 사유를 구분하기 위해서**다
        (정상 청산인지, 갭다운 방어선에 걸린 것인지, 재시작으로 창을 놓친
        것인지가 스코어보드에서 섞이면 진단이 불가능해진다).
        """
        entry_session = lot.get("session")
        if not entry_session:
            return None  # 진입일을 모른다 — 모듈 docstring "아직 못 하는 것" 5번
        today_iso = snap.now.astimezone(market_tz(market)).date().isoformat()
        if entry_session == today_iso:
            return None  # 진입 당일은 밤을 넘긴다 — 그게 이 전략이다

        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        price = float(quote.price)
        entry = float(lot["entry"])  # _my_lot 이 존재를 이미 보장한다
        stop_raw = lot.get("stop")
        # 방어선은 진입가와 파라미터의 함수라 결정론적으로 복원할 수 있다 —
        # 지어내는 값이 아니다. 다만 파라미터가 밤새 바뀌면 복원값이 기록값과
        # 달라진다(정상 경로에서는 진입 state_update 가 늘 stop 을 싣는다).
        stop = float(stop_raw) if stop_raw is not None else entry * (1 - self.stop_pct / 100)

        def _exit(reason: str) -> Signal:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0, reason=reason,
            )

        pnl_bp = (price / entry - 1.0) * 1e4
        base = (f"진입={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)} "
                f"({pnl_bp:+.0f}bp)")
        if price <= stop:
            return _exit(f"오버나이트 드리프트 갭다운 손절({self.stop_pct:g}%): "
                         f"{base} 손절선={fmt_price(stop, symbol)}")
        since_open = self._minutes_since_open(market, snap)
        if since_open <= self.exit_after_open_minutes:
            return _exit(f"오버나이트 드리프트 청산(개장 +{since_open:.0f}분): {base}")
        return _exit(
            f"오버나이트 드리프트 지연 청산(개장 +{since_open:.0f}분 — 창"
            f"{self.exit_after_open_minutes:g}분을 놓쳤다): {base}"
        )


class OvernightDriftShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies` 가 기존 전략과 같은 방식으로
    (`cls(symbols=..., params=..., market=..., id=...)`) 생성할 수 있게 하는 얇은
    팩토리 — `MrVwapQuietShell` 과 동일 패턴.

    **레지스트리 배선은 이 파일 밖이다**(`quant/trade/strategy/__init__.py` 의
    `STRATEGY_REGISTRY` + `config/settings.yaml` 의 `strategies:` 블록). 배선
    시 `quant/trade/loop.py` 의 `_OVERNIGHT_STRATEGIES` 등재도 함께 해야 한다 —
    모듈 docstring "오버나이트 — 배선할 때 반드시 함께 할 일" 참고.
    """

    def __init__(self, symbols: list[str], params: dict,
                 market: str = "US", id: str = "overnight_drift"):
        super().__init__(OvernightDriftStrategy(symbols, params, market=market, id=id))
