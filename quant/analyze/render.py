"""Snapshot → 사람이 읽는 HTML + 엔진이 먹는 JSON.

**두 산출물은 같은 구조체에서 생성된다.** 사람이 보는 문장과 엔진이 먹는 숫자가
따로 놀면 리포트를 신뢰할 수 없다.

렌더러는 스냅샷만 읽는다 — 네트워크를 타지 않으므로 같은 스냅샷은 항상 같은
바이트를 낸다(재현성 검증의 근거). 차트도 서버사이드 SVG다: JS 차트 라이브러리는
클라이언트에서 그리므로 이 성질을 깬다.
"""
from __future__ import annotations

from functools import partial as _partial

import json
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from quant.analyze import charts, news_direction
from quant.analyze.news_cluster import dedup_with_counts
from quant.collect.contracts import Snapshot
from quant.collect.sources.dart import classify_report
from quant.collect.sources.feeds import outlet_diversity, parse_published
from quant.collect.sources.youtube_brief import channels_for as _youtube_channels_for
from quant.core.report_clock import KST
from quant.core.terms import event_full, event_term
from quant.analyze.relations import MIN_EVIDENCE
from quant.analyze.themes import cluster, summarize
from quant.analyze.units import (
    fmt_fred, fmt_krw_eok, fmt_shares, fmt_value, group_quotes,
)
from quant.control import flows as flows_ledger

_TEMPLATES = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------- 엔진 산출물

# 후보로 올릴 최소 근거. 뉴스에 딱 한 번 스친 종목은 신호가 아니라 잡음이다 —
# 실측(2026-08-12 KR)에서 삼성전자·SK하이닉스가 18건씩인데 대신증권·신세계·
# 에이스침대는 각 1건이었고, 그런 종목이 단타 후보에 오르면 리포트의 의미가 없다.
MIN_ARTICLES = 2
MIN_STREAK = 2


def bearish_markers(c: dict) -> list[str]:
    """오늘 이 종목 기사에서 발견된 명백한 악재 표지.

    `mentions.continuity` 가 채운 `titles`(오늘치만)를 본다. 제목만 읽고 본문은
    읽지 않는다 — 좁고 정밀한 거부권이지 감성 분석이 아니다.
    """
    titles = [t.get("title", "") for t in (c.get("titles") or []) if isinstance(t, dict)]
    return news_direction.scan(titles)


def is_candidate(c: dict) -> bool:
    """후보 자격. 셋 중 하나면 근거가 있다고 본다.

    ① 오늘 여러 건 언급 ② 며칠 연속 등장 ③ 거래 랭킹 상위(돈이 실제로 몰림).
    ③ 이 있는 이유는 뉴스 언급과 거래 관심이 다른 것이기 때문이다 — 미국은
    특히 헤드라인이 개별 종목을 잘 안 부르고, 한국도 잡음이 섞인다.

    ③ 은 `in_ranking` 이 아니라 `ranking_bullish` 를 본다 (2026-08-13). 하락률
    보드 편입은 "돈이 몰린다"가 아니라 "팔리고 있다"이고, 우리 전략은 전부 롱
    온리다. 뉴스도 연속성도 없이 하락률 보드 하나로 후보가 되면 안 된다.
    """
    # 뉴스 근거(①②)는 악재 표지가 있으면 인정하지 않는다. ③ 거래 랭킹은 그대로
    # 둔다 — 매수세가 실제로 몰리는 것은 별개 사실이고, 그쪽은 Phase 2 의
    # ranking_bullish 가 이미 방향을 본다.
    news_ok = not bearish_markers(c)
    return (
        (news_ok and (c.get("today_articles") or 0) >= MIN_ARTICLES)
        or (news_ok and (c.get("streak_days") or 0) >= MIN_STREAK)
        or bool(c.get("ranking_bullish", c.get("in_ranking")))
    )


def candidates_line(
    cont: dict[str, dict], anchors: dict[str, dict],
    volume_watch: list[str] | None = None,
) -> str:
    """기존 파이프라인 호환 한 줄: `AUTO_WATCH: SYMBOL[:TAG[+TAG]] ...`

    태그가 근거를 담는다 — NEWS(오늘 언급), STREAK(2일 이상 연속), NEW(원장 최초),
    RANK(거래 랭킹 상위), ANCHOR(고정 편입 반도체).
    watch-score 확신도 엔진이 이 형식을 그대로 먹는다.

    **근거가 얕은 종목은 넣지 않는다** — 확신도 엔진이 어차피 걸러내겠지만,
    잡음을 넘기면 그쪽 임계도 흐려지고 리포트를 읽는 사람도 헷갈린다.

    volume_watch(`quant.analyze.volume_watch.recurring_volume_symbols`의 결과,
    2026-08-25)는 오늘 뉴스·랭킹 근거는 없지만 최근 며칠 거래대금 상위에 반복
    등장한 종목이다 — 새 태그를 만들지 않고 기존 RANK 를 재사용한다(RANK→TREND
    번역이 이미 있다). cont/anchors 에 이미 토큰이 있는 심볼은 건너뛴다(중복 방지).
    기본 None 이면 동작이 완전히 그대로다.
    """
    tokens: list[str] = []
    for symbol, c in sorted(
        cont.items(), key=lambda kv: (-kv[1]["today_articles"], -kv[1]["streak_days"], kv[0])
    ):
        if not is_candidate(c):
            continue
        tags = []
        # NEWS 는 엔진 어휘의 EVENT 로 번역되고, news_momentum 은 EVENT 종목을
        # **개장 직후 매수**한다. 그러니 "기사가 있다"만으로는 부족하다 — 오늘
        # 명백한 악재 표지(목표가 하향·어닝쇼크·유상증자…)가 있으면 태그를 주지
        # 않는다. 호재 여섯 건이 있어도 목표가 하향 한 건이면 "방향이 분명한
        # 촉매"가 아니다 (news_direction 모듈 docstring, 대신증권 실측 사례).
        if (c.get("today_articles") or 0) > 0 and not bearish_markers(c):
            tags.append("NEWS")
        # in_ranking 이 아니라 ranking_bullish 를 본다 — 하락률 보드 편입은 RANK
        # 근거가 아니다. 이 태그가 엔진 어휘의 TREND 로 번역돼 롱 온리 전략의
        # 진입 우선순위를 정하기 때문이다 (mentions.mark_origin docstring).
        if c.get("ranking_bullish", c.get("in_ranking")):
            tags.append("RANK")
        if (c.get("streak_days") or 0) >= MIN_STREAK:
            tags.append("STREAK")
        if c.get("is_new"):
            tags.append("NEW")
        tokens.append(f"{symbol}:{'+'.join(tags or ['NEWS'])}")
    for a in anchors.values():
        code = a.get("symbol")
        if code and not any(t.startswith(f"{code}:") for t in tokens):
            tokens.append(f"{code}:ANCHOR")
    for symbol in volume_watch or []:
        if not any(t.startswith(f"{symbol}:") for t in tokens):
            tokens.append(f"{symbol}:RANK")
    return "AUTO_WATCH: " + (" ".join(tokens) if tokens else "없음")


