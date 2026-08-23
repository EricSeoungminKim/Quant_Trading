"""개장일 집계(G Task 3) — 마지막 개장일 이후 휴장 기간의 engine.json 을 오늘
payload 뒤에 병합한다.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.analyze.carryover import merge_carryover
from quant.analyze.opendays import anchor_dir_for, last_open_day, window_dates

from quant.report.paths import _engine_json_path, _load_artifact


def _apply_carryover(payload: dict, snap, root: Path, out_root: Path) -> dict:
    """개장일 집계(G Task 3) — 마지막 개장일 이후 휴장 기간의 engine.json 을
    오늘 payload 뒤에 병합한다.

    앵커(`opendays.anchor_dir_for`) 데이터가 없어 개장일 판정이 안 되면
    병합하지 않고 **기존 동작**(오늘 payload 그대로)을 유지한다 — 안전한
    방향은 집계를 넓히는 쪽이지 억지로 좁히는 쪽이 아니다(모듈 계약).
    """
    market = snap.market
    today = snap.session_date
    last_open = last_open_day(anchor_dir_for(market, root), today)
    if last_open is None:
        print(f"개장일 판정 불가(앵커 데이터 없음) — 이월 병합 생략: {market}",
              file=sys.stderr)
        return payload

    prior: list[tuple] = []
    for d in window_dates(last_open, today):
        art = _load_artifact(_engine_json_path(out_root, market, d))
        if art is None:
            print(f"이월 병합: {d.isoformat()} {market}_engine.json 없음 — 건너뜀",
                  file=sys.stderr)
            continue
        prior.append((d, art))

    return merge_carryover(payload, prior)
