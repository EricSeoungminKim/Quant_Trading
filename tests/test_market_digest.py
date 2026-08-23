from quant.analyze.market_digest import (
    STOCK_KEYWORDS, build_digest, build_news_flow, summarize_digest,
)


def _item(title, link, published=None, outlet=""):
    return {"title": title, "link": link, "published": published, "outlet": outlet}


def test_empty_feeds_returns_empty_lists_honestly():
    assert build_digest({}, "KR") == {"domestic": [], "us_impact": []}


def test_feeds_with_only_empty_lists_returns_empty():
    assert build_digest({"한국경제_경제": [], "구글_코스피": []}, "KR") == {
        "domestic": [], "us_impact": [],
    }


def test_domestic_and_us_impact_split_by_keyword():
    feeds = {
        "한국경제_경제": [
            _item("삼성전자, 3나노 파운드리 수주 확대", "https://a", outlet="한국경제"),
        ],
        "구글_증시": [
            _item("연준 파월 금리 동결 시사에 나스닥 급등", "https://b", outlet="매일경제"),
        ],
    }
    out = build_digest(feeds, "KR")
    assert [i["title"] for i in out["domestic"]] == ["삼성전자, 3나노 파운드리 수주 확대"]
    assert [i["title"] for i in out["us_impact"]] == ["연준 파월 금리 동결 시사에 나스닥 급등"]


def test_multi_outlet_duplicate_ranked_above_single_outlet_event():
    """다매체 중복 사건(다매체 = 큰 사건)이 dup_count 내림차순으로 앞에 온다."""
    feeds = {
        "한국경제_경제": [
            _item("카카오, 신규 AI 서비스 출시 예고", "https://single", outlet="한국경제"),
            _item("SK텔레콤, 앤트로픽 지분가치 기대감에 급등", "https://a",
                  published="2026-08-17T09:00:00+09:00", outlet="한국경제"),
        ],
        "매일경제_경제": [
            _item("SK텔레콤 주가 급등…앤트로픽 지분가치 기대", "https://b",
                  published="2026-08-17T10:00:00+09:00", outlet="매일경제"),
        ],
    }
    out = build_digest(feeds, "KR")
    titles = [i["title"] for i in out["domestic"]]
    assert titles[0] == "SK텔레콤 주가 급등…앤트로픽 지분가치 기대"  # dup_count=2, 대표는 최신
    assert out["domestic"][0]["dup_count"] == 2
    assert out["domestic"][1]["dup_count"] == 1


def test_events_across_feeds_are_clustered_together():
    """같은 사건이 서로 다른 피드(매체)에 실려도 dedup_with_counts 가 하나로
    묶는다 — market_digest 는 전체 피드를 합쳐서 넘겨야 한다."""
    feeds = {
        "한국경제_경제": [
            _item("SK텔레콤, 앤트로픽 지분가치 기대감에 급등", "https://a", outlet="한국경제"),
        ],
        "구글_코스피": [
            _item("SK텔레콤 주가 급등…앤트로픽 지분가치 기대", "https://b", outlet="구글뉴스"),
        ],
    }
    out = build_digest(feeds, "KR")
    assert len(out["domestic"]) == 1
    assert out["domestic"][0]["dup_count"] == 2


def test_top_n_caps_at_eight():
    # 클러스터링(자카드 유사도)이 서로 뭉치지 않도록 제목을 완전히 다르게 만든다
    # — 그래야 12건이 12개의 서로 다른 사건으로 남아 TOP_N 자름을 검증할 수 있다.
    distinct_titles = [
        "삼성전자 실적 발표", "카카오 신규 서비스 출시", "현대차 판매량 급증",
        "LG에너지솔루션 배터리 수주", "네이버 클라우드 사업 확대", "포스코 철강 가격 인상",
        "셀트리온 바이오시밀러 승인", "한화 방산 수출 계약", "SK하이닉스 반도체 증설",
        "KB금융 배당 확대", "롯데케미칼 설비 투자", "두산에너빌리티 원전 수주",
    ]
    feeds = {
        "한국경제_경제": [
            _item(t, f"https://{i}", outlet="한국경제") for i, t in enumerate(distinct_titles)
        ],
    }
    out = build_digest(feeds, "KR")
    assert len(out["domestic"]) + len(out["us_impact"]) == 8


