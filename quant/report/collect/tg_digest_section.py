"""📡 채널 브리핑 종합 — 리포트 뉴스 창에 `quant.analyze.tg_digest`를 적용한다
(2026-09-05, 소유자 요구 (3): "리스크 브리핑 방들을 리포트가 완전히 흡수해
소유자가 직접 읽을 필요가 없게 한다").

뉴스 창은 기존 규칙(`news_since_for` — 직전 리포트 생성시각 ~ 이번 생성시각,
`news.py`/`_build_digest`와 동일)을 그대로 재사용한다. 마감 리포트는 완전
무LLM 계약(`report_cli._emit_close` docstring)이라 `narrator=None`으로 불러
결정론 다이제스트만 받는다(스탠스·방별 요약은 `None` — 후보·리스크·숫자 검증
표는 그대로 나온다).
"""
from __future__ import annotations

import sys
from datetime import datetime

from quant.analyze import tg_digest
from quant.analyze.delta import previous_snapshot
from quant.analyze.entities import load_table, load_us_table
from quant.collect.sources.telegram_channels import load_window
from quant.report.collect.snapshot import news_since_for
from quant.report.paths import _paths


def _load_regime_for_report(root, market: str) -> dict | None:
    """`data/state/regime.json`에서 `market`(KR/US) sub-dict(label/risk_multiplier/
    reasons)만 뽑는다 — `cli._tg_digest_load_regime`과 같은 로직(공유 유틸
    없이 각자 짧게 두는 이유는 `_fetch_telegram_briefs`의 ledger_path 인라인
    구성과 같은 관례). 실패는 예외가 아니라 `None`(`Digest.
    program_stance_display()`가 정직하게 "판정 불가"로 보여준다)."""
    import json as _json

    path = root / "data" / "state" / "regime.json"
    if not path.exists():
        return None
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = (payload.get("markets") or {}).get(market)
    if not isinstance(state, dict) and market == "US":
        state = payload if "label" in payload else None
    return state if isinstance(state, dict) else None


def _channel_digest_stance_call():
    """스탠스 전용 마이크로프롬프트 콜러블 — `OPENROUTER_API_KEY`가 있을 때만
    만든다(`_build_telegram_image_desc`와 같은 게이트, `briefs.py`). 마감
    리포트(`narrator=None`)는 호출부에서 애초에 이 함수를 부르지 않는다 —
    "완전 무LLM" 계약(`report_cli._emit_close` docstring)을 그대로 지킨다."""
    from quant.adapters.env import get_key
    from quant.adapters.narrate import stance_only

    key = get_key("OPENROUTER_API_KEY")
    if not key:
        return None
    return lambda prompt: stance_only(prompt, key)


def _build_channel_digest_view(
    snap, root, snap_root, sym_quotes: dict, narrator=None,
) -> tg_digest.Digest | None:
    """실패해도 리포트 발행을 막지 않는다(`_build_us_kr_bridge`/`_build_digest_prose`
    등 다른 `_build_*` 헬퍼와 같은 관례) — 실패 시 `None`, 템플릿은 섹션을
    생략한다."""
    try:
        prev = previous_snapshot(snap.market, snap.session_date, snap_root)
        since = news_since_for(prev, snap.generated_at)
        until: datetime = snap.generated_at

        ledger_path = root / "data" / "ledger" / "telegram_msgs.jsonl"
        messages = load_window(ledger_path, since=since, until=until)

        _, _, cache_dir, _ = _paths(root)
        name_table = load_table(cache_dir) if snap.market == "KR" else load_us_table(cache_dir)

        regime = _load_regime_for_report(root, snap.market)
        llm_call = narrator.narrate if narrator is not None else None
        # 스탠스 전용 마이크로프롬프트(2026-09-05, tg_digest 모듈 docstring
        # "LLM 스탠스" 절)는 마감판(narrator=None)엔 절대 붙이지 않는다 —
        # 아침판(narrator=quality_narrator)에만.
        stance_llm_call = _channel_digest_stance_call() if narrator is not None else None
        return tg_digest.build_digest(
            messages, snap.market, until, since=since, name_table=name_table,
            quotes_lookup=sym_quotes.get, llm_call=llm_call,
            stance_llm_call=stance_llm_call, regime=regime,
        )
    except Exception as e:  # noqa: BLE001 — 채널 브리핑 종합 실패가 리포트를 막지 않는다
        print(f"채널 브리핑 종합 생략: {type(e).__name__}: {e}", file=sys.stderr)
        return None
