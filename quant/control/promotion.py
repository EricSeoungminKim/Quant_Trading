"""백테스트 게이트 통과 → 모의투자 승격 (2026-09-03).

## 왜 필요한가

`backtest-gate` CLI(quant/backtest/gate.py)는 GO/NO_GO/판단 불가를 낸다. 이
파일이 그 판정을 config/settings.yaml 반영으로 잇는 마지막 칸이다 — 소유자가
로컬에서 게이트를 보고 손으로 `enabled: true`를 치는 대신, **증거 없이는
승격 자체가 안 되도록** 강제한다(게이트 파일이 없거나, 판정이 GO가 아니거나,
전략이 안 맞거나, 오래됐으면 `check_promotable`이 그 이유를 그대로 돌려준다 —
조용히 통과시키지 않는다).

## settings.yaml을 직접 쓰는 이유(그리고 위험)

`quant/apps/config.py`의 `AUTO_PARAMS_FILENAME` 주석이 밝히듯, 이 저장소의
거버너(`quant/control/governor.py`)는 자동 반영을 위해 **오버레이 파일**
(`config/auto_params.yaml`)을 따로 쓴다 — settings.yaml을 파싱해서 다시 쓰면
사람이 쓴 주석이 전부 날아가기 때문이다. 승격은 그 관례와 다르다: 오버레이는
파라미터 미세조정용이고, 승격은 `enabled`/`validation` 같은 **그 전략 블록에
고유한, 소유자가 늘 settings.yaml에서 직접 읽는 필드**를 바꾼다 — 오버레이에
숨기면 "이 전략이 왜 켜져 있지?"를 settings.yaml만 보고는 답할 수 없게 된다.
그래서 여기서는 대상 전략 블록의 **해당 필드 줄만** 텍스트 수준에서 교체한다
(ruamel.yaml 같은 라운드트립 파서는 이 저장소의 의존성이 아니라 새로 추가하지
않았다) — 다른 전략 블록과 파일 상단 주석은 바이트 단위로 그대로 남는다.
교체되는 필드(`enabled`, `validation.status`, `validation.evidence`, 선택적
`capital_fraction`) 위/옆에 붙어 있던 주석은 그 필드의 옛 상태를 설명하던
것이라 승격 후에는 부정확해진다 — 지우는 대신 남기면 거짓말이 되므로 그 줄
자체를 새 값으로 교체할 때 같이 버린다(그 필드를 설명하던 다른 주석, 예:
전략 블록 맨 위의 역사 기록은 건드리지 않는다).

## 대상 밖

승격은 사람이 `backtest-gate` → `promote --dry-run` → `promote`를 순서대로
돌리는 동안 일어난다 — 이 파일은 그 심사·반영만 하고, 언제 돌릴지·실계좌
전환 여부는 여전히 소유자 판단이다(docs/runbooks/go-live.md).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "load_gate",
    "check_promotable",
    "render_promoted_settings",
    "apply_promotion",
]

_MARKETS = ("KR", "US")
_MAX_GATE_AGE_DAYS = 14


def load_gate(path: str | Path) -> dict[str, Any]:
    """`backtest-gate` CLI가 쓴 게이트 JSON(`data/backtest/gate_<전략>_<날짜>.json`)을
    읽는다. 반환 dict에 `gate_path`(문자열)를 얹는다 — evidence에 "이 승격이 어느
    파일 근거로 이뤄졌는지"를 남기려면 원본 경로가 필요한데, 게이트 JSON 자체는
    자기 경로를 모른다."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.setdefault("gate_path", str(p))
    return data


def _normalize_capital_fraction(value: Any) -> dict[str, float]:
    if isinstance(value, dict):
        return {m: float(value.get(m, 0.0)) for m in _MARKETS}
    v = float(value)
    return {m: v for m in _MARKETS}


