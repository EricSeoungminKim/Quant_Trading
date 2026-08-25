"""전일 한국장 세션 패턴 분류 — 마감 종합(양시장) 리포트의 KR 절반 (2026-08-25).

소유자 지시 원문이 곧 스펙이다:

> "전날 한국장에서 거래세가 초반부터 몰리고 안 빠진 종목들, 후반에 회복하며
> 전고점을 뚫은 종목, 후반에 급 매수세가 파동친 종목 등을 전체적으로 파악하고,
> 최종적인 전날 한국장의 외국인 및 기관 매수세 흐름 또한 읽음으로써, 다음날
> 시작할 때 프로그램이 그 전날 종목들과 섹터들의 1분봉·일봉을 보고 조건에 맞는
> 종목 선정 및 진입각을 보게 되는 거지."

세 패턴은 전부 **1분봉의 모양**으로만 판정한다(수급·뉴스는 리포트의 다른 절이
담당 — 역할 분담 관례). 분류 문턱은 백테스트로 검증된 값이 아니라 소유자 서술을
정량화한 초기값이다 [미검증] — 파라미터로 노출해 두었고, 이 패턴 태그가 실제로
다음날 수익과 이어지는지는 선정 원장 채점(outcomes)이 판정한다.

순수 함수만 — 파일/네트워크 없음(봉 로딩은 호출부 몫).
"""
from __future__ import annotations

import pandas as pd

# 세션 구간 정의(분). KR 정규장 381분(09:00~15:30) 기준.
EARLY_MINUTES = 60      # "초반" = 개장 후 60분
LATE_MINUTES = 90       # "후반" = 마감 전 90분
SURGE_WINDOW = 60       # 매수 파동 판정 창 = 마지막 60분


def classify_session(day: pd.DataFrame) -> list[str]:
    """하루치 1분봉 → 해당하는 패턴 라벨 리스트(0~3개).

    - "초반강세지속": 개장 60분 내 +1.5% 이상 상승 출발 + 종가 강도>=0.6 +
      장중 고점 대비 되돌림이 상승폭의 40% 이내 — "초반부터 몰리고 안 빠짐".
    - "후반전고돌파": 마감 90분 전까지의 세션 고점을 후반 종가가 돌파 + 중반에
      고점 대비 -1.5% 이상 눌린 적 있음 — "후반에 회복하며 전고 돌파".
    - "후반매수파동": 마지막 60분 거래량이 그 이전 60분 평균의 2배 이상 +
      마지막 60분 수익률 +1% 이상 — "후반 급 매수세 파동".

    봉이 120개 미만이면 판정하지 않는다(반나절 데이터로 패턴을 지어내지 않는다).
    """
    if day is None or len(day) < 120:
        return []
    out: list[str] = []
    close = day["close"].astype(float)
    open_px = float(day.iloc[0]["open"])
    if open_px <= 0:
        return []
    day_high = float(day["high"].max())
    last = float(close.iloc[-1])

    # ── 초반강세지속 ────────────────────────────────────────────────────
    early = day.iloc[:EARLY_MINUTES]
    early_ret = (float(early["close"].iloc[-1]) / open_px - 1) * 100
    day_low = float(day["low"].min())
    strength = (last - day_low) / (day_high - day_low) if day_high > day_low else 0.0
    rise = day_high - open_px
    max_fade = (day_high - float(day.iloc[EARLY_MINUTES:]["low"].min())) if len(day) > EARLY_MINUTES else 0.0
    if early_ret >= 1.5 and strength >= 0.6 and (rise <= 0 or max_fade <= rise * 0.4):
        out.append("초반강세지속")

    # ── 후반전고돌파 ────────────────────────────────────────────────────
    pre, post = day.iloc[:-LATE_MINUTES], day.iloc[-LATE_MINUTES:]
    if len(pre) >= 30 and len(post) >= 10:
        pre_high = float(pre["high"].max())
        # 눌림은 **고점 도달 이후**의 하락이어야 한다 — 세션 저가 전체로 보면
        # 단조 상승(저가=시가)도 '눌렸다'로 오인한다(테스트가 잡은 결함).
        peak_pos = pre["high"].astype(float).idxmax()
        after_peak = pre.loc[peak_pos:]
        dipped = (len(after_peak) > 1
                  and float(after_peak["low"].min()) <= pre_high * (1 - 0.015))
        if dipped and last > pre_high:
            out.append("후반전고돌파")

    # ── 후반매수파동 ────────────────────────────────────────────────────
    tail = day.iloc[-SURGE_WINDOW:]
    base = day.iloc[:-SURGE_WINDOW]
    if len(base) >= SURGE_WINDOW:
        base_per_min = float(base["volume"].sum()) / len(base)
        tail_per_min = float(tail["volume"].sum()) / len(tail)
        tail_ret = (last / float(tail["close"].iloc[0]) - 1) * 100
        if base_per_min > 0 and tail_per_min >= base_per_min * 2 and tail_ret >= 1.0:
            out.append("후반매수파동")

    return out


