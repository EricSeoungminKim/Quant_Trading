"""KR 시세 조회의 KIND 부재 폴백 — 2026-08-26 실사고 회귀.

실사고: KIND(상장법인목록)가 EC2 IP를 403으로 막으면서 `load_market_map` 이
매번 실패했다. 이름 사전은 DART 로 폴백하지만 **시장구분(유가/코스닥)은 DART 에
없어** 야후 접미사(.KS/.KQ)를 만들 수 없었고, 그래서 KR 리포트가 **시세를 하나도
못 받았다**(실측 2026-08-26: 선정 원장 13행 중 close 2건, baseline_score100 전무).

파급: 기준가가 없으면 전방 수익률(outcomes)이 전 지평 None → 리더보드·AI
트레이더 채점이 통째로 죽는다. "정직하게 빈 값"이지만 결과는 파이프라인 정지다.

수리: 시장구분을 모르면 **.KS/.KQ 양쪽을 한 배치로 조회해 실제로 값이 오는 쪽을
채택한다**. 야후 배치 조회는 결측 심볼을 조용히 건너뛰므로(fetch_symbol_quotes
계약) 추가 왕복이 없다.
"""
from __future__ import annotations

import logging
from pathlib import Path

from quant.report.collect.quotes import _ExpectedDelistFilter, fetch_kr_quotes


def _patch(monkeypatch, market_map=None, quotes=None, raise_map=False):
    import quant.report.collect.quotes as Q

    def _map(_cache):
        if raise_map:
            raise RuntimeError("KIND 403")
        return market_map or {}

    seen: dict = {}

    def _fetch(syms, **kw):
        seen["asked"] = list(syms)
        return dict(quotes or {})

    monkeypatch.setattr(Q, "load_market_map", _map)
    monkeypatch.setattr(Q, "fetch_symbol_quotes", _fetch)
    return seen


def test_uses_kind_map_when_available(monkeypatch):
    """KIND 가 살아 있으면 정확한 접미사 하나만 조회한다(요청 낭비 없음)."""
    seen = _patch(monkeypatch, market_map={"005930": "005930.KS"},
                  quotes={"005930.KS": {"close": 71000.0}})
    got, route = fetch_kr_quotes(["005930"], Path("/tmp"))
    assert got == {"005930": {"close": 71000.0}}
    assert seen["asked"] == ["005930.KS"]
    assert "KIND" in route


def test_probes_both_suffixes_when_kind_is_down(monkeypatch):
    """KIND 가 죽으면 .KS/.KQ 를 한 배치로 물어보고 값이 온 쪽을 채택한다."""
    seen = _patch(monkeypatch, raise_map=True,
                  quotes={"247540.KQ": {"close": 320000.0}})
    got, route = fetch_kr_quotes(["247540"], Path("/tmp"))
    assert got == {"247540": {"close": 320000.0}}
    assert set(seen["asked"]) == {"247540.KS", "247540.KQ"}
    assert "폴백" in route


def test_prefers_ks_when_both_return(monkeypatch):
    """둘 다 값이 오면 유가(.KS)를 택한다 — 코스닥 코드가 .KS 로도 조회되는
    드문 경우에 매일 답이 흔들리지 않게 결정론적으로 고정한다."""
    _patch(monkeypatch, raise_map=True,
           quotes={"005930.KS": {"close": 71000.0}, "005930.KQ": {"close": 1.0}})
    got, _ = fetch_kr_quotes(["005930"], Path("/tmp"))
    assert got["005930"]["close"] == 71000.0


def test_missing_symbol_is_omitted_not_zeroed(monkeypatch):
    """어느 쪽에서도 안 오면 키를 만들지 않는다 — 0 으로 위장 금지."""
    _patch(monkeypatch, raise_map=True, quotes={})
    got, _ = fetch_kr_quotes(["999999"], Path("/tmp"))
    assert got == {}


def test_partial_map_falls_back_only_for_missing_codes(monkeypatch):
    """KIND 매핑에 있는 코드는 그대로, 빠진 코드만 양쪽 접미사로 보완한다."""
    seen = _patch(monkeypatch, market_map={"005930": "005930.KS"},
                  quotes={"005930.KS": {"close": 71000.0},
                          "247540.KQ": {"close": 320000.0}})
    got, _ = fetch_kr_quotes(["005930", "247540"], Path("/tmp"))
    assert set(got) == {"005930", "247540"}
    assert set(seen["asked"]) == {"005930.KS", "247540.KS", "247540.KQ"}


