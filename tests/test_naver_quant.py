"""네이버 거래상위 파서. 픽스처는 2026-08-15 실측으로 받은 실제 응답 조각이다 —
마크업을 추측해 만들지 않는다.
_ROW_1 / _ROW_2 / _ROW_ALNUM_CODE: finance.naver.com/sise/sise_quant.naver (page=1)
_ROW_SAMSUNG: 같은 페이지, 2026-08-19 실측 — PER/ROE가 "N/A"가 아닌 실제 종목.
"""
import json

from quant.collect.sources.naver_quant import (
    INDEX_URL,
    append_ledger,
    fetch_and_persist,
    fetch_quant_top,
    parse_quant_rows,
)

# 1위: KODEX 200선물인버스2X — 숫자 6자리 코드, 음수 등락률.
_ROW_1 = '''<tr>
					<td class="no">1</td>
					<td><a href="/item/main.naver?code=252670" class="tltle">KODEX 200선물인버스2X</a></td>
					<td class="number">76</td>
					<td class="number">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				5
				</span>
			</td>
					<td class="number">
				<span class="tah p11 nv01">
				-6.17%
				</span>
			</td>





									<td class="number">6,365,295,139</td>







									<td class="number">489,799</td>







									<td class="number">75</td>







									<td class="number">76</td>







									<td class="number">5,879</td>






					<td class="number">N/A</td>







					<td class="number">N/A</td>




				</tr>'''

# 2위: KODEX 인버스 — 같은 구조, 다른 값으로 리스트 파싱을 확인한다.
_ROW_2 = '''<tr>
					<td class="no">2</td>
					<td><a href="/item/main.naver?code=114800" class="tltle">KODEX 인버스</a></td>
					<td class="number">1,011</td>
					<td class="number">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				28
				</span>
			</td>
					<td class="number">
				<span class="tah p11 nv01">
				-2.69%
				</span>
			</td>





									<td class="number">1,020,141,859</td>







									<td class="number">1,037,229</td>







									<td class="number">1,010</td>







									<td class="number">1,011</td>







									<td class="number">6,705</td>






					<td class="number">N/A</td>







					<td class="number">N/A</td>




				</tr>'''

# 85위: TIGER 미국우주테크 — 코드가 6자리 영숫자 혼합(ETN류). 2026-08-15 실측
# 100종목 중 19종목이 이런 형태였다 — \d{6} 만으로는 놓친다.
_ROW_ALNUM_CODE = '''<tr>
					<td class="no">85</td>
					<td><a href="/item/main.naver?code=0183J0" class="tltle">TIGER 미국우주테크</a></td>
					<td class="number">8,845</td>
					<td class="number">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				120
				</span>
			</td>
					<td class="number">
				<span class="tah p11 nv01">
				-1.34%
				</span>
			</td>





								<td class="number">1,897,156</td>







								<td class="number">16,771</td>







								<td class="number">8,845</td>







								<td class="number">8,850</td>







								<td class="number">12,954</td>






					<td class="number">N/A</td>







					<td class="number">N/A</td>




				</tr>'''

# 14위: 삼성전자 — 2026-08-19 실측. ETF와 달리 PER/ROE 칸이 실제 숫자다.
_ROW_SAMSUNG = '''<tr>
					<td class="no">14</td>
					<td><a href="/item/main.naver?code=005930" class="tltle">삼성전자</a></td>
					<td class="number">268,500</td>
					<td class="number">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				6,000
				</span>
			</td>
					<td class="number">
				<span class="tah p11 nv01">
				-2.19%
				</span>
			</td>




								<td class="number">24,003,581</td>




								<td class="number">6,616,933</td>




								<td class="number">268,500</td>




								<td class="number">269,000</td>




								<td class="number">15,697,258</td>




					<td class="number">21.70</td>




					<td class="number">10.85</td>




				</tr>'''

# _ROW_1 을 실측 행 구조 그대로 두고 등락률 span 만 비운 것 — naver_sector.py 의
# _INDEX_WITH_BROKEN_ROW 와 같은 관례로 데이터 누락/깨짐 상황을 재현한다(실
# 페이지에서 오늘(2026-08-15) 100종목 전부 정상이어서 관측되진 않았다).
_ROW_BROKEN_CHANGE = '''<tr>
					<td class="no">1</td>
					<td><a href="/item/main.naver?code=252670" class="tltle">KODEX 200선물인버스2X</a></td>
					<td class="number">76</td>
					<td class="number">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				5
				</span>
			</td>
					<td class="number">
				</td>





									<td class="number">6,365,295,139</td>







									<td class="number">489,799</td>







									<td class="number">75</td>







									<td class="number">76</td>







									<td class="number">5,879</td>






					<td class="number">N/A</td>







					<td class="number">N/A</td>




				</tr>'''


def test_parse_quant_rows_reads_code_name_change_and_value_traded():
    result = parse_quant_rows(_ROW_1 + _ROW_2)
    assert result == [
        {
            "code": "252670",
            "name": "KODEX 200선물인버스2X",
            "change_pct": -6.17,
            "value_traded": 489799,
            "market_cap": 5879,
            "per": None,
            "roe": None,
        },
        {
            "code": "114800",
            "name": "KODEX 인버스",
            "change_pct": -2.69,
            "value_traded": 1037229,
            "market_cap": 6705,
            "per": None,
            "roe": None,
        },
    ]


def test_parse_quant_rows_handles_alnum_etn_code():
    result = parse_quant_rows(_ROW_ALNUM_CODE)
    assert result == [
        {
            "code": "0183J0",
            "name": "TIGER 미국우주테크",
            "change_pct": -1.34,
            "value_traded": 16771,
            "market_cap": 12954,
            "per": None,
            "roe": None,
        }
    ]