def build_kr_session_wrap(
    bars_by_symbol: dict[str, pd.DataFrame],
    names: dict[str, str] | None = None,
    flow_summary: dict | None = None,
    max_per_pattern: int = 5,
) -> dict | None:
    """전일 KR 세션 종합 — 패턴별 종목 + 외인/기관 흐름.

    `bars_by_symbol`: {심볼: 그날 1분봉}. 없는 심볼은 없는 대로(지어내지 않는다).
    `flow_summary`: 호출부가 frgn_flow 원장에서 만든 요약(그대로 싣는다).
    입력이 전부 비면 None.
    """
    names = names or {}
    patterns: dict[str, list[dict]] = {}
    for symbol, day in sorted(bars_by_symbol.items()):
        for label in classify_session(day):
            entry = {"symbol": symbol, "name": names.get(symbol, symbol)}
            if day is not None and len(day):
                open_px = float(day.iloc[0]["open"])
                last = float(day["close"].iloc[-1])
                if open_px > 0:
                    entry["change_pct"] = round((last / open_px - 1) * 100, 2)
            patterns.setdefault(label, []).append(entry)
    for label in patterns:
        patterns[label] = sorted(
            patterns[label], key=lambda e: -(e.get("change_pct") or 0)
        )[:max_per_pattern]

    if not patterns and not flow_summary:
        return None
    out: dict = {"patterns": patterns}
    if flow_summary:
        out["flow"] = flow_summary
    # 다음날 아침 후보 유니버스 합류용 — 패턴에 걸린 심볼 전체(중복 제거).
    out["symbols"] = sorted({e["symbol"] for lst in patterns.values() for e in lst})
    return out


def flow_day_summary(rows: list[dict], day_iso: str, top: int = 5) -> dict | None:
    """frgn_flow 원장 행들 → 그날의 외인/기관 흐름 요약.

    `rows`: `[{date, symbol, foreign_net, inst_net}, ...]` (원장 스키마 그대로,
    로딩은 호출부). 그날 행이 없으면 None — 어제 흐름을 다른 날로 때우지 않는다.
    """
    todays = [r for r in rows if r.get("date") == day_iso]
    if not todays:
        return None
    f_total = sum(float(r.get("foreign_net") or 0) for r in todays)
    i_total = sum(float(r.get("inst_net") or 0) for r in todays)
    by_combined = sorted(
        todays, key=lambda r: float(r.get("foreign_net") or 0) + float(r.get("inst_net") or 0),
    )
    def _row(r):
        return {"symbol": r.get("symbol"),
                "foreign_net": r.get("foreign_net"), "inst_net": r.get("inst_net")}
    return {
        "date": day_iso,
        "n_symbols": len(todays),
        "foreign_net_total": f_total,
        "inst_net_total": i_total,
        "top_buy": [_row(r) for r in by_combined[-top:][::-1]],
        "top_sell": [_row(r) for r in by_combined[:top]],
    }
