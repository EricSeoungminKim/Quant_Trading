"""당일 단타 스코어러(서브프로젝트 K) 후보 조립 + 3단계 등급 표시 필터.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

from pathlib import Path

from quant.analyze.bullish_markers import classify_titles, classify_titles_dated
from quant.analyze.foreign_flow_v2 import foreign_score_v2
from quant.analyze.intraday_score import rank_intraday
from quant.analyze.news_cluster import dedup_with_counts
from quant.analyze.scalp_grade import grade_scalp
from quant.collect.sources.dart import classify_report
from quant.control import frgn_flow as frgn_flow_ledger

from quant.report.collect.news import _news_z_by_symbol

# 단타 스코어러(서브프로젝트 K) 요인명(한글, intraday_score.py) → 원장 flat key
# 접두. 원장에 한글 키를 그대로 쓰지 않는 이유: warehouse/leaderboard 쪽 소비자가
# 늘어날 걸 대비해 다른 producer 들과 동일하게 영문 스네이크케이스로 맞춘다.
_INTRADAY_FACTOR_KEYS = {
    "호재 뉴스": "bullish_news",
    "외국인 추세": "foreign_trend",
    "트렌딩": "trending",
    "촉매": "catalyst",
    "텔레그램 시그널": "telegram_signal",
}

# 2026-08-17: 외국인 추세 축이 라벨 기반→v2(`foreign_score_v2`, 서브프로젝트 O)로
# 바뀌면서 "intraday_scorer" → "intraday_scorer_v2"로 올렸다. `selections.append`
# 의 자연키에 producer 가 포함되므로(`_record_intraday_selections` docstring
# 참고) 이 버전 문자열을 바꾸면 예전(v1, 라벨 기반) 픽 행과 새(v2) 픽 행이
# 같은 (날짜,시장,종목) 자연키에서 절대 섞이지 않는다 — 스코어러 정의가
# 바뀌었는데 리더보드가 v1/v2를 같은 계열로 합산하면 그 집계가 조용히
# 틀린다. 과거 v1 행은 원장에 그대로 남는다(재작성하지 않는다 — 그날
# 실제로 무엇이 채점됐는지의 기록이다).
#
# 같은 날 다시(서브프로젝트 P): 뉴스 축이 "기사량"에서 "호재 탐지"(v3,
# `news_axis_v2`)로 바뀌면서 "뉴스 모멘텀"+"다매체 사건" 두 팩터가 "호재
# 뉴스" 하나로 합쳐졌다 — 같은 이유로 "intraday_scorer_v2" → "_v3"로 다시
# 올린다(팩터 구성이 달라졌는데 리더보드가 v2/v3를 섞으면 집계가 틀린다).
#
# 또 같은 날(서브프로젝트 S part 2): "텔레그램 시그널" 축 신설(+가산점 재분배,
# intraday_score.py 모듈 docstring "2026-08-17 — 텔레그램" 절)로 "_v3" → "_v4".
_INTRADAY_PRODUCER = "intraday_scorer_v4"


def _candidate_symbols(payload: dict) -> set[str]:
    """`AUTO_WATCH:` 줄에서 후보 심볼만 뽑는다.

    `_record_selections`(리포트 본선 원장 기록)와 단타 스코어러 조립
    (`_build_intraday_view`)이 이 파싱을 공유한다 — 따로 두면 두 producer가
    서로 다른 후보 집합을 보게 될 수 있다.
    """
    auto = str(payload.get("auto_watch") or "")
    body = auto.split(":", 1)[1] if auto.startswith("AUTO_WATCH:") else ""
    body = body.strip()
    if not body or body == "없음":
        return set()
    return {t.split(":", 1)[0] for t in body.split()}


def _theme_change_pct(symbol: str, relations: dict | None, themes: dict | None) -> float | None:
    """`symbol` 이 속한 테마의 등락률.

    `relations[symbol]` 의 `via_theme`(그 종목이 소속된 테마명 — `deepdive.
    _theme_relations` 가 테마 리더의 수혜주 관계를 만들 때 리더 자신에게
    붙이는 값)로 `themes.json` 아티팩트에서 같은 이름의 테마를 찾아
    `change_pct` 를 읽는다. `relations`/`themes` 어느 하나라도 없거나
    `via_theme` 을 못 찾으면 `None` — 테마가 없는 것과 테마를 모르는 것을
    구분하지 않고 둘 다 정직하게 None 이다(intraday_score.py 의 4대 nullable
    입력 계약과 동일).
    """
    if not relations or not themes:
        return None
    theme_name = next(
        (r.get("via_theme") for r in relations.get(symbol) or [] if r.get("via_theme")),
        None,
    )
    if theme_name is None:
        return None
    for t in themes.values():
        if t.get("name") == theme_name:
            return t.get("change_pct")
    return None


def _build_intraday_view(
    root: Path, market: str, payload: dict, cont: dict, foreign_view: dict | None,
    relations: dict | None, themes: dict | None, disclosures: dict[str, list[str]],
    telegram_mentions: dict[str, dict] | None = None,
    sector_map: dict | None = None, sector_quotes: list[dict] | None = None,
) -> list[dict]:
    """당일 단타 후보(서브프로젝트 K) — `score_intraday` 입력을 리포트가 이미
    로드한 데이터에서 조립해 `rank_intraday(top=8)` 을 돌린다.

    KR 전용이다(`_build_foreign_view` 와 같은 시장 가드) — 외국인 수급·업종·
    테마 아티팩트가 전부 KR 전용이라 US 는 입력 4종 중 다수가 항상 None이
    되어 근거가 되지 않는다.

    채점 대상은 오늘 AUTO_WATCH 후보(`_candidate_symbols`)로 좁힌다 — 근거
    없는 종목까지 단타 후보로 올리면 스코어러의 의미가 없다(candidates_line
    의 근거 원칙과 동일).

    입력은 전부 **이미 계산된 값을 재사용**한다(재계산 금지) — 예외 하나는
    `foreign_v2_score`다(아래 참고, `frgn_flow.jsonl`을 직접 읽는다):
    - `today_articles`/`trending_score100` 은 `machine_payload` 가 이미
      `payload["symbols"]` 항목에 얹은 값(entry) — 없으면 `cont` 로 폴백한다.
    - `foreign_v2_score` 는 종목별 `frgn_flow.jsonl` 시계열(최근 20일,
      `frgn_flow_ledger.load_series`)에 `foreign_score_v2()`를 적용한 원점수다
      (2026-08-17, 서브프로젝트 O — `foreign_trend.classify()` 라벨 대신).
      `_build_foreign_view`의 표시용 `series`(최근 10일, `inst_net` 없음)는
      쌍끌이·20일 정합 판정에 부족해 재사용하지 않는다 — 원장을 독립적으로
      다시 읽는다. 시계열이 아예 없으면 `None`(0으로 위장하지 않는다). 이
      경로엔 봉 캐시가 없어 강도 서브점수는 늘 0으로 평가된다(`intraday_
      verify.py`와 동일한 한계, `foreign_intensity_ratio`가 정직하게 None을
      돌려주는 덕에 예외 없이 조용히 degrade).
    - `max_dup_count` 는 `cont[symbol]["titles"]` 를 `news_cluster.
      dedup_with_counts` 로 사건 단위로 묶은 뒤 최대 `dup_count`.
    - `titles` 는 `cont[symbol]["titles"]`(오늘치 기사 항목 dict 리스트)에서
      `title` 문자열만 뽑는다(2026-08-17, 서브프로젝트 P — `render.
      bearish_markers`가 같은 구조를 읽는 방식과 동일). 악재 거부권 판정
      (`bullish_markers.classify_titles`의 `bearish` 필드)의 폴백 입력이다.
    - `recent_titles` 는 `cont[symbol]["recent_titles"]`(2026-08-18, SK하이닉스
      15점 사건 재점검 — `mentions.continuity()`가 만드는 최근 3개장일 감쇠
      가중 제목, `{"title","weight"}`)를 그대로 넘긴다. 호재 마커 판정은
      이걸 우선 쓴다(`intraday_score._news_axis_factor`가 있으면
      `classify_titles_dated`, 없으면 `titles`로 폴백) — `titles`(오늘치만)
      만 보면 직전 개장일에 터진 호재가 "호재 마커 없음"으로 잡히던 문제를
      고친다.
    - `dart_types` 는 `disclosures[symbol]`(이미 로드된 공시 라벨 문자열)에
      `dart.classify_report` 를 적용한다.
    - `theme_change_pct` 는 `_theme_change_pct` — 없으면 정직하게 None.
    - `telegram_mention` 은 `telegram_mentions.get(symbol)`(서브프로젝트 S
      part 2, v4 신설 축) — 텔레그램 채널 언급이 없으면 정직하게 None(다른
      nullable 4종과 달리 3개-None 게이트 대상이 아니다, intraday_score.py
      `_telegram_factor` docstring).

    뷰의 `foreign_label`(표시용, factors 와 별개)은 여전히 `foreign_view`의
    라벨을 그대로 쓴다 — 그건 채점 입력이 아니라 리포트 사람이 읽는 참고
    문구다(바뀐 적 없음).

    각 항목에 `grade`/`grade_reasons`(`quant.analyze.scalp_grade`, 2026-08-18
    사용자 지시)를 얹는다 — 점수(score100)와 별개로 "지금 사도 되는지" 3단계
    행동 등급. 미달(등급 없음)이면 `grade`는 `None`이고, 이 함수는 **그 항목을
    빼지 않는다** — 원장/engine.json은 전체를 유지하고, 표시 필터는 호출부
    (`_emit`/`_emit_close`)가 별도 목록으로 만든다(원장 채점 연속성 보존,
    `scalp_grade.py` 모듈 docstring). `sector_map`/`sector_quotes`가 없으면
    (예: 마감판 — 속도 우선이라 업종 등락률 네트워크 호출을 안 탄다) 섹터
    관련 두 입력은 정직하게 비활성(섹터 호재 0건·섹터 상승 None)으로 채점된다.
    """
    if market != "KR":
        return []

    candidates = _candidate_symbols(payload)
    if not candidates:
        return []

    sym_by_code = {s["symbol"]: s for s in payload.get("symbols") or [] if s.get("symbol")}
    news_z = _news_z_by_symbol(payload, root)

    foreign_label_by_symbol: dict[str, str] = {}
    for sector in (foreign_view or {}).get("sectors") or []:
        for row in sector.get("rows") or []:
            foreign_label_by_symbol[row["symbol"]] = row["label"]

    frgn_flow_path = root / "data" / "ledger" / "frgn_flow.jsonl"

    # 3단계 등급(scalp_grade)의 "섹터 호재 뉴스" 입력 — 오늘 `cont`에 잡힌
    # 전체 종목(top-8 후보로 좁히기 전)을 업종별로 묶어 호재 히트 수를
    # 센다. `titles`(오늘치)만 본다 — 섹터 버즈는 "오늘 이 업종이 얼마나
    # 시끄러운가"라 개별 종목의 3개장일 감쇠(recent_titles)와는 다른
    # 질문이다(과도한 설계 확장 방지, 최소 수정 원칙).
    sector_bullish_counts: dict[str, int] = {}
    if sector_map:
        for other_symbol, other_c in cont.items():
            sector_name = sector_map.get(other_symbol)
            if sector_name is None:
                continue
            other_titles = [
                t.get("title", "") for t in (other_c.get("titles") or []) if isinstance(t, dict)
            ]
            if classify_titles(other_titles)["bullish_types"]:
                sector_bullish_counts[sector_name] = sector_bullish_counts.get(sector_name, 0) + 1
    sector_change_by_name = {q["name"]: q.get("change_pct") for q in (sector_quotes or [])}

    score_inputs: dict[str, dict] = {}
    grade_inputs: dict[str, dict] = {}
    for symbol in candidates:
        c = cont.get(symbol) or {}
        entry = sym_by_code.get(symbol) or {}
        dup_counts = [r.get("dup_count", 1) for r in dedup_with_counts(c.get("titles") or [])]
        titles = [t.get("title", "") for t in (c.get("titles") or []) if isinstance(t, dict)]
        recent_titles = c.get("recent_titles")
        dart_types = sorted({
            tag for tag in (classify_report(label) for label in disclosures.get(symbol) or [])
            if tag
        })
        flow_series = frgn_flow_ledger.load_series(frgn_flow_path, symbol, days=20)
        foreign_v2_score = foreign_score_v2(flow_series, {})[0] if flow_series else None
        score_inputs[symbol] = {
            "today_articles": entry.get("news_articles_today", c.get("today_articles") or 0),
            "news_z": news_z.get(symbol),
            "max_dup_count": max(dup_counts, default=0),
            "titles": titles,
            "recent_titles": recent_titles,
            "foreign_v2_score": foreign_v2_score,
            "trending_score100": entry.get("trending_score100"),
            "dart_types": dart_types,
            "theme_change_pct": _theme_change_pct(symbol, relations, themes),
            "telegram_mention": (telegram_mentions or {}).get(symbol),
        }

        # scalp_grade 입력 — 여기서 다시 계산하는 건 `classify_titles_dated`
        # 자체(순수 함수, 제목 리스트 substring 매칭)뿐이다. `score_intraday`가
        # 내부에서 이미 같은 판정을 하지만, 그건 100점 배점의 "호재 뉴스" 축
        # 점수용이고 이건 3단계 등급의 "종목 호재 있음/없음" 불리언용이라
        # 별개 소비자다(재계산 비용은 문자열 substring 검사뿐 — 무시할 만하다).
        bull_for_grade = classify_titles_dated(recent_titles) if recent_titles else classify_titles(titles)
        sector_name = sector_map.get(symbol) if sector_map else None
        chg = sector_change_by_name.get(sector_name) if sector_name else None
        last_foreign_net = None
        if flow_series:
            last_foreign_net = flow_series[-1].get("foreign_net")
        grade_inputs[symbol] = {
            "symbol_bullish": bool(bull_for_grade.get("bullish_types")),
            "telegram_mentions": len(((telegram_mentions or {}).get(symbol) or {}).get("channels") or []),
            "sector_bullish_hits": sector_bullish_counts.get(sector_name, 0) if sector_name else 0,
            "sector_rising": (chg > 0) if chg is not None else None,
            "foreign_net_buying": (
                None if last_foreign_net is None else (last_foreign_net > 0 if last_foreign_net != 0 else None)
            ),
        }

    view: list[dict] = []
    for symbol, result in rank_intraday(score_inputs, top=8):
        entry = sym_by_code.get(symbol) or {}
        name = entry.get("name") or (cont.get(symbol) or {}).get("name", symbol)
        grade = grade_scalp(**grade_inputs[symbol])
        view.append({
            "symbol": symbol,
            "name": name,
            "score100": result["score100"],
            "factors": [
                {"name": f[0], "pts": f[1], "max": f[2], "evidence": f[3]}
                for f in result["factors"]
            ],
            "foreign_label": foreign_label_by_symbol.get(symbol),
            "grade": grade.grade if grade else None,
            "grade_reasons": grade.reasons if grade else [],
        })
    return view


def _visible_intraday(intraday_view: list[dict]) -> list[dict]:
    """3단계 등급(scalp_grade) 표시 필터 — "3단계보다도 안 될 것 같은 건
    보여주지도 말고"(사용자 지시, 2026-08-18). `_build_intraday_view`가 만든
    전체 목록(등급 미달=grade None 포함)에서 표시용으로 등급이 있는 항목만
    남긴다. **표시 계층 전용** — engine.json(payload["intraday_view"])과
    선정 원장(_record_intraday_selections)은 이 함수를 거치지 않은 전체
    목록을 그대로 쓴다(채점 연속성 보존, `_build_intraday_view` docstring).
    `_emit`/`_emit_close` 둘 다 이 헬퍼를 공유한다."""
    return [it for it in intraday_view if it.get("grade")]


# 마감 포지션 리포트(서브프로젝트 R) 전용 producer — _INTRADAY_PRODUCER 와 동일 계열,
# 원장 자연키에서 아침/오후 선정이 섞이지 않게 producer 로만 구분한다.
_INTRADAY_PRODUCER_CLOSE = "intraday_scorer_v4_close"
