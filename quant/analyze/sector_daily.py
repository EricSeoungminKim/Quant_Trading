"""일일 "주도 섹터" 판정 — 거래대금(turnover) 순위 + 외국인 수급으로 프로그램이
스스로 오늘 자금이 몰린 업종을 뽑는다(소유자 철학 지시 B, 2026-09-03: "프로그램이
스스로 고르는 주도 섹터"). 랭킹 기준은 등락률이 아니라 **거래대금**이라는 게
소유자의 정의다 — `quant.analyze.sector_view`(등락률 기준 "업종별 시세")와는
다른 판단축이다.

순수 함수다 — `sector_view.py`와 같은 원칙(네트워크·파일 I/O 금지). 입력은 전부
호출부(`quant.report.collect.sector`)가 읽어온 것을 받는다:
  - `sector_members`: `data/ledger/sector_members.json` (`{업종명:
    [{"code","name",...}]}`, `quant.apps.deepdive`가 채운다)
  - `turnover_by_symbol`: 그날 관측된 거래대금(KRW). `data/ledger/
    fundamentals_naver.jsonl`(naver_quant, 거래대금 상위 100 종목만 커버 —
    그 밖 종목은 이 사전에 없다, 0으로 위장하지 않고 그냥 빠진다)
  - `foreign_net_by_symbol`: 그날 관측된 외국인 순매수(주식 수). `data/ledger/
    frgn_flow.jsonl`(그날 fetch_many 상위 20종목만 채워진다, 같은 이유로 결측
    종목은 빠진다)

"오늘 데이터가 없다"(멤버십·거래대금 원장 둘 다 없음)는 빈 리스트가 아니라
호출부가 "결측 — 섹터 데이터 없음"으로 렌더링해야 한다(§C) — 이 모듈은 빈
리스트를 반환해 그 판단을 호출부에 위임한다.
"""
from __future__ import annotations

from quant.analyze.kr_sectors import GICS_TO_KR_LABEL, gics_for_upjong

TOP_N_MEMBERS = 5
NEG_STREAK_DAYS = 3

# 전일 US 섹터 링크 신호가 composite_score 에 미치는 가중치 (2026-09-06).
#
# 근거: `quant-backtest` 저장소 `results/sector_link/SUMMARY.md` S1 — 전일 US
# 섹터 ETF 종가→종가 수익률의 Rank IC(스피어만, 일별 횡단면) mean=0.070,
# t=4.31, n=488거래일, 2024/2025/2026 3개 연도 전부 양수(문헌이 말하는
# "미국장이 오르면 한국장도 따라간다"가 실제로 확인된다). 그런데 S3(그 신호를
# 매수/미러링 실행 규칙으로 바꾼 walk-forward 백테스트, 비용 포함)는 다섯
# 변형 전부 순비용 음수·DSR 0.5 미달로 NO_GO — KR 개별주 왕복비용(28bp)이
# 엣지를 삼킨다.
#
# 그래서 weight=0(비활성)으로 시작한다: composite_score 는 아래 식에서
# weight=0 이면 us_link_ret 유무·값과 무관하게 항상 기존 거래대금 순위 기준
# 점수(base)와 완전히 같다 — 이 랭킹 로직을 쓰는 기존 소비자(watch_scorer.
# sector_daily_adjustment 등)의 동작이 이 필드 도입으로 하나도 안 바뀐다.
# us_link_ret/composite_score 는 그래도 매일 sector_daily.jsonl 에 함께
# 적재된다 — report_accuracy 가 표본이 쌓인 뒤 "거래대금 순위만" vs
# "거래대금 순위+US신호 결합"의 실측 예측력을 비교해(S4 방법론) 사람이 weight
# 를 0이 아닌 값으로 올릴지 판단하는 근거가 된다. 지금은 참고 신호일 뿐 실행
# 신호가 아니다(quant.analyze.kr_sectors.us_sector_signal docstring 참고).
US_LINK_WEIGHT = 0.0


