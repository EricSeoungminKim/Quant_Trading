import re
from datetime import date, datetime, timedelta

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.render import (
    candidates_line, is_candidate, machine_payload, relation_items, render,
)

_AT = datetime(2026, 8, 12, 8, 0, tzinfo=KST)


def _snap() -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {})


def _snap_us() -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "US", date(2026, 8, 12), _AT, {})


def _snap_with_news(feeds: dict) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "news": SourceResult(
            key="news", ok=True, data={"feeds": feeds}, error=None,
            url="https://news.google.com", fetched_at=_AT, latency_ms=1,
        ),
    })


def _cont():
    return {
        "005930": {
            "name": "삼성전자",
            "days": 3,
            "articles": 5,
            "today_articles": 2,
            "streak_days": 3,
            "is_new": False,
            "history": [True] * 10,
            "titles": [],
        }
    }


def test_machine_payload_without_sym_quotes_omits_price_fields():
    """sym_quotes 생략 시 기존 호출부와 동일하게 동작한다 — 하위호환."""
    payload = machine_payload(_snap(), _cont(), {}, [])
    sym = payload["symbols"][0]
    assert sym["symbol"] == "005930"
    assert "close" not in sym
    assert "change_pct" not in sym


def test_machine_payload_with_sym_quotes_fills_price_fields():
    payload = machine_payload(
        _snap(), _cont(), {}, [],
        sym_quotes={"005930": {"close": 256250.0, "change_pct": 6.9}},
    )
    sym = payload["symbols"][0]
    assert sym["close"] == 256250.0
    assert sym["change_pct"] == 6.9


def test_machine_payload_sym_quotes_ignores_symbol_not_in_cont():
    """sym_quotes 에 있어도 cont(오늘 언급 종목)에 없는 코드는 payload 에 안 생긴다."""
    payload = machine_payload(
        _snap(), _cont(), {}, [],
        sym_quotes={"000660": {"close": 1000.0, "change_pct": -1.0}},
    )
    assert len(payload["symbols"]) == 1
    assert payload["symbols"][0]["symbol"] == "005930"
    assert "close" not in payload["symbols"][0]


# ── baselines (§E-2) ──────────────────────────────────────────

def test_machine_payload_without_baselines_omits_field():
    """baselines 생략 시 기존 호출부와 동일 — 하위호환."""
    payload = machine_payload(_snap(), _cont(), {}, [])
    sym = payload["symbols"][0]
    assert "baseline_score100" not in sym


def test_machine_payload_with_baselines_fills_baseline_score100():
    payload = machine_payload(
        _snap(), _cont(), {}, [],
        baselines={"005930": 62},
    )
    sym = payload["symbols"][0]
    assert sym["baseline_score100"] == 62


def test_machine_payload_baselines_ignores_symbol_not_in_cont():
    """baselines 에 있어도 cont(오늘 언급 종목)에 없는 코드는 payload 에 안 생긴다."""
    payload = machine_payload(
        _snap(), _cont(), {}, [],
        baselines={"000660": 40},
    )
    assert "baseline_score100" not in payload["symbols"][0]


def test_machine_payload_baseline_score100_zero_is_not_dropped():
    """0 은 유효한 점수다(채점 불가와 다르다) — falsy 라고 빠지면 baseline.py 의
    'None 은 채점 불가, 0 은 최하위 점수' 계약이 render 단계에서 깨진다."""
    payload = machine_payload(_snap(), _cont(), {}, [], baselines={"005930": 0})
    assert payload["symbols"][0]["baseline_score100"] == 0


# ── news_diversity ────────────────────────────────────────────


def test_machine_payload_news_diversity_none_without_news_source():
    payload = machine_payload(_snap(), _cont(), {}, [])
    assert payload["news_diversity"] is None


def test_machine_payload_news_diversity_summarizes_outlets():
    snap = _snap_with_news({
        "한국경제_경제": [
            {"title": "a", "link": "https://a", "published": None, "outlet": ""},
            {"title": "b", "link": "https://b", "published": None, "outlet": ""},
        ],
        "구글_코스피": [
            {"title": "c", "link": "https://c", "published": None, "outlet": "매일경제"},
        ],
    })
    payload = machine_payload(snap, {}, {}, [])
    diversity = payload["news_diversity"]
    assert diversity["total_articles"] == 3
    assert diversity["distinct_outlets"] == 2
    assert diversity["by_outlet"]["한국경제"] == 2
    assert diversity["by_outlet"]["매일경제"] == 1


# ── trending ─────────────────────────────────────────────────


def _trending():
    return {
        "005930": {
            "score": 5, "score100": 73, "label": "약한 트렌딩",
            "factors": [{"key": "board:거래대금", "delta": 2, "text": "거래대금 1위"}],
            "boards": {"거래대금": 1},
            "relative_volume": 2.5,
            "baseline_days": 5,
        }
    }


def test_machine_payload_without_trending_omits_trending_fields():
    """trending 생략 시 기존 호출부와 동일 — 하위호환."""
    payload = machine_payload(_snap(), _cont(), {}, [])
    sym = payload["symbols"][0]
    assert "trending_score" not in sym
    assert "relative_volume" not in sym


def test_machine_payload_with_trending_fills_fields():
    payload = machine_payload(_snap(), _cont(), {}, [], trending=_trending())
    sym = payload["symbols"][0]
    assert sym["trending_score"] == 5
    assert sym["trending_score100"] == 73
    assert sym["trending_label"] == "약한 트렌딩"
    assert sym["trending_factors"] == ["board:거래대금"]
    assert sym["trending_boards"] == {"거래대금": 1}
    assert sym["relative_volume"] == 2.5
    assert sym["trending_baseline_days"] == 5


def test_machine_payload_trending_distinguishes_news_only_from_corroborated():
    """뉴스만 있고 트렌딩 근거가 없는 종목과, 둘 다 있는 종목이 payload 에서
    구분돼야 한다 — 근거 유무를 뭉개면 안 된다."""
    cont = {
        **_cont(),
        "003540": {"name": "대신증권", "days": 1, "articles": 3, "today_articles": 3,
                   "streak_days": 1, "is_new": False, "history": [], "titles": []},
    }
    trending = {
        **_trending(),
        "003540": {"score": 0, "score100": 50, "label": "중립", "factors": [],
                   "boards": {}, "relative_volume": None, "baseline_days": 0},
    }
    payload = machine_payload(_snap(), cont, {}, [], trending=trending)
    by_symbol = {s["symbol"]: s for s in payload["symbols"]}
    assert by_symbol["005930"]["trending_score100"] == 73
    assert by_symbol["003540"]["trending_score100"] == 50
    assert by_symbol["003540"]["trending_factors"] == []


# ── sector·relations (news deepdive Task 6) ────────────────────


def test_machine_payload_carries_sector_and_relations():
    """Task 5 아티팩트가 있으면 sector 가 붙고 relations 는 임계 미달을 거른다."""
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비",
         "evidence_score": 70, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
        {"dst": "039030", "kind": "beneficiary", "reason": "약한 근거",
         "evidence_score": 30, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
    ]}
    sectors = {"005930": "반도체와반도체장비"}
    payload = machine_payload(_snap(), _cont(), {}, [], relations=relations, sectors=sectors)
    sym = next(s for s in payload["symbols"] if s["symbol"] == "005930")
    assert sym["sector"] == "반도체와반도체장비"
    assert [r["symbol"] for r in sym["relations"]] == ["042700"]  # 임계 미달은 빠진다
    assert sym["relations"][0]["kind"] == "beneficiary"


def test_machine_payload_without_relations_and_sector_unchanged():
    """신규 인자 없이 부르면 기존 호출부와 동일 — 하위호환."""
    payload = machine_payload(_snap(), _cont(), {}, [])
    sym = payload["symbols"][0]
    assert "relations" not in sym and "sector" not in sym


def test_machine_payload_relations_carry_last_verified():
    """소비 계약(I4) — relations 항목에 last_verified 가 있어야 30일 재검증
    로직(후속)이 "이 관계가 언제 마지막으로 확인됐나"를 판단할 수 있다."""
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비",
         "evidence_score": 70, "first_seen": "2026-08-01", "last_verified": "2026-08-15"},
    ]}
    payload = machine_payload(_snap(), _cont(), {}, [], relations=relations)
    sym = payload["symbols"][0]
    assert sym["relations"][0]["last_verified"] == "2026-08-15"


def test_machine_payload_relations_as_of_reflects_latest_verification():
    """I1b — LLM 이 죽어도 리포트 소비자가 관계 데이터의 신선도를 볼 수 있어야
    한다. top-level relations_as_of 는 스냅샷 전체에서 가장 최근 last_verified."""
    relations = {
        "005930": [{"dst": "042700", "kind": "beneficiary", "reason": "r",
                    "evidence_score": 70, "first_seen": "2026-08-01",
                    "last_verified": "2026-08-10"}],
        "000660": [{"dst": "039030", "kind": "supplier", "reason": "r",
                    "evidence_score": 60, "first_seen": "2026-08-01",
                    "last_verified": "2026-08-15"}],
    }
    payload = machine_payload(_snap(), _cont(), {}, [], relations=relations)
    assert payload["relations_as_of"] == "2026-08-15"


def test_machine_payload_relations_as_of_absent_without_relations():
    """relations 인자가 없으면(하위호환) relations_as_of 키 자체가 없다."""
    payload = machine_payload(_snap(), _cont(), {}, [])
    assert "relations_as_of" not in payload


def test_machine_payload_relations_carry_source_and_via_theme_when_present():
    """I4 — engine.json 소비자가 naver_theme 팩트 경로와 LLM 추론 경로를
    `source` 키로 구분할 수 있어야 한다. `via_theme` 는 있을 때만 싣는다."""
    relations = {"005930": [
        {"dst": "039030", "kind": "beneficiary", "reason": "정유 테마 편입사유",
         "evidence_score": 70, "via_theme": "정유", "source": "naver_theme"},
    ]}
    payload = machine_payload(_snap(), _cont(), {}, [], relations=relations)
    rel = payload["symbols"][0]["relations"][0]
    assert rel["source"] == "naver_theme"
    assert rel["via_theme"] == "정유"


def test_machine_payload_relations_via_theme_absent_when_not_llm():
    """LLM 경로(via_theme 없음)는 키 자체가 안 생긴다 — 빈 값으로 채우지 않는다."""
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비",
         "evidence_score": 70, "source": "llm"},
    ]}
    payload = machine_payload(_snap(), _cont(), {}, [], relations=relations)
    rel = payload["symbols"][0]["relations"][0]
    assert rel["source"] == "llm"
    assert "via_theme" not in rel


# ── 캘린더 날짜 박스 ──────────────────────────────────────────

from quant.analyze.render import NEAR_LABELS, group_events_by_day  # noqa: E402


def _ev(days, name="X", hi=True):
    return {"name": name, "date": f"2026-08-{12 + days:02d}", "dday": "x",
            "days_ahead": days, "high_impact": hi, "weekday": "수"}


def test_near_boxes_always_exist_even_when_empty():
    near, later = group_events_by_day([])
    assert [b["label"] for b in near] == list(NEAR_LABELS)
    assert all(b["events"] == [] for b in near)
    assert later == []


def test_events_land_in_the_right_day_box():
    near, _ = group_events_by_day([_ev(0, "CPI"), _ev(1, "PPI"), _ev(2, "소매판매")])
    assert [b["events"][0]["name"] for b in near] == ["CPI", "PPI", "소매판매"]


def test_multiple_events_on_one_day_all_listed():
    near, _ = group_events_by_day([_ev(1, "PPI"), _ev(1, "실업수당")])
    assert len(near[1]["events"]) == 2
    assert near[0]["events"] == [] and near[2]["events"] == []


def test_beyond_three_days_goes_to_later_sorted():
    _, later = group_events_by_day([_ev(9, "B"), _ev(3, "A"), _ev(9, "A")])
    assert [(e["days_ahead"], e["name"]) for e in later] == [(3, "A"), (9, "A"), (9, "B")]


def test_low_impact_events_are_excluded():
    """캘린더 원본은 440건이다 — 고영향만 세워야 읽힌다."""
    near, later = group_events_by_day([_ev(0, "잡음", hi=False), _ev(5, "잡음2", hi=False)])
    assert all(b["events"] == [] for b in near) and later == []


# ── RANK 태그는 매수세 근거일 때만 (2026-08-13) ─────────────────────────────


def _cand(**kw):
    base = {"today_articles": 3, "streak_days": 0, "is_new": False,
            "in_ranking": True, "ranking_bullish": True}
    return {**base, **kw}


def test_rank_tag_requires_bullish_ranking():
    """하락률 보드에만 오른 종목에 RANK 를 달면 롱 온리 전략이 그걸 우선 진입한다."""
    line = candidates_line({"263750": _cand(ranking_bullish=False)}, {})
    assert "263750" in line
    assert "RANK" not in line


def test_rank_tag_present_when_bullish():
    line = candidates_line({"005930": _cand()}, {})
    assert "RANK" in line


def test_rank_tag_falls_back_to_in_ranking_when_field_absent():
    """ranking_bullish 가 없는 옛 페이로드도 깨지지 않는다."""
    c = _cand()
    del c["ranking_bullish"]
    assert "RANK" in candidates_line({"005930": c}, {})


def test_declining_only_symbol_is_not_a_candidate():
    """뉴스도 연속성도 없이 하락률 보드 하나로 후보가 되면 안 된다."""
    assert not is_candidate({"today_articles": 0, "streak_days": 0,
                             "in_ranking": True, "ranking_bullish": False})
    assert is_candidate({"today_articles": 0, "streak_days": 0,
                         "in_ranking": True, "ranking_bullish": True})


# ── 종목 뉴스 전량 표시 (2026-08-16, 리포트 고도화 B Task 3) ──────────────


