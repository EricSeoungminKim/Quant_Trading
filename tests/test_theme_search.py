"""테마 기반 탐색(순수). 핵심 계약: `select_sources` 는 한 테마가 결과를 독식하지
못한다 — 뉴스가 아무리 반도체 편중이어도 삼성전자·SK하이닉스만 나오지 않는다."""
from quant.analyze.theme_search import (
    beneficiaries, hot_themes, leaders, select_sources, theme_index)
from quant.analyze.relations import match_codes

_THEMES = {
    "1": {
        "name": "반도체",
        "symbols": [
            {"code": "005930", "name": "삼성전자", "reason": "메모리 반도체 1위",
             "change_pct": 1.2, "value_traded": 900_000},
            {"code": "000660", "name": "SK하이닉스", "reason": "HBM 생산",
             "change_pct": 2.5, "value_traded": 800_000},
            {"code": "042700", "name": "한미반도체", "reason": "HBM 본딩 장비 공급",
             "change_pct": 3.1, "value_traded": 300_000},
            {"code": "039030", "name": "이오테크닉스", "reason": "레이저 마킹 장비",
             "change_pct": 0.5, "value_traded": 100_000},
        ],
    },
    "2": {
        "name": "2차전지(생산)",
        "symbols": [
            {"code": "373220", "name": "LG에너지솔루션", "reason": "배터리셀 생산",
             "change_pct": 1.0, "value_traded": 500_000},
            {"code": "006400", "name": "삼성SDI", "reason": "배터리셀 생산",
             "change_pct": 1.5, "value_traded": 200_000},
        ],
    },
    "3": {
        "name": "정유",
        "symbols": [
            {"code": "010950", "name": "S-Oil", "reason": "정유 정제마진",
             "change_pct": 0.8, "value_traded": None},
        ],
    },
}


def test_theme_index_maps_code_to_theme_numbers():
    idx = theme_index(_THEMES)
    assert idx["005930"] == ["1"]
    assert idx["373220"] == ["2"]
    assert "999999" not in idx


def test_leaders_sorted_by_value_traded_desc():
    got = leaders(_THEMES, "1", top=2)
    assert [s["code"] for s in got] == ["005930", "000660"]


def test_leaders_none_value_traded_goes_last_not_excluded():
    got = leaders(_THEMES, "3", top=5)
    assert [s["code"] for s in got] == ["010950"]  # 제외되지 않는다


def test_beneficiaries_excludes_src_and_carries_reason_and_via_theme():
    got = beneficiaries(_THEMES, "1", src_code="005930", limit=5)
    codes = [b["code"] for b in got]
    assert "005930" not in codes
    assert set(codes) == {"000660", "042700", "039030"}
    for b in got:
        assert b["reason"]  # 편입사유가 그대로 실린다(빈 이유 없음)
        assert b["via_theme"] == "반도체"


def test_beneficiaries_defensive_drop_of_empty_reason():
    """Task 1 에서 편입사유 없는 종목은 이미 걸러지지만, 방어적으로 여기서도
    빈 이유는 후보에서 뺀다."""
    themes = {"1": {"name": "테스트", "symbols": [
        {"code": "111111", "name": "A", "reason": "이유있음", "value_traded": 10},
        {"code": "222222", "name": "B", "reason": "", "value_traded": 20},
        {"code": "333333", "name": "C", "reason": "src", "value_traded": 30},
    ]}}
    got = beneficiaries(themes, "1", src_code="333333", limit=5)
    assert [b["code"] for b in got] == ["111111"]


def test_hot_themes_counts_distinct_matched_symbols_not_repeated_title_mentions():
    """대형주 편중 완화: 삼성전자·SK하이닉스가 여러 제목에 걸쳐 반복 언급돼도
    반도체 테마의 매칭 종목 수는 distinct 2 로 유지된다(제목 3개가 두 종목을
    겹쳐 언급해도 특이도의 분자가 3으로 부풀지 않는다)."""
    titles = [
        "삼성전자 HBM4 조기 양산",
        "SK하이닉스 실적 서프라이즈",
        "삼성전자·SK하이닉스 동반 강세",  # 이미 센 두 종목의 반복 언급
    ]
    table = [(s["name"], s["code"]) for t in _THEMES.values() for s in t["symbols"]]
    matches = match_codes(table, titles)
    # 반도체(테마 1, 4종목 중 2매칭 = 특이도 0.5)만 자격(매칭≥2) 충족 —
    # 2차전지·정유는 이 제목들에서 아예 매칭이 없다.
    assert hot_themes(_THEMES, matches, limit=5) == ["1"]


def test_hot_themes_empty_when_no_matches():
    assert hot_themes(_THEMES, [set(), set()], limit=3) == []


