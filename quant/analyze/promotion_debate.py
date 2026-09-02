"""승격 토론(Bull/Bear Debate) — 관심종목 자동 편입 직후 2차 심사, 리포팅
레이어(2026-09-02, 소유자 지시 "회사형 AI 에이전트 레이어" 레인 1).

## 왜

own_brief.sh 의 확신도 게이트(watch-score)는 결정론 규칙 하나가 통과/탈락을
가른다. 규칙이 놓칠 수 있는 "그럴듯하지만 위험한" 편입(고거래량 추격, 테마
과열, 비용 대비 엣지 부족)을 Bull(찬성)/Bear(반대) 두 페르소나가 겨루게 하고,
심판(Judge) 역할이 "유지/보류"를 판정해 **기록만** 한다.

## 핵심 계약 (quant/analyze/ai_trader.py 와 동일한 원칙을 재사용)

- **관심종목을 바꾸지 않는다.** "보류" 판정도 실제 편입을 취소하지 않는다 —
  `data/ledger/debate.jsonl` 에 기록하고 텔레그램(notify_auto)으로 알릴 뿐이다.
  한 달 뒤 "보류 판정 종목의 실제 성과"로 이 에이전트 자체를 채점하는 게
  목적이다 — 판단 귀속 층(`quant/control/judgment.py`)이 이미 열어 둔 자리와
  같은 설계다: "실현 수익으로 채점해 이겨야만 승격한다."
- **전략 코드를 보여주지 않는다** — 프롬프트에 싣는 것은 watch_scorer 의 채점
  근거(점수 항목·사유)뿐이다. 전략의 진입/청산 로직은 프롬프트 밖이다.
- **환각 차단** — 서류(오늘 통과 종목)에 없는 심볼의 판정은 버린다.
- **결근 처리** — 어느 단계든 LLM 실패/파싱 불가면 그날 판단이 없다(오늘
  결근) — 지어낸 판단이 원장에 들어가는 것보다 결근이 낫다.

`quant/analyze/` 소속 — 순수 함수만, 파일/네트워크는 호출부(`quant.apps.cli`)
가 맡는다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

PRODUCER = "promotion_debate"
DEBATE_LEDGER = "data/ledger/debate.jsonl"

_VALID_VERDICTS = {"유지", "보류"}


def dossier_lines(items: list[dict]) -> list[str]:
    """`quant.analyze.watch_scorer.ScoreResult`형 dict 목록 → 종목별 한 줄 서류.

    **전략 코드는 싣지 않는다** — watch_scorer 가 이미 계산한 채점 근거(점수
    항목별 득점, 임계값, 게이트/수급 사유)만 보여준다. 증거 항목("(+N)")이
    아닌 사유(프리퍼시티 등)만 별도로 요약한다."""
    out: list[str] = []
    for it in items:
        symbol = it.get("symbol")
        score = it.get("score")
        threshold = it.get("eff_threshold")
        profile = it.get("profile") or "무태그"
        parts = [f"{symbol} [{profile}] 총점 {score}/100 (임계 {threshold})"]
        for name, earned, mx, detail in (it.get("breakdown") or []):
            mx_str = f"/{mx}" if mx else ""
            parts.append(f"{name} {earned}{mx_str}({detail})")
        reasons = [
            str(r) for r in (it.get("reasons") or [])
            if not re.search(r"\(\+\d+\)($|;)", str(r))
        ]
        if reasons:
            parts.append("근거: " + "; ".join(reasons[:3]))
        out.append(" | ".join(parts))
    return out


_ROLE_COMMON = (
    "당신은 한국/미국 주식 자동매매 회사의 리스크 심사팀이다. 오늘 확신도 "
    "엔진(결정론 채점)이 이미 통과시켜 관심종목에 편입한 종목만 다룬다 — "
    "서류에 없는 종목은 절대 언급하지 않는다. 반드시 JSON 하나만 출력한다: "
    '{{"verdicts": [{{"symbol": "...", "verdict": "유지|보류", '
    '"reason": "한 문장"}}]}}. reason 은 짧게.\n\n'
    "[오늘 리포트 요약]\n{report}\n\n[서류: 오늘 통과 종목의 채점 근거]\n"
)


def _bull_prompt(lines: list[str], report_summary: str) -> str:
    return (
        _ROLE_COMMON.format(report=report_summary or "(요약 없음)") + "\n".join(lines)
        + "\n\n[역할: Bull] 편입에 찬성하는 최강 논거를 각 종목에 대라 — 점수 "
          "구성·리포트 재료가 실제로 왜 근거 있는지 설명한다. 이 단계는 찬성 "
          "논거 제시가 목적이므로 전 종목 verdict=\"유지\"로 낸다."
    )


def _bear_prompt(lines: list[str], report_summary: str, bull_json: str) -> str:
    return (
        _ROLE_COMMON.format(report=report_summary or "(요약 없음)") + "\n".join(lines)
        + "\n\n[Bull 초안]\n" + bull_json
        + "\n\n[역할: Bear] **초안에 있는 종목만** 반박하라 — 이미 급등해 "
          "추격이 되는 자리, 고거래량 쏠림(눌림 없는 단기 과열), 테마성 "
          "재료의 지속성 부족, 왕복 비용(수수료+세금) 대비 엣지 부족을 "
          "지적하고, 위험이 크다고 판단되는 종목은 verdict 를 \"보류\"로 "
          "낮춰라. 근거가 약하면 그대로 \"유지\"를 유지해도 된다."
    )


def _judge_prompt(lines: list[str], report_summary: str, bull_json: str, bear_json: str) -> str:
    return (
        _ROLE_COMMON.format(report=report_summary or "(요약 없음)") + "\n".join(lines)
        + "\n\n[Bull]\n" + bull_json
        + "\n\n[Bear]\n" + bear_json
        + "\n\n[역할: 심판] 두 의견을 저울질해 **초안에 있는 종목만** 최종 "
          "\"유지\"|\"보류\" 판정을 내라. reason 은 판정 근거를 한 문장으로."
    )


def parse_verdicts(text: str | None, allowed: set[str]) -> list[dict] | None:
    """LLM 응답 → 판정 목록. 엄격 모드 — 실패는 None(결근)이지 빈 목록이 아니다.

    - 첫 `{...}` JSON 블록만 인정, `verdicts` 리스트 필수.
    - **서류에 없는 심볼은 버린다**(환각 차단).
    - verdict 가 "유지"/"보류" 밖이면 안전측인 "유지"로 낮춘다 — 파싱 실패로
      멀쩡한 편입을 함부로 보류 처리하지 않는다.
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
    verdicts = data.get("verdicts")
    if not isinstance(verdicts, list):
        return None

    out: list[dict] = []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        sym = str(v.get("symbol") or "").strip()
        if sym not in allowed:
            continue
        verdict = str(v.get("verdict") or "").strip()
        if verdict not in _VALID_VERDICTS:
            verdict = "유지"
        out.append({
            "symbol": sym, "verdict": verdict,
            "reason": str(v.get("reason") or "").strip()[:200],
        })
    return out