def machine_payload(
    snap: Snapshot,
    cont: dict[str, dict],
    delta: dict,
    brief: list[dict],
    sym_quotes: dict[str, dict] | None = None,
    details: dict[str, dict] | None = None,
    view: dict | None = None,
    scores: dict[str, dict] | None = None,
    trending: dict[str, dict] | None = None,
    relations: dict[str, list[dict]] | None = None,
    sectors: dict[str, str] | None = None,
    baselines: dict[str, int] | None = None,
    volume_watch: list[str] | None = None,
) -> dict:
    """엔진이 파싱할 정규화 피처. 산문 없음 — 숫자와 열거값만.

    sym_quotes 는 뉴스로 발굴된 종목의 시세(6자리 종목코드 기준)다. 없으면
    기존과 동일하게 뉴스 종목엔 close/change_pct 가 안 붙는다(호출부 하위호환).

    baselines 는 호출부(`report_cli`)가 sym_quotes 의 `ohlcv` 로
    `quant.analyze.baseline.baseline_score` 를 미리 돌려 낸 {심볼: 점수} 다 —
    이 함수는 계산하지 않고 값만 받는다(scores/trending 과 같은 관례, 순수 유지).
    없으면(호출부 하위호환) symbols 항목에 baseline_score100 이 안 붙는다.
    있어도 채점 불가한 심볼(키 자체 부재)은 그대로 빠진다 — 0 으로 위장하지
    않는다(baseline.py 계약). 0 은 유효한 점수이므로 `is not None` 으로 판정한다.

    trending 은 `quant.analyze.trending_score.score_all()` 결과 — 종목별 랭킹 보드
    순위 + 상대 거래량으로 낸 결정론적 트렌딩 점수다. 없으면(호출부 하위호환)
    symbols 항목에 trending_* 필드가 안 붙는다.

    relations/sectors 는 뉴스 심화 파이프라인 Task 5 산출물(`data/ledger/
    relations.json`, `sector_map.json`)이다. 없으면(호출부 하위호환) symbols
    항목에 sector/relations 필드가 안 붙는다. relations 는 `evidence_score >=
    MIN_EVIDENCE` 만, 점수 내림차순 상위 5개만 남긴다 — 근거 약한 관계로
    B(UI)·C(자동편입)를 오염시키지 않기 위해서다.

    HTML 은 naver_theme(네이버 테마 편입사유, 팩트)를 임계 우회로 노출하지만
    engine.json 은 임계를 유지한다 — 자동편입에 저점수 관계를 흘리지 않기
    위해서다. 소비자는 각 관계의 `source` 키로 스스로 판단한다.

    volume_watch 는 `candidates_line` 에 그대로 전달된다(§candidates_line 참고).
    없으면(호출부 하위호환) auto_watch 에 최근 거래대금 반복 종목이 안 붙는다 —
    이 함수는 snap_root(스냅샷 디렉터리)에 접근하지 못하므로 계산은 호출부 몫이다.
    """
    market = _ok(snap, "market") or {}
    quotes = market.get("quotes", {})
    anchors = market.get("anchors", {})
    flow = _ok(snap, "kospi_flow") or {}
    cal = _ok(snap, "calendar") or {}
    macro = _ok(snap, "macro") or {}
    news_data = _ok(snap, "news")

    row = (flow.get("rows") or [{}])[0]
    features = {
        "vix": _close(quotes, "^VIX"),
        "usdkrw": _close(quotes, "KRW=X"),
        "kospi_change_pct": _pct(quotes, "^KS11"),
        "kosdaq_change_pct": _pct(quotes, "^KQ11"),
        "sp500_change_pct": _pct(quotes, "^GSPC"),
        "foreign_net_100m_krw": row.get("외국인"),
        "institution_net_100m_krw": row.get("기관계"),
        "individual_net_100m_krw": row.get("개인"),
        "foreign_flipped": (delta.get("flow", {}).get("외국인", {}) or {}).get("flipped"),
        "high_impact_events_2d": sum(
            1 for e in cal.get("events", [])
            if e.get("high_impact") and e.get("days_ahead", 99) <= 2
        ),
        "net_liquidity_musd": (macro.get("net_liquidity") or {}).get("value"),
        "risk_flags": sum(1 for b in brief if b["level"] == "risk"),
        "missing_sources": len(snap.missing()),
    }

    sym_quotes = sym_quotes or {}
    symbols = []
    for symbol, c in sorted(
        cont.items(), key=lambda kv: (-kv[1]["today_articles"], -kv[1]["streak_days"], kv[0])
    ):
        entry = {
            "symbol": symbol,
            "name": c["name"],
            "origin": c.get("origin", "news"),
            "news_articles_today": c["today_articles"],
            "news_streak_days": c["streak_days"],
            "news_days_in_window": c["days"],
            "is_new": c["is_new"],
            "in_ranking": c.get("in_ranking", False),
            "ranking_bullish": c.get("ranking_bullish", c.get("in_ranking", False)),
            # 왜 뉴스 근거가 인정되지 않았는지 — 빈 목록이 "악재 없음"이다.
            # "점수가 낮아서"는 사람이 검증할 수 없고 규칙이 틀렸을 때 고칠 수도 없다.
            "bearish_markers": bearish_markers(c),
        }
        q = sym_quotes.get(symbol)
        if q:
            entry["close"] = q.get("close")
            entry["change_pct"] = q.get("change_pct")
            # date(D2, 2026-09-03): close 가 실제로 어느 거래일 종가인지 —
            # `fetch_symbol_quotes` 가 준 "date"(quant/collect/sources/market.py)
            # 를 그대로 싣는다. 선정 원장 writer(`quant/report/collect/ledger.py`
            # 의 `_record_intraday_selections` 등)가 `base.get("date")` 로 이미
            # 읽고 있었지만 이 필드가 없어 늘 None 이었다 — outcomes.py 가
            # 전방수익률 지평을 셀 기준 세션(`base_session_date`)을 정하는 값이다.
            if q.get("date") is not None:
                entry["date"] = q["date"]
        sc = (scores or {}).get(symbol)
        if sc:
            entry["ai_score"] = sc["score"]
            entry["ai_score100"] = sc["score100"]
            entry["ai_label"] = sc["label"]
            entry["ai_factors"] = [f["key"] for f in sc["factors"]]
        tr = (trending or {}).get(symbol)
        if tr:
            entry["trending_score"] = tr["score"]
            entry["trending_score100"] = tr["score100"]
            entry["trending_label"] = tr["label"]
            entry["trending_factors"] = [f["key"] for f in tr["factors"]]
            entry["trending_boards"] = tr["boards"]
            entry["relative_volume"] = tr["relative_volume"]
            entry["trending_baseline_days"] = tr["baseline_days"]
        if sectors and symbol in sectors:
            entry["sector"] = sectors[symbol]
        bl = (baselines or {}).get(symbol)
        if bl is not None:
            entry["baseline_score100"] = bl
        rels = sorted(
            (r for r in (relations or {}).get(symbol, [])
             if r.get("evidence_score", 0) >= MIN_EVIDENCE),
            key=lambda r: -r["evidence_score"],
        )[:5]
        if rels:
            entry["relations"] = [
                {"symbol": r["dst"], "kind": r["kind"], "reason": r["reason"],
                 "score": r["evidence_score"], "last_verified": r.get("last_verified"),
                 "source": r.get("source"),
                 **({"via_theme": r["via_theme"]} if r.get("via_theme") else {})}
                for r in rels
            ]
        det = (details or {}).get(symbol)
        if det:
            cs = det.get("consensus") or {}
            entry.update({
                "analyst_opinion_score": cs.get("opinion_score"),
                "analyst_opinion_label": cs.get("opinion_label"),
                "analyst_target_price": cs.get("target_price"),
                "analyst_count": cs.get("analyst_count"),
                "upside_pct": det.get("upside_pct"),
                "foreign_net_5d_shares": (det.get("flow_summary") or {}).get("foreign_net_5d"),
                "inst_net_5d_shares": (det.get("flow_summary") or {}).get("inst_net_5d"),
                "foreign_buy_streak": (det.get("flow_summary") or {}).get("foreign_buy_streak"),
                "inst_buy_streak": (det.get("flow_summary") or {}).get("inst_buy_streak"),
            })
        symbols.append(entry)
    for a in anchors.values():
        code = a.get("symbol")
        if not code:
            continue
        existing = next((s for s in symbols if s["symbol"] == code), None)
        target = existing or {
            "symbol": code, "name": a.get("label", code), "origin": "anchor",
            "news_articles_today": 0, "news_streak_days": 0,
            "news_days_in_window": 0, "is_new": False,
        }
        target["close"] = a.get("close")
        target["change_pct"] = a.get("change_pct")
        if existing is None:
            symbols.append(target)
        else:
            existing["origin"] = "news+anchor"

    payload = {
        "schema": 1,
        "market": snap.market,
        "session_date": snap.session_date.isoformat(),
        "generated_at": snap.generated_at.isoformat(),
        "missing": snap.missing(),
        "features": features,
        "symbols": symbols,
        "auto_watch": candidates_line(cont, anchors, volume_watch),
        "stance": view or {},
        "news_diversity": outlet_diversity(news_data) if news_data else None,
    }
    # relations 스냅샷 전체에서 가장 최근 재검증 시각 — deepdive(LLM) 가 며칠째
    # 불통이어도 리포트 소비자가 "관계 데이터가 얼마나 낡았나"를 알 수 있게 한다
    # (I1b). 없으면(호출부 하위호환 또는 relations 미제공) 키 자체를 생략한다.
    if relations:
        verified = [r.get("last_verified") for rs in relations.values()
                   for r in rs if r.get("last_verified")]
        if verified:
            payload["relations_as_of"] = max(verified)
    return payload


