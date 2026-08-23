"""DART 재무제표 수집기(fnlttSinglAcnt/stockTotqySttus/corpCode) 테스트.

응답 구조 픽스처는 2026-08-19 실측(corp_code=00126380 삼성전자,
bsns_year=2025, reprt_code=11011)을 그대로 옮긴 것이다 — 계정명·필드명을
추측해 만들지 않는다.
"""
import json
import zipfile
from io import BytesIO

from quant.collect.sources.dart_financials import (
    append_ledger,
    build_stock_to_corp,
    compute_factors,
    extract_key_accounts,
    extract_shares_outstanding,
    fetch_and_persist,
    fetch_corp_codes,
    fetch_financials,
    fetch_shares_outstanding,
    get_corp_code_map,
    load_corp_code_cache,
    parse_corp_codes,
    parse_financial_payload,
    save_corp_code_cache,
)

_CORP_CODE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<result>
    <list>
        <corp_code>00126380</corp_code>
        <corp_name>삼성전자</corp_name>
        <corp_eng_name>SAMSUNG ELECTRONICS CO,.LTD</corp_eng_name>
        <stock_code>005930</stock_code>
        <modify_date>20260310</modify_date>
    </list>
    <list>
        <corp_code>00434003</corp_code>
        <corp_name>다코</corp_name>
        <corp_eng_name>Daco corporation</corp_eng_name>
        <stock_code> </stock_code>
        <modify_date>20170630</modify_date>
    </list>
