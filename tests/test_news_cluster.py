from quant.analyze.news_cluster import cluster_titles, dedup_with_counts


def _items(*titles):
    return [{"title": t, "link": f"https://x/{i}"} for i, t in enumerate(titles)]


def test_same_event_different_outlets_cluster_together():
    """같은 사건을 쉼표/말줄임표 등 다른 구두점으로 쓴 두 매체 제목은 묶인다
    (실측 사례: SK텔레콤-앤트로픽 지분가치 보도, 2026-08-16)."""
    items = _items(
        "SK텔레콤, 앤트로픽 지분가치 기대감에 급등",
        "SK텔레콤 주가 급등…앤트로픽 지분가치 기대",
    )
    assert cluster_titles(items) == [[0, 1]]


def test_different_events_do_not_cluster():
    items = _items(
        "삼성전자, 3나노 파운드리 수주 확대",
        "카카오, 신규 AI 서비스 출시 예고",
    )
    assert cluster_titles(items) == [[0], [1]]


def test_threshold_boundary_inclusive_and_exclusive():
    """자카드 정확히 0.5인 두 제목 — threshold=0.5 면 묶이고(경계 포함),
    threshold=0.51 이면 안 묶인다."""
    # "aabb" shingles={aa,ab,bb}, "aabc" shingles={aa,ab,bc} → 교집합2/합집합4=0.5
    items = _items("aabb", "aabc")
    assert cluster_titles(items, threshold=0.5) == [[0, 1]]
    assert cluster_titles(items, threshold=0.51) == [[0], [1]]


def test_order_determinism_greedy_matches_first_cluster_representative():
    """세 번째 항목이 첫 항목과도, 두 번째 항목과도 threshold 이상이면 먼저
    열린(첫) 클러스터에 합류한다 — 입력 순서가 결과를 결정한다."""
    # a="aabb", b="aabc" 는 서로 0.5(교집합2/합집합4). c="aabd" 는 a와도
    # b와도 각각 자카드 0.5 — a가 먼저 클러스터를 열었으므로 c는 a의 클러스터로.
    items = _items("aabb", "aabc", "aabd")
    assert cluster_titles(items, threshold=0.5) == [[0, 1, 2]]


def test_empty_input_returns_empty():
    assert cluster_titles([]) == []
    assert dedup_with_counts([]) == []


def test_dedup_with_counts_representative_is_latest_published():
    items = [
        {"title": "SK텔레콤, 앤트로픽 지분가치 기대감에 급등",
         "link": "https://a", "published": "2026-08-16T09:00:00+09:00"},
        {"title": "SK텔레콤 주가 급등…앤트로픽 지분가치 기대",
         "link": "https://b", "published": "2026-08-16T10:30:00+09:00"},
    ]
    out = dedup_with_counts(items)
    assert len(out) == 1
    assert out[0]["link"] == "https://b"  # 더 늦게 발행된 쪽이 대표
    assert out[0]["dup_count"] == 2


def test_dedup_with_counts_falls_back_to_first_when_published_missing():
    items = [
        {"title": "SK텔레콤, 앤트로픽 지분가치 기대감에 급등", "link": "https://a"},
        {"title": "SK텔레콤 주가 급등…앤트로픽 지분가치 기대",
         "link": "https://b", "published": "2026-08-16T10:30:00+09:00"},
    ]
    out = dedup_with_counts(items)
    assert len(out) == 1
    assert out[0]["link"] == "https://a"  # published 누락 항목이 섞이면 첫 항목으로 폴백
    assert out[0]["dup_count"] == 2


def test_dedup_with_counts_falls_back_to_first_when_published_unparsable():
    items = [
        {"title": "SK텔레콤, 앤트로픽 지분가치 기대감에 급등",
         "link": "https://a", "published": "not-a-date"},
        {"title": "SK텔레콤 주가 급등…앤트로픽 지분가치 기대",
         "link": "https://b", "published": "2026-08-16T10:30:00+09:00"},
    ]
    out = dedup_with_counts(items)
    assert out[0]["link"] == "https://a"


def test_dedup_with_counts_singleton_gets_dup_count_one():
    items = _items("삼성전자, 3나노 파운드리 수주 확대")
    out = dedup_with_counts(items)
    assert out == [{**items[0], "dup_count": 1}]


def test_dedup_with_counts_representatives_keep_cluster_first_input_order():
    """클러스터가 여럿일 때, 대표들의 순서는 각 클러스터의 첫 항목이 입력에
    등장한 순서를 따른다."""
    items = _items(
        "삼성전자, 3나노 파운드리 수주 확대",   # cluster A 시작
        "카카오, 신규 AI 서비스 출시 예고",       # cluster B 시작
        "삼성전자 3나노 파운드리 수주 확대…",     # cluster A 합류
    )
    out = dedup_with_counts(items)
    assert [o["link"] for o in out] == ["https://x/0", "https://x/1"]
    assert out[0]["dup_count"] == 2
    assert out[1]["dup_count"] == 1


# ------------------------------------------------------- 완전중복 전처리 (2026-08-17 실측)
#
# 실측 자료: data/news/KR/2026-08-15.jsonl (942건). 아래 제목/링크는 실제
# 수집된 값 그대로다 — 합성 텍스트가 아니다.


