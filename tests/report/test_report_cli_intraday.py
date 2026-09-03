"""`report_cli._build_intraday_view`/`_record_intraday_selections` — 당일 단타
후보 배선(서브프로젝트 K).

`_emit`(전체 리포트 발행 파이프라인)을 통째로 태우지 않는다 — 다른
`_build_*` 배선 테스트(`test_foreign_view_wiring.py`,
`test_report_cli_digest_prose.py`)와 같은 관례로 헬퍼만 단위 검증한다.
채점 로직 자체(요인 배점)는 `tests/test_intraday_score.py` 의 몫이라 여기선
"입력이 리포트가 이미 아는 데이터에서 올바르게 조립됐는가"만 본다.
"""
from __future__ import annotations

import json
from pathlib import Path

from quant.apps.report_cli import (
    _build_intraday_view,
    _candidate_symbols,
    _record_intraday_selections,
    _theme_change_pct,
    _visible_intraday,
)
from quant.analyze.scalp_grade import GRADE_ENTER
from quant.control import selections


def _write_frgn_flow(tmp_path: Path, rows: list[dict]) -> None:
    path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _payload(**over) -> dict:
    base = {
        "session_date": "2026-08-17",
        "market": "KR",
        "auto_watch": "AUTO_WATCH: 005930:NEWS+RANK 000660:RANK",
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "news_articles_today": 5,
             "trending_score100": 80, "close": 71_000.0, "change_pct": 1.2},
            {"symbol": "000660", "name": "SK하이닉스", "news_articles_today": 0,
             "close": 200_000.0, "change_pct": -0.5},
        ],
    }
    base.update(over)
    return base


def _cont() -> dict:
    return {
        "005930": {
            "name": "삼성전자", "today_articles": 5,
            "titles": [
                {"title": "삼성전자 실적 발표", "link": "https://a/1", "feed": "a"},
                {"title": "삼성전자 실적 발표", "link": "https://b/1", "feed": "b"},
                {"title": "삼성전자 실적 발표", "link": "https://c/1", "feed": "c"},
                {"title": "완전히 다른 기사", "link": "https://d/1", "feed": "d"},
            ],
        },
        "000660": {"name": "SK하이닉스", "today_articles": 0, "titles": []},
    }


def _foreign_view() -> dict:
    return {
        "market_foreign_net": 100, "market_date": "2026-08-17",
        "sectors": [{
            "name": "반도체", "net_sum": 1000,
            "rows": [{
                "symbol": "005930", "name": "삼성전자",
                "label": "매수 시그널(재유입)", "residual": 1000,
                "inst_follows": True, "days": 5, "series": [],
            }],
        }],
    }


def _relations() -> dict:
    return {
        "005930": [{
            "dst": "000990", "kind": "beneficiary", "reason": "이유",
            "evidence_score": 90, "via_theme": "반도체 테마",
        }],
    }


def _themes() -> dict:
    return {"1": {"name": "반도체 테마", "change_pct": 4.5, "symbols": []}}


# ── _candidate_symbols ───────────────────────────────────────────────────

def test_candidate_symbols_parses_auto_watch_tokens():
    assert _candidate_symbols(_payload()) == {"005930", "000660"}


def test_candidate_symbols_empty_when_no_watch():
    assert _candidate_symbols(_payload(auto_watch="AUTO_WATCH: 없음")) == set()


# ── _theme_change_pct ────────────────────────────────────────────────────

def test_theme_change_pct_resolves_via_relations_via_theme():
    assert _theme_change_pct("005930", _relations(), _themes()) == 4.5


def test_theme_change_pct_none_without_relations():
    assert _theme_change_pct("005930", None, _themes()) is None


def test_theme_change_pct_none_without_themes():
    assert _theme_change_pct("005930", _relations(), None) is None


def test_theme_change_pct_none_when_no_via_theme_on_relation():
    relations = {"005930": [{"dst": "000990", "kind": "beneficiary", "reason": "x",
                              "evidence_score": 90}]}  # via_theme 없음
    assert _theme_change_pct("005930", relations, _themes()) is None


# ── _build_intraday_view ─────────────────────────────────────────────────

def test_us_market_returns_empty_without_scoring(tmp_path):
    """외국인 수급·업종·테마 아티팩트가 전부 KR 전용이라 US 는 애초에
    가드한다(`_build_foreign_view` 와 같은 시장 가드)."""
    view = _build_intraday_view(
        tmp_path, "US", _payload(market="US"), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )

    assert view == []


