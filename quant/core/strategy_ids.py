"""전략 id 접미사 규약 — 단일 정의(2026-09-03).

**왜 core 인가.** `quant/trade/loop.py`(오버나이트 판정·표시명), `quant/trade/risk/
manager.py`(정책 상속), `quant/control/ledger.py`(A/B 갈래 비교)가 전부 같은 규칙을
각자 베껴 쓰고 있었다 — `quant/trade/`가 `quant/control/`을 모르고(평면 규칙),
`loop.py`가 `manager.py`를 임포트하지 않아 서로 참조할 수 없었기 때문이다.
그 결과 `ledger.base_strategy_id`가 `_pure` 접미사를 벗기지 않는 채로 갈라졌다
(A/B 비교가 `donchian_pure_cat` 같은 조합을 잘못 묶을 뻔한 버그). 의존 방향의
바닥인 `quant.core`에 두면 세 평면 모두 임포트할 수 있다(core는 아무도 임포트하지
않는다).

## 접미사 두 종류

- `_cat` — A/B 촉매 갈래(`config/settings.yaml`의 `<id>_cat` 블록). 같은 클래스를
  다른 유니버스(`universe_filter`)로 돌리는 실험용 쌍.
- `_pure` — 순수함수 계약 껍질(`donchian_pure`, `scalp_1m_pure` 등). 레거시
  구현과 나란히 비교하기 위한 별도 등록 id.

## 벗기는 순서

**`_cat`을 먼저 벗기고, 그다음 `_pure`를 벗긴다.** 두 접미사가 겹치는 조합
(`scalp_1m_pure_cat` — 2026-09-03 기준 `config/settings.yaml`에 실제 등록된
전략은 없지만, 형태 자체는 이 함수로 다뤄질 수 있다)에서도 순서가 결과를
바꾼다: `scalp_1m_pure_cat` → (`_cat` 벗김) `scalp_1m_pure` → (`_pure` 벗김)
`scalp_1m`. 반대 순서로 벗기면 `_pure`가 없으므로 `scalp_1m_pure_cat` →
`scalp_1m_cat`(틀린 결과)가 된다. 이 순서는 `quant/trade/loop.py`·
`quant/trade/risk/manager.py`가 기존에 쓰던 순서를 그대로 승계한 것이다.
"""
from __future__ import annotations

CATALYST_ARM_SUFFIX = "_cat"
PURE_ARM_SUFFIX = "_pure"


def base_strategy_id(strategy_id: str | None) -> str:
    """`_cat`(A/B 촉매 갈래)을 먼저, `_pure`(순수 계약 껍질)를 그다음 벗긴 기준
    전략 id. 둘 다 없으면 그대로 돌려준다. `None`/빈 문자열은 빈 문자열로."""
    sid = str(strategy_id or "")
    return sid.removesuffix(CATALYST_ARM_SUFFIX).removesuffix(PURE_ARM_SUFFIX)


def is_catalyst_arm(strategy_id: str | None) -> bool:
    """A/B 촉매 갈래(`<id>_cat`)인지."""
    return str(strategy_id or "").endswith(CATALYST_ARM_SUFFIX)