</result>"""


def _zip_bytes(xml_text: str) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml_text)
    return buf.getvalue()


# 실측(2026-08-19) fnlttSinglAcnt.json 응답 — CFS(연결)/OFS(개별) 둘 다 오고,
# 당기순이익(손실)은 같은 fs_div 안에서 두 번(ord=29, ord=61) 중복돼 온다.
_FIN_OK_PAYLOAD = {
    "status": "000",
    "message": "정상",
    "list": [
        {"rcept_no": "20260310002820", "fs_div": "CFS", "sj_div": "BS",
         "account_nm": "자산총계", "thstrm_amount": "566,942,110,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "CFS", "sj_div": "BS",
         "account_nm": "부채총계", "thstrm_amount": "130,621,773,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "CFS", "sj_div": "BS",
         "account_nm": "자본총계", "thstrm_amount": "436,320,337,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "CFS", "sj_div": "IS",
         "account_nm": "당기순이익(손실)", "thstrm_amount": "45,206,805,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "CFS", "sj_div": "IS",
         "account_nm": "당기순이익(손실)", "thstrm_amount": "45,206,805,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "OFS", "sj_div": "BS",
         "account_nm": "부채총계", "thstrm_amount": "104,571,968,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "OFS", "sj_div": "BS",
         "account_nm": "자본총계", "thstrm_amount": "254,330,083,000,000"},
        {"rcept_no": "20260310002820", "fs_div": "OFS", "sj_div": "IS",
         "account_nm": "당기순이익(손실)", "thstrm_amount": "33,686,601,000,000"},
    ],
}

_NO_DATA_PAYLOAD = {"status": "013", "message": "조회된 데이타가 없습니다."}
_RATE_LIMIT_PAYLOAD = {"status": "020", "message": "요청 제한을 초과하였습니다."}

# 실측 stockTotqySttus.json 응답 — 보통주/우선주/합계/비고 4행.
_SHARES_OK_PAYLOAD = {
    "status": "000",
    "message": "정상",
    "list": [
        {"se": "보통주", "istc_totqy": "5,919,637,922"},
        {"se": "우선주", "istc_totqy": "815,974,664"},
        {"se": "합계", "istc_totqy": "6,735,612,586"},
        {"se": "비고", "istc_totqy": "-"},
    ],
}


# ---------------------------------------------------------------------------
# corp_code
# ---------------------------------------------------------------------------


def test_parse_corp_codes_keeps_only_listed():
    rows = parse_corp_codes(_CORP_CODE_XML.encode("utf-8"))
    assert rows == [
        {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
    ]


def test_fetch_corp_codes_no_key_returns_error_without_network():
    calls = []

    def getter(key):
        calls.append(key)
        raise AssertionError("네트워크를 타면 안 된다")

    rows, err = fetch_corp_codes(api_key="", getter=getter)
    assert rows == []
    assert err == "no key"
    assert calls == []


def test_fetch_corp_codes_unzips_and_parses():
    def getter(key):
        return _zip_bytes(_CORP_CODE_XML)

    rows, err = fetch_corp_codes(api_key="dummy", getter=getter)
    assert err is None
    assert rows == [
        {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"},
    ]


def test_fetch_corp_codes_bad_zip_is_an_error():
    def getter(key):
        return b"not a zip file"

    rows, err = fetch_corp_codes(api_key="dummy", getter=getter)
    assert rows == []
    assert err is not None


def test_build_stock_to_corp_keeps_first_seen_on_duplicate():
    rows = [
        {"corp_code": "A1", "corp_name": "먼저", "stock_code": "005930"},
        {"corp_code": "A2", "corp_name": "나중", "stock_code": "005930"},
    ]
    m = build_stock_to_corp(rows)
    assert m == {"005930": {"corp_code": "A1", "corp_name": "먼저"}}


def test_corp_code_cache_roundtrip(tmp_path):
    path = tmp_path / "dart_corp_codes.json"
    rows = [{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"}]
    save_corp_code_cache(rows, path)
    assert load_corp_code_cache(path) == rows


def test_load_corp_code_cache_missing_returns_none(tmp_path):
    assert load_corp_code_cache(tmp_path / "nope.json") is None


def test_get_corp_code_map_uses_cache_without_network(tmp_path):
    cache_path = tmp_path / "data" / "cache" / "dart_corp_codes.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps([{"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930"}]),
        encoding="utf-8",
    )

    def getter(key):
        raise AssertionError("캐시가 있으면 네트워크를 타면 안 된다")

    m, err = get_corp_code_map(tmp_path, api_key="dummy", getter=getter)
    assert err is None
    assert m == {"005930": {"corp_code": "00126380", "corp_name": "삼성전자"}}


def test_get_corp_code_map_fetches_and_caches_when_missing(tmp_path):
    def getter(key):
        return _zip_bytes(_CORP_CODE_XML)

    m, err = get_corp_code_map(tmp_path, api_key="dummy", getter=getter)
    assert err is None
    assert "005930" in m
    cache_path = tmp_path / "data" / "cache" / "dart_corp_codes.json"
    assert cache_path.exists()


# ---------------------------------------------------------------------------
# fnlttSinglAcnt
# ---------------------------------------------------------------------------


def test_parse_financial_payload_no_data_is_not_an_error():
    rows, err = parse_financial_payload(_NO_DATA_PAYLOAD)
    assert rows == []
    assert err is None


def test_parse_financial_payload_rate_limit_is_an_error():
    rows, err = parse_financial_payload(_RATE_LIMIT_PAYLOAD)
    assert rows == []
    assert "020" in err


def test_extract_key_accounts_prefers_consolidated_cfs():
    result = extract_key_accounts(_FIN_OK_PAYLOAD["list"])
    assert result == {
        "fs_div": "CFS",
        "capital_total": 436_320_337_000_000,
        "liabilities_total": 130_621_773_000_000,
        "net_income": 45_206_805_000_000,
        "rcept_no": "20260310002820",
    }


def test_extract_key_accounts_falls_back_to_ofs_when_cfs_missing():
    ofs_only = [row for row in _FIN_OK_PAYLOAD["list"] if row["fs_div"] == "OFS"]
    result = extract_key_accounts(ofs_only)
    assert result["fs_div"] == "OFS"
    assert result["capital_total"] == 254_330_083_000_000
    assert result["liabilities_total"] == 104_571_968_000_000
    assert result["net_income"] == 33_686_601_000_000


def test_extract_key_accounts_missing_account_is_none_not_zero():
    result = extract_key_accounts([
        {"rcept_no": "X", "fs_div": "CFS", "sj_div": "BS",
         "account_nm": "자본총계", "thstrm_amount": "1,000"},
    ])
    assert result["capital_total"] == 1000
    assert result["liabilities_total"] is None  # 0이 아니라 None
    assert result["net_income"] is None


def test_extract_key_accounts_empty_rows():
    result = extract_key_accounts([])
    assert result == {
        "fs_div": None, "capital_total": None, "liabilities_total": None,
        "net_income": None, "rcept_no": None,
    }


def test_fetch_financials_no_key_returns_error_without_network():
    def getter(url, params):
        raise AssertionError("네트워크를 타면 안 된다")

    result, err = fetch_financials("00126380", "2025", "11011", api_key="", getter=getter)
    assert result == {}
    assert err == "no key"


def test_fetch_financials_ok():
    def getter(url, params):
        assert params["corp_code"] == "00126380"
        return _FIN_OK_PAYLOAD

    result, err = fetch_financials("00126380", "2025", "11011", api_key="dummy", getter=getter)
    assert err is None
    assert result["capital_total"] == 436_320_337_000_000


def test_fetch_financials_network_exception_no_key_leak():
    def getter(url, params):
        raise ConnectionError("boom with secret_key=abc123")

    result, err = fetch_financials("00126380", "2025", "11011", api_key="dummy", getter=getter)
    assert result == {}
    assert "abc123" not in err
    assert "dummy" not in err


# ---------------------------------------------------------------------------
# stockTotqySttus
# ---------------------------------------------------------------------------


def test_extract_shares_outstanding_uses_common_stock_row():
    assert extract_shares_outstanding(_SHARES_OK_PAYLOAD["list"]) == 5_919_637_922


def test_extract_shares_outstanding_missing_common_row_is_none():
    assert extract_shares_outstanding([{"se": "우선주", "istc_totqy": "1,000"}]) is None


def test_fetch_shares_outstanding_ok():
    def getter(url, params):
        return _SHARES_OK_PAYLOAD

    shares, err = fetch_shares_outstanding("00126380", "2025", "11011", api_key="dummy", getter=getter)
    assert err is None
    assert shares == 5_919_637_922


# ---------------------------------------------------------------------------
# 파생 팩터
# ---------------------------------------------------------------------------


def test_compute_factors_normal_case():
    factors = compute_factors(
        capital_total=436_320_337_000_000,
        liabilities_total=130_621_773_000_000,
        net_income=45_206_805_000_000,
        shares_outstanding=5_919_637_922,
    )
    assert factors["debt_ratio"] == 29.94  # 130.6조/436.3조 * 100
    assert factors["roe"] == 10.36  # 45.2조/436.3조 * 100
    assert factors["bps"] == 73707.27  # 436.3조/59.2억주


def test_compute_factors_missing_denominator_is_none_not_zero():
    factors = compute_factors(None, 100, 50, 1000)
    assert factors == {"debt_ratio": None, "roe": None, "bps": None}


def test_compute_factors_zero_denominator_is_none():
    factors = compute_factors(0, 100, 50, 0)
    assert factors["debt_ratio"] is None
    assert factors["roe"] is None
    assert factors["bps"] is None


def test_compute_factors_missing_numerator_is_none():
    factors = compute_factors(capital_total=1000, liabilities_total=None,
                               net_income=None, shares_outstanding=None)
    assert factors == {"debt_ratio": None, "roe": None, "bps": None}


# ---------------------------------------------------------------------------
# 원장
# ---------------------------------------------------------------------------


def test_append_ledger_dedup_by_stock_year_report(tmp_path):
    path = tmp_path / "fundamentals_dart.jsonl"
    rows = [
        {"stock_code": "005930", "bsns_year": "2025", "reprt_code": "11011", "roe": 10.36},
    ]
    assert append_ledger(rows, path) == 1

    more = rows + [
        {"stock_code": "000660", "bsns_year": "2025", "reprt_code": "11011", "roe": 30.0},
    ]
    assert append_ledger(more, path) == 1  # 005930/2025/11011 은 이미 있음

    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2


def test_append_ledger_empty_rows_noop(tmp_path):
    path = tmp_path / "fundamentals_dart.jsonl"
    assert append_ledger([], path) == 0
    assert not path.exists()


# ---------------------------------------------------------------------------
# 통합 (fetch_and_persist)
# ---------------------------------------------------------------------------


def test_fetch_and_persist_end_to_end(tmp_path):
    def corp_getter(key):
        return _zip_bytes(_CORP_CODE_XML)

    def fin_getter(url, params):
        assert url.endswith("fnlttSinglAcnt.json")
        return _FIN_OK_PAYLOAD

    def shares_getter(url, params):
        assert url.endswith("stockTotqySttus.json")
        return _SHARES_OK_PAYLOAD

    stat = fetch_and_persist(
        ["005930"], "2025", "11011", tmp_path, api_key="dummy",
        corp_code_getter=corp_getter, financials_getter=fin_getter, shares_getter=shares_getter,
    )
    assert stat == {"requested": 1, "added": 1, "errors": []}

    path = tmp_path / "data" / "ledger" / "fundamentals_dart.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["stock_code"] == "005930"
    assert row["corp_code"] == "00126380"
    assert row["corp_name"] == "삼성전자"
    assert row["fs_div"] == "CFS"
    assert row["capital_total"] == 436_320_337_000_000
    assert row["shares_outstanding"] == 5_919_637_922
    assert row["roe"] == 10.36
    assert row["rcept_dt"] == "20260310"
    assert row["source"] == "dart_financials"


def test_fetch_and_persist_unmapped_stock_code_is_recorded_as_error(tmp_path):
    def corp_getter(key):
        return _zip_bytes(_CORP_CODE_XML)  # 삼성전자만 있음

    stat = fetch_and_persist(
        ["999999"], "2025", "11011", tmp_path, api_key="dummy",
        corp_code_getter=corp_getter, financials_getter=lambda u, p: _FIN_OK_PAYLOAD,
        shares_getter=lambda u, p: _SHARES_OK_PAYLOAD,
    )
    assert stat["added"] == 0
    assert stat["requested"] == 1
    assert len(stat["errors"]) == 1
    assert "999999" in stat["errors"][0]


def test_fetch_and_persist_financials_error_recorded_and_skipped(tmp_path):
    def corp_getter(key):
        return _zip_bytes(_CORP_CODE_XML)

    def fin_getter(url, params):
        return _RATE_LIMIT_PAYLOAD

    stat = fetch_and_persist(
        ["005930"], "2025", "11011", tmp_path, api_key="dummy",
        corp_code_getter=corp_getter, financials_getter=fin_getter,
        shares_getter=lambda u, p: _SHARES_OK_PAYLOAD,
    )
    assert stat["added"] == 0
    assert len(stat["errors"]) == 1
    assert "005930" in stat["errors"][0]


def test_fetch_and_persist_shares_error_does_not_block_row(tmp_path, capsys):
    # 발행주식수 조회가 실패해도 재무제표 값은 살린다 — BPS만 None.
    def corp_getter(key):
        return _zip_bytes(_CORP_CODE_XML)

    def shares_getter(url, params):
        return _RATE_LIMIT_PAYLOAD

    stat = fetch_and_persist(
        ["005930"], "2025", "11011", tmp_path, api_key="dummy",
        corp_code_getter=corp_getter, financials_getter=lambda u, p: _FIN_OK_PAYLOAD,
        shares_getter=shares_getter,
    )
    assert stat["added"] == 1
    assert stat["errors"] == []

    path = tmp_path / "data" / "ledger" / "fundamentals_dart.jsonl"
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["shares_outstanding"] is None
    assert row["bps"] is None
    assert row["capital_total"] == 436_320_337_000_000  # 재무제표 값은 살아있음
    err = capsys.readouterr().err
    assert "005930" in err
