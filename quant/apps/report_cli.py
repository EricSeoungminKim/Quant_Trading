"""CLI. Phase 1은 크론 없이 손으로 돌려 검증한다.

    python -m quant.apps.report_cli build  --market KR
    python -m quant.apps.report_cli render --market KR --date 2026-08-12
    python -m quant.apps.report_cli when   --market US --date 2026-08-12
    python -m quant.apps.report_cli summary --market KR --date 2026-08-12

Phase D 엔진 분리(2026-08-19, `docs/superpowers/specs/2026-08-19-engine-separation-design.md`)
— 이 파일은 이제 얇은 진입점(인자 파싱 + 조립)이다. 섹션별 수집기는
`quant/report/collect/`, 렌더러는 `quant/report/render/`, 두 세션(아침/마감)이
그리는 대상은 `quant/report/model.py`의 `ReportModel`/`CloseReportModel`에 있다.
`_emit`/`_emit_close`가 수집기를 호출해 모델을 채우고 렌더러에 넘기는 조립부다.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.analyze.delta import previous_snapshot
from quant.analyze.entities import (
    extract as extract_kr, extract_us, load_name_map, load_table, load_us_table,
)
from quant.analyze.opendays import anchor_dir_for, last_open_day
from quant.analyze.telegram_view import build_telegram_view
from quant.core.report_clock import KST, publish_at
from quant.collect import collector
from quant.collect.snapshot import collect, load_snapshot, save_snapshot
from quant.analyze.entities import make_symbol_resolver
from quant.collect.sources import build_seeded_source, build_sources
from quant.collect.sources.dart import append_ledger as append_disclosures
from quant.collect.sources.dart import fetch_disclosures
from quant.adapters.kv import make_kv
from quant.control import selections
from quant.control.opstate import record_feed_health, record_run

from quant.report.model import CloseReportModel, ReportModel
from quant.report.paths import (
    _close_engine_json_path, _engine_json_path, _load_artifact, _paths,
)
from quant.report.render.html import write_close_report, write_open_report
from quant.report.render.telegram import _format_close_summary, _format_summary
from quant.report.collect.agent_interpret import (
    AGENT_INTERPRET_TOP_N, _AGENT_INTERPRET_PRODUCER, _AGENT_INTERPRET_PRODUCER_CLOSE,
    _build_agent_disclosures, _build_agent_foreign_flow, _build_agent_interpret,
    _build_agent_news_items, _build_track_record, _score_breakdown_from_intraday,
)
from quant.report.collect.briefs import _fetch_blog_briefs, _fetch_telegram_briefs, _fetch_youtube_briefs
from quant.report.collect.carryover import _apply_carryover
from quant.report.collect.close import (
    _build_close_bet_view, _build_close_flow_view, _build_close_news_view,
    _build_close_ranking_view,
)
from quant.report.collect.core import _derive
from quant.report.collect.holiday_synthesis import _apply_holiday_synthesis
from quant.report.collect.intraday import (
    _INTRADAY_PRODUCER, _INTRADAY_PRODUCER_CLOSE, _build_intraday_view, _candidate_symbols,
    _theme_change_pct, _visible_intraday,
)
from quant.report.collect.ledger import (
    _load_flow_rows, _record_agent_interpret_selections, _record_intraday_selections,
    _record_midterm_selections, _record_selections, _should_record_ledger,
)
from quant.report.collect.midterm import (
    _MIDTERM_PRODUCER, _MIDTERM_PRODUCER_CLOSE, _apply_midterm_prose,
    _build_midterm_bullish, _build_midterm_prose, _build_midterm_watch_view,
    _build_us_news_kr_view, _load_midterm_telegram_msgs, _midterm_entities,
    _midterm_name_by_symbol,
)
from quant.report.collect.money_flow import build_money_flow_view
from quant.report.collect.news import (
    _build_digest, _build_digest_prose, _build_exec_summary, _build_news_flow,
    _build_section_advice, _build_stance_prose, _load_disclosures, _load_research,
    _research_badges, _source_data,
)
from quant.report.collect.sector import (
    _build_foreign_view, _build_sector_daily_view, _build_sector_view, _build_top_movers,
    _load_sector_data,
)
from quant.report.collect.snapshot import (
    CLOSE_NEWS_FALLBACK_WINDOW, _close_snapshot_path, _collect_snapshot,
    _load_morning_snapshot, close_news_since_for, news_since_for,
)
from quant.report.collect.telegram import (
    _build_telegram_image_desc, _build_telegram_mentions, _build_telegram_prose,
    _usnews_headlines, _usnews_titles,
)
from quant.report.collect.tg_digest_section import _build_channel_digest_view
from quant.report.collect.uswrap import build_us_wrap, gather_kr_wrap, load_latest_us_wrap, write_us_wrap


def _print_summary(market: str, root: Path, session: date, session_kind: str = "open") -> None:
    """`summary` 서브커맨드 몸통. 엔진 JSON 이 없거나 깨졌으면 아무 것도
    출력하지 않는다(exit 0) — 알림은 부가 기능이라 실패가 발송을 막지 않는다.

    `session_kind="close"`(서브프로젝트 R)면 close 엔진 JSON
    (`_close_engine_json_path`)을 읽고 `_format_close_summary`로 포맷한다 —
    아침 경로/포맷과 완전히 분리돼 있어 한쪽이 없어도 다른 쪽 알림에 영향이
    없다.
    """
    out_root = root / "out"
    if session_kind == "close":
        payload = _load_artifact(_close_engine_json_path(out_root, market, session))
        if payload is None:
            return
        print(_format_close_summary(payload))
        return
    payload = _load_artifact(_engine_json_path(out_root, market, session))
    if payload is None:
        return
    print(_format_summary(payload))


def _emit_close(snap, root: Path, out_root: Path, snap_root: Path) -> None:
    """마감 포지션 리포트 발행(서브프로젝트 R) — 아침 파이프라인(`_emit`)의
    축약판. Exec Summary·업종 히트맵·유튜브/블로그 브리핑·시황 다이제스트
    LLM 요약 등 무거운 섹션은 아예 조립하지 않는다(스펙 §형식).

    **텔레그램 인사이트(서브프로젝트 S part 2)는 예외적으로 포함한다** —
    사용자 지시대로 "오후엔 장중 시황방(tazastock/mootda) 가치가 특히 크다".
    단 결정론적 채널 카드 뷰(`build_telegram_view`)만 싣고, 채널별 narrator
    요약(`_build_telegram_prose`)은 **부르지 않는다**.

    **AI 심층 해석(서브프로젝트 U)도 예외적으로 포함한다(사용자 결정
    2026-08-17: 대상 top-5, 아침+오후 양쪽 적용)** — 오후판은 그 전까지
    완전 무LLM 계약이었고, 이 호출이 그 계약을 깨는 유일한 지점이다.
    `_build_agent_interpret`가 실패해도(키 없음/LLM 실패) 예외를 삼키고
    빈 리스트를 돌려주므로(narrate 계약과 동일) 마감 리포트 발행 자체는
    막지 않는다 — 그 외 섹션(Exec Summary 등)은 여전히 아예 조립하지
    않는다.

    원장 기록은 단타 스코어러 producer(③, `_INTRADAY_PRODUCER_CLOSE`)와
    AI 심층 해석 producer(`_AGENT_INTERPRET_PRODUCER_CLOSE`)만 한다 —
    `_derive`를 `record_ledger=False`로 불러 `_log_overlap`/`_record_flows`/
    `_record_frgn_flow`/`_record_selections`는 건너뛴다. 그 원장들은 아침
    리포트가 이미 그날의 대표 기록을 남겼다(하루 두 번 같은 날짜로
    재기록하면 flows.jsonl 기간합 등이 중복 오염된다 — G Task 4 의 휴장일
    재기록 차단과 같은 문제의식). 단타 스코어러·AI 심층 해석 원장만은
    아침/오후 선정을 **의도적으로** 각각 남긴다(검증 하네스가 세션별
    성적을 따로 채점하기 위해서, 스펙 §내용 3) — 그래서
    `_should_record_ledger`(주말/재기록 게이트)는 그대로 통과시킨다.
    """
    record_ledger = _should_record_ledger(snap.market, root, snap.session_date)
    cont, delta, brief, payload, sym_quotes, details, view, scores = _derive(
        snap, root, snap_root, record_ledger=False,
    )
    relations = _load_artifact(root / "data" / "ledger" / "relations.json")
    themes = _load_artifact(root / "data" / "ledger" / "themes.json")
    disclosures = _load_disclosures(root, snap.session_date, set(cont.keys()))

    news_flow = _build_news_flow(snap)
    news_view = _build_close_news_view(news_flow)
    flow_view = _build_close_flow_view(payload)
    ranking_view = _build_close_ranking_view(snap)
    telegram_result = _fetch_telegram_briefs(root)
    # 텔레그램 인사이트 KR/US 고정 탭(2026-08-18) — 리포트 시장과 무관하게
    # 두 시장 채널 카드 뷰를 모두 빌드해 템플릿에 넘긴다. `telegram_view`는
    # 이 리포트 시장 몫(기존 계약 유지 — engine.json의 "telegram" 필드,
    # narrator 프롬프트 등은 여전히 이 시장 채널만 본다).
    telegram_view_kr = build_telegram_view(telegram_result, "KR")
    telegram_view_us = build_telegram_view(telegram_result, "US")
    telegram_view = telegram_view_kr if snap.market == "KR" else telegram_view_us
    telegram_mentions = _build_telegram_mentions(root, snap.market, payload, telegram_result)
    # scalp_grade의 "섹터 상승" 입력용 sector_map — 로컬 아티팩트라 네트워크가
    # 없다(마감판은 속도가 우선이라 `fetch_sector_quotes()`는 여기서 타지
    # 않는다 — `sector_quotes=None`이면 `grade_scalp`가 섹터 상승을 정직하게
    # None으로 받는다, `_build_intraday_view` docstring).
    close_sector_map = (
        _load_artifact(root / "data" / "ledger" / "sector_map.json") or {}
        if snap.market == "KR" else {}
    )
    intraday_view = _build_intraday_view(
        root, snap.market, payload, cont, None, relations, themes, disclosures,
        telegram_mentions=telegram_mentions, sector_map=close_sector_map,
    )
    if record_ledger:
        _record_intraday_selections(
            intraday_view, payload, root, producer=_INTRADAY_PRODUCER_CLOSE,
        )
    # 중기 관심 종목(서브프로젝트 W part 3) — AI 심층 해석(U)의 단타 후보
    # 0건 폴백 소스로도 재사용하므로 여기서 먼저 계산한다(호출 순서 조정
    # 2026-08-18 — 중복 계산 금지, 등급/정렬까지 결정론 계산만 먼저 하고
    # AI 산문은 아래에서 별도로 얹는다). 아침판과 같은 예외(사용자 결정
    # 2026-08-17: U 가 이미 오후판의 무LLM 계약을 깬 전례를 그대로 따른다).
    midterm_telegram_msgs = _load_midterm_telegram_msgs(root)
    midterm_view = _build_midterm_watch_view(
        root, snap.market, payload, midterm_telegram_msgs, snap.session_date,
    )
    # AI 심층 해석(서브프로젝트 U) — 사용자 결정(2026-08-17: 아침+오후 양쪽
    # 적용)으로 오후판의 무LLM 계약을 이 지점에서만 깬다(_emit_close docstring).
    # 단타 후보 0건이면 위에서 미리 계산한 중기 관심 종목 상위 5개로 폴백한다
    # (2026-08-18, `_build_agent_interpret` docstring).
    #
    # 시간 예산 180s (2026-08-18 실측 사고): 마감판은 13:40 빌드 → 13:50 발행 →
    # 13:55 오후 자동편입 → 14:00 유니버스 롤의 분 단위 체인 위에 있다. 무료
    # 레인이 퇴화한 날(라운드 소진→폴백 소진, 후보당 수 분) 첫 가동에서 빌드가
    # 24분+ 걸려 편입이 rc=3 랭킹 폴백으로 떨어졌다 — 오후판에서 AI 해석은
    # 있으면 좋은 것이고 체인은 깨지면 안 되는 것이다.
    agent_interpret_view, agent_interpret_status = _build_agent_interpret(
        root, snap, payload, intraday_view, midterm_view, telegram_mentions,
        time_budget_seconds=180,
    )
    if record_ledger:
        _record_agent_interpret_selections(
            agent_interpret_view, payload, root, producer=_AGENT_INTERPRET_PRODUCER_CLOSE,
        )
    midterm_view = _apply_midterm_prose(midterm_view, _build_midterm_prose(midterm_view))
    if record_ledger:
        _record_midterm_selections(midterm_view, payload, root, producer=_MIDTERM_PRODUCER_CLOSE)
    us_news_kr_view = (
        _build_us_news_kr_view(root, telegram_result, snap.session_date)
        if snap.market == "KR" else []
    )
    usnews_headlines = _usnews_headlines(telegram_result) if snap.market == "US" else []

    # 📡 채널 브리핑 종합(2026-09-05, 소유자 요구 (3)) — 마감판은 narrator=None
    # 으로 불러 결정론 다이제스트만(마감판 LLM-free 계약, 이 함수 docstring).
    channel_digest = _build_channel_digest_view(snap, root, snap_root, sym_quotes, narrator=None)

    # 종가배팅 후보(2026-08-25) — KR 전용. 토큰 체인: 이 뷰 → close_bet_tokens
    # (SYM:CLOSE) → own_brief 14:52 → watch-score → 태그 CLOSE_BET → 유니버스 롤
    # 14:53 → close_bet 진입 창 15:15~15:19(연속 거래 마지막 구간).
    # 15:20 부터는 동시호가라 체결 자체가 15:30 일괄이다 — 그 구간엔 주문하지 않는다.
    # 소유자도 같은 카드로 실계좌 판단.
    close_bet_view = (
        _build_close_bet_view(snap, root, cont) if snap.market == "KR" else []
    )
    if close_bet_view:
        print(f"종가배팅 후보 {len(close_bet_view)}건: "
              + ", ".join(i["symbol"] for i in close_bet_view))

    close_payload = {
        "schema": 1,
        "market": snap.market,
        "session_date": snap.session_date.isoformat(),
        "generated_at": snap.generated_at.isoformat(),
        "session": "close",
        "missing": snap.missing(),
        "market_flow": flow_view,
        "rankings": ranking_view,
        "news": news_view,
        "intraday_view": intraday_view,
        "telegram": telegram_view,
        "agent_interpret": agent_interpret_status,
        "midterm_watch": midterm_view,
        "us_news_kr_map": us_news_kr_view,
        "usnews_headlines": usnews_headlines,
        "close_bet_view": close_bet_view,
        # 📡 채널 브리핑 종합(2026-09-05) — ReportModel과 같은 직렬화 요약
        # (Digest 객체 자체가 아니라 요약만 — engine.json은 json.dumps 대상).
        "channel_digest_summary": (
            {
                "candidates": len(channel_digest.candidates),
                "risk_items": len(channel_digest.risk_items),
                "stance": channel_digest.stance,
            }
            if channel_digest is not None and (channel_digest.has_content() or channel_digest.channel_notices)
            else None
        ),
    }

    intraday_display = _visible_intraday(intraday_view)

    model = CloseReportModel(
        payload=close_payload, news_view=news_view, flow_view=flow_view,
        ranking_view=ranking_view, intraday_view=intraday_display,
        telegram_view_kr=telegram_view_kr, telegram_view_us=telegram_view_us,
        agent_interpret_view=agent_interpret_view, midterm_view=midterm_view,
        us_news_kr_view=us_news_kr_view, usnews_headlines=usnews_headlines,
        channel_digest=channel_digest,
    )
    hp, jp = write_close_report(model, snap, out_root)
    print(f"HTML(마감) {hp}\n엔진(마감) {jp}")
    if snap.missing():
        print(f"결측 {len(snap.missing())}건: {', '.join(snap.missing())}", file=sys.stderr)


def _emit(snap, root: Path, out_root: Path, snap_root: Path) -> None:
    # 원장 기록 게이트(G Task 4) — 개장일 판정과 무관하게 렌더·write_machine·
    # 텔레그램은 항상 돈다. 세 원장 쓰기(_log_overlap/_record_flows/
    # _record_selections)만 건너뛴다.
    record_ledger = _should_record_ledger(snap.market, root, snap.session_date)
    if not record_ledger:
        last_open = last_open_day(anchor_dir_for(snap.market, root), snap.session_date)
        print(
            f"원장 기록 건너뜀 — 마지막 개장일 데이터는 이미 기록됨 (last_open={last_open.isoformat()})",
            file=sys.stderr,
        )
    # 전일 마감 종합(wrap)을 _derive 보다 먼저 읽는다 — KR 패턴 종목을 후보
    # 유니버스(machine_payload volume_watch 합류)에 넣기 위해서다. 표시용
    # 카드(us_wrap)도 같은 객체를 재사용한다.
    us_wrap = load_latest_us_wrap(out_root, snap.session_date) if snap.market == "KR" else None
    extra_watch = ((us_wrap or {}).get("kr") or {}).get("symbols") or None
    cont, delta, brief, payload, sym_quotes, details, view, scores = _derive(
        snap, root, snap_root, record_ledger=record_ledger, extra_watch=extra_watch,
    )
    if record_ledger:
        _record_selections(payload, root)
    # 수혜주 트리(§Task 4) — machine_payload 가 이미 읽는 관계 아티팩트를 HTML
    # 렌더 경로에도 넘긴다. _derive 는 내부 지역변수로만 쓰므로 여기서 한 번 더
    # 읽는다(작은 JSON, 결정론적 재읽기 — _derive 튜플을 늘려 기존 테스트
    # 언패킹을 깨뜨리는 것보다 저렴하다).
    relations = _load_artifact(root / "data" / "ledger" / "relations.json")
    # 업종·테마 아티팩트는 네이버 KR 전용이다(themes.json 도 KR 에서만
    # 갱신된다 — deepdive.py). US 리포트에 KR 업종 데이터가 새는 걸 막는다.
    if snap.market == "KR":
        sector_map, sector_quotes, sector_members = _load_sector_data(root)
        themes = _load_artifact(root / "data" / "ledger" / "themes.json")
        sector_view = _build_sector_view(sector_map, sector_quotes, sector_members,
                                         cont, sym_quotes, relations)
        top_movers = _build_top_movers(sector_quotes, themes, sector_members)
        foreign_view = _build_foreign_view(root, snap, cont, sector_map, payload)
        # 어젯밤 미국장→오늘 한국장 브리지(2026-08-21 소유자 지시). 미국 정규장은
        # KST 새벽에 끝나므로 아침 빌드 시점의 `sectors` 소스(S&P 섹터 ETF 11종)가
        # 곧 "방금 끝난 미국 세션"이다 — 새 수집 없이 스냅샷+업종 아티팩트만 쓴다.
        # 표시·근거 계층이다: AUTO_WATCH(자동편입→자동매수)에는 넣지 않는다
        # (백로그 §6 "자동매수 소스 추가 = 사용자 결정").
        from quant.analyze.us_kr_bridge import build_us_kr_bridge
        from quant.report.collect.news import _source_data

        us_kr_bridge = build_us_kr_bridge(
            (_source_data(snap, "sectors") or {}).get("sectors"), sector_members,
        )
        # 전일 마감 종합(wrap)은 위(_derive 전)에서 이미 로드했다 — 후보 합류와
        # 카드 표시가 같은 객체를 쓴다.
        # 주도 섹터(sector_daily, 2026-09-03 소유자 철학 지시 B) — 데이터가
        # 없으면(원장 초기 배포 등) {"missing": True}로 감싸 "결측" 카드를
        # 그리게 한다(§C, us_kr_bridge처럼 조용히 None으로 섹션을 지우면
        # "US 리포트라 해당 없음"과 "KR인데 오늘 데이터가 없음"이 구분 안 된다).
        sector_daily = _build_sector_daily_view(root, snap.market) or {"missing": True}
    else:
        sector_view, top_movers = [], {}
        foreign_view = None
        themes = None
        sector_map, sector_quotes = {}, []
        us_kr_bridge = None
        sector_daily = None
    flow_rows = _load_flow_rows(root)
    youtube = _fetch_youtube_briefs()
    blog = _fetch_blog_briefs()
    # 텔레그램 인사이트(서브프로젝트 S part 2) — 채널 카드 뷰 + 종목 언급
    # 가산점(v4) 입력을 여기서 한 번만 조립해 아래 두 곳(intraday_view/
    # write_html)이 공유한다.
    telegram_result = _fetch_telegram_briefs(root)
    # 텔레그램 인사이트 KR/US 고정 탭(2026-08-18) — 리포트 시장과 무관하게
    # 두 시장 채널 카드 뷰를 모두 빌드해 템플릿에 넘긴다. `telegram_view`
    # (이 리포트 시장 몫)는 부가 기능(narrator 요약·사진 AI 해석) 입력으로
    # 기존과 동일하게 쓴다 — 그 로직 자체는 불변이다, 탭은 표시 계층이다.
    telegram_view_kr = build_telegram_view(telegram_result, "KR")
    telegram_view_us = build_telegram_view(telegram_result, "US")
    telegram_view = telegram_view_kr if snap.market == "KR" else telegram_view_us
    telegram_prose = _build_telegram_prose(telegram_view)
    telegram_image_desc = _build_telegram_image_desc(telegram_view)
    disclosures = _load_disclosures(root, snap.session_date, set(cont.keys()))
    # 미국발 뉴스가 시황 서사에 안 닿던 문제(P0, 2026-08-19) — usnews tier
    # 채널(walterbloomberg/financialjuice) 헤드라인은 이미 아래에서
    # us_news_kr_view/usnews_headlines 로 쓰지만, 이 digest 에는 안 들어가
    # "미국발 뉴스 없음"으로 잘못 요약됐다. 같은 텔레그램 결과에서 뽑아 넘긴다.
    digest = _build_digest(snap, usnews_titles=_usnews_titles(telegram_result))
    # 품질 레인(2026-08-18, 사용자 결정: "느린 건 상관없어, 정확도가 중요해")
    # — Exec Summary/시황 다이제스트/4섹션 AI 해석 3곳이 narrator 인스턴스
    # 하나를 공유한다(각자 새로 만들지 않는다). **아침판(`_emit`)에서만** 쓴다
    # — 마감판(`_emit_close`)은 13:50 발행→13:55 자동편입→14:00 유니버스 롤
    # 체인이 분 단위라 속도가 정확도의 전제이므로 기존 무료 레인을 그대로 쓴다.
    from quant.adapters.narrate import TOOL_MODEL, make_quality_narrator

    t0 = time.monotonic()
    quality_narrator = make_quality_narrator()
    digest_prose = _build_digest_prose(digest, narrator=quality_narrator)
    news_flow = _build_news_flow(snap)
    exec_summary = _build_exec_summary(digest, news_flow, payload, foreign_view,
                                       narrator=quality_narrator)
    section_advice = _build_section_advice(snap, narrator=quality_narrator)
    # 엔진 예측 헤드라인 근거 밀도 보강(P0, 2026-08-19) — view(stance) 자체는
    # `_derive`가 이미 결정론으로 정했다(채점 계약 불변). 여기선 그 판정을
    # 뒷받침할 매크로 근거 문단만 추가로 얹는다.
    stance_prose = _build_stance_prose(snap, view, narrator=quality_narrator)
    print(f"품질 레인(exec/digest/section) {time.monotonic() - t0:.1f}초 "
         f"(narrator={quality_narrator.name})")
    # 돈의 흐름(money_flow, 2026-08-31 소유자 지시) — 유가·금리·환율·VIX
    # 매크로 원장으로 자금 흐름/섹터 기울기를 판정한다. quality_narrator를
    # 재사용해(품질 레인 5번째 지점) 산문을 얹지만, 실패/원장 결측이면
    # None — 템플릿은 결정론 판정만으로 완전하다(us_kr_bridge와 같은 관례).
    money_flow_view = build_money_flow_view(snap, root, narrator=quality_narrator)
    payload["money_flow"] = money_flow_view
    # 당일 단타 후보(서브프로젝트 K) — 시장 가드는 _build_intraday_view 내부에 있다.
    telegram_mentions = _build_telegram_mentions(root, snap.market, payload, telegram_result)
    intraday_view = _build_intraday_view(
        root, snap.market, payload, cont, foreign_view, relations, themes, disclosures,
        telegram_mentions=telegram_mentions, sector_map=sector_map, sector_quotes=sector_quotes,
    )
    if record_ledger:
        _record_intraday_selections(intraday_view, payload, root)
    # 중기 관심 종목(서브프로젝트 W part 3) — 텔레그램 언급 + 외국인 수급 +
    # 호재/악재로 진입 등급을 매긴 "앞으로 투자하기 좋아 보이는 종목". AI 심층
    # 해석(U)의 단타 후보 0건 폴백 소스로도 재사용하므로 여기서 먼저 계산한다
    # (호출 순서 조정 2026-08-18 — 중복 계산 금지, 등급/정렬까지 결정론 계산만
    # 먼저 하고 AI 산문은 아래에서 별도로 얹는다). engine.json 에도 실어(아래
    # payload["midterm_watch"]) 무LLM 소비자(own_brief)가 나중에 쓸 수 있게 한다.
    midterm_telegram_msgs = _load_midterm_telegram_msgs(root)
    midterm_view = _build_midterm_watch_view(
        root, snap.market, payload, midterm_telegram_msgs, snap.session_date,
    )
    # AI 심층 해석(서브프로젝트 U) — 단타 후보 top-5 에 툴콜링 에이전트를 돌려
    # "왜 지금 이 종목인가" 산문 + 방향·확신 판정을 만든다(아침판 producer는
    # 기본값 _AGENT_INTERPRET_PRODUCER). 단타 후보 0건이면 위에서 미리 계산한
    # 중기 관심 종목 상위 5개로 폴백한다(2026-08-18, `_build_agent_interpret`
    # docstring). 상태는 engine.json 에 남겨(아래 payload["agent_interpret"])
    # LLM 지연/실패가 빌드를 죽이지 않았다는 증거로 남는다.
    # 시간 예산 600s(2026-08-18): 아침·저녁판 빌드 리드는 25분 — 정상일 땐
    # 2~3분에 끝나지만(실측 128s), 무료 레인 퇴화 날 발행 시각을 못 지키는
    # 것보다 후보 일부 생략이 낫다(마감판 180s 와 같은 사고의 예방).
    agent_interpret_view, agent_interpret_status = _build_agent_interpret(
        root, snap, payload, intraday_view, midterm_view, telegram_mentions,
        time_budget_seconds=600,
    )
    payload["agent_interpret"] = agent_interpret_status
    if record_ledger:
        _record_agent_interpret_selections(agent_interpret_view, payload, root)
    # 품질 레인 4번째 지점 — TOOL_MODEL(U 가 실측한 1순위)을 OpenRouter 폴백에
    # 그대로 지정하고 싶어(`_build_midterm_prose` docstring) 위 3곳과는 별도
    # 인스턴스를 만든다. Claude CLI 1순위는 동일하다.
    t0 = time.monotonic()
    midterm_narrator = make_quality_narrator(model=TOOL_MODEL)
    midterm_view = _apply_midterm_prose(
        midterm_view, _build_midterm_prose(midterm_view, narrator=midterm_narrator),
    )
    print(f"품질 레인(중기 관심종목 산문) {time.monotonic() - t0:.1f}초 "
         f"(narrator={midterm_narrator.name})")
    if record_ledger:
        _record_midterm_selections(midterm_view, payload, root)
    # "🇺🇸→🇰🇷 미국발 섹터 수혜주"(KR 전용) / "🇺🇸 실시간 헤드라인"(US 전용) —
    # 둘 다 usnews tier 채널(walterbloomberg/financialjuice) 헤드라인이 입력이라
    # 시장별로 배타적으로 하나만 채운다.
    us_news_kr_view = (
        _build_us_news_kr_view(root, telegram_result, snap.session_date)
        if snap.market == "KR" else []
    )
    usnews_headlines = _usnews_headlines(telegram_result) if snap.market == "US" else []
    # 📡 채널 브리핑 종합(2026-09-05, 소유자 요구 (3)) — 품질 레인(quality_narrator)을
    # 재사용해 스탠스·방별 요약까지 낸다(아침판만 — 마감판은 narrator=None).
    channel_digest = _build_channel_digest_view(snap, root, snap_root, sym_quotes, narrator=quality_narrator)
    # engine.json 에는 Digest 객체 그대로가 아니라 직렬화 가능한 요약만 싣는다
    # (write_machine 이 json.dumps 로 그대로 찍는다 — dataclass 는 못 찍는다).
    # 텔레그램 발행 요약(_format_summary)이 이 키를 읽어 4번째 줄을 만든다.
    payload["channel_digest_summary"] = (
        {
            "candidates": len(channel_digest.candidates),
            "risk_items": len(channel_digest.risk_items),
            "stance": channel_digest.stance,
        }
        if channel_digest is not None and (channel_digest.has_content() or channel_digest.channel_notices)
        else None
    )
    payload["midterm_watch"] = midterm_view
    if snap.market == "KR":
        payload["us_news_kr_map"] = us_news_kr_view
        # engine.json 에도 실어 무LLM 소비자(own_brief 등)가 나중에 쓸 수 있게
        # 한다 — midterm_watch 와 같은 관례. None 이어도 키는 남긴다(소비자가
        # "없음"과 "빌드 전"을 구분할 수 있게).
        payload["us_kr_bridge"] = us_kr_bridge
    if snap.market == "US":
        payload["usnews_headlines"] = usnews_headlines
    # 증권사 리서치(H-2 Task 5, P1 배선 수정 2026-08-19) — research.jsonl 도
    # sector/themes 처럼 KR 상장사 전용(naver_research 는 국내 종목분석
    # 리포트만 다룬다). US 리포트로 새지 않게 막는다(sector_view/top_movers
    # 와 같은 게이트).
    #
    # 매칭 대상을 cont(오늘 뉴스에 잡힌 종목)로 좁히면 뉴스에 안 걸린 종목의
    # 리포트가 통째로 버려진다 — naver_research 는 뉴스 언급과 무관하게 매일
    # 60~120건이 나온다. KR 상장사 전체 이름표(load_name_map, 이미 다른
    # 소비자도 재사용하는 캐시라 네트워크 추가 없음)로 넓힌다.
    _, _, _research_cache_dir, _ = _paths(root)
    research_code_to_name = (
        load_name_map(_research_cache_dir, "KR") if snap.market == "KR" else {}
    )
    research = (
        _load_research(root, snap.session_date, research_code_to_name)
        if snap.market == "KR" else {}
    )
    # 뉴스 카드가 없는(=cont 밖) 종목은 리서치가 매칭돼도 붙일 카드가 없다
    # (report.html.j2 의 리서치 배지는 cont 기준 카드 루프 안에서만 그려진다).
    # 그 종목들만 별도로 "N건(증권사...)" 사실 칩으로 최소 노출한다 — 제목·
    # 투자의견은 원장에 없으므로 지어내지 않는다(사용자 지시 2026-08-19).
    research_badges = sorted(
        (
            info for code, info in
            _research_badges(root, snap.session_date, research_code_to_name).items()
            if code not in cont
        ),
        key=lambda r: -r["count"],
    ) if snap.market == "KR" else []
    # 개장일 집계(G Task 3) — selections 원장은 이미 위에서 병합 전 payload 로
    # 기록했다. 여기서부터는 병합본을 쓴다: engine.json(write_machine)·
    # candidates.txt·HTML 의 AUTO_WATCH 문자열이 모두 이 payload 를 공유하므로,
    # 휴장 기간 후보가 자동편입 경로(own_brief → watch-score)에도 그대로 실린다.
    payload = _apply_carryover(payload, snap, root, out_root)
    # 휴장 기간 종합(소유자 요청 2026-08-29) — 오늘이 휴장 뒤 첫 개장일 아침일
    # 때만 값이 들어간다(그 외엔 None, `us_kr_bridge`와 같은 관례로 키는 남긴다).
    # 산문은 위에서 이미 만든 quality_narrator 를 재사용한다 — 새 레인을
    # 만들지 않는다(아침 빌드 LLM 예산: 최대 4곳+1곳, 짧게).
    payload["holiday_synthesis"] = _apply_holiday_synthesis(
        snap, root, out_root, snap_root, narrator=quality_narrator,
    )
    carried_candidates = [
        s for s in payload.get("symbols") or []
        if isinstance(s, dict) and "carried_from" in s
    ]
    # AI 심층 해석(agent_interpret_view)은 이미 위에서 전체 intraday_view
    # 기준으로 계산했으니 여기서 다시 건드리지 않는다 — 아래 표시 필터는
    # write_html 에 넘길 사본에만 적용한다(`_visible_intraday` docstring).
    intraday_display = _visible_intraday(intraday_view)
    model = ReportModel(
        payload=payload, cont=cont, delta=delta, brief=brief, sym_quotes=sym_quotes,
        details=details, view=view, scores=scores, relations=relations,
        sector_view=sector_view, flow_rows=flow_rows, youtube=youtube, blog=blog,
        top_movers=top_movers, carried_candidates=carried_candidates,
        disclosures=disclosures, research=research, research_badges=research_badges,
        foreign_view=foreign_view,
        digest=digest, digest_prose=digest_prose, news_flow=news_flow,
        stance_prose=stance_prose,
        intraday_view=intraday_display, exec_summary=exec_summary,
        section_advice=section_advice, telegram_view_kr=telegram_view_kr,
        telegram_view_us=telegram_view_us, telegram_prose=telegram_prose,
        telegram_image_desc=telegram_image_desc, agent_interpret_view=agent_interpret_view,
        midterm_view=midterm_view, us_news_kr_view=us_news_kr_view,
        usnews_headlines=usnews_headlines, us_kr_bridge=us_kr_bridge,
        us_wrap=us_wrap,
        # 지수별 전망/휴장 기간 종합(소유자 요청 2026-08-29) — 둘 다 위에서
        # payload 에 이미 얹혔다(index_outlook: core._derive, holiday_synthesis:
        # 바로 위). 같은 값을 렌더 모델에도 실어야 report.html.j2 가 표시한다
        # (us_kr_bridge/us_wrap 과 같은 관례 — payload 키와 모델 필드 이중 유지).
        index_outlook=payload.get("index_outlook"),
        holiday_synthesis=payload.get("holiday_synthesis"),
        money_flow=money_flow_view,
        name_map=research_code_to_name,
        sector_daily=sector_daily,
        channel_digest=channel_digest,
    )
    hp, jp, cp = write_open_report(model, snap, out_root)
    print(f"HTML   {hp}\n엔진   {jp}\n후보   {cp}")
    if snap.missing():
        print(f"결측 {len(snap.missing())}건: {', '.join(snap.missing())}", file=sys.stderr)


def _run_uswrap(session: date, root: Path, snap_root: Path, out_root: Path) -> None:
    """`uswrap` 서브커맨드 몸통 — 미국장 마감 직후 종합 리포트(2026-08-25).

    `session`(=그날 US 세션 날짜)의 스냅샷이 이미 저장돼 있으면(`data/
    snapshots/US/{session}.json` — 수동 재실행 등) 새로 수집하지 않고
    재사용한다. 정상 크론 경로(05:10 KST)에서는 그 시각에 아직 그날의 US
    스냅샷이 없으므로(아침판은 저녁에 만들어진다) sectors/market/vix_term
    3개 소스만 최소 수집한다. 텔레그램 발송은 하지 않는다(부가 발송은 셸
    몫 — crontab.txt 참고), stdout 에 한국어 요약만 낸다.
    """
    snap_path = snap_root / "US" / f"{session.isoformat()}.json"
    if snap_path.exists():
        snap = load_snapshot(snap_path)
        print(f"기존 US 스냅샷 재사용: {snap_path}")
    else:
        sources = build_sources("US", session)
        sources = {k: v for k, v in sources.items() if k in ("sectors", "market", "vix_term")}
        snap = collect("US", session, sources)
        if snap.missing():
            print(f"결측 {len(snap.missing())}건: {', '.join(snap.missing())}", file=sys.stderr)

    sector_members = _load_artifact(root / "data" / "ledger" / "sector_members.json")
    payload = build_us_wrap(
        _source_data(snap, "sectors"), _source_data(snap, "market"),
        _source_data(snap, "vix_term"), sector_members,
    )
    if payload is None:
        print("US wrap 생략 — sectors/market/vix_term 소스 전부 없음")
        return
    # 전일 KR 세션 절반(2026-08-25 확장) — "미국장만이 아니라 한국장·미국장을
    # 둘 다 고려한, 다음날 흐름을 파악하는 리포트". KR 전일 개장일 기준.
    from quant.analyze.opendays import anchor_dir_for as _adf, last_open_day as _lod

    try:
        kr_day = _lod(_adf("KR", root), session)
        kr = gather_kr_wrap(root, kr_day)
    except Exception as e:  # noqa: BLE001 — KR 절반 실패가 US 절반 발행을 막지 않는다
        print(f"KR 세션 종합 생략: {type(e).__name__}: {e}", file=sys.stderr)
        kr = None
    if kr:
        payload["kr"] = kr
        n_pat = sum(len(v) for v in (kr.get("patterns") or {}).values())
        print(f"  전일 KR 세션: 패턴 종목 {n_pat}건"
              + (f" · 외인 {kr['flow']['foreign_net_total']:+,.0f}" if kr.get("flow") else ""))
    path = write_us_wrap(payload, out_root, session)
    print(f"US wrap {path}")
    if "tone" in payload:
        print(f"  {payload['tone']} · {payload.get('up_count')}↑ {payload.get('down_count')}↓")
    if payload.get("indices"):
        idx_line = " · ".join(f"{i['label']} {i['change_pct']:+.2f}%" for i in payload["indices"])
        print(f"  지수: {idx_line}")
    if payload.get("kr_focus"):
        kr_line = " · ".join(
            f"{f['us_name']}→{'/'.join(f['kr_sectors'])}" for f in payload["kr_focus"]
        )
        print(f"  국내 연결: {kr_line}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="report")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("build", "render", "when", "collect", "deepdive", "summary", "dart-collect"):
        s = sub.add_parser(name)
        s.add_argument("--market", choices=["KR", "US"], required=True)
        s.add_argument("--date", default=date.today().isoformat())
        if name != "when":
            s.add_argument("--root", default=".")
        # --session(서브프로젝트 R) — "open"(기본, 기존 개장 전 리포트)/
        # "close"(13:40 마감 포지션 리포트). build/summary 에만 있다: render/
        # collect/deepdive/dart-collect 는 마감판 산출물이 없다(when 도 발행
        # 시각 계산이 open 세션 고정이라 무관).
        if name in ("build", "summary"):
            s.add_argument("--session", choices=["open", "close"], default="open")
        if name == "collect":
            # 텔레그램 채널 누적 수집(telegram-collect@, 2026-09-03) — 뉴스
            # collect 와 같은 서브커맨드를 공유하되 플래그로 분기한다. 채널
            # 목록(telegram_channels.CHANNELS)은 시장 무관이라 --market 값
            # 자체는 이 경로에서 쓰이지 않지만, 서브커맨드 계약(필수 인자)은
            # 그대로 따른다 — 별도 서브커맨드를 새로 만들면 systemd 유닛만
            # 하나 더 늘고 얻는 게 없다.
            s.add_argument("--telegram", action="store_true")
    # uswrap(2026-08-25) — US 전용이라 다른 서브커맨드와 달리 --market 이 없다.
    su = sub.add_parser("uswrap")
    su.add_argument("--date", default=date.today().isoformat())
    su.add_argument("--root", default=".")
    a = p.parse_args(argv)
    session = date.fromisoformat(a.date)
    session_kind = getattr(a, "session", "open")

    if a.cmd == "uswrap":
        root = Path(a.root)
        snap_root, out_root, _, _ = _paths(root)
        _run_uswrap(session, root, snap_root, out_root)
        return 0

    if a.cmd == "when":
        print(f"{a.market} {session} 발행: {publish_at(a.market, session):%Y-%m-%d %H:%M %Z}")
        return 0

    if a.cmd == "summary":
        # run_report.sh notify() 가 발행 알림 본문에 덧붙이는 결정론 요약.
        _print_summary(a.market, Path(a.root), session, session_kind)
        return 0

    if a.cmd == "dart-collect":
        # DART 공시 수집(H-2 Task 1). 어제~오늘 창 — dart_collect.sh 가 매일 아침
        # KR 리포트(07:50) 전에 돌려 원장을 채운다. 실패해도 항상 exit 0 —
        # 이 배치가 리포트 발행을 막으면 안 된다(다른 수집/배치 커맨드와 같은 관례).
        root = Path(a.root)
        bgn_de = (session - timedelta(days=1)).strftime("%Y%m%d")
        end_de = session.strftime("%Y%m%d")
        try:
            rows, err = fetch_disclosures(bgn_de, end_de)
            if err:
                print(f"DART 조회 오류: {err}", file=sys.stderr)
            path = root / "data" / "ledger" / "disclosures.jsonl"
            added = append_disclosures(rows, path)
            print(f"공시 {added}건 추가 (조회 {len(rows)}건, {bgn_de}~{end_de})")
        except Exception as e:  # noqa: BLE001 — 이 배치는 절대 리포트 파이프라인을 막지 않는다
            print(f"DART 공시 수집 실패: {type(e).__name__}: {e}", file=sys.stderr)
        return 0

    if a.cmd == "collect" and getattr(a, "telegram", False):
        # 텔레그램 채널 누적 수집(telegram-collect@, 2026-09-03) — news-collect
        # 와 같은 30분 주기 패턴이되 대상이 채널 원장(`telegram_msgs.jsonl`)
        # 하나뿐이다(`--market` 무관, 위 argparse 주석 참고).
        #
        # 텍스트·이미지가 전혀 없는 행은 원장에 남기지 않는다(2026-09-05,
        # `briefs._fetch_telegram_briefs` docstring "포워딩 우회" 절과 같은
        # 이유) — 그대로 두면 나중에 오너가 봇으로 포워딩한 같은 msg_id 의
        # 실제 본문이 append_ledger 의 dedup 에 가려 조용히 버려진다.
        from quant.collect.sources import telegram_channels

        root = Path(a.root)
        path = root / "data" / "ledger" / "telegram_msgs.jsonl"
        result = telegram_channels.fetch_all()
        rows = [
            {"handle": handle, **msg}
            for handle, entry in result.items()
            for msg in entry.get("messages") or []
            if msg.get("text") or msg.get("images")
        ]
        added = telegram_channels.append_ledger(rows, path)
        removed = telegram_channels.prune(path, session)
        errors = {h: e["error"] for h, e in result.items() if e.get("error")}
        print(f"텔레그램 수집 · 신규 {added}건 · 채널 {len(result)}개"
              + (f" · 보존기간 지난 {removed}건 삭제" if removed else ""))
        if errors:
            # 채널 하나의 실패를 조용히 넘기지 않는다 — news-collect 의
            # dead_feeds 경고와 같은 관례.
            print(f"  ⚠ 오류 채널 {len(errors)}개: {', '.join(errors)}", file=sys.stderr)
        return 0

    if a.cmd == "collect":
        # 30분마다 도는 누적 수집. 리포트 빌드와 분리돼 있어 실패해도 리포트가
        # 죽지 않는다(그날 쌓인 만큼으로 돈다).
        root = Path(a.root)
        stat = collector.collect_once(a.market, root)
        print(f"수집 {stat['collected_at'][11:19]} · 신규 {stat['new']}건 "
              f"· 중복 {stat['duplicate']}건 · 누적 {stat['total']}건 "
              f"· 피드 {stat['feeds']}개")
        if stat["dead_feeds"]:
            # 0건 피드를 조용히 넘기지 않는다 — 죽은 피드와 조용한 피드는 다르다.
            print(f"  ⚠ 0건 피드 {len(stat['dead_feeds'])}개: "
                  f"{', '.join(stat['dead_feeds'])}", file=sys.stderr)
        _, _, cache_dir, _ = _paths(root)
        try:
            table = (load_us_table(cache_dir) if a.market == "US"
                     else load_table(cache_dir))
            extract = extract_us if a.market == "US" else extract_kr
            syms = lambda t: [h["symbol"] for h in extract(t, table)]  # noqa: E731
        except Exception as e:  # noqa: BLE001 — 종목 추출 실패가 수집을 죽이지 않는다
            print(f"  종목 태깅 생략: {type(e).__name__}: {e}", file=sys.stderr)
            syms = None
        vp = collector.render_vault(root, a.market, session, symbols_of=syms)
        removed = collector.prune(root, a.market, session)
        print(f"  볼트 {vp}" + (f" · 보존기간 지난 {removed}일치 삭제" if removed else ""))

        # 운영 상태 기록 — 감시(`cli health`)가 읽는 유일한 입구다. 이게 없으면
        # 그 감지기는 "기록이 없다"만 영원히 답한다(2026-08-13 실측: 감시를 배포하니
        # 계측 부재가 그대로 드러났다). 기록 실패는 수집을 죽이지 않는다 —
        # Redis 가 없으면 NullKeyValue 가 조용히 받아 넘긴다(opstate 계약).
        kv = make_kv()
        record_run(kv, f"collect:{a.market}", ok=True,
                   detail=(f"신규 {stat['new']} · 누적 {stat['total']} · "
                           f"피드 {stat['feeds']} · 0건 {len(stat['dead_feeds'])}"))
        record_feed_health(kv, a.market,
                           alive=stat.get("alive_feeds") or [],
                           dead=stat["dead_feeds"])
        return 0

    if a.cmd == "deepdive":
        # 야간 심화 배치 — 리포트 발행과 무관하게 정시. LLM 이 죽어도
        # relations.json 은 어제 것이 그대로 남는다(run_deepdive 계약).
        from quant.adapters.narrate import make_narrator
        from quant.apps.deepdive import ingest_snapshot, run_deepdive
        stats = run_deepdive(a.market, Path(a.root), make_narrator(),
                             today=a.date)
        print(f"deepdive {a.market} · 대상 {stats['sources']}종목 "
              f"· 후보 {stats['candidates']} · 채택 {stats['accepted']}"
              + (" · LLM 불통" if stats["skipped_llm"] else ""))

        # MySQL 적재는 선택 사항이다 — 아티팩트(relations.json)가 이미 진실이고
        # 이건 색인이다(warehouse_cli.py 와 같은 원칙). 접속·적재 어떤 실패도
        # deepdive 자체를 실패시키지 않는다. 스키마(003_relations.sql) 미적용도
        # 여기서 잡히는 실패일 뿐 — 별도로 책임지지 않는다.
        db_status = "skip"
        try:
            import json as _json
            from quant.adapters.db import connect
            conn = connect()
            if conn is not None:
                try:
                    snap_path = Path(a.root) / "data" / "ledger" / "relations.json"
                    snapshot = (_json.loads(snap_path.read_text())
                               if snap_path.exists() else {})
                    n = ingest_snapshot(conn, snapshot)
                    db_status = "ok"
                    print(f"  DB 적재 {n}행")
                finally:
                    conn.close()
        except Exception as e:  # noqa: BLE001 — DB 적재 실패가 deepdive 를 죽이지 않는다
            print(f"  DB 적재 생략: {type(e).__name__}: {e}", file=sys.stderr)

        record_run(make_kv(), f"deepdive:{a.market}",
                   ok=not stats["skipped_llm"],
                   detail=f"accepted={stats['accepted']} db={db_status}")
        return 0

    root = Path(a.root)
    snap_root, out_root, cache_dir, _ = _paths(root)

    if a.cmd == "build" and session_kind == "close":
        # 마감 포지션 리포트(서브프로젝트 R) — KR 전용(비목표: US 오후판 없음,
        # 정규장 구조가 다르다). 아침 build 블록(바로 아래)은 손대지 않고
        # 그대로 둔다 — 이 분기는 완전히 별도 경로다.
        if a.market != "KR":
            print("마감 리포트(--session close)는 KR 전용입니다", file=sys.stderr)
            return 2
        morning_snap = _load_morning_snapshot(snap_root, a.market, session)
        news_since = close_news_since_for(morning_snap, datetime.now(KST))
        print(f"마감 뉴스 표본 시작: {news_since:%Y-%m-%d %H:%M %Z}"
              + (" (아침 스냅샷 없음 — 폴백)" if morning_snap is None else ""))
        snap = _collect_snapshot(a.market, session, cache_dir, news_since)
        close_snap_path = _close_snapshot_path(snap_root, a.market, session)
        close_snap_path.parent.mkdir(parents=True, exist_ok=True)
        close_snap_path.write_text(snap.to_json(), encoding="utf-8")
        print(f"스냅샷(마감) {close_snap_path}")
        _emit_close(snap, root, out_root, snap_root)
        try:
            record_run(make_kv(), f"report_close:{a.market}", ok=True,
                       detail=("결측 " + ",".join(snap.missing())) if snap.missing() else "결측 없음")
        except Exception:  # noqa: BLE001 — 기록 실패가 리포트를 죽이지 않는다
            pass
        return 0

    if a.cmd == "build":
        # 뉴스 표본의 시작점 = 직전 리포트 생성시각. 요일·공휴일을 하드코딩하지
        # 않으므로 주말·장애로 걸른 구간이 자동으로 메워진다(clock.session_window).
        prev = previous_snapshot(a.market, session, snap_root)
        news_since = news_since_for(prev, datetime.now(KST))
        print(f"뉴스 표본 시작: {news_since:%Y-%m-%d %H:%M %Z}")
        snap = collect(
            a.market, session,
            build_sources(a.market, session, news_since=news_since),
        )
        # 2차 배치: 랭킹 시드 뉴스는 toss_rankings 결과에 의존한다. 1차와 같이
        # 돌리면 랭킹 API를 병렬로 두 번 불러 레이트 리밋에 걸린다.
        ranking = snap.results.get("toss_rankings")
        if ranking is not None and ranking.ok and ranking.data:
            seeded = collect(
                a.market, session,
                build_seeded_source(
                    a.market, ranking.data,
                    resolver_factory=lambda: make_symbol_resolver(a.market, cache_dir),
                ),
            )
            snap = replace(snap, results={**snap.results, **seeded.results})
        print(f"스냅샷 {save_snapshot(snap, snap_root)}")
        _emit(snap, root, out_root, snap_root)
        # 운영 상태 기록 (감시가 읽는 입구). 결측 목록을 detail 에 실어 사람이
        # 알림에서 바로 보게 한다 — 2026-08-14 에 결측을 며칠 못 봤다.
        try:
            record_run(make_kv(), f"report:{a.market}", ok=True,
                       detail=("결측 " + ",".join(snap.missing())) if snap.missing() else "결측 없음")
        except Exception:  # noqa: BLE001 — 기록 실패가 리포트를 죽이지 않는다
            pass
        return 0

    sp = snap_root / a.market / f"{session.isoformat()}.json"
    if not sp.exists():
        print(f"스냅샷 없음: {sp} — 먼저 build 를 돌린다", file=sys.stderr)
        return 1
    _emit(load_snapshot(sp), root, out_root, snap_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
