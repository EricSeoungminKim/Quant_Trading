from quant.analyze.exec_summary import build_prompt, gather_evidence, summarize


class _RecordingNarrator:
    """프롬프트를 저장하고, 사전 지정한 텍스트(또는 None)를 돌려준다."""

    def __init__(self, reply):
        self._reply = reply
        self.prompts: list[str] = []

    def narrate(self, prompt: str):
        self.prompts.append(prompt)
        return self._reply


def _digest(domestic=None, us_impact=None):
    return {"domestic": domestic or [], "us_impact": us_impact or []}


def _event(title, link, outlet="한국경제"):
    return {"title": title, "link": link, "outlet": outlet, "dup_count": 1}


# ── gather_evidence ──────────────────────────────────────────────────────


def test_gather_evidence_fetches_body_for_each_event():
    digest = _digest(domestic=[_event("삼성전자 3나노 수주", "https://a")])
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return "본문 내용입니다." * 10

    evidence = gather_evidence(digest, [], {}, {}, fetch_body=fake_fetch)
    assert calls == ["https://a"]
    assert evidence["events"][0]["title"] == "삼성전자 3나노 수주"
    assert evidence["events"][0]["outlet"] == "한국경제"
    assert evidence["events"][0]["body_excerpt"] is not None


def test_gather_evidence_body_excerpt_capped_at_600_chars():
    digest = _digest(domestic=[_event("긴 기사", "https://a")])
    evidence = gather_evidence(digest, [], {}, {}, fetch_body=lambda url: "가" * 4000)
    assert len(evidence["events"][0]["body_excerpt"]) == 600


def test_gather_evidence_failed_fetch_falls_back_to_title_only():
    digest = _digest(domestic=[_event("본문 못 가져온 기사", "https://a")])
    evidence = gather_evidence(digest, [], {}, {}, fetch_body=lambda url: None)
    ev = evidence["events"][0]
    assert ev["title"] == "본문 못 가져온 기사"
    assert ev["body_excerpt"] is None


def test_gather_evidence_caps_at_six_fetches():
    digest = _digest(domestic=[_event(f"사건{i}", f"https://{i}") for i in range(10)])
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return "본문"

    evidence = gather_evidence(digest, [], {}, {}, fetch_body=fake_fetch)
    assert len(calls) == 6
    assert len(evidence["events"]) == 6


def test_gather_evidence_empty_digest_falls_back_to_news_flow():
    """econ 큐레이션을 통과한 사건이 없어도(digest 비어있음) 근거 자체가
    아예 비지 않게 news_flow 로 폴백한다."""
    news_flow = [_event("일반지 단독 보도", "https://a", outlet="연합뉴스")]
    evidence = gather_evidence(_digest(), news_flow, {}, {}, fetch_body=lambda url: None)
    assert evidence["events"][0]["title"] == "일반지 단독 보도"


def test_gather_evidence_no_link_skips_fetch():
    digest = _digest(domestic=[{"title": "링크 없는 사건", "link": "", "outlet": "x"}])
    calls = []
    evidence = gather_evidence(
        digest, [], {}, {}, fetch_body=lambda url: calls.append(url) or "본문",
    )
    assert calls == []
    assert evidence["events"][0]["body_excerpt"] is None


def test_gather_evidence_carries_market_features():
    features = {
        "kospi_change_pct": 1.23, "kosdaq_change_pct": -0.5, "vix": 15.4,
        "foreign_net_100m_krw": 3200, "institution_net_100m_krw": -800,
    }
    evidence = gather_evidence(_digest(), [], features, {})
    assert evidence["flows"]["kospi_change_pct"] == 1.23
    assert evidence["flows"]["foreign_net_100m_krw"] == 3200


def test_gather_evidence_carries_foreign_view_top_labels():
    foreign_view = {"sectors": [{
        "name": "반도체", "net_sum": 100,
        "rows": [{"symbol": "005930", "name": "삼성전자", "label": "매수 시그널(재유입)"}],
    }]}
    evidence = gather_evidence(_digest(), [], {}, foreign_view)
    assert evidence["foreign_top"] == ["삼성전자(매수 시그널(재유입))"]


