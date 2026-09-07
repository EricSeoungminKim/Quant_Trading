"""장 마감 하루 요약 — "오늘 무슨 일이 있었나" 한 장(HTML). 순수 조립 + 렌더.

## 왜 이 리포트가 따로 있나 (2026-08-28 소유자 지시)

텔레그램 메시지가 너무 많아 읽히지 않는다는 지적을 받았다. 결론은 **역할 분리**다:

- 장중 메시지 = 매매(체결·신호)만.
- 장 마감 후 = 이 파일 한 장. **변경된 점 / 오늘의 실적 / 문제와 조치 / 지분 변경.**

그래서 여기에는 **시세 분석·종목 추천·전략 설명을 넣지 않는다** — 그건 아침
리포트(`quant/report/`)와 마감 결과 리포트(`quant/control/close_report.py`)의
몫이고, 같은 말을 두 번 하면 그게 곧 소음이다.

## 왜 순수인가

`close_report.py`와 같은 계약이다 — 이 모듈은 파일도 네트워크도 만지지 않는다.
호출부(`quant.apps.cli daily-wrap`)가 원장·포트폴리오·종목명 캐시·git 로그를
읽어 인자로 주입하고, 여기서는 조립과 렌더만 한다. 덕분에 테스트가 픽스처만으로
전체 문구를 검증할 수 있고, 데이터 한 갈래가 비어도 리포트 자체는 나온다.

## 숫자 규율

- 없는 데이터는 "없음"/"판단 불가"로 쓴다. 추정하지 않는다.
- 승률은 표본이 `ledger.MIN_TRIPS_FOR_JUDGEMENT` 미만이면 내지 않는다 — 하루치
  트립으로는 대개 미달이고, 그게 정직한 답이다(표본을 부풀리는 별도 임계를
  새로 만들지 않는다 — 임계는 원장 하나에만 있다).
- 손익은 부호를 항상 명시하고 색을 함께 준다. **색맹 대비로 부호가 본체이고
  색은 보조다** — 색을 못 봐도 `+`/`-`로 읽힌다.

## HTML 규율

외부 요청 0(인라인 `<style>`만), 모바일 폭 우선(텔레그램에서 폰으로 연다),
표는 가로 스크롤, 다크/라이트 둘 다 읽히게(`prefers-color-scheme`).
`tests/test_daily_wrap.py`가 외부 URL 부재를 정규식으로 강제한다.
"""
from __future__ import annotations

from datetime import date, datetime
from html import escape as _esc

from quant.control import alpha as _alpha
from quant.control import cost_model as _cost_model
from quant.control import exposure as _exposure
from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT, ab_compare
from quant.core.models import market_of_symbol

# 절 제목 — 소유자가 지정한 순서 그대로. 순서를 바꾸지 않는다. "지수 대비 성적"은
# 2026-08-29에 추가됐다(`alpha.py` 모듈 docstring — "항상 지수 위에서 노는 것").
# 기존 1~4절 번호를 그대로 두려고 맨 끝에 붙였다 — 4절(변경된 점)은 git 을 못
# 읽으면 절 자체가 생략되므로, 그 경우 번호가 3 → 5로 건너뛸 수 있다(기존에도
# 4절이 조건부로 사라지는 설계라 새 사실이 아니다). "체결 비용"은 2026-08-30에
# 같은 이유로(조건부 절 번호 흔들림 회피) 맨 끝에 붙었다.
SECTION_TITLES = (
    "오늘의 실적", "지분 변경", "문제 발견 및 개선", "변경된 점", "지수 대비 성적",
    "체결 비용", "오늘의 매매 리뷰",
)


# ── 포맷 ────────────────────────────────────────────────────────────────

def fmt_amount(value: float, market: str, signed: bool = True) -> str:
    """천단위 구분 + 부호 명시. KR=원(정수), US=$(소수 2자리)."""
    sign = "+" if signed and value > 0 else ("-" if value < 0 else ("+" if signed else ""))
    body = abs(value)
    if market == "KR":
        return f"{sign}{body:,.0f}원"
    return f"{sign}${body:,.2f}"


def _cls(value: float) -> str:
    """이익=up / 손실=down / 0=flat. 색은 보조, 부호가 본체다."""
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "flat"


def _money(value: float, market: str) -> str:
    return f'<span class="{_cls(value)}">{_esc(fmt_amount(value, market))}</span>'


# ── 1. 오늘의 실적 ──────────────────────────────────────────────────────

def trips_closed_between(trips: list[dict], start: datetime, end: datetime) -> list[dict]:
    """`exit_ts`가 [start, end] 안인 라운드트립만 — 오늘 종결된 것이 오늘의 성적이다."""
    out: list[dict] = []
    for t in trips:
        raw = t.get("exit_ts")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=start.tzinfo)
        if start <= ts <= end:
            out.append(t)
    return out


def _strategy_rows(trips: list[dict]) -> list[dict]:
    """전략별 n·평균bp·승률. 표본 미달이면 승률 자리에 "판단 불가"."""
    by: dict[str, list[dict]] = {}
    for t in trips:
        by.setdefault(str(t.get("strategy", "?")), []).append(t)

    rows: list[dict] = []
    for sid, group in sorted(by.items()):
        known = [t for t in group if t.get("pnl_known")]
        avg_bps = (sum(float(t.get("bps", 0.0)) for t in known) / len(known)) if known else None
        if len(known) >= MIN_TRIPS_FOR_JUDGEMENT:
            wins = sum(1 for t in known if float(t.get("pnl", 0.0)) > 0)
            win_rate = f"{wins / len(known) * 100:.0f}%"
        else:
            win_rate = "판단 불가"
        rows.append({
            "strategy": sid,
            "n": len(group),
            "n_known": len(known),
            "avg_bps": avg_bps,
            "win_rate": win_rate,
        })
    return rows


