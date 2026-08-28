"""SmartFlowLogger — 키움 웹소켓의 "세력 신호" 프레임을 원장에 남기는 어댑터.

**왜**(2026-08-28 소유자 지시): "한국장은 세력이 움직이는 곳을 따라가면 승률이
높다." 우리 실측으로도 세력 추종의 일간 버전(frgn_accumulate, 외국인 수급 원장)이
유일하게 평가익을 내는 전략이다(매수건별 지수 대비 알파 중앙 +0.52%p, n=32).
그런데 **실시간** 세력 신호는 하나도 수집되지 않고 있었다 — 웹소켓은 `0B`(주식체결)
하나만 구독하고 그 프레임의 FID 도 `10`(현재가)·`20`(시간)만 파싱했다. 같은
프레임에 이미 실려 오는 `27`/`28`(최우선 호가)은 버렸고, `0w`(종목프로그램매매)·
`0F`(주식당일거래원)는 구독조차 안 했다.

**이건 수집이지 전략이 아니다.** 이 모듈은 아무것도 판단하지 않고, 알림도 보내지
않는다 — 나중에 전략을 짤 재료를 디스크에 쌓기만 한다.

저장 형식:

- 원장 ``data/ledger/smart_flow.jsonl`` — 한 줄
  ``{"ts": ISO8601, "symbol": ..., "kind": "program"|"broker"|"quote_l1",
  "fields": {...}}``
- raw 폴백 ``data/ticks/raw_kiwoom/{YYYY-MM-DD}.jsonl`` — 한 줄
  ``{"ts": ISO8601, "raw": <파싱 실패한 프레임 그대로>}``

raw 폴백이 있는 이유: `0w`/`0F` 의 **FID 이름 매핑표가 우리 문서에 없다**
(`docs/api/kiwoom/README.md` 5.5 는 `0B` 예시의 FID 몇 개만 verbatim 으로 옮겼고,
전체 표는 키움 원문 `kiwoom_docs/실시간시세.md` 에 있다고만 적혀 있다). 스펙에 없는
필드를 추측해서 이름 붙이면 그 추측이 그대로 원장에 굳는다 — 그래서 이 로거는
`0w`/`0F` 의 `values` 를 **FID 코드 그대로** 보존하고, 그나마도 형태가 어긋난
프레임은 raw 로 남긴다. 실서버 프레임을 받아 본 뒤에 파서를 고칠 수 있게 하려는
것이다.

핫패스 규칙(CLAUDE.md 거래 평면 불변식, `quant/adapters/tick_log.py` 와 동일):
``record()``/``record_raw()`` 는 메모리 버퍼 append 만 하고, 디스크는
``flush_if_due()`` 가 ``flush_seconds`` 경과 시 한 번의 append 로만 만진다.
네트워크 호출은 없다. 쓰기 실패는 예외를 삼키고 경고를 1회만 남긴다 — 어느
메서드도 예외를 밖으로 새어나가게 하지 않는다. **이 로거가 죽어도 시세 수신과
매매는 멈추지 않는다.**
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = Path("data/ledger/smart_flow.jsonl")
DEFAULT_RAW_DIR = Path("data/ticks/raw_kiwoom")

# 이 kind 만 "직전과 값이 같으면 버린다"를 적용한다. 최우선호가는 체결 프레임마다
# 따라오는데 대부분 직전과 동일하다 — 같은 값을 초당 수십 줄 쌓아도 정보가 늘지
# 않는다. 값이 **바뀐 순간**만 남기므로 정보 손실은 없고 용량만 준다.
_DEDUP_KIND = "quote_l1"


class SmartFlowLogger:
    """세력 신호 프레임을 버퍼링했다가 flush_seconds 마다 append-only 로 쓴다."""

    def __init__(
        self,
        ledger_path: Path = DEFAULT_LEDGER_PATH,
        raw_dir: Path = DEFAULT_RAW_DIR,
        *,
        flush_seconds: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self._ledger_path = Path(ledger_path)
        self._raw_dir = Path(raw_dir)
        self._flush_seconds = float(flush_seconds)
        self._enabled = bool(enabled)
        self._buffer: list[dict] = []
        self._raw_buffer: list[tuple[datetime, object]] = []
        # 첫 record() 가 flush 타이머의 기준 시각이 된다. None 이면 "아직 아무것도
        # 기록된 적이 없다" — flush_if_due 는 이때 항상 0 을 돌려준다.
        self._last_flush: datetime | None = None
        self._last_l1: dict[str, tuple] = {}
        self._write_failed_warned = False

    # ------------------------------------------------------------------ 기록
    def record(self, kind: str, symbol: str, ts: datetime, fields: dict) -> None:
        """메모리 버퍼에 append. 디스크에 닿지 않는다(핫패스 예산 보호)."""
        if not self._enabled:
            return
        if self._last_flush is None:
            self._last_flush = ts
        if kind == _DEDUP_KIND:
            key = tuple(sorted((str(k), v) for k, v in fields.items()))
            if self._last_l1.get(symbol) == key:
                return
            self._last_l1[symbol] = key
        self._buffer.append(
            {"ts": ts.isoformat(), "symbol": symbol, "kind": kind, "fields": fields}
        )

    def record_raw(self, payload: object, ts: datetime) -> None:
        """파싱하지 못한 프레임을 그대로 보존한다 — 버리면 파서를 고칠 근거가 없다."""
        if not self._enabled:
            return
        if self._last_flush is None:
            self._last_flush = ts
        self._raw_buffer.append((ts, payload))

    # ------------------------------------------------------------------ flush
    def flush_if_due(self, now: datetime) -> int:
        """마지막 flush 이후 flush_seconds 경과 시에만 디스크에 쓴다. 반환은 쓴 행 수."""
        if not self._enabled or self._last_flush is None:
            return 0
        if (now - self._last_flush).total_seconds() < self._flush_seconds:
            return 0
        self._last_flush = now
        return self._flush_to_disk()

    def close(self) -> int:
        """프로세스 종료 시 남은 버퍼를 flush_seconds 와 무관하게 즉시 쓴다."""
        if not self._enabled:
            return 0
        return self._flush_to_disk()

    def _flush_to_disk(self) -> int:
        written = 0
        if self._buffer:
            rows, self._buffer = self._buffer, []
            written += self._append(
                self._ledger_path,
                [json.dumps(r, ensure_ascii=False) for r in rows],
            )
        if self._raw_buffer:
            raws, self._raw_buffer = self._raw_buffer, []
            by_file: dict[Path, list[str]] = {}
            for ts, payload in raws:
                path = self._raw_dir / f"{ts.date().isoformat()}.jsonl"
                by_file.setdefault(path, []).append(
                    json.dumps({"ts": ts.isoformat(), "raw": payload},
                               ensure_ascii=False, default=str)
                )
            for path, lines in by_file.items():
                written += self._append(path, lines)
        return written

    def _append(self, path: Path, lines: list[str]) -> int:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return len(lines)
        except Exception:
            # 기록 실패가 시세 수신을 막으면 안 된다 — 삼키고 1회만 경고한다.
            if not self._write_failed_warned:
                self._write_failed_warned = True
                logger.warning(
                    "세력 신호 로그 쓰기 실패(%s) — 이후 실패는 조용히 무시한다",
                    path, exc_info=True,
                )
            return 0
