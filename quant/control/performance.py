"""공개 포트폴리오 사이트용 성과 JSON — `quant.apps.cli publish-performance`의 순수 로직.

**입력은 `data/state/trades.jsonl` 원장 하나**(+ 필요시 `execution` 설정 비용
상수, `real_account_snapshot`은 선택)다. `real_account_snapshot`을 안 줘도
정상 동작한다(현금만으로 폴백) — 이 함수 자체는 파일 I/O를 하지 않는다, 호출부
(CLI)가 있으면 읽어서 넘길 뿐이다. 대시보드 숫자가 원장 하나(+선택 스냅샷)로
재현 가능해야 감사 가능하다.

## 정직성 규칙(호출부/유지보수자를 위한 요약, 상세는 각 함수 docstring)

1. 손익은 항상 수수료 차감 후(`ledger.round_trips`가 이미 그렇게 낸다).
2. 통화 혼합 금지 — 지분곡선은 `equity_asia`(KRW)/`equity_us`(USD)로 분리돼
   있고 **각자 통화 그대로** 집계한다(소유자 지시 2026-09-02: 이식 후 지갑이
   물리적으로 분리돼 있어 FX 환산 없이 각자 통화로 정규화하는 게 더 정확하다).
   `FX_KRW_PER_USD`(2026-09-01 실계좌 이식 스냅샷 환율)는 지분곡선 계산엔 전혀
   쓰이지 않고, `phases[].seed_krw`(총자산을 KRW 한 숫자로 요약하는 서술용)와
   `prior_paper.net_krw`/`excluded` 요약처럼 "총액을 KRW로 뭉뚱그려 보여주는"
   곳에서만 쓴다.
3. 실계좌 이식 시 물려받은 레거시 포지션 정리 매도(reason에 `SEEDING_LIQUIDATION_MARKER`
   포함)는 프로그램의 매매 판단이 아니므로 성과 계산에서 빼고 `excluded`에만 집계한다.
4. 전략별 통계는 `ledger.round_trips`/`ledger._wilson_ci`/`ledger._verdict`를
   재사용한다(중복 구현 금지). `strategies[].total`은 통화 무관 합산, `by_market`은
   KR/US 각각 30건(`MIN_TRIPS_FOR_JUDGEMENT`) 표본 임계까지 독립 적용.
5. 날짜 귀속은 `quant.core.models.trading_day`(KST 08:00 경계) 그대로 쓴다 —
   이미 "그날 KR장 + 그날 밤 US장 = 하루"를 정확히 구현하고 있다.
6. paper→real_seeded 경계는 **거래일이 아니라 시각**이다(소유자 지시 2026-09-02).
   이식이 하루 중간에 일어나면(예: 2026-09-01 23:01 KST) 같은 거래일 버킷에
   이식 전 거래와 이식 후 거래가 섞인다 — `trading_day()` 단위로 나누면 이 결함을
   못 잡는다. 경계 시각은 이식 정리 매도 행들의 **최대 ts**(정리가 끝난 순간).
7. 공개 지분곡선(`equity_asia`/`equity_us`)은 **경계 이후(real_seeded)만**
   보여준다. 경계 이전(paper) 기록은 숨기지 않되 곡선에서 빼고 `prior_paper`에
   사실만 요약해 남긴다 — 소유자 지시("성과는 실계좌 이식 이후만 보여야 한다")와
   정직성 원칙(숨기지 않기)을 동시에 만족시킨다. 이식 이벤트가 아직 없으면(옛
   테스트/이전 동작) 지분곡선은 지금까지처럼 전체 paper 기록을 보여준다 —
   대체할 real_seeded 구간이 없으니 숨길 이유가 없다.
8. 사용자 노출 문구는 전부 `_en` 짝을 함께 낸다(`disclaimer_en`, `phases[].note_en`,
   `phases[].label_en`, `phases[].seed_basis_en`, `equity_*.seed_basis_en`,
   `prior_paper.note_en`, `excluded.seeding_liquidation.note_en`, `costs.note_en`) —
   프론트가 로케일별로 고르되, 한글 원문 강도를 낮추지 않는다.
9. 렌더 준비 완료(오너 지시 2026-09-02) — 누적/일별 % 수익률, 승률·Wilson CI,
   y축 범위·눈금(`equity_*.chart.y_axis`), phase 경계 인덱스
   (`equity_*.chart.phase_boundaries`)까지 전부 이 모듈이 계산해 낸다. 프론트에
   남기는 건 로케일 날짜 포맷·부호/퍼센트 기호·색상 결정·SVG 좌표 매핑(x축 라벨
   솎아내기 포함, 렌더된 컨테이너 픽셀 폭에 의존해 서버가 미리 알 수 없다)뿐이다.

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

from quant.control.cost_model import round_trip_bp_from_settings
from quant.control.ledger import (
    MIN_TRIPS_FOR_JUDGEMENT,
    SEEDING_LIQUIDATION_MARKER,
    _verdict,
    _wilson_ci,
    base_strategy_id,
    is_seeding_liquidation,
    round_trips,
)
from quant.core.models import trading_day

__all__ = ["build_performance_payload"]

# 2026-09-01 실계좌 이식 스냅샷 환율(소유자 지시: "원화는 원화, 달러는 달러로만 —
# 환전 금지"). equity 곡선의 KRW 환산 전용 고정값 — 그날그날의 실제 환율이 아니다.
FX_KRW_PER_USD = 1376.7

# `SEEDING_LIQUIDATION_MARKER`/`_is_seeding_liquidation`은 2026-09-02에
# `quant.control.ledger`로 옮겼다 — 세션 손익(`session_pnl_summary`)이 같은
# 판별식을 쓰지 않아 텔레그램만 이식 정리를 성과로 세고 있었다(ledger.py 참고).
# 여기서는 기존 임포트 경로를 깨지 않기 위해 이름만 다시 내건다.

# paper 시대 참고 시드 — 위 모듈 docstring "알려진 한계" 참고.
PAPER_SEED_KRW = 10_000_000

# paper 시대는 KRW/USD 풀이 물리적으로 분리돼 있지 않았다(단일 가상 계좌를 FX로
# KRW 환산해 다뤘다) — 시장별 분리 지분곡선(오너 지시 2026-09-02)을 그 시절에도
# 그리려면 US 북에 쓸 자기 통화 기준 시드가 필요한데, 원장엔 그런 값이 없다.
# PAPER_SEED_KRW 를 고정환율로 나눈 **가상 참고치**일 뿐, 실제로 존재했던 USD
# 잔고가 아니다 — real_seeded 시대(경계 이후)로 넘어가면 이 상수는 안 쓰인다.
PAPER_SEED_USD = round(PAPER_SEED_KRW / FX_KRW_PER_USD)

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


def _strategy_name_ko(sid: str) -> str:
    """전략 id → 한글 표시명. A/B 촉매 갈래(`<id>_cat`, 2026-09-03)는 기준
    전략의 이름을 **상속**하고 꼬리표만 붙인다 — 같은 클래스를 다른 유니버스로
    돌리는 갈래이므로 이름을 따로 짓는 것이 오히려 거짓말이다. 공개 대시보드에
    id 원문이 그대로 노출되는 것도 막는다."""
    if sid in STRATEGY_NAME_KO:
        return STRATEGY_NAME_KO[sid]
    base = base_strategy_id(sid)
    if base != sid and base in STRATEGY_NAME_KO:
        return f"{STRATEGY_NAME_KO[base]}(촉매 갈래)"
    return sid


def _parse_ts(trade: dict) -> datetime:
    ts = datetime.fromisoformat(str(trade.get("ts")))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # 원장은 항상 오프셋 포함 — 방어적 처리
    return ts


def _ts_iso(ts: datetime) -> str:
    """사람이 읽을 phases from/to용 — KST, 초 단위."""
    return ts.astimezone(_KST).isoformat(timespec="seconds")


_is_seeding_liquidation = is_seeding_liquidation


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
) -> tuple[float | None, str, str, str, str]:
    """(real_seeded 시대 시드 KRW, seed_basis, seed_basis_en, note 접미사(ko),
    note 접미사(en)) — 모듈 docstring "알려진 한계" 참고.

    이식 정리 행 중 KRW 풀 최종 상태(`cash_after`, 시간순 마지막 값)에 US 정리
    매도의 체결대금(`qty*price - fee`, USD 풀에 실제 credit된 금액)과 이월 보유
    평가액을 더한다. `cash_after`가 하나도 없으면(구버전 원장) None — 지어내지 않는다.

    이 값은 phases 스텝퍼 카드에 쓰는 **총자산(KRW 환산)** 서술용이다 — 시장별
    분리 지분곡선(`_asia_seed_krw`/`_us_seed_usd`)과는 목적이 다르다: 여기는
    "그때 실제로 가진 전부가 얼마였나"를 KRW 한 숫자로 요약하는 것이라 FX
    환산이 맞다. 지분곡선은 "각자 통화 기준 수익률"이라 FX를 안 쓴다.
    """
    ordered = sorted(excluded, key=lambda t: str(t.get("ts", "")))
    krw_component = None
    for t in ordered:
        if t.get("cash_after") is not None:
            krw_component = float(t["cash_after"])
    if krw_component is None:
        return None, "현금만", "cash only", "", ""
    usd_component = sum(
        float(t.get("qty", 0) or 0) * float(t.get("price", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "US"
    )
    seed = round(krw_component + usd_component * FX_KRW_PER_USD + carryover_krw)
    if carryover_krw > 0:
        basis = "현금+이월보유"
        basis_en = "cash + carried position"
        suffix = " / 이월 보유 평가액 포함(스냅샷 평단 기준, 종목·수량은 비공개)"
        suffix_en = (
            " / Includes carried-over holdings valuation (based on snapshot "
            "average cost; symbol and quantity are not disclosed)"
        )
    elif not carryover_sourced:
        basis = "현금만"
        basis_en = "cash only"
        suffix = " / 이월 보유 평가액 미포함 — 실계좌 스냅샷 파일 없어 현금만 집계"
        suffix_en = (
            " / Carried-over holdings valuation not included — no real-account "
            "snapshot file, cash only"
        )
    else:
        basis = "현금만"
        basis_en = "cash only"
        suffix = ""
        suffix_en = ""
    return seed, basis, basis_en, suffix, suffix_en


def _asia_seed_krw(
    excluded: list[dict], carryover_krw: float, carryover_sourced: bool,
) -> tuple[float | None, str, str]:
    """(아시아(KRW) 북 지분곡선 시드, seed_basis, seed_basis_en).

    KRW 풀 최종 `cash_after` + 이월 보유 평가액만 — USD 는 섞지 않는다(오너
    지시 2026-09-02: 이식 후 지갑이 물리적으로 분리돼 있어 FX 환산 없이 각자
    통화로 정규화하는 게 더 정확하다). `_real_seed_krw`(phases 총자산 서술용)와
    달리 US 이식대금을 더하지 않는다."""
    ordered = sorted(excluded, key=lambda t: str(t.get("ts", "")))
    krw_component = None
    for t in ordered:
        if t.get("cash_after") is not None:
            krw_component = float(t["cash_after"])
    if krw_component is None:
        return None, "현금만", "cash only"
    seed = round(krw_component + carryover_krw)
    if carryover_krw > 0:
        return seed, "현금+이월보유", "cash + carried position"
    return seed, "현금만", "cash only"


def _us_seed_usd(excluded: list[dict]) -> float:
    """미국(USD) 북 지분곡선 시드 — 이식 정리 매도 중 US 체결의 체결대금
    (`qty*price - fee`) 합, FX 미적용. US 레거시는 전량 청산(이월 보유 없음,
    모듈 docstring "알려진 한계" 참고)이라 캐리오버 개념이 없다 — basis 는
    항상 "현금만"이다. 이식 이벤트에 US 정리 매도가 없으면(예: KR 전용
    이식) 0.0 — "몰라서 0"이 아니라 "그 이식에서 USD 풀에 새로 들어온 돈이
    없었다"는 뜻이며, 0인 시드로는 지분곡선의 %를 계산할 수 없어(0으로
    나누기) day_pct/cum_pct 가 None 이 된다."""
    usd = sum(
        float(t.get("qty", 0) or 0) * float(t.get("price", 0) or 0) - float(t.get("fee", 0) or 0)
        for t in excluded if str(t.get("market")) == "US"
    )
    return round(usd, 2)


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
            "id": "paper", "label": "모의 운용", "label_en": "Paper trading",
            "from": _ts_iso(first_ts), "to": None,
            "seed_krw": PAPER_SEED_KRW,
            "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
            "note_en": (
                "Virtual capital of 10,000,000 KRW (fixed reference value — "
                "no starting capital recorded in the ledger)"
            ),
        }]

    phases = []
    paper_included = [t for t in included if _parse_ts(t) <= boundary_ts]
    if paper_included:
        first_ts = min(_parse_ts(t) for t in paper_included)
        phases.append({
            "id": "paper", "label": "모의 운용", "label_en": "Paper trading",
            "from": _ts_iso(first_ts), "to": _ts_iso(boundary_ts),
            "seed_krw": PAPER_SEED_KRW,
            "note": "가상 자본 1천만원(고정 참고값 — 원장에 시작 자본 기록 없음)",
            "note_en": (
                "Virtual capital of 10,000,000 KRW (fixed reference value — "
                "no starting capital recorded in the ledger)"
            ),
        })
    real_seed, seed_basis, seed_basis_en, seed_note_suffix, seed_note_suffix_en = _real_seed_krw(
        excluded, carryover_krw, carryover_sourced,
    )
    phases.append({
        "id": "real_seeded", "label": "실계좌 스냅샷 이식",
        "label_en": "Live-account snapshot transplant",
        "from": _ts_iso(boundary_ts), "to": None,
        "seed_krw": real_seed,
        "seed_basis": seed_basis,
        "seed_basis_en": seed_basis_en,
        "note": (
            "실계좌 현금·보유를 이어받아 재시작 (KRW/USD 풀 분리, USD→KRW 환산은 "
            f"고정환율 {FX_KRW_PER_USD} — 2026-09-01 스냅샷)" + seed_note_suffix
        ),
        "note_en": (
            "Restarted by carrying over real-account cash and holdings (KRW/USD "
            "pools kept separate; USD→KRW conversion uses a fixed rate of "
            f"{FX_KRW_PER_USD} — 2026-09-01 snapshot)" + seed_note_suffix_en
        ),
    })
    return phases


def _equity_rows(trades: list[dict], seed: float | None, phase_id: str, market: str) -> list[dict]:
    """일별 실현손익(수수료 차감 후) 기반 지분 곡선 — 단일 phase, 단일 market 분량.

    시가평가(마크투마켓)가 아니라 **실현손익 누적**이다 — 포지션 마크는 이
    함수의 입력(trades.jsonl 단독)으로는 알 수 없다. 매수 체결의 수수료도
    그날 순손익에서 빠진다(수수료는 진입 시점에도 이미 나간 돈).

    오너 지시(2026-09-02): 아시아(KR)/미국(US) 북은 물리적으로 분리된 통화
    풀이라 **FX 환산 없이 각자 통화 그대로** 집계하고, `seed`도 그 통화
    기준이어야 한다 — 이 함수 자체가 한 시장만 본다(FX_KRW_PER_USD를 아예
    참조하지 않는다). `seed`가 0/None이면 나눌 기준이 없어 pct는 None.

    `trades`는 호출부가 이미 보여줄 phase(경계 이후만, 또는 경계 자체가 없으면
    전체)로 걸러 넘긴다 — 여기서는 날짜별 합산 + market 필터만 한다."""
    by_day: dict[date, dict] = {}
    for t in trades:
        if str(t.get("market") or "US") != market:
            continue
        day = trading_day(_parse_ts(t))
        b = by_day.setdefault(day, {"gross": 0.0, "fees": 0.0, "n": 0})
        b["n"] += 1
        b["fees"] += float(t.get("fee", 0) or 0)
        if str(t.get("side", "")).upper() != "BUY" and t.get("realized_pnl") is not None:
            b["gross"] += float(t["realized_pnl"])

    days = sorted(by_day)
    cum = 0.0
    rows = []
    for day in days:
        b = by_day[day]
        day_pnl = b["gross"] - b["fees"]
        cum += day_pnl
        day_pct = round(day_pnl / seed * 100, 4) if seed else None
        cum_pct = round(cum / seed * 100, 4) if seed else None
        rows.append({
            "date": day.isoformat(),
            "cum_pct": cum_pct,
            "day_pct": day_pct,
            "fills": b["n"],
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
        "note_en": (
            "From the virtual-capital (10,000,000 KRW) era — predates the "
            "real-account transition, so it is excluded from the current curve"
        ),
    }


def _round_trip_stats(known: list[dict]) -> dict:
    """라운드트립 목록(`pnl_known`만) → 승률/Wilson CI/기대값 블록.
    `_strategy_stats`가 total(통화 무관 합산)과 by_market(시장별) 양쪽에
    이 블록을 재사용한다 — `MIN_TRIPS_FOR_JUDGEMENT` 임계는 항상 자기
    표본 `n` 기준이라 by_market 호출분은 시장별로 각각 30건 임계가 걸린다."""
    n = len(known)
    wins = [t for t in known if t["pnl"] > 0]
    losses = [t for t in known if t["pnl"] <= 0]
    wr = len(wins) / n
    lower, upper = _wilson_ci(len(wins), n)
    aw = sum(t["bps"] for t in wins) / len(wins) if wins else 0.0
    al = abs(sum(t["bps"] for t in losses) / len(losses)) if losses else 0.0
    expectancy = wr * aw - (1 - wr) * al
    return {
        "trips": n,
        "wins": len(wins),
        "win_rate": round(wr, 4),
        "ci_low": round(lower, 4),
        "ci_high": round(upper, 4),
        "expectancy_bp": round(expectancy, 2),
        "verdict": _verdict(lower, upper),
        "sample_warning": n < MIN_TRIPS_FOR_JUDGEMENT,
    }


def _trip_hold_minutes(trip: dict) -> float | None:
    """트립 보유 분. entry/exit ts 중 하나라도 못 읽으면 None."""
    try:
        entry = datetime.fromisoformat(str(trip.get("entry_ts")))
        exit_ = datetime.fromisoformat(str(trip.get("exit_ts")))
    except ValueError:
        return None
    if entry.tzinfo is None:
        entry = entry.replace(tzinfo=timezone.utc)
    if exit_.tzinfo is None:
        exit_ = exit_.replace(tzinfo=timezone.utc)
    return (exit_ - entry).total_seconds() / 60.0


def _strategy_stats(trades: list[dict], strategies_cfg: dict | None = None) -> list[dict]:
    """전략별 승률/기대값 — `ledger.round_trips` + Wilson CI(`ledger._wilson_ci`)를
    그대로 재사용한다.

    **입력은 원장 전체**(이식 정리 행 포함)다 — `round_trips`가 그 행들을 스스로
    걸러내면서 동시에 **이식 시대 경계**로 쓰기 때문이다(2026-09-02, ledger.py
    `round_trips` docstring). 정리 행을 미리 빼서 넘기면 경계를 못 찾아 이식 시점에
    열려 있던 유령 재고가 남고, 이식 이후의 정상 왕복이 트립으로 안 세진다
    (실측: 2026-09-01 gap_fade TQQQ +$1.13이 통째로 누락됐다).

    `total`은 기존 통화 무관 합산(KR+US bps 축 함께) 그대로 유지하고,
    `by_market`에 KR/US 각각 따로 `_round_trip_stats`를 태운 블록을 더한다
    (오너 지시 2026-09-02: 표본 임계도 시장별로 각각 적용). 그 시장 표본이
    없으면 해당 키는 None(지어내지 않는다).

    `trades_per_day`/`avg_hold_minutes`/`enabled`는 "이 전략이 얼마나 자주,
    얼마나 오래 들고, 지금 켜져 있나"에 답한다 — 승률만으로는 회전율이 높은
    스캘프와 며칠 들고 가는 전략을 구분할 수 없다. `enabled`는 설정을 못 받으면
    (`strategies_cfg=None`) False — 모르면 꺼진 것으로 본다."""
    strategies_cfg = strategies_cfg or {}
    trips = round_trips(trades)
    stats = []
    for sid in sorted({t["strategy"] for t in trips}):
        strip = [t for t in trips if t["strategy"] == sid]
        known = [t for t in strip if t["pnl_known"]]
        if not known:
            continue
        total = _round_trip_stats(known)
        total["markets"] = sorted({t["market"] for t in known})
        known_kr = [t for t in known if t["market"] == "KR"]
        known_us = [t for t in known if t["market"] == "US"]
        days = {trading_day(_parse_ts({"ts": t["entry_ts"]})) for t in known if t.get("entry_ts")}
        holds = [m for m in (_trip_hold_minutes(t) for t in known) if m is not None]
        stats.append({
            "id": sid,
            "name_ko": _strategy_name_ko(sid),
            "total": total,
            "trades_per_day": round(len(known) / len(days), 2) if days else None,
            "avg_hold_minutes": round(sum(holds) / len(holds), 1) if holds else None,
            "enabled": bool((strategies_cfg.get(sid) or {}).get("enabled", False)),
            "by_market": {
                "asia": _round_trip_stats(known_kr) if known_kr else None,
                "us": _round_trip_stats(known_us) if known_us else None,
            },
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
            "note_en": (
                "Liquidation of legacy positions inherited at the real-account "
                "transition — excluded from performance since it was not a "
                "trading decision made by the program"
            ),
            "krw_impact": round(krw_impact, 2),
            "usd_impact": round(usd_impact, 2),
        }
    }


def _fee_drag_pct_of_gross(rows: list[dict]) -> float | None:
    """수수료·세금이 총 실현손익(수수료 전)의 몇 %를 먹었나 — `rows`(보통 이식
    경계 이후 체결) 기준. gross가 0이면 나눌 수 없으니 None(지어내지 않는다).

    **KRW/USD를 `FX_KRW_PER_USD`로 한 축(KRW)에 모아 계산한다** — 비율 하나로
    답해야 하는 요약값이라 모듈 docstring 규칙 2의 예외("총액을 KRW로 뭉뚱그려
    보여주는 곳")에 해당한다. 지분곡선은 여전히 통화별로 분리돼 있다."""
    gross = 0.0
    fees = 0.0
    for t in rows:
        rate = FX_KRW_PER_USD if str(t.get("market") or "US") == "US" else 1.0
        fees += float(t.get("fee", 0) or 0) * rate
        if str(t.get("side", "")).upper() != "BUY" and t.get("realized_pnl") is not None:
            gross += float(t["realized_pnl"]) * rate
    if gross == 0:
        return None
    return round(fees / abs(gross) * 100, 2)


def _costs(execution_cfg: dict, fee_drag_pct: float | None = None) -> dict:
    """왕복(편도×2) 비용 참고표 — `config/settings.yaml`의 `execution` 블록에서
    유도. 실측(`quant.control.cost_model`)이 아니라 **설정상 가정치**다.

    2026-09-02: 유도 산식은 `cost_model.round_trip_bp_from_settings`로 옮겼다 —
    같은 표가 세 곳에 서로 다른 값으로 있었다(cost_model 상단 주석 참고)."""
    table = round_trip_bp_from_settings(execution_cfg)
    kr_sell_tax = float(execution_cfg.get("kr_stock_sell_tax_bps", 0.0))
    kr_etf_roundtrip = table["KR_ETF"]
    kr_stock_roundtrip = table["KR_STOCK"]
    us_roundtrip = table["US"]
    return {
        "kr_stock_roundtrip_bp": kr_stock_roundtrip,
        "kr_etf_roundtrip_bp": kr_etf_roundtrip,
        "us_roundtrip_bp": us_roundtrip,
        # 실측 비용 압박 — 이식 이후 실제로 낸 수수료·세금이 수수료 전 실현손익의
        # 몇 %였나(2026-09-02 추가). 가정치(위 왕복 bp)와 달리 원장에서 나온 값이다.
        "fee_drag_pct_of_gross": fee_drag_pct,
        # 세금 bp 를 별도 필드로도 낸다 — 프론트가 `costs.note` 문장에서 숫자를
        # 다시 파싱하거나 20bp 를 매직넘버로 하드코딩하지 않게(렌더 준비 완료
        # 원칙, 오너 지시 2026-09-02).
        "kr_tax_bp": kr_sell_tax,
        "note": (
            f"KR 개별주 왕복 {kr_stock_roundtrip:g}bp 중 {kr_sell_tax:g}bp가 "
            "증권거래세+농특세 (US SEC Fee/TAF 등 소액 항목은 미포함)"
        ),
        "note_en": (
            f"Of the {kr_stock_roundtrip:g}bp KR individual-stock round-trip cost, "
            f"{kr_sell_tax:g}bp is securities transaction tax + rural development "
            "surtax (small items like US SEC Fee/TAF not included)"
        ),
    }


def _chart_axis(rows: list[dict]) -> dict:
    """지분곡선 y축 메타(min/max/ticks/zero) — 프론트 `EquityChart`가 하던
    패딩·눈금 계산을 그대로 서버로 옮긴 것(오너 지시 2026-09-02: 렌더 준비
    완료 JSON, 프론트는 표시 형식만). x축은 옮기지 않는다 — 라벨 솎아내기가
    실제 렌더된 컨테이너 픽셀 폭(ResizeObserver)에 의존해 서버가 미리 알 수
    없다(뷰포트 의존 렌더 관심사, SVG 좌표 매핑과 함께 프론트에 남는다).

    행이 없거나(cum_pct 전부 None, 예: seed=0) 유효한 cum_pct가 하나도 없으면
    0 근처의 임의 범위로 폴백한다 — 빈 차트도 축은 그려야 한다."""
    values = [r["cum_pct"] for r in rows if r["cum_pct"] is not None]
    if not values:
        return {"min": -1.0, "max": 1.0, "ticks": [1.0, 0.5, 0.0, -0.5, -1.0], "zero": 0.0}
    raw_max = max(0.0, *values)
    raw_min = min(0.0, *values)
    span = max(raw_max - raw_min, 1.0)
    max_v = raw_max + span * 0.18
    min_v = raw_min - span * 0.18
    return {
        "min": round(min_v, 4),
        "max": round(max_v, 4),
        "ticks": [round(max_v, 4), round(max_v / 2, 4), 0.0, round(min_v / 2, 4), round(min_v, 4)],
        "zero": 0.0,
    }


def _max_drawdown_pct(rows: list[dict]) -> float | None:
    """지분곡선 최대 낙폭(%, 양수) — `cum_pct`(시드 대비 누적 %)에서 직접 계산.

    `1 + cum_pct/100`을 지분 배수로 보고 고점 대비 최대 하락률을 낸다. 유효한
    점이 2개 미만이면 None — 낙폭은 두 점이 있어야 정의된다(지어내지 않는다)."""
    values = [1 + r["cum_pct"] / 100 for r in rows if r.get("cum_pct") is not None]
    if len(values) < 2:
        return None
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return round(worst * 100, 4)


def _phase_boundaries(rows: list[dict], phases: list[dict]) -> list[dict]:
    """행 배열 안에서 phase 가 바뀌는 지점의 인덱스 — 프론트가 하던 스캔을
    그대로 서버로 옮김. `_equity_rows`가 항상 경계 이후(또는 경계가 아예
    없을 때는 paper) 구간 하나만 넘겨주므로 실제로는 거의 항상 빈 리스트다
    (여러 phase 가 한 곡선에 섞이는 경로가 현재 없다) — 그래도 계약은
    지킨다: 미래에 여러 phase 가 한 곡선에 실리면 여기서 자동으로 잡힌다."""
    marks = []
    prev = None
    for i, r in enumerate(rows):
        if prev is not None and r["phase"] != prev:
            phase = next((p for p in phases if p["id"] == r["phase"]), None)
            label_ko = phase["label"] if phase else r["phase"]
            label_en = phase.get("label_en", label_ko) if phase else label_ko
            marks.append({"index": i, "phase": r["phase"], "label": label_ko, "label_en": label_en})
        prev = r["phase"]
    return marks


def build_performance_payload(
    trades: list[dict], execution_cfg: dict, *, now: datetime | None = None,
    real_account_snapshot: dict | None = None, strategies_cfg: dict | None = None,
) -> dict:
    """`trades.jsonl` 원장(dict 리스트, `ledger.load_trades` 출력) → 공개 성과 JSON.

    순수 함수 — 파일 I/O는 호출부(CLI)가 한다. `real_account_snapshot`은 선택
    (`quant.apps.cli.cmd_seed_real`이 쓰는 것과 같은 형식의 dict) — 없으면 이월
    보유 평가액 없이 현금만으로 계산한다(모듈 docstring "알려진 한계" 참고).
    `strategies_cfg`도 선택(`config/settings.yaml`의 `strategies:` 블록) — 없으면
    `strategies[].enabled`가 전부 False 다(모르면 꺼진 것으로 본다).

    **두 스코프가 한 JSON에 공존한다**(2026-09-02 결함 수정): `period`/지분곡선은
    이식 경계 이후만, `strategies` 표는 모의 시대를 포함한 누적이다. 전략 표를
    경계 이후로 자르면 표본이 거의 0이 되어 승률·CI가 무의미해지므로 자르지 않고,
    대신 `strategies_scope`/`period.scope`로 어느 쪽이 어느 스코프인지 **명시**한다
    — 그전에는 히어로의 "세션 2 · 체결 53" 옆에 257왕복 표가 나란히 찍혀 같은
    JSON이 스스로 모순돼 보였다."""
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
        asia_seed, asia_basis, asia_basis_en = _asia_seed_krw(excluded, carryover_krw, carryover_sourced)
        us_seed = _us_seed_usd(excluded)
        asia_rows = _equity_rows(real_included, asia_seed, "real_seeded", "KR")
        us_rows = _equity_rows(real_included, us_seed, "real_seeded", "US")
        us_basis, us_basis_en = "현금만", "cash only"
        prior_paper = _prior_paper_summary(paper_included)
        curve_rows = real_included
    else:
        # 이식 이벤트가 아직 없으면 대체할 real_seeded 구간이 없다 — 지금까지처럼
        # 전체 paper 기록을 그대로 보여준다(숨길 이유가 없다). paper 시대엔 KRW/USD
        # 풀이 물리적으로 분리돼 있지 않았다 — PAPER_SEED_USD 는 그 시절 몫으로만
        # 쓰는 파생 가상값(위 상수 정의부 주석 참고).
        asia_seed, asia_basis, asia_basis_en = PAPER_SEED_KRW, "가상 자본", "virtual capital"
        us_seed, us_basis, us_basis_en = PAPER_SEED_USD, "가상 자본", "virtual capital"
        asia_rows = _equity_rows(included, asia_seed, "paper", "KR")
        us_rows = _equity_rows(included, us_seed, "paper", "US")
        prior_paper = {}
        curve_rows = included

    equity_asia = {
        "currency": "KRW",
        "seed": asia_seed,
        "seed_basis": asia_basis,
        "seed_basis_en": asia_basis_en,
        "rows": asia_rows,
        # 최대 낙폭(%, 양수) — 곡선을 프론트가 다시 훑지 않게 서버가 낸다(렌더
        # 준비 완료 원칙). 점 2개 미만이면 None.
        "max_drawdown_pct": _max_drawdown_pct(asia_rows),
        "chart": {"y_axis": _chart_axis(asia_rows), "phase_boundaries": _phase_boundaries(asia_rows, phases)},
    }
    equity_us = {
        "currency": "USD",
        "seed": us_seed,
        "seed_basis": us_basis,
        "seed_basis_en": us_basis_en,
        "rows": us_rows,
        "max_drawdown_pct": _max_drawdown_pct(us_rows),
        "chart": {"y_axis": _chart_axis(us_rows), "phase_boundaries": _phase_boundaries(us_rows, phases)},
    }

    # 원장 **전체**를 넘긴다 — round_trips가 이식 정리 행을 시대 경계로 쓴다
    # (_strategy_stats docstring 참고).
    strategies = _strategy_stats(trades, strategies_cfg)

    # period 는 두 곡선(아시아/미국)을 합쳐 하나의 요약이어야 한다 — 시장이
    # 다른 날 각자 장이 서므로 union of dates. 2026-09-02 실측 결함(이식 후
    # 2거래일인데 체결을 전체 602로 세어 히어로에 모순된 숫자가 찍힌 것)의
    # 재발 방지 원칙은 그대로: **곡선에 실린 체결만** 센다, prior_paper 는 별도.
    all_dates = sorted({r["date"] for r in asia_rows} | {r["date"] for r in us_rows})
    period = {
        "start": all_dates[0] if all_dates else None,
        "end": all_dates[-1] if all_dates else None,
        "sessions": len(all_dates),
        "total_fills": sum(r["fills"] for r in asia_rows) + sum(r["fills"] for r in us_rows),
    }
    # 스코프 명시 — period/곡선과 strategies 표가 서로 다른 구간을 보고 있다는
    # 사실을 JSON 자신이 말한다(위 함수 docstring 참고).
    if boundary_ts is not None:
        period["scope"] = "real_seeded"
        period["note"] = "실계좌 이식 이후"
        period["note_en"] = "Since real-account transplant"
    else:
        period["scope"] = "paper"
        period["note"] = "모의 운용 전체 구간(실계좌 이식 이전)"
        period["note_en"] = "Entire paper-trading period (before the real-account transplant)"

    strategy_trips = sum(s["total"]["trips"] for s in strategies)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "disclaimer": "모의투자(paper) 기록입니다. 실제 자금이 투입되지 않았습니다.",
        "disclaimer_en": "Paper trading record. No real capital was deployed.",
        "period": period,
        "phases": phases,
        "equity_asia": equity_asia,
        "equity_us": equity_us,
        "prior_paper": prior_paper,
        "strategies": strategies,
        "strategies_scope": "lifetime",
        # 2026-09-02: 히어로의 "지금 가동 N개"는 settings 기준이어야 한다 —
        # strategies[].enabled 만 세면 왕복 기록이 아직 없는 활성 전략이 빠진다.
        "enabled_count": sum(
            1 for v in (strategies_cfg or {}).values() if (v or {}).get("enabled")
        ),
        "strategies_note": (
            f"전략 통계는 모의 시대 포함 누적 {strategy_trips}왕복"
        ),
        "strategies_note_en": (
            f"Strategy stats are lifetime incl. paper era, {strategy_trips} round trips"
        ),
        "excluded": _excluded_summary(excluded),
        "costs": _costs(execution_cfg, _fee_drag_pct_of_gross(curve_rows)),
    }
