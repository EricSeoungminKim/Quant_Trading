"""휴장 기간 종합(소유자 요청 2026-08-29) 배선 — 마지막 개장일 이후 휴장 창의
engine.json + 스냅샷 캘린더 사건을 모아 "그동안 무슨 흐름이 있었고 오늘 개장에
어떤 의미인지"를 한 섹션으로 낸다.

결정론 집계(`quant.analyze.holiday_synthesis.aggregate`)가 뼈대이고, 그 위에
LLM 산문 한 문단을 얹는다(선택, `news.py`의 `_build_digest_prose`와 같은
무LLM 폴백 관례 — narrator 가 없거나 실패해도 섹션은 결정론 집계만으로
성립한다).
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.analyze.holiday_synthesis import aggregate, detect_gap, is_empty

from quant.report.collect.snapshot import _load_morning_snapshot
from quant.report.paths import _engine_json_path, _load_artifact


def _load_window(market: str, root: Path, out_root: Path, snap_root: Path,
                  window: list) -> list[dict]:
    """휴장 창 각 날짜의 engine.json + 스냅샷 calendar 원본을 읽는다.

    한쪽만 있어도(또는 둘 다 없어도) 그 날짜를 목록에 그대로 남긴다 — 결손
    표기(`aggregate`의 `missing_*_days`)는 호출부가 아니라 `aggregate` 몫이다.
    """
    days = []
    for d in window:
        engine = _load_artifact(_engine_json_path(out_root, market, d))
        try:
            day_snap = _load_morning_snapshot(snap_root, market, d)
        except Exception as e:  # noqa: BLE001 — 손상된 스냅샷 하나가 집계 전체를 막지 않는다
            print(f"휴장 종합: {d.isoformat()} 스냅샷 읽기 실패 — {type(e).__name__}: {e}",
                  file=sys.stderr)
            day_snap = None
        calendar_events = None
        if day_snap is not None:
            cal_result = day_snap.results.get("calendar")
            if cal_result is not None and cal_result.ok and cal_result.data:
                calendar_events = cal_result.data.get("events")
        days.append({"date": d, "engine": engine, "calendar_events": calendar_events})
    return days


def _build_holiday_synthesis_prose(view: dict, narrator=None) -> str | None:
    """휴장 기간 흐름 → 오늘 개장 의미 → 시스템 대처 산문 한 문단.

    `_build_stance_prose`(news.py)와 같은 관례: 판단이 아니라 이미 집계된
    사실을 서술만 한다. narrator 실패/부재 시 `None` — 호출부는 결정론
    집계만으로 이미 완전하다(무LLM 폴백)."""
    try:
        from quant.adapters.narrate import make_narrator

        lines = [
            "다음은 휴장 기간(주말·공휴일) 동안 쌓인 리포트 데이터를 오늘 개장",
            "아침에 종합한 것이다. 이 재료로 '휴장 기간 흐름 → 오늘 개장에 갖는",
            "의미 → 우리 시스템이 어떻게 대처하는지'를 하나의 문단(3문장 이내)으로",
            "서술하라. 새로운 판단(매수/매도 지시, 점수 변경)을 내리지 말고 이미",
            "나온 데이터를 설명만 하라. 반드시 한국어로만 답하라(다른 언어를 섞지 마라).",
            "",
            f"휴장 {view['gap_days']}일 (마지막 개장일 {view['last_open']} 이후).",
        ]
        if view["theme_freq"]:
            themes = ", ".join(f"{t['theme']}({t['count']})" for t in view["theme_freq"][:5])
            lines.append(f"휴장 기간 상위 테마: {themes}")
        if view["new_symbols"]:
            names = ", ".join(s["name"] for s in view["new_symbols"][:8])
            lines.append(f"휴장 기간 신규 편입 후보 {len(view['new_symbols'])}건: {names}")
        if view["stance_trend"]:
            trend = " → ".join(f"{t['date']} {t['score100']}점" for t in view["stance_trend"])
            lines.append(f"스탠스 점수 추이: {trend}")
        if view["high_impact_events"]:
            evs = ", ".join(f"{e['date']} {e['name']}" for e in view["high_impact_events"])
            lines.append(f"휴장 중 주요 이벤트: {evs}")
        lines += [
            "", "다음 형식으로 정확히 한 문단만 답하라(다른 텍스트 없이):",
            "휴장 종합: <문단>",
        ]
        prompt = "\n".join(lines)

        text = (narrator or make_narrator()).narrate(prompt)
        if not text:
            return None
        text = text.strip()
        if text.startswith("휴장 종합"):
            text = text.split(":", 1)[-1].strip() if ":" in text else text
        return text or None
    except Exception as e:  # noqa: BLE001 — 휴장 종합 AI 서술 실패가 리포트를 막지 않는다
        print(f"휴장 기간 종합 AI 서술 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _apply_holiday_synthesis(snap, root: Path, out_root: Path, snap_root: Path,
                              narrator=None) -> dict | None:
    """`report_cli._emit` 배선점.

    오늘이 휴장 뒤 첫 개장일 아침이 아니면(연속 거래일/앵커 판정 불가/주말)
    `None` — `payload["holiday_synthesis"]`에 그대로 실린다(`us_kr_bridge`와
    같은 관례: 키는 남기고 값을 `None`으로 둬 "없음"과 "빌드 전"을 소비자가
    구분하게 한다).

    이 함수 자체는 예외를 던지지 않는다 — 파일 로딩 실패는 `_load_window`가
    개별 날짜 결손으로 흡수하고, 산문 생성 실패는 `_build_holiday_synthesis_prose`
    내부에서 이미 삼킨다. 혹시 남는 예외까지 한 번 더 감싸 리포트 발행을
    막지 않는다(`_build_exec_summary`/`_build_section_advice`와 같은 관례).
    """
    try:
        gap = detect_gap(snap.market, root, snap.session_date)
        if gap is None:
            return None
        last_open, gap_days, window = gap

        days = _load_window(snap.market, root, out_root, snap_root, window)
        agg = aggregate(days)

        view = {
            "market": snap.market,
            "last_open": last_open.isoformat(),
            "gap_days": gap_days,
            "window_dates": [d.isoformat() for d in window],
            **agg,
            "prose": None,
        }
        if not is_empty(agg):
            view["prose"] = _build_holiday_synthesis_prose(view, narrator=narrator)
        return view
    except Exception as e:  # noqa: BLE001 — 휴장 종합 실패가 리포트 발행을 막지 않는다
        print(f"휴장 기간 종합 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None
