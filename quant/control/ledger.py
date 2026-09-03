"""거래 원장 + 스코어보드 — "숫자가 자본을 결정한다"(2026-08-10 사용자 원칙).

세션 마감 요약(_SessionTallySink)은 그 세션의 합계만 알고 죽으면 잊는다. 이 원장은
체결을 JSONL로 영속화해 전략별·종목별 **누적** 승률/평균R/거래당 bps를 만든다 —
어느 전략을 끄고 어디에 자본을 몰지 판단하는 유일한 근거 데이터.

설계 원칙:
- 리포팅이 거래를 죽이면 안 된다: 쓰기 실패는 1회 경고 후 무시(_SessionTallySink와
  같은 계약). 읽기는 깨진 줄을 건너뛴다.
- 라운드트립 손익은 Fill.realized_pnl(브로커가 체결 시점에 확정, 수수료 차감 전)을
  합산한다 — 로그에서 평균단가를 재구성하지 않는다(models.py의 경고 그대로).
- 통화를 섞지 않는다: 승률·payoff·bps는 통화 무관 축이라 전략 단위로 합산하고,
  손익 절대액은 시장(KR=₩, US=$)별로만 합산해 보여준다.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant.core.ports import EventSink
from quant.core.models import Fill, OrderStatus, Signal

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = Path("data/state/trades.jsonl")
DEFAULT_EQUITY_CURVE_PATH = Path("data/ledger/equity_curve.jsonl")

# 종결 트립 30건 미만이면 승률 표준오차가 너무 커서(이항비율 CI 폭이 대략
# ±1/sqrt(n) 규모) 자본배분 근거로 쓸 수 없다 — 30은 이 저장소가 실무적으로
# 합의한 최소 표본선(통계적으로 "충분"을 보장하진 않지만 그 미만은 명백히 부족).
MIN_TRIPS_FOR_JUDGEMENT = 30

# 명목가가 이 미만인 트립은 bps/승률 표본을 왜곡한다(잔돈 트립 1건이 부호를
# 뒤집을 수 있음 — 2026-08-10 실측). 통화별로 별도 임계를 둔다: 환율로 환산해
# 하나로 합치면 환율 의존성이 생기므로 KR/US를 각자 통화 기준으로 판정한다.
DUST_NOTIONAL_KRW = 30_000
DUST_NOTIONAL_USD = 20

# 반복 거부 로그 쿨다운(2026-08-31 실측: EC2 orders.jsonl 에 donchian SQQQ
# "매도 가능 수량 0" 거부가 사이클마다 — 사실상 수 초 단위로 — 똑같이 찍혔다,
# 110줄). 같은 (전략, 심볼, 사유)가 이 창 안에서 다시 오면 파일에는 안 쓴다.
# 재시도 자체(엔진 판단)는 그대로 두고 **부기만** 억제한다 — 메모리에만 있어
# 프로세스 재시작 때마다 리셋된다(디스크 쿨다운 상태를 새로 만들지 않는다).
REJECT_LOG_COOLDOWN = timedelta(hours=1)

_Z_95 = 1.959963984540054  # 95% 신뢰수준 표준정규분포 z-score


def _wilson_ci(wins: int, n: int, z: float = _Z_95) -> tuple[float, float]:
    """이항비율 95% 신뢰구간 — Wilson score interval.

    scipy가 uv.lock에 간접 의존성으로 존재하긴 하나 pyproject.toml의 직접
    의존성이 아니므로(전이 의존성은 언제든 사라질 수 있음) 여기서는 순수
    stdlib(``math``)로 구현한다. Clopper-Pearson(정확 이항)보다 소표본에서
    구간이 다소 좁지만, 표준정규근사 기반 방법 중 정밀도가 가장 낫다고
    알려진 방법이다.
    """
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    lower = (center - margin) / denom
    upper = (center + margin) / denom
    return (max(0.0, lower), min(1.0, upper))


def _verdict(lower: float, upper: float) -> str:
    """승률 CI가 50%(동전던지기)를 포함하면 판단 불가 — 방향성 있는 CI만 유의로 본다."""
    if lower > 0.5:
        return "유의(양)"
    if upper < 0.5:
        return "유의(음)"
    return "판단 불가"


def _market_of(symbol: str) -> str:
    """6자리 숫자 = KR — 저장소 전역과 동일한 추론 (orb_scan/assembly와 같음)."""
    return "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"


# `quant.apps.cli.cmd_seed_real`이 이식 정리 매도에 남기는 마커. **원장을 읽는
# 모든 집계의 단일 출처**다 (2026-09-02): 이 판별식이 performance.py 에만 있었던
# 탓에 공개 JSON 은 이식 정리를 뺐는데 `session_pnl_summary`(텔레그램·daily-wrap·
# pnl-attribution 이 전부 이걸 쓴다)는 안 빼서, 2026-09-01 US 세션이 프로그램
# 매매 -$102.90 인데 -$1,214.65 로 발송됐다. 새 집계는 여기서 가져다 쓴다.
SEEDING_LIQUIDATION_MARKER = "실계좌 이식 정리"


def is_seeding_liquidation(trade: dict) -> bool:
    """이 체결이 실계좌 이식 시 물려받은 레거시 포지션의 정리 매도인가.

    프로그램의 매매 판단이 아니므로 성과·세션 손익에서 빼야 한다. `strategy_id`
    (`legacy`)가 아니라 `reason` 마커로 판별한다 — strategy_id 는 다른 목적으로도
    쓰일 수 있지만 이 문구는 `cmd_seed_real` 한 곳에서만 나온다."""
    return SEEDING_LIQUIDATION_MARKER in str(trade.get("reason") or "")


def seeding_boundary_ts(trades: list[dict]) -> datetime | None:
    """paper→real_seeded 경계 시각 = 이식 정리 매도 행들의 **최대 ts**.

    이식 이벤트가 없으면 None. `quant.control.performance._boundary_ts`와 같은
    정의를 쓴다 — 두 곳이 서로 다른 경계를 쓰면 공개 JSON 과 스코어보드가
    갈라진다."""
    best: datetime | None = None
    for t in trades:
        if not is_seeding_liquidation(t):
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if best is None or ts > best:
            best = ts
    return best


class TradeLedgerSink:
    """EventSink 래퍼 — 체결을 JSONL 원장에 추가 기록한다."""

    def __init__(self, inner: EventSink, path: Path | str | None = None,
                 orders_path: Path | str | None = None):
        self._inner = inner
        # None이면 호출 시점에 모듈 전역을 읽는다 — conftest의 monkeypatch 격리가
        # 기본 인자 바인딩(정의 시점 고정)에 막히지 않게 하기 위함.
        self._path = Path(path or DEFAULT_LEDGER_PATH)
        # 주문 생애는 **체결 원장과 다른 파일**이다. trades.jsonl 은 "일어난 체결"의
        # append-only 기록이고 스코어보드·적재기가 그 스키마를 전제로 읽는다 —
        # 체결되지 않은 주문을 거기 섞으면 승률·payoff 가 조용히 오염된다.
        self._orders_path = Path(orders_path or self._path.parent / "orders.jsonl")
        self._write_warned = False
        self._order_write_warned = False
        # 반복 거부 로그 쿨다운(REJECT_LOG_COOLDOWN) — (strategy_id, symbol, reason)
        # 별 마지막 기록 시각. 프로세스 메모리에만 있다.
        self._reject_last_logged: dict[tuple[str, str, str], datetime] = {}

    def on_signal(self, signal: Signal) -> None:
        self._inner.on_signal(signal)

    def on_fill(self, fill: Fill) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "ts": fill.ts.isoformat(),
                "strategy_id": fill.strategy_id,
                "symbol": fill.symbol,
                "side": str(getattr(fill.side, "value", fill.side)),
                "qty": fill.qty,
                "price": fill.price,
                "fee": fill.fee,
                "realized_pnl": fill.realized_pnl,
                # 체결 직후 현금 스냅샷 — 원장↔현금 갭의 발생 지점을 기록으로 특정
                # (2026-08-11 160,974원 미설명 갭의 교훈). 구버전 Fill엔 없다.
                "cash_after": getattr(fill, "cash_after", None),
                # 체결 직후 USD 현금 풀 스냅샷(2026-09-02 추가, additive). `cash_after`는
                # 시장 무관 항상 KRW 풀이라 US 체결의 현금 변화를 전혀 담지 못했고,
                # 그래서 세션 리포트가 "US … 계좌 현금 변화 +0원"이라는 거짓을 찍었다.
                # dual_currency=False(백테스트 등)나 구버전 행에는 없다 — None.
                "cash_after_usd": getattr(fill, "cash_after_usd", None),
                # 트레이더의 시그널 기록(2026-08-26 소유자 조직도 역할 4·5) —
                # "진입 당시 시그널이 어디서 나온건지 트레이더가 기록해줘야,
                # 장 종료 후 5번 직원이 그걸 보고 피드백을 준다." 패턴A/B·구조
                # 손절 근거·게이트/W%R 판정이 전부 이 문자열에 실린다. 그전엔
                # 엔진 로그에만 있어 로테이션과 함께 사라졌다.
                "reason": getattr(fill, "reason", "") or "",
                "market": _market_of(fill.symbol),
            }, ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 — 원장 기록 실패가 체결 처리를 막으면 안 된다
            if not self._write_warned:
                self._write_warned = True
                logger.warning("거래 원장 기록 실패(이후 경고 생략): %s: %s", type(e).__name__, e)
        self._inner.on_fill(fill)

    def on_order(self, state) -> None:
        """주문의 생애 한 줄. **체결이 아니라 "시킨 것과 일어난 일의 차이"다.**

        오늘 답할 수 없는 질문에 답하기 위한 기록이다:
        "20주를 시켰는데 8주만 채워졌나" — 체결 원장엔 8주 체결만 있고 못 받은
        12주는 어디에도 없다(토스 어댑터: "미체결 잔량은 버린다").

        기록 실패는 삼킨다 — `on_fill` 과 같은 계약이다. 사후 분석용 부기가 매매를
        멈추면 안 된다.

        **거부(REJECTED)는 반복 억제가 걸린다** — 같은 (전략, 심볼, 사유)가
        `REJECT_LOG_COOLDOWN`(1시간) 안에 다시 오면 파일에 또 쓰지 않는다
        (2026-08-31, EC2 실측: 사이클마다 동일 거부가 그대로 반복 기록됐다).
        엔진의 재시도 판단 자체는 건드리지 않는다 — 내부 싱크(`self._inner`)는
        억제와 무관하게 항상 부른다.
        """
        reason = state.reason or state.order.reason
        if state.status == OrderStatus.REJECTED:
            key = (state.order.strategy_id, state.order.symbol, reason)
            now = state.updated_at or datetime.now(timezone.utc)
            last = self._reject_last_logged.get(key)
            if last is not None and (now - last) < REJECT_LOG_COOLDOWN:
                inner_on_order = getattr(self._inner, "on_order", None)
                if inner_on_order is not None:
                    inner_on_order(state)
                return
            self._reject_last_logged[key] = now
        try:
            self._orders_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps({
                "ts": state.updated_at.isoformat() if state.updated_at else None,
                "strategy_id": state.order.strategy_id,
                "symbol": state.order.symbol,
                "side": str(getattr(state.order.side, "value", state.order.side)),
                "status": state.status.value,
                "requested_qty": state.order.qty,
                "filled_qty": state.filled_qty,
                # 이 한 칸이 이 파일의 존재 이유다.
                "remaining_qty": state.remaining_qty,
                "avg_price": state.avg_price,
                "broker_order_id": state.broker_order_id,
                "reason": reason,
                "market": _market_of(state.order.symbol),
            }, ensure_ascii=False)
            with open(self._orders_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 — 부기가 매매를 막으면 안 된다
            if not self._order_write_warned:
                self._order_write_warned = True
                logger.warning("주문 원장 기록 실패(이후 경고 생략): %s: %s",
                               type(e).__name__, e)
        inner_on_order = getattr(self._inner, "on_order", None)
        if inner_on_order is not None:
            inner_on_order(state)


def load_trades(path: Path | str = DEFAULT_LEDGER_PATH) -> list[dict]:
    """깨진 줄은 건너뛴다 — 원장 일부 손상이 전체 스코어보드를 죽이면 안 된다."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("symbol"):
                out.append(row)
        except ValueError:
            continue
    return out


