"""휴장 기간 종합 — 결정론 집계(소유자 요청 2026-08-29).

**왜 필요한가.** 주말·공휴일에도 리포트는 매일 발행되지만(`opendays.py` 참고),
그 며칠은 서로 이어지지 않는다 — 다음 개장일 아침에 "휴장 동안 무슨 흐름이
있었는가"를 한 번에 보여주는 서사가 없다. `carryover.py`가 이미 개별 종목
후보를 오늘 payload 에 병합하지만, 그건 종목 단위 병합일 뿐 "무슨 일이
있었나 종합"은 아니다.

이 모듈은 순수 함수만 담는다 — LLM 없음, 네트워크 없음. `detect_gap`은
`opendays.last_open_day`(앵커 일봉 parquet 직접 읽기, opendays.py 와 동일한
파일 I/O 관례)를 재사용하지만 그 밖의 함수(`aggregate`/`is_empty`)는 이미
로드된 데이터만 받는 완전한 순수 함수다. LLM 산문 배선은
`quant.report.collect.holiday_synthesis`(리포트 평면) 몫이다 —
`quant/analyze` → `quant/adapters` 임포트는 아키텍처 규칙(`test_architecture.py`
FORBIDDEN)이 금지한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from quant.analyze.opendays import anchor_dir_for, last_open_day, window_dates

# 리포트 상단에 낼 테마 수 — 너무 길면 안 읽힌다(digest TOP_N=8 관례와 같은 절제).
_TOP_THEMES = 10


def detect_gap(market: str, root: Path, today: date) -> tuple[date, int, list[date]] | None:
    """오늘이 "휴장 뒤 첫 개장일 아침"인지 판정한다.

    `opendays.last_open_day`가 `today` 미만 마지막 개장일을 찾는다. 판정
    불가(앵커 데이터 없음)면 `None`(opendays.py 관례 — 안전한 방향은 집계를
    건너뛰는 쪽이지 지어내는 쪽이 아니다). 어제가 그 개장일이면(연속 거래일)
    휴장이 없었으므로 `None`.

    **평일에만 판정한다** — `today`가 토/일(`weekday() >= 5`)이면 오늘 자체가
    아직 개장일이 아니므로 `None`(휴장이 진행 중일 뿐 "재개장 아침"이 아니다).
    공휴일표를 쓰지 않으므로(opendays.py 철학) 평일 중간에 낀 공휴일(예: 추석
    연휴 화·수·목)은 이 판정만으로 완벽히 걸러지지 않는다 — 그 경우 마지막
    휴장 평일까지 재실행 때마다 이 함수가 계속 갭을 보고할 수 있다(과소
    집계보다 과다 노출이 안전한 방향이라는 opendays.py 의 기존 선택을 그대로
    따른다). 주말 갭(전체 휴장의 절대다수)은 이 조건으로 정확히 걸러진다.

    반환: `(last_open, gap_days, window)`.
    `gap_days` = 어제 기준 휴장 캘린더일 수. `window` = `opendays.window_dates`와
    동일 규칙(오름차순, cap=7)의 휴장 기간 날짜 목록. 창이 비면(길이 0) `None`.
    """
    if today.weekday() >= 5:
        return None
    last_open = last_open_day(anchor_dir_for(market, root), today)
    if last_open is None:
        return None
    yesterday = today - timedelta(days=1)
    if last_open == yesterday:
        return None
    window = window_dates(last_open, today)
    if not window:
        return None
    gap_days = (yesterday - last_open).days
    return (last_open, gap_days, window)


def aggregate(days: list[dict]) -> dict:
    """휴장 창 결정론 집계.

    `days`는 날짜 오름차순, 각 원소는
    `{"date": date, "engine": dict|None, "calendar_events": list[dict]|None}`.
    `engine`은 그날 `{market}_engine.json`(`render.machine_payload` 출력),
    `calendar_events`는 그날 스냅샷의 `calendar` 소스 결과(`data["events"]`,
    `quant.collect.sources.calendar.to_dday` 출력)다.

    `engine`/`calendar_events`가 `None`이면 그날 데이터가 결손이다 — 그
    날짜만 건너뛰고 나머지로 부분 집계한다(과소 집계보다 안전, opendays.py
    관례와 동일). 결손 날짜는 `missing_engine_days`/`missing_calendar_days`로
    남는다.

    반환:
        theme_freq — 심볼별 `sector`(engine.json, KR 전용 — US는 자연히
            빈다) 빈도 상위 `_TOP_THEMES`, count 내림차순.
        new_symbols — 창 안에서 `is_new`였던 심볼(첫 등장일 기준 오름차순,
            같은 심볼이 여러 날 나와도 한 번만).
        stance_trend — 날짜별 `stance.score100`(있는 날만, 날짜 오름차순).
        high_impact_events — 그날 스케줄된(`is_today`) 고영향 이벤트
            (`high_impact`), 날짜 오름차순.
    """
    missing_engine_days = [d["date"].isoformat() for d in days if d.get("engine") is None]
    missing_calendar_days = [
        d["date"].isoformat() for d in days if d.get("calendar_events") is None
    ]

    theme_counts: dict[str, int] = {}
    new_symbols: dict[str, dict] = {}
    stance_trend: list[dict] = []
    high_impact_events: list[dict] = []

    for row in days:
        d = row["date"]
        engine = row.get("engine")
        if engine is not None:
            for sym in engine.get("symbols") or []:
                if not isinstance(sym, dict):
                    continue
                theme = sym.get("sector")
                if theme:
                    theme_counts[theme] = theme_counts.get(theme, 0) + 1
                symbol = sym.get("symbol")
                if symbol and sym.get("is_new") and symbol not in new_symbols:
                    new_symbols[symbol] = {
                        "symbol": symbol,
                        "name": sym.get("name", symbol),
                        "first_seen": d.isoformat(),
                    }
            stance = engine.get("stance") or {}
            if stance.get("score100") is not None:
                stance_trend.append({
                    "date": d.isoformat(),
                    "score100": stance["score100"],
                    "label": stance.get("label"),
                })

        events = row.get("calendar_events")
        if events:
            for ev in events:
                if ev.get("high_impact") and ev.get("is_today"):
                    high_impact_events.append({"date": d.isoformat(), "name": ev.get("name")})

    theme_freq = sorted(
        ({"theme": t, "count": c} for t, c in theme_counts.items()),
        key=lambda x: (-x["count"], x["theme"]),
    )[:_TOP_THEMES]

    return {
        "missing_engine_days": missing_engine_days,
        "missing_calendar_days": missing_calendar_days,
        "theme_freq": theme_freq,
        "new_symbols": sorted(
            new_symbols.values(), key=lambda s: (s["first_seen"], s["symbol"]),
        ),
        "stance_trend": stance_trend,
        "high_impact_events": high_impact_events,
    }


def is_empty(agg: dict) -> bool:
    """집계 결과에 산문을 만들 재료가 하나도 없는가 — narrator 호출 여부 판단용."""
    return not (
        agg["theme_freq"] or agg["new_symbols"] or agg["stance_trend"]
        or agg["high_impact_events"]
    )
