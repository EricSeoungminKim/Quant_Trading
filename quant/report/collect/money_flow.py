""""돈의 흐름" 섹션 수집 — `quant.analyze.money_flow`(순수 판정)을 실제 원장
(`data/ledger/macro_rates.jsonl`) + 스냅샷 지수 시세로 채운다 (2026-08-31
소유자 지시: "유가·금리·원자재·지수의 숫자 흐름으로 큰손들의 돈이 어디로
쏠릴지 읽어서 리포트와 종목·섹터 선정에 녹여라").

LLM 산문은 기존 narrator 레인(`quant.report.collect.news._build_stance_prose`와
같은 관례)을 재사용하고, 실패/결측이면 결정론 문장
(`quant.analyze.money_flow.format_money_flow_text`)으로 폴백한다 — 이
리포트 파이프라인의 무자격증명 안전망 관례(us_kr_bridge/index_outlook과 동일).
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.adapters.macro.fred import DEFAULT_LEDGER_PATH
from quant.analyze.money_flow import analyze_money_flow, format_money_flow_text


def _ok(snap, key: str) -> dict | None:
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def _equity_change(snap) -> tuple[float | None, str]:
    """시장 대표 지수의 당일 등락률 — `snap.results["market"].data["quotes"]`에
    이미 있다(`quant.report.collect.index_outlook._pct`와 같은 소스, 추가 조회
    없음). KR=KOSPI(^KS11), US=NASDAQ(^IXIC)."""
    quotes = (_ok(snap, "market") or {}).get("quotes", {})
    symbol, label = ("^KS11", "KOSPI") if snap.market == "KR" else ("^IXIC", "NASDAQ")
    return (quotes.get(symbol) or {}).get("change_pct"), label


def _build_money_flow_prose(result: dict, narrator=None) -> str | None:
    """LLM 2~3문장 해석(스펙 §3) — 프롬프트가 숫자 인용을 명시적으로 요구한다.
    narrator 없음/실패면 `None` — 호출부는 `format_money_flow_text`(결정론
    문장)로 완전하다(`_build_stance_prose`와 같은 무LLM 폴백 관례)."""
    try:
        from quant.adapters.narrate import make_narrator

        flow, cash = result["flow"], result["cash"]
        lines = [
            "다음은 오늘 매크로 지표(금리·유가·달러·VIX)로 판정한 자금 흐름이다.",
            "숫자를 반드시 인용해 2~3문장으로 서술하라(증권사 리서치 캡션처럼",
            "간결하게). 새로운 판단(매수/매도 지시, 점수 변경)을 내리지 말고",
            "이미 나온 판정을 뒷받침·설명만 하라. 반드시 한국어로만 답하라.",
            "",
            f"자금 흐름 판정: {flow['label']}" + (f" ({'; '.join(flow['reasons'])})" if flow.get("reasons") else ""),
            f"현금 체력 판정: {cash['label']}" + (f" ({'; '.join(cash['reasons'])})" if cash.get("reasons") else ""),
            "",
            "다음 형식으로 정확히 한 문단만 답하라(다른 텍스트 없이):",
            "돈의 흐름: <문단>",
        ]
        text = (narrator or make_narrator()).narrate("\n".join(lines))
        if not text:
            return None
        text = text.strip()
        if text.startswith("돈의 흐름"):
            text = text.split(":", 1)[-1].strip() if ":" in text else text
        return text or None
    except Exception as e:  # noqa: BLE001 — 돈의 흐름 AI 서술 실패가 리포트를 막지 않는다
        print(f"돈의 흐름 AI 서술 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def build_money_flow_view(snap, root: Path, narrator=None) -> dict | None:
    """리포트 payload/모델에 얹을 `money_flow` 뷰.

    원장(`data/ledger/macro_rates.jsonl`)이 아직 없거나 시리즈가 하나도 없으면
    `None` — 섹션 자체를 생략한다(`us_kr_bridge`와 같은 관례: 있는 걸 없다고
    하지 않지만 없는 걸 지어내지도 않는다).

    반환 딕셔너리의 `sector_tilt`는 **이 리포트의 시장**(KR/US) 몫만 남긴다 —
    독자가 지금 보는 시장과 무관한 섹터 표를 보여주지 않는다. 전체(KR+US) 는
    `quant.analyze.money_flow.analyze_money_flow`를 직접 쓰는 소비자(§4
    watch_scorer 등) 몫이다.
    """
    ledger_path = root / DEFAULT_LEDGER_PATH
    equity_pct, equity_label = _equity_change(snap)
    try:
        result = analyze_money_flow(
            ledger_path, equity_change_pct=equity_pct, equity_label=equity_label,
        )
    except Exception as e:  # noqa: BLE001 — 판정 실패가 리포트를 막지 않는다
        print(f"돈의 흐름 판정 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if not result["series"]:
        return None

    series_view = {
        name: {
            "label": s.label, "date": s.date, "value": s.value,
            "chg_1d": s.chg_1d, "chg_5d": s.chg_5d, "chg_20d": s.chg_20d,
            "direction_5d": s.direction_5d,
        }
        for name, s in result["series"].items()
    }
    return {
        "series": series_view,
        "flow": result["flow"],
        "cash": result["cash"],
        "sector_tilt": result["sector_tilt"].get(snap.market, {}),
        "prose": _build_money_flow_prose(result, narrator=narrator),
        "fallback_text": format_money_flow_text(result),
    }
