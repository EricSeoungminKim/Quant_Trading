"""네이버 테마 파서. 픽스처는 2026-08-15 EC2 실측으로 받은 실제 응답 조각이다 —
마크업을 추측해 만들지 않는다.
_LIST_ROWS / _NAV_PAGE1 / _NAV_PAGE7: finance.naver.com/sise/theme.naver (page=1, page=7)
_MEMBERS_185: finance.naver.com/sise/sise_group_detail.naver?type=theme&no=185 (정유)
"""
from quant.collect.sources.naver_theme import (
    THEME_LIST_URL,
    fetch_themes,
    parse_theme_list,
    parse_theme_members,
    parse_theme_quotes,
)

_LIST_ROWS = '''<tr>
						<td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=185">정유</a></td>
						<td class="number col_type2">
				<span class="tah p11 red01">
				+6.48%
				</span>
			</td>
						<td class="number col_type3">
				<span class="tah p11 red01">
				+0.98%
				</span>
			</td>
						<td class="number col_type4">3</td>
						<td class="number col_type4">0</td>
						<td class="number col_type4">0</td>



							<td class="ls col_type5">
				<img src='https://ssl.pstatic.net/imgstock/images/images4/ico_up.gif' width='7' height='6' alt='상승'>
			<a href="/item/main.naver?code=078930">GS</a></td>


							<td class="ls col_type6">
				<img src='https://ssl.pstatic.net/imgstock/images/images4/ico_up.gif' width='7' height='6' alt='상승'>
			<a href="/item/main.naver?code=096770">SK이노베..</a></td>


					</tr>

					<tr>
						<td class="col_type1"><a href="/sise/sise_group_detail.naver?type=theme&no=488">웹툰</a></td>
						<td class="number col_type2">
				<span class="tah p11 red01">
				+5.44%
				</span>
			</td>
						<td class="number col_type3">
				<span class="tah p11 red01">
				+1.43%
				</span>
			</td>
						<td class="number col_type4">9</td>
						<td class="number col_type4">2</td>
						<td class="number col_type4">4</td>



							<td class="ls col_type5">
				<img src='https://ssl.pstatic.net/imgstock/images/images4/ico_up02.gif' width='7' height='11' alt='상한'>
			<a href="/item/main.naver?code=084180">수성웹툰</a></td>


							<td class="ls col_type6">
				<img src='https://ssl.pstatic.net/imgstock/images/images4/ico_up.gif' width='7' height='6' alt='상승'>
			<a href="/item/main.naver?code=134580">탑코미디어</a></td>


					</tr>'''

# 1페이지 네비게이션 — 현재 page=1 이 "on", 다음 페이지(2)·맨뒤(7) 링크가 있다.
_NAV_PAGE1 = '''<table summary="페이지 네비게이션 리스트" class="Nnavi" align="center">
					<caption>페이지 네비게이션</caption>
					<tr>



							<td class="on">
				<a href="/sise/theme.naver?&amp;page=1"  >1</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=2"  >2</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=3"  >3</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=4"  >4</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=5"  >5</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=6"  >6</a>
				</td>
<td>
				<a href="/sise/theme.naver?&amp;page=7"  >7</a>
				</td>


							<td class="pgRR">
				<a href="/sise/theme.naver?&amp;page=7"  >맨뒤
				<img src="https://ssl.pstatic.net/static/n/cmn/bu_pgarRR.gif" width="8" height="5" alt="" border="0">
				</a>
				</td>


					</tr>
				</table>'''

# 7페이지(마지막) 네비게이션 — page=8 링크가 없다. 다음 페이지 존재 여부 판단의 근거.
_NAV_PAGE7 = '''<table summary="페이지 네비게이션 리스트" class="Nnavi" align="center">
					<caption>페이지 네비게이션</caption>
					<tr>

							<td class="pgLL">
				<a href="/sise/theme.naver?page=1"  >
				<img src="https://ssl.pstatic.net/static/n/cmn/bu_pgarLL.gif" width="7" height="5" alt="">맨앞
				</a>
				</td>


							<td>
				<a href="/sise/theme.naver?page=1"  >1</a>
				</td>
<td>
				<a href="/sise/theme.naver?page=2"  >2</a>
				</td>
<td>
				<a href="/sise/theme.naver?page=3"  >3</a>
				</td>
<td>
				<a href="/sise/theme.naver?page=4"  >4</a>
				</td>
<td>
				<a href="/sise/theme.naver?page=5"  >5</a>
				</td>
<td>
				<a href="/sise/theme.naver?page=6"  >6</a>
				</td>
<td class="on">
				<a href="/sise/theme.naver?page=7"  >7</a>
				</td>




					</tr>
				</table>'''