def _titles(n):
    return [{"title": f"제목{i}", "link": f"https://x/{i}"} for i in range(n)]


def _cont_with_titles(n):
    return {
        "005930": {
            "name": "삼성전자", "days": 1, "articles": n, "today_articles": n,
            "streak_days": 1, "is_new": False, "history": [], "titles": _titles(n),
        }
    }


def test_symbol_card_shows_all_titles_beyond_three_inside_details():
    """titles 는 이미 오늘치 전부다 — 템플릿의 [:3] 슬라이스가 잘라내던 것뿐이니
    나머지는 details 안에 전부 나와야 한다."""
    html = render(_snap(), _cont_with_titles(7))
    for i in range(7):
        assert f"제목{i}" in html
    assert html.count("<details>") == 2  # 기존 '엔진 판독값' 1개 + 이번 것 1개
    assert "4건 더" in html


def test_symbol_card_no_details_when_three_or_fewer_titles():
    """3건 이하면 접을 게 없다 — 빈 <details> 를 만들면 안 된다."""
    html = render(_snap(), _cont_with_titles(2))
    assert "제목0" in html and "제목1" in html
    assert html.count("<details>") == 1  # '엔진 판독값' 뿐, 뉴스용은 없음


# ── 제목 클러스터 배지 + 전량 유지 (2026-08-16, H-2 Task 3) ─────────────


def _cont_with_dupes(*titles):
    items = [{"title": t, "link": f"https://x/{i}"} for i, t in enumerate(titles)]
    return {
        "005930": {
            "name": "삼성전자", "days": 1, "articles": len(items),
            "today_articles": len(items), "streak_days": 1, "is_new": False,
            "history": [], "titles": items,
        }
    }


_SAME_EVENT_A = "SK텔레콤, 앤트로픽 지분가치 기대감에 급등"
_SAME_EVENT_B = "SK텔레콤 주가 급등…앤트로픽 지분가치 기대"


def test_symbol_card_shows_dup_badge_and_keeps_all_titles_beyond_three():
    """5건 중 2건이 같은 사건 — 대표만 위에 보이고 배지가 붙되, 원본 5건은
    전부(중복 포함) details 안에서 찾을 수 있어야 한다(정보 소실 금지)."""
    html = render(_snap(), _cont_with_dupes(
        _SAME_EVENT_A, _SAME_EVENT_B,
        "카카오, 신규 AI 서비스 출시 예고",
        "네이버, 클라우드 사업 분사 검토",
        "LG에너지솔루션, 미국 공장 증설 발표",
    ))
    assert "외 1개 매체" in html
    for t in (_SAME_EVENT_A, _SAME_EVENT_B, "카카오, 신규 AI 서비스 출시 예고",
              "네이버, 클라우드 사업 분사 검토", "LG에너지솔루션, 미국 공장 증설 발표"):
        assert t in html
    assert "2건 더" in html  # 5건 - 시각적 상단 3건
    assert html.count("<details>") == 2


def test_symbol_card_dup_badge_keeps_both_titles_even_with_three_or_fewer():
    """대표만 셋 다 보여도(원본은 3건) 클러스터로 묶인 항목이 있으면 details 로
    전량을 다시 노출해야 한다 — 원본 건수 기준 접기 조건만으로는 부족하다."""
    html = render(_snap(), _cont_with_dupes(
        _SAME_EVENT_A, _SAME_EVENT_B, "카카오, 신규 AI 서비스 출시 예고",
    ))
    assert "외 1개 매체" in html
    assert _SAME_EVENT_A in html and _SAME_EVENT_B in html
    assert html.count("<details>") == 2


def test_symbol_card_no_badge_when_titles_are_all_distinct():
    """중복이 없으면 배지도, 추가 details 트리거도 없다 — 기존 동작 그대로."""
    html = render(_snap(), _cont_with_titles(2))
    assert "개 매체" not in html


# ── 수혜주 트리 (2026-08-16, 리포트 고도화 B Task 4) ────────────────────


def _relations():
    return {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비 공급",
         "evidence_score": 70, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
    ]}


def test_relation_items_filters_by_min_evidence():
    """임계 미달 + LLM 산(source 없음)은 빠진다."""
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "강한 근거", "evidence_score": 70},
        {"dst": "039030", "kind": "beneficiary", "reason": "약한 근거", "evidence_score": 30},
    ]}
    items = relation_items({}, relations, "005930")
    assert [it["symbol"] for it in items] == ["042700"]


def test_relation_items_keeps_naver_theme_regardless_of_score():
    """source=='naver_theme' 은 팩트라서 evidence_score 임계와 무관하게 노출된다
    (F Task 3 이 채점만 하고 안 거른 이유와 같다)."""
    relations = {"005930": [
        {"dst": "039030", "kind": "beneficiary", "reason": "정유 테마 편입사유",
         "evidence_score": 0, "via_theme": "정유", "source": "naver_theme"},
    ]}
    items = relation_items({}, relations, "005930")
    assert [it["symbol"] for it in items] == ["039030"]
    assert items[0]["via_theme"] == "정유"
    assert items[0]["source"] == "naver_theme"


def test_relation_items_resolves_name_from_cont_or_falls_back_to_code():
    cont = {"042700": {"name": "한미반도체"}}
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70},
        {"dst": "039030", "kind": "supplier", "reason": "r", "evidence_score": 70},
    ]}
    items = {it["symbol"]: it for it in relation_items(cont, relations, "005930")}
    assert items["042700"]["name"] == "한미반도체"
    assert items["039030"]["name"] == "039030"  # 사전에 없으면 코드 그대로 — 지어내지 않는다


def test_relation_items_translates_kind_to_korean():
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70},
    ]}
    assert relation_items({}, relations, "005930")[0]["kind"] == "수혜주"


def test_relation_items_prefers_dst_name_over_cont_and_code():
    """이름 해석 우선순위: 행의 dst_name → cont → 코드(리뷰 결함 c). naver_theme
    항목은 오늘 뉴스에 없어 cont 에 없는 게 주 케이스이므로 dst_name 이 먼저다."""
    cont = {"039030": {"name": "cont가 아는 이름"}}
    relations = {"005930": [
        {"dst": "039030", "kind": "beneficiary", "reason": "정유 테마 편입사유",
         "evidence_score": 0, "via_theme": "정유", "source": "naver_theme",
         "dst_name": "GS"},
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70},
    ]}
    items = {it["symbol"]: it for it in relation_items(cont, relations, "005930")}
    assert items["039030"]["name"] == "GS"       # dst_name 우선 — cont 무시
    assert items["042700"]["name"] == "042700"   # dst_name 없고 cont 에도 없음 — 코드 그대로


def test_symbol_card_renders_relation_tree_with_reason():
    """① relations 가 있으면 트리와 이유가 렌더된다."""
    html = render(_snap(), _cont(), relations=_relations())
    assert "HBM 장비 공급" in html
    assert "042700" in html
    assert "수혜주" in html


def test_symbol_card_omits_relation_tree_when_no_relations():
    """② relations 가 없으면 트리 마크업 자체가 없다."""
    html = render(_snap(), _cont())
    assert 'class="rel-tree"' not in html


def test_symbol_card_omits_relation_tree_when_symbol_has_no_relations():
    """빈 목록(다른 종목에만 relations 가 있는 경우)도 섹션 자체를 그리지 않는다."""
    html = render(_snap(), _cont(), relations={"999999": [
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70},
    ]})
    assert 'class="rel-tree"' not in html


def test_rel_tree_hides_score_for_naver_theme_shows_source_llm_shows_score():
    """리뷰 결함 b — naver_theme(팩트 축)은 근거점수를 감추고 출처만, LLM
    (추론 축)은 근거점수만 보여준다. 두 축이 같은 숫자선을 공유하지 않는다."""
    html = render(_snap(), _cont(), relations={"005930": [
        {"dst": "039030", "kind": "beneficiary", "reason": "정유 테마 편입사유",
         "evidence_score": 0, "via_theme": "정유", "source": "naver_theme",
         "dst_name": "GS"},
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비 공급",
         "evidence_score": 70, "source": "llm"},
    ]})
    assert "출처: 네이버 테마 편입사유" in html
    # naver_theme 항목(GS) 블록에는 "근거점수" 문구가 없어야 하고, LLM
    # 항목(한미반도체)에는 있어야 한다 — 각 rel-item 블록 단위로 쪼개서 검사.
    blocks = html.split('<div class="rel-item">')
    # rel-tree 는 <details> 안에서 끝난다 — 그 뒤(섹션 각주 등)는 이 종목의
    # 트리 블록이 아니므로 잘라낸다(I6 로 각주가 추가되며 "근거점수"라는
    # 단어가 페이지 하단에도 등장하게 됐다).
    naver_block = next(b for b in blocks if "GS" in b and "정유 테마 편입사유" in b).split("</details>")[0]
    llm_block = next(b for b in blocks if "HBM 장비 공급" in b)
    assert "근거점수" not in naver_block
    assert "근거점수 70" in llm_block


def test_symbol_card_escapes_script_tag_in_reason():
    """③ reason 에 <script>alert(1)</script> 를 넣어도 결과 HTML 에 실행 가능한
    태그가 없다 — |safe 를 쓰지 않는다."""
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "<script>alert(1)</script>",
         "evidence_score": 70},
    ]}
    html = render(_snap(), _cont(), relations=relations)
    assert "<script>" not in html
    assert "alert(1)" in html  # 텍스트 자체는 escape 되어 그대로 보인다(&lt;script&gt;)


def test_symbol_card_shows_via_theme_badge():
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70,
         "via_theme": "HBM"},
    ]}
    html = render(_snap(), _cont(), relations=relations)
    assert "[HBM]" in html


def test_symbol_card_shows_source_label_for_naver_theme_only():
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "r", "evidence_score": 70,
         "via_theme": "정유", "source": "naver_theme"},
        {"dst": "039030", "kind": "beneficiary", "reason": "r2", "evidence_score": 70},
    ]}
    html = render(_snap(), _cont(), relations=relations)
    assert "출처: 네이버 테마 편입사유" in html
    # LLM 산 항목(source 없음)에는 출처 문구가 붙지 않는다 — 위 문구는 1건뿐.
    assert html.count("출처: 네이버 테마 편입사유") == 1


def test_ranked_section_has_beginner_footnote_for_relations_and_flow():
    """I6 — 수혜주 트리(출처/근거점수)와 수급 기간 뷰(외/기/N일치) 용어를
    초보용으로 각주 처리."""
    html = render(_snap(), _cont())
    assert "네이버가 큐레이션한 테마 편입 사유" in html
    assert "함께 언급된 정도" in html
    assert "외</b>=외국인" in html and "기</b>=기관" in html
    assert "우리 원장에 쌓인 실측 일수" in html


# ── 테마별 시세 (2026-08-16, 리포트 고도화 B Task 5) ────────────────────


def _sector_view():
    return [{
        "name": "반도체와반도체장비", "change_pct": 3.5, "up": 5, "down": 2,
        "symbols": [{
            "symbol": "005930", "name": "삼성전자", "change_pct": 3.5,
            "relations": [{
                "symbol": "042700", "name": "한미반도체", "kind": "수혜주",
                "reason": "HBM 장비 공급", "score": 70, "via_theme": None, "source": None,
            }],
        }],
    }]


def test_sector_section_renders_sector_and_member_and_relation_tree():
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert "업종별 시세" in html  # I5 — "테마별 시세"는 수혜주 칩의 [테마] 와 용어가 충돌해 정정
    assert "반도체와반도체장비" in html
    assert "삼성전자" in html
    assert "한미반도체" in html  # rel_tree 재사용 — 수혜주까지 내려간다
    assert 'class="sector"' in html


def test_sector_section_shows_up_down_counts():
    """I5 — 업종 상승/하락 종목 수 노출(둘 다 있으면 표시)."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert "상승 5 · 하락 2" in html


def test_sector_section_omits_up_down_when_both_none():
    view = _sector_view()
    view[0]["up"] = None
    view[0]["down"] = None
    html = render(_snap(), _cont(), sector_view=view)
    assert "· 하락" not in html  # "상승"만으로는 CSS 셀렉터(강한 상승 신호 등)와 겹친다


def test_sector_section_omitted_when_sector_view_empty():
    """빈 섹션을 그리지 않는다 — sector_map/등락률 실패 시 스펙 실패 모드."""
    html = render(_snap(), _cont())
    assert "업종별 시세" not in html


def test_sector_section_shows_unconfirmed_for_none_change_pct():
    view = _sector_view()
    view[0]["change_pct"] = None
    view[0]["up"] = None
    view[0]["down"] = None
    html = render(_snap(), _cont(), sector_view=view)
    assert "등락률 미확인" in html


def test_sector_section_has_beginner_footnote():
    """I6 — 업종/네이버 테마 배지 용어를 초보용으로 각주 처리."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert "네이버 금융 업종 분류" in html
    assert "[배지]" in html and "네이버 테마" in html