def build_ab(all_trips: list[dict], bases: list[str], market: str) -> list[dict]:
    """A/B 갈래(2026-09-03) 한 줄 재료 — 이 시장 것만, **누적** 트립 기준.

    하루치로는 어느 갈래도 30건이 안 되므로 오늘 트립이 아니라 누적 원장을 받는다
    (호출부가 넘긴다). 계산은 전부 `ledger.ab_compare` 가 하고 여기서는 표시할
    칸만 고른다 — 임계도 문구도 새로 만들지 않는다("판단 불가(n<30)" 그대로)."""
    rows = []
    for r in ab_compare(all_trips, bases=bases):
        if r["market"] not in (market, None):
            continue
        rows.append({
            "base": r["base"],
            "n_a": r["baseline"]["n"], "n_b": r["catalyst"]["n"],
            "exp_a": r["baseline"]["expectancy_bp"],
            "exp_b": r["catalyst"]["expectancy_bp"],
            "delta": r["delta_expectancy_bp"],
            "p_value": r["p_value"],
            "reason": r["reason"],
        })
    return rows


def build_performance(pnl: dict | None, trips: list[dict],
                      equity_points: list[dict], market: str) -> dict:
    """1절 — 실현손익·수수료·체결 수·전략별 왕복 성적 + 자본 곡선 전일 대비.

    `pnl`은 `ledger.session_pnl_summary()` 결과(없으면 None), `equity_points`는
    같은 시장의 자본 곡선 점들(날짜 오름차순, 마지막 두 점만 쓴다)."""
    has = bool(pnl and pnl.get("has_trades"))
    equity_delta = None
    equity_now = None
    if equity_points:
        equity_now = float(equity_points[-1].get("total_krw", 0.0))
        if len(equity_points) >= 2:
            equity_delta = equity_now - float(equity_points[-2].get("total_krw", 0.0))
    return {
        "market": market,
        "has_trades": has,
        "n_fills": int(pnl.get("n_fills", 0)) if pnl else 0,
        "n_buys": int(pnl.get("n_buys", 0)) if pnl else 0,
        "n_sells": int(pnl.get("n_sells", 0)) if pnl else 0,
        "net_realized": float(pnl.get("net_realized", 0.0)) if pnl else 0.0,
        "fees": float(pnl.get("fees", 0.0)) if pnl else 0.0,
        "unknown_sells": int(pnl.get("unknown_sells", 0)) if pnl else 0,
        # 이식 정리 제외분(2026-09-02) — `session_pnl_summary`가 빼고 준 것을
        # 조용히 삼키지 않고 한 줄로 밝힌다. 없으면 (0, 0.0).
        "excluded_seeding_n": int((pnl or {}).get("excluded_seeding", {}).get("n", 0)),
        "excluded_seeding_net": (
            float((pnl or {}).get("excluded_seeding", {}).get("gross", 0.0))
            - float((pnl or {}).get("excluded_seeding", {}).get("fees", 0.0))
        ),
        "strategies": _strategy_rows(trips),
        "equity_krw": equity_now,
        "equity_delta_krw": equity_delta,
    }


# ── 2. 지분 변경 ────────────────────────────────────────────────────────

def _lots_from_positions(positions: dict) -> tuple[dict, dict]:
    """portfolio.json positions(그 시장만 필터된 상태) → exposure.build_report가
    원하는 `(lots, prices)`. `lots`는 심볼 → {전략id: 수량}, `prices`는 심볼 →
    평단가(이 리포트는 "읽기만 한다"는 계약이라 실시간 시세를 새로 조회하지
    않는다 — cmd_daily_wrap docstring과 동일 원칙).

    `meta["lots"]`가 없는 레거시 포지션(전략별 lot 구조 이전)은 소유 전략을
    "?"로 담아 그래도 노출에 넣는다 — 모르는 전략이라고 노출 자체를 숨기면
    안 된다(quant.trade.loop._build_exposure_snapshot과 동일 원칙)."""
    lots: dict[str, dict[str, float]] = {}
    prices: dict[str, float] = {}
    for symbol, p in (positions or {}).items():
        qty = float(p.get("qty", 0) or 0)
        if qty <= 0:
            continue
        meta = p.get("meta") or {}
        active = {
            sid: float(lot.get("qty", 0.0))
            for sid, lot in (meta.get("lots") or {}).items()
            if float(lot.get("qty", 0.0)) > 0
        }
        if not active:
            active = {str(meta.get("strategy") or "?"): qty}
        lots[symbol] = active
        prices[symbol] = float(p.get("avg_cost", 0) or 0)
    return lots, prices


def build_exposure_summary(positions: dict, capital_krw: float | None,
                           leverage_of: dict[str, float] | None = None) -> str:
    """2절 꼬리 — 전략 간 합산 노출 한 줄(그 시장 보유분 기준, 2026-08-30).

    `capital_mode: per_strategy`에서 리스크 상한이 전부 전략별 장부 기준이라
    안 보이는 사각지대(quant/control/exposure.py 모듈 docstring)를 마감
    리포트에서도 드러낸다. 시세는 평단가로 저하한다 — 방향(중복·상쇄 존재
    여부)은 시세와 무관하게 정확하다. `leverage_of`가 없으면(오프라인 리포트,
    네트워크 조회 없음) 알려진 상쇄 쌍만 내장 배수로 보강된다
    (`exposure._KNOWN_PAIR_LEVERAGE`)."""
    lots, prices = _lots_from_positions(positions)
    report = _exposure.build_report(
        lots=lots, prices=prices, leverage_of=leverage_of, capital_krw=capital_krw,
    )
    return report.summary_line()


