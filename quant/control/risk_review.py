"""독립 리스크 리뷰 — 트레이딩/보고 라인 분리, 리포팅 레이어(2026-09-02,
소유자 지시 "회사형 AI 에이전트 레이어" 레인 2).

## 왜 `ops_judge.py`와 다른 스크립트/프롬프트인가

`quant.control.ops_judge`는 "서로 다른 데이터 소스가 서로 앞뒤가 맞는가"를
본다(리포트 등락률 vs 실제 봉, 원장 체결 vs 세션 요약 등) — 운영 정합성
감시다. 이 모듈은 그것과 **역할이 다르다**: 드로다운·집중도·상쇄쌍 노출·연속
손실만 전담해서 보는 리스크 리뷰다. 회사 조직에서 "트레이더가 자기 손익을
자기가 감사하지 않는다"는 원칙(리스크 관리 라인과 매매 라인의 분리)을 이
저장소식으로 구현한 것 — `ops_judge`가 이미 쓰는 도구 호출 루프를 재사용하지
않고, 별도 프롬프트·별도 스크립트로 완전히 분리한다.

## 판정은 결정론, 서술만 LLM

**임계 초과 여부(threshold_breach)는 이 모듈의 순수 함수가 결정한다** — LLM
에게 "위험한가?"를 판정시키지 않는다(이 저장소 전역 원칙: "숫자가 자본을
배분한다" — 판정은 코드가, LLM 은 설명만). LLM 은 "리스크 관점 상위 3문제 +
권고"를 산문으로 정리하는 역할만 맡는다. LLM 이 죽거나 헛소리를 해도
`threshold_breach`와 `flags`는 항상 유효하다 — 서술이 없으면 카드가 짧아질
뿐 판정 자체는 흔들리지 않는다.

## 입력

이 모듈 자신은 파일도 네트워크도 만지지 않는다(`quant.control.ops_judge`와
같은 계약) — 호출부(`quant.apps.cli`)가 이미 읽은 원장/포트폴리오/노출 리포트를
넘긴다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PRODUCER = "risk_review"
RISK_LEDGER = "data/ledger/risk_review.jsonl"

# 연속 손실 임계 — 이 이상이면 "우연이 아니라 전략이 지고 있다"는 신호로
# 본다(capital_review.sh 의 강등 판정과는 별개 — 여기는 관찰·알림만, 자동
# 강등은 이미 별도 배선이 있다).
LOSS_STREAK_THRESHOLD = 5

# 단일 종목 집중도 임계 — "보유 중인 명목 합계 대비 이 종목 비중"(총자본
# 대비가 아니다. capital_krw 는 daily_wrap 도 아직 배선 전이라 항상 모른다 —
# 없는 정밀도를 지어내지 않는다. exposure.py 모듈 docstring 참고).
CONCENTRATION_THRESHOLD = 0.30


@dataclass
class RiskData:
    """호출부가 이미 읽어둔 스냅샷 — 이 모듈 자신은 파일/네트워크를 만지지 않는다.

    - `scoreboard_text`: `quant.control.ledger.scoreboard_text()` 출력(누적 승률·payoff).
    - `exposure`: `quant.control.exposure.build_report(...).to_dict()` 출력 또는 `None`.
    - `trips`: `quant.control.ledger.round_trips()` 출력(연속 손실 계산용).
    - `portfolio_cash`: `data/state/portfolio.json` 의 현금(KRW) 또는 `None`.
    """

    scoreboard_text: str = ""
    exposure: dict | None = None
    trips: list[dict] = field(default_factory=list)
    portfolio_cash: float | None = None


def strategy_consecutive_losses(trips: list[dict]) -> dict[str, int]:
    """전략별 **가장 최근** 연속 손실 트립 수. `exit_ts` 오름차순으로 정렬해
    끝에서부터 손실(pnl<=0, 손익 확정분만)이 이어지는 길이를 센다.

    승리를 만나면 그 즉시 멈춘다 — "지금 지고 있는 연속"만 본다(과거 어딘가의
    연속 손실은 이미 회복됐으므로 지금의 리스크가 아니다)."""
    by_strategy: dict[str, list[dict]] = {}
    for t in trips:
        if not t.get("pnl_known"):
            continue
        by_strategy.setdefault(str(t.get("strategy", "?")), []).append(t)

    out: dict[str, int] = {}
    for strategy, ts in by_strategy.items():
        ordered = sorted(ts, key=lambda x: str(x.get("exit_ts", "")))
        streak = 0
        for t in reversed(ordered):
            if float(t.get("pnl", 0.0)) <= 0:
                streak += 1
            else:
                break
        out[strategy] = streak
    return out


def deterministic_flags(
    exposure: dict | None,
    consecutive: dict[str, int],
    concentration_threshold: float = CONCENTRATION_THRESHOLD,
    loss_streak_threshold: int = LOSS_STREAK_THRESHOLD,
) -> dict:
    """순수 판정 — 임계 초과 여부와 사유 목록. **LLM 을 거치지 않는다.**"""
    reasons: list[str] = []

    if exposure:
        if exposure.get("offsetting_pairs"):
            pairs = ", ".join(
                f"{p['long_symbol']}/{p['inverse_symbol']}" for p in exposure["offsetting_pairs"]
            )
            reasons.append(f"상쇄 쌍 동시 보유: {pairs}")
        by_symbol = exposure.get("by_symbol") or []
        total = sum(float(s.get("notional_krw", 0.0)) for s in by_symbol)
        if total > 0:
            for s in by_symbol:
                pct = float(s.get("notional_krw", 0.0)) / total
                if pct >= concentration_threshold:
                    reasons.append(
                        f"단일 종목 집중: {s['symbol']} 보유 명목의 {pct * 100:.0f}%"
                        f" (임계 {concentration_threshold * 100:.0f}%)"
                    )
        if exposure.get("duplicates"):
            dups = ", ".join(
                f"{s['symbol']}({s['n_strategies']}개 전략)" for s in exposure["duplicates"]
            )
            reasons.append(f"중복 보유(여러 전략이 같은 심볼): {dups}")

    for strategy, streak in sorted(consecutive.items()):
        if streak >= loss_streak_threshold:
            reasons.append(f"연속 손실: [{strategy}] {streak}건 (임계 {loss_streak_threshold}건)")

    return {"breach": bool(reasons), "reasons": reasons}


def build_dossier(scoreboard_text: str, exposure: dict | None, flags: dict) -> str:
    """LLM 프롬프트에 실을 서류. 결정론 판정(flags)을 **먼저** 보여준다 —
    LLM 이 이미 확정된 사실을 재확인하는 게 아니라, 그 위에서 우선순위·권고를
    보태는 역할임을 분명히 한다."""
    parts = ["[결정론 판정]"]
    if flags["reasons"]:
        parts.extend(f"- {r}" for r in flags["reasons"])
    else:
        parts.append("- 임계 초과 없음")
    parts.append("\n[노출 요약]")
    parts.append(exposure.get("summary") if exposure else "포트폴리오 상태 없음")
    parts.append("\n[누적 스코어보드]")
    parts.append(scoreboard_text or "(집계 없음)")
    return "\n".join(parts)


_PROMPT_TEMPLATE = (
    "당신은 개인 자동매매 시스템(\"우리 시스템\")의 리스크 리뷰어다. 매매 "
    "판단이나 종목 발굴에는 관여하지 않는다 — 오직 드로다운·집중도·상쇄쌍 "
    "노출·연속 손실 관점에서 지금 상태를 진단하고 권고할 뿐이다.\n\n"
    "아래 서류(결정론 판정 + 노출 요약 + 누적 스코어보드)를 보고, 리스크 "
    "관점에서 가장 중요한 문제 **최대 3개**를 각각 권고와 함께 제시하라. "
    "결정론 판정에 이미 나온 사실을 그대로 우선순위 1~2번에 반영하되, 근거 "
    "없는 새 위험을 지어내지 마라. 문제가 전혀 없으면 issues 를 빈 리스트로 "
    "낸다.\n\n"
    "반드시 JSON 하나만 출력하라: "
    '{{"issues": [{{"issue": "한 문장", "recommendation": "한 문장"}}]}}\n\n'
    "[서류]\n{dossier}\n"
)


def build_prompt(dossier: str) -> str:
    return _PROMPT_TEMPLATE.format(dossier=dossier)


def parse_issues(text: str | None) -> list[dict] | None:
    """LLM 응답 → 이슈 목록. 실패는 None(결근) — 빈 리스트로 위장하지 않는다.
    이슈는 최대 3개로 자른다(모델이 상한을 안 지켜도 여기서 지켜진다)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    issues = data.get("issues")
    if not isinstance(issues, list):
        return None
    out: list[dict] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        issue = str(it.get("issue") or "").strip()[:300]
        rec = str(it.get("recommendation") or "").strip()[:300]
        if not issue:
            continue
        out.append({"issue": issue, "recommendation": rec})
    return out[:3]


