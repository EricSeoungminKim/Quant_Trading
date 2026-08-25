"""선정/수급/외국인수급/뉴스겹침 원장 기록 + 휴장일 재기록 게이트.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from quant.analyze.entities import load_market_map, load_name_map
from quant.analyze.opendays import anchor_dir_for, last_open_day
from quant.collect.sources.market import fetch_symbol_quotes
from quant.control import flows as flows_ledger
from quant.control import frgn_flow as frgn_flow_ledger
from quant.control import selections

from quant.report.collect.agent_interpret import _AGENT_INTERPRET_PRODUCER
from quant.report.collect.intraday import _INTRADAY_FACTOR_KEYS, _INTRADAY_PRODUCER, _candidate_symbols
from quant.report.collect.midterm import _MIDTERM_PRODUCER
from quant.report.collect.news import _news_z_by_symbol


def _log_overlap(root: Path, market: str, session_date, cont: dict, ranked: set) -> None:
    """뉴스 추출과 거래 랭킹이 오늘 얼마나 겹쳤는지 append-only 로 남긴다.

    실측(2026-08-12)에서 '상승률' 보드는 뉴스와 겹침 0% 였다 — 급등주는
    주요 언론이 그날 다루지 않기 때문이다. 이게 결함인지 계속되는 현상인지는
    이 로그가 몇 주 쌓여야 판단할 수 있다. 지금은 판단하지 않고 기록만 한다.
    """
    import json as _json

    both = sum(1 for c in cont.values() if c.get("in_ranking"))
    row = {
        "date": session_date.isoformat(),
        "market": market,
        "news_symbols": len(cont),
        "ranked_symbols": len(ranked),
        "both": both,
        "news_only": len(cont) - both,
        "overlap_pct": round(both / len(cont) * 100, 1) if cont else 0.0,
    }
    path = root / "data" / "ledger" / "overlap.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(_json.dumps(row, ensure_ascii=False) + "\n")


def _should_record_ledger(market: str, root: Path, today: date) -> bool:
    """휴장일 원장 재기록 차단(G Task 4).

    **왜.** 매일 실행되면 토/일/월 크롤이 금요일의 수급·시세를 새 날짜로
    재기록한다 — flows.jsonl 기간합(1일~1년 뷰)과 selections 판단 표본이
    중복 오염된다. 토요일은 예외적으로 기록한다 — 금요일 마감 데이터의
    **첫** 기록이기 때문이다(기존 월~금 스케줄은 이걸 놓쳤다). 일요일·
    휴장 월요일은 같은 금요일 데이터의 재기록이므로 건너뛴다.

    앵커(`opendays`) 데이터가 없어 개장일 판정이 안 되면 `True`(기존 동작
    유지) — 기록이 판정에 인질잡히지 않는다.
    """
    last_open = last_open_day(anchor_dir_for(market, root), today)
    if last_open is None:
        return True
    return today <= last_open + timedelta(days=1)


def _record_flows(details: dict, root: Path, today: str) -> None:
    """`stock_detail.fetch_many` 가 받아온 수급 스냅샷을 수급 원장에 쌓는다.

    1년치를 외부에서 긁는 대신 리포트가 매일 받아오는 네이버 10일치를 그날그날
    적재한다(§핵심결정 2, `docs/superpowers/specs/2026-08-15-report-ui-design.md`).
    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다(`_record_selections`
    와 같은 관례).
    """
    try:
        rows = [
            {
                "date": f.get("date"),
                "symbol": symbol,
                "foreign_net": f.get("foreign_net"),
                "inst_net": f.get("inst_net"),
            }
            for symbol, detail in details.items()
            for f in (detail.get("flow") or [])
        ]
        path = root / "data" / "ledger" / "flows.jsonl"
        added = flows_ledger.append_flows(path, rows, today=today)
        print(f"수급 원장 {added}건 갱신")
    except Exception as e:  # noqa: BLE001
        print(f"수급 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _record_frgn_flow(details: dict, root: Path) -> None:
    """`stock_detail.fetch_many` 가 받아온 `flow_daily`(최대 20일치)를 외국인
    수급 원장(서브프로젝트 I, `data/ledger/frgn_flow.jsonl`)에 쌓는다.

    `_record_flows`(5일 합계 뷰용 `flows.jsonl`)와 별개 원장이다 —
    `quant.analyze.foreign_trend.classify()` 가 날짜 오름차순 시계열을
    입력으로 쓴다. 실패해도 리포트를 막지 않는다(원장은 부가 산출물,
    `_record_flows` 와 같은 관례).
    """
    try:
        rows = [
            {
                "date": f.get("date"),
                "symbol": symbol,
                "foreign_net": f.get("foreign_net"),
                "inst_net": f.get("inst_net"),
            }
            for symbol, detail in details.items()
            for f in (detail.get("flow_daily") or [])
        ]
        path = root / "data" / "ledger" / "frgn_flow.jsonl"
        added = frgn_flow_ledger.append_daily(rows, path)
        print(f"외국인 수급 원장 {added}건 갱신")
    except Exception as e:  # noqa: BLE001
        print(f"외국인 수급 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _selection_params() -> dict:
    """그날 적용된 임계값 스냅샷.

    **상수를 하드코딩하지 않고 실제 모듈에서 읽는다** — 값이 바뀌었는데 원장에는
    옛 값이 남으면, 하네스가 서로 다른 설정의 결과를 한 표본으로 섞어 버린다.
    바로 그걸 막으려고 남기는 필드다.
    """
    from quant.analyze import render as _render
    from quant.analyze import symbol_score as _ss
    from quant.analyze import trending_score as _ts

    return {
        "min_articles": _render.MIN_ARTICLES,
        "min_streak": _render.MIN_STREAK,
        "news_hot": _ss.NEWS_HOT,
        "streak_min": _ss.STREAK_MIN,
        "rank_top": _ts.RANK_TOP,
        "vol_surge_ratio": _ts.VOL_SURGE_RATIO,
        "vol_elevated_ratio": _ts.VOL_ELEVATED_RATIO,
        "min_history_days": _ts.MIN_HISTORY_DAYS,
    }


def _record_selections(payload: dict, root: Path) -> None:
    """그날 올린 종목의 속성 벡터를 선정 원장에 남긴다.

    **속성은 오늘 안 남기면 복원할 수 없다** — 트렌딩 점수·뉴스 건수·보드 순위는
    그 시점에만 존재한다. 전방 수익률은 가격이라 나중에도 조회되므로 여기서
    채우지 않는다(selections.py 상단 참고).

    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다.
    """
    try:
        candidates = _candidate_symbols(payload)
        news_z = _news_z_by_symbol(payload, root)
        rows = selections.build_rows(payload, candidates, params=_selection_params(),
                                      news_z_by_symbol=news_z)
        path = root / "data" / "ledger" / "selections.jsonl"
        added = selections.append(rows, path)
        print(f"선정 원장 {added}건 추가 (후보 {len(candidates)}개 / 전체 {len(rows)}종목)")
    except Exception as e:  # noqa: BLE001
        print(f"선정 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _record_watch_join_selections(
    payload: dict, root: Path, cache_dir: Path, origins: dict[str, list[str]],
) -> None:
    """뉴스 언급 없이 **감시 축으로만** 유니버스에 합류한 종목을 선정 원장에 남긴다
    (2026-08-26 감사 수리).

    ## 왜 필요한가

    `payload["symbols"]` 는 `cont`(오늘 뉴스에 언급된 종목)에서만 만들어진다.
    그런데 거래량 반복 감시·전일 KR 세션 패턴·점수 연속 강세로 합류한 종목은
    `AUTO_WATCH` 줄에 실려 **확신도 엔진을 거쳐 실제 매매 유니버스에 들어간다**.
    그 종목들이 원장에 없으면 전방 수익률(outcomes)·리더보드·ai_trader 가 영원히
    보지 못한다 — "매매는 했는데 채점 표본에는 없는" 종목이 생긴다.

    본선 행과 **별도 producer**(`WATCH_JOIN_PRODUCER`)로 남긴다: 속성 벡터의
    모양이 다르기 때문이다(뉴스·트렌딩 축이 통째로 없다). `selections.append`
    의 자연키가 producer 를 포함하므로 같은 (날짜,시장,종목)의 본선 행과 섞이지
    않고, 채점 경로(`pending_outcomes`/리더보드)는 producer 구분 없이 그대로
    재사용된다 — `_record_intraday_selections` 와 같은 관례다.

    기준가(`close`)를 함께 남기는 이유: 없으면 `forward_returns_bps` 가 전 지평을
    None 으로 돌려줘 이 종목은 영영 채점 불가가 된다. 못 구하면 **키를 생략**한다
    (0 으로 위장하면 조회 실패가 "본전"으로 굳는다 — outcomes.apply_outcome 과
    같은 원칙).

    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다.
    """
    try:
        market = str(payload.get("market") or "")
        session_date = payload.get("session_date")
        already = {s.get("symbol") for s in payload.get("symbols") or []}

        # 먼저 잡은 축이 사유가 된다(같은 종목이 여러 축에 걸려도 행은 하나).
        reason_of: dict[str, str] = {}
        for reason, symbols in origins.items():
            for sym in symbols or []:
                if sym and sym not in already and sym not in reason_of:
                    reason_of[sym] = reason
        if not reason_of:
            return

        joined = list(reason_of)
        quotes: dict[str, dict] = {}
        names: dict[str, str] = {}
        try:
            names = load_name_map(cache_dir, market) or {}
        except Exception:  # noqa: BLE001 — 이름은 장식이다
            names = {}
        try:
            if market == "US":
                quotes = {
                    sym: q for sym, q in (fetch_symbol_quotes(joined) or {}).items()
                }
            else:
                # 본선 시세와 같은 경로(quotes.py) — KIND 가 죽어도 기준가를 잃지
                # 않는다. 기준가가 없으면 이 행은 영영 채점 불가가 된다.
                from quant.report.collect.quotes import fetch_kr_quotes

                quotes, route = fetch_kr_quotes(
                    joined, cache_dir,
                    map_loader=load_market_map, quote_fetcher=fetch_symbol_quotes,
                )
                print(f"합류 종목 시세 경로: {route}")
        except Exception as e:  # noqa: BLE001 — 시세 실패가 기록을 막지 않는다
            print(f"합류 종목 시세 조회 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

        rows = []
        for sym in joined:
            row = {
                "schema": selections.SCHEMA,
                "date": session_date,
                "market": market,
                "producer": selections.WATCH_JOIN_PRODUCER,
                "symbol": sym,
                # AUTO_WATCH 줄에 실렸다 = 그날의 후보였다. 확신도 엔진이 뒤에서
                # 한 번 더 거르지만, 이 원장이 답하는 질문은 "무엇을 후보로 올렸나"다.
                "is_candidate": True,
                "join_reason": reason_of[sym],
                "outcome_filled": False,
            }
            name = names.get(sym)
            if name:
                row["name"] = name
            q = quotes.get(sym) or {}
            for key in ("close", "change_pct"):
                if q.get(key) is not None:
                    row[key] = q[key]
            rows.append(row)

        path = root / "data" / "ledger" / "selections.jsonl"
        added = selections.append(rows, path)
        priced = sum(1 for r in rows if "close" in r)
        print(f"합류 종목 원장 {added}건 추가 "
              f"(producer={selections.WATCH_JOIN_PRODUCER} · 기준가 {priced}/{len(rows)})")
    except Exception as e:  # noqa: BLE001
        print(f"합류 종목 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _load_flow_rows(root: Path) -> list[dict]:
    """수급 기간 뷰(§Task 6)가 쓸 원장 전체를 읽는다.

    읽기 실패가 리포트를 막으면 안 된다 — 실패하면 빈 리스트를 돌려주고,
    render 쪽은 빈 리스트를 "이 종목은 원장에 없다"와 동일하게 처리해
    기존 5일 표시로 폴백한다(flow_periods 계약).
    """
    try:
        return flows_ledger.load(root / "data" / "ledger" / "flows.jsonl")
    except Exception as e:  # noqa: BLE001
        print(f"수급 원장 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _record_intraday_selections(
    intraday_view: list[dict], payload: dict, root: Path, producer: str = _INTRADAY_PRODUCER,
) -> None:
    """당일 단타 스코어러(K) 픽을 선정 원장에 **별도 producer** 로 남긴다.

    `producer`(기본 `_INTRADAY_PRODUCER`, 아침 리포트) — 마감 리포트(서브
    프로젝트 R)는 `_INTRADAY_PRODUCER_CLOSE`("intraday_scorer_v3_close")를
    넘겨 같은 (날짜,시장,종목)이라도 아침/오후 선정이 원장 자연키에서
    섞이지 않게 한다(`selections._natural_key` 가 producer 를 포함하는
    이유와 동일 — 아래 원 docstring 참고). 아침/오후 각자 성적이 독립적으로
    쌓여야 검증 하네스가 어느 세션의 선정이 더 나은지 비교할 수 있다.

    `selections.build_rows`/`_attributes` 를 재사용하지 않는다 — 그건 리포트
    본선 엔진 payload 심볼 항목(ai_score100 등)의 명시적 허용목록이고, 단타
    스코어러의 속성은 score100 + 요인별 배점(factor pts)뿐이라 구조가 다르다
    (`docs/superpowers/specs/2026-08-17-foreign-flow-report-design.md` 범위 —
    이 스코어러는 진입을 결정하지 않는다, `intraday_score.py` 상단 주석).
    그래도 식별자 열(date/market/symbol/close/outcome_filled 등)은
    `selections.py` 계약을 그대로 따른다 — 그래야 전방 수익률 채움
    (`pending_outcomes`)과 리더보드가 producer 구분 없이 그대로 재사용된다.

    `producer=_INTRADAY_PRODUCER`(`"intraday_scorer_v3"`, 2026-08-17 —
    뉴스 축이 기사량→호재 탐지(v3)로 바뀌면서 `"intraday_scorer_v2"`에서
    다시 올렸다)를 준 행은 `selections.append` 의 자연키에 producer 가
    포함돼(그 함수 docstring 참고) 같은 (날짜,시장,종목)의 리포트 본선 행
    (producer 없음)이나 예전 v1/v2 행과 충돌하지 않는다 — producer 마다
    독립적으로 채점된다. AUTO_WATCH(자동 관심종목 등록)는 이 행을 전혀
    읽지 않는다 — 검증 수치가 쌓이기 전까지는 사람이 리포트에서 직접
    판단한다.

    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다(`_record_selections`
    와 같은 관례).
    """
    if not intraday_view:
        return
    try:
        sym_by_code = {s["symbol"]: s for s in payload.get("symbols") or [] if s.get("symbol")}
        session_date = payload.get("session_date")
        market = payload.get("market")
        rows = []
        for item in intraday_view:
            base = sym_by_code.get(item["symbol"]) or {}
            row = {
                "schema": selections.SCHEMA,
                "date": session_date,
                "market": market,
                "producer": producer,
                "symbol": item["symbol"],
                "name": item.get("name"),
                "score100": item["score100"],
                # top-8 랭킹 통과분만 여기 온다 — 전부 그날의 픽이다.
                "is_candidate": True,
                "close": base.get("close"),
                "change_pct": base.get("change_pct"),
                "outcome_filled": False,
            }
            for f in item["factors"]:
                key = _INTRADAY_FACTOR_KEYS.get(f["name"], f["name"])
                row[f"{key}_pts"] = f["pts"]
            rows.append(row)
        path = root / "data" / "ledger" / "selections.jsonl"
        added = selections.append(rows, path)
        print(f"단타 스코어러 원장 {added}건 추가 (producer={producer})")
    except Exception as e:  # noqa: BLE001
        print(f"단타 스코어러 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _record_agent_interpret_selections(
    view: list[dict], payload: dict, root: Path, producer: str = _AGENT_INTERPRET_PRODUCER,
) -> None:
    """AI 심층 해석(서브프로젝트 U) 판정을 선정 원장에 별도 producer 로
    남긴다 — 기존 outcomes 크론(16:00)이 전방 수익률을 자동으로 채우고,
    `get_track_record` 툴이 그 실측치를 다음 판단에 되먹인다
    (`_record_intraday_selections` 와 같은 producer 분리 관례:
    (날짜,시장,종목,producer) 자연키로 다른 producer 와 절대 섞이지 않는다).

    direction·confidence 가 `None`(JUDGMENT 파싱 실패)이어도 행은 그대로
    기록한다 — 산문 생성 자체가 성공했다는 사실도 채점 대상이다. 전부
    기록하고 방향별 분해는 나중에 채점 단계에서 한다(사용자 결정
    2026-08-17: "direction이 bullish인 것만? 아니다: 전부 기록").

    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다.
    """
    if not view:
        return
    try:
        sym_by_code = {s["symbol"]: s for s in payload.get("symbols") or [] if s.get("symbol")}
        session_date = payload.get("session_date")
        market = payload.get("market")
        rows = []
        for item in view:
            base = sym_by_code.get(item["symbol"]) or {}
            rows.append({
                "schema": selections.SCHEMA,
                "date": session_date,
                "market": market,
                "producer": producer,
                "symbol": item["symbol"],
                "name": item.get("name"),
                "is_candidate": True,
                "close": base.get("close"),
                "change_pct": base.get("change_pct"),
                "outcome_filled": False,
                "direction": item.get("direction"),
                "confidence": item.get("confidence"),
                "rounds": item.get("rounds"),
                # 폴백 채점 분해(2026-08-18) — "intraday"|"midterm". 대상이
                # 단타 후보였는지 중기 관심 종목 폴백이었는지 원장에서 구분한다.
                "source": item.get("source"),
            })
        path = root / "data" / "ledger" / "selections.jsonl"
        added = selections.append(rows, path)
        print(f"AI 심층 해석 원장 {added}건 추가 (producer={producer})")
    except Exception as e:  # noqa: BLE001
        print(f"AI 심층 해석 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)


def _record_midterm_selections(
    view: list[dict], payload: dict, root: Path, producer: str = _MIDTERM_PRODUCER,
) -> None:
    """중기 관심 종목 픽을 선정 원장에 별도 producer로 남긴다 — 기존 outcomes
    크론이 전방 수익률을 채우고, 다른 producer와 (날짜,시장,종목,producer)
    자연키로 절대 섞이지 않는다(`_record_intraday_selections`와 같은 관례).
    이 추천도 채점 루프에 자동으로 태워진다.

    실패해도 리포트를 막지 않는다 — 원장은 부가 산출물이다."""
    if not view:
        return
    try:
        sym_by_code = {s["symbol"]: s for s in payload.get("symbols") or [] if s.get("symbol")}
        session_date = payload.get("session_date")
        market = payload.get("market")
        rows = []
        for item in view:
            base = sym_by_code.get(item["symbol"]) or {}
            rows.append({
                "schema": selections.SCHEMA,
                "date": session_date,
                "market": market,
                "producer": producer,
                "symbol": item["symbol"],
                "name": item.get("name"),
                "is_candidate": True,
                "close": base.get("close"),
                "change_pct": base.get("change_pct"),
                "outcome_filled": False,
                "grade": item.get("grade"),
                "mentions": item.get("mentions"),
            })
        path = root / "data" / "ledger" / "selections.jsonl"
        added = selections.append(rows, path)
        print(f"중기 관심 종목 원장 {added}건 추가 (producer={producer})")
    except Exception as e:  # noqa: BLE001
        print(f"중기 관심 종목 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
