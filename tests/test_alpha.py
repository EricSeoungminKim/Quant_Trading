"""알파(지수 대비 초과수익) 추적 — 전부 오프라인, 손계산 대조.

이 테스트가 지키는 것은 소유자의 요구 그 자체다: "오를 땐 더 얻고, 잃을 땐 덜
잃는다"가 **두 개의 분리된 숫자**로 나와야 하고, 표본이 모자라면 숫자 대신
None 이 나와야 한다(3일치로 만든 방어율은 근거가 아니라 착시다).
"""
from datetime import date, timedelta

import pytest

from quant.control.alpha import (
    BENCHMARKS,
    MIN_SAMPLE_DAYS,
    alpha_series,
    alpha_summary,
    benchmark_returns,
    daily_returns,
    wrap_section,
)


# `cli equity-snapshot` 이 실제로 쓰는 행 형식(quant/apps/cli.py cmd_equity_snapshot).
# 필드 이름을 추측하지 않기 위해 그 코드에서 그대로 옮겼다.
def _equity_row(day: str, market: str, total: float, bench: float | None = None) -> dict:
    return {
        "date": day,
        "market": market,
        "total_krw": total,
        "books": {"donchian": total / 2, "orb_scan": total / 2},
        "marked": 2,
        "degraded": [],
        "benchmark_symbol": BENCHMARKS[market],
        "benchmark_close": bench,
        "recorded_at": f"{day}T15:40:00+09:00",
    }


def test_daily_returns_hand_computed():
    """①-1 일간 수익률 — 손계산 대조 + 첫날은 전일이 없어 빠진다."""
    rows = [
        _equity_row("2026-08-24", "US", 10_000_000.0),
        _equity_row("2026-08-25", "US", 10_100_000.0),   # +1.00%
        _equity_row("2026-08-26", "US", 9_898_000.0),    # -2.00%
    ]
    out = daily_returns(rows)
    assert [(d, m) for d, m, _ in out] == [
        (date(2026, 8, 25), "US"), (date(2026, 8, 26), "US"),
    ]
    assert out[0][2] == pytest.approx(1.0)
    assert out[1][2] == pytest.approx(-2.0)


def test_daily_returns_separates_markets_and_last_row_wins():
    """①-2 시장은 섞이지 않고, 같은 (date, market) 중복은 마지막이 이긴다(원장 관례)."""
    rows = [
        _equity_row("2026-08-24", "US", 1_000.0),
        _equity_row("2026-08-24", "KR", 2_000.0),
        _equity_row("2026-08-25", "US", 1_100.0),
        _equity_row("2026-08-25", "US", 1_200.0),  # 재실행 — 이게 이긴다
        _equity_row("2026-08-25", "KR", 2_100.0),
    ]
    out = daily_returns(rows)
    by_market = {m: r for _, m, r in out}
    assert by_market["US"] == pytest.approx(20.0)  # 1000 → 1200
    assert by_market["KR"] == pytest.approx(5.0)


def test_benchmark_returns_from_bars():
    """①-3 벤치마크 일봉 → 일간 수익률(손계산)."""
    bars = [
        (date(2026, 8, 24), 500.0),
        (date(2026, 8, 25), 505.0),   # +1.00%
        (date(2026, 8, 26), 494.9),   # -2.00%
    ]
    out = benchmark_returns(bars)
    assert [d for d, _ in out] == [date(2026, 8, 25), date(2026, 8, 26)]
    assert out[0][1] == pytest.approx(1.0)
    assert out[1][1] == pytest.approx(-2.0)
    # 매핑 형식(파케이 행을 dict 로 넘기는 경로)도 같은 결과.
    as_dicts = [{"date": d.isoformat(), "close": c} for d, c in bars]
    assert benchmark_returns(as_dicts) == out