def check_promotable(
    gate: dict[str, Any],
    *,
    strategy_id: str,
    settings: dict[str, Any],
    now: datetime | None = None,
    max_age_days: int = _MAX_GATE_AGE_DAYS,
) -> list[str]:
    """게이트 JSON(`load_gate` 출력) + 현재 settings dict → 승격을 막는 이유
    목록. 빈 리스트면 승격 가능.

    **하나만 걸려도 즉시 반환하지 않고 전부 모아서 돌려준다** — 첫 이유만 보고
    고쳤다가 재실행에서 다음 이유를 만나는 왕복을 없애기 위함.

    `data_range`/`fill_model`/`cost_assumptions` 세 필드는 게이트 JSON 최상위에
    있어야 한다고 가정한다 — 2026-09-03 시점 `cmd_backtest_gate`(다른 작업자가
    동시 편집 중)는 아직 이 필드들을 쓰지 않는다. 그래서 지금은 이 체크가 항상
    "증거 없음"으로 막는다 — **의도된 동작이다**(증거 없는 승격을 조용히
    통과시키지 않는다는 게 이 파일의 존재 이유). 그 작업자가 게이트 JSON에
    필드를 채우기 시작하면 이 함수는 코드 변경 없이 그대로 작동한다.
    """
    reasons: list[str] = []
    now = now or datetime.now()

    gate_verdict = (gate.get("gate") or {}).get("verdict")
    if gate_verdict != "GO":
        reasons.append(f"게이트 판정이 GO가 아님: {gate_verdict!r}")

    gate_strategy = gate.get("strategy")
    if gate_strategy != strategy_id:
        reasons.append(f"게이트의 전략({gate_strategy!r})이 승격 대상({strategy_id!r})과 다름")

    data_range = gate.get("data_range")
    fill_model = gate.get("fill_model")
    cost_assumptions = gate.get("cost_assumptions")
    if not data_range:
        reasons.append("게이트에 data_range(백테스트 구간) 없음")
    if not fill_model:
        reasons.append("게이트에 fill_model(체결 가정) 없음")
    elif fill_model != "intrabar":
        reasons.append(f"fill_model={fill_model!r} — 보수적 모델(intrabar)만 승격 가능")
    if not cost_assumptions:
        reasons.append("게이트에 cost_assumptions(비용 가정) 없음")

    generated_at = gate.get("generated_at")
    if not generated_at:
        reasons.append("게이트에 generated_at 없음 — 신선도 판정 불가")
    else:
        try:
            gen_dt = datetime.fromisoformat(generated_at)
        except ValueError:
            reasons.append(f"generated_at 형식을 읽을 수 없음: {generated_at!r}")
        else:
            age_days = (now - gen_dt).days
            if age_days > max_age_days:
                reasons.append(
                    f"게이트가 {age_days}일 전 것 — {max_age_days}일 초과(재검증 필요)"
                )

    strat_cfg = (settings.get("strategies") or {}).get(strategy_id)
    if strat_cfg is None:
        reasons.append(f"config/settings.yaml에 전략 {strategy_id!r} 없음")
    else:
        enabled = strat_cfg.get("enabled", True)
        status = (strat_cfg.get("validation") or {}).get("status")
        if enabled is not False and status == "backtest_pass":
            reasons.append(
                "이미 승격됨(enabled=true, validation.status=backtest_pass) — 다시 승격할 필요 없음"
            )

        fractions = _normalize_capital_fraction(strat_cfg.get("capital_fraction", 1.0))
        if all(v <= 0 for v in fractions.values()):
            reasons.append(f"capital_fraction이 전 시장 0 — {fractions}")

    return reasons


# ---------------------------------------------------------------------------
# settings.yaml 텍스트 수술 — 대상 전략 블록의 필드 줄만 교체한다.
# ---------------------------------------------------------------------------


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_blank_or_comment_only(line: str) -> bool:
    s = line.strip()
    return s == "" or s.startswith("#")


def _key_of(line: str) -> str | None:
    s = line.strip()
    if not s or s.startswith("#") or ":" not in s:
        return None
    return s.split(":", 1)[0].strip()


def _find_key_span(lines: list[str], start: int, end: int, key: str, indent: int) -> tuple[int, int] | None:
    """[start, end) 범위에서 들여쓰기 `indent`에 있는 `key:` 줄의 (시작, 끝)
    인덱스(끝은 배타적). "끝"은 그 키의 자식 줄이 전부 끝나는 지점 — 빈 줄은
    안에 포함하고, 들여쓰기가 `indent` 이하인 실질(공백/주석 아닌) 줄을 만나면
    멈춘다. 주석 전용 줄이 `indent`와 같은 들여쓰기면 그건 보통 *다음* 필드를
    설명하는 것이라 여기서도 멈춘다."""
    i = start
    while i < end:
        line = lines[i]
        if not _is_blank_or_comment_only(line) and _indent_of(line) == indent and _key_of(line) == key:
            j = i + 1
            while j < end and (lines[j].strip() == "" or _indent_of(lines[j]) > indent):
                j += 1
            return i, j
        i += 1
    return None


def _flow_yaml(value: dict[str, Any]) -> str:
    """dict → 한 줄 flow-style YAML 조각(주어진 키 순서 보존)."""
    import yaml as _yaml

    return _yaml.safe_dump(value, allow_unicode=True, sort_keys=False, default_flow_style=True, width=1_000_000).strip()


