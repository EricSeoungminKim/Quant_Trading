"""스냅샷에서 파생물(언급/랭킹/트렌딩/시세/베이스라인/스탠스/machine_payload) 계산.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.analyze import trending_score as trending_mod
from quant.analyze.baseline import baseline_score
from quant.analyze.briefing import build as build_brief
from quant.analyze.briefing import regime_stance, stance
from quant.analyze.candidate_gate import GATE_REASON_LABEL, gate_candidates
from quant.analyze.delta import compare, previous_snapshot
from quant.analyze.entities import load_market_map, load_name_map, load_table, load_us_table
from quant.analyze.mentions import append_ledger, collect_mentions, continuity, load_ledger, mark_origin
from quant.analyze.regime_state import load_regime_for_market
from quant.analyze.render import machine_payload, rank, rejection_reasons
from quant.analyze.symbol_score import score_all
from quant.analyze.watch_scorer import _rvol as watch_scorer_rvol
from quant.collect.sources.market import fetch_symbol_quotes
from quant.collect.sources.stock_detail import fetch_many
from quant.report.collect.index_outlook import build_index_outlook
from quant.report.collect.ledger import _log_overlap, _record_flows, _record_frgn_flow
from quant.report.paths import _load_artifact, _paths

# 외국인 수급 상세 조회 상한(2026-09-07 Phase 2 §5) — `_derive` 아래 호출부
# 주석 참고. 20(옛 값)에서 3배 확대.
FOREIGN_FLOW_FETCH_CAP = 60


def _fill_ranking_names_via_resolver(
    root: Path, cache_dir: Path, symbols: list[str], market: str,
) -> dict[str, str]:
    """결정론 사전에 없는 랭킹 종목명을 `quant.analyze.symbol_names`(캐시 →
    워치리스트 → LLM 배치)로 채운다. Toss 재조회는 여기서 하지 않는다(랭킹
    자체가 이미 Toss 응답이고, 이 파이프라인엔 별도 클라이언트 인스턴스가
    없다) — 남는 건 워치리스트 이름과 LLM뿐이다. `quant.report`는 4평면
    임포트 제약 밖이라 어댑터(narrator)를 직접 조립해도 된다."""
    try:
        from quant.adapters.narrate import make_narrator
        from quant.analyze.symbol_names import build_resolver
        from quant.trade.universe import DEFAULT_WATCHLIST_PATH

        resolver = build_resolver(
            cache_dir, root / "data" / "state",
            watchlist_paths=[root / DEFAULT_WATCHLIST_PATH],
            narrator=make_narrator(timeout=60),
        )
        return resolver.names_for(symbols, market=market)
    except Exception as e:  # noqa: BLE001 — 이름 채우기 실패가 랭킹 표시를 막지 않는다
        print(f"랭킹 종목명 LLM 보강 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


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
            unmatched_items = []
            for items in ranking.data.get("boards", {}).values():
                for item in items:
                    nm = name_map.get(item.get("symbol", ""))
                    if nm:
                        item["name"] = nm
                        named += 1
                    else:
                        unnamed += 1
                        unmatched_items.append(item)
            # 결정론 사전(KIND/S&P500)에 없는 잔여분(ETF·리츠·신규상장 등)은
            # 종합 리졸버(마지막 수단 LLM 배치 포함)로 한 번 더 채운다
            # (2026-09-07 오너 요청: "종목코드만 보이지 않게, 모르면 LLM으로").
            # 실패해도 랭킹 표시 자체는 코드로 계속 나간다.
            if unmatched_items:
                filled = _fill_ranking_names_via_resolver(
                    root, cache_dir, [it["symbol"] for it in unmatched_items], snap.market,
                )
                for item in unmatched_items:
                    nm = filled.get(item.get("symbol", ""))
                    if nm:
                        item["name"] = nm
                        named += 1
                        unnamed -= 1
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
                #
                # 2026-09-07 Phase 2 §5(SUMMARY.md §⑤): 뉴스 언급 440건 중
                # 외국인 수급(foreign_buy_streak)이 채워진 건 43건(9.8%)뿐 —
                # 상위 20건만 조회하면 나머지 언급 종목은 "왜 승격 안 됐나"를
                # 사후에 재구성할 근거가 없다. 상한을 20→FOREIGN_FLOW_FETCH_CAP
                # (60)으로 넓힌다 — 그날 뉴스에 걸린 종목 전부를 조회하는 게
                # 이상적이지만(SUMMARY §⑤ 수정 제안) `fetch_many`가 심볼당
                # 0.3초 슬립을 두는 순차 크롤이라(server/scripts 크론 예산
                # 고려) 상한 없이 전부를 매일 조회하면 뉴스 폭주일(§⑦, 09-04
                # US 172건 실측)에 빌드 시간이 통제 불능이 된다 — 60은 비용과
                # 커버리지 사이의 절충값이다(20→60 = 3배 확대).
                details = fetch_many(
                    [code for code, _ in rank(cont, limit=FOREIGN_FLOW_FETCH_CAP)],
                    limit=FOREIGN_FLOW_FETCH_CAP,
                )
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

    # 상대 거래량 확장(2026-09-07 Phase 2 §5, SUMMARY.md §⑤) — 옛
    # `trending_score.relative_volume`은 토스 랭킹 보드(top10)에 오늘
    # 거래대금이 잡힌 종목만 계산 가능해, 뉴스 언급 440건 중 2건(0.5%)만
    # 채워졌다(SUMMARY §종합 수치). 여기서는 이미 받아온 `sym_quotes[symbol]
    # ["ohlcv"]`(baseline_score와 같은 데이터, 위 루프)로 `watch_scorer._rvol`
    # (마지막 완결일 거래량 / 직전 14일 평균)을 재사용해 **cont 전체**(랭킹
    # 보드 편입 여부 무관)에 대해 계산한다 — render.machine_payload가 이
    # 값을 트렌딩 기반 relative_volume이 없을 때만 폴백으로 채운다(§4 오류
    # 수정용 known_signal_flags가 실제 신호를 볼 수 있게).
    #
    # ## 회고 recall 실측 (2026-09-07, 이 계측이 배선되기 전 61장 회고 카드
    # 재분석 — `results/report_review/*.md`의 "미언급 상위 등락" 440행을
    # 그날짜 그 심볼의 실제 yfinance 일봉으로 재계산)
    #
    # RVOL(마지막 완결일 거래량/직전14일평균) >= 2.0을 "놓친 급등주를 미리
    # 알렸을 신호"로 놓고 잰 결과:
    #   - recall(440건 중 RVOL 데이터 확보 435건 기준) = 130/435 = **29.9%**
    #     (Wilson 95% CI [25.8%, 34.3%]) — 소유자가 제시한 30% 문턱에 걸친다.
    #   - 시장별: KR 37.0%(98/265) vs US 18.8%(32/170) — KR에서 더 잘 잡는다.
    #   - precision(플래그된 130건 중 D+0 open→close>0 적중) = 65/130 = 50.0%
    #     (CI [41.5%, 58.5%])인데, **플래그 안 된 305건의 적중률도 47.9%,
    #     전체 440건 기준선도 48.5%(CI [43.8%, 53.2%])** — 세 구간이 전부
    #     겹친다. 즉 recall은 문턱에 걸치지만 **precision은 통계적으로
    #     구분되지 않는다**(현재 AUTO_WATCH 승격 리스트의 기존 적중률
    #     48.8%, SUMMARY §종합 수치와도 사실상 동률).
    #   - **결론(소유자 지시의 승격 조건 미충족)**: recall≥30%·precision이
    #     기존 승격 리스트보다 나음 — 이 회고 재분석은 두 조건 중 뒤쪽이
    #     성립하지 않는다(표본 130건짜리 CI가 기존 리스트 값을 가볍게
    #     포함한다). 그래서 **가중치를 매기는 승격 입력으로 넣지 않고
    #     계측(instrumentation)으로만 남긴다** — 위 `rvol_by_symbol`은
    #     `selections.jsonl`에 쌓이기만 하고 `symbol_score.score_symbol`의
    #     factors에는 아직 들어가지 않는다. 표본이 (이 계측 배선 이후) 실시간
    #     으로 더 쌓이면 재평가한다.
    #   - **외국인 수급(foreign net-buy) recall은 이 재분석에서 계산하지
    #     않았다** — KRX 투자자 수급은 과거 임의 날짜·임의 종목에 대해
    #     소급 조회할 수 있는 로컬 원장이 없다(`frgn_flow.jsonl`은 이
    #     리포트가 그날 이미 추적하던 종목만, 그날부터 쌓는다 — "놓친"
    #     종목은 정의상 그 원장에 없었다). 지어내지 않는다 — 이 축은
    #     "판단 불가"로 남긴다.
    rvol_by_symbol: dict[str, float] = {}
    for symbol, q in sym_quotes.items():
        ohlcv = q.get("ohlcv")
        if ohlcv is None or len(ohlcv) < 15:
            continue
        try:
            rvol_by_symbol[symbol] = round(float(watch_scorer_rvol(ohlcv)), 2)
        except Exception as e:  # noqa: BLE001 — 한 심볼 실패가 전체를 죽이면 안 된다
            print(f"상대거래량(OHLCV) 계산 건너뜀({symbol}): {type(e).__name__}: {e}",
                  file=sys.stderr)

    delta = compare(snap, previous_snapshot(snap.market, snap.session_date, snap_root))
    brief = build_brief(snap, cont, delta)
    view = stance(snap, cont, delta)
    # 국면(regime) 기반 1차 스탠스(2026-09-06, Phase 2 §1 — SUMMARY.md §①
    # "방향콜이 직전 세션의 이미 실현된 등락을 그대로 연장" 오류 수정).
    # `view`(위 stance() 결과)는 이제 진단(참고)으로 격하되고, `view["regime"]`
    # 가 리포트의 1차 스탠스가 된다 — `report.html.j2`가 이 키를 우선 그린다.
    # `view`는 그대로 `payload["stance"]`가 되므로(아래 machine_payload 호출)
    # 같은 dict에 키를 더하는 것만으로 하위호환 소비자(report_accuracy.
    # extract_open_claims 의 label/score100 읽기 등)를 건드리지 않는다.
    view["regime"] = regime_stance(load_regime_for_market(root, snap.market))
    scores = score_all(cont, details, trending)
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
            append_scores,
            build_score_rows,
            hot_streak_symbols,
            load_scores,
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
        extra_relative_volume=rvol_by_symbol,
    )
    # 승격 거부 사유(2026-09-07 Phase 2 §5, SUMMARY.md §⑤) — 후보 게이트
    # (아래)가 목록을 더 줄이기 **전** 원래 `is_candidate()` 판정 기준으로
    # 계산한다. 방어 국면 게이트로 관망(watch_status)이 된 종목은 애초에
    # 후보였던 것이므로 여기서 "거부"로 잘못 표시하면 안 된다 — 두 사유는
    # 서로 다른 단계다.
    from quant.report.collect.intraday import _candidate_symbols as _raw_candidate_symbols

    reject_by_symbol, _reject_counts = rejection_reasons(
        cont, _raw_candidate_symbols(payload), sym_quotes,
    )
    for row in payload["symbols"]:
        reason = reject_by_symbol.get(row.get("symbol"))
        if reason:
            row["rejection_reason"] = reason
    # 후보 게이트(2026-09-06 Phase 2 §2, SUMMARY.md §② "후보 리스트 크기가
    # 스탠스 방향·강도와 무관하게 나간다" 수정) — 방어 국면(또는 진단 스탠스
    # 강한 하락)일 때 승격 목록(AUTO_WATCH, own_brief.sh가 그대로 읽어
    # 확신도 엔진에 태우는 값)을 top_n으로 줄인다. 원장(payload["symbols"])
    # 에서는 아무것도 지우지 않는다 — 잘린 심볼엔 표시용 watch_status만
    # 얹는다(candidate_gate.py 모듈독스트링). `_record_report_claims`(호출부,
    # report_cli._run_open)가 이 이후의 payload["auto_watch"]를 읽으므로
    # 청구 원장에도 게이트가 적용된 후보만 남는다 — "log the cap in claims".
    gate = gate_candidates(
        payload["auto_watch"], payload["symbols"],
        regime_label=(view.get("regime") or {}).get("label"),
        diagnostic_score100=view.get("score100"),
    )
    payload["candidate_gate"] = gate
    # `view`(=payload["stance"], 같은 dict 참조)에도 얹는다 — report.html.j2가
    # 스탠스 섹션 안에서 view.candidate_gate로 바로 읽는다(신규 템플릿 인자
    # 스레딩 없이, view.regime과 같은 관례).
    view["candidate_gate"] = gate
    if gate["capped"]:
        payload["auto_watch"] = gate["auto_watch"]
        dropped = set(gate["watch_only"])
        for row in payload["symbols"]:
            if row.get("symbol") in dropped:
                row["watch_status"] = GATE_REASON_LABEL
        print(f"후보 게이트 발동: {gate['gate_reason']}")
    # 지수별 전망(코스피/코스닥, US=S&P500/나스닥) — 소유자 요청(2026-08-29).
    # 기존 stance(시장당 지수 1개)와 완전히 별개인 새 payload 키만 얹는다.
    # 실패해도 리포트 발행을 막지 않는다(이 파이프라인의 기존 관례와 동일).
    try:
        payload["index_outlook"] = build_index_outlook(snap, root)
    except Exception as e:  # noqa: BLE001
        print(f"지수별 전망 생략: {type(e).__name__}: {e}", file=sys.stderr)
    # 리포트 정확도(2026-09-06, 소유자 지시 priority-1 §2) — 방향콜 라벨 옆
    # 참고용 한 줄("정확도 미측정" 포함) + '리포트 정확도' 박스가 이 값을
    # 그대로 쓴다. `report_accuracy.jsonl`이 아직 없거나(최초 실행) 깨지면
    # report_summary(None)이 "정확도 미측정"으로 채운 dict를 낸다 —
    # index_outlook과 같은 관례로 실패해도 리포트 발행을 막지 않는다.
    try:
        from quant.control import report_accuracy as _report_accuracy

        acc_path = root / "data" / "ledger" / "report_accuracy.jsonl"
        latest = None
        if acc_path.exists():
            acc_lines = acc_path.read_text(encoding="utf-8").splitlines()
            if acc_lines:
                import json as _json

                latest = _json.loads(acc_lines[-1])
        payload["report_accuracy"] = _report_accuracy.report_summary(latest)
    except Exception as e:  # noqa: BLE001
        print(f"리포트 정확도 요약 생략: {type(e).__name__}: {e}", file=sys.stderr)
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
