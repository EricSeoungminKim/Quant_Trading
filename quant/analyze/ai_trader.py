"""신입사원 AI 트레이더 — 수습 단계 (2026-08-26 소유자 지시).

> "우리 회사는 다양한 직군을 하나로 합쳐 운영하는 회사. 여기에 신입 사원
> AI 트레이더가 들어왔다고 가정하자. 기존 회사의 아이덴티티는 무너지지 않되,
> 이 새로운 직원이 우리에게 득이 되도록."

## 회사 조직도에서 이 직원의 자리

TradingAgents(github.com/tauricresearch/tradingagents, 멀티에이전트 협의 매매)의
역할 분담 아이디어를 **우리 회사의 기존 인사평가 체계 안에** 넣는다. 새 평가
인프라는 0줄이다 — 판단 귀속 층(`quant/control/judgment.py`)의 docstring 이
이미 이 자리를 비워 놓았다: "LLM 이 이 통로로만 들어오게 하는 것이 설계
의도다. 실현 수익으로 채점해 이겨야만 승격한다."

- **읽는 서류**: 그날 selections 원장의 속성 벡터 — 기존 직원(watch_scorer)이
  본 것과 **동일한 입력**이다. `input_hash` 가 일치해야 리더보드가 "같은
  입력을 본 판단끼리" 비교한다는 전제가 성립한다(이 모듈의 제1 계약).
- **답안지는 안 보여준다**: 기존 직원의 점수(baseline/ai/trending)와 합격
  여부(is_candidate)는 프롬프트에서 뺀다 — 베끼면 독립 판단이 아니고,
  비교가 무의미해진다. (해시는 행 전체 속성으로 계산하므로 무엇을 프롬프트에
  보여주든 해시 계약은 불변이다.)
- **업무 방식**: 3역할 토론 — 애널리스트(강세론) → 리스크 매니저(반박) →
  트레이더(최종 verdict/score/논지). 각 단계는 엄격한 JSON 만 인정한다.
- **성적표**: judgments 원장(producer="ai_trader") → outcomes 가 D+1/5/20
  전방 수익률을 채우고 → 리더보드가 일별 rank IC + 다중검정 보정 승격 판정을
  낸다. 매일 16:20 장마감 리포트에 자동 표출 — 추가 배선 없음.
- **권한**: 없음. 주문·워치리스트·엔진에 닿지 않는다(분석 평면 — 아키텍처
  테스트가 trade 임포트를 막는다). 승격은 리더보드 promote 판정 + **사람**이
  결정한다(거버너 층 0 불변).
- **결근 처리**: LLM 이 죽거나 헛소리를 하면 그날 판단 자체가 없다. 지어낸
  판단이 원장에 들어가는 것보다 결근이 낫다 — 어차피 리더보드는 판단이 있는
  날만 센다.

## 가중치 내 look-ahead 를 피하는 이유

백테스트가 아니라 **미래 수익률로만** 채점한다 — 판단 시점 이후의 가격으로
채점하므로 모델이 과거 결말을 기억하고 있어도 유리할 수 없다(수습 설계의 핵심,
docs/vault 참고자료의 Look-Ahead-Bench 논의).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from quant.control.judgment import selection_attributes
from quant.core.models import Judgment, input_hash

logger = logging.getLogger(__name__)

PRODUCER = "ai_trader"
PRODUCER_VERSION = "1"
# 픽 상한 — 신입은 소수 정예로만 목소리를 낸다. 상한 초과분은 점수 낮은 쪽부터
# reject 강등(결정론 규칙 — LLM 이 상한을 안 지켜도 여기서 지켜진다).
MAX_PICKS = 5

DEBATE_LEDGER = "data/ledger/ai_trader.jsonl"

# 프롬프트에서 숨기는 열 — 기존 직원의 답안지(점수·합격 여부). ②번 계약.
_HIDDEN = frozenset({
    "baseline_score100", "ai_score100", "trending_score100", "trending_label",
    "is_candidate",
})


# ------------------------------------------------------------------ 서류(dossier)

def dossier_lines(rows: list[dict]) -> list[str]:
    """선정 원장 행 → 신입이 읽을 종목별 한 줄 서류(원자료만, 답안지 제외).

    없는 축은 표기하지 않는다(0 으로 위장 금지 — selections 원장과 같은 원칙)."""
    out = []
    for r in rows:
        a = selection_attributes(r)
        parts = [f"{r.get('symbol')} {r.get('name') or ''}".strip()]
        if a.get("close") is not None:
            chg = a.get("change_pct")
            parts.append(f"종가 {a['close']:,.0f}" + (f" ({chg:+.1f}%)" if chg is not None else ""))
        if a.get("news_articles_today") is not None:
            streak = a.get("news_streak_days")
            parts.append(f"뉴스 {a['news_articles_today']}건" + (f"·{streak}일 연속" if streak else ""))
        if a.get("foreign_buy_streak"):
            parts.append(f"외인 {a['foreign_buy_streak']}일 순매수")
        if a.get("inst_buy_streak"):
            parts.append(f"기관 {a['inst_buy_streak']}일 순매수")
        if a.get("best_board_rank") is not None:
            parts.append(f"거래대금 보드 {a['best_board_rank']}위({a.get('n_boards')}개 보드)")
        if a.get("relative_volume") is not None:
            parts.append(f"상대 거래량 {a['relative_volume']:.1f}x")
        if a.get("analyst_opinion_score") is not None:
            parts.append(f"컨센서스 {a['analyst_opinion_score']:.1f}")
        if a.get("upside_pct") is not None:
            parts.append(f"목표가 괴리 {a['upside_pct']:+.1f}%")
        if a.get("origin"):
            parts.append(f"출처 {a['origin']}")
        out.append(" | ".join(parts))
    return out


# ------------------------------------------------------------------ 파싱(환각 차단)

def parse_stage_json(text: str | None, allowed: set[str]) -> list[dict] | None:
    """LLM 응답 → 픽 목록. 엄격 모드 — 실패는 None(결근)이지 빈 목록이 아니다.

    - 첫 `{...}` JSON 블록만 인정, `picks` 리스트 필수.
    - **서류에 없는 심볼은 버린다**(환각 차단 — 신입은 회사가 조사한 종목
      안에서만 발언한다. 유니버스 밖 티커는 채점 기준가도 없다).
    - score 는 0~100 클램프, verdict 는 pass/reject 외엔 reject.
    - pass 가 MAX_PICKS 를 넘으면 점수 낮은 쪽부터 reject 강등.
    """
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    picks = data.get("picks")
    if not isinstance(picks, list):
        return None

    out: list[dict] = []
    for p in picks:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip()
        if sym not in allowed:
            continue
        try:
            score = max(0.0, min(100.0, float(p.get("score"))))
        except (TypeError, ValueError):
            continue
        verdict = "pass" if str(p.get("verdict")).strip().lower() == "pass" else "reject"
        out.append({
            "symbol": sym, "score": score, "verdict": verdict,
            "thesis": str(p.get("thesis") or "").strip()[:200],
        })

    passes = sorted((p for p in out if p["verdict"] == "pass"),
                    key=lambda p: p["score"], reverse=True)
    for p in passes[MAX_PICKS:]:
        p["verdict"] = "reject"
    return out


# ------------------------------------------------------------------ 3역할 토론

# 토론 대상 숏리스트 상한 — 실측(2026-08-26 EC2 첫 출근): 서류 81종목에 전
# 종목 verdict 를 요구하니 출력 상한 안에서 완주하지 못해 결근했다. 실무
# 워크플로우대로 애널리스트가 주목 종목만 추리고, 숏리스트 밖 행은 결정론
# 규칙(to_judgments)이 reject 로 기록한다 — 전 행 기록 계약은 불변.
MAX_DISCUSS = 15

_ROLE_COMMON = (
    "당신은 한국/미국 주식 자동매매 회사의 수습 트레이딩 팀이다. 아래 서류의 "
    "종목만 다룬다 — 서류에 없는 종목·외부 지식의 미래 가격 언급 금지. "
    "반드시 JSON 하나만 출력한다: "
    '{"picks": [{"symbol": "...", "score": 0-100, "verdict": "pass|reject", '
    '"thesis": "한 문장"}]}. thesis 는 짧게.\n\n[서류]\n'
)


def _analyst_prompt(lines: list[str]) -> str:
    return (
        _ROLE_COMMON + "\n".join(lines)
        + f"\n\n[역할: 애널리스트] 주목할 가치가 있는 종목 **최대 {MAX_DISCUSS}개만** "
          "골라 항목을 내라 — 뉴스 흐름·수급(외인/기관)·거래대금 쏠림이 겹치는 "
          "종목에 높은 점수를. 나머지 종목은 항목을 내지 않는다(자동 탈락 처리). "
          "thesis 에 가장 강한 근거 하나."
    )


def _risk_prompt(lines: list[str], analyst_json: str) -> str:
    return (
        _ROLE_COMMON + "\n".join(lines)
        + "\n\n[애널리스트의 초안]\n" + analyst_json
        + "\n\n[역할: 리스크 매니저] **초안에 있는 종목만** 다뤄 반박하라 — 이미 "
          "급등해 추격이 되는 자리, 뉴스만 있고 수급이 비는 종목, 하루짜리 "
          "이벤트성 재료를 감점하고 verdict 를 낮춰라. thesis 에 가장 큰 위험 하나."
    )


def _trader_prompt(lines: list[str], analyst_json: str, risk_json: str) -> str:
    return (
        _ROLE_COMMON + "\n".join(lines)
        + "\n\n[애널리스트]\n" + analyst_json
        + "\n\n[리스크 매니저]\n" + risk_json
        + f"\n\n[역할: 트레이더] 두 의견을 종합해 **초안에 있는 종목만** 최종 "
          f"판단하라. pass 는 최대 {MAX_PICKS}개 — 강세 논거와 위험을 저울질해 "
          "확신 있는 것만. thesis 는 판단 근거 한 문장(강세론과 위험을 모두 반영)."
    )


def run_debate(rows: list[dict], narrate) -> dict | None:
    """3역할 토론 실행. 반환 {"final": picks, "transcript": [(role, raw)...]}.

    어느 단계든 실패(None/파싱 불가)하면 None — 결근. `narrate` 는
    `quant.core.ports.Narrator.narrate` 시그니처(prompt -> str | None)."""
    if not rows:
        return None
    lines = dossier_lines(rows)
    allowed = {str(r.get("symbol")) for r in rows}
    transcript: list[tuple[str, str]] = []

    raw_analyst = narrate(_analyst_prompt(lines))
    analyst = parse_stage_json(raw_analyst, allowed)
    if analyst is None:
        logger.warning("ai_trader: 애널리스트 단계 실패 — 오늘 결근")
        return None
    transcript.append(("analyst", raw_analyst))

    raw_risk = narrate(_risk_prompt(lines, json.dumps({"picks": analyst}, ensure_ascii=False)))
    risk = parse_stage_json(raw_risk, allowed)
    if risk is None:
        logger.warning("ai_trader: 리스크 단계 실패 — 오늘 결근")
        return None
    transcript.append(("risk", raw_risk))

    raw_final = narrate(_trader_prompt(
        lines, json.dumps({"picks": analyst}, ensure_ascii=False),
        json.dumps({"picks": risk}, ensure_ascii=False)))
    final = parse_stage_json(raw_final, allowed)
    if final is None:
        logger.warning("ai_trader: 트레이더 단계 실패 — 오늘 결근")
        return None
    transcript.append(("trader", raw_final))

    return {"final": final, "transcript": transcript}


# ------------------------------------------------------------------ 판단 귀속

def to_judgments(final: list[dict], rows: list[dict], version: str = PRODUCER_VERSION) -> list[Judgment]:
    """최종 픽 → Judgment 목록. **전 행**을 남긴다 — 픽에 없는 행은 reject
    (score None). 떨어뜨린 종목도 판단이다(selection_judgment 와 같은 원칙 —
    전 행이 있어야 일별 rank IC 가 성립한다).

    input_hash 는 selection_judgment 와 **동일한 산식**(selection_attributes →
    input_hash)이어야 한다 — 리더보드의 "같은 입력을 본 판단끼리 비교" 전제."""
    by_symbol = {p["symbol"]: p for p in final}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[Judgment] = []
    for row in rows:
        attrs = selection_attributes(row)
        p = by_symbol.get(str(row.get("symbol")))
        out.append(Judgment(
            producer=PRODUCER,
            producer_version=str(version),
            input_hash=input_hash(attrs),
            market=str(row.get("market") or ""),
            symbol=str(row.get("symbol") or ""),
            session_date=str(row.get("date") or ""),
            score=p["score"] if p else None,
            verdict=p["verdict"] if p else "reject",
            rationale=(p["thesis"] if p else "토론 결과에 항목 없음")[:255],
            ts=now,
        ))
    return out


def append_judgments(judgments: list[Judgment], path: Path | str) -> int:
    """judgments.jsonl 멱등 append — cmd_outcomes 의 natural_key 중복 방지와
    동일한 규칙(같은 판단은 하나다)."""
    p = Path(path)
    existing: set[tuple] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            existing.add((r.get("producer"), r.get("producer_version"),
                          r.get("input_hash"), r.get("symbol"), r.get("session_date")))
    p.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with p.open("a", encoding="utf-8") as f:
        for j in judgments:
            if j.natural_key() in existing:
                continue
            existing.add(j.natural_key())
            f.write(json.dumps(j.__dict__, ensure_ascii=False) + "\n")
            added += 1
    return added


# ------------------------------------------------------------------ 텔레그램 카드

def daily_note(final: list[dict], market: str, names: dict[str, str]) -> str | None:
    """pass 픽만 담은 짧은 카드. 픽이 없으면 None(침묵 — 결근과 "픽 없음" 모두
    조용하다. 신입의 발언권은 픽이 있을 때만이다)."""
    picks = sorted((p for p in final if p["verdict"] == "pass"),
                   key=lambda p: p["score"], reverse=True)
    if not picks:
        return None
    flag = "🇰🇷" if market == "KR" else "🇺🇸"
    lines = [f"🧑‍💼 AI 트레이더(수습) 오늘의 픽 {flag}"]
    for p in picks:
        name = names.get(p["symbol"]) or p["symbol"]
        lines.append(f"  {name} ({p['symbol']}) {p['score']:.0f}점 — {p['thesis']}")
    lines.append("※ 제안일 뿐 주문하지 않는다 — 성적은 매일 16:20 장마감 리포트의 "
                 "리더보드가 매긴다(같은 서류를 본 watch_scorer 와 경쟁).")
    return "\n".join(lines)