def build_positions(positions: dict, session_trades: list[dict],
                    names: dict[str, str], *,
                    capital_krw: float | None = None,
                    leverage_of: dict[str, float] | None = None) -> dict:
    """2절 — 오늘 늘어난/줄어든/청산된 포지션 + 현재 보유 목록(종목명 필수) +
    전략 간 합산 노출 요약(2026-08-30).

    전일 스냅샷을 따로 두지 않는다. 오늘 체결의 순증감(`net`)과 현재 수량으로
    직전 수량을 역산할 수 있어서다: `qty_before = qty_now - net`. 스냅샷 파일을
    새로 만들면 그 파일이 빠지는 날이 곧 침묵하는 날이 된다.
    """
    net: dict[str, float] = {}
    for t in session_trades:
        sym = str(t.get("symbol", ""))
        if not sym:
            continue
        qty = float(t.get("qty", 0) or 0)
        side = str(t.get("side", "")).upper()
        net[sym] = net.get(sym, 0.0) + (qty if side == "BUY" else -qty)

    held = {
        sym: float(p.get("qty", 0) or 0)
        for sym, p in (positions or {}).items()
        if float(p.get("qty", 0) or 0) > 0
    }

    changes: list[dict] = []
    for sym, delta in sorted(net.items()):
        if abs(delta) < 1e-9:
            continue
        qty_now = held.get(sym, 0.0)
        qty_before = qty_now - delta
        if qty_now <= 1e-9:
            kind = "청산"
        elif qty_before <= 1e-9:
            kind = "신규"
        else:
            kind = "증가" if delta > 0 else "감소"
        changes.append({
            "symbol": sym, "name": names.get(sym, ""), "kind": kind,
            "delta": delta, "qty_now": qty_now,
        })

    holdings = [
        {
            "symbol": sym,
            "name": names.get(sym, ""),
            "qty": qty,
            "avg_cost": float((positions.get(sym) or {}).get("avg_cost", 0) or 0),
            "market": str((positions.get(sym) or {}).get("market") or _market_of(sym)),
        }
        for sym, qty in sorted(held.items())
    ]
    return {
        "changes": changes, "holdings": holdings,
        "exposure_summary": build_exposure_summary(positions, capital_krw, leverage_of),
    }


def _market_of(symbol: str) -> str:
    """6자리 숫자 = KR — 저장소 전역(ledger/assembly)과 같은 추론."""
    return market_of_symbol(symbol)


# ── 3·4. 이상 / 변경 ────────────────────────────────────────────────────

# 미뤄둔 알림은 최대 이만큼만 찍는다 — 나머지는 건수와 파일 경로로 가리킨다.
# "간단 명료"가 요구사항이라 큐 전체를 붙여넣으면 리포트가 곧 예전의 시끄러운
# 텔레그램이 된다.
MAX_DEFERRED_LINES = 12


def build_deferred(rows: list[dict]) -> dict:
    """알림 게이트(`server/scripts/lib/notify.sh`)가 장중에 미뤄둔 알림 요약.

    큐 줄 계약은 그쪽 파일이 정의한다: `{ts, source, text, level}`.
    `level="auto"`는 장외였다면 이미 나갔을 것이라 위로 올린다.

    **이 목록은 "이상"으로 세지 않는다** — 대부분 백필 결과·요약 같은 정보성이고,
    캡션의 "이상 N건"에 섞으면 그 숫자가 경보로서 의미를 잃는다."""
    ordered = sorted(rows, key=lambda r: (0 if str(r.get("level")) == "auto" else 1,
                                          str(r.get("ts", ""))))
    shown = [
        {"source": str(r.get("source") or "?"),
         "text": str(r.get("text") or "").strip().splitlines()[0][:160] if r.get("text") else "",
         "level": str(r.get("level") or "")}
        for r in ordered[:MAX_DEFERRED_LINES]
    ]
    return {"total": len(rows), "shown": shown}


# ── 5. 지수 대비 성적 ──────────────────────────────────────────────────

def build_alpha(series: list[tuple], market: str) -> dict:
    """5절 — 지수 대비 초과수익(알파). 계산 자체는 `control.alpha.wrap_section()`
    이 이미 순수하게 정의해 뒀다(그 모듈 docstring 통합 계약 그대로) — 여기서는
    호출부(`apps/cli.py`)가 원장·벤치마크 일봉에서 만든 (날짜, 우리%, 지수%,
    알파pp) 시퀀스를 그대로 넘길 뿐이다. 표본이 없으면(빈 시퀀스)
    `wrap_section()`이 알아서 "표본 없음"을 낸다 — 여기서 지어내지 않는다."""
    return _alpha.wrap_section(series, market)


# ── 6. 체결 비용 ────────────────────────────────────────────────────────

def build_cost_section(trips: list[dict], spread_rows: list[dict], market: str,
                       kr_etf: set[str] | None = None) -> dict:
    """6절 — 오늘 종결된 왕복의 실측 비용(수수료 + 당시 스프레드) vs 우리 비용
    가정(`cost_model.ASSUMED_ROUND_TRIP_BP`) — 가정이 낙관인지 보수인지
    (2026-08-30, `quant.control.cost_model.compare_spread_cost` 재사용).

    US는 단일 가정(그룹 하나), KR은 ETF/개별주로 갈라 각각 대조한다(세율이
    갈리므로 하나로 뭉개면 판정 자체가 무의미해진다) — `kr_etf`가 없으면
    (cmd_daily_wrap은 네트워크를 쓰지 않아 보통 캐시 파일에서 온다) 전부
    개별주로 본다("모르면 안전한 쪽", assembly.py의 kr_etf 판정과 동일 원칙).
    그룹별로 스프레드 표본이 없으면 그 그룹은 `comparison=None`("표본 없음").

    2026-09-02: 기준값이 `execution` 설정에서 유도된 **수수료·세금만**으로
    바뀌었다(비용 표 3개가 서로 달랐던 문제, cost_model 상단 주석). 실측은
    수수료+스프레드라서 스프레드가 있는 한 판정이 "낙관" 쪽으로 나오는 게
    정상이다 — 그 차이가 곧 우리가 무는 스프레드다."""
    kr_etf = kr_etf or set()
    groups: list[dict] = []
    if market == "KR":
        etf_trips = [t for t in trips if str(t.get("symbol")) in kr_etf]
        stock_trips = [t for t in trips if str(t.get("symbol")) not in kr_etf]
        for label, group_trips, key in (
            ("KR ETF", etf_trips, "KR_ETF"), ("KR 개별주", stock_trips, "KR_STOCK"),
        ):
            cmp = (
                _cost_model.compare_spread_cost(
                    group_trips, spread_rows, _cost_model.ASSUMED_ROUND_TRIP_BP[key], scope=label,
                ) if group_trips else None
            )
            groups.append({"label": label, "comparison": cmp.to_dict() if cmp else None})
    else:
        cmp = (
            _cost_model.compare_spread_cost(
                trips, spread_rows, _cost_model.ASSUMED_ROUND_TRIP_BP["US"], scope=market,
            ) if trips else None
        )
        groups.append({"label": market, "comparison": cmp.to_dict() if cmp else None})
    return {"groups": groups}


