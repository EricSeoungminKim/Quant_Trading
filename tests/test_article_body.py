"""본문 수집기 — 실패를 빈 본문으로 위장하지 않는지가 핵심이다."""
from quant.collect.sources.article_body import MAX_BODY_CHARS, extract_text, fetch_body

_HTML = """<html><head><style>p{color:red}</style>
<script>var x = "<p>가짜 문단</p>";</script></head>
<body><p>삼성전자가 차세대 HBM4 양산 일정을 앞당긴다고 밝혔다. 업계에서는 관련 소재·부품 공급망 전반의 수혜를 점치고 있다.</p>
<nav><p>메뉴</p></nav>
<p>한미반도체와 이오테크닉스 등 장비주가 동반 강세를 보였다.</p></body></html>"""


def test_extract_text_collects_paragraphs_and_strips_tags():
    text = extract_text(_HTML)
    assert "HBM4" in text and "한미반도체" in text
    assert "<p>" not in text and "var x" not in text


def test_extract_text_drops_short_boilerplate():
    # "메뉴" 같은 짧은 <p> 는 본문이 아니다 (30자 미만 문단 제외)
    assert "메뉴" not in extract_text(_HTML)


def test_extract_text_caps_length():
    huge = "<p>" + "가" * 10000 + "</p>"
    assert len(extract_text(huge)) <= MAX_BODY_CHARS


def test_fetch_body_failure_is_none_not_empty():
    """실패는 None — "" 로 위장하면 하류가 '본문 없는 기사'로 오독한다."""
    assert fetch_body("http://x", getter=lambda url: None) is None

    def boom(url):
        raise RuntimeError("connection reset")
    assert fetch_body("http://x", getter=boom) is None


def test_fetch_body_empty_extraction_is_none():
    # <p> 없는 페이지 → 추출 결과가 비면 None (부분 성공 위장 금지)
    assert fetch_body("http://x", getter=lambda url: "<div>no paras</div>") is None
