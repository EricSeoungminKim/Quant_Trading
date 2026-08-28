"""지수 대비 초과수익(알파) 일일 추적 — 2026-08-28 소유자 지시.

"지수가 빠지는 날 손실이 있는 건 당연하다. 내가 원하는 건 지수가 빠지건 오르건
항상 지수 그래프 기준으로 우리는 위에서 노는 것 — 잃더라도 덜 잃고, 지수가
오를 땐 더 얻고."

그것의 이름이 **알파**다. 아무도 알파를 보장할 수 없지만, 측정하지 않으면 있는지
조차 알 수 없다. `data/ledger/equity_curve.jsonl` 은 절대 손익만 기록한다 —
"오늘 -1% 났는데 지수가 -3% 였다면 이긴 날"이라는 사실이 그 원장에는 없다.
이 모듈이 그 한 칸을 채운다.

설계:
- **순수 로직**이다. 파일도 네트워크도 pandas 도 모른다 — 호출부(`apps/cli.py`)가
  읽어서 (날짜, 값) 시퀀스로 넘긴다. 그래야 손계산으로 검증할 수 있다.
- 알파는 **pp(percentage point)** 단위다. 우리 수익률(%)에서 지수 수익률(%)을 뺀
  차이지 비율이 아니다. 단위를 섞으면 "1.5배 좋다"와 "1.5%p 좋다"가 뒤엉킨다.
- 요약은 **상승일과 하락일을 절대 섞지 않는다.** 평균 하나로 뭉치면 소유자가
  물은 두 질문("오를 땐 더?", "잃을 땐 덜?") 중 어느 쪽도 답이 안 나온다.
- 표본 미달(각 5일 미만)이면 해당 값은 `None` 이다. 3일치로 계산한 참여율은
  숫자처럼 생겼을 뿐 근거가 아니다 — 없는 것으로 표시하는 편이 정직하다.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime

# 시장별 벤치마크. KR=069500(KODEX 200), US=QQQ — regime 평면이 국면 판정에 쓰는
# 것과 같은 대표 지수 ETF 다(ADR-0009).
BENCHMARKS: dict[str, str] = {"KR": "069500", "US": "QQQ"}

# 상승일/하락일 각각 이 일수 미만이면 그쪽 요약값은 None.
MIN_SAMPLE_DAYS = 5


def _as_date(value: object) -> date | None:
    """date/datetime/"YYYY-MM-DD..." 를 date 로. 못 읽으면 None(그 행은 버린다)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    # pandas.Timestamp 등 date() 를 가진 것들.
    getter = getattr(value, "date", None)
    if callable(getter):
        try:
            got = getter()
            return got if isinstance(got, date) else None
        except Exception:  # noqa: BLE001 — 남의 타입이 던지는 건 알 수 없다
            return None
    return None


def _as_float(value: object) -> float | None:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN 배제


