"""뉴스/공시/리서치/시황 다이제스트/Executive Summary/섹션 AI 해석 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from quant.analyze.opendays import anchor_dir_for, last_open_day


def _news_z_by_symbol(payload: dict, root: Path) -> dict[str, float]:
    """오늘 리포트에 오른 종목별 뉴스 발행량 z-score (H-2 Task 4).

    `mentions.jsonl` 을 종목마다 다시 읽는다 — 하루 한 번 리포트 빌드에서만
    부르므로 비용보다 단순함을 우선한다. 계산 불가한 심볼은 결과 dict에서
    아예 빠진다(`build_rows` 가 그걸 "키 생략"으로 옮긴다).
    """
    from quant.analyze import news_momentum

    session_date = payload.get("session_date")
    try:
        upto = date.fromisoformat(str(session_date))
    except (TypeError, ValueError):
        return {}

    path = root / "data" / "ledger" / "mentions.jsonl"
    out: dict[str, float] = {}
    for sym in payload.get("symbols") or []:
        symbol = sym.get("symbol")
        if not symbol:
            continue
        counts = news_momentum.daily_counts(path, symbol, upto)
        z = news_momentum.news_zscore(counts)
        if z is not None:
            out[symbol] = round(z, 2)
    return out


def _load_disclosures(root: Path, session: date, symbols: set) -> dict[str, list[str]]:
    """DART 공시 원장(H-2 Task 1)에서 오늘·전일분 중 리포트 종목과 교집합만 남긴다.

    원장은 계속 자라므로 전체를 매번 스캔하는 대신 `rcept_dt` 로 최근 2일만
    본다. 읽기 실패·원장 부재는 빈 dict 로 폴백해 리포트를 막지 않는다(다른
    `_load_*` 헬퍼와 같은 관례). 교집합이 0 이면 호출부(템플릿)가 아무 것도
    그리지 않는다.

    **(stock_code, report_nm) 동일건 dedup(서브프로젝트 I Part B).** EC2 실측
    (2026-08-17, disclosures.jsonl 2,000건)에서 같은 종목·같은 공시제목이
    `rcept_no`만 다르게(정정·재제출 등) 27그룹·53건 중복 — 전부 같은
    `rcept_dt` 안에서 벌어져 2일 창 안에 그대로 노출되면 리포트에 똑같은
    줄이 반복된다. `rcept_no` 만으로 원장을 append(dart.append_ledger)하니
    이 단계에선 안 걸러진다 — 여기서 (code, report_nm) 로 묶고 건수가
    1건보다 많으면 "외 N건"을 붙인다. 최신 `rcept_dt`만 대표로 남기지만,
    이 함수는 이미 같은 2일 창만 보므로 순서(그룹 최초 등장 순)만 정한다.
    """
    path = root / "data" / "ledger" / "disclosures.jsonl"
    if not path.exists() or not symbols:
        return {}
    # 창의 시작 = 마지막 개장일 (G 의 집계 창 원칙을 공시에도 적용).
    # 고정 2일 창은 연휴를 못 덮는다 — 실측(2026-08-17 광복절 대체휴일 월요일):
    # 금요일(08-14) 공시가 원장에 있는데도 월요일 리포트 창(일·월)에 안 걸려
    # 공시 칩이 0 이었다. 앵커 부재로 개장일 판정이 안 되면 기존 2일 창 유지.
    last_open = last_open_day(anchor_dir_for("KR", root), session)
    since = last_open if last_open is not None else session - timedelta(days=1)
    recent_days = {
        (since + timedelta(days=i)).strftime("%Y%m%d")
        for i in range((session - since).days + 1)
    }
    grouped: dict[tuple[str, str], int] = {}
    try:
        import json as _json

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            if row.get("rcept_dt") not in recent_days:
                continue
            code = row.get("stock_code")
            if code not in symbols:
                continue
            key = (code, row.get("report_nm", ""))
            grouped[key] = grouped.get(key, 0) + 1
    except (OSError, ValueError) as e:  # noqa: BLE001
        print(f"공시 원장 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}

    out: dict[str, list[str]] = {}
    for (code, report_nm), count in grouped.items():
        label = report_nm if count <= 1 else f"{report_nm} 외 {count - 1}건"
        out.setdefault(code, []).append(label)
    return out


def _load_research(root: Path, session: date, code_to_name: dict[str, str]) -> dict[str, list[str]]:
    """증권사 리서치 원장(H-2 Task 5)에서 오늘·전일분 중 리포트 종목명과 일치하는
    것만 남긴다. 원장(`research.jsonl`)은 종목코드가 아니라 종목명만 갖고 있어
    `_load_disclosures`(stock_code 교집합)와 매칭 축이 다르다 — 여기선
    `code_to_name`(호출부가 넘기는 표시용 이름표 — KR 상장사 전체를 넘기면
    당일 뉴스에 안 잡힌 종목의 리포트도 이 dict 엔 남는다, P1 2026-08-19)을
    뒤집어 이름→코드 표를 만들고 리서치 행의 `stock_name` 과 **정확히 같은 문자열**일
    때만 매칭한다. `entities.load_table` 류의 부분매칭/모호명 방어 표를 굳이
    재사용하지 않는 이유: 그 방어는 "뉴스 본문에서 회사명을 찾아내는" 작업의
    오탐(SK·LG 같은 조각)을 막기 위한 것이고, 여긴 이미 확정된 리포트 종목명
    문자열과 이미 확정된 리포트 대상 종목명을 비교하는 것뿐이라 그 위험이
    없다 — 스펙이 명시한 "단순 매칭" 선택.
    """
    path = root / "data" / "ledger" / "research.jsonl"
    if not path.exists() or not code_to_name:
        return {}
    name_to_code = {name: code for code, name in code_to_name.items()}
    recent_days = {
        session.isoformat(),
        (session - timedelta(days=1)).isoformat(),
    }
    out: dict[str, list[str]] = {}
    try:
        import json as _json

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            if row.get("date") not in recent_days:
                continue
            code = name_to_code.get(row.get("stock_name"))
            if code is None:
                continue
            text = f"오늘 리포트: {row.get('broker')} — {row.get('title')}"
            target_price = row.get("target_price")
            if target_price:  # 결측이면 키 자체가 없다 — 0/None 위장 없음(원장 계약)
                text += f" (목표가 {target_price:,}원)"
            out.setdefault(code, []).append(text)
    except (OSError, ValueError) as e:  # noqa: BLE001
        print(f"증권사 리서치 원장 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    return out


def _research_badges(root: Path, session: date, code_to_name: dict[str, str]) -> dict[str, dict]:
    """`_load_research`와 같은 원장을 종목 카드 유무와 무관하게 집계한다(P1,
    2026-08-19 — "증권사 리서치가 통째로 버려진다").

    `report.html.j2`의 리서치 배지는 `cont`(오늘 뉴스에 잡힌 종목) 기준 카드
    루프 안에서만 그려진다 — 뉴스에 안 걸린 종목의 리포트는 `_load_research`가
    잡아도 붙일 카드가 없어 화면에서 사라졌다. 이 함수는 카드와 무관하게
    "오늘 이 종목에 리포트 N건(증권사...)이 나왔다"는 사실만 종목별로 집계해,
    카드 밖에서도(호출부가 `cont`로 다시 걸러 orphan 만 남기는 방식) 노출할 수
    있게 한다. 제목·투자의견은 원장에 없는 정보이므로 **여기 담지 않는다** —
    지어내지 않는다는 사용자 지시(2026-08-19)를 코드로 강제한다.

    반환: `{code: {"name": str, "count": int, "brokers": [중복없이 등장순]}}`.
    """
    path = root / "data" / "ledger" / "research.jsonl"
    if not path.exists() or not code_to_name:
        return {}
    name_to_code = {name: code for code, name in code_to_name.items()}
    recent_days = {
        session.isoformat(),
        (session - timedelta(days=1)).isoformat(),
    }
    out: dict[str, dict] = {}
    try:
        import json as _json

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = _json.loads(line)
            if row.get("date") not in recent_days:
                continue
            stock_name = row.get("stock_name")
            code = name_to_code.get(stock_name)
            if code is None:
                continue
            entry = out.setdefault(code, {"name": stock_name, "count": 0, "brokers": []})
            entry["count"] += 1
            broker = row.get("broker")
            if broker and broker not in entry["brokers"]:
                entry["brokers"].append(broker)
    except (OSError, ValueError) as e:  # noqa: BLE001
        print(f"증권사 리서치 배지 집계 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    return out


def _build_digest(snap, usnews_titles: list[str] | None = None) -> dict:
    """오늘의 시황 요약(서브프로젝트 I) — 스냅샷 뉴스 피드를
    `market_digest.build_digest` 에 그대로 넘긴다.

    뉴스 소스가 결측이거나 실패했으면 빈 `feeds`(`{}`) 를 넘긴다 —
    `build_digest` 는 빈 입력에도 `{"domestic": [], "us_impact": []}` 를
    정직하게 돌려주므로 여기서 따로 예외를 감쌀 필요가 없다(순수 함수,
    네트워크 없음).

    `usnews_titles`(선택, P0 배선 수정 2026-08-19) — 호출부(`report_cli._emit`)가
    `quant.report.collect.telegram._usnews_titles(telegram_result)`를 그대로
    넘긴다. `build_digest`에 통과만 시킨다(이 함수 자체는 여전히 순수 전달).
    """
    from quant.analyze.market_digest import build_digest

    news = snap.results.get("news")
    feeds = (news.data or {}).get("feeds", {}) if news is not None and news.ok else {}
    return build_digest(feeds, snap.market, usnews_titles=usnews_titles)


def _build_news_flow(snap) -> list[dict]:
    """오늘의 뉴스 흐름(리포트 UX 2차 요구 1) — `_build_digest`와 같은 스냅샷
    뉴스 피드에서 econ 큐레이션 없이 사건 단위 전체를 뽑는다.

    feeds 추출 로직이 `_build_digest`와 동일하다 — 뉴스 소스가 결측/실패면
    빈 `feeds`(`{}`)를 넘긴다. `build_news_flow`는 빈 입력에도 빈 리스트를
    정직하게 돌려주므로(순수 함수, 네트워크 없음) 여기서 따로 예외를 감쌀
    필요가 없다.
    """
    from quant.analyze.market_digest import build_news_flow

    news = snap.results.get("news")
    feeds = (news.data or {}).get("feeds", {}) if news is not None and news.ok else {}
    return build_news_flow(feeds)


def _build_digest_prose(digest: dict, narrator=None) -> dict | None:
    """시황 다이제스트 LLM 요약(스펙 §2, 사용자 명시 허용 — 리포트 평면).

    다이제스트가 비어 있으면(오늘 econ 큐레이션을 통과한 사건 없음)
    narrator 를 부르지 않는다 — 빈 입력으로 LLM 을 부르는 건 낭비다.
    `make_narrator`/`Narrator.narrate` 는 절대 예외를 던지지 않는 계약
    (quant.adapters.narrate 문서)이므로 여기서 추가로 감쌀 필요가 없다 —
    키가 없거나 호출이 실패하면 그대로 `None` 이 내려와 호출부는 결정론
    목록만으로 완전하다.

    `narrator`(선택) — 아침판(`_emit`)이 품질 레인(`make_quality_narrator`)을
    4곳에서 공유하려고 주입한다(2026-08-18). 안 넘기면(마감판 `_emit_close`
    등 기존 호출부) 기존과 동일하게 기본 무료 레인을 스스로 만든다.
    """
    if not (digest.get("domestic") or digest.get("us_impact")):
        return None

    from quant.adapters.narrate import make_narrator
    from quant.analyze.market_digest import summarize_digest

    return summarize_digest(digest, narrator or make_narrator())


def _build_exec_summary(
    digest: dict, news_flow: list[dict], payload: dict, foreign_view: dict | None,
    narrator=None,
) -> dict | None:
    """Executive Summary(스펙 §L-1) — 본문 근거 기반 AI 통합 요약.

    `exec_summary.gather_evidence`가 상위 사건 본문을 `article_body.
    fetch_body`로 긁는다 — 개별 기사 fetch 실패는 그 함수 안에서 이미
    title-only 로 격리되지만, 여기서도 한 번 더 감싼다(다른 `_build_*`
    헬퍼와 같은 관례) — narrator 호출까지 포함해 이 블록 전체가 실패해도
    리포트 발행 자체는 막지 않는다. 실패 시 `None` — 호출부(템플릿)는
    기존 스탠스+시황 다이제스트만으로 완전하다(무LLM 폴백).

    `narrator`(선택) — `_build_digest_prose`와 같은 관례(품질 레인 공유 주입)."""
    try:
        from quant.adapters.narrate import make_narrator
        from quant.analyze.exec_summary import gather_evidence, summarize

        evidence = gather_evidence(digest, news_flow, payload.get("features"), foreign_view)
        return summarize(evidence, narrator or make_narrator())
    except Exception as e:  # noqa: BLE001 — Executive Summary 실패가 리포트를 막지 않는다
        print(f"Executive Summary 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _source_data(snap, key: str) -> dict | None:
    """`render._ok`와 동일한 규칙(성공+데이터 있음만 통과)을 리포트 CLI
    쪽에서도 그대로 쓴다 — 템플릿이 `d(key)`로 읽는 것과 같은 소스를
    section_advice 가 파이썬 쪽에서도 봐야 하기 때문이다."""
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def _build_section_advice(snap, narrator=None) -> dict | None:
    """섹션 AI 해석(리포트 UX 3차, 2026-08-17 사용자 피드백) — 수급 체력/
    시장 심리/기술적 지표/유동성·금리 각 섹션이 이미 보여주는 숫자를 조건부
    서술로 재해석한다.

    `_build_exec_summary`와 같은 관례: 근거 수집부터 narrator 호출까지 이
    블록 전체가 실패해도 리포트 발행 자체는 막지 않는다. 실패/전 섹션 결측
    시 `None` — 템플릿은 숫자 카드만으로 이미 완전하다(무LLM 폴백).

    `narrator`(선택) — `_build_digest_prose`와 같은 관례(품질 레인 공유 주입)."""
    try:
        from quant.adapters.narrate import make_narrator
        from quant.analyze.section_advice import advise, gather_section_numbers

        numbers = gather_section_numbers(
            kr_funding=_source_data(snap, "kr_funding"),
            sentiment=_source_data(snap, "sentiment"),
            macro=_source_data(snap, "macro"),
            vix_term=_source_data(snap, "vix_term"),
            sectors=_source_data(snap, "sectors"),
            breadth=_source_data(snap, "breadth"),
        )
        return advise(numbers, narrator or make_narrator())
    except Exception as e:  # noqa: BLE001 — 섹션 AI 해석 실패가 리포트를 막지 않는다
        print(f"섹션 AI 해석 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _build_stance_prose(snap, view: dict, narrator=None) -> str | None:
    """엔진 예측 헤드라인 근거 밀도 보강(P0, 2026-08-19).

    `briefing.stance()`의 점수·라벨·요인(`view`)은 채점에 쓰이므로 절대
    건드리지 않는다 — 여기서 하는 일은 이미 정해진 판정을 뒷받침하는 문단
    하나를 **추가로** 서술하는 것뿐이다(`_build_section_advice`와 같은
    관례: 판단이 아니라 서술). 근거는 `briefing.stance_macro_context`가
    모은, 이미 수집 중인 FRED 금리·NFCI·순유동성·유가·VIX 기간구조다 —
    새 수집기 없음.

    매크로 맥락이 하나도 없으면(전부 결측) narrator 를 부르지 않는다 —
    `view.line`과 다를 게 없는 문장을 LLM에 요청하는 낭비를 막는다.
    실패/무LLM 이면 `None` — 호출부(템플릿)는 `view.line`만으로 이미
    완전하다(무LLM 폴백)."""
    try:
        from quant.adapters.narrate import make_narrator
        from quant.analyze.briefing import stance_macro_context

        macro_ctx = stance_macro_context(snap)
        if not macro_ctx:
            return None

        lines = [
            "다음은 오늘 엔진이 산출한 시장 스탠스와 그 근거, 그리고 관련",
            "매크로 지표다. 이 지표들을 스탠스 판단과 엮어 하나의 문단으로",
            "(3문장 이내) 서술하라. 새로운 판단(매수/매도 지시, 점수 변경)을",
            "내리지 말고, 이미 나온 스탠스를 뒷받침하는 근거를 설명만 하라.",
            "반드시 한국어로만 답하라(영어·중국어 등 다른 언어를 섞지 마라).",
            "",
            f"스탠스: {view.get('label')} ({view.get('score100')}점/100) — {view.get('line')}",
        ]
        if view.get("positives"):
            lines.append("우호 요인: " + ", ".join(view["positives"]))
        if view.get("negatives"):
            lines.append("비우호 요인: " + ", ".join(view["negatives"]))
        if macro_ctx.get("curve"):
            c = macro_ctx["curve"]
            lines.append(f"미 국채 10년-2년 스프레드: {c.get('spread')}%p ({c.get('label')})")
        if macro_ctx.get("nfci_label"):
            lines.append(f"시카고연준 금융환경지수(NFCI): {macro_ctx['nfci_label']}")
        if macro_ctx.get("net_liquidity_change") is not None:
            lines.append(f"순유동성 변화: {macro_ctx['net_liquidity_change']:+,.0f}백만달러")
        if macro_ctx.get("oil"):
            oil_str = ", ".join(f"{k} {v:+.2f}%" for k, v in macro_ctx["oil"].items())
            lines.append(f"유가: {oil_str}")
        if macro_ctx.get("vix_structure"):
            spread = macro_ctx.get("vix_spread")
            lines.append(
                f"VIX 기간구조: {macro_ctx['vix_structure']}"
                + (f" (스프레드 {spread})" if spread is not None else "")
            )
        lines += ["", "다음 형식으로 정확히 한 문단만 답하라(다른 텍스트 없이):",
                  "스탠스 근거: <문단>"]
        prompt = "\n".join(lines)

        text = (narrator or make_narrator()).narrate(prompt)
        if not text:
            return None
        text = text.strip()
        if text.startswith("스탠스 근거"):
            text = text.split(":", 1)[-1].strip() if ":" in text else text
        return text or None
    except Exception as e:  # noqa: BLE001 — 스탠스 AI 해석 실패가 리포트를 막지 않는다
        print(f"엔진 예측 AI 서술 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None