def test_exact_title_different_link_merges_across_outlets():
    """실측 사례: 한국경제 직링크와 구글뉴스 RSS 프록시 링크가 같은 기사를
    다른 링크로 각각 수집했고, 제목은 아스키 따옴표(') vs 유니코드 따옴표(')
    차이만 있다 — 정규화하면 완전히 같다. 자카드 1.0(임계값 0.6 통과)으로도
    이미 잡히지만, 완전중복 전처리가 이 경로를 자카드 점수와 무관하게
    보장한다."""
    items = [
        {"title": "26일 '인베스터 데이' 여는 현대차…자사주 매입 발표 가능성은",
         "link": "https://www.hankyung.com/article/202608142047i"},
        {"title": "26일 '인베스터 데이' 여는 현대차…자사주 매입 발표 가능성은",
         "link": "https://news.google.com/rss/articles/CBMiWkFVX3lxTE5Wb0RKbXlrZ1g3aWxhZkpjT3lpRURnVVhaTWllOXRnQkVjRUJQVGZMeWw2WWJ2QnBnT0I3YURVbzZzWkt6aFdrbDhxSHBuempvMU1VR2g1XzVyZw?oc=5"},
    ]
    out = dedup_with_counts(items)
    assert len(out) == 1
    assert out[0]["dup_count"] == 2


def test_link_equality_merges_even_when_title_jaccard_below_threshold():
    """완전중복 전처리의 실제 가치: 링크가 같으면 제목이 자카드 임계값을
    통과하지 못할 만큼 크게 갱신돼도(예: 속보 제목이 나중에 전혀 다른
    요약으로 바뀌는 경우) 여전히 합친다. 2026-08-15 실측에서는 이 패턴(같은
    정규화 링크 + 임계값 미만 제목)이 관측되지 않았다 — 수집 단계 저장소가
    이미 정규화 링크로 키를 잡기 때문(quant/collect/collector.py). 하지만
    렌더 단계의 종목별 제목 목록(quant/analyze/mentions.py)은 원본 링크
    기준으로만 중복을 걸러 이 경로가 열려 있어 방어적으로 넣는다."""
    items = [
        {"title": "코스피 2.4%↑, 7천피 턱밑까지…이번주 내내 올랐다(종합)",
         "link": "https://example.com/article/123?utm_source=rss"},
        {"title": "완전히 다른 후속 요약으로 갱신된 제목입니다",
         "link": "https://example.com/article/123"},
    ]
    from quant.analyze.news_cluster import cluster_titles, _jaccard, _shingles
    assert _jaccard(_shingles(items[0]["title"]), _shingles(items[1]["title"])) < 0.6
    assert cluster_titles(items) == [[0], [1]]  # 자카드만으로는 안 묶인다
    out = dedup_with_counts(items)
    assert len(out) == 1  # 링크 동일성 전처리가 합친다
    assert out[0]["dup_count"] == 2


def test_different_companies_same_earnings_template_do_not_merge():
    """실측 오탐 사례(자카드 0.457): DB증권과 iM증권의 상반기 실적 발표
    제목이 같은 템플릿("OO증권, 상반기 순이익 XXX원…전년比 YY%")을 써서
    자카드 점수가 근접-미스 구간(0.35~0.6)에 들어오지만 회사도, 금액도,
    실적 방향(증가 vs 감소)도 다른 별개 사건이다 — threshold 를 낮추지
    않은 근거."""
    items = [
        {"title": "DB증권, 상반기 순이익 708억원…전년比 49.4%↑",
         "link": "https://www.hankyung.com/article/2026081416856"},
        {"title": "iM증권, 상반기 순이익 426억원…전년比 21.2% 감소",
         "link": "https://www.chosun.com/economy/stock-finance/2026/08/14/HAYTQOLEMY3DMMZVGRQTGNTCGU/"},
    ]
    out = dedup_with_counts(items)
    assert len(out) == 2
    assert all(o["dup_count"] == 1 for o in out)


def test_different_episodes_of_same_show_do_not_merge():
    """실측 오탐 사례(자카드 0.500): "출발증시 1부"/"출발증시 2부" — 같은
    방송의 다른 회차이지 같은 사건이 아니다. 링크도 다르므로 완전중복
    전처리도 합치지 않는다."""
    items = [
        {"title": "출발증시 2부",
         "link": "https://news.google.com/rss/articles/CBMiowFBVV95cUxNRlplTVloRldUenZucjZzYXpfcEtrYVhwNGhQOU0ySmMyUXlOR2dPdk9ISS1ydEZ6UTJteU1XbTFHTnVtajVyZm53QVRpaXRzMDlOMHpOUFZrSTgxdlN0OUZjNG5MdGEtR1FVa2VfRDRmY3lQYmhPckQ3QVc0NUVEWmNSQXc2cS1lMy1oRndxWFdhUE1HVWIwd0ptaWRqRThfTWo4?oc=5"},
        {"title": "출발증시 1부",
         "link": "https://news.google.com/rss/articles/CBMiowFBVV95cUxQSEd1c3k4R0RJcVJwMHNUcDhSNVZ5RFZwWWE4ak4zeEMzelAyNS1pTWFSdlcyWmtseC1GLVNGeWpJQnR3c0Vrc2tPeVFmOXRJOHZJR2RMQVRFUWlLd2t1TEJQd3otbjRnMXNiUXdzSS1YTGZ0dnowZzFidkJlWENGX0V2dVFFOTd4UUJvZDh2XzVXWVl1ZmYyaGQ2dDFyVGV1VDJv?oc=5"},
    ]
    out = dedup_with_counts(items)
    assert len(out) == 2
