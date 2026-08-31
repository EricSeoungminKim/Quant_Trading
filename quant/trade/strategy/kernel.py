"""전략 파일들이 바이트 단위로 반복해 온 순수 로직의 공용 커널.

`quant/trade/strategy/CLAUDE.md`(순수 전략 규칙)와 같은 등급 — **순수 함수만,
`quant.core` 밖의 아무것도 임포트하지 않는다**(네트워크·config·브로커 금지,
`quant/trade/indicators/__init__.py`와 같은 제약). 여기 있는 각 함수는 여러
전략 파일에 바이트 단위로 복붙돼 있던 코드를 그대로 옮긴 것 — 새 동작이
아니라 중복 제거다. 어느 중복을 대체하는지는 각 함수 docstring에 출처 파일과
벌 수를 적어 둔다.

이 파일에 손대는 사람에게: 여기 함수 하나를 바꾸면 그 함수를 쓰는 **모든**
전략이 동시에 바뀐다 — 전략 하나만 고치고 싶으면 그 전략 파일에 로컬로
남겨라, 이 커널에 넣지 마라.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Mapping

from quant.core.models import Signal, SignalAction
from quant.core.session import continuous_window, market_tz


def exit_signal(strategy_id: str, symbol: str, reason: str) -> Signal:
    """청산 신호 생성 — 7개 파일의 바이트 동일 `_exit(reason)` 클로저를
    대체한다: gap_fade, intraday_momentum, mr_vwap_quiet, pullback_impulse,
    rsi2_dip, overnight_drift, vol_breakout (각 파일 `decide()`/`_manage()`
    안의 로컬 클로저였다 — `target_weight=0.0, exit_fraction=1.0`으로 전량
    청산을 표현하는 동일한 `Signal`)."""
    return Signal(
        strategy_id=strategy_id, symbol=symbol, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0, reason=reason,
    )


def parse_min_stop_bp(params: Mapping[str, Any], default: float = 40.0) -> float:
    """`params`에서 `min_stop_bp` 파싱 + 검증 — intraday_momentum, gap_fade,
    pullback_impulse, vol_breakout 4개 파일의 동일한 `__init__` 파싱 줄
    (`float(params.get("min_stop_bp", 40.0))`)과 동일한 검증 줄(`if
    self.min_stop_bp < 0: raise ValueError(...)`)을 하나로 합친다. 0은
    "게이트 비활성"이라는 의미라 허용하고, 음수만 거부한다."""
    value = float(params.get("min_stop_bp", default))
    if value < 0:
        raise ValueError("min_stop_bp는 0(비활성) 이상이어야 합니다.")
    return value


def stop_bp_gate_ok(stop_bp: float, min_stop_bp: float) -> bool:
    """손절폭(bp)이 최소 문턱을 통과하는가 — intraday_momentum, gap_fade,
    pullback_impulse, vol_breakout 4개 파일의 진입 게이트(`stop_bp <
    self.min_stop_bp`면 거부)를 대체한다. `min_stop_bp<=0`이면 게이트가
    비활성이라 항상 통과(참) — 4개 파일 모두 0을 "비활성"으로 문서화한
    관례와 같다."""
    if not min_stop_bp:
        return True
    return stop_bp >= min_stop_bp


def my_lot(lots: Mapping[str, Mapping[str, Any]], symbol: str) -> Mapping[str, Any] | None:
    """내가 **진입가(entry)를 써 넣은** 열린 랏만 돌려준다 — gap_fade,
    mr_vwap_quiet, vol_breakout, rsi2_dip, overnight_drift 5개 파일의 동일한
    `_my_lot(snap, symbol)` 정적 메서드를 대체한다.

    `lots[symbol]`이 빈 dict(`{}`)인 경우가 두 가지 있어(`shell.py`
    `_snapshot`) 스냅샷만으로는 구분되지 않는다: (a) 다른 전략이 그 심볼을
    보유 중이라 내 lot이 없다, (b) 내가 방금 체결됐는데 아직 lot 필드가
    없다. `entry` 유무로 판정하면 두 경우 모두 "내 관리 대상이 아니다"로
    안전하게 떨어지고, (a)에서 남의 포지션을 내 것으로 오인해 청산 주문을
    내는 사고가 구조적으로 불가능해진다."""
    lot = lots.get(symbol)
    if not lot or lot.get("entry") is None:
        return None
    return lot


def held_lot(
    lots: Mapping[str, Mapping[str, Any]], candidates: Iterable[str]
) -> tuple[str, Mapping[str, Any]] | None:
    """`candidates` 중 지금 내가 방어선을 써 넣은 랏이 있는 첫 심볼 —
    intraday_momentum의 `_held_lot`(다중 후보 변형, `long_symbol`/
    `short_symbol` 두 개를 순회)을 대체한다. 판정 기준은 `my_lot`과 같다
    (`entry` 유무)."""
    for symbol in candidates:
        lot = lots.get(symbol)
        if lot and lot.get("entry") is not None:
            return symbol, lot
    return None


def should_flatten_calendar(
    minutes_to_close: float | None, cadence_minutes: float, flatten_minutes: float
) -> bool:
    """단일(캘린더 기준) EoD 강제청산 판정 — donchian, gap_fade,
    pullback_impulse, news_momentum, vol_breakout, scalp_1m 6개 파일의 동일한
    `_should_flatten(market, snap)` 재현식을 대체한다(`Clock._should_flatten`,
    `quant/core/clock.py`의 스냅샷 원재료 재구현). `minutes_to_close`가
    None이거나 0 이하(연속 거래 종료/동시호가)면 False. `cadence_minutes`를
    빼는 이유는 판단 주기가 굵을 때 "마감 N분 전" 경계를 사이클 사이에서
    건너뛰지 않기 위해서다(`clock.py` docstring의 3배 레버리지 ETF 오버나잇
    실사고 근거)."""
    if minutes_to_close is None or minutes_to_close <= 0:
        return False
    return minutes_to_close - cadence_minutes < flatten_minutes


def should_flatten_dual(
    market: str,
    now: datetime,
    minutes_to_close: float | None,
    cadence_minutes: float,
    flatten_minutes: float,
) -> bool:
    """이중 EoD 강제청산 판정(캘린더 기준 **또는** 연속거래종료 벽시계 기준의
    논리합) — intraday_momentum, mr_vwap_quiet 2개 파일의 동일한
    `_should_flatten(market, snap)`을 대체한다.

    (a) **캘린더 기준**(조기폐장 방어): `minutes_to_close`(SessionCalendar
        경유, 조기폐장 인지) − `cadence_minutes` < `flatten_minutes`.
    (b) **연속 거래 종료 기준**(주경로, KR 15:20 / US 16:00): 고정
        시간표(`continuous_window`)까지 남은 벽시계 시간 − `cadence_minutes`
        < `flatten_minutes`. `Clock.minutes_to_close`는 정규장 마감(KR
        15:30)까지를 세므로 그것만 쓰면 KR 청산 신호가 동시호가 안에서
        나간다 — 체결될 수 없는 주문이다.

    둘 다 `cadence_minutes`를 빼서 판정한다 — 다음 사이클이 오기 전에 창이
    닫히면 이번 사이클에 나가야 한다."""
    if minutes_to_close is not None and 0 < minutes_to_close and minutes_to_close - cadence_minutes < flatten_minutes:
        return True
    tz = market_tz(market)
    now_local = now.astimezone(tz)
    _, end_t = continuous_window(market)
    remaining = (
        datetime.combine(now_local.date(), end_t, tzinfo=tz) - now_local
    ).total_seconds() / 60
    return 0 < remaining and remaining - cadence_minutes < flatten_minutes


def is_overnight_carry(lot: Mapping[str, Any], today_iso: str) -> bool:
    """세션 롤 강제청산 판정 — gap_fade, intraday_momentum, mr_vwap_quiet,
    pullback_impulse, vol_breakout 5개 파일의 동일한 판정(`entry_session =
    lot.get("session"); if entry_session and entry_session != 오늘:
    청산`)을 대체한다. 랏에 진입 세션 날짜가 기록돼 있고 그게 오늘이 아니면
    참 — 오버나잇 보유가 됐다는 뜻이다.

    세션 롤로 하루짜리 상태(`session_date`/`taken`/`entries_today` 등)를
    "무엇을 지울지"는 전략마다 다르므로 여기서 강제하지 않는다 —
    `session_rolled()`가 그 판정만 따로 제공한다."""
    entry_session = lot.get("session")
    return bool(entry_session) and entry_session != today_iso


def session_rolled(prev_session_iso: str | None, today_iso: str) -> bool:
    """오늘이 마지막으로 기록된 세션 날짜와 다른가 — 여러 전략이 하루짜리
    상태(`session_date` 딕셔너리 등)를 새 날에 리셋할지 판단하는 데 쓰는
    반복 패턴(`if today_iso == session_date.get(market): continue` 형태)의
    판정부만 뽑은 것이다. **무엇을 지울지는 전략에 남는다** — 이 함수는
    "지금 리셋해야 하는가"만 답하고, 리셋 자체(어떤 딕셔너리의 어떤 키를
    비울지)는 강제하지 않는다. `prev_session_iso`가 None(첫 사이클)이면 항상
    참."""
    return prev_session_iso != today_iso
