"""리포트 날짜별 회고 카드 — 순수 함수만 (I/O는 `quant/apps/report_cli.py`).

## 왜 만드나 (2026-09-06 소유자 지시, 실전화 계획 1단계)

`report_accuracy.py`(2026-09-06)는 **집계** — 여러 날짜를 모아 방향콜 적중률/IC를
낸다. 소유자 지시는 다르다: "과거 리포트를 **날짜별로 하나씩** 보며 그날 리포트가
말한 것과 실제가 맞았는지, 그날 놓친 급등주가 뭔지"를 확인하라는 것이다. 집계
숫자는 "58% 적중"처럼 뭉뚱그려지지만, 날짜별 카드는 "8/25 KR: 스탠스는 하락을
불렀는데 실제로도 -3% — 적중. 그런데 언급 종목 다수가 그날 반도체 급락에
같이 쓸렸다 — 스탠스 적중이 종목 후보 적중을 보장하지 않는다"처럼 **구체적** 오류
패턴을 드러낸다. 이 모듈은 그 카드 한 장을 만드는 순수 계산부다.

## 이 모듈이 다루는 지평

`report_accuracy.py`는 방향콜/후보 채점을 "리포트 대상일 직전 거래일 종가"를
기준(ref)으로 h=1/3/5(ref로부터 거래일 수)로 잰다 — h=1 이 사실상 "리포트 대상일
자체의 종가"다. 이 모듈은 소유자가 이번에 명시한 더 직관적인 표기를 쓴다:

- **D+0 open→close**: 리포트 대상일(`session_date`) 그 세션 자체의 시가→종가.
  "오늘 사서 오늘 파는 승부"에 대한 답.
- **D+0 close→close**(지수 전용): 직전 거래일 종가→오늘 종가. 오버나이트 갭을
  포함한 "오늘 전체"에 대한 답 — 지수(스탠스) 채점은 이걸 쓴다(오전판 스탠스는
  "오늘 장이 어느 쪽이냐"를 말하는 것이지 "오늘 장중 레인지"를 말하는 게 아니다).
- **D+1 close→close**: 오늘 종가→다음 거래일 종가. "오늘 사서 하루 더 들고 갔으면".
- 종목에도 참고용 `cc0_bps`(직전 종가→오늘 종가, 갭 포함 총수익)를 같이 낸다 —
  `oc0`과 `cc0`을 나란히 보면 "오늘 움직임의 대부분이 갭(오버나이트)에서 났는지
  장중에서 났는지"가 드러난다. 예: 언급 시점엔 이미 좋은 뉴스가 반영돼 갭업하고
  장중엔 되레 눌리는 패턴 — 이게 사실이면 "장 시작가 매수" 전제 자체가 의심된다.

## 종목 적중 규칙 (소유자 지시, 단순화)

"D+0 open→close > 0 = hit" — 이 리포트가 올리는 후보(뉴스/트렌딩/AUTO_WATCH)는
전부 롱 후보다(공매도 후보를 올리지 않는다, 루트 CLAUDE.md 전략 목록 참고). 그래서
방향을 따로 안 묻고 "오늘 장중 올랐으면 적중"으로 취급한다. 마감판(close_bet_view)
후보도 같은 규칙을 쓴다 — 근거(`reasons`)가 "당일 +N%"처럼 이미 상승 중인 종목의
지속을 노리는 후보라 방향은 마찬가지로 항상 롱이다.

## 유니버스 정의와 그 한계 (숨기지 않는다)

"그날 실제 상위 등락 종목 중 리포트가 놓친 것"(§4)을 재려면 **그날 시장 전체**가
필요하다. 이 모듈에 그런 소스가 없다 — 있는 건 그날 뉴스/트렌딩/랭킹 파이프라인이
**이미 한 번 훑은** 종목 집합(`payload["symbols"]`, 후보로 승격됐든 안 됐든 전부)
뿐이다. 그래서 여기서 내는 "놓친 상위 등락 종목"은 **"봤는데 안 올린" 것**이지
**"아예 안 본" 것**이 아니다 — 후자는 이 데이터로 원천적으로 못 잰다(recall 상한이
있다). 카드/요약 모두 이 한계를 그대로 적는다 — 판단 불가를 지어내지 않는다는
원칙의 연장.

거래대금(turnover)도 마찬가지다: 마감판 `close_bet_view`의 `trading_amount`
말고는 종목별 거래대금이 이 페이로드에 없다. 거래대금 상위 랭킹이 필요하면
`missed_movers()`가 `turnover_by_symbol` 인자를 받되, 없으면(대부분의 경우)
그 열을 전부 "미계산"으로 낸다 — 0으로 위장하지 않는다.

## 호출 계약

가격은 전부 호출부가 채워 넘긴다(`report_accuracy.py`와 같은 관례):
- `calendar`: 그 시장의 거래일 문자열(YYYY-MM-DD) 오름차순 리스트.
- `price_by_symbol`: `{symbol: {date: (open, close)}}` — 없는 키는 결측.
- `index_prices`: `{date: (open, close)}` — 지수 프록시(KR=069500, US=QQQ) 일봉.
"""
from __future__ import annotations

from quant.control.report_accuracy import (
    direction_bucket,
    generation_timing_flag,
    prev_trading_day,
    spearman_ic,
    trading_day_plus,
)

SCHEMA = 1

# SUMMARY(여러 날짜 집계)에서만 쓴다 — 카드 한 장은 "그날 하루의 사실"이라
# 표본 크기 게이트가 필요 없다(사실 하나에 신뢰구간을 씌우지 않는다).
MIN_N = 20