def test_empty_input_makes_no_request(monkeypatch):
    seen = _patch(monkeypatch, raise_map=True)
    got, _ = fetch_kr_quotes([], Path("/tmp"))
    assert got == {} and "asked" not in seen


# ── D3(2026-09-03): .KS/.KQ 이중 조회의 예상된 ERROR 를 DEBUG 로 낮춘다 ────────

def _record(msg: str, level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord("yfinance", level, __file__, 0, msg, (), None)


def test_expected_delist_filter_downgrades_probe_symbol_errors():
    """이번 호출이 만든 추측 후보 심볼(.KS/.KQ 이중 조회) 얘기면 DEBUG 로 낮춘다
    — KIND 가 죽어 다수 종목이 이 경로를 타면 한쪽은 항상 결측이 정상이라, 그걸
    ERROR 로 두면 매 종목마다 도배돼 진짜 장애를 가린다."""
    f = _ExpectedDelistFilter(frozenset({"091990.KS", "091990.KQ"}))
    record = _record('$091990.KQ: possibly delisted; no price data found  (period=1y)')

    assert f.filter(record) is True  # 레코드 자체는 버리지 않는다
    assert record.levelno == logging.DEBUG
    assert record.levelname == "DEBUG"


def test_expected_delist_filter_leaves_unrelated_errors_alone():
    """추측 후보 심볼 집합에 없는 심볼(예: KIND 로 이미 확정된 심볼)의 ERROR 는
    진짜 장애일 수 있다 — 낮추지 않는다."""
    f = _ExpectedDelistFilter(frozenset({"091990.KS", "091990.KQ"}))
    record = _record('$005930.KS: possibly delisted; no price data found  (period=1y)')

    f.filter(record)

    assert record.levelno == logging.ERROR


def test_expected_delist_filter_is_noop_with_no_probe_symbols():
    """이중 조회 후보가 없으면(코드가 전부 KIND 매핑됨) 아무것도 낮추지 않는다."""
    f = _ExpectedDelistFilter(frozenset())
    record = _record('$091990.KS: possibly delisted; no price data found  (period=1y)')

    f.filter(record)

    assert record.levelno == logging.ERROR


def test_fetch_kr_quotes_downgrades_expected_dual_probe_errors(monkeypatch, caplog):
    """통합: KIND 가 죽어 .KS/.KQ 를 이중 조회할 때, `fetch_symbol_quotes` 내부(여기선
    스텁)가 yfinance 로거에 남기는 것과 같은 모양의 ERROR 를 낮춘다. 실제
    yfinance 호출은 없다 — 필터가 배선돼 있는지만 확인한다."""
    import quant.report.collect.quotes as Q

    def _map(_cache):
        raise RuntimeError("KIND 403")

    def _fetch(syms, **kw):
        # 실제 yfinance 가 하듯, 이 호출 도중 "yfinance" 로거에 결측 후보의
        # ERROR 를 남긴다.
        logging.getLogger("yfinance").error(
            "$247540.KS: possibly delisted; no price data found  (period=1y)")
        return {"247540.KQ": {"close": 320000.0}}

    monkeypatch.setattr(Q, "load_market_map", _map)
    monkeypatch.setattr(Q, "fetch_symbol_quotes", _fetch)

    with caplog.at_level(logging.DEBUG, logger="yfinance"):
        got, _ = fetch_kr_quotes(["247540"], Path("/tmp"))

    assert got == {"247540": {"close": 320000.0}}
    matching = [r for r in caplog.records if "247540.KS" in r.getMessage()]
    assert matching and matching[0].levelno == logging.DEBUG

    # 필터가 이 호출 밖으로 새지 않는다 — 다음 호출은 다시 ERROR 로 보인다.
    assert not any(isinstance(fl, Q._ExpectedDelistFilter)
                   for fl in logging.getLogger("yfinance").filters)


def test_fetch_failure_returns_empty_not_raise(monkeypatch):
    import quant.report.collect.quotes as Q

    monkeypatch.setattr(Q, "load_market_map", lambda _c: {"005930": "005930.KS"})

    def boom(*_a, **_k):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(Q, "fetch_symbol_quotes", boom)
    got, route = fetch_kr_quotes(["005930"], Path("/tmp"))
    assert got == {} and "실패" in route
