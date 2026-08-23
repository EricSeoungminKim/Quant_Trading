"""네이버 업종 파서. 픽스처는 2026-08-15/16 실측으로 받은 실제 응답 조각이다 —
마크업을 추측해 만들지 않는다.
_INDEX: finance.naver.com/sise/sise_group.naver?type=upjong
_MEMBERS: finance.naver.com/sise/sise_group_detail.naver?type=upjong&no=333
_MEMBERS_WITH_QUOTES: 위와 같은 URL, 2026-08-16 EC2 재실측 — 무선통신서비스 4종목
전체(SK텔레콤 +10.32%, LG유플러스 +2.45%, 프리티 0.00%(보합), 와이어블 -1.03%
(코스닥 별표 `*`)). 사용자가 스크린샷으로 지적한 결함(멤버 1종목만 표시)의
재현/회귀 픽스처다.
"""
from quant.collect.sources.naver_sector import (
    fetch_sector_data,
    fetch_sector_map,
    fetch_sector_quotes,
    parse_sector_detail_members,
    parse_sector_index,
    parse_sector_members,
    parse_sector_quotes,
)

_INDEX = '''<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=333">무선통신서비스</a></td>
						<td class="number">
				<span class="tah p11 red01">
				+8.49%
				</span>
			</td>
						<td class="number">4</td>
						<td class="number">2</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:100%;"><span class="graph_txt">100%</span></div></div></td>
					</tr>




					<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=273">자동차</a></td>
						<td class="number">
				<span class="tah p11 red01">
				+6.20%
				</span>
			</td>
						<td class="number">12</td>
						<td class="number">10</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:73%;"><span class="graph_txt">73%</span></div></div></td>
					</tr>'''

_MEMBERS = '''<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=017670">SK텔레콤</a> <span class="dot"></span></div></td>


								<td class="number" style="padding-right:15px;">100,500</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				9,400
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+10.32%
				</span>
			</td>
								<td class="center"><a href="/item/board.naver?code=017670"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=032640">LG유플러스</a> <span class="dot"></span></div></td>


								<td class="number" style="padding-right:15px;">15,060</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				360
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+2.45%
				</span>
			</td>
								<td class="center"><a href="/item/board.naver?code=032640"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>'''


# 무선통신서비스(no=333) 업종 상세 페이지의 멤버 테이블 4행 전체 —
# 2026-08-16 EC2 재실측(사용자가 스크린샷으로 지적한 실제 케이스). 보합
# (프리티 0.00%, 부호·색상 클래스 없음)과 코스닥 별표(와이어블,
# <span class="dot">*</span>)를 그대로 포함한다.
_MEMBERS_WITH_QUOTES = '''<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=017670">SK텔레콤</a> <span class="dot"></span></div></td>


								<td class="number" style="padding-right:15px;">100,500</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				9,400
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+10.32%
				</span>
			</td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=032640">LG유플러스</a> <span class="dot"></span></div></td>


								<td class="number" style="padding-right:15px;">15,060</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				360
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+2.45%
				</span>
			</td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=006490">프리티</a> <span class="dot"></span></div></td>


								<td class="number" style="padding-right:15px;">898</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pn"><span class="blind">보합</span></em><span class="tah p11">0</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11">0.00%</span>
			</td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=065530">와이어블</a> <span class="dot">*</span></div></td>


								<td class="number" style="padding-right:15px;">1,340</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pdn"><span class="blind">하락</span></em><span class="tah p11 nv01">
				14
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 nv01">
				-1.03%
				</span>
			</td>
							</tr>'''


# 두 번째 행은 실제 행 마크업을 그대로 두고 등락률 span 만 비운 것 — 데이터
# 누락/깨짐 상황을 재현한다(실 페이지에서 오늘 관측되진 않았지만 마크업 구조
# 자체는 위 _INDEX 실측과 동일하다).
_INDEX_WITH_BROKEN_ROW = '''<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=333">무선통신서비스</a></td>
						<td class="number">
				<span class="tah p11 red01">
				+8.49%
				</span>
			</td>
						<td class="number">4</td>
						<td class="number">2</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:100%;"><span class="graph_txt">100%</span></div></div></td>
					</tr>
					<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=999">깨진업종</a></td>
						<td class="number">
			</td>
						<td class="number">3</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:33%;"><span class="graph_txt">33%</span></div></div></td>
					</tr>'''