# ── 7. 오늘의 매매 리뷰 (2026-09-07 오너 요청) ──────────────────────────────
#
# 별도 상시 페이지(`quant/report/render/trade_review.py`, Plotly 인터랙티브
# 차트)와 같은 원장(`quant.control.trade_review.build_trade_review`)을 본다 —
# 여기서는 그 결과 dict(`review`)를 인자로 받기만 한다(이 모듈 자체는 원장을
# 읽지 않는다, "순수 조립" 원칙). `review`가 `None`이면(그날 아직 안 돌았거나
# 실패) "표본 없음"으로 절 자체는 남긴다 — 다른 절(alpha 등)과 같은 관례.
#
# **차트는 인라인 SVG뿐이다.** 이 문서 자체가 "외부 요청 0"(모듈 docstring)
# 계약이라 Plotly/CDN을 못 쓴다 — `_svg_candle`이 `bars_window`(review JSON에
# 이미 포함된 캔들 배열)로 서버사이드 SVG를 그린다. `bars_window`가 비어 있으면
# (오늘 처음 도는 `cmd_daily_wrap`이 네트워크 없이 즉석에서 만든 review라 봉이
# 없을 수 있다) 차트를 생략하고 "봉 없음"만 낸다 — 표를 지어내지 않는다.


def build_trade_review_section(review: dict | None, url: str | None = None) -> dict:
    """`review`(build_trade_review 반환값, 없으면 `None`) → 렌더용 평범한 dict.

    `url`은 상시 페이지 링크 — 이 문서 본문(HTML)에는 절대 넣지 않는다(외부
    요청 0 규율, `test_html_has_no_external_requests`). 캡션(`caption_line`,
    HTML이 아니라 텔레그램 메시지 필드)에만 쓰인다."""
    if not review or not review.get("groups"):
        return {"available": False, "url": url, "totals": None, "per_strategy": [], "cards": []}
    totals = review["summary"]["totals"]
    per_strategy = [
        {"strategy_id": sid, **stats}
        for sid, stats in sorted(review["summary"]["per_strategy"].items())
    ]
    cards = [_trade_review_card(g) for g in review["groups"]]
    return {
        "available": True, "url": url, "totals": totals, "per_strategy": per_strategy,
        "cards": cards, "market": review.get("market"), "date": review.get("date"),
    }


def _trade_review_card(g: dict) -> dict:
    return {
        "strategy_id": g["strategy_id"], "symbol": g["symbol"], "status": g["status"],
        "market": g["market"], "tz_label": g.get("tz_label") or g["market"],
        "entry_price": g["entry_price"],
        # ts는 build_trade_review가 이미 시장 로컬 벽시계(오프셋 없음)로 준다
        # (2026-09-07 수리 — `to_market_local_iso`) — 여기서 다시 변환하지 않는다.
        "entry_ts": g["entries"][0]["ts"] if g["entries"] else None,
        "exit_ts": g["exits"][-1]["ts"] if g["exits"] else None,
        "exit_price": g["exits"][-1]["price"] if g["exits"] else None,
        "pnl": g["pnl"], "pnl_bp": g["pnl_bp"], "pnl_known": g["pnl_known"],
        "band": g["band"], "mfe_bp": g["mfe_bp"], "mae_bp": g["mae_bp"],
        "bars_window": g.get("bars_window") or [],
        "bars_source": g.get("bars_source"),
        "pattern": g["thinking"]["entry_parsed"].get("pattern"),
        "exit_reason": g["thinking"]["exit_reason"],
        "gates": g["thinking"]["entry_parsed"].get("gates") or [],
        "strategy_params": g["thinking"].get("strategy_params") or {},
    }


# 파라미터 표 전체(20줄+)를 보여주지 않고, 그 트립을 실제로 결정한 것들만 —
# 표준 페이지(trade_review.html.j2)의 `key_params_summary` 매크로와 같은
# 우선순위 목록(2026-09-07, 오너 지적: "25줄짜리 표는 4개만 보고 싶다").
_KEY_PARAM_NAMES = (
    "stop_mode", "partial_take_r", "trail_bp", "adx_min", "trend_gate_mode",
    "take_profit_bps", "stop_pct", "take_profit_pct",
)


def _key_param_summary(params: dict, limit: int = 4) -> str:
    picked = [f"{n}={params[n]}" for n in _KEY_PARAM_NAMES if n in params][:limit]
    return " · ".join(picked) if picked else ""


def build_sections(*, market: str, on: date, pnl: dict | None, trips: list[dict],
                   equity_points: list[dict], positions: dict,
                   session_trades: list[dict], names: dict[str, str],
                   issues: list[str], commits: list[str] | None,
                   deferred: list[dict] | None = None,
                   alpha_series: list[tuple] | None = None,
                   leverage_of: dict[str, float] | None = None,
                   spread_rows: list[dict] | None = None,
                   kr_etf: set[str] | None = None,
                   all_trips: list[dict] | None = None,
                   ab_bases: list[str] | None = None,
                   trade_review: dict | None = None,
                   trade_review_url: str | None = None) -> dict:
    """6개 절을 소유자가 지정한 순서(실적→지분→이상→변경→지수 대비 성적→체결
    비용)로 조립한다.

    `commits`가 `None`이면 "git 을 못 읽었다"는 뜻 — 4절 자체를 생략한다(빈
    리스트는 "오늘 배포 없음"이라 절을 남긴다. 둘을 뭉개지 않는다).
    `alpha_series`는 없으면(`None`/빈 시퀀스) 5절이 "표본 없음"으로 나온다 —
    5절 자체는 항상 있다(계산 불가와 절 부재는 다른 뜻이라 뭉개지 않는다).
    `leverage_of`는 2절 꼬리 합산 노출 요약(build_exposure_summary)에만 쓰인다 —
    없으면(cmd_daily_wrap은 네트워크를 쓰지 않아 보통 없다) 알려진 상쇄 쌍만
    내장 배수로 보강된다. `spread_rows`/`kr_etf`는 6절(build_cost_section)
    재료 — 둘 다 없으면(`None`/빈 리스트) 6절이 그룹별로 "표본 없음"을 낸다.
    `trade_review`(2026-09-07)는 이미 조립된 `build_trade_review` 결과(또는
    `None`) — 이 함수 자체는 원장을 읽지 않는다. `trade_review_url`은 7절
    캡션에만 쓰인다(본문 HTML에는 절대 안 들어간다, 위 절 docstring 참고)."""
    performance = build_performance(pnl, trips, equity_points, market)
    # A/B 갈래 줄(2026-09-03) — `trips`(오늘)가 아니라 `all_trips`(누적)로 잰다.
    # 둘 중 하나라도 없으면 절을 만들지 않는다(빈 표는 "0건"처럼 읽혀 거짓말이 된다).
    performance["ab"] = (
        build_ab(all_trips, ab_bases, market) if all_trips is not None and ab_bases else []
    )
    return {
        "market": market,
        "date": on.isoformat(),
        "performance": performance,
        "positions": build_positions(
            positions, session_trades, names,
            capital_krw=performance.get("equity_krw"), leverage_of=leverage_of,
        ),
        "issues": list(issues),
        "deferred": build_deferred(deferred or []),
        "commits": None if commits is None else list(commits)[:10],
        "alpha": build_alpha(list(alpha_series or []), market),
        "cost": build_cost_section(trips, list(spread_rows or []), market, kr_etf),
        "trade_review": build_trade_review_section(trade_review, trade_review_url),
    }


