from quant.analyze.themes import MIN_COUNT, THEMES, cluster, summarize


def test_headlines_are_bucketed_by_keyword():
    titles = ["Fed holds rates steady", "Powell signals caution on rate cut"]
    out = cluster(titles, "US", min_count=1)
    assert out[0]["theme"] == "연준·금리" and out[0]["count"] == 2


def test_one_headline_can_belong_to_several_themes():
    """'Fed's decision hits chip stocks' 는 연준이자 반도체다 — 배타 분류는 왜곡이다."""
    out = cluster(["Fed rate decision hits chip stocks"], "US", min_count=1)
    themes = {c["theme"] for c in out}
    assert {"연준·금리", "AI·반도체"} <= themes


def test_single_occurrence_is_noise_not_a_theme():
    out = cluster(["Bitcoin rallies"], "US")
    assert out == []
    assert cluster(["Bitcoin rallies"], "US", min_count=1)[0]["theme"] == "암호화폐"


def test_keyword_needs_word_boundary():
    """'ai' 가 'said'/'chain' 안에서 잡히면 집계가 통째로 무의미해진다."""
    out = cluster(["He said the supply chain remains fragile"], "US", min_count=1)
    assert "AI·반도체" not in {c["theme"] for c in out}


def test_korean_keywords_match_without_word_boundary():
    """한글은 \\b 가 동작하지 않는다 — 포함 검사로 잡아야 한다."""
    out = cluster(["삼성전자 반도체 수출 급증"], "KR", min_count=1)
    assert out[0]["theme"] == "반도체"


def test_clusters_sorted_by_count_desc():
    titles = ["Fed rate", "Powell yield", "hawkish fed", "oil rises", "crude up"]
    out = cluster(titles, "US")
    assert [c["count"] for c in out] == sorted([c["count"] for c in out], reverse=True)


def test_share_pct_is_relative_to_total_headlines():
    out = cluster(["Fed rate", "Fed cut", "oil up", "gold down"], "US", min_count=2)
    fed = next(c for c in out if c["theme"] == "연준·금리")
    assert fed["count"] == 2 and fed["share_pct"] == 50.0


def test_samples_are_capped():
    titles = [f"Fed rate move {i}" for i in range(10)]
    assert len(cluster(titles, "US")[0]["samples"]) == 3


def test_unknown_market_yields_nothing_without_crashing():
    assert cluster(["anything"], "JP") == []


def test_summarize_names_top_three_themes():
    titles = ["Fed rate", "Fed cut", "chip demand", "AI spending", "oil up", "crude rises"]
    cl = cluster(titles, "US")
    line = summarize(cl, len(titles), "US")
    assert "미국장 뉴스 6건" in line and "건)" in line
    assert line.count("(") <= 3


def test_summarize_handles_empty_clusters():
    assert "두드러진 테마가 없다" in summarize([], 12, "US")


def test_both_markets_have_themes_defined():
    assert set(THEMES) == {"US", "KR"}
    assert all(THEMES[m] for m in THEMES)


def test_min_count_default_filters_singletons():
    assert MIN_COUNT >= 2
