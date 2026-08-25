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
        for h in due_horizons(str(row.get("date") or ""), today):
            if row.get(_key(h)) is None:
                sym = str(row.get("symbol") or "").strip()
                if sym:
                    out.add(sym)
    return out


def apply_outcome(row: dict, horizon: int, close_now: float | None,
                  asof: str) -> dict:
    """한 행의 한 지평을 채운다. **입력을 변형하지 않는다**(새 dict 반환).

    시세를 못 구했으면 **아무것도 쓰지 않는다** — 0 을 쓰면 조회 실패가 "본전"으로
    영구히 굳고 다시 시도할 기회도 사라진다.
    """
    out = dict(row)
    # 속성은 최상위에 펼쳐져 있다(build_rows 의 `**attrs`) — 중첩 키를 읽으면
    # 기준가가 영원히 None 이고 수익률이 한 건도 안 채워진다(2026-08-14 실측).
    from quant.control.judgment import selection_attributes

    base = selection_attributes(row).get("close")
    if not close_now or not base or float(base) <= 0:
        return out
    out[_key(horizon)] = (float(close_now) - float(base)) / float(base) * 10_000
    out[f"outcome_d{horizon}_asof"] = asof
    # `outcome_filled` 은 "더 채울 게 없다"는 뜻이다. D+1 만 채우고 세우면
    # pending_outcomes 가 이 행을 건너뛰어 D+5·D+20 이 영영 안 채워진다.
    if all(out.get(_key(h)) is not None for h in HOLD_HORIZONS):
        out["outcome_filled"] = True
    return out


def closes_from_quotes(quotes: dict) -> dict[str, float]:
    """`fetch_symbol_quotes` 결과 → `{심볼: 종가}`.

    그 함수는 `{심볼: {"close":..., "prev":..., ...}}` 를 돌려준다. 2026-08-14 실측
    버그: 배선이 `float(v)` 로 dict 를 변환하려다 예외가 났고 except 가 삼켜
    "시세 0건"만 남았다 — **형태를 여기 한 곳에서 다루고 테스트로 못 박는다.**

    `close` 가 없으면 그 종목은 **시세를 못 구한 것**이다(0 으로 위장하지 않는다).
    """
    out: dict[str, float] = {}
    for sym, q in (quotes or {}).items():
        if not isinstance(q, dict):
            continue
        close = q.get("close")
        if close is None:
            continue
        try:
            out[str(sym)] = float(close)
        except (TypeError, ValueError):
            continue
    return out


def split_for_quotes(symbols: set[str]) -> tuple[set[str], set[str]]:
    """(US 티커, KR 코드). **KR 6자리 코드는 야후 심볼이 아니다** — 매핑 없이 부르면
    조용히 빈 결과가 온다(내일 KR 선정이 만기가 되면 그렇게 실패할 예정이었다)."""
    from quant.core.models import market_of_symbol

    us = {s for s in symbols if market_of_symbol(s) == "US"}
    return us, set(symbols) - us
