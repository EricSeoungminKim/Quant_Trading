"""리포트 세션 스냅샷 수집 타이밍 — 뉴스 창 계산 + 스냅샷 수집/저장 경로.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from quant.collect.snapshot import collect, load_snapshot
from quant.collect.sources import build_seeded_source, build_sources
from quant.core.report_clock import session_window


def news_since_for(prev, now: datetime) -> datetime:
    """뉴스 표본의 시작점 = 직전 리포트 생성시각. 없으면 24시간 전(session_window 계약).

    **`prev.generated_at` 은 이미 `datetime` 이다** — `Snapshot.from_json` 이 파싱한다.
    여기서 `datetime.fromisoformat()` 을 한 번 더 부르면 TypeError 로 리포트 빌드가
    통째로 죽는다. 2026-08-14 KR 08:00 이 실제로 그렇게 죽었고, `prev` 가 있을 때만
    터지는 탓에 스냅샷이 하루치 쌓인 다음 날에야 드러났다.

    한 줄짜리지만 함수로 뽑은 이유: 원래 자리는 네트워크를 타는 `main()` 안이라
    테스트가 닿지 못했다. 그래서 이 버그가 운영에서 처음 발견됐다.
    """
    return session_window(now, prev.generated_at if prev else None)[0]


# ------------------------------------------------------------------ 마감 포지션 리포트 (서브프로젝트 R)
# 13:40 KST 빌드 → 13:50 발행. 아침 리포트(개장 60분 전)와 별도 산출물
# (`{market}_close_report.html`/`{market}_close_engine.json`)이고, 아침
# 리포트를 절대 덮지 않는다. KR 전용(비목표: US 오후판 없음 — 정규장 구조가
# 다르다). 스냅샷도 별도 파일명(`_close.json`)에 저장해 아침 체인의
# `previous_snapshot()`("{date}.json" 패턴만 스캔)에 절대 걸리지 않는다.


def _close_snapshot_path(snap_root: Path, market: str, session) -> Path:
    """마감 리포트 스냅샷 경로 — 아침 경로(`{market}/{date}.json`)와 다른
    파일명(`_close` 접미사)이라 `delta.previous_snapshot()`(정확히
    `{date}.json` 패턴만 글롭 없이 직접 조립해 스캔한다)이 이 파일을 절대
    "직전 스냅샷"으로 집어 올 수 없다 — 아침 체인 격리가 파일명 규칙만으로
    보장된다(CRITICAL, 스펙 §1)."""
    return snap_root / market / f"{session.isoformat()}_close.json"


def _load_morning_snapshot(snap_root: Path, market: str, session):
    """오늘 아침(open 세션) 스냅샷 — 있으면 그 `generated_at`이 마감 리포트
    뉴스 창의 시작점이 된다(`close_news_since_for`). 아침 경로와 완전히
    동일한 파일(`{market}/{date}.json`)을 그대로 읽는다 — 별도 저장을 하지
    않으므로 아침 리포트가 이미 쓴 스냅샷 하나를 공유할 뿐이다."""
    path = snap_root / market / f"{session.isoformat()}.json"
    if not path.exists():
        return None
    return load_snapshot(path)


def close_news_since_for(morning_snap, now: datetime) -> datetime:
    """마감 리포트 뉴스 창의 시작점(스펙 §내용 1) — 오늘 아침 스냅샷의
    생성시각. 오늘 아침 리포트가 아직 없으면(수동 재실행·결측 등)
    `CLOSE_NEWS_FALLBACK_WINDOW`(6시간) 전으로 폴백한다."""
    if morning_snap is not None:
        return morning_snap.generated_at
    return now - CLOSE_NEWS_FALLBACK_WINDOW


def _collect_snapshot(market: str, session, cache_dir: Path, news_since: datetime):
    """1차(공통 소스) + 2차(랭킹 시드 뉴스) 배치 수집 — `main()`의 기존 `build`
    (open) 블록과 마감 `build`가 공유한다. 로직은 기존 open 경로에서 그대로
    옮긴 것(동작 변화 없음) — open 블록 자체는 회귀 위험을 피하려 손대지
    않고 그대로 남겨 두었다."""
    snap = collect(
        market, session, build_sources(market, session, news_since=news_since),
    )
    ranking = snap.results.get("toss_rankings")
    if ranking is not None and ranking.ok and ranking.data:
        # resolver 는 분석 평면에서 만들어 주입한다(부채 상환 2026-08-24 —
        # 수집이 종목 사전을 직접 만들면 collect → analyze 평면 위반).
        from quant.analyze.entities import make_symbol_resolver

        seeded = collect(
            market, session,
            build_seeded_source(
                market, ranking.data,
                resolver_factory=lambda: make_symbol_resolver(market, cache_dir),
            ),
        )
        snap = replace(snap, results={**snap.results, **seeded.results})
    return snap


# 오늘 아침 스냅샷이 없을 때(수동 재실행 등)의 폴백 창. `session_window`의
# 24시간 폴백을 그대로 쓰지 않는 이유 — 오후판이 24시간을 돌아보면 전날 밤
# 뉴스까지 다시 실어 "장중 신규"라는 절 취지가 깨진다(설계 스펙 §내용 1).
CLOSE_NEWS_FALLBACK_WINDOW = timedelta(hours=6)
