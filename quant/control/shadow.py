"""LLM 섀도우 판단 — Phase 7.4. 순수 함수만 (모델 호출은 `apps.cli shadow-judge`).

## 계약

LLM 은 **주문을 내지 않는다.** 판단만 남기고 실현 수익으로 채점돼, 이겨야만 승격한다.
못 이기면 안 넣으면 되고 비용은 0이다(무료 레인 모델). 구조적으로도 보장된다 —
판단을 읽어 주문을 내는 코드가 없고, 아키텍처 테스트가 `collect`/`analyze` → `trade`
임포트를 금지한다.

## 공정한 비교의 전제

LLM 이 결정론적 스코어러보다 **더 많은 정보를 보면** 그건 실력 비교가 아니라 정보량
비교다. 그래서 프롬프트는 선정 원장의 `attributes` 만으로 만들고, `input_hash` 도
그 같은 dict 로 계산한다. **결정론적 판정·점수는 프롬프트에 넣지 않는다** — 보여주면
베끼기만 해도 이긴다.

## 파싱 실패를 점수로 위장하지 않는다

모델이 형식을 어기면 그 종목은 **판단이 없는 것**이다. 0점을 주면 "최하위로 평가했다"가
되어 IC 를 오염시킨다. 우리가 주지 않은 종목이 나오면 환각이므로 버린다.
"""
from __future__ import annotations

import json

# 척도를 0~100 으로 못 박는다 — 순위만 쓰므로 절대값은 중요하지 않지만, 범위를 벗어난
# 값은 "지시를 안 따랐다"는 신호라 버려야 한다.
SCORE_MIN, SCORE_MAX = 0.0, 100.0

PROMPT_HEADER = """다음은 어느 거래일에 관심종목 후보로 올라온 종목들의 속성 벡터다.
각 종목이 앞으로 5거래일 동안 **다른 종목들보다** 더 오를 가능성을 0~100 으로 채점해라.

규칙:
- 한 줄에 `심볼 점수` 형식으로만 출력한다. 설명·서론·마크다운 금지.
- 준 종목만 채점한다. 목록에 없는 종목을 만들지 않는다.
- 모르는 종목은 그 줄을 생략한다. 추측으로 채우지 않는다.
- 절대 수익률이 아니라 **상대 순위**를 매기는 문제다.

종목:
"""


def build_prompt(rows: list[dict]) -> str:
    """선정 원장 행들 → 채점 프롬프트. **속성만** 넣는다."""
    lines = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        attrs = {k: v for k, v in (r.get("attributes") or {}).items()
                 if k not in ("symbol", "name")}
        lines.append(f"{sym} {json.dumps(attrs, ensure_ascii=False, sort_keys=True)}")
    return PROMPT_HEADER + "\n".join(lines)


def parse_scores(text: str | None, allowed: set[str]) -> dict[str, float]:
    """`심볼 점수` 줄만 골라낸다. 형식 위반·범위 밖·목록 밖은 **버린다**."""
    if not text:
        return {}
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        sym, raw = parts
        if sym not in allowed:
            continue          # 우리가 주지 않은 종목 = 환각
        try:
            score = float(raw)
        except ValueError:
            continue          # 점수를 못 읽었다 = 판단이 없는 것(0 이 아니다)
        if not (SCORE_MIN <= score <= SCORE_MAX):
            continue          # 척도를 벗어났다 = 지시를 안 따랐다
        out[sym] = score
    return out
