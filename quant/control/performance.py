"""공개 포트폴리오 사이트용 성과 JSON — `quant.apps.cli publish-performance`의 순수 로직.

**입력은 `data/state/trades.jsonl` 원장 하나**(+ 필요시 `execution` 설정 비용
상수, `real_account_snapshot`은 선택)다. `real_account_snapshot`을 안 줘도
정상 동작한다(현금만으로 폴백) — 이 함수 자체는 파일 I/O를 하지 않는다, 호출부
(CLI)가 있으면 읽어서 넘길 뿐이다. 대시보드 숫자가 원장 하나(+선택 스냅샷)로
재현 가능해야 감사 가능하다.

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
6. paper→real_seeded 경계는 **거래일이 아니라 시각**이다(소유자 지시 2026-09-02).
   이식이 하루 중간에 일어나면(예: 2026-09-01 23:01 KST) 같은 거래일 버킷에
   이식 전 거래와 이식 후 거래가 섞인다 — `trading_day()` 단위로 나누면 이 결함을
   못 잡는다. 경계 시각은 이식 정리 매도 행들의 **최대 ts**(정리가 끝난 순간).
7. 공개 `equity` 곡선은 **경계 이후(real_seeded)만** 보여준다. 경계 이전(paper)
   기록은 숨기지 않되 곡선에서 빼고 `prior_paper`에 사실만 요약해 남긴다 — 소유자
   지시("성과는 실계좌 이식 이후만 보여야 한다")와 정직성 원칙(숨기지 않기)을
   동시에 만족시킨다. 이식 이벤트가 아직 없으면(옛 테스트/이전 동작) `equity`는
   지금까지처럼 전체 paper 기록을 보여준다 — 대체할 real_seeded 구간이 없으니
   숨길 이유가 없다.

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
- **이월 보유(청산하지 않은 포지션)**: 2026-09-01 이식에서 005930(삼성전자) 6주는
  정리 매도하지 않고 그대로 이어받았다(`quant.apps.cli.cmd_seed_real`의
  `KEEP_SYMBOL`). 이 보유의 평가액을 더하지 않으면 시드가 "그때 실제로 가진 전부"보다
  작게 잡혀 수익률이 부풀려진다. `real_account_snapshot`이 주어지면 그 보유의
  `qty*avg_cost`를 시드에 더한다(현재가가 아니라 평단 기준 — 소유자가 그렇게 계산해
  지시했다). 스냅샷이 없으면 0으로 폴백하고 `seed_basis`/`note`에 그 사실을 남긴다 —
  종목코드·수량 자체는 출력에 없고 시드 총액 하나로만 녹인다(공개 안전 규칙).
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

# 2026-09-01 이식에서 청산하지 않고 이어받은 종목 — `quant.apps.cli.cmd_seed_real`의
# KEEP_SYMBOL과 동일한 일회성 결정. 다음 이식이 다른 종목을 유지한다면 이 상수를 고쳐라.
CARRYOVER_POSITION_SYMBOL = "005930"

_KST = ZoneInfo("Asia/Seoul")

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


def _ts_iso(ts: datetime) -> str:
    """사람이 읽을 phases from/to용 — KST, 초 단위."""
    return ts.astimezone(_KST).isoformat(timespec="seconds")


def _is_seeding_liquidation(trade: dict) -> bool:
    return SEEDING_LIQUIDATION_MARKER in str(trade.get("reason") or "")


def _split_excluded(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """(성과에 포함되는 행, 이식 정리로 제외되는 행)."""
    included, excluded = [], []
    for t in trades:
        (excluded if _is_seeding_liquidation(t) else included).append(t)
    return included, excluded


def _boundary_ts(excluded: list[dict]) -> datetime | None:
    """paper→real_seeded 경계 시각 — 이식 정리 매도 행들의 최대 ts(정리가 끝난
    순간부터 새 시대). 이식 이벤트가 없으면 None."""
    if not excluded:
        return None
    return max(_parse_ts(t) for t in excluded)


def _carryover_position_krw(snapshot: dict | None) -> tuple[float, bool]:
    """(이월 보유 평가액 KRW, 스냅샷을 읽었는지).

    스냅샷이 없으면 (0.0, False) — 호출부가 이 사실을 `seed_basis`/note에 남긴다.
    스냅샷은 있지만 이월 종목이 없으면(전량 청산됐다면) (0.0, True) — 이건 "몰라서
    0"이 아니라 "확인했더니 0"이라 폴백이 아니다."""
    if not snapshot:
        return 0.0, False
    for h in snapshot.get("holdings", []):
        if h.get("symbol") == CARRYOVER_POSITION_SYMBOL:
            return float(h["qty"]) * float(h["avg_cost"]), True
    return 0.0, True