def test_sector_detail_member_without_in_universe_key_still_bold_and_shows_relations():
    """하위호환: build_sector_view 가 in_universe 키를 안 실어도(레거시 산출)
    기존처럼 볼드+코드+수혜주 트리를 그대로 보여준다."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert "한미반도체" in html  # rel_tree 계속 렌더 — in_universe 부재로 숨지 않는다


def test_sector_detail_all_members_uniform_rows_universe_marked_with_chip():
    """2026-08-16 사용자 피드백: 유니버스만 볼드+코드로 그리니 같은 목록에서
    행 스타일이 제각각이라 깨진 UI 로 읽혔다. 새 계약 — 모든 멤버가 같은
    스타일(이름+코드)이고, 유니버스 소속은 '관심' 칩으로만 구분한다."""
    view = _sector_view()
    view[0]["symbols"][0]["in_universe"] = True
    view[0]["symbols"].append({
        "symbol": "032640", "name": "LG유플러스", "change_pct": 2.45,
        "in_universe": False, "relations": [],
    })
    html = render(_snap(), _cont(), sector_view=view)
    assert "LG유플러스" in html
    assert "032640" in html  # 비유니버스도 종목코드를 노출 — 행 스타일 통일
    assert "관심" in html  # 유니버스 소속 표시는 칩이 담당한다
    # 비유니버스 행에는 수혜주 트리가 없다 — 관계 데이터 자체가 유니버스에만 있다
    lg = html.index("LG유플러스")
    assert "rel-tree" not in html[lg:lg + 300]


def test_sector_member_flat_change_rendered_without_plus_sign():
    """보합 +0.00% 는 상승처럼 읽힌다(2026-08-16 사용자 피드백, 프리티 사례)
    — 0 은 부호 없이 0.00% 로 그린다."""
    view = _sector_view()
    view[0]["symbols"].append({
        "symbol": "006490", "name": "프리티", "change_pct": 0.0,
        "in_universe": False, "relations": [],
    })
    html = render(_snap(), _cont(), sector_view=view)
    assert "+0.00%" not in html
    assert "0.00%" in html


# ── 업종상위/테마상위 카드 (네이버 홈 패리티, H-1c Task 3) ────────────────


def _top_movers():
    return {
        "sectors": [
            {"name": "반도체", "change_pct": 5.0,
             "symbols": [{"code": "005930", "name": "삼성전자", "change_pct": 6.0},
                         {"code": "000660", "name": "SK하이닉스", "change_pct": 5.5}]},
        ],
        "themes": [
            {"name": "정유", "change_pct": 6.48,
             "symbols": [{"code": "010950", "name": "S-Oil", "change_pct": 7.0}]},
        ],
        "sectors_all": [{"name": "반도체", "change_pct": 5.0}, {"name": "자동차", "change_pct": 1.0}],
        "themes_all": [{"name": "정유", "change_pct": 6.48}, {"name": "게임", "change_pct": None}],
    }


def test_top_movers_card_renders_sector_and_theme_rows():
    html = render(_snap(), _cont(), top_movers=_top_movers())
    assert "업종상위" in html and "테마상위" in html
    assert "반도체" in html and "삼성전자" in html and "SK하이닉스" in html
    assert "정유" in html and "S-Oil" in html


def test_top_movers_card_more_button_lists_all():
    html = render(_snap(), _cont(), top_movers=_top_movers())
    assert "자동차" in html  # 상위3엔 없지만 더보기 전체 목록엔 있다
    assert "게임" in html


def test_top_movers_card_omitted_when_empty():
    html = render(_snap(), _cont())
    assert "업종상위" not in html and "테마상위" not in html


def test_top_movers_card_omitted_when_sectors_and_themes_both_empty():
    html = render(_snap(), _cont(), top_movers={"sectors": [], "themes": [], "sectors_all": [], "themes_all": []})
    assert "업종상위" not in html and "테마상위" not in html


# ── 업종 히트맵 (리포트 UX 2차, 2026-08-17 스펙 요구 3) ───────────────────
# 지도 원칙: "너는 알아도, 유저는 봤을 때 이해가 안되면 그건 필요없는
# 정보가 돼." 색·강도만으로 등락 규모가 읽혀야 한다 — 서버사이드 CSS
# 타일, JS 없음.


def _hm_sector_view(*pcts):
    """업종별 시세 히트맵 테스트용 — 이름·등락률만 있는 최소 sector_view."""
    return [
        {"name": f"업종{i}", "change_pct": p, "up": None, "down": None, "symbols": []}
        for i, p in enumerate(pcts, start=1)
    ]


def test_heatmap_intensity_classes_match_threshold_table():
    """|등락률| >=5→5, >=3→4, >=2→3, >=1→2, >0→1, 0/None→flat 초기값 임계."""
    view = _hm_sector_view(8.49, 0.5, -1.03, 0.0, None, -5.0, 3.0)
    html = render(_snap(), _cont(), sector_view=view)
    assert '<a class="hm u5" href="#sector-1">' in html  # +8.49 → u5
    assert '<a class="hm u1" href="#sector-2">' in html  # +0.5 → u1
    assert '<a class="hm d2" href="#sector-3">' in html  # -1.03 → d2
    assert '<a class="hm flat" href="#sector-4">' in html  # 0.0 → flat
    assert '<a class="hm flat" href="#sector-5">' in html  # None → flat
    assert '<a class="hm d5" href="#sector-6">' in html  # -5.0 → d5 (경계값 포함)
    assert '<a class="hm u4" href="#sector-7">' in html  # 3.0 → u4 (경계값 포함)


def test_sector_heatmap_tile_anchor_resolves_to_matching_details_id():
    """타일 href="#sector-N" 이 실제로 존재하는 <details id="sector-N"> 를 가리킨다."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert 'href="#sector-1"' in html
    assert 'id="sector-1"' in html


def test_sector_heatmap_grid_renders_above_detail_list():
    """히트맵(지도)이 먼저, 상세 <details> 드릴다운은 그 아래 그대로 유지."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    grid_idx = html.index("hm-grid")
    detail_idx = html.index('<details class="sector"')
    assert grid_idx < detail_idx


def test_heatmap_legend_present():
    """상승=초록/하락=빨강(회사 리포트 관례) — 2026-08-17 사용자 피드백
    ("빨간색 상승은 잘 안 보인다")로 색상 계약이 반전됐다."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    assert "색 = 전일 대비 등락률" in html
    assert '<span class="up">초록</span>=상승' in html
    assert '<span class="down">빨강</span>=하락' in html
    assert "진할수록 등락폭이 큼" in html


def test_heatmap_tiles_use_dedicated_green_red_vars_not_shared_up_down():
    """히트맵 타일은 --hm-up(초록)/--hm-down(빨강) 전용 변수를 쓴다 — 리포트
    전역의 --up/--down(KR 등락 텍스트 색, 상승=빨강 유지)과는 분리."""
    html = render(_snap(), _cont(), sector_view=_sector_view())
    style = html[html.index("<style>"):html.index("</style>")]
    assert "--hm-up:" in style and "--hm-down:" in style
    assert ".hm.u1{background:color-mix(in srgb, var(--hm-up)" in style
    assert ".hm.d1{background:color-mix(in srgb, var(--hm-down)" in style


def test_top_movers_strip_shows_up_to_six_tiles_per_column():
    html = render(_snap(), _cont(), top_movers=_top_movers())
    # _top_movers() 는 업종 2개(반도체·자동차)·테마 2개(정유·게임) 뿐이라 6장을
    # 채우지 못하지만, sectors(대표종목 포함 상위3) + sectors_all[3:6](이어붙임)
    # 으로 만든 타일 목록에 둘 다 들어간다.
    assert "반도체" in html and "자동차" in html
    assert "정유" in html and "게임" in html


def test_carried_candidates_grouped_by_sector_heatmap():
    carried = [
        {"symbol": "035420", "name": "NAVER", "carried_from": "2026-08-10",
         "sector": "서비스업", "change_pct": 1.2},
        {"symbol": "005380", "name": "현대차", "carried_from": "2026-08-11",
         "sector": "운수장비", "change_pct": -2.5},
    ]
    html = render(_snap(), _cont(), carried_candidates=carried)
    assert '<h3 class="cat">서비스업</h3>' in html
    assert '<h3 class="cat">운수장비</h3>' in html
    assert "08-10 발" in html and "08-11 발" in html


def test_carried_candidates_without_sector_fall_back_to_기타():
    carried = [{"symbol": "000001", "name": "무섹터종목", "carried_from": "2026-08-10"}]
    html = render(_snap(), _cont(), carried_candidates=carried)
    assert '<h3 class="cat">기타</h3>' in html
    assert "무섹터종목" in html


def test_carried_candidates_named_sectors_sort_before_기타():
    carried = [
        {"symbol": "000001", "name": "무섹터종목", "carried_from": "2026-08-10"},
        {"symbol": "035420", "name": "NAVER", "carried_from": "2026-08-10", "sector": "서비스업"},
    ]
    html = render(_snap(), _cont(), carried_candidates=carried)
    assert html.index('<h3 class="cat">서비스업</h3>') < html.index('<h3 class="cat">기타</h3>')


def test_carried_candidate_tile_is_the_link_no_nested_anchor():
    """스펙 요구 6 — 이월 후보 타일은 그 자체가 네이버 링크(중첩 <a> 금지)."""
    carried = [{"symbol": "035420", "name": "NAVER", "carried_from": "2026-08-10",
                "sector": "서비스업", "change_pct": -1.03}]
    html = render(_snap(), _cont(), carried_candidates=carried)
    assert '<a class="hm d2" href="https://finance.naver.com/item/main.naver?code=035420"' in html
    assert re.search(r"<a[^>]*><a", html) is None


def test_heatmap_sections_have_no_script_tag():
    html = render(
        _snap(), _cont(),
        sector_view=_sector_view(), top_movers=_top_movers(),
        carried_candidates=[{"symbol": "035420", "name": "NAVER",
                              "carried_from": "2026-08-10", "sector": "서비스업",
                              "change_pct": -1.03}],
    )
    assert "<script" not in html


# ── 외국인 수급 추종 (서브프로젝트 I, 2026-08-17 사용자 원칙) ────────────


def _foreign_view():
    return {
        "market_foreign_net": 1234,
        "market_date": "2026-08-17",
        "sectors": [{
            "name": "반도체와반도체장비",
            "net_sum": 500000,
            "rows": [{
                "symbol": "005930", "name": "삼성전자",
                "label": "매수 시그널(재유입)", "residual": 500000,
                "inst_follows": True, "days": 5,
                "series": [
                    {"date": "2026-08-10", "foreign_net": 100},
                    {"date": "2026-08-11", "foreign_net": -50},
                    {"date": "2026-08-12", "foreign_net": -30},
                    {"date": "2026-08-13", "foreign_net": 20},
                    {"date": "2026-08-14", "foreign_net": 80},
                    {"date": "2026-08-17", "foreign_net": 90},
                ],
            }],
        }],
    }


def test_foreign_view_section_renders_header_and_sector_rows():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert "외국인 수급 추종" in html
    assert "반도체와반도체장비" in html
    assert "삼성전자" in html
    assert "매수 시그널(재유입)" in html


def test_foreign_view_market_line_shows_kospi_composite_and_date():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert "코스피 종합 외국인" in html
    assert "2026-08-17" in html


def test_foreign_view_inflow_label_gets_on_chip():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert 'class="chip on">매수 시그널(재유입)' in html


def test_foreign_view_outflow_label_gets_down_chip():
    view = _foreign_view()
    view["sectors"][0]["rows"][0]["label"] = "이탈 추세(부분 매도/중립 고려)"
    html = render(_snap(), _cont(), foreign_view=view)
    assert 'class="chip down">이탈 추세(부분 매도/중립 고려)' in html


def test_foreign_view_watch_label_gets_base_chip_not_on_or_down():
    view = _foreign_view()
    view["sectors"][0]["rows"][0]["label"] = "관망(잔여 있음)"
    html = render(_snap(), _cont(), foreign_view=view)
    assert 'class="chip">관망(잔여 있음)' in html


def test_foreign_view_shows_residual_and_days():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert "5일 근거" in html


def test_foreign_view_inst_follows_true_shows_reference_chip():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert "기관 동행" in html


def test_foreign_view_inst_follows_false_hides_chip():
    view = _foreign_view()
    view["sectors"][0]["rows"][0]["inst_follows"] = False
    html = render(_snap(), _cont(), foreign_view=view)
    assert "기관 동행" not in html


def test_foreign_view_renders_daily_bars_svg():
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert 'class="ybar"' in html


def test_foreign_view_footnote_states_foreign_flow_is_main_logic():
    """사용자 원칙(2026-08-17) — 각주에 그대로 남긴다."""
    html = render(_snap(), _cont(), foreign_view=_foreign_view())
    assert "메인 로직은 외국인 매수세" in html
    assert "기관 매수세는 참고용" in html
    assert "네이버" in html  # 전 저점·현 고점 맥락은 네이버 차트로 위임


def test_foreign_view_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "외국인 수급 추종" not in html


def test_foreign_view_section_omitted_when_empty_dict():
    html = render(_snap(), _cont(), foreign_view={})
    assert "외국인 수급 추종" not in html


def test_foreign_view_market_line_omitted_when_net_none():
    view = _foreign_view()
    view["market_foreign_net"] = None
    html = render(_snap(), _cont(), foreign_view=view)
    assert "코스피 종합 외국인" not in html
    assert "외국인 수급 추종" in html  # 종목 섹션 자체는 그대로 렌더


# ── 당일 단타 후보 (서브프로젝트 K) ────────────────────────────────────────


def _intraday_view():
    return [
        {
            "symbol": "005930", "name": "삼성전자", "score100": 72,
            "factors": [
                {"name": "호재 뉴스", "pts": 15, "max": 40,
                 "evidence": "호재: 수주/공급계약 (+15); 다매체 5건≥3 (+10)"},
                {"name": "외국인 추세", "pts": 30, "max": 30,
                 "evidence": "매수 시그널(재유입) (+30)"},
                {"name": "촉매", "pts": 0, "max": 15, "evidence": "공시 촉매 없음"},
            ],
            "foreign_label": "매수 시그널(재유입)",
            "grade": "단타 적극 진입",
            "grade_reasons": ["종목 호재 뉴스", "섹터 상승 중", "외국인 순매수"],
        },
    ]