def _set_scalar_field(block: list[str], key: str, indent: int, value_str: str) -> list[str]:
    """`block` 안에서 `key:`(들여쓰기 `indent`) 줄을 통째로 `key: value_str`로
    교체한다. 없으면 `block[0]`(블록의 헤더 줄) 바로 뒤에 새로 끼워 넣는다."""
    span = _find_key_span(block, 1, len(block), key, indent)
    new_line = " " * indent + f"{key}: {value_str}\n"
    if span:
        s, e = span
        return block[:s] + [new_line] + block[e:]
    return block[:1] + [new_line] + block[1:]


def _set_validation_fields(block: list[str], *, status: str, evidence: dict[str, Any]) -> list[str]:
    evidence_line = _flow_yaml(evidence)
    v_span = _find_key_span(block, 1, len(block), "validation", 4)
    if v_span is None:
        new_sub = [
            "    validation:\n",
            f"      status: {status}\n",
            f"      evidence: {evidence_line}\n",
        ]
        return block[:1] + new_sub + block[1:]
    v_s, v_e = v_span
    sub = block[v_s:v_e]
    sub = _set_scalar_field(sub, "status", 6, status)
    sub = _set_scalar_field(sub, "evidence", 6, evidence_line)
    return block[:v_s] + sub + block[v_e:]


def _set_capital_fraction(block: list[str], capital_fraction: dict[str, float]) -> list[str]:
    flow = _flow_yaml({m: float(capital_fraction[m]) for m in _MARKETS if m in capital_fraction})
    return _set_scalar_field(block, "capital_fraction", 4, flow)


def _build_evidence(gate: dict[str, Any], promoted_at: datetime) -> dict[str, Any]:
    g = gate.get("gate") or {}
    criteria = g.get("criteria") or {}
    cost_assumptions = gate.get("cost_assumptions")
    cost_bp = cost_assumptions.get("cost_bp") if isinstance(cost_assumptions, dict) else cost_assumptions
    return {
        "gate_path": gate.get("gate_path"),
        "verdict": g.get("verdict"),
        "oos_trades": (criteria.get("oos_n_trades") or {}).get("value"),
        "expectancy_bp": (criteria.get("oos_expectancy") or {}).get("value"),
        "deflated_sharpe": (criteria.get("deflated_sharpe") or {}).get("value"),
        "data_range": gate.get("data_range"),
        "fill_model": gate.get("fill_model"),
        "cost_bp": cost_bp,
        "promoted_at": promoted_at.isoformat(timespec="seconds"),
    }


def render_promoted_settings(
    text: str,
    strategy_id: str,
    gate: dict[str, Any],
    *,
    capital_fraction: dict[str, float] | None = None,
    promoted_at: datetime | None = None,
) -> str:
    """settings.yaml 전체 텍스트 → 승격 반영된 새 텍스트(순수 함수, 파일 I/O
    없음). CLI의 `--dry-run` 디프는 이 함수의 출력을 원본과 비교해서 만든다."""
    lines = text.splitlines(keepends=True)
    promoted_at = promoted_at or datetime.now()

    strategies_span = _find_key_span(lines, 0, len(lines), "strategies", 0)
    if strategies_span is None:
        raise ValueError("config/settings.yaml에 최상위 strategies: 섹션이 없음")
    strategies_start, strategies_end = strategies_span

    block_span = _find_key_span(lines, strategies_start + 1, strategies_end, strategy_id, 2)
    if block_span is None:
        raise ValueError(f"strategies: 아래에 {strategy_id!r} 블록이 없음")
    block_start, block_end = block_span

    block = lines[block_start:block_end]
    block = _set_scalar_field(block, "enabled", 4, "true")
    block = _set_validation_fields(
        block, status="backtest_pass", evidence=_build_evidence(gate, promoted_at)
    )
    if capital_fraction is not None:
        block = _set_capital_fraction(block, capital_fraction)

    new_lines = lines[:block_start] + block + lines[block_end:]
    return "".join(new_lines)


def apply_promotion(
    settings_path: str | Path,
    strategy_id: str,
    gate: dict[str, Any],
    *,
    capital_fraction: dict[str, float] | None = None,
    promoted_at: datetime | None = None,
) -> None:
    """`render_promoted_settings`의 결과를 실제로 `settings_path`에 쓴다 — 이
    모듈에서 파일에 쓰는 유일한 함수."""
    path = Path(settings_path)
    original = path.read_text(encoding="utf-8")
    updated = render_promoted_settings(
        original, strategy_id, gate, capital_fraction=capital_fraction, promoted_at=promoted_at,
    )
    path.write_text(updated, encoding="utf-8")
