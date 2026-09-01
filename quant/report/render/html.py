"""HTML/engine.json 렌더러 — `ReportModel`/`CloseReportModel`만 보고 그린다.

Phase D 엔진 분리(2026-08-19) — 실제 jinja 템플릿·`write_*`(디스크 기록) 구현은
`quant.analyze.render`에 그대로 둔다(그 모듈은 `quant/analyze/sector_view.py`와
기존 테스트 5종도 함께 참조하는 공유 자산이라 옮기면 그쪽 회귀 위험이 커진다 —
"동작 변경 금지" 우선). 이 모듈은 리포트 조립부(`quant/apps/report_cli.py`)가
흩어진 위치 인자 대신 **모델 하나**만 넘기면 되도록 감싸는 경계다 — `_emit`/
`_emit_close`는 이제 `render.html`이 무엇을 어떻게 그리는지 몰라도 된다.
"""
from __future__ import annotations

from pathlib import Path

from quant.analyze import render as _render
from quant.collect.contracts import Snapshot

from quant.report.model import CloseReportModel, ReportModel


def write_open_report(model: ReportModel, snap: Snapshot, out_root: Path) -> tuple[Path, Path, Path]:
    """아침 리포트 3종 산출물(HTML/engine.json/candidates.txt) — `_emit`이 쓰던
    `write_html`/`write_machine`/`write_candidates` 호출을 모델 하나로 감싼다."""
    # 뉴스 노출 상위 종목 카드의 "점수 내역" 접힘(트렌딩 요인)이 심볼별
    # machine_payload 항목을 찾아볼 수 있게 symbol -> entry 사전을 만든다.
    # model.payload["symbols"] 는 이미 write_machine 이 그대로 쓰는 값이라
    # 새 계산 없이 조회만 한다.
    symbol_payload = {
        s["symbol"]: s for s in (model.payload.get("symbols") or [])
        if isinstance(s, dict) and s.get("symbol")
    }
    hp = _render.write_html(
        snap, out_root, model.cont, model.delta, model.brief, model.payload["auto_watch"],
        model.sym_quotes, model.details, model.view, model.scores,
        relations=model.relations, sector_view=model.sector_view, flow_rows=model.flow_rows,
        youtube=model.youtube, blog=model.blog, top_movers=model.top_movers,
        carried_candidates=model.carried_candidates, disclosures=model.disclosures,
        research=model.research, research_badges=model.research_badges,
        foreign_view=model.foreign_view, digest=model.digest,
        digest_prose=model.digest_prose, stance_prose=model.stance_prose,
        news_flow=model.news_flow,
        intraday_view=model.intraday_view, exec_summary=model.exec_summary,
        section_advice=model.section_advice, telegram_view_kr=model.telegram_view_kr,
        telegram_view_us=model.telegram_view_us, telegram_prose=model.telegram_prose,
        telegram_image_desc=model.telegram_image_desc,
        agent_interpret_view=model.agent_interpret_view, midterm_view=model.midterm_view,
        us_news_kr_view=model.us_news_kr_view, usnews_headlines=model.usnews_headlines,
        us_kr_bridge=model.us_kr_bridge, us_wrap=model.us_wrap,
        index_outlook=model.index_outlook, holiday_synthesis=model.holiday_synthesis,
        symbol_payload=symbol_payload, money_flow=model.money_flow,
        name_map=model.name_map,
    )
    jp = _render.write_machine(model.payload, snap, out_root)
    cp = _render.write_candidates(model.payload["auto_watch"], snap, out_root)
    _render.write_robots(out_root)
    return hp, jp, cp


def write_close_report(model: CloseReportModel, snap: Snapshot, out_root: Path) -> tuple[Path, Path]:
    """마감 리포트(서브프로젝트 R) 2종 산출물(HTML/close_engine.json) — `_emit_close`가
    쓰던 `write_close_html`/`write_close_machine` 호출을 모델 하나로 감싼다."""
    hp = _render.write_close_html(
        snap, out_root, news_view=model.news_view, flow_view=model.flow_view,
        ranking_view=model.ranking_view, intraday_view=model.intraday_view,
        telegram_view_kr=model.telegram_view_kr, telegram_view_us=model.telegram_view_us,
        agent_interpret_view=model.agent_interpret_view,
        midterm_view=model.midterm_view, us_news_kr_view=model.us_news_kr_view,
        close_bet_view=model.close_bet_view,
        usnews_headlines=model.usnews_headlines,
    )
    jp = _render.write_close_machine(model.payload, snap, out_root)
    return hp, jp
