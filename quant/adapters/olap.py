"""Parquet 위의 분석 질의 (DuckDB).

## 왜 MySQL 이 아닌가

봉 데이터는 이미 `data/history/{symbol}/{interval}/{year}/{month}.parquet` 로
있다. DuckDB 는 **그 파일을 그대로 테이블로 읽는다** — 적재 단계가 없고, 데몬도
없고(라이브러리다), 1.8GB 박스에서 메모리를 상주로 먹지 않는다. OHLCV 를
MySQL 에 넣는 건 행 저장소에 시계열을 넣는 것이라 구조적으로 나쁜 선택이다.

MySQL 은 관계(기사↔종목↔선정↔체결)를 담고, 여기는 시계열을 담는다.

## 이 모듈의 목적은 "커버리지"다

적합도 함수가 답해야 하는 질문 중 하나: **이 구간을 점수 매길 자격이 있나.**
봉이 빠진 구간에서 돌린 백테스트는 숫자가 멀쩡해 보이지만 근거가 없다. 하네스는
그 숫자를 그대로 믿고 "이 변형이 낫다"고 말한다.

DuckDB 가 없으면 `None` 을 돌려준다 — **"커버리지 정상"이 아니라 "모른다"**다.
호출자는 그 둘을 구분해야 한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HISTORY_ROOT = Path("data/history")


@dataclass(frozen=True)
class Coverage:
    """한 심볼·간격의 봉 커버리지."""

    symbol: str
    interval: str
    n_bars: int
    first_ts: str | None
    last_ts: str | None
    n_days: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "interval": self.interval,
            "n_bars": self.n_bars, "first_ts": self.first_ts,
            "last_ts": self.last_ts, "n_days": self.n_days,
        }


def _connect():
    """DuckDB 연결 또는 None. 미설치가 예외가 되면 안 된다 — 선택 의존성이다."""
    try:
        import duckdb
    except ImportError:
        log.debug("duckdb 미설치 — 커버리지 조회를 건너뛴다")
        return None
    return duckdb.connect(":memory:")


def glob_for(symbol: str, interval: str, root: Path | None = None) -> str:
    r = (root or HISTORY_ROOT) / symbol / interval
    return str(r / "**" / "*.parquet")


def coverage(symbol: str, interval: str, root: Path | None = None) -> Coverage | None:
    """봉 커버리지. DuckDB 가 없거나 파일이 없으면 None(= "모른다").

    **빈 결과를 `n_bars=0` 으로 돌려주지 않는다** — "봉이 0개"와 "조회할 수
    없었다"는 다른 사건이고, 섞으면 커버리지 게이트가 조용히 무력해진다.
    """
    con = _connect()
    if con is None:
        return None
    pattern = glob_for(symbol, interval, root)
    try:
        # **union_by_name 이 필수다.** 백필은 데이터가 없는 달에도 0행 파일을
        # 남기는데, 빈 DataFrame 은 DatetimeIndex 를 잃고 RangeIndex 가 된다 —
        # 그래서 그 파일에는 `ts` 컬럼이 아예 없다. pandas 는 빈 프레임을 무해하게
        # 흘려보내지만 DuckDB 는 스키마 불일치로 **글롭 전체를 실패**시킨다
        # (2026-08-13 실측: 1,030개 중 14개가 그런 파일이었고 TQQQ/15m 커버리지
        # 조회가 통째로 None 을 냈다).
        row = con.execute(
            "SELECT count(ts), min(ts), max(ts), count(DISTINCT CAST(ts AS DATE)) "
            f"FROM read_parquet('{pattern}', union_by_name=true)"
        ).fetchone()
    except Exception as e:  # 파일 없음·스키마 불일치 등
        log.debug("커버리지 조회 실패(%s): %s", symbol, type(e).__name__)
        return None
    finally:
        con.close()
    if not row or row[0] is None:
        return None
    return Coverage(
        symbol=symbol, interval=interval, n_bars=int(row[0]),
        first_ts=None if row[1] is None else str(row[1]),
        last_ts=None if row[2] is None else str(row[2]),
        n_days=int(row[3] or 0),
    )


def query(sql: str, params: list | None = None) -> list[tuple] | None:
    """임의 SQL. 에이전트가 봉 데이터를 탐색할 때 쓴다.

    `read_parquet('data/history/**/*.parquet')` 처럼 파일을 직접 가리킨다 —
    적재된 테이블이 없으므로 스키마를 외울 필요가 없다.
    """
    con = _connect()
    if con is None:
        return None
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception as e:
        log.warning("DuckDB 질의 실패: %s: %s", type(e).__name__, e)
        return None
    finally:
        con.close()
