"""순수 도메인 모델. 외부 의존성 없음 (stdlib only).

모든 가격은 종목의 표시 통화(USD/KRW) 그대로 저장한다.
환산은 portfolio/risk 레이어에서만 수행한다 (통화 순수성 원칙).
"""
from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

# 거래일 경계 — KST 08:00. 달력 자정이 아니다.
#
# 한 거래일은 KR 개장(09:00 KST)에서 시작해 그날 US 세션 마감(다음날 05:00~06:00
# KST, 서머타임에 따라 변동)에서 끝난다. 달력 자정으로 끊으면 US 세션이 한복판에서
# 두 날로 쪼개진다 — 그러면 일일 손실 차단기(3%)가 US 세션 도중에 리셋돼
# **하루에 최대 6%를 잃을 수 있다**. 일일 주문 상한도 마찬가지로 두 배가 된다.
# 08:00을 고르는 이유: US 마감(EST 06:00 / EDT 05:00) 이후이면서 KR 개장(09:00)
# 이전이라 두 서머타임 체계 모두에서 안전하다 (2026-08-11 사용자 정의).
TRADING_DAY_START_HOUR_KST = 8
_KST = timezone(timedelta(hours=9))


def trading_day(now: datetime) -> date:
    """이 시각이 속한 **거래일**. KST 08:00 이전은 전날 거래일에 속한다.

    **입력 타임존을 여기서 KST로 변환한다.** 이전 버전은 "엔진의 Clock은 항상 KST를
    준다"고 가정했는데 그 전제가 틀렸다 — `data.clock.WallClock.now()`는 **UTC**를
    돌려준다. 그 결과 실제 경계가 08:00 UTC(=17:00 KST)가 되어 **KR 오전 세션 전체가
    전날 거래일로 집계**됐다(2026-08-12 감사에서 실행으로 확인). 회로차단기의 "오늘"과
    텔레그램 리포트의 "오늘"이 서로 다른 날을 가리키고 있었다.

    naive datetime은 거부한다 — 조용히 KST로 가정하면 같은 부류의 착오가 반복된다.
    실패는 드러내는 편이 낫다.
    """
    if now.tzinfo is None:
        raise ValueError(
            "trading_day()는 tz-aware datetime만 받는다 — naive는 어느 타임존인지 "
            "알 수 없어 거래일 경계를 조용히 어긋나게 만든다"
        )
    return (now.astimezone(_KST) - timedelta(hours=TRADING_DAY_START_HOUR_KST)).date()


def market_of_symbol(symbol: str) -> str:
    """심볼에서 시장을 추론한다 — 6자리 숫자면 KR, 그 외는 US.

    저장소 4곳(loop/ledger/watch_scorer/kiwoom datafeed)이 각자 구현하던 규칙을
    한 곳으로 모은 것. **매핑 dict의 기본값으로 "US"를 쓰지 마라** — 관심종목은
    프로세스 기동 후에도 늘어나는데, 부팅 시점 스냅샷에 없는 KR 심볼이 US로
    떨어지면 예산이 환율로 나뉘어 수량이 ~1,400배 축소되고 KR 정수 반올림도
    건너뛴다 (2026-08-11 실운영에서 058610을 0.0015주 매수한 사고의 원인).
    """
    s = symbol.strip()
    return "KR" if (s.isdigit() and len(s) == 6) else "US"


def market_of(universe: dict) -> dict[str, str]:
    """symbol -> "US"/"KR" 매핑. universe.us / universe.kr 목록 기반.

    **왜 config 가 아니라 여기 있나.** 이 함수는 dict 를 받아 dict 를 돌려주는 순수
    함수인데 `apps/config.py` 에 얹혀 있었다. 그 바람에 `trade/strategy/__init__.py`
    가 설정 로더를 임포트해야 했고, 그건 `trade/strategy/CLAUDE.md` 가 명시적으로
    금지한 의존이다(전략은 파라미터를 생성자 인자로만 받는다). 함수를 옮겨 위반을
    없앴다 — 설정 파싱이 아니라 심볼 분류이므로 `market_of_symbol` 옆이 제자리다.
    """
    out: dict[str, str] = {}
    for sym in universe.get("us", []):
        out[sym] = "US"
    for sym in universe.get("kr", []):
        out[sym] = "KR"
    return out


STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_EXECUTED = "executed"


@dataclass
class ApprovalRequest:
    """승인 대기 중인 신규 진입 1건.

    `quant/trade/approval.py`의 `ApprovalGate`(영속화)와
    `quant/adapters/notify/telegram_approval.py`(텔레그램 표시) 양쪽이 이 타입을
    안다. 어댑터가 거래 평면을 임포트하면 안 되므로(core/CLAUDE.md), 이 순수
    데이터클래스는 여기 core에 둔다.

    qty/price는 **사용자에게 보여준 시점의 값**이다 — 집행에 그대로 쓰는 값이
    아니다. 승인 시점에 loop가 최신 시세로 risk.approve를 다시 돌려 수량을 새로
    계산하고, price는 그때 "얼마나 움직였나"(drift)를 재는 기준으로만 쓴다.
    """

    id: str
    strategy_id: str
    symbol: str
    action: str  # SignalAction의 value ("enter_long" | "scale_in")
    qty: float
    price: float
    reason: str
    created_at: float  # epoch seconds. 만료 판정 기준 — 재시작해도 이 값이 살아남는다
    # ↓ 승인 시점에 Signal을 그대로 재구성해 risk.approve를 재실행하기 위한 원본 필드.
    # 낡은 Order 객체를 집행하지 않는 것이 이 게이트의 불변식 중 하나라, 승인 때
    # 다시 만들 수 있을 만큼은 저장해야 한다.
    target_weight: float = 0.0
    stop: float | None = None
    target: float | None = None
    state_update: dict = field(default_factory=dict)
    status: str = STATUS_PENDING
    message_id: int | None = None
    decided_at: float | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING


class Market(str, Enum):
    US = "US"
    KR = "KR"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalAction(str, Enum):
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"
    SCALE_IN = "scale_in"
    SCALE_OUT = "scale_out"


