"""마감 포지션 리포트(서브프로젝트 R) 전용 뷰 조립 — 장중 신규 뉴스/당일
수급/랭킹.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

from quant.analyze.bullish_markers import classify_titles

# 랭킹 스냅샷(②)에서 재사용할 보드. "하락률"·"토스 사용자 거래대금"은 마감
# 포지션 판단(롱 온리)과 무관해 뺀다 — 아침 리포트의 "랭킹 스냅샷" 섹션과
# 달리 여긴 두 보드만 축약해서 보여준다(스펙 §내용 2 문구 그대로).
CLOSE_RANKING_BOARDS = ("거래대금", "상승률")


def _build_close_news_view(news_flow: list[dict]) -> list[dict]:
    """장중 신규 호재(①) — `_build_news_flow`가 이미 만든 사건 단위 뉴스
    목록(다매체 중복만 제거, 뉴스 창은 `news_since`로 이미 걸러짐)에 제목
    단위로 호재 마커/악재 거부권을 얹어 재정렬한다.

    `bullish_markers.classify_titles`는 원래 종목 단위(그 종목의 오늘치
    제목 여러 개)를 보고 호재 유형을 찾는 함수인데, 여기서는 사건(제목)
    하나짜리 리스트를 넘겨 "이 제목 자체가 호재/악재 마커를 담고 있는가"만
    독립적으로 판정한다 — 종목 태깅이 없는 시장 전체 뉴스 흐름이라
    `cont[symbol]["titles"]` 처럼 종목별로 묶을 입력이 없다.

    정렬: 호재 티어 내림차순 → 다매체(dup_count) 내림차순 → 기존 순서
    보존(안정 정렬 — `_build_news_flow`가 이미 정한 "증권 관련·최신" 우선
    순위가 동점 구간의 tie-break 로 남는다)."""
    out = []
    for item in news_flow:
        bull = classify_titles([item.get("title", "")])
        out.append({
            **item,
            "bullish_types": bull["bullish_types"],
            "bullish_tier": bull["tier"],
            "bearish": bull["bearish"],
        })
    out.sort(key=lambda it: (-it["bullish_tier"], -(it.get("dup_count") or 1)))
    return out


def _build_close_flow_view(payload: dict) -> dict:
    """당일 수급 — 시장 잠정 외국인/기관/개인(②). `machine_payload`가 이미
    계산한 시장 피처(`kospi_flow` 유래)를 그대로 재사용한다 — 종목별 당일
    잠정 수급 크롤은 신설하지 않는다(비목표, 스펙 §비목표 2번째 항). 결측은
    `None` 그대로(0으로 위장하지 않는다) — 템플릿이 "결측"으로 정직하게
    표시한다. "잠정"이라는 라벨은 템플릿이 고정 문구로 명시한다(장중 시점의
    같은 소스 값은 확정치가 아니다)."""
    features = payload.get("features") or {}
    return {
        "foreign_net": features.get("foreign_net_100m_krw"),
        "institution_net": features.get("institution_net_100m_krw"),
        "individual_net": features.get("individual_net_100m_krw"),
    }


def _build_close_ranking_view(snap, top: int = 8) -> dict[str, list[dict]]:
    """당일 랭킹 보드 — 거래대금/상승률(②). 이미 수집된 `toss_rankings`
    스냅샷에서 뽑을 뿐 새 크롤은 없다. `_derive`가 이미 종목명을 붙여
    두므로(그 함수의 "랭킹 종목명 매칭" 단계) 여기선 재사용만 한다 —
    이 함수가 `_derive` 이후에 호출돼야 이름이 채워진다(호출부 계약)."""
    ranking = snap.results.get("toss_rankings")
    if ranking is None or not ranking.ok or not ranking.data:
        return {}
    boards = ranking.data.get("boards") or {}
    out: dict[str, list[dict]] = {}
    for name in CLOSE_RANKING_BOARDS:
        items = boards.get(name) or []
        if items:
            out[name] = items[:top]
    return out


# 종가배팅 후보 상한 — 후보가 많으면 근거가 옅은 것까지 태그된다. 소유자 실전
# 감각(15:00~15:20 에 "몇 개"를 고르는 기법)과 맞춘 값.
CLOSE_BET_TOP = 5
# 당일 등락 하한 — 종가배팅은 "이미 강한 종목의 관성"을 사는 기법이다(웹 리서치:
# 풍부한 거래대금 + 주도 테마 + 고가 근처 양봉). 약한 종목에 들어가면 기관·외인
# 단타에 물리는 개미가 된다(소유자 지시 그대로).
CLOSE_BET_MIN_CHANGE_PCT = 3.0


def _build_close_bet_view(snap, root, cont: dict, top: int = CLOSE_BET_TOP) -> list[dict]:
    """종가배팅 후보(2026-08-25, 전략 4종 체제 ③) — **결정론 채점**.

    재료는 전부 이미 수집된 것: 거래대금 랭킹(toss_rankings, 하루 종일 상위권
    = 수급이 도는 종목), 당일 등락(같은 보드), 외국인 수급 추세(frgn_flow.jsonl,
    agent_interpret 와 같은 로더), 뉴스 지속성(cont — 오늘 언급 종목). 새 크롤
    없음.

    채점(각 축 가중치는 근거의 질 순서 — 수급 > 등락 > 뉴스):
      +3 외국인 추세 라벨이 매수 계열
      +2 당일 등락 >= {CLOSE_BET_MIN_CHANGE_PCT}% (하한 미달은 아예 제외)
      +1 오늘 뉴스 언급 있음
    거래대금 보드 밖 종목은 후보가 아니다(1차 필터가 곧 "수급전광판 상위").

    마감 강도·양봉 정밀 확인은 여기서 **하지 않는다** — 그건 1분봉을 보는
    전략(close_bet)의 몫이다(역할 분담: 리포트=수급·뉴스, 전략=차트·시각).
    소유자도 이 카드를 보고 실계좌에서 직접 판단한다 — 프로그램과 같은 근거.
    """
    ranking = snap.results.get("toss_rankings")
    if ranking is None or not ranking.ok or not ranking.data:
        return []
    board = (ranking.data.get("boards") or {}).get("거래대금") or []
    if not board:
        return []

    from quant.analyze.foreign_trend import classify
    from quant.control import frgn_flow as frgn_flow_ledger

    flow_path = root / "data" / "ledger" / "frgn_flow.jsonl"
    out: list[dict] = []
    for item in board:
        sym = item.get("symbol")
        change = item.get("change_pct")
        if not sym or change is None or change < CLOSE_BET_MIN_CHANGE_PCT:
            continue
        score = 2
        reasons = [f"당일 +{change:.1f}%", f"거래대금 {item.get('rank')}위"]
        try:
            series = frgn_flow_ledger.load_series(flow_path, sym, days=20)
            label = (classify(series) or {}).get("label") or ""
        except Exception:  # noqa: BLE001 — 수급 원장 문제로 후보 전체를 버리지 않는다
            label = ""
        if "매수" in label:
            score += 3
            reasons.append(f"외국인 {label}")
        if sym in cont:
            score += 1
            reasons.append("오늘 뉴스 언급")
        out.append({
            "symbol": sym, "name": item.get("name") or sym,
            "change_pct": change, "trading_amount": item.get("trading_amount"),
            "score": score, "reasons": reasons,
        })
    out.sort(key=lambda x: (-x["score"], -(x["change_pct"] or 0)))
    return out[:top]