def test_alpha_series_uses_date_intersection_only():
    """② 날짜 교집합만 — 한쪽만 있는 날을 0으로 메우면 알파가 조용히 왜곡된다."""
    ours = [
        (date(2026, 8, 25), 1.0),
        (date(2026, 8, 26), -1.0),
        (date(2026, 8, 27), 3.0),   # 지수 데이터 없음 → 빠져야 한다
    ]
    bench = [
        (date(2026, 8, 24), 0.5),   # 우리 데이터 없음 → 빠져야 한다
        (date(2026, 8, 26), -3.0),
        (date(2026, 8, 25), 0.4),   # 순서가 섞여 들어와도 정렬돼 나온다
    ]
    series = alpha_series(ours, bench)
    assert [d for d, *_ in series] == [date(2026, 8, 25), date(2026, 8, 26)]
    assert series[0][3] == pytest.approx(0.6)   # 1.0 - 0.4
    assert series[1][3] == pytest.approx(2.0)   # -1.0 - (-3.0)


def test_alpha_series_accepts_daily_returns_triples():
    """②-2 daily_returns() 의 (날짜, 시장, %) 3튜플을 그대로 넘길 수 있다."""
    ours = daily_returns([
        _equity_row("2026-08-24", "US", 100.0),
        _equity_row("2026-08-25", "US", 101.0),
    ])
    series = alpha_series(ours, [(date(2026, 8, 25), 0.5)])
    assert series == [(date(2026, 8, 25), pytest.approx(1.0), pytest.approx(0.5),
                       pytest.approx(0.5))]


def _series(pairs):
    """(우리%, 지수%) 목록 → alpha_series 형식."""
    return [
        (date(2026, 8, 1) + timedelta(days=i), our, bench, our - bench)
        for i, (our, bench) in enumerate(pairs)
    ]


def test_alpha_summary_splits_up_and_down_days():
    """③ 상승일 참여와 하락일 방어는 **분리된 두 숫자**다 — 뭉치면 답이 안 나온다.

    지수 +2%인 날 5개(우리 +3%), 지수 -2%인 날 5개(우리 -1%).
    상승일 참여율 3/2 = 1.5x(더 먹었다), 하락일 방어율 -1/-2 = 0.5x(덜 잃었다).
    """
    series = _series([(3.0, 2.0)] * 5 + [(-1.0, -2.0)] * 5)
    s = alpha_summary(series)

    assert s["up_days"] == 5 and s["down_days"] == 5
    assert s["up_our_avg_pct"] == pytest.approx(3.0)
    assert s["up_bench_avg_pct"] == pytest.approx(2.0)
    assert s["up_capture"] == pytest.approx(1.5)
    assert s["down_our_avg_pct"] == pytest.approx(-1.0)
    assert s["down_bench_avg_pct"] == pytest.approx(-2.0)
    assert s["down_capture"] == pytest.approx(0.5)
    # 누적 알파는 복리로 계산한다(단순 합산이 아니다).
    cum_our = (1.03 ** 5) * (0.99 ** 5) - 1
    cum_bench = (1.02 ** 5) * (0.98 ** 5) - 1
    assert s["cum_alpha_pp"] == pytest.approx((cum_our - cum_bench) * 100)
    assert s["win_days"] == 10


def test_alpha_summary_flat_benchmark_days_belong_to_neither_bucket():
    """③-2 지수가 정확히 0인 날은 참여도 방어도 아니다."""
    s = alpha_summary(_series([(1.0, 0.0)] * 7))
    assert s["up_days"] == 0 and s["down_days"] == 0
    assert s["n_days"] == 7


def test_alpha_summary_returns_none_below_sample_floor():
    """④ 표본 미달(각 5일 미만)이면 그쪽 값은 None — 숫자처럼 생긴 착시를 안 만든다."""
    series = _series([(3.0, 2.0)] * (MIN_SAMPLE_DAYS - 1) + [(-1.0, -2.0)] * MIN_SAMPLE_DAYS)
    s = alpha_summary(series)
    assert s["up_days"] == MIN_SAMPLE_DAYS - 1
    assert s["up_our_avg_pct"] is None
    assert s["up_bench_avg_pct"] is None
    assert s["up_capture"] is None
    # 하락일 쪽은 표본을 채웠으므로 살아 있다.
    assert s["down_our_avg_pct"] == pytest.approx(-1.0)
    assert s["down_capture"] == pytest.approx(0.5)


