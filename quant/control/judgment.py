"""판단 귀속 + 전방 수익률 — Phase 7.1/7.2. 순수 함수만 (I/O 는 호출부).

## 이 모듈이 만드는 구조

목표는 LLM 을 "도입"하는 게 아니라 **채점**하는 것이다. 결정론적 스코어러가
베이스라인이고, LLM 은 같은 입력을 보고 판단을 남기되 **주문을 내지 않는다**.
실현 수익으로 채점해 이겨야만 승격한다 — 못 이기면 안 넣으면 되고 비용은 0이다.

## 왜 selections 를 다시 만들지 않았나

`quant/control/selections.py` 가 이미 매일 종목별 **속성 벡터**를 append-only 로
남기고 `outcome_*` 를 비워 둔다. 그게 이 층의 입력이다. 여기서 더하는 것은 둘뿐:

1. **생산자 귀속** — 누가 그 판단을 했나(`producer`, `producer_version`).
   selections 는 "리포트가 그랬다"까지만 알고, 생산자를 바꿔 비교할 수 없다.
2. **`input_hash`** — 같은 입력을 본 판단끼리만 비교한다(`core.models.input_hash`).

## 왜 전방 수익률이 DB 인가

`selections.py` 는 속성을 그날 남기고 수익률은 나중에 계산한다("속성은 썩고 가격은
안 썩는다"). 그 "나중에"가 **사후 갱신**이라 append-only JSONL 이 잘 못 하는 일이고,
그래서 착수보고서가 이걸 MySQL 도입의 근거로 들었다. 여기서는 계산만 하고,
갱신은 `control/warehouse.py` 가 한다.

`pending_outcomes()` 는 selections 에 이미 있었지만 **부르는 코드가 없었다** —
며칠째 속성만 쌓이고 채점의 나머지 절반이 비어 있었다(2026-08-14 확인).
"""
from __future__ import annotations

from quant.core.models import Judgment, input_hash

# 계획서가 못 박은 지평. T+1 은 다음 거래일이다(달력일이 아니다 — 주말·공휴일은
# 가격이 없으므로 거래일로 세야 "5일 뒤"가 실제로 5개 세션 뒤가 된다).
HOLD_HORIZONS = (1, 5, 20)

# 결정론적 베이스라인의 이름. 리더보드에서 LLM 이 이겨야 할 상대다.
DETERMINISTIC_PRODUCER = "watch_scorer"


def forward_returns_bps(base_close: float | None,
                        closes: dict[int, float | None]) -> dict[int, float | None]:
    """선정 당시 종가 대비 지평별 수익률(bps).

    **bps 로 재는 이유**: 종목 가격대가 달라도 비교 가능해야 한다(적합도 함수가
    명목 대비 bps 를 쓰는 것과 같은 이유 — 총수익률은 계좌·가격대에 오염된다).

    **`None` 은 "수익률 0"이 아니라 "가격을 못 구했다"다.** 0 으로 위장하면
    리더보드가 조회 실패를 "본전"으로 집계하고, 그 오염이 승격 판정까지 간다.
    """
    if not base_close or base_close <= 0:
        # 기준가가 없거나 0 이면 나눗셈이 폭발한다 — 조용히 inf 를 넣지 않는다.
        return {h: None for h in HOLD_HORIZONS}
    out: dict[int, float | None] = {}
    for h in HOLD_HORIZONS:
        future = closes.get(h)
        out[h] = None if not future else (future - base_close) / base_close * 10_000
    return out


# 선정 원장 행에서 **입력이 아닌** 열. 해시에서 빼야 한다.
#
# `build_rows` 는 속성을 최상위에 펼쳐 넣으므로(`**attrs`) 중첩 `attributes` 키가
# 없다. 2026-08-14 실측 버그: `row["attributes"]` 를 읽어 판단 199건이 전부 같은
# 해시(빈 dict)를 가졌고 score 도 전부 None 이었다 — 리더보드 전제가 조용히 파괴됐다.
#
# 제외 이유는 셋이다:
#   - `schema`/`date`/`market`/`symbol` — 식별자이고 입력이 아니다.
#   - `is_candidate` — **결정론적 판정**이다. 해시에 넣으면 같은 입력을 본 LLM 판단과
#     묶이지 않는다(그게 이 해시의 유일한 용도다).
#   - `outcome_*`/`params` — 사후에 채워지거나 바뀐다. 넣으면 같은 판단의 해시가
#     나중에 변한다.
_NOT_INPUT = frozenset({
    "schema", "date", "market", "symbol", "name", "is_candidate",
    "outcome_filled", "params",
})