def load_orders(path: Path | str) -> list[dict]:
    """주문 생애 원장(orders.jsonl, `TradeLedgerSink.on_order`) 로더.

    파일 포맷은 trades.jsonl과 다르지만 파싱 계약(깨진 줄 건너뛰기, symbol 없는
    행 제외)은 동일하다 — `load_trades`를 그대로 재사용한다. CLI의 `orders`
    서브커맨드(자금 부족 거부 조회 등)가 이 함수를 쓴다."""
    return load_trades(path)


def round_trips(trades: list[dict]) -> list[dict]:
    """(전략, 종목)별 체결을 라운드트립으로 묶는다 — 누적 수량이 0으로 돌아오면 종결.

    트립 pnl = 매도 체결들의 realized_pnl 합(수수료 차감 전) - 트립 전체 수수료.
    realized_pnl이 None(브로커가 원가를 모름)인 체결이 낀 트립은 pnl_known=False로
    표시하고 승패 집계에서 제외한다 — 모르는 것을 0으로 위장하지 않는다.

    ## 실계좌 이식은 **시대 경계**다 (2026-09-02)

    `cmd_seed_real`은 Portfolio 를 실계좌 스냅샷으로 갈아끼우면서 원장에는
    상계 행을 남기지 않는다. 그래서 이 함수가 원장을 재생하면 이식 시점에
    열려 있던 lot 이 영원히 남아, 19개 (전략,종목) 쌍에 유령 재고가 생겼다
    (실측: llm_trader 024110 원장 106주 / 실제 0주). 결과는 두 가지다 —
    ①이식 후의 정상 왕복이 유령 재고에 가려 트립으로 안 세지고(2026-09-01
    gap_fade TQQQ +$1.13), ②이식으로 물려받은 주식을 나중에 팔면 모의 시대
    매수와 짝지어져 **없던 트립이 만들어진다**.

    그래서 `seeding_boundary_ts()` 시점에 열린 lot 을 전부 버린다(그건 트립이
    아니다). 경계 이후 재고 없는 매도는 아무것도 열지도 닫지도 않고 건너뛴다
    — 경계를 가로질러 짝지우지 않는다. 이식 정리 매도 행 자체도 집계에서 뺀다.

    **이월 보유(2026-09-01 이식의 005930 6주)는 원장에 이관 행이 없다** —
    `cmd_seed_real`이 남긴 7행은 전부 정리 *매도*고 이월분에 대한 행은 없다.
    따라서 이 함수는 그 보유에 대해 라운드트립을 만들 수 없다(원가를 모른다).
    그 손익은 세션 손익(`session_pnl_summary`)에만 나타난다.
    """
    boundary_ts = seeding_boundary_ts(trades)

    def _ts(row: dict) -> datetime | None:
        try:
            d = datetime.fromisoformat(str(row.get("ts")))
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    by_key: dict[tuple[str, str], list[dict]] = {}
    for t in sorted(trades, key=lambda x: str(x.get("ts", ""))):
        if is_seeding_liquidation(t):
            continue  # 프로그램의 매매 판단이 아니다 — 트립 재료가 아니다
        by_key.setdefault((str(t.get("strategy_id", "?")), str(t["symbol"])), []).append(t)

    trips: list[dict] = []
    for (strategy, symbol), fills in by_key.items():
        qty = 0.0
        cur: list[dict] = []
        crossed = False
        for f in fills:
            side = str(f.get("side", "")).upper()
            f_qty = float(f.get("qty", 0) or 0)
            if boundary_ts is not None and not crossed:
                ts = _ts(f)
                if ts is not None and ts > boundary_ts:
                    crossed = True
                    if cur:
                        logger.debug(
                            "이식 경계에서 미종결 lot 폐기: %s/%s %d체결",
                            strategy, symbol, len(cur),
                        )
                    qty, cur = 0.0, []
            if crossed and not cur and side != "BUY":
                # 이식으로 물려받은 주식의 매도 — 짝지을 진입이 이 시대에 없다.
                # 모의 시대 매수와 엮으면 없던 트립을 지어내는 셈이다.
                logger.debug("이식 경계 이후 재고 없는 매도 건너뜀: %s/%s", strategy, symbol)
                continue
            qty += f_qty if side == "BUY" else -f_qty
            cur.append(f)
            if cur and abs(qty) < 1e-9:
                buys = [x for x in cur if str(x.get("side", "")).upper() == "BUY"]
                sells = [x for x in cur if str(x.get("side", "")).upper() != "BUY"]
                pnl_known = all(x.get("realized_pnl") is not None for x in sells)
                gross = sum(float(x.get("realized_pnl") or 0) for x in sells)
                fees = sum(float(x.get("fee", 0) or 0) for x in cur)
                notional = sum(float(x.get("qty", 0) or 0) * float(x.get("price", 0) or 0) for x in buys)
                pnl = gross - fees
                trips.append({
                    "strategy": strategy, "symbol": symbol,
                    "market": str(cur[0].get("market") or _market_of(symbol)),
                    "entry_ts": cur[0].get("ts"), "exit_ts": cur[-1].get("ts"),
                    "pnl": pnl, "fees": fees, "notional": notional,
                    "bps": (pnl / notional * 1e4) if notional > 0 else 0.0,
                    "pnl_known": pnl_known,
                    "n_fills": len(cur),
                })
                cur = []
        # cur에 남은 것 = 미종결 포지션 — 트립으로 세지 않는다
    return trips


