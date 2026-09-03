"""유튜브/블로그/텔레그램 브리핑 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
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


def _telegram_default_window(now: datetime | None = None) -> datetime:
    """`since` 생략 시 폴백 — 오늘(KST) 자정부터. `_fetch_telegram_briefs`가
    news_since 처럼 "직전 리포트 이후" 창을 넘겨받지 않는(호출부 시그니처를
    넓게 고치지 않기 위해) 상황에서, 원장 저장소 병합 범위를 정하는 합리적
    기본값 — 텔레그램 원장도 뉴스 저장소처럼 KST 하루 단위로 쌓인다(append-only,
    `data/ledger/telegram_msgs.jsonl`)."""
    now = now or datetime.now(timezone.utc)
    kst = timezone(timedelta(hours=9))
    start = now.astimezone(kst).replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc)


def _merge_telegram_results(fresh: dict, store_rows: list[dict]) -> dict:
    """채널별 최신 20개(`fresh`, `fetch_all` 결과) + 원장 누적분(`store_rows`,
    `telegram_channels.load_window` 결과)을 합쳐 `fetch_all`과 같은 모양
    (`{handle: {"messages": [...], "error": ...}}`)으로 돌려준다.

    (handle, msg_id) 기준 중복 제거 — 같은 메시지가 양쪽에 있으면 `fresh`(방금
    받은 최신 파싱)를 우선한다. 채널별 메시지는 발행시각 내림차순으로 정렬한다
    — `build_telegram_view`가 앞 N개만 잘라 쓰므로(`ITEMS_PER_CHANNEL`) 순서가
    그대로 결과에 반영된다."""
    from quant.collect.sources.feeds import parse_published

    buckets: dict[str, dict[str, dict]] = {}
    errors: dict[str, str | None] = {}
    for handle, entry in fresh.items():
        errors[handle] = entry.get("error")
        bucket = buckets.setdefault(handle, {})
        for msg in entry.get("messages") or []:
            msg_id = msg.get("msg_id")
            if msg_id:
                bucket[msg_id] = msg
    for row in store_rows:
        handle = row.get("handle")
        msg_id = row.get("msg_id")
        if not handle or not msg_id:
            continue
        bucket = buckets.setdefault(handle, {})
        errors.setdefault(handle, None)
        if msg_id in bucket:
            continue  # fresh 가 이미 갖고 있으면 그대로 둔다(fresh 우선)
        bucket[msg_id] = {
            "msg_id": msg_id,
            "text": row.get("text", ""),
            "published": row.get("published"),
            "links": row.get("links") or [],
            "images": row.get("images") or [],
        }

    out: dict[str, dict] = {}
    for handle, bucket in buckets.items():
        msgs = sorted(
            bucket.values(),
            key=lambda m: parse_published(m.get("published")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        out[handle] = {"messages": msgs, "error": errors.get(handle)}
    return out


def _fetch_telegram_briefs(root: Path, getter=None, since: datetime | None = None) -> dict:
    """텔레그램 공개 채널 12방(2026-08-17, 사용자 지정, 서브프로젝트 S). `youtube_brief`/
    `blog_brief`와 같은 관례 — `telegram_channels.fetch_all`이 채널 단위로 실패를
    이미 격리하지만, 전 채널이 죽어도(레이트리밋 등) 리포트가 죽지 않도록 여기서
    한 번 더 감싼다.

    빌드마다 채널별 최신 20개를 원장(`data/ledger/telegram_msgs.jsonl`)에 upsert한다
    — 하루 3~4회 빌드로 밀도는 충분하다. `_emit`/`_emit_close` 둘 다 이 함수를
    부른다(part 2) — 오후엔 장중 시황방(tazastock/mootda) 가치가 특히 커서
    마감 리포트도 예외 없이 부른다(`_emit_close` docstring).

    **저장소 병합(2026-09-03)** — `fetch_all()`은 채널당 딱 최신 20개다. 오후
    빌드 시점엔 오전에 나온 메시지가 이미 그 20개 밖으로 밀려나 있을 수 있다
    (뉴스 RSS 와 같은 문제, `collector.py` 모듈 docstring 참고). 30분마다 도는
    `telegram-collect` 수집기(`report_cli collect --telegram`)가 쌓은 원장에서
    `since`(생략 시 오늘 KST 자정) 이후분을 읽어 `fresh`와 합친다."""
    from quant.collect.sources import telegram_channels

    try:
        result = telegram_channels.fetch_all(getter=getter)
    except Exception as e:  # noqa: BLE001 — 텔레그램 브리핑 실패가 리포트를 막지 않는다
        print(f"텔레그램 브리핑 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        result = {}

    path = root / "data" / "ledger" / "telegram_msgs.jsonl"
    rows = [
        {"handle": handle, **msg}
        for handle, entry in result.items()
        for msg in entry.get("messages") or []
    ]
    try:
        added = telegram_channels.append_ledger(rows, path)
        print(f"텔레그램 원장 {added}건 추가")
    except Exception as e:  # noqa: BLE001 — 원장 기록 실패가 리포트를 막지 않는다
        print(f"텔레그램 원장 기록 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)

    try:
        window_since = since or _telegram_default_window()
        store_rows = telegram_channels.load_window(path, window_since)
    except Exception as e:  # noqa: BLE001 — 저장소 읽기 실패가 리포트를 막지 않는다
        print(f"텔레그램 저장소 읽기 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        store_rows = []

    return _merge_telegram_results(result, store_rows)