def selection_attributes(row: dict) -> dict:
    """선정 원장 행 → **입력 속성만**. 중첩 `attributes` 키와 평평한 행을 모두 받는다."""
    nested = row.get("attributes")
    if isinstance(nested, dict) and nested:
        return {k: v for k, v in nested.items() if k not in _NOT_INPUT}
    return {
        k: v for k, v in row.items()
        if k not in _NOT_INPUT and not k.startswith("outcome_")
    }


def selection_judgment(row: dict, producer_version: str,
                       producer: str = DETERMINISTIC_PRODUCER) -> Judgment:
    """선정 원장 한 줄 → 결정론적 베이스라인 판단 (Phase 7.3).

    **떨어뜨린 종목도 판단이다.** 통과분만 남기면 문턱이 너무 높은지 낮은지 영영 알
    수 없다 — selections 가 탈락분을 남기는 이유와 같다.

    `input_hash` 는 **속성 벡터만** 해싱한다. 판정(`is_candidate`)도 생산자도 넣지
    않는다 — 넣으면 같은 입력을 본 LLM 판단과 해시가 달라져 비교가 불가능해진다.
    """
    attrs = selection_attributes(row)
    # 점수 출처: baseline_score100(v2 — watch_scorer 전 종목 확장, §E-2) 만 쓴다.
    # trending_score100(v1) 폴백은 없앴다(2026-08-15 리뷰 C1) — 남겨두면
    # `cmd_outcomes` 가 날짜 필터 없이 selections 전 행을 재판단할 때, 아직
    # baseline_score100 이 없던 옛 행(v1 시절, trending 은 상수 50이 대부분)이
    # `--scorer-version` 기본값 "2" 로 v2 버킷에 v1 산식 점수로 섞여 들어간다 —
    # 옛 행은 이미 v1 시절에 기록된 판단이 있으므로 여기서 잃는 것이 없다.
    # 0 은 유효한 점수라 `is None` 으로만 판정한다. 없으면 None(채점 못 함) —
    # 0 으로 위장하지 않는다(리더보드가 None 을 자동 제외한다).
    score = attrs.get("baseline_score100")
    return Judgment(
        producer=producer,
        producer_version=str(producer_version),
        input_hash=input_hash(attrs),
        market=str(row.get("market") or ""),
        symbol=str(row.get("symbol") or ""),
        session_date=str(row.get("date") or ""),
        # 생산자별 척도다 — 그대로 비교하지 않고 순위/판정으로 환산해 쓴다.
        score=score,
        verdict="pass" if row.get("is_candidate") else "reject",
        rationale=str(attrs.get("origin") or attrs.get("trending_label") or ""),
        ts=str(row.get("recorded_at") or row.get("date") or ""),
    )


# ── DB 행 변환 (순수) ─────────────────────────────────────────────────────

JUDGMENT_COLS = ["producer", "producer_version", "input_hash", "market", "symbol",
                 "session_date", "score", "verdict", "rationale", "ts"]
# 판단은 불변이다 — 충돌하면 아무것도 갱신하지 않는다(같은 내용이므로).
# `warehouse.upsert_sql` 이 빈 update 목록을 `INSERT IGNORE` 로 만든다.
JUDGMENT_UPDATE: list[str] = []


def judgment_row(j: Judgment) -> tuple:
    """`JUDGMENT_COLS` 순서 그대로. 순서가 어긋나면 컬럼이 조용히 뒤바뀐다."""
    return (
        j.producer, j.producer_version, j.input_hash, j.market, j.symbol,
        j.session_date, j.score, j.verdict, j.rationale[:255], j.ts,
    )
