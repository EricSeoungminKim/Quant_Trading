"""텔레그램 발행 알림에 붙는 결정론 요약 렌더러 — engine.json payload 만 보고
문자열을 만든다(순수, 외부 I/O 없음).

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations


def _auto_watch_count(auto_watch: str) -> int:
    """`AUTO_WATCH:` 줄의 토큰 수. `"AUTO_WATCH: 없음"` → 0.

    `market_brief.auto_watch_tokens` 처럼 시장별 형태·상한을 검증하지 않는다
    — 여기선 "오늘 리포트가 몇 개를 실어보냈나"만 사람에게 보이면 되고,
    검증은 own_brief(watch-score 경로)가 이미 한다. 두 번 하지 않는다.
    """
    body = str(auto_watch or "").removeprefix("AUTO_WATCH:").strip()
    if not body or body == "없음":
        return 0
    return len(body.split())


def _format_summary(payload: dict) -> str:
    """텔레그램 발행 알림에 붙일 stdout 3줄 이내 결정론 요약(G Task 5).

    1행 `후보 N개` — auto_watch 토큰 수. 2행 `상위: 이름(점수) · …` — `symbols`
    를 `baseline_score100` 내림차순 상위 3. 동점은 **당일 등락률 내림차순**,
    그다음 심볼코드 오름차순(결정론). 코드순만 쓰면 baseline 포화(백로그
    P1-b — 08-16 실측 100점 9개) 탓에 당일 -5.8% 종목이 요약 맨 앞에 오는
    실사고가 있었다. 표시 정렬일 뿐 원장에 기록되는 점수는 건드리지 않는다.
    `baseline_score100` 이 없는(`None`) 심볼은 상위 산정에서 뺀다 — 0 으로
    위장하지 않는다(baseline.py 계약과 동일). 채점된 심볼이 하나도 없으면
    2행 자체를 만들지 않는다.
    """
    lines = [f"후보 {_auto_watch_count(payload.get('auto_watch'))}개"]

    def _chg(s: dict) -> float:
        v = s.get("change_pct")
        return v if isinstance(v, (int, float)) else float("-inf")

    scored = [
        s for s in (payload.get("symbols") or [])
        if isinstance(s, dict) and s.get("baseline_score100") is not None
    ]
    scored.sort(
        key=lambda s: (-s["baseline_score100"], -_chg(s), str(s.get("symbol", "")))
    )
    top3 = scored[:3]
    if top3:
        names = " · ".join(
            f"{s.get('name') or s.get('symbol')}({s['baseline_score100']})"
            for s in top3
        )
        lines.append(f"상위: {names}")

    line = _channel_digest_line(payload.get("channel_digest_summary"))
    if line:
        lines.append(line)

    return "\n".join(lines)


def _channel_digest_line(summary: dict | None) -> str | None:
    """📡 채널 브리핑 종합(2026-09-05) 발행 요약 4번째 줄 — `report_cli._emit`/
    `_emit_close`가 engine.json에 남긴 `channel_digest_summary`(직렬화 요약,
    Digest 객체 자체는 JSON이 아니다)를 한 줄로. 없으면(수집 실패·빈 창)
    `None` — 기존 3줄 계약을 건드리지 않는다."""
    if not summary:
        return None
    parts = []
    if summary.get("stance"):
        parts.append(summary["stance"])
    if summary.get("candidates"):
        parts.append(f"후보 {summary['candidates']}")
    if summary.get("risk_items"):
        parts.append(f"리스크 {summary['risk_items']}")
    return f"채널 브리핑: {' · '.join(parts)}" if parts else None


def _format_close_summary(payload: dict) -> str:
    """마감 리포트(서브프로젝트 R) 발행 알림 요약 — 아침 `_format_summary`와
    같은 3줄 이내 계약이지만, close 엔진 JSON에는 `auto_watch`/`symbols`가
    없다(대신 `intraday_view` — 마감 포지션 후보 top-8). 그래서 후보 수는
    `intraday_view` 길이, 상위 3은 `score100` 내림차순(동점은 심볼코드
    오름차순, 결정론)으로 별도 계산한다."""
    items = payload.get("intraday_view") or []
    lines = [f"마감 후보 {len(items)}개"]
    top3 = sorted(
        (it for it in items if isinstance(it, dict) and it.get("score100") is not None),
        key=lambda it: (-it["score100"], str(it.get("symbol", ""))),
    )[:3]
    if top3:
        names = " · ".join(
            f"{it.get('name') or it.get('symbol')}({it['score100']})" for it in top3
        )
        lines.append(f"상위: {names}")
    line = _channel_digest_line(payload.get("channel_digest_summary"))
    if line:
        lines.append(line)
    return "\n".join(lines)