def test_outlet_included_only_when_present():
    feeds = {
        "구글_코스피": [_item("코스피 상승 마감", "https://a", outlet="한국경제")],
        "한국경제_경제": [_item("코스닥 하락 마감", "https://b", outlet="한국경제")],
    }
    out = build_digest(feeds, "KR")
    by_title = {i["title"]: i for i in out["domestic"]}
    assert by_title["코스피 상승 마감"]["outlet"] == "한국경제"
    # 원본 outlet 이 "" 인 항목은 econ 큐레이션(자기 outlet econ 아님, 클러스터
    # 멤버도 econ 아님)에서 애초에 탈락한다 — 남는 항목에만 outlet 유무를 검증.


# ── 경제지 큐레이션(스펙 §2) ───────────────────────────────────────────────


def test_non_econ_outlet_event_is_dropped():
    """일반지(연합뉴스)만 보도한 사건은 다이제스트 후보가 아니다 — 사용자
    피드백("아무 신문이나 들어간다")의 핵심 케이스."""
    feeds = {
        "연합뉴스_경제": [_item("일반지 단독 보도 사건", "https://a", outlet="연합뉴스")],
    }
    out = build_digest(feeds, "KR")
    assert out == {"domestic": [], "us_impact": []}


def test_econ_outlet_event_is_kept():
    feeds = {
        "한국경제_경제": [_item("경제지 단독 보도 사건", "https://a", outlet="한국경제")],
    }
    out = build_digest(feeds, "KR")
    assert [i["title"] for i in out["domestic"]] == ["경제지 단독 보도 사건"]


def test_cluster_member_econ_outlet_rescues_non_econ_representative():
    """같은 사건을 연합뉴스(일반지)와 한국경제(경제지)가 같이 보도했고, 대표로
    뽑히는 쪽(최신 published)이 연합뉴스여도 — 클러스터에 econ 매체가 있으면
    사건 전체가 큐레이션을 통과해야 한다(스펙 §2: "대표 OR 클러스터 멤버")."""
    feeds = {
        "연합뉴스_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대", "https://a",
                  published="2026-08-17T10:00:00+09:00", outlet="연합뉴스"),
        ],
        "한국경제_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대 소식", "https://b",
                  published="2026-08-17T09:00:00+09:00", outlet="한국경제"),
        ],
    }
    out = build_digest(feeds, "KR")
    titles = [i["title"] for i in out["domestic"]]
    assert titles == ["삼성전자 3나노 파운드리 수주 확대"]  # 대표는 연합뉴스(최신)지만 살아남는다
    assert out["domestic"][0]["dup_count"] == 2


def test_outlet_containing_econ_name_as_substring_matches():
    """"한국경제TV"는 "한국경제"를 부분 문자열로 포함하므로 econ 으로 친다."""
    feeds = {
        "한국경제TV_증권": [_item("경제TV 단독 사건", "https://a", outlet="한국경제TV")],
    }
    out = build_digest(feeds, "KR")
    assert [i["title"] for i in out["domestic"]] == ["경제TV 단독 사건"]


def test_mixed_econ_and_non_econ_events_only_econ_survives():
    feeds = {
        "한국경제_경제": [_item("경제지 사건", "https://a", outlet="한국경제")],
        "동아일보_경제": [_item("일반지만의 사건", "https://b", outlet="동아일보")],
    }
    out = build_digest(feeds, "KR")
    titles = [i["title"] for i in out["domestic"]]
    assert titles == ["경제지 사건"]


# ── LLM 요약(스펙 §2, summarize_digest) ────────────────────────────────────


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


