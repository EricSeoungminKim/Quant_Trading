"""`quant.apps.assembly.build_paper_runtime`가 `TradeLedgerSink`에 전략별
파라미터 지문 맵을 배선하는지 확인한다 (2026-09-07, 진화가능성 평가 투자 #1).

`build_paper_runtime`은 TossClient/브로커/유니버스를 실제로 조립하는 무거운
함수라 네트워크 없이 단위 테스트하기 어렵다(다른 조립 테스트도 이 함수 자체는
피하고 `rebuild_strategies` 같은 하위 함수만 단위 테스트한다). 그래서 이
파일은 실제로 배선하는지를 소스 검사로 고정한다 —
`tests/test_data_contracts_ledger.py::test_trades_jsonl_consumers_sort_by_ts`와
같은 패턴(그 테스트도 `round_trips`가 `sorted(...)`를 쓰는지 소스로 고정한다).

지문을 실제로 계산하는 로직(`strategy_fingerprints`)은 `tests/test_experiments.py`가
이미 두텁게 커버하므로 여기서 재검증하지 않는다 — 이 파일의 관심사는 오직
"조립이 그 함수를 실제로 부르는가"다."""
from __future__ import annotations

import inspect

from quant.apps import assembly
from quant.control.experiments import strategy_fingerprints


def test_assembly_reuses_the_single_source_of_truth_for_fingerprints():
    """`quant.control.experiments.strategy_fingerprints`가 유일한 계산처다 —
    assembly가 자기 버전을 따로 만들면 조립 시점 지문과 hot-reload 지문
    (`TradeLedgerSink.refresh_params_fingerprints`가 부르는 것과 동일 함수)이
    갈라질 수 있다."""
    assert assembly.strategy_fingerprints is strategy_fingerprints


def test_build_paper_runtime_wires_params_fingerprint_into_the_ledger_sink():
    src = inspect.getsource(assembly.build_paper_runtime)
    assert "params_fingerprint_of=strategy_fingerprints(" in src, (
        "TradeLedgerSink 조립에서 params_fingerprint_of 배선이 사라졌다 — "
        "원장 행에 파라미터 판본이 더는 안 찍히는 회귀"
    )