def _ok(snap: Snapshot, key: str) -> dict | None:
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def _close(quotes: dict, sym: str):
    return (quotes.get(sym) or {}).get("close")


def _pct(quotes: dict, sym: str):
    return (quotes.get(sym) or {}).get("change_pct")


# ---------------------------------------------------------------- 수혜주 트리

_KIND_KO = {"beneficiary": "수혜주", "supplier": "공급사", "competitor": "경쟁사"}


def relation_items(cont: dict[str, dict], relations: dict[str, list[dict]] | None,
                    symbol: str, name_map: dict[str, str] | None = None) -> list[dict]:
    """종목 카드에 실을 수혜주/공급사/경쟁사 트리 항목(점수 내림차순).

    노출 임계는 `evidence_score >= MIN_EVIDENCE` — 단 `source == "naver_theme"`
    (네이버 편입사유, 팩트) 는 임계와 무관하게 노출한다. LLM 추론(source 가 없거나
    "llm")만 근거 점수로 거른다 — naver_theme 은 추측이 아니라 회사가 공시한
    편입사유라 점수로 거를 대상이 아니다(F Task 3 이 채점만 하고 걸러내지 않은
    이유와 같다).

    종목명 해석 우선순위: 관계 행 자체의 `dst_name`(테마 경로가 실어 보내는 값,
    오늘 뉴스에 안 뜬 수혜주도 이름을 안다) → `cont`(이 리포트가 이미 쓰는 이름
    사전, LLM 경로 하위호환) → `name_map`(KR 상장사 전체 이름표) → 코드 그대로
    (지어내지 않는다). `cont` 만 보면 naver_theme 항목(리뷰 결함 c의 주 대상)이
    오늘 뉴스에 없어 폴백에 실패해 6자리 코드가 그대로 노출됐다.

    **2026-09-02**: 위 두 단계로도 부족했다 — 실제 리포트에 "153890 153890
    공급사", "011070 011070 경쟁사"처럼 코드만 노출됐는데, DART 캐시(3,985행)는
    두 종목의 이름을 **알고 있었다**. 이름표가 사슬에 없었을 뿐이다. report_cli
    가 이미 로드해 쓰는 `research_code_to_name` 을 그대로 넘겨 메운다(네트워크
    추가 없음). 여전히 모르는 심볼은 코드 그대로 — 지어내지 않는 원칙은 유지.
    """
    rows = (relations or {}).get(symbol) or []
    out = []
    for r in rows:
        if r.get("evidence_score", 0) < MIN_EVIDENCE and r.get("source") != "naver_theme":
            continue
        dst = r["dst"]
        out.append({
            "symbol": dst,
            "name": (r.get("dst_name")
                     or (cont.get(dst) or {}).get("name")
                     or (name_map or {}).get(dst)
                     or dst),
            "kind": _KIND_KO.get(r.get("kind"), r.get("kind")),
            "reason": r.get("reason", ""),
            "score": r.get("evidence_score", 0),
            "via_theme": r.get("via_theme"),
            "source": r.get("source"),
        })
    return sorted(out, key=lambda it: -it["score"])


# ---------------------------------------------------------------- 수급 기간 뷰

# 표시할 기간 후보 — 1일/5일/10일. 이보다 더 쌓였으면 flow_periods 가 전체
# 누적 칸을 하나 더 얹는다(장기 구간).
FLOW_WINDOWS: tuple[int, ...] = (1, 5, 10)


def flow_periods(rows: list[dict], symbol: str, today: str) -> dict | None:
    """종목 카드에 실을 수급 기간 뷰 — 1일/5일/10일 + 축적된 장기 구간.

    Task 2 의 `window_sums`/`coverage` 를 그대로 쓴다. 원장은 그날 랭킹 상위
    6종목만 축적되므로, 이 종목에 데이터가 하루도 없으면 `None`(호출부가 기존
    5일 표시로 폴백하거나 "축적 전"을 보여줘야 한다는 신호) — 0 으로 위장하지
    않는다.

    같은 값을 반복 표기하지 않는다: 요청 기간(예: 10일)이 실제로 더 채워주는
    게 없으면(예: 5일 칸과 실제 합산 일수가 같으면) 그 칸은 만들지 않는다 —
    "5일 중 3일치"와 "10일 중 3일치"를 나란히 보여줘 봤자 같은 숫자의 반복일
    뿐이다. 장기 구간도 같은 규칙으로, 10일 칸이 이미 전체 누적을 다 담았으면
    따로 만들지 않는다.
    """
    sym_rows = [r for r in rows if r.get("symbol") == symbol]
    cov = flows_ledger.coverage(sym_rows)
    if cov["n_days"] == 0:
        return None

    windows: list[dict] = []
    last_n = None
    for days in FLOW_WINDOWS:
        w = flows_ledger.window_sums(rows, symbol, days, today)
        if w is None or w["n_days"] == last_n:
            continue
        windows.append({"days": days, **w})
        last_n = w["n_days"]

    if cov["n_days"] != last_n:
        try:
            span = (date.fromisoformat(today) - date.fromisoformat(cov["first_date"])).days + 1
        except (TypeError, ValueError):
            span = None
        if span:
            w = flows_ledger.window_sums(rows, symbol, span, today)
            if w is not None and w["n_days"] != last_n:
                windows.append({"days": None, **w})

    return {"windows": windows, "coverage": cov}


# ---------------------------------------------------------------- HTML

def missing_hint(error: str | None) -> str | None:
    """결측 원문 에러에 사람용 원인 힌트 한 줄 — 원문을 대체하지 않는다(결측을
    숨기지 않는 원칙 그대로).

    왜(2026-08-18): 사용자가 로컬 비교 빌드의 Toss 403 결측을 운영 고장으로
    읽었다. 에러 문구는 정확했지만 **원인**(IP 화이트리스트)을 모르면 고장처럼
    보인다 — 알림은 있었는데 독해가 없던 own_brief 나흘 사고와 같은 부류라,
    독해를 돕는 한 줄을 결정론으로 붙인다. 패턴에 없는 에러는 힌트 없이 원문만.
    """
    if not error:
        return None
    lowered = error.lower()
    if "403" in lowered and "tossinvest" in lowered:
        return ("Toss API는 등록된 IP만 허용한다 — EC2 운영 빌드에서는 정상 수집되는 "
                "소스다(미등록 IP·로컬 빌드의 구조적 결측).")
    if "시간외단일가" in error:
        return ("키움 시간외 순위는 세션(16:00~18:00 KST) 밖이거나 미등록 키 환경에서 "
                "빈다 — 운영 빌드에서 반복되면 키움 키 상태를 확인.")
    if "429" in lowered:
        return "레이트 리밋 — 다음 빌드에서 대개 자가 회복된다."
    if "401" in lowered or "unauthorized" in lowered:
        return "인증 실패 — .env.local 의 해당 API 키를 확인."
    return None


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    # 차트는 우리가 만든 SVG 문자열이므로 Markup 으로 통과시킨다.
    # 사용자 입력(뉴스 제목 등)은 절대 여기로 오지 않는다 — 숫자와 라벨뿐.
    env.globals.update(
        missing_hint=missing_hint,
        sparkline=lambda v, **k: Markup(charts.sparkline(v, **k)),
        diverging_bars=lambda r, **k: Markup(charts.diverging_bars(r, **k)),
        event_axis=lambda e, **k: Markup(charts.event_axis(e, **k)),
        gauge=lambda v, lo, hi, **k: Markup(charts.gauge(v, lo, hi, **k)),
        daily_bars=lambda v, **k: Markup(charts.daily_bars(v, **k)),
        term=event_term,
        term_full=event_full,
        unit=fmt_value,
        eok=fmt_krw_eok,
        shares=fmt_shares,
        fred=fmt_fred,
        relation_items=relation_items,
        flow_periods=flow_periods,
        classify_report=classify_report,
    )
    return env


