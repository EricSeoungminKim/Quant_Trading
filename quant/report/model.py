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
    # 전일 미국장 마감 종합 리포트(uswrap, 2026-08-25, KR 아침판 전용) — 전날
    # 새벽에 발행된 US_wrap.json 을 그대로 읽어온 것. None 이면 렌더가 카드를
    # 생략한다. report.collect.uswrap.load_latest_us_wrap 결과.
    us_wrap: dict | None = None
    # 지수별 전망(index_outlook, 2026-08-29) — payload["index_outlook"]과 같은
    # 값(report.collect.index_outlook.build_index_outlook). None 이면 렌더가
    # "지수별 전망" 카드를 생략한다(us_kr_bridge 와 같은 관례).
    index_outlook: dict | None = None
    # 휴장 기간 종합(holiday_synthesis, 2026-08-29) — payload["holiday_synthesis"]
    # 와 같은 값. 오늘이 휴장 뒤 첫 개장일 아침이 아니면 None — 렌더가 섹션
    # 자체를 생략한다.
    holiday_synthesis: dict | None = None
    # 돈의 흐름(money_flow, 2026-08-31 소유자 지시 — "유가·금리·원자재·지수의
    # 숫자 흐름으로 큰손들의 돈이 어디로 쏠릴지 읽어라") — payload["money_flow"]
    # 와 같은 값. quant.report.collect.money_flow.build_money_flow_view 결과.
    # 원장(macro_rates.jsonl)이 비어 있으면 None — 렌더가 섹션을 생략한다
    # (us_kr_bridge와 같은 관례).
    money_flow: dict | None = None
    # KR 상장사 전체 이름표(load_name_map). relation_items 의 마지막 이름 폴백 —
    # 없으면 관련 종목 섹션에 6자리 코드가 그대로 노출된다(2026-09-02 실측 결함).
    name_map: dict[str, str] | None = None
    # 주도 섹터(sector_daily, 2026-09-03 소유자 철학 지시 B) — 거래대금 상위
    # 업종 + 외국인 순매수 + 5일 순위 추이(quant.report.collect.sector.
    # _build_sector_daily_view). `{"date": ..., "sectors": [...]}`이면 표를
    # 그린다. `{"missing": True}`면 KR 리포트인데 그날 데이터가 결측(§C) —
    # 렌더가 "결측 — 섹터 데이터 없음"을 보인다. `None`이면 US 리포트 등
    # 애초에 해당 없음 — 섹션 자체를 생략한다(다른 KR 전용 필드와 같은 관례).
    sector_daily: dict | None = None


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
    # 종가배팅 후보(2026-08-25) — close 전용, 결정론 채점 top-5.
    close_bet_view: list = field(default_factory=list)
