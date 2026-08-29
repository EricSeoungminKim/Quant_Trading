"""워치 조건 평가기 — 알림 전용, 트레이딩뷰 알림의 우리판.

전략과 다르다: `Signal`을 만들지 않고, 주문·시그널로 이어지는 경로가 이 모듈
안에 전혀 없다(반환 타입 `Hit`에는 수량·비중·target_weight 같은 사이징 필드가
없다). 순수 판정 함수만 두고, 실제 데이터 조회(quote/history)와 쿨다운·알림
전송은 `quant/trade/loop.py`가 담당한다 — 이 모듈은 네트워크를 모른다
(`quant/core/`와 같은 등급의 순수성).

RSI는 `quant/trade/indicators`의 기존 `rsi()`(일봉, Wilder 평활)를 그대로
재사용한다 — 새 구현을 만들지 않는다.

## change_pct — Quote 모델과의 차이

설계 문서는 "quote의 등락률 필드"를 가정했지만, `quant.core.models.Quote`는
`symbol`/`ts`/`price` 세 필드뿐이고 등락률 필드가 없다(확인 결과). 그래서
`change_pct`는 quote 가격과 **전일 종가**(`ctx.data.history(symbol, "1d", n)`의
마지막 완성봉 종가 — `quant/trade/strategy/gap_fade.py`의 `prior_close`와 동일
로직)로 직접 계산한다. 이 때문에 `change_pct`도 `rsi2`/`rsi14`와 마찬가지로
일봉 조회가 필요하다(loop.py 배선 참고) — `price`만 quote 단독으로 충분하다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from quant.core.models import Quote
from quant.trade.indicators import rsi

logger = logging.getLogger(__name__)

_VALID_METRICS = {"rsi2", "rsi14", "change_pct", "price"}
_VALID_OPS = {"<", ">", "<=", ">="}

_OP_FUNCS = {
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class Rule:
    name: str
    symbol: str
    metric: str  # rsi2 | rsi14 | change_pct | price
    op: str  # < | > | <= | >=
    threshold: float


@dataclass(frozen=True)
class Hit:
    """조건 발동 1건 — 시그널이 아니다. 수량·비중·주문 관련 필드가 없다."""

    rule_name: str
    symbol: str
    metric: str
    value: float
    op: str
    threshold: float


def parse_rules(raw: list[dict]) -> list[Rule]:
    """settings.yaml `watch_conditions.rules`를 파싱·검증한다.

    미지의 metric/op는 오타가 조용히 무시되는 것을 막기 위해 여기서 즉시
    `ValueError`로 거부한다(부팅 시 실패) — 런타임에 그 규칙만 조용히
    스킵하지 않는다."""
    rules: list[Rule] = []
    for i, item in enumerate(raw):
        metric = item.get("metric")
        op = item.get("op")
        if metric not in _VALID_METRICS:
            raise ValueError(
                f"watch_conditions.rules[{i}]: 알 수 없는 metric {metric!r} "
                f"(허용: {sorted(_VALID_METRICS)})"
            )
        if op not in _VALID_OPS:
            raise ValueError(
                f"watch_conditions.rules[{i}]: 알 수 없는 op {op!r} "
                f"(허용: {sorted(_VALID_OPS)})"
            )
        rules.append(Rule(
            name=str(item["name"]),
            symbol=str(item["symbol"]),
            metric=metric,
            op=op,
            threshold=float(item["threshold"]),
        ))
    return rules


def _prior_close(daily_bars: pd.DataFrame | None) -> float | None:
    """마지막 완성 일봉의 종가(=전일 종가). `history()`는 완성봉만 반환하므로
    장중에는 마지막 행이 항상 전일 종가다(`gap_fade.prior_close`와 동일 로직 —
    전략 모듈에 대한 의존을 만들지 않기 위해 여기 그대로 둔다)."""
    if daily_bars is None or daily_bars.empty:
        return None
    value = float(daily_bars["close"].iloc[-1])
    if pd.isna(value) or not (value > 0):
        return None
    return value


def _metric_value(rule: Rule, quote: Quote | None, bars: pd.DataFrame | None) -> float | None:
    """규칙의 metric 현재값. 판단 불가면 None(스킵용 — 발동 아님)."""
    if rule.metric == "price":
        if quote is None or quote.price <= 0:
            return None
        return quote.price
    if rule.metric == "change_pct":
        if quote is None or quote.price <= 0:
            return None
        prev_close = _prior_close(bars)
        if prev_close is None:
            return None
        return (quote.price / prev_close - 1) * 100
    if rule.metric in ("rsi2", "rsi14"):
        if bars is None or bars.empty:
            return None
        period = 2 if rule.metric == "rsi2" else 14
        series = rsi(bars["close"], period)
        if series.empty or pd.isna(series.iloc[-1]):
            return None
        return float(series.iloc[-1])
    raise AssertionError(f"parse_rules가 걸렀어야 할 metric: {rule.metric!r}")


def evaluate(
    rules: list[Rule],
    quotes: dict[str, Quote | None],
    bars: dict[str, pd.DataFrame | None],
    now: datetime,
) -> list[Hit]:
    """규칙을 판정한다 — 순수 함수, 네트워크 없음(quote/bars는 호출부가 미리
    조회해 넘긴다). `now`는 향후 시간대 조건을 위해 시그니처에 남겨둔다(현재
    스키마는 쓰지 않는다).

    데이터가 없어 판단 불가한 규칙은 조용히 스킵한다(디버그 로그만) —
    "확인 불가"는 "발동"이 아니다."""
    hits: list[Hit] = []
    for rule in rules:
        value = _metric_value(rule, quotes.get(rule.symbol), bars.get(rule.symbol))
        if value is None:
            logger.debug("워치 조건 %s: 데이터 없음 — 스킵", rule.name)
            continue
        if _OP_FUNCS[rule.op](value, rule.threshold):
            hits.append(Hit(
                rule_name=rule.name, symbol=rule.symbol, metric=rule.metric,
                value=value, op=rule.op, threshold=rule.threshold,
            ))
    return hits


def apply_cooldown(
    hits: list[Hit],
    last_hit_mono: dict[str, float],
    cooldown_seconds: float,
    now_mono: float,
) -> list[Hit]:
    """쿨다운(같은 규칙 재알림 최소 간격)을 통과한 발동만 남긴다.

    `last_hit_mono`(rule name -> 마지막 알림 시각, `time.monotonic()`)는 통과분에
    대해 **그 자리에서(in-place)** `now_mono`로 갱신한다 — 호출부(loop.py)가
    루프 인스턴스 메모리로 들고 있다가 그대로 다시 넘기면 된다(재시작 시
    리셋되는 것은 무해하다, watch_conditions 설계 문서 참고)."""
    out: list[Hit] = []
    for hit in hits:
        last = last_hit_mono.get(hit.rule_name, float("-inf"))
        if now_mono - last < cooldown_seconds:
            continue
        last_hit_mono[hit.rule_name] = now_mono
        out.append(hit)
    return out