# ── 리포트가 말한 것 (§1) ────────────────────────────────────────────────

def mentioned_candidates(payload: dict, candidate_symbols: set[str]) -> list[dict]:
    """오전판 `payload["symbols"]` → `AUTO_WATCH` 후보로 실제 승격된 것만.

    `report_accuracy.extract_open_claims`과 같은 필터(같은 이유: `AUTO_WATCH`
    파싱을 두 번 하면 판정이 갈릴 수 있다)지만, 회고 카드는 사람이 읽을 것이라
    표시용 필드(sector/change_pct/news_articles_today)를 더 남긴다.
    """
    out = []
    for sym in payload.get("symbols") or []:
        symbol = sym.get("symbol")
        if not symbol or symbol not in candidate_symbols:
            continue
        out.append({
            "symbol": symbol,
            "name": sym.get("name"),
            "ai_score100": sym.get("ai_score100"),
            "trending_score100": sym.get("trending_score100"),
            "origin": sym.get("origin"),
            "upside_pct": sym.get("upside_pct"),
            "sector": sym.get("sector"),
            "reported_change_pct": sym.get("change_pct"),
            "news_articles_today": sym.get("news_articles_today"),
        })
    return out


def close_bet_candidates(payload: dict) -> list[dict]:
    """마감판 `close_bet_view` → 표시용 행. 근거(`reasons`)를 한 줄로 접는다."""
    out = []
    for row in payload.get("close_bet_view") or []:
        reasons = row.get("reasons") or []
        out.append({
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "score": row.get("score"),
            "reason": " · ".join(reasons) if reasons else None,
            "reported_change_pct": row.get("change_pct"),
            "trading_amount": row.get("trading_amount"),
        })
    return out


def sector_focus(payload: dict) -> list[dict]:
    """오전판 `us_kr_bridge.focus` → 리포트가 실제로 낸 "이 섹터가 유망하다" 청구.

    이 필드가 없으면(US 리포트는 애초에 `us_kr_bridge` 자체가 없다 — 자기
    시장 얘기라 US→KR 연결이 없다) 빈 리스트. 지어내지 않는다.
    """
    bridge = payload.get("us_kr_bridge") or {}
    focus = bridge.get("focus") or []
    out = []
    for item in focus:
        out.append({
            "us_name": item.get("us_name"),
            "kr_sectors": item.get("kr_sectors") or [],
            "stocks": item.get("stocks") or [],
        })
    return out


# ── 발행 시각 정합성 (데이터 품질 플래그, §1 부속) ────────────────────────
#
# `generation_timing_flag`는 2026-09-07(Phase 2 §6)에 `report_accuracy.py`로
# 옮겼다 — `report_claims.jsonl`을 쓰는 시점(`report_accuracy.extract_open_
# claims`/`extract_close_claims`)에도 같은 판정이 필요해졌기 때문이다(late
# 재생성 리포트를 채점 표본에서 빼는 §6 수정). 이 모듈은 위 import로 그대로
# 재사용한다 — 정의는 하나, 사용처는 둘.

# ── 실제 (§2) ────────────────────────────────────────────────────────────