def test_no_candidates_returns_empty(tmp_path):
    payload = _payload(auto_watch="AUTO_WATCH: 없음")

    view = _build_intraday_view(
        tmp_path, "KR", payload, _cont(), _foreign_view(), _relations(), _themes(), {},
    )

    assert view == []


def test_view_maps_symbol_name_and_score(tmp_path):
    view = _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )

    row = next(r for r in view if r["symbol"] == "005930")
    assert row["name"] == "삼성전자"
    assert isinstance(row["score100"], int)
    assert row["foreign_label"] == "매수 시그널(재유입)"


def test_max_dup_count_from_cont_titles(tmp_path, monkeypatch):
    """3건이 같은 사건(제목 완전동일)으로 묶이면 max_dup_count=3 이 채점
    입력으로 전달된다 — `score_intraday` 가 실제로 그 값을 받는지 캡처."""
    captured: dict = {}

    from quant.analyze import intraday_score as intraday_mod

    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )

    assert captured["005930"]["max_dup_count"] == 3
    assert captured["000660"]["max_dup_count"] == 0


def test_titles_from_cont_titles_flow_into_score_inputs(tmp_path, monkeypatch):
    """서브프로젝트 P — `cont[symbol]["titles"]`의 원문 제목이 채점 입력
    `titles`로 그대로 전달돼야 호재 마커/악재 거부권 판정이 된다."""
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )

    assert "삼성전자 실적 발표" in captured["005930"]["titles"]
    assert captured["000660"]["titles"] == []


def test_dart_types_from_disclosures_via_classify_report(tmp_path, monkeypatch):
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )

    assert captured["005930"]["dart_types"] == ["수주"]
    assert captured["000660"]["dart_types"] == []


def test_trending_score100_and_today_articles_from_payload_entry(tmp_path, monkeypatch):
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {},
    )

    assert captured["005930"]["trending_score100"] == 80
    assert captured["005930"]["today_articles"] == 5
    assert captured["000660"]["trending_score100"] is None


def test_theme_change_pct_flows_into_score_inputs(tmp_path, monkeypatch):
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {},
    )

    assert captured["005930"]["theme_change_pct"] == 4.5
    assert captured["000660"]["theme_change_pct"] is None


# ── foreign_v2_score (2026-08-17, 서브프로젝트 O — 외국인 축 라벨→v2 교체) ──

def test_foreign_v2_score_computed_from_frgn_flow_ledger_not_foreign_view(tmp_path, monkeypatch):
    """채점 입력 `foreign_v2_score`는 `foreign_view`의 표시용 라벨/series가
    아니라 `frgn_flow.jsonl` 원장을 독립적으로 다시 읽어서 계산돼야 한다
    (`foreign_view`의 series는 최근 10일·inst_net 없음이라 쌍끌이 판정에
    부족하다 — `_build_intraday_view` docstring 참고)."""
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _write_frgn_flow(tmp_path, [
        {"date": "2026-08-14", "symbol": "005930", "foreign_net": 100, "inst_net": 50},
        {"date": "2026-08-15", "symbol": "005930", "foreign_net": 200, "inst_net": 80},
    ])

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {},
    )

    # 쌍끌이(최근일 외국인+기관 동반 순매수)가 트리거되므로 0보다 커야 한다.
    assert isinstance(captured["005930"]["foreign_v2_score"], int)
    assert captured["005930"]["foreign_v2_score"] > 0


def test_foreign_v2_score_none_when_no_flow_history(tmp_path, monkeypatch):
    """`frgn_flow.jsonl` 자체가 없거나 그 종목 행이 없으면 `None`(0으로
    위장 금지) — `score_intraday`가 "수급 시계열 없음"으로 정직하게
    처리하는 nullable 계약(모듈 상단 참고)과 일치해야 한다."""
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {},
    )

    assert captured["005930"]["foreign_v2_score"] is None
    assert captured["000660"]["foreign_v2_score"] is None


# ── telegram_mention (서브프로젝트 S part 2, v4) ─────────────────────────

def test_telegram_mentions_flow_into_score_inputs(tmp_path, monkeypatch):
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    mentions = {
        "005930": {
            "channels": [{"handle": "pikachu_aje", "분류": "전력 섹터", "tier": "sector"}],
            "sector_room": True,
        },
    }
    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {}, telegram_mentions=mentions,
    )

    assert captured["005930"]["telegram_mention"] == mentions["005930"]
    assert captured["000660"]["telegram_mention"] is None


