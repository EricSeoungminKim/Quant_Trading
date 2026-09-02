"""전략 파라미터 제안자 — AI 트레이더 3단계 (2026-08-26 소유자 지시).

> "태그 소스 승격이랑 전략 파라미터 제안 같은 것들도 해야 하는 거 아닌가?"

**제안만 한다.** 이 모듈은 settings.yaml 을 읽지도 쓰지도 않는다 — 주간 재검토
숫자(원장)와 현재 파라미터 발췌를 받아 LLM 에게 "숫자가 가리키는 개선 가설"을
묻고, 파싱·검열한 제안을 돌려줄 뿐이다. 반영은 **사람이** settings.yaml 을
고치는 행위이고, 그 순간 기존 자동 판정 루프(experiments, 매일 16:30)가 변경
지문을 감지해 이중차분+순열검정으로 실측 판정한다 — 제안→반영→판정의 마지막
두 칸은 이미 있던 회사 인프라다.

LLM 정책(소유자, 2026-08-26): 논리가 중요한 작업이라 **Claude Code CLI(EC2)**
를 1순위로 쓰고, 실패하면 OpenRouter 무료 레인으로 폴백한다(호출부 cmd 참고).

검열(파싱 단계에서 강제 — LLM 이 뭐라 하든):
- 사이징·거버너 계열 파라미터(_FORBIDDEN)는 버린다 — 거버너 층 0.
- 등록에 없는 전략은 버린다(환각).
- 최대 3건 — 산탄총 제안은 다중검정 낭비다(제안이 많을수록 우연 통과가 는다).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROPOSALS_LEDGER = "data/ledger/param_proposals.jsonl"
MAX_PROPOSALS = 3

# 거버너 층 0 — 사이징·안전장치 파라미터는 LLM 제안 대상이 아니다. 부분 문자열
# 매칭(param 이름에 이 조각이 들어가면 버림).
_FORBIDDEN = (
    "capital_fraction", "sizing", "max_position", "max_order", "max_total",
    "daily_loss", "max_orders", "cooldown", "kill", "halt", "hard_cap",
)

_REQUIRED_FIELDS = ("strategy", "param", "current", "proposed", "rationale", "risk", "verify")


def build_prompt(review_text: str, params_yaml: str, strategies: list[str]) -> str:
    return (
        "당신은 자동매매 회사의 퀀트 리뷰어다. 아래는 이번 주 실측 원장 요약과 "
        "현재 전략 파라미터다. **주어진 숫자만 근거로** 파라미터 변경 가설을 "
        f"최대 {MAX_PROPOSALS}건 제안하라.\n\n"
        "규칙:\n"
        f"- 대상 전략은 이 목록뿐: {', '.join(sorted(strategies))}\n"
        "- **사이징·안전장치 파라미터 제안 금지**(capital_fraction, max_position 등 "
        "— 거버너 층 0, 사람 영역이다)\n"
        "- 표본이 부족한 전략(주간 거래 수가 적은)은 제안하지 말라 — 확신 없으면 "
        '빈 목록 {"proposals": []} 이 정답이다\n'
        "- verify 에는 \"어떤 숫자가 몇 주 안에 어떻게 되면 성공/철회\"를 적어라\n"
        "- 가능하면 samples(이 제안의 근거가 된 거래 건수, 정수)와 "
        "expected_improvement(기대 개선율, 0~1 사이 소수 — 예: 0.2 = 20% 개선)를 "
        "**정량적으로 추정 가능할 때만** 같이 채워라. 이 둘이 모두 있는 제안만 "
        "거버너 자동 반영 심사 대상이 된다(2026-09-02) — 확신 없으면 생략해도 "
        "되고, 그 경우 이 제안은 사람이 검토하는 제안으로만 남는다(정상 동작).\n\n"
        "반드시 JSON 하나만 출력한다: "
        '{"proposals": [{"strategy": "...", "param": "...", "current": ..., '
        '"proposed": ..., "rationale": "...", "risk": "...", "verify": "...", '
        '"samples": ..., "expected_improvement": ...}]}\n\n'
        f"[이번 주 실측]\n{review_text}\n\n[현재 파라미터]\n{params_yaml}"
    )


def parse_proposals(text: str | None, valid_strategies: set[str]) -> list[dict] | None:
    """LLM 응답 → 검열된 제안 목록. JSON 자체가 안 읽히면 None(결근),
    읽혔는데 유효 항목이 없으면 [] — "실패"와 "제안 없음"을 구분한다."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    props = data.get("proposals")
    if not isinstance(props, list):
        return None

    out: list[dict] = []
    for p in props:
        if not isinstance(p, dict):
            continue
        if any(p.get(k) in (None, "") for k in _REQUIRED_FIELDS):
            continue
        if str(p["strategy"]) not in valid_strategies:
            continue  # 환각 전략
        param = str(p["param"])
        if any(f in param for f in _FORBIDDEN):
            continue  # 거버너 층 0
        out.append({k: p[k] for k in _REQUIRED_FIELDS})
        if len(out) >= MAX_PROPOSALS:
            break
    return out


def render_note(proposals: list[dict]) -> str | None:
    if not proposals:
        return None
    lines = ["🧪 전략 파라미터 제안 (AI 리뷰 — 주간)"]
    for p in proposals:
        lines.append(
            f"  [{p['strategy']}] {p['param']}: {p['current']} → {p['proposed']}\n"
            f"    근거: {p['rationale']}\n    리스크: {p['risk']}\n    검증: {p['verify']}"
        )
    lines.append(
        "※ 자동 반영되지 않는다 — 사람이 settings.yaml 을 바꾸면 매일 16:30 "
        "자동 판정 루프가 변경 지문을 감지해 이중차분+순열검정으로 실측 판정한다."
    )
    return "\n".join(lines)


def propose(review_text: str, params_yaml: str, valid_strategies: set[str],
            narrate) -> dict | None:
    """한 번의 제안 사이클. LLM 실패/파싱 불가면 None(결근), 유효 제안 0건도
    None(침묵 — 무제안은 알릴 일이 아니다)."""
    raw = narrate(build_prompt(review_text, params_yaml, sorted(valid_strategies)))
    props = parse_proposals(raw, valid_strategies)
    if not props:
        if props is None:
            logger.warning("param-proposer: LLM 실패/파싱 불가 — 이번 주 결근")
        return None
    return {"proposals": props, "note": render_note(props), "raw": raw}


def append_proposals(proposals: list[dict], path: Path | str, week: str) -> int:
    """(week, strategy, param) 멱등 적재 — 같은 주의 같은 제안은 한 번만.
    다음 주에 같은 제안이 또 나오는 것은 새 표본이다(숫자가 계속 가리키는 방향)."""
    p = Path(path)
    existing: set[tuple] = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            existing.add((r.get("week"), r.get("strategy"), r.get("param")))
    p.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    with p.open("a", encoding="utf-8") as f:
        for prop in proposals:
            key = (week, prop.get("strategy"), prop.get("param"))
            if key in existing:
                continue
            existing.add(key)
            f.write(json.dumps({**prop, "week": week, "recorded_at": now},
                               ensure_ascii=False) + "\n")
            added += 1
    return added
