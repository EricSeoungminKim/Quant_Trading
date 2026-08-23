"""베이스라인 = watch_scorer TREND 프로필의 적용 범위 확장.
핵심 계약: 채점 불가는 None (0 이 아니다 — 0 은 '최하위 평가'가 되어 IC 를 오염)."""
import pandas as pd
from datetime import date
from quant.analyze.baseline import baseline_score


def _df(n=60, up=True):
    idx = pd.bdate_range(end="2026-08-14", periods=n)
    base = pd.Series(range(n), index=idx, dtype=float)
    close = 100 + (base if up else -base) * 0.5
    return pd.DataFrame({"open": close - 0.2, "high": close + 0.5,
                         "low": close - 0.5, "close": close,
                         "volume": [1_000_000] * n}, index=idx)


def test_uptrend_scores_above_downtrend():
    up, down = baseline_score(_df(up=True)), baseline_score(_df(up=False))
    assert up is not None and down is not None and up > down  # 순위를 만든다


def test_insufficient_rows_is_none_not_zero():
    assert baseline_score(_df(n=10)) is None


def test_incomplete_today_bar_is_dropped():
    df = _df()
    # 오늘 날짜의 미완성 봉을 붙여도 점수가 어제 기준과 같아야 한다
    today = pd.Timestamp(date.today())
    partial = df.iloc[[-1]].rename(index={df.index[-1]: today})
    with_partial = pd.concat([df, partial])
    assert baseline_score(with_partial, today=date.today()) == baseline_score(df)
