"""실측 왕복 비용 모델 — 하드코딩 20bp를 원장으로 대체한다 (2026-08-28).

## 왜

이 저장소에서 가장 비싼 사실은 **수수료가 엣지보다 크다**는 것이다(2026-08-21
원장: US 3개 전략 전부 수수료 전 양수인데 왕복 20bp가 음수로 뒤집었다). 그런데
그 20bp는 `forensics.DEFAULT_ROUND_TRIP_BP`에 **상수로 박혀 있다** — 2026-08-21
당시 원장 평균(17.9~22.7bp)을 눈으로 보고 적은 값이다.

상수의 문제는 틀렸다는 게 아니라 **언제 틀리게 됐는지 아무도 모른다**는 것이다.
브로커를 바꾸거나, KR 비중이 늘거나, 명목이 커지면 실제 비용은 움직이는데 상수는
안 움직인다. 이 모듈은 그 값을 원장에서 매번 다시 센다.

## 무엇을 세는가

    왕복 비용(bp) = 수수료(bp) + 슬리피지(bp)

- **수수료**: 원장 체결의 `fee`를 트립 단위로 합해 **진입 명목 대비** bp.
  `ledger.round_trips`가 이미 트립별 `fees`/`notional`을 낸다.
- **슬리피지**: 신호 시점 가격 대비 체결가 차이. `tca.py`가 이미 계산한다 —
  여기서는 그 결과를 왕복(2 leg) 단위로 환산할 뿐 다시 구현하지 않는다.

## 없는 정밀도를 지어내지 않는다

- 트립 표본이 모자라면 `measure()`가 **None**을 반환한다. 호출부가 기본값
  (`FALLBACK_ROUND_TRIP_BP`)을 쓰되 **기본값을 썼다고 출력에 밝혀야** 한다 —
  그게 `effective_round_trip_bp()`가 라벨을 함께 돌려주는 이유다.
- 슬리피지만 표본이 없는 경우가 **정상**이다: `tca.py`의 intent 기록은 실주문
  경로(TossBroker)에서만 남고 paper 모드에는 없다. 그때 `slippage_bp`와
  `total_bp`는 None이고 `fee_bp`만 안다 — 아는 절반을 전부인 척하지 않는다.
- 잔돈 트립은 제외한다. KR은 최소 수수료가 있어 명목이 작으면 bp가 폭발한다
  (`ledger.DUST_NOTIONAL_*`와 같은 기준을 쓴다). 몇 건을 뺐는지 함께 낸다.

`quant/control/` 소속 — 원장을 읽어 다음 세션을 낫게 하는 층. 거래 평면을
임포트하지 않고, 설정 파일도 쓰지 않는다. 파일 I/O도 없다(호출부가 원장을 읽어
넘긴다 — `tca.py`·`forensics.py`와 같은 계약).
"""
from __future__ import annotations

from dataclasses import dataclass

from quant.control.forensics import DEFAULT_ROUND_TRIP_BP
from quant.control.ledger import DUST_NOTIONAL_KRW, DUST_NOTIONAL_USD

__all__ = [
    "ASSUMED_ROUND_TRIP_BP",
    "FALLBACK_ROUND_TRIP_BP",
    "MAX_SPREAD_SAMPLE_GAP_SECONDS",
    "MIN_LEGS_FOR_SLIPPAGE",
    "MIN_TRIPS_FOR_FEE",
    "RoundTripCost",
    "SpreadCostComparison",
    "by_market",
    "by_strategy",
    "compare_spread_cost",
    "effective_round_trip_bp",
    "measure",
]

# 실측이 없을 때 호출부가 쓸 값 — forensics의 상수를 그대로 재수출한다.
# 20bp를 두 번 적는 순간 둘은 반드시 갈라진다.
FALLBACK_ROUND_TRIP_BP = DEFAULT_ROUND_TRIP_BP

# 수수료는 거의 정률이다(2026-08-15 실측: US 명목 $201~$1,411 전 구간에서
# 19.85~20.56bp). 분산이 작으므로 승률 판정선(30건)만큼 필요하지 않다 —
# 10건이면 평균이 실무적으로 안정된다.
MIN_TRIPS_FOR_FEE = 10

# 슬리피지는 반대다. 체결 시점 호가와 유동성에 따라 크게 흔들리므로 더 많은
# 표본이 필요하다. 20 leg(= 왕복 10회 상당)을 하한으로 둔다.
MIN_LEGS_FOR_SLIPPAGE = 20


