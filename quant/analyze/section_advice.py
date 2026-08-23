"""AI 포지션 해석 — 수급 체력 · 시장 심리 · 기술적 지표 · 유동성·금리를
전문가 톤으로 재해석한다 (리포트 UX 3차, 2026-08-17 사용자 피드백).

`exec_summary.py`와 같은 관례다: **판단이 아니라 서술이다.** 어떤 숫자를
보여줄지는 이미 각 섹션(`kr_funding`/`sentiment`/`macro`/`vix_term`/
`sectors`/`breadth`)이 결정론으로 정했다 — 여기서는 그 숫자를 초보 투자자가
읽을 수 있는 조건부 서술로 바꿀 뿐, 새 사실을 만들지 않는다.

무LLM 폴백: `advise`가 `None`을 돌려주면 호출부(`report_cli`)는 기존
숫자 카드만으로 이미 완전하다 — 어느 섹션이든 숫자만 있고 해석 문단이 없는
게 실패가 아니라 기본 상태다.

**단정 지시·수익 보장은 금지한다** — 프롬프트로 조건부 톤("~인 국면에서는
~이 일반적 대응이다")을 요구하고, 각 섹션 해석 끝에는 코드가 결정론으로
"판단과 책임은 투자자 본인 몫이다" 문장을 붙인다. LLM 이 그 문장을 깜빡
잊어도(응답 파싱 실패와 무관하게) 안전 문구가 빠지면 안 되므로 narrator
응답에 의존하지 않는다.
"""
from __future__ import annotations

import logging

# 섹션 키 → 프롬프트/파싱에 쓰는 한글 마커. 템플릿 헤딩과 그대로 맞춘다
# (한국 수급 체력/시장 심리/기술적 지표/유동성·금리) — narrator 가 헷갈리지
# 않게 리포트에 실제로 보이는 제목을 그대로 쓴다.
logger = logging.getLogger(__name__)

_MARKERS = {
    "supply": "[한국 수급 체력]",
    "sentiment": "[시장 심리]",
    "technical": "[기술적 지표]",
    "liquidity": "[유동성·금리]",
}
_SECTION_ORDER = ("supply", "sentiment", "technical", "liquidity")

_DISCLAIMER = "판단과 책임은 투자자 본인 몫이다."


def gather_section_numbers(
    kr_funding: dict | None,
    sentiment: dict | None,
    macro: dict | None,
    vix_term: dict | None,
    sectors: dict | None,
    breadth: dict | None,
) -> dict:
    """네 섹션이 화면에 실제로 보여주는 숫자만 뽑는다(각 섹션의 원본 소스
    dict를 그대로 받는다 — `report.html.j2`의 `d('kr_funding')` 등과 동일한
    입력). 섹션에 표시할 숫자가 하나도 없으면 그 키는 `None`(있는 걸 없다고
    하지 않지만, 없는 걸 지어내지도 않는다)."""
    numbers: dict[str, dict | None] = {}

    funding = (kr_funding or {}).get("funding") or {}
    numbers["supply"] = (
        {
            name: {"value": v.get("value"), "unit": v.get("unit"), "change_pct": v.get("change_pct")}
            for name, v in funding.items()
        }
        if funding else None
    )

    sent_out: dict = {}
    if sentiment:
        if sentiment.get("cnn_fear_greed"):
            fg = sentiment["cnn_fear_greed"]
            sent_out["cnn_fear_greed"] = {"value": fg.get("value"), "rating": fg.get("rating_ko")}
        if sentiment.get("naaim"):
            nm = sentiment["naaim"]
            sent_out["naaim"] = {"value": nm.get("value"), "label": nm.get("label")}
        if sentiment.get("aaii"):
            aa = sentiment["aaii"]
            sent_out["aaii"] = {
                "bull_pct": aa.get("bull_pct"), "bear_pct": aa.get("bear_pct"),
                "spread": aa.get("spread"),
            }
    numbers["sentiment"] = sent_out or None

    tech_out: dict = {}
    if vix_term:
        tech_out["vix_structure"] = vix_term.get("structure")
        tech_out["vix_spread"] = vix_term.get("spread")
    if sectors and sectors.get("sectors"):
        ranked = sorted(sectors["sectors"], key=lambda x: x.get("change_pct") or 0, reverse=True)
        tech_out["top_sector"] = {"name": ranked[0]["name"], "change_pct": ranked[0]["change_pct"]}
        tech_out["bottom_sector"] = {"name": ranked[-1]["name"], "change_pct": ranked[-1]["change_pct"]}
    if breadth:
        tech_out["highs"] = (breadth.get("highs") or {}).get("count")
        tech_out["lows"] = (breadth.get("lows") or {}).get("count")
        tech_out["universe"] = breadth.get("universe")
    numbers["technical"] = tech_out or None

    liq_out: dict = {}
    if macro:
        nl = macro.get("net_liquidity")
        if nl:
            liq_out["net_liquidity_change"] = nl.get("change")
        if macro.get("nfci_label"):
            liq_out["nfci_label"] = macro["nfci_label"]
        if macro.get("curve"):
            liq_out["curve_spread"] = macro["curve"].get("spread")
            liq_out["curve_label"] = macro["curve"].get("label")
    numbers["liquidity"] = liq_out or None

    return numbers


