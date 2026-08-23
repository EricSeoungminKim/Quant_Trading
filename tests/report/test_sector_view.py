"""테마(업종) 시세 뷰 조립 (리포트 고도화 B Task 5 / 네이버 패리티 H-1c Task 3).

`build_sector_view`/`build_top_movers`는 순수 함수 — 네트워크·파일 접근 없이
인자로만 판단한다.
"""
from quant.analyze.sector_view import build_sector_view, build_top_movers


def _sector_map():
    return {"005930": "반도체와반도체장비", "000660": "반도체와반도체장비", "005380": "자동차"}


def _sector_quotes():
    return [
        {"no": "1", "name": "반도체와반도체장비", "change_pct": 3.5, "up": 5, "flat": 1, "down": 2},
        # "자동차"는 등락률 조회 실패로 빠져 있다 — 미확인 표현을 테스트한다.
    ]


def _symbols():
    return {
        "005930": {"name": "삼성전자", "change_pct": 3.5},
        "000660": {"name": "SK하이닉스", "change_pct": 1.2},
        "005380": {"name": "현대차", "change_pct": -0.5},
    }


# ① 유니버스 종목이 있는 업종만 나온다


def test_only_sectors_with_universe_symbols_appear():
    """sector_map 에 업종이 하나 더 있어도(우리 유니버스에 종목이 없으면) 결과에서 빠진다."""
    sector_map = {**_sector_map(), "999999": "우리가 모르는 업종"}
    view = build_sector_view(sector_map, _sector_quotes(), _symbols(), {})
    names = [s["name"] for s in view]
    assert "우리가 모르는 업종" not in names
    assert set(names) == {"반도체와반도체장비", "자동차"}


# ② 등락률 없는 업종은 change_pct: None 이고 정렬에서 뒤로


def test_sector_without_quote_is_none_and_sorted_last():
    view = build_sector_view(_sector_map(), _sector_quotes(), _symbols(), {})
    assert view[0]["name"] == "반도체와반도체장비"
    assert view[0]["change_pct"] == 3.5
    assert view[0]["up"] == 5 and view[0]["down"] == 2
    assert view[-1]["name"] == "자동차"
    assert view[-1]["change_pct"] is None  # 0.0 아님
    assert view[-1]["up"] is None and view[-1]["down"] is None


# ③ 종목에 relations 가 붙는다


def test_symbols_carry_relations():
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비 공급",
         "evidence_score": 70, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
    ]}
    view = build_sector_view(_sector_map(), _sector_quotes(), _symbols(), relations)
    semi = next(s for s in view if s["name"] == "반도체와반도체장비")
    sym005930 = next(m for m in semi["symbols"] if m["symbol"] == "005930")
    assert [r["symbol"] for r in sym005930["relations"]] == ["042700"]
    assert sym005930["relations"][0]["reason"] == "HBM 장비 공급"

    sym000660 = next(m for m in semi["symbols"] if m["symbol"] == "000660")
    assert sym000660["relations"] == []  # relations 없는 종목은 빈 목록


# ④ sector_map 이 비면 []


def test_empty_sector_map_returns_empty_list():
    assert build_sector_view({}, _sector_quotes(), _symbols(), {}) == []


# ── 종목 순서: 업종 내에서도 등락률 내림차순, 미확인은 뒤로 ──


def test_members_within_sector_sorted_by_change_pct_desc():
    view = build_sector_view(_sector_map(), _sector_quotes(), _symbols(), {})
    semi = next(s for s in view if s["name"] == "반도체와반도체장비")
    assert [m["symbol"] for m in semi["symbols"]] == ["005930", "000660"]


# ── symbols 가 비면 어떤 업종도 나오지 않는다 ──


def test_no_universe_symbols_yields_empty_list():
    assert build_sector_view(_sector_map(), _sector_quotes(), {}, {}) == []


# ══════════════════════════════════════════════════════════════════════════
# T3 (H-1c): sector_members 로 업종 전체 멤버 표시 — 사용자 재현 케이스
# (무선통신서비스: 네이버는 4종목인데 우리는 유니버스 교집합 1종목만 보였다)
# ══════════════════════════════════════════════════════════════════════════


def _telecom_sector_map():
    return {"017670": "무선통신서비스"}


def _telecom_quotes():
    return [{"no": "333", "name": "무선통신서비스", "change_pct": 4.2, "up": 2, "flat": 1, "down": 1}]


def _telecom_symbols():
    return {"017670": {"name": "SK텔레콤", "change_pct": 10.32}}


def _telecom_members():
    return {"무선통신서비스": [
        {"code": "017670", "name": "SK텔레콤", "change_pct": 10.32},
        {"code": "032640", "name": "LG유플러스", "change_pct": 2.45},
        {"code": "041560", "name": "프리티", "change_pct": 0.0},
        {"code": "079960", "name": "와이어블", "change_pct": -1.03},
    ]}


