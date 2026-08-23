"""국면(regime)은 낡은 봉을 현재가로 쓰지 않는다.

**2026-08-13 실측 사고.** EC2 라이브 US 국면이 `2026-07-31` 종가로 계산되고 있었다.
백필 크론이 애초에 없어서 `data/history/QQQ/1d/` 가 13일간 멈춰 있었는데,
`_load_qqq_daily_closes()` 는 신선도를 보지 않고 그 시리즈를 넘겼고
`qqq_trend_score()` 는 마지막 봉을 "현재가"로 삼아 MA20 과 비교했다. 결과가
`risk_multiplier`(방어 0.5x / 중립 1.0x / 공격 1.3x)로 **라이브 사이징에 곱해졌고**,
상태는 스스로를 `degraded=False`(정상)라고 보고했다.

이 저장소에서 반복되는 결함 모양이다 — **"모른다"를 "이상 없음"으로 읽는 것.**
백필 크론을 넣는 것만으로는 부족하다. 크론은 언젠가 또 실패하고, 그때도 사이징은
계속된다. 그래서 가드는 코드에 있어야 한다.

지표 모듈의 기존 계약을 그대로 쓴다: `score=None` 은 "판단 못 함"이고 집계에서
제외된다 (0 = "판단했는데 중립"과 구분된다).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant.trade.regime.provider import STALE_DAILY_BARS_AFTER, RegimeProvider

KST = ZoneInfo("Asia/Seoul")


# ── 도우미 ────────────────────────────────────────────────────────────────

def _write_qqq_daily(root: Path, closes: list[float], last_bar: str,
                     tz: str | None = "UTC") -> None:
    """마지막 봉이 `last_bar` 인 QQQ 일봉 파티션. 실데이터처럼 tz-aware 가 기본.

    구 파티션은 tz-naive 인 것도 있어서 `tz=None` 으로 그 경우도 시험한다.
    """
    idx = pd.bdate_range(end=last_bar, periods=len(closes), tz=tz)
    df = pd.DataFrame({
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": [1000.0] * len(closes),
    }, index=idx)
    for (year, month), part in df.groupby([df.index.year, df.index.month]):
        path = root / "QQQ" / "1d" / f"{year:04d}" / f"{month:02d}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.to_parquet(path)


def _aggressive_closes() -> list[float]:
    """신선하면 `aggressive` 가 나오는 종가열.

    앞 60봉은 ±2 로 흔들려 장기 변동성을 키우고, 뒤 20봉은 매끄럽게 올라간다 →
    추세 +1(MA20 상회) + 변동성 +1(단기 안정) = 2 ≥ aggressive_min.
    """
    noisy = [98.0 if i % 2 else 102.0 for i in range(60)]
    ramp = [100.0 + 0.75 * i for i in range(1, 21)]
    return noisy + ramp


def _provider(root: Path, closes: list[float], last_bar: str, now: datetime,
              tz: str | None = "UTC") -> RegimeProvider:
    _write_qqq_daily(root / "history", closes, last_bar, tz=tz)
    return RegimeProvider(
        # 2026-08-19: aggressive 승격 전용 게이트(aggressive_min_valid 기본 3 /
        # aggressive_min_sources 기본 2, quant/trade/regime/provider.py)가 새로
        # 생겼다. 이 파일은 원격 지표를 전부 제외하고 QQQ 지표 2개(qqq_trend/
        # qqq_volatility, 둘 다 같은 원천 QQQ 1개)만으로 신선도/파티션 견고성을
        # 시험한다 — 그 목적과 새 게이트는 무관하므로, 여기서는 명시적으로 낮춰
        # 격리한다(신선도 테스트가 새 정보-요건 게이트를 우연히 시험하게 두지
        # 않는다). 게이트 자체는 tests/test_regime.py가 검증한다.
        settings={"regime": {"aggressive_min_valid": 2, "aggressive_min_sources": 1}},
        indicator_client=None,   # 원격 지표 전부 제외 — QQQ 지표만 남긴다
        bitcoin_adapter=None,
        history_dir=root / "history",
        state_path=root / "state" / "regime.json",
        now_fn=lambda: now,
    )


NOW = datetime(2026, 8, 13, 8, 0, tzinfo=KST)


# ── 핵심: 같은 봉, 다른 시계 ──────────────────────────────────────────────

def test_same_bars_are_aggressive_when_fresh_and_neutral_when_stale(tmp_path):
    """**바이트가 같고 시계만 다르다.** 낡았으면 공격적으로 사이징하지 않는다.

    이게 실제로 일어난 일이다 — 07-31 봉으로 08-13 의 US 세션을 판단했다.
    """
    closes = _aggressive_closes()
    fresh = _provider(tmp_path / "fresh", closes, last_bar="2026-08-12", now=NOW)
    stale = _provider(tmp_path / "stale", closes, last_bar="2026-07-31", now=NOW)

    assert fresh.refresh().label == "aggressive"

    state = stale.refresh()
    assert state.label == "neutral"
    assert state.risk_multiplier == 1.0
    assert state.degraded is True


def test_stale_reason_names_the_last_bar_and_the_age(tmp_path):
    """사유가 원인을 말해야 한다.

    "로드 실패"로 뭉개면 백필이 멈춘 걸 못 찾는다 — 이 저장소는 이미 그 값을
    치렀다(없는 간격 요청이 타임존 에러로 위장한 사건, ce6a755).
    """
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-07-31", now=NOW)

    reasons = " / ".join(provider.refresh().reasons)

    # 경과일은 테스트가 직접 계산한다 — 상수로 박으면 봉 스탬프 시각(합성은 자정,
    # 실데이터는 04:00Z)에 따라 하루씩 어긋나 실패 원인을 오해하게 된다.
    days = (pd.Timestamp(NOW).tz_convert("UTC") - pd.Timestamp("2026-07-31", tz="UTC")).days
    assert "2026-07-31" in reasons
    assert f"{days}일 경과" in reasons
    assert "백필" in reasons


def test_stale_bars_exclude_both_qqq_indicators(tmp_path):
    """추세와 변동성 둘 다 같은 봉에서 나온다 — 하나만 막으면 반쯤 낡은 판단이 남는다."""
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-07-31", now=NOW)

    reasons = provider.refresh().reasons

    assert sum(1 for r in reasons if "낡음" in r) == 2


# ── 오탐 방지: 주말·연휴는 정상이다 ───────────────────────────────────────

def test_weekend_gap_is_not_stale(tmp_path):
    """금요일 봉으로 월요일 세션을 판단하는 건 정상이다 — US 장이 주말에 안 열린다.

    오탐하는 가드는 꺼진다. 꺼진 가드는 없는 가드다.
    """
    monday = datetime(2026, 8, 17, 22, 10, tzinfo=KST)  # US 개장 전 refresh 시각
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-08-14", now=monday)

    state = provider.refresh()

    assert state.degraded is False
    assert not [r for r in state.reasons if "낡음" in r]


def test_long_holiday_weekend_is_not_stale(tmp_path):
    """목·금 연휴(추수감사절형) 후 월요일 — 최악의 정상 공백이 임계 안에 들어온다."""
    monday = datetime(2026, 11, 30, 22, 10, tzinfo=KST)
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-11-25", now=monday)

    assert provider.refresh().degraded is False


def test_threshold_is_wide_enough_for_a_long_weekend():
    """임계값이 조용히 좁아지면 위 오탐 테스트가 왜 통과했는지 알 수 없게 된다."""
    assert STALE_DAILY_BARS_AFTER.days >= 6


# ── 파티션 이상에 죽지 않는다 (거래 평면이다) ─────────────────────────────

def test_tz_naive_partition_does_not_crash(tmp_path):
    """구 파티션은 tz-naive 다. 거래 평면에서 예외가 나면 사이클이 멈춘다."""
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-08-12",
                         now=NOW, tz=None)

    assert provider.refresh().label == "aggressive"


def test_empty_partition_does_not_break_freshness(tmp_path):
    """빈 parquet 은 DatetimeIndex 를 잃고 RangeIndex 가 된다 — 섞어 concat 하면
    인덱스가 혼합 타입이 되고 신선도 판정이 터진다. history.py 가 ce6a755 에서 고친
    것과 같은 결함이 이 경로에는 남아 있었다."""
    provider = _provider(tmp_path, _aggressive_closes(), last_bar="2026-08-12", now=NOW)
    empty = tmp_path / "history" / "QQQ" / "1d" / "2025" / "01.parquet"
    empty.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(columns=["open", "high", "low", "close", "volume"]).to_parquet(empty)

    assert provider.refresh().label == "aggressive"