def _fmt_amount(v: float, market: str) -> str:
    return f"{v:,.0f}원" if market == "KR" else f"${v:,.2f}"


def _strategy_block(name: str, trips: list[dict]) -> list[str]:
    known = [t for t in trips if t["pnl_known"]]
    unknown = len(trips) - len(known)
    if not known:
        return [f"[{name}] 종결 트레이드 없음" + (f" (손익미상 {unknown}건 제외)" if unknown else "")]
    wins = [t for t in known if t["pnl"] > 0]
    losses = [t for t in known if t["pnl"] <= 0]
    n = len(known)
    wr = len(wins) / n
    lower, upper = _wilson_ci(len(wins), n)
    verdict = _verdict(lower, upper)
    avg_bps = sum(t["bps"] for t in known) / n
    lines: list[str] = []
    if n < MIN_TRIPS_FOR_JUDGEMENT:
        lines.append(f"⚠️ 표본 부족 ({n}/{MIN_TRIPS_FOR_JUDGEMENT}건) — 이 숫자로 자본 배분을 결정하지 마라")
    lines.append(
        f"[{name}] {n}건 · 승률 {wr:.0%} (95% CI {lower:.0%}~{upper:.0%}) — {verdict}"
        f" · 평균 {avg_bps:+.1f}bp/건"
    )
    # payoff/기대값은 bps 축으로 계산 — 통화 혼합 없이 전략 전체를 한 축으로 본다
    aw = sum(t["bps"] for t in wins) / len(wins) if wins else 0.0
    al = abs(sum(t["bps"] for t in losses) / len(losses)) if losses else 0.0
    payoff = (aw / al) if al > 0 else float("inf") if aw > 0 else 0.0
    expectancy = wr * aw - (1 - wr) * al
    lines.append(
        f"  평균이익 {aw:+.1f}bp · 평균손실 -{al:.1f}bp · payoff {payoff:.2f}"
        f" · 기대값 {expectancy:+.1f}bp (표본 {n}건)"
    )
    for market in ("KR", "US"):
        mt = [t for t in known if t["market"] == market]
        if mt:
            lines.append(
                f"  {market}: 손익 {_fmt_amount(sum(t['pnl'] for t in mt), market)}"
                f" · 수수료 {_fmt_amount(sum(t['fees'] for t in mt), market)} ({len(mt)}건)"
            )
    for market, threshold in (("KR", DUST_NOTIONAL_KRW), ("US", DUST_NOTIONAL_USD)):
        dust = [t for t in known if t["market"] == market and t["notional"] < threshold]
        if dust:
            lines.append(
                f"  (명목 {_fmt_amount(threshold, market)} 미만 {len(dust)}건 포함 — 표본 왜곡 주의)"
            )
    if unknown:
        lines.append(f"  (손익미상 {unknown}건 집계 제외)")
    return lines


