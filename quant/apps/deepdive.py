"""야간 심화 배치 — 본문 → LLM 후보 → 결정론 검증 → 관계 사전.

리포트 발행은 이 배치와 **무관하게 정시**다. 여기서 무엇이 실패하든
relations.json 은 (a) 갱신되거나 (b) 어제 것이 그대로 남는다 — 지워지는
경우는 없다. 스펙: docs/superpowers/specs/2026-08-15-news-deepdive-design.md
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from quant.analyze.relations import (
    MIN_EVIDENCE, build_extraction_prompt, evidence_score, match_codes,
    merge_relation, parse_candidates)
from quant.analyze.theme_search import beneficiaries, select_sources
from quant.collect.sources.article_body import fetch_body
from quant.collect.sources.naver_quant import fetch_quant_top
from quant.collect.sources.naver_research import append_ledger as append_research
from quant.collect.sources.naver_research import fetch_research
from quant.collect.sources.naver_sector import fetch_sector_data
from quant.collect.sources.naver_theme import fetch_themes

TOP_SOURCES = 10        # 유망 종목 수 — 뉴스 언급 상위 (render.rank 와 같은 기준).
                        # KR 에서 테마 아티팩트를 못 얻었을 때만 쓰는 폴백 경로다.
BODIES_PER_SOURCE = 3   # 종목당 본문 기사 수 상한
EVIDENCE_WINDOW_DAYS = 7  # evidence_score 코퍼스 창 — Task 3 계약("최근 7일 뉴스 제목 전체")
SOURCE_WINDOW_DAYS = 2    # 소스 선정·본문 대상 창 — 어제+오늘(C2). 크론이 새벽/저녁에
                          # 돌아 "오늘" 달력일 파일만 보면 몇 시간 표본으로 왜곡된다.

# 테마 기반 소스 선정(서브프로젝트 F, KR 전용) — select_sources 의 상한 파라미터.
# "한 테마가 소스를 독식하지 않는다" 계약을 여기 숫자로 구체화한다.
THEME_MAX_THEMES = 3     # 그날 뉴스가 가리키는 상위 테마 수
THEME_PER_THEME = 3      # 테마당 대장주(거래대금 상위) 소스 수
THEME_LEADER_SLOTS = 2   # 거래상위(naver_quant) 보조 예약석


def _name_maps(root: Path, market: str):
    """(회사명→코드 매칭용, 코드→회사명 표시용).

    매칭용 표는 `entities.load_table`(KR)/`load_us_table`(US) — 뉴스 본문에서
    짧은 이름·모호명이 오탐을 내는 것을 막는 방어(min_len, `AMBIGUOUS_NAMES`)가
    걸린 쪽이다. `load_name_map`의 코드→이름 역변환은 그 방어를 **일부러
    건너뛴다**(코드가 이미 확정된 뒤 표시용 이름을 붙이는 것뿐이라 오탐 위험이
    없다는 전제) — 그걸 매칭에 재사용하면 SK·LG(2글자) 같은 조각, Gap(모호명)
    같은 이름이 그대로 들어와 오탐을 낸다(I5). 테스트는 이 함수를 바꿔친다."""
    from quant.analyze.entities import (
        AMBIGUOUS_NAMES, MIN_NAME_LEN, load_name_map, load_table, load_us_table)
    cache = root / "data" / "cache"          # report_cli._paths 와 같은 경로 규약
    code_to_name = load_name_map(cache, market)  # 표시용 — 우선주명 등 매칭엔 불필요한 이형도 포함
    if market == "US":
        # load_us_table 자체는 필터를 안 걸고 이름·티커를 합친 원표를 낸다
        # (필터는 entities._us_candidates 안에만 있다) — 여기서 같은 방어를 적용한다.
        name_to_code = {n: c for n, c in load_us_table(cache)
                        if len(n) >= MIN_NAME_LEN and n not in AMBIGUOUS_NAMES}
    else:
        name_to_code = dict(load_table(cache))  # load_table 이 이미 min_len 필터 적용
    return name_to_code, code_to_name


def _load_titles(root: Path, market: str, days: list[str]) -> list[dict]:
    rows = []
    for d in days:
        p = root / "data" / "news" / market / f"{d}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _window_days(day: str, n: int) -> list[str]:
    """`day` 를 포함해 과거 `n`일치 날짜 문자열."""
    d0 = date.fromisoformat(day)
    return [(d0 - timedelta(days=i)).isoformat() for i in range(n)]


def _load_or_fetch_themes(root: Path, getter=None) -> dict | None:
    """테마 아티팩트(`data/ledger/themes.json`) — 실패 사다리(F 설계 스펙):
    오늘 이미 갱신됐으면 재수집을 생략하고(전수 수집 ~150초, 리포트 경로에서
    매번 부를 수 없다), 갱신이 필요하면 수집하고, 수집이 실패하면(예외·빈
    결과 모두) **어제 파일**로 폴백한다. 그것도 없으면 `None` — 호출부가
    기존 LLM 경로로 넘어간다.

    "오늘"은 실제 벽시계 날짜(파일 mtime)로 판단한다 — `run_deepdive`의
    `today` 인자(테스트가 과거 날짜로 고정하기도 하는 채점용 값)와는 다른
    축이다. 파일을 새로 쓰면 mtime 은 항상 지금이므로 둘이 어긋나지 않는다.
    """
    led = root / "data" / "ledger"
    path = led / "themes.json"
    if path.exists():
        mtime_day = date.fromtimestamp(path.stat().st_mtime)
        if mtime_day == date.today():
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass  # 파손 — 아래에서 재수집으로 복구 시도
    try:
        themes = fetch_themes(getter=getter)
    except Exception as e:  # noqa: BLE001 — 수집 실패, 어제 파일로 폴백(아래)
        print(f"테마 수집 실패({type(e).__name__}) — 어제 themes.json 으로 폴백",
             file=sys.stderr)
        themes = {}
    if themes:
        led.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")  # A 의 relations.json 관례 — 원자적 쓰기
        tmp_path.write_text(json.dumps(themes, ensure_ascii=False, indent=1))
        os.replace(tmp_path, path)
        return themes
    if path.exists():  # 오늘 수집이 비었거나 실패 — 어제 파일이라도 쓴다
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None
    return None


def _theme_relations(themes: dict, recent_matches: list[set[str]],
                     corpus_matches: list[set[str]],
                     market_leaders: list[dict]) -> tuple[list[str], list[dict]]:
    """테마 기반 소스 선정 + 관계 후보(서브프로젝트 F). `(소스 코드 목록,
    관계 후보 목록)`을 돌려준다 — 소스 목록은 언급 수 상위 10 을 대체해
    LLM 루프에도 그대로 쓰인다(브리프 계약).

    관계의 `reason` 은 네이버 편입사유 **그대로**, `via_theme` 는 테마명이다 —
    LLM 문장과 절대 섞지 않는다(전역 제약). `score` 는 기존 `evidence_score`
    로 채점만 하고 **MIN_EVIDENCE 로 거르지 않는다** — 편입사유라는 팩트가
    이미 있으니, 뉴스에 아직 안 뜬 수혜주까지 사전에 남겨 소비자(B)가 노출
    시점에 임계로 거른다(컨트롤러 결정 — 여기서 거르면 아직 안 뜬 수혜주가
    전부 죽는다).

    ETN 류 영숫자 코드(예: `0183J0`)는 여기서 따로 거르지 않는다 —
    `market_leaders` 가 소스가 되려면 `select_sources` 가 `recent_matches`
    (뉴스 매칭, KIND 명부 기반)에 코드가 있어야 하는데, ETN 은 애초에 그
    명부 밖이라 매칭될 수 없다. 뉴스 매칭이라는 필수 조건이 이미 ETN 을
    자연스럽게 걸러낸다 — 별도 필터는 이 사실과 중복이자 유지보수 부담이다.
    """
    sources = select_sources(
        themes, recent_matches, THEME_MAX_THEMES, THEME_PER_THEME,
        market_leaders=market_leaders, leader_slots=THEME_LEADER_SLOTS,
    )
    name_to_no = {t["name"]: no for no, t in themes.items()}
    candidates: list[dict] = []
    for s in sources:
        via_theme = s.get("via_theme")
        if via_theme is None:
            continue  # 거래상위 보조 소스(select_sources) — 소속 테마가 없어 수혜주 후보를 못 만든다
        theme_no = name_to_no.get(via_theme)
        if theme_no is None:
            continue
        for b in beneficiaries(themes, theme_no, s["code"], THEME_PER_THEME):
            score = evidence_score(b["code"], s["code"], corpus_matches)
            candidates.append({
                "src": s["code"], "dst": b["code"], "kind": "beneficiary",
                "reason": b["reason"], "via_theme": b["via_theme"], "score": score,
                # 이름을 여기서 실어 보낸다 — 오늘 뉴스에 안 뜬 수혜주(naver_theme
                # 의 주 대상)는 relation_items 의 cont 폴백(오늘 언급 종목 사전)에
                # 없어 6자리 코드 그대로 노출됐다(리뷰 결함 c). beneficiaries() 가
                # 이미 이름을 들고 있으니 버리지 않고 실어 보낸다.
                "dst_name": b["name"],
            })
    return [s["code"] for s in sources], candidates


def ingest_snapshot(conn, snapshot: dict) -> int:
    """스냅샷(`{src: [행...]}`) → `relation_store.upsert_relations` 로 적재.

    아티팩트(relations.json)가 진실이고 이건 색인이다 — 실패해도 스냅샷은
    이미 디스크에 남아 있다. 호출부(report_cli)가 접속·적재 실패를 삼킨다.
    """
    from quant.control.relation_store import upsert_relations
    rows = [r for rs in snapshot.values() for r in rs]
    return upsert_relations(conn, rows)


def run_deepdive(market: str, root: Path, narrator, getter=None,
                 today: str | None = None, deadline_min: int = 90) -> dict:
    t0 = time.monotonic()
    day = today or date.today().isoformat()
    # 소스 선정·본문 대상은 오늘 하루가 아니라 어제+오늘이다(C2) — 크론이
    # 05:00/17:30(새벽·이른 저녁)에 돌아 "오늘" 달력일 파일만 보면 몇 시간
    # 표본으로 유망 종목이 정해진다.
    rows = _load_titles(root, market, _window_days(day, SOURCE_WINDOW_DAYS))
    recent_titles = [r["title"] for r in rows]
    # 증거 코퍼스는 오늘 하루가 아니라 최근 7일 전체다(Task 3 계약) — 가중치
    # (≥3건 40점, ≥5건 다양성 +30)가 다일 코퍼스를 전제로 잡혀 있어 하루치만
    # 쓰면 체계적으로 저평가된다.
    corpus_titles = [r["title"]
                     for r in _load_titles(root, market, _window_days(day, EVIDENCE_WINDOW_DAYS))]
    name_to_code, code_to_name = _name_maps(root, market)
    table = list(name_to_code.items())
    # src_name 표시용 폴백(I5 잔여 결함). US 는 매칭용 표(name_to_code =
    # load_us_table, S&P500+전체 상장 ~13,000)와 표시용 표(code_to_name =
    # load_name_map, S&P500 500여개)의 도메인이 다르다 — S&P500 밖 티커(이
    # 확장이 노리던 CoreWeave 류)가 소스로 뽑히면 code_to_name 에 없어
    # `.get(src, src)` 가 원시 티커로 떨어지고, 제목에 티커가 그대로 나올 리
    # 없어 arts 가 비어 조용히 0건이 된다. KR 은 name_to_code(load_table,
    # min_len 필터만)의 도메인이 code_to_name(load_name_map, 필터 없음)의
    # 부분집합이라 이 문제가 없다 — 그쪽은 그대로 code_to_name 이 항상 먼저 맞는다.
    match_name_of = {c: n for n, c in table}

    # 유망 종목: 제목에서 이름 매칭 빈도 상위 (mentions 모듈과 같은 결이지만
    # 밀폐를 위해 제목만으로 센다 — 정확 순위가 아니라 '어디를 팔지'다).
    # `match_codes` 로 긴 이름 우선 마스킹(실측: '이닉스'가 'SK하이닉스'의
    # 부분문자열이라 마스킹 없이는 SK하이닉스 헤드라인을 전부 이닉스 언급으로
    # 잘못 셌다). `recent_matches` 는 `rows` 와 같은 순서라 아래 본문 대상
    # 선택(arts)에도 그대로 재사용한다 — 소스 선정과 본문 선택이 각자
    # 매칭하면 또 갈린다.
    recent_matches = match_codes(table, recent_titles)
    counts: dict[str, int] = {}
    for codes in recent_matches:
        for code in codes:
            counts[code] = counts.get(code, 0) + 1
    # evidence_score 코퍼스도 같은 함수로 한 번만 매칭한다 — 후보마다(최대
    # 5개 × 소스 10개) 다시 매칭하면 비용도 크고, 매칭 결과가 갈릴 수 있다.
    corpus_matches = match_codes(table, corpus_titles)

    # 테마 기반 소스 선정(서브프로젝트 F, KR 전용). US 는 테마 데이터가 없다
    # (naver_theme 이 한국 상장사 전용) — 테마 아티팩트를 아예 보지 않는다.
    # 테마 수집·읽기가 모두 실패하면 `themes` 가 None 이 되고, 아래 `sources`
    # 는 기존 언급 수 상위 폴백으로 넘어간다(실패 사다리 3단계).
    themes = _load_or_fetch_themes(root, getter) if market == "KR" else None
    theme_candidates: list[dict] = []
    if themes:
        market_leaders: list[dict] = []
        try:
            market_leaders = fetch_quant_top(getter=getter)
        except Exception as e:  # noqa: BLE001 — 거래상위 보조 신호 실패가 테마 소스 선정을 막지 않는다
            print(f"거래상위(naver_quant) 수집 실패({type(e).__name__}) —"
                 " 거래상위 보조 소스 없이 진행", file=sys.stderr)
            market_leaders = []
        sources, theme_candidates = _theme_relations(
            themes, recent_matches, corpus_matches, market_leaders)
    else:
        sources = sorted(counts, key=counts.get, reverse=True)[:TOP_SOURCES]

    led = root / "data" / "ledger"
    led.mkdir(parents=True, exist_ok=True)
    snap_path = led / "relations.json"
    snapshot: dict = {}
    if snap_path.exists():
        try:
            snapshot = json.loads(snap_path.read_text())
        except ValueError:
            # 파손된 파일을 조용히 버리지 않는다 — 증거로 옆에 남기고 빈 사전에서
            # 다시 쌓는다(I3). 안 그러면 다음 실행마다 여기서 영구히 죽는다.
            corrupt_path = led / "relations.json.corrupt"
            os.replace(snap_path, corrupt_path)
            print(f"relations.json 파손 — {corrupt_path} 로 백업하고 새로 시작",
                 file=sys.stderr)

    stats = {"sources": len(sources), "candidates": 0, "accepted": 0,
             "skipped_llm": False, "bodies_ok": 0, "bodies_failed": 0}
    for src in sources:
        if (time.monotonic() - t0) > deadline_min * 60:
            break  # 시간 상한 — 부분 결과로 종료(스펙). 리포트는 정시 발행.
        src_name = code_to_name.get(src)
        if src_name is None:  # 표시 표 밖(S&P500 밖 US 티커) — 매칭 표에서 폴백
            src_name = match_name_of.get(src, src)
        arts = []
        # `src_name in r["title"]` (부분문자열) 대신 위에서 이미 계산한
        # `recent_matches`(마스킹 적용)를 쓴다 — 같은 이닉스/SK하이닉스류
        # 오염이 본문 선택에서도 일어날 수 있었다.
        for i, r in enumerate(rows):
            if src in recent_matches[i] and len(arts) < BODIES_PER_SOURCE:
                body = fetch_body(r["link"], getter=getter)
                if body is None:
                    stats["bodies_failed"] += 1
                    continue  # 이 기사만 건너뛴다 — 빈 본문으로 위장하지 않는다(I1a)
                stats["bodies_ok"] += 1
                arts.append({"title": r["title"], "body": body})
        if not arts:
            continue  # 본문을 하나도 못 얻었다 — LLM 호출 없이 스킵(카운트만 남긴다)
        text = narrator.narrate(build_extraction_prompt(src_name, arts, market))
        if text is None:
            stats["skipped_llm"] = True
            continue  # 이 종목만 스킵 — 어제 사전은 그대로
        cands = parse_candidates(text, name_to_code, src)
        stats["candidates"] += len(cands)
        for c in cands:
            score = evidence_score(c["dst"], src, corpus_matches)
            if score < MIN_EVIDENCE:
                continue
            olds = {r["dst"]: r for r in snapshot.get(src, [])}
            old = olds.get(c["dst"])
            if old is not None and old.get("source") == "naver_theme":
                # 팩트 우선은 이번 실행 안에서만이 아니라 날짜를 걸쳐서도
                # 성립해야 한다(2026-08-16 리뷰 I2) — 어제 테마 기반으로
                # 확정된 (src,dst) 가 오늘 테마 후보에서 빠져도(거래대금 순위
                # 변동 등) LLM 이 같은 쌍을 지목했다는 이유만으로 강등되면
                # 안 된다. 오늘 이 쌍이 다시 테마 후보라면 아래 테마 병합
                # 루프가 어차피 갱신한다.
                continue
            snapshot.setdefault(src, [])
            snapshot[src] = [r for r in snapshot[src] if r["dst"] != c["dst"]]
            row = merge_relation(old, c, score, day)
            row["via_theme"] = None
            row["source"] = "llm"
            snapshot[src].append(row)
            stats["accepted"] += 1

    # 테마 관계는 LLM 루프 **뒤에** 병합한다 — 같은 (src,dst) 를 LLM 도 냈다면
    # naver_theme 이 이긴다(팩트 우선, 컨트롤러 결정). 아래는 LLM 루프와 같은
    # "제거 후 추가" 패턴이라 자연히 마지막에 처리된 쪽이 최종값이 된다.
    # MIN_EVIDENCE 로 거르지 않는다 — `_theme_relations` docstring 참고.
    for cand in theme_candidates:
        src, dst = cand["src"], cand["dst"]
        olds = {r["dst"]: r for r in snapshot.get(src, [])}
        snapshot.setdefault(src, [])
        snapshot[src] = [r for r in snapshot[src] if r["dst"] != dst]
        row = merge_relation(olds.get(dst), cand, cand["score"], day)
        row["via_theme"] = cand["via_theme"]
        row["source"] = "naver_theme"
        snapshot[src].append(row)
        stats["accepted"] += 1

    # 원자적 쓰기 — 같은 디렉토리 임시파일 + os.replace(I3). write_text 도중
    # 죽으면(디스크 풀·kill) 절반만 쓰인 JSON이 다음 실행을 영구히 깨뜨린다.
    tmp_path = snap_path.with_name(snap_path.name + ".tmp")
    tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))
    os.replace(tmp_path, snap_path)

    if market == "KR":
        sectors, sector_members = fetch_sector_data(getter=getter)
        if sectors:  # 실패(빈 dict)면 어제 파일을 덮지 않는다
            (led / "sector_map.json").write_text(
                json.dumps(sectors, ensure_ascii=False, indent=1))
        if sector_members:  # 실패(빈 dict)면 어제 파일을 덮지 않는다
            members_path = led / "sector_members.json"
            tmp_members_path = members_path.with_name(members_path.name + ".tmp")
            tmp_members_path.write_text(
                json.dumps(sector_members, ensure_ascii=False, indent=1))
            os.replace(tmp_members_path, members_path)
        # 증권사 리서치 목록(H-2 Task 5) — company_list.naver 도 KR 상장사
        # 전용이라 US 배치에선 돌지 않는다. fetch_research 자체가 페이지 단위로
        # 실패를 삼키지만, append_ledger 쪽까지 한 번 더 방어해 이 스텝의
        # 실패가 deepdive 전체(테마/관계 적재)를 막지 않게 한다.
        try:
            research_rows = fetch_research(getter=getter)
            added = append_research(research_rows, led / "research.jsonl")
            if added:
                print(f"증권사 리서치 {added}건 추가", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — 이 스텝 실패가 deepdive 를 죽이지 않는다
            print(f"증권사 리서치 수집 실패({type(e).__name__}) — 건너뜀",
                 file=sys.stderr)
    return stats