def test_parse_quant_rows_etf_per_roe_are_none_not_zero():
    # ETF/ETN 은 PER·ROE 개념이 없어 네이버가 항상 "N/A" 를 준다(2026-08-19 실측,
    # 오늘 거래상위 100종목 중 레버리지/인버스 상품 다수 확인). 0 으로 위장하면
    # "PER 0" 이라는 거짓 팩터가 만들어진다 — None 이어야 한다.
    result = parse_quant_rows(_ROW_1)
    assert result[0]["per"] is None
    assert result[0]["roe"] is None


def test_parse_quant_rows_reads_real_per_roe_for_ordinary_stock():
    # 005930 삼성전자 — 2026-08-19 실측: ETF와 달리 PER/ROE 가 실제 숫자로 온다.
    result = parse_quant_rows(_ROW_SAMSUNG)
    assert result == [
        {
            "code": "005930",
            "name": "삼성전자",
            "change_pct": -2.19,
            "value_traded": 6616933,
            "market_cap": 15697258,
            "per": 21.70,
            "roe": 10.85,
        }
    ]


def test_parse_quant_rows_skips_row_with_missing_change_pct():
    result = parse_quant_rows(_ROW_BROKEN_CHANGE + _ROW_2)
    assert len(result) == 1
    assert result[0]["code"] == "114800"
    assert all(row["code"] != "252670" for row in result)


def test_parse_quant_rows_logs_skip_count_to_stderr(capsys):
    parse_quant_rows(_ROW_BROKEN_CHANGE)
    err = capsys.readouterr().err
    assert "0/1건" in err or "건너뜀 1건" in err


def test_parse_quant_rows_silent_when_nothing_skipped(capsys):
    parse_quant_rows(_ROW_1)
    err = capsys.readouterr().err
    assert err == ""


def test_parse_quant_rows_empty_html_returns_empty_list():
    assert parse_quant_rows("") == []
    assert parse_quant_rows(None) == []


def test_fetch_quant_top_reads_index_only_by_default():
    def getter(url):
        assert url == INDEX_URL  # 기본 1페이지 상한 — page 파라미터를 붙이지 않는다
        return _ROW_1 + _ROW_2

    result = fetch_quant_top(getter=getter)
    assert len(result) == 2


def test_fetch_quant_top_returns_empty_list_on_index_failure():
    def getter(url):
        raise RuntimeError("timeout")

    assert fetch_quant_top(getter=getter) == []


def test_fetch_quant_top_dedupes_across_pages(monkeypatch):
    # 실측(2026-08-15): sise_quant.naver 는 page 파라미터를 붙여도 같은 100종목을
    # 반복해서 돌려준다(페이지네이션 없음) — pages>1 요청 시 중복 적재를 코드
    # 기준 dedup 으로 막는다는 것만 확인한다.
    monkeypatch.setattr("quant.collect.sources.naver_quant.time.sleep", lambda s: None)
    calls = []

    def getter(url):
        calls.append(url)
        return _ROW_1 + _ROW_2

    result = fetch_quant_top(getter=getter, pages=2)
    assert len(result) == 2  # 중복 없이 2종목
    assert calls == [INDEX_URL, f"{INDEX_URL}?page=2"]


def test_append_ledger_dedup_by_date_and_code(tmp_path):
    path = tmp_path / "fundamentals_naver.jsonl"
    rows = [
        {"date": "2026-08-19", "code": "005930", "per": 21.70},
        {"date": "2026-08-19", "code": "000660", "per": 15.0},
    ]
    added1 = append_ledger(rows, path)
    assert added1 == 2

    # 같은 날 같은 종목 재적재 — 장중에 값이 바뀌어도 그날 첫 관측값만 남는다.
    more = [
        {"date": "2026-08-19", "code": "005930", "per": 99.0},  # 무시돼야 함
        {"date": "2026-08-20", "code": "005930", "per": 22.0},  # 다음날 값은 신규
    ]
    added2 = append_ledger(more, path)
    assert added2 == 1

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert lines[0]["per"] == 21.70  # 첫 관측값 유지, 99.0 으로 덮이지 않음


def test_append_ledger_empty_rows_noop(tmp_path):
    path = tmp_path / "fundamentals_naver.jsonl"
    assert append_ledger([], path) == 0
    assert not path.exists()


def test_fetch_and_persist_records_kst_date_and_source(tmp_path):
    from datetime import datetime, timezone

    def getter(url):
        return _ROW_SAMSUNG

    # UTC 2026-08-19 15:30 = KST 2026-08-20 00:30 — 날짜 경계를 넘는지 확인한다.
    now = datetime(2026, 8, 19, 15, 30, tzinfo=timezone.utc)
    stat = fetch_and_persist(tmp_path, getter=getter, now=now)
    assert stat == {"fetched": 1, "added": 1, "date": "2026-08-20"}

    path = tmp_path / "data" / "ledger" / "fundamentals_naver.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {
            "code": "005930",
            "name": "삼성전자",
            "change_pct": -2.19,
            "value_traded": 6616933,
            "market_cap": 15697258,
            "per": 21.70,
            "roe": 10.85,
            "date": "2026-08-20",
            "source": "naver_quant",
        }
    ]


def test_fetch_and_persist_second_call_same_day_does_not_duplicate(tmp_path):
    from datetime import datetime, timezone

    def getter(url):
        return _ROW_SAMSUNG

    now = datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc)
    fetch_and_persist(tmp_path, getter=getter, now=now)
    stat2 = fetch_and_persist(tmp_path, getter=getter, now=now)
    assert stat2["added"] == 0

    path = tmp_path / "data" / "ledger" / "fundamentals_naver.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