def test_select_sources_never_lets_one_theme_dominate():
    """사용자 요구의 핵심 계약: 반도체 뉴스가 압도적으로 많아도(삼성전자·
    SK하이닉스·한미반도체·이오테크닉스 전부 매칭) select_sources 결과의 테마
    수는 max_themes 를 넘지 않고, 테마당 종목 수는 per_theme 를 넘지 않는다."""
    titles = [
        "삼성전자 HBM4 조기 양산", "삼성전자 목표가 상향", "삼성전자 신고가",
        "SK하이닉스 실적 서프라이즈", "SK하이닉스 HBM 증설", "SK하이닉스 급등",
        "한미반도체 수주 확대", "한미반도체 HBM 장비", "한미반도체 신고가",
        "이오테크닉스 레이저 장비 수주", "이오테크닉스 실적 개선",
    ]
    table = [(s["name"], s["code"]) for t in _THEMES.values() for s in t["symbols"]]
    matches = match_codes(table, titles)
    got = select_sources(_THEMES, matches, max_themes=2, per_theme=2)

    theme_nos = {via for src in got for via in [src["via_theme"]]}
    assert len(theme_nos) <= 2
    from collections import Counter
    per_theme_counts = Counter(src["via_theme"] for src in got)
    assert all(c <= 2 for c in per_theme_counts.values())
    # 독식 방지: 반도체(테마 1) 4종목 전부가 아니라 상한만큼만 나온다
    codes = {src["code"] for src in got}
    assert not {"005930", "000660", "042700", "039030"}.issubset(codes)


def test_select_sources_without_market_leaders_backward_compatible():
    matches = [{"005930"}]
    got = select_sources(_THEMES, matches, max_themes=1, per_theme=1)
    assert len(got) <= 1


def test_select_sources_merges_market_leaders_matched_in_titles_within_overall_cap():
    """Task 1b 거래상위 목록은 선택 인자다. 테마 밖이라도 그날 뉴스에 매칭된
    거래상위 종목은 보조로 합류하되, 전체 상한(max_themes*per_theme)은 유지된다.
    max_themes=2 이지만 뉴스가 가리키는 테마는 1개뿐이라 남는 자리(1개)에
    거래상위 종목이 채워지는 케이스."""
    market_leaders = [
        {"code": "999999", "name": "테마밖종목", "value_traded": 5_000_000, "change_pct": 5.0},
        {"code": "888888", "name": "매칭안된종목", "value_traded": 9_000_000, "change_pct": 1.0},
    ]
    titles = ["삼성전자 HBM4 조기 양산", "테마밖종목 급등 마감"]
    table = [(s["name"], s["code"]) for t in _THEMES.values() for s in t["symbols"]]
    table.append(("테마밖종목", "999999"))
    table.append(("매칭안된종목", "888888"))
    matches = match_codes(table, titles)

    got = select_sources(_THEMES, matches, max_themes=2, per_theme=1,
                         market_leaders=market_leaders)
    codes = [s["code"] for s in got]
    assert len(got) <= 2                # 전체 상한(max_themes*per_theme) 유지
    assert "999999" in codes            # 뉴스 매칭된 거래상위는 합류
    assert "888888" not in codes        # 뉴스에 매칭 안 된 거래상위는 합류하지 않는다


def test_select_sources_market_leader_code_format_not_assumed_numeric():
    """ETN 영숫자 코드(예: 0183J0)가 거래상위에 섞인다 — 코드 형식을 가정하지
    않고 그대로 통과시킨다(필터링은 하류 결정)."""
    market_leaders = [
        {"code": "0183J0", "name": "ETN상품", "value_traded": 1_000_000, "change_pct": 0.1},
    ]
    titles = ["ETN상품 거래대금 급증"]
    table = [("ETN상품", "0183J0")]
    matches = match_codes(table, titles)
    got = select_sources({}, matches, max_themes=1, per_theme=1,
                         market_leaders=market_leaders)
    assert any(s["code"] == "0183J0" for s in got)


# ── 2026-08-16 리뷰 수정 (#1~#3) ─────────────────────────────

def test_hot_themes_specificity_beats_absolute_count_umbrella_scenario():
    """2026-08-16 EC2 e2e 실측 결함: `hot_themes` 가 절대 매칭 종목 수로만
    순위를 매기면 뉴스 대형주를 많이 품은 대형 우산 지수 테마(예: 코리아
    밸류업 지수)가 구조적으로 항상 이겼다 — 그 편입사유는 회사 소개문일
    뿐이라 가이드 가치가 0인데도. 특이도(매칭 종목 수 ÷ 테마 전체 종목 수)
    로 재면 대형 테마(멤버 100, 매칭 5, 특이도 0.05)보다 소형 섹터 테마
    (멤버 15, 매칭 4, 특이도 0.267)가 이긴다 — 절대 매칭 수는 대형이 더
    많은데도(5 > 4) 소형이 순위에서 이긴다."""
    big_umbrella = [
        {"code": f"9{i:05d}", "name": f"대형{i}", "reason": "지수 구성종목",
         "value_traded": 10}
        for i in range(100)
    ]
    small_sector = [
        {"code": f"8{i:05d}", "name": f"소형{i}", "reason": "섹터 종목",
         "value_traded": 10}
        for i in range(15)
    ]
    themes = {
        "1": {"name": "우산지수", "symbols": big_umbrella},
        "2": {"name": "소형섹터", "symbols": small_sector},
    }
    table = [(s["name"], s["code"]) for t in themes.values() for s in t["symbols"]]
    titles = (
        [f"{big_umbrella[i]['name']} 강세" for i in range(5)]
        + [f"{small_sector[i]['name']} 강세" for i in range(4)]
    )
    matches = match_codes(table, titles)

    assert hot_themes(themes, matches, limit=2) == ["2", "1"]  # 소형이 대형을 이긴다
    assert hot_themes(themes, matches, limit=1) == ["2"]       # max_themes=1이면 대형은 탈락

    got = select_sources(themes, matches, max_themes=1, per_theme=5)
    assert {s["via_theme"] for s in got} == {"소형섹터"}
    assert not any(s["via_theme"] == "우산지수" for s in got)