def run_review(dossier: str, narrate) -> list[dict] | None:
    """LLM 호출 1회. `narrate` 는 `Narrator.narrate` 시그니처(prompt -> str | None).
    실패(narrate 가 None, 또는 파싱 불가)는 None — 호출부가 결정론 판정만으로
    기록·알림을 계속한다(LLM 없이도 리스크 리뷰 자체는 완전하다)."""
    text = narrate(build_prompt(dossier))
    return parse_issues(text)


def to_record(date_str: str, flags: dict, issues: list[dict] | None) -> dict:
    return {
        "producer": PRODUCER, "date": date_str,
        "threshold_breach": flags["breach"], "breach_reasons": flags["reasons"],
        "issues": issues or [],
        "llm_ok": issues is not None,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def append_ledger(record: dict, path: Path | str) -> bool:
    """`risk_review.jsonl` 멱등 append — 같은 날짜는 하루 한 번만(재실행 방어)."""
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("producer") == PRODUCER and r.get("date") == record.get("date"):
                return False
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return True


def format_card(record: dict) -> str:
    """텔레그램/HTML 공용 카드. 첫 줄은 셸이 파싱하는 `BREACH: yes|no` 마커
    (`own_brief.sh`의 `TOKENS:`/`ai_trader.sh`의 `AI_WATCH:`와 같은 관례)."""
    lines = [f"BREACH: {'yes' if record['threshold_breach'] else 'no'}"]
    lines.append("🛡 독립 리스크 리뷰")
    if record["breach_reasons"]:
        lines.append("결정론 판정:")
        lines.extend(f"  - {r}" for r in record["breach_reasons"])
    else:
        lines.append("결정론 판정: 임계 초과 없음")
    if record["issues"]:
        lines.append("상위 문제 + 권고:")
        for it in record["issues"]:
            lines.append(f"  · {it['issue']} → {it['recommendation']}")
    elif not record["llm_ok"]:
        lines.append("(LLM 리뷰 결근 — 결정론 판정만)")
    return "\n".join(lines)
