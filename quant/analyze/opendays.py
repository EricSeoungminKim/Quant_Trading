"""개장일 판정 — 달력(공휴일표)이 아니라 앵커 종목의 실제 일봉 데이터로 (서브프로젝트 G).

**왜 달력이 아닌가.** 공휴일표는 유지보수 대상이다 — 대체휴일·임시공휴일을 놓치면
조용히 틀린다. 반면 앵커(KR=069500 KODEX200, US=QQQ)의 일봉은 그 시장이 실제로
열렸을 때만 생긴다. "마지막 봉이 언제냐"를 물으면 공휴일표 없이 정답이 나온다.

**안전한 방향은 창이 넓어지는 쪽이다.** 앵커 데이터가 없거나 파손되면 `None`을
반환한다 — 0 이나 오늘 날짜로 위장하지 않는다. 호출부(`window_dates`)는 `None`을
받으면 빈 리스트를 내 집계를 건너뛰므로, 실패가 과다 집계가 아니라 무집계로
안전하게 향한다.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd


def anchor_dir_for(market: str, root: Path) -> Path:
    """개장일 판정에 쓸 앵커 종목의 일봉 디렉토리.

    KR 은 069500(KODEX200) — 코스피 정규장의 대리 지표. US 는 QQQ — 이미
    `quant/trade/regime/provider.py`가 국면 판정에 쓰는 것과 같은 앵커라 신선도
    보장이 이미 있다.
    """
    if market == "KR":
        return root / "data" / "history" / "069500" / "1d"
    if market == "US":
        return root / "data" / "history" / "QQQ" / "1d"
    raise ValueError(f"알 수 없는 시장: {market!r}")


def last_open_day(anchor_dir: Path, today: date) -> date | None:
    """`today` 미만(strictly before)의 마지막 개장일. 판정 불가 시 `None`.

    파티션은 `YYYY/MM.parquet` 로 월 단위이므로, 최신 2개만 읽어도 "오늘 미만
    마지막 봉"을 찾기에 충분하다(한 달 넘게 무봉일 리 없다) — 매번 전체 이력을
    읽지 않는다.

    빈 파일은 버린다. 백필은 데이터가 없는 달에도 0행 파일을 남기는데, 빈
    DataFrame 은 DatetimeIndex 를 잃고 RangeIndex 가 된다 — 그대로 concat 하면
    인덱스가 혼합 타입이 되어 터진다(`quant/trade/regime/provider.py`
    `_load_qqq_daily_closes`가 이미 겪은 결함과 같은 모양).

    봉 타임스탬프는 tz-aware 다. QQQ 실측 봉은 `04:00 UTC` 인데 이게 그 거래일
    (예: 2026-08-14 04:00+00 = 08-14 거래) 이므로, **UTC 기준 date()** 로 환산한다
    (KST 로 바꾸면 다음날 자정 근처로 밀려 날짜가 하루 어긋난다). KR(069500.KS
    via yfinance)의 일봉은 Yahoo가 tz-aware Asia/Seoul 00:00으로 내려주는데,
    `quant/collect/quotes/yf_source.py`의 `fetch()`가 1d 인터벌에 한해 그 로컬
    캘린더 날짜를 UTC 자정으로 재해석해서 저장하므로(단순 tz_convert가 아님 —
    그러면 KST -9h만큼 하루 당겨진다) 마찬가지로 UTC date 로 거래일이 그대로 맞는다.
    """
    parts = sorted(anchor_dir.glob("*/*.parquet"))
    if not parts:
        return None
    try:
        frames = [d for d in (pd.read_parquet(p) for p in parts[-2:]) if not d.empty]
        if not frames:
            return None
        df = pd.concat(frames)
    except Exception:
        return None

    if df.empty:
        return None

    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")

    bar_dates = idx.date
    before_today = [d for d in bar_dates if d < today]
    if not before_today:
        return None
    return max(before_today)


def window_dates(last_open: date | None, today: date, cap: int = 7) -> list[date]:
    """`last_open` 다음날부터 `today` 전날까지, 오름차순. 상한 `cap`일.

    `last_open is None`(판정 불가)이면 `[]` — 집계 자체를 건너뛴다(기존 동작
    유지). 창이 `cap`을 넘으면 **최근 cap 일만** 남긴다 — 잘렸다는 사실을 로그로
    남기는 건 호출부 책임이다(이 함수는 순수 함수라 로깅하지 않는다).
    """
    if last_open is None:
        return []
    dates = []
    d = last_open + timedelta(days=1)
    while d < today:
        dates.append(d)
        d += timedelta(days=1)
    if len(dates) > cap:
        dates = dates[-cap:]
    return dates