def test_summarize_digest_prompt_includes_titles_and_counts():
    digest = _digest(
        domestic=[{"title": "삼성전자 3나노 수주", "link": "https://a", "dup_count": 1}],
        us_impact=[{"title": "연준 금리 동결", "link": "https://b", "dup_count": 1}],
    )
    narrator = _RecordingNarrator(
        "① 국내 시장 요약: 삼성전자가 수주 소식을 냈다.\n\n"
        "② 미국발 재료가 한국 증시에 주는 영향: 연준 금리 동결이 부담을 덜었다.",
    )
    summarize_digest(digest, narrator)
    assert len(narrator.prompts) == 1
    prompt = narrator.prompts[0]
    assert "삼성전자 3나노 수주" in prompt
    assert "연준 금리 동결" in prompt
    assert "국내 1건" in prompt
    assert "미국발 1건" in prompt
    assert "투자" in prompt  # 투자 권유 금지 지시가 프롬프트에 있어야 한다


def test_summarize_digest_parses_two_paragraphs():
    digest = _digest(domestic=[{"title": "사건1", "link": "https://a", "dup_count": 1}])
    narrator = _RecordingNarrator(
        "① 국내 시장 요약: 오늘 국내 증시는 사건1로 움직였다.\n\n"
        "② 미국발 재료가 한국 증시에 주는 영향: 오늘은 미국발 재료가 크지 않았다.",
    )
    out = summarize_digest(digest, narrator)
    assert out == {
        "domestic_prose": "오늘 국내 증시는 사건1로 움직였다.",
        "us_prose": "오늘은 미국발 재료가 크지 않았다.",
    }


def test_summarize_digest_returns_none_when_narrator_fails():
    digest = _digest(domestic=[{"title": "사건1", "link": "https://a", "dup_count": 1}])
    narrator = _RecordingNarrator(None)
    assert summarize_digest(digest, narrator) is None


def test_summarize_digest_returns_none_when_response_unparsable():
    """빈 줄도, ①/② 마커도 없는 한 덩어리 텍스트는 두 문단으로 쪼갤 수 없다."""
    digest = _digest(domestic=[{"title": "사건1", "link": "https://a", "dup_count": 1}])
    narrator = _RecordingNarrator("그냥 한 문장짜리 응답입니다.")
    assert summarize_digest(digest, narrator) is None


def test_summarize_digest_returns_none_when_digest_is_empty():
    """다이제스트가 비어 있으면 narrator 를 부르지 않고 바로 None."""
    narrator = _RecordingNarrator("아무 응답")
    assert summarize_digest(_digest(), narrator) is None
    assert narrator.prompts == []


# ── 오늘의 뉴스 흐름(리포트 UX 2차 요구 1, build_news_flow) ─────────────────
#
# build_digest 와 짝을 이루는 반대 극단이다 — econ 큐레이션도 TOP_N 자름도
# 없다(diversity 가 목적). 그룹핑은 같은 dedup_with_counts 를 쓴다.


def test_news_flow_empty_feeds_returns_empty_list():
    assert build_news_flow({}) == []


def test_news_flow_feeds_with_only_empty_lists_returns_empty():
    assert build_news_flow({"한국경제_경제": [], "구글_코스피": []}) == []


def test_news_flow_includes_non_econ_outlet_unlike_digest():
    """econ 큐레이션이 없다 — build_digest 라면 탈락했을 일반지(연합뉴스)
    단독 보도 사건도 포함돼야 한다(스펙 §1 핵심 차이: "다양성이 목적")."""
    feeds = {
        "연합뉴스_경제": [_item("일반지 단독 보도 사건", "https://a", outlet="연합뉴스")],
    }
    out = build_news_flow(feeds)
    assert [i["title"] for i in out] == ["일반지 단독 보도 사건"]


def test_news_flow_groups_events_across_feeds():
    feeds = {
        "한국경제_경제": [
            _item("SK텔레콤, 앤트로픽 지분가치 기대감에 급등", "https://a", outlet="한국경제"),
        ],
        "구글_코스피": [
            _item("SK텔레콤 주가 급등…앤트로픽 지분가치 기대", "https://b", outlet="구글뉴스"),
        ],
    }
    out = build_news_flow(feeds)
    assert len(out) == 1
    assert out[0]["dup_count"] == 2


