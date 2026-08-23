"""텔레그램 채널 카드 뷰 가산점/AI 해석 + usnews tier 헤드라인 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동. `_USNEWS_HEADLINE_LIMIT`은 원래 "중기 관심
종목" 블록에 있었지만 실제로는 `_usnews_headlines`(이 파일)만 쓰므로 같이
옮긴다 — `quant.report.collect.midterm`이 이 파일의 `_usnews_titles`를 쓰는
반대 방향 의존과 겹치면 순환 임포트가 생긴다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from quant.analyze.entities import load_table
from quant.analyze.telegram_view import describe_sector_images, narrate_channels
from quant.analyze.telegram_view import telegram_mentions as compute_telegram_mentions
from quant.collect.sources.feeds import parse_published
from quant.collect.sources.telegram_channels import CHANNELS as TELEGRAM_CHANNELS

from quant.report.paths import _paths

# US 리포트 "🇺🇸 실시간 헤드라인" 소구획 최대 건수(사용자 지시).
_USNEWS_HEADLINE_LIMIT = 12


def _build_telegram_mentions(
    root: Path, market: str, payload: dict, telegram_result: dict,
) -> dict[str, dict]:
    """텔레그램 시그널 가산점(v4) 입력 — `telegram_view.telegram_mentions`를
    감싼다(서브프로젝트 S part 2). KR은 `entities.load_table`(이미 캐시된
    상장법인목록, anti-collision 경계 검사)을, US는 오늘 payload 심볼(이미
    검증된 후보)만 화이트리스트로 쓴다(`telegram_view.telegram_mentions`
    docstring — 상장 전체 사전을 새로 받지 않는다).

    실패해도 리포트를 막지 않는다 — 다른 `_build_*`/`_fetch_*` 헬퍼와 같은
    관례. 실패 시 빈 dict(가산점 0, 위장 아님 — 텔레그램 언급이 "없다"와
    "몰라서 못 채점했다"를 이 시점에서 굳이 구분하지 않는다. 가산점 축
    자체가 `_NULLABLE_KEYS` 대상이 아니라 어느 쪽이든 0점으로 수렴한다)."""
    try:
        if market == "KR":
            _, _, cache_dir, _ = _paths(root)
            name_table = load_table(cache_dir)
        else:
            name_table = {
                s.get("symbol") for s in payload.get("symbols") or [] if s.get("symbol")
            }
        return compute_telegram_mentions(telegram_result, name_table, market)
    except Exception as e:  # noqa: BLE001 — 텔레그램 가산점 실패가 리포트를 막지 않는다
        print(f"텔레그램 언급 가산점 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _build_telegram_prose(telegram_view: list[dict]) -> dict[str, str] | None:
    """텔레그램 채널별 동향 1문장 요약(선택, LLM, 서브프로젝트 S part 2) —
    `_build_section_advice`와 같은 관례: narrator 호출까지 이 블록 전체가
    실패해도 리포트 발행 자체는 막지 않는다. 실패/전 채널 결측 시 `None` —
    템플릿은 채널별 메시지 목록만으로 이미 완전하다(무LLM 폴백). **마감
    리포트(`_emit_close`)는 이 함수를 부르지 않는다** — 마감판은 완전
    무LLM이 계약이다."""
    try:
        from quant.adapters.narrate import make_narrator

        return narrate_channels(telegram_view, make_narrator())
    except Exception as e:  # noqa: BLE001 — 텔레그램 AI 해석 실패가 리포트를 막지 않는다
        print(f"텔레그램 AI 해석 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _build_telegram_image_desc(telegram_view: list[dict]) -> dict[str, str]:
    """텔레그램 섹터방 사진 AI 해석(선택, LLM, 서브프로젝트 S part 3) —
    `_build_telegram_prose`와 같은 관례: 실패해도 리포트 발행을 막지 않는다.
    `OPENROUTER_API_KEY`가 없으면(get_key — 크론이 `.env.local`을 export 하지
    않는 경로, `narrate.make_narrator`와 같은 이유) 조용히 빈 dict — 템플릿은
    "🖼 사진 N" 칩 + t.me 링크만으로 이미 완전하다(무LLM 폴백). **마감
    리포트(`_emit_close`)는 이 함수를 부르지 않는다** — 마감판은 완전
    무LLM이 계약이다."""
    try:
        from quant.adapters.env import get_key
        from quant.adapters.narrate import describe_image

        key = get_key("OPENROUTER_API_KEY")
        if not key:
            return {}
        return describe_sector_images(telegram_view, lambda url: describe_image(url, key))
    except Exception as e:  # noqa: BLE001 — 텔레그램 사진 AI 해석 실패가 리포트를 막지 않는다
        print(f"텔레그램 사진 AI 해석 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _usnews_titles(telegram_result: dict) -> list[str]:
    """미국발 뉴스 **서사** 입력 — usnews tier(walterbloomberg/financialjuice,
    영문 시간당 헤드라인) + usdigest tier(insidertracking, 한국어 일일 다이제스트).
    `_build_us_news_kr_view`(KR)와 시황 다이제스트가 이걸 쓴다.

    `_usnews_headlines`("🇺🇸 실시간 헤드라인" 구획)는 **usnews 만** 본다 —
    하루 한 번짜리 요약을 실시간 헤드라인으로 표시하면 라벨이 거짓이 된다
    (2026-08-21 소유자 지시로 insidertracking 을 서사에 연결하며 나눈 구분).

    `telegram_result`는 `_fetch_telegram_briefs`가 이미 전 채널(시장 무관)을
    받아온 결과라, KR 빌드에서도 이 채널 메시지가 그대로 들어 있다."""
    handles = [c["handle"] for c in TELEGRAM_CHANNELS
               if c.get("tier") in ("usnews", "usdigest")]
    return [
        msg["text"]
        for handle in handles
        for msg in (telegram_result.get(handle) or {}).get("messages") or []
        if msg.get("text")
    ]


def _usnews_headlines(telegram_result: dict) -> list[dict]:
    """US 리포트 "🇺🇸 실시간 헤드라인" 소구획 — usnews tier 채널 최신
    `_USNEWS_HEADLINE_LIMIT`(12)건(제목+시각), 채널 둘을 시각 내림차순으로
    합친다(채널별로는 이미 최신순이지만 둘을 인터리브해야 진짜 최신순이 된다)."""
    from quant.analyze.telegram_view import _hhmm

    handles = [c["handle"] for c in TELEGRAM_CHANNELS if c.get("tier") == "usnews"]
    rows = []
    for handle in handles:
        for msg in (telegram_result.get(handle) or {}).get("messages") or []:
            if not msg.get("text"):
                continue
            rows.append({
                "text": msg["text"],
                "published_hhmm": _hhmm(msg.get("published")),
                "_dt": parse_published(msg.get("published")) or datetime.min.replace(tzinfo=timezone.utc),
            })
    rows.sort(key=lambda r: r["_dt"], reverse=True)
    return [{"text": r["text"], "published_hhmm": r["published_hhmm"]} for r in rows[:_USNEWS_HEADLINE_LIMIT]]
