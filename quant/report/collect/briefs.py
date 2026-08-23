"""유튜브/블로그/텔레그램 브리핑 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _fetch_youtube_briefs() -> dict:
    """시황 브리핑 유튜브(§Task 7). `fetch_briefs` 자체가 채널 단위로 실패를
    격리하지만, 다른 `_build_*` 헬퍼와 같은 관례로 여기서 한 번 더 감싼다 —
    유튜브가 전부 죽어도(레이트리밋 등) 리포트가 죽으면 안 된다."""
    from quant.collect.sources import youtube_brief

    try:
        return youtube_brief.fetch_briefs()
    except Exception as e:  # noqa: BLE001 — 유튜브 브리핑 실패가 리포트를 막지 않는다
        print(f"유튜브 브리핑 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _fetch_blog_briefs() -> dict:
    """고정 브리핑 블로그(2026-08-17, 사용자 지정). `youtube_brief`와 같은
    관례 — `fetch_briefs` 자체가 블로그 단위로 실패를 격리하지만, 전 블로그가
    죽어도(레이트리밋 등) 리포트가 죽지 않도록 여기서 한 번 더 감싼다."""
    from quant.collect.sources import blog_brief

    try:
        return blog_brief.fetch_briefs()
    except Exception as e:  # noqa: BLE001 — 블로그 브리핑 실패가 리포트를 막지 않는다
        print(f"블로그 브리핑 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _fetch_telegram_briefs(root: Path, getter=None) -> dict:
    """텔레그램 공개 채널 12방(2026-08-17, 사용자 지정, 서브프로젝트 S). `youtube_brief`/
    `blog_brief`와 같은 관례 — `telegram_channels.fetch_all`이 채널 단위로 실패를
    이미 격리하지만, 전 채널이 죽어도(레이트리밋 등) 리포트가 죽지 않도록 여기서
    한 번 더 감싼다.

    빌드마다 채널별 최신 20개를 원장(`data/ledger/telegram_msgs.jsonl`)에 upsert한다
    — 하루 3~4회 빌드로 밀도는 충분하다. `_emit`/`_emit_close` 둘 다 이 함수를
    부른다(part 2) — 오후엔 장중 시황방(tazastock/mootda) 가치가 특히 커서
    마감 리포트도 예외 없이 부른다(`_emit_close` docstring)."""
    from quant.collect.sources import telegram_channels

    try:
        result = telegram_channels.fetch_all(getter=getter)
    except Exception as e:  # noqa: BLE001 — 텔레그램 브리핑 실패가 리포트를 막지 않는다
        print(f"텔레그램 브리핑 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}

    rows = [
        {"handle": handle, **msg}
        for handle, entry in result.items()
        for msg in entry["messages"]
    ]
    try:
        path = root / "data" / "ledger" / "telegram_msgs.jsonl"
        added = telegram_channels.append_ledger(rows, path)
        print(f"텔레그램 원장 {added}건 추가")
    except Exception as e:  # noqa: BLE001 — 원장 기록 실패가 리포트를 막지 않는다
        print(f"텔레그램 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

    return result