def test_news_flow_sorted_by_dup_count_desc_then_published_desc():
    feeds = {
        "한국경제_경제": [
            _item("카카오, 신규 AI 서비스 출시 예고", "https://single",
                  published="2026-08-17T11:00:00+09:00", outlet="아무매체"),
            _item("SK텔레콤, 앤트로픽 지분가치 기대감에 급등", "https://a",
                  published="2026-08-17T09:00:00+09:00", outlet="한국경제"),
        ],
        "매일경제_경제": [
            _item("SK텔레콤 주가 급등…앤트로픽 지분가치 기대", "https://b",
                  published="2026-08-17T10:00:00+09:00", outlet="매일경제"),
        ],
    }
    out = build_news_flow(feeds)
    # dup_count=2 사건이 dup_count=1 보다 먼저 온다(같은 사건 다매체 = 큰 사건).
    assert out[0]["title"] == "SK텔레콤 주가 급등…앤트로픽 지분가치 기대"
    assert out[0]["dup_count"] == 2
    assert out[1]["title"] == "카카오, 신규 AI 서비스 출시 예고"
    assert out[1]["dup_count"] == 1


def test_news_flow_ties_broken_by_published_desc():
    feeds = {
        "한국경제_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대", "https://a",
                  published="2026-08-17T08:00:00+09:00", outlet="한국경제"),
        ],
        "매일경제_경제": [
            _item("카카오 신규 AI 서비스 출시 예고", "https://b",
                  published="2026-08-17T15:30:00+09:00", outlet="매일경제"),
        ],
    }
    out = build_news_flow(feeds)
    assert [i["title"] for i in out] == [
        "카카오 신규 AI 서비스 출시 예고", "삼성전자 3나노 파운드리 수주 확대",
    ]


def test_news_flow_limit_caps_output():
    feeds = {
        "한국경제_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대", "https://a", outlet="한국경제"),
            _item("카카오 신규 AI 서비스 출시 예고", "https://b", outlet="한국경제"),
            _item("현대차 판매량 급증 발표", "https://c", outlet="한국경제"),
        ],
    }
    out = build_news_flow(feeds, limit=2)
    assert len(out) == 2


def test_news_flow_entry_shape_includes_outlet_and_dup_count():
    feeds = {
        "한국경제_경제": [_item("삼성전자 3나노 파운드리 수주 확대", "https://a", outlet="한국경제")],
    }
    out = build_news_flow(feeds)
    assert out[0]["title"] == "삼성전자 3나노 파운드리 수주 확대"
    assert out[0]["link"] == "https://a"
    assert out[0]["outlet"] == "한국경제"
    assert out[0]["dup_count"] == 1


def test_news_flow_outlet_empty_string_when_missing():
    feeds = {"구글_코스피": [_item("출처 없는 사건", "https://a", outlet="")]}
    out = build_news_flow(feeds)
    assert out[0]["outlet"] == ""


def test_news_flow_published_hhmm_present_when_parseable():
    feeds = {
        "한국경제_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대", "https://a",
                  published="2026-08-17T09:12:00+09:00", outlet="한국경제"),
        ],
    }
    out = build_news_flow(feeds)
    assert out[0]["published_hhmm"] == "09:12"


def test_news_flow_published_hhmm_absent_when_missing():
    feeds = {"한국경제_경제": [_item("삼성전자 3나노 파운드리 수주 확대", "https://a", outlet="한국경제")]}
    out = build_news_flow(feeds)
    assert "published_hhmm" not in out[0]


def test_news_flow_published_hhmm_absent_when_unparseable():
    feeds = {
        "한국경제_경제": [
            _item("삼성전자 3나노 파운드리 수주 확대", "https://a",
                  published="어제쯤", outlet="한국경제"),
        ],
    }
    out = build_news_flow(feeds)
    assert "published_hhmm" not in out[0]


