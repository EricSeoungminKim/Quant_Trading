"""실측 슬리피지 리포트 — paper 체결과 스프레드 실측을 이어 붙여
`execution.slippage_bps`(편도, 기본 2.5bp) 가정을 검증한다 (2026-09-06
live-readiness §4).

## 왜 있나

`config/settings.yaml`의 `slippage_bps: 2.5`는 스스로 "실측 데이터로 검증된
값이 아니다"라고 적어둔 추정치다. `server/scripts/spread_sample.sh`/
`spread_sample_us.sh`(2026-08-28, 2026-09-06)가 10분 간격으로 실제 호가
스프레드를 `data/ledger/spread.jsonl`에 쌓아온 지 시간이 지났으니, 이제
paper 체결(`data/state/trades.jsonl`)과 그 스프레드 표본을 이어 붙여 "우리가
가정한 슬리피지가 실제로 맞았나"를 숫자로 답할 수 있다.

## 반쪽 스프레드 = 편도 슬리피지의 실측 근사

paper 브로커(`quant/adapters/execution/paper.py`)는 체결가에 `slippage_bps`
만큼 불리한 방향을 편도로 적용한다 — 마켓오더가 스프레드를 가로질러 체결될
때 실제로 물리는 비용의 근사다. 그래서 이 모듈은 각 체결 시점에 가장 가까운
스프레드 표본의 **절반**(`spread_bp / 2`)을 그 체결의 "실측 반쪽 스프레드"로
본다 — `slippage_bps` 가정과 같은 축(편도, bp)이라 직접 비교할 수 있다.
왕복(매수+매도 두 다리) 환산은 그 값의 2배다.

## 이 모듈은 순수 계산만 한다

`quant.control.ledger`(load_trades)/`quant.collect.spread`(spread.jsonl 적재)와
같은 결로, 파일 I/O는 `quant.apps.cli cmd_slippage_report`가 한다. 여기는
이미 읽어온 리스트만 받는다 — 테스트가 네트워크·파일 없이 전부 오프라인으로
돈다.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SPREAD_LEDGER_PATH = Path("data/ledger/spread.jsonl")

# 스프레드 표본 수집 주기는 10분(spread_sample.sh/spread_sample_us.sh) — 그
# 절반인 5분을 매칭 허용 오차로 둔다. 이보다 먼 표본은 "그 순간의 스프레드"로
# 보지 않는다 — 없는 정밀도를 지어내지 않는다(quant.control.cost_model의
# 같은 원칙, 그쪽은 왕복 트립 entry/exit 매칭이라 15분 허용치를 쓴다 — 이
# 모듈은 개별 체결 단위라 더 좁게 잡는다).
MAX_SPREAD_SAMPLE_GAP_SECONDS = 300.0


def load_spread_rows(path: Path | str = DEFAULT_SPREAD_LEDGER_PATH) -> list[dict]:
    """`spread.jsonl` 로더 — 깨진 줄은 건너뛴다(`ledger.load_trades`와 같은 계약)."""
    import json

    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict) and row.get("symbol"):
                out.append(row)
        except ValueError:
            continue
    return out


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _nearest_spread_bp(rows: list[dict], ts: object, max_gap_seconds: float) -> float | None:
    """`ts`(체결의 ts)에 가장 가까운 `rows`(이미 그 심볼로 필터된 spread.jsonl
    행)의 `spread_bp`. 표본이 없거나 가장 가까운 표본도 `max_gap_seconds`보다
    멀면 None."""
    target = _parse_ts(ts)
    if target is None or not rows:
        return None
    best_bp: float | None = None
    best_gap: float | None = None
    for row in rows:
        cand = _parse_ts(row.get("ts"))
        if cand is None:
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


def realized_half_spreads(
    fills: list[dict], spread_rows: list[dict],
    max_gap_seconds: float = MAX_SPREAD_SAMPLE_GAP_SECONDS,
) -> list[dict]:
    """각 체결에 가장 가까운 스프레드 표본을 붙여 반쪽 스프레드(bp)를 계산한다.

    매칭 못 한 체결(표본 자체가 없거나 `max_gap_seconds`보다 먼 경우)은 결과에서
    빠진다 — 없는 정밀도를 지어내지 않는다. 반환은
    `[{"market", "strategy", "symbol", "half_spread_bp"}, ...]`."""
    by_symbol: dict[str, list[dict]] = {}
    for row in spread_rows:
        by_symbol.setdefault(str(row.get("symbol") or ""), []).append(row)

    out: list[dict] = []
    for f in fills:
        symbol = str(f.get("symbol") or "")
        bp = _nearest_spread_bp(by_symbol.get(symbol) or [], f.get("ts"), max_gap_seconds)
        if bp is None:
            continue
        out.append({
            "market": str(f.get("market") or "?"),
            "strategy": str(f.get("strategy_id") or "?"),
            "symbol": symbol,
            "half_spread_bp": bp / 2.0,
        })
    return out


def _percentile(values: list[float], pct: float) -> float:
    """순위 기반 백분위수(선형 보간). 표본 1개면 그 값 그대로."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (len(s) - 1) * pct
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


