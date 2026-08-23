from quant.analyze.section_advice import advise, build_prompt, gather_section_numbers


class _RecordingNarrator:
    """프롬프트를 저장하고, 사전 지정한 텍스트(또는 None)를 돌려준다."""

    def __init__(self, reply):
        self._reply = reply
        self.prompts: list[str] = []

    def narrate(self, prompt: str):
        self.prompts.append(prompt)
        return self._reply


# ── gather_section_numbers ──────────────────────────────────────────────


def test_gather_section_numbers_extracts_kr_funding_as_supply():
    numbers = gather_section_numbers(
        kr_funding={"funding": {"투자자예탁금": {"value": 50.1, "unit": "조원", "change_pct": 1.2}}},
        sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["supply"] == {
        "투자자예탁금": {"value": 50.1, "unit": "조원", "change_pct": 1.2},
    }


def test_gather_section_numbers_supply_none_when_no_funding():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["supply"] is None


def test_gather_section_numbers_extracts_sentiment_components():
    numbers = gather_section_numbers(
        kr_funding=None,
        sentiment={
            "cnn_fear_greed": {"value": 62, "rating_ko": "탐욕"},
            "naaim": {"value": 80, "label": "높음"},
            "aaii": {"bull_pct": 40, "bear_pct": 25, "spread": 15},
        },
        macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["sentiment"]["cnn_fear_greed"] == {"value": 62, "rating": "탐욕"}
    assert numbers["sentiment"]["naaim"] == {"value": 80, "label": "높음"}
    assert numbers["sentiment"]["aaii"]["spread"] == 15


def test_gather_section_numbers_sentiment_none_when_empty_dict():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment={}, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["sentiment"] is None


def test_gather_section_numbers_extracts_technical_from_vix_sectors_breadth():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None, macro=None,
        vix_term={"structure": "콘탱고 (정상)", "spread": 1.5},
        sectors={"sectors": [
            {"ticker": "XLE", "name": "에너지", "change_pct": 2.0},
            {"ticker": "XLU", "name": "유틸리티", "change_pct": -1.0},
        ]},
        breadth={"highs": {"count": 20}, "lows": {"count": 3}, "universe": 500},
    )
    tech = numbers["technical"]
    assert tech["vix_structure"] == "콘탱고 (정상)"
    assert tech["top_sector"] == {"name": "에너지", "change_pct": 2.0}
    assert tech["bottom_sector"] == {"name": "유틸리티", "change_pct": -1.0}
    assert tech["highs"] == 20 and tech["lows"] == 3 and tech["universe"] == 500


def test_gather_section_numbers_technical_none_when_all_three_missing():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["technical"] is None


def test_gather_section_numbers_extracts_liquidity_from_macro():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None,
        macro={
            "net_liquidity": {"change": 12345.0},
            "nfci_label": "완화",
            "curve": {"spread": -0.15, "label": "역전"},
        },
        vix_term=None, sectors=None, breadth=None,
    )
    liq = numbers["liquidity"]
    assert liq["net_liquidity_change"] == 12345.0
    assert liq["nfci_label"] == "완화"
    assert liq["curve_spread"] == -0.15 and liq["curve_label"] == "역전"


def test_gather_section_numbers_liquidity_none_when_no_macro():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    assert numbers["liquidity"] is None


# ── build_prompt ─────────────────────────────────────────────────────────


def test_build_prompt_only_includes_present_sections():
    numbers = gather_section_numbers(
        kr_funding={"funding": {"투자자예탁금": {"value": 50.1, "unit": "조원", "change_pct": 1.2}}},
        sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    prompt = build_prompt(numbers)
    assert "[한국 수급 체력]" in prompt
    assert "[시장 심리]" not in prompt
    assert "[기술적 지표]" not in prompt
    assert "[유동성·금리]" not in prompt


def test_build_prompt_forbids_directive_and_profit_promise_language():
    numbers = gather_section_numbers(
        kr_funding={"funding": {"a": {"value": 1, "unit": "조원", "change_pct": 1}}},
        sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    prompt = build_prompt(numbers)
    assert "단정적 지시" in prompt
    assert "조건부 서술" in prompt


# ── advise ───────────────────────────────────────────────────────────────


def _numbers_all_present():
    return gather_section_numbers(
        kr_funding={"funding": {"투자자예탁금": {"value": 50.1, "unit": "조원", "change_pct": 1.2}}},
        sentiment={"cnn_fear_greed": {"value": 62, "rating_ko": "탐욕"}},
        macro={"nfci_label": "완화"},
        vix_term={"structure": "콘탱고 (정상)", "spread": 1.5},
        sectors=None, breadth=None,
    )


def test_advise_parses_all_present_sections_and_appends_disclaimer():
    numbers = _numbers_all_present()
    narrator = _RecordingNarrator(
        "[한국 수급 체력]\n대기자금이 늘고 있는 국면에서는 매수 여력이 커진다.\n\n"
        "[시장 심리]\n탐욕 구간에서는 추격매수보다 분할 접근이 일반적 대응이다.\n\n"
        "[기술적 지표]\n콘탱고 구간에서는 변동성 급등 가능성이 낮게 읽힌다.\n\n"
        "[유동성·금리]\n완화 국면에서는 위험자산에 우호적으로 해석되는 경우가 많다."
    )
    result = advise(numbers, narrator)
    assert result is not None
    assert "대기자금이 늘고 있는 국면" in result["supply"]
    assert "판단과 책임은 투자자 본인 몫이다." in result["supply"]
    assert "탐욕 구간에서는" in result["sentiment"]
    assert "콘탱고 구간에서는" in result["technical"]
    assert "완화 국면에서는" in result["liquidity"]


def test_advise_returns_none_when_all_sections_missing():
    numbers = gather_section_numbers(
        kr_funding=None, sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    narrator = _RecordingNarrator("아무 응답")
    assert advise(numbers, narrator) is None
    assert narrator.prompts == []


def test_advise_returns_none_when_narrator_fails():
    narrator = _RecordingNarrator(None)
    assert advise(_numbers_all_present(), narrator) is None


def test_advise_returns_none_when_response_missing_a_requested_marker():
    numbers = _numbers_all_present()
    narrator = _RecordingNarrator(
        "[한국 수급 체력]\n문단.\n\n[시장 심리]\n문단.\n\n[기술적 지표]\n문단."
        # [유동성·금리] 마커 누락
    )
    assert advise(numbers, narrator) is None


def test_advise_returns_none_when_a_section_content_is_empty():
    numbers = _numbers_all_present()
    narrator = _RecordingNarrator(
        "[한국 수급 체력]\n\n[시장 심리]\n문단.\n\n[기술적 지표]\n문단.\n\n[유동성·금리]\n문단."
    )
    assert advise(numbers, narrator) is None


def test_advise_only_returns_keys_for_sections_that_were_present():
    numbers = gather_section_numbers(
        kr_funding={"funding": {"투자자예탁금": {"value": 50.1, "unit": "조원", "change_pct": 1.2}}},
        sentiment=None, macro=None, vix_term=None, sectors=None, breadth=None,
    )
    narrator = _RecordingNarrator("[한국 수급 체력]\n대기자금 문단이다.")
    result = advise(numbers, narrator)
    assert result is not None
    assert set(result.keys()) == {"supply"}