@dataclass(frozen=True)
class Bar:
    """완성된 OHLCV 봉. 미완성(forming) 봉은 이 타입으로 존재해선 안 된다."""
    symbol: str
    ts: datetime  # bar open time, tz-aware
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    ts: datetime
    price: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float  # 표시 통화 기준
    opened_at: datetime | None = None
    # 전략이 관리하는 부가 상태 (stop/target 등) — 영속화 대상.
    # 여러 전략이 같은 심볼을 동시 보유할 수 있다(2026-08-11 사용자 지시: "전략마다
    # 구매한 만큼을 그대로 지키면서 매도·청산"). meta["lots"]: dict[strategy_id, lot]에
    # 전략별 상태(qty/avg_cost/entry/stop/target/...)를 담고, qty/avg_cost 필드
    # 자체는 지금처럼 **심볼 합산 진실**로 유지한다(브로커 대사·리스크 총노출 계산이
    # 이걸 쓴다) — lot은 그 합산을 전략별로 쪼갠 부분합이다.
    meta: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.qty > 0

    def lot(self, strategy_id: str) -> dict | None:
        """strategy_id의 lot을 순수 조회한다(없으면 None). **이행(migrate)하지
        않는다** — 소유권을 아직 확정하지 않은 채(예: 다른 전략의 `_owns` 스캔)
        호출해도 부작용이 없어야 하기 때문이다. 쓰기 의도가 있는 접근은
        `ensure_lot`을 쓴다."""
        lots = self.meta.get("lots")
        if lots is None:
            return None
        return lots.get(strategy_id)

    def lot_qty(self, strategy_id: str) -> float:
        """strategy_id가 이 심볼에 보유한 수량(lot 기준). lot이 없으면 0.0 —
        호출부가 조건 없이 안전하게 쓸 수 있게 한다(고아/미추적 케이스는 호출부가
        pos.qty로 폴백)."""
        lot = self.lot(strategy_id)
        return float(lot.get("qty", 0.0)) if lot else 0.0

    def ensure_lot(self, strategy_id: str) -> dict:
        """strategy_id의 lot을 반환한다(없으면 만든다) — **쓰기 의도가 있는 첫
        접근에서만** 이행(migrate)이 일어난다.

        재시작 이행: meta에 아직 내 lot이 없는데 평평한(레거시, lots 도입 이전)
        상태가 남아 있고 그 소유자가 나(명시적 "strategy" 태그 일치) 또는 미상
        (태그 자체가 없음 — donchian처럼 태그를 남기지 않던 전략)이면, 그 평평한
        상태를 내 lot으로 옮긴다(qty/avg_cost 포함). 소유자가 다른 전략이면 이행하지
        않고 내 몫만 빈 lot으로 새로 만든다 — 남의 평평한 상태는 그 전략이 자기
        ensure_lot을 호출할 때 이행된다.

        같은 심볼에 내가 아직 lot이 없는 동안 다른 전략이 먼저 lot을 만들었을 수
        있다(같은 사이클에 다른 전략이 먼저 진입) — 그 경우 평평한 qty를 통째로
        내 몫으로 이행하면 이미 추적된 다른 lot의 수량까지 중복 계산된다. 그래서
        이행 시 qty는 `pos.qty - 이미 추적된 lot qty 합`(잔여분)만 가져간다.
        avg_cost는 심볼 합산값으로 근사한다 — 레거시 단일 meta 체제는 전략별
        원가를 따로 기록하지 않았으므로 완전한 복원은 불가능하다(최선의 근사)."""
        lots = self.meta.setdefault("lots", {})
        if strategy_id in lots:
            return lots[strategy_id]
        flat_keys = [k for k in self.meta if k not in ("lots", "strategy")]
        owner = self.meta.get("strategy")
        if flat_keys and (owner is None or owner == strategy_id):
            flat = {k: self.meta[k] for k in flat_keys}
            tracked = sum(float(l.get("qty", 0.0)) for l in lots.values())
            flat["qty"] = max(self.qty - tracked, 0.0)
            flat["avg_cost"] = self.avg_cost
            for k in flat_keys:
                del self.meta[k]
            self.meta.pop("strategy", None)
            lots[strategy_id] = flat
        else:
            lots[strategy_id] = {}
        return lots[strategy_id]


@dataclass(frozen=True)
class Signal:
    """전략의 의사 표현. 수량이 아니라 목표 비중으로 말한다 — 사이징은 risk 레이어 소관."""
    strategy_id: str
    symbol: str
    action: SignalAction
    target_weight: float  # 전략 배정 자본 대비 비중 (ENTER/SCALE_IN)
    exit_fraction: float = 1.0  # EXIT/SCALE_OUT 시 청산 비율
    reason: str = ""  # 사람이 읽는 근거 — telegram/리포트에 그대로 노출
    stop: float | None = None
    target: float | None = None
    # 체결 확인 후에만 Position.meta에 적용할 상태 변경분 (scale_out/scale_in 등).
    # run_cycle이 place_order로부터 실제 Fill을 받은 뒤에만 적용한다 — 거부/미체결
    # 시 전략 상태가 오염되는 것을 막는다.
    state_update: dict = field(default_factory=dict)
    # 고정 수량 지정(선택, 2026-08-19). 있으면 risk 레이어가 target_weight 대신
    # 이 수량 그대로를 예산으로 환산해 요청한다 — "종목 가격이 얼마든 N주씩"을
    # 표현하는 유일한 방법(비중은 전략 자본 규모에 좌우되므로 표현 불가).
    # None(기본)이면 기존 비중 기반 사이징 경로가 100% 그대로 유지된다(이 필드를
    # 쓰지 않는 다른 모든 전략은 동작이 전혀 바뀌지 않는다). 하드레일
    # (max_position_pct/max_symbol_pct_total/max_order_notional_pct/전략별
    # 현금 게이트)은 target_qty를 써도 그대로 적용되므로 실제 체결 수량은
    # target_qty보다 작아질 수 있다 — risk.manager._approve_entry_per_strategy
    # 참고. frgn_accumulate가 첫 사용처(fixed_amount_krw 근사 사이징이 전략
    # 자본 규모 변경에 취약해 매수액이 2배로 튄 사고의 수정).
    target_qty: float | None = None


