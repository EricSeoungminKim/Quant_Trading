"""`quant.apps.cli._tg_digest_stance_call`(2026-09-07, Claude CLI 주 레인 전환).

이전엔 `OPS_NARRATOR=openrouter`일 때만 스탠스 마이크로프롬프트가 동작했다
(owner 실측: 무료 레인이 한도 초과로 상시 실패). 기본값(미설정/`claude`)에서도
Claude CLI 1순위 + OpenRouter 폴백(`narrate.stance`)으로 동작하게 바꿨다 —
`OPS_NARRATOR=openrouter`는 명시적 선택으로 존중(OpenRouter 단독),
`OPS_NARRATOR=none`은 완전히 끈다.
"""
from __future__ import annotations

from quant.apps.cli import _tg_digest_stance_call


def test_none_disables_the_stance_call(monkeypatch):
    monkeypatch.setenv("OPS_NARRATOR", "none")
    assert _tg_digest_stance_call() is None


def test_default_claude_lane_builds_a_callable_when_binary_exists(monkeypatch):
    monkeypatch.delenv("OPS_NARRATOR", raising=False)
    monkeypatch.setenv("CLAUDE_BIN", __file__)  # 존재하는 파일이면 충분(실행하지 않는다)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    call = _tg_digest_stance_call()

    assert call is not None


def test_default_claude_lane_is_none_when_nothing_is_available(monkeypatch):
    monkeypatch.delenv("OPS_NARRATOR", raising=False)
    monkeypatch.setenv("CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import quant.adapters.env as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: None)

    assert _tg_digest_stance_call() is None


def test_default_claude_lane_calls_stance_with_both_transports(monkeypatch):
    """`OPS_NARRATOR` 미설정이면 `narrate.stance`(claude 1순위 + openrouter
    폴백)를 쓴다 — `stance_only`(openrouter 단독)가 아니다."""
    monkeypatch.delenv("OPS_NARRATOR", raising=False)
    monkeypatch.setenv("CLAUDE_BIN", __file__)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    seen = {}

    import quant.adapters.narrate as narrate_mod
    monkeypatch.setattr(
        narrate_mod, "stance",
        lambda prompt, **kw: seen.update(prompt=prompt, kw=kw) or {"stance": "중립", "why": "이유"},
    )

    call = _tg_digest_stance_call()
    result = call("프롬프트")

    assert result == {"stance": "중립", "why": "이유"}
    assert seen["kw"]["claude_binary"] == __file__
    assert seen["kw"]["api_key"] == "k"


def test_openrouter_choice_stays_openrouter_only(monkeypatch):
    """명시적으로 `OPS_NARRATOR=openrouter`면 claude 를 시도하지 않는다 —
    기존(2026-09-05) 동작을 그대로 지킨다."""
    monkeypatch.setenv("OPS_NARRATOR", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    called = []
    import quant.adapters.narrate as narrate_mod
    monkeypatch.setattr(narrate_mod, "stance_only",
                        lambda prompt, key, **kw: called.append(key) or {"stance": "공격", "why": "y"})
    monkeypatch.setattr(narrate_mod, "stance",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("claude 경로를 타면 안 된다")))

    call = _tg_digest_stance_call()
    result = call("프롬프트")

    assert result == {"stance": "공격", "why": "y"}
    assert called == ["k"]


def test_openrouter_choice_without_key_is_none(monkeypatch):
    monkeypatch.setenv("OPS_NARRATOR", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import quant.adapters.env as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: None)

    assert _tg_digest_stance_call() is None
