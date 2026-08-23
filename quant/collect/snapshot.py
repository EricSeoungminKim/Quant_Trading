"""소스들을 병렬 실행해 하나의 Snapshot으로 합친다.

**한 소스의 실패가 리포트를 죽이지 않는다** — 모든 예외를 소스 경계에서 잡아
SourceResult(ok=False)로 만든다. 이게 이 모듈의 유일한 존재 이유다.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult

MAX_WORKERS = 8
_ERROR_MAX = 160

# httpx 예외 메시지는 URL 전체를 담는다 — FRED/DART 처럼 키가 쿼리스트링에
# 실리는 API 는 그 키가 error 필드로 스냅샷 파일(과 백업 번들)에 영속화된다.
# 2026-08-16 실측: FRED 502 에러에 api_key=... 가 그대로 저장돼 있었다.
# 시크릿 파라미터 값만 가리고 나머지(series_id 등)는 디버깅용으로 남긴다.
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(api_key|apikey|crtfc_key|servicekey|token|secret|client_secret)=[^&\s'\"]+"
)


def _redact(text: str) -> str:
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", text)


def run_source(key: str, url: str, fn: Callable[[], dict]) -> SourceResult:
    started = time.monotonic()
    try:
        data, error, ok = fn(), None, True
    except Exception as e:  # 소스 경계 — 여기서 삼킨다
        data, ok = None, False
        error = _redact(f"{type(e).__name__}: {e}")[:_ERROR_MAX]
    return SourceResult(
        key=key,
        ok=ok,
        data=data,
        error=error,
        url=url,
        fetched_at=datetime.now(KST),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def collect(
    market: str,
    session_date: date,
    sources: dict[str, tuple[str, Callable[[], dict]]],
) -> Snapshot:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            key: pool.submit(run_source, key, url, fn)
            for key, (url, fn) in sources.items()
        }
        results = {key: f.result() for key, f in futures.items()}
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=session_date,
        generated_at=datetime.now(KST),
        results=results,
    )


def save_snapshot(snap: Snapshot, root: Path) -> Path:
    path = root / snap.market / f"{snap.session_date.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snap.to_json(), encoding="utf-8")
    return path


def load_snapshot(path: Path) -> Snapshot:
    return Snapshot.from_json(path.read_text(encoding="utf-8"))