# 보합(0.00%) 행 — 인덱스 페이지 자체에서는 오늘(2026-08-15) 79개 업종 전부
# 0이 아니어서 실측하지 못했지만, 같은 사이트의 업종 상세 페이지
# (sise_group_detail.naver?type=upjong&no=333, 2026-08-15 EC2 실측)에서
# 보합 종목이 `<span class="tah p11">0.00%</span>` 로 부호·색상 클래스
# 없이 렌더링되는 걸 확인했다. 그 등락률 셀 마크업을 위 _INDEX 의 실측 행
# 구조에 그대로 옮겨 재현한다 — 셀 자체는 두 실측 조각의 조합이지 추측이
# 아니다.
_INDEX_WITH_FLAT_ROW = '''<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=333">무선통신서비스</a></td>
						<td class="number">
				<span class="tah p11 red01">
				+8.49%
				</span>
			</td>
						<td class="number">4</td>
						<td class="number">2</td>
						<td class="number">1</td>
						<td class="number">1</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:100%;"><span class="graph_txt">100%</span></div></div></td>
					</tr>
					<tr>
						<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=555">보합업종</a></td>
						<td class="number">
				<span class="tah p11">0.00%</span>
			</td>
						<td class="number">3</td>
						<td class="number">1</td>
						<td class="number">2</td>
						<td class="number">0</td>
						<td class="tc"><div class="graph_type_1" style="width:80px;"><div class="graph_bar" style="width:33%;"><span class="graph_txt">33%</span></div></div></td>
					</tr>'''


def test_parse_sector_index():
    result = parse_sector_index(_INDEX)
    assert ("333", "무선통신서비스") in result
    assert ("273", "자동차") in result


def test_parse_sector_members():
    result = parse_sector_members(_MEMBERS)
    assert ("017670", "SK텔레콤") in result
    assert ("032640", "LG유플러스") in result


def test_fetch_sector_map_maps_code_to_sector():
    def getter(url):
        if "type=upjong&no=" not in url:
            return _INDEX
        if "no=333" in url:
            return _MEMBERS
        return ""  # no=273 상세는 이 테스트에서 안 씀

    m = fetch_sector_map(getter=getter, sleep=lambda s: None)
    assert m.get("017670") == "무선통신서비스"
    assert m.get("032640") == "무선통신서비스"


def test_fetch_sector_map_partial_failure_keeps_going():
    # 업종 하나(333) 조회가 실패해도 나머지(273)는 수집된다 — 전체를 버리지 않는다
    def getter(url):
        if "type=upjong&no=" not in url:
            return _INDEX
        if "no=333" in url:
            raise RuntimeError("timeout")
        if "no=273" in url:
            return _MEMBERS
        return ""

    m = fetch_sector_map(getter=getter, sleep=lambda s: None)
    assert m.get("017670") == "자동차"
    assert m.get("032640") == "자동차"


def test_parse_sector_quotes_name_change_and_updown_counts():
    result = parse_sector_quotes(_INDEX)
    assert result == [
        {
            "no": "333",
            "name": "무선통신서비스",
            "change_pct": 8.49,
            "up": 2,
            "flat": 1,
            "down": 1,
        },
        {
            "no": "273",
            "name": "자동차",
            "change_pct": 6.20,
            "up": 10,
            "flat": 1,
            "down": 1,
        },
    ]


def test_parse_sector_quotes_skips_row_with_missing_change_pct():
    result = parse_sector_quotes(_INDEX_WITH_BROKEN_ROW)
    assert len(result) == 1
    assert result[0]["name"] == "무선통신서비스"
    assert all(row["name"] != "깨진업종" for row in result)


def test_parse_sector_quotes_keeps_flat_row_as_zero_not_skip():
    # 보합(0.00%, 부호 없음)은 결측이 아니라 유효한 데이터 — 스킵하면 안 된다
    result = parse_sector_quotes(_INDEX_WITH_FLAT_ROW)
    assert len(result) == 2
    flat_row = next(r for r in result if r["name"] == "보합업종")
    assert flat_row["change_pct"] == 0.0
    assert flat_row["up"] == 1
    assert flat_row["flat"] == 2
    assert flat_row["down"] == 0