def link_to_symbols(cont: dict[str, dict]) -> dict[str, list[dict]]:
    """기사 링크 → 그 기사를 언급한 종목 목록(이름 오름차순) 역인덱스.

    오늘의 시황/오늘의 뉴스 흐름 불릿에 "관련 종목" 칩을 달기 위한 순수 조회
    헬퍼(2026-08-29 소유자 피드백 — 기사 나열을 종목과 이어 보이게). `cont`
    (뉴스 연속성 사전)가 이미 갖고 있는 `c["titles"]`([{title, link}, ...])
    만 뒤집는다 — 새 수집도 LLM 호출도 없다. 링크가 없는 기사(기존에도
    존재하는 케이스)는 건너뛴다.
    """
    out: dict[str, list[dict]] = {}
    for symbol, c in cont.items():
        for t in c.get("titles") or []:
            link = t.get("link")
            if not link:
                continue
            entries = out.setdefault(link, [])
            if not any(e["symbol"] == symbol for e in entries):
                entries.append({"symbol": symbol, "name": c.get("name", symbol)})
    for entries in out.values():
        entries.sort(key=lambda e: e["name"])
    return out


def rank(cont: dict[str, dict], limit: int = 10) -> list[tuple[str, dict]]:
    """노출이 많고 연속된 종목이 위로.

    Jinja의 dictsort로는 못 한다 — 값이 dict라 비교 불가고, 무엇보다 '무엇을
    위로 올릴지'는 표현이 아니라 판단이라 파이썬 쪽에 있어야 한다.
    """
    return sorted(
        cont.items(),
        key=lambda kv: (-kv[1]["today_articles"], -kv[1]["streak_days"], kv[0]),
    )[:limit]


# 시간 척추 좌측(과거 흐름)에 세울 심볼과 순서. 시장마다 '먼저 봐야 할 것'이
# 다르다 — 한국장 리포트에서 KOSPI가 빠지고 비트코인이 올라오면 리포트가 아니다.
# dict 삽입 순서에 맡겼다가 실제로 그렇게 나왔다(2026-08-12).
SPINE_ORDER = {
    "KR": ("^KS11", "^KQ11", "KRW=X", "^GSPC", "^VIX"),
    "US": ("^GSPC", "^IXIC", "^VIX", "^TNX", "DX-Y.NYB"),
}


NEAR_LABELS = ("오늘", "내일", "모레")