@dataclass(frozen=True)
class Order:
    symbol: str
    side: Side
    qty: float
    strategy_id: str
    reason: str = ""
    # 진입 주문에 딸린 보호가격. risk 레이어가 Signal에서 그대로 실어 보낸다.
    # 브로커 어댑터는 진입 체결 후 **서버측 조건주문(손절/OCO)**을 거는 데 쓴다 —
    # 폴링 주기(10초)로는 급락을 못 잡으므로 서버측 손절이 실전 안전의 핵심이다.
    # None이면 조건주문을 걸지 않는다(어댑터가 값을 지어내지 않는다).
    stop: float | None = None
    target: float | None = None
    # 결정(사이징) 시점의 시세 — 리스크 레이어가 수량 계산에 쓴 가격 그대로.
    # 브로커 의도 저널이 이 값을 남겨야 TCA(의도가 vs 체결가 슬리피지)가
    # 표본을 얻는다(2026-08-26 감사: 가격 없는 의도 행은 슬리피지를 못 잰다).
    # None = 모른다 — 저널은 키를 생략한다(값을 지어내지 않는다).
    ref_price: float | None = None


@dataclass(frozen=True)
class Fill:
    symbol: str
    side: Side
    qty: float
    price: float
    ts: datetime
    strategy_id: str
    fee: float = 0.0
    reason: str = ""
    # 이 체결이 실현한 손익 = (체결가 - 체결 직전 평균단가) x 수량. 표시 통화 기준
    # (price와 같은 통화), 수수료 차감 전. 매수는 항상 0.0.
    #
    # None은 "브로커가 평균단가를 몰라 계산할 수 없다"는 뜻이다 — 0.0으로 위장하지
    # 않는다. 실현손익을 체결 시점에 브로커가 붙이는 이유: 체결 로그만 보고 나중에
    # 평균단가를 재구성하면 로그가 한 건이라도 어긋나는 순간 그 뒤 전 구간의 원가가
    # 오염되고, 그 사실이 드러나지 않는다(2026-08 백테스트 손익 부호 역전 사고).
    realized_pnl: float | None = None
    # 체결 직후 계좌 현금(KRW). 원장↔현금 대조에서 갭이 발견됐을 때 "어느 체결부터
    # 어긋났는가"를 재구성이 아니라 기록으로 답하기 위한 스냅샷 — 2026-08-11
    # 160,974원 미설명 갭에서 시점별 현금 기록이 없어 원인 특정에 실패한 교훈.
    # None = 브로커가 현금을 모른다(예: 실브로커 지연 정산) — 0.0으로 위장하지 않는다.
    cash_after: float | None = None


class OrderStatus(str, Enum):
    """주문의 생애. **어휘를 지어내지 않았다** — 브로커가 실제로 돌려주는 값에서 왔다
    (토스: PENDING / PARTIAL_FILLED / FILLED / REJECTED / CANCELED ...).

    비터미널: ACCEPTED, PARTIALLY_FILLED — 아직 결론이 없다.
    터미널:   FILLED, REJECTED, CANCELED, EXPIRED — 더 이상 변하지 않는다.

    **"모른다"는 상태가 아니다.** 폴링이 결론 없이 끝나면 주문은 그냥 *열린 채*로
    남는다. 그 잔량이 대사(reconcile)가 집어야 할 대상이고, 지금은 그게 버려진다
    (토스 어댑터: "미체결 잔량은 버린다").
    """
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"


# 미국 분할주는 소수점 6자리까지다 — 그 아래 잔량은 반올림 잡음이지 미체결이 아니다
# (`quant/trade/reconcile.py` 의 _QTY_TOLERANCE 와 같은 기준).
QTY_TOLERANCE = 1e-6