# 정유(no=185) 상세 페이지 멤버 테이블 그대로 — GS·SK이노베이션·S-Oil 3종목 전부
# 편입사유가 있다.
_MEMBERS_185 = '''<tbody>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=078930">GS</a> <span class="dot"></span></div></td>

									<td>
										<div class="theme_info_area">
											<a href="javascript:;" class="btn_history" onMouseOver="mouseThemeToggle(this)" onMouseOut="mouseThemeToggle(this)">
												<span class="blind">테마 편입 사유</span>
											</a>

											<div class="info_layer_wrap"  tabindex="0" style="display:none;">
												<strong class="info_title">GS</strong>
												<p class="info_txt">정유업체 GS칼텍스를 손자회사로 보유.</p>
											</div>
										</div>
									</td>


								<td class="number" style="padding-right:15px;">117,000</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				9,600
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+8.94%
				</span>
			</td>



										<td class="number" style="padding-right:20px;">117,000</td>




										<td class="number" style="padding-right:20px;">117,100</td>




										<td class="number" style="padding-right:20px;">805,628</td>




										<td class="number" style="padding-right:20px;">93,140</td>




										<td class="number" style="padding-right:20px;">942,502</td>


								<td class="center"><a href="/item/board.naver?code=078930"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=096770">SK이노베이션</a> <span class="dot"></span></div></td>

									<td>
										<div class="theme_info_area">
											<a href="javascript:;" class="btn_history" onMouseOver="mouseThemeToggle(this)" onMouseOut="mouseThemeToggle(this)">
												<span class="blind">테마 편입 사유</span>
											</a>

											<div class="info_layer_wrap"  tabindex="0" style="display:none;">
												<strong class="info_title">SK이노베이션</strong>
												<p class="info_txt">SK그룹 계열의 종합에너지기업으로 100% 자회사인 SK에너지를 통해 정유사업을 영위.</p>
											</div>
										</div>
									</td>


								<td class="number" style="padding-right:15px;">128,700</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				7,000
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+5.75%
				</span>
			</td>



										<td class="number" style="padding-right:20px;">128,600</td>




										<td class="number" style="padding-right:20px;">128,700</td>




										<td class="number" style="padding-right:20px;">677,520</td>




										<td class="number" style="padding-right:20px;">86,091</td>




										<td class="number" style="padding-right:20px;">582,784</td>


								<td class="center"><a href="/item/board.naver?code=096770"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=010950">S-Oil</a> <span class="dot"></span></div></td>

									<td>
										<div class="theme_info_area">
											<a href="javascript:;" class="btn_history" onMouseOver="mouseThemeToggle(this)" onMouseOut="mouseThemeToggle(this)">
												<span class="blind">테마 편입 사유</span>
											</a>

											<div class="info_layer_wrap"  tabindex="0" style="display:none;">
												<strong class="info_title">S-Oil</strong>
												<p class="info_txt">국내 대표적인 정유사. 주요사업은 석유류 제품, 윤활기유 제품, 석유화학 제품의 제조 및 판매.</p>
											</div>
										</div>
									</td>


								<td class="number" style="padding-right:15px;">147,600</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				6,700
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+4.76%
				</span>
			</td>



										<td class="number" style="padding-right:20px;">147,600</td>




										<td class="number" style="padding-right:20px;">147,700</td>




										<td class="number" style="padding-right:20px;">325,864</td>




										<td class="number" style="padding-right:20px;">47,268</td>




										<td class="number" style="padding-right:20px;">323,206</td>


								<td class="center"><a href="/item/board.naver?code=010950"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>

							<tr><td colspan="12" class="blank_09"></td></tr>
							<tr><td colspan="12" class="division_line_1"></td></tr>
					</tbody>'''