def test_intraday_section_renders_rank_name_score_and_factor_chips():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    assert "당일 단타 후보" in html
    assert "삼성전자" in html
    assert ">72<" in html
    assert "호재 뉴스 +15" in html
    assert "외국인 추세 +30" in html


def test_intraday_section_shows_evidence_as_visible_text_not_title_attribute():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    assert "호재: 수주/공급계약 (+15)" in html
    assert 'title="호재: 수주/공급계약 (+15); 다매체 5건≥3 (+10)"' not in html


def test_intraday_section_shows_foreign_label_chip():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    zone = html[html.index("당일 단타 후보"):]
    zone = zone[:zone.index("</section>")]
    assert "매수 시그널(재유입)" in zone


def test_intraday_section_shows_guidance_line():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    assert "검증 하네스(intraday_verify)가" in html
    assert "박스(손절·익절)를 좁게" in html


def test_intraday_section_sym_link_points_to_naver():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    zone = html[html.index("당일 단타 후보"):]
    zone = zone[:zone.index("</section>")]
    assert 'href="https://finance.naver.com/item/main.naver?code=005930"' in zone


def test_intraday_section_omitted_when_empty():
    html = render(_snap(), _cont(), intraday_view=[])
    assert "당일 단타 후보" not in html


def test_intraday_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "당일 단타 후보" not in html


def test_intraday_section_has_no_script_tag():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    assert "<script" not in html


# ── 3단계 행동 등급 카드(scalp_grade, 2026-08-18) ──────────────────────────

def test_intraday_section_shows_grade_badge_with_scalp_data_attribute():
    """등급 배지는 entry_grade(중기 관심 종목, `data-g` 1~5 숫자)와 다른
    속성(`data-sg`, 등급 문자열)을 쓴다 — 두 등급 체계 CSS 셀렉터가 서로
    오염되면 안 된다(`.grade-badge[data-sg=...]` CSS 주석 참고)."""
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    zone = html[html.index("당일 단타 후보"):]
    zone = zone[:zone.index("</section>")]
    assert 'data-sg="단타 적극 진입"' in zone
    assert "단타 적극 진입" in zone


def test_intraday_section_shows_grade_reason_chips():
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    zone = html[html.index("당일 단타 후보"):]
    zone = zone[:zone.index("</section>")]
    assert "종목 호재 뉴스" in zone
    assert "섹터 상승 중" in zone
    assert "외국인 순매수" in zone


def test_intraday_section_no_reasons_block_when_grade_reasons_empty():
    view = _intraday_view()
    view[0]["grade_reasons"] = []
    html = render(_snap(), _cont(), intraday_view=view)
    assert "당일 단타 후보" in html  # 카드 자체는 여전히 그려진다(정보 삭제 아님)


# ── 수급 기간 뷰 (2026-08-16, 리포트 고도화 B Task 6) ────────────────────


def _score():
    return {"005930": {"score": 1, "score100": 60, "label": "보통", "factors": []}}


def _flow_rows_3days():
    """세션일(2026-08-12) 포함 3일 연속 — 5일·10일치는 못 채운다."""
    return [
        {"date": "2026-08-10", "symbol": "005930", "foreign_net": 10, "inst_net": 1},
        {"date": "2026-08-11", "symbol": "005930", "foreign_net": 20, "inst_net": 2},
        {"date": "2026-08-12", "symbol": "005930", "foreign_net": 30, "inst_net": 3},
    ]


def test_symbol_card_shows_available_windows_with_honest_n_days():
    """① 3일치만 쌓였으면 1일 칸 + 있는 만큼(3일치) 칸만 나오고, 10일 칸처럼
    같은 데이터를 중복 표기하는 칸은 안 만든다.

    라벨은 2026-08-17 사용자 피드백(J)으로 "N일 중 M일치" → "원장에 N일 중
    M일 기록"으로 바뀌었다 — "일치"만으로는 무엇의 며칠인지 안 읽혔다."""
    html = render(_snap(), _cont(), scores=_score(), flow_rows=_flow_rows_3days())
    assert "원장에 1일 중 1일 기록" in html
    assert "원장에 5일 중 3일 기록" in html
    assert "원장에 10일 중 3일 기록" not in html  # 5일 칸과 값이 같아 중복 — 렌더 안 함
    assert "3일치 축적(2026-08-10 부터)" in html


def test_symbol_card_shows_not_yet_accumulated_when_zero_days():
    """② 원장에 이 종목 데이터가 하루도 없으면(그리고 기존 5일 표시도 없으면)
    KR 은 수급 칸 대신 '수급 미수집(랭킹 상위 6종목만 수집)' 문구 — "축적
    전"(곧 채워진다는 거짓 약속, I1)이 아니라 애초에 수집 대상이 아닐 수
    있다는 정직한 표기다. 0 으로 위장하지도 않는다."""
    html = render(_snap(), _cont(), scores=_score(), flow_rows=[])
    assert "수급 미수집(랭킹 상위 6종목만 수집)" in html
    assert "축적 전" not in html
    assert "외국인 5일" not in html


def test_symbol_card_hides_flow_placeholder_line_for_us_market():
    """I1 — US 는 원장을 아예 안 쌓으므로(KR 랭킹 상위 6종목 한정) 이 줄
    자체를 그리지 않는다. '수급 미수집' 문구가 US 리포트에 나오면 안 된다."""
    html = render(_snap_us(), _cont(), scores=_score(), flow_rows=[])
    assert "수급 미수집" not in html
    assert "축적 전" not in html


def test_symbol_card_period_view_replaces_5day_but_other_elements_survive():
    """③ 원장에 있는 종목은 기존 5일 표시(det.flow_summary) 대신 기간 뷰가
    뜨지만, 이름·칩·기타 카드 요소는 그대로다 — 회귀 없음.

    'N일 연속' 칩은 2026-08-17 사용자 피드백(J)으로 제거됐다 — 직관적이지
    않고 중요하지 않다는 지적."""
    details = {"005930": {"flow_summary": {
        "foreign_net_5d": 100, "inst_net_5d": -50,
        "foreign_buy_streak": 3, "inst_buy_streak": 0,
    }}}
    html = render(_snap(), _cont(), scores=_score(), details=details,
                  flow_rows=_flow_rows_3days())
    assert "외국인 5일" not in html  # 기존 5일 표시는 기간 뷰로 대체됐다
    assert "원장에 5일 중 3일 기록" in html
    assert "삼성전자" in html and "005930" in html
    assert "뉴스 오늘 2건" in html
    assert "3일 연속" not in html


# ── 종목 카드 헤더 정리 (2026-08-17, 리포트 UX 2차 스펙 요구 5·6) ─────────────
# 사용자 스크린샷 피드백(삼성전자 카드): 10칸 히스토리 박스·"2일 연속"·
# "10일 중 4일 · 누적 49건"은 직관적이지 않고 중요하지 않다. "전체 누적 외
# +276만주 · 기 -679만주"는 보유량처럼 읽혀 음수가 말이 안 된다.


def test_symbol_card_history_strip_removed():
    """10칸 히스토리 박스(streak SVG)와 그 안내 문구가 더는 렌더되지 않는다."""
    html = render(_snap(), _cont(), scores=_score())
    assert 'class="streak"' not in html
    assert "최근 10세션 등장 이력" not in html


def test_symbol_card_promotes_price_over_history():
    """히스토리 박스 자리는 가격(현재가+전일 대비)이 대신한다 — '전일 종가
    대비' 캡션으로 무엇 대비인지 밝힌다."""
    html = render(_snap(), _cont(), scores=_score(),
                   sym_quotes={"005930": {"close": 274500.0, "change_pct": 2.42}})
    assert "274,500" in html
    assert "+2.42%" in html
    assert "전일 종가 대비" in html


def test_symbol_card_news_chip_only_today_count():
    """뉴스 맥락 칩은 '뉴스 오늘 N건' 하나로 축약 — '일 연속'/'신규' 칩과
    '10일 중 M일 · 누적 N건' 줄은 제거."""
    html = render(_snap(), _cont(), scores=_score())
    assert "뉴스 오늘 2건" in html
    assert "일 연속" not in html
    assert "신규" not in html
    assert "10일 중" not in html
    assert "누적 5건" not in html


def test_symbol_card_flow_footnote_explains_net_buy():
    """'전체 누적 외 +276만주' 류가 보유량처럼 읽힌다는 지적 — 순매수 각주로
    음수 = 판 물량 우위임을 밝힌다."""
    html = render(_snap(), _cont(), scores=_score(), flow_rows=_flow_rows_3days())
    assert "순매수 = 매수" in html
    assert "보유량이 아니다" in html


def _flow_rows_long():
    """15일 연속 축적 — 5일/10일 칸을 넘어서는 장기 구간 칸(구 '전체 누적')을
    트리거한다."""
    return [
        {"date": (date(2026, 7, 29) + timedelta(days=i)).isoformat(),
         "symbol": "005930", "foreign_net": 10 + i, "inst_net": 1}
        for i in range(15)
    ]


def test_symbol_card_full_window_label_states_actual_days_not_vague_total():
    """'전체 누적'은 보유량처럼 읽혀서 문제였다 — 실제로 쌓인 일수를 라벨에
    그대로 박아 자기설명적으로 바꾼다."""
    html = render(_snap(), _cont(), scores=_score(), flow_rows=_flow_rows_long())
    assert "전체 누적" not in html
    assert "15일 순매수 합" in html


# ── 유튜브 브리핑 (2026-08-16, 리포트 고도화 B Task 7) ────────────────────
# _snap()/_AT 의 생성시각은 2026-08-12 08:00 KST(=2026-08-11 23:00 UTC) —
# "N시간 전"/"MM-DD HH:MM" 갈림길을 테스트하는 기준시각이다.


def _youtube_briefs(published: str = "2026-08-11T20:00:00+00:00"):
    return {
        "삼프로TV": [{
            "title": "<script>이 제목은 순수 텍스트다</script> & 코스피 전망",
            "link": "https://www.youtube.com/watch?v=i-4V4E8izfI",
            "published": published,
            "channel": "삼프로TV",
        }],
    }


def test_youtube_section_renders_channel_and_video_link():
    html = render(_snap(), _cont(), youtube=_youtube_briefs())
    assert "삼프로TV" in html
    assert 'href="https://www.youtube.com/watch?v=i-4V4E8izfI"' in html


def test_youtube_section_omitted_when_no_briefs():
    """전 채널 실패(빈 dict)면 섹션 자체를 그리지 않는다 — 빈 섹션 금지."""
    html = render(_snap(), _cont())
    assert "시황 브리핑" not in html


def test_youtube_title_is_escaped_not_rendered_as_html():
    """제목은 외부(유튜브) 입력이다 — |safe 로 그대로 꽂으면 안 되고 이스케이프돼야 한다."""
    html = render(_snap(), _cont(), youtube=_youtube_briefs())
    assert "<script>이 제목은 순수 텍스트다</script>" not in html
    assert "&lt;script&gt;" in html


def test_youtube_recent_video_shows_hours_ago():
    """발행 3시간 전(생성시각 기준) — 'N시간 전' 표기."""
    html = render(_snap(), _cont(), youtube=_youtube_briefs("2026-08-11T20:00:00+00:00"))
    assert "3시간 전" in html


def test_youtube_old_video_shows_month_day_time():
    """24시간을 넘긴 발행분은 절대시각(KST, MM-DD HH:MM)으로 표기한다."""
    html = render(_snap(), _cont(), youtube=_youtube_briefs("2026-08-09T20:00:00+00:00"))
    assert "08-10 05:00" in html


def test_youtube_section_has_beginner_footnote():
    """I6 — 유튜브 브리핑은 검증되지 않은 외부 콘텐츠라는 걸 초보에게 밝힌다."""
    html = render(_snap(), _cont(), youtube=_youtube_briefs())
    assert "내용 검증 없음, 참고용" in html


# ── 유튜브 브리핑 KR/US 고정 탭 (2026-08-18) ────────────────────────────────


def test_youtube_tabs_default_checked_matches_report_market():
    """기본 checked 탭은 리포트 시장(snap.market)이다."""
    html_kr = render(_snap(), _cont(), youtube=_youtube_briefs())
    assert 'id="yt-kr" checked' in html_kr
    assert 'id="yt-us" checked' not in html_kr

    html_us = render(_snap_us(), _cont(), youtube=_youtube_briefs())
    assert 'id="yt-us" checked' in html_us
    assert 'id="yt-kr" checked' not in html_us


def test_youtube_us_tab_shows_honest_empty_message_when_no_us_channel():
    """등록된 채널이 KR 전용이면(실측: 한국경제TV) US 탭은 빈 걸 숨기지 않고
    정직하게 문구를 보인다."""
    briefs = {
        "한국경제TV": [{
            "title": "코스피 마감 시황", "link": "https://www.youtube.com/watch?v=abc",
            "published": "2026-08-11T20:00:00+00:00", "channel": "한국경제TV",
        }],
    }
    html = render(_snap(), _cont(), youtube=briefs)
    assert "등록된 미국 채널 없음" in html


def test_youtube_kr_tab_shows_kr_only_channel():
    briefs = {
        "한국경제TV": [{
            "title": "코스피 마감 시황", "link": "https://www.youtube.com/watch?v=abc",
            "published": "2026-08-11T20:00:00+00:00", "channel": "한국경제TV",
        }],
    }
    html = render(_snap(), _cont(), youtube=briefs)
    assert "한국경제TV" in html
    assert "코스피 마감 시황" in html


def test_youtube_us_tab_shows_us_channel():
    """실측 분류(매경 월가월부 = US) — US 탭에 US 전용 채널도 보인다."""
    briefs = {
        "매경 월가월부": [{
            "title": "뉴욕브리핑", "link": "https://www.youtube.com/watch?v=xyz",
            "published": "2026-08-11T20:00:00+00:00", "channel": "매경 월가월부",
        }],
    }
    html = render(_snap(), _cont(), youtube=briefs)
    assert "매경 월가월부" in html
    assert "뉴욕브리핑" in html
    assert "등록된 미국 채널 없음" not in html


