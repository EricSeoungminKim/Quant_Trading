"""사이징 + 하드레일. RiskManager Protocol 구현.

사이징 모드는 `risk.sizing_mode`로 고른다:
- `capital_fraction` (기본, 기존 동작): budget = min(target_weight * 전략자본, 잔여룸).
  전략자본 = capital_fraction * 총자산. 백테스트/paper의 기존 결과를 바꾸지 않는다.
- `cash_pct`: budget = **그날 실제 가용 현금** * target_weight * 국면배수.
  사용자가 같은 계좌에서 손으로도 매매하기 때문에 필요하다 — 고정 시작자본이나
  총자산을 기준으로 잡으면, 사용자가 현금을 빼간 날에도 엔진이 같은 크기로 들어가려
  들고 매수 여력을 초과한다. 가용 현금이 줄면 자동으로 작게 들어가는 것이 의도다.
  가용 현금은 `Broker.cash()` — TossBroker에서는 buying-power API의 cashBuyingPower다.

두 모드 공통: max_symbol_pct_total(총자산 대비)/포트폴리오 상한/팻핑거 가드가 그대로 적용된다.
US는 소수점 수량, KR은 정수 내림. daily_loss_limit_pct 도달 시 당일 신규
ENTER_LONG/SCALE_IN만 차단(청산은 항상 허용).

capital_fraction은 전략별로 스칼라(양 시장 동일, 기존 동작) 또는 시장별 dict
({"KR": .., "US": ..})를 받는다(2026-08-12). 시장별 dict에서 빠진 시장은 0.0 —
그 전략은 그 시장에서 진입 자체가 차단된다(_capital_fraction_for 참고). 이
게이트는 **두 사이징 모드 공통**이다 — cash_pct도 budget 산식에는 안 쓰지만
"이 전략이 이 시장에서 거래 가능한가"는 같은 답을 써야 한다.

회로차단기(코드 버그와 무관하게 걸리는 독립 레일 — 브로커가 포지션 메타를 잃어 10초마다
절반씩 매도하던 상태 폭주 사고 이후 추가됨. quant-expert SKILL.md §5 참고):
- max_orders_per_day: 하루 승인 주문 수 상한(전 전략/종목 합산).
- cooldown_bars_after_stop: 손절 청산 후 해당 종목 신규 진입을 N봉 차단(휩소 재진입 방지).
- max_order_notional_pct: 최종 계산된 단일 주문 금액이 자산 대비 이 비율을 넘으면 거부
  (max_position_pct/max_symbol_pct_total의 "산식 자체가 잘못됐을 때"를 잡는 독립 재검증).
- NaN/inf/음수/0 수량 가드: 가격/자산이 NaN이어도 주문이 만들어지지 않게 한다.

이 네 가지 전부 **청산(EXIT_LONG/SCALE_OUT)은 절대 막지 않는다** — 청산을 막으면 손실
포지션을 가두는 꼴이라 방어하려는 리스크보다 더 나쁘다. daily_loss_limit_pct와 동일한
원칙(_ENTRY_ACTIONS에만 적용)을 따른다.

**장 마감 게이트는 이 원칙의 예외가 아니다.** approve()는 시장이 닫혀 있으면 진입뿐
아니라 청산 신호도 막는다(2026-08-12 감사 A-5) — 이건 리스크 판단이 아니라 물리적
제약이다: 닫힌 시장에는 애초에 체결될 가격이 없다. "청산은 절대 막지 않는다"는 열려
있는 시장에서 리스크 레일이 손실 포지션을 가두지 않는다는 뜻이지, 존재하지 않는
호가에 억지로 체결시키라는 뜻이 아니다. 전략은 계속 신호를 낸다(관측 가능) — 주문만
다음 개장까지 보류된다.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from quant.core.fx import FixedFxProvider, FxProvider
from quant.core.ports import Context
from quant.core.models import (
    Order,
    Position,
    Side,
    Signal,
    SignalAction,
    market_of_symbol,
    trading_day,
)
from quant.core.portfolio.portfolio import from_krw, to_krw
from quant.trade.risk.books import StrategyBooks

# 확장 세션 창 판정용 — 기본 폴백은 KST(crontab·clock 과 동일).
_KST = ZoneInfo("Asia/Seoul")

# 거래소 현지 시간대 — 확장 세션 창은 **그 시장의 현지 시각**으로 판정한다(DST 안전).
# KST 하나로 KR/US 를 함께 판정하면 서머타임 전환기(3월/11월)에 US 창이 KST 기준
# 1시간씩 밀린다(EDT UTC-4 ↔ EST UTC-5, KST 오프셋은 고정 +9) — 이 저장소가 이미
# crontab 에서 겪은 것과 같은 종류의 함정이다(server/CLAUDE.md). KR 은 DST 가 없어
# KST=현지 시각이므로 이 표를 도입해도 KR 판정은 기존과 100% 동일하다.
_MARKET_TZ: dict[str, ZoneInfo] = {
    "KR": ZoneInfo("Asia/Seoul"),
    "US": ZoneInfo("America/New_York"),
}

_ENTRY_ACTIONS = (SignalAction.ENTER_LONG, SignalAction.SCALE_IN)
_EXIT_ACTIONS = (SignalAction.EXIT_LONG, SignalAction.SCALE_OUT)

# donchian.py가 손절 청산에 실제로 쓰는 사유 문자열의 마커. Signal에 exit_kind 같은 구조화
# 필드가 없어(도메인 모델 확장은 이 변경의 범위 밖) reason 텍스트로 판별한다 — 취약하지만
# domain/models.py나 strategies/를 건드리지 않고 쿨다운을 구현할 수 있는 유일한 방법이다.
# 새 전략을 추가하거나 donchian의 손절 사유 문구를 바꾸면 이 마커도 같이 확인할 것.
_STOP_LOSS_MARKER = "손절"

# app/loop.py의 _flatten_all이 "장 마감으로 청산이 보류됐다"를 구분해 알림을 보낼 때
# last_block 문자열에서 찾는 마커. approve()의 block 사유 문구를 바꾸면 같이 확인할 것.
MARKET_CLOSED_MARKER = "장 마감"

logger = logging.getLogger(__name__)


class RiskManagerImpl:
    def __init__(
        self,
        settings: dict,
        capital_fraction: dict[str, float | dict[str, float]] | None = None,
        market_of: dict[str, str] | None = None,
        fx: FxProvider | None = None,
        state_path: "Path | str | None" = None,
        leverage_of: dict[str, float] | None = None,
        books: "StrategyBooks | None" = None,
    ):
        risk_cfg = settings.get("risk", {})
        self.max_position_pct = risk_cfg.get("max_position_pct", 50) / 100
        self.max_symbol_pct_total = risk_cfg.get("max_symbol_pct_total", 0) / 100
        self.daily_loss_limit_pct = risk_cfg.get("daily_loss_limit_pct", 3) / 100
        # 하루 상한 30: donchian은 15분봉·2종목·max_concurrent_names=1이라 세션당
        # 종목별 최대 ~26봉의 판단 기회뿐이고, 정상적인 풀 왕복(진입+스케일인+
        # 스케일아웃+청산=4주문)이 하루 4~5회 반복돼도(재진입 쿨다운으로 더 제한됨)
        # 종목당 16~20건, 2종목 합산 30건 안팎이 현실적 상한이다. 반면 이 레일이
        # 막으려던 폭주 버그는 poll_seconds=10초마다 주문을 냈으므로 30건은 5분 안에
        # 소진된다 — 정상 거래를 막지 않으면서 버그는 수 분 내로 잡아낸다.
        # **0 = 비활성**(2026-08-14 사용자 판단). 실측 4거래일 시장별 하루 진입 최대
        # 9건 / 상한 30 — 이 상한은 정상 진입을 막은 적이 없다. 그리고 총량 상한은
        # "바쁜 날"과 "버그 난 루프"를 구분하지 못한다. 폭주 방어는 아래 반복 진입
        # 레일이 맡는다(그게 실제 사고의 모양이다).
        self.max_orders_per_day = risk_cfg.get("max_orders_per_day", 30)
        # 같은 (종목, 전략)이 이 창 안에서 이만큼 반복 진입하면 차단. 0 = 비활성.
        # 3/5분인 이유: 2026-08 폭주는 10초 폴링마다 재발동했으므로 30초면 걸린다.
        # 반면 정상 재진입은 손절 쿨다운(4봉)과 봉 간격(15분)에 이미 눌려 있어
        # 5분 안에 같은 종목·전략이 3번 진입하는 건 버그 말고는 설명이 없다.
        self.max_repeat_entries_per_window = int(
            risk_cfg.get("max_repeat_entries_per_window", 3))
        self.repeat_entry_window_minutes = float(
            risk_cfg.get("repeat_entry_window_minutes", 5))
        # 최소 주문 명목가(KRW). 이보다 작은 주문은 수수료만 내고 스코어보드 표본만
        # 오염시킨다. 0이면 비활성 — 기존 백테스트 결과를 바꾸지 않으려면 0으로 둔다.
        self.min_order_notional_krw = float(risk_cfg.get("min_order_notional_krw", 0))
        # 4봉(15분봉 기준 1시간): 손절 직후 같은 신호가 곧바로 재발동하는 휩소 재진입을
        # 막되, 세션(약 26봉)의 상당 부분을 잃지 않을 정도로 짧게 잡는다.
        self.cooldown_bars_after_stop = risk_cfg.get("cooldown_bars_after_stop", 4)
        # 쿨다운 봉 카운팅에 쓰는 봉 간격(분) — 전략이 쓰는 간격과 달라지면 안 된다.
        # 단일 전역값이던 시절 orb(5분봉)에 15분봉이 적용됐고, DataFeed에 15분봉이
        # 없는 구성에서는 history()가 빈 프레임을 돌려줘 **쿨다운이 조용한 no-op**이
        # 됐다(에러도 로그도 없이 그냥 동작하지 않는다). 아래 dict가 전략별 실제
        # 간격을 들고, 이 값은 전략을 못 찾았을 때의 폴백으로만 남는다.
        self.cooldown_bar_interval_minutes = risk_cfg.get("cooldown_bar_interval_minutes", 15)
        # 확장 세션 허용 목록 (2026-08-18, scalp_1m 프리마켓). 장 닫힘 게이트는
        # 물리적 제약이지만, KR 프리마켓(NXT 08:00~08:50)은 실제로 체결이 존재하는
        # 세션이다(Toss 1m 봉 실측 59개/일). **명시된 (전략, 시장, 시각 창)만**
        # 정규장 밖 주문을 통과시킨다 — 기본 비어 있으므로 이 키가 없는 구성의
        # 판정은 이 파일을 고치기 전과 동일하고, 나머지 전략은 계속 막힌다.
        # 형식: {strategy_id: {market: ["HH:MM-HH:MM", ...]}} — 시각은 **그 시장의
        # 현지 시각**이다(KR=KST, US=America/New_York, `_MARKET_TZ` 참고). 평일만.
        self._extended_sessions: dict[str, dict[str, list[tuple[int, int]]]] = {}
        for sid, mkts in (risk_cfg.get("extended_sessions") or {}).items():
            for mkt, windows in (mkts or {}).items():
                parsed = []
                for w in windows or []:
                    try:
                        a, b = str(w).split("-")
                        ah, am = a.split(":"); bh, bm = b.split(":")
                        parsed.append((int(ah) * 60 + int(am), int(bh) * 60 + int(bm)))
                    except (ValueError, AttributeError):
                        continue  # 형식 오류 창은 조용히 넓히지 않는다 — 무시가 안전측
                if parsed:
                    self._extended_sessions.setdefault(str(sid), {})[str(mkt).upper()] = parsed
        # 전략마다 파라미터 이름이 다르다(donchian=interval_minutes, orb=bar_interval_minutes).
        self._strategy_bar_minutes: dict[str, int] = {}
        for sid, strat_cfg in settings.get("strategies", {}).items():
            params = strat_cfg.get("params", {})
            self._strategy_bar_minutes[sid] = int(
                params.get(
                    "interval_minutes",
                    params.get("bar_interval_minutes", self.cooldown_bar_interval_minutes),
                )
            )
        # 60%: max_position_pct(50%)/max_symbol_pct_total(60%)로 정상 산식이 만들 수
        # 있는 최대 주문보다 같거나 크게 잡아, "정상 캡을 한 번 더 좁히는 룰"이 아니라
        # "그 산식 자체가 고장났을 때만 걸리는 독립 재검증"으로 동작하게 한다.
        self.max_order_notional_pct = risk_cfg.get("max_order_notional_pct", 60) / 100
        # --- 포트폴리오 레벨 상한 (다종목) ---
        # max_position_pct는 종목마다 적용되므로 종목 수가 늘면 총노출이 그만큼
        # 곱해진다(신호 20개 x 50% = 1,000%). 아래 둘은 그것과 다른 축이다.
        # 0이면 비활성 — 1~2종목 구성의 기존 동작을 바꾸지 않기 위한 기본값.
        self.max_concurrent_positions = int(risk_cfg.get("max_concurrent_positions", 0))
        self.max_total_exposure_pct = risk_cfg.get("max_total_exposure_pct", 0) / 100
        # 레버리지 ETF(TQQQ/SOXL 등)가 서로 상관계수 0.9+라 종목 수 상한
        # (max_concurrent_positions)만으로는 분산이 안 된다 — 3배 상품 3개면 실질
        # 9배 단일 팩터 노출이다. leverage_of가 주입됐을 때만(아래 참고) 걸리는
        # 별도 레일. 0이면 비활성. 신규 진입만 차단, 청산은 항상 허용(기존 레일 원칙).
        self.max_leveraged_exposure_pct = risk_cfg.get("max_leveraged_exposure_pct", 50) / 100
        # 기본값은 기존 동작(capital_fraction) — 설정에 없으면 결과가 바뀌지 않는다.
        self.sizing_mode = str(risk_cfg.get("sizing_mode", "capital_fraction"))
        # 전략별 독립 명목계정(2026-08-19). 기본값 "shared" — 설정에 명시하지 않으면
        # 아래 capital_mode 분기는 전부 건너뛰고 기존 동작(계좌 전체 equity 기준)이
        # 100% 보존된다. "per_strategy"이고 books가 실제로 주입됐을 때만(둘 다 필요)
        # 새 경로를 탄다 — books.py 참고.
        self.capital_mode = str(risk_cfg.get("capital_mode", "shared"))
        self.books = books
        # {strategy_id: 스칼라} 또는 {strategy_id: {market: 비중}} 두 형태를 모두
        # 받는다(2026-08-12 시장별 배분 분리). 값 형태는 저장 시점엔 구분하지 않고
        # _capital_fraction_for()가 조회 시점에 판별한다 — 스칼라면 시장 무관하게
        # 그대로 쓰고(기존 동작 100% 보존), dict면 신호 심볼의 시장 키를 찾는다.
        self.capital_fraction = capital_fraction or {}
        self.market_of = market_of or {}
        # {symbol: 레버리지 배수(절대값)}. **None(기본값)과 빈 dict({})는 다르게
        # 동작한다** — None이면 이 기능 자체가 꺼진 것으로 취급해 헤어컷도, 노출
        # 레일도, 경고 로그도 전혀 발생하지 않는다(테스트/백테스트가 leverage_of를
        # 넘기지 않으므로 기존 결과가 100% 보존된다). {}나 부분적으로 채워진
        # dict가 주입되면(assembly.py가 부팅 시 stock_info로 채운다) 기능이
        # 켜지고, 그 안에 없는 심볼은 "모르는 것"으로 보수적으로 처리한다
        # (헤어컷 없음 + 최초 1회 경고 — _leverage_haircut 참고).
        self.leverage_of = leverage_of
        self._warned_unknown_leverage: set[str] = set()
        self.fx = fx or FixedFxProvider()
        self.last_block: str = ""
        self._day: str | None = None
        self._day_start_equity: float | None = None
        self._last_day_pnl_pct: float | None = None
        # per_strategy 모드 전용 일일 상태 — capital_mode가 shared면 절대 읽지도
        # 쓰지도 않는다(아래 approve()의 `per_strategy` 분기 안에서만 참조).
        # 전략별로 "그 전략의 첫 승인 호출 시점" equity를 그날의 시작자산으로 쓴다
        # (계좌 전체의 self._day_start_equity와 같은 방식 — 자정 정각 스냅샷이
        # 아니라 그날 첫 판단 시점 스냅샷).
        self._day_per_strategy: dict[str, str] = {}
        self._day_start_equity_per_strategy: dict[str, float] = {}
        self._last_day_pnl_pct_per_strategy: dict[str, float] = {}
        # {"strategy_id:market": count} — max_orders_per_day를 전략별로 분리해서
        # 센다. shared 모드의 self._day_entry_count(시장 키만)와는 별개 dict라
        # shared 모드 카운팅에 전혀 영향을 주지 않는다.
        self._day_entry_count_per_strategy: dict[str, int] = {}
        # **시장별로 나눈다.** 하나였을 때 밤사이 US 세션이 KR 아침 예산을 먹었다
        # (2026-08-14 실측: KR 개장 전에 이미 "135건/상한 30건 — 한도 도달").
        # 거래일 경계(KST 08:00)는 시장별로 이미 옳다 — US 세션(22:30~06:00 KST)도
        # KR 세션(09:00~15:30)도 한 경계 안에 통째로 들어간다. 문제는 지갑이 하나였던 것.
        #
        # 두 숫자를 나눈 이유: 상한은 **진입만** 막는데 카운터가 청산도 올리면 상한이
        # 스스로를 소진한다(체결 안 되는 청산이 매 사이클 재승인 → 그날 9,047 사이클).
        # 그렇다고 청산을 안 셀 수는 없다 — 총 주문 수는 폭주 가시성으로 의도된
        # 설계다(tests/test_risk_circuit_breakers.py 상단). 그래서 세되, 막지 않는다.
        # {(symbol, strategy_id): [진입 승인 시각]} — 폭주 탐지용 짧은 창.
        # 영속화하지 않는다: 재시작으로 비워져도 폭주라면 수십 초 안에 다시 채워진다.
        self._recent_entries: dict[tuple[str, str], list] = {}
        self._day_entry_count: dict[str, int] = {}   # 진입 예산 — 이것만 상한을 건다
        self._day_order_count: dict[str, int] = {}   # 총 승인 주문 — 가시성 전용
        self._stop_bar_ts: dict[str, object] = {}  # symbol -> 마지막 손절 청산 시점 봉 ts
        self._stop_day: dict[str, str] = {}  # symbol -> 그 손절이 난 거래일(세션 롤 시 쿨다운 해제)
        # 일일 리스크 상태 영속화 경로. 없으면(백테스트/단위테스트) 저장하지 않는다.
        #
        # 왜 필요한가: 이 상태가 전부 인메모리라 **재시작 한 번에 회로차단기가 전부
        # 풀렸다** — 손실 한도가 현재 자산 기준으로 재설정(이미 -2.9%여도 0%부터 다시),
        # 주문 상한 리셋, 손절 쿨다운 전부 해제. 그리고 재시작은 "나쁜 날"에 더 자주
        # 일어난다(연속 사이클 실패 → 자동 halt → 운영자 재시작, 배포, systemd Restart).
        # 즉 레일이 가장 필요한 순간에 정확히 풀렸다 (2026-08-12 감사 A-3).
        self.state_path = Path(state_path) if state_path else None
        self._load_day_state()

    def _load_day_state(self) -> None:
        """디스크의 일일 리스크 상태를 복원한다. 없거나 깨졌으면 조용히 기본값으로 —
        상태 복원 실패가 거래를 막으면 안 된다(그건 더 나쁜 실패다)."""
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            d = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            logger.warning("일일 리스크 상태 복원 실패 — 기본값으로 시작: %s", e)
            return
        self._day = d.get("day")
        self._day_start_equity = d.get("day_start_equity")
        # 구버전은 스칼라 하나였다. 어느 시장 것인지 알 수 없으므로 **버린다** —
        # 하루치 상한이라 다음 롤에서 정상화되고, 복원 실패가 기동을 막는 건 더 나쁜
        # 실패다(이 메서드의 기존 계약).
        raw_orders = d.get("day_order_count")
        raw_entries = d.get("day_entry_count")
        self._day_order_count = {str(k): int(v) for k, v in raw_orders.items()} \
            if isinstance(raw_orders, dict) else {}
        self._day_entry_count = {str(k): int(v) for k, v in raw_entries.items()} \
            if isinstance(raw_entries, dict) else {}
        if raw_orders is not None and not isinstance(raw_orders, dict):
            logger.warning(
                "구버전 일일 주문 카운트(시장 구분 없음, %r)를 버린다 — 시장별로 다시 센다",
                raw_orders,
            )
        # 쿨다운 판정은 `ts > stop_ts`(pandas Timestamp 비교)라 문자열로 복원하면
        # TypeError가 난다 — 반드시 Timestamp로 되돌린다. 파싱 실패한 항목은
        # **버리지 않고 남기지도 않는다**: 쿨다운이 조용히 풀리는 쪽(위험)보다
        # 그 종목만 복원 실패로 드러나는 편이 낫지만, 여기서 예외를 던지면 기동이
        # 막히므로 해당 심볼만 제외하고 경고를 남긴다.
        import pandas as pd

        restored: dict[str, object] = {}
        for sym, raw in (d.get("stop_bar_ts") or {}).items():
            try:
                restored[sym] = pd.Timestamp(raw)
            except (ValueError, TypeError):
                logger.warning("손절 쿨다운 복원 실패 — %s 쿨다운이 해제됨: %r", sym, raw)
        self._stop_bar_ts = restored
        self._stop_day = dict(d.get("stop_day") or {})
        # per_strategy 일일 상태 — 구버전 상태 파일에는 이 키들이 없으므로
        # `or {}` 로 안전하게 빈 값에서 시작한다(다른 필드들과 같은 폴백 원칙).
        raw_day_per_strategy = d.get("day_per_strategy")
        self._day_per_strategy = {str(k): str(v) for k, v in raw_day_per_strategy.items()} \
            if isinstance(raw_day_per_strategy, dict) else {}
        raw_start_equity_ps = d.get("day_start_equity_per_strategy")
        self._day_start_equity_per_strategy = {str(k): float(v) for k, v in raw_start_equity_ps.items()} \
            if isinstance(raw_start_equity_ps, dict) else {}
        raw_entry_count_ps = d.get("day_entry_count_per_strategy")
        self._day_entry_count_per_strategy = {str(k): int(v) for k, v in raw_entry_count_ps.items()} \
            if isinstance(raw_entry_count_ps, dict) else {}
        logger.info(
            "일일 리스크 상태 복원: 거래일=%s 주문 %d건 시작자산=%s 쿨다운 %d종목",
            self._day, sum(self._day_order_count.values()), self._day_start_equity,
            len(self._stop_bar_ts),
        )

    def _save_day_state(self) -> None:
        """원자적 tmp-replace (TradingControl과 같은 패턴). 쓰기 실패는 경고만 —
        영속화 실패가 주문 경로를 죽이면 안 된다."""
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "day": self._day,
                "day_start_equity": self._day_start_equity,
                "day_order_count": self._day_order_count,
                "day_entry_count": self._day_entry_count,
                # 봉 ts는 JSON 직렬화가 안 되는 타입일 수 있어 문자열로 남긴다.
                # 쿨다운 판정은 "같은 봉인가"만 보므로 문자열 비교로 충분하다.
                "stop_bar_ts": {k: str(v) for k, v in self._stop_bar_ts.items()},
                "stop_day": self._stop_day,
                "day_per_strategy": self._day_per_strategy,
                "day_start_equity_per_strategy": self._day_start_equity_per_strategy,
                "day_entry_count_per_strategy": self._day_entry_count_per_strategy,
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except Exception as e:  # noqa: BLE001
            logger.warning("일일 리스크 상태 저장 실패(거래는 계속): %s", e)

    def _block(self, why: str) -> None:
        self.last_block = why

    def _in_extended_session(self, strategy_id: str, market: str, ctx) -> bool:
        """정규장 밖이지만 허용 목록의 (전략, 시장, 시각 창) 안인가.

        **그 시장의 현지 시각**(`_MARKET_TZ`) 평일 + 창(분 단위, [시작, 끝) 반개구간)
        만 본다 — KST 로 고정 판정하면 US 프리마켓 창이 DST 전환기에 밀린다(모듈
        상단 `_MARKET_TZ` 주석 참고). 휴장일(공휴일) 판정은 하지 않는다: 프리마켓
        주문이 공휴일에 나가도 paper 는 무해하고, 실계좌 전환 시점에 거래소 달력
        게이트를 추가하는 게 이 자리의 TODO 다(지금 넣으면 검증 없는 달력 의존이
        하나 늘 뿐이다). 창 형식 오류·시계 부재는 전부 False — 모르면 막는 쪽이
        안전측이다.
        """
        windows = self._extended_sessions.get(strategy_id, {}).get(market)
        if not windows:
            return False
        now_fn = getattr(ctx.clock, "now", None)
        if not callable(now_fn):
            return False
        tz = _MARKET_TZ.get(market, _KST)
        try:
            now = now_fn()
            local = now.astimezone(tz) if now.tzinfo else now
        except Exception:  # noqa: BLE001 — 시계 이상은 막는 쪽으로
            return False
        if local.weekday() >= 5:
            return False
        minute = local.hour * 60 + local.minute
        return any(a <= minute < b for a, b in windows)

    def breaker_state(self, now=None) -> dict:
        """엔진/하트비트가 읽는 회로차단기 상태 스냅샷 — print 전용이 아니라 구조화된 값으로
        노출한다. approve() 호출 시점 기준 값이라 마지막 approve 이후로는 갱신되지 않는다.
        symbols_in_cooldown은 다음 approve() 호출에서 쿨다운이 실제로 풀린 뒤에야 정리된다.

        `now` 를 주면 **거래일이 이미 바뀐 경우 카운트를 0으로 보여준다.** 롤오버는
        approve() 안에서만 일어나므로, 오늘 아직 신호가 없으면 어제 숫자가 그대로
        남아 있다 — 하트비트가 그걸 "오늘 주문 135건 — 한도 도달"로 표시해 사용자가
        진입이 막힌 줄 알았다(2026-08-14). 실제로는 첫 approve() 에서 리셋된다.
        표시만 바로잡는다 — approve() 의 롤오버 의미는 건드리지 않는다."""
        rolled = now is not None and self._day is not None and \
            trading_day(now).isoformat() != self._day
        entry_count = {} if rolled else self._day_entry_count
        order_count = {} if rolled else self._day_order_count
        return {
            "day": trading_day(now).isoformat() if rolled else self._day,
            "max_orders_per_day": {
                # count/tripped 는 하위 호환 — 하트비트가 이 키를 읽는다. 상한은
                # **시장별**이므로 tripped 는 "어느 한 시장이라도 소진됐나"다.
                "count": sum(order_count.values()),
                "limit": self.max_orders_per_day,
                "tripped": bool(self.max_orders_per_day) and any(
                    n >= self.max_orders_per_day for n in entry_count.values()),
                # entries 가 예산을 쓰는 쪽, orders 는 폭주 가시성(청산 포함).
                # 둘이 크게 벌어지면 체결되지 않는 주문이 반복 승인되고 있다는 뜻이다
                # (2026-08-14: 체결 29건인데 카운터 135건).
                "by_market": {
                    m: {
                        "entries": entry_count.get(m, 0),
                        "orders": order_count.get(m, 0),
                        "tripped": bool(self.max_orders_per_day)
                        and entry_count.get(m, 0) >= self.max_orders_per_day,
                    }
                    for m in sorted(set(order_count) | set(entry_count))
                },
            },
            "daily_loss_limit_pct": {
                "limit_pct": self.daily_loss_limit_pct * 100,
                # 금일 손익 금액 계산용(KRW) — 리포트가 최신 MTM 자산과 대조한다
                "day_start_equity": self._day_start_equity,
                "day_pnl_pct": None if self._last_day_pnl_pct is None else self._last_day_pnl_pct * 100,
                "tripped": (
                    self._last_day_pnl_pct is not None
                    and self._last_day_pnl_pct <= -self.daily_loss_limit_pct
                ),
            },
            "cooldown_bars_after_stop": {
                "limit_bars": self.cooldown_bars_after_stop,
                "symbols_in_cooldown": sorted(self._stop_bar_ts.keys()),
            },
            "portfolio_caps": {
                "max_concurrent_positions": self.max_concurrent_positions,
                "max_total_exposure_pct": self.max_total_exposure_pct * 100,
            },
            "sizing_mode": self.sizing_mode,
        }

    def _capital_fraction_for(self, strategy_id: str, market: str) -> float:
        """전략의 시장별 자본배분 비중(capital_fraction 모드의 사이징 기준이자,
        두 사이징 모드 공통의 "이 전략이 이 시장에서 거래 가능한가" 게이트).

        값이 dict면(시장별 배분) 그 시장 키를 찾는다 — 없으면 0.0(명시하지 않은
        시장엔 자본을 주지 않는다, "모르면 안전한 쪽"). 값이 스칼라면 시장과
        무관하게 그대로 쓴다 — 이게 기존 동작이고, dict 형태를 쓰지 않는 설정은
        이 브랜치만 타므로 결과가 100% 보존된다."""
        v = self.capital_fraction.get(strategy_id, 0.0)
        if isinstance(v, dict):
            return float(v.get(market, 0.0))
        return float(v)

    def _leverage_haircut(self, symbol: str) -> float:
        """capital_fraction/cash_pct 사이징 예산에 곱할 레버리지 헤어컷(1/|lev|).

        3배 ETF에 1배 ETF와 같은 명목을 배분하면 기초자산 기준 리스크가 3배가
        된다 — risk 기반 사이징(손절폭 기준 수량 산정)이었다면 ATR이 3배 넓어진
        만큼 수량이 자동으로 1/3로 줄어 자기교정됐겠지만, capital_fraction/
        cash_pct는 명목 기준이라 그 자기교정이 없다. 여기서 명목을 미리 줄여
        기초자산 노출을 1배 상품과 맞춘다.

        `self.leverage_of`가 None이면(주입 자체가 안 됨 — 테스트/백테스트/구성
        누락) 기능이 꺼진 것으로 취급해 헤어컷 없이 1.0을 반환한다. 그 외에는
        모르는 심볼도 헤어컷 없이 1.0이지만, 3배 상품에 조용히 1배 명목이
        들어가는 사고를 막기 위해 심볼당 최초 1회 WARNING을 남긴다."""
        if self.leverage_of is None:
            return 1.0
        lev = self.leverage_of.get(symbol)
        if lev is None:
            if symbol not in self._warned_unknown_leverage:
                self._warned_unknown_leverage.add(symbol)
                logger.warning(
                    "레버리지 배수 미상: %s — 헤어컷 없이 사이징(1배 명목 가정). "
                    "실제로 레버리지 상품이면 기초자산 대비 노출이 과대해진다", symbol,
                )
            return 1.0
        try:
            lev = abs(float(lev))
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(lev) or lev <= 0:
            return 1.0
        return 1.0 / lev

    def _bar_ts(self, symbol: str, ctx: Context, n: int = 1, strategy_id: str = ""):
        """symbol의 최근 완성봉 n개를 **그 전략이 쓰는 봉 간격**으로 조회.
        DataFeed.history()가 완성봉만 반환한다는 계약(interfaces.py)에 그대로 얹는다."""
        minutes = self._strategy_bar_minutes.get(strategy_id, self.cooldown_bar_interval_minutes)
        return ctx.data.history(symbol, f"{minutes}m", n)

    def _approve_entry_per_strategy(
        self,
        signal: Signal,
        ctx: Context,
        price: float,
        market: str,
        risk_multiplier: float,
        marks: dict[str, float] | None,
        strategy_equity: float,
    ) -> float | None:
        """per_strategy 모드의 신규 진입 사이징(2026-08-19). 계좌 전체
        `ctx.broker.positions()`/`equity` 대신 그 전략의 books 장부
        (`self.books`)만 본다 — 다른 전략의 포지션·현금은 이 계산에 전혀
        섞이지 않는다.

        **capital_fraction의 "값"은 여기서 참조하지 않는다** — 이 메서드가
        불리기 전에 호출부(`approve()`)가 이미 `_capital_fraction_for(...) <= 0`
        여부(시장 허용 boolean)만 확인해 걸러냈다. 값의 크기(0.05 vs 0.3)는
        전략이 이미 독립된 1,000만원을 가지므로 그 위에 다시 곱하면 이중
        분할이 된다 — 그래서 사이징에는 쓰지 않는다. 하지만 "이 전략은 이
        시장에서 아예 거래하지 않는다"는 의미(0 이하 선언)는 그대로 유지해야
        한다 — donchian처럼 심볼이 고정된 전략은 심볼 구성 자체가 다른 시장
        진입을 막지만, `universe: watchlist`로 관심종목에서 심볼을 받는
        전략(news_momentum/news_scalp/frgn_accumulate 등)은 그 보장이 없다
        (관심종목에 US 심볼이 섞여 있으면 KR 전용으로 설계된 전략이 US 신호를
        낼 수 있다). 그래서 boolean 차단은 approve()에서 계속 걸고, 이 메서드는
        "그 시장에서 거래가 허용된 이후의 크기"만 책임진다.

        반환값이 None이면 `self.last_block`에 사유가 이미 채워져 있다(호출부는
        그대로 `return None`하면 된다) — 0보다 큰 정수 수량이면 그대로 매수
        수량으로 쓴다(최종 sanity 가드는 approve()가 공통으로 한 번 더 본다).
        """
        strategy_id = signal.strategy_id
        book = self.books.books.get(strategy_id, {}) if self.books else {}
        book_positions: dict = book.get("positions", {}) or {}
        cash = self.books.available_cash_krw(strategy_id) if self.books else 0.0

        def _book_market(sym: str) -> str:
            p = book_positions.get(sym) or {}
            return p.get("market") or (self.market_of.get(sym) or market_of_symbol(sym))

        open_symbols = {sym for sym, p in book_positions.items() if float(p.get("qty", 0.0)) > 0}
        if (
            self.max_concurrent_positions
            and signal.symbol not in open_symbols
            and len(open_symbols) >= self.max_concurrent_positions
        ):
            self._block(
                f"[{strategy_id}] 동시 보유 종목 수 상한 "
                f"({len(open_symbols)}/{self.max_concurrent_positions}) — {signal.symbol} 신규 진입 차단"
            )
            return None

        existing_qty = float(book_positions.get(signal.symbol, {}).get("qty", 0.0))
        # target_qty(고정 수량 지정, 2026-08-19 frgn_accumulate 고정액→고정수량
        # 전환) — 있으면 target_weight/risk_multiplier로 근사하지 않고 지정 수량
        # 그대로를 예산(KRW)으로 환산해 요청한다. 아래 room/현금/노출 하드레일은
        # 그대로 적용되므로 실제 체결 수량은 target_qty보다 줄 수 있다(우회 아님).
        requested_krw = (
            to_krw(signal.target_qty * price, market, self.fx)
            if signal.target_qty is not None else None
        )
        if self.sizing_mode == "cash_pct":
            budget = (
                requested_krw if requested_krw is not None
                else max(cash, 0.0) * signal.target_weight * risk_multiplier
            )
        else:
            used = to_krw(existing_qty * price, market, self.fx)
            room = max(self.max_position_pct * strategy_equity - used, 0.0)
            budget = min(
                requested_krw if requested_krw is not None
                else signal.target_weight * strategy_equity * risk_multiplier,
                room,
            )

        budget = budget * self._leverage_haircut(signal.symbol)

        if self.max_total_exposure_pct:
            exposure = sum(
                to_krw(
                    float(p.get("qty", 0.0)) * (price if sym == signal.symbol else float(p.get("avg_cost", 0.0))),
                    _book_market(sym), self.fx,
                )
                for sym, p in book_positions.items()
                if float(p.get("qty", 0.0)) > 0
            )
            room_portfolio = self.max_total_exposure_pct * strategy_equity - exposure
            if room_portfolio <= 0:
                self._block(
                    f"[{strategy_id}] 총노출 상한 도달 (노출 {exposure:,.0f}원 >= "
                    f"{self.max_total_exposure_pct*100:.0f}%×전략자산 {strategy_equity:,.0f}원) — 신규 진입 차단"
                )
                return None
            budget = min(budget, room_portfolio)

        if self.max_symbol_pct_total:
            room_total = self.max_symbol_pct_total * strategy_equity - to_krw(existing_qty * price, market, self.fx)
            budget = min(budget, max(room_total, 0.0))

        # 전략별 현금 게이트 — 마이너스 현금 방지의 핵심. equity 기준 budget/room이
        # 이미 대부분 이 안에 들어오지만, equity에는 보유 포지션 평가액도 섞여
        # 있어 "room은 있는데 실제 현금이 없는" 경우가 생길 수 있다 — 그래서
        # 최종적으로 한 번 더 가용 현금으로 자른다.
        budget = min(budget, max(cash, 0.0))

        qty = from_krw(budget, market, self.fx) / price
        if not math.isfinite(qty):
            self._block(
                f"[{strategy_id}] 수량 계산 이상 — 주문 생성 거부: qty={qty!r} price={price!r} "
                f"전략자산={strategy_equity!r} symbol={signal.symbol}"
            )
            return None
        if signal.target_qty is not None:
            # KRW 왕복 환산의 부동소수점 오차로 하드레일에 걸리지 않았는데도
            # target_qty보다 1주 밑돌 수 있다 — 작은 허용오차를 더하되,
            # target_qty 자체를 넘어서게 반올림되지 않도록 다시 상한을 씌운다.
            qty = min(math.floor(qty + 1e-6), signal.target_qty)
        else:
            qty = math.floor(qty)
        if qty <= 0:
            self._block(f"[{strategy_id}] 배분 자금 부족 (예산 {budget:,.0f}원, 가용현금 {cash:,.0f}원)")
            return None

        notional_krw = to_krw(qty * price, market, self.fx)
        if self.min_order_notional_krw and notional_krw < self.min_order_notional_krw:
            self._block(
                f"[{strategy_id}] 최소 주문금액 미달: {notional_krw:,.0f}원 < "
                f"{self.min_order_notional_krw:,.0f}원 (수량 {qty:g}) — 주문 생략"
            )
            return None

        cap_krw = self.max_order_notional_pct * strategy_equity
        if self.max_order_notional_pct and notional_krw > cap_krw:
            self._block(
                f"[{strategy_id}] 단일 주문 규모 상한 초과: 주문금액 {notional_krw:,.0f}원 > "
                f"상한 {self.max_order_notional_pct*100:.0f}%×전략자산({strategy_equity:,.0f}원)={cap_krw:,.0f}원"
            )
            return None

        # 전략별 현금 게이트 최종 확인(부동소수점 여유 1e-6) — 위 budget 캡이 정상
        # 경로에서는 이미 걸러내지만, 명확한 사유를 남기기 위한 마지막 방어선이다.
        if notional_krw > cash + 1e-6:
            self._block(
                f"[{strategy_id}] 전략별 가용현금 부족: 주문금액 {notional_krw:,.0f}원 > "
                f"가용현금 {cash:,.0f}원 — 신규 진입 차단"
            )
            return None

        if self.max_leveraged_exposure_pct and self.leverage_of is not None:
            this_lev = abs(self.leverage_of.get(signal.symbol) or 1.0)
            if this_lev > 1.0:
                current_lev_exposure = sum(
                    to_krw(
                        float(p.get("qty", 0.0))
                        * (price if sym == signal.symbol else float(p.get("avg_cost", 0.0))),
                        _book_market(sym), self.fx,
                    ) * abs(self.leverage_of.get(sym) or 1.0)
                    for sym, p in book_positions.items()
                    if float(p.get("qty", 0.0)) > 0 and abs(self.leverage_of.get(sym) or 1.0) > 1.0
                )
                added_lev_exposure = notional_krw * this_lev
                cap_lev_krw = self.max_leveraged_exposure_pct * strategy_equity
                if current_lev_exposure + added_lev_exposure > cap_lev_krw:
                    self._block(
                        f"[{strategy_id}] 레버리지 총노출 상한 초과: 현재 {current_lev_exposure:,.0f}원 + "
                        f"이번 주문 {added_lev_exposure:,.0f}원(명목 {notional_krw:,.0f}원×{this_lev:g}배) > "
                        f"상한 {self.max_leveraged_exposure_pct*100:.0f}%×전략자산({strategy_equity:,.0f}원)"
                        f"={cap_lev_krw:,.0f}원 — 신규 진입 차단"
                    )
                    return None

        return qty

    def approve(
        self,
        signal: Signal,
        ctx: Context,
        risk_multiplier: float = 1.0,
        marks: dict[str, float] | None = None,
    ) -> Order | None:
        """risk_multiplier는 국면(regime) 배수 — **두 사이징 모드 모두** 요청 예산에
        곱는다(cash_pct: 가용현금×비중×배수, capital_fraction: 전략자본×비중×배수).
        하드레일(max_position_pct의 room, 총노출/팻핑거/일손실 한도)에는 절대 곱하지
        않는다 — 한도는 최악의 날 기준 고정이다(ADR-0009 통합 결정). 기본 1.0(중립),
        비정상 값(NaN/음수)은 1.0으로 되돌린다.

        marks는 신호 종목이 아닌 다른 보유 종목의 현재가(엔진 루프가 사이클마다
        캐시 시세로 채운다) — daily_loss_limit_pct가 보는 자산(equity)을 실제
        평가금액(MTM)에 가깝게 만드는 데 쓴다. 종목별로 값이 없거나 무효(NaN/inf/
        0 이하)면 그 종목만 평균단가로 저하(degrade)한다 — 절대 예외를 내거나
        승인을 막지 않는다. marks 자체가 None이면(백테스트 등 기존 호출부) 이전과
        동일하게 신호 종목만 현재가, 나머지는 전부 평균단가로 근사한다.

        장이 닫혀 있으면 진입/청산 신호 모두 여기서 막는다(MARKET_CLOSED_MARKER) —
        리스크 판단이 아니라 물리적 제약이다(모듈 docstring 참고). ctx.clock에
        is_market_open이 없는 테스트 페이크는 게이트를 건너뛴다(기존 계약 보존)."""
        self.last_block = ""
        if not math.isfinite(risk_multiplier) or risk_multiplier < 0:
            risk_multiplier = 1.0
        quote = ctx.data.quote(signal.symbol)
        if quote is None or not math.isfinite(quote.price) or quote.price <= 0:
            self._block("현재가 없음")
            return None
        price = quote.price
        # dict는 힌트일 뿐 — 없으면 "US"로 떨어뜨리지 않고 심볼에서 계산한다.
        # market_of는 부팅 시점 스냅샷이라 장중에 편입된 관심종목이 빠져 있다.
        market = self.market_of.get(signal.symbol) or market_of_symbol(signal.symbol)

        is_market_open = getattr(ctx.clock, "is_market_open", None)
        if callable(is_market_open) and not is_market_open(market):
            # 확장 세션 허용 목록(생성자 주석) — 명시된 (전략, 시장, 창)만 통과.
            if not self._in_extended_session(signal.strategy_id, market, ctx):
                self._block(f"{MARKET_CLOSED_MARKER} — 주문 불가 ({market})")
                return None

        positions = ctx.broker.positions()
        available_cash = ctx.broker.cash()

        def _mark(sym: str, pos: Position) -> float:
            if sym == signal.symbol:
                return price
            m = (marks or {}).get(sym)
            if m is not None and math.isfinite(m) and m > 0:
                return m
            return pos.avg_cost

        equity = available_cash + sum(
            to_krw(
                p.qty * _mark(sym, p),
                self.market_of.get(sym) or market_of_symbol(sym),
                self.fx,
            )
            for sym, p in positions.items()
            if p.qty > 0
        )
        if not math.isfinite(equity) or equity <= 0:
            self._block("자산 0 이하")
            return None

        # per_strategy 모드: 계좌 전체가 아니라 **그 전략의 books 장부**가 자산
        # 기준이다(설계는 quant/trade/risk/books.py 참고). books가 실제로 주입되지
        # 않았으면(assembly가 shared 모드에서 넘기지 않음) capital_mode 설정값과
        # 무관하게 조용히 shared로 강등한다 — "모드는 켰는데 books가 없어서 크래시"
        # 보다 "표시만 shared처럼 동작"이 안전측이다.
        per_strategy = self.capital_mode == "per_strategy" and self.books is not None
        strategy_equity: float | None = None
        if per_strategy:
            book_marks = dict(marks or {})
            book_marks[signal.symbol] = price
            strategy_equity = self.books.equity_krw(signal.strategy_id, book_marks, self.fx)
            if not math.isfinite(strategy_equity) or strategy_equity <= 0:
                self._block(f"[{signal.strategy_id}] 전략 자산 0 이하")
                return None

        # 달력 자정이 아니라 **거래일** 경계(KST 08:00)로 끊는다 — 자정으로 끊으면
        # US 세션 도중에 손실 한도·주문 상한이 리셋돼 하루 한도가 두 배가 된다.
        today = trading_day(ctx.clock.now()).isoformat()
        if today != self._day:
            self._day = today
            self._day_start_equity = equity
            # 두 시장이 같은 경계에서 함께 롤한다. 주말·공휴일에 안 여는 시장은 그날
            # 주문이 0건이라 카운터가 0인 채 롤할 뿐이다 — 그래서 휴장 달력이 따로
            # 필요하지 않다(있으면 쓰지 않는 코드가 된다).
            self._day_entry_count = {}
            self._day_order_count = {}
            self._save_day_state()

        if per_strategy:
            # 전략별 거래일 롤 — 계좌 전체와 같은 경계(today)를 쓰되, "그 전략의
            # 첫 호출" 시점에만 그 전략의 시작자산을 스냅샷한다(전략마다 첫 신호가
            # 나오는 시각이 다르므로 계좌 전체 롤과 분리해서 관리해야 한다).
            if self._day_per_strategy.get(signal.strategy_id) != today:
                self._day_per_strategy[signal.strategy_id] = today
                self._day_start_equity_per_strategy[signal.strategy_id] = strategy_equity
                prefix = f"{signal.strategy_id}:"
                self._day_entry_count_per_strategy = {
                    k: v for k, v in self._day_entry_count_per_strategy.items()
                    if not k.startswith(prefix)
                }
                self._save_day_state()
            day_start = self._day_start_equity_per_strategy.get(signal.strategy_id)
            if day_start:
                day_pnl_pct = (strategy_equity - day_start) / day_start
                self._last_day_pnl_pct_per_strategy[signal.strategy_id] = day_pnl_pct
                if day_pnl_pct <= -self.daily_loss_limit_pct and signal.action in _ENTRY_ACTIONS:
                    self._block(
                        f"[{signal.strategy_id}] 일일 손실 한도 도달 ({day_pnl_pct*100:.1f}% <= "
                        f"-{self.daily_loss_limit_pct*100:.0f}%) — 신규 진입 차단"
                    )
                    return None
        elif self._day_start_equity:
            day_pnl_pct = (equity - self._day_start_equity) / self._day_start_equity
            self._last_day_pnl_pct = day_pnl_pct
            if day_pnl_pct <= -self.daily_loss_limit_pct and signal.action in _ENTRY_ACTIONS:
                self._block(
                    f"일일 손실 한도 도달 ({day_pnl_pct*100:.1f}% <= "
                    f"-{self.daily_loss_limit_pct*100:.0f}%) — 신규 진입 차단"
                )
                return None

        if signal.action in _ENTRY_ACTIONS and self.max_repeat_entries_per_window:
            key = (signal.symbol, signal.strategy_id)
            now_dt = ctx.clock.now()
            cutoff = now_dt - timedelta(minutes=self.repeat_entry_window_minutes)
            recent = [t for t in self._recent_entries.get(key, []) if t > cutoff]
            self._recent_entries[key] = recent
            if len(recent) >= self.max_repeat_entries_per_window:
                self._block(
                    f"반복 진입 차단 — {signal.symbol}/{signal.strategy_id} 가 "
                    f"{self.repeat_entry_window_minutes:g}분 안에 {len(recent)}회 진입 "
                    f"(폭주 의심). 청산은 계속 허용된다."
                )
                return None

        if signal.action in _ENTRY_ACTIONS and self.max_orders_per_day:
            # per_strategy 모드는 "strategy_id:market" 키의 별도 dict로 센다
            # (self._day_entry_count_per_strategy) — shared 모드의 시장별 전역
            # 카운터(self._day_entry_count)는 건드리지 않는다.
            if per_strategy:
                count_key = f"{signal.strategy_id}:{market}"
                current = self._day_entry_count_per_strategy.get(count_key, 0)
            else:
                count_key = market
                current = self._day_entry_count.get(market, 0)
            if current >= self.max_orders_per_day:
                self._block(
                    f"{market} 일일 진입 상한 도달 ({current}/{self.max_orders_per_day}건) — "
                    f"{market} 신규 진입 차단" + (f" [{signal.strategy_id}]" if per_strategy else "")
                )
                return None

        if signal.action in _ENTRY_ACTIONS and self.cooldown_bars_after_stop > 0:
            stop_ts = self._stop_bar_ts.get(signal.symbol)
            # 쿨다운은 **세션 안에서만** 유효하다. 이 레일이 막으려는 것은 손절 직후
            # 같은 신호가 곧바로 재발동하는 휩소 재진입이고, 그건 장중 현상이다.
            # 세션을 넘겨 유지하면 장 마감 직전 손절이 다음 날 진입을 통째로
            # 막아버린다 — 야간에는 봉이 생기지 않아 "N봉 경과"가 영영 안 채워지기
            # 때문이다(5분봉 실측: 15:50 손절 → 다음 날 09:35 진입이 "3/4봉 경과"로
            # 차단). 하루 1회 진입 전략에서는 그 자체로 전략을 반쯤 꺼버린다.
            if stop_ts is not None and self._stop_day.get(signal.symbol) != today:
                del self._stop_bar_ts[signal.symbol]
                self._stop_day.pop(signal.symbol, None)
                stop_ts = None
            if stop_ts is not None:
                bars = self._bar_ts(
                    signal.symbol, ctx, self.cooldown_bars_after_stop + 5, signal.strategy_id,
                )
                elapsed = sum(1 for ts in bars.index if ts > stop_ts)
                if elapsed < self.cooldown_bars_after_stop:
                    self._block(
                        f"손절 쿨다운: {signal.symbol} 손절 후 {elapsed}/{self.cooldown_bars_after_stop}"
                        f"봉 경과 — 신규 진입 차단"
                    )
                    return None
                del self._stop_bar_ts[signal.symbol]  # 쿨다운 종료 — 상태 정리
                self._stop_day.pop(signal.symbol, None)

        pos = positions.get(signal.symbol)
        existing_qty = pos.qty if pos is not None else 0.0
        # 브로커 보유에는 사용자가 손으로 산 물량이 섞여 있을 수 있다. 엔진 소유
        # 원장을 노출하는 브로커(TossBroker)라면 **매도 가능 수량은 그 원장이 상한**이다
        # — 이것을 빼먹으면 flatten/청산이 사용자의 장기 보유를 팔아치운다.
        # PaperBroker처럼 원장을 노출하지 않는 브로커는 보유 전부가 엔진 소유다.
        engine_owned_qty = getattr(ctx.broker, "engine_owned_qty", None)
        sellable_qty = existing_qty
        if callable(engine_owned_qty):
            sellable_qty = min(existing_qty, engine_owned_qty(signal.symbol))
        is_stop_loss_exit = False

        if signal.action in _EXIT_ACTIONS:
            if existing_qty <= 0:
                self._block(f"{signal.symbol} 보유 없음 — 매도 불가")
                return None
            if sellable_qty <= 0:
                self._block(
                    f"{signal.symbol} 엔진 소유분 없음 (브로커 보유 {existing_qty:g}는 "
                    f"사용자 수동 보유) — 매도 대상 아님"
                )
                return None
            existing_qty = sellable_qty
            # 청산 수량은 **그 전략의 lot**을 기준으로 한다 — 다른 전략이 같은
            # 심볼에 보유한 몫까지 팔면 안 된다(2026-08-11 사용자 지시: "전략마다
            # 구매한 만큼을 그대로 지키면서 매도·청산"). lot이 없으면(레거시/고아 —
            # 이 전략이 이 심볼에 lot을 추적한 적이 없음) 기존 동작(포지션 전체
            # 기준)으로 폴백한다 — **청산은 랏 도입 때문에 절대 막히거나 줄어들면
            # 안 된다**는 원칙이 lot 정밀도보다 우선한다.
            lot_qty = pos.lot_qty(signal.strategy_id) if pos is not None else 0.0
            if lot_qty > 0:
                existing_qty = min(existing_qty, lot_qty)
            qty = existing_qty * signal.exit_fraction
            if market == "KR" and signal.exit_fraction < 1:
                qty = math.floor(qty)
                if qty < 1:
                    self._block(f"부분매도 수량 <1주 (보유 {existing_qty:g} x {signal.exit_fraction:g})")
                    return None
            side = Side.SELL
            is_stop_loss_exit = signal.action is SignalAction.EXIT_LONG and _STOP_LOSS_MARKER in signal.reason
        else:
            if per_strategy:
                # capital_fraction은 두 가지 일을 겸해왔다: (a) 사이징 비중,
                # (b) "이 전략은 이 시장에서 아예 거래하지 않는다"는 차단(예:
                # donchian은 심볼이 TQQQ/SQQQ로 고정돼 있어 KR: 0.0이 죽은
                # 설정이지만, news_momentum/news_scalp/frgn_accumulate처럼
                # `universe: watchlist`로 심볼을 관심종목에서 받는 전략은 그렇지
                # 않다 — 관심종목에 US 심볼(예: AMZN/QQQ/SOXL)이 섞여 있으면
                # US: 0.0을 무시할 경우 그 전략이 실제로 미국 주식을 살 수 있게
                # 된다). per_strategy 모드는 (a)만 버린다 — 전략이 이미 독립된
                # 1,000만원을 가지므로 비중 크기(0.05 vs 0.3)는 사이징에 영향을
                # 주지 않되, (b)는 그대로 유지해 "이 시장에서 아예 안 판다"는
                # 원래의 시장 허용 의미를 지킨다. 값의 크기는 버리고 <=0 여부만
                # 본다 — 이중 분할(전략 자본 위에 비중을 또 곱하는 것) 방지.
                if self._capital_fraction_for(signal.strategy_id, market) <= 0:
                    self._block(f"{signal.strategy_id}는 {market} 시장 배분이 0 — 신규 진입 차단")
                    return None
                # 사이징은 capital_fraction 값을 참조하지 않는다(설계 결정, 아래
                # _approve_entry_per_strategy docstring 참고) — 계좌 전체
                # positions/equity 대신 그 전략의 books 장부를 쓴다.
                qty = self._approve_entry_per_strategy(
                    signal, ctx, price, market, risk_multiplier, marks, strategy_equity,
                )
                if qty is None:
                    return None
            else:
                # 시장별 자본배분 게이트 — **두 사이징 모드 공통**이다. cash_pct는 budget
                # 산식에 capital_fraction을 쓰지 않지만(가용현금 기준), "이 전략이 이
                # 시장에서 아예 거래 가능한가"라는 구조적 질문에는 두 모드가 같은 답을
                # 써야 한다(2026-08-12 시장별 배분 분리 — donchian은 KR 배분이 0이라
                # KR 세션에는 절대 진입하면 안 된다). 0이면 조용히 수량 0으로 떨어뜨리지
                # 않고 여기서 명확한 사유로 차단한다.
                split = self._capital_fraction_for(signal.strategy_id, market)
                if split <= 0:
                    self._block(f"{signal.strategy_id}는 {market} 시장 배분이 0 — 신규 진입 차단")
                    return None

                # --- 포트폴리오 레벨 상한 (다종목 전용) ---
                # max_position_pct는 **종목마다** 적용되는 값이다. 1~2종목 시절엔 그것이
                # 사실상 총노출 상한이었지만, 상위 100종목에서 신호를 받는 구성에서는
                # 신호 20개가 오면 자본의 20 x 50% = 1,000%를 배분하려 든다. 아래 두
                # 레일이 그것을 막는다 — 종목별 상한과는 다른 축이므로 둘 다 필요하다.
                open_symbols = {sym for sym, p in positions.items() if p.qty > 0}
                if (
                    self.max_concurrent_positions
                    and signal.symbol not in open_symbols
                    and len(open_symbols) >= self.max_concurrent_positions
                ):
                    self._block(
                        f"동시 보유 종목 수 상한 ({len(open_symbols)}/{self.max_concurrent_positions}) "
                        f"— {signal.symbol} 신규 진입 차단"
                    )
                    return None

                # target_qty(고정 수량 지정, 2026-08-19) — 있으면 target_weight로
                # 근사하지 않고 지정 수량 그대로를 예산(KRW)으로 요청한다. 아래
                # 하드레일은 그대로 적용된다(Signal.target_qty 문서 참고).
                requested_krw = (
                    to_krw(signal.target_qty * price, market, self.fx)
                    if signal.target_qty is not None else None
                )
                if self.sizing_mode == "cash_pct":
                    # 그날 실제 가용 현금 기준. max_position_pct(=전략자본 대비 잔여룸)는
                    # 여기선 쓰지 않는다 — "전략자본"이라는 고정 기준 자체를 대체하는
                    # 모드이기 때문이다. 종목별 상한은 아래 max_symbol_pct_total(총자산
                    # 대비)이, 총노출은 max_total_exposure_pct가 계속 담당한다.
                    budget = (
                        requested_krw if requested_krw is not None
                        else max(available_cash, 0.0) * signal.target_weight * risk_multiplier
                    )
                else:
                    strategy_capital = split * equity
                    used = to_krw(existing_qty * price, market, self.fx)
                    room = max(self.max_position_pct * strategy_capital - used, 0.0)
                    # 국면 배수는 **요청 예산**에만 곱한다. room(=max_position_pct 상한)에는
                    # 곱하지 않는다 — 공격 국면(>1.0)이라고 안전 한도까지 넓어지면 보호
                    # 장치가 국면 판단의 인질이 된다. 방어 국면에 작게 들어가는 것은
                    # 사이징의 몫이고, 한도는 최악의 날 기준으로 고정이다. target_qty에는
                    # 국면 배수를 곱하지 않는다 — 지정 수량은 비례 사이징이 아니다.
                    budget = min(
                        requested_krw if requested_krw is not None
                        else signal.target_weight * strategy_capital * risk_multiplier,
                        room,
                    )

                # 레버리지 헤어컷 — capital_fraction/cash_pct 두 모드 공통. cash_pct에도
                # 적용하는 이유: 기초자산 대비 과대노출은 예산의 산출 기준(현금 vs
                # 전략자본)과 무관하게 명목 자체에서 생기는 문제이기 때문이다. 이후의
                # 포트폴리오 상한(총노출/종목별 상한)은 헤어컷 적용된 budget 위에 그대로
                # 걸린다.
                budget = budget * self._leverage_haircut(signal.symbol)

                if self.max_total_exposure_pct:
                    # 보유 중인 다른 종목은 시세를 다시 조회하지 않고 평균단가로 근사한다
                    # (approve가 종목마다 네트워크를 치면 사이클 지연이 종목 수에 비례한다).
                    # 근사는 노출을 과소평가할 수 있으므로 상한 자체를 보수적으로 잡을 것.
                    exposure = sum(
                        to_krw(
                            p.qty * (price if sym == signal.symbol else p.avg_cost),
                            self.market_of.get(sym) or market_of_symbol(sym),
                            self.fx,
                        )
                        for sym, p in positions.items()
                        if p.qty > 0
                    )
                    room_portfolio = self.max_total_exposure_pct * equity - exposure
                    if room_portfolio <= 0:
                        self._block(
                            f"총노출 상한 도달 (노출 {exposure:,.0f}원 >= "
                            f"{self.max_total_exposure_pct*100:.0f}%×자산 {equity:,.0f}원) — 신규 진입 차단"
                        )
                        return None
                    budget = min(budget, room_portfolio)

                if self.max_symbol_pct_total:
                    agg_qty = sum(p.qty for sym, p in positions.items() if sym == signal.symbol)
                    room_total = self.max_symbol_pct_total * equity - to_krw(agg_qty * price, market, self.fx)
                    budget = min(budget, max(room_total, 0.0))

                qty = from_krw(budget, market, self.fx) / price
                # floor보다 **먼저** 유한성을 본다. math.floor(inf)는 OverflowError로
                # approve() 전체를 크래시시켜, 아래 최종 sanity 가드가 실행되기도 전에
                # 죽는다 (price가 극단적으로 작으면 qty가 inf가 된다).
                if not math.isfinite(qty):
                    self._block(
                        f"수량 계산 이상 — 주문 생성 거부: qty={qty!r} price={price!r} "
                        f"equity={equity!r} symbol={signal.symbol}"
                    )
                    return None
                # 매수 수량은 **전 시장 정수**다. Toss 스펙(docs/api/toss/QUICKREF.md:207):
                # "quantity must be a positive integer EXCEPT US MARKET+SELL".
                # 소수점 매수는 live에서 400으로 거부된다 — paper에서만 조용히 체결돼
                # 백테스트/페이퍼와 라이브가 다르게 행동한다(이 저장소가 이미 겪은 부류).
                # 소수점 US 매수가 정말 필요하면 수량이 아니라 orderAmount(금액 주문)로
                # 가야 하고, 그건 브로커 어댑터의 별도 경로다.
                if signal.target_qty is not None:
                    # KRW 왕복 환산의 부동소수점 오차 허용 + target_qty 초과 방지
                    # (per_strategy 경로와 동일한 이유, 위 _approve_entry_per_strategy 참고).
                    qty = min(math.floor(qty + 1e-6), signal.target_qty)
                else:
                    qty = math.floor(qty)
                if qty <= 0:
                    self._block(f"배분 자금 부족 (예산 {budget:,.0f})")
                    return None

                # 경제적으로 무의미한 주문 차단. 2026-08-11 실운영에서 058610을
                # 0.0015주(명목 181원) 매수해 −7원 손실이 스코어보드에 33,000원 손실과
                # 같은 1표로 집계됐다. 표본을 오염시키고 수수료만 낸다.
                # KR은 소수점 매매 자체가 불가하므로(docs/api/toss QUICKREF: quantity는
                # US MARKET+SELL 외 항상 양의 정수) 1주 미만은 주문이 될 수 없다.
                notional_krw_pre = to_krw(qty * price, market, self.fx)
                if self.min_order_notional_krw and notional_krw_pre < self.min_order_notional_krw:
                    self._block(
                        f"최소 주문금액 미달: {notional_krw_pre:,.0f}원 < "
                        f"{self.min_order_notional_krw:,.0f}원 (수량 {qty:g}) — 주문 생략"
                    )
                    return None

                notional_krw = to_krw(qty * price, market, self.fx)
                cap_krw = self.max_order_notional_pct * equity
                if self.max_order_notional_pct and notional_krw > cap_krw:
                    self._block(
                        f"단일 주문 규모 상한 초과: 주문금액 {notional_krw:,.0f}원 > "
                        f"상한 {self.max_order_notional_pct*100:.0f}%×자산({equity:,.0f}원)={cap_krw:,.0f}원"
                    )
                    return None

                # 레버리지 총노출 레일 — TQQQ/SOXL/SPXL처럼 상관계수 0.9+인 레버리지
                # 상품끼리는 종목 수 상한(max_concurrent_positions)이 분산 장치가 되지
                # 못한다(3배 상품 3종목 = 실질 9배 단일 팩터). leverage_of가 주입되지
                # 않았으면(None) 이 레일은 판정 불가이므로 완전히 건너뛴다 — 기존
                # 테스트/백테스트 결과를 바꾸지 않는다. 신규 진입이 **레버리지 상품
                # 자신**일 때만 건다(비레버리지 진입까지 과거 레버리지 노출 때문에
                # 막는 것은 이 레일의 취지가 아니다). 청산은 건드리지 않는다(이 분기
                # 자체가 진입 전용).
                if self.max_leveraged_exposure_pct and self.leverage_of is not None:
                    this_lev = abs(self.leverage_of.get(signal.symbol) or 1.0)
                    if this_lev > 1.0:
                        current_lev_exposure = sum(
                            to_krw(
                                p.qty * (price if sym == signal.symbol else p.avg_cost),
                                self.market_of.get(sym) or market_of_symbol(sym), self.fx,
                            ) * abs(self.leverage_of.get(sym) or 1.0)
                            for sym, p in positions.items()
                            if p.qty > 0 and abs(self.leverage_of.get(sym) or 1.0) > 1.0
                        )
                        added_lev_exposure = notional_krw * this_lev
                        cap_lev_krw = self.max_leveraged_exposure_pct * equity
                        if current_lev_exposure + added_lev_exposure > cap_lev_krw:
                            self._block(
                                f"레버리지 총노출 상한 초과: 현재 {current_lev_exposure:,.0f}원 + "
                                f"이번 주문 {added_lev_exposure:,.0f}원(명목 {notional_krw:,.0f}원×"
                                f"{this_lev:g}배) > 상한 {self.max_leveraged_exposure_pct*100:.0f}%×"
                                f"자산({equity:,.0f}원)={cap_lev_krw:,.0f}원 — 신규 진입 차단"
                            )
                            return None

            side = Side.BUY

        # 계산된 최종 수량 sanity 가드 — NaN/inf/음수/0은 여기서 전부 걸러진다. 위의
        # 개별 체크들은 흔한 케이스에 사람이 읽을 사유를 붙이기 위함이고, 이건 그
        # 체크들이 놓칠 수 있는 것(NaN은 `<= 0` 비교를 통과한다)까지 잡는 최종 방어선.
        if not math.isfinite(qty) or qty <= 0:
            self._block(
                f"수량 계산 이상 — 주문 생성 거부: qty={qty!r} price={price!r} "
                f"equity={equity!r} symbol={signal.symbol}"
            )
            return None

        if is_stop_loss_exit:
            bars = self._bar_ts(signal.symbol, ctx, 1, signal.strategy_id)
            if len(bars) > 0:
                self._stop_bar_ts[signal.symbol] = bars.index[-1]
                self._stop_day[signal.symbol] = today

        self._day_order_count[market] = self._day_order_count.get(market, 0) + 1
        if signal.action in _ENTRY_ACTIONS:
            # 진입만 예산을 쓴다. 청산은 위 카운터로 보이되 아무것도 막지 않는다 —
            # 청산이 예산을 먹으면 상한이 스스로를 소진한다(135건 사고).
            self._day_entry_count[market] = self._day_entry_count.get(market, 0) + 1
            if per_strategy:
                count_key = f"{signal.strategy_id}:{market}"
                self._day_entry_count_per_strategy[count_key] = (
                    self._day_entry_count_per_strategy.get(count_key, 0) + 1
                )
            self._recent_entries.setdefault((signal.symbol, signal.strategy_id), []) \
                .append(ctx.clock.now())
        # 주문 승인·손절 기록은 재시작을 넘어 유지돼야 한다 — 이 저장이 없으면
        # 재시작 한 번에 하루 주문 상한과 손절 쿨다운이 전부 초기화된다.
        self._save_day_state()
        return Order(
            symbol=signal.symbol,
            side=side,
            qty=qty,
            strategy_id=signal.strategy_id,
            reason=signal.reason,
            # 브로커 어댑터가 서버측 조건주문(손절/OCO)을 거는 데 쓴다. 진입에만
            # 의미가 있으므로 청산 주문에는 싣지 않는다.
            stop=signal.stop if side is Side.BUY else None,
            target=signal.target if side is Side.BUY else None,
        )