# ── 뉴스 흐름 품질 — 증권 위/종합지 강등 (스펙 §L-2, 2026-08-17) ──────────
#
# "아무 신문이나 들어간다"는 피드백에 대한 순서 조정 — build_digest 의
# econ 큐레이션(탈락은 소실)과 달리 여기는 제거하지 않고 순서만 바꾼다.
# 종합지 비증권 기사는 여전히 build_news_flow 결과에 남고(<details> 로
# 강등되는 건 템플릿 앞 12건 컷 몫), 이 테스트는 정렬 순서만 검증한다.


def test_stock_keywords_constant_covers_spec_terms():
    for kw in ("증시", "주가", "코스피", "코스닥", "상장", "실적", "공모", "투자", "반도체"):
        assert kw in STOCK_KEYWORDS


def test_news_flow_econ_outlet_ranks_above_general_outlet_even_when_older():
    """econ 매체(한국경제) 사건이 종합지(경향신문) 사건보다 먼저 온다 —
    dup_count 동률에 발행시각도 종합지 쪽이 더 최신인데도 순위가 뒤집힌다."""
    feeds = {
        "경향신문_경제": [
            _item("종합지 단독 시사 기사", "https://a",
                  published="2026-08-17T15:00:00+09:00", outlet="경향신문"),
        ],
        "한국경제_경제": [
            _item("한국경제가 다룬 증권 소식", "https://b",
                  published="2026-08-17T08:00:00+09:00", outlet="한국경제"),
        ],
    }
    out = build_news_flow(feeds)
    assert [i["title"] for i in out] == [
        "한국경제가 다룬 증권 소식", "종합지 단독 시사 기사",
    ]


def test_news_flow_stock_keyword_title_ranks_above_general_outlet_even_from_non_econ_outlet():
    """econ 매체가 아니어도 제목에 증시 키워드가 있으면 우선한다 — 매체
    단위(ECON_OUTLETS)가 아니라 제목 단위 판정임을 확인."""
    feeds = {
        "동아일보_경제": [
            _item("동아일보 정치 기사", "https://a", outlet="동아일보"),
        ],
        "ZDNet코리아": [
            _item("코스피 급등, 반도체주 강세", "https://b", outlet="ZDNet코리아"),
        ],
    }
    out = build_news_flow(feeds)
    assert out[0]["title"] == "코스피 급등, 반도체주 강세"
    assert out[1]["title"] == "동아일보 정치 기사"


def test_news_flow_general_outlet_events_not_dropped_only_reordered():
    """제거가 아니라 순서만 바뀐다 — 비증권 종합지 사건도 결과에 그대로 남는다."""
    feeds = {
        "경향신문_경제": [_item("종합지 단독 시사 기사", "https://a", outlet="경향신문")],
        "한국경제_경제": [_item("한국경제가 다룬 증권 소식", "https://b", outlet="한국경제")],
    }
    out = build_news_flow(feeds)
    assert len(out) == 2
    assert {i["title"] for i in out} == {"종합지 단독 시사 기사", "한국경제가 다룬 증권 소식"}


def test_news_flow_relevance_outranks_dup_count():
    """증권·경제 관련 여부가 다매체 보도량보다 먼저 온다 — 정렬 키 순서 확인."""
    feeds = {
        "경향신문_경제": [
            _item("종합지 다매체 사건 A", "https://a", outlet="경향신문"),
        ],
        "동아일보_경제": [
            _item("종합지 다매체 사건 A(동아)", "https://b", outlet="동아일보"),
        ],
        "한국경제_경제": [
            _item("증권 단독 사건 B", "https://c", outlet="한국경제"),
        ],
    }
    # "종합지 다매체 사건 A" 계열은 두 매체가 다뤄 dup_count=2 지만, 증권
    # 관련(econ outlet)인 "증권 단독 사건 B"(dup_count=1)가 그래도 앞선다.
    out = build_news_flow(feeds)
    assert out[0]["title"] == "증권 단독 사건 B"
    assert out[0]["dup_count"] == 1