def scoreboard_text(trips: list[dict], title: str = "누적 스코어보드") -> str:
    if not trips:
        return f"📊 {title}: 종결된 트레이드가 아직 없음"
    lines = [f"📊 {title} (종결 {len(trips)}건)"]
    strategies = sorted({t["strategy"] for t in trips})
    for s in strategies:
        lines += _strategy_block(s, [t for t in trips if t["strategy"] == s])
    # 종목별 상/하위 3 (bps 기준, 손익 확정분만)
    known = [t for t in trips if t["pnl_known"]]
    by_symbol: dict[str, list[dict]] = {}
    for t in known:
        by_symbol.setdefault(t["symbol"], []).append(t)
    ranked = sorted(
        ((sym, sum(x["bps"] for x in ts) / len(ts), len(ts)) for sym, ts in by_symbol.items()),
        key=lambda x: x[1], reverse=True,
    )
    if ranked:
        lines.append("종목 상위: " + ", ".join(f"{s} {b:+.0f}bp({n})" for s, b, n in ranked[:3]))
        if len(ranked) > 3:
            lines.append("종목 하위: " + ", ".join(f"{s} {b:+.0f}bp({n})" for s, b, n in ranked[-3:]))
    return "\n".join(lines)[:3500]


# --- A/B 갈래 비교 (2026-09-03) -----------------------------------------------
# 질문 하나만 답하는 계산이다: **"촉매(뉴스·수급) 있는 종목만 골라 매매하면
# 실제로 더 버는가?"** 같은 전략 클래스를 두 갈래로 나란히 돌리고(설정의
# `universe_filter` — `quant/trade/strategy/__init__.py`), 두 갈래가 **서로 겹치지
# 않는 종목 집합**을 보게 만들었으므로 원장의 `strategy_id` 만으로 비교가 성립한다.
#
# 규율은 스코어보드와 같다: 표본이 얇으면 숫자를 내지 않고 "판단 불가"라고 쓴다.
# 여기서는 **양쪽 갈래 모두** MIN_TRIPS_FOR_JUDGEMENT 를 넘어야 판정한다 — 한쪽만
# 30건이어도 차이의 신뢰구간은 얇은 쪽 분산이 지배해 아무 말도 못 한다.

CATALYST_ARM_SUFFIX = "_cat"


def base_strategy_id(strategy_id: str) -> str:
    """A/B 촉매 갈래 id(`scalp_1m_cat`) → 기준 전략 id(`scalp_1m`).

    갈래 id 는 settings.yaml 의 **블록 키**이고 두 갈래는 같은 `class:`를 쓴다 —
    즉 접미사를 벗기면 클래스 단위 처리(오버나이트 판정·보호 목록·표시명)가
    그대로 상속된다. 접미사가 없으면 그대로 돌려준다."""
    sid = str(strategy_id or "")
    return sid[: -len(CATALYST_ARM_SUFFIX)] if sid.endswith(CATALYST_ARM_SUFFIX) else sid


def ab_pairs_from_config(cfg: dict) -> list[str]:
    """설정에서 A/B 쌍을 찾는다 → 기준 전략 id 목록(정렬).

    쌍의 정의는 "`<id>` 와 `<id>_cat` 이 **둘 다** `strategies:` 에 있다"이다.
    비활성(enabled: false) 갈래도 포함한다 — 한쪽을 끈 뒤에도 지금까지 쌓인
    표본을 계속 비교해야 "왜 껐는지"가 숫자로 남는다."""
    ids = set((cfg or {}).get("strategies", {}) or {})
    return sorted(
        base_strategy_id(sid) for sid in ids
        if sid.endswith(CATALYST_ARM_SUFFIX) and base_strategy_id(sid) in ids
    )