def _real_seed_krw(
    excluded: list[dict], carryover_krw: float, carryover_sourced: bool,
) -> tuple[float | None, str, str]:
    """(real_seeded 시대 시드 KRW, seed_basis, note 접미사) — 모듈 docstring
    "알려진 한계" 참고.

    이식 정리 행 중 KRW 풀 최종 상태(`cash_after`, 시간순 마지막 값)에 US 정리
    매도의 체결대금(`qty*price - fee`, USD 풀에 실제 credit된 금액)과 이월 보유
    평가액을 더한다. `cash_after`가 하나도 없으면(구버전 원장) None — 지어내지 않는다.
    """
    ordered = sorted(excluded, key=lambda t: str(t.get("ts", "")))
    krw_component = None
    for t in ordered:
        if t.get("cash_after") is not None:
            krw_component = float(t["cash_after"])
    if krw_component is None:
        return None, "현금만", ""
    usd_component = sum(
        float(t.get("qty", 0) or 0) * float(t.get("price", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "US"
    )
    seed = round(krw_component + usd_component * FX_KRW_PER_USD + carryover_krw)
    if carryover_krw > 0:
        basis = "현금+이월보유"
        suffix = " / 이월 보유 평가액 포함(스냅샷 평단 기준, 종목·수량은 비공개)"
    elif not carryover_sourced:
        basis = "현금만"
        suffix = " / 이월 보유 평가액 미포함 — 실계좌 스냅샷 파일 없어 현금만 집계"
    else:
        basis = "현금만"
        suffix = ""
    return seed, basis, suffix


def _build_phases(
    included: list[dict], excluded: list[dict], boundary_ts: datetime | None,
    carryover_krw: float, carryover_sourced: bool,
) -> list[dict]:
    """phases 목록. `from`/`to`는 사람이 읽을 KST 시각(초 단위)."""
    if boundary_ts is None:
        all_days = sorted({trading_day(_parse_ts(t)) for t in included})
        if not all_days:
            return []
        first_ts = min(_parse_ts(t) for t in included)
        return [{
            "id": "paper", "label": "모의 운용",
            "from": _ts_iso(first_ts), "to": None,
            "seed_krw": PAPER_SEED_KRW,
            "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
        }]

    phases = []
    paper_included = [t for t in included if _parse_ts(t) <= boundary_ts]
    if paper_included:
        first_ts = min(_parse_ts(t) for t in paper_included)
        phases.append({
            "id": "paper", "label": "모의 운용",
            "from": _ts_iso(first_ts), "to": _ts_iso(boundary_ts),
            "seed_krw": PAPER_SEED_KRW,
            "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
        })
    real_seed, seed_basis, seed_note_suffix = _real_seed_krw(
        excluded, carryover_krw, carryover_sourced,
    )
    phases.append({
        "id": "real_seeded", "label": "실계좌 스냅샷 이식",
        "from": _ts_iso(boundary_ts), "to": None,
        "seed_krw": real_seed,
        "seed_basis": seed_basis,
        "note": (
            "실계좌 현금·보유를 이어받아 재시작 (KRW/USD 풀 분리, USD→KRW 환산은 "
            f"고정환율 {FX_KRW_PER_USD} — 2026-09-01 스냅샷)" + seed_note_suffix
        ),
    })
    return phases


def _equity_rows(trades: list[dict], seed: float | None, phase_id: str) -> list[dict]:
    """일별 실현손익(수수료 차감 후) 기반 지분 곡선(단일 phase 분량).

    시가평가(마크투마켓)가 아니라 **실현손익 누적**이다 — 포지션 마크는 이
    함수의 입력(trades.jsonl 단독)으로는 알 수 없다. 매수 체결의 수수료도
    그날 순손익에서 빠진다(수수료는 진입 시점에도 이미 나간 돈).

    `trades`는 호출부가 이미 보여줄 phase(경계 이후만, 또는 경계 자체가 없으면
    전체)로 걸러 넘긴다 — 여기서는 날짜/통화별 합산만 한다."""
    by_day_market: dict[tuple[date, str], dict] = {}
    for t in trades:
        day = trading_day(_parse_ts(t))
        market = str(t.get("market") or "US")
        b = by_day_market.setdefault((day, market), {"gross": 0.0, "fees": 0.0, "n": 0})
        b["n"] += 1
        b["fees"] += float(t.get("fee", 0) or 0)
        if str(t.get("side", "")).upper() != "BUY" and t.get("realized_pnl") is not None:
            b["gross"] += float(t["realized_pnl"])

    days = sorted({d for d, _ in by_day_market})
    cum_krw = 0.0
    rows = []
    for day in days:
        kr = by_day_market.get((day, "KR"), {"gross": 0.0, "fees": 0.0, "n": 0})
        us = by_day_market.get((day, "US"), {"gross": 0.0, "fees": 0.0, "n": 0})
        day_pnl_krw = (kr["gross"] - kr["fees"]) + (us["gross"] - us["fees"]) * FX_KRW_PER_USD
        cum_krw += day_pnl_krw
        day_pct = round(day_pnl_krw / seed * 100, 4) if seed else None
        cum_pct = round(cum_krw / seed * 100, 4) if seed else None
        rows.append({
            "date": day.isoformat(),
            "cum_pct": cum_pct,
            "day_pct": day_pct,
            "fills": kr["n"] + us["n"],
            "phase": phase_id,
        })
    return rows


def _prior_paper_summary(paper_included: list[dict]) -> dict:
    """경계 이전(paper) 기록 요약 — 공개 곡선에서는 빠지지만 숨기지 않는다.
    일별 세부가 아니라 세션수/체결수/누적 순손익(KRW)만 남긴다."""
    if not paper_included:
        return {}
    days = {trading_day(_parse_ts(t)) for t in paper_included}
    net_krw = 0.0
    for t in paper_included:
        market = str(t.get("market") or "US")
        fee = float(t.get("fee", 0) or 0)
        pnl = 0.0
        if str(t.get("side", "")).upper() != "BUY" and t.get("realized_pnl") is not None:
            pnl = float(t["realized_pnl"])
        net = pnl - fee
        if market == "US":
            net *= FX_KRW_PER_USD
        net_krw += net
    return {
        "sessions": len(days),
        "fills": len(paper_included),
        "net_krw": round(net_krw),
        "note": (
            "가상 자본 1천만원 시대 — 실계좌 이식 전 기록이라 현재 곡선에 포함하지 않는다"
        ),
    }


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
    real_account_snapshot: dict | None = None,
) -> dict:
    """`trades.jsonl` 원장(dict 리스트, `ledger.load_trades` 출력) → 공개 성과 JSON.

    순수 함수 — 파일 I/O는 호출부(CLI)가 한다. `real_account_snapshot`은 선택
    (`quant.apps.cli.cmd_seed_real`이 쓰는 것과 같은 형식의 dict) — 없으면 이월
    보유 평가액 없이 현금만으로 계산한다(모듈 docstring "알려진 한계" 참고)."""
    now = now or datetime.now(_KST)
    included, excluded = _split_excluded(trades)
    boundary_ts = _boundary_ts(excluded)
    carryover_krw, carryover_sourced = _carryover_position_krw(real_account_snapshot)
    phases = _build_phases(included, excluded, boundary_ts, carryover_krw, carryover_sourced)

    if boundary_ts is not None:
        # 경계가 있으면 공개 곡선은 경계 이후(real_seeded)만 — 경계 이전은 곡선에서
        # 빼고 prior_paper에 요약만 남긴다(모듈 docstring 규칙 7).
        paper_included = [t for t in included if _parse_ts(t) <= boundary_ts]
        real_included = [t for t in included if _parse_ts(t) > boundary_ts]
        visible_seed = next((p["seed_krw"] for p in phases if p["id"] == "real_seeded"), None)
        equity = _equity_rows(real_included, visible_seed, "real_seeded")
        prior_paper = _prior_paper_summary(paper_included)
    else:
        # 이식 이벤트가 아직 없으면 대체할 real_seeded 구간이 없다 — 지금까지처럼
        # 전체 paper 기록을 그대로 보여준다(숨길 이유가 없다).
        visible_seed = phases[0]["seed_krw"] if phases else None
        equity = _equity_rows(included, visible_seed, "paper")
        prior_paper = {}

    strategies = _strategy_stats(included)

    days = [row["date"] for row in equity]
    # total_fills 는 **곡선에 실린 구간의 체결 수**여야 한다 — start/end/sessions 가
    # 이미 그 구간 기준이기 때문. 2026-09-02 실측: 이식 후 2거래일인데 체결을
    # 전체(549 모의 + 53 이식후 = 602)로 세어 히어로에 "2 Trading days / 602 Fills"
    # 라는 모순된 숫자가 찍혔다. 이식 전 체결 수는 prior_paper.fills 에 따로 있다.
    period = {
        "start": days[0] if days else None,
        "end": days[-1] if days else None,
        "sessions": len(days),
        "total_fills": sum(int(row.get("fills") or 0) for row in equity),
    }

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "disclaimer": "모의투자(paper) 기록입니다. 실제 자금이 투입되지 않았습니다.",
        "period": period,
        "phases": phases,
        "equity": equity,
        "prior_paper": prior_paper,
        "strategies": strategies,
        "excluded": _excluded_summary(excluded),
        "costs": _costs(execution_cfg),
    }
