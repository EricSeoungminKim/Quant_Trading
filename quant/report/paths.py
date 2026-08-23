"""리포트 산출물 경로 헬퍼 — 스냅샷/출력/캐시/원장 디렉토리와 engine.json 경로.

Phase D 엔진 분리(2026-08-19, `docs/superpowers/specs/2026-08-19-engine-separation-design.md`)
— `quant/apps/report_cli.py`에서 그대로 옮겼다. 동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path


def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    return (
        root / "data" / "snapshots",
        root / "out",
        root / "data" / "cache",
        root / "data" / "ledger" / "mentions.jsonl",
    )


def _load_artifact(path: Path) -> dict | None:
    """뉴스 심화 파이프라인 아티팩트(`relations.json`/`sector_map.json`)를 읽는다.

    없거나 깨졌으면 `None` — 리포트 발행은 아티팩트 유무와 무관해야 한다.
    """
    import json as _json

    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _engine_json_path(out_root: Path, market: str, d: date) -> Path:
    """`write_machine`(render.py `_dated_dir`)과 같은 경로 규칙 — `out/YYYY/MM/DD/{market}_engine.json`."""
    return out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_engine.json"


def _close_engine_json_path(out_root: Path, market: str, d: date) -> Path:
    """마감 리포트(서브프로젝트 R) 엔진 JSON 경로 — `write_close_machine`
    (render.py `_dated_dir`)과 같은 규칙, 파일명만 `_close_engine.json`.
    아침 경로(`_engine_json_path`)와 절대 겹치지 않는다."""
    return out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_close_engine.json"