def test_hot_themes_requires_at_least_two_matched_symbols():
    """매칭 종목 수 1개뿐인 초소형 테마가 특이도 1.0으로 최상위를 차지하는
    잡음을 막는다 — 자격 요건은 매칭 종목 수 ≥ 2."""
    themes = {"1": {"name": "초소형", "symbols": [
        {"code": "700001", "name": "단독종목", "reason": "r", "value_traded": 10},
        {"code": "700002", "name": "동료종목", "reason": "r", "value_traded": 10},
    ]}}
    table = [(s["name"], s["code"]) for t in themes.values() for s in t["symbols"]]
    matches = match_codes(table, ["단독종목 강세"])  # 테마 내 1개만 매칭
    assert hot_themes(themes, matches, limit=5) == []


def test_hot_themes_tie_break_is_deterministic_regardless_of_dict_order():
    """동점 테마(특이도·매칭 종목 수·언급 제목 수 완전히 같음)의 최종 순위가
    실행마다(해시 랜덤화) 달라지면 Phase 8 하네스의 재현성 요구를 어긴다.
    딕셔너리 삽입 순서를 바꿔 두 번 호출해도 결과가 같아야 하고, 완전
    동률은 테마 번호 오름차순으로 해소돼야 한다. 두 테마 모두 멤버 2개 중
    2개가 매칭돼(특이도 1.0) 자격 요건(≥2)도 함께 만족한다."""
    def build(order):
        d = {}
        for no in order:
            if no == "9":
                d["9"] = {"name": "테마9", "symbols": [
                    {"code": "900001", "name": "구구공일", "reason": "r", "value_traded": 10},
                    {"code": "900002", "name": "구구공이", "reason": "r", "value_traded": 10},
                ]}
            else:
                d["5"] = {"name": "테마5", "symbols": [
                    {"code": "500001", "name": "오공공일", "reason": "r", "value_traded": 10},
                    {"code": "500002", "name": "오공공이", "reason": "r", "value_traded": 10},
                ]}
        return d

    themes_a = build(["9", "5"])
    themes_b = build(["5", "9"])
    titles = ["구구공일 강세", "구구공이 강세", "오공공일 강세", "오공공이 강세"]
    table_a = [(s["name"], s["code"]) for t in themes_a.values() for s in t["symbols"]]
    table_b = [(s["name"], s["code"]) for t in themes_b.values() for s in t["symbols"]]
    matches_a = match_codes(table_a, titles)
    matches_b = match_codes(table_b, titles)

    result_a = hot_themes(themes_a, matches_a, limit=2)
    result_b = hot_themes(themes_b, matches_b, limit=2)
    assert result_a == result_b == ["5", "9"]  # 완전 동점 → 테마 번호 오름차순


def test_select_sources_reserves_leader_slots_even_when_theme_sources_fill_cap():
    """리뷰 판정(2026-08-16): 테마 소스가 max_themes*per_theme 를 항상 꽉
    채우면 거래상위 보조 신호가 구조적으로 못 들어온다. leader_slots 는
    거래상위 전용 예약석이라 테마가 cap 을 꽉 채워도 반드시 반영돼야 한다."""
    market_leaders = [
        {"code": "999999", "name": "테마밖1", "value_traded": 5_000_000, "change_pct": 1.0},
        {"code": "888888", "name": "테마밖2", "value_traded": 4_000_000, "change_pct": 1.0},
    ]
    titles = [
        "삼성전자 HBM4 조기 양산", "SK하이닉스 실적 서프라이즈",
        "테마밖1 급등", "테마밖2 강세",
    ]
    table = [(s["name"], s["code"]) for t in _THEMES.values() for s in t["symbols"]]
    table.append(("테마밖1", "999999"))
    table.append(("테마밖2", "888888"))
    matches = match_codes(table, titles)

    # 테마 "1" 만으로 max_themes*per_theme(=1*2=2) 캡을 꽉 채운다
    got = select_sources(_THEMES, matches, max_themes=1, per_theme=2,
                         market_leaders=market_leaders, leader_slots=2)
    theme_codes = {s["code"] for s in got if s["via_theme"] is not None}
    leader_codes = {s["code"] for s in got if s["via_theme"] is None}
    assert len(theme_codes) == 2                  # 테마 소스가 cap 을 꽉 채웠다
    assert leader_codes == {"999999", "888888"}   # 그래도 예약석 2개는 들어온다
    assert len(got) == 4                          # 2(theme) + 2(leader_slots)