# ── 고정 브리핑 네이버 블로그 (2026-08-17, 서브프로젝트 I) ─────────────────
# 유튜브 브리핑과 같은 스냅샷 기준시각(_snap()/_AT = 2026-08-12 08:00 KST)을 쓴다.


def _blog_briefs(published: str = "2026-08-11T20:00:00+00:00"):
    return {
        "침착해": [{
            "title": "<script>이 제목은 순수 텍스트다</script> 코스피 선물 트레이딩 관점",
            "link": "https://blog.naver.com/stock_blog/224261045776",
            "published": published,
        }],
    }


def test_blog_section_renders_blog_and_post_link():
    html = render(_snap(), _cont(), blog=_blog_briefs())
    assert "침착해" in html
    assert 'href="https://blog.naver.com/stock_blog/224261045776"' in html


def test_blog_section_omitted_when_no_briefs():
    """전 블로그 실패(빈 dict)면 섹션 자체를 그리지 않는다 — 빈 섹션 금지."""
    html = render(_snap(), _cont())
    assert "네이버 블로그" not in html


def test_blog_title_is_escaped_not_rendered_as_html():
    """제목은 외부(네이버 블로그) 입력이다 — |safe 로 그대로 꽂으면 안 되고 이스케이프돼야 한다."""
    html = render(_snap(), _cont(), blog=_blog_briefs())
    assert "<script>이 제목은 순수 텍스트다</script>" not in html
    assert "&lt;script&gt;" in html


def test_blog_recent_post_shows_hours_ago():
    """발행 3시간 전(생성시각 기준) — 'N시간 전' 표기."""
    html = render(_snap(), _cont(), blog=_blog_briefs("2026-08-11T20:00:00+00:00"))
    assert "3시간 전" in html


def test_blog_section_has_beginner_footnote():
    """블로그 브리핑도 유튜브와 같이 검증되지 않은 외부 콘텐츠라는 걸 밝힌다."""
    html = render(_snap(), _cont(), blog=_blog_briefs())
    assert "내용 검증 없음, 참고용" in html


def test_blog_section_link_opens_new_tab():
    html = render(_snap(), _cont(), blog=_blog_briefs())
    assert 'target="_blank"' in html


# ── 증권사 리서치 목록 (2026-08-16, H-2 Task 5) ───────────────────────────


def test_symbol_card_shows_research_line_when_present():
    """report_cli._load_research 가 만들어 넘기는 문장을 그대로 노출한다 —
    '오늘 리포트: {증권사} — {제목}' 형태."""
    research = {"005930": ["오늘 리포트: 미래에셋증권 — 손에 잡힐 것도 같은 배당"]}
    html = render(_snap(), _cont(), research=research)
    assert "리포트" in html
    assert "오늘 리포트: 미래에셋증권 — 손에 잡힐 것도 같은 배당" in html


def test_symbol_card_omits_research_chip_when_no_match():
    """교집합이 0 이면(다른 종목·해당 없음) 칩 자체를 그리지 않는다."""
    html = render(_snap(), _cont(), research={})
    assert "<span class=\"chip on\">리포트</span>" not in html


def test_symbol_card_research_collapses_beyond_three_inside_details():
    """공시 칩과 같은 규약 — 4건 이상이면 <details> 로 접는다, 정보 소실 없음."""
    lines = [f"오늘 리포트: 증권사{i} — 제목{i}" for i in range(4)]
    html = render(_snap(), _cont(), research={"005930": lines})
    assert "<summary>4건</summary>" in html
    for line in lines:
        assert line in html


# ── 종목 클릭 → 네이버/야후 이동 (서브프로젝트 I) ──────────────────────


def _cont_us():
    return {
        "AAPL": {
            "name": "애플",
            "days": 3,
            "articles": 5,
            "today_articles": 2,
            "streak_days": 3,
            "is_new": False,
            "history": [True] * 10,
            "titles": [],
        }
    }


def test_kr_news_card_symbol_links_to_naver():
    """KR 리포트의 뉴스 노출 상위 종목 카드 — 종목명 클릭이 네이버 상세로 간다."""
    html = render(_snap(), _cont())
    assert "https://finance.naver.com/item/main.naver?code=005930" in html


def test_us_news_card_symbol_links_to_yahoo():
    """US 리포트(티커, 6자리 숫자 아님)는 야후 파이낸스로 간다."""
    html = render(_snap_us(), _cont_us())
    assert "https://finance.yahoo.com/quote/AAPL" in html


def test_symbol_link_has_noopener():
    html = render(_snap(), _cont())
    assert 'rel="noopener"' in html


def test_symbol_links_do_not_nest_inside_existing_anchors():
    """<a class="sym-link">가 기사 제목 <a> 등 기존 링크 안에 중첩되면 안 된다."""
    html = render(
        _snap(), _cont(),
        sector_view=_sector_view(),
        top_movers=_top_movers(),
        carried_candidates=[{"symbol": "035420", "name": "NAVER", "carried_from": "2026-08-10"}],
    )
    assert re.search(r"<a[^>]*><a", html) is None


# ── 오늘의 시황 — 사건 단위(서브프로젝트 I) ──────────────────────────────


def _digest(domestic=None, us_impact=None):
    return {"domestic": domestic or [], "us_impact": us_impact or []}


def test_digest_section_renders_domestic_and_us_impact_columns():
    digest = _digest(
        domestic=[{"title": "삼성전자, 3나노 파운드리 수주", "link": "https://a", "dup_count": 1}],
        us_impact=[{"title": "연준 파월 금리 동결 시사", "link": "https://b", "dup_count": 1}],
    )
    html = render(_snap(), _cont(), digest=digest)
    assert "오늘의 시황 — 사건 단위" in html
    assert "국내 시장" in html
    assert "미국발 — 한국 영향" in html
    assert '<a href="https://a" target="_blank" rel="noopener">삼성전자, 3나노 파운드리 수주</a>' in html
    assert '<a href="https://b" target="_blank" rel="noopener">연준 파월 금리 동결 시사</a>' in html


def test_digest_section_shows_multi_outlet_badge():
    digest = _digest(
        domestic=[{"title": "SK텔레콤 급등", "link": "https://a", "dup_count": 3}],
    )
    html = render(_snap(), _cont(), digest=digest)
    assert "외 2개 매체" in html


def test_digest_section_no_badge_for_single_outlet():
    digest = _digest(
        domestic=[{"title": "단독 보도 사건", "link": "https://a", "dup_count": 1}],
    )
    html = render(_snap(), _cont(), digest=digest)
    assert "개 매체" not in html


def test_digest_section_has_dedup_footnote():
    digest = _digest(domestic=[{"title": "사건", "link": "https://a", "dup_count": 1}])
    html = render(_snap(), _cont(), digest=digest)
    assert "다매체 보도량 순 — 같은 사건은 대표 1건(중복은 노이즈, 2026-08-17 사용자 원칙)" in html


def test_digest_section_omitted_when_both_lists_empty():
    html = render(_snap(), _cont(), digest=_digest())
    assert "오늘의 시황 — 사건 단위" not in html


def test_digest_section_omitted_when_digest_none():
    html = render(_snap(), _cont())
    assert "오늘의 시황 — 사건 단위" not in html


def test_digest_section_title_escaped_not_rendered_as_html():
    digest = _digest(domestic=[
        {"title": "<script>alert(1)</script>", "link": "https://a", "dup_count": 1},
    ])
    html = render(_snap(), _cont(), digest=digest)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── 시황 다이제스트 LLM 요약(스펙 §2, digest_prose) ────────────────────────


def test_digest_prose_renders_above_item_lists_with_ai_chip():
    digest = _digest(
        domestic=[{"title": "삼성전자 수주", "link": "https://a", "dup_count": 1}],
        us_impact=[{"title": "연준 금리 동결", "link": "https://b", "dup_count": 1}],
    )
    digest_prose = {
        "domestic_prose": "국내 시장은 조용했다.",
        "us_prose": "미국발 재료는 제한적이었다.",
    }
    html = render(_snap(), _cont(), digest=digest, digest_prose=digest_prose)
    assert "AI 요약" in html
    assert "국내 시장은 조용했다." in html
    assert "미국발 재료는 제한적이었다." in html
    # 각 블록에서 산문이 기사 목록보다 앞에 와야 한다(스펙: "목록 위에").
    assert html.index("국내 시장은 조용했다.") < html.index("삼성전자 수주")
    assert html.index("미국발 재료는 제한적이었다.") < html.index("연준 금리 동결")
    assert "요약은 서술만 — 사건 선정은 결정론(다매체 보도량)" in html


def test_digest_prose_absent_renders_clean_list_only():
    digest = _digest(domestic=[{"title": "삼성전자 수주", "link": "https://a", "dup_count": 1}])
    html = render(_snap(), _cont(), digest=digest)
    assert "AI 요약" not in html
    assert "요약은 서술만" not in html
    assert "삼성전자 수주" in html


def test_digest_prose_partial_only_domestic_chip_shown():
    """domestic_prose 만 있고 us_prose 가 없으면 국내 블록에만 AI 요약이 붙는다."""
    digest = _digest(
        domestic=[{"title": "삼성전자 수주", "link": "https://a", "dup_count": 1}],
        us_impact=[{"title": "연준 금리 동결", "link": "https://b", "dup_count": 1}],
    )
    digest_prose = {"domestic_prose": "국내 시장은 조용했다."}
    html = render(_snap(), _cont(), digest=digest, digest_prose=digest_prose)
    assert "국내 시장은 조용했다." in html
    # AI 요약 칩은 국내 블록 한 번만 나온다.
    assert html.count("AI 요약") == 1


# ── 오늘의 뉴스 흐름(리포트 UX 2차 요구 1, news_flow) ───────────────────────


def _flow_item(title, link, outlet="", dup_count=1, published_hhmm=None):
    item = {"title": title, "link": link, "outlet": outlet, "dup_count": dup_count}
    if published_hhmm:
        item["published_hhmm"] = published_hhmm
    return item


def test_news_flow_section_renders_after_digest_section():
    digest = _digest(domestic=[{"title": "다이제스트 사건", "link": "https://d", "dup_count": 1}])
    news_flow = [_flow_item("뉴스 흐름 사건", "https://a", outlet="한국경제")]
    html = render(_snap(), _cont(), digest=digest, news_flow=news_flow)
    assert "오늘의 뉴스 흐름" in html
    assert '<a href="https://a" target="_blank" rel="noopener">' in html
    assert "뉴스 흐름 사건" in html
    # 시황 다이제스트 섹션 다음에 온다(스펙 §1: "시황 다이제스트 바로 뒤").
    assert html.index("오늘의 시황 — 사건 단위") < html.index("오늘의 뉴스 흐름")


def test_news_flow_outlet_badge_always_shown():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "한국경제" in html


