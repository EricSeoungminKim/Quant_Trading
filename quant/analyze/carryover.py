"""개장일 집계 — 휴장 기간 engine JSON 을 오늘 payload 에 최신일 우선으로 병합
(서브프로젝트 G, Task 3).

**왜 필요한가.** `opendays.window_dates` 가 "마지막 개장일 다음날부터 오늘 전날까지"의
날짜를 내주지만, 그 사이 주말·공휴일마다 따로 생성된 engine.json 은 서로 남남이다.
휴장 이틀 동안 뉴스가 몰린 종목이 있어도, 아무도 그 파일을 안 열어보면 다음 개장일
아침엔 그냥 사라진다. 이 모듈은 그 창의 engine.json 전부를 오늘 payload 뒤에
이어붙여, 첫 개장일 자동편입(`quant/analyze/market_brief.engine_tokens` →
own_brief → watch-score)이 휴장 기간 근거를 놓치지 않게 한다.

**의도된 부작용: EVENT 신선도가 오늘 날짜로 찍힌다.** `market_brief.engine_tokens`
는 EVENT 태그에 오늘 `session_date` 를 붙인다(뉴스 신선도 30점 배점). 캐리해 온
휴장 기간 뉴스도 이 병합을 거치면 오늘 `auto_watch` 줄의 일부가 되므로, 실제로는
며칠 전 뉴스인데도 "오늘 신선"으로 채점된다. 이건 버그가 아니라 의도다 —
휴장 기간 재료는 다음 개장일이 그 재료가 시장에 처음 반영되는 날이라, 그날
기준으로 신선하다고 보는 것이 맞다.
"""
from __future__ import annotations

from datetime import date


def merge_carryover(payload: dict, prior: list[tuple[date, dict]]) -> dict:
    """`prior`(날짜 오름차순의 (날짜, 그날 engine payload) 목록)를 `payload` 뒤에 병합.

    (a) `symbols`: 오늘 `payload` 에 이미 있는 `symbol` 은 그대로 둔다(오늘 우선).
        오늘에 없는 심볼만 prior 에서 가져오되, 여러 prior 날짜에 같은 심볼이
        있으면 **가장 최근 날짜** 것을 쓰고 `carried_from: "YYYY-MM-DD"` 를 붙인다.
    (b) `auto_watch`: `"AUTO_WATCH: SYM:TAG ..."` 문자열에서, prior 토큰 중 오늘
        토큰에 없는 심볼만 뒤에 덧붙인다(오늘 토큰 순서가 앞선다 — 브리핑의
        상한(MAX_CANDIDATES)이 오늘 것부터 채우게 하기 위해서다). 여기서도 같은
        심볼이 여러 prior 날짜에 있으면 최신 날짜의 토큰이 이긴다.

    두 키 모두 없으면 그대로 반환한다(하위 호환). 입력(`payload`/`prior` 안의
    dict)은 변경하지 않는다 — 항상 새 dict/list 를 만들어 반환한다.
    """
    merged = dict(payload)

    if "symbols" in payload:
        merged["symbols"] = _merge_symbols(payload.get("symbols") or [], prior)

    if "auto_watch" in payload:
        merged["auto_watch"] = _merge_auto_watch(str(payload.get("auto_watch") or ""), prior)

    return merged


def _merge_symbols(today_symbols: list[dict], prior: list[tuple[date, dict]]) -> list[dict]:
    today_syms = {s.get("symbol") for s in today_symbols if isinstance(s, dict)}

    # prior 는 오름차순으로 들어오므로, 같은 심볼이 여러 날 나오면 나중 날짜가
    # 먼저 것을 그냥 덮어써서 "최신 prior 날짜" 가 자연히 남는다.
    carry: dict[str, tuple[date, dict]] = {}
    for d, day_payload in prior:
        for s in (day_payload or {}).get("symbols") or []:
            sym = s.get("symbol") if isinstance(s, dict) else None
            if not sym or sym in today_syms:
                continue
            carry[sym] = (d, s)

    out = list(today_symbols)
    for _, (d, s) in carry.items():
        entry = dict(s)
        entry["carried_from"] = d.isoformat()
        out.append(entry)
    return out


def _merge_auto_watch(today_line: str, prior: list[tuple[date, dict]]) -> str:
    prefix = ""
    body = today_line
    if today_line.startswith("AUTO_WATCH:"):
        prefix, _, body = today_line.partition(":")
        prefix += ":"

    today_tokens = body.split()
    today_syms = {t.split(":", 1)[0] for t in today_tokens}

    # 같은 심볼이 여러 prior 날짜에 있으면 나중(최신) 날짜의 토큰이 이긴다 —
    # symbols 병합과 같은 관례.
    carry: dict[str, str] = {}
    for _, day_payload in prior:
        line = str((day_payload or {}).get("auto_watch") or "")
        pbody = line.split(":", 1)[1] if line.startswith("AUTO_WATCH:") else line
        for tok in pbody.split():
            sym = tok.split(":", 1)[0]
            if sym in today_syms:
                continue
            carry[sym] = tok

    merged_tokens = today_tokens + list(carry.values())
    body_str = " ".join(merged_tokens) if merged_tokens else "없음"
    return f"{prefix} {body_str}".strip() if prefix else body_str
