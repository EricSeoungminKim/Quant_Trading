"""`quant.analyze.symbol_names` — 해석 순서·LLM 배치·검증·영속성·label()."""
from __future__ import annotations

import json

from quant.analyze import symbol_names as sn


class _AssertNoCallLookup:
    def __call__(self, symbol: str):
        raise AssertionError(f"toss_lookup 호출됨: {symbol}")


class _AssertNoCallNarrator:
    def narrate(self, prompt: str) -> str | None:
        raise AssertionError(f"narrator 호출됨: {prompt}")


class _FakeNarrator:
    def __init__(self, response: str | None):
        self.response = response
        self.calls: list[str] = []

    def narrate(self, prompt: str) -> str | None:
        self.calls.append(prompt)
        return self.response


def test_label_basic():
    assert sn.label("005930", "삼성전자") == "삼성전자(005930)"
    assert sn.label("005930", None) == "005930"
    assert sn.label("005930", "") == "005930"
    assert sn.label("005930", "005930") == "005930"


def test_names_for_empty_input_short_circuits(tmp_path):
    resolver = sn.build_resolver(
        tmp_path / "cache", tmp_path / "state",
        toss_lookup=_AssertNoCallLookup(), narrator=_AssertNoCallNarrator(),
    )
    assert resolver.names_for([]) == {}


def test_cache_hit_needs_no_network_or_llm(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "symbol_names.json").write_text(
        json.dumps({"005930": "삼성전자"}), encoding="utf-8"
    )
    resolver = sn.build_resolver(
        tmp_path / "cache", state,
        toss_lookup=_AssertNoCallLookup(), narrator=_AssertNoCallNarrator(),
    )
    assert resolver.names_for(["005930"]) == {"005930": "삼성전자"}


def test_precedence_deterministic_over_watchlist_over_toss(tmp_path, monkeypatch):
    state = tmp_path / "state"
    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(
        json.dumps({"symbols": [
            {"symbol": "AAA", "name": "워치리스트이름"},
            {"symbol": "BBB", "name": "워치리스트이름B"},
        ]}),
        encoding="utf-8",
    )

    def fake_load_name_map(cache_dir, market):
        if market == "KR":
            return {"AAA": "결정론이름"}
        return {}

    monkeypatch.setattr(sn, "load_name_map", fake_load_name_map)

    toss_calls = []

    def toss_lookup(symbol):
        toss_calls.append(symbol)
        return {"name": "토스이름"}

    resolver = sn.build_resolver(
        tmp_path / "cache", state, watchlist_paths=[wl_path],
        toss_lookup=toss_lookup, narrator=_AssertNoCallNarrator(),
    )
    out = resolver.names_for(["AAA", "BBB", "CCC"], market="KR")
    assert out["AAA"] == "결정론이름"  # 결정론이 워치리스트보다 우선
    assert out["BBB"] == "워치리스트이름B"  # 결정론에 없으면 워치리스트
    assert out["CCC"] == "토스이름"  # 둘 다 없으면 Toss
    assert toss_calls == ["CCC"]  # 이미 풀린 심볼은 Toss까지 안 간다


def test_llm_batch_called_once_with_only_unknown_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {"AAA": "결정론이름"})
    narrator = _FakeNarrator(json.dumps({"BBB": "LLM이름"}))
    resolver = sn.build_resolver(tmp_path / "cache", tmp_path / "state", narrator=narrator)
    out = resolver.names_for(["AAA", "BBB"], market="KR")
    assert out == {"AAA": "결정론이름", "BBB": "LLM이름"}
    assert len(narrator.calls) == 1
    assert "AAA" not in narrator.calls[0]
    assert "BBB" in narrator.calls[0]