def test_gather_evidence_all_empty_inputs_returns_empty_shape():
    evidence = gather_evidence(_digest(), [], {}, {})
    assert evidence["events"] == []
    assert evidence["foreign_top"] == []
    assert all(v is None for v in evidence["flows"].values())


# ── build_prompt ─────────────────────────────────────────────────────────


def test_build_prompt_contains_market_figures():
    evidence = gather_evidence(
        _digest(domestic=[_event("삼성전자 3나노 수주", "https://a")]),
        [], {"kospi_change_pct": 1.23, "foreign_net_100m_krw": 3200}, {},
        fetch_body=lambda url: "본문",
    )
    prompt = build_prompt(evidence)
    assert "+1.23%" in prompt
    assert "+3,200억원" in prompt
    assert "삼성전자 3나노 수주" in prompt
    assert "투자" in prompt  # 투자 권유 금지 지시


def test_build_prompt_contains_body_excerpt():
    evidence = gather_evidence(
        _digest(domestic=[_event("본문 있는 기사", "https://a")]),
        [], {}, {}, fetch_body=lambda url: "이것은 기사 본문 발췌 내용이다.",
    )
    prompt = build_prompt(evidence)
    assert "이것은 기사 본문 발췌 내용이다." in prompt


def test_build_prompt_marks_three_paragraph_order():
    evidence = gather_evidence(_digest(), [], {}, {})
    prompt = build_prompt(evidence)
    assert "① 시장 흐름" in prompt
    assert "② 수급" in prompt
    assert "③ 주요 재료" in prompt


# ── summarize ────────────────────────────────────────────────────────────


def test_summarize_parses_three_paragraphs_with_source_footnotes():
    evidence = gather_evidence(
        _digest(domestic=[_event("삼성전자 3나노 수주", "https://a", outlet="한국경제")]),
        [], {"kospi_change_pct": 1.0}, {}, fetch_body=lambda url: "본문",
    )
    narrator = _RecordingNarrator(
        "① 시장 흐름: 코스피가 강세였다. [근거: 한국경제]\n\n"
        "② 수급: 외국인이 순매수했다.\n\n"
        "③ 주요 재료: 삼성전자가 수주 소식을 냈다. [근거: 한국경제]"
    )
    result = summarize(evidence, narrator)
    assert result is not None
    assert "코스피가 강세였다" in result["market"]
    assert "외국인이 순매수했다" in result["flow"]
    assert "삼성전자가 수주 소식을 냈다" in result["catalyst"]
    assert result["sources"] == ["한국경제"]


def test_summarize_returns_none_when_narrator_fails():
    evidence = gather_evidence(
        _digest(domestic=[_event("사건", "https://a")]), [], {}, {},
        fetch_body=lambda url: None,
    )
    narrator = _RecordingNarrator(None)
    assert summarize(evidence, narrator) is None


def test_summarize_returns_none_when_response_unparsable():
    evidence = gather_evidence(
        _digest(domestic=[_event("사건", "https://a")]), [], {}, {},
        fetch_body=lambda url: None,
    )
    narrator = _RecordingNarrator("그냥 한 문장짜리 응답입니다.")
    assert summarize(evidence, narrator) is None


def test_summarize_returns_none_when_evidence_completely_empty():
    """근거가 전혀 없으면(사건도 시장 지표도 없음) narrator 를 부르지 않는다."""
    evidence = gather_evidence(_digest(), [], {}, {})
    narrator = _RecordingNarrator("아무 응답")
    assert summarize(evidence, narrator) is None
    assert narrator.prompts == []


def test_summarize_returns_none_when_only_two_paragraphs():
    evidence = gather_evidence(
        _digest(domestic=[_event("사건", "https://a")]), [], {}, {},
        fetch_body=lambda url: None,
    )
    narrator = _RecordingNarrator(
        "① 시장 흐름: 문단 하나.\n\n② 수급: 문단 둘."
    )
    assert summarize(evidence, narrator) is None


def test_summarize_sources_empty_when_no_outlets():
    evidence = gather_evidence(
        _digest(domestic=[{"title": "사건", "link": "", "outlet": None}]), [], {}, {},
    )
    narrator = _RecordingNarrator(
        "① 시장 흐름: 문단.\n\n② 수급: 문단.\n\n③ 주요 재료: 문단."
    )
    result = summarize(evidence, narrator)
    assert result["sources"] == []