def caption_line(sections: dict) -> str:
    """텔레그램 sendDocument 캡션 — 파일을 열기 전에 알아야 할 것만.

    매매 리뷰(7절)가 있으면 둘째 줄에 한 줄 더 붙인다(2026-09-07 오너 요청:
    "문서의 텍스트 메시지에 매매 리뷰 한 줄") — 캡션은 HTML이 아니라 텔레그램
    메시지 필드라 URL을 그대로 넣어도 "외부 요청 0"(본문 HTML 전용 규율)을
    건드리지 않는다."""
    perf = sections["performance"]
    market, on = sections["market"], sections["date"]
    mm, dd = on.split("-")[1:]
    head = f"📄 {int(mm)}/{int(dd)} {market} 마감"
    pnl = ("거래 없음" if not perf["has_trades"]
           else f"실현 {fmt_amount(perf['net_realized'], market)}")
    held = len(sections["positions"]["holdings"])
    issues = sections["issues"]
    ops = "이상 없음" if not issues else f"이상 {len(issues)}건"
    line = f"{head} — {pnl} · 보유 {held}종목 · {ops}"

    tr = sections.get("trade_review") or {}
    if tr.get("available") and tr.get("totals"):
        t = tr["totals"]
        bits = [f"{t['n']}트립"]
        if t.get("win_rate") is not None:
            bits.append(f"승률 {t['win_rate']:.0%}")
        if t.get("net_bp") is not None:
            bits.append(f"순 {t['net_bp']:+.0f}bp")
        tr_line = "📈 매매 리뷰: " + " · ".join(bits)
        if tr.get("url"):
            tr_line += f" — {tr['url']}"
        line += f"\n{tr_line}"
    return line


# ── 렌더 ────────────────────────────────────────────────────────────────

_CSS = """
:root{--bg:#ffffff;--fg:#16181d;--muted:#6b7280;--line:#e5e7eb;--card:#f6f7f9;
--up:#0a7d33;--down:#c1121f;--flat:#6b7280}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e9eaec;--muted:#9aa0a6;
--line:#2c3036;--card:#1d2025;--up:#4ade80;--down:#ff7b7b;--flat:#9aa0a6}}
*{box-sizing:border-box}
body{margin:0;padding:14px;background:var(--bg);color:var(--fg);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",
"Malgun Gothic",sans-serif;-webkit-text-size-adjust:100%}
h1{font-size:18px;margin:0 0 2px}
.sub{color:var(--muted);font-size:13px;margin-bottom:12px}
.key{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;margin-bottom:16px}
.key div{display:flex;justify-content:space-between;gap:12px;padding:2px 0}
.key .k{color:var(--muted)}
.key .v{font-weight:600;text-align:right}
h2{font-size:15px;margin:18px 0 6px;padding-bottom:4px;border-bottom:1px solid var(--line)}
p{margin:4px 0}
ul{margin:4px 0;padding-left:18px}
li{margin:2px 0}
.scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;min-width:340px;font-size:13px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;
white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600}
.up{color:var(--up)}
.down{color:var(--down)}
.flat{color:var(--flat)}
.muted{color:var(--muted)}
.tr-card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 12px;margin:10px 0}
.tr-chart-wrap{width:100%;overflow-x:auto;margin-bottom:2px}
.tr-chart{display:block;max-width:100%;height:auto}
.tr-date{font-size:11px;margin-bottom:4px}
.tr-src{font-size:11px;margin-top:0}
.key-params{font-size:12px}
"""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _render_performance(perf: dict) -> str:
    market = perf["market"]
    seeding_n = int(perf.get("excluded_seeding_n", 0))
    seeding_html = (
        f'<p class="muted">이식 정리 {seeding_n}건 제외: '
        f'{_money(perf.get("excluded_seeding_net", 0.0), market)} '
        "(프로그램 매매 아님)</p>"
    ) if seeding_n else ""
    if not perf["has_trades"]:
        out = ["<p>오늘 거래 없음</p>"]
        if seeding_html:
            out.append(seeding_html)
    else:
        unk = perf["unknown_sells"]
        out = [
            f'<p>체결 {perf["n_fills"]}건 (매수 {perf["n_buys"]} · 매도 {perf["n_sells"]})</p>',
            f'<p>실현손익(수수료 차감) {_money(perf["net_realized"], market)}'
            + (f' <span class="muted">손익미상 매도 {unk}건 제외</span>' if unk else "")
            + "</p>",
            f'<p>수수료 {_esc(fmt_amount(perf["fees"], market, signed=False))}</p>',
        ]
        if seeding_html:
            out.append(seeding_html)
        rows = perf["strategies"]
        if rows:
            out.append(_table(
                ["전략", "왕복", "평균bp", "승률"],
                [[
                    _esc(r["strategy"]),
                    str(r["n"]),
                    ("판단 불가" if r["avg_bps"] is None
                     else f'<span class="{_cls(r["avg_bps"])}">{r["avg_bps"]:+,.1f}</span>'),
                    _esc(r["win_rate"]),
                ] for r in rows],
            ))
        else:
            out.append('<p class="muted">오늘 종결된 왕복 없음</p>')

    for row in perf.get("ab", []):
        head = (f'A/B {_esc(row["base"])}: 기준 n={row["n_a"]}'
                f' vs 촉매 n={row["n_b"]}')
        def _bp(v):
            return "-" if v is None else f"{v:+,.1f}bp"
        body = f'기대값 {_bp(row["exp_a"])} vs {_bp(row["exp_b"])}'
        tail = (_esc(row["reason"]) if row["reason"]
                else f'차이 {row["delta"]:+,.1f}bp · p={row["p_value"]:.3f}')
        out.append(f'<p class="muted">{head} · {body} — {tail}</p>')

    delta = perf["equity_delta_krw"]
    if perf["equity_krw"] is None:
        out.append('<p class="muted">자본 곡선: 기록 없음</p>')
    elif delta is None:
        out.append(f'<p>자본 {perf["equity_krw"]:,.0f}원 '
                   f'<span class="muted">(전일 값 없음 — 대비 판단 불가)</span></p>')
    else:
        out.append(f'<p>자본 {perf["equity_krw"]:,.0f}원 '
                   f'(전일 대비 {_money(delta, "KR")})</p>')
    return "".join(out)