def test_llm_answer_validation_rejects_junk(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    junk = {
        "TOO_LONG": "가" * 41,
        "SAME": "SAME",
        "DIGITS": "123456",
        "SENTENCE": "이것은 문장입니다.",
        "NEWLINE": "이름\n포함",
        "EMPTY": "",
        "NULLED": None,
        "NOTASTRING": 12345,
        "GOOD": "정상이름",
    }
    narrator = _FakeNarrator(json.dumps(junk))
    resolver = sn.build_resolver(tmp_path / "cache", tmp_path / "state", narrator=narrator)
    out = resolver.names_for(list(junk.keys()), market="US")
    assert out == {"GOOD": "정상이름"}


def test_llm_response_unparseable_is_swallowed(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    narrator = _FakeNarrator("이건 JSON이 아님")
    resolver = sn.build_resolver(tmp_path / "cache", tmp_path / "state", narrator=narrator)
    assert resolver.names_for(["AAA"], market="US") == {}


def test_llm_narrator_none_returns_none_response_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    narrator = _FakeNarrator(None)
    resolver = sn.build_resolver(tmp_path / "cache", tmp_path / "state", narrator=narrator)
    assert resolver.names_for(["AAA"], market="US") == {}


def test_persistence_and_provenance(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    narrator = _FakeNarrator(json.dumps({"AAA": "LLM이름"}))
    resolver = sn.build_resolver(tmp_path / "cache", state, narrator=narrator)
    resolver.names_for(["AAA"], market="US")

    names = json.loads((state / "symbol_names.json").read_text(encoding="utf-8"))
    meta = json.loads((state / "symbol_names_meta.json").read_text(encoding="utf-8"))
    assert names["AAA"] == "LLM이름"
    assert meta["AAA"]["source"] == "llm"
    assert "ts" in meta["AAA"]


def test_deterministic_overwrites_llm_on_later_run(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    (state / "symbol_names.json").write_text(json.dumps({"AAA": "LLM이름"}), encoding="utf-8")
    (state / "symbol_names_meta.json").write_text(
        json.dumps({"AAA": {"source": "llm", "ts": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {"AAA": "결정론이름"})
    resolver = sn.build_resolver(tmp_path / "cache", state, narrator=_AssertNoCallNarrator())
    out = resolver.names_for(["AAA"], market="KR")
    assert out["AAA"] == "결정론이름"

    names = json.loads((state / "symbol_names.json").read_text(encoding="utf-8"))
    meta = json.loads((state / "symbol_names_meta.json").read_text(encoding="utf-8"))
    assert names["AAA"] == "결정론이름"
    assert meta["AAA"]["source"] == "kind"


def test_llm_entry_never_overwrites_deterministic(tmp_path, monkeypatch):
    """결정론 소스로 이미 캐시된 항목은 재조회되지 않는다(짧은 회로) —
    나중에 결정론 사전이 실패하거나 LLM이 다른 답을 줘도 그대로 남는다."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "symbol_names.json").write_text(json.dumps({"AAA": "결정론이름"}), encoding="utf-8")
    (state / "symbol_names_meta.json").write_text(
        json.dumps({"AAA": {"source": "kind", "ts": "2026-01-01T00:00:00+00:00"}}),
        encoding="utf-8",
    )
    resolver = sn.build_resolver(
        tmp_path / "cache", state,
        toss_lookup=_AssertNoCallLookup(), narrator=_AssertNoCallNarrator(),
    )
    out = resolver.names_for(["AAA"], market="KR")
    assert out["AAA"] == "결정론이름"


def test_watchlist_symbols_path_reads_missing_file_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    resolver = sn.build_resolver(
        tmp_path / "cache", tmp_path / "state",
        watchlist_paths=[tmp_path / "does_not_exist.json"],
    )
    assert resolver.names_for(["AAA"], market="US") == {}


def test_market_inferred_when_not_given(tmp_path, monkeypatch):
    seen_markets = []

    def fake_load_name_map(cache_dir, market):
        seen_markets.append(market)
        return {}

    monkeypatch.setattr(sn, "load_name_map", fake_load_name_map)
    resolver = sn.build_resolver(tmp_path / "cache", tmp_path / "state")
    resolver.names_for(["005930", "AAPL"])
    assert set(seen_markets) == {"KR", "US"}


def test_toss_etf_ticker_as_name_falls_back_to_english_name(tmp_path, monkeypatch):
    """Toss `stock_info` 는 ETF 에서 name=티커(EWY→"EWY")를 준다(2026-09-07 EC2 실측) —
    englishName 을 쓰고, 코드=이름은 절대 이름으로 저장하지 않는다."""
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    resolver = sn.build_resolver(
        tmp_path / "cache", tmp_path / "state",
        toss_lookup=lambda s: {"symbol": s, "name": s, "englishName": "ISHARES INC MSCI SOUTH KOREA ETF"},
        narrator=_AssertNoCallNarrator(),
    )
    assert resolver.names_for(["EWY"], market="US") == {"EWY": "ISHARES INC MSCI SOUTH KOREA ETF"}
    meta = json.loads((tmp_path / "state" / "symbol_names_meta.json").read_text(encoding="utf-8"))
    assert meta["EWY"]["source"] == "toss"


def test_cached_code_equals_name_entry_is_treated_as_unknown(tmp_path, monkeypatch):
    """엔진 부팅 채움이 남긴 {"EWY": "EWY"} 캐시 항목은 '모른다'와 같다 — 뒤 단계로 넘어간다."""
    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    state = tmp_path / "state"
    state.mkdir()
    (state / "symbol_names.json").write_text(json.dumps({"EWY": "EWY"}), encoding="utf-8")
    narrator = _FakeNarrator('{"EWY": "iShares MSCI South Korea ETF"}')
    resolver = sn.build_resolver(tmp_path / "cache", state, narrator=narrator)
    assert resolver.names_for(["EWY"], market="US") == {"EWY": "iShares MSCI South Korea ETF"}
    assert len(narrator.calls) == 1


def test_watchlist_code_as_name_falls_through_to_toss(tmp_path, monkeypatch):
    """watch-add 는 모르는 티커의 name 에 코드를 그대로 넣는다(EC2 실측: EWY/FXI) — 그건 이름이 아니므로
    Toss/LLM 단계로 넘어가야 한다."""
    import yaml

    monkeypatch.setattr(sn, "load_name_map", lambda cache_dir, market: {})
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(yaml.safe_dump({"symbols": [{"symbol": "EWY", "name": "EWY"}]}), encoding="utf-8")
    resolver = sn.build_resolver(
        tmp_path / "cache", tmp_path / "state", watchlist_paths=[wl],
        toss_lookup=lambda s: {"name": s, "englishName": "ISHARES INC MSCI SOUTH KOREA ETF"},
        narrator=_AssertNoCallNarrator(),
    )
    assert resolver.names_for(["EWY"], market="US") == {"EWY": "ISHARES INC MSCI SOUTH KOREA ETF"}