def test_telegram_mentions_defaults_to_none_when_omitted(tmp_path, monkeypatch):
    captured: dict = {}
    from quant.analyze import intraday_score as intraday_mod
    real_rank = intraday_mod.rank_intraday

    def _spy(candidates, top=8):
        captured.update(candidates)
        return real_rank(candidates, top=top)

    monkeypatch.setattr("quant.report.collect.intraday.rank_intraday", _spy)

    _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(), _relations(), _themes(), {},
    )

    assert captured["005930"]["telegram_mention"] is None


# ── scalp_grade 배선(2026-08-18) ─────────────────────────────────────────

def test_grade_is_none_when_no_signal_at_all(tmp_path):
    """`_cont()` 기본 픽스처(호재 마커 미매칭, 섹터/텔레그램/외국인 데이터
    없음)면 두 종목 다 미달(grade=None) — 표시 필터는 호출부의 몫이라
    이 함수 자체는 항목을 빼지 않는다(아래 test_view_keeps_hidden_items)."""
    view = _build_intraday_view(
        tmp_path, "KR", _payload(), _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )
    row = next(r for r in view if r["symbol"] == "005930")
    assert row["grade"] is None
    assert row["grade_reasons"] == []


def test_view_keeps_hidden_items_display_filter_is_callers_job(tmp_path):
    """등급 미달이어도 `_build_intraday_view`는 항목을 지우지 않는다 —
    원장/engine.json 연속성 보존(`grade_scalp` 모듈 docstring). 표시에서
    빼는 건 `_emit`/`_emit_close`가 별도 목록(intraday_display)으로 한다.

    000660엔 `trending_score100` + 순매매 0(중립)인 외국인 수급을 얹어
    `score_intraday`의 표본부족 게이트(None 4개 중 3개 이상)를 통과시킨다
    — 그래야 애초에 랭킹에 올라 grade=None 인 채로 view 에 남는지를
    검증할 수 있다(게이트에 걸려 애초에 빠지는 것과는 다른 문제다)."""
    payload = _payload(symbols=[
        {"symbol": "005930", "name": "삼성전자", "news_articles_today": 5,
         "trending_score100": 80, "close": 71_000.0, "change_pct": 1.2},
        {"symbol": "000660", "name": "SK하이닉스", "news_articles_today": 0,
         "trending_score100": 40, "close": 200_000.0, "change_pct": -0.5},
    ])
    _write_frgn_flow(tmp_path, [
        {"date": "2026-08-16", "symbol": "000660", "foreign_net": 0, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "000660", "foreign_net": 0, "inst_net": 0},
    ])
    view = _build_intraday_view(
        tmp_path, "KR", payload, _cont(), _foreign_view(),
        _relations(), _themes(), {"005930": ["수주 계약 체결"]},
    )
    assert {r["symbol"] for r in view} == {"005930", "000660"}
    row = next(r for r in view if r["symbol"] == "000660")
    assert row["grade"] is None


def test_grade_enter_when_symbol_has_bullish_marker_and_foreign_not_selling(tmp_path):
    """종목 자체 호재 마커가 있고 외국인이 명시적 순매도가 아니면(여기선
    데이터 없음=None) 진입 등급이 나온다."""
    cont = {
        "005930": {
            "name": "삼성전자", "today_articles": 1,
            "titles": [{"title": "삼성전자, 대규모 수주 공시", "link": "https://a/1", "feed": "a"}],
        },
        "000660": {"name": "SK하이닉스", "today_articles": 0, "titles": []},
    }
    view = _build_intraday_view(
        tmp_path, "KR", _payload(), cont, _foreign_view(),
        _relations(), _themes(), {},
    )
    row = next(r for r in view if r["symbol"] == "005930")
    assert row["grade"] == GRADE_ENTER
    assert "종목 호재 뉴스" in row["grade_reasons"]


def test_grade_enter_blocked_by_explicit_foreign_selling(tmp_path):
    """진입 조건(종목 호재)이 있어도 최근일 외국인이 명시적 순매도면
    진입까지는 못 가고 보류로 강등된다(scalp_grade 규칙)."""
    from quant.analyze.scalp_grade import GRADE_HOLD

    cont = {
        "005930": {
            "name": "삼성전자", "today_articles": 1,
            "titles": [{"title": "삼성전자, 대규모 수주 공시", "link": "https://a/1", "feed": "a"}],
        },
        "000660": {"name": "SK하이닉스", "today_articles": 0, "titles": []},
    }
    _write_frgn_flow(tmp_path, [
        {"date": "2026-08-16", "symbol": "005930", "foreign_net": -500, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "005930", "foreign_net": -300, "inst_net": 0},
    ])
    view = _build_intraday_view(
        tmp_path, "KR", _payload(), cont, _foreign_view(),
        _relations(), _themes(), {},
    )
    row = next(r for r in view if r["symbol"] == "005930")
    assert row["grade"] == GRADE_HOLD
    assert "외국인 순매도" in row["grade_reasons"]