def _render_positions(pos: dict) -> str:
    out = []
    changes = pos["changes"]
    if not changes:
        out.append("<p>오늘 지분 변경 없음</p>")
    else:
        out.append(_table(
            ["종목", "구분", "수량 변화", "보유 수량"],
            [[
                _esc(_label(c["symbol"], c["name"])),
                _esc(c["kind"]),
                f'<span class="{_cls(c["delta"])}">{c["delta"]:+,.4g}</span>',
                f'{c["qty_now"]:,.4g}',
            ] for c in changes],
        ))

    holdings = pos["holdings"]
    if not holdings:
        out.append("<p>현재 보유 없음</p>")
    else:
        out.append(f"<p>현재 보유 {len(holdings)}종목</p>")
        out.append(_table(
            ["종목", "수량", "평균단가"],
            [[
                _esc(_label(h["symbol"], h["name"])),
                f'{h["qty"]:,.4g}',
                _esc(fmt_amount(h["avg_cost"], h["market"], signed=False)),
            ] for h in holdings],
        ))
    out.append(
        f'<p class="muted">전략 간 합산 노출: {_esc(pos["exposure_summary"])}</p>'
    )
    return "".join(out)


def _label(symbol: str, name: str) -> str:
    """종목명이 있으면 "이름(코드)". 없으면 코드만 — 없는 이름을 지어내지 않는다."""
    return f"{name}({symbol})" if name else symbol


def _render_deferred(deferred: dict) -> str:
    """3절 꼬리 — 장중에 미뤄둔 알림. 하나도 없으면 아무것도 그리지 않는다
    (빈 소제목은 그 자체가 소음이다)."""
    total = int(deferred.get("total", 0))
    if total == 0:
        return ""
    shown = deferred.get("shown") or []
    items = "".join(
        f'<li><span class="muted">[{_esc(d["source"])}]</span> {_esc(d["text"])}</li>'
        for d in shown
    )
    more = ("" if total <= len(shown)
            else f'<p class="muted">…외 {total - len(shown)}건 (data/notify_queue.jsonl)</p>')
    return f'<p class="muted">장중에 미뤄둔 알림 {total}건</p><ul>{items}</ul>{more}'


def _render_alpha(sec: dict) -> str:
    """5절 — 알파 핵심 줄(`sec["lines"]`) + 최근 5일 표(`sec["rows"]`, 비어 있으면
    표를 그리지 않는다 — 표본 없음이면 lines 자체가 이미 그렇게 말한다)."""
    lines = sec.get("lines") or []
    out = ["<ul>" + "".join(f"<li>{_esc(str(line))}</li>" for line in lines) + "</ul>"]
    rows = sec.get("rows") or []
    if rows:
        out.append(_table(
            ["날짜", "우리%", "지수%", "알파pp"],
            [[
                _esc(str(r["date"])),
                f'{r["our_pct"]:+.2f}',
                f'{r["bench_pct"]:+.2f}',
                f'<span class="{_cls(r["alpha_pp"])}">{r["alpha_pp"]:+.2f}</span>',
            ] for r in rows],
        ))
    return "".join(out)


def _render_cost(sec: dict) -> str:
    """6절 — 그룹별(US 단일 / KR은 ETF·개별주) 한 줄 판정. 표본 없는 그룹은
    "표본 없음"만 낸다(지어내지 않는다)."""
    items = []
    for g in sec.get("groups", []):
        cmp = g.get("comparison")
        label = _esc(str(g.get("label", "")))
        if cmp is None:
            items.append(f'<li>{label}: <span class="muted">표본 없음</span></li>')
            continue
        items.append(
            f'<li>{label}: 실측 {cmp["observed_bp"]:.1f}bp vs 가정 {cmp["assumed_bp"]:.1f}bp'
            f' ({cmp["n_priced"]}/{cmp["n_trips"]}건) — 가정이 {_esc(cmp["verdict"])}적</li>'
        )
    return "<ul>" + "".join(items) + "</ul>"


# ── 7절 렌더 — 인라인 SVG 캔들(외부 요청 0 규율, 이 파일 docstring "HTML 규율") ──