# 게임(no=42) 상세의 시프트업 행 — 편입사유 본문에 <게임 제목> 형태의 리터럴
# 꺾쇠가 그대로 섞여 있다(2026-08-15 EC2 실측). [^<]* 로 사유를 잡으면 이 지점에서
# 매치가 깨져 사유가 있는데도 없는 것으로 오판했던 실제 회귀 케이스.
_MEMBERS_LT_IN_REASON = '''<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=462870">시프트업</a> <span class="dot"></span></div></td>

									<td>
										<div class="theme_info_area">
											<a href="javascript:;" class="btn_history" onMouseOver="mouseThemeToggle(this)" onMouseOut="mouseThemeToggle(this)">
												<span class="blind">테마 편입 사유</span>
											</a>

											<div class="info_layer_wrap"  tabindex="0" style="display:none;">
												<strong class="info_title">시프트업</strong>
												<p class="info_txt">게임 개발을 주사업으로 영위하는 글로벌 게임업체. 모바일 게임과 AAA 게임 IP를 보유하고 있으며, <승리의 여신: 니케>, <스텔라 블레이드> 등의 게임을 개발. 특히, 주력 게임인 <승리의 여신: 니케>는 2022년 11월 모바일 버전의 글로벌출시 이후 큰 성공을 거두면서 매출 대부분을 차지.</p>
											</div>
										</div>
									</td>


								<td class="number" style="padding-right:15px;">31,700</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				700
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+2.26%
				</span>
			</td>



										<td class="number" style="padding-right:20px;">31,650</td>




										<td class="number" style="padding-right:20px;">31,700</td>




										<td class="number" style="padding-right:20px;">39,687</td>




										<td class="number" style="padding-right:20px;">1,245</td>




										<td class="number" style="padding-right:20px;">71,245</td>


								<td class="center"><a href="/item/board.naver?code=462870"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>'''

# GS 행을 실측 마크업 그대로 두고 편입사유 블록(theme_info_area 전체)만 비운 것 —
# "편입사유 없음" 상황을 재현한다. 2026-08-15 EC2 전수 크롤(266개 테마·6,419종목)
# 에서도 이 케이스는 관측되지 않았다(전부 사유가 있었다).
# 그래도 브리프가 요구하는 방어 분기이므로, 실측 행 구조에서 사유 블록만 제거해
# 테스트로 고정한다 — 마크업 자체를 추측하지 않는다.
_MEMBERS_NO_REASON = '''<tbody>

							<tr onMouseOver="mouseOver(this)" onMouseOut="mouseOut(this)" >
								<td class="name"><div class="name_area"><a href="/item/main.naver?code=078930">GS</a> <span class="dot"></span></div></td>

									<td></td>


								<td class="number" style="padding-right:15px;">117,000</td>
								<td class="number" style="padding-right:15px;">
				<em class="bu_p bu_pup"><span class="blind">상승</span></em><span class="tah p11 red02">
				9,600
				</span>
			</td>
								<td class="number" style="padding-right:20px;">
				<span class="tah p11 red01">
				+8.94%
				</span>
			</td>



										<td class="number" style="padding-right:20px;">117,000</td>




										<td class="number" style="padding-right:20px;">117,100</td>




										<td class="number" style="padding-right:20px;">805,628</td>




										<td class="number" style="padding-right:20px;">93,140</td>




										<td class="number" style="padding-right:20px;">942,502</td>


								<td class="center"><a href="/item/board.naver?code=078930"><img src="https://ssl.pstatic.net/imgstock/images5/ico_debatebl2.gif" width="15" height="13" alt="토론"></a></td>
							</tr>

					</tbody>'''


def test_parse_theme_list():
    result = parse_theme_list(_LIST_ROWS)
    assert ("185", "정유") in result
    assert ("488", "웹툰") in result


def test_parse_theme_quotes_reads_change_pct_not_3day_column():
    # _LIST_ROWS 실측: col_type2 = 전일대비(등락률, 네이버 홈 "테마상위" 값),
    # col_type3 = 최근 3일 등락률. 정유는 전일대비 +6.48% / 3일 +0.98% — 값이
    # 달라 컬럼을 착각하면 테스트가 바로 깨진다.
    result = parse_theme_quotes(_LIST_ROWS)
    assert {"no": "185", "name": "정유", "change_pct": 6.48} in result
    assert {"no": "488", "name": "웹툰", "change_pct": 5.44} in result


