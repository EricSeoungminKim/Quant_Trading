"""스냅샷에서 파생물(언급/랭킹/트렌딩/시세/베이스라인/스탠스/machine_payload) 계산.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.analyze.baseline import baseline_score
from quant.analyze.briefing import build as build_brief
from quant.analyze.briefing import stance
from quant.analyze.delta import compare, previous_snapshot
from quant.analyze.entities import load_market_map, load_name_map, load_table, load_us_table
from quant.analyze.mentions import append_ledger, collect_mentions, continuity, load_ledger, mark_origin
from quant.analyze.render import machine_payload, rank
from quant.analyze.symbol_score import score_all
from quant.analyze import trending_score as trending_mod
from quant.collect.sources.market import fetch_symbol_quotes
from quant.collect.sources.stock_detail import fetch_many

from quant.report.paths import _load_artifact, _paths
from quant.report.collect.ledger import _log_overlap, _record_flows, _record_frgn_flow


def _derive(snap, root: Path, snap_root: Path, record_ledger: bool = True,
            extra_watch: list[str] | None = None) -> tuple:
    """스냅샷에서 파생물 계산. 네트워크는 종목 사전 캐시가 없을 때만 탄다.

    `record_ledger=False`(G Task 4)면 `_log_overlap`/`_record_flows` 를
    건너뛴다 — 나머지 파생(시세·트렌딩·베이스라인 등)은 그대로 계산한다.
    """
    _, _, cache_dir, ledger_path = _paths(root)
    cont: dict = {}
    sym_quotes: dict = {}
    details: dict = {}
    trending: dict = {}

    # 종목 추출은 두 시장 모두 한다 — 시장마다 사전과 매처가 다르다
    # (KR=KIND 한글 회사명, US=S&P500 티커·영문 회사명).
    try:
        table = load_us_table(cache_dir) if snap.market == "US" else load_table(cache_dir)
        added = append_ledger(
            collect_mentions(snap, table, market=snap.market), ledger_path
        )
        cont = continuity(
            load_ledger(ledger_path), snap.session_date, market=snap.market
        )
        print(f"언급 {added}건 추가 · 오늘 등장 종목 {len(cont)}개")
    except Exception as e:  # 종목 추출 실패가 리포트를 막지 않는다
        print(f"종목 추출 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

    # 뉴스로 잡힌 종목 중 거래 랭킹에도 있는지 표시한다 — Phase 3 채점의
    # 입력("뉴스만" vs "둘 다" 중 뭐가 맞았나)이 여기서 쌓이기 시작한다.
    ranking = snap.results.get("toss_rankings")

    # 랭킹에 회사명을 붙인다 — 토스는 심볼만 내려주므로 그대로 두면 표가 종목코드로만
    # 보인다(2026-08-13 사용자 지적). 스냅샷은 이미 저장된 뒤라(save_snapshot →
    # _emit 순서) 원본은 토스 응답 그대로 남고, 이름은 렌더할 때마다 캐시된
    # 상장법인목록에서 다시 붙는다. 사전에 없는 심볼(ETF·리츠 등)은 건드리지 않아
    # 호출부가 코드를 그대로 보여준다 — 모르는 이름을 지어내지 않는다.
    if ranking is not None and ranking.ok and ranking.data:
        try:
            name_map = load_name_map(cache_dir, snap.market)
            named = unnamed = 0
            for items in ranking.data.get("boards", {}).values():
                for item in items:
                    nm = name_map.get(item.get("symbol", ""))
                    if nm:
                        item["name"] = nm
                        named += 1
                    else:
                        unnamed += 1
            print(f"랭킹 종목명 {named}건 매칭"
                  + (f" · 사전에 없어 코드 표시 {unnamed}건" if unnamed else ""))
        except Exception as e:  # 이름 붙이기 실패가 리포트를 막지 않는다
            print(f"랭킹 종목명 생략: {type(e).__name__}: {e}", file=sys.stderr)

    if cont and ranking is not None and ranking.ok and ranking.data:
        ranked_symbols = {
            item["symbol"]
            for items in ranking.data.get("boards", {}).values()
            for item in items
        }
        # 하락 중인 종목의 랭킹 편입은 매수세 근거가 아니다 — 어느 보드에서든
        # 한 번이라도 음수 등락률로 잡히면 제외한다(보드 간 스냅샷 시각이 미세하게
        # 달라도 보수적으로 판정하기 위해 합집합이 아니라 차집합을 쓴다).
        declining = {
            item["symbol"]
            for items in ranking.data.get("boards", {}).values()
            for item in items
            if item.get("change_pct") is not None and float(item["change_pct"]) < 0
        }
        cont = mark_origin(cont, ranked_symbols, ranked_symbols - declining)
        if record_ledger:
            _log_overlap(root, snap.market, snap.session_date, cont, ranked_symbols)
        # 트렌딩 점수 — 뉴스에 잡힌 종목별로 랭킹 보드 순위 + 상대 거래량을
        # 정량화한다. 랭킹 실패 시(위 if에서 이미 걸러짐) 계산하지 않는다.
        try:
            trending = trending_mod.score_all(
                cont, ranking.data.get("boards", {}), snap.market, snap.session_date, snap_root
            )
        except Exception as e:  # 트렌딩 점수 실패가 리포트를 막지 않는다
            print(f"트렌딩 점수 계산 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

    if cont:
        # 시세는 두 시장 모두. KR 은 코드→야후심볼 매핑이 필요하고,
        # US 는 티커가 곧 야후 심볼이다.
        try:
            # 시세 대상은 cont 전체 — HTML 카드 노출(rank 상위 10)과는 별개다.
            # rank()로 자르면 채점 교집합이 노출 상위 10으로 묶여버린다(§E-1).
            codes = list(cont.keys())
            route = "US 티커 직행"
            if snap.market == "US":
                quotes = fetch_symbol_quotes(codes)
                sym_quotes = dict(quotes)
            else:
                # KIND(시장구분)가 죽어도 시세를 포기하지 않는다 — quotes.py 참고.
                # 2026-08-26 실사고: KIND 403 으로 매핑이 통째로 실패해 KR 리포트가
                # 기준가를 하나도 못 받았고, 그러면 전방 수익률·리더보드 채점이 멈춘다.
                from quant.report.collect.quotes import fetch_kr_quotes

                # 이 모듈의 두 함수를 그대로 넘긴다 — 조회 로직만 옮기고 seam 은
                # 여기 남겨 둔다(quotes.fetch_kr_quotes docstring 참고).
                sym_quotes, route = fetch_kr_quotes(
                    codes, cache_dir,
                    map_loader=load_market_map, quote_fetcher=fetch_symbol_quotes,
                )
            # **미확보 건수는 조사 대상 전체 기준으로 센다**(상위 10 이 아니라 —
            # 2026-08-15 회귀). 예전엔 "매핑실패/조회실패"로 나눠 셌지만, KIND
            # 폴백(.KS/.KQ 양쪽 조회) 도입으로 '매핑' 단계 자체가 사라져 지금
            # 정직한 숫자는 "시세를 못 받은 종목 수" 하나다. 경로를 함께 찍는다 —
            # 폴백으로 받았는지 정상 경로였는지가 조용한 강등을 드러낸다.
            missing = len(codes) - len(sym_quotes)
            if missing:
                print(f"시세 미확보 {missing}건 / 조사 {len(codes)}건 (경로: {route})",
                      file=sys.stderr)
            else:
                print(f"시세 {len(sym_quotes)}건 확보 (경로: {route})")
        except Exception as e:  # 시세 조회 실패가 리포트를 막지 않는다
            print(f"종목 시세 조회 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

        # 수급·컨센서스는 한국 종목에만 있다 (네이버 기반).
        if snap.market == "KR":
            try:
                # 외국인 수급 추종에 일별 시계열이 필요해 확대, 페이지 2개×20종목
                # = 기존 대비 +28요청/일 수준(서브프로젝트 I).
                details = fetch_many([code for code, _ in rank(cont, limit=20)], limit=20)
                print(f"종목 상세 {len(details)}건 (수급·컨센서스)")
            except Exception as e:
                print(f"종목 상세 조회 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
            if record_ledger:
                _record_flows(details, root, snap.session_date.isoformat())
                _record_frgn_flow(details, root)

    ledger_dir = root / "data" / "ledger"
    relations = _load_artifact(ledger_dir / "relations.json")
    sectors = _load_artifact(ledger_dir / "sector_map.json")

    # 결정론 베이스라인 점수(§E-2) — sym_quotes 의 ohlcv 로 검증된 채점기
    # (watch_scorer TREND 프로필)를 전 종목에 돌린다. 채점 불가 심볼은 키
    # 자체를 만들지 않는다(baseline_score 계약 — 0 으로 위장하지 않는다).
    baselines: dict[str, int] = {}
    for symbol, q in sym_quotes.items():
        ohlcv = q.get("ohlcv")
        if ohlcv is None:
            continue
        try:
            score = baseline_score(ohlcv, today=snap.session_date)
        except Exception as e:  # 한 심볼의 오염된 프레임이 리포트 전체를 죽이면 안 된다
            print(f"베이스라인 점수 계산 건너뜀({symbol}): {type(e).__name__}: {e}",
                  file=sys.stderr)
            continue
        if score is not None:
            baselines[symbol] = score

    delta = compare(snap, previous_snapshot(snap.market, snap.session_date, snap_root))
    brief = build_brief(snap, cont, delta)
    view = stance(snap, cont, delta)
    scores = score_all(cont, details)
    # 최근 거래량 몰림 감시(2026-08-25 소유자 지시: "최근 거래량이 몰렸던 종목들도
    # 계속 감시 리스트로") — 최근 5일 거래대금 보드 상위에 2회 이상 등장한 KR
    # 종목을 AUTO_WATCH 에 RANK 태그로 합류시킨다(새 태그 없음 — RANK→TREND
    # 번역 기존 경로 그대로). KR 전용: 이 축의 근거 데이터(toss_rankings 보드
    # 누적)가 KR 스냅샷에만 안정적으로 쌓인다.
    volume_watch = None
    if snap.market == "KR":
        from quant.analyze.volume_watch import recurring_volume_symbols

        try:
            volume_watch = recurring_volume_symbols(snap_root, "KR", snap.session_date)
        except Exception as e:  # noqa: BLE001 — 감시 메모리 실패가 리포트를 막지 않는다
            print(f"거래량 감시 메모리 생략: {type(e).__name__}: {e}", file=sys.stderr)
    # ── 종목 점수 일일 원장 (2026-08-26 소유자: "조사한 것을 버리지 않는다") ──
    # 그날 조사된 전 종목(cont)의 수치를 기록하고, 최근 이틀+ 강세를 이어온
    # 종목(hot streak)을 오늘 후보 유니버스에 합류시킨다. 기록 실패는 리포트를
    # 막지 않는다.
    streak_watch: list[str] = []
    try:
        from quant.control.symbol_log import (
            append_scores, build_score_rows, hot_streak_symbols, load_scores,
        )

        log_path = root / "data" / "ledger" / "symbol_scores.jsonl"
        prior = load_scores(log_path, days=5, today=snap.session_date)
        streak_watch = hot_streak_symbols(prior, snap.session_date, market=snap.market)
        if record_ledger:
            added = append_scores(
                build_score_rows(snap.session_date, snap.market, cont, scores,
                                 sym_quotes=sym_quotes), log_path)
            print(f"종목 점수 원장 {added}건 추가 (producer=symbol_scores_v1)")
        if streak_watch:
            print(f"점수 연속 강세 {len(streak_watch)}종목 후보 합류: "
                  + ", ".join(streak_watch[:8]))
    except Exception as e:  # noqa: BLE001
        print(f"종목 점수 원장 생략: {type(e).__name__}: {e}", file=sys.stderr)

    # 전일 마감 종합의 KR 패턴 종목(extra_watch)·점수 연속 강세(streak_watch)도
    # 후보 유니버스에 합류한다 — "다음날 프로그램이 전날 종목들을 보고 진입각을
    # 본다"(2026-08-25) + "좋은 흐름을 이어오던 주식 참고"(2026-08-26).
    merged_watch = list(dict.fromkeys(
        (volume_watch or []) + (extra_watch or []) + streak_watch)) or None
    payload = machine_payload(
        snap, cont, delta, brief, sym_quotes, details, view, scores, trending,
        relations, sectors, baselines, volume_watch=merged_watch,
    )
    # 합류 종목(뉴스 언급 없이 감시 축으로만 들어온)도 선정 원장에 남긴다 —
    # payload["symbols"] 는 cont 에서만 만들어져 이 종목들이 채점 표본에서
    # 통째로 빠져 있었다(2026-08-26 감사). ledger.py 의 함수가 사유를 설명한다.
    if record_ledger and merged_watch:
        from quant.report.collect.ledger import _record_watch_join_selections

        _record_watch_join_selections(
            payload, root, cache_dir,
            {"volume": volume_watch or [], "wrap": extra_watch or [], "streak": streak_watch},
        )
    return cont, delta, brief, payload, sym_quotes, details, view, scores