def build_sector_daily_rows(
    date_str: str,
    market: str,
    sector_members: dict[str, list[dict]],
    turnover_by_symbol: dict[str, int],
    foreign_net_by_symbol: dict[str, int],
) -> list[dict]:
    """그날 업종별 거래대금 합계 + 외국인 순매수 합계 rows(거래대금 내림차순).

    거래대금이 하나도 안 잡히는(원장에 없는) 업종은 결과에서 빠진다 — 0으로
    채우지 않는다. 외국인 순매수도 마찬가지로, 멤버 중 원장에 있는 것만
    합산하고 하나도 없으면 `foreign_net`을 `None`으로 남긴다(§C, 결측을
    0으로 위장하지 않는다). `top_members`는 개별 거래대금 내림차순 상위
    `TOP_N_MEMBERS`개(코드/이름만 — 표시는 호출부 몫)."""
    rows: list[dict] = []
    for sector, members in (sector_members or {}).items():
        if not members:
            continue
        member_turnovers: list[tuple[str, str, int]] = []
        turnover_sum = 0
        foreign_sum = 0
        has_any_turnover = False
        has_any_foreign = False
        for m in members:
            code = m.get("code")
            if not code:
                continue
            t = turnover_by_symbol.get(code)
            if t is not None:
                turnover_sum += t
                has_any_turnover = True
                member_turnovers.append((code, m.get("name", code), t))
            f = foreign_net_by_symbol.get(code)
            if f is not None:
                foreign_sum += f
                has_any_foreign = True
        if not has_any_turnover:
            continue  # 이 업종은 오늘 거래대금 관측이 하나도 없다 — 순위에서 제외
        member_turnovers.sort(key=lambda x: -x[2])
        rows.append({
            "date": date_str,
            "market": market,
            "sector": sector,
            "turnover_krw": turnover_sum,
            "foreign_net": foreign_sum if has_any_foreign else None,
            "n_members": len(members),
            "top_members": [
                {"code": c, "name": n} for c, n, _t in member_turnovers[:TOP_N_MEMBERS]
            ],
        })
    rows.sort(key=lambda r: -r["turnover_krw"])
    return rows


def composite_score(
    rank: int, n_sectors: int, us_link_ret: float | None, weight: float = US_LINK_WEIGHT,
) -> float:
    """거래대금 순위 기반 base 점수 + `weight * us_link_ret`.

    base = (n_sectors - rank + 1) / n_sectors — 거래대금 1위가 1.0, 꼴찌로
    갈수록 0에 가까워진다(기존 `rank` 정렬과 완전히 같은 순서). `weight=0`
    (모듈 기본값 `US_LINK_WEIGHT`)이면 `us_link_ret`이 무엇이든(None 포함)
    결과는 base 와 정확히 같다 — 재현성 보장(§US_LINK_WEIGHT 주석 참고)."""
    base = (n_sectors - rank + 1) / n_sectors if n_sectors else 0.0
    return base + weight * (us_link_ret or 0.0)


