"""`quant.report.collect.tg_digest_section._channel_digest_stance_call`
(2026-09-07, Codex CLI 주 레인 전환) — 아침판 채널 브리핑 종합의 스탠스
마이크로프롬프트. 이전엔 `OPENROUTER_API_KEY`가 있을 때만 OpenRouter
단독으로 동작했다. 이제 Codex CLI 1순위 + OpenRouter 폴백(`narrate.stance`)
으로 바뀐다 — 실행파일도 키도 없을 때만 `None`.
"""
from __future__ import annotations

import pytest

from quant.report.collect.tg_digest_section import _channel_digest_stance_call


@pytest.fixture(autouse=True)
def isolate_stance_environment(monkeypatch):
    monkeypatch.setenv("OPS_NARRATOR", "codex")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def test_none_when_neither_codex_nor_key_available(monkeypatch):
    # 실제 로그인된 머신에서 기본 경로(~/.local/bin/codex)가 존재할 수 있으므로
    # 반드시 없는 경로를 명시한다 — env 삭제만으로는 격리되지 않는다.
    monkeypatch.setenv("CODEX_BIN", "/nonexistent/codex")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    import quant.adapters.narrate as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: None)

    assert _channel_digest_stance_call() is None


def test_builds_a_callable_when_codex_binary_exists(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", __file__)
    import quant.adapters.narrate as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: None)

    assert _channel_digest_stance_call() is not None


def test_builds_a_callable_when_only_openrouter_key_exists(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", "/nonexistent/codex")
    import quant.adapters.narrate as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: "k" if name == "OPENROUTER_API_KEY" else None)

    assert _channel_digest_stance_call() is not None


def test_delegates_to_narrate_stance_with_both_transports(monkeypatch):
    monkeypatch.setenv("CODEX_BIN", __file__)
    import quant.adapters.narrate as env_mod
    monkeypatch.setattr(env_mod, "get_key", lambda name: "k" if name == "OPENROUTER_API_KEY" else None)

    seen = {}
    import quant.adapters.narrate as narrate_mod
    monkeypatch.setattr(
        narrate_mod, "stance",
        lambda prompt, **kw: seen.update(prompt=prompt, kw=kw) or {"stance": "방어", "why": "y"},
    )

    call = _channel_digest_stance_call()
    result = call("프롬프트")

    assert result == {"stance": "방어", "why": "y"}
    assert seen["kw"]["codex_binary"] == __file__
    assert seen["kw"]["api_key"] == "k"
