"""전방 수익률 채우기 — Phase 7.2 배선. 순수 함수만 (I/O 는 `apps.cli outcomes`).

## 없던 절반

`selections.py` 는 매일 종목별 속성 벡터를 남기고 `outcome_*` 를 비워 둔다. 거기
`pending_outcomes()` 도 있는데 **부르는 코드가 없었다** — 2026-08-14 확인: 선정 원장
199행 중 `outcome_filled` 이 **0건**. 속성만 며칠 쌓이고 채점의 나머지 절반이 비어
있었다. 리더보드(7.5)는 이 값을 입력으로 받는다.

## 왜 "과거 시세 조회"가 아닌가

D+1 수익률에는 D+1 종가가 필요하다. 그걸 나중에 과거 시세로 조회하려면 종목마다 일봉
히스토리가 필요한데 우리 히스토리는 몇 종목만 덮는다(실측: EC2 parquet 4종목).

대신 **매일 돌면서 그날 만기가 된 지평만 채운다.** 그때 필요한 건 **오늘 종가**뿐이고
그건 리포트가 이미 매일 받는다. 과거 조회가 사라진다.

## 거래일 근사와 그 한계 (숨기지 않는다)

공휴일 달력이 없으므로 **평일 수로 센다.** 한·미 공휴일에 하루씩 밀릴 수 있다.
그래서 채운 행에 **실제 기준 날짜(`outcome_dN_asof`)를 함께 남긴다** — "D+5 라고 적힌
게 실제로 며칠 뒤였나"를 되짚을 수 있어야 한다. `backfill._find_gaps` 도 같은 근사를
쓰고 같은 이유로 주석에 밝혀 두었다.

`quant/analyze/opendays.py` 에 앵커 종목 실봉 기반의 진짜 개장일 판정이 있지만
parquet 를 읽는 I/O 다 — 이 모듈은 의도적으로 순수 함수만 두므로(상단 제목) 끌어오려면
`root` 를 `due_horizons`/`apply_outcome`/`horizon_status_counts` 전체와 그 테스트까지
배선해야 한다. 이번 수리(D2, 2026-09-03) 범위를 넘어 평일 근사를 그대로 남긴다 —
공휴일만큼의 오차는 `outcome_dN_asof` 로 여전히 검산 가능하다.

## 2026-09-03 감사 수리 (D1~D3)

- **D1 (가짜 0bp)**: `closes_from_quotes` 가 종가만 남기고 그 종가가 **어느 거래일
  것인지**를 버렸다. 휴장일 다음날(월요일 등)엔 "오늘 종가"가 선정일 종가와 같은
  세션이라 `(close_now-base)/base` 가 정확히 0.0 으로 찍혔고, `cmd_outcomes` 의
  `is not None` 필터가 그 가짜 0 을 "이미 채워짐"으로 보고 다시는 재시도하지
  않았다(실측: filled D+1 행의 17.4%). 이제 `closes_from_quotes` 는
  `{심볼: (종가, 날짜)}` 를 돌려주고 `apply_outcome` 은 그 날짜가 기준 세션보다
  뒤가 아니면 **아무것도 쓰지 않는다**(시세 없음과 동일 취급 — grace 재시도가
  마저 채운다).
- **D2 (기준가에 날짜가 없다)**: 선정 행의 `close` 는 "그날 리포트가 받은 값"일 뿐
  실제 거래일을 안 남겼다 — `due_horizons` 가 선정 원장의 `date`(리포트가 빌드를
  돈 날짜, 반드시 거래일은 아니다)로 세션을 셌다. `close_date`(있으면)를 기준
  세션으로 우선 쓰고, 없는 레거시 행은 기존 `date` 근사로 폴백한다.
- **D3 (BRK.B 등 종류주가 영원히 안 채점됨)**: 야후는 점(`.`)이 든 미국 종류주
  심볼을 대시(`-`)로 요구한다 — `split_for_quotes` 가 원 심볼을 그대로 넘기면
  그 심볼만 조용히 빠졌다. `to_yahoo_us_symbol` 이 조회 직전에만 변환하고,
  결과는 원래 심볼로 다시 매핑해 돌려준다(선정 행의 `symbol` 은 그대로 `BRK.B`).
"""
from __future__ import annotations

from datetime import date, timedelta

from quant.control.judgment import HOLD_HORIZONS

_LONGEST = max(HOLD_HORIZONS)


def _business_days_between(start: date, end: date) -> int | None:
    """`start` 다음 영업일부터 `end` 까지의 영업일 수. `end <= start` 면 None.

    공휴일은 모른다 — 그래서 근사이고, 호출부가 `asof` 를 함께 기록한다.
    """
    if end <= start:
        return None
    n = 0
    cur = start
    while cur < end:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    # end 자체가 주말이면 그 날은 기준일이 될 수 없다(가격이 없다).
    return n if end.weekday() < 5 else None


