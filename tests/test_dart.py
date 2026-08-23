"""DART OpenAPI(전자공시) list.json 파서. 픽스처는 DART 공식 문서에 실린 응답
형태를 그대로 옮긴 것이다(status 000/013/020, list 항목 필드명 포함).
"""
import json

from quant.collect.sources.dart import (
    append_ledger,
    classify_report,
    fetch_disclosures,
    parse_disclosures,
)

_OK_PAYLOAD = {
    "status": "000",
    "message": "정상",
    "page_no": 1,
    "page_count": 100,
    "total_count": 2,
    "total_page": 1,
    "list": [
        {
            "corp_code": "00126380",
            "corp_name": "삼성전자",
            "stock_code": "005930",
            "corp_cls": "Y",
            "report_nm": "주요사항보고서(현금·현물배당결정)",
            "rcept_no": "20260816000123",
            "flr_nm": "삼성전자",
            "rcept_dt": "20260816",
            "rm": "유",
        },
        {
            "corp_code": "00164779",
            "corp_name": "비상장이엔지",
            "stock_code": "",
            "corp_cls": "N",
            "report_nm": "감사보고서제출",
            "rcept_no": "20260816000456",
            "flr_nm": "비상장이엔지",
            "rcept_dt": "20260816",
            "rm": "",
        },
    ],
}

_NO_DATA_PAYLOAD = {"status": "013", "message": "조회된 데이타가 없습니다."}

_RATE_LIMIT_PAYLOAD = {"status": "020", "message": "요청 제한을 초과하였습니다."}


def test_parse_disclosures_ok_filters_unlisted():
    rows, err = parse_disclosures(_OK_PAYLOAD)
    assert err is None
    assert len(rows) == 1
    assert rows[0] == {
        "rcept_no": "20260816000123",
        "stock_code": "005930",
        "corp_name": "삼성전자",
        "report_nm": "주요사항보고서(현금·현물배당결정)",
        "rcept_dt": "20260816",
    }


def test_parse_disclosures_no_data_is_not_an_error():
    rows, err = parse_disclosures(_NO_DATA_PAYLOAD)
    assert rows == []
    assert err is None


def test_parse_disclosures_rate_limit_is_an_error():
    rows, err = parse_disclosures(_RATE_LIMIT_PAYLOAD)
    assert rows == []
    assert err is not None
    assert "020" in err


def test_parse_disclosures_empty_payload():
    rows, err = parse_disclosures({})
    assert rows == []
    assert err is not None  # status 필드 없음 → != "000"


def test_fetch_disclosures_no_key_returns_error_without_network():
    calls = []

    def getter(params):
        calls.append(params)
        raise AssertionError("네트워크를 타면 안 된다")

    rows, err = fetch_disclosures("20260815", "20260816", api_key="", getter=getter)
    assert rows == []
    assert err == "no key"
    assert calls == []


def test_fetch_disclosures_single_page_both_corp_classes():
    calls = []

    def getter(params):
        calls.append(params["corp_cls"])
        return _OK_PAYLOAD

    rows, err = fetch_disclosures("20260815", "20260816", api_key="dummy", getter=getter)
    assert err is None
    # Y, K 두 번 호출 — 각각 1건씩(비상장 걸러짐) 합쳐 2건
    assert calls == ["Y", "K"]
    assert len(rows) == 2
    assert all(r["stock_code"] == "005930" for r in rows)


def test_fetch_disclosures_paginates_until_total_page():
    page1 = dict(_OK_PAYLOAD, total_page=2, page_no=1)
    page2 = dict(_OK_PAYLOAD, total_page=2, page_no=2)
    calls = []

    def getter(params):
        calls.append((params["corp_cls"], params["page_no"]))
        return page1 if params["page_no"] == 1 else page2

    rows, err = fetch_disclosures("20260815", "20260816", api_key="dummy", getter=getter)
    assert err is None
    # 각 corp_cls 마다 2페이지씩 = 4번 호출
    assert calls == [("Y", 1), ("Y", 2), ("K", 1), ("K", 2)]
    assert len(rows) == 4