def test_sector_members_given_shows_full_members_not_just_universe():
    view = build_sector_view(
        _telecom_sector_map(), _telecom_quotes(), _telecom_symbols(), {},
        sector_members=_telecom_members(),
    )
    sector = next(s for s in view if s["name"] == "무선통신서비스")
    assert len(sector["symbols"]) == 4
    names = {m["name"] for m in sector["symbols"]}
    assert names == {"SK텔레콤", "LG유플러스", "프리티", "와이어블"}


def test_summary_counts_match_member_list_length():
    """상승 N·하락 N 요약과 멤버 목록 개수가 일치한다 — 사용자가 본 모순 해소."""
    view = build_sector_view(
        _telecom_sector_map(), _telecom_quotes(), _telecom_symbols(), {},
        sector_members=_telecom_members(),
    )
    sector = next(s for s in view if s["name"] == "무선통신서비스")
    assert sector["up"] + sector["flat"] + sector["down"] == len(sector["symbols"])


def test_universe_member_marked_in_universe_with_relations_others_plain():
    relations = {"017670": [
        {"dst": "035420", "kind": "beneficiary", "reason": "테스트",
         "evidence_score": 70, "last_verified": "2026-08-16"},
    ]}
    view = build_sector_view(
        _telecom_sector_map(), _telecom_quotes(), _telecom_symbols(), relations,
        sector_members=_telecom_members(),
    )
    sector = next(s for s in view if s["name"] == "무선통신서비스")
    skt = next(m for m in sector["symbols"] if m["symbol"] == "017670")
    assert skt["in_universe"] is True
    assert [r["symbol"] for r in skt["relations"]] == ["035420"]

    lguplus = next(m for m in sector["symbols"] if m["symbol"] == "032640")
    assert lguplus["in_universe"] is False
    assert lguplus["relations"] == []


def test_selected_sectors_are_universe_owned_union_top10_by_change_pct():
    """표시 업종 = 유니버스 보유 업종 ∪ 등락률 상위10."""
    quotes = [
        {"no": str(i), "name": f"업종{i}", "change_pct": 20.0 - i, "up": 1, "flat": 0, "down": 0}
        for i in range(10)
    ]
    quotes.append({"no": "333", "name": "무선통신서비스", "change_pct": 1.0, "up": 1, "flat": 0, "down": 0})
    quotes.append({"no": "999", "name": "업종밖", "change_pct": 0.5, "up": 1, "flat": 0, "down": 0})
    sector_map = {"017670": "무선통신서비스"}
    symbols = {"017670": {"name": "SK텔레콤", "change_pct": 10.32}}
    members = {"무선통신서비스": [{"code": "017670", "name": "SK텔레콤", "change_pct": 10.32}]}
    for i in range(10):
        members[f"업종{i}"] = [{"code": f"00000{i}", "name": f"종목{i}", "change_pct": 20.0 - i}]

    view = build_sector_view(sector_map, quotes, symbols, {}, sector_members=members)
    names = {s["name"] for s in view}
    assert "무선통신서비스" in names  # 유니버스 보유 — 순위 밖이어도 포함
    assert "업종밖" not in names  # 유니버스 없고 상위10 밖 — 제외
    assert len(names) == 11  # top10 + 무선통신서비스(중복 없이)


def test_no_universe_overlap_returns_empty_even_with_sector_members():
    """미국 리포트처럼 유니버스가 sector_map 과 전혀 겹치지 않으면(코드 형식이
    다른 시장) 무관한 업종 데이터를 새지 않는다 — 기존 '교집합 없으면 []' 동작 유지."""
    view = build_sector_view(
        _telecom_sector_map(), _telecom_quotes(), {"AAPL": {"name": "Apple", "change_pct": 1.0}}, {},
        sector_members=_telecom_members(),
    )
    assert view == []


def test_sector_members_none_keeps_backward_compat_universe_only():
    """sector_members 생략(None) 시 기존 동작(유니버스 교집합만) 그대로."""
    view = build_sector_view(_telecom_sector_map(), _telecom_quotes(), _telecom_symbols(), {})
    sector = next(s for s in view if s["name"] == "무선통신서비스")
    assert len(sector["symbols"]) == 1
    assert sector["symbols"][0]["symbol"] == "017670"