@dataclass(frozen=True)
class RoundTripCost:
    """왕복 1회에 실제로 물은 비용(진입 명목 대비 bp).

    `slippage_bp`/`total_bp`가 None인 것은 "0"이 아니라 **"모른다"**다.
    """

    scope: str                    # "전체" / "KR" / "donchian" 등 — 무엇의 비용인가
    fee_bp: float
    slippage_bp: float | None
    total_bp: float | None        # fee + slippage. 슬리피지를 모르면 None
    n_trips: int
    n_slippage_legs: int
    n_dust_excluded: int

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "fee_bp": round(self.fee_bp, 2),
            "slippage_bp": None if self.slippage_bp is None else round(self.slippage_bp, 2),
            "total_bp": None if self.total_bp is None else round(self.total_bp, 2),
            "n_trips": self.n_trips,
            "n_slippage_legs": self.n_slippage_legs,
            "n_dust_excluded": self.n_dust_excluded,
        }


def _dust_threshold(market: str) -> float:
    return DUST_NOTIONAL_KRW if market == "KR" else DUST_NOTIONAL_USD


def _usable(trips: list[dict]) -> tuple[list[dict], int]:
    """명목이 있고 잔돈이 아닌 트립만. `(쓸 트립, 제외한 잔돈 수)`."""
    kept, dust = [], 0
    for t in trips:
        notional = float(t.get("notional") or 0.0)
        if notional <= 0:
            continue
        if notional < _dust_threshold(str(t.get("market") or "US")):
            dust += 1
            continue
        kept.append(t)
    return kept, dust


def measure(
    trips: list[dict], slippage_rows: list[dict] | None = None, *, scope: str = "전체",
) -> RoundTripCost | None:
    """트립 목록(+선택적 TCA 슬리피지 행) → 왕복 비용. 표본 부족이면 None.

    `trips`는 `ledger.round_trips()`의 출력, `slippage_rows`는
    `tca.slippage_bps()`의 출력이다(각 행이 **한 leg**, `bps` 양수 = 불리).

    수수료 bp는 **가중평균**이다: Σfees / Σnotional. 트립별 bp의 단순평균이
    아니다 — 작은 트립 하나가 평균을 끌고 가면 안 된다.

    왕복 슬리피지는 leg 평균 × 2다. 표본이 한쪽 방향(매수만/매도만)뿐이면
    반대편도 같다고 가정하는 셈이라 낙관/비관 어느 쪽으로도 틀릴 수 있다 —
    `n_slippage_legs`를 함께 보고 판단한다.
    """
    kept, dust = _usable(trips)
    if len(kept) < MIN_TRIPS_FOR_FEE:
        return None

    total_fees = sum(float(t.get("fees") or 0.0) for t in kept)
    total_notional = sum(float(t["notional"]) for t in kept)
    fee_bp = total_fees / total_notional * 1e4

    legs = [
        float(r["bps"]) for r in (slippage_rows or [])
        if r.get("bps") is not None
    ]
    slippage_bp: float | None = None
    if len(legs) >= MIN_LEGS_FOR_SLIPPAGE:
        slippage_bp = sum(legs) / len(legs) * 2.0

    return RoundTripCost(
        scope=scope,
        fee_bp=fee_bp,
        slippage_bp=slippage_bp,
        total_bp=None if slippage_bp is None else fee_bp + slippage_bp,
        n_trips=len(kept),
        n_slippage_legs=len(legs),
        n_dust_excluded=dust,
    )


def _grouped(
    trips: list[dict], slippage_rows: list[dict] | None,
    trip_key: str, slip_key: str,
) -> dict[str, RoundTripCost | None]:
    keys = sorted({str(t.get(trip_key) or "?") for t in trips})
    out: dict[str, RoundTripCost | None] = {}
    for key in keys:
        group = [t for t in trips if str(t.get(trip_key) or "?") == key]
        slips = [
            r for r in (slippage_rows or []) if str(r.get(slip_key) or "?") == key
        ]
        out[key] = measure(group, slips, scope=key)
    return out


