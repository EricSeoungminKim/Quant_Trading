"""중기 관심 종목(서브프로젝트 W part 3) — 리포트 하단 "앞으로 투자하기 좋아
보이는 종목". 텔레그램 언급 빈도로 후보를 고르고(mention_counts), 외국인
수급 + 호재/악재 판정으로 진입 등급(entry_grade, W-1)을 매긴다. AI
(narrate_prose)는 산문 요약만 얹는다 — 등급은 절대 LLM이 정하지 않는다
(midterm_watch.py 모듈 docstring). 아침(`_emit`)+오후(`_emit_close`) 양쪽
적용.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path

from quant.analyze.bullish_markers import classify_titles
from quant.analyze.entities import load_name_map, load_table
from quant.analyze.midterm_watch import (
    all_beneficiary_symbols,
    build_midterm_watch,
    build_us_news_kr_map,
    mention_counts as midterm_mention_counts,
    narrate_prose as midterm_narrate_prose,
)
from quant.collect.sources.telegram_channels import load_ledger as load_telegram_ledger

from quant.report.paths import _paths
from quant.report.collect.agent_interpret import _build_agent_foreign_flow, _build_agent_news_items
from quant.report.collect.telegram import _usnews_titles

_MIDTERM_PRODUCER = "midterm_watch_v1"
_MIDTERM_PRODUCER_CLOSE = "midterm_watch_v1_close"

# 후보 호재/악재 판정 입력 창(일) — 20일 외국인 수급 창(_build_agent_foreign_flow)
# 과 같은 폭으로 맞춘다(둘 다 "최근 한 달가량의 활동"을 보는 창).
_MIDTERM_BULLISH_WINDOW_DAYS = 20


def _load_midterm_telegram_msgs(root: Path) -> list[dict]:
    """텔레그램 누적 원장(`telegram_msgs.jsonl`) 전체 — 중기 후보 선정은
    당일 fetch(`_fetch_telegram_briefs`)가 아니라 여러 날짜가 쌓인 이 원장을
    읽어야 "최근 N일 언급"을 판정할 수 있다(`midterm_watch.mention_counts`
    docstring). 실패해도 리포트를 막지 않는다 — 다른 `_load_*` 헬퍼와 같은 관례."""
    try:
        return load_telegram_ledger(root / "data" / "ledger" / "telegram_msgs.jsonl")
    except Exception as e:  # noqa: BLE001
        print(f"중기 관심 종목 텔레그램 원장 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _build_midterm_bullish(root: Path, symbols: set[str], session: date) -> dict[str, dict]:
    """중기 후보의 호재/악재 판정 입력 — 뉴스 원장(`mentions.jsonl`) 최근
    `_MIDTERM_BULLISH_WINDOW_DAYS`일 제목에 `bullish_markers.classify_titles`를
    적용한다. `_build_agent_news_items`(AI 심층 해석 입력)를 그대로 재사용한다
    — 원장 읽기 로직을 새로 만들지 않는다."""
    news_items = _build_agent_news_items(root, symbols, session, days=_MIDTERM_BULLISH_WINDOW_DAYS)
    return {
        symbol: classify_titles([item["title"] for item in items])
        for symbol, items in news_items.items()
    }


def _midterm_entities(root: Path, market: str, payload: dict):
    """`mention_counts`/`build_midterm_watch`의 `entities` 인자 — KR은 상장사
    사전(`entities.load_table`), US는 오늘 payload 심볼 화이트리스트다
    (`telegram_view.telegram_mentions`의 `name_table`과 같은 계약)."""
    if market == "KR":
        _, _, cache_dir, _ = _paths(root)
        return load_table(cache_dir)
    return {s.get("symbol") for s in payload.get("symbols") or [] if s.get("symbol")}


def _midterm_name_by_symbol(root: Path, market: str, payload: dict) -> dict[str, str]:
    """종목명 표시용 — KR은 상장사 사전 캐시 재사용(`load_name_map`, 네트워크
    추가 없음), US는 오늘 payload 심볼에 이미 실린 이름."""
    if market == "KR":
        _, _, cache_dir, _ = _paths(root)
        return load_name_map(cache_dir, "KR")
    return {s["symbol"]: s.get("name") for s in payload.get("symbols") or [] if s.get("symbol")}


def _build_midterm_watch_view(
    root: Path, market: str, payload: dict, telegram_msgs: list[dict], session: date,
) -> list[dict]:
    """중기 관심 종목 후보(결정론) — 아래 순서로 조립한다:
    1) 후보 심볼 발굴(`mention_counts`) — 후보를 알아야 그 종목만 원장을 좁혀
       읽을 수 있다(전체 종목을 읽지 않는다, `_build_agent_*` 헬퍼와 같은 절약).
    2) 후보 심볼만 외국인 수급(`_build_agent_foreign_flow`, KR 전용)·호재/악재
       (`_build_midterm_bullish`)를 로드.
    3) `build_midterm_watch`가 (내부에서 같은 발굴을 재현하며) 등급을 매기고
       정렬한다 — 1)과 3)이 같은 결정론 계산을 두 번 하지만, 원장 I/O 자체는
       한 번만 한다(비용은 발굴이 아니라 원장 읽기에 있다). `now`를 고정해
       두 계산이 같은 시각 기준으로 후보 집합을 재현하게 한다.

    실패해도 리포트를 막지 않는다 — 다른 `_build_*` 헬퍼와 같은 관례."""
    try:
        now = datetime.now(timezone.utc)
        entities = _midterm_entities(root, market, payload)
        candidates = midterm_mention_counts(market, telegram_msgs, entities, now=now)
        symbols = set(candidates.keys())
        if not symbols:
            return []
        frgn_rows = _build_agent_foreign_flow(root, symbols) if market == "KR" else {}
        bullish = _build_midterm_bullish(root, symbols, session)
        name_by_symbol = _midterm_name_by_symbol(root, market, payload)
        return build_midterm_watch(
            market, telegram_msgs, frgn_rows, bullish, entities,
            name_by_symbol=name_by_symbol, now=now,
        )
    except Exception as e:  # noqa: BLE001
        print(f"중기 관심 종목 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _build_midterm_prose(candidates: list[dict], narrator=None) -> dict[str, str]:
    """중기 후보 전망 산문(선택, LLM, 종목당 1콜) — narrator 를 안 넘기면
    (마감판 `_emit_close` 등 기존 호출부) 모델은 U(툴콜링 해석 에이전트)가
    실측한 1순위(`narrate.TOOL_MODEL`)를 명시 지정한다. 단순 요약 1콜이면
    충분해 `chat_with_tools`(도구 루프)는 쓰지 않는다(사용자 지시). 실패해도
    리포트를 막지 않는다.

    `narrator`(선택) — 아침판(`_emit`)이 품질 레인을 4곳에서 공유하려고
    주입한다(2026-08-18, `_build_digest_prose`와 같은 관례). 이 경우
    `TOOL_MODEL` 지정은 적용되지 않는다 — 품질 레인은 Claude CLI 가 1순위라
    OpenRouter 모델 선택 자체가 폴백 경로에서만 의미가 있고, 그 폴백 모델은
    호출부(`_emit`)가 `make_quality_narrator(model=TOOL_MODEL)`로 직접
    지정한다."""
    if not candidates:
        return {}
    try:
        from quant.adapters.narrate import TOOL_MODEL, make_narrator

        return midterm_narrate_prose(candidates, narrator or make_narrator(model=TOOL_MODEL))
    except Exception as e:  # noqa: BLE001
        print(f"중기 관심 종목 AI 전망 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _apply_midterm_prose(view: list[dict], prose_by_symbol: dict[str, str]) -> list[dict]:
    for item in view:
        item["prose"] = prose_by_symbol.get(item["symbol"])
    return view


def _build_us_news_kr_view(root: Path, telegram_result: dict, session: date) -> list[dict]:
    """"🇺🇸→🇰🇷 미국발 섹터 수혜주" 소구획(KR 리포트 전용, 서브프로젝트 W part 2/3).

    수혜주 범위는 `us_sector_map.KR_BENEFICIARIES`(정적 큐레이션) 전체
    (`all_beneficiary_symbols`) — 텔레그램 언급과 무관하게 항상 같은 고정
    종목코드 집합이라 매 빌드 같은 비용으로 로드할 수 있다."""
    try:
        titles = _usnews_titles(telegram_result)
        if not titles:
            return []
        symbols = all_beneficiary_symbols()
        frgn_rows = _build_agent_foreign_flow(root, symbols)
        bullish = _build_midterm_bullish(root, symbols, session)
        return build_us_news_kr_map(titles, frgn_rows, bullish)
    except Exception as e:  # noqa: BLE001
        print(f"미국발 섹터 수혜주 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return []