def base_session_date(row: dict) -> str:
    """이 행의 기준가(`close`)가 실제로 속한 세션 날짜 (D2, 2026-09-03).

    `close_date`(새 행 — 시세를 조회한 실제 거래일)가 있으면 그걸 쓴다. 없는
    레거시 행은 선정 원장의 `date`(리포트 빌드일 — 반드시 거래일은 아니다)로
    폴백한다. 세션 카운트(`due_horizons`)와 재시도 판단(`apply_outcome`)이 모두
    이 함수 하나로 기준 날짜를 정해야 두 계산이 어긋나지 않는다.
    """
    return str(row.get("close_date") or row.get("date") or "")


def due_horizons(selection_date: str, today: str, grace_days: int = 2) -> list[int]:
    """오늘이 만기이거나, 만기를 놓친 지 `grace_days` 이내인 지평 목록. 보통
    0개나 1개다.

    **왜 정확 일치가 아니라 유예(grace)인가** (2026-08-26 감사 재현): 예전엔
    `h == age` 정확 일치였다 — D+1 당일 시세 조회가 실패하면(야후 매핑 실패 등)
    다음날엔 age=2가 돼 버려 어느 지평에도 안 걸리고, `outcome_d1_bps`가 영원히
    None 으로 굳었다. 재시도 메커니즘이 없었다. `h <= age <= h + grace_days`로
    넓혀 늦게라도 채울 기회를 준다.

    이미 채워진 지평은 이 함수가 아니라 호출부(`pending_symbols`,
    `cmd_outcomes`)가 `outcome_dN_bps is not None`으로 걸러 재기록하지 않는다.
    grace 밖(`age > h + grace_days`)은 여전히 반환하지 않는다 — 사흘 넘게 지난
    종가를 "D+1"이라 적는 건 근사가 아니라 오염이다. 늦게 채워도 `apply_outcome`이
    `outcome_dN_asof`에 실제 기준일을 정직하게 남긴다("거래일 근사와 그 한계" 참고).

    `selection_date` 인자는 호출부가 `base_session_date(row)`(D2)로 넘겨야
    한다 — 이 함수 자체는 어느 필드가 기준인지 모른다(순수 문자열 계산).
    """
    try:
        sel = date.fromisoformat(selection_date)
        now = date.fromisoformat(today)
    except (TypeError, ValueError):
        return []
    age = _business_days_between(sel, now)
    if age is None:
        return []
    return [h for h in HOLD_HORIZONS if h <= age <= h + grace_days]


def _key(horizon: int) -> str:
    return f"outcome_d{horizon}_bps"


def pending_symbols(rows: list[dict], today: str) -> set[str]:
    """오늘 시세가 **필요한** 종목만. 시세 조회는 네트워크라 필요 없는 종목까지
    부르면 레이트 리밋에 걸린다."""
    out: set[str] = set()
    for row in rows:
        for h in due_horizons(base_session_date(row), today):
            if row.get(_key(h)) is None:
                sym = str(row.get("symbol") or "").strip()
                if sym:
                    out.add(sym)
    return out


def apply_outcome(row: dict, horizon: int, quote: tuple[float, str] | None,
                  asof: str) -> dict:
    """한 행의 한 지평을 채운다. **입력을 변형하지 않는다**(새 dict 반환).

    `quote` 는 `closes_from_quotes` 가 주는 `(종가, 그 종가가 속한 실제 거래일)`.
    `asof` 는 이 명령이 도는 날짜("오늘") — `close_report.matured_today`가
    "그 지평이 오늘 채워졌나"를 `outcome_dN_asof == today` 로 판단하므로, 시세의
    실제 날짜가 아니라 **호출부가 넘긴 값을 그대로** 적는다(계약을 안 바꾼다).
    시세를 못 구했으면(`quote is None`) **아무것도 쓰지 않는다** — 0 을 쓰면 조회
    실패가 "본전"으로 영구히 굳고 다시 시도할 기회도 사라진다.

    **D1 (2026-09-03) — 같은 세션이면 시세 없음과 동일하게 취급한다.** `quote`
    의 날짜가 기준 세션(`base_session_date`)보다 뒤가 아니면, 그건 "아직 다음
    거래일 시세가 안 나왔다"(휴장일 다음날 등)는 뜻이지 "수익률이 0" 이 아니다.
    여기서 확정해 버리면 `is not None` 재조회 필터에 걸려 영원히 굳는다 — grace
    재시도가 마저 채우게 그대로 둔다. (`quote` 의 날짜는 이 게이트에만 쓴다 —
    `outcome_dN_asof` 에는 안 적는다, 위 이유.)
    """
    out = dict(row)
    # 속성은 최상위에 펼쳐져 있다(build_rows 의 `**attrs`) — 중첩 키를 읽으면
    # 기준가가 영원히 None 이고 수익률이 한 건도 안 채워진다(2026-08-14 실측).
    from quant.control.judgment import selection_attributes

    base = selection_attributes(row).get("close")
    if not quote or not base or float(base) <= 0:
        return out
    close_now, quote_date = quote
    if not close_now:
        return out
    base_session = base_session_date(row)
    if base_session and str(quote_date) <= base_session:
        return out
    out[_key(horizon)] = (float(close_now) - float(base)) / float(base) * 10_000
    out[f"outcome_d{horizon}_asof"] = asof
    # `outcome_filled` 은 "더 채울 게 없다"는 뜻이다. D+1 만 채우고 세우면
    # pending_outcomes 가 이 행을 건너뛰어 D+5·D+20 이 영영 안 채워진다.
    if all(out.get(_key(h)) is not None for h in HOLD_HORIZONS):
        out["outcome_filled"] = True
    return out