def by_market(
    trips: list[dict], slippage_rows: list[dict] | None = None,
) -> dict[str, RoundTripCost | None]:
    """시장별(KR/US) 왕복 비용. 값이 None인 시장은 표본 부족이다.

    시장을 섞으면 안 되는 이유: KR ETF와 US 3배 ETF는 수수료 체계가 다르고,
    "왕복 20bp"라는 하나의 숫자는 그 둘 중 어느 쪽에도 맞지 않을 수 있다."""
    return _grouped(trips, slippage_rows, "market", "market")


def by_strategy(
    trips: list[dict], slippage_rows: list[dict] | None = None,
) -> dict[str, RoundTripCost | None]:
    """전략별 왕복 비용. 트립의 `strategy`와 TCA 행의 `strategy_id`를 맞춘다."""
    return _grouped(trips, slippage_rows, "strategy", "strategy_id")


def effective_round_trip_bp(cost: RoundTripCost | None) -> tuple[float, str]:
    """`(쓸 bp, 출처 라벨)` — 실측이 있으면 그것을, 없으면 기본값을 쓴다.

    라벨을 **같이** 돌려주는 게 요점이다. 숫자만 돌려주면 호출부는 그게
    실측인지 기본값인지 모른 채 리포트에 찍고, 읽는 사람은 그걸 실측으로
    읽는다. 이 저장소에서 가장 비싼 오해가 정확히 그 종류다.
    """
    if cost is None:
        return FALLBACK_ROUND_TRIP_BP, f"기본값 {FALLBACK_ROUND_TRIP_BP:.0f}bp (실측 표본 부족)"
    if cost.total_bp is None:
        return (
            cost.fee_bp,
            f"실측 수수료만 {cost.fee_bp:.1f}bp ({cost.n_trips}트립) — "
            "슬리피지 표본 없음(paper 구간), 실제 왕복 비용은 이보다 크다",
        )
    return (
        cost.total_bp,
        f"실측 {cost.total_bp:.1f}bp = 수수료 {cost.fee_bp:.1f} + "
        f"슬리피지 {cost.slippage_bp:.1f} ({cost.n_trips}트립 / {cost.n_slippage_legs}leg)",
    )


# ── 호가 스프레드 실측(spread.jsonl) 기반 비교 (2026-08-30) ────────────────
#
# 위 `measure()`의 슬리피지는 `tca.py`의 intent-vs-fill 매칭에서 온다 — 그
# intent 행은 **TossBroker(실주문 경로)만** 남긴다(`tca.py` 모듈 docstring).
# 이 저장소가 지금 paper로만 도는 동안은 그 슬리피지 표본이 항상 0이다.
#
# `data/ledger/spread.jsonl`(`server/scripts/spread_sample.sh`가 10분마다
# 수집하는 실측 호가 스프레드)은 paper/live 무관하게 항상 쌓인다 — 그래서
# daily_wrap의 "체결 비용" 절은 이 원장을 대신 쓴다: 왕복 실측 = 수수료(fee_bp)
# + 진입·청산 시점에 가장 가까운 스프레드 표본의 절반씩(entry half + exit
# half). 우리가 이미 인용해 온 비용 가정(전략 docstring들 — overnight_drift.py:
# "US ETF ≈26bp·KR 개별주 ≈30bp", rsi2_dip.py: "KR ETF 4bp")과 대조해 그
# 가정이 낙관인지 보수인지 한 줄로 판정한다.

# 수집 주기(10분)의 1.5배 여유 — 이보다 먼 표본은 "당시 스프레드"로 보지
# 않는다(지어내지 않는다).
MAX_SPREAD_SAMPLE_GAP_SECONDS = 900

# 전략 docstring들이 실측으로 인용해 온 왕복 비용 가정(bp) — daily_wrap
# "체결 비용" 절의 대조 기준값. 여기 한 곳에 모아 그 인용들과 어긋나지 않게 한다.
ASSUMED_ROUND_TRIP_BP: dict[str, float] = {"US": 26.0, "KR_ETF": 4.0, "KR_STOCK": 30.0}