def test_fetch_disclosures_caps_at_max_pages():
    infinite = dict(_OK_PAYLOAD, total_page=999)

    def getter(params):
        return infinite

    rows, err = fetch_disclosures("20260815", "20260816", api_key="dummy", getter=getter)
    assert err is None
    # corp_cls 마다 최대 10페이지 * 2 corp_cls = 20번 호출분
    assert len(rows) == 20


def test_fetch_disclosures_network_exception_swallowed_no_key_leak():
    def getter(params):
        raise ConnectionError("boom with secret_key=abc123")

    rows, err = fetch_disclosures("20260815", "20260816", api_key="dummy", getter=getter)
    assert rows == []
    assert err is not None
    assert "abc123" not in err
    assert "dummy" not in err


def test_fetch_disclosures_one_corp_cls_errors_other_still_collected():
    def getter(params):
        if params["corp_cls"] == "Y":
            return _RATE_LIMIT_PAYLOAD
        return _OK_PAYLOAD

    rows, err = fetch_disclosures("20260815", "20260816", api_key="dummy", getter=getter)
    assert err is not None
    assert "020" in err
    # K 쪽은 정상 수집됐다
    assert len(rows) == 1
    assert rows[0]["stock_code"] == "005930"


def test_append_ledger_dedup_by_rcept_no(tmp_path):
    path = tmp_path / "disclosures.jsonl"
    rows = [
        {"rcept_no": "A1", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "공시1", "rcept_dt": "20260816"},
        {"rcept_no": "A2", "stock_code": "005930", "corp_name": "삼성전자",
         "report_nm": "공시2", "rcept_dt": "20260816"},
    ]
    added1 = append_ledger(rows, path)
    assert added1 == 2

    # 같은 rcept_no 로 다시 append 하면 0건만 추가된다
    more = rows + [
        {"rcept_no": "A3", "stock_code": "000660", "corp_name": "SK하이닉스",
         "report_nm": "공시3", "rcept_dt": "20260816"},
    ]
    added2 = append_ledger(more, path)
    assert added2 == 1

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    rcept_nos = [json.loads(line)["rcept_no"] for line in lines]
    assert rcept_nos == ["A1", "A2", "A3"]


def test_append_ledger_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "dir" / "disclosures.jsonl"
    added = append_ledger(
        [{"rcept_no": "B1", "stock_code": "005930", "corp_name": "삼성전자",
          "report_nm": "공시", "rcept_dt": "20260816"}],
        path,
    )
    assert added == 1
    assert path.exists()


def test_append_ledger_empty_rows_noop(tmp_path):
    path = tmp_path / "disclosures.jsonl"
    added = append_ledger([], path)
    assert added == 0
    assert not path.exists()


def test_classify_report_supply_contract():
    assert classify_report("단일판매ㆍ공급계약체결") == "수주"
    assert classify_report("주요사항보고서(수주계약)") == "수주"


def test_classify_report_clinical_approval():
    assert classify_report("임상 3상 승인") == "임상·승인"
    assert classify_report("FDA 품목허가 신청") == "임상·승인"
    assert classify_report("품목허가 취득") == "임상·승인"


def test_classify_report_paid_in_capital_increase():
    assert classify_report("유상증자결정") == "유상증자"


def test_classify_report_bonus_capital_increase():
    assert classify_report("무상증자결정") == "무상증자"


def test_classify_report_convertible_bond():
    assert classify_report("전환사채권발행결정") == "CB"
    assert classify_report("CB 발행 결정") == "CB"


def test_classify_report_dividend():
    assert classify_report("현금·현물배당결정") == "배당"


def test_classify_report_earnings():
    assert classify_report("연결재무제표기준영업(잠정)실적(공정공시)") == "실적"
    assert classify_report("분기보고서(실적)") == "실적"


def test_classify_report_treasury_stock():
    assert classify_report("자기주식취득결정") == "자사주"


def test_classify_report_ownership_change():
    assert classify_report("최대주주변경") == "지분변동"
    assert classify_report("특정증권등지분변동") == "지분변동"


def test_classify_report_no_match_returns_none():
    assert classify_report("기업설명회(IR)개최(안내공시)") is None


def test_classify_report_empty_string_returns_none():
    assert classify_report("") is None