def test_sector_bullish_hits_and_rising_wired_from_sector_map_and_quotes(tmp_path):
    """섹터 호재(같은 업종 종목의 호재 히트 합)·섹터 상승은 sector_map/
    sector_quotes 에서 온다 — 종목 자체엔 호재 마커가 없어도 같은 업종의
    다른 종목이 호재고 그 업종이 오르는 중이면 진입 등급을 받는다.

    000660엔 `trending_score100` + 순매매 0(중립, 등급 신호로는 무해)인
    외국인 수급을 얹어 `score_intraday`의 표본부족 게이트를 통과시킨다
    (`test_view_keeps_hidden_items_display_filter_is_callers_job`과 같은
    이유)."""
    payload = _payload(symbols=[
        {"symbol": "005930", "name": "삼성전자", "news_articles_today": 5,
         "trending_score100": 80, "close": 71_000.0, "change_pct": 1.2},
        {"symbol": "000660", "name": "SK하이닉스", "news_articles_today": 0,
         "trending_score100": 40, "close": 200_000.0, "change_pct": -0.5},
    ])
    cont = {
        "005930": {
            "name": "삼성전자", "today_articles": 1,
            "titles": [{"title": "삼성전자, 대규모 수주 공시", "link": "https://a/1", "feed": "a"}],
        },
        "000660": {"name": "SK하이닉스", "today_articles": 0, "titles": []},
    }
    _write_frgn_flow(tmp_path, [
        {"date": "2026-08-16", "symbol": "000660", "foreign_net": 0, "inst_net": 0},
        {"date": "2026-08-17", "symbol": "000660", "foreign_net": 0, "inst_net": 0},
    ])
    sector_map = {"005930": "반도체", "000660": "반도체"}
    sector_quotes = [{"name": "반도체", "change_pct": 2.0}]

    view = _build_intraday_view(
        tmp_path, "KR", payload, cont, _foreign_view(),
        _relations(), _themes(), {},
        sector_map=sector_map, sector_quotes=sector_quotes,
    )
    row = next(r for r in view if r["symbol"] == "000660")
    assert row["grade"] == GRADE_ENTER
    assert any("섹터 호재" in r for r in row["grade_reasons"])


def test_sector_data_absent_degrades_honestly_not_falsely(tmp_path):
    """`sector_map`/`sector_quotes`를 안 주면(예: 마감판) 섹터 상승은 정직하게
    None — False(하락)로 위장해 등급을 깎지 않는다."""
    cont = {
        "005930": {
            "name": "삼성전자", "today_articles": 1,
            "titles": [{"title": "삼성전자, 대규모 수주 공시", "link": "https://a/1", "feed": "a"}],
        },
        "000660": {"name": "SK하이닉스", "today_articles": 0, "titles": []},
    }
    view = _build_intraday_view(
        tmp_path, "KR", _payload(), cont, _foreign_view(),
        _relations(), _themes(), {},
    )
    row = next(r for r in view if r["symbol"] == "005930")
    # 종목 자체 호재만으로도 진입은 나온다 — 섹터 신호 부재가 등급을 깎지 않았다.
    assert row["grade"] == GRADE_ENTER


def test_visible_intraday_drops_none_grade_but_keeps_graded(tmp_path):
    """표시 필터(`_visible_intraday`)는 grade가 있는 항목만 남긴다 — 원장
    기록용 전체 목록(`_build_intraday_view`의 반환값)과는 별개다. 등급
    문자열 자체(빈 문자열이 아닌 실제 값)가 falsy 가 아님을 확인해 `it.get(
    "grade")` 필터가 우연히 "0"/빈 문자열 같은 값을 오탐하지 않는지도 겸사
    검증한다."""
    view = [
        {"symbol": "005930", "grade": "단타 진입", "grade_reasons": ["종목 호재 뉴스"]},
        {"symbol": "000660", "grade": None, "grade_reasons": []},
    ]
    visible = _visible_intraday(view)
    assert [it["symbol"] for it in visible] == ["005930"]


def test_visible_intraday_empty_when_all_hidden():
    view = [{"symbol": "005930", "grade": None, "grade_reasons": []}]
    assert _visible_intraday(view) == []