def group_events_by_day(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """(오늘/내일/모레 박스, 사흘 뒤 이후 목록).

    시간축 그래프를 쓰지 않는다 — 이벤트가 같은 날에 몰리면 점과 라벨이 겹쳐
    읽을 수 없고, 무엇보다 "오늘 뭐가 있나"는 위치가 아니라 **목록**으로 답하는
    질문이다. 근일 3개는 비어 있어도 박스를 남긴다 — '없음'도 정보다.
    """
    hi = [e for e in events if e.get("high_impact")]
    near = []
    for idx, label in enumerate(NEAR_LABELS):
        items = [e for e in hi if e.get("days_ahead") == idx]
        near.append({
            "label": label,
            "date": items[0]["date"] if items else "",
            "weekday": items[0].get("weekday", "") if items else "",
            "events": items,
        })
    later = sorted(
        (e for e in hi if e.get("days_ahead", 0) >= len(NEAR_LABELS)),
        key=lambda e: (e["days_ahead"], e["name"]),
    )
    return near, later


def spine_quotes(market: str, quotes: dict) -> list[tuple[str, dict]]:
    order = SPINE_ORDER.get(market, ())
    out = [(s, quotes[s]) for s in order if s in quotes]
    if out:
        return out
    return list(quotes.items())[:5]  # 우선순위 심볼이 전부 결측이면 있는 것이라도


def _video_age(published: str | None, now: datetime) -> str:
    """영상 발행시각 → 초보가 읽기 쉬운 문자열. 24시간 이내는 'N시간 전',
    그 밖은 KST 'MM-DD HH:MM'.

    `now`를 인자로 받는다 — `datetime.now()`를 여기서 직접 부르면 이 함수를
    부르는 `render()`가 "같은 입력이면 같은 바이트를 낸다"는 모듈 계약(상단
    docstring)을 깬다. 호출부(`youtube_view`)가 `snap.generated_at`을 넘겨
    스냅샷 기준으로 고정한다.
    """
    dt = parse_published(published)
    if dt is None:
        return "-"
    hours = (now - dt).total_seconds() / 3600
    if 0 <= hours < 24:
        return f"{max(1, round(hours))}시간 전"
    return dt.astimezone(KST).strftime("%m-%d %H:%M")


def youtube_view(briefs: dict[str, list[dict]], now: datetime) -> list[dict]:
    """채널별 최신 영상(§Task 7)을 템플릿이 그대로 뿌릴 수 있게 정리한다.

    영상이 없는 채널은 빼고 돌려준다 — `fetch_briefs`가 이미 실패 채널을
    빼지만, 호출부가 무엇을 넘기든 방어적으로 한 번 더 거른다.
    """
    out: list[dict] = []
    for channel, videos in briefs.items():
        rows = [
            {"title": v["title"], "link": v["link"], "when": _video_age(v.get("published"), now)}
            for v in videos
        ]
        if rows:
            out.append({"channel": channel, "videos": rows})
    return out


def youtube_view_for_market(briefs: dict[str, list[dict]], market: str, now: datetime) -> list[dict]:
    """유튜브 브리핑 KR/US 고정 탭(2026-08-18, 텔레그램 인사이트와 같은 요구) —
    `youtube_brief.channels_for`로 `market`에 해당하는 채널명만 걸러
    `youtube_view`에 넘긴다. `briefs`는 `_fetch_youtube_briefs`가 이미
    한 번에 가져온 전 채널분(시장 무관)이다 — `telegram_view.build_telegram_view`가
    `channels_for(market)`으로 채널을 거르는 것과 같은 패턴, 다만 유튜브는
    이미 render() 내부가 뷰 조립을 전담하고 있어(호출부는 원본 dict만 넘김)
    그 자리에서 시장별로 한 번 더 나눈다.

    해당 시장에 등록된 채널이 없거나(현재 미국 채널 0건일 수 있다) 전부 영상이
    없으면 빈 리스트 — 템플릿이 "등록된 미국 채널 없음"을 정직하게 그린다."""
    names = set(_youtube_channels_for(market))
    filtered = {name: videos for name, videos in briefs.items() if name in names}
    return youtube_view(filtered, now)


def blog_view(briefs: dict[str, list[dict]], now: datetime) -> list[dict]:
    """블로그별 최신 글(고정 브리핑 소스, 2026-08-17)을 템플릿이 그대로
    뿌릴 수 있게 정리한다. `youtube_view`와 같은 계약 — 나이 표기(`_video_age`)는
    영상이든 글이든 발행시각 하나로 정하는 순수 포맷 함수라 그대로 재사용한다.

    글이 없는 블로그는 빼고 돌려준다 — `fetch_briefs`가 이미 실패 블로그를
    빼지만, 호출부가 무엇을 넘기든 방어적으로 한 번 더 거른다.
    """
    out: list[dict] = []
    for blog, posts in briefs.items():
        rows = [
            {"title": p["title"], "link": p["link"], "when": _video_age(p.get("published"), now)}
            for p in posts
        ]
        if rows:
            out.append({"blog": blog, "posts": rows})
    return out


def group_carried_by_sector(carried: list[dict]) -> list[dict]:
    """휴장 이월 후보(리포트 UX 2차 Task 3)를 업종별로 묶는다.

    `sector` 키가 없거나 None 인 항목은 '기타'로 묶는다 — 섹터 미상이라고
    타일이 통째로 빠지면 정보 소실이다. `sector` 는 이월 항목이 이미
    `machine_payload` 에서 얻은 값을 `carryover.merge_carryover` 가 그대로
    복사해 온 것이다(호출부가 따로 채우지 않아도 대부분 채워져 있다).
    업종명은 오름차순, '기타'는 항상 맨 뒤. 각 그룹 안은 등락률 내림차순
    (None 은 뒤로) — 히트맵에서 강한 색이 먼저 보이게.
    """
    groups: dict[str, list[dict]] = {}
    for s in carried:
        # 사본 + change_pct 키 보정 — 템플릿이 점(.) 표기로 읽는데(`s.change_pct`),
        # 키 자체가 없으면 Jinja 가 Undefined 를 돌려주고 비교 연산(hm_class 의
        # `pct > 0`)에서 그대로 예외가 난다(실측). None 이면 dict 접근이 정상값
        # None 을 돌려주므로 hm_class/pct() 의 기존 None 분기가 그대로 작동한다.
        entry = dict(s)
        entry.setdefault("change_pct", None)
        name = entry.get("sector") or "기타"
        groups.setdefault(name, []).append(entry)
    for items in groups.values():
        items.sort(key=lambda s: (s.get("change_pct") is None, -(s.get("change_pct") or 0)))
    order = sorted(k for k in groups if k != "기타") + (["기타"] if "기타" in groups else [])
    # 키는 "tiles" — "items"는 dict 내장 메서드와 이름이 겹쳐 템플릿의
    # `grp.items` 점(.) 접근이 (딕셔너리 값이 아니라) 메서드 객체를 돌려준다
    # (Jinja 속성 우선 규칙, 실측: TypeError 'builtin_function_or_method' object
    # is not iterable).
    return [{"name": name, "tiles": groups[name]} for name in order]


# ---------------------------------------------------------------- 섹션 요약줄 (접힘 details 의 <summary>)
#
# 소유자 지시(2026-09-02): 섹션이 많아지면서 리포트가 길어졌다 — 각 섹션을
# details/summary 로 접되, 접힌 상태에서도 "그 섹션의 결론"이 한 줄로 읽혀야
# 한다(라벨 자기설명 원칙, 2026-08-29 개편과 동일). 조립 로직은 여기(파이썬)에
# 둔다 — 템플릿에 판단 로직을 우겨넣지 않는다. 전부 **이미 model 에 있는
# 필드를 재사용**할 뿐 새 수집·새 계산은 하지 않는다. 각 함수는 데이터가
# 없으면 None — 템플릿의 `{% if tldr.xxx %}` 가 조용히 생략한다.

def _pct_fmt(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else "―"


def summarize_index_outlook(index_outlook: dict[str, dict] | None) -> str | None:
    """지수별 전망 접힘 요약 — 지수마다 상승 확률(또는 점수/라벨)을 한 줄로.

    확률 엔진 v2(up_prob)가 있으면 그 값을, 없으면 기존 probability.prob 를,
    그마저 없으면 score100/label 을 쓴다 — 화면 표시(§2 필드 계약)와 같은
    폴백 순서.
    """
    if not index_outlook:
        return None
    names = {"kospi": "코스피", "kosdaq": "코스닥", "sp500": "S&P500", "nasdaq": "나스닥"}
    parts = []
    for key, idx in index_outlook.items():
        label = names.get(key, key)
        up = idx.get("up_prob")
        prob = (idx.get("probability") or {}).get("prob")
        if up is not None:
            parts.append(f"{label} 상승 {up:.0%}")
        elif prob is not None:
            parts.append(f"{label} 상승 {prob:.0%}")
        elif idx.get("score100") is not None:
            parts.append(f"{label} {idx.get('label')}")
        else:
            parts.append(f"{label} 판단 근거 부족")
    return " · ".join(parts) if parts else None


def summarize_money_flow(money_flow: dict | None) -> str | None:
    """돈의 흐름 접힘 요약 — 자금 흐름 판정 + 현금 체력 판정 두 줄을 한 줄로."""
    if not money_flow:
        return None
    flow = (money_flow.get("flow") or {}).get("label")
    cash = (money_flow.get("cash") or {}).get("label")
    parts = [p for p in (flow, cash) if p]
    return " · ".join(parts) if parts else None


def summarize_candidates(ranked: list[tuple[str, dict]]) -> str | None:
    """뉴스 노출 상위 종목(오늘의 후보) 접힘 요약 — 종목 수 + 노출 1위."""
    if not ranked:
        return "오늘 뉴스에서 추출된 후보 없음"
    _, top = ranked[0]
    return f"{len(ranked)}종목 · 노출 1위 {top['name']}(오늘 {top['today_articles']}건)"


def summarize_holiday(holiday_synthesis: dict | None) -> str | None:
    if not holiday_synthesis:
        return None
    gap = holiday_synthesis.get("gap_days")
    themes = holiday_synthesis.get("theme_freq") or []
    if themes:
        return f"휴장 {gap}일 · 상위 테마 {themes[0]['theme']}({themes[0]['count']}건)"
    return f"휴장 {gap}일 종합"


def summarize_digest(digest: dict | None) -> str | None:
    if not digest:
        return None
    n_dom = len(digest.get("domestic") or [])
    n_us = len(digest.get("us_impact") or [])
    if not n_dom and not n_us:
        return None
    return f"국내 {n_dom}건 · 미국발 {n_us}건"


def summarize_news_flow(news_flow: list[dict] | None) -> str | None:
    if not news_flow:
        return None
    return f"전 매체 사건 단위 {len(news_flow)}건"


def summarize_us_wrap(us_wrap: dict | None) -> str | None:
    if not us_wrap:
        return None
    tone = us_wrap.get("tone")
    up, down = us_wrap.get("up_count"), us_wrap.get("down_count")
    if tone and up is not None and down is not None:
        return f"{tone} · {up}↑ {down}↓"
    return tone


def summarize_us_kr_bridge(us_kr_bridge: dict | None) -> str | None:
    if not us_kr_bridge:
        return None
    parts = []
    if us_kr_bridge.get("tone"):
        parts.append(us_kr_bridge["tone"])
    up, down = us_kr_bridge.get("up_count"), us_kr_bridge.get("down_count")
    if up is not None and down is not None:
        parts.append(f"{up}↑ {down}↓")
    n_focus = len(us_kr_bridge.get("focus") or [])
    if n_focus:
        parts.append(f"연동 업종 {n_focus}개")
    return " · ".join(parts) if parts else None


def summarize_usnews_headlines(usnews_headlines: list[dict] | None) -> str | None:
    if not usnews_headlines:
        return None
    return f"최신 {len(usnews_headlines)}건"


def summarize_channel_digest(channel_digest) -> str | None:
    """📡 채널 브리핑 종합(2026-09-05) 접힘 요약줄. `channel_digest`는
    `quant.analyze.tg_digest.Digest`(또는 `None`, 수집 실패) — 종목 후보·
    리스크 항목 건수만 뽑는다(다른 `summarize_*`와 같은 관례, 새 계산 없음)."""
    if channel_digest is None or not getattr(channel_digest, "channel_entries", None):
        return None
    n_cand = len(channel_digest.candidates)
    n_risk = len(channel_digest.risk_items)
    parts = []
    if channel_digest.stance:
        parts.append(channel_digest.stance)
    if n_cand:
        parts.append(f"후보 {n_cand}종목")
    if n_risk:
        parts.append(f"리스크 {n_risk}건")
    return " · ".join(parts) if parts else "새 메시지 있음"


def summarize_top_movers(top_movers: dict | None) -> str | None:
    if not top_movers:
        return None
    parts = []
    sectors = top_movers.get("sectors") or []
    themes = top_movers.get("themes") or []
    if sectors:
        s = sectors[0]
        parts.append(f"업종 1위 {s['name']} {_pct_fmt(s.get('change_pct'))}")
    if themes:
        t = themes[0]
        parts.append(f"테마 1위 {t['name']} {_pct_fmt(t.get('change_pct'))}")
    return " · ".join(parts) if parts else None


def summarize_sector_view(sector_view: list[dict] | None) -> str | None:
    if not sector_view:
        return None
    dated = [s for s in sector_view if s.get("change_pct") is not None]
    if not dated:
        return f"{len(sector_view)}개 업종"
    top = max(dated, key=lambda s: s["change_pct"])
    return f"{len(sector_view)}개 업종 · 최고 {top['name']} {_pct_fmt(top['change_pct'])}"


def summarize_foreign_view(foreign_view: dict | None) -> str | None:
    if not foreign_view:
        return None
    sectors = foreign_view.get("sectors") or []
    buy = sum(
        1 for s in sectors for row in (s.get("rows") or [])
        if row.get("label") == "매수 시그널(재유입)"
    )
    return f"{len(sectors)}개 업종 · 매수 시그널 {buy}건"


def summarize_intraday_view(intraday_view: list[dict] | None) -> str | None:
    if not intraday_view:
        return None
    top = intraday_view[0]
    return f"{len(intraday_view)}종목 · 1위 {top['name']} {top.get('grade', '')} {top.get('score100')}점"


def summarize_midterm_view(midterm_view: list[dict] | None) -> str | None:
    if not midterm_view:
        return None
    top = midterm_view[0]
    return f"{len(midterm_view)}종목 · 1위 {top['name']} {top.get('grade_label', '')}"


def summarize_agent_interpret_view(agent_interpret_view: list[dict] | None) -> str | None:
    if not agent_interpret_view:
        return None
    bullish = sum(1 for it in agent_interpret_view if it.get("direction") == "bullish")
    return f"{len(agent_interpret_view)}종목 심층 해석 · 상승 근거 {bullish}건"


def summarize_us_news_kr_view(us_news_kr_view: list[dict] | None) -> str | None:
    if not us_news_kr_view:
        return None
    hits = sum(s.get("hits", 0) for s in us_news_kr_view)
    return f"{len(us_news_kr_view)}개 섹터 · 헤드라인 {hits}건"


def summarize_research_badges(research_badges: list[dict] | None) -> str | None:
    if not research_badges:
        return None
    return f"{len(research_badges)}종목 리포트 발행"


def summarize_carried_candidates(
    carried_candidates: list[dict] | None, carried_by_sector: list[dict],
) -> str | None:
    if not carried_candidates:
        return None
    return f"{len(carried_candidates)}종목 · {len(carried_by_sector)}개 업종"


def render(
    snap: Snapshot,
    cont: dict | None = None,
    delta: dict | None = None,
    brief: list[dict] | None = None,
    auto_watch: str = "AUTO_WATCH: 없음",
    sym_quotes: dict[str, dict] | None = None,
    details: dict[str, dict] | None = None,
    view: dict | None = None,
    scores: dict[str, dict] | None = None,
    relations: dict[str, list[dict]] | None = None,
    # KR 상장사 전체 이름표 — relation_items 의 마지막 이름 폴백(2026-09-02,
    # 관련 종목 섹션에 6자리 코드가 그대로 노출되던 결함 수리).
    name_map: dict[str, str] | None = None,
    sector_view: list[dict] | None = None,
    flow_rows: list[dict] | None = None,
    youtube: dict[str, list[dict]] | None = None,
    blog: dict[str, list[dict]] | None = None,
    top_movers: dict | None = None,
    carried_candidates: list[dict] | None = None,
    disclosures: dict[str, list[str]] | None = None,
    research: dict[str, list[str]] | None = None,
    research_badges: list[dict] | None = None,
    foreign_view: dict | None = None,
    digest: dict | None = None,
    digest_prose: dict | None = None,
    stance_prose: str | None = None,
    news_flow: list[dict] | None = None,
    intraday_view: list[dict] | None = None,
    exec_summary: dict | None = None,
    section_advice: dict | None = None,
    telegram_view_kr: list[dict] | None = None,
    telegram_view_us: list[dict] | None = None,
    telegram_prose: dict | None = None,
    telegram_image_desc: dict | None = None,
    agent_interpret_view: list[dict] | None = None,
    midterm_view: list[dict] | None = None,
    us_news_kr_view: list[dict] | None = None,
    usnews_headlines: list[dict] | None = None,
    us_kr_bridge: dict | None = None,
    us_wrap: dict | None = None,
    index_outlook: dict | None = None,
    holiday_synthesis: dict | None = None,
    symbol_payload: dict[str, dict] | None = None,
    money_flow: dict | None = None,
    sector_daily: dict | None = None,
    channel_digest: object | None = None,
) -> str:
    from quant.analyze.indicators import describe

    cont = cont or {}
    news = _ok(snap, "news") or {}
    titles = [
        i["title"] for items in news.get("feeds", {}).values() for i in items
    ]
    theme_clusters = cluster(titles, snap.market)
    quotes = (_ok(snap, "market") or {}).get("quotes", {})
    # 종목별 오늘 기사(c["titles"])를 사건 단위로 묶어 대표만 카드 상단에
    # 노출한다(H-2 Task 3) — cont 원본은 건드리지 않고 템플릿에 넘길 사본에만
    # title_reps 를 얹는다(machine_payload 등 다른 소비자는 cont 를 그대로 본다).
    ranked = [
        (sym, {**c, "title_reps": dedup_with_counts(c.get("titles") or [])})
        for sym, c in rank(cont)
    ]
    carried_by_sector = group_carried_by_sector(carried_candidates or [])
    # 섹션 접힘 요약줄(2026-09-02) — 결정론 조립, 새 수집 없음(§섹션 요약줄
    # docstring). 데이터가 없는 섹션은 함수가 None 을 돌려주고, 템플릿의
    # `{% if tldr.xxx %}` 가 조용히 생략한다(digest/news_flow 등 기존 폴백과
    # 같은 관례).
    tldr = {
        "index_outlook": summarize_index_outlook(index_outlook),
        "money_flow": summarize_money_flow(money_flow),
        "candidates": summarize_candidates(ranked),
        "holiday": summarize_holiday(holiday_synthesis),
        "digest": summarize_digest(digest),
        "news_flow": summarize_news_flow(news_flow),
        "us_wrap": summarize_us_wrap(us_wrap),
        "us_kr_bridge": summarize_us_kr_bridge(us_kr_bridge),
        "usnews_headlines": summarize_usnews_headlines(usnews_headlines),
        "top_movers": summarize_top_movers(top_movers),
        "sector_view": summarize_sector_view(sector_view),
        "foreign_view": summarize_foreign_view(foreign_view),
        "intraday": summarize_intraday_view(intraday_view),
        "midterm": summarize_midterm_view(midterm_view),
        "agent_interpret": summarize_agent_interpret_view(agent_interpret_view),
        "us_news_kr": summarize_us_news_kr_view(us_news_kr_view),
        "research_badges": summarize_research_badges(research_badges),
        "carried": summarize_carried_candidates(carried_candidates, carried_by_sector),
        "channel_digest": summarize_channel_digest(channel_digest),
    }
    return _env().get_template("report.html.j2").render(
        snap=snap,
        cont=cont,
        ranked=ranked,
        spine=spine_quotes(snap.market, quotes),
        quote_groups=group_quotes(quotes),
        cal_near=group_events_by_day(((_ok(snap, 'calendar') or {}).get('events') or []))[0],
        themes=theme_clusters,
        theme_line=summarize(theme_clusters, len(titles), snap.market),
        theme_total=len(titles),
        cal_later=group_events_by_day(((_ok(snap, 'calendar') or {}).get('events') or []))[1],
        indi={sym: describe(q) for sym, q in quotes.items()},
        sym_quotes=sym_quotes or {},
        details=details or {},
        view=view or {},
        scores=scores or {},
        delta=delta or {"quotes": {}, "flow": {}},
        brief=brief or [],
        auto_watch=auto_watch,
        relations=relations or {},
        # 전역으로 등록된 relation_items 를 이 렌더에 한해 이름표 바인딩본으로
        # 가린다(jinja 는 render kwarg 가 global 을 shadow 한다).
        relation_items=_partial(relation_items, name_map=name_map or {}),
        sector_view=sector_view or [],
        flow_rows=flow_rows or [],
        # 시황 브리핑 유튜브 KR/US 고정 탭(2026-08-18) — `youtube`는 호출부가
        # 시장 구분 없이 한 번에 가져온 전 채널분(youtube_view_for_market
        # docstring). 기본 checked 탭은 템플릿이 snap.market 으로 정한다.
        youtube_kr=youtube_view_for_market(youtube or {}, "KR", snap.generated_at),
        youtube_us=youtube_view_for_market(youtube or {}, "US", snap.generated_at),
        blog=blog_view(blog or {}, snap.generated_at),
        top_movers=top_movers or {},
        # 개장일 집계(G Task 3) — carried_from 은 병합된 machine payload 의
        # symbols 에만 있고 cont(뉴스 연속성 사전)엔 없다. `ranked` 카드 확장
        # 대신 별도 목록으로 최소 노출한다(report.html.j2 참고).
        carried_candidates=carried_candidates or [],
        # 이월 후보 업종별 그룹(리포트 UX 2차 Task 3) — 히트맵을 섹터로 묶어
        # 그리기 위한 뷰. 섹터 미상은 '기타'로 묶는다(group_carried_by_sector).
        carried_by_sector=carried_by_sector,
        # 공시(H-2 Task 1) — DART 원장 교집합. 없으면 빈 dict, 칩 자체를 안 그린다.
        disclosures=disclosures or {},
        # 증권사 리서치(H-2 Task 5) — research.jsonl 원장 교집합. 없으면 빈 dict.
        research=research or {},
        # 뉴스 카드가 없는(=cont 밖) 종목의 리서치 사실 칩(P1, 2026-08-19) —
        # research 위 카드 루프가 못 보여주는 나머지를 별도 목록으로 최소
        # 노출한다. 없으면 빈 리스트, 섹션 자체를 생략한다.
        research_badges=research_badges or [],
        # 외국인 수급 추종(서브프로젝트 I, 2026-08-17 사용자 원칙). None 이면
        # 섹션 자체를 그리지 않는다(report_cli 가 US 시장·데이터 없음을 가드).
        foreign_view=foreign_view,
        # 오늘의 시황 요약(서브프로젝트 I) — 국내/미국발 사건 단위 다이제스트.
        # build_digest 는 항상 {"domestic": [...], "us_impact": [...]} 형태를
        # 돌려주므로(뉴스 없어도 빈 리스트) None 은 호출부 하위호환일 때뿐이다.
        digest=digest or {},
        # 시황 다이제스트 LLM 요약(스펙 §2) — summarize_digest 가 None 을
        # 돌려주면(narrator 없음/실패/파싱 불가) 템플릿은 결정론 목록만
        # 그린다(완전성 유지). {} 가 아니라 None 그대로 넘긴다 — 템플릿의
        # `{% if digest_prose %}` 가 빈 dict 와 None 을 동일하게 취급하므로
        # 굳이 바꿔치기할 이유가 없다.
        digest_prose=digest_prose,
        # 엔진 예측 헤드라인 근거 밀도 보강(P0, 2026-08-19) — _build_stance_prose
        # 가 None 을 돌려주면(매크로 결측/narrator 없음/실패) 템플릿은 기존
        # view.line 만으로 이미 완전하다(digest_prose 와 같은 무LLM 폴백 관례).
        stance_prose=stance_prose,
        # 오늘의 뉴스 흐름(리포트 UX 2차 요구 1) — market_digest.build_news_flow
        # 가 항상 리스트(빈 리스트 포함)를 돌려주므로 여기서도 []로 정직하게
        # 폴백한다(digest 와 같은 관례). 템플릿의 `{% if news_flow %}`가
        # 빈 리스트면 섹션 자체를 생략한다.
        news_flow=news_flow or [],
        # 당일 단타 후보(서브프로젝트 K) — intraday_score.rank_intraday(top=8)
        # 결과. 데이터가 없으면(비어있는 리스트) 템플릿의 `{% if intraday_view %}`
        # 가 섹션 자체를 생략한다(digest/news_flow 와 같은 관례).
        intraday_view=intraday_view or [],
        # Executive Summary(서브프로젝트 L, L-1) — 본문 근거 기반 AI 통합
        # 요약. exec_summary.summarize 가 None 을 돌려주면(narrator 없음/
        # 실패/파싱 불가) 템플릿은 이 섹션 자체를 그리지 않는다 — 기존
        # 스탠스+시황 다이제스트 블록이 무LLM 폴백 역할을 그대로 한다.
        exec_summary=exec_summary,
        # 섹션 AI 해석(리포트 UX 3차) — section_advice.advise 가 수급 체력/
        # 시장 심리/기술적 지표/유동성·금리 각 섹션의 숫자를 조건부 서술로
        # 바꾼 결과. 실패/결측이면 None — 템플릿은 숫자 카드만으로 완전하다.
        section_advice=section_advice,
        # 텔레그램 인사이트(서브프로젝트 S part 2) — telegram_view.
        # build_telegram_view 가 이미 만든 채널별 카드 뷰(빈 리스트 포함,
        # news_flow/intraday_view 와 같은 관례). KR/US 고정 탭(2026-08-18) —
        # 호출부(report_cli)가 market 별로 두 벌 빌드해 넘긴다. telegram_prose 는
        # 채널별 narrator 요약(선택, 리포트 시장 채널만) — 실패/결측이면 None,
        # 템플릿은 카드만으로 완전하다.
        telegram_view_kr=telegram_view_kr or [],
        telegram_view_us=telegram_view_us or [],
        telegram_prose=telegram_prose,
        # 텔레그램 사진 AI 해석(서브프로젝트 S part 3) — telegram_view.
        # describe_sector_images 결과({link: 1문장}), 최대 3장(예산 상한).
        # 실패/결측이면 빈 dict — 템플릿은 사진 칩+링크만으로 완전하다
        # (telegram_prose 와 같은 관례).
        telegram_image_desc=telegram_image_desc or {},
        # AI 심층 해석(서브프로젝트 U) — agent_interpret.interpret_candidates
        # 결과(빈 리스트 포함, intraday_view/news_flow 와 같은 관례). 없으면
        # 템플릿의 `{% if agent_interpret_view %}` 가 섹션 자체를 생략한다.
        agent_interpret_view=agent_interpret_view or [],
        # 중기 관심 종목(서브프로젝트 W part 3) — midterm_watch.build_midterm_watch
        # 결과(빈 리스트 포함, intraday_view 와 같은 관례). us_news_kr_view(KR
        # 전용 "🇺🇸→🇰🇷 미국발 섹터 수혜주")/usnews_headlines(US 전용 "🇺🇸 실시간
        # 헤드라인")는 시장별로 배타적으로 채워진다 — report_cli 가 반대 시장에는
        # 빈 리스트를 넘긴다.
        midterm_view=midterm_view or [],
        us_news_kr_view=us_news_kr_view or [],
        usnews_headlines=usnews_headlines or [],
        # 어젯밤 미국장→오늘 한국장 브리지(2026-08-21) — KR 아침판 전용.
        # None 이면 템플릿이 섹션을 생략한다(us_news_kr_view 와 같은 관례).
        us_kr_bridge=us_kr_bridge,
        # 전일 미국장 마감 종합(uswrap, 2026-08-25) — KR 아침판 전용, 전날
        # 새벽 US_wrap.json 을 그대로 읽어온 것. None 이면 카드 생략(같은 관례).
        us_wrap=us_wrap,
        # 지수별 전망(index_outlook)/휴장 기간 종합(holiday_synthesis) — 소유자
        # 요청(2026-08-29). 둘 다 report_cli._emit 이 payload 에 이미 얹어 둔
        # 값을 그대로 받는다(계산은 quant.report.collect 몫, 여기는 표시만).
        # None 이면 템플릿이 해당 섹션을 생략한다(us_kr_bridge 와 같은 관례).
        index_outlook=index_outlook,
        holiday_synthesis=holiday_synthesis,
        # 돈의 흐름(money_flow, 2026-08-31 소유자 지시) — 유가·금리·환율·VIX
        # 매크로 시계열로 자금 흐름/섹터 기울기를 판정한 결과
        # (quant.report.collect.money_flow.build_money_flow_view). None 이면
        # 템플릿이 섹션을 생략한다(us_kr_bridge와 같은 관례 — 원장이 비어
        # 있는 초기 배포 등).
        money_flow=money_flow,
        # 주도 섹터(sector_daily, 2026-09-03 소유자 철학 지시 B) — 거래대금
        # 상위 업종 + 외국인 순매수 + 5일 추이(quant.report.collect.sector.
        # _build_sector_daily_view). `{"missing": True}`면 KR인데 그날 데이터가
        # 결측(§C, "결측 — 섹터 데이터 없음" 카드). `None`이면 US 등 해당 없음
        # — 섹션 자체를 생략한다(money_flow와 같은 관례).
        sector_daily=sector_daily,
        # 뉴스 노출 상위 종목 카드의 "점수 내역" 접힘에서 트렌딩 요인을 보여주기
        # 위한 심볼별 machine_payload 조회. 없으면(호출부 하위호환) 트렌딩
        # 내역 없이 AI 점수 내역만 보인다.
        symbol_payload=symbol_payload or {},
        # 📡 채널 브리핑 종합(2026-09-05, 소유자 요구 (3)) — quant.analyze.
        # tg_digest.Digest 또는 None(수집 실패). 템플릿의 `{% if channel_digest %}`
        # 가 섹션을 생략한다(us_kr_bridge 와 같은 관례).
        channel_digest=channel_digest,
        # 오늘의 시황/오늘의 뉴스 흐름 불릿의 "관련 종목" 칩(2026-08-29 소유자
        # 피드백) — cont 에서 파생한 순수 조회(link_to_symbols), 새 데이터 아님.
        link_symbols=link_to_symbols(cont),
        # 섹션 접힘 요약줄(2026-09-02) — details/summary 로 접은 각 섹션의
        # <summary> 에 실을 결론 한 줄. §섹션 요약줄 docstring 참고.
        tldr=tldr,
        r=lambda k: snap.results.get(k),
        d=lambda k: _ok(snap, k),
    )


def _dated_dir(root: Path, snap: Snapshot) -> Path:
    s = snap.session_date
    path = root / f"{s.year:04d}" / f"{s.month:02d}" / f"{s.day:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_html(
    snap: Snapshot, root: Path, cont=None, delta=None, brief=None,
    auto_watch: str = "AUTO_WATCH: 없음", sym_quotes=None, details=None, view=None,
    scores=None, *, relations=None, sector_view=None, flow_rows=None, youtube=None,
    blog=None, top_movers=None, carried_candidates=None, disclosures=None, research=None,
    research_badges=None,
    foreign_view=None, digest=None, digest_prose=None, stance_prose=None, news_flow=None,
    intraday_view=None, exec_summary=None, section_advice=None,
    telegram_view_kr=None, telegram_view_us=None, telegram_prose=None, telegram_image_desc=None,
    agent_interpret_view=None, midterm_view=None, us_news_kr_view=None,
    usnews_headlines=None, us_kr_bridge=None, us_wrap=None,
    index_outlook=None, holiday_synthesis=None, symbol_payload=None,
    money_flow=None, name_map=None, sector_daily=None, channel_digest=None,
) -> Path:
    path = _dated_dir(root, snap) / f"{snap.market}_report.html"
    path.write_text(
        render(snap, cont, delta, brief, auto_watch, sym_quotes, details, view, scores,
               relations=relations, sector_view=sector_view, flow_rows=flow_rows,
               youtube=youtube, blog=blog, top_movers=top_movers,
               carried_candidates=carried_candidates,
               disclosures=disclosures, research=research, research_badges=research_badges,
               foreign_view=foreign_view,
               digest=digest, digest_prose=digest_prose, stance_prose=stance_prose,
               news_flow=news_flow,
               intraday_view=intraday_view, exec_summary=exec_summary,
               section_advice=section_advice, telegram_view_kr=telegram_view_kr,
               telegram_view_us=telegram_view_us,
               telegram_prose=telegram_prose, telegram_image_desc=telegram_image_desc,
               agent_interpret_view=agent_interpret_view, midterm_view=midterm_view,
               us_news_kr_view=us_news_kr_view, usnews_headlines=usnews_headlines,
               us_kr_bridge=us_kr_bridge, us_wrap=us_wrap,
               index_outlook=index_outlook, holiday_synthesis=holiday_synthesis,
               symbol_payload=symbol_payload, money_flow=money_flow,
               name_map=name_map, sector_daily=sector_daily, channel_digest=channel_digest),
        encoding="utf-8",
    )
    return path


def write_machine(payload: dict, snap: Snapshot, root: Path) -> Path:
    path = _dated_dir(root, snap) / f"{snap.market}_engine.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path


ROBOTS = """User-agent: *
Disallow: /
"""


def write_robots(root: Path) -> Path:
    """검색엔진 색인 차단. 공개 호스팅이라도 검색으로 퍼지는 건 다른 문제다."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "robots.txt"
    path.write_text(ROBOTS, encoding="utf-8")
    return path


def write_candidates(line: str, snap: Snapshot, root: Path) -> Path:
    path = _dated_dir(root, snap) / f"{snap.market}_candidates.txt"
    path.write_text(line + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- 마감 포지션 리포트 (서브프로젝트 R)
# 13:40 오후판 — 아침 리포트의 축약판. 별도 템플릿(`close_report.html.j2`)과
# 별도 산출물 파일명(`{market}_close_report.html`/`{market}_close_engine.json`)을
# 쓴다 — 아침 산출물(`write_html`/`write_machine`)은 이 함수들과 완전히
# 무관하며 한 바이트도 건드리지 않는다(경로 헬퍼를 additive 로 확장).

def render_close(
    snap: Snapshot,
    news_view: list[dict] | None = None,
    flow_view: dict | None = None,
    ranking_view: dict[str, list[dict]] | None = None,
    intraday_view: list[dict] | None = None,
    telegram_view_kr: list[dict] | None = None,
    agent_interpret_view: list[dict] | None = None,
    midterm_view: list[dict] | None = None,
    us_news_kr_view: list[dict] | None = None,
    usnews_headlines: list[dict] | None = None,
    telegram_view_us: list[dict] | None = None,
    close_bet_view: list[dict] | None = None,
    channel_digest: object | None = None,
) -> str:
    market_name = {"KR": "한국", "US": "미국"}.get(snap.market, snap.market)
    return _env().get_template("close_report.html.j2").render(
        snap=snap,
        market_name=market_name,
        news_view=news_view or [],
        flow_view=flow_view or {},
        ranking_view=ranking_view or {},
        intraday_view=intraday_view or [],
        # 텔레그램 인사이트(서브프로젝트 S part 2) — 아침판과 달리 narrator
        # 요약(telegram_prose)은 없다. 마감판은 그 외 섹션은 완전 무LLM
        # 계약이다(report_cli._emit_close docstring). KR/US 고정 탭(2026-08-18)
        # — 호출부가 market 별로 두 벌 빌드해 넘긴다.
        telegram_view_kr=telegram_view_kr or [],
        telegram_view_us=telegram_view_us or [],
        # AI 심층 해석(서브프로젝트 U) — 사용자 결정(2026-08-17)으로 오후판의
        # 무LLM 계약을 이 섹션에서만 깬다. 없으면(빈 리스트) 템플릿이 섹션
        # 자체를 생략한다.
        agent_interpret_view=agent_interpret_view or [],
        # 중기 관심 종목(서브프로젝트 W part 3) — 아침판과 같은 예외(AI 심층
        # 해석과 동일하게 오후판에서도 이 섹션만 narrator 를 부른다).
        midterm_view=midterm_view or [],
        us_news_kr_view=us_news_kr_view or [],
        usnews_headlines=usnews_headlines or [],
        close_bet_view=close_bet_view or [],
        # 📡 채널 브리핑 종합(2026-09-05) — 마감판은 narrator=None 으로 계산된
        # 결정론 다이제스트(report_cli._emit_close 가 보장, LLM-free 계약).
        channel_digest=channel_digest,
    )


def write_close_html(
    snap: Snapshot, root: Path,
    news_view: list[dict] | None = None,
    flow_view: dict | None = None,
    ranking_view: dict[str, list[dict]] | None = None,
    intraday_view: list[dict] | None = None,
    telegram_view_kr: list[dict] | None = None,
    agent_interpret_view: list[dict] | None = None,
    midterm_view: list[dict] | None = None,
    us_news_kr_view: list[dict] | None = None,
    usnews_headlines: list[dict] | None = None,
    telegram_view_us: list[dict] | None = None,
    close_bet_view: list[dict] | None = None,
    channel_digest: object | None = None,
) -> Path:
    path = _dated_dir(root, snap) / f"{snap.market}_close_report.html"
    path.write_text(
        render_close(snap, news_view, flow_view, ranking_view, intraday_view, telegram_view_kr,
                     agent_interpret_view, midterm_view=midterm_view,
                     us_news_kr_view=us_news_kr_view, usnews_headlines=usnews_headlines,
                     telegram_view_us=telegram_view_us, close_bet_view=close_bet_view,
                     channel_digest=channel_digest),
        encoding="utf-8",
    )
    return path


def write_close_machine(payload: dict, snap: Snapshot, root: Path) -> Path:
    path = _dated_dir(root, snap) / f"{snap.market}_close_engine.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return path