def index_moves(index_prices: dict[str, tuple[float, float]], calendar: list[str],
                session_date: str) -> dict:
    """지수 프록시의 그날 오픈→클로즈 + 전일종가→오늘종가 + 오늘종가→익일종가.

    `index_prices`: `{date: (open, close)}`. 없는 값은 None(0으로 위장하지 않는다).
    """
    prev_date = prev_trading_day(calendar, session_date)
    next_date = trading_day_plus(calendar, session_date, 1)

    today_row = index_prices.get(session_date)
    prev_row = index_prices.get(prev_date) if prev_date else None
    next_row = index_prices.get(next_date) if next_date else None

    def _pct(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or a <= 0:
            return None
        return (b - a) / a * 10_000

    oc0_bps = _pct(today_row[0], today_row[1]) if today_row else None
    cc0_bps = _pct(prev_row[1], today_row[1]) if (prev_row and today_row) else None
    cc1_bps = _pct(today_row[1], next_row[1]) if (today_row and next_row) else None

    return {
        "prev_date": prev_date, "next_date": next_date,
        "oc0_bps": oc0_bps, "cc0_bps": cc0_bps, "cc1_bps": cc1_bps,
    }


def symbol_moves(price_by_date: dict[str, tuple[float, float]], calendar: list[str],
                 session_date: str) -> dict:
    """한 종목의 D+0 open→close / D+0 close→close(참고) / D+1 close→close.

    `price_by_date`: 그 종목의 `{date: (open, close)}` (이미 심볼별로 분리해
    호출부가 넘긴다 — `price_by_symbol[symbol]`).
    """
    prev_date = prev_trading_day(calendar, session_date)
    next_date = trading_day_plus(calendar, session_date, 1)

    today_row = price_by_date.get(session_date)
    prev_row = price_by_date.get(prev_date) if prev_date else None
    next_row = price_by_date.get(next_date) if next_date else None

    def _pct(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or a <= 0:
            return None
        return (b - a) / a * 10_000

    oc0_bps = _pct(today_row[0], today_row[1]) if today_row else None
    cc0_bps = _pct(prev_row[1], today_row[1]) if (prev_row and today_row) else None
    cc1_bps = _pct(today_row[1], next_row[1]) if (today_row and next_row) else None

    return {"oc0_bps": oc0_bps, "cc0_bps": cc0_bps, "cc1_bps": cc1_bps}


def rank_within_universe(universe_oc0: dict[str, float]) -> dict[str, tuple[int, int]]:
    """그날 |D+0 open→close| 기준 내림차순 순위. `{symbol: (rank, n)}`, 1-based.

    결측(None)인 심볼은 순위 계산에서 빠진다(전체 n에도 안 들어간다) — 순위가
    "그날 가격이 있던 종목들 중"이라는 걸 분명히 한다.
    """
    priced = [(sym, v) for sym, v in universe_oc0.items() if v is not None]
    ordered = sorted(priced, key=lambda kv: abs(kv[1]), reverse=True)
    n = len(ordered)
    return {sym: (i + 1, n) for i, (sym, _) in enumerate(ordered)}


# ── 판정 (§3) ────────────────────────────────────────────────────────────

def stance_verdict(direction: dict | None, idx_moves: dict) -> dict:
    """스탠스(방향콜) 적중/실패. `direction`: `{"label":..,"score100":..}` 또는 None.

    실현치는 `cc0_bps`(전일종가→오늘종가) — 스탠스는 "오늘 장 전체가 어느
    쪽이냐"를 말하지 "오늘 장중 레인지"를 말하지 않는다(모듈 docstring).
    """
    if not direction:
        return {"bucket": None, "realized_cc0_bps": idx_moves.get("cc0_bps"), "hit": None}
    bucket = direction_bucket(direction.get("label"), direction.get("score100"))
    realized = idx_moves.get("cc0_bps")
    if bucket not in ("bull", "bear") or realized is None:
        return {"bucket": bucket, "realized_cc0_bps": realized, "hit": None}
    hit = (bucket == "bull" and realized > 0) or (bucket == "bear" and realized < 0)
    return {"bucket": bucket, "realized_cc0_bps": realized, "hit": hit}


def mention_verdicts(mentions: list[dict], moves_by_symbol: dict[str, dict]) -> list[dict]:
    """언급 종목마다 실제 수익률 + 적중여부(§3 단순 규칙: D+0 oc>0 = 적중)를 붙인다."""
    out = []
    for m in mentions:
        moves = moves_by_symbol.get(m["symbol"]) or {}
        oc0 = moves.get("oc0_bps")
        hit = None if oc0 is None else (oc0 > 0)
        out.append({**m, **moves, "hit": hit})
    return out


def score_ic_for_day(verdicts: list[dict], score_key: str,
                     return_key: str = "oc0_bps") -> tuple[float | None, int]:
    """그날 언급 종목의 점수-수익률 Spearman IC. `(ic, n)` — n<2 면 `(None, n)`.

    표본 크기 판단(`판단 불가`)은 여기서 하지 않는다 — 렌더/집계 쪽이 `MIN_N`을
    적용한다(계산과 정책을 분리, `report_accuracy.py`와 같은 관례).
    """
    pairs = [
        (float(v[score_key]), float(v[return_key]))
        for v in verdicts
        if v.get(score_key) is not None and v.get(return_key) is not None
    ]
    return spearman_ic(pairs), len(pairs)


# ── 놓친 것 (§4) ─────────────────────────────────────────────────────────

def missed_movers(universe_moves: dict[str, dict], mentioned_symbols: set[str],
                  names: dict[str, str] | None = None,
                  turnover_by_symbol: dict[str, float] | None = None,
                  top_n: int = 10) -> dict:
    """언급 안 된 종목 중 |D+0 oc| 상위 N + 거래대금 상위 N(있으면).

    `universe_moves`: `{symbol: {"oc0_bps":..}}` — 그날 파이프라인이 본 전체
    종목(모듈 docstring의 유니버스 한계 그대로). `turnover_by_symbol` 이 없으면
    거래대금 랭킹은 빈 리스트 + `turnover_computed=False`로 낸다(0으로 위장 금지).
    """
    names = names or {}
    turnover_by_symbol = turnover_by_symbol or {}

    unmentioned = {
        sym: v for sym, v in universe_moves.items()
        if sym not in mentioned_symbols and v.get("oc0_bps") is not None
    }
    by_move = sorted(unmentioned.items(), key=lambda kv: abs(kv[1]["oc0_bps"]), reverse=True)[:top_n]
    top_by_move = [
        {"symbol": sym, "name": names.get(sym), "oc0_bps": v["oc0_bps"]}
        for sym, v in by_move
    ]

    turnover_computed = bool(turnover_by_symbol)
    top_by_turnover: list[dict] = []
    if turnover_computed:
        unmentioned_to = {
            sym: turnover_by_symbol[sym] for sym in unmentioned if sym in turnover_by_symbol
        }
        by_to = sorted(unmentioned_to.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
        top_by_turnover = [
            {"symbol": sym, "name": names.get(sym), "turnover": to,
             "oc0_bps": unmentioned.get(sym, {}).get("oc0_bps")}
            for sym, to in by_to
        ]

    return {
        "top_by_move": top_by_move,
        "top_by_turnover": top_by_turnover,
        "turnover_computed": turnover_computed,
        "universe_n": len(universe_moves),
    }


def known_signal_flags(symbol_row: dict | None) -> dict:
    """놓친 종목 하나에 대해 "이미 계산하던 신호가 미리 알렸을까"를 `payload
    ["symbols"]` 원본 행(있으면)에서 읽는다. 행 자체가 없으면(파이프라인이
    아예 못 본 종목) 전부 "unknown" — 지어내지 않는다.

    `symbol_row`: 그날 `payload["symbols"]`에서 그 심볼을 찾은 원본 dict, 없으면 None.
    """
    if symbol_row is None:
        return {"foreign_flow": "unknown", "rvol": "unknown",
                "news_count": "unknown", "sector_move": "unknown"}

    def _flag(cond: bool | None) -> bool | str:
        return "unknown" if cond is None else bool(cond)

    foreign_streak = symbol_row.get("foreign_buy_streak")
    rvol = symbol_row.get("relative_volume")
    news = symbol_row.get("news_articles_today")

    return {
        "foreign_flow": _flag(None if foreign_streak is None else foreign_streak >= 2),
        "rvol": _flag(None if rvol is None else rvol >= 1.5),
        "news_count": _flag(None if news is None else news >= 1),
        "sector_move": "unknown",  # 섹터 등락은 sector_daily.jsonl 표본이 짧아(§모듈 docstring) 여기서 판정하지 않는다.
    }


# ── 한 줄 교훈 (§5, 규칙 기반 — 사람이 다시 다듬을 수 있는 초안) ──────────

def derive_lesson(card: dict) -> str:
    """카드 데이터에서 규칙 기반 한 줄 교훈 초안을 만든다. 결정론적(LLM 없음) —
    매일 자동 생성(6단계, 재발 방지 루프)에 그대로 쓸 수 있어야 한다는 요구
    (계획 문서 §6)와 `quant/control`이 LLM을 쓰지 않는다는 원칙(루트 CLAUDE.md)
    을 같이 만족한다. 사람이 최초 축적(이번 회고) 때는 각 카드에서 이 초안을
    검토해 더 구체적인 문장으로 덮어써도 된다 — 이 함수는 "지어내지 않는
    기본값"이다.
    """
    stance = card.get("stance_verdict") or {}
    verdicts = card.get("mention_verdicts") or []
    hits = [v for v in verdicts if v.get("hit") is not None]
    hit_rate = (sum(1 for v in hits if v["hit"]) / len(hits)) if hits else None
    missed = card.get("missed") or {}
    n_missed = len(missed.get("top_by_move") or [])

    parts = []
    if stance.get("hit") is True:
        parts.append("스탠스 적중")
    elif stance.get("hit") is False:
        parts.append("스탠스 실패")
    else:
        parts.append("스탠스 판정 불가(중립 또는 지수 결측)")

    if hit_rate is not None:
        parts.append(f"언급 종목 {len(hits)}건 중 {hit_rate:.0%} 상승 마감")

    if n_missed:
        top = missed["top_by_move"][0]
        parts.append(f"미언급 상위 등락({top['symbol']} {top['oc0_bps']:+.0f}bp) 존재")

    return " · ".join(parts) if parts else "판정 가능한 데이터 부족"


# ── 카드 조립 + 렌더 ──────────────────────────────────────────────────────

def build_card(*, date: str, market: str, session: str, open_payload: dict | None,
               close_payload: dict | None, candidate_symbols: set[str],
               calendar: list[str], index_prices: dict[str, tuple[float, float]],
               universe_symbols: list[dict], price_by_symbol: dict[str, dict[str, tuple]],
               names: dict[str, str] | None = None,
               turnover_by_symbol: dict[str, float] | None = None) -> dict:
    """한 장의 회고 카드를 구성하는 오케스트레이션(여전히 순수 — 가격은 인자로
    받는다). `open_payload`/`close_payload` 중 그 세션에 해당하는 쪽만 있으면
    된다(둘 다 없으면 빈 카드).

    `universe_symbols`: 그날 `payload["symbols"]` 리스트 그대로(§4 유니버스,
    모듈 docstring의 한계 그대로 계승) — open/close 어느 쪽이든 그 세션 자체의
    `symbols` 리스트를 넘긴다(마감판은 이 필드가 없다 — `close_bet_view`만 있다,
    그 경우 빈 리스트로 넘긴다).
    """
    idx = index_moves(index_prices, calendar, date)

    if session == "open":
        payload = open_payload or {}
        stance = payload.get("stance")
        mentions = mentioned_candidates(payload, candidate_symbols)
        score_keys = ("ai_score100", "trending_score100")
    else:
        payload = close_payload or {}
        stance = None
        mentions = close_bet_candidates(payload)
        score_keys = ("score",)

    moves_by_symbol = {
        m["symbol"]: symbol_moves(price_by_symbol.get(m["symbol"]) or {}, calendar, date)
        for m in mentions
    }
    verdicts = mention_verdicts(mentions, moves_by_symbol)

    ic_by_score = {}
    for key in score_keys:
        ic, n = score_ic_for_day(verdicts, key)
        ic_by_score[key] = {"ic": ic, "n": n, "measured": n >= MIN_N and ic is not None}

    universe_moves = {
        row["symbol"]: symbol_moves(price_by_symbol.get(row["symbol"]) or {}, calendar, date)
        for row in universe_symbols if row.get("symbol")
    }
    universe_oc0 = {sym: v["oc0_bps"] for sym, v in universe_moves.items()}
    ranks = rank_within_universe(universe_oc0)
    for v in verdicts:
        v["rank"] = ranks.get(v["symbol"])

    mentioned_symbols = {m["symbol"] for m in mentions}
    missed = missed_movers(universe_moves, mentioned_symbols, names, turnover_by_symbol)
    universe_by_symbol = {row["symbol"]: row for row in universe_symbols if row.get("symbol")}
    for row in missed["top_by_move"]:
        row["signals"] = known_signal_flags(universe_by_symbol.get(row["symbol"]))

    card = {
        "schema": SCHEMA, "date": date, "market": market, "session": session,
        "generation_timing": generation_timing_flag(payload.get("generated_at"), market),
        "stance": stance,
        "sector_focus": sector_focus(payload) if session == "open" else [],
        "mentions": mentions,
        "index_moves": idx,
        "stance_verdict": stance_verdict(stance, idx),
        "mention_verdicts": verdicts,
        "ic_by_score": ic_by_score,
        "missed": missed,
    }
    card["lesson"] = derive_lesson(card)
    return card


def _fmt_bps(v: float | None) -> str:
    return f"{v:+.0f}bp" if v is not None else "결측"


def render_markdown(card: dict) -> str:
    """카드 dict → Markdown. 소유자 지시 5절(리포트가 말한 것/실제/판정/놓친 것/
    한 줄 교훈)을 그대로 따른다."""
    date, market, session = card["date"], card["market"], card["session"]
    title_suffix = "" if session == "open" else " (마감판)"
    lines = [f"# {date} {market}{title_suffix} 리포트 회고", ""]

    # §1 리포트가 말한 것
    lines.append("## 1. 리포트가 말한 것")
    if card.get("generation_timing") == "late" and session == "open":
        lines.append("- ⚠ 이 리포트는 정상 프리마켓 창(KR 05~09시/US 17~22시 KST)을 벗어나 "
                     "당일 늦게 재생성됐다 — 스탠스가 순수 프리마켓 예측이 아니라 그날 정규장 "
                     "일부/전부를 이미 본 뒤 나왔을 수 있다(모듈 docstring §발행 시각 정합성 참고).")
    stance = card.get("stance")
    if stance:
        lines.append(f"- 스탠스: **{stance.get('label')}** (score100={stance.get('score100')})")
        if stance.get("line"):
            lines.append(f"  - {stance['line']}")
    else:
        lines.append("- 스탠스: (이 세션은 방향콜을 내지 않는다)" if session == "close" else "- 스탠스: 없음")

    focus = card.get("sector_focus") or []
    if focus:
        lines.append("- 섹터 포커스(US→KR 연결):")
        for f in focus:
            secs = ", ".join(f.get("kr_sectors") or [])
            lines.append(f"  - {f.get('us_name')} → {secs}")

    mentions = card.get("mentions") or []
    lines.append(f"- 언급 종목 {len(mentions)}건:")
    lines.append("")
    if session == "open":
        lines.append("| 종목 | 이름 | ai_score100 | trending_score100 | origin | upside_pct |")
        lines.append("|---|---|---|---|---|---|")
        for m in mentions:
            lines.append(f"| {m['symbol']} | {m.get('name') or ''} | {m.get('ai_score100')} | "
                         f"{m.get('trending_score100')} | {m.get('origin')} | {m.get('upside_pct')} |")
    else:
        lines.append("| 종목 | 이름 | score | 근거 |")
        lines.append("|---|---|---|---|")
        for m in mentions:
            lines.append(f"| {m['symbol']} | {m.get('name') or ''} | {m.get('score')} | {m.get('reason') or ''} |")
    lines.append("")

    # §2 실제
    lines.append("## 2. 실제")
    idx = card.get("index_moves") or {}
    lines.append(f"- 지수(프록시) D+0 open→close: {_fmt_bps(idx.get('oc0_bps'))}")
    lines.append(f"- 지수(프록시) D+0 close→close(전일 대비): {_fmt_bps(idx.get('cc0_bps'))}")
    lines.append(f"- 지수(프록시) D+1 close→close: {_fmt_bps(idx.get('cc1_bps'))}")
    lines.append("")
    verdicts = card.get("mention_verdicts") or []
    if verdicts:
        lines.append("| 종목 | D+0 open→close | D+0 close→close(참고) | D+1 close→close | 유니버스 내 순위 |")
        lines.append("|---|---|---|---|---|")
        for v in verdicts:
            rank = v.get("rank")
            rank_s = f"{rank[0]}/{rank[1]}" if rank else "결측"
            lines.append(f"| {v['symbol']} | {_fmt_bps(v.get('oc0_bps'))} | {_fmt_bps(v.get('cc0_bps'))} | "
                         f"{_fmt_bps(v.get('cc1_bps'))} | {rank_s} |")
    lines.append("")

    # §3 판정
    lines.append("## 3. 판정")
    sv = card.get("stance_verdict") or {}
    if sv.get("hit") is None:
        lines.append(f"- 스탠스: 판정 불가 (bucket={sv.get('bucket')}, 실현={_fmt_bps(sv.get('realized_cc0_bps'))})")
    else:
        verdict_word = "적중" if sv["hit"] else "실패"
        lines.append(f"- 스탠스: **{verdict_word}** (bucket={sv.get('bucket')}, 실현={_fmt_bps(sv.get('realized_cc0_bps'))})")

    hits = [v for v in verdicts if v.get("hit") is not None]
    if hits:
        n_hit = sum(1 for v in hits if v["hit"])
        lines.append(f"- 종목 적중(D+0 open→close>0): {n_hit}/{len(hits)} "
                     f"({n_hit/len(hits):.0%})")
        miss_syms = [v["symbol"] for v in hits if not v["hit"]]
        if miss_syms:
            lines.append(f"  - 실패: {', '.join(miss_syms)}")
    else:
        lines.append("- 종목 적중: 판정 불가 (가격 결측)")

    for key, stat in (card.get("ic_by_score") or {}).items():
        if stat["measured"]:
            lines.append(f"- {key} vs D+0 open→close IC(Spearman): {stat['ic']:+.3f} (n={stat['n']})")
        else:
            lines.append(f"- {key} vs D+0 open→close IC: 판단 불가 (n={stat['n']} < {MIN_N})")
    lines.append("")

    # §4 놓친 것
    lines.append("## 4. 놓친 것")
    missed = card.get("missed") or {}
    lines.append(f"- 유니버스(그날 파이프라인이 본 전 종목, 후보+비후보): {missed.get('universe_n', 0)}종목 "
                 "— 파이프라인이 아예 못 본 종목은 이 유니버스에 없다(recall 상한, 모듈 docstring 참고)")
    top_move = missed.get("top_by_move") or []
    if top_move:
        lines.append("- 미언급 상위 등락(|D+0 open→close| 기준):")
        lines.append("")
        lines.append("| 종목 | 이름 | D+0 open→close | 외국인수급 신호 | RVOL 신호 | 뉴스량 신호 |")
        lines.append("|---|---|---|---|---|---|")
        for row in top_move:
            sig = row.get("signals") or {}
            lines.append(f"| {row['symbol']} | {row.get('name') or ''} | {_fmt_bps(row['oc0_bps'])} | "
                         f"{sig.get('foreign_flow')} | {sig.get('rvol')} | {sig.get('news_count')} |")
    else:
        lines.append("- 미언급 상위 등락: 없음 또는 계산 불가")
    if missed.get("turnover_computed"):
        lines.append("- 거래대금 상위(미언급):")
        for row in missed.get("top_by_turnover") or []:
            lines.append(f"  - {row['symbol']} {row.get('name') or ''}: 거래대금 {row['turnover']:,.0f}"
                         f" (D+0 {_fmt_bps(row.get('oc0_bps'))})")
    else:
        lines.append("- 거래대금 상위(미언급): 미계산 (이 페이로드엔 종목별 거래대금이 없다)")
    lines.append("")

    # §5 한 줄 교훈
    lines.append("## 5. 한 줄 교훈")
    lines.append(f"- {card.get('lesson', '')}")
    lines.append("")

    return "\n".join(lines)


# ── 재발 방지 루프 (Phase 6, 2026-09-06 소유자 지시) ──────────────────────
#
# 1단계(위)는 사람이 손으로 61장을 감사한 결과물이다. 이 절은 그 감사를 매일
# 자동으로 반복해 원장에 쌓고(§요약행), 원장 한 줄에서 텔레그램 한 줄(§일일)과
# 주간 집계(§주간)를 뽑는다. 전부 순수 함수 — 파일 I/O(카드 렌더, 원장 append,
# 텔레그램 발송)는 `quant/apps/report_cli.py`의 `review-daily` 서브커맨드와
# `server/scripts/report_review_daily.sh`가 한다(이 모듈은 여전히 계산만,
# `report_accuracy.py`/이 파일 §카드 조립과 같은 관례).

# 원장 행에 남기는 "미언급 상위 등락" 심볼 수 — 주간 집계의 "반복 미포착 상위 3"
# 계산에 필요한 만큼만(카드 자체의 top_n=10 전부를 원장에 실을 필요는 없다).
_MISSED_LEDGER_TOP_K = 5

# `missed_movers()`의 `top_n` 기본값과 같다 — 일일 텔레그램 한 줄의 "놓친 급등
# N/10" 분모. `build_card()`가 `top_n`을 넘기지 않아 항상 이 기본값을 쓰므로
# 여기서도 상수로 고정한다(호출부가 top_n을 바꾸면 이 상수도 같이 바꿔야 한다).
_MISSED_DISPLAY_CAP = 10


def review_summary_row(card: dict, *, generated_at: str | None = None) -> dict:
    """회고 카드(`build_card` 출력) → 원장(`data/ledger/report_review.jsonl`)
    압축 행. 순수 함수 — `generated_at`(이 행 자체를 만든 시각, ISO)은
    호출부가 넘긴다(테스트가 벽시계에 의존하지 않게, 이 파일의 다른 함수들과
    같은 관례).

    적중률 분모는 카드의 §3 판정과 같은 규칙을 쓴다 — 가격이 결측이라
    `hit=None`인 언급은 분모에서 뺀다(`mentions_n`은 "판정 가능했던" 수,
    `mentions_total_n`은 언급 전체 수 — 후자는 참고용).
    """
    stance_v = card.get("stance_verdict") or {}
    verdicts = card.get("mention_verdicts") or []
    hits = [v for v in verdicts if v.get("hit") is not None]
    hit_n = sum(1 for v in hits if v["hit"])
    hit_rate = (hit_n / len(hits)) if hits else None

    by_origin: dict[str, dict] = {}
    for v in hits:
        tag = v.get("origin") or "unknown"
        row = by_origin.setdefault(tag, {"n": 0, "hit_n": 0})
        row["n"] += 1
        if v["hit"]:
            row["hit_n"] += 1

    ic_by_score = {
        key: {"ic": stat.get("ic"), "n": stat.get("n"), "measured": stat.get("measured")}
        for key, stat in (card.get("ic_by_score") or {}).items()
    }

    missed = card.get("missed") or {}
    top_move = missed.get("top_by_move") or []
    rvol_n = sum(1 for row in top_move if (row.get("signals") or {}).get("rvol") is True)
    missed_symbols = [
        {"symbol": row.get("symbol"), "name": row.get("name"), "oc0_bps": row.get("oc0_bps")}
        for row in top_move[:_MISSED_LEDGER_TOP_K]
    ]

    return {
        "schema": 1,
        "date": card["date"], "market": card["market"], "session": card["session"],
        "generated_at": generated_at,
        "stance_hit": stance_v.get("hit"),
        "stance_bucket": stance_v.get("bucket"),
        "mentions_total_n": len(verdicts),
        "mentions_n": len(hits),
        "mentions_hit_n": hit_n,
        "mentions_hit_rate": hit_rate,
        "mentions_by_origin": by_origin,
        "ic_by_score": ic_by_score,
        "missed_n": len(top_move),
        "missed_universe_n": missed.get("universe_n"),
        "missed_rvol_n": rvol_n,
        "missed_symbols": missed_symbols,
    }


def format_daily_telegram(row: dict) -> str:
    """원장 행(§요약행) → 텔레그램 한 줄(브리핑 레인). 소유자 예시 형식:

        🔁 회고 KR 09-05: 스탠스 ✗ · 언급 12건 적중 42% · 놓친 급등 3/10 (RVOL 신호 2)

    평문(HTML 태그 없음) — 이 줄이 담는 값(시장 코드·날짜·정수·퍼센트)은 종목명
    같은 자유 텍스트가 아니라 `<`/`>`를 낼 소스가 없다. 그래도 만에 하나를 대비해
    호출부(테스트)가 태그 균형을 확인한다(`tests/test_report_review.py`)."""
    mmdd = row["date"][5:]
    suffix = "" if row.get("session") == "open" else " 마감"

    if row.get("session") == "close":
        stance_part = "스탠스 없음(마감판)"
    elif row.get("stance_hit") is True:
        stance_part = "스탠스 ✓"
    elif row.get("stance_hit") is False:
        stance_part = "스탠스 ✗"
    else:
        stance_part = "스탠스 판정불가"

    total = row.get("mentions_total_n") or 0
    n = row.get("mentions_n") or 0
    rate = row.get("mentions_hit_rate")
    if n and rate is not None:
        mention_part = f"언급 {n}건 적중 {rate:.0%}"
    elif total:
        mention_part = f"언급 {total}건 적중 판정불가"
    else:
        mention_part = "언급 0건"

    missed_n = row.get("missed_n") or 0
    rvol_n = row.get("missed_rvol_n") or 0
    missed_part = f"놓친 급등 {missed_n}/{_MISSED_DISPLAY_CAP} (RVOL 신호 {rvol_n})"

    return (f"🔁 회고 {row['market']} {mmdd}{suffix}: {stance_part} · "
            f"{mention_part} · {missed_part}")


def aggregate_weekly(rows: list[dict]) -> dict:
    """원장 행 리스트(호출부가 이미 주 경계로 걸러 넘긴다 — `report_accuracy.py`
    의 `--since/--until`과 같은 관례) → 주간 집계. 순수 함수.

    `stance`는 오전판(`session=="open"`) 행만 본다(마감판은 스탠스가 없다).
    `mentions`/`ic_distribution`/`recall`은 세션 무관하게 전부 합산한다."""
    stance_rows = [r for r in rows if r.get("session") == "open" and r.get("stance_hit") is not None]
    stance_hit_n = sum(1 for r in stance_rows if r["stance_hit"])
    stance = {
        "n": len(stance_rows), "hit_n": stance_hit_n,
        "hit_rate": (stance_hit_n / len(stance_rows)) if stance_rows else None,
    }

    mentions_n = sum(r.get("mentions_n") or 0 for r in rows)
    mentions_hit_n = sum(r.get("mentions_hit_n") or 0 for r in rows)
    mentions = {
        "n": mentions_n, "hit_n": mentions_hit_n,
        "hit_rate": (mentions_hit_n / mentions_n) if mentions_n else None,
    }

    by_tag: dict[str, dict] = {}
    for r in rows:
        for tag, d in (r.get("mentions_by_origin") or {}).items():
            acc = by_tag.setdefault(tag, {"n": 0, "hit_n": 0})
            acc["n"] += d.get("n", 0)
            acc["hit_n"] += d.get("hit_n", 0)
    for acc in by_tag.values():
        acc["hit_rate"] = (acc["hit_n"] / acc["n"]) if acc["n"] else None

    ic_series: dict[str, list[float]] = {}
    for r in rows:
        for key, stat in (r.get("ic_by_score") or {}).items():
            if stat.get("measured") and stat.get("ic") is not None:
                ic_series.setdefault(key, []).append(stat["ic"])
    ic_distribution = {}
    for key, vals in ic_series.items():
        ic_distribution[key] = {
            "n_days": len(vals),
            "positive_days": sum(1 for v in vals if v > 0),
            "negative_days": sum(1 for v in vals if v < 0),
            "mean_ic": sum(vals) / len(vals),
        }

    recall = {
        "missed_n": sum(r.get("missed_n") or 0 for r in rows),
        "universe_n": sum(r.get("missed_universe_n") or 0 for r in rows),
        "rvol_signal_n": sum(r.get("missed_rvol_n") or 0 for r in rows),
    }

    miss_counter: dict[str, dict] = {}
    for r in rows:
        for sym_row in r.get("missed_symbols") or []:
            sym = sym_row.get("symbol")
            if not sym:
                continue
            acc = miss_counter.setdefault(
                sym, {"symbol": sym, "name": sym_row.get("name"), "days": 0, "_moves": []})
            acc["days"] += 1
            if sym_row.get("oc0_bps") is not None:
                acc["_moves"].append(sym_row["oc0_bps"])
    recurring = sorted(
        (v for v in miss_counter.values() if v["days"] >= 2),
        key=lambda v: v["days"], reverse=True,
    )[:3]
    for v in recurring:
        moves = v.pop("_moves")
        v["avg_oc0_bps"] = (sum(moves) / len(moves)) if moves else None
    for v in miss_counter.values():
        v.pop("_moves", None)

    return {
        "n_cards": len(rows),
        "stance": stance,
        "mentions": mentions,
        "mentions_by_tag": by_tag,
        "ic_distribution": ic_distribution,
        "recall": recall,
        "top_recurring_misses": recurring,
    }


def render_weekly_markdown(agg: dict, week_label: str) -> str:
    """주간 집계(`aggregate_weekly` 출력) → Markdown."""
    lines = [f"# {week_label} 리포트 회고 — 주간 집계", "", f"- 카드 {agg['n_cards']}장 집계", ""]

    st = agg["stance"]
    if st["n"]:
        lines.append(f"- 스탠스 적중률: {st['hit_rate']:.0%} ({st['hit_n']}/{st['n']})")
    else:
        lines.append("- 스탠스 적중률: 판정 불가 (표본 없음)")

    m = agg["mentions"]
    if m["n"]:
        lines.append(f"- 언급 종목 적중률: {m['hit_rate']:.0%} ({m['hit_n']}/{m['n']})")
    else:
        lines.append("- 언급 종목 적중률: 판정 불가 (표본 없음)")
    lines.append("")

    lines.append("## 태그(origin)별 언급 적중률")
    if agg["mentions_by_tag"]:
        lines.append("| 태그 | n | 적중 | 적중률 |")
        lines.append("|---|---|---|---|")
        for tag, d in sorted(agg["mentions_by_tag"].items()):
            rate = f"{d['hit_rate']:.0%}" if d.get("hit_rate") is not None else "결측"
            lines.append(f"| {tag} | {d['n']} | {d['hit_n']} | {rate} |")
    else:
        lines.append("데이터 없음")
    lines.append("")

    lines.append("## 점수 IC 분포 (일별 측정치 기준)")
    if agg["ic_distribution"]:
        lines.append("| 점수 | 일수 | 양(+)인 날 | 음(-)인 날 | 평균 IC |")
        lines.append("|---|---|---|---|---|")
        for key, d in sorted(agg["ic_distribution"].items()):
            lines.append(f"| {key} | {d['n_days']} | {d['positive_days']} | "
                         f"{d['negative_days']} | {d['mean_ic']:+.3f} |")
    else:
        lines.append("데이터 없음 (측정 가능한 IC 없음 — n<MIN_N 인 날뿐)")
    lines.append("")

    lines.append("## 놓친 급등주 recall")
    r = agg["recall"]
    lines.append(f"- 유니버스 합계 {r['universe_n']}종목 중 미언급 상위 등락 {r['missed_n']}건, "
                 f"그중 RVOL 신호 있던 건 {r['rvol_signal_n']}건")
    lines.append("")

    lines.append("## 반복적으로 놓친 종목 상위 3")
    if agg["top_recurring_misses"]:
        for v in agg["top_recurring_misses"]:
            avg = f"{v['avg_oc0_bps']:+.0f}bp" if v.get("avg_oc0_bps") is not None else "결측"
            lines.append(f"- {v['symbol']} {v.get('name') or ''}: {v['days']}일 반복, "
                         f"평균 D+0 open→close {avg}")
    else:
        lines.append("- 이번 주 2일 이상 반복해서 놓친 종목 없음")
    lines.append("")

    lines.append("## Phase 2 반영 전/후 비교")
    lines.append("- Phase 2(점수·예측 개선)가 아직 착수/반영 전이라 비교할 '이후' 데이터가 없다 "
                 "— 반영일 이후 원장이 쌓이면 이 절이 자동으로 채워진다(지어내지 않는다).")
    lines.append("")

    return "\n".join(lines)


def format_weekly_telegram(agg: dict, week_label: str) -> str:
    """주간 집계 → 텔레그램 한 줄(브리핑 레인)."""
    st, m, r = agg["stance"], agg["mentions"], agg["recall"]
    stance_s = f"{st['hit_rate']:.0%}({st['hit_n']}/{st['n']})" if st["n"] else "판정불가"
    mention_s = f"{m['hit_rate']:.0%}({m['hit_n']}/{m['n']})" if m["n"] else "판정불가"
    top = agg["top_recurring_misses"]
    top_s = ", ".join(v["symbol"] for v in top) if top else "없음"
    return (f"📅 주간 회고 {week_label}: 카드 {agg['n_cards']}장 · 스탠스 {stance_s} · "
            f"언급 적중 {mention_s} · 놓친 급등 {r['missed_n']}건(유니버스 {r['universe_n']}) · "
            f"반복 미포착 {top_s}")


def build_report_review_block(recent_rows: list[dict] | None) -> dict:
    """공개 사이트 payload 서브트리 후보 — `quant.control.performance`의
    `_build_report_accuracy_block`(2026-09-06)과 같은 관례: 순수 함수, 파일
    I/O는 이 함수를 호출할 쪽(Phase 5 담당, `quant/control/performance.py`
    — 이 파일은 Phase 6 범위라 여기서 직접 엮지 않는다)이 한다.

    호출 계약: `recent_rows`에 `data/ledger/report_review.jsonl`의 최근 N행
    (`review_summary_row` 출력 그대로, 순서 무관)을 읽어 넘기면 된다. 비어
    있으면(회고 자동화가 아직 한 번도 안 돈 상태) 빈 dict — 착륙 전 의존성
    없음(`_build_paper_epoch`/`_build_report_accuracy_block`과 같은 관례).

    집계는 `aggregate_weekly`를 그대로 재사용한다 — "그 주"든 "최근 N일"이든
    행 리스트를 집계하는 셈법 자체는 같다."""
    if not recent_rows:
        return {}
    agg = aggregate_weekly(recent_rows)
    latest = max(recent_rows, key=lambda r: (r.get("date") or "", r.get("session") or ""))
    return {
        "as_of": latest.get("date"),
        "n_cards": agg["n_cards"],
        "stance": agg["stance"],
        "mentions": agg["mentions"],
        "recall": agg["recall"],
    }