def test_view_returns_at_most_top8(tmp_path):
    symbols = [
        {"symbol": f"{i:06d}", "name": f"종목{i}", "trending_score100": 90}
        for i in range(12)
    ]
    payload = _payload(
        auto_watch="AUTO_WATCH: " + " ".join(f"{s['symbol']}:NEWS" for s in symbols),
        symbols=symbols,
    )
    cont = {s["symbol"]: {"name": s["name"], "today_articles": 3, "titles": []}
            for s in symbols}

    view = _build_intraday_view(tmp_path, "KR", payload, cont, None, None, None, {})

    assert len(view) <= 8


# ── _record_intraday_selections ─────────────────────────────────────────

def _view_item(symbol="005930", name="삼성전자", score100=72) -> dict:
    return {
        "symbol": symbol, "name": name, "score100": score100,
        "factors": [
            {"name": "호재 뉴스", "pts": 15, "max": 40, "evidence": "…"},
            {"name": "외국인 추세", "pts": 30, "max": 30, "evidence": "…"},
        ],
        "foreign_label": "매수 시그널(재유입)",
    }


def test_records_row_with_intraday_scorer_producer(tmp_path):
    payload = _payload()
    _record_intraday_selections([_view_item()], payload, tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert len(rows) == 1
    row = rows[0]
    # 2026-08-17: 텔레그램 시그널 축 신설(서브프로젝트 S part 2)로 producer
    # 버전이 다시 올랐다 — v3/v4 행이 같은 자연키에서 섞이지 않게(모듈 상단
    # _INTRADAY_PRODUCER 주석 참고).
    assert row["producer"] == "intraday_scorer_v4"
    assert row["symbol"] == "005930"
    assert row["score100"] == 72
    assert row["is_candidate"] is True
    assert row["close"] == 71_000.0  # payload["symbols"] 의 close 재사용


def test_records_close_date_from_payload_symbols_date(tmp_path):
    """render.py 가 payload["symbols"] 항목에 실은 date(quote 의 실제 거래일,
    D2 2026-09-03)가 close_date 로 그대로 옮겨진다 — outcomes.base_session_date
    가 이 값을 기준 세션으로 쓴다."""
    payload = _payload(symbols=[
        {"symbol": "005930", "name": "삼성전자", "news_articles_today": 5,
         "trending_score100": 80, "close": 71_000.0, "change_pct": 1.2,
         "date": "2026-08-14"},
        {"symbol": "000660", "name": "SK하이닉스", "news_articles_today": 0,
         "close": 200_000.0, "change_pct": -0.5},
    ])
    _record_intraday_selections([_view_item()], payload, tmp_path)

    rows = selections.load(tmp_path / "data" / "ledger" / "selections.jsonl")
    assert rows[0]["close_date"] == "2026-08-14"


def test_records_flat_factor_points(tmp_path, monkeypatch):
    captured: list = []

    def _capture(rows, path):
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr("quant.apps.report_cli.selections.append", _capture)

    _record_intraday_selections([_view_item()], _payload(), tmp_path)

    row = captured[0]
    assert row["bullish_news_pts"] == 15
    assert row["foreign_trend_pts"] == 30


def test_does_not_collide_with_existing_report_producer_row(tmp_path):
    """같은 (날짜,시장,종목)에 리포트 본선 producer 행이 이미 있어도 단타
    스코어러 행이 별도로 쌓인다 — natural_key 에 producer 가 들어간 덕이다."""
    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    report_row = {
        "schema": 1, "date": "2026-08-17", "market": "KR", "symbol": "005930",
        "close": 71_000.0, "outcome_filled": False,
    }
    selections.append([report_row], path)

    payload = _payload()
    _record_intraday_selections([_view_item()], payload, tmp_path)

    rows = selections.load(path)
    assert len(rows) == 2
    assert {r.get("producer") for r in rows} == {None, "intraday_scorer_v4"}


def test_empty_view_writes_nothing(tmp_path):
    _record_intraday_selections([], _payload(), tmp_path)

    path = tmp_path / "data" / "ledger" / "selections.jsonl"
    assert not path.exists()


def test_record_intraday_failure_does_not_raise(tmp_path, capsys):
    """실패해도 예외를 올리지 않는다 — 원장은 부가 산출물(`_record_selections`
    와 같은 관례)."""
    _record_intraday_selections([_view_item()], {"symbols": "이것은 리스트가 아니다"}, tmp_path)

    err = capsys.readouterr().err
    assert "건너뜀" in err