def test_news_flow_outlet_badge_shows_fallback_when_missing():
    news_flow = [_flow_item("사건", "https://a", outlet="")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "출처 미상" in html


def test_news_flow_multi_outlet_chip_shown_when_dup_count_ge_2():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제", dup_count=3)]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "외 2개 매체" in html


def test_news_flow_no_multi_outlet_chip_when_single():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제", dup_count=1)]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "개 매체" not in html


def test_news_flow_published_hhmm_shown_when_present():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제", published_hhmm="09:12")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "09:12" in html


def test_news_flow_top_12_visible_rest_in_details():
    news_flow = [
        _flow_item(f"사건{i}", f"https://{i}", outlet="한국경제") for i in range(15)
    ]
    html = render(_snap(), _cont(), news_flow=news_flow)
    for i in range(12):
        assert f"사건{i}" in html
    # 나머지 3건은 <details> 로 접히되 정보는 소실되지 않는다(그대로 존재).
    assert "사건12" in html
    assert "사건13" in html
    assert "사건14" in html
    assert "…외 3개 사건" in html
    assert "<details>" in html


def test_news_flow_no_details_when_12_or_fewer():
    news_flow = [_flow_item(f"사건{i}", f"https://{i}", outlet="한국경제") for i in range(5)]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "…외" not in html


def test_news_flow_has_source_diversity_footnote():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert (
        "모든 수집 매체의 사건 단위 흐름 — 시황 다이제스트(경제지 큐레이션)와 달리 전 매체를 보여준다"
        in html
    )


def test_news_flow_section_omitted_when_empty():
    html = render(_snap(), _cont(), news_flow=[])
    assert "오늘의 뉴스 흐름" not in html


def test_news_flow_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "오늘의 뉴스 흐름" not in html


def test_news_flow_title_escaped_not_rendered_as_html():
    news_flow = [_flow_item("<script>alert(1)</script>", "https://a", outlet="한국경제")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── DART 공시 유형 태그 + 후발 리스크 박스 노트(서브프로젝트 I Part B) ────


def test_disclosure_shows_type_tag_chip():
    html = render(_snap(), _cont(), disclosures={"005930": ["단일판매ㆍ공급계약체결"]})
    assert "단일판매ㆍ공급계약체결" in html
    assert '<span class="chip">수주</span>' in html


def test_disclosure_no_tag_chip_when_type_unrecognized():
    html = render(_snap(), _cont(), disclosures={"005930": ["기업설명회(IR)개최(안내공시)"]})
    assert "기업설명회(IR)개최(안내공시)" in html
    # 태그를 못 붙였다고 조용히 지어내지 않는다 — 일반 칩(수주 등)이 안 붙는다
    assert "<span class=\"chip\">수주</span>" not in html


def test_disclosure_has_late_risk_box_note_when_present():
    html = render(_snap(), _cont(), disclosures={"005930": ["최대주주변경"]})
    assert "공시발 재료는 퍼블릭 후발 리스크 — 진입 시 손절·익절 박스를 좁게 (2026-08-17 사용자 원칙)" in html


def test_disclosure_box_note_omitted_when_no_disclosures():
    html = render(_snap(), _cont(), disclosures={})
    assert "공시발 재료는 퍼블릭 후발 리스크" not in html


# ── 기술적 지표 — S&P 섹터 ETF·52주 신고/신저 종목도 네이버로(리포트 UX 2차 Task 4) ──
# URL 형태는 2026-08-17 curl 실측: 섹터 ETF(고정 11종)는 접미사 없이
# m.stock.naver.com/worldstock/etf/{ticker}/total 이 바로 200, 개별 종목은
# 거래소 접미사(.O/.K 등)를 우리 데이터로 결정론적으로 못 만들어 검색 결과
# 페이지(search?query=)로 보낸다 — 자세한 근거는 report.html.j2 의 매크로 주석.


def _snap_technical(sectors=None, breadth=None) -> Snapshot:
    results = {}
    if sectors is not None:
        results["sectors"] = SourceResult(
            key="sectors", ok=True, data=sectors, error=None,
            url="https://finance.yahoo.com", fetched_at=_AT, latency_ms=1,
        )
    if breadth is not None:
        results["breadth"] = SourceResult(
            key="breadth", ok=True, data=breadth, error=None,
            url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            fetched_at=_AT, latency_ms=1,
        )
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, results)


def _sectors_fixture():
    return {"sectors": [{"ticker": "XLK", "name": "기술", "change_pct": 1.23}]}


def _breadth_fixture():
    return {
        "highs": {"count": 1, "by_sector": {"기술": [{"ticker": "AAPL", "name": "Apple Inc."}]}},
        "lows": {"count": 1, "by_sector": {"금융": [{"ticker": "JPM", "name": "JPMorgan Chase & Co."}]}},
        "universe": 500,
        "as_of": "2026-08-14",
    }


def test_sp_sector_etf_links_to_naver_worldstock():
    """S&P 섹터 ETF 타일 클릭 → 네이버 해외증시 ETF 상세(실측: 접미사 불필요)."""
    html = render(_snap_technical(sectors=_sectors_fixture()), _cont())
    assert "https://m.stock.naver.com/worldstock/etf/XLK/total" in html


def test_breadth_52w_ticker_links_to_naver_search():
    """52주 신고/신저 개별 종목 클릭 → 네이버 검색(거래소 접미사 미상이라 폴백)."""
    html = render(
        _snap_technical(sectors=_sectors_fixture(), breadth=_breadth_fixture()), _cont(),
    )
    assert "https://m.stock.naver.com/search?query=AAPL" in html
    assert "https://m.stock.naver.com/search?query=JPM" in html


def test_technical_zone_links_have_noopener():
    html = render(
        _snap_technical(sectors=_sectors_fixture(), breadth=_breadth_fixture()), _cont(),
    )
    zone = html[html.index("기술적 지표"):]
    zone = zone[:zone.index("</section>")]
    assert zone.count('rel="noopener"') >= 2


def test_technical_zone_no_nested_anchors():
    html = render(
        _snap_technical(sectors=_sectors_fixture(), breadth=_breadth_fixture()), _cont(),
    )
    zone = html[html.index("기술적 지표"):]
    zone = zone[:zone.index("</section>")]
    assert re.search(r"<a[^>]*><a", zone) is None


# ── 52주 신고/신저 이름 병기 + 기준일 (리포트 UX 2차 L-3) ──────────────────
# "MRK·SOLV·TECH 는 실제 헬스케어 종목이나 티커만 보여서 TECH 가 섹터명처럼
# 읽히는 혼란" 피드백 — 티커 옆에 회사명을 병기하고, 회사 리포트와 날짜가
# 다른 데이터로 오해되지 않도록 기준일을 명시한다.


def test_breadth_52w_chip_shows_ticker_and_company_name():
    html = render(
        _snap_technical(sectors=_sectors_fixture(), breadth=_breadth_fixture()), _cont(),
    )
    assert "AAPL Apple Inc." in html
    # "JPMorgan Chase & Co."(20자)는 ~14자로 잘려 "JPMorgan Chase"까지만 보인다.
    assert "JPM JPMorgan Chase" in html


def test_breadth_52w_company_name_truncated_to_14_chars():
    breadth = {
        "highs": {"count": 1, "by_sector": {"기술": [
            {"ticker": "AAPL", "name": "A Very Long Company Name Inc."},
        ]}},
        "lows": {"count": 0, "by_sector": {}},
        "universe": 500,
        "as_of": "2026-08-14",
    }
    html = render(_snap_technical(sectors=_sectors_fixture(), breadth=breadth), _cont())
    assert "A Very Long Co" in html
    assert "A Very Long Company Name Inc." not in html


def test_breadth_52w_chip_falls_back_to_ticker_only_when_name_missing():
    breadth = {
        "highs": {"count": 1, "by_sector": {"기술": [{"ticker": "AAPL", "name": None}]}},
        "lows": {"count": 0, "by_sector": {}},
        "universe": 500,
        "as_of": "2026-08-14",
    }
    html = render(_snap_technical(sectors=_sectors_fixture(), breadth=breadth), _cont())
    zone = html[html.index("52주 신고/신저"):]
    zone = zone[:zone.index("</table>")]
    assert ">AAPL</a>" in zone


def test_breadth_52w_as_of_caption_present():
    html = render(
        _snap_technical(sectors=_sectors_fixture(), breadth=_breadth_fixture()), _cont(),
    )
    assert "기준일" in html
    assert "2026-08-14" in html


# ── Executive Summary (서브프로젝트 L, L-1, 2026-08-17) ──────────────────
# 리포트 최상단 통합 요약 — quant.analyze.exec_summary.summarize 산출물을
# 그대로 렌더한다. 판단은 exec_summary 모듈이 이미 끝냈고, 템플릿은 문단
# 세 개 + 근거 매체 각주 + AI 생성 칩만 그린다.


def _exec_summary(sources=("한국경제", "매일경제")):
    return {
        "market": "코스피가 강세로 마감했다.",
        "flow": "외국인이 순매수 우위를 보였다.",
        "catalyst": "삼성전자가 3나노 수주 소식을 냈다.",
        "sources": list(sources),
    }


def test_exec_summary_section_renders_three_paragraphs():
    html = render(_snap(), _cont(), exec_summary=_exec_summary())
    assert "Executive Summary" in html
    assert "코스피가 강세로 마감했다." in html
    assert "외국인이 순매수 우위를 보였다." in html
    assert "삼성전자가 3나노 수주 소식을 냈다." in html


def test_exec_summary_section_shows_ai_chip_and_source_footnote():
    html = render(_snap(), _cont(), exec_summary=_exec_summary())
    assert "AI 생성 — 근거는 본문 참조" in html
    assert "한국경제" in html and "매일경제" in html


# ── 섹션 AI 해석(리포트 UX 3차, 2026-08-17) ────────────────────────────
# 수급 체력/시장 심리/기술적 지표/유동성·금리 각 섹션에 실제 숫자를 조건부
# 서술로 재해석한 문단이 "AI 해석" 칩과 함께 붙는다 — section_advice 가
# None(또는 해당 키 결측)이면 그 섹션은 숫자 카드만으로 깔끔하게 끝난다.


def _snap_for_section_advice() -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "kr_funding": SourceResult(
            key="kr_funding", ok=True,
            data={"funding": {"투자자예탁금": {
                "value": 50.1, "unit": "조원", "change": 0.5, "change_pct": 1.0, "as_of": "2026-08-11",
            }}},
            error=None, url="https://freesis.kofia.or.kr", fetched_at=_AT, latency_ms=1,
        ),
        "sentiment": SourceResult(
            key="sentiment", ok=True,
            data={"cnn_fear_greed": {"value": 62, "rating_ko": "탐욕"}},
            error=None, url="https://edition.cnn.com", fetched_at=_AT, latency_ms=1,
        ),
        "vix_term": SourceResult(
            key="vix_term", ok=True,
            data={"points": [], "structure": "콘탱고 (정상)", "spread": 1.0, "move": None},
            error=None, url="https://finance.yahoo.com", fetched_at=_AT, latency_ms=1,
        ),
        "macro": SourceResult(
            key="macro", ok=True,
            data={
                "net_liquidity": {"value": 100.0, "date": "2026-08-11", "change": 10.0, "history": []},
                "series": {},
            },
            error=None, url="https://api.stlouisfed.org", fetched_at=_AT, latency_ms=1,
        ),
    })


def _section_advice() -> dict:
    return {
        "supply": "대기자금이 늘고 있는 국면에서는 매수 여력이 커진다. 판단과 책임은 투자자 본인 몫이다.",
        "sentiment": "탐욕 구간에서는 분할 접근이 일반적 대응이다. 판단과 책임은 투자자 본인 몫이다.",
        "technical": "콘탱고 구간에서는 변동성 급등 가능성이 낮게 읽힌다. 판단과 책임은 투자자 본인 몫이다.",
        "liquidity": "완화 국면에서는 위험자산에 우호적으로 해석되는 경우가 많다. 판단과 책임은 투자자 본인 몫이다.",
    }


def test_section_advice_renders_ai_chip_and_prose_in_each_section():
    html = render(_snap_for_section_advice(), _cont(), section_advice=_section_advice())
    assert html.count(">AI 해석<") == 4
    assert "대기자금이 늘고 있는 국면" in html
    assert "탐욕 구간에서는" in html
    assert "콘탱고 구간에서는" in html
    assert "완화 국면에서는" in html


def test_section_advice_clean_absence_when_none():
    html = render(_snap_for_section_advice(), _cont(), section_advice=None)
    assert ">AI 해석<" not in html


def test_section_advice_only_renders_keys_that_are_present():
    html = render(
        _snap_for_section_advice(), _cont(),
        section_advice={"supply": "대기자금 해석이다. 판단과 책임은 투자자 본인 몫이다."},
    )
    assert html.count(">AI 해석<") == 1
    assert "대기자금 해석이다" in html


def test_section_advice_has_no_script_tag():
    html = render(_snap_for_section_advice(), _cont(), section_advice=_section_advice())
    assert "<script" not in html


def test_exec_summary_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "Executive Summary" not in html


def test_exec_summary_section_omitted_when_empty_dict():
    html = render(_snap(), _cont(), exec_summary={})
    assert "Executive Summary" not in html


def test_exec_summary_section_renders_above_stance_block():
    """L-1 스펙 — 통합 요약이 스탠스 블록보다 먼저(리포트 최상단)."""
    view = {"label": "약한 상승 신호", "score100": 60, "line": "line",
            "positives": [], "negatives": []}
    html = render(_snap(), _cont(), view=view, exec_summary=_exec_summary())
    assert html.index("Executive Summary") < html.index("엔진 예측")


def test_exec_summary_section_renders_above_digest_and_news_flow():
    digest = {
        "domestic": [{"title": "삼성전자, 3나노 파운드리 수주", "link": "https://a", "dup_count": 1}],
        "us_impact": [],
    }
    news_flow = [{"title": "사건", "link": "https://a", "outlet": "한국경제", "dup_count": 1}]
    html = render(_snap(), _cont(), exec_summary=_exec_summary(), digest=digest, news_flow=news_flow)
    assert html.index("Executive Summary") < html.index("오늘의 시황 — 사건 단위")
    assert html.index("Executive Summary") < html.index("오늘의 뉴스 흐름")


def test_exec_summary_section_no_source_footnote_segment_when_sources_empty():
    html = render(_snap(), _cont(), exec_summary=_exec_summary(sources=[]))
    assert "근거 매체:" not in html


def test_exec_summary_paragraphs_escaped_not_rendered_as_html():
    summary = _exec_summary()
    summary["market"] = "<script>alert(1)</script>"
    html = render(_snap(), _cont(), exec_summary=summary)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_exec_summary_has_no_script_tag():
    html = render(_snap(), _cont(), exec_summary=_exec_summary())
    assert "<script" not in html


# ── 52주 신고/신저 종목 상세 직행 링크(리포트 UX 3차, 2026-08-17) ─────────
# 검색 랜딩(us_search_link)은 인기종목만 보인다는 사용자 지적 — technical.
# fetch_breadth_52w 가 네이버 자동완성 API로 미리 해석해둔 naver_path 가
# 있으면 그 종목 상세로 바로 보내고, 없으면(해석 실패) 검색 폴백을 유지한다.


def _snap_with_breadth(breadth_data: dict) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "US", date(2026, 8, 12), _AT, {
        "breadth": SourceResult(
            key="breadth", ok=True, data=breadth_data, error=None,
            url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            fetched_at=_AT, latency_ms=1,
        ),
    })


def _breadth(high_tk: dict, low_tk: dict) -> dict:
    return {
        "highs": {"count": 1, "by_sector": {"기술": [high_tk]}},
        "lows": {"count": 1, "by_sector": {"금융": [low_tk]}},
        "universe": 500,
        "as_of": "2026-08-11",
    }


def test_breadth_section_links_to_naver_stock_detail_when_naver_path_present():
    high_tk = {"ticker": "AAPL", "name": "Apple Inc.", "naver_path": "/worldstock/stock/AAPL.O"}
    low_tk = {"ticker": "JPM", "name": "JPMorgan", "naver_path": None}
    html = render(_snap_with_breadth(_breadth(high_tk, low_tk)), _cont())
    assert 'href="https://m.stock.naver.com/worldstock/stock/AAPL.O/total"' in html