def _pct_changes(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """(날짜, 수준) → (날짜, 전일 대비 %). 첫날은 전일이 없으므로 빠진다.

    달력상 연속일이 아니어도(주말·휴장·기록 누락) 원장에 있는 **직전 점** 대비로
    계산한다 — 없는 날을 지어내지 않는다."""
    out: list[tuple[date, float]] = []
    for i in range(1, len(points)):
        prev_v = points[i - 1][1]
        if prev_v <= 0:
            continue
        out.append((points[i][0], (points[i][1] / prev_v - 1.0) * 100.0))
    return out


def daily_returns(equity_rows: Iterable[Mapping]) -> list[tuple[date, str, float]]:
    """자본 곡선 원장 행 → 시장별 일간 수익률 (날짜, 시장, %).

    행 형식은 `cli equity-snapshot` 이 쓰는 그대로다:
    `{"date": "2026-08-27", "market": "US", "total_krw": 10123456.78, ...}`.
    같은 (date, market) 이 여러 줄이면 **마지막 것이 이긴다**(원장 관례 —
    재실행은 덮어쓰기가 아니라 append 이고 읽는 쪽이 마지막만 쓴다).
    """
    latest: dict[tuple[date, str], float] = {}
    for row in equity_rows or []:
        if not isinstance(row, Mapping):
            continue
        d = _as_date(row.get("date"))
        market = row.get("market")
        total = _as_float(row.get("total_krw"))
        if d is None or not isinstance(market, str) or total is None or total <= 0:
            continue
        latest[(d, market)] = total

    by_market: dict[str, list[tuple[date, float]]] = {}
    for (d, market), total in latest.items():
        by_market.setdefault(market, []).append((d, total))

    out: list[tuple[date, str, float]] = []
    for market, points in by_market.items():
        points.sort()
        out += [(d, market, ret) for d, ret in _pct_changes(points)]
    out.sort(key=lambda r: (r[0], r[1]))
    return out


def benchmark_returns(bars_1d: Iterable) -> list[tuple[date, float]]:
    """벤치마크 일봉 → 일간 수익률 (날짜, %).

    `bars_1d` 는 (날짜, 종가) 쌍 또는 `{"date"/"ts": ..., "close": ...}` 매핑의
    시퀀스다. 파케이/DataFrame 을 읽는 건 어댑터·호출부의 일이다 — 이 모듈은
    pandas 를 모른다. 같은 날짜가 여럿이면 마지막이 이긴다.
    """
    latest: dict[date, float] = {}
    for bar in bars_1d or []:
        if isinstance(bar, Mapping):
            d = _as_date(bar.get("date") if "date" in bar else bar.get("ts"))
            close = _as_float(bar.get("close"))
        elif isinstance(bar, Sequence) and not isinstance(bar, str) and len(bar) >= 2:
            d, close = _as_date(bar[0]), _as_float(bar[1])
        else:
            continue
        if d is None or close is None or close <= 0:
            continue
        latest[d] = close
    return _pct_changes(sorted(latest.items()))


def alpha_series(
    ours: Iterable, bench: Iterable,
) -> list[tuple[date, float, float, float]]:
    """(날짜, 우리 %, 지수 %, 알파 pp) — **날짜 교집합만**, 날짜순.

    `ours` 는 `daily_returns()` 의 (날짜, 시장, %) 3튜플도, (날짜, %) 2튜플도
    받는다. 3튜플을 넘길 때는 **호출부가 시장을 미리 걸러야 한다** — 두 시장을
    섞어 넘기면 같은 날짜가 충돌한다.

    교집합만 쓰는 이유: 우리가 쉰 날의 지수 등락은 우리 성적이 아니고, 지수
    데이터가 빈 날의 우리 등락은 비교 대상이 없다. 한쪽을 0으로 메우면 알파가
    조용히 부풀거나 깎인다.
    """
    our_map: dict[date, float] = {}
    for item in ours or []:
        if not isinstance(item, Sequence) or isinstance(item, str) or len(item) < 2:
            continue
        d, ret = _as_date(item[0]), _as_float(item[-1])
        if d is not None and ret is not None:
            our_map[d] = ret

    bench_map: dict[date, float] = {}
    for item in bench or []:
        if not isinstance(item, Sequence) or isinstance(item, str) or len(item) < 2:
            continue
        d, ret = _as_date(item[0]), _as_float(item[-1])
        if d is not None and ret is not None:
            bench_map[d] = ret

    return [
        (d, our_map[d], bench_map[d], our_map[d] - bench_map[d])
        for d in sorted(set(our_map) & set(bench_map))
    ]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _compound_pct(returns: list[float]) -> float:
    """일간 %수익률들을 복리로 누적한 총 %."""
    acc = 1.0
    for r in returns:
        acc *= 1.0 + r / 100.0
    return (acc - 1.0) * 100.0


def alpha_summary(series: Sequence[tuple[date, float, float, float]]) -> dict:
    """알파 요약 — 소유자의 두 질문을 두 숫자로 분리해서 답한다.

    - `cum_alpha_pp`: 복리 누적 우리 % − 복리 누적 지수 % (pp). 일별 알파를
      단순 합산하지 않는다 — 곡선의 차이는 복리로 벌어진다.
    - 상승일(지수 > 0) 참여: `up_our_avg_pct` (우리 평균 %), 대조군
      `up_bench_avg_pct`, 비율 `up_capture`(우리/지수, 1 초과면 더 먹었다).
    - 하락일(지수 < 0) 방어: `down_our_avg_pct`, `down_bench_avg_pct`,
      `down_capture`(우리/지수, **1 미만이면 덜 잃었다**).

    지수가 정확히 0인 날은 어느 쪽에도 넣지 않는다(참여도 방어도 아니다).
    표본 미달(각 `MIN_SAMPLE_DAYS` 미만)이면 그쪽 값은 전부 None — 일수만 남긴다.
    """
    rows = list(series or [])
    up = [(our, bench) for _, our, bench, _ in rows if bench > 0]
    down = [(our, bench) for _, our, bench, _ in rows if bench < 0]

    out: dict = {
        "n_days": len(rows),
        "first_date": rows[0][0].isoformat() if rows else None,
        "last_date": rows[-1][0].isoformat() if rows else None,
        "cum_our_pct": _compound_pct([r[1] for r in rows]) if rows else None,
        "cum_bench_pct": _compound_pct([r[2] for r in rows]) if rows else None,
        "cum_alpha_pp": None,
        "win_days": sum(1 for r in rows if r[3] > 0),
        "up_days": len(up),
        "up_our_avg_pct": None,
        "up_bench_avg_pct": None,
        "up_capture": None,
        "down_days": len(down),
        "down_our_avg_pct": None,
        "down_bench_avg_pct": None,
        "down_capture": None,
    }
    if rows:
        out["cum_alpha_pp"] = out["cum_our_pct"] - out["cum_bench_pct"]

    if len(up) >= MIN_SAMPLE_DAYS:
        our_avg, bench_avg = _mean([o for o, _ in up]), _mean([b for _, b in up])
        out["up_our_avg_pct"] = our_avg
        out["up_bench_avg_pct"] = bench_avg
        out["up_capture"] = our_avg / bench_avg if bench_avg else None

    if len(down) >= MIN_SAMPLE_DAYS:
        our_avg, bench_avg = _mean([o for o, _ in down]), _mean([b for _, b in down])
        out["down_our_avg_pct"] = our_avg
        out["down_bench_avg_pct"] = bench_avg
        out["down_capture"] = our_avg / bench_avg if bench_avg else None

    return out


def wrap_section(
    series: Sequence[tuple[date, float, float, float]], market: str,
) -> dict:
    """마감 HTML 리포트(`control/daily_wrap.py`)에 꽂을 섹션 데이터.

    렌더링은 하지 않는다 — 제목·핵심 3줄·최근 5일 표 데이터만 만든다.
    (통합은 daily_wrap 쪽 작업이다. 여기서는 계약만 고정해 둔다.)
    """
    summary = alpha_summary(series)
    bench = BENCHMARKS.get(market, "?")
    rows = list(series or [])

    def _pp(v: float | None, unit: str = "%p") -> str:
        return "표본 없음" if v is None else f"{v:+.2f}{unit}"

    if summary["up_our_avg_pct"] is None:
        up_line = f"지수 상승일 참여: 표본 부족({summary['up_days']}일 / 최소 {MIN_SAMPLE_DAYS}일)"
    else:
        up_line = (
            f"지수 상승일 참여: 우리 {_pp(summary['up_our_avg_pct'], '%')} vs "
            f"지수 {_pp(summary['up_bench_avg_pct'], '%')} "
            f"({summary['up_days']}일, 참여율 {summary['up_capture']:.2f}x)"
        )
    if summary["down_our_avg_pct"] is None:
        down_line = f"지수 하락일 방어: 표본 부족({summary['down_days']}일 / 최소 {MIN_SAMPLE_DAYS}일)"
    else:
        down_line = (
            f"지수 하락일 방어: 우리 {_pp(summary['down_our_avg_pct'], '%')} vs "
            f"지수 {_pp(summary['down_bench_avg_pct'], '%')} "
            f"({summary['down_days']}일, 방어율 {summary['down_capture']:.2f}x — 낮을수록 덜 잃었다)"
        )

    return {
        "title": f"{market} 지수 대비 초과수익 (벤치마크 {bench})",
        "benchmark": bench,
        "lines": [
            f"누적 알파: {_pp(summary['cum_alpha_pp'])} "
            f"(우리 {_pp(summary['cum_our_pct'], '%')} / 지수 {_pp(summary['cum_bench_pct'], '%')}, "
            f"{summary['n_days']}일 · 이긴 날 {summary['win_days']}일)",
            up_line,
            down_line,
        ],
        "rows": [
            {
                "date": d.isoformat(),
                "our_pct": round(our, 3),
                "bench_pct": round(bench_ret, 3),
                "alpha_pp": round(alpha, 3),
            }
            for d, our, bench_ret, alpha in rows[-5:]
        ],
        "summary": summary,
    }