def rank_with_trend(
    today_rows: list[dict], history_rows: list[dict],
    us_sector_returns: dict[str, float] | None = None,
) -> list[dict]:
    """오늘 rows에 거래대금 순위 + 직전 거래일 대비 순위 추이(↑/↓/=/신규)와
    외국인 순매수 연속 음수일수를 얹는다.

    `history_rows`는 오늘 이전 날짜들의 sector_daily 원장 행(순서 무관) —
    가장 최근 날짜와 순위를 비교해 추이를 정하고, 최근 날짜부터 거슬러 올라가며
    연속 음수일수를 센다. 직전 순위가 없으면(신규 업종 등장 등) trend는 "신규".
    반환값은 원본 `today_rows`를 그대로 in-place로 확장한다(호출부 편의 —
    build_sector_daily_rows가 만든 새 리스트를 넘긴다는 전제).

    `us_sector_returns`(선택, 2026-09-06) — `quant.analyze.kr_sectors.
    us_sector_signal()` 결과(GICS-11 영문 키 → 전일 US 섹터 ETF 종가→종가
    수익률, fraction). 각 행의 Naver 업종을 `gics_for_upjong()`으로 GICS에
    연결해 `us_link_ret`(연결된 US 섹터 수익률, 매핑/데이터 없으면 None)과
    `us_link_gics_kr`(표시용 한글 GICS 라벨, 마찬가지로 없으면 None) — 두
    필드, 그리고 `composite_score`(§`composite_score` 참고)를 얹는다. 생략하면
    (기존 호출부 하위호환) 전부 US 신호 없이 처리한 것과 같다."""
    ranked = sorted(today_rows, key=lambda r: -r["turnover_krw"])
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i

    # 전일 US 섹터 링크 신호(§US_LINK_WEIGHT) — 이 업종에 연결된 GICS 섹터의
    # 전일 수익률을 붙이고, 그걸 얹은 composite_score 를 계산한다. weight=0
    # 이면 composite_score 는 rank 만의 함수와 같다(재현성).
    n_sectors = len(ranked)
    for r in ranked:
        gics = gics_for_upjong(r["sector"])
        us_link_ret = (us_sector_returns or {}).get(gics) if gics else None
        r["us_link_ret"] = us_link_ret
        r["us_link_gics_kr"] = GICS_TO_KR_LABEL.get(gics) if us_link_ret is not None else None
        r["composite_score"] = composite_score(r["rank"], n_sectors, us_link_ret)

    by_date: dict[str, list[dict]] = {}
    for r in history_rows:
        by_date.setdefault(r["date"], []).append(r)
    dates_sorted = sorted(by_date)

    prev_rank_by_sector: dict[str, int] = {}
    if dates_sorted:
        last_date = dates_sorted[-1]
        prev_rows = sorted(by_date[last_date], key=lambda r: -r["turnover_krw"])
        for i, r in enumerate(prev_rows, start=1):
            prev_rank_by_sector[r["sector"]] = i

    for r in ranked:
        prev = prev_rank_by_sector.get(r["sector"])
        if prev is None:
            r["trend"] = "신규"
        elif r["rank"] < prev:
            r["trend"] = "↑"
        elif r["rank"] > prev:
            r["trend"] = "↓"
        else:
            r["trend"] = "="

    # 외국인 순매수 연속 음수일수 — 오늘(포함) 최근일부터 거슬러 올라가며
    # 끊기지 않고 이어지는 음수 개수를 센다. 결측(그 업종의 그날 데이터 없음)은
    # 음수가 아니므로 스트릭을 끊는다 — 모르는 걸 이탈로 단정하지 않는다.
    for r in ranked:
        sector = r["sector"]
        series_desc = [
            row for d in reversed(dates_sorted)
            for row in by_date[d] if row["sector"] == sector
        ]
        streak = 0
        for row in [r] + series_desc:
            fn = row.get("foreign_net")
            if fn is not None and fn < 0:
                streak += 1
            else:
                break
        r["foreign_net_negative_streak"] = streak

    return ranked


def scoring_context(ranked_rows: list[dict]) -> dict:
    """`watch_scorer.sector_daily_adjustment`가 바로 쓸 수 있는 형태로 축약:
    `top3_positive`(업종명 집합, 오늘 거래대금 상위3 이면서 외국인 순매수
    합계가 양수) / `negative_streak3`(업종명 집합, 외국인 순매수가 3거래일
    연속 음수). `ranked_rows`는 `rank_with_trend()` 결과여야 한다(`rank`/
    `foreign_net_negative_streak` 필드가 필요)."""
    top3_positive = {
        r["sector"] for r in ranked_rows
        if r.get("rank", 0) <= 3 and (r.get("foreign_net") or 0) > 0
    }
    negative_streak3 = {
        r["sector"] for r in ranked_rows
        if r.get("foreign_net_negative_streak", 0) >= NEG_STREAK_DAYS
    }
    return {"top3_positive": top3_positive, "negative_streak3": negative_streak3}