def _nearest_bar_index(bars_window: list[dict], ts: str | None) -> int:
    """`ts`(체결 시각)와 가장 가까운 `bars_window` 인덱스. 봉 경계와 체결 초
    단위가 정확히 안 맞는 게 정상이라(체결은 봉 중간 아무 때나 난다) 정확히
    일치를 찾지 않고 최소 시간차로 고른다."""
    if not bars_window:
        return 0
    if ts is None:
        return len(bars_window) - 1
    try:
        target = datetime.fromisoformat(ts)
    except ValueError:
        return len(bars_window) - 1
    best_i, best_diff = 0, None
    for i, b in enumerate(bars_window):
        try:
            bt = datetime.fromisoformat(b["ts"])
        except (ValueError, KeyError):
            continue
        diff = abs((bt - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_i, best_diff = i, diff
    return best_i


def _svg_candle(card: dict, w: float = 300.0, h: float = 130.0) -> str:
    """카드 하나짜리 캔들 SVG. index 기반 x좌표(`charts.sparkline`과 같은 방식
    — 실제 봉 간 시간차는 시각 목적상 무시해도 된다). 봉이 2개 미만이면 빈
    문자열(호출부가 "봉 없음" 문구로 대체한다) — 차트를 지어내지 않는다."""
    bars = card.get("bars_window") or []
    n = len(bars)
    if n < 2:
        return ""
    entry_price = card["entry_price"]
    band = card.get("band") or {}
    stop, target = band.get("stop"), band.get("target")

    values = [b["high"] for b in bars] + [b["low"] for b in bars]
    values += [v for v in (stop, target, entry_price) if v is not None]
    lo, hi = min(values), max(values)
    if hi <= lo:
        hi = lo + max(abs(lo) * 0.01, 1.0)
    pad = 8.0
    plot_h = h - pad * 2

    def y(v: float) -> float:
        return pad + (hi - v) / (hi - lo) * plot_h

    step = w / n
    body_w = max(step * 0.5, 1.0)
    entry_i = _nearest_bar_index(bars, card.get("entry_ts"))
    exit_i = _nearest_bar_index(bars, card.get("exit_ts")) if card.get("exit_ts") else n - 1
    x_entry, x_exit = entry_i * step + step / 2, exit_i * step + step / 2
    x0, x1 = sorted((x_entry, x_exit))

    parts = [
        f'<svg class="tr-chart" width="{w:.0f}" height="{h:.0f}" viewBox="0 0 {w:.0f} {h:.0f}" '
        f'role="img" aria-label="{_esc(str(card.get("symbol", "")))} 캔들 차트">'
        # 보유 구간(진입→청산/마지막 봉) — 노랑 배경.
        f'<rect x="{x0:.1f}" y="0" width="{max(x1 - x0, 1):.1f}" height="{h:.0f}" '
        f'fill="#E6B414" opacity="0.12"/>'
    ]
    if target is not None:
        yt0, yt1 = sorted((y(entry_price), y(target)))
        parts.append(f'<rect x="{x0:.1f}" y="{yt0:.1f}" width="{max(x1 - x0, 1):.1f}" '
                     f'height="{max(yt1 - yt0, 0.5):.1f}" fill="#0A7D33" opacity="0.16"/>')
    if stop is not None:
        ys0, ys1 = sorted((y(entry_price), y(stop)))
        parts.append(f'<rect x="{x0:.1f}" y="{ys0:.1f}" width="{max(x1 - x0, 1):.1f}" '
                     f'height="{max(ys1 - ys0, 0.5):.1f}" fill="#C1121F" opacity="0.16"/>')
    ma_pts = [(i * step + step / 2, y(b["ma60"])) for i, b in enumerate(bars) if b.get("ma60") is not None]
    if len(ma_pts) >= 2:
        d = " ".join(f"{x:.1f},{yy:.1f}" for x, yy in ma_pts)
        parts.append(f'<polyline points="{d}" fill="none" stroke="#D98A1E" stroke-width="1.3"/>')
    for i, b in enumerate(bars):
        x = i * step + step / 2
        up = b["close"] >= b["open"]
        color = "#C1272D" if up else "#2F5FC4"  # KR 관례: 상승=적, 하락=청
        parts.append(f'<line x1="{x:.1f}" y1="{y(b["high"]):.1f}" x2="{x:.1f}" y2="{y(b["low"]):.1f}" '
                     f'stroke="{color}" stroke-width="1"/>')
        yo, yc = y(b["open"]), y(b["close"])
        ybt, ybb = sorted((yo, yc))
        parts.append(f'<rect x="{x - body_w / 2:.1f}" y="{ybt:.1f}" width="{body_w:.1f}" '
                     f'height="{max(ybb - ybt, 0.8):.1f}" fill="{color}"/>')
    parts.append(f'<circle cx="{x_entry:.1f}" cy="{y(entry_price):.1f}" r="3.2" fill="#0052FF"/>')
    if card.get("exit_price") is not None:
        parts.append(f'<circle cx="{x_exit:.1f}" cy="{y(card["exit_price"]):.1f}" r="3.2" fill="#A6570A"/>')
    parts.append("</svg>")
    return "".join(parts)


def _render_trade_review_card(c: dict) -> str:
    market = c["market"]
    tz_label = c.get("tz_label") or market
    if c["status"] == "closed" and c["pnl_known"]:
        cls = _cls(c["pnl"])
        badge = f"{fmt_amount(c['pnl'], market)} ({c['pnl_bp']:+.0f}bp)"
    elif c["status"] == "open":
        cls, badge = "muted", "보유중"
    else:
        cls, badge = "muted", "손익 미상"
    svg = _svg_candle(c)
    chart_html = (
        f'<div class="tr-chart-wrap">{svg}</div>'
        f'<p class="muted tr-src">봉 출처: {_esc(c.get("bars_source") or "?")} · {_esc(tz_label)} 기준</p>'
        if svg else '<p class="muted">봉 없음</p>'
    )
    # ts는 이미 시장 로컬 벽시계(오프셋 없음, 2026-09-07 수리) — 여기서 HH:MM만
    # 뽑으면 그대로 맞는 로컬 시각이다. 날짜는 카드 헤더에 한 번만 보여준다.
    entry_date = (c.get("entry_ts") or "").split("T")[0]
    entry_hhmm = (c.get("entry_ts") or "").split("T")[-1][:5]
    exit_hhmm = (c.get("exit_ts") or "").split("T")[-1][:5] if c.get("exit_ts") else None
    fill_line = f"매수 {entry_hhmm} {fmt_amount(c['entry_price'], market, signed=False)}"
    if exit_hhmm and c.get("exit_price") is not None:
        fill_line += f" · 매도 {exit_hhmm} {fmt_amount(c['exit_price'], market, signed=False)}"
    band = c.get("band") or {}
    band_bits = []
    if band.get("stop") is not None:
        band_bits.append(f"손절 {fmt_amount(band['stop'], market, signed=False)}" + (f"({band['stop_note']})" if band.get("stop_note") else ""))
    if band.get("target") is not None:
        band_bits.append(f"목표 {fmt_amount(band['target'], market, signed=False)}" + (f"({band['target_note']})" if band.get("target_note") else ""))
    out = [
        '<div class="tr-card">',
        f'<p class="muted tr-date">{_esc(entry_date)} · {_esc(tz_label)}</p>',
        chart_html,
        f'<p><b>{_esc(c["symbol"])}</b> <span class="muted">{_esc(c["strategy_id"])}</span> '
        f'<span class="{cls}">{_esc(badge)}</span></p>',
        f'<p class="muted">{_esc(fill_line)}</p>',
    ]
    if band_bits:
        out.append(f'<p class="muted">{_esc(" · ".join(band_bits))}</p>')
    if c.get("pattern"):
        out.append(f'<p class="muted">생각: {_esc(c["pattern"])}</p>')
    if c.get("exit_reason"):
        out.append(f'<p class="muted">청산: {_esc(c["exit_reason"])}</p>')
    key_params = _key_param_summary(c.get("strategy_params") or {})
    if key_params:
        out.append(f'<p class="muted key-params">파라미터: {_esc(key_params)}</p>')
    out.append("</div>")
    return "".join(out)


def _render_trade_review_section(sec: dict) -> str:
    """7절 본문. `sec["url"]`은 절대 쓰지 않는다(캡션 전용, 위 절 docstring)."""
    if not sec.get("available"):
        return '<p class="muted">오늘 진입 체결 없음</p>'
    t = sec["totals"]
    bits = [f"{t['n']}건" + (f" (+{t['n_open']} 보유중)" if t.get("n_open") else "")]
    if t.get("win_rate") is not None:
        bits.append(f"승률 {t['win_rate']:.0%}")
    if t.get("net_bp") is not None:
        bits.append(f"순 {t['net_bp']:+.0f}bp")
    out = [f'<p>{" · ".join(bits)}</p>']
    if sec["per_strategy"]:
        rows = [
            [s["strategy_id"], str(s["n"]),
             (f"{s['win_rate']:.0%}" if s.get("win_rate") is not None else "-"),
             (f"{s['net_bp']:+.0f}bp" if s.get("net_bp") is not None else "-"),
             (s.get("best") or "-"), (s.get("worst") or "-")]
            for s in sec["per_strategy"]
        ]
        out.append('<div class="scroll">' + _table(["전략", "n", "승률", "순bp", "최고", "최저"], rows) + "</div>")
    out.extend(_render_trade_review_card(c) for c in sec["cards"])
    if sec.get("date") and sec.get("market"):
        y, m, d = sec["date"].split("-")
        rel_path = f"out/{y}/{int(m):02d}/{int(d):02d}/{sec['market']}_trade_review.html"
        out.append(f'<p class="muted">전체 인터랙티브 카드: {_esc(rel_path)}</p>')
    return "".join(out)


def render_html(sections: dict) -> str:
    """4절 HTML 한 장. 외부 요청 0 — 스타일은 인라인, 이미지·스크립트 없음."""
    market, on = sections["market"], sections["date"]
    perf = sections["performance"]
    issues = sections["issues"]
    commits = sections["commits"]

    # 상단 4~5줄 요약 — 한 화면에 이것만 들어와도 하루가 파악돼야 한다.
    held = len(sections["positions"]["holdings"])
    key = [
        ("실현손익", (_money(perf["net_realized"], market) if perf["has_trades"]
                  else '<span class="muted">거래 없음</span>')),
        ("체결", f'{perf["n_fills"]}건'),
        ("보유", f"{held}종목"),
        ("이상", ('<span class="muted">없음</span>' if not issues
                else f'<span class="down">{len(issues)}건</span>')),
    ]
    delta = perf["equity_delta_krw"]
    if delta is not None:
        key.append(("자본 전일 대비", _money(delta, "KR")))

    parts = [
        "<!doctype html>",
        '<html lang="ko"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(market)} 마감 요약 {_esc(on)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>{_esc(market)} 마감 요약 · {_esc(on)}</h1>",
        '<div class="sub">오늘 하루 무슨 일이 있었나 — 실적 · 지분 · 문제 · 변경</div>',
        '<div class="key">',
        "".join(f'<div><span class="k">{_esc(k)}</span><span class="v">{v}</span></div>'
                for k, v in key),
        "</div>",
        f"<h2>1. {SECTION_TITLES[0]}</h2>",
        _render_performance(perf),
        f"<h2>2. {SECTION_TITLES[1]}</h2>",
        _render_positions(sections["positions"]),
        f"<h2>3. {SECTION_TITLES[2]}</h2>",
        ("<p>없음</p>" if not issues
         else "<ul>" + "".join(f"<li>{_esc(i)}</li>" for i in issues) + "</ul>"),
        _render_deferred(sections.get("deferred") or {"total": 0, "shown": []}),
    ]
    if commits is not None:
        parts.append(f"<h2>4. {SECTION_TITLES[3]}</h2>")
        parts.append("<p>오늘 배포된 커밋 없음</p>" if not commits
                     else "<ul>" + "".join(f"<li>{_esc(c)}</li>" for c in commits) + "</ul>")
    parts.append(f"<h2>5. {SECTION_TITLES[4]}</h2>")
    parts.append(_render_alpha(sections.get("alpha") or {"lines": [], "rows": []}))
    parts.append(f"<h2>6. {SECTION_TITLES[5]}</h2>")
    parts.append(_render_cost(sections.get("cost") or {"groups": []}))
    parts.append(f"<h2>7. 📈 {SECTION_TITLES[6]}</h2>")
    parts.append(_render_trade_review_section(sections.get("trade_review") or {"available": False}))
    parts.append("</body></html>")
    return "".join(parts)
