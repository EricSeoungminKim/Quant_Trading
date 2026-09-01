"""공개 포트폴리오 사이트용 성과 JSON — `quant.apps.cli publish-performance`의 순수 로직.

**입력은 `data/state/trades.jsonl` 원장 하나뿐**(+ 필요시 `execution` 설정 비용
상수). 다른 파일(포지션 마크, 자본 곡선 원장 등)을 읽지 않는다 — 대시보드 숫자가
원장 하나로 재현 가능해야 감사 가능하다.

## 정직성 규칙(호출부/유지보수자를 위한 요약, 상세는 각 함수 docstring)

1. 손익은 항상 수수료 차감 후(`ledger.round_trips`가 이미 그렇게 낸다).
2. 통화 혼합 금지 — KR은 원화, US는 달러로 각각 집계한 뒤, `equity`의 KRW 환산에만
   고정 환율 `FX_KRW_PER_USD`(2026-09-01 실계좌 이식 스냅샷 환율)를 쓴다.
3. 실계좌 이식 시 물려받은 레거시 포지션 정리 매도(reason에 `SEEDING_LIQUIDATION_MARKER`
   포함)는 프로그램의 매매 판단이 아니므로 성과 계산에서 빼고 `excluded`에만 집계한다.
4. 전략별 통계는 `ledger.round_trips`/`ledger._wilson_ci`/`ledger._verdict`를
   재사용한다(중복 구현 금지).
5. 날짜 귀속은 `quant.core.models.trading_day`(KST 08:00 경계) 그대로 쓴다 —
   이미 "그날 KR장 + 그날 밤 US장 = 하루"를 정확히 구현하고 있다.

## `seed_krw`에 대한 알려진 한계 (정직하게 밝힌다)

- **paper 시대**: 최초 paper 계좌 시작 자본은 원장에 남아 있지 않다(가장 오래된
  체결에도 `cash_after`가 없다 — 그 필드는 2026-08-11에 추가됐다). `PAPER_SEED_KRW`는
  이 저장소가 반복적으로 써 온 "전략당 1천만원" 관례에서 가져온 **고정 참고값**이지
  원장에서 유도한 값이 아니다 — 틀렸다면 이 상수를 고쳐라.
- **real_seeded 시대**: 실계좌 이식 정리 매도 행들의 `cash_after`(KRW 현금 풀,
  이식 시점 dual_currency 모드에서도 이 필드는 항상 KRW 풀 값)와, US 정리 매도의
  체결대금(`qty*price - fee`, USD 풀에 실제로 credit된 금액과 동일 — paper.py 참고)을
  원장에서 직접 더해 구한다. **USD 풀의 이식 이전 잔액이 0이었다는 사실**(2026-09-01
  변경기록: "US 5종목+USD 0")에 의존한다 — 원장 자체에는 이 사전 잔액이 없어 확인할
  방법이 없다. 이 가정이 깨지면(다음 이식 이벤트가 USD 잔액이 있는 채로 시작하면)
  이 함수가 조용히 과소평가한다 — 그럴 땐 이 함수를 갱신해라.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from quant.control.ledger import MIN_TRIPS_FOR_JUDGEMENT, _verdict, _wilson_ci, round_trips
from quant.core.models import trading_day

__all__ = ["build_performance_payload"]

# 2026-09-01 실계좌 이식 스냅샷 환율(소유자 지시: "원화는 원화, 달러는 달러로만 —
# 환전 금지"). equity 곡선의 KRW 환산 전용 고정값 — 그날그날의 실제 환율이 아니다.
FX_KRW_PER_USD = 1376.7

# `quant.apps.cli seed_real.cmd_seed_real`이 남기는 마커(REASON 상수와 동일 문자열).
# 이 부분 문자열만 맞으면 되므로 상수를 그쪽과 공유하지 않아도(제어 평면이 apps를
# 임포트하지 않는다는 원칙, `quant/control/CLAUDE.md` 없음이나 기존 관례 유지) 안전하다.
SEEDING_LIQUIDATION_MARKER = "실계좌 이식 정리"

# paper 시대 참고 시드 — 위 모듈 docstring "알려진 한계" 참고.
PAPER_SEED_KRW = 10_000_000

# 전략 id → 한글 표시명. 종목/파라미터는 절대 넣지 않는다 — 공개 대시보드용.
STRATEGY_NAME_KO: dict[str, str] = {
    "donchian": "돈치안 채널 추세추종",
    "orb_scan": "개장 돌파 스캐너",
    "intraday_scan": "장중 신고가 스캐너",
    "scalp_1m": "1분봉 스캘핑",
    "frgn_accumulate": "외국인 수급 적립매수",
    "news_momentum": "뉴스 모멘텀",
    "news_scalp": "뉴스 스캘프",
    "confluence": "복합 신호 합류",
    "cross_momentum": "교차 모멘텀",
    "mean_reversion": "평균회귀",
    "overnight_drift": "오버나이트 드리프트",
    "pullback_impulse": "눌림목 임펄스",
    "llm_trader": "AI 트레이더",
    "close_bet": "종가배팅",
    "orb": "개장 범위 돌파",
    "vol_breakout": "변동성 돌파",
    "mr_vwap_quiet": "VWAP 평균회귀(저변동)",
    "intraday_momentum": "장중 모멘텀",
    "gap_fade": "갭 페이드",
    "rsi2_dip": "RSI2 눌림목",
}


def _parse_ts(trade: dict) -> datetime:
    ts = datetime.fromisoformat(str(trade.get("ts")))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # 원장은 항상 오프셋 포함 — 방어적 처리
    return ts


def _is_seeding_liquidation(trade: dict) -> bool:
    return SEEDING_LIQUIDATION_MARKER in str(trade.get("reason") or "")


def _split_excluded(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """(성과에 포함되는 행, 이식 정리로 제외되는 행)."""
    included, excluded = [], []
    for t in trades:
        (excluded if _is_seeding_liquidation(t) else included).append(t)
    return included, excluded


def _real_seed_krw(excluded: list[dict]) -> float | None:
    """real_seeded 시대 시드(KRW) — 모듈 docstring "알려진 한계" 참고.

    이식 정리 행 중 KRW 풀 최종 상태(`cash_after`, 시간순 마지막 값)에 US 정리
    매도의 체결대금(`qty*price - fee`, USD 풀에 실제 credit된 금액)을 환산해 더한다.
    `cash_after`가 하나도 없으면(구버전 원장) None — 지어내지 않는다.
    """
    ordered = sorted(excluded, key=lambda t: str(t.get("ts", "")))
    krw_component = None
    for t in ordered:
        if t.get("cash_after") is not None:
            krw_component = float(t["cash_after"])
    if krw_component is None:
        return None
    usd_component = sum(
        float(t.get("qty", 0) or 0) * float(t.get("price", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "US"
    )
    return round(krw_component + usd_component * FX_KRW_PER_USD)


def _build_phases(included: list[dict], excluded: list[dict]) -> tuple[list[dict], date | None]:
    """(phases 목록, real_seeded 시작 거래일 — 이식 이벤트가 없으면 None)."""
    all_days = sorted({trading_day(_parse_ts(t)) for t in included})
    if not excluded:
        phases = []
        if all_days:
            phases.append({
                "id": "paper", "label": "모의 운용",
                "from": all_days[0].isoformat(), "to": None,
                "seed_krw": PAPER_SEED_KRW,
                "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
            })
        return phases, None

    boundary = min(trading_day(_parse_ts(t)) for t in excluded)
    phases = []
    paper_days = [d for d in all_days if d < boundary]
    if paper_days:
        phases.append({
            "id": "paper", "label": "모의 운용",
            "from": paper_days[0].isoformat(), "to": boundary.isoformat(),
            "seed_krw": PAPER_SEED_KRW,
            "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
        })
    real_seed = _real_seed_krw(excluded)
    phases.append({
        "id": "real_seeded", "label": "실계좌 스냅샷 이식",
        "from": boundary.isoformat(), "to": None,
        "seed_krw": real_seed,
        "note": (
            f"실계좌 현금·보유를 이어받아 재시작 (KRW/USD 풀 분리, USD→KRW 환산은 "
            f"고정환율 {FX_KRW_PER_USD} — 2026-09-01 스냅샷)"
        ),
    })
    return phases, boundary


def _phase_id_for(day: date, boundary: date | None) -> str:
    if boundary is None or day < boundary:
        return "paper"
    return "real_seeded"


def _equity_rows(included: list[dict], phases: list[dict], boundary: date | None) -> list[dict]:
    """일별 실현손익(수수료 차감 후) 기반 지분 곡선.

    시가평가(마크투마켓)가 아니라 **실현손익 누적**이다 — 포지션 마크는 이
    함수의 입력(trades.jsonl 단독)으로는 알 수 없다. 매수 체결의 수수료도
    그날 순손익에서 빠진다(수수료는 진입 시점에도 이미 나간 돈).
    """
    seed_by_phase = {p["id"]: p["seed_krw"] for p in phases}
    by_day_market: dict[tuple[date, str], dict] = {}
    for t in included:
        day = trading_day(_parse_ts(t))
        market = str(t.get("market") or "US")
        b = by_day_market.setdefault((day, market), {"gross": 0.0, "fees": 0.0, "n": 0})
        b["n"] += 1
        b["fees"] += float(t.get("fee", 0) or 0)
        if str(t.get("side", "")).upper() != "BUY" and t.get("realized_pnl") is not None:
            b["gross"] += float(t["realized_pnl"])

    days = sorted({d for d, _ in by_day_market})
    cum_krw_by_phase: dict[str, float] = {}
    rows = []
    for day in days:
        phase = _phase_id_for(day, boundary)
        kr = by_day_market.get((day, "KR"), {"gross": 0.0, "fees": 0.0, "n": 0})
        us = by_day_market.get((day, "US"), {"gross": 0.0, "fees": 0.0, "n": 0})
        day_pnl_krw = (kr["gross"] - kr["fees"]) + (us["gross"] - us["fees"]) * FX_KRW_PER_USD
        seed = seed_by_phase.get(phase)
        cum_krw_by_phase[phase] = cum_krw_by_phase.get(phase, 0.0) + day_pnl_krw
        day_pct = round(day_pnl_krw / seed * 100, 4) if seed else None
        cum_pct = round(cum_krw_by_phase[phase] / seed * 100, 4) if seed else None
        rows.append({
            "date": day.isoformat(),
            "cum_pct": cum_pct,
            "day_pct": day_pct,
            "fills": kr["n"] + us["n"],
            "phase": phase,
        })
    return rows


def _strategy_stats(included: list[dict]) -> list[dict]:
    """전략별 승률/기대값 — `ledger.round_trips` + Wilson CI(`ledger._wilson_ci`)를
    그대로 재사용한다. `legacy`(이식 정리 전용 strategy_id)는 애초에 매수 체결이
    없어 라운드트립이 절대 종결되지 않으므로 `round_trips`가 자연히 걸러낸다."""
    trips = round_trips(included)
    stats = []
    for sid in sorted({t["strategy"] for t in trips}):
        strip = [t for t in trips if t["strategy"] == sid]
        known = [t for t in strip if t["pnl_known"]]
        if not known:
            continue
        n = len(known)
        wins = [t for t in known if t["pnl"] > 0]
        losses = [t for t in known if t["pnl"] <= 0]
        wr = len(wins) / n
        lower, upper = _wilson_ci(len(wins), n)
        aw = sum(t["bps"] for t in wins) / len(wins) if wins else 0.0
        al = abs(sum(t["bps"] for t in losses) / len(losses)) if losses else 0.0
        expectancy = wr * aw - (1 - wr) * al
        stats.append({
            "id": sid,
            "name_ko": STRATEGY_NAME_KO.get(sid, sid),
            "trips": n,
            "wins": len(wins),
            "win_rate": round(wr, 4),
            "ci_low": round(lower, 4),
            "ci_high": round(upper, 4),
            "expectancy_bp": round(expectancy, 2),
            "verdict": _verdict(lower, upper),
            "sample_warning": n < MIN_TRIPS_FOR_JUDGEMENT,
            "markets": sorted({t["market"] for t in known}),
        })
    return stats


def _excluded_summary(excluded: list[dict]) -> dict:
    if not excluded:
        return {}
    krw_impact = sum(
        float(t.get("realized_pnl", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "KR"
    )
    usd_impact = sum(
        float(t.get("realized_pnl", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "US"
    )
    return {
        "seeding_liquidation": {
            "fills": len(excluded),
            "note": (
                "이식 시 물려받은 레거시 포지션 정리 — 프로그램의 매매 판단이 "
                "아니므로 성과에서 제외"
            ),
            "krw_impact": round(krw_impact, 2),
            "usd_impact": round(usd_impact, 2),
        }
    }


def _costs(execution_cfg: dict) -> dict:
    """왕복(편도×2) 비용 참고표 — `config/settings.yaml`의 `execution` 블록에서
    유도. 실측(`quant.control.cost_model`)이 아니라 **설정상 가정치**다."""
    fee_bps = execution_cfg.get("fee_bps", 0.0)
    if isinstance(fee_bps, dict):
        kr_fee = float(fee_bps.get("KR", 0.0))
        us_fee = float(fee_bps.get("US", 0.0))
    else:
        kr_fee = us_fee = float(fee_bps or 0.0)
    kr_sell_tax = float(execution_cfg.get("kr_stock_sell_tax_bps", 0.0))

    kr_etf_roundtrip = round(kr_fee * 2, 2)
    kr_stock_roundtrip = round(kr_fee * 2 + kr_sell_tax, 2)
    us_roundtrip = round(us_fee * 2, 2)
    return {
        "kr_stock_roundtrip_bp": kr_stock_roundtrip,
        "kr_etf_roundtrip_bp": kr_etf_roundtrip,
        "us_roundtrip_bp": us_roundtrip,
        "note": (
            f"KR 개별주 왕복 {kr_stock_roundtrip:g}bp 중 {kr_sell_tax:g}bp가 "
            "증권거래세+농특세 (US SEC Fee/TAF 등 소액 항목은 미포함)"
        ),
    }


def build_performance_payload(
    trades: list[dict], execution_cfg: dict, *, now: datetime | None = None,
) -> dict:
    """`trades.jsonl` 원장(dict 리스트, `ledger.load_trades` 출력) → 공개 성과 JSON.

    순수 함수 — 파일 I/O는 호출부(CLI)가 한다."""
    now = now or datetime.now(ZoneInfo("Asia/Seoul"))
    included, excluded = _split_excluded(trades)
    phases, boundary = _build_phases(included, excluded)
    equity = _equity_rows(included, phases, boundary)
    strategies = _strategy_stats(included)

    days = [row["date"] for row in equity]
    period = {
        "start": days[0] if days else None,
        "end": days[-1] if days else None,
        "sessions": len(days),
        "total_fills": len(included),
    }

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "disclaimer": "모의투자(paper) 기록입니다. 실제 자금이 투입되지 않았습니다.",
        "period": period,
        "phases": phases,
        "equity": equity,
        "strategies": strategies,
        "excluded": _excluded_summary(excluded),
        "costs": _costs(execution_cfg),
    }