def test_partial_crawl_failure_falls_back_to_universe_but_up_down_go_none():
    """리뷰 결함: sector_members 에 특정 업종만 없으면(79페이지 순차 크롤
    부분 실패 — 드물지 않다) 그 업종은 유니버스 교집합으로 폴백하지만, 그
    목록은 전체 멤버가 아니다. up/flat/down 요약을 그대로 두면 "상승2·하락1
    인데 목록엔 1종목"이라는, 사용자가 신고한 원래 모순이 그대로 재발한다 —
    모르는 걸 아는 척하지 않는다: 이 업종은 요약 배지째로 미확인 처리한다.
    성공 경로(전체 멤버 확보)는 지금처럼 요약=목록 일치가 유지된다."""
    sector_map = {"005930": "반도체", "005380": "자동차"}
    symbols = {
        "005930": {"name": "삼성전자", "change_pct": 3.5},
        "005380": {"name": "현대차", "change_pct": -0.5},
    }
    quotes = [
        {"no": "1", "name": "반도체", "change_pct": 5.0, "up": 3, "flat": 0, "down": 0},
        {"no": "2", "name": "자동차", "change_pct": -0.5, "up": 0, "flat": 0, "down": 1},
    ]
    # "자동차"는 sector_members 에서 빠져 있다 — 그 업종만 크롤 실패했다고 가정
    members = {"반도체": [
        {"code": "005930", "name": "삼성전자", "change_pct": 3.5},
        {"code": "000660", "name": "SK하이닉스", "change_pct": 6.0},
        {"code": "042700", "name": "한미반도체", "change_pct": 5.0},
    ]}

    view = build_sector_view(sector_map, quotes, symbols, {}, sector_members=members)

    semi = next(s for s in view if s["name"] == "반도체")
    assert len(semi["symbols"]) == 3
    assert semi["up"] + semi["flat"] + semi["down"] == len(semi["symbols"])  # 성공 경로: 일치 유지

    auto = next(s for s in view if s["name"] == "자동차")
    assert len(auto["symbols"]) == 1  # 유니버스 교집합으로 폴백(현대차만)
    assert auto["symbols"][0]["symbol"] == "005380"
    assert auto["up"] is None and auto["flat"] is None and auto["down"] is None  # 요약 배지 부재
    assert auto["change_pct"] == -0.5  # 업종 자체 등락률(멤버 개수와 무관)은 그대로 노출


# ══════════════════════════════════════════════════════════════════════════
# build_top_movers — 네이버 홈 "업종상위/테마상위" 카드 (신규, 순수 함수)
# ══════════════════════════════════════════════════════════════════════════


def _sector_quotes_movers():
    return [
        {"no": "1", "name": "반도체", "change_pct": 5.0, "up": 3, "flat": 0, "down": 0},
        {"no": "2", "name": "정유", "change_pct": 4.0, "up": 2, "flat": 0, "down": 0},
        {"no": "3", "name": "화학", "change_pct": 3.0, "up": 2, "flat": 0, "down": 0},
        {"no": "4", "name": "자동차", "change_pct": 2.0, "up": 2, "flat": 0, "down": 0},
    ]


def _sector_members_movers():
    return {
        "반도체": [
            {"code": "005930", "name": "삼성전자", "change_pct": 6.0},
            {"code": "000660", "name": "SK하이닉스", "change_pct": 5.5},
            {"code": "042700", "name": "한미반도체", "change_pct": 4.0},
        ],
    }


def _themes_movers():
    return {
        "185": {"name": "정유", "change_pct": 6.48, "symbols": [
            {"code": "010950", "name": "S-Oil", "change_pct": 7.0, "reason": "정유", "value_traded": 1},
            {"code": "096770", "name": "SK이노베이션", "change_pct": 6.0, "reason": "정유", "value_traded": 1},
        ]},
        "42": {"name": "게임", "symbols": [  # change_pct 없음 — 제외
            {"code": "251270", "name": "넷마블", "change_pct": 1.0, "reason": "게임", "value_traded": 1},
        ]},
    }


def test_build_top_movers_returns_top3_sectors_and_themes_change_pct_gates():
    result = build_top_movers(_sector_quotes_movers(), _themes_movers(),
                               sector_members=_sector_members_movers())
    assert [s["name"] for s in result["sectors"]] == ["반도체", "정유", "화학"]
    assert [t["name"] for t in result["themes"]] == ["정유"]  # 게임은 change_pct 없어 제외


def test_build_top_movers_sector_representatives_top2_by_change_pct():
    result = build_top_movers(_sector_quotes_movers(), _themes_movers(),
                               sector_members=_sector_members_movers())
    semi = next(s for s in result["sectors"] if s["name"] == "반도체")
    assert [m["name"] for m in semi["symbols"]] == ["삼성전자", "SK하이닉스"]


def test_build_top_movers_theme_representatives_from_theme_symbols():
    result = build_top_movers(_sector_quotes_movers(), _themes_movers(),
                               sector_members=_sector_members_movers())
    oil = next(t for t in result["themes"] if t["name"] == "정유")
    assert [m["name"] for m in oil["symbols"]] == ["S-Oil", "SK이노베이션"]


def test_build_top_movers_without_sector_members_has_empty_representatives():
    """sector_members 없어도 업종 카드 자체는 유지 — 대표종목만 빈 목록(0 위장 금지)."""
    result = build_top_movers(_sector_quotes_movers(), _themes_movers())
    semi = next(s for s in result["sectors"] if s["name"] == "반도체")
    assert semi["symbols"] == []


def test_build_top_movers_all_lists_for_more_button():
    result = build_top_movers(_sector_quotes_movers(), _themes_movers(),
                               sector_members=_sector_members_movers())
    assert len(result["sectors_all"]) == 4
    assert {t["name"] for t in result["themes_all"]} == {"정유", "게임"}  # 더보기엔 change_pct 없는 것도


def test_build_top_movers_empty_inputs():
    assert build_top_movers([], {}) == {
        "sectors": [], "themes": [], "sectors_all": [], "themes_all": [],
    }
