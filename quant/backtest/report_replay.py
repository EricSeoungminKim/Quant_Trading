"""과거 1개월 리포트 재구성 백테스트(뉴스 축 부재) — 서브프로젝트 N.

**질문**: 자체 리포트가 존재하기 전(2026-08-13 이전)에도 "만약 그날 리포트를
만들었다면" 무엇을 골랐을까, 그리고 그 선정과 시장 스탠스가 실제 결과와
맞았을까? `quant.backtest.report_follow`는 리포트가 **실제로 존재한** 구간만
검증한다(08-13~) — 이 모듈은 리포트가 없던 과거를 **재구성**해 표본을
늘린다. 채점(재구성)과 검증(백테스트)을 분리하는 이유는 `catalyst_study.py`·
`intraday_verify.py`와 같다 — "여기가 거짓말하면 그 위에 쌓은 모든 판단이
조용히 틀린다."

실행: `uv run python -m quant.backtest.report_replay --days 30 --top 8 --max-symbols 250`

## 뉴스 축은 정직하게 없다

`mentions.jsonl`은 2026-08-15 단 하루만 있다(자체 크롤이 이제 막 시작됐다) —
과거 재구성에 뉴스 모멘텀·다매체 축을 채울 방법이 없다. `intraday_score.py`의
실제 100점 배점(뉴스25+다매체15+외국인30+트렌딩15+촉매15)은 건드리지 않고,
이 모듈 전용의 `REPLAY_WEIGHTS`(외국인/트렌딩/촉매 3축만, 합 100)를 따로
정의한다 — 뉴스·다매체 축(합 40점)을 빼고 남은 60점을 100으로 재정규화했다
(30:15:15 → 50:25:25, 원래 비율 2:1:1을 그대로 유지). **이건 실거래 스코어러가
아니다** — 재구성 전용, `intraday_score.rank_intraday`를 대체하지 않는다.

## Step 1 — 수급 이력 백필은 실데이터다

`quant.collect.sources.stock_detail.fetch_stock_detail`이 오늘 서빙하는 페이지가
종목당 최대 20일치 **실제 과거 거래일** 외국인/기관 순매수를 준다(합성이
아니다). 이걸 `quant.control.frgn_flow.append_daily`로 원장에 쌓으면 프로덕션
원장(`data/ledger/frgn_flow.jsonl`)도 같이 채워진다 — 이 백테스트의 부산물이
아니라 의도된 효과다.

## Step 2 — look-ahead 경계 (정직하게 문서화)

- **외국인 라벨**: D **미만**(strictly before) 행만 써서 `foreign_trend.classify`를
  호출한다(`intraday_verify.py`와 동일 규율). 시계열이 아예 없는 종목은 라벨이
  `None`이 되고, 그 종목은 그날 채점 대상에서 완전히 빠진다("수급 시계열 있는
  종목만" — 커버리지를 점수로 위장하지 않는다).
- **트렌딩 근사**: `watch_scorer`의 실시간 랭킹 API가 없으므로, 그 종목 자신의
  D-1 이전 봉만으로 5일 수익률과 RVOL(D-1 거래량 / D-2~D-21 20일 평균거래량,
  D-1 자기 자신은 baseline에서 뺀다 — `news_momentum.py`의 self-influence 회피와
  같은 사고)을 근사한다. 21거래일 미만이면 `None`(계산 불가를 0으로 위장하지
  않는다).
- **촉매**: `catalyst_study.precursor_types`를 그대로 재사용한다 — D 또는 D-1에
  접수된(`rcept_dt`) 공시 유형. D 당일 공시가 장중에 올라왔을 수 있어(같은
  모듈의 문서화된 한계) 오염 가능성이 있지만, 그 한계를 그대로 물려받는다.

## Step 3 — 시장 스탠스 정합

"재구성 스탠스"는 KODEX200(069500) D-1 이전 5일 추세(%)와 유니버스 전체의
D-1 외국인 순매수 합(원)을 **각각 부호로 정규화한 뒤 더해서** 판정한다 —
두 값은 스케일이 완전히 다르므로(수익률 % vs 순매수 합계 원화) 원 수치를
그대로 더하면 스케일이 큰 쪽이 항상 이긴다. 부호(+1/0/-1)로 정규화한 뒤
합산하는 게 유일하게 정직한 방법이라고 판단했다(사용자 요청 원문의
"sign(A+B)"를 스케일 불일치 없이 구현한 해석). "실제 방향"은 D 종가 - D-1
종가의 부호다.

## Step 3 addendum — "맞았나"가 아니라 "움직였나"

평균 수익률만으로는 단타 선정의 진짜 질문에 답하지 못한다 — 살짝 플러스인
횡보 종목은 승률에는 기여해도 단타로는 무의미하다. 그래서 시가→종가 bp
표와 별개로 **고가 기준 movers 포착 능력**을 측정한다: 당일 진폭
((high-low)/open, 픽 vs 그날 채점된 유니버스 전체의 base rate), 상승력
도달률(고가가 시가 대비 +3%/+5% 이상 도달한 비율, 픽 vs 유니버스), 그리고
**movers 포착률**(그날 유니버스 안의 실제 movers 중 top-K가 몇 %를
잡았는지 = recall, top-K 중 movers 비율 = precision, movers 판정 기준은
고가가 시가 대비 `MOVER_THRESHOLD_PCT`=5% 이상). 네 지표 모두 픽과
"그날 채점 대상이 됐던 유니버스 전체"를 같은 정의로 나란히 계산한 뒤
전체 기간을 풀링한다(일별 평균의 평균이 아니라 종목-일 전체를 직접
풀링 — `intraday_verify.aggregate_metrics`와 같은 사고) — 픽이 base rate보다
유의미하게 낫지 않으면 재구성 스코어러가 movers를 우연 이상으로 골라내지
못한다는 뜻이고, 이 결과는 그대로 보고한다(좋게 포장하지 않는다).

## 표본 부족을 성과로 위장하지 않는다

`intraday_verify.py`·`report_follow.py`와 같은 문턱(`MIN_SAMPLE_FOR_JUDGEMENT=30`).
종료 코드는 항상 0 — 이 배치가 실패해도 다른 파이프라인을 막지 않는다.

## 서브프로젝트 O — 외국인 축 v2(실무 규칙) A/B

위 3절이 보여준 "movers 포착이 base rate와 같다"는 결과는 외국인 축이
`foreign_trend.classify()`의 **재유입/이탈 라벨 5종**만 쓴다는 점과 무관하지
않을 수 있다 — KR 시장 실무자들이 실제로 보는 신호(쌍끌이 동반매수, 연속
순매수일수, 1·5·20일 추세 정합, 거래량 대비 강도)는 라벨 하나로 뭉개진다.
`foreign_score_v2()`가 그 신호들을 웹 리서치 기반으로 별도 점수화하고,
`--variant {a,b,ab}`가 같은 창·같은 유니버스 위에서 기존 라벨 기반(A)과
v2(B)를 나란히 채점해 movers 포착력이 실제로 나아지는지 실측한다
(`run_ab_reconstruction`, `format_ab_comparison`). 근거·검증 방법은
`foreign_score_v2` docstring과
[`docs/plans/리포트-재구성-백테스트-2026-08-17.md`](../../docs/plans/리포트-재구성-백테스트-2026-08-17.md)
A/B 절 참고.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from quant.analyze.foreign_flow_v2 import (
    FOREIGN_V2_MAX,
    FOREIGN_V2_STRENGTH_HIGH_PCT,
    FOREIGN_V2_STRENGTH_MED_PCT,
    foreign_intensity_ratio,
    foreign_score_v2,
)
from quant.analyze.foreign_trend import (
    LABEL_INFLOW,
    LABEL_NEUTRAL,
    LABEL_OUTFLOW_TREND,
    LABEL_WATCH_ANOTHER_DAY,
    LABEL_WATCH_RESIDUAL,
    classify,
)
from quant.analyze.intraday_score import (
    CATALYST_DART_TYPES,
    FOREIGN_LABEL_POINTS,
    TRENDING_HIGH_THRESHOLD,
    TRENDING_MED_THRESHOLD,
)
from quant.backtest.catalyst_study import (
    index_disclosures_by_symbol,
    precursor_types,
    select_symbols_by_frequency,
)
from quant.backtest.intraday_verify import index_frgn_flow
from quant.collect.sources.stock_detail import fetch_stock_detail
from quant.control.frgn_flow import append_daily

DEFAULT_DAYS = 30
DEFAULT_TOP = 8
DEFAULT_MAX_SYMBOLS = 250
DEFAULT_FEE_BP = 20.0
FLOW_SLEEP_SECONDS = 0.35
BAR_SLEEP_SECONDS = 0.3
FLOW_HISTORY_WINDOW_DAYS = 20   # foreign_trend.classify 에 넘길 최근 창(intraday_verify.py 와 동일)
RVOL_BASELINE_DAYS = 20         # RVOL 분모 — D-1 자기 자신 제외 20일
LOOKBACK_BUFFER_DAYS = 60       # 봉 조회 창 앞 여유(휴장일 + RVOL/5일수익률 baseline 감안)
MIN_SAMPLE_FOR_JUDGEMENT = 30   # intraday_verify.py/report_follow.py 와 동일 기준
ANCHOR_SYMBOL = "069500"        # KODEX200 — 다른 백테스트 모듈과 동일한 시장 대리 지표
MOVER_THRESHOLD_PCT = 5.0       # "실제 movers" 판정 — 고가가 시가 대비 +5% 이상
REACH_THRESHOLDS_PCT = (3.0, 5.0)  # 상승력 도달률 판정 임계값(모듈 docstring "Step 3 addendum")

FRGN_FLOW_LEDGER = "frgn_flow.jsonl"
DISCLOSURES_LEDGER = "disclosures.jsonl"
SECTOR_MEMBERS_FILE = "sector_members.json"  # 사용자 스펙이 가정한 파일명 — 이 저장소엔 없다(아래 select_universe 참고)
REPLAY_LEDGER = "report_replay.jsonl"

# 뉴스(25)+다매체(15)=40점을 뺀 나머지 외국인(30)/트렌딩(15)/촉매(15)=60점을
# 100으로 재정규화(×5/3) — 원래 비율 2:1:1 유지. 모듈 docstring 참고.
REPLAY_WEIGHTS: dict[str, float] = {"foreign": 50.0, "trending": 25.0, "catalyst": 25.0}
assert abs(sum(REPLAY_WEIGHTS.values()) - 100.0) < 1e-9  # noqa: S101 — 배점 설계가 어긋나면 임포트 시점에 바로 드러난다

# `intraday_score.py`의 FOREIGN_TREND_MAX/TRENDING_HIGH_POINTS/TRENDING_MED_POINTS
# 를 더 이상 임포트하지 않는다(2026-08-17, 서브프로젝트 S part 2) — 텔레그램
# 시그널 축 신설로 그 값들이 라이브에서 30→28/15→13/8→7로 바뀌었는데, 이
# 재구성 변형들은 그 개편 **이전**(뉴스·다매체 없이 외국인:트렌딩:촉매=
# 50:25:25 였던 시절)의 비율을 그대로 재현해야 한다. `FOREIGN_LABEL_POINTS`를
# 이 모듈이 계속 참조하려고 라이브 채점 경로에서 지우지 않고 남겨둔 것과
# 같은 이유로, 이 세 값도 여기 얼려서 남긴다 — 라이브 값을 계속 따라가면
# "그때 비율"이 스코어러 개편 때마다 조용히 바뀐다.
_FOREIGN_TREND_MAX_FROZEN = 30    # 위 FOREIGN_LABEL_POINTS 의 만점(재유입=30)과 짝
_TRENDING_HIGH_POINTS_FROZEN = 15
_TRENDING_MED_POINTS_FROZEN = 8

_FOREIGN_STRENGTH_ORDER = (
    LABEL_INFLOW, LABEL_WATCH_RESIDUAL, LABEL_WATCH_ANOTHER_DAY, LABEL_OUTFLOW_TREND, LABEL_NEUTRAL,
)

# ------------------------------------------------------------------ 서브프로젝트 O: 외국인 축 v2 배점(초기값)
# `foreign_score_v2()`와 그 배점 상수는 `quant/analyze/foreign_flow_v2.py`로
# 옮겼다(2026-08-17 라이브 스코어러 적용 후속) — `quant/analyze/intraday_score.py`
# 도 같은 곳에서 임포트해야 analyze→backtest 역방향 의존이 생기지 않는다
# (`tests/test_architecture.py`). 이 파일은 이제 그 상수를 재사용만 한다.

# 뉴스·다매체 없이(REPLAY_WEIGHTS와 동일한 제약) 외국인 축만 v2로 교체한
# 변형 B의 배점. `REPLAY_WEIGHTS`의 50:25:25(외국인:트렌딩:촉매) 비율에서
# 외국인 축 배점을 살짝 줄이고(50→40) 그 5점씩을 트렌딩(25)·촉매(25→20)가
# 아니라 "강도보정" 축으로 뗀 게 아니라 — 사용자가 지정한 배점
# (외국인v2 40/트렌딩 25/촉매 20/강도보정 15, 합 100)을 그대로 쓴다.
# "강도보정"은 `foreign_score_v2` 내부의 강도 가점(최대 6점, `foreign_intensity_ratio`
# 기반)과 같은 신호를 재사용하되 전체 100점 배점에서 별도 축으로 다시 반영한다
# — 의도된 이중 계산이다(강도-수익률 상관이 리서치에서 별도 규칙으로 지목됐으므로
# v2 내부 캡(6점)보다 전체 스코어에 더 큰 영향력을 주기 위함). 이 이중 계산이
# A/B 결과를 어떻게 해석할지 왜곡할 수 있다는 점을 `docs/plans/
# 리포트-재구성-백테스트-2026-08-17.md`의 A/B 절에 그대로 남긴다.
REPLAY_WEIGHTS_B: dict[str, float] = {
    "foreign_v2": 40.0, "trending": 25.0, "catalyst": 20.0, "intensity": 15.0,
}
assert abs(sum(REPLAY_WEIGHTS_B.values()) - 100.0) < 1e-9  # noqa: S101

# 변형 C — B에서 "강도보정" **독립 축만** 뺀 격리 실험(오케스트레이터 후속 지시,
# 2026-08-17). B의 이중 계산(강도가 foreign_score_v2 내부 6점 캡 + 별도 15점
# 축 양쪽에 반영됨)이 B의 우위 원인인지, 아니면 쌍끌이·연속일·정합 같은
# 실무 규칙 자체가 원인인지 분리하려고 만들었다 — v2 내부의 강도 서브점수는
# 그대로 둔 채(그건 "이중 계산"이 아니라 v2 규칙의 일부다) 최상위 강도보정
# 축만 제거하고 남은 예산을 재분배한다. 배점은 REPLAY_WEIGHTS_B의 외국인v2/
# 트렌딩/촉매 비율(40:25:20=8:5:4)을 100으로 그대로 재정규화한 값
# (40/65*100≈61.5 이 아니라, 오케스트레이터가 지정한 47/29/24 을 그대로 쓴다 —
# 8:5:4 비율의 반올림과 거의 같다: 47:29:24 ≈ 8.15:5.03:4.17, 합 100).
REPLAY_WEIGHTS_C: dict[str, float] = {
    "foreign_v2": 47.0, "trending": 29.0, "catalyst": 24.0,
}
assert abs(sum(REPLAY_WEIGHTS_C.values()) - 100.0) < 1e-9  # noqa: S101


# ------------------------------------------------------------------ 원장 로딩(순수, 네트워크 없음)

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ------------------------------------------------------------------ 유니버스 선정(순수 파싱 + 파일 존재 체크)

def select_universe(root: Path, max_symbols: int) -> tuple[list[str], dict]:
    """공시 빈도 상위 `max_symbols` ∪ 섹터 리더(있으면). `sector_members.json`은
    사용자 요청이 가정한 파일명이지만 이 저장소엔 없다(`catalyst_study.py`
    docstring이 이미 밝힌 동일한 사실 — 존재하는 동등 원장 `sector_map.json`은
    "리더"가 아니라 4천여 종목 전체의 업종 맵이라 여기 쓰면 예산을 크게
    벗어난다). 없으면 스펙이 명시한 fallback(disclosures만)을 그대로 탄다."""
    disclosures_rows = _read_jsonl(root / "data" / "ledger" / DISCLOSURES_LEDGER)
    freq: dict[str, int] = {}
    for r in disclosures_rows:
        sym = r.get("stock_code")
        if sym:
            freq[sym] = freq.get(sym, 0) + 1

    primary = select_symbols_by_frequency(freq, max_symbols)

    sector_path = root / "data" / "ledger" / SECTOR_MEMBERS_FILE
    if sector_path.exists():
        try:
            data = json.loads(sector_path.read_text(encoding="utf-8"))
            sector_symbols = list(data.keys()) if isinstance(data, dict) else list(data)
        except ValueError:
            sector_symbols = []
        universe = sorted(set(primary) | set(sector_symbols))
        method = f"disclosures 상위 {max_symbols}종목 ∪ sector_members.json({len(sector_symbols)}종목)"
    else:
        universe = primary
        method = (
            f"disclosures 상위 {max_symbols}종목 (sector_members.json 없음 — "
            "fallback: disclosures만, catalyst_study.py와 동일하게 문서화된 우회)"
        )

    return universe, {"method": method, "n_disclosure_rows": len(disclosures_rows)}


# ------------------------------------------------------------------ Step 1: 수급 이력 백필(I/O, 네트워크)

def backfill_flow_history(
    root: Path, symbols: list[str], sleep_seconds: float = FLOW_SLEEP_SECONDS, fetcher=None,
) -> dict:
    """종목별 `fetch_stock_detail`로 실제 과거 일별 외국인/기관 순매수를 받아
    `frgn_flow.jsonl`에 upsert한다. 종목 하나의 실패가 전체를 막지 않는다 —
    실패는 `failed_sample`에 그대로 남는다(0으로 위장하지 않는다)."""
    fetcher = fetcher or fetch_stock_detail
    path = root / "data" / "ledger" / FRGN_FLOW_LEDGER

    all_rows: list[dict] = []
    ok = 0
    failed: list[str] = []

    for i, sym in enumerate(symbols):
        flow_daily: list[dict] = []
        try:
            detail = fetcher(sym)
            flow_daily = detail.get("flow_daily") or []
        except Exception:  # noqa: BLE001 — 종목 하나의 실패가 전체 백필을 막지 않는다
            flow_daily = []

        if flow_daily:
            ok += 1
            for r in flow_daily:
                if r.get("date"):
                    all_rows.append({
                        "date": r["date"], "symbol": sym,
                        "foreign_net": r.get("foreign_net"), "inst_net": r.get("inst_net"),
                    })
        else:
            failed.append(sym)

        if sleep_seconds and i < len(symbols) - 1:
            time.sleep(sleep_seconds)

    written = append_daily(all_rows, path) if all_rows else 0
    return {
        "n_symbols": len(symbols), "n_ok": ok, "n_failed": len(failed),
        "failed_sample": failed[:20], "rows_written": written,
    }


# ------------------------------------------------------------------ Step 2: pre-open 입력 재구성(순수)

def prior_flow_rows(
    frgn_idx: dict[str, list[dict]], symbol: str, d: date, days: int = FLOW_HISTORY_WINDOW_DAYS,
) -> list[dict]:
    """D **미만**(strictly before) 행만, 날짜 오름차순, 최근 `days`개.
    `foreign_label_for`(변형 A)와 `foreign_score_v2`(변형 B)가 공유하는
    look-ahead 경계 — 둘 다 같은 이 함수로 후보를 걸러 같은 날 같은
    유니버스를 채점한다(공정한 A/B 전제)."""
    rows = frgn_idx.get(symbol, [])
    d_str = d.isoformat()
    prior = sorted((r for r in rows if r.get("date") and str(r["date"]) < d_str), key=lambda r: r["date"])
    return prior[-days:]


def foreign_label_for(
    frgn_idx: dict[str, list[dict]], symbol: str, d: date, days: int = FLOW_HISTORY_WINDOW_DAYS,
) -> str | None:
    """D **미만**(strictly before) 행만 써서 라벨을 낸다. 시계열이 아예 없으면
    `None`(중립으로 위장하지 않는다) — 호출자가 이 종목을 그날 채점에서 뺀다."""
    prior = prior_flow_rows(frgn_idx, symbol, d, days)
    if not prior:
        return None
    return classify(prior)["label"]


def trending_score_proxy(bars: dict[date, dict], d: date) -> tuple[float | None, dict]:
    """D-1 이전 봉만으로 `watch_scorer`의 TREND 요인을 근사한다(재구성 전용).

    ret5 = D-1 종가 / D-6(5거래일 전) 종가 - 1. RVOL = D-1 거래량 / D-2~D-21
    20일 평균거래량(D-1 자기 자신은 baseline에서 뺀다 — self-influence 회피).
    21거래일 미만이면 계산 불가 `(None, {"reason": ...})`.
    """
    days_sorted = sorted(k for k in bars if k < d)
    if len(days_sorted) < RVOL_BASELINE_DAYS + 1:
        return None, {"reason": f"D-1 이전 거래일 {len(days_sorted)}일 < {RVOL_BASELINE_DAYS + 1}일(RVOL baseline 부족)"}

    d1 = days_sorted[-1]     # D-1
    d6 = days_sorted[-6]     # D-6 (5거래일 전)
    close_d1 = bars[d1].get("close")
    close_d6 = bars[d6].get("close")
    if not close_d1 or not close_d6:
        return None, {"reason": "D-1/D-6 종가 결측"}
    ret5_pct = (close_d1 / close_d6 - 1) * 100

    baseline_days = days_sorted[-(RVOL_BASELINE_DAYS + 1):-1]  # D-2..D-21, D-1 제외
    vols = [bars[dd].get("volume") for dd in baseline_days if bars[dd].get("volume")]
    if len(vols) < RVOL_BASELINE_DAYS:
        return None, {"reason": "거래량 baseline 결측"}
    mean20 = statistics.fmean(vols)
    vol_d1 = bars[d1].get("volume")
    if not mean20 or not vol_d1:
        return None, {"reason": "D-1 거래량 결측"}
    rvol = vol_d1 / mean20

    if rvol >= 1.5 and ret5_pct > 0:
        score = 75.0
    elif rvol >= 1.0 and ret5_pct > 0:
        score = 60.0
    else:
        score = 45.0
    return score, {"ret5_pct": ret5_pct, "rvol": rvol}


def score_replay(foreign_label: str | None, trending_score100: float | None, dart_types: list[str] | None) -> dict | None:
    """`REPLAY_WEIGHTS` 3축 채점. `foreign_label`이 `None`이면(수급 시계열 없음)
    이 종목은 아예 채점하지 않는다 — 커버리지를 점수로 위장하지 않는다."""
    if foreign_label is None:
        return None

    foreign_pts = REPLAY_WEIGHTS["foreign"] * (FOREIGN_LABEL_POINTS.get(foreign_label, 0) / _FOREIGN_TREND_MAX_FROZEN)

    if trending_score100 is None:
        trending_pts = 0.0
    elif trending_score100 >= TRENDING_HIGH_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS["trending"] * (_TRENDING_HIGH_POINTS_FROZEN / _TRENDING_HIGH_POINTS_FROZEN)
    elif trending_score100 >= TRENDING_MED_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS["trending"] * (_TRENDING_MED_POINTS_FROZEN / _TRENDING_HIGH_POINTS_FROZEN)
    else:
        trending_pts = 0.0

    matched = [t for t in (dart_types or []) if t in CATALYST_DART_TYPES]
    catalyst_pts = REPLAY_WEIGHTS["catalyst"] if matched else 0.0

    score100 = foreign_pts + trending_pts + catalyst_pts
    return {
        "score100": score100,
        "foreign_label": foreign_label, "foreign_pts": foreign_pts,
        "trending_score100": trending_score100, "trending_pts": trending_pts,
        "matched_dart_types": matched, "catalyst_pts": catalyst_pts,
    }


def _foreign_strength(label: str | None) -> int:
    if label is None:
        return -1
    try:
        idx = _FOREIGN_STRENGTH_ORDER.index(label)
    except ValueError:
        return 0
    return len(_FOREIGN_STRENGTH_ORDER) - idx


def rank_replay(candidates: dict[str, dict], top: int) -> list[tuple[str, dict]]:
    """`candidates`(symbol → {"foreign_label","trending_score100","dart_types"}) 를
    채점해 상위 `top`개. `score_replay`가 `None`을 낸 종목(수급 시계열 없음)은
    제외. 정렬: score100 내림차순 → 외국인 라벨 강도 내림차순 → symbol 오름차순
    (결정론, `intraday_score.rank_intraday`와 동일한 tie-break 원칙)."""
    scored: list[tuple[str, dict, int]] = []
    for sym, inputs in candidates.items():
        result = score_replay(inputs.get("foreign_label"), inputs.get("trending_score100"), inputs.get("dart_types"))
        if result is None:
            continue
        scored.append((sym, result, _foreign_strength(inputs.get("foreign_label"))))
    scored.sort(key=lambda item: (-item[1]["score100"], -item[2], item[0]))
    return [(sym, result) for sym, result, _ in scored[:top]]


def score_replay_v2(
    foreign_v2_score: int, trending_score100: float | None,
    dart_types: list[str] | None, intensity_ratio: float | None,
) -> dict:
    """`REPLAY_WEIGHTS_B` 채점(변형 B — 외국인 축을 `foreign_score_v2`로
    교체). `score_replay`(변형 A)와 달리 `foreign_v2_score`는 항상 int다 —
    수급 시계열이 아예 없는 종목은 호출 전에 `run_reconstruction`이 이미
    후보에서 제외한다(`prior_flow_rows`가 빈 리스트일 때, 변형 A와 동일한
    게이트 — 공정한 A/B 전제)."""
    foreign_pts = REPLAY_WEIGHTS_B["foreign_v2"] * (foreign_v2_score / FOREIGN_V2_MAX)

    if trending_score100 is None:
        trending_pts = 0.0
    elif trending_score100 >= TRENDING_HIGH_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS_B["trending"]
    elif trending_score100 >= TRENDING_MED_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS_B["trending"] * (_TRENDING_MED_POINTS_FROZEN / _TRENDING_HIGH_POINTS_FROZEN)
    else:
        trending_pts = 0.0

    matched = [t for t in (dart_types or []) if t in CATALYST_DART_TYPES]
    catalyst_pts = REPLAY_WEIGHTS_B["catalyst"] if matched else 0.0

    if intensity_ratio is None:
        intensity_pts = 0.0
    elif intensity_ratio >= FOREIGN_V2_STRENGTH_HIGH_PCT / 100:
        intensity_pts = REPLAY_WEIGHTS_B["intensity"]
    elif intensity_ratio >= FOREIGN_V2_STRENGTH_MED_PCT / 100:
        intensity_pts = REPLAY_WEIGHTS_B["intensity"] * 0.5
    else:
        intensity_pts = 0.0

    score100 = foreign_pts + trending_pts + catalyst_pts + intensity_pts
    return {
        "score100": score100,
        "foreign_v2_score": foreign_v2_score, "foreign_pts": foreign_pts,
        "trending_score100": trending_score100, "trending_pts": trending_pts,
        "matched_dart_types": matched, "catalyst_pts": catalyst_pts,
        "intensity_ratio": intensity_ratio, "intensity_pts": intensity_pts,
    }


def rank_replay_v2(candidates: dict[str, dict], top: int) -> list[tuple[str, dict]]:
    """`candidates`(symbol → {"foreign_v2_score","trending_score100",
    "dart_types","intensity_ratio"})를 `score_replay_v2`로 채점해 상위
    `top`개. 정렬: score100 내림차순 → foreign_v2_score 내림차순(동점이면
    수급 신호가 더 뚜렷한 쪽 우선) → symbol 오름차순(결정론, `rank_replay`와
    동일 원칙)."""
    scored: list[tuple[str, dict, int]] = []
    for sym, inputs in candidates.items():
        result = score_replay_v2(
            inputs["foreign_v2_score"], inputs.get("trending_score100"),
            inputs.get("dart_types"), inputs.get("intensity_ratio"),
        )
        scored.append((sym, result, inputs["foreign_v2_score"]))
    scored.sort(key=lambda item: (-item[1]["score100"], -item[2], item[0]))
    return [(sym, result) for sym, result, _ in scored[:top]]


def score_replay_c(foreign_v2_score: int, trending_score100: float | None, dart_types: list[str] | None) -> dict:
    """`REPLAY_WEIGHTS_C` 채점(변형 C — B에서 최상위 강도보정 축만 뺀 격리
    실험). `foreign_v2_score`는 `score_replay_v2`와 마찬가지로 항상 int(게이트는
    호출자 — 변형 A/B와 동일한 `prior_flow_rows` 게이트)."""
    foreign_pts = REPLAY_WEIGHTS_C["foreign_v2"] * (foreign_v2_score / FOREIGN_V2_MAX)

    if trending_score100 is None:
        trending_pts = 0.0
    elif trending_score100 >= TRENDING_HIGH_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS_C["trending"]
    elif trending_score100 >= TRENDING_MED_THRESHOLD:
        trending_pts = REPLAY_WEIGHTS_C["trending"] * (_TRENDING_MED_POINTS_FROZEN / _TRENDING_HIGH_POINTS_FROZEN)
    else:
        trending_pts = 0.0

    matched = [t for t in (dart_types or []) if t in CATALYST_DART_TYPES]
    catalyst_pts = REPLAY_WEIGHTS_C["catalyst"] if matched else 0.0

    score100 = foreign_pts + trending_pts + catalyst_pts
    return {
        "score100": score100,
        "foreign_v2_score": foreign_v2_score, "foreign_pts": foreign_pts,
        "trending_score100": trending_score100, "trending_pts": trending_pts,
        "matched_dart_types": matched, "catalyst_pts": catalyst_pts,
    }


def rank_replay_c(candidates: dict[str, dict], top: int) -> list[tuple[str, dict]]:
    """`candidates`(symbol → {"foreign_v2_score","trending_score100",
    "dart_types"})를 `score_replay_c`로 채점해 상위 `top`개. 정렬 원칙은
    `rank_replay_v2`와 동일(score100 → foreign_v2_score → symbol)."""
    scored: list[tuple[str, dict, int]] = []
    for sym, inputs in candidates.items():
        result = score_replay_c(
            inputs["foreign_v2_score"], inputs.get("trending_score100"), inputs.get("dart_types"),
        )
        scored.append((sym, result, inputs["foreign_v2_score"]))
    scored.sort(key=lambda item: (-item[1]["score100"], -item[2], item[0]))
    return [(sym, result) for sym, result, _ in scored[:top]]


# ------------------------------------------------------------------ 봉(bar) 캐시(I/O, 네트워크는 candle_source가 담당)

def fetch_daily_bars_ohlcv(
    candle_source, symbol: str, start: datetime, end: datetime, sleep_seconds: float = BAR_SLEEP_SECONDS,
) -> dict[date, dict] | None:
    """`{date: {open, close, volume}}` — RVOL 계산에 거래량이 필요해
    `catalyst_study.fetch_daily_bars`(open/close만)와 별도로 둔다."""
    try:
        df = candle_source.fetch(symbol, start, end)
    except Exception:  # noqa: BLE001 — 종목 하나의 실패가 전체를 막지 않는다
        df = None
    finally:
        if sleep_seconds:
            time.sleep(sleep_seconds)

    if df is None or df.empty:
        return None

    out: dict[date, dict] = {}
    for ts, row in df.iterrows():
        o, c = row["open"], row["close"]
        if o is None or o <= 0:
            continue
        h, l = row.get("high"), row.get("low")  # noqa: E741 — 짧은 변수명이지만 지역 스코프라 명확
        out[ts.date()] = {
            "open": float(o), "close": float(c),
            "high": float(h) if h is not None else float(c),
            "low": float(l) if l is not None else float(o),
            "volume": float(row.get("volume") or 0.0),
        }
    return out if out else None


def build_bar_cache(
    candle_source, symbols: list[str], start: datetime, end: datetime, sleep_seconds: float = BAR_SLEEP_SECONDS,
) -> tuple[dict[str, dict[date, dict]], list[str]]:
    cache: dict[str, dict[date, dict]] = {}
    failed: list[str] = []
    for sym in symbols:
        bars = fetch_daily_bars_ohlcv(candle_source, sym, start, end, sleep_seconds)
        if bars is None:
            failed.append(sym)
        else:
            cache[sym] = bars
    return cache, failed


# ------------------------------------------------------------------ Step 3: 백테스트 산수(순수)

def picks_outcome(bars_for_symbol: dict[date, dict], d: date, fee_bp: float) -> dict | None:
    """D 시가→종가 bp, 수수료(왕복, bp) 차감. 봉 없으면 `None`(0 위장 금지)."""
    bar = bars_for_symbol.get(d)
    if bar is None or not bar.get("open") or bar["open"] <= 0:
        return None
    gross_bp = (bar["close"] - bar["open"]) / bar["open"] * 10000
    return {"gross_bp": gross_bp, "net_bp": gross_bp - fee_bp}


def aggregate_replay(records: list[dict]) -> dict:
    """레코드(각 {"net_bp", "rvol"?}) 를 종목-일 단위로 풀링(intraday_verify.py의
    `aggregate_metrics`와 동일 사고 — 일별 평균의 평균이 아니다)."""
    n = len(records)
    if n == 0:
        return {"n": 0, "hit_rate": None, "avg_net_bp": None, "sum_net_bp": None, "avg_rvol": None}
    net = [r["net_bp"] for r in records]
    rvols = [r["rvol"] for r in records if r.get("rvol") is not None]
    return {
        "n": n,
        "hit_rate": sum(1 for x in net if x > 0) / n,
        "avg_net_bp": statistics.fmean(net),
        "sum_net_bp": sum(net),
        "avg_rvol": statistics.fmean(rvols) if rvols else None,
    }


def amplitude_pct(bar: dict | None) -> float | None:
    """당일 진폭 (high-low)/open %. 봉 없거나 high/low 결측이면 `None`."""
    if not bar or not bar.get("open") or bar["open"] <= 0 or bar.get("high") is None or bar.get("low") is None:
        return None
    return (bar["high"] - bar["low"]) / bar["open"] * 100


def upside_reach_pct(bar: dict | None) -> float | None:
    """고가 기준 상승력 (high-open)/open % — 단타는 종가가 아니라 장중 고점
    익절이 가능하므로 이 지표를 따로 본다."""
    if not bar or not bar.get("open") or bar["open"] <= 0 or bar.get("high") is None:
        return None
    return (bar["high"] - bar["open"]) / bar["open"] * 100


def bar_mover_record(bar: dict | None, mover_threshold_pct: float = MOVER_THRESHOLD_PCT) -> dict | None:
    """봉 하나 → `{"amplitude_pct", "upside_reach_pct", "is_mover"}`. 진폭/상승력
    둘 중 하나라도 계산 불가하면 `None`(0 위장 금지 — 그 종목-일은 movers
    집계 자체에서 빠진다)."""
    amp = amplitude_pct(bar)
    reach = upside_reach_pct(bar)
    if amp is None or reach is None:
        return None
    return {"amplitude_pct": amp, "upside_reach_pct": reach, "is_mover": reach >= mover_threshold_pct}


def aggregate_mover_metrics(records: list[dict]) -> dict:
    """`records`(각 `bar_mover_record`의 반환값) 를 종목-일 단위로 풀링 —
    일별 평균의 평균이 아니라 전체를 직접 풀링한다(`intraday_verify.
    aggregate_metrics`와 동일한 사고)."""
    n = len(records)
    if n == 0:
        return {"n": 0, "amplitude_median": None, "reach3_rate": None, "reach5_rate": None, "mover_rate": None}
    amps = [r["amplitude_pct"] for r in records]
    reaches = [r["upside_reach_pct"] for r in records]
    return {
        "n": n,
        "amplitude_median": statistics.median(amps),
        "reach3_rate": sum(1 for v in reaches if v >= REACH_THRESHOLDS_PCT[0]) / n,
        "reach5_rate": sum(1 for v in reaches if v >= REACH_THRESHOLDS_PCT[1]) / n,
        "mover_rate": sum(1 for r in records if r["is_mover"]) / n,
    }


def mover_precision_recall(pick_records: list[dict], universe_records: list[dict]) -> dict:
    """picks가 그날 채점된 유니버스 전체(base rate 모집단)의 실제 movers를 얼마나
    잡아냈는지. `pick_records`는 `universe_records`의 부분집합이어야 한다(같은
    날 같은 정의로 뽑은 봉이므로 픽은 항상 유니버스 안에 있다) — recall 분모는
    유니버스 전체의 movers 수, 분자는 그중 픽에 포함된 movers 수."""
    n_picks = len(pick_records)
    n_pick_movers = sum(1 for r in pick_records if r["is_mover"])
    n_universe_movers = sum(1 for r in universe_records if r["is_mover"])
    return {
        "n_picks": n_picks, "n_pick_movers": n_pick_movers, "n_universe_movers": n_universe_movers,
        "precision": (n_pick_movers / n_picks) if n_picks else None,
        "recall": (n_pick_movers / n_universe_movers) if n_universe_movers else None,
    }


def kodex_5d_trend(anchor_bars: dict[date, dict], d: date) -> float | None:
    """D **미만** 봉만으로 계산한 5거래일 수익률(%). 6거래일 미만이면 `None`."""
    days_sorted = sorted(k for k in anchor_bars if k < d)
    if len(days_sorted) < 6:
        return None
    d1, d6 = days_sorted[-1], days_sorted[-6]
    c1, c6 = anchor_bars[d1].get("close"), anchor_bars[d6].get("close")
    if not c1 or not c6:
        return None
    return (c1 / c6 - 1) * 100


def market_foreign_proxy(frgn_idx: dict[str, list[dict]], universe: list[str], prior_day: date | None) -> float | None:
    """유니버스 전체의 `prior_day` 외국인 순매수 합. 그 날짜 행이 없는 종목은
    0으로 채우지 않고 그냥 빼며(부분합), 아무도 없으면 `None`."""
    if prior_day is None:
        return None
    d_str = prior_day.isoformat()
    vals = [
        r["foreign_net"] for sym in universe for r in frgn_idx.get(sym, [])
        if r.get("date") == d_str and r.get("foreign_net") is not None
    ]
    if not vals:
        return None
    return sum(vals)


def _sign(v: float | None) -> int | None:
    if v is None:
        return None
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def reconstructed_stance(kodex_trend_pct: float | None, foreign_proxy_sum: float | None) -> str | None:
    """두 성분을 부호로 정규화한 뒤 더해서 판정(모듈 docstring — 스케일 불일치
    회피). 둘 중 하나라도 없으면 `None`(판단 불가를 위장하지 않는다)."""
    trend_sign, foreign_sign = _sign(kodex_trend_pct), _sign(foreign_proxy_sum)
    if trend_sign is None or foreign_sign is None:
        return None
    combined = trend_sign + foreign_sign
    if combined > 0:
        return "상승"
    if combined < 0:
        return "하락"
    return "중립"


def actual_direction(anchor_bars: dict[date, dict], d: date, prior_day: date | None) -> str | None:
    if prior_day is None:
        return None
    bar_d, bar_prior = anchor_bars.get(d), anchor_bars.get(prior_day)
    if not bar_d or not bar_prior or not bar_d.get("close") or not bar_prior.get("close"):
        return None
    diff = bar_d["close"] - bar_prior["close"]
    if diff > 0:
        return "상승"
    if diff < 0:
        return "하락"
    return "중립"


def stance_agreement(rows: list[dict]) -> dict:
    """`rows`(각 {"stance", "actual"}) 중 둘 다 있는 행만으로 정합률."""
    valid = [r for r in rows if r.get("stance") is not None and r.get("actual") is not None]
    n = len(valid)
    if n == 0:
        return {"n": 0, "agree_rate": None}
    agree = sum(1 for r in valid if r["stance"] == r["actual"])
    return {"n": n, "agree_rate": agree / n}


# ------------------------------------------------------------------ 전체 오케스트레이션(I/O, bar_cache는 candle_source로 조회)

def _build_context(
    root: Path, days: int, universe: list[str], candle_source, today: date, sleep_seconds: float,
) -> dict:
    """네트워크·원장 I/O — 변형 A/B가 공유하는 부분(유니버스·수급 원장·봉
    캐시·거래일 목록). `run_ab_reconstruction`이 이 함수를 **한 번만** 호출해
    두 변형을 채점함으로써 재수집을 피한다(모듈 docstring "no recrawl")."""
    frgn_idx = index_frgn_flow(_read_jsonl(root / "data" / "ledger" / FRGN_FLOW_LEDGER))
    disclosures_idx = index_disclosures_by_symbol(_read_jsonl(root / "data" / "ledger" / DISCLOSURES_LEDGER))

    symbols_to_fetch = sorted(set(universe) | {ANCHOR_SYMBOL})
    fetch_start = datetime.combine(today - timedelta(days=days * 2 + LOOKBACK_BUFFER_DAYS), datetime.min.time())
    fetch_end = datetime.combine(today, datetime.min.time())
    bar_cache, failed_symbols = build_bar_cache(candle_source, symbols_to_fetch, fetch_start, fetch_end, sleep_seconds)

    anchor_bars = bar_cache.get(ANCHOR_SYMBOL, {})
    all_anchor_days = sorted(anchor_bars.keys())
    trading_days = [d for d in all_anchor_days if d < today]
    trading_days = trading_days[-days:] if days else trading_days

    return {
        "frgn_idx": frgn_idx, "disclosures_idx": disclosures_idx,
        "bar_cache": bar_cache, "failed_symbols": failed_symbols,
        "anchor_bars": anchor_bars, "all_anchor_days": all_anchor_days,
        "trading_days": trading_days,
        "n_symbols_with_flow": sum(1 for sym in universe if frgn_idx.get(sym)),
    }


def _score_context(
    ctx: dict, universe: list[str], top: int, fee_bp: float, today: date, variant: str, days: int,
) -> dict:
    """`ctx`(`_build_context` 반환값) 위에서 변형(`"a"`|`"b"`|`"c"`) 하나를
    채점한다 — 순수(네트워크 없음, ctx가 이미 필요한 모든 I/O 결과를 갖고
    있다). 세 변형 모두 **같은** `prior_flow_rows` 게이트를 쓰므로(수급
    시계열 없으면 후보에서 제외) 같은 날 같은 후보 집합 위에서 채점된다 —
    공정한 A/B/C 전제."""
    frgn_idx, disclosures_idx, bar_cache = ctx["frgn_idx"], ctx["disclosures_idx"], ctx["bar_cache"]
    anchor_bars, all_anchor_days, trading_days = ctx["anchor_bars"], ctx["all_anchor_days"], ctx["trading_days"]

    def _prior_trading_day(d: date) -> date | None:
        earlier = [x for x in all_anchor_days if x < d]
        return earlier[-1] if earlier else None

    day_rows: list[dict] = []
    all_records: list[dict] = []
    stance_rows: list[dict] = []
    all_pick_mover_records: list[dict] = []
    all_universe_mover_records: list[dict] = []

    for d in trading_days:
        prior_day = _prior_trading_day(d)

        candidates: dict[str, dict] = {}
        rvol_by_symbol: dict[str, float | None] = {}
        for sym in universe:
            prior = prior_flow_rows(frgn_idx, sym, d)
            if not prior:
                continue
            bars = bar_cache.get(sym, {})
            t_score, t_meta = trending_score_proxy(bars, d)
            dart_types = precursor_types(disclosures_idx, sym, d)
            rvol_by_symbol[sym] = t_meta.get("rvol")
            if variant == "a":
                f_label = classify(prior)["label"]
                candidates[sym] = {"foreign_label": f_label, "trending_score100": t_score, "dart_types": dart_types}
            else:
                v2_score, v2_evidence = foreign_score_v2(prior, bars)
                candidates[sym] = {
                    "foreign_v2_score": v2_score, "foreign_v2_evidence": v2_evidence,
                    "trending_score100": t_score, "dart_types": dart_types,
                    # variant "c"는 이 값을 랭킹에 안 쓴다(최상위 강도보정 축을
                    # 뺀 격리 실험) — 그래도 계산해 두면 day_records 분석 시
                    # 참고할 수 있어 무해하게 남긴다.
                    "intensity_ratio": foreign_intensity_ratio(prior, bars),
                }

        if variant == "a":
            ranked = rank_replay(candidates, top=top)
        elif variant == "b":
            ranked = rank_replay_v2(candidates, top=top)
        else:
            ranked = rank_replay_c(candidates, top=top)
        pick_symbols = {sym for sym, _ in ranked}

        day_records: list[dict] = []
        for sym, result in ranked:
            outcome = picks_outcome(bar_cache.get(sym, {}), d, fee_bp)
            if outcome is None:
                continue
            day_records.append({
                "date": d.isoformat(), "symbol": sym, "score100": result["score100"],
                "gross_bp": outcome["gross_bp"], "net_bp": outcome["net_bp"],
                "rvol": rvol_by_symbol.get(sym),
            })
        all_records.extend(day_records)
        day_agg = aggregate_replay(day_records)

        # movers 포착(고가 기준) — 그날 채점된 유니버스 전체를 base rate로,
        # picks는 그 부분집합으로 같은 정의를 적용한다(모듈 docstring "Step 3 addendum").
        day_universe_mover_records: list[dict] = []
        day_pick_mover_records: list[dict] = []
        for sym in candidates:
            bar = bar_cache.get(sym, {}).get(d)
            rec = bar_mover_record(bar)
            if rec is None:
                continue
            rec = {**rec, "date": d.isoformat(), "symbol": sym}
            day_universe_mover_records.append(rec)
            if sym in pick_symbols:
                day_pick_mover_records.append(rec)
        all_universe_mover_records.extend(day_universe_mover_records)
        all_pick_mover_records.extend(day_pick_mover_records)

        kodex_trend = kodex_5d_trend(anchor_bars, d)
        f_proxy = market_foreign_proxy(frgn_idx, universe, prior_day)
        stance = reconstructed_stance(kodex_trend, f_proxy)
        actual = actual_direction(anchor_bars, d, prior_day)
        stance_rows.append({"date": d.isoformat(), "stance": stance, "actual": actual})

        day_rows.append({
            "date": d.isoformat(), "n_candidates_with_flow": len(candidates),
            **day_agg, "stance": stance, "actual": actual,
            "mover_picks": aggregate_mover_metrics(day_pick_mover_records),
            "mover_universe": aggregate_mover_metrics(day_universe_mover_records),
            "mover_precision_recall": mover_precision_recall(day_pick_mover_records, day_universe_mover_records),
        })

    return {
        "params": {
            "days": days, "top": top, "max_symbols": len(universe), "fee_bp": fee_bp,
            "today": today.isoformat(), "variant": variant,
        },
        "universe_size": len(universe),
        "n_symbols_with_flow": ctx["n_symbols_with_flow"],
        "n_symbols_bars_ok": len(bar_cache), "n_symbols_bars_failed": len(ctx["failed_symbols"]),
        "trading_days": [d.isoformat() for d in trading_days],
        "day_rows": day_rows,
        "records": all_records,
        "aggregate": aggregate_replay(all_records),
        "stance_rows": stance_rows,
        "stance_summary": stance_agreement(stance_rows),
        "mover_picks_aggregate": aggregate_mover_metrics(all_pick_mover_records),
        "mover_universe_aggregate": aggregate_mover_metrics(all_universe_mover_records),
        "mover_precision_recall": mover_precision_recall(all_pick_mover_records, all_universe_mover_records),
    }


def run_reconstruction(
    root: Path, days: int, top: int, universe: list[str], candle_source,
    fee_bp: float = DEFAULT_FEE_BP, today: date | None = None, sleep_seconds: float = BAR_SLEEP_SECONDS,
    variant: str = "a",
) -> dict:
    """변형 하나(`"a"`=기존 라벨 기반, `"b"`=`foreign_score_v2`+강도보정 축,
    `"c"`=`foreign_score_v2`만 강도보정 축 없이)를 채점한다. 여러 변형을
    같은 데이터 위에서 비교하려면 `run_variant_comparison`을 쓴다(그쪽이
    봉 캐시를 한 번만 받아온다)."""
    today = today or date.today()
    ctx = _build_context(root, days, universe, candle_source, today, sleep_seconds)
    return _score_context(ctx, universe, top, fee_bp, today, variant, days)


def run_variant_comparison(
    root: Path, days: int, top: int, universe: list[str], candle_source,
    fee_bp: float = DEFAULT_FEE_BP, today: date | None = None, sleep_seconds: float = BAR_SLEEP_SECONDS,
    variants: tuple[str, ...] = ("a", "b"),
) -> dict[str, dict]:
    """`variants`에 든 변형들을 **같은** 유니버스·같은 거래일창·같은 봉 캐시
    위에서 채점한다(서브프로젝트 O) — `_build_context`를 한 번만 호출해
    네트워크 재수집을 피한다. 반환 `{variant: result}`, 각 result는
    `run_reconstruction`과 동일한 shape."""
    today = today or date.today()
    ctx = _build_context(root, days, universe, candle_source, today, sleep_seconds)
    return {v: _score_context(ctx, universe, top, fee_bp, today, v, days) for v in variants}


def run_ab_reconstruction(
    root: Path, days: int, top: int, universe: list[str], candle_source,
    fee_bp: float = DEFAULT_FEE_BP, today: date | None = None, sleep_seconds: float = BAR_SLEEP_SECONDS,
) -> dict:
    """변형 A(기존 라벨)·B(`foreign_score_v2`)만 비교하는 `run_variant_comparison`의
    얇은 래퍼(기존 호출부·테스트 호환). 반환 `{"a": ..., "b": ...}`."""
    today = today or date.today()
    result = run_variant_comparison(
        root, days, top, universe, candle_source, fee_bp, today, sleep_seconds, variants=("a", "b"),
    )
    return {"a": result["a"], "b": result["b"]}


# ------------------------------------------------------------------ 출력 + 원장 기록

def _fmt_bp(v: float | None) -> str:
    return "n/a" if v is None else f"{v:+.1f}bp"


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _fmt_num(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def format_report(result: dict) -> str:
    lines: list[str] = []
    lines.append("=== 과거 리포트 재구성 백테스트 (뉴스 축 제외 — 재구성 전용) ===")
    p = result["params"]
    lines.append(
        f"기간: 최근 {p['days']}거래일 · top {p['top']} · 유니버스 상한 {p['max_symbols']} · "
        f"수수료 {p['fee_bp']:.0f}bp(왕복) · 기준일 {p['today']}"
    )
    um = result.get("universe_meta") or {}
    if um:
        lines.append(f"유니버스 구성: {um.get('method', 'n/a')}")
    bf = result.get("backfill")
    if bf:
        lines.append(f"수급 백필: 성공 {bf['n_ok']}종목 · 실패 {bf['n_failed']}종목 · 신규/갱신 {bf['rows_written']}행")
    lines.append(
        f"수급 시계열 확보: {result['n_symbols_with_flow']}/{result['universe_size']}종목 · "
        f"봉 캐시: 성공 {result['n_symbols_bars_ok']}종목 · 실패 {result['n_symbols_bars_failed']}종목"
    )
    lines.append("")
    lines.append(f"{'날짜':<12}{'후보':>6}{'적중률':>10}{'평균bp':>10}{'합계bp':>10}{'RVOL평균':>10}{'재구성':>8}{'실제':>8}")
    for row in result["day_rows"]:
        lines.append(
            f"{row['date']:<12}{row['n']:>6}"
            f"{_fmt_pct(row['hit_rate']):>10}"
            f"{_fmt_bp(row['avg_net_bp']):>10}"
            f"{_fmt_bp(row['sum_net_bp']):>10}"
            f"{_fmt_num(row['avg_rvol']):>10}"
            f"{(row['stance'] or 'n/a'):>8}"
            f"{(row['actual'] or 'n/a'):>8}"
        )
    agg = result["aggregate"]
    lines.append("-" * 74)
    lines.append(
        f"{'종합':<12}{agg['n']:>6}"
        f"{_fmt_pct(agg['hit_rate']):>10}"
        f"{_fmt_bp(agg['avg_net_bp']):>10}"
        f"{_fmt_bp(agg['sum_net_bp']):>10}"
        f"{_fmt_num(agg['avg_rvol']):>10}"
    )
    if agg["n"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(
            f"\n표본 부족(종목-일 n={agg['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — "
            "판단 불가. 이 숫자로 재구성 선정 정확도를 주장하지 않는다."
        )

    ss = result["stance_summary"]
    lines.append("")
    lines.append("--- 시장 스탠스 정합률(재구성 vs 실제 KODEX200 방향) ---")
    lines.append(f"표본 n={ss['n']} · 정합률 {_fmt_pct(ss['agree_rate'])}")
    if ss["n"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(f"표본 부족(n={ss['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가.")

    mp, mu = result["mover_picks_aggregate"], result["mover_universe_aggregate"]
    pr = result["mover_precision_recall"]
    lines.append("")
    lines.append(f"--- Movers 포착(고가 기준, movers = 고가≥시가+{MOVER_THRESHOLD_PCT:.0f}%) — picks vs 유니버스 base rate ---")
    lines.append(f"{'구분':<10}{'n':>6}{'진폭중앙값':>12}{'도달률≥3%':>12}{'도달률≥5%':>12}{'mover비율':>12}")
    lines.append(
        f"{'picks':<10}{mp['n']:>6}{_fmt_num(mp['amplitude_median']):>12}"
        f"{_fmt_pct(mp['reach3_rate']):>12}{_fmt_pct(mp['reach5_rate']):>12}{_fmt_pct(mp['mover_rate']):>12}"
    )
    lines.append(
        f"{'유니버스':<10}{mu['n']:>6}{_fmt_num(mu['amplitude_median']):>12}"
        f"{_fmt_pct(mu['reach3_rate']):>12}{_fmt_pct(mu['reach5_rate']):>12}{_fmt_pct(mu['mover_rate']):>12}"
    )
    lines.append(
        f"movers 포착: 유니버스 내 movers {pr['n_universe_movers']}건 · 그중 picks 포함 {pr['n_pick_movers']}건 · "
        f"recall {_fmt_pct(pr['recall'])} · precision {_fmt_pct(pr['precision'])}(picks {pr['n_picks']}건 중)"
    )
    if mu["n"] < MIN_SAMPLE_FOR_JUDGEMENT:
        lines.append(f"표본 부족(유니버스 n={mu['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가.")
    verdict = (
        "picks의 mover비율이 유니버스 base rate보다 뚜렷이 높다 — 우연 이상으로 movers를 골랐다."
        if (mp["mover_rate"] is not None and mu["mover_rate"] is not None and mp["mover_rate"] > mu["mover_rate"])
        else "picks의 mover비율이 유니버스 base rate와 비슷하거나 낮다 — 우연 이상으로 movers를 골랐다고 주장할 수 없다."
    )
    lines.append(f"판정: {verdict}")

    return "\n".join(lines)


def format_ab_comparison(result_a: dict, result_b: dict) -> str:
    """변형 A(기존 라벨 기반)와 B(`foreign_score_v2`) 비교표 — 서브프로젝트
    O. `run_ab_reconstruction`이 보장하는 전제(같은 유니버스·같은 거래일창·
    같은 봉 캐시·같은 후보 게이트) 위에서만 이 비교가 의미 있다(paired
    comparison, 같은 n, 같은 날짜). B를 "더 낫다"고 선언하는 기준은
    movers precision **그리고** 방향성(적중률) 둘 다 A보다 나을 때뿐이다
    — 한쪽만 개선되면 혼재 결과로 그대로 보고한다(통계적 정직성)."""
    agg_a, agg_b = result_a["aggregate"], result_b["aggregate"]
    mp_a, mu_a, pr_a = result_a["mover_picks_aggregate"], result_a["mover_universe_aggregate"], result_a["mover_precision_recall"]
    mp_b, mu_b, pr_b = result_b["mover_picks_aggregate"], result_b["mover_universe_aggregate"], result_b["mover_precision_recall"]

    lines: list[str] = []
    lines.append("=== A/B 비교 — 외국인 축 v2(쌍끌이·연속일·강도) vs 기존 라벨 (서브프로젝트 O) ===")
    lines.append(
        f"표본: A n={agg_a['n']}, B n={agg_b['n']} (같은 유니버스·같은 거래일창·같은 게이트 — paired)"
    )

    lines.append("")
    lines.append("--- 방향성(시가→종가) ---")
    lines.append(f"{'':<12}{'n':>6}{'적중률':>10}{'평균bp':>10}")
    lines.append(f"{'A(기존)':<12}{agg_a['n']:>6}{_fmt_pct(agg_a['hit_rate']):>10}{_fmt_bp(agg_a['avg_net_bp']):>10}")
    lines.append(f"{'B(v2)':<12}{agg_b['n']:>6}{_fmt_pct(agg_b['hit_rate']):>10}{_fmt_bp(agg_b['avg_net_bp']):>10}")

    lines.append("")
    lines.append(f"--- movers 포착(고가≥시가+{MOVER_THRESHOLD_PCT:.0f}%) ---")
    lines.append(f"{'':<12}{'n':>6}{'진폭중앙값':>12}{'mover비율':>12}{'precision':>12}{'recall':>10}")
    lines.append(
        f"{'A(기존)':<12}{mp_a['n']:>6}{_fmt_num(mp_a['amplitude_median']):>12}"
        f"{_fmt_pct(mp_a['mover_rate']):>12}{_fmt_pct(pr_a['precision']):>12}{_fmt_pct(pr_a['recall']):>10}"
    )
    lines.append(
        f"{'B(v2)':<12}{mp_b['n']:>6}{_fmt_num(mp_b['amplitude_median']):>12}"
        f"{_fmt_pct(mp_b['mover_rate']):>12}{_fmt_pct(pr_b['precision']):>12}{_fmt_pct(pr_b['recall']):>10}"
    )
    lines.append(
        f"{'유니버스':<12}{mu_a['n']:>6}{_fmt_num(mu_a['amplitude_median']):>12}{_fmt_pct(mu_a['mover_rate']):>12}"
        f"{'n/a':>12}{'n/a':>10}"
    )

    precision_a, precision_b = pr_a["precision"], pr_b["precision"]
    hit_a, hit_b = agg_a["hit_rate"], agg_b["hit_rate"]
    better_precision = precision_a is not None and precision_b is not None and precision_b > precision_a
    better_direction = hit_a is not None and hit_b is not None and hit_b > hit_a

    if agg_a["n"] < MIN_SAMPLE_FOR_JUDGEMENT or agg_b["n"] < MIN_SAMPLE_FOR_JUDGEMENT:
        verdict = f"표본 부족(A n={agg_a['n']}, B n={agg_b['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가."
    elif better_precision and better_direction:
        verdict = "B가 movers precision과 방향성(적중률) 둘 다 A보다 낫다 — B 채택 근거 있음."
    elif better_precision or better_direction:
        which = "movers precision만" if better_precision else "방향성(적중률)만"
        verdict = f"B가 {which} 개선 — 혼재 결과, B 전면 채택 근거로는 부족하다."
    else:
        verdict = "B가 A보다 나은 지표가 없다 — 이 실행에서는 v2 축 교체 근거가 나오지 않았다."

    lines.append("")
    lines.append(f"판정: {verdict}")
    return "\n".join(lines)


# B의 우위 개선분 중 C(강도보정 축 없음)가 얼마나 유지하는지 판정하는 문턱.
# ≥COMPOSITION_RETAIN_HIGH 이면 "실무 규칙(쌍끌이·연속일·정합)이 승리 원인" —
# 강도보정 축 없이도 대부분의 개선이 유지된다는 뜻. ≤COMPOSITION_RETAIN_LOW면
# "강도가 driver" — 강도보정 축을 빼면 개선이 A 수준으로 붕괴한다는 뜻. 그
# 사이는 혼재로 그대로 보고한다(억지로 한쪽으로 판정하지 않는다).
COMPOSITION_RETAIN_HIGH = 0.7
COMPOSITION_RETAIN_LOW = 0.3


def format_abc_comparison(result_a: dict, result_b: dict, result_c: dict) -> str:
    """변형 A(기존 라벨)·B(v2+강도보정 축)·C(v2, 강도보정 축 없음) 3-way 비교
    — 오케스트레이터 후속 지시(2026-08-17)의 격리 실험. B가 A를 이겼다는
    전제 위에서, C가 그 개선분을 얼마나 유지하는지로 "무엇이 승리 원인인지"
    (실무 규칙 자체 vs 강도의 이중 계산)를 정량적으로 판정한다
    (`COMPOSITION_RETAIN_HIGH`/`COMPOSITION_RETAIN_LOW` 문턱). `run_variant_comparison`
    이 보장하는 전제(같은 유니버스·같은 거래일창·같은 봉 캐시·같은 후보 게이트)
    위에서만 이 비교가 의미 있다."""
    agg_a, agg_b, agg_c = result_a["aggregate"], result_b["aggregate"], result_c["aggregate"]
    mp_a, pr_a = result_a["mover_picks_aggregate"], result_a["mover_precision_recall"]
    mp_b, pr_b = result_b["mover_picks_aggregate"], result_b["mover_precision_recall"]
    mp_c, pr_c = result_c["mover_picks_aggregate"], result_c["mover_precision_recall"]
    mu_a = result_a["mover_universe_aggregate"]

    lines: list[str] = []
    lines.append("=== A/B/C 비교 — 강도보정 축 격리 실험 (서브프로젝트 O 후속) ===")
    lines.append(
        f"표본: A n={agg_a['n']}, B n={agg_b['n']}, C n={agg_c['n']} "
        "(같은 유니버스·같은 거래일창·같은 게이트 — paired)"
    )

    lines.append("")
    lines.append("--- 방향성(시가→종가) ---")
    lines.append(f"{'':<14}{'n':>6}{'적중률':>10}{'평균bp':>10}")
    for label, agg in (("A(기존)", agg_a), ("B(v2+강도)", agg_b), ("C(v2만)", agg_c)):
        lines.append(f"{label:<14}{agg['n']:>6}{_fmt_pct(agg['hit_rate']):>10}{_fmt_bp(agg['avg_net_bp']):>10}")

    lines.append("")
    lines.append(f"--- movers 포착(고가≥시가+{MOVER_THRESHOLD_PCT:.0f}%) ---")
    lines.append(f"{'':<14}{'n':>6}{'진폭중앙값':>12}{'mover비율':>12}{'precision':>12}{'recall':>10}")
    for label, mp, pr in (("A(기존)", mp_a, pr_a), ("B(v2+강도)", mp_b, pr_b), ("C(v2만)", mp_c, pr_c)):
        lines.append(
            f"{label:<14}{mp['n']:>6}{_fmt_num(mp['amplitude_median']):>12}"
            f"{_fmt_pct(mp['mover_rate']):>12}{_fmt_pct(pr['precision']):>12}{_fmt_pct(pr['recall']):>10}"
        )
    lines.append(
        f"{'유니버스':<14}{mu_a['n']:>6}{_fmt_num(mu_a['amplitude_median']):>12}{_fmt_pct(mu_a['mover_rate']):>12}"
        f"{'n/a':>12}{'n/a':>10}"
    )

    gap_b_precision = (pr_b["precision"] - pr_a["precision"]) if pr_a["precision"] is not None and pr_b["precision"] is not None else None
    gap_c_precision = (pr_c["precision"] - pr_a["precision"]) if pr_a["precision"] is not None and pr_c["precision"] is not None else None
    gap_b_hit = (agg_b["hit_rate"] - agg_a["hit_rate"]) if agg_a["hit_rate"] is not None and agg_b["hit_rate"] is not None else None
    gap_c_hit = (agg_c["hit_rate"] - agg_a["hit_rate"]) if agg_a["hit_rate"] is not None and agg_c["hit_rate"] is not None else None

    retain_precision = gap_c_precision / gap_b_precision if gap_b_precision else None
    retain_hit = gap_c_hit / gap_b_hit if gap_b_hit else None

    lines.append("")
    if any(n < MIN_SAMPLE_FOR_JUDGEMENT for n in (agg_a["n"], agg_b["n"], agg_c["n"])):
        verdict = (
            f"표본 부족(A n={agg_a['n']}, B n={agg_b['n']}, C n={agg_c['n']} < {MIN_SAMPLE_FOR_JUDGEMENT}) — 판단 불가."
        )
    elif gap_b_precision is None or gap_b_hit is None or gap_b_precision <= 0 or gap_b_hit <= 0:
        verdict = "이 실행에서는 B가 A를 (precision·방향성 둘 다) 이기지 못해 C 격리 비교가 성립하지 않는다."
    elif retain_precision is not None and retain_hit is not None and retain_precision >= COMPOSITION_RETAIN_HIGH and retain_hit >= COMPOSITION_RETAIN_HIGH:
        verdict = (
            f"C가 B 개선분의 precision {retain_precision * 100:.0f}%·방향성 {retain_hit * 100:.0f}%를 유지한다"
            f"(≥{COMPOSITION_RETAIN_HIGH * 100:.0f}%) — 쌍끌이·연속일·정합 같은 실무 규칙 자체가 승리 원인이다. "
            "강도보정 축 없이도(C) B와 비슷한 성능 — C 채택 권고(더 단순, 이중 계산 없음)."
        )
    elif retain_precision is not None and retain_hit is not None and retain_precision <= COMPOSITION_RETAIN_LOW and retain_hit <= COMPOSITION_RETAIN_LOW:
        verdict = (
            f"C가 B 개선분의 precision {retain_precision * 100:.0f}%·방향성 {retain_hit * 100:.0f}%만 유지한다"
            f"(≤{COMPOSITION_RETAIN_LOW * 100:.0f}%) — C가 A 근처로 붕괴한다. 강도가 B 우위의 실질적 driver다 —"
            " 강도보정 축(이중 계산이지만)을 유지한 B를 그대로 쓴다."
        )
    else:
        rp = f"{retain_precision * 100:.0f}%" if retain_precision is not None else "n/a"
        rh = f"{retain_hit * 100:.0f}%" if retain_hit is not None else "n/a"
        verdict = f"혼재(precision 유지율 {rp}, 방향성 유지율 {rh}) — 실무 규칙과 강도 중 어느 쪽이 driver인지 이 실행만으로는 명확히 갈리지 않는다."

    lines.append(f"판정: {verdict}")
    return "\n".join(lines)


def write_ledger(result: dict, root: Path, today: date | None = None) -> Path:
    today = today or date.today()
    path = root / "data" / "ledger" / REPLAY_LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date": today.isoformat(),
        "params": result["params"],
        "aggregate": result["aggregate"],
        "stance_summary": result["stance_summary"],
        "universe_size": result["universe_size"],
        "n_symbols_with_flow": result["n_symbols_with_flow"],
        "mover_picks_aggregate": result["mover_picks_aggregate"],
        "mover_universe_aggregate": result["mover_universe_aggregate"],
        "mover_precision_recall": result["mover_precision_recall"],
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="report_replay")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--max-symbols", type=int, default=DEFAULT_MAX_SYMBOLS)
    parser.add_argument("--fee-bp", type=float, default=DEFAULT_FEE_BP)
    parser.add_argument("--root", default=".")
    parser.add_argument("--skip-backfill", action="store_true", help="Step1 수급 백필 스킵(재실행/디버그용)")
    parser.add_argument(
        "--variant", choices=["a", "b", "c", "ab", "abc"], default="a",
        help=(
            "a=기존 라벨 기반(기본, 불변) · b=foreign_score_v2+강도보정 축 · "
            "c=foreign_score_v2만(강도보정 축 없음, 격리 실험) · "
            "ab=A/B 비교 · abc=A/B/C 3-way 비교(서브프로젝트 O)"
        ),
    )
    args = parser.parse_args(argv)
    root = Path(args.root)

    try:
        from quant.collect.quotes.yf_source import YFinanceCandleSource

        candle_source = YFinanceCandleSource("1d")

        universe, universe_meta = select_universe(root, args.max_symbols)
        print(f"유니버스: {len(universe)}종목 ({universe_meta['method']})")

        backfill_result = None
        if not args.skip_backfill:
            backfill_result = backfill_flow_history(root, universe, sleep_seconds=FLOW_SLEEP_SECONDS)
            print(
                f"수급 백필: 성공 {backfill_result['n_ok']} · 실패 {backfill_result['n_failed']} · "
                f"신규/갱신 {backfill_result['rows_written']}행"
            )

        if args.variant == "abc":
            variants = run_variant_comparison(
                root, args.days, args.top, universe, candle_source, fee_bp=args.fee_bp, variants=("a", "b", "c"),
            )
            labels = {"a": "A — 기존 라벨 기반", "b": "B — foreign_score_v2+강도보정 축", "c": "C — foreign_score_v2만"}
            paths = []
            for key in ("a", "b", "c"):
                variants[key]["universe_meta"] = universe_meta
                variants[key]["backfill"] = backfill_result
                print(f"\n[변형 {labels[key]}]")
                print(format_report(variants[key]))
                paths.append(write_ledger(variants[key], root))

            print()
            print(format_abc_comparison(variants["a"], variants["b"], variants["c"]))
            print(f"\n원장 기록: {paths[0]} (A), {paths[1]} (B), {paths[2]} (C)")
        elif args.variant == "ab":
            ab = run_ab_reconstruction(root, args.days, args.top, universe, candle_source, fee_bp=args.fee_bp)
            for result in (ab["a"], ab["b"]):
                result["universe_meta"] = universe_meta
                result["backfill"] = backfill_result

            print("\n[변형 A — 기존 라벨 기반]")
            print(format_report(ab["a"]))
            print("\n[변형 B — foreign_score_v2]")
            print(format_report(ab["b"]))
            print()
            print(format_ab_comparison(ab["a"], ab["b"]))

            path_a = write_ledger(ab["a"], root)
            path_b = write_ledger(ab["b"], root)
            print(f"\n원장 기록: {path_a} (A), {path_b} (B)")
        else:
            result = run_reconstruction(
                root, args.days, args.top, universe, candle_source, fee_bp=args.fee_bp, variant=args.variant,
            )
            result["universe_meta"] = universe_meta
            result["backfill"] = backfill_result
            print(format_report(result))

            ledger_path = write_ledger(result, root)
            print(f"\n원장 기록: {ledger_path}")
    except Exception as e:  # noqa: BLE001 — 이 배치는 항상 exit 0 (다른 파이프라인을 막지 않는다)
        print(f"리포트 재구성 백테스트 실행 실패: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