def test_empty_inputs_are_safe():
    """⑤ 빈 데이터 — 예외 없이 비어 있음을 말한다(원장이 아직 안 쌓인 날)."""
    assert daily_returns([]) == []
    assert daily_returns(None) == []
    assert benchmark_returns([]) == []
    assert alpha_series([], []) == []

    s = alpha_summary([])
    assert s["n_days"] == 0
    assert s["cum_alpha_pp"] is None
    assert s["up_our_avg_pct"] is None and s["down_our_avg_pct"] is None

    section = wrap_section([], "US")
    assert section["benchmark"] == "QQQ"
    assert section["rows"] == []
    assert len(section["lines"]) == 3
    assert any("표본" in line for line in section["lines"])


def test_parses_real_equity_curve_row_shape():
    """⑥ 실제 원장 행 형식 파싱 — 필드 이름을 추측하지 않았음을 고정한다.

    아래 두 줄은 `cmd_equity_snapshot` 이 쓰는 JSON 그대로다(2026-08-28 벤치마크
    동반 기록 추가 이전 행 + 이후 행). **옛 행에 benchmark_* 가 없어도 자본
    수익률 계산은 그대로 돌아야 한다** — 스키마 추가는 additive 다.
    """
    import json

    old_row = json.loads(
        '{"date": "2026-08-26", "market": "US", "total_krw": 10096230.75, '
        '"books": {"donchian": 5048115.37, "orb_scan": 5048115.38}, '
        '"marked": 3, "degraded": ["SQQQ"], "recorded_at": "2026-08-27T06:15:03+09:00"}'
    )
    new_row = json.loads(
        '{"date": "2026-08-27", "market": "US", "total_krw": 10196000.0, '
        '"books": {"donchian": 5098000.0, "orb_scan": 5098000.0}, '
        '"marked": 3, "degraded": [], "benchmark_symbol": "QQQ", '
        '"benchmark_close": 611.23, "recorded_at": "2026-08-28T06:15:02+09:00"}'
    )
    junk = {"note": "형식이 다른 줄은 건너뛴다"}

    out = daily_returns([old_row, junk, new_row])
    assert len(out) == 1
    d, market, ret = out[0]
    assert (d, market) == (date(2026, 8, 27), "US")
    assert ret == pytest.approx((10196000.0 / 10096230.75 - 1) * 100)

    # 동반 기록된 종가는 벤치마크 수익률 입력으로 그대로 쓸 수 있다.
    bars = [(r["date"], r["benchmark_close"]) for r in (new_row,) if r.get("benchmark_close")]
    assert benchmark_returns(bars) == []  # 점 1개 — 전일이 없으니 수익률도 없다
    assert benchmark_returns([("2026-08-26", 605.0), *bars])[0][1] == pytest.approx(
        (611.23 / 605.0 - 1) * 100
    )


def test_wrap_section_shape_for_daily_wrap():
    """마감 HTML 통합용 계약 — 제목·핵심 3줄·최근 5일 표(통합은 daily_wrap 쪽)."""
    series = _series([(3.0, 2.0)] * 5 + [(-1.0, -2.0)] * 5)
    section = wrap_section(series, "KR")

    assert section["benchmark"] == "069500"
    assert "069500" in section["title"]
    assert len(section["lines"]) == 3
    assert len(section["rows"]) == 5  # 최근 5일만
    assert section["rows"][-1]["date"] == series[-1][0].isoformat()
    assert set(section["rows"][0]) == {"date", "our_pct", "bench_pct", "alpha_pp"}
    assert section["summary"]["up_capture"] == pytest.approx(1.5)