def build_prompt(numbers: dict) -> str:
    """`numbers`에 값이 있는 섹션만 프롬프트에 넣는다 — 없는 섹션을 지어내라고
    시키지 않는다."""
    present = [k for k in _SECTION_ORDER if numbers.get(k)]

    lines = [
        "다음은 오늘 리포트에 실린 실제 수치다. 초보 투자자에게 조언하는",
        "전문가 톤으로 아래 섹션을 해석하라. 규칙:",
        "- 섹션당 2~3문장.",
        "- '사라'/'팔아라' 같은 단정적 지시나 수익 보장 표현은 금지한다.",
        "- 조건부 서술 톤을 쓴다 (예: '~인 국면에서는 ~이 일반적 대응이다').",
        "- 제시된 숫자만 근거로 쓰고, 새 사실을 지어내지 않는다.",
        "",
    ]
    for key in present:
        lines.append(_MARKERS[key])
        lines.append(f"수치: {numbers[key]}")
        lines.append("")

    lines.append("다음 형식으로 정확히 요청된 섹션만, 마커 그대로 답하라(다른 텍스트 없이):")
    for key in present:
        lines.append(_MARKERS[key])
        lines.append("<2~3문장>")
        lines.append("")
    return "\n".join(lines)


def _parse(text: str, present: list[str]) -> dict[str, str] | None:
    """narrator 응답을 요청한 섹션 마커 기준으로 쪼갠다. 마커가 하나라도
    없거나, 어느 섹션 내용이든 비어 있으면 통째로 실패(`None`) —
    `exec_summary._split_three_paragraphs`와 같은 all-or-nothing 원칙."""
    text = text.strip()
    positions = []
    missing = [k for k in present if _MARKERS[k] not in text]
    if missing:
        # 조용히 버리지 않는다. 2026-08-16~20: 토큰 상한(기본 700)에 걸려 응답이
        # 두 섹션에서 잘렸는데 all-or-nothing 파서가 아무 말 없이 None 을 돌려줘
        # 리포트에서 요약이 5일 넘게 사라진 것을 아무도 몰랐다. 사람이 물어서야
        # 발견됐다 — 무엇이 왜 버려졌는지는 반드시 로그에 남는다.
        logger.warning(
            "섹션 AI 해석 파싱 실패 — 마커 누락 %s (요청 %d개 중 %d개 발견, "
            "응답 %d자). 응답이 잘렸을 가능성: OPENROUTER_MAX_TOKENS 확인",
            [_MARKERS[k] for k in missing], len(present), len(present) - len(missing), len(text),
        )
        return None
    for key in present:
        idx = text.find(_MARKERS[key])
        positions.append((idx, key))
    positions.sort()

    result: dict[str, str] = {}
    for i, (idx, key) in enumerate(positions):
        start = idx + len(_MARKERS[key])
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        content = text[start:end].strip()
        if not content:
            logger.warning("섹션 AI 해석 파싱 실패 — '%s' 섹션 내용이 비었다", _MARKERS[key])
            return None
        result[key] = content
    return result


def advise(numbers: dict, narrator) -> dict | None:
    """숫자를 조건부 서술 해석으로 바꾼다. 섹션이 하나도 없으면(전 섹션
    결측) narrator 를 부르지 않는다 — 빈 입력으로 LLM 을 부르는 건 낭비다.

    성공한 섹션에는 결정론으로 안전 문구(`_DISCLAIMER`)를 덧붙인다 — LLM
    응답 내용과 무관하게 항상 붙는다."""
    present = [k for k in _SECTION_ORDER if numbers.get(k)]
    if not present:
        return None

    prompt = build_prompt(numbers)
    text = narrator.narrate(prompt)
    if text is None:
        return None

    parsed = _parse(text, present)
    if parsed is None:
        return None

    return {key: f"{value} {_DISCLAIMER}" for key, value in parsed.items()}