def run_debate(items: list[dict], report_summary: str, narrate) -> dict | None:
    """Bull → Bear → Judge 3단 토론 실행. 반환 `{"final": [...], "transcript": [...]}`.

    어느 단계든 실패(None/파싱 불가)하면 None — 결근. `narrate` 는
    `quant.core.ports.Narrator.narrate` 시그니처(prompt -> str | None)."""
    if not items:
        return None
    lines = dossier_lines(items)
    allowed = {str(it.get("symbol")) for it in items}
    transcript: list[tuple[str, str]] = []

    raw_bull = narrate(_bull_prompt(lines, report_summary))
    bull = parse_verdicts(raw_bull, allowed)
    if bull is None:
        return None
    transcript.append(("bull", raw_bull))

    bull_json = json.dumps({"verdicts": bull}, ensure_ascii=False)
    raw_bear = narrate(_bear_prompt(lines, report_summary, bull_json))
    bear = parse_verdicts(raw_bear, allowed)
    if bear is None:
        return None
    transcript.append(("bear", raw_bear))

    bear_json = json.dumps({"verdicts": bear}, ensure_ascii=False)
    raw_judge = narrate(_judge_prompt(lines, report_summary, bull_json, bear_json))
    judge = parse_verdicts(raw_judge, allowed)
    if judge is None:
        return None
    transcript.append(("judge", raw_judge))

    return {"final": judge, "transcript": transcript}


def to_records(final: list[dict], items: list[dict], market: str, date_str: str) -> list[dict]:
    """최종 판정 → 원장 행. **서류의 전 종목**을 남긴다(판정이 빠진 종목도
    "유지"로 안전하게 기록 — ai_trader.to_judgments 와 같은 "전 행 기록" 원칙)."""
    by_symbol = {v["symbol"]: v for v in final}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: list[dict] = []
    for it in items:
        sym = str(it.get("symbol") or "")
        v = by_symbol.get(sym)
        out.append({
            "producer": PRODUCER, "date": date_str, "market": market, "symbol": sym,
            "score": it.get("score"), "verdict": v["verdict"] if v else "유지",
            "reason": (v["reason"] if v else "토론 결과에 항목 없음 — 기본 유지"),
            "ts": now,
        })
    return out


def append_ledger(records: list[dict], path: Path | str) -> int:
    """`debate.jsonl` 멱등 append — 같은 (date, market, symbol) 은 다시 쓰지 않는다."""
    p = Path(path)
    existing: set[tuple] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            existing.add((r.get("date"), r.get("market"), r.get("symbol")))
    p.parent.mkdir(parents=True, exist_ok=True)
    added = 0
    with p.open("a", encoding="utf-8") as f:
        for r in records:
            key = (r.get("date"), r.get("market"), r.get("symbol"))
            if key in existing:
                continue
            existing.add(key)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            added += 1
    return added


def notify_text(records: list[dict], market: str, names: dict[str, str] | None = None) -> str | None:
    """텔레그램 카드. 기록이 없으면 None(침묵)."""
    if not records:
        return None
    names = names or {}
    flag = "🇰🇷" if market == "KR" else "🇺🇸"
    lines = [f"⚖️ 승격 토론(Bull/Bear) {flag}"]
    for r in records:
        name = names.get(r["symbol"]) or r["symbol"]
        mark = "✅ 유지" if r["verdict"] == "유지" else "⏸ 보류"
        lines.append(f"  {name} ({r['symbol']}) {mark} — {r['reason']}")
    if any(r["verdict"] == "보류" for r in records):
        lines.append("※ 보류는 관심종목에서 실제로 제거하지 않는다 — 기록만, "
                     "한 달 뒤 실제 성과로 이 판정 자체를 채점한다.")
    return "\n".join(lines)