@dataclass(frozen=True)
class OrderState:
    """주문 하나의 현재 상태. **불변** — 전이는 새 값을 만든다.

    왜 `Order` 에 상태를 넣지 않았나: `Order` 는 "무엇을 시켰나"라는 의도이고 변하지
    않는다. 상태는 그 의도에 대해 브로커가 답한 결과다. 둘을 한 객체에 섞으면
    "내가 시킨 것"과 "실제로 일어난 것"이 구분되지 않는다 — 이 phase 가 없애려는
    혼동이 정확히 그것이다.
    """
    order: Order
    status: OrderStatus
    filled_qty: float = 0.0
    # 체결된 수량의 **가중평균** 단가. 마지막 체결가가 아니다(그러면 실현손익이 틀어진다).
    # None = 아직 체결이 없다 — 0.0 으로 위장하지 않는다(Fill.realized_pnl 과 같은 원칙).
    avg_price: float | None = None
    # None = **브로커에 닿지 못했다.** "거부됐다"와 "애초에 안 나갔다"를 구분하는
    # 것이 이 필드다 — MODE!=live, 엔진 소유 수량 0, 수량 계산 실패는 전부 로컬
    # 차단이라 주문번호가 없다. 상태를 하나 더 만들지 않고 이걸로 구분한다.
    broker_order_id: str | None = None
    updated_at: datetime | None = None
    reason: str = ""
    # 이 주문이 만들어낸 체결(있으면). 원장에 넣을 값은 여기서 나온다 —
    # 수수료·실현손익·현금 스냅샷은 브로커만 아는 값이라 상태기계가 만들 수 없다.
    fill: "Fill | None" = None

    @property
    def remaining_qty(self) -> float:
        """못 받은 수량. **오늘 조용히 버려지는 정보가 이것이다.**"""
        left = self.order.qty - self.filled_qty
        return 0.0 if abs(left) <= QTY_TOLERANCE else left

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED, OrderStatus.REJECTED,
    OrderStatus.CANCELED, OrderStatus.EXPIRED,
})


def input_hash(features: dict) -> str:
    """판단이 **본 입력**의 지문. Phase 7.1.

    리더보드의 목적은 결정론적 스코어러와 LLM 을 **공정하게** 비교하는 것이다.
    서로 다른 입력을 보고 낸 판단을 나란히 세우면 그건 실력 비교가 아니라 정보량
    비교다. 그래서 판단마다 이 해시를 남기고 **같은 해시끼리만** 비교한다.

    그래서 여기 들어가면 안 되는 것이 분명하다 — **생산자 이름도, 그 판단의
    출력도.** 섞이면 같은 입력을 본 두 판단이 다른 해시를 갖게 되고 묶을 수 없다.

    `sort_keys` 로 키 순서를 지우고, `None` 은 `null` 로 남겨 **결측과 0을 구분한다**
    ("트렌딩 0점"과 "트렌딩을 계산 못 했다"는 다른 사건이다 — selections 원칙).
    """
    canon = json.dumps(features, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Judgment:
    """"누가, 어떤 입력을 보고, 무엇이라 했나". Phase 7.1.

    `Signal` 과 다르다: `Signal` 은 **행동 지시**(전략 → 리스크 → 주문)이고, 이건
    **채점 대상 의견**이다. 판단은 주문을 내지 않는다 — 기록되고 나중에 실현 수익으로
    채점된다. LLM 이 이 통로로만 들어오게 하는 것이 설계 의도다(못 이기면 승격하지
    않으면 되고 비용은 0이다).

    `score` 는 생산자별 척도라 그대로 비교하지 않는다 — 순위/판정으로 환산해 쓴다.
    `None` 은 "채점 못 함"이고 0 과 다르다.
    """
    producer: str
    producer_version: str
    input_hash: str
    market: str
    symbol: str
    session_date: str
    score: float | None
    verdict: str          # "pass" | "reject" — 생산자 공통 어휘
    rationale: str
    ts: str

    def natural_key(self) -> tuple:
        """멱등 적재용. 같은 생산자가 같은 입력을 같은 날 두 번 봐도 판단은 하나다."""
        return (self.producer, self.producer_version, self.input_hash,
                self.symbol, self.session_date)