def test_breadth_section_falls_back_to_search_link_when_naver_path_absent():
    high_tk = {"ticker": "AAPL", "name": "Apple Inc.", "naver_path": "/worldstock/stock/AAPL.O"}
    low_tk = {"ticker": "JPM", "name": "JPMorgan", "naver_path": None}
    html = render(_snap_with_breadth(_breadth(high_tk, low_tk)), _cont())
    assert 'href="https://m.stock.naver.com/search?query=JPM"' in html


# ── 랭킹 스냅샷 4열 수직 배치(리포트 UX 3차, 2026-08-17 사용자 피드백) ────
# 기존엔 보드마다 표를 .tw(overflow-x:auto)로 감싸 가로 슬라이드가 생겼다 —
# 4개 보드(거래대금/상승률/하락률/토스 사용자 거래대금)가 그리드로 나란히
# 서고, 각 보드 안은 가로 스크롤 없는 세로 목록이어야 한다.


def _snap_with_toss_rankings(boards: dict) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "toss_rankings": SourceResult(
            key="toss_rankings", ok=True,
            data={"ranked_at": "2026-08-12T08:00:00+09:00", "boards": boards},
            error=None, url="https://wts-cert-api.tossinvest.com", fetched_at=_AT, latency_ms=1,
        ),
    })


def _rank_item(rank: int, symbol: str, name: str, change_pct: float = 1.0) -> dict:
    return {
        "rank": rank, "symbol": symbol, "name": name,
        "trading_amount": 1_000_000_000, "price": 10_000, "change_pct": change_pct,
    }


def _toss_boards() -> dict:
    return {
        "거래대금": [_rank_item(1, "005930", "삼성전자")],
        "상승률": [_rank_item(1, "000660", "SK하이닉스")],
        "하락률": [_rank_item(1, "035420", "NAVER", change_pct=-2.0)],
        "토스 사용자 거래대금": [_rank_item(1, "005380", "현대차")],
    }


def test_ranking_snapshot_shows_all_four_board_headers():
    html = render(_snap_with_toss_rankings(_toss_boards()), _cont())
    zone = html[html.index("랭킹 스냅샷"):]
    zone = zone[:zone.index("</section>")]
    assert zone.count('<h3 class="cat">') == 4
    for board in ("거래대금", "상승률", "하락률", "토스 사용자 거래대금"):
        assert f'<h3 class="cat">{board}</h3>' in zone


def test_ranking_snapshot_zone_has_no_horizontal_scroll_container():
    html = render(_snap_with_toss_rankings(_toss_boards()), _cont())
    zone = html[html.index("랭킹 스냅샷"):]
    zone = zone[:zone.index("</section>")]
    assert "overflow-x" not in zone
    assert 'class="tw"' not in zone


def test_ranking_snapshot_shows_symbol_name_and_change_pct():
    html = render(_snap_with_toss_rankings(_toss_boards()), _cont())
    zone = html[html.index("랭킹 스냅샷"):]
    zone = zone[:zone.index("</section>")]
    assert "삼성전자" in zone
    assert "+1.00%" in zone
    assert "-2.00%" in zone


# ── 텔레그램 인사이트 (서브프로젝트 S part 2) ──────────────────────────────


def _telegram_view():
    return [
        {
            "handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector",
            "msgs": [
                {"text": "전력 테마 강세", "link": "https://t.me/pikachu_aje/10", "published_hhmm": "09:10"},
            ],
            "error": None,
        },
        {
            "handle": "tazastock", "분류": "장중 시황·국내 시황", "tier": "news",
            "msgs": [], "error": "no preview (프리뷰 비활성 또는 존재하지 않는 채널로 추정 — 프리뷰 섹션 없음)",
        },
    ]


def test_telegram_section_renders_channel_card_with_classification_and_tier_chips():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "pikachu_aje" in zone
    assert "전력 섹터" in zone
    assert "sector" in zone


def test_telegram_section_shows_message_link_opening_new_tab():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view())
    assert 'href="https://t.me/pikachu_aje/10"' in html
    assert 'target="_blank"' in html


def test_telegram_section_honestly_labels_unavailable_preview():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "프리뷰 미제공" in zone


def test_telegram_section_shows_narrator_prose_when_present():
    html = render(
        _snap(), _cont(), telegram_view_kr=_telegram_view(),
        telegram_prose={"pikachu_aje": "전력 테마가 강세를 보이고 있다."},
    )
    assert "전력 테마가 강세를 보이고 있다." in html


def test_telegram_section_omitted_when_empty():
    html = render(_snap(), _cont(), telegram_view_kr=[])
    assert "텔레그램 인사이트" not in html


def test_telegram_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "텔레그램 인사이트" not in html


def test_telegram_section_has_no_script_tag():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view())
    assert "<script" not in html


# ── 텔레그램 인사이트 KR/US 고정 탭 (2026-08-18) ────────────────────────────


def _telegram_view_us():
    return [
        {
            "handle": "insidertracking", "분류": "미국 주식·해외 뉴스", "tier": "news",
            "msgs": [
                {"text": "AAPL 강세", "link": "https://t.me/insidertracking/5",
                 "published_hhmm": "22:10"},
            ],
            "error": None,
        },
    ]


def test_telegram_tabs_render_both_market_views_simultaneously():
    """KR/US 두 뷰를 모두 전달하면 두 탭 내용이 같은 문서에 함께 실린다
    (CSS 전용 탭이라 비활성 탭도 마크업엔 존재한다)."""
    html = render(
        _snap(), _cont(), telegram_view_kr=_telegram_view(), telegram_view_us=_telegram_view_us(),
    )
    assert "pikachu_aje" in html
    assert "insidertracking" in html
    assert "AAPL 강세" in html


def test_telegram_tabs_default_checked_matches_report_market():
    """기본 checked 탭은 리포트 시장(snap.market)이다."""
    html_kr = render(
        _snap(), _cont(), telegram_view_kr=_telegram_view(), telegram_view_us=_telegram_view_us(),
    )
    assert 'id="tg-kr" checked' in html_kr
    assert 'id="tg-us" checked' not in html_kr

    html_us = render(
        _snap_us(), _cont(), telegram_view_kr=_telegram_view(), telegram_view_us=_telegram_view_us(),
    )
    assert 'id="tg-us" checked' in html_us
    assert 'id="tg-kr" checked' not in html_us


def test_telegram_tabs_us_only_still_renders_section():
    """KR 뷰가 비어도 US 뷰만 있으면 섹션 자체는 그려진다."""
    html = render(_snap_us(), _cont(), telegram_view_us=_telegram_view_us())
    assert "텔레그램 인사이트" in html
    assert "insidertracking" in html


def _telegram_view_with_photo():
    return [
        {
            "handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector",
            "msgs": [
                {"text": "전력 설비 사진", "link": "https://t.me/pikachu_aje/11",
                 "published_hhmm": "09:10", "image_count": 2,
                 "first_image_url": "https://cdn5.telesco.pe/a.jpg"},
            ],
            "error": None,
        },
    ]


def test_telegram_section_shows_photo_chip_with_count():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view_with_photo())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "🖼 사진 2" in zone


def test_telegram_section_omits_photo_chip_when_no_images():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert "🖼" not in zone


def test_telegram_section_does_not_hotlink_cdn_image_url():
    """`first_image_url`(텔레그램 CDN)은 카드 어디에도 그대로 노출되지 않는다 —
    사진 칩은 t.me 원문 링크(item.link)만 건다(외부 이미지 임베드 금지)."""
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view_with_photo())
    assert "cdn5.telesco.pe" not in html


_AI_CHIP = '<span class="chip on">AI 해석</span>'


def test_telegram_section_shows_ai_interpretation_chip_and_text_when_present():
    html = render(
        _snap(), _cont(), telegram_view_kr=_telegram_view_with_photo(),
        telegram_image_desc={"https://t.me/pikachu_aje/11": "전력 설비를 보여준다."},
    )
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert _AI_CHIP in zone
    assert "전력 설비를 보여준다." in zone


def test_telegram_section_omits_ai_interpretation_when_absent():
    html = render(_snap(), _cont(), telegram_view_kr=_telegram_view_with_photo())
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert _AI_CHIP not in zone


def test_telegram_section_omits_ai_interpretation_when_none():
    html = render(
        _snap(), _cont(), telegram_view_kr=_telegram_view_with_photo(), telegram_image_desc=None,
    )
    zone = html[html.index("텔레그램 인사이트"):]
    zone = zone[:zone.index("</section>")]
    assert _AI_CHIP not in zone


def test_missing_hint_explains_known_failure_shapes():
    """결측 원문에 원인 힌트를 붙인다 (2026-08-18) — 사용자가 로컬 빌드의
    Toss 403(IP 화이트리스트, 구조적)을 운영 고장으로 읽었다. 원문은 유지하고
    독해만 돕는다: 모르는 에러엔 힌트를 지어내지 않는다(None)."""
    from quant.analyze.render import missing_hint

    assert "IP" in missing_hint(
        "HTTPStatusError: Client error '403 Forbidden' for url "
        "'https://openapi.tossinvest.com/oauth2/token'")
    assert "시간외" in missing_hint("ValueError: 키움 시간외단일가 순위를 하나도 못 가져왔다")
    assert missing_hint("ValueError: 알 수 없는 새 실패") is None
    assert missing_hint(None) is None


# ── 읽는 흐름 재구성(2026-08-29) — 목차·챕터 헤더 ─────────────────────────


def test_toc_lists_chapters_in_order():
    html = render(_snap(), _cont())
    assert html.index('href="#ch1"') < html.index('href="#ch3"')
    assert html.index('href="#ch3"') < html.index('href="#ch6"')
    assert html.index('href="#ch6"') < html.index('href="#ch7"')
    assert html.index('href="#ch7"') < html.index('href="#ch8"')


def test_chapter_headers_render_in_order():
    html = render(_snap(), _cont())
    assert html.index('id="ch1"') < html.index('id="ch3"')
    assert html.index('id="ch3"') < html.index('id="ch4"')
    assert html.index('id="ch4"') < html.index('id="ch6"')
    assert html.index('id="ch6"') < html.index('id="ch7"')
    assert html.index('id="ch7"') < html.index('id="ch8"')


def test_exec_summary_still_precedes_stance_after_reorg():
    """L-1 스펙(엔진 예측보다 먼저)이 챕터 재구성 후에도 유지된다."""
    view = {"label": "약한 상승 신호", "score100": 60, "line": "line",
            "positives": [], "negatives": []}
    html = render(_snap(), _cont(), view=view, exec_summary=_exec_summary())
    assert html.index("Executive Summary") < html.index("엔진 예측")


# ── 지수별 전망(index_outlook, 소유자 요청 2026-08-29) ────────────────────


def _index_outlook():
    return {
        "kospi": {
            "score": 2, "span": 3, "score100": 83, "label": "강한 상승 신호",
            "positives": ["KOSPI +1.20%", "외국인 +3,500억 순매수"],
            "negatives": [],
            "probability": {
                "prob": 0.62, "n": 145, "reason": None,
                "method": "최근 3.2년 유사 조건일 145회 중 상승 90회",
            },
            "proxy_symbol": "069500",
        },
        "kosdaq": {
            "score": 0, "span": 0, "score100": None, "label": None,
            "positives": [], "negatives": [],
            "probability": {"prob": None, "n": 0, "method": None, "reason": "표본 부족"},
            "proxy_symbol": "229200",
        },
    }


def test_index_outlook_section_renders_score_and_probability():
    html = render(_snap(), _cont(), index_outlook=_index_outlook())
    assert "지수별 전망" in html
    assert "코스피" in html and "코스닥" in html
    assert "83점" in html
    assert "강한 상승 신호" in html
    assert "KOSPI +1.20%" in html
    assert "62%" in html
    assert "최근 3.2년 유사 조건일 145회 중 상승 90회" in html


def test_index_outlook_section_shows_reason_when_probability_missing():
    html = render(_snap(), _cont(), index_outlook=_index_outlook())
    assert "표본 부족" in html


def test_index_outlook_section_shows_insufficient_basis_when_score_none():
    html = render(_snap(), _cont(), index_outlook=_index_outlook())
    assert "판단 근거 부족" in html


def test_index_outlook_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert 'class="idx-outlook-grid"' not in html


def test_index_outlook_section_omitted_when_empty_dict():
    html = render(_snap(), _cont(), index_outlook={})
    assert 'class="idx-outlook-grid"' not in html


def test_index_outlook_explains_score_vs_probability_distinction():
    html = render(_snap(), _cont(), index_outlook=_index_outlook())
    assert "서로 다른 근거" in html


def test_index_outlook_us_market_uses_sp500_nasdaq_labels():
    outlook = {
        "sp500": {
            "score": 1, "span": 1, "score100": 75, "label": "약한 상승 신호",
            "positives": ["S&P500 +0.80%"], "negatives": [],
            "probability": {"prob": None, "n": 0, "method": None, "reason": "일봉 부족"},
            "proxy_symbol": "SPY",
        },
        "nasdaq": {
            "score": -1, "span": 1, "score100": 25, "label": "약한 하락 신호",
            "positives": [], "negatives": ["반도체(SOXX) 평균 -2.1%"],
            "probability": {"prob": None, "n": 0, "method": None, "reason": "일봉 부족"},
            "proxy_symbol": "QQQ",
        },
    }
    html = render(_snap_us(), _cont_us(), index_outlook=outlook)
    assert "S&amp;P500" in html and "나스닥" in html


# ── 휴장 기간 종합(holiday_synthesis, 소유자 요청 2026-08-29) ─────────────


