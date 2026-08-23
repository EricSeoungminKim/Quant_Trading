"""전략별 독립 명목계좌(각 1,000만원) 성과 요약 — 2026-08-19 사용자 요청.

`data/state/strategy_books.json`은 리스크 평면(별도 작업자, `quant/trade/risk/
books.py`)이 쓴다. **이 모듈은 읽기 전용이다** — 쓰지 않는다.

계좌 전체 손익 하나로 뭉치는 `session_pnl_text`/`scoreboard_text`와 달리 이 모듈은
"어느 전략이 이기고 있는가"에 답한다 — 전략마다 평가금액·수익률·실현/미실현손익·
보유종목·거래수/승률을 나눠 보여준다.

순수 함수만 둔다(네트워크 없음) — 시세 조회는 호출부(`quant.apps.cli`)가 해서
`quotes`/`usd_krw`로 주입한다. 장부 파일이 없거나 비어 있어도 예외를 던지지
않고 "장부 없음"을 명시적으로 반환한다 — 조용히 0으로 채우지 않는다
(병렬 작업자 계약: 다른 세션이 아직 파일을 만드는 중일 수 있다).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT

logger = logging.getLogger(__name__)

DEFAULT_BOOKS_PATH = Path("data/state/strategy_books.json")
_DEFAULT_INITIAL_KRW = 10_000_000.0

# 텔레그램 메시지 경계 구분자(2026-08-19 Phase C). CLI(quant.apps.cli
# cmd_strategy_pnl)는 이 문자로 메시지들을 이어 stdout에 한 번에 찍고,
# server/scripts/session_pnl.sh가 이 문자로 다시 잘라 메시지마다 따로 보낸다.
# 사람이 쓰는 텍스트(원화 금액·종목명·전략명)에 절대 나오지 않는 제어문자
# (ASCII Record Separator)라 구분자 충돌을 걱정할 필요가 없다.
MESSAGE_SEPARATOR = "\x1e"


def load_strategy_books(path: Path | str = DEFAULT_BOOKS_PATH) -> dict:
    """장부 파일을 읽는다. 파일이 없거나/비었거나/깨졌어도 예외 없이 상태를 반환한다.

    반환: {"exists": bool, "books": dict[str, dict], "initial_krw": float,
    "error": str | None}. `exists=False`는 "아직 파일 없음"(다른 작업자가 만드는
    중일 수 있음), `error`는 "파일은 있는데 읽기/파싱 실패"를 구분한다.
    """
    path = Path(path)
    if not path.exists():
        return {"exists": False, "books": {}, "initial_krw": _DEFAULT_INITIAL_KRW, "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        return {
            "exists": True, "books": {}, "initial_krw": _DEFAULT_INITIAL_KRW,
            "error": f"{type(e).__name__}: {e}",
        }
    if not isinstance(raw, dict):
        return {
            "exists": True, "books": {}, "initial_krw": _DEFAULT_INITIAL_KRW,
            "error": "장부 파일 최상위가 객체가 아님",
        }
    books = raw.get("books")
    if not isinstance(books, dict):
        books = {}
    initial = raw.get("initial_krw", _DEFAULT_INITIAL_KRW)
    try:
        initial = float(initial)
    except (TypeError, ValueError):
        initial = _DEFAULT_INITIAL_KRW
    return {"exists": True, "books": books, "initial_krw": initial, "error": None}


def _position_krw(qty: float, per_unit_krw: float, market: str, usd_krw: float) -> float:
    v = qty * per_unit_krw
    return v if market == "KR" else v * usd_krw


def strategy_equity(book: dict, quotes: dict[str, float], usd_krw: float) -> dict:
    """book(cash_krw, positions) → 평가금액(KRW) + 보유종목별 평가/평가손익.

    가격을 모르는 포지션(시세 조회 실패)은 합계에서 제외하고 `missing_quotes`에
    기록한다 — "모르면 0으로 위장하지 않는다"(원장 모듈과 같은 원칙).
    """
    cash = float(book.get("cash_krw", 0) or 0)
    positions = book.get("positions") or {}
    equity = cash
    unrealized = 0.0
    rows: list[dict] = []
    missing: list[str] = []
    for sym, p in positions.items():
        qty = float(p.get("qty", 0) or 0)
        if qty == 0:
            continue
        market = str(p.get("market") or "KR")
        avg_cost = float(p.get("avg_cost", 0) or 0)
        price = quotes.get(sym)
        if price is None:
            missing.append(sym)
            rows.append({
                "symbol": sym, "qty": qty, "market": market, "avg_cost": avg_cost,
                "price": None, "market_value_krw": None, "unrealized_pnl_krw": None,
            })
            continue
        mv = _position_krw(qty, price, market, usd_krw)
        upnl = _position_krw(qty, price - avg_cost, market, usd_krw)
        equity += mv
        unrealized += upnl
        rows.append({
            "symbol": sym, "qty": qty, "market": market, "avg_cost": avg_cost,
            "price": price, "market_value_krw": mv, "unrealized_pnl_krw": upnl,
        })
    return {
        "cash_krw": cash, "equity_krw": equity, "unrealized_pnl_krw": unrealized,
        # 주식에 들어가 있는 금액(평가액 기준). 시세를 모르는 종목은 equity 에서
        # 빠져 있으므로 여기서도 자동으로 빠진다 — 0으로 위장하지 않는다.
        "stock_value_krw": equity - cash,
        "positions": rows, "missing_quotes": missing,
    }


def strategy_trade_stats(trips: list[dict], strategy_id: str) -> dict:
    """`ledger.round_trips()` 출력에서 이 전략 몫만 뽑아 거래수/승률.

    표본 0건은 승률을 0%로 찍지 않는다 — "아직 거래 없음"과 "전부 패배"는
    다른 정보이고, 전자를 후자처럼 보이면 오해를 만든다(사용자 지침).
    """
    mine = [t for t in trips if str(t.get("strategy")) == strategy_id]
    known = [t for t in mine if t.get("pnl_known")]
    n = len(known)
    wins = sum(1 for t in known if t["pnl"] > 0)
    return {
        "n_trades": n,
        "wins": wins,
        "win_rate": (wins / n) if n else None,
        "n_unknown": len(mine) - n,
    }


def strategy_records(books_data: dict, trips: list[dict], quotes: dict[str, float],
                      usd_krw: float) -> list[dict]:
    """전략별 요약 레코드(정렬 안 된 채) — `strategy_books_messages`가 정렬해 쓴다.

    시작금은 **전략별로** 다를 수 있다(2026-08-19 Phase B `capital_policy:
    equal_split` — 전략이 서로 다른 시점에 생겨 서로 다른 분모로 시작할 수
    있다). `book["initial_krw"]`(장부 생성 시점에 못박힌 값, `risk/books.py`
    `_ensure` 참고)를 우선 쓰고, 없으면(과거 파일·손으로 만든 픽스처) 컨테이너
    전체 기본값 `books_data["initial_krw"]`로 저하(degrade)한다 — `fixed`
    정책에서는 모든 장부가 같은 값을 못박으므로 이 저하가 발동하지 않는다.
    """
    default_initial = books_data["initial_krw"]
    out: list[dict] = []
    for sid, book in books_data["books"].items():
        initial = float(book.get("initial_krw", default_initial) or default_initial)
        eq = strategy_equity(book, quotes, usd_krw)
        stats = strategy_trade_stats(trips, sid)
        equity = eq["equity_krw"]
        return_pct = (equity / initial - 1) * 100 if initial else 0.0
        out.append({
            "strategy": sid,
            "equity_krw": equity,
            "cash_krw": eq["cash_krw"],
            "stock_value_krw": eq["stock_value_krw"],
            "initial_krw": initial,
            "return_pct": return_pct,
            "realized_pnl_krw": float(book.get("realized_pnl_krw", 0) or 0),
            "fees_krw": float(book.get("fees_krw", 0) or 0),
            "unrealized_pnl_krw": eq["unrealized_pnl_krw"],
            "positions": eq["positions"],
            "missing_quotes": eq["missing_quotes"],
            **stats,
        })
    return out


def _is_idle(r: dict) -> bool:
    """활동도 보유도 없는 전략 — 요약 한 줄로 접을 대상(2026-08-19 Phase C).

    "활동 없음" = 이 전략의 거래가 원장에 (손익 판정 가능/불가 포함) 단 한 건도
    없다는 뜻이다. n_trades/n_unknown이 둘 다 0이고 보유 종목도 없으면, 이
    전략은 시작금 그대로 멈춰 있다는 뜻이라 매 세션 따로 메시지를 보낼 정보가
    없다(사용자 지침: "유저가 봤을 때 이해가 안 되면 그건 필요없는 정보").
    """
    return not r["positions"] and r["n_trades"] == 0 and r["n_unknown"] == 0


def strategy_summary_message(ranked: list[dict], quote_error: str | None = None) -> str:
    """순위 + 합계(총 평가금액·총 실현손익) 한 줄 요약 메시지.

    활동·보유가 없는 전략은 여기 이름만 한 줄로 접힌다 — 상세 메시지는
    `strategy_detail_message`가 활동 있는 전략에만 따로 만든다.
    """
    initials = {r["initial_krw"] for r in ranked}
    lines = ["📊 전략별 성과 요약", ""]
    if len(initials) == 1:
        lines.append(f"전략마다 {next(iter(initials)):,.0f}원 시작 · {len(ranked)}개 전략")
    else:
        # capital_policy: equal_split — 전략이 서로 다른 시점에 생겨 시작금이 다르다.
        lines.append(f"전략별 시작금 상이(아래 상세 참고) · {len(ranked)}개 전략")
    lines.append("")
    lines.append("순위: " + " > ".join(f"{r['strategy']} {r['return_pct']:+.1f}%" for r in ranked))
    # 명목계정은 도입 시점부터 0원에서 출발하는데 거래수·승률은 그 이전 원장까지
    # 포함한 누적이다. 그대로 두면 상세 메시지의 "누적 거래 21건 · 승률 29%"가
    # "실현손익 +0원"과 나란히 찍혀 서로 모순돼 보인다 — 무엇이 어느 구간인지
    # 요약에서 한 줄로 밝힌다.
    lines.append("(수익률·손익은 명목계정 시작 이후 · 거래수·승률은 이전 원장 포함 누적)")
    lines.append("")
    total_equity = sum(r["equity_krw"] for r in ranked)
    total_realized = sum(r["realized_pnl_krw"] for r in ranked)
    total_fees = sum(r["fees_krw"] for r in ranked)
    # "총 실현손익"이라고만 쓰면 session-pnl의 "순손익"(수수료 차감 후)과 이름이
    # 갈려 두 명령의 숫자가 서로 안 맞는 것처럼 보인다(2026-08-19 코디네이터
    # 확증 — 장부의 realized_pnl_krw는 수수료 전, session-pnl의 표시값은 수수료
    # 후). session_pnl_text()가 쓰는 어휘를 그대로 가져와 "(수수료 전)"을 밝히고,
    # 합계도 검산할 수 있게 총 수수료를 같이 낸다.
    lines.append(
        f"총 평가금액 {total_equity:,.0f}원 · 총 실현손익(수수료 전) {total_realized:+,.0f}원"
        f" · 총 수수료 {total_fees:,.0f}원"
    )

    idle = [r["strategy"] for r in ranked if _is_idle(r)]
    if idle:
        lines.append("")
        lines.append(f"거래·보유 없음(상세 생략): {', '.join(idle)}")

    if quote_error:
        lines.append("")
        lines.append(f"(시세 조회 참고: {quote_error})")

    return "\n".join(lines).rstrip()


def strategy_detail_message(r: dict) -> str:
    """전략 하나의 상세 메시지 — 시작금 → 현금+주식 → 평가/수익률 → 실현·미실현손익
    → 보유종목 → 거래수·승률 순(2026-08-19 Phase C, 전략마다 별도 메시지).

    실현손익 줄(2026-08-19 코디네이터 확증 결함 수정): 장부의 `realized_pnl_krw`는
    **수수료 전** 값이고 수수료는 `fees_krw`에 따로 쌓인다(스키마는 안 건드린다 —
    이미 쌓인 장부와 어긋난다). 예전엔 이 줄에 수수료가 안 보여서 "시작+실현-수수료
    +미실현=평가"를 독자가 검산할 수 없었다(EC2 실측: orb_scan 이 딱 수수료
    25,169원만큼 안 맞아 보였다). `quant.control.ledger.session_pnl_text`가 이미
    쓰는 어휘("실현손익(수수료 전)"/"수수료 합계"/"실현손익(순, 수수료 차감)")를
    그대로 가져온다 — 새 어휘를 만들면 두 명령이 같은 숫자를 다른 이름으로 불러
    또 안 맞아 보이게 된다.
    """
    lines = [f"📈 [{r['strategy']}]", ""]
    lines.append(f"시작 {r['initial_krw']:,.0f}원 → 평가 {r['equity_krw']:,.0f}원 ({r['return_pct']:+.2f}%)")
    lines.append(f"현금 {r['cash_krw']:,.0f}원 + 주식 {r['stock_value_krw']:,.0f}원")
    fees = r["fees_krw"]
    net_realized = r["realized_pnl_krw"] - fees
    lines.append(
        f"실현손익(수수료 전) {r['realized_pnl_krw']:+,.0f}원 · 수수료 합계 {fees:,.0f}원 · "
        f"실현손익(순, 수수료 차감) {net_realized:+,.0f}원"
    )
    unreal_note = " (일부 시세 조회 실패 — 미실현손익 미포함)" if r["missing_quotes"] else ""
    lines.append(f"미실현손익 {r['unrealized_pnl_krw']:+,.0f}원{unreal_note}")
    lines.append("")

    if r["positions"]:
        lines.append("보유 종목:")
        for p in r["positions"]:
            if p["market_value_krw"] is None:
                lines.append(f"  - {p['symbol']}: 시세 조회 실패 (평가금액 제외)")
            else:
                lines.append(
                    f"  - {p['symbol']} {p['qty']:g}주 · 평가 {p['market_value_krw']:,.0f}원"
                    f" ({p['unrealized_pnl_krw']:+,.0f}원)"
                )
    else:
        lines.append("보유 종목 없음")
    lines.append("")

    unk = f" (손익미상 {r['n_unknown']}건 제외)" if r["n_unknown"] else ""
    if r["n_trades"] == 0:
        lines.append(f"누적 거래: 아직 거래 없음{unk}")
    else:
        # n<30(원장 스코어보드와 같은 임계, MIN_TRIPS_FOR_JUDGEMENT)이면 승률
        # 0%/100%가 정보가 아니라 잡음이다 — 표본 부족을 곁들여 "아직 판단하기엔
        # 이르다"를 명시한다(사용자 지침: 이해 안 되면 필요없는 정보). 승률
        # 자체는 지우지 않는다 — 숨기면 그것대로 궁금해진다.
        sample_note = f" (표본 부족, {MIN_TRIPS_FOR_JUDGEMENT}건 미만)" if r["n_trades"] < MIN_TRIPS_FOR_JUDGEMENT else ""
        lines.append(f"누적 거래 {r['n_trades']}건 · 승률 {r['win_rate']:.0%}{sample_note}{unk}")

    return "\n".join(lines).rstrip()


def strategy_books_messages(books_data: dict, trips: list[dict], quotes: dict[str, float],
                             usd_krw: float, quote_error: str | None = None) -> list[str]:
    """전략별 성과를 텔레그램으로 보낼 **메시지 리스트**로 조립한다(2026-08-19
    Phase C, 사용자 요청: "텔레그램 챗으로 오는 것도 전략마다 오는 걸로").

    [0]은 순위+합계 요약, 그 뒤로 활동(거래) 또는 보유가 있는 전략마다 상세
    메시지 1건씩. 활동도 보유도 없는 전략은 요약의 한 줄로 접힌다 — 전부
    "+0.00%"인 유휴 전략 10개가 따로 오면 알림 피로만 늘어난다(사용자 지침:
    "너는 알아도, 유저가 봤을 때 이해가 안 되면 그건 필요없는 정보가 돼").

    순수 함수(네트워크 없음) — 발송(I/O)은 호출부(server/scripts/session_pnl.sh)의
    몫이다. `MESSAGE_SEPARATOR`로 join해 stdout에 찍는 건 CLI(`quant.apps.cli
    cmd_strategy_pnl`)가 한다.
    """
    if not books_data["exists"]:
        return ["📊 전략별 성과: 장부 파일 없음 (data/state/strategy_books.json) — 아직 생성되지 않음"]
    if books_data.get("error"):
        return [f"📊 전략별 성과: 장부 읽기 실패 ({books_data['error']})"]
    if not books_data["books"]:
        return ["📊 전략별 성과: 장부는 있으나 등록된 전략 없음"]

    records = strategy_records(books_data, trips, quotes, usd_krw)
    ranked = sorted(records, key=lambda r: r["return_pct"], reverse=True)

    messages = [strategy_summary_message(ranked, quote_error=quote_error)]
    messages.extend(strategy_detail_message(r) for r in ranked if not _is_idle(r))
    return messages
