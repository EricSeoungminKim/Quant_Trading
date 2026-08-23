"""증권사 리서치 목록 파서. 픽스처는 2026-08-16 EC2 실측
`https://finance.naver.com/research/company_list.naver` 응답 조각이다 —
마크업을 추측해 만들지 않는다. 목표가(target_price) 컬럼은 이 목록 페이지
자체에 없다(전수 60건 실측, naver_research.py 모듈 docstring 참고) — 그래서
`target_price` 는 아래 픽스처 어디에도 등장하지 않고, 파서는 이 필드를
계약대로 결측 시 키 자체를 생략한다.
"""
from quant.collect.sources.naver_research import (
    append_ledger,
    fetch_research,
    parse_research_list,
)

# finance.naver.com/research/company_list.naver 1페이지, 두 행 발췌(2026-08-16 EC2).
_LIST_HTML = '''
					<tr>
						<th>종목명</th>
						<th>제목</th>
						<th style="text-align:left">증권사</th>
						<th>첨부</th>
						<th>작성일</th>
						<th>조회수</th>
					</tr>
					<tr><td colspan="6" class="blank_07"></td></tr>
					<tr>
						<td style="padding-left:10">
							<a href="/item/main.naver?code=001450" title="현대해상" class="stock_item">현대해상</a>
						</td>
						<td><a href="company_read.naver?nid=95658&page=1">손에 잡힐 것도 같은 배당</a></td>
						<td>미래에셋증권</td>
						<td class="file">
							<a href="https://stock.pstatic.net/stock-research/company/56/20260814_company_816592000.pdf" target="_blank"><img src="https://ssl.pstatic.net/imgstock/images5/down.gif" alt="pdf" align="absmiddle"></a>
						</td>
						<td class="date" style="padding-left:5px">26.08.14</td>
						<td class="date">6146</td>
					</tr>
					<tr>
						<td style="padding-left:10">
							<a href="/item/main.naver?code=005930" title="삼성전자" class="stock_item">삼성전자</a>
						</td>
						<td><a href="company_read.naver?nid=95657&page=1">2Q26 Review: 눈에 띄는 실적 개선, 기대..</a></td>
						<td>교보증권</td>
						<td class="file">
							<a href="https://stock.pstatic.net/stock-research/company/62/20260814_company_380000000.pdf" target="_blank"><img src="https://ssl.pstatic.net/imgstock/images5/down.gif" alt="pdf" align="absmiddle"></a>
						</td>
						<td class="date" style="padding-left:5px">26.08.14</td>
						<td class="date">5349</td>
					</tr>
'''

_EMPTY_HTML = "<html><body>결과가 없습니다</body></html>"


def test_parse_research_list_extracts_rows_from_real_fixture():
    rows = parse_research_list(_LIST_HTML)
    assert rows == [
        {
            "stock_name": "현대해상",
            "title": "손에 잡힐 것도 같은 배당",
            "broker": "미래에셋증권",
            "date": "2026-08-14",
        },
        {
            "stock_name": "삼성전자",
            "title": "2Q26 Review: 눈에 띄는 실적 개선, 기대..",
            "broker": "교보증권",
            "date": "2026-08-14",
        },
    ]


def test_parse_research_list_omits_target_price_key_when_absent():
    """목록 페이지엔 목표가 컬럼 자체가 없다 — 0/None 위장 없이 키 자체가 없다."""
    rows = parse_research_list(_LIST_HTML)
    assert rows  # 파싱은 됐다
    for row in rows:
        assert "target_price" not in row


def test_parse_research_list_empty_html_returns_empty_list():
    assert parse_research_list(_EMPTY_HTML) == []


def test_parse_research_list_none_input_returns_empty_list():
    assert parse_research_list(None) == []


def test_fetch_research_crawls_first_two_pages_only():
    calls = []

    def getter(url):
        calls.append(url)
        return _LIST_HTML

    rows = fetch_research(getter=getter, sleep=lambda s: None)
    assert len(calls) == 2
    assert calls[0] == "https://finance.naver.com/research/company_list.naver"
    assert calls[1] == "https://finance.naver.com/research/company_list.naver?page=2"
    assert len(rows) == 4  # 페이지당 2행 × 2페이지


def test_fetch_research_page_failure_skips_that_page_only():
    def getter(url):
        if url.endswith("page=2"):
            raise RuntimeError("boom")
        return _LIST_HTML

    rows = fetch_research(getter=getter, sleep=lambda s: None)
    assert len(rows) == 2  # 1페이지만 살았다


def test_fetch_research_all_pages_fail_returns_empty_list_no_exception():
    def getter(url):
        raise RuntimeError("boom")

    rows = fetch_research(getter=getter, sleep=lambda s: None)
    assert rows == []


def test_append_ledger_dedup_by_date_broker_title(tmp_path):
    path = tmp_path / "research.jsonl"
    row = {"stock_name": "현대해상", "title": "손에 잡힐 것도 같은 배당",
           "broker": "미래에셋증권", "date": "2026-08-14"}
    added1 = append_ledger([row], path)
    added2 = append_ledger([row], path)  # 같은 (date, broker, title) 재수집
    assert added1 == 1
    assert added2 == 0
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_append_ledger_different_stock_same_title_still_dedups_by_key(tmp_path):
    """dedup 키는 (date, broker, title) 뿐 — stock_name 은 키에 없다(스펙 계약)."""
    path = tmp_path / "research.jsonl"
    row_a = {"stock_name": "현대해상", "title": "동일 제목",
             "broker": "미래에셋증권", "date": "2026-08-14"}
    row_b = {"stock_name": "삼성전자", "title": "동일 제목",
             "broker": "미래에셋증권", "date": "2026-08-14"}
    added = append_ledger([row_a, row_b], path)
    assert added == 1


def test_append_ledger_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "research.jsonl"
    added = append_ledger(
        [{"stock_name": "현대해상", "title": "t", "broker": "b", "date": "2026-08-14"}],
        path,
    )
    assert added == 1
    assert path.exists()


def test_append_ledger_empty_rows_noop(tmp_path):
    path = tmp_path / "research.jsonl"
    assert append_ledger([], path) == 0
    assert not path.exists()
