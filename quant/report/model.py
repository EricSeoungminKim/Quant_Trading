"""ReportModel — 리포트가 그릴 대상 전부를 담는 순수 DTO.

Phase D 엔진 분리(2026-08-19, `docs/superpowers/specs/2026-08-19-engine-separation-design.md`)
— 수집기(`quant/report/collect/`)가 채우고 렌더러(`quant/report/render/`)가
이 모델**만** 보고 그린다. 필드는 기존 `_emit`/`_emit_close`(구 `quant/apps/
report_cli.py`)가 `write_html`/`write_machine`/`write_close_html`/
`write_close_machine`에 넘기던 인자를 그대로 옮긴 것 — 값도 이름도 바뀌지
않았다(순수 구조 이동, 동작 변경 없음).

두 세션(아침 `open`/마감 `close`)은 산출물 형태 자체가 달라(아침은 카드형
섹션이 10여 개, 마감은 축약 4섹션) 필드 집합이 겹치지 않는 부분이 많다 —
하나로 합치면 옵셔널 필드가 반씩 죽어 있는 모델이 되므로 세션별로 분리한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReportModel:
    """아침(open) 리포트 — `_emit`가 조립해 `write_html`/`write_machine`에 넘긴다."""

    payload: dict  # engine.json 그대로 (write_machine 입력)
    cont: dict = field(default_factory=dict)
    delta: object = None
    brief: object = None
    sym_quotes: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)
    view: object = None
    scores: object = None
    relations: dict | None = None
    sector_view: list = field(default_factory=list)
    flow_rows: list = field(default_factory=list)
    youtube: dict = field(default_factory=dict)
    blog: dict = field(default_factory=dict)
    top_movers: dict = field(default_factory=dict)
    carried_candidates: list = field(default_factory=list)
    disclosures: dict = field(default_factory=dict)
    research: dict = field(default_factory=dict)
    research_badges: list = field(default_factory=list)
    foreign_view: dict | None = None
    digest: dict = field(default_factory=dict)
    digest_prose: dict | None = None
    stance_prose: str | None = None
    news_flow: list = field(default_factory=list)
    intraday_view: list = field(default_factory=list)
    exec_summary: dict | None = None
    section_advice: dict | None = None
    telegram_view_kr: list = field(default_factory=list)
    telegram_view_us: list = field(default_factory=list)
    telegram_prose: dict | None = None
    telegram_image_desc: dict = field(default_factory=dict)
    agent_interpret_view: list = field(default_factory=list)
    midterm_view: list = field(default_factory=list)
    us_news_kr_view: list = field(default_factory=list)
    usnews_headlines: list = field(default_factory=list)
    # 어젯밤 미국장→오늘 한국장 브리지(2026-08-21, KR 아침판 전용) — None 이면
    # 렌더가 섹션을 생략한다. analyze/us_kr_bridge.build_us_kr_bridge 결과.
    us_kr_bridge: dict | None = None


@dataclass
class CloseReportModel:
    """마감(close, 서브프로젝트 R) 리포트 — `_emit_close`가 조립해
    `write_close_html`/`write_close_machine`에 넘긴다."""

    payload: dict  # close_engine.json 그대로 (write_close_machine 입력)
    news_view: list = field(default_factory=list)
    flow_view: dict = field(default_factory=dict)
    ranking_view: dict = field(default_factory=dict)
    intraday_view: list = field(default_factory=list)
    telegram_view_kr: list = field(default_factory=list)
    telegram_view_us: list = field(default_factory=list)
    agent_interpret_view: list = field(default_factory=list)
    midterm_view: list = field(default_factory=list)
    us_news_kr_view: list = field(default_factory=list)
    usnews_headlines: list = field(default_factory=list)
