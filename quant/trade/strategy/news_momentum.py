"""뉴스 모멘텀 개장매수 전략 — 회사 리포트 브리핑이 뽑은 "신선한 촉매"(EVENT 태그)
종목만 대상으로, 그 종목이 속한 시장이 열리는 즉시 매수했다가 고정 % 사다리로
단타 청산한다. 롱 온리.

## 2단 구조 (ADR-0002: 거래 핫패스에 LLM 금지)

이 전략 자체는 뉴스를 읽지 않는다 — 판정은 개장 전 파이프라인이 끝낸다.

> **2026-08-13 정정.** 이 자리에 원래 "판정은 08:40 daily-brief의 Claude 세션이
> 끝냈다"고 적혀 있었다. 그날 회사 리포트 의존을 끊고 `own_brief.sh` 로
> 갈아치우면서 **그 세션이 사라졌는데 이 문단만 남아 있었다.** 그동안 EVENT
> 태그의 실제 조건은 `render.candidates_line` 의 `today_articles > 0` —
> 즉 "오늘 기사가 한 건이라도 있다"였고, 그게 "개장에 사라"가 됐다.
> 실측 피해: 펄어비스 -13,580원(어닝쇼크·목표가 59% 하향 기사가 이미 수집돼
> 있었다), 대신증권 -14,904원(호재 5건에 목표주가 하향 2건이 섞여 있었다).
>
> 지금 방향 판정은 `quant.analyze.news_direction` 의 **결정론적 거부권**이
> 한다 — 명백한 악재 표지(목표가 하향·어닝쇼크·유상증자…)가 제목에 있으면
> NEWS 태그를 주지 않는다. 좁은 규칙이라 대부분의 악재는 놓친다. 제대로 된
> 판정은 Phase 7 의 판단 리더보드에서 LLM 이 도전자로 들어와 결정론적
> 베이스라인을 이겨야 승격되는 방식으로 온다.

리포트가 뽑은 후보를
결정론적 확신도 엔진(`watch_scorer`)이 채점해 통과분만 `data/watchlist.yaml`에
`tags: [EVENT]`로 저장하고(`server/scripts/tg_bridge.py`의 `watch-add --tags`),
이 전략은 그 태그만 읽어 진입 여부를 정한다 — `tags_of` 생성자 인자
(`MeanReversionStrategy`의 `leverage_of`와 같은 배선 패턴: `strategies/__init__.py`의
`build_strategies`와 `app/assembly.py`의 `rebuild_strategies`가 주입한다).
**`tags_of`가 없거나(None/빈 dict) EVENT 태그가 없는 심볼은 절대 진입 대상이
아니다** — 근거 없이 개장가를 사는 것은 이 전략의 정의가 아니다.

## 진입

- 대상: `tags_of`에 `"EVENT"`가 포함된 관심종목만 (`self.symbols` 교집합).
- 시각: 그 종목이 속한 시장이 연 직후 `entry_window_seconds`(기본 120초) 안.
  창을 놓치면 그날은 진입하지 않는다 — 개장 갭을 노리는 전략인데 몇 분 뒤에
  들어가면 다른 전략이 된다.
- 당일 완성봉을 요구하지 않는다 — 개장 직후엔 완성봉이 0개다. `ctx.data.quote()`만
  본다. "과거 흐름"(뉴스 호재 판정, 외국인·기관 수급) 검증은 이미 08:40
  파이프라인(`watch_scorer`의 프리퍼시티 게이트 + EVENT 프로파일의 갭·거래량
  스파이크·뉴스 신선도 채점 + 시장/종목 수급 임계 조정)이 전일까지의 데이터로
  끝낸 뒤 태그만 남긴 것이다 — 이 전략은 그 판정을 재검증하지 않는다.
- 종목당 세션 1회. 세션(하루) 최대 진입 종목 수 `max_entries_per_session`
  (기본 3) — 개장 동시 진입은 버스트라 상한이 없으면 자본이 한 번에 다 나간다.

## 청산 (사용자 지정 고정 % 사다리 — ATR 기반이 아니다)

- `stop_loss_pct`(기본 2.0%): 진입가 대비 이하로 내려가면 전량 청산.
- `full_take_pct`(기본 10.0%) 도달 → 잔량 전량 청산.
- `max_hold_minutes`(기본 30분) 경과 → 잔량 전량 청산(목표 미달 시 시간 손절).
  두 조건이 같은 사이클에 겹치면(예: 31분째에 +12%) 목표가 조건을 우선한다 —
  사유 문구가 더 구체적일 뿐 결과(전량 청산)는 같다.
- `partial_take_pct`(기본 5.0%) 도달 → `partial_fraction`(기본 0.5) 청산,
  **세션당 1회만**(`Position.meta`의 랏에 `partial_taken` 플래그, 재발동 없음).
  타임아웃/목표가 청산이 이미 잔량을 정리했다면 부분 익절 판정 자체가 열리지
  않는다(둘 다 먼저 검사됨).
- EoD 강제청산 + 세션 롤 오버나잇 금지 레일은 다른 스캐너(orb_scan 등)와
  동일하게 반드시 건다 — 30분 타임아웃이 있어도 이 안전망은 별개다.
- 판정은 매 사이클 `ctx.data.quote()` 기준. 기준가는 **내 랏**
  (`pos.ensure_lot(self.id)`)의 `entry`(체결 확인된 진입가) — 다른 전략이 같은
  심볼을 들고 있어도 내 몫만 본다.
- 재시작 복구(진입 컨텍스트 유실): `entered_at`을 `max_hold_minutes`만큼 과거로
  잡아 다음 관리 사이클에서 즉시 시간 손절이 걸리게 한다 — 진짜 진입 시각을
  모르는 상태에서 타이머를 "지금부터" 다시 재는 것보다(원래 의도보다 오래
  들고 있게 될 위험) 조기 청산 쪽이 이 전략의 안전 취지(짧게 들고 빠진다)에
  맞다. `session`은 남기지 않아(orb_scan/mean_reversion과 동일 관례) 세션 롤
  강제청산 오탐을 막는다 — EoD 강제청산이 그날 안에 어차피 정리한다.

## 랏·소유권

`confluence.py`/`orb_scan.py`와 동일한 `_owns` 3단 판정과 `ensure_lot` 패턴을
따른다 — 다른 전략이 같은 심볼을 동시에 들고 있어도 내 랏만 관리한다
(2026-08-11 랏 도입 시맨틱).

## 정직성 경고 [미검증] — 반드시 읽을 것

- **개장가 진입은 구조적으로 불리하다.** 호재 뉴스가 있으면 이미 갭업으로
  시작한다 — 우리가 사는 가격은 뉴스를 이미 반영한 가격이고, 남들보다 먼저
  사는 것이 아니다.
- **−2% 손절은 개장 직후 변동성 기준으로 타이트하다.** 개장 5분은 하루 중
  변동성이 가장 큰 구간이라 갭업 후 되돌림만으로 −2%는 흔하다. **승률이 낮을
  것으로 예상된다** — 검증된 수치는 아니다.
- **+10%/30분은 개별주 일일 변동폭 기준 상한가급 목표다.** 실질적으로는
  대부분 −2% 손절 또는 30분 타임아웃 청산으로 끝날 가능성이 높다.
- payoff 구조(이론상): 최선 +7.5%(+5%에서 절반 익절, 나머지 절반이 +10%에서
  청산), 부분 익절 후 되돌림 시 +1.5%(예시 수치일 뿐 보장 아님), 최악 −2%.
  **payoff는 비대칭적으로 유리해 보이나 승률이 관건이며 아직 측정된 바 없다.**
  이 전략의 수익성을 주장하지 않는다 — 웹에서 흔히 인용되는 "개장 모멘텀"
  승률 수치도 인용하지 않는다. paper 번인(스코어보드의 거래당 bps)이 쌓이기
  전까지는 판단 근거가 없다.

## 개장 확인 (open confirmation, 2026-08-18 관측 기반, 기본 off)

**관측**: 2026-08-18 KR 09:00:11~13, EVENT 태그 3종목(005180·005930·034020)이
"개장 즉시" 매수돼 **3건 전부 -2% 손절**, 합계 -28,996원(그날 총손실의 61%).
그 종목들의 당일 1분봉으로 반사실 측정(EC2 원장·Toss 캔들 조회, 읽기 전용)한 결과:

| 규칙 | 진입 | 그날 결과 |
|---|---|---|
| (a) 기존(개장 즉시) | 3/3 진입 | 3/3 -2% 손절, 합계 -28,996원(실측 정확 일치) |
| (b) 1봉 확인(`bar`) | 0/3 진입(첫 1분봉이 셋 다 음봉/보합) | 손실 0 |
| (c) 5분 확인(`above_open`, 5분) | 0/3 진입(5분 후 셋 다 시가 이하) | 손실 0 |
| (d) 손절만 시가 하회로 변경 | 3/3 진입(기존과 동일) | 근사 -0.02%~-2%(구간 추정, 1분봉 해상도로 이탈 틱 특정 불가 — 종목별 상이) |

(b)/(c)는 "그날 가격 정보가 전혀 없이 사는" 구조적 결함에 대한 명확한 메커니즘
(정보 없이 사지 않는다)이 있고, 오늘 표본에서 손실을 100% 피했다. (d)는 독립된
메커니즘이 아니라 손절 위치만 시가로 옮긴 것이라 "즉시 청산"이 실제로 얼마나
빨리 걸렸을지 1분봉으로는 특정할 수 없어 채택하지 않았다(표본도 1일 3건뿐이라
"오늘 잘 맞았다"만으로 손절 로직을 바꿀 근거가 약하다).

**기본값은 여전히 `off`다** — 위 결과가 (b)/(c)를 지지함에도 기본을 바꾸지 않은
이유는 순전히 하위호환: 기존 테스트 수십 개가 `tags_of`에 EVENT만 채우고 봉 데이터
없이 즉시 진입을 기대한다(백테스트 stub·paper 초기 구동에서도 1분봉 히스토리가
항상 준비돼 있다는 보장이 없다). `bar`/`above_open`을 켜려면 `entry_window_seconds`
(기본 120초)가 확인에 필요한 시간보다 커야 한다 — `bar`는 첫 완성봉(약 60~70초)
안에 끝나 기본 120초 창에 맞지만, `above_open`(기본 5분=300초)은 **기본
`entry_window_seconds`로는 확인이 끝나기 전에 창이 닫혀 절대 진입하지 못한다**
(운영자가 두 값을 함께 조정해야 함 — `config/settings.yaml` 노출은 오케스트레이터
소관). 표본이 쌓이면(paper 번인) `bar`를 기본으로 승격하는 걸 권고한다.

`open_confirm_mode`: `"off"`(기존 동작, 데이터 조회 없음) | `"bar"`(세션 첫
완성 1분봉이 양봉으로 마감해야 진입) | `"above_open"`(`open_confirm_minutes`분
경과 후 현재가가 당일 시가 위여야 진입). 확인 실패(음봉/시가 이하)면 그날 그
종목은 재시도 없이 종료(`_entered_today`에 등록 — "이미 진입함"과 동일 시맨틱
재사용). 데이터가 끝내 도착하지 않으면(사가 짧거나 피드 지연) 진입하지
않는다(폴백 없음) — 이 전략의 결함이 애초에 "정보 없이 산다"였으므로 정보
없으면 안 사는 쪽이 논리적으로 일관된다.

## 시장 리스크오프 게이트 (2026-08-18 관측 기반, shadow 기본)

판정 로직은 `quant/trade/indicators/breadth.py`(순수 함수, 그 모듈 docstring
"측정 결과" 절 참고) — 여기서는 배선만 한다. 시장 앵커(KR=069500)가 당일
시가 대비 `market_risk_max_drawdown_pct`(기본 0.5%)보다 더 빠졌으면 시장별로
1회 판정한다. **실측(2026-08-18) 근거로 이 전략에는 특히 무력하다**: 이
전략의 진입은 개장 후 entry_window_seconds(기본 120초) 안에서만 나가는데,
앵커의 당일 첫 1분봉조차 그 시점엔 아직 닫히지 않아(첫 완성봉은 개장+1분)
`anchor_drawdown`이 거의 항상 None(게이트 부재)이다 — 그래서 기본은 block이
아니라 **shadow**(판정만 계산해 신호 사유에 `[시장:리스크오프 ...]`로 표기,
진입은 막지 않음). 앵커 조회는 시장당 분 경계 캐시(`_anchor_cache`)로
사이클마다 재조회하지 않는다. `market_risk_gate_mode="off"`로 계산 자체를
끌 수 있다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime, timedelta

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction, market_of_symbol
from quant.trade.indicators.breadth import ANCHOR_SYMBOLS, anchor_drawdown
from quant.trade.strategy.orb_scan import _SESSION_OPEN

_EVENT_TAG = "EVENT"
# 진입 자격은 EVENT 뿐이지만, 상한에 걸려 고를 때는 TREND 를 함께 가진 쪽을
# 먼저 본다 — `_rank_candidates` 참고.
_TREND_TAG = "TREND"


class NewsMomentumStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "news_momentum",
                 tags_of: dict[str, list[str]] | None = None):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # {symbol: [태그, ...]}. None/빈 dict면(테스트·백테스트·미배선) EVENT 대상이
        # 하나도 없어 진입 판정 자체를 건너뛴다 — 뉴스 근거 없이 아무 종목이나 사는
        # 것으로 조용히 후퇴하지 않는다(mean_reversion의 leverage_of와 같은 안전
        # 기본값 철학: 정보가 없으면 더 위험한 쪽으로 기본값을 잡지 않는다).
        self.tags_of = tags_of

        self.entry_window_seconds: float = params.get("entry_window_seconds", 120)
        self.max_entries_per_session: int = params.get("max_entries_per_session", 3)
        self.stop_loss_pct: float = params.get("stop_loss_pct", 2.0)
        self.partial_take_pct: float = params.get("partial_take_pct", 5.0)
        self.partial_fraction: float = params.get("partial_fraction", 0.5)
        self.full_take_pct: float = params.get("full_take_pct", 10.0)
        # None/0 이면 시간 청산을 끈다 — 손절·목표가·EoD 강제청산만으로 관리한다
        # (2026-08-13 사용자 지시: "30분 후에 파는거 말고 정해둔 기준이 되면 판다").
        _mh = params.get("max_hold_minutes", 30)
        self.max_hold_minutes: float | None = float(_mh) if _mh else None
        self.flatten_minutes: float = params.get("flatten_before_close_minutes", 1)
        self.risk_budget_pct: float = params.get("risk_budget_pct", 1.0)
        self.max_leverage: float = params.get("max_leverage", 1.0)

        # 시장 리스크오프 게이트(모듈 docstring "시장 리스크오프 게이트" 절,
        # quant/trade/indicators/breadth.py) — scalp_1m의 trend_gate_mode와
        # 동일 관례(off/shadow/block). 기본 shadow(실측 근거: 이 전략의 개장
        # 직후 진입창엔 앵커 데이터가 아직 없어 게이트가 사실상 무력하다).
        mode = str(params.get("market_risk_gate_mode", "shadow")).strip().lower()
        if mode not in ("off", "shadow", "block"):
            mode = "shadow"
        self.market_risk_gate_mode: str = mode
        self.market_risk_max_drawdown_pct: float = params.get("market_risk_max_drawdown_pct", 0.5)
        if self.market_risk_max_drawdown_pct <= 0:
            raise ValueError("market_risk_max_drawdown_pct는 양수여야 합니다.")

        # 개장 확인(모듈 docstring "개장 확인" 절) — 기본 off(기존 동작 100% 보존,
        # 근거는 docstring 절 참고: 하위호환 + above_open은 기본 entry_window_seconds
        # 로는 확인이 끝나기 전에 창이 닫힘).
        confirm_mode = str(params.get("open_confirm_mode", "off")).strip().lower()
        if confirm_mode not in ("off", "bar", "above_open"):
            confirm_mode = "off"
        self.open_confirm_mode: str = confirm_mode
        self.open_confirm_minutes: float = params.get("open_confirm_minutes", 5)
        if self.open_confirm_minutes <= 0:
            raise ValueError("open_confirm_minutes는 양수여야 합니다.")

        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct는 양수여야 합니다.")
        if not 0 < self.partial_fraction <= 1:
            raise ValueError("partial_fraction은 0과 1 사이여야 합니다.")
        if self.entry_window_seconds <= 0:
            raise ValueError("entry_window_seconds는 양수여야 합니다.")

        self._session_date: dict[str, dtdate] = {}
        self._entries_this_session: dict[str, int] = {}
        self._entered_today: set[str] = set()
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}
        # 앵커 봉 분 경계 캐시 — {market: (분 키, bars)}(scalp_1m `_get_bars`와
        # 동일 패턴, 심볼이 아니라 시장 단위 — 앵커는 시장당 1개).
        self._anchor_cache: dict[str, tuple[str, object]] = {}
        # 개장 확인용 봉 분 경계 캐시 — {symbol: (분 키, bars)}(위 앵커 캐시와 동일
        # 패턴이지만 후보 심볼별. open_confirm_mode="off"면 절대 채워지지 않는다
        # (그 모드는 _confirm_open이 이 캐시를 건드리기 전에 조기 반환).
        self._bars_cache: dict[str, tuple[str, object]] = {}
        # 마지막 시장 리스크오프 판정 — {market: bool}(shadow 표본 관측용).
        self.market_risk_verdict: dict[str, bool] = {}

    # ------------------------------------------------------------------ 사이클

    def _owns(self, pos: Position) -> bool:
        """orb_scan.py/confluence.py의 `_owns`와 동일 구현(2026-08-11 랏 도입 포함) —
        소유권 없는 포지션을 잘못 청산하거나 다른 전략이 이미 추적 중인 lot을
        입양하지 않는다."""
        if pos.lot(self.id) is not None:
            return True
        meta = pos.meta or {}
        if meta.get("lots") is not None:
            return False
        owner = meta.get("strategy")
        if owner:
            return owner == self.id
        return pos.symbol in self.symbols

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        positions = ctx.broker.positions()

        # 1) 포지션 관리 — 유니버스와 무관하게 열려 있는 자기 포지션을 본다.
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            self._ensure_state(symbol, pos, ctx)
            signal = self._manage_position(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)

        # 2) EVENT 태그 대상만 진입 후보로 삼는다. 태그 정보 자체가 없으면(백테스트
        #    stub, tags_of 미배선) 후보가 있을 수 없다 — 네트워크/파일 조회 없이
        #    즉시 빠진다.
        if not self.tags_of:
            return signals
        candidates = [s for s in self.symbols if _EVENT_TAG in self.tags_of.get(s, [])]
        if not candidates:
            return signals

        markets_present = sorted({market_of_symbol(s) for s in candidates})
        for market in markets_present:
            if not ctx.clock.is_market_open(market):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = ctx.clock.now().astimezone(tz)
            today = now_local.date()
            if today != self._session_date.get(market):
                self._session_date[market] = today
                # 세션 롤은 시장별이다 — 한쪽 시장이 새 날이 됐다고 다른 시장 종목의
                # "오늘 진입함" 기록을 지우면 그 시장 장중에 재진입 창이 다시 열린다.
                self._entered_today = {
                    s for s in self._entered_today if market_of_symbol(s) != market
                }
                self._entries_this_session[market] = 0
                self._pending = {
                    s: p for s, p in self._pending.items()
                    if market_of_symbol(s) != market
                    or (positions.get(s) is not None and positions.get(s).is_open)
                }
                self._anchor_cache.pop(market, None)

            if self._entries_this_session.get(market, 0) >= self.max_entries_per_session:
                continue

            session_open_dt = datetime.combine(today, session_open, tzinfo=tz)
            seconds_since_open = (now_local - session_open_dt).total_seconds()
            if not 0 <= seconds_since_open <= self.entry_window_seconds:
                continue

            # 시장 리스크오프 게이트 — 시장당 1회 판정(모듈 docstring "시장
            # 리스크오프 게이트" 절). 후보 심볼 수와 무관하게 앵커 조회는 한 번.
            market_blocked, market_note = self._market_risk_note(market, ctx)

            market_candidates = self._rank_candidates(
                [s for s in candidates if market_of_symbol(s) == market]
            )
            for symbol in market_candidates:
                if self._entries_this_session.get(market, 0) >= self.max_entries_per_session:
                    break
                pos = positions.get(symbol)
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                if held_qty > 0 or symbol in self._entered_today:
                    continue  # 세션당 1회 — 재진입 없음(allow_add_while_holding 없음)
                if market_blocked:
                    self.last_reject[symbol] = (
                        f"시장 리스크오프 차단(앵커={ANCHOR_SYMBOLS.get(market)})"
                    )
                    continue
                confirm_status, confirm_note = self._confirm_open(
                    symbol, market, ctx, session_open_dt, now_local,
                )
                if confirm_status == "wait":
                    continue  # 아직 판정 불가 — 진입 창이 남아 있는 한 다음 사이클 재시도
                if confirm_status == "fail":
                    # "이미 진입함"과 동일 시맨틱 재사용 — 오늘 이 종목은 재시도 없음
                    # (모듈 docstring "개장 확인" 절).
                    self._entered_today.add(symbol)
                    self.last_reject[symbol] = f"개장확인 실패({self.open_confirm_mode})"
                    continue
                signal = self._check_entry_for(
                    symbol, market, ctx, market_note=market_note, confirm_note=confirm_note,
                )
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------ 시장 리스크오프

    def _get_anchor_bars(self, market: str, ctx: Context):
        """분 경계 캐시 — 시장당 1개 앵커, 같은 분 안의 반복 호출은 캐시 재사용
        (scalp_1m `_get_bars`와 동일 패턴). 등록되지 않은 시장(ANCHOR_SYMBOLS에
        없음)은 None."""
        anchor = ANCHOR_SYMBOLS.get(market)
        if anchor is None:
            return None
        tz, _ = _SESSION_OPEN[market]
        minute_key = ctx.clock.now().astimezone(tz).strftime("%Y-%m-%d %H:%M")
        cached = self._anchor_cache.get(market)
        if cached is not None and cached[0] == minute_key:
            return cached[1]
        bars = ctx.data.history(anchor, "1m", 400)
        self._anchor_cache[market] = (minute_key, bars)
        return bars

    def _market_risk_note(self, market: str, ctx: Context) -> tuple[bool, str]:
        """시장 리스크오프 판정(모듈 docstring 절 참고) — (차단 여부, 신호
        사유용 표기). off 모드는 앵커 조회 자체를 하지 않는다."""
        if self.market_risk_gate_mode == "off":
            return False, ""
        bars = self._get_anchor_bars(market, ctx)
        dd = anchor_drawdown(bars) if bars is not None else None
        if dd is None:
            return False, ""  # 게이트 부재(앵커 데이터 없음) — 기존 동작
        risk_off = dd <= -self.market_risk_max_drawdown_pct
        self.market_risk_verdict[market] = risk_off
        if not risk_off:
            return False, ""
        note = f" [시장:리스크오프 {ANCHOR_SYMBOLS[market]} {dd:+.2f}%]"
        return (self.market_risk_gate_mode == "block"), note

    def _rank_candidates(self, symbols: list[str]) -> list[str]:
        """진입 우선순위 — **관심종목 파일 순서가 아니라 근거의 강도**로 고른다.

        왜 필요한가(2026-08-13 실장 관측): 세션 상한이 3인데 후보가 8개였고, 이전
        구현은 `self.symbols` 순서대로 앞 3개를 집었다. 그 순서는 자동 편입 스크립트가
        태그별로 묶어 등록한 결과라 `sort -u`의 알파벳 순("EVENT" < "TREND+EVENT")이
        그대로 반영돼 있었다 — 결과적으로 **알파벳 정렬이 매수 종목을 정했다.**
        그날 EVENT만 있던 대신증권·신세계·LG이노텍이 체결되고, 뉴스·트렌딩·거래대금
        랭킹 1·2위를 전부 차지한 삼성전자·SK하이닉스는 상한에 걸려 밀렸다.

        기준: **TREND 태그를 함께 가진 종목을 먼저.** EVENT는 "뉴스 촉매가 있다",
        TREND는 "거래대금/상승률 랭킹에 실제로 올라 있다"는 뜻이다. 둘 다인 종목은
        '언급만 된' 것이 아니라 **실제로 돈이 몰리는 중인 호재주**이고, 이 전략이
        노리는 개장 매수세가 붙을 가능성이 그쪽이 높다.

        동점은 심볼 오름차순 — 상한에 걸려 잘리는 종목이 매일 달라지면 성과를
        해석할 수 없다(결정론).

        한계: 태그는 있고/없고 뿐이라 트렌딩 **점수**(리포트가 계산한 0~100)까지는
        보지 못한다. 점수를 관심종목 파일에 실어 보내면 더 정확해지겠지만 그건
        스키마 변경이라 별도 작업으로 남긴다.
        """
        return sorted(symbols, key=lambda s: (_TREND_TAG not in self.tags_of.get(s, []), s))

    # ------------------------------------------------------------------ 개장 확인

    # 개장 확인 봉 조회 lookback — bar 모드(첫 1개봉)·above_open 모드(기본 5분)
    # 둘 다 넉넉히 덮는다. 실거래 값이 아니라 조회 개수라 설정 노출 대상이 아니다.
    _CONFIRM_LOOKBACK_BARS = 60

    def _get_symbol_bars(self, symbol: str, market: str, ctx: Context):
        """개장 확인 전용 분 경계 캐시 — scalp_1m `_get_bars`/위 `_get_anchor_bars`와
        동일 패턴(같은 분 안의 반복 호출은 재조회하지 않음), 앵커와 달리 후보
        심볼별로 캐시한다."""
        tz, _ = _SESSION_OPEN[market]
        minute_key = ctx.clock.now().astimezone(tz).strftime("%Y-%m-%d %H:%M")
        cached = self._bars_cache.get(symbol)
        if cached is not None and cached[0] == minute_key:
            return cached[1]
        bars = ctx.data.history(symbol, "1m", self._CONFIRM_LOOKBACK_BARS)
        self._bars_cache[symbol] = (minute_key, bars)
        return bars

    def _confirm_open(
        self, symbol: str, market: str, ctx: Context, session_open_dt: datetime, now_local: datetime,
    ) -> tuple[str, str]:
        """개장 확인 판정(모듈 docstring "개장 확인" 절). 반환 (status, note):
        status는 "ok"(진입 가능)/"wait"(아직 판정 불가, 재시도)/"fail"(오늘 이
        종목은 확인 실패, 재시도 없음). note는 "ok"일 때 신호 사유에 붙일 표기.

        off 모드는 `ctx.data.history` 조회 자체를 하지 않고 즉시 "ok"를 반환한다
        — 기존 동작 100% 보존(네트워크/캐시 부작용 없음, 기존 테스트의 history 호출
        카운트도 그대로)."""
        if self.open_confirm_mode == "off":
            return "ok", ""

        bars = self._get_symbol_bars(symbol, market, ctx)
        if bars is None or len(bars) == 0:
            return "wait", ""  # 데이터 미도착 — 폴백 없음(진입하지 않는 쪽이 기본)
        session_bars = bars[bars.index >= session_open_dt]
        if len(session_bars) == 0:
            return "wait", ""
        open_bar = session_bars.iloc[0]
        day_open = float(open_bar["open"])

        if self.open_confirm_mode == "bar":
            if float(open_bar["close"]) > day_open:
                return "ok", " [개장확인:bar]"
            return "fail", ""

        # above_open — N분 경과 전엔 아직 판정하지 않는다(재확인 대상 아님, 스펙:
        # "N분 후 시가 위 유지 시" 1회 판정).
        elapsed_min = (now_local - session_open_dt).total_seconds() / 60
        if elapsed_min < self.open_confirm_minutes:
            return "wait", ""
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            return "wait", ""
        if quote.price > day_open:
            return "ok", " [개장확인:above_open]"
        return "fail", ""

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(
        self, symbol: str, market: str, ctx: Context, *, market_note: str = "", confirm_note: str = "",
    ) -> Signal | None:
        self.last_reject.pop(symbol, None)
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        entry_price = quote.price

        risk_pct = self.stop_loss_pct / 100
        target_weight = min((self.risk_budget_pct / 100) / risk_pct, self.max_leverage)
        stop = entry_price * (1 - risk_pct)

        self._pending[symbol] = {
            "entry": entry_price,
            "entered_at": ctx.clock.now().isoformat(),
            "partial_taken": False,
            "session": self._session_date[market].isoformat(),
            "strategy": self.id,
            # 세션당 1회 진입이라 항상 최초 체결이다(기존 보유분 위에 얹는 추가
            # 진입이 없음) — 체결 확인 기준은 그래서 항상 0.
            "qty_at_signal": 0.0,
        }
        self._entered_today.add(symbol)
        self._entries_this_session[market] = self._entries_this_session.get(market, 0) + 1

        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"뉴스 모멘텀 개장진입(EVENT): {symbol} w={target_weight:.2f} "
                f"손절 -{self.stop_loss_pct:g}%{market_note}{confirm_note}"
            ),
            stop=stop,
        )

    # ------------------------------------------------------------------ 관리

    def _ensure_state(self, symbol: str, pos: Position, ctx: Context) -> None:
        lot = pos.ensure_lot(self.id)
        pending = self._pending.get(symbol)
        if pending is not None and lot.get("qty", 0.0) > pending.get("qty_at_signal", 0.0):
            self._pending.pop(symbol, None)
            fresh = {k: v for k, v in pending.items() if k != "qty_at_signal"}
            lot.update(fresh)
            return
        if "entry" in lot:
            return
        # 재시작 복구 — 진짜 진입 시각/세션을 모른다. 모듈 docstring 참고: 타이머를
        # "지금부터" 다시 재면 원래 의도보다 오래 들고 있게 될 위험이 있어, 대신
        # 이미 max_hold_minutes가 지난 것으로 보수적으로 잡아 다음 관리 사이클에서
        # 곧바로 시간 손절이 걸리게 한다. session은 남기지 않아(orb_scan/
        # mean_reversion과 동일 관례) 세션 롤 강제청산 오탐을 막는다.
        entry = lot.get("avg_cost", pos.avg_cost)
        # 타임아웃이 꺼져 있으면 이 트릭(즉시 시간손절 유도)이 성립하지 않는다 —
        # 진입 시각을 지금으로 두고 손절·목표가·EoD 레일에 맡긴다.
        recovered_entered_at = (
            ctx.clock.now() - timedelta(minutes=self.max_hold_minutes)
            if self.max_hold_minutes else ctx.clock.now()
        )
        lot.update(entry=entry, entered_at=recovered_entered_at.isoformat(),
                   partial_taken=False, session=None)

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry = lot["entry"]
        market = market_of_symbol(symbol)
        tz, _ = _SESSION_OPEN[market]

        # 오버나잇 금지 — should_flatten 하나에 기대지 않는다(orb_scan의 세션 롤
        # 레일과 동일 이유).
        entry_session = lot.get("session")
        if entry_session and entry_session != ctx.clock.now().astimezone(tz).date().isoformat():
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} 현재={price:.2f}",
            )
        if ctx.clock.should_flatten(market, self.flatten_minutes):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"EoD 청산: entry={entry:.2f} 현재={price:.2f}",
            )

        stop_price = entry * (1 - self.stop_loss_pct / 100)
        if price <= stop_price:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(
                    f"손절(-{self.stop_loss_pct:g}%): entry={entry:.2f} "
                    f"stop={stop_price:.2f} 현재={price:.2f}"
                ),
            )

        full_price = entry * (1 + self.full_take_pct / 100)
        if price >= full_price:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(
                    f"목표가 도달(+{self.full_take_pct:g}%): entry={entry:.2f} "
                    f"현재={price:.2f} 잔량 전량 청산"
                ),
            )

        entered_at = lot.get("entered_at")
        if entered_at and self.max_hold_minutes:
            elapsed_min = (ctx.clock.now() - datetime.fromisoformat(entered_at)).total_seconds() / 60
            if elapsed_min >= self.max_hold_minutes:
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(
                        f"보유시간 초과({elapsed_min:.1f}분 >= {self.max_hold_minutes:g}분): "
                        f"entry={entry:.2f} 현재={price:.2f} 잔량 청산"
                    ),
                )

        partial_price = entry * (1 + self.partial_take_pct / 100)
        if not lot.get("partial_taken") and price >= partial_price:
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                target_weight=0.0, exit_fraction=self.partial_fraction,
                reason=(
                    f"부분 익절(+{self.partial_take_pct:g}%): entry={entry:.2f} "
                    f"현재={price:.2f} {self.partial_fraction * 100:.0f}% 청산"
                ),
                state_update={"partial_taken": True},
            )
        return None