def _nearest_spread_bp(rows: list[dict], ts: object, max_gap_seconds: float) -> float | None:
    """`ts`(체결 원장의 entry_ts/exit_ts)에 가장 가까운 `rows`(이미 그 심볼로
    필터된 spread.jsonl 행)의 `spread_bp`. 표본이 없거나 가장 가까운 표본도
    `max_gap_seconds`보다 멀면 None — 없는 정밀도를 지어내지 않는다."""
    if not ts or not rows:
        return None
    from datetime import datetime

    try:
        target = datetime.fromisoformat(str(ts))
    except ValueError:
        return None

    best_bp: float | None = None
    best_gap: float | None = None
    for row in rows:
        raw_ts = row.get("ts")
        if not raw_ts:
            continue
        try:
            cand = datetime.fromisoformat(str(raw_ts))
        except ValueError:
            continue
        # tz 유무가 서로 다르면 뺄셈 자체가 TypeError다 — 섞인 표본은 비교하지
        # 않고 건너뛴다(임의로 tz를 붙이면 오차를 지어내는 셈이다).
        if (target.tzinfo is None) != (cand.tzinfo is None):
            continue
        gap = abs((cand - target).total_seconds())
        if best_gap is None or gap < best_gap:
            best_gap, best_bp = gap, row.get("spread_bp")

    if best_gap is None or best_gap > max_gap_seconds or best_bp is None:
        return None
    try:
        return float(best_bp)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SpreadCostComparison:
    """왕복 실측(수수료 + 당시 스프레드) vs 가정 — daily_wrap 6절 재료."""

    scope: str
    observed_bp: float
    assumed_bp: float
    n_trips: int  # 대상 트립 수(명목 0 제외)
    n_priced: int  # 스프레드 표본을 찾아 observed_bp 계산에 실제로 들어간 트립 수
    verdict: str  # "낙관" | "보수" | "근접" — 가정이 실측 대비 어느 쪽인지

    def to_dict(self) -> dict:
        return {
            "scope": self.scope,
            "observed_bp": round(self.observed_bp, 2),
            "assumed_bp": round(self.assumed_bp, 2),
            "n_trips": self.n_trips,
            "n_priced": self.n_priced,
            "verdict": self.verdict,
        }


def compare_spread_cost(
    trips: list[dict],
    spread_rows: list[dict],
    assumed_bp: float,
    *,
    scope: str = "전체",
    max_gap_seconds: float = MAX_SPREAD_SAMPLE_GAP_SECONDS,
    near_pct: float = 0.1,
) -> SpreadCostComparison | None:
    """`trips`(`ledger.round_trips()` 출력) + `spread_rows`(`spread.jsonl` 행) →
    왕복 실측 vs `assumed_bp` 비교. 스프레드 표본을 하나도 못 찾으면(모든 트립이
    `n_priced=0`) **None**(표본 없음) — 수수료만으로는 "스프레드까지 실측했다"는
    이 함수의 계약을 만족하지 않는다.

    entry/exit 양쪽 표본이 다 있으면 평균, 한쪽만 있으면 그 값을 그대로 쓴다
    (entry half + exit half의 산술과 동일 — `_nearest_spread_bp`가 돌려주는
    `spread_bp`는 이미 "전체 스프레드"이므로 절반씩 두 번 더하나 평균 내나
    같은 값이 된다). `near_pct`(기본 10%) 이내 차이는 "근접"으로 판정한다."""
    by_symbol: dict[str, list[dict]] = {}
    for row in spread_rows:
        by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)

    observed_bps: list[float] = []
    for t in trips:
        notional = float(t.get("notional") or 0.0)
        if notional <= 0:
            continue
        rows = by_symbol.get(str(t.get("symbol") or "")) or []
        entry_spread = _nearest_spread_bp(rows, t.get("entry_ts"), max_gap_seconds)
        exit_spread = _nearest_spread_bp(rows, t.get("exit_ts"), max_gap_seconds)
        legs = [s for s in (entry_spread, exit_spread) if s is not None]
        if not legs:
            continue
        fee_bp = float(t.get("fees") or 0.0) / notional * 1e4
        spread_cost_bp = sum(legs) / len(legs)
        observed_bps.append(fee_bp + spread_cost_bp)

    if not observed_bps:
        return None

    observed = sum(observed_bps) / len(observed_bps)
    diff_pct = abs(observed - assumed_bp) / assumed_bp if assumed_bp else 0.0
    if diff_pct <= near_pct:
        verdict = "근접"
    elif observed > assumed_bp:
        verdict = "낙관"  # 실측이 가정보다 비싸다 = 가정이 낙관적이었다
    else:
        verdict = "보수"  # 실측이 가정보다 싸다 = 가정이 보수적이었다

    return SpreadCostComparison(
        scope=scope, observed_bp=observed, assumed_bp=assumed_bp,
        n_trips=len(trips), n_priced=len(observed_bps), verdict=verdict,
    )
