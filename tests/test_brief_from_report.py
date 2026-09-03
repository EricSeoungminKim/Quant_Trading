"""brief_from_report.py의 FRGN/FRGN_EXIT 재평가 대상 확장(2026-09-03 P1 수정) 테스트.

server/scripts/brief_from_report.py는 패키지가 아닌 독립 스크립트라 sys.path에 그
디렉토리를 얹어 직접 import한다(tests/test_tg_bridge_watchlist.py와 같은 관례).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "server" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import brief_from_report  # noqa: E402


def _write_watchlist(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"symbols": entries}, allow_unicode=True), encoding="utf-8")


def test_already_frgn_tagged_reads_frgn_and_frgn_exit_symbols(tmp_path):
    wl = tmp_path / "watchlist.yaml"
    _write_watchlist(wl, [
        {"symbol": "001450", "tags": ["FRGN"]},
        {"symbol": "000660", "tags": ["EVENT", "FRGN_EXIT"]},
        {"symbol": "066570", "tags": ["EVENT"]},  # FRGN/FRGN_EXIT 없음 — 제외
        {"symbol": "005930"},  # 태그 없음 — 제외
    ])
    assert brief_from_report._already_frgn_tagged(str(wl)) == {"001450", "000660"}


def test_already_frgn_tagged_missing_file_returns_empty_set(tmp_path):
    """관심종목 파일이 없어도(첫 실행 등) 예외 없이 빈 집합 — 브리핑 발행을
    막으면 안 된다(모듈 docstring)."""
    assert brief_from_report._already_frgn_tagged(str(tmp_path / "no_such_file.yaml")) == set()


def test_already_frgn_tagged_no_tags_key_returns_empty_set(tmp_path):
    wl = tmp_path / "watchlist.yaml"
    _write_watchlist(wl, [{"symbol": "005930"}])
    assert brief_from_report._already_frgn_tagged(str(wl)) == set()