def test_parse_sector_quotes_logs_skip_count_to_stderr(capsys):
    parse_sector_quotes(_INDEX_WITH_BROKEN_ROW)
    err = capsys.readouterr().err
    assert "1/2건" in err or "건너뜀 1건" in err


def test_parse_sector_quotes_silent_when_nothing_skipped(capsys):
    parse_sector_quotes(_INDEX)
    err = capsys.readouterr().err
    assert err == ""


def test_fetch_sector_quotes_reads_index_only():
    def getter(url):
        assert "type=upjong&no=" not in url  # 인덱스 1페이지만 — 상세를 열지 않는다
        return _INDEX

    result = fetch_sector_quotes(getter=getter)
    assert len(result) == 2


def test_fetch_sector_quotes_returns_empty_list_on_index_failure():
    def getter(url):
        raise RuntimeError("timeout")

    assert fetch_sector_quotes(getter=getter) == []


def test_parse_sector_detail_members_returns_all_four_with_change_pct():
    # 사용자가 지적한 결함의 근본 원인 재현 — 이 페이지엔 4종목이 있는데
    # 기존 fetch_sector_map 은 code/name 만 쓰고 시세를 버렸다.
    result = parse_sector_detail_members(_MEMBERS_WITH_QUOTES)
    assert result == [
        {"code": "017670", "name": "SK텔레콤", "change_pct": 10.32},
        {"code": "032640", "name": "LG유플러스", "change_pct": 2.45},
        {"code": "006490", "name": "프리티", "change_pct": 0.0},
        {"code": "065530", "name": "와이어블", "change_pct": -1.03},
    ]


def test_parse_sector_detail_members_handles_flat_zero_pct():
    result = parse_sector_detail_members(_MEMBERS_WITH_QUOTES)
    flat = next(r for r in result if r["name"] == "프리티")
    assert flat["change_pct"] == 0.0


def test_parse_sector_detail_members_handles_kosdaq_star_without_corrupting_name():
    # 와이어블은 <span class="dot">*</span> (코스닥 표기) — 이름 캡처가
    # </a> 에서 끝나 별표가 이름에 섞이면 안 된다.
    result = parse_sector_detail_members(_MEMBERS_WITH_QUOTES)
    starred = next(r for r in result if r["code"] == "065530")
    assert starred["name"] == "와이어블"
    assert starred["change_pct"] == -1.03


def test_fetch_sector_data_returns_map_and_members_from_one_crawl():
    calls: list[str] = []

    def getter(url):
        calls.append(url)
        if "type=upjong&no=" not in url:
            return _INDEX
        if "no=333" in url:
            return _MEMBERS_WITH_QUOTES
        return ""  # no=273 상세는 이 테스트에서 안 씀

    sector_map, sector_members = fetch_sector_data(getter=getter, sleep=lambda s: None)
    assert sector_map.get("017670") == "무선통신서비스"
    assert sector_map.get("032640") == "무선통신서비스"
    assert len(sector_members["무선통신서비스"]) == 4
    assert {"code": "065530", "name": "와이어블", "change_pct": -1.03} in sector_members["무선통신서비스"]
    # 추가 네트워크 0 — 인덱스 1 + 업종별 상세 각 1(기존 fetch_sector_map 과 동일한 호출 수)
    assert calls.count("https://finance.naver.com/sise/sise_group.naver?type=upjong") == 1


def test_fetch_sector_map_wraps_fetch_sector_data_first_element():
    # 하위호환 — fetch_sector_map 은 fetch_sector_data 의 첫 원소(sector_map)만 돌려준다
    def getter(url):
        if "type=upjong&no=" not in url:
            return _INDEX
        if "no=333" in url:
            return _MEMBERS_WITH_QUOTES
        return ""

    m = fetch_sector_map(getter=getter, sleep=lambda s: None)
    assert m == {"017670": "무선통신서비스", "032640": "무선통신서비스",
                 "006490": "무선통신서비스", "065530": "무선통신서비스"}
