"""방어 국면(regime) 또는 강한 하락 진단일 때 후보 리스트 크기를 줄인다.

## 왜 (2026-09-06 리포트 정확도 감사, `results/report_review/SUMMARY.md` §②)

"강한 하락 신호"(score100=10~20) 날에도 후보가 26~32건 나갔다(08-25 KR
26건, 09-02 KR 32건, 09-03 KR 26건) — 반대로 "강한 상승 신호"(score100=80)
였던 08-13 KR은 11건뿐이었다. 후보는 전부 롱 온리인데, 스탠스가 하락을
불러도 롱 후보 물량이 줄지 않는다 — "발표를 확인한 뒤 진입한다"는 스탠스
문구와 실제로 30개 가까운 종목을 올리는 행동이 모순된다.

이 모듈은 그 모순을 없앤다: 국면이 방어(defensive)이거나 옛 점수 기반
진단(`quant.analyze.briefing.stance`)이 "강한 하락"(score100<=25)이면,
승격 목록(`AUTO_WATCH` 줄 — `own_brief.sh`가 그대로 읽어 확신도 엔진에
태우는 값)을 `top_n`(기본 8)으로 줄인다. **원장에서는 아무것도 지우지
않는다** — `payload["symbols"]`는 그대로 두고, 잘린 종목은
`watch_only`(관망) 목록으로만 표시한다(소유자 지시: "Never drop symbols
from the ledger — only from the promoted list").

## 정렬 기준: 트렌딩 + 확인(confirmation)

`quant/analyze/symbol_score.py` 모듈독스트링의 2026-09-06 감사: 뉴스+트렌딩
동시 확인("origin=both") D+1 평균 +278bp·적중 56.0%(n=166)가 news-only
(+28bp)·watch_join(+35bp)를 압도했고, `trending_score100` IC=+0.186이
`ai_score100`(-0.082, 역상관)보다 뚜렷이 높았다. 그래서 방어 국면에서
살아남을 top_n을 고를 때는 ai_score100이 아니라 **확인(origin=="both")
우선, 그다음 trending_score100 내림차순**으로 줄을 세운다 — 근거 없이
사라지는 종목이 없게, "가장 근거가 두꺼운 것부터 남긴다."

## 계약

이 모듈은 순수 함수만 담는다. `AUTO_WATCH:` 문자열 파싱은
`quant/report/collect/intraday.py::_candidate_symbols`와 같은 규칙이지만
독립적으로 다시 구현한다 — `quant/analyze/`가 `quant/report/`를 임포트하면
의존 방향이 뒤집힌다(report가 analyze 위에 얹히는 계층이다).
"""
from __future__ import annotations

DEFAULT_TOP_N = 8

# "강한 하락" 진단 임계 — `quant.analyze.briefing.stance()`의 액션 분기
# (`score100 <= 25: "신규 진입을 접고 보유 비중을 줄인다"`)와 같은 값.
DIAGNOSTIC_STRONG_BEAR_MAX = 25

GATE_REASON_LABEL = "관망(방어 국면)"


def _parse_auto_watch(auto_watch: str) -> tuple[str, list[str]]:
    """`"AUTO_WATCH: SYM:TAG SYM2:TAG2 ..."` → (prefix, tokens). 빈 목록/
    "없음"은 `tokens=[]`."""
    prefix = "AUTO_WATCH:"
    body = auto_watch[len(prefix):].strip() if auto_watch.startswith(prefix) else auto_watch.strip()
    if not body or body == "없음":
        return prefix, []
    return prefix, body.split()


def is_defensive_stance(regime_label: str | None, diagnostic_score100: int | None) -> bool:
    """국면이 방어(defensive)이거나, 옛 점수 기반 진단이 강한 하락이면 True.
    둘 다 없으면(regime 판정 불가 + 진단 결측) False — 게이트는 "확실히
    방어적일 때만" 발동한다(불확실할 때 후보를 줄이면 그 자체가 새로운
    실수 표면이 된다)."""
    if regime_label == "defensive":
        return True
    return diagnostic_score100 is not None and diagnostic_score100 <= DIAGNOSTIC_STRONG_BEAR_MAX


def _rank_key(entry: dict) -> tuple:
    """확인(origin=="both") 우선 → trending_score100 내림차순 → ai_score100
    내림차순 → 심볼(결정론 tie-break)."""
    confirmed = 0 if entry.get("origin") == "both" else 1
    trending = entry.get("trending_score100")
    trending_key = -trending if isinstance(trending, (int, float)) else 0
    ai = entry.get("ai_score100")
    ai_key = -ai if isinstance(ai, (int, float)) else 0
    return (confirmed, trending_key, ai_key, str(entry.get("symbol") or ""))


def gate_candidates(
    auto_watch: str, symbols: list[dict], *, regime_label: str | None,
    diagnostic_score100: int | None, top_n: int = DEFAULT_TOP_N,
) -> dict:
    """방어 국면이 아니면 입력을 그대로 돌려준다(`capped=False`). 방어
    국면이고 후보가 `top_n`을 넘으면 상위만 남긴 `auto_watch` 문자열과
    잘려나간 심볼 목록을 낸다.

    반환: `{"auto_watch": str, "watch_only": [symbol,...], "capped": bool,
    "gate_reason": str|None}`. `watch_only`에 실린 심볼은 `payload["symbols"]`
    에서 지우지 않는다 — 호출부가 그 항목에 표시용 상태만 얹는다.
    """
    if not is_defensive_stance(regime_label, diagnostic_score100):
        return {"auto_watch": auto_watch, "watch_only": [], "capped": False, "gate_reason": None}

    prefix, tokens = _parse_auto_watch(auto_watch)
    if len(tokens) <= top_n:
        return {"auto_watch": auto_watch, "watch_only": [], "capped": False, "gate_reason": None}

    by_symbol = {s.get("symbol"): s for s in symbols if s.get("symbol")}
    token_by_symbol = {t.split(":", 1)[0]: t for t in tokens}
    ordered = sorted(token_by_symbol, key=lambda sym: _rank_key(by_symbol.get(sym) or {}))
    keep = set(ordered[:top_n])
    kept_tokens = [t for t in tokens if t.split(":", 1)[0] in keep]
    dropped = [sym for sym in token_by_symbol if sym not in keep]

    return {
        "auto_watch": f"{prefix} " + " ".join(kept_tokens),
        "watch_only": dropped,
        "capped": True,
        "gate_reason": (
            f"방어 국면 — 후보 {len(tokens)}건 중 상위 {top_n}건만 승격, "
            f"{len(dropped)}건 {GATE_REASON_LABEL}"
        ),
    }
