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
from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT

# 절 제목 — 소유자가 지정한 순서 그대로. 순서를 바꾸지 않는다. "지수 대비 성적"은
# 2026-08-29에 추가됐다(`alpha.py` 모듈 docstring — "항상 지수 위에서 노는 것").
# 기존 1~4절 번호를 그대로 두려고 맨 끝에 붙였다 — 4절(변경된 점)은 git 을 못
# 읽으면 절 자체가 생략되므로, 그 경우 번호가 3 → 5로 건너뛸 수 있다(기존에도
# 4절이 조건부로 사라지는 설계라 새 사실이 아니다). "체결 비용"은 2026-08-30에
# 같은 이유로(조건부 절 번호 흔들림 회피) 맨 끝에 붙었다.
SECTION_TITLES = (
    "오늘의 실적", "지분 변경", "문제 발견 및 개선", "변경된 점", "지수 대비 성적",
    "체결 비용",
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
    return "KR" if (symbol.isdigit() and len(symbol) == 6) else "US"


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
    그룹별로 스프레드 표본이 없으면 그 그룹은 `comparison=None`("표본 없음")."""
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


def build_sections(*, market: str, on: date, pnl: dict | None, trips: list[dict],
                   equity_points: list[dict], positions: dict,
                   session_trades: list[dict], names: dict[str, str],
                   issues: list[str], commits: list[str] | None,
                   deferred: list[dict] | None = None,
                   alpha_series: list[tuple] | None = None,
                   leverage_of: dict[str, float] | None = None,
                   spread_rows: list[dict] | None = None,
                   kr_etf: set[str] | None = None) -> dict:
    """6개 절을 소유자가 지정한 순서(실적→지분→이상→변경→지수 대비 성적→체결
    비용)로 조립한다.

    `commits`가 `None`이면 "git 을 못 읽었다"는 뜻 — 4절 자체를 생략한다(빈
    리스트는 "오늘 배포 없음"이라 절을 남긴다. 둘을 뭉개지 않는다).
    `alpha_series`는 없으면(`None`/빈 시퀀스) 5절이 "표본 없음"으로 나온다 —
    5절 자체는 항상 있다(계산 불가와 절 부재는 다른 뜻이라 뭉개지 않는다).
    `leverage_of`는 2절 꼬리 합산 노출 요약(build_exposure_summary)에만 쓰인다 —
    없으면(cmd_daily_wrap은 네트워크를 쓰지 않아 보통 없다) 알려진 상쇄 쌍만
    내장 배수로 보강된다. `spread_rows`/`kr_etf`는 6절(build_cost_section)
    재료 — 둘 다 없으면(`None`/빈 리스트) 6절이 그룹별로 "표본 없음"을 낸다."""
    performance = build_performance(pnl, trips, equity_points, market)
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
    }


def caption_line(sections: dict) -> str:
    """텔레그램 sendDocument 캡션 — **한 줄**. 파일을 열기 전에 알아야 할 것만."""
    perf = sections["performance"]
    market, on = sections["market"], sections["date"]
    mm, dd = on.split("-")[1:]
    head = f"📄 {int(mm)}/{int(dd)} {market} 마감"
    pnl = ("거래 없음" if not perf["has_trades"]
           else f"실현 {fmt_amount(perf['net_realized'], market)}")
    held = len(sections["positions"]["holdings"])
    issues = sections["issues"]
    ops = "이상 없음" if not issues else f"이상 {len(issues)}건"
    return f"{head} — {pnl} · 보유 {held}종목 · {ops}"


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
"""


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _render_performance(perf: dict) -> str:
    market = perf["market"]
    if not perf["has_trades"]:
        out = ["<p>오늘 거래 없음</p>"]
    else:
        unk = perf["unknown_sells"]
        out = [
            f'<p>체결 {perf["n_fills"]}건 (매수 {perf["n_buys"]} · 매도 {perf["n_sells"]})</p>',
            f'<p>실현손익(수수료 차감) {_money(perf["net_realized"], market)}'
            + (f' <span class="muted">손익미상 매도 {unk}건 제외</span>' if unk else "")
            + "</p>",
            f'<p>수수료 {_esc(fmt_amount(perf["fees"], market, signed=False))}</p>',
        ]
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
    parts.append("</body></html>")
    return "".join(parts)