def test_parse_theme_members_reads_reason_change_and_value_traded():
    result = parse_theme_members(_MEMBERS_185)
    assert result == [
        {
            "code": "078930",
            "name": "GS",
            "reason": "정유업체 GS칼텍스를 손자회사로 보유.",
            "change_pct": 8.94,
            "value_traded": 93140,
        },
        {
            "code": "096770",
            "name": "SK이노베이션",
            "reason": "SK그룹 계열의 종합에너지기업으로 100% 자회사인 SK에너지를 통해 정유사업을 영위.",
            "change_pct": 5.75,
            "value_traded": 86091,
        },
        {
            "code": "010950",
            "name": "S-Oil",
            "reason": "국내 대표적인 정유사. 주요사업은 석유류 제품, 윤활기유 제품, 석유화학 제품의 제조 및 판매.",
            "change_pct": 4.76,
            "value_traded": 47268,
        },
    ]


def test_parse_theme_members_keeps_reason_with_literal_angle_brackets():
    result = parse_theme_members(_MEMBERS_LT_IN_REASON)
    assert len(result) == 1
    assert result[0]["code"] == "462870"
    assert "<승리의 여신: 니케>" in result[0]["reason"]


def test_parse_theme_members_excludes_row_without_reason():
    result = parse_theme_members(_MEMBERS_NO_REASON)
    assert result == []


def test_parse_theme_members_logs_skip_count_to_stderr(capsys):
    parse_theme_members(_MEMBERS_NO_REASON)
    err = capsys.readouterr().err
    assert "0/1건" in err or "건너뜀 1건" in err


def test_parse_theme_members_silent_when_nothing_skipped(capsys):
    parse_theme_members(_MEMBERS_185)
    err = capsys.readouterr().err
    assert err == ""


def test_fetch_themes_stops_when_no_next_page_link():
    # 실측(2026-08-15 EC2): 테마 목록은 총 7페이지, 1~6페이지는 다음 페이지 링크가
    # 있고(_NAV_PAGE1 과 동일한 1~7+맨뒤 링크 구성) 7페이지(_NAV_PAGE7 실측)엔
    # page=8 링크가 없다. 8페이지는 절대 요청되면 안 된다(무한 루프 금지).
    calls = []

    def getter(url):
        calls.append(url)
        if "no=" in url:
            return _MEMBERS_185 if "no=185" in url else ""
        if "page=7" in url:
            return _LIST_ROWS + _NAV_PAGE7
        return _LIST_ROWS + _NAV_PAGE1  # page=1 (파라미터 없음) ~ page=6

    result = fetch_themes(getter=getter, sleep=lambda s: None)
    list_calls = [u for u in calls if "no=" not in u]
    assert list_calls == [THEME_LIST_URL] + [f"{THEME_LIST_URL}?page={p}" for p in range(2, 8)]
    assert "185" in result
    assert result["185"]["name"] == "정유"
    assert len(result["185"]["symbols"]) == 3


def test_fetch_themes_max_pages_caps_list_fetches():
    calls = []

    def getter(url):
        calls.append(url)
        if "no=" in url:
            return _MEMBERS_185 if "no=185" in url else ""
        return _LIST_ROWS + _NAV_PAGE1  # 항상 "다음 페이지 있음"

    fetch_themes(getter=getter, sleep=lambda s: None, max_pages=1)
    list_calls = [u for u in calls if "no=" not in u]
    assert list_calls == [THEME_LIST_URL]


def test_fetch_themes_adds_change_pct_additively():
    def getter(url):
        if "no=" in url:
            return _MEMBERS_185 if "no=185" in url else ""
        return _LIST_ROWS + _NAV_PAGE7  # 1페이지에서 바로 멈춘다

    result = fetch_themes(getter=getter, sleep=lambda s: None)
    assert result["185"]["name"] == "정유"
    assert len(result["185"]["symbols"]) == 3
    assert result["185"]["change_pct"] == 6.48


def test_fetch_themes_partial_theme_failure_keeps_going():
    def getter(url):
        if "no=185" in url:
            raise RuntimeError("timeout")
        if "no=488" in url:
            return _MEMBERS_185  # 내용은 상관없음, 실패하지 않는다는 것만 확인
        return _LIST_ROWS + _NAV_PAGE7  # 1페이지에서 바로 멈춘다

    result = fetch_themes(getter=getter, sleep=lambda s: None)
    assert "185" not in result
    assert "488" in result