def closes_from_quotes(quotes: dict) -> dict[str, tuple[float, str]]:
    """`fetch_symbol_quotes` 결과 → `{심볼: (종가, 그 종가가 속한 거래일)}`.

    그 함수는 `{심볼: {"close":..., "date":..., "prev":..., ...}}` 를 돌려준다.
    2026-08-14 실측 버그: 배선이 `float(v)` 로 dict 를 변환하려다 예외가 났고 except
    가 삼켜 "시세 0건"만 남았다 — **형태를 여기 한 곳에서 다루고 테스트로 못 박는다.**

    2026-09-03 (D1): 예전엔 `date` 를 버리고 종가만 남겼다 — `apply_outcome` 이
    그 종가가 선정일과 같은 세션인지 구분할 방법이 없어, 휴장일 다음날엔 가짜
    0bp 를 확정해 버렸다. 이제 날짜를 함께 남긴다.

    `close` 가 없으면 그 종목은 **시세를 못 구한 것**이다(0 으로 위장하지 않는다).
    `date` 가 없으면(방어적으로만 — `fetch_symbol_quotes` 는 항상 준다) 그 종목도
    통째로 뺀다. 날짜 없는 종가는 세션 판정이 불가능해 D1 의 가짜-0bp 위험을 그대로
    재현하기 때문이다.
    """
    out: dict[str, tuple[float, str]] = {}
    for sym, q in (quotes or {}).items():
        if not isinstance(q, dict):
            continue
        close = q.get("close")
        quote_date = q.get("date")
        if close is None or not quote_date:
            continue
        try:
            out[str(sym)] = (float(close), str(quote_date))
        except (TypeError, ValueError):
            continue
    return out


def to_yahoo_us_symbol(symbol: str) -> str:
    """미국 종류주 심볼(`BRK.B` 등) → 야후가 받는 형태(`BRK-B`) (D3, 2026-09-03).

    야후는 점(`.`)이 든 심볼을 조용히 못 받는다 — 배치 조회에서 그 심볼만 결측으로
    빠지고 예외도 안 난다. 그래서 BRK.B 는 한 번도 채점된 적이 없었다(감사 재현).
    조회 직전에만 이 형태로 바꾸고, 결과는 원래 점 표기 심볼로 되돌려 매핑한다 —
    선정 원장의 `symbol` 필드(`row.get("symbol")`)는 그대로 `BRK.B` 라서, 여기서
    바꾼 채로 두면 `apply_outcome` 의 조회가 다시 어긋난다.
    """
    return symbol.replace(".", "-")


def split_for_quotes(symbols: set[str]) -> tuple[set[str], set[str]]:
    """(US 티커, KR 코드). **KR 6자리 코드는 야후 심볼이 아니다** — 매핑 없이 부르면
    조용히 빈 결과가 온다(내일 KR 선정이 만기가 되면 그렇게 실패할 예정이었다)."""
    from quant.core.models import market_of_symbol

    us = {s for s in symbols if market_of_symbol(s) == "US"}
    return us, set(symbols) - us


def horizon_status_counts(rows: list[dict], today: str,
                          grace_days: int = 2) -> dict[int, dict[str, int]]:
    """지평(D+1/5/20)별 `{filled, due, lost}` 카운트 — `cmd_outcomes` 요약용
    (D4, 2026-09-03).

    **"진행 중"과 "죽은 표본"은 다르다.** `filled` 만 세면 리더보드가 아직 만기가
    안 된 표본과 grace 를 넘겨 영원히 못 채운 표본을 구분하지 못한다.

    - `filled`: 이미 채워졌다.
    - `due`: 만기가 됐거나(또는 grace 안에서 놓쳤다) 아직 못 채웠다 — 다음 회차에
      채워질 여지가 있다.
    - `lost`: grace 를 넘겼는데 못 채웠다 — **영구 유실**(다시 채울 기회가 없다,
      `due_horizons` 가 더는 이 지평을 반환하지 않는다).

    만기가 아직 안 된 행(`age < h`)은 셋 중 어디에도 넣지 않는다 — "아직 물어보지
    않은 질문"이라 진행 중/유실과 다르다.
    """
    out: dict[int, dict[str, int]] = {h: {"filled": 0, "due": 0, "lost": 0} for h in HOLD_HORIZONS}
    try:
        now = date.fromisoformat(today)
    except (TypeError, ValueError):
        return out
    for row in rows:
        try:
            sel = date.fromisoformat(base_session_date(row))
        except (TypeError, ValueError):
            continue
        age = _business_days_between(sel, now)
        if age is None:
            continue
        for h in HOLD_HORIZONS:
            if age < h:
                continue
            if row.get(_key(h)) is not None:
                out[h]["filled"] += 1
            elif age <= h + grace_days:
                out[h]["due"] += 1
            else:
                out[h]["lost"] += 1
    return out