@dataclass(frozen=True)
class SlippageRow:
    market: str
    strategy: str
    n: int
    median_half_spread_bp: float
    p90_half_spread_bp: float
    implied_round_trip_bp: float  # median_half_spread_bp * 2

    def to_dict(self) -> dict:
        return {
            "market": self.market,
            "strategy": self.strategy,
            "n": self.n,
            "median_half_spread_bp": round(self.median_half_spread_bp, 2),
            "p90_half_spread_bp": round(self.p90_half_spread_bp, 2),
            "implied_round_trip_bp": round(self.implied_round_trip_bp, 2),
        }


def slippage_report(
    fills: list[dict], spread_rows: list[dict],
    max_gap_seconds: float = MAX_SPREAD_SAMPLE_GAP_SECONDS,
) -> list[SlippageRow]:
    """market/strategy 별 n·중앙값·p90·왕복환산(2026-09-06 live-readiness §4).

    표본이 하나도 매칭되지 않으면 빈 리스트 — "0bp"로 지어내지 않는다.
    market 오름차순, 그 안에서 strategy 오름차순으로 정렬한다(재현 가능한 출력)."""
    matched = realized_half_spreads(fills, spread_rows, max_gap_seconds)
    groups: dict[tuple[str, str], list[float]] = {}
    for row in matched:
        key = (row["market"], row["strategy"])
        groups.setdefault(key, []).append(row["half_spread_bp"])

    out: list[SlippageRow] = []
    for market, strategy in sorted(groups):
        values = groups[(market, strategy)]
        median = statistics.median(values)
        out.append(SlippageRow(
            market=market, strategy=strategy, n=len(values),
            median_half_spread_bp=median,
            p90_half_spread_bp=_percentile(values, 0.90),
            implied_round_trip_bp=median * 2.0,
        ))
    return out


def slippage_report_text(
    rows: list[SlippageRow], assumed_one_way_bp: float,
    title: str = "실측 슬리피지 리포트",
    *, near_pct: float = 0.1,
) -> str:
    """`quant.control.cost_model.compare_spread_cost`와 같은 판정 관례
    (근접/낙관/보수, `near_pct` 기본 10%) — 왕복 실측(median 반쪽 스프레드 x2)을
    설정 가정의 왕복(편도 x2)과 비교한다."""
    assumed_round_trip = assumed_one_way_bp * 2.0
    if not rows:
        return (
            f"📏 {title}: 매칭된 체결-스프레드 표본 없음 "
            f"(설정 가정: 편도 {assumed_one_way_bp:g}bp · 왕복 {assumed_round_trip:g}bp)"
        )
    lines = [f"📏 {title} (설정 가정: 편도 {assumed_one_way_bp:g}bp · 왕복 {assumed_round_trip:g}bp)"]
    for r in rows:
        diff_pct = (
            abs(r.implied_round_trip_bp - assumed_round_trip) / assumed_round_trip
            if assumed_round_trip else 0.0
        )
        if diff_pct <= near_pct:
            verdict = "근접"
        elif r.implied_round_trip_bp > assumed_round_trip:
            verdict = "낙관"  # 실측이 가정보다 비싸다 = 가정이 낙관적이었다
        else:
            verdict = "보수"
        lines.append(
            f"[{r.market}/{r.strategy}] n={r.n} · 중앙값 {r.median_half_spread_bp:.1f}bp"
            f" · p90 {r.p90_half_spread_bp:.1f}bp · 왕복환산 {r.implied_round_trip_bp:.1f}bp"
            f" — {verdict}"
        )
    return "\n".join(lines)
