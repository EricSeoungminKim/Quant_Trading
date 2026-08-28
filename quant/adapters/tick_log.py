"""TickLogger — 엔진이 사이클마다 읽는 시세를 초 단위로 디스크에 남기는 어댑터.

**왜**: Toss 1분봉은 4거래일 롤링이라 소급이 안 된다(2026-08-28 소유자 지시:
"1분봉 과거를 못 가져온다면, 우리가 데이터를 읽기 시작한 시점부터 기록하면 사실상
움직임이 보인다"). 엔진(quant.apps.cli paper)은 poll_seconds(기본 5초) 주기로
워치리스트 전 종목의 시세를 이미 읽고 있다 — 그 값을 버리지 않고 남기면 1분봉보다
12배 촘촘한 우리 고유 시계열이 매일 쌓인다. 다음 목표(파동 스캘핑 전략)의 재료다.

용량 실측 근거(주석): 관심종목 약 20종목 × 5초 주기 × 6.5시간(정규장, 23,400초)
≈ 20 × 23,400/5 ≈ 93,600행/일(≈94k행/일). 한 행이 JSON으로 약 65바이트라
94,000 × 65B ≈ 6.1MB/일. data/history의 월간 parquet(약 21MB)보다 하루치가 큰
편이라 **일자별 파일**로 쪼개 나중에 압축·정리(오래된 날짜 삭제/gzip)가 쉽게 한다.

거래 핫패스 규칙(CLAUDE.md 거래 평면 불변식): 디스크 I/O는 flush 시점 한 번의
append뿐이고, record()는 메모리 버퍼 append만 한다 — 네트워크 호출은 전혀 없다.
쓰기 실패는 예외를 삼키고 경고 로그를 1회만 남긴다(사이클마다 반복하면 로그가
폭발한다 — scalp_1m이 이미 분당 3.8초를 쓰고 느린 사이클 경고가 뜨는 상황이라
이 로거가 예산을 더 먹으면 안 된다). record()/flush_if_due()/close() 중 어느
것도 예외를 밖으로 새어나가게 하지 않는다 — 이 로거가 죽어도 매매는 멈추지 않는다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from quant.core.models import market_of_symbol

logger = logging.getLogger(__name__)


class TickLogger:
    """시세를 메모리에 버퍼링하다가 flush_seconds마다 append-only로 디스크에 쓴다.

    저장 경로: ``data/ticks/{market}/{YYYY-MM-DD}.jsonl``, 한 줄
    ``{"ts": ISO8601, "symbol": ..., "price": ...}``. market 판정은
    ``quant.core.models.market_of_symbol``을 재사용한다(loop/ledger/watch_scorer/
    kiwoom datafeed가 각자 구현하던 규칙을 다시 쪼개지 않는다).
    """

    def __init__(self, root: Path, *, flush_seconds: float = 30.0, enabled: bool = True) -> None:
        self._root = Path(root)
        self._flush_seconds = float(flush_seconds)
        self._enabled = bool(enabled)
        # (symbol, 초 단위로 자른 ts) -> (ts, symbol, price). 같은 초에 여러 번
        # 기록되면 마지막 값으로 덮는다 — 계약을 명확히 한 것일 뿐, 5초 주기
        # 폴링에서는 사실상 일어나지 않는다.
        self._buffer: dict[tuple[str, datetime], tuple[datetime, str, float]] = {}
        # 첫 record()가 flush 타이머의 기준 시각이 된다. None이면 "아직 아무것도
        # 기록된 적이 없다" — flush_if_due는 이때 항상 0을 돌려준다.
        self._last_flush: datetime | None = None
        self._write_failed_warned = False

    def record(self, symbol: str, price: float, ts: datetime) -> None:
        """메모리 버퍼에 append. 디스크에 닿지 않는다(핫패스 예산 보호)."""
        if not self._enabled:
            return
        if self._last_flush is None:
            self._last_flush = ts
        self._buffer[(symbol, ts.replace(microsecond=0))] = (ts, symbol, price)

    def flush_if_due(self, now: datetime) -> int:
        """마지막 flush 이후 flush_seconds 경과 시에만 디스크에 쓴다. 반환은 쓴 행 수."""
        if not self._enabled or self._last_flush is None:
            return 0
        if (now - self._last_flush).total_seconds() < self._flush_seconds:
            return 0
        self._last_flush = now
        return self._flush_to_disk()

    def close(self) -> None:
        """프로세스 종료 시 남은 버퍼를 flush_seconds와 무관하게 즉시 쓴다."""
        if not self._enabled:
            return
        self._flush_to_disk()

    def _flush_to_disk(self) -> int:
        if not self._buffer:
            return 0
        rows = list(self._buffer.values())
        self._buffer.clear()
        by_file: dict[Path, list[str]] = {}
        for ts, symbol, price in rows:
            market = market_of_symbol(symbol)
            path = self._root / market / f"{ts.date().isoformat()}.jsonl"
            line = json.dumps({"ts": ts.isoformat(), "symbol": symbol, "price": price}, ensure_ascii=False)
            by_file.setdefault(path, []).append(line)
        written = 0
        for path, lines in by_file.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                written += len(lines)
            except Exception:
                # 기록 실패가 매매를 막으면 안 된다 — 삼키고 1회만 경고한다.
                if not self._write_failed_warned:
                    self._write_failed_warned = True
                    logger.warning(
                        "틱 로그 쓰기 실패(%s) — 이후 실패는 조용히 무시한다", path, exc_info=True,
                    )
        return written