def _arm_stats(trips: list[dict]) -> dict:
    """한 갈래의 요약. `bps`는 이미 수수료 차감 후다(`round_trips`: pnl = 실현손익 - 수수료)."""
    known = [t for t in trips if t.get("pnl_known")]
    n = len(known)
    wins = sum(1 for t in known if float(t.get("pnl", 0.0)) > 0)
    bps = [float(t.get("bps", 0.0)) for t in known]
    lower, upper = _wilson_ci(wins, n)
    return {
        "n": n,
        "n_unknown": len(trips) - n,
        "wins": wins,
        "win_rate": (wins / n) if n else None,
        "win_ci": (lower, upper),
        "expectancy_bp": (sum(bps) / n) if n else None,
        "net_pnl": sum(float(t.get("pnl", 0.0)) for t in known),
        "fees": sum(float(t.get("fees", 0.0)) for t in known),
        "_bps": bps,
    }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _var(xs: list[float]) -> float:
    """표본분산(자유도 n-1). n<2 면 0."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def _permutation_p(a: list[float], b: list[float], iters: int = 2000, seed: int = 42) -> float | None:
    """평균 차이(a-b)의 양측 순열검정 p값. 표본이 얇으면 None.

    정규성 가정이 필요 없다 — 트립 bp 분포는 꼬리가 두껍고(손절 클러스터 + 드문
    큰 승리) t검정 가정을 만족하지 않는다. **시드를 고정**해 같은 원장에서 같은
    p값이 나오게 한다(리포트가 실행마다 흔들리면 판단 근거로 못 쓴다)."""
    if len(a) < 2 or len(b) < 2:
        return None
    import random

    observed = abs(_mean(a) - _mean(b))
    pool = a + b
    n_a = len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(iters):
        rng.shuffle(pool)
        if abs(_mean(pool[:n_a]) - _mean(pool[n_a:])) >= observed - 1e-12:
            hits += 1
    # (hits+1)/(iters+1) — 관측 자체를 순열 하나로 세는 관례. p=0 을 만들지 않는다.
    return (hits + 1) / (iters + 1)


def ab_compare(
    trips: list[dict], bases: list[str] | None = None,
    min_trips: int = MIN_TRIPS_FOR_JUDGEMENT, permutations: int = 2000,
) -> list[dict]:
    """A/B 갈래 비교표 — (기준 전략, 시장)마다 한 행. **순수 계산, 출력 없음**.

    `bases`는 설정에서 온 기준 id 목록(`ab_pairs_from_config`). 원장에 `_cat`
    트립이 이미 있으면 그것도 자동으로 합친다 — 설정에서 갈래를 지운 뒤에도
    과거 표본은 계속 보인다.

    행 하나:
      base/market/baseline/catalyst/delta_expectancy_bp/delta_ci/p_value/
      judgeable/reason

    `judgeable=False`면 `delta_*`·`p_value`가 전부 None 이고 `reason`이
    "판단 불가(n<30)"다 — 호출부는 그 문구를 그대로 쓴다(임계를 새로 만들지
    않는다: 이 저장소의 표본선은 `MIN_TRIPS_FOR_JUDGEMENT` 하나뿐이다)."""
    by_arm: dict[tuple[str, str], list[dict]] = {}
    for t in trips:
        sid = str(t.get("strategy", "?"))
        by_arm.setdefault((sid, str(t.get("market") or "US")), []).append(t)

    seen_bases = {base_strategy_id(sid) for sid, _ in by_arm if sid.endswith(CATALYST_ARM_SUFFIX)}
    all_bases = sorted(set(bases or []) | seen_bases)

    rows: list[dict] = []
    for base in all_bases:
        cat = base + CATALYST_ARM_SUFFIX
        markets = sorted({m for (sid, m) in by_arm if sid in (base, cat)})
        for market in markets or [None]:
            b_trips = by_arm.get((base, market), []) if market else []
            c_trips = by_arm.get((cat, market), []) if market else []
            baseline, catalyst = _arm_stats(b_trips), _arm_stats(c_trips)
            judgeable = baseline["n"] >= min_trips and catalyst["n"] >= min_trips
            row = {
                "base": base, "catalyst_id": cat, "market": market,
                "baseline": baseline, "catalyst": catalyst,
                "delta_expectancy_bp": None, "delta_ci": None, "p_value": None,
                "judgeable": judgeable,
                "reason": "" if judgeable else f"판단 불가(n<{min_trips})",
            }
            if judgeable:
                a, b = catalyst["_bps"], baseline["_bps"]
                delta = _mean(a) - _mean(b)
                # Welch(등분산 가정 없음) 정규근사 — 두 갈래의 표본 수도 분산도
                # 다를 수밖에 없다(촉매 갈래는 종목이 적다).
                se = math.sqrt(_var(a) / len(a) + _var(b) / len(b))
                row["delta_expectancy_bp"] = delta
                row["delta_ci"] = (delta - _Z_95 * se, delta + _Z_95 * se)
                row["p_value"] = _permutation_p(a, b, iters=permutations)
            rows.append(row)
    return rows


# --- 세션(장 마감 후) 손익 리포트 ---------------------------------------------
# 누적 스코어보드(bps/승률)와 달리 여기는 "이번 세션에 실제 얼마를 벌었나"를
# 실화폐 단위로 보여준다(2026-08-13 사용자 요청). 세션 경계는 시장별 **현지
# 시간대**로 계산한다 — US는 America/New_York이라 서머타임(EDT/EST)이 자동
# 반영된다. KST 고정 클럭으로 US 마감을 하드코딩하면 서머타임 전환기마다
# 최대 1시간이 밀린다(server/CLAUDE.md에 기록된 과거 실수 — 되풀이하지 않는다).

_SESSION_TZ = {"KR": ZoneInfo("Asia/Seoul"), "US": ZoneInfo("America/New_York")}
_SESSION_HOURS = {"KR": (time(9, 0), time(15, 30)), "US": (time(9, 30), time(16, 0))}


def session_window(market: str, on: date) -> tuple[datetime, datetime]:
    """market의 정규장 [개장, 마감]을 그 시장 현지시간대의 tz-aware datetime으로.

    양끝 포함 구간(개장 시각 정각 체결, 마감 시각 정각 체결 모두 세션에 속한다)."""
    if market not in _SESSION_HOURS:
        raise ValueError(f"모르는 시장: {market!r} (KR/US만 지원)")
    tz = _SESSION_TZ[market]
    open_t, close_t = _SESSION_HOURS[market]
    return (datetime.combine(on, open_t, tzinfo=tz), datetime.combine(on, close_t, tzinfo=tz))


def trades_in_session(trades: list[dict], market: str, on: date) -> list[dict]:
    """market·on 세션 구간(양끝 포함) 안의, 그 시장 체결만 골라낸다."""
    start, end = session_window(market, on)
    out: list[dict] = []
    for t in trades:
        if str(t.get("market")) != market:
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # 방어적 — 원장은 항상 오프셋 포함으로 기록
        if start <= ts <= end:
            out.append(t)
    return out


def session_cash_delta_krw(all_trades: list[dict], session_trades: list[dict]) -> tuple[float | None, int]:
    """세션 체결들이 계좌 현금(KRW)에 미친 변화 합계와, 계산 불가 건수.

    cash_after는 체결 직후 현금 스냅샷(paper 브로커, 시장 무관 항상 KRW —
    PaperBroker.place_order가 to_krw로 변환한 뒤의 값)이라 그 자체로는 델타가
    아니다. 체결 하나의 기여분은 "그 체결 직후 cash_after − 전체 원장(시장 무관)
    기준 그 직전 체결의 cash_after"로 구한다. 라이브(Toss) 체결은 cash_after가
    없어(TossBroker._build_fill 참고) 계산 불가로 잡힌다 — 0으로 위장하지 않는다.
    """
    ordered = sorted(all_trades, key=lambda x: str(x.get("ts", "")))
    delta_of: dict[int, float] = {}
    prev_cash: float | None = None
    for t in ordered:
        cash_after = t.get("cash_after")
        if cash_after is not None:
            if prev_cash is not None:
                delta_of[id(t)] = float(cash_after) - prev_cash
            prev_cash = float(cash_after)
    total = 0.0
    unknown = 0
    for t in session_trades:
        d = delta_of.get(id(t))
        if d is None:
            unknown += 1
        else:
            total += d
    known = len(session_trades) - unknown
    return (total if known > 0 else None), unknown


def session_cash_delta_usd(all_trades: list[dict], session_trades: list[dict]) -> tuple[float | None, int]:
    """세션 체결들이 **USD 현금 풀**에 미친 변화 합계와, 계산 불가 건수.

    `session_cash_delta_krw`와 같은 알고리즘을 `cash_after_usd`(2026-09-02 추가)에
    적용한 것뿐이다. KR 체결도 이 필드를 남기지만 USD 풀을 건드리지 않으므로
    연속 두 값의 차가 0이라 합계에 기여하지 않는다 — 시장으로 거르지 않아도
    맞는다(오히려 걸러내면 KR 체결을 사이에 낀 US 체결의 델타를 잃는다).

    `cash_after_usd`가 하나도 없으면(구 형식 원장, dual_currency=False) None —
    0으로 위장하지 않는다."""
    ordered = sorted(all_trades, key=lambda x: str(x.get("ts", "")))
    delta_of: dict[int, float] = {}
    prev_cash: float | None = None
    for t in ordered:
        cash_after = t.get("cash_after_usd")
        if cash_after is not None:
            if prev_cash is not None:
                delta_of[id(t)] = float(cash_after) - prev_cash
            prev_cash = float(cash_after)
    total = 0.0
    unknown = 0
    for t in session_trades:
        d = delta_of.get(id(t))
        if d is None:
            unknown += 1
        else:
            total += d
    known = len(session_trades) - unknown
    return (total if known > 0 else None), unknown


def session_pnl_summary(all_trades: list[dict], market: str, on: date) -> dict:
    """market·on 세션의 실현손익/수수료/현금변화 요약(구조화 dict, 통화 무관 축 없음).

    `all_trades`는 원장 **전체**(시장 무관)를 받는다 — session_cash_delta_krw가
    세션 밖 체결까지 봐야 정확한 델타를 계산할 수 있어서다.

    **실계좌 이식 정리 매도는 빼고 센다**(2026-09-02, `is_seeding_liquidation`).
    프로그램의 매매 판단이 아닌 일회성 정리라서다 — 안 빼면 2026-09-01 US 세션이
    프로그램 매매 -$102.90 인데 -$1,214.65 로 발송된다(실측). 다만 조용히
    버리지는 않는다: 건수와 총액을 `excluded_seeding`에 담아 텍스트가 한 줄로
    밝힌다."""
    in_window = trades_in_session(all_trades, market, on)
    session = [t for t in in_window if not is_seeding_liquidation(t)]
    seeding = [t for t in in_window if is_seeding_liquidation(t)]
    sells = [t for t in session if str(t.get("side", "")).upper() != "BUY"]
    buys = [t for t in session if str(t.get("side", "")).upper() == "BUY"]

    def _bucket(rows: list[dict], key) -> dict[str, dict]:
        # 알려진 한계(F8, 2026-09-02): 진입 수수료는 **매수한 세션**에 잡히고
        # 실현손익은 청산한 세션에 잡힌다 — 세션을 걸쳐 보유한 왕복은 두 세션의
        # 전략별 순손익이 각각 그만큼 어긋난다(누적 스코어보드 `round_trips`는
        # 트립 단위라 이 문제가 없다).
        out: dict[str, dict] = {}
        for t in rows:
            k = str(key(t))
            b = out.setdefault(k, {"gross": 0.0, "fees": 0.0, "unknown": 0, "n": 0})
            b["n"] += 1
            b["fees"] += float(t.get("fee", 0) or 0)
            if str(t.get("side", "")).upper() != "BUY":
                if t.get("realized_pnl") is not None:
                    b["gross"] += float(t["realized_pnl"])
                else:
                    b["unknown"] += 1
        return out

    known_sells = [t for t in sells if t.get("realized_pnl") is not None]
    unknown_sells = len(sells) - len(known_sells)
    gross = sum(float(t["realized_pnl"]) for t in known_sells)
    fees = sum(float(t.get("fee", 0) or 0) for t in session)
    cash_delta, cash_unknown = session_cash_delta_krw(all_trades, session)
    cash_delta_usd, cash_usd_unknown = session_cash_delta_usd(all_trades, session)

    return {
        "market": market,
        "date": on,
        "has_trades": len(session) > 0,
        "n_fills": len(session),
        "n_buys": len(buys),
        "n_sells": len(sells),
        "gross_realized": gross,
        "fees": fees,
        "net_realized": gross - fees,
        "unknown_sells": unknown_sells,
        "cash_delta_krw": cash_delta,
        "cash_delta_unknown": cash_unknown,
        "cash_delta_usd": cash_delta_usd,
        "cash_delta_usd_unknown": cash_usd_unknown,
        # 이식 정리 매도 — 성과에서 뺐다는 사실 자체를 숨기지 않기 위한 버킷.
        "excluded_seeding": {
            "n": len(seeding),
            "gross": sum(
                float(t["realized_pnl"]) for t in seeding if t.get("realized_pnl") is not None
            ),
            "fees": sum(float(t.get("fee", 0) or 0) for t in seeding),
        },
        "by_strategy": _bucket(session, lambda t: t.get("strategy_id", "?")),
        "by_symbol": _bucket(session, lambda t: t["symbol"]),
    }


def session_pnl_text(summary: dict) -> str:
    """session_pnl_summary()의 구조화 결과를 사람이 읽는 텍스트로."""
    market, on = summary["market"], summary["date"]
    start, end = session_window(market, on)
    lines = [
        f"💰 세션 손익 — {market} {on.isoformat()}",
        f"({start.strftime('%Y-%m-%d %H:%M %Z')} ~ {end.strftime('%H:%M %Z')})",
        "",
    ]
    # 이식 정리 제외 사실은 거래가 있든 없든 항상 밝힌다 — 조용히 빼면 원장 총액과
    # 리포트가 안 맞는 이유를 아무도 모른다(2026-09-02).
    seeding = summary.get("excluded_seeding") or {}
    seeding_line = None
    if seeding.get("n"):
        net_seed = float(seeding["gross"]) - float(seeding["fees"])
        seeding_line = (
            f"이식 정리 {seeding['n']}건 제외: {_fmt_amount(net_seed, market)}"
            " (프로그램 매매 아님 — 실계좌 이식 시 물려받은 레거시 청산)"
        )

    if not summary["has_trades"]:
        lines.append("이 세션에 체결된 거래 없음")
        if seeding_line:
            lines.append(seeding_line)
        return "\n".join(lines)

    lines.append(f"체결 {summary['n_fills']}건 (매수 {summary['n_buys']} · 매도 {summary['n_sells']})")
    unk = summary["unknown_sells"]
    lines.append(
        f"실현손익(수수료 전) {_fmt_amount(summary['gross_realized'], market)}"
        + (f" (손익미상 매도 {unk}건 제외)" if unk else "")
    )
    lines.append(f"수수료 합계 {_fmt_amount(summary['fees'], market)}")
    lines.append(f"실현손익(순, 수수료 차감) {_fmt_amount(summary['net_realized'], market)}")
    if seeding_line:
        lines.append(seeding_line)
    lines.append("")

    if market == "US":
        # US 체결은 KRW 풀을 건드리지 않는다(dual_currency 지갑 분리) — KRW 델타를
        # 찍으면 "+0원"이라는 거짓이 나간다(2026-09-02 실측). USD 풀로 답한다.
        cash_delta = summary.get("cash_delta_usd")
        cu = summary.get("cash_delta_usd_unknown", 0)
        if cash_delta is not None:
            lines.append(
                f"계좌 USD 현금 변화 ${cash_delta:+,.2f}"
                + (f" (계산불가 {cu}건 제외)" if cu else "")
            )
        else:
            lines.append(
                "계좌 USD 현금 변화: 집계 불가(구 형식 — cash_after_usd 없는 원장 행)"
            )
    else:
        cash_delta = summary["cash_delta_krw"]
        cu = summary["cash_delta_unknown"]
        if cash_delta is not None:
            lines.append(
                f"계좌 현금 변화(KRW, paper 브로커 체결시점 환산 반영) {cash_delta:+,.0f}원"
                + (f" (계산불가 {cu}건 제외)" if cu else "")
            )
        else:
            lines.append("계좌 현금 변화: 계산 불가 (cash_after 없음 — 라이브 체결이거나 원장에 없음)")
    lines.append("")

    lines.append("전략별:")
    for sid, d in sorted(summary["by_strategy"].items()):
        net_s = d["gross"] - d["fees"]
        lines.append(
            f"  [{sid}] {d['n']}건 · 순손익 {_fmt_amount(net_s, market)}"
            + (f" (손익미상 매도 {d['unknown']}건 제외)" if d["unknown"] else "")
        )
    lines.append("")

    lines.append("종목별:")
    ranked = sorted(summary["by_symbol"].items(), key=lambda kv: kv[1]["gross"] - kv[1]["fees"], reverse=True)
    for sym, d in ranked:
        net_s = d["gross"] - d["fees"]
        lines.append(
            f"  {sym}: {d['n']}건 · 순손익 {_fmt_amount(net_s, market)}"
            + (f" (손익미상 매도 {d['unknown']}건 제외)" if d["unknown"] else "")
        )

    return "\n".join(lines)


# --- 갈래 A/B 승격 게이트 (2026-08-17, spec §5/§4) -----------------------------
# `docs/superpowers/specs/2026-08-17-quant-automation-design.md` §4: "숫자가
# 자본을 배분한다 — 자동 승격 없음, 판정만 자동." 아래 두 함수는 **판정만**
# 돌려준다 — capital_fraction을 고치거나 enabled를 켜지 않는다.
#
# 왜 `quant/control/leaderboard.py`가 아니라 여기인가: leaderboard.py는 판단
# 품질(생산자별 순위 IC) 리더보드다 — LLM/결정론 스코어러가 "어느 종목이 더
# 오를지"를 얼마나 잘 맞췄는지를 잰다. A/B의 승격 기준(거래일 수·수수료 후
# bp)은 판단 품질이 아니라 **실제 체결 성과**를 재는 것이라 이 원장(trade
# ledger) 모듈의 기존 표본 부족 경고(`MIN_TRIPS_FOR_JUDGEMENT`,
# `_strategy_block`)와 같은 성격이다 — 구조상 이쪽이 더 정직한 위치다.

# B(frgn_accumulate) 승격 최소 거래일 — spec §5 사용자 결정.
MIN_TRADING_DAYS_FRGN = 20


def strategy_trading_days(trades: list[dict], strategy_id: str) -> int:
    """전략의 원장에 체결이 있는 **거래일 수**(trading_day() 경계, KST 08:00) —
    B 승격 게이트가 쓰는 표본 단위다. 라운드트립(트립) 수가 아니라 거래일 수를
    세는 이유: 적립 전략은 하루에 여러 번 매수해도(태그가 유지되는 한 계속) 그날의
    "판단 기회"는 하루 1번뿐이다 — 트립 수를 세면 매수 체결마다 표본이 부풀려진다.
    """
    from quant.core.models import trading_day

    days: set = set()
    for t in trades:
        if str(t.get("strategy_id")) != strategy_id:
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)  # 방어적 — 원장은 항상 오프셋 포함으로 기록
        days.add(trading_day(ts))
    return len(days)


def frgn_accumulate_promotion_verdict(
    trades: list[dict], strategy_id: str = "frgn_accumulate",
    min_days: int = MIN_TRADING_DAYS_FRGN,
) -> dict:
    """갈래 B 승격 판정 — paper 원장 거래일 수만 본다(spec §5: "B: paper 원장
    20거래일"). **판정만 반환, 자동 승격 없음** — capital_fraction/enabled는
    사람이(오케스트레이터가) 바꾼다."""
    n = strategy_trading_days(trades, strategy_id)
    if n >= min_days:
        return {
            "strategy": strategy_id, "promote": True, "n_trading_days": n,
            "reason": f"paper 거래일 {n}/{min_days}일 — 승격 판정 가능(사용자 결정 필요)",
        }
    return {
        "strategy": strategy_id, "promote": False, "n_trading_days": n,
        "reason": f"paper 거래일 {n}/{min_days}일 — 아직 미달",
    }


def news_scalp_promotion_verdict(
    aggregate: dict, round_trip_fee_bps: float, min_n_symbol_days: int = 30,
) -> dict:
    """갈래 A 승격 판정 — `quant.backtest.intraday_verify.aggregate_metrics()`의
    출력을 받아 수수료 차감 후 평균 bp를 판정한다(spec §5: "A: intraday_verify
    n>=30 & 수수료 후 평균 bp>0"). **판정만 반환, 자동 승격 없음.**

    `aggregate`를 dict로만 받고 `quant.backtest.intraday_verify`를 임포트하지
    않는다 — `quant/control/`이 `quant/backtest/`(이 저장소 평면 밖 실험 코드)를
    몰라도 되게 하는 의도적 결합 축소다. 호출부(리포팅 레이어)가
    `aggregate_metrics()` 출력을 그대로 넘기면 된다.

    `round_trip_fee_bps`는 호출부가 넘긴다 — `aggregate_metrics()`의
    `avg_open_close_bp`는 **수수료 전** 총(gross) bp이고(intraday_verify.py의
    `symbol_outcome` 참고), 실제 왕복비용(진입+청산 수수료, KR 개별주 매도
    거래세 포함)은 `config/settings.yaml`의 `execution.fee_bps`/
    `kr_stock_sell_tax_bps`에 있다 — 이 함수는 순수 판정 로직이라 그 설정을
    직접 읽지 않는다.
    """
    n = aggregate.get("n_symbol_days") or 0
    avg_bp = aggregate.get("avg_open_close_bp")
    if n < min_n_symbol_days or avg_bp is None:
        return {
            "promote": False, "n_symbol_days": n, "net_bp": None,
            "reason": f"표본 부족 (종목-일 {n}/{min_n_symbol_days}) — 승격 판단 불가",
        }
    net_bp = avg_bp - round_trip_fee_bps
    if net_bp > 0:
        return {
            "promote": True, "n_symbol_days": n, "net_bp": net_bp,
            "reason": (
                f"종목-일 {n}건, 수수료({round_trip_fee_bps:g}bp) 차감 후 평균 "
                f"{net_bp:+.1f}bp — 승격 판정 가능(사용자 결정 필요)"
            ),
        }
    return {
        "promote": False, "n_symbol_days": n, "net_bp": net_bp,
        "reason": (
            f"종목-일 {n}건, 수수료({round_trip_fee_bps:g}bp) 차감 후 평균 "
            f"{net_bp:+.1f}bp <= 0 — 승격 근거 없음"
        ),
    }


def filter_recent(trips: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def _ts(t: dict) -> datetime | None:
        try:
            d = datetime.fromisoformat(str(t.get("exit_ts")))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    return [t for t in trips if (ts := _ts(t)) is not None and ts >= cutoff]


def load_equity_curve_rows(path: Path | str = DEFAULT_EQUITY_CURVE_PATH) -> list[dict]:
    """`cli equity-snapshot`이 append하는 원장을 읽는다. 깨진 줄은 건너뛴다 —
    `load_trades`와 같은 계약(원장 일부 손상이 전체 리포트를 죽이면 안 된다)."""
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("date") and row.get("market"):
                out.append(row)
        except ValueError:
            continue
    return out


def daily_equity_series_by_market(
    path: Path | str = DEFAULT_EQUITY_CURVE_PATH,
) -> dict[str, pd.Series]:
    """자본 곡선 원장 → 시장별 일별 자본(KRW) pd.Series (quantstats 티어시트용).

    이 파일이 quant/research/가 아니라 여기(control/ledger.py)에 있는 이유: 이 저장소의
    quant/research/ 모듈(walkforward.py, pathstats.py 등)은 전부 **순수 계산**만 하고
    파일을 직접 읽지 않는다(호출부가 데이터를 만들어 넘긴다) — 이 함수는 반대로
    JSONL을 직접 열어 읽는 I/O 코드라서, 파일 I/O를 이미 하고 있는 이 모듈
    (load_trades와 같은 결)에 넣는 게 기존 관례에 맞는다.

    하루에 여러 번 마크가 찍힐 수 있다(장중 재실행 등, 예: 2026-08-24 KR 03:20 +
    15:40). 같은 (market, date)는 **원장에 쓰인 순서상 마지막 것이 이긴다** —
    `cmd_performance`/`cmd_equity_snapshot`/`control.alpha.daily_returns`가 이미 쓰는
    것과 동일한 관례("재실행은 append이지 덮어쓰기가 아니고, 읽는 쪽이 마지막만
    쓴다"). pandas로는 groupby(date).last()가 그 관례를 그대로 구현한다 — JSONL이
    항상 시간순으로 append되므로 원본 순서를 보존하는 groupby().last()가 정확히
    "마지막 기록"이 된다.

    결측일(휴장일 포함)은 채우지 않는다 — 원장에 없는 날은 시리즈에도 없다.
    total_krw가 없거나 0 이하인 행은 깨진 마크로 보고 제외한다(alpha.py의
    `_as_float` 필터와 동일한 판단).
    """
    rows = load_equity_curve_rows(path)
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    df = df[pd.to_numeric(df["total_krw"], errors="coerce") > 0]
    if df.empty:
        return {}
    df = df.assign(date=pd.to_datetime(df["date"], errors="coerce"))
    df = df.dropna(subset=["date"])

    out: dict[str, pd.Series] = {}
    for market, g in df.groupby("market"):
        s = g.groupby("date")["total_krw"].last().sort_index()
        s = s.astype(float)
        s.index.name = "date"
        out[str(market)] = s
    return out


def daily_benchmark_series_by_market(
    path: Path | str = DEFAULT_EQUITY_CURVE_PATH,
) -> dict[str, pd.Series]:
    """자본 곡선 원장의 동반 기록(`benchmark_close`, 2026-08-28~) → 시장별 일별
    벤치마크 종가 pd.Series. `daily_equity_series_by_market`와 같은
    groupby(date).last() 원칙 + 결측일 미보간을 따른다.

    `benchmark_close`가 없거나(구버전 행) 0 이하인 행은 그 날짜에 기여하지
    않는다 — 없는 벤치마크 값을 지어내지 않는다."""
    rows = load_equity_curve_rows(path)
    if not rows:
        return {}
    df = pd.DataFrame(rows)
    if "benchmark_close" not in df.columns:
        return {}
    df = df[pd.to_numeric(df["benchmark_close"], errors="coerce") > 0]
    if df.empty:
        return {}
    df = df.assign(date=pd.to_datetime(df["date"], errors="coerce"))
    df = df.dropna(subset=["date"])

    out: dict[str, pd.Series] = {}
    for market, g in df.groupby("market"):
        s = g.groupby("date")["benchmark_close"].last().sort_index()
        s = s.astype(float)
        s.index.name = "date"
        out[str(market)] = s
    return out
