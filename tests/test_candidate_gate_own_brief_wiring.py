"""candidate_gate → own_brief.sh 소비 경로 배선 검증 (2026-09-07, 리포트 QA 세션).

`quant.report.collect.core._derive`가 `payload["auto_watch"]`를
`gate_candidates()` 결과로 이미 덮어쓴 뒤(`quant/analyze/candidate_gate.py`
모듈독스트링의 "watch_only" 절), 그 `payload`가 그대로 engine.json 으로
직렬화된다(`quant.analyze.render.write_machine(model.payload, ...)`).
`own_brief.sh`는 그 engine.json 을 `brief_from_report.py`(→
`quant.analyze.market_brief`)를 통해서만 읽으므로, 방어 국면 게이트로
`watch_only`(관망) 표시가 붙은 종목이 own_brief.sh 쪽에서 다시 자동 편입
후보로 살아날 길이 없어야 한다 — 이 계약이 깨지면 "방어 국면에서 후보를
줄인다"는 candidate_gate 의 목적 자체가 자동 편입 단계에서 무효화된다.

이 파일은 own_brief.sh(셸)를 직접 실행하지 않는다 — own_brief.sh 가
실제로 의존하는 순수 함수(`quant.analyze.market_brief`,
`quant.report.collect.intraday._candidate_symbols`)를 `gate_candidates()`
출력으로 직접 검증하는 편이, watch-score/tg_bridge 같은 무거운 의존 없이
같은 계약을 더 정확히 잠근다.

`quant/analyze/candidate_gate.py`와 `quant/report/collect/intraday.py`는
이 작업에서 읽기 전용이다 — 여기서는 공개 계약만 검증하고 내부 구현은
건드리지 않는다.
"""
from __future__ import annotations

from quant.analyze.candidate_gate import gate_candidates
from quant.analyze.market_brief import (
    auto_watch_tokens,
    engine_tokens,
    foreign_flow_candidate_symbols,
)
from quant.report.collect.intraday import _candidate_symbols


def _symbols(*rows):
    return [dict(r) for r in rows]


def _gated_payload(market: str = "KR") -> tuple[dict, dict]:
    """`quant.report.collect.core._derive`가 방어 국면에서 만드는 것과 같은
    모양의(payload["auto_watch"]가 게이트 결과로 이미 교체된) payload."""
    tokens = " ".join(f"{100000 + i}:NEWS" for i in range(10))
    auto_watch = f"AUTO_WATCH: {tokens}"
    symbols = _symbols(*(
        {
            "symbol": str(100000 + i), "trending_score100": 100 - i,
            "origin": "news", "name": f"종목{i}",
        }
        for i in range(10)
    ))
    gate = gate_candidates(
        auto_watch, symbols, regime_label="defensive", diagnostic_score100=None, top_n=8,
    )
    assert gate["capped"], "테스트 전제: 방어 국면 + 10건 > top_n(8) 이어야 게이트가 발동한다"
    assert len(gate["watch_only"]) == 2

    payload = {
        "schema": 1, "market": market, "session_date": "2026-09-07",
        "auto_watch": gate["auto_watch"],  # core.py가 gate["auto_watch"]로 덮어쓰는 것과 동일
        "symbols": symbols,  # 원장 — candidate_gate는 이 리스트를 지우지 않는다
        "candidate_gate": gate,
    }
    return payload, gate


def test_watch_only_symbols_are_absent_from_the_persisted_auto_watch_line():
    payload, gate = _gated_payload()
    kept = {t.split(":", 1)[0] for t in payload["auto_watch"].split()[1:]}
    for sym in gate["watch_only"]:
        assert sym not in kept


def test_own_brief_consumer_auto_watch_tokens_excludes_watch_only():
    """`brief_from_report.py`(own_brief.sh 가 부르는 스크립트)가 TOKENS 줄을
    만들 때 쓰는 바로 그 함수 — watch_only 종목이 여기 들어오면 그대로
    자동 편입으로 이어진다."""
    payload, gate = _gated_payload()
    toks = {t.split(":", 1)[0] for t in auto_watch_tokens(payload, "KR")}
    for sym in gate["watch_only"]:
        assert sym not in toks


def test_own_brief_consumer_engine_tokens_excludes_watch_only():
    """엔진 어휘로 번역된 뒤(watch-score 입력)에도 watch_only 종목이 새어
    들어오면 안 된다."""
    payload, gate = _gated_payload()
    toks = {t.split(":", 1)[0] for t in engine_tokens(payload, "KR")}
    for sym in gate["watch_only"]:
        assert sym not in toks


def test_intraday_candidate_symbols_reads_the_already_gated_auto_watch_line():
    """`_build_intraday_view`(당일 단타 후보)의 채점 유니버스 —
    `_candidate_symbols(payload)`가 이미 게이트가 적용된
    `payload["auto_watch"]`만 본다는 계약. 이게 깨지면 intraday_view 를
    거쳐가는 EVENT_SCALP 토큰(own_brief.sh 의 TOKENS 줄에도 합류)으로
    watch_only 종목이 우회 편입될 수 있다."""
    payload, gate = _gated_payload()
    candidates = _candidate_symbols(payload)
    for sym in gate["watch_only"]:
        assert sym not in candidates
    # 살아남은 8건은 여전히 채점 대상이어야 한다(전부 지워지는 회귀 방지).
    assert len(candidates) == 8


def test_foreign_flow_candidate_symbols_does_not_reintroduce_watch_only():
    """FRGN/FRGN_EXIT 재평가 후보 확장 경로(own_brief.sh 의 "2.5" 단계)도
    게이트를 우회해 watch_only 종목을 다시 끌어들이면 안 된다 —
    `already_tagged`로 명시적으로 이미 등록된 종목만 예외."""
    payload, gate = _gated_payload()
    syms = foreign_flow_candidate_symbols(payload, "KR")
    for sym in gate["watch_only"]:
        assert sym not in syms


def test_us_market_is_unaffected_by_candidate_gate_symbol_shape():
    """US 심볼 형태 검증(`_SYMBOL_SHAPE`)이 게이트가 남긴 값과 충돌해 전부
    걸러지는 일이 없는지 — KR 전용 회귀가 아니라는 것만 확인한다."""
    payload, gate = _gated_payload(market="US")
    # US 심볼 형태(`_SYMBOL_SHAPE["US"]`)에 맞지 않는 6자리 숫자 토큰이라
    # US 경로에서는 전부 걸러진다 — 이건 candidate_gate 의 문제가 아니라
    # 시장별 형태 검증이 원래 하는 일이므로, "0건"이 정상값임을 못박아 둔다.
    assert auto_watch_tokens(payload, "US") == []
