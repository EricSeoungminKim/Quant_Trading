"""AI 심층 해석(서브프로젝트 U) 입력 수집 + 툴콜링 에이전트 실행.

`docs/superpowers/specs/2026-08-17-tool-calling-agent-design.md`. 단타
스코어러(K)가 이미 뽑은 top-N 후보에 툴콜링 에이전트를 돌려 "왜 지금 이
종목인가" 산문 + 방향·확신 판정을 얹는다. 사용자 결정(2026-08-17): 대상
top-5, 아침(`_emit`)+오후(`_emit_close`) 양쪽 적용.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from quant.analyze.agent_interpret import AgentData, interpret_candidates
from quant.analyze.mentions import load_ledger
from quant.control import frgn_flow as frgn_flow_ledger
from quant.control import selections

AGENT_INTERPRET_TOP_N = 5
_AGENT_INTERPRET_PRODUCER = "agent_interpret_v1"
_AGENT_INTERPRET_PRODUCER_CLOSE = "agent_interpret_v1_close"


def _build_agent_foreign_flow(root: Path, symbols: set[str]) -> dict[str, list[dict]]:
    """AI 심층 해석 입력 — 후보별 외국인/기관 시계열 원본(`get_foreign_flow` 툴).

    `_build_intraday_view` 도 같은 원장을 읽지만 `foreign_score_v2` 채점용
    점수만 뽑아 쓰고 시계열 자체는 버린다 — 여기는 원시 시계열이 필요하다
    (`get_foreign_flow` 가 `foreign_trend.classify` 라벨을 직접 계산한다).
    """
    path = root / "data" / "ledger" / "frgn_flow.jsonl"
    return {symbol: frgn_flow_ledger.load_series(path, symbol, days=20) for symbol in symbols}


def _build_agent_news_items(
    root: Path, symbols: set[str], session: date, days: int = 30,
) -> dict[str, list[dict]]:
    """AI 심층 해석 입력 — 후보별 뉴스 원장(`mentions.jsonl`) 최근 `days`일치
    원본(제목+날짜+피드, `get_news_titles` 툴). `cont[symbol]["titles"]`(오늘치만)
    와 달리 여러 날짜를 담아야 `get_news_titles(symbol, days)` 가 모델이
    요청한 창을 실제로 좁힐 수 있다."""
    path = root / "data" / "ledger" / "mentions.jsonl"
    if not path.exists() or not symbols:
        return {}
    since = session - timedelta(days=days)
    out: dict[str, list[dict]] = {}
    for row in load_ledger(path):
        symbol = row.get("symbol")
        if symbol not in symbols:
            continue
        try:
            d = date.fromisoformat(str(row.get("date")))
        except (TypeError, ValueError):
            continue
        if d < since or d > session:
            continue
        out.setdefault(symbol, []).append({
            "date": row["date"], "title": row.get("title", ""), "feed": row.get("feed"),
        })
    return out


def _build_agent_disclosures(
    root: Path, symbols: set[str], session: date, days: int = 30,
) -> dict[str, list[dict]]:
    """AI 심층 해석 입력 — 후보별 DART 원장 최근 `days`일치 원본(제목+날짜,
    `get_disclosures` 툴). `_load_disclosures`(라벨 문자열, 2일 창 고정)와
    매칭 축(stock_code)은 같지만, 여기는 `days` 툴 파라미터가 창을 조절해야
    하므로 원시 (date, report_nm) 쌍만 담고 유형·촉매 라벨링
    (`classify_report`)은 도구 실행 시점(`agent_interpret`)으로 미룬다."""
    path = root / "data" / "ledger" / "disclosures.jsonl"
    if not path.exists() or not symbols:
        return {}
    since = session - timedelta(days=days)
    out: dict[str, list[dict]] = {}
    try:
        import json as _json

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            code = row.get("stock_code")
            if code not in symbols:
                continue
            rcept_dt = str(row.get("rcept_dt") or "")
            try:
                d = date(int(rcept_dt[:4]), int(rcept_dt[4:6]), int(rcept_dt[6:8]))
            except (ValueError, IndexError):
                continue
            if d < since or d > session:
                continue
            out.setdefault(code, []).append({"date": d.isoformat(), "report_nm": row.get("report_nm", "")})
    except (OSError, ValueError) as e:  # noqa: BLE001
        print(f"AI 심층 해석 공시 데이터 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    return out


def _score_breakdown_from_intraday(candidates: list[dict]) -> dict[str, dict]:
    """AI 심층 해석 입력 — `get_score_breakdown` 툴. `_build_intraday_view` 가
    이미 계산한 값을 그대로 감쌀 뿐 재계산하지 않는다."""
    return {c["symbol"]: {"score100": c["score100"], "factors": c["factors"]} for c in candidates}


def _build_track_record(root: Path) -> dict[str, dict]:
    """AI 심층 해석 입력 — `get_track_record` 툴. producer별 D+1 채점 간이
    통계(표본 수·승률·평균 bps)를 선정 원장에서 집계한다.

    `quant.control.leaderboard`(순위상관 rank IC 승격 판정)와는 목적이
    다르다 — 그건 프로듀서를 승격/폐기할지 사람이 결정하는 엄격한 통계고,
    여긴 에이전트가 스스로 확신도를 실측치에 정박시키는 참고용 요약이라
    더 단순하게 D+1 만 본다.

    읽기 실패는 빈 dict — 다른 `_build_*`/`_load_*` 헬퍼와 같은 관례로
    리포트를 막지 않는다."""
    try:
        rows = selections.load(root / "data" / "ledger" / "selections.jsonl")
    except Exception as e:  # noqa: BLE001
        print(f"AI 심층 해석 성적 원장 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}

    counts: dict[str, int] = {}
    bps_by_producer: dict[str, list[float]] = {}
    for row in rows:
        producer = row.get("producer")
        if not producer:
            continue
        counts[producer] = counts.get(producer, 0) + 1
        bps = row.get("outcome_d1_bps")
        if bps is not None:
            bps_by_producer.setdefault(producer, []).append(float(bps))

    out: dict[str, dict] = {}
    for producer, n in counts.items():
        vals = bps_by_producer.get(producer) or []
        if vals:
            wins = sum(1 for v in vals if v > 0)
            out[producer] = {
                "n_selections": n,
                "n_scored_d1": len(vals),
                "win_rate_d1": round(wins / len(vals), 3),
                "avg_bps_d1": round(sum(vals) / len(vals), 1),
            }
        else:
            out[producer] = {"n_selections": n, "n_scored_d1": 0, "win_rate_d1": None, "avg_bps_d1": None}
    return out


def _build_agent_interpret(
    root: Path, snap, payload: dict, intraday_view: list[dict], midterm_view: list[dict],
    telegram_mentions: dict[str, dict], time_budget_seconds: float | None = None,
) -> tuple[list[dict], str]:
    """AI 심층 해석(서브프로젝트 U) — 단타 후보 top-`AGENT_INTERPRET_TOP_N`에
    툴콜링 에이전트를 돌려 "왜 지금 이 종목인가" 산문 + 방향·확신 판정을
    만든다.

    **폴백(2026-08-18, 사용자 지시)**: 오늘 단타 후보(`intraday_view`)가
    0건이면 — `_build_intraday_view`가 KR 전용이라 US 리포트는 항상 이
    경로였다 — 중기 관심 종목(`midterm_view`, 이미 계산된 값을 그대로
    받는다, 재계산하지 않는다) 상위 `AGENT_INTERPRET_TOP_N`개로 대상을
    바꾼다. 둘 다 0건이면 기존과 같이 완전히 건너뛴다.

    반환 `(view, status)`. `status`: `"skipped_no_candidates"`(단타·중기
    후보 모두 없음) | `"skipped_no_key"`(OPENROUTER_API_KEY 없음) |
    `"ok"`/`"ok_midterm_fallback"`(N건 해석 성공, 대상이 단타 후보였는지
    중기 폴백이었는지 구분) | `"failed"`/`"failed_midterm_fallback"`(후보는
    있었으나 전부 실패, 마찬가지로 구분). `engine.json`(아침)/
    `close_engine.json`(오후)의 `agent_interpret` 필드에 그대로 남는다 —
    빌드가 LLM 지연/실패로 죽지 않았다는 증거이자, 폴백이 실제로 발동했는지의
    증거다. `view`의 각 원소에도 `source`("intraday"|"midterm")를 실어
    선정 원장(`_record_agent_interpret_selections`)까지 그대로 흘려보낸다
    — 채점 분해(단타 해석 vs 중기 폴백 해석)를 나중에 나눌 수 있게.

    `get_score_breakdown` 툴 입력(`score_breakdown`)은 단타 후보일 때만
    채운다 — 중기 후보 dict(`midterm_watch.build_midterm_watch` 반환)엔
    `score100`/`factors` 키 자체가 없다(단타 스코어러 v4 전용 필드).

    실패해도 리포트를 막지 않는다(narrate 계약과 동일, `chat_with_tools`
    자체가 예외 대신 `None`을 돌려주지만 이 함수도 한 번 더 감싼다) —
    예외를 통째로 삼킨다.
    """
    candidates = intraday_view[:AGENT_INTERPRET_TOP_N]
    source = "intraday"
    if not candidates:
        candidates = midterm_view[:AGENT_INTERPRET_TOP_N]
        source = "midterm"
    if not candidates:
        return [], "skipped_no_candidates"
    try:
        from functools import partial
        from time import monotonic

        from quant.adapters.env import get_key
        from quant.adapters.narrate import chat_with_tools

        key = get_key("OPENROUTER_API_KEY")
        if not key:
            return [], "skipped_no_key"

        symbols = {c["symbol"] for c in candidates}
        session = snap.session_date
        data = AgentData(
            session_date=session.isoformat(),
            foreign_flow=_build_agent_foreign_flow(root, symbols),
            news_items=_build_agent_news_items(root, symbols, session),
            disclosures=_build_agent_disclosures(root, symbols, session),
            telegram_mentions={s: telegram_mentions.get(s) or {} for s in symbols},
            score_breakdown=(
                _score_breakdown_from_intraday(candidates) if source == "intraday" else {}
            ),
            track_record=_build_track_record(root),
        )
        chat = partial(chat_with_tools, api_key=key)
        t0 = monotonic()
        results = interpret_candidates(candidates, data, chat,
                                       time_budget_seconds=time_budget_seconds)
        for item in results:
            item["source"] = source
        elapsed = monotonic() - t0
        print(
            f"AI 심층 해석 {len(results)}/{len(candidates)}건 성공 "
            f"({elapsed:.1f}s, source={source})"
        )
        suffix = "" if source == "intraday" else "_midterm_fallback"
        return results, (("ok" if results else "failed") + suffix)
    except Exception as e:  # noqa: BLE001 — AI 해석 실패가 리포트를 막지 않는다
        print(f"AI 심층 해석 생략: {type(e).__name__}: {e}", file=sys.stderr)
        suffix = "" if source == "intraday" else "_midterm_fallback"
        return [], "failed" + suffix