def _holiday_synthesis(prose="반도체 강세가 이어졌다."):
    return {
        "market": "KR", "last_open": "2026-08-14", "gap_days": 2,
        "window_dates": ["2026-08-15", "2026-08-16"],
        "theme_freq": [{"theme": "반도체", "count": 2}],
        "new_symbols": [{"symbol": "005930", "name": "삼성전자", "first_seen": "2026-08-15"}],
        "stance_trend": [
            {"date": "2026-08-15", "score100": 48, "label": "중립"},
            {"date": "2026-08-16", "score100": 53, "label": "중립"},
        ],
        "high_impact_events": [{"date": "2026-08-15", "name": "Consumer Price Index"}],
        "missing_engine_days": [],
        "prose": prose,
    }


def test_holiday_synthesis_section_renders_when_present():
    html = render(_snap(), _cont(), holiday_synthesis=_holiday_synthesis())
    assert "휴장 기간 종합" in html
    assert "휴장 2일 종합" in html
    assert "2026-08-14" in html
    assert "반도체" in html
    assert "삼성전자" in html
    assert "반도체 강세가 이어졌다." in html


def test_holiday_synthesis_section_omitted_when_none():
    html = render(_snap(), _cont())
    assert "휴장 기간 종합" not in html


def test_holiday_synthesis_shows_missing_days_note():
    view = _holiday_synthesis()
    view["missing_engine_days"] = ["2026-08-15"]
    html = render(_snap(), _cont(), holiday_synthesis=view)
    assert "일부 결손" in html
    assert "2026-08-15" in html[html.index("일부 결손"):]


def test_holiday_synthesis_omits_prose_block_when_none():
    view = _holiday_synthesis(prose=None)
    html = render(_snap(), _cont(), holiday_synthesis=view)
    assert "휴장 기간 종합" in html
    assert "반도체" in html  # theme_freq 는 그대로 나온다


# ── 뉴스 노출 상위 종목 점수 내역 접힘(symbol_payload, 소유자 요청 2026-08-29) ──


def test_ranked_card_score_breakdown_shows_trending_factors():
    html = render(
        _snap(), _cont(), scores=_score(),
        symbol_payload={"005930": {
            "symbol": "005930", "trending_score100": 73,
            "trending_factors": ["board:거래대금"],
        }},
    )
    assert "점수 내역 보기" in html
    assert "트렌딩 요인" in html
    assert "board:거래대금" in html
    assert "73점" in html


def test_ranked_card_score_breakdown_omitted_without_scores_or_trending():
    html = render(_snap(), _cont())
    assert "점수 내역 보기" not in html


def test_ranked_card_score_breakdown_has_no_extra_details_when_symbol_payload_absent():
    """symbol_payload 를 안 넘겨도(하위호환) 기존 동작이 그대로다."""
    html = render(_snap(), _cont_with_titles(2))
    assert html.count("<details>") == 1  # '엔진 판독값' 뿐


# ── 부록(8장) 접힘 — 소유자 요청 2026-08-29 ────────────────────────────────


def test_appendix_omitted_when_no_reference_data():
    """시세·랭킹·심리·기술지표 등 근거 데이터가 하나도 없으면 부록 자체를
    그리지 않는다 — 빈 접힘 상자를 남기지 않는다."""
    html = render(_snap(), _cont())
    assert 'class="appendix"' not in html


def test_appendix_renders_when_missing_sources_present():
    snap = Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "market": SourceResult(
            key="market", ok=False, data=None, error="타임아웃",
            url="https://example.com", fetched_at=_AT, latency_ms=1,
        ),
    })
    html = render(snap, _cont())
    assert 'class="appendix"' in html
    assert "결측 소스" in html


# ── 모바일 반응형(소유자 피드백 2026-08-29) — grid-column:span 2 고정폭 제거 ──


def _snap_with_aaii() -> Snapshot:
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "sentiment": SourceResult(
            key="sentiment", ok=True,
            data={"aaii": {"spread": 12.3, "bull_pct": 40, "neutral_pct": 30,
                            "bear_pct": 30, "as_of": "2026-08-11"}},
            error=None, url="https://aaii.com", fetched_at=_AT, latency_ms=1,
        ),
    })


def test_aaii_card_uses_class_not_inline_grid_column_style():
    """375px 모바일 가로 스크롤의 실제 원인 — 인라인
    style="grid-column:span 2" 는 좁은 화면에서 카드 그리드가 강제로 2열을
    만들어 페이지 전체가 옆으로 밀렸다. 클래스로 옮겨 미디어쿼리로 되돌릴 수
    있어야 한다."""
    html = render(_snap_with_aaii(), _cont())
    assert 'style="grid-column:span 2"' not in html
    assert 'class="card card-wide"' in html


def test_mobile_media_query_collapses_card_wide_to_single_column():
    html = render(_snap_with_aaii(), _cont())
    style = html[html.index("<style>"):html.index("</style>")]
    assert "@media(max-width:640px){\n  .card.card-wide{grid-column:span 1}" in style


def test_html_has_overflow_x_hidden_backstop():
    """어떤 규칙이 실패해도 페이지 자체에 가로 스크롤바가 생기지 않도록 하는
    2차 방어선."""
    html = render(_snap(), _cont())
    style = html[html.index("<style>"):html.index("</style>")]
    assert "html{overflow-x:hidden}" in style


# ── 기사 불릿 리스트 + 관련 종목 칩(소유자 피드백 2026-08-29) ──────────────


def test_link_to_symbols_reverse_indexes_article_links():
    from quant.analyze.render import link_to_symbols

    cont = {
        "005930": {"name": "삼성전자", "titles": [
            {"title": "삼성전자 HBM4", "link": "https://a/1"},
        ]},
        "000660": {"name": "SK하이닉스", "titles": [
            {"title": "SK하이닉스도 HBM4", "link": "https://a/1"},
            {"title": "단독 기사", "link": "https://a/2"},
        ]},
    }
    out = link_to_symbols(cont)
    assert {e["symbol"] for e in out["https://a/1"]} == {"005930", "000660"}
    assert [e["symbol"] for e in out["https://a/2"]] == ["000660"]


def test_link_to_symbols_dedupes_same_symbol_multiple_titles_same_link():
    cont = {"005930": {"name": "삼성전자", "titles": [
        {"title": "제목1", "link": "https://a/1"},
        {"title": "제목1 중복 표기", "link": "https://a/1"},
    ]}}
    from quant.analyze.render import link_to_symbols
    out = link_to_symbols(cont)
    assert len(out["https://a/1"]) == 1


def test_link_to_symbols_skips_titles_without_link():
    cont = {"005930": {"name": "삼성전자", "titles": [{"title": "링크 없음"}]}}
    from quant.analyze.render import link_to_symbols
    assert link_to_symbols(cont) == {}


def test_digest_section_renders_bullet_list_not_flat_block():
    digest = _digest(
        domestic=[{"title": "삼성전자, 3나노 파운드리 수주", "link": "https://a", "dup_count": 1}],
    )
    html = render(_snap(), _cont(), digest=digest)
    assert '<ul class="news-list">' in html
    assert "<li>" in html


def test_digest_bullet_shows_related_symbol_chip_from_cont_links():
    cont = _cont()  # 005930 has titles=[] by default; give it a matching link
    cont["005930"]["titles"] = [{"title": "삼성전자, HBM4 양산", "link": "https://a"}]
    digest = _digest(domestic=[{"title": "삼성전자, HBM4 양산", "link": "https://a", "dup_count": 1}])
    html = render(_snap(), cont, digest=digest)
    assert 'class="news-syms"' in html
    assert "삼성전자" in html[html.index('class="news-syms"'):html.index('class="news-syms"') + 300]


def test_digest_bullet_omits_related_symbol_block_when_no_match():
    digest = _digest(domestic=[{"title": "무관 기사", "link": "https://unrelated", "dup_count": 1}])
    html = render(_snap(), _cont(), digest=digest)
    assert 'class="news-syms"' not in html


def test_digest_bullet_shows_outlet_chip_outside_anchor():
    digest = _digest(domestic=[
        {"title": "삼성전자 기사", "link": "https://a", "dup_count": 1, "outlet": "한국경제"},
    ])
    html = render(_snap(), _cont(), digest=digest)
    assert '<span class="chip">한국경제</span>' in html
    # 앵커 태그 자체는 여전히 속성 추가 없이 정확히 이 형태다(기존 계약 유지).
    assert '<a href="https://a" target="_blank" rel="noopener">삼성전자 기사</a>' in html


def test_news_flow_bullet_shows_related_symbol_chip():
    cont = _cont()
    cont["005930"]["titles"] = [{"title": "삼성전자 기사", "link": "https://a"}]
    news_flow = [_flow_item("삼성전자 기사", "https://a", outlet="한국경제")]
    html = render(_snap(), cont, news_flow=news_flow)
    assert 'class="news-syms"' in html


def test_news_flow_renders_as_bullet_list():
    news_flow = [_flow_item("사건", "https://a", outlet="한국경제")]
    html = render(_snap(), _cont(), news_flow=news_flow)
    assert '<ul class="news-list">' in html


# ── 실기기(iPhone) 확인 2건(2026-08-29) — .sym-scalp/.sym 카드 모바일 붕괴 ──


def test_sym_scalp_uses_flex_header_not_grid():
    """옛 grid(auto 1fr auto) 3칸 레이아웃을 버리고, 모든 폭에서 동일하게
    동작하는 flex-wrap 머리 줄(.scalp-head)로 바꿨다 — 별도 모바일 오버라이드가
    필요 없어야 캐스케이드 동률 버그가 재발하지 않는다."""
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    assert 'class="scalp-head"' in html
    assert 'class="scalp-score"' in html
    style = html[html.index("<style>"):html.index("</style>")]
    assert ".sym-scalp{display:grid" not in style


def test_chip_and_grade_badge_css_prevents_character_wrap():
    """칩/배지가 white-space:nowrap 없이 좁은 flex 컨테이너에 놓이면 한글이
    글자 단위로 줄바꿈된다(CJK 기본 줄바꿈 규칙) — 실기기에서 확인된 버그."""
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    style = html[html.index("<style>"):html.index("</style>")]
    chip_rule = style[style.index(".chip{"):style.index(".chip.on{")]
    assert "white-space:nowrap" in chip_rule
    assert "flex-shrink:0" in chip_rule
    badge_rule = style[style.index(".grade-badge{"):style.index('.grade-badge[data-g="3"]')]
    assert "white-space:nowrap" in badge_rule


def test_sym_nm_uses_keep_all_word_break():
    """종목명(한글)이 좁은 칸에서 두 줄로 쪼개지는 건 괜찮지만, 글자 단위로
    쪼개지면 안 된다 — word-break:keep-all 로 한글 단어 경계만 허용한다."""
    html = render(_snap(), _cont(), intraday_view=_intraday_view())
    style = html[html.index("<style>"):html.index("</style>")]
    assert "word-break:keep-all" in style[style.index(".sym-nm{"):style.index(".sym-nm-plain{")]


def test_sym_mobile_collapse_rule_is_placed_after_base_rule():
    """캐스케이드 동률 버그 재발 방지 — .sym 1열 붕괴 미디어쿼리가 .sym 본
    정의보다 소스 순서상 뒤에 있어야 실제로 이긴다."""
    html = render(_snap(), _cont())
    style = html[html.index("<style>"):html.index("</style>")]
    base_idx = style.index(".sym{display:grid")
    media_idx = style.index("@media(max-width:640px){\n  .card.card-wide{grid-column:span 1}")
    assert base_idx < media_idx


# ── 시세 스냅샷 기준 시각 표기(2026-08-29 소유자 피드백) ────────────────────


def _snap_with_market_fetched_at(fetched_hhmm: str) -> Snapshot:
    fetched = datetime(2026, 8, 12, int(fetched_hhmm[:2]), int(fetched_hhmm[3:]), tzinfo=KST)
    return Snapshot(SCHEMA_VERSION, "KR", date(2026, 8, 12), _AT, {
        "market": SourceResult(
            key="market", ok=True,
            data={"quotes": {"^KS11": {"label": "KOSPI", "close": 3000.0, "change_pct": 1.0,
                                        "history": []}}, "anchors": {},
                  "crosscheck": {"checked": [], "warnings": []}},
            error=None, url="https://finance.yahoo.com", fetched_at=fetched, latency_ms=10,
        ),
    })


def test_quote_time_label_uses_market_fetched_at_when_present():
    """지수 등락률이 실제로 찍힌 시각(SourceResult.fetched_at)을 쓴다 —
    스냅샷 조립 시각(generated_at, 08:00)과 다른 값으로 구분해서 확인한다."""
    html = render(_snap_with_market_fetched_at("07:52"), _cont())
    assert "시세 기준 07:52 KST" in html


def test_quote_time_label_falls_back_to_generated_at_when_market_missing():
    """market 소스가 없으면(또는 실패하면) 지어내지 않고 리포트 생성 시각을
    다른 문구로 정직하게 구분해서 쓴다."""
    html = render(_snap(), _cont())  # _snap()은 market 소스가 없다
    assert "리포트 생성 08:00 KST 기준" in html
    assert "시세 기준" not in html


def test_quote_time_label_appears_in_key_box():
    html = render(_snap_with_market_fetched_at("07:52"), _cont(), view=_view_for_key_box())
    zone = html[html.index('class="key"'):html.index('class="key"') + 800]
    assert "시세 기준 07:52 KST" in zone


def _view_for_key_box():
    return {"label": "약한 상승 신호", "score100": 60, "line": "line",
            "positives": [], "negatives": []}


def test_quote_time_label_appears_in_candidates_chapter_head():
    html = render(_snap_with_market_fetched_at("07:52"), _cont())
    zone = html[html.index('id="ch6"'):html.index('id="ch6"') + 400]
    assert "시세 기준 07:52 KST" in zone


def test_quote_time_label_appears_in_appendix_quote_table():
    html = render(_snap_with_market_fetched_at("07:52"), _cont())
    zone = html[html.index("<h2>시세<span"):html.index("<h2>시세<span") + 300]
    assert "시세 기준 07:52 KST" in zone
