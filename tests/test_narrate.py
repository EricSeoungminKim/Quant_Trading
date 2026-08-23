"""서술기 어댑터 — Phase 5.4. 네트워크 호출 없음(poster/runner 주입).

중심 주장: **경보는 서술기에 의존하지 않는다.** 모델이 죽든, 키가 없든, 응답 형태가
바뀌든 `narrate()` 는 `None` 을 돌려주고 예외를 올리지 않는다 — 호출자가 결정론적
형식으로 떨어질 수 있어야 한다. `Notifier` 와 같은 계약이다.
"""
from __future__ import annotations

import json

from quant.adapters.narrate import (
    CLAUDE_DISALLOWED_TOOLS,
    DEFAULT_OPENROUTER_MODEL,
    TOOL_MODEL,
    TOOL_MODEL_FALLBACK,
    VISION_MODEL,
    ClaudeCliNarrator,
    NullNarrator,
    OpenRouterNarrator,
    QualityFallbackNarrator,
    chat_with_tools,
    describe_image,
    make_narrator,
    make_quality_narrator,
)
from quant.core.ports import Narrator


# ── 포트 계약 ─────────────────────────────────────────────────────────────

def test_all_implementations_satisfy_the_port():
    assert isinstance(NullNarrator(), Narrator)
    assert isinstance(ClaudeCliNarrator("/bin/true"), Narrator)
    assert isinstance(OpenRouterNarrator("k"), Narrator)


def test_null_narrator_returns_none_not_empty_string():
    """빈 문자열을 돌려주면 호출자가 "서술했다"로 읽고 빈 알림을 보낸다.

    실제로 그렇게 빈 알림이 나간 적이 있다(ops_watch 첫 리허설).
    """
    assert NullNarrator().narrate("무엇이든") is None


# ── OpenRouter ───────────────────────────────────────────────────────────

def _reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


def test_openrouter_sends_the_prompt_and_returns_the_text():
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload)
        return _reply("  국면 지표가 낡았다.  ")

    out = OpenRouterNarrator("secret-key", poster=poster).narrate("판정 JSON")

    assert out == "국면 지표가 낡았다."          # 양쪽 공백은 정리한다
    assert seen["payload"]["model"] == DEFAULT_OPENROUTER_MODEL
    assert seen["payload"]["messages"][0]["content"] == "판정 JSON"
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert "openrouter.ai" in seen["url"]


def test_openrouter_default_model_has_no_date_suffix():
    """웹 URL 에는 `-20260604` 같은 접미사가 붙어 보이지만 실제 모델 ID 에는 없다.
    붙이면 404 다 — 그리고 404 는 `None` 이 되어 조용히 서술이 사라진다."""
    assert DEFAULT_OPENROUTER_MODEL == "nvidia/nemotron-3-ultra-550b-a55b:free"
    assert not DEFAULT_OPENROUTER_MODEL.rstrip(":free").endswith(tuple("0123456789"))


def test_openrouter_default_max_tokens_is_700_for_short_ops_narration():
    """운영 서술("텔레그램 한 통") 경로는 기본값이 바뀌면 안 된다 — 파라미터화
    (E2E 실측 2026-08-15, deepdive 프롬프트가 700에서 잘리던 문제)가 이 기본
    동작을 건드리지 않았는지 고정한다."""
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(payload=payload)
        return _reply("짧은 서술")

    OpenRouterNarrator("k", poster=poster).narrate("x")
    assert seen["payload"]["max_tokens"] == 700


def test_openrouter_honours_injected_max_tokens():
    """추론 모델은 최종 답 전에 '생각'에 토큰을 쓴다 — 긴 프롬프트(deepdive)는
    max_tokens 를 올려야 답이 안 잘린다."""
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(payload=payload)
        return _reply("긴 프롬프트에 대한 답")

    OpenRouterNarrator("k", poster=poster, max_tokens=4000).narrate("x" * 9000)
    assert seen["payload"]["max_tokens"] == 4000


def test_openrouter_returns_none_when_transport_fails():
    out = OpenRouterNarrator("k", poster=lambda *a: None).narrate("x")
    assert out is None


def test_openrouter_returns_none_on_unexpected_response_shape():
    """응답 스키마가 바뀌어도 예외로 죽지 않고 "못 읽었다"로 답한다."""
    out = OpenRouterNarrator("k", poster=lambda *a: {"unexpected": True}).narrate("x")
    assert out is None


def test_openrouter_returns_none_for_blank_completion():
    """빈 문장은 서술이 아니다."""
    out = OpenRouterNarrator("k", poster=lambda *a: _reply("   \n ")).narrate("x")
    assert out is None


def test_openrouter_transport_exception_never_escapes():
    def boom(*a):
        raise RuntimeError("network down")

    # 포트 계약상 호출자를 죽이면 안 된다. poster 가 던지면 어댑터가 막아야 한다.
    try:
        out = OpenRouterNarrator("k", poster=boom).narrate("x")
    except RuntimeError:
        raise AssertionError("어댑터가 예외를 흘려보냈다 — 경보가 서술기 때문에 죽는다")
    assert out is None


# ── 이미지 해석(서브프로젝트 S part 3) ──────────────────────────────────────

def test_describe_image_sends_image_url_and_returns_text():
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(url=url, headers=headers, payload=payload, timeout=timeout)
        return _reply("  전력 설비 사진이다.  ")

    out = describe_image("https://cdn5.telesco.pe/a.jpg", "secret-key", poster=poster)

    assert out == "전력 설비 사진이다."  # 양쪽 공백은 정리한다
    assert seen["payload"]["model"] == VISION_MODEL
    content = seen["payload"]["messages"][0]["content"]
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://cdn5.telesco.pe/a.jpg"}}
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert "openrouter.ai" in seen["url"]
    assert seen["timeout"] == 30


def test_describe_image_honours_injected_timeout():
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(timeout=timeout)
        return _reply("사진 해석")

    describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=poster, timeout=10)
    assert seen["timeout"] == 10


def test_describe_image_returns_none_when_transport_fails():
    out = describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=lambda *a: None)
    assert out is None


def test_describe_image_returns_none_on_unexpected_response_shape():
    out = describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=lambda *a: {"unexpected": True})
    assert out is None


def test_describe_image_returns_none_for_blank_completion():
    out = describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=lambda *a: _reply("   \n "))
    assert out is None


def test_describe_image_transport_exception_never_escapes():
    def boom(*a):
        raise RuntimeError("network down")

    try:
        out = describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=boom)
    except RuntimeError:
        raise AssertionError("어댑터가 예외를 흘려보냈다 — 호출부(예산 소비 루프)가 죽는다")
    assert out is None


def test_describe_image_does_not_retry_on_failure():
    """`OpenRouterNarrator.narrate`와 달리 재시도가 없다 — 예산(최대 3장)을
    실패 재시도로 낭비하지 않는다."""
    calls = []

    def poster(url, headers, payload, timeout):
        calls.append(1)
        return None

    describe_image("https://cdn5.telesco.pe/a.jpg", "k", poster=poster)
    assert len(calls) == 1


# ── Claude CLI ───────────────────────────────────────────────────────────

def test_claude_cli_blocks_every_tool():
    """프롬프트에 로그 내용(신뢰 불가 입력)이 들어간다 — 도구가 살아 있으면
    프롬프트 주입이 실행으로 이어진다. 주문 경로 접근 금지의 실제 구현이다."""
    seen = {}

    def runner(cmd, stdin, timeout):
        seen.update(cmd=cmd, stdin=stdin)
        return "요약문"

    out = ClaudeCliNarrator("/usr/bin/claude", runner=runner).narrate("판정 JSON")

    assert out == "요약문"
    assert "--disallowedTools" in seen["cmd"]
    blocked = seen["cmd"][seen["cmd"].index("--disallowedTools") + 1]
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Task", "Agent"):
        assert tool in blocked
    assert blocked == CLAUDE_DISALLOWED_TOOLS
    assert seen["stdin"] == "판정 JSON"


def test_claude_cli_returns_none_when_runner_fails():
    assert ClaudeCliNarrator("/x", runner=lambda *a: None).narrate("x") is None


# ── 팩토리 ───────────────────────────────────────────────────────────────

def test_factory_defaults_to_claude_but_falls_back_when_binary_missing():
    """기본값이 claude 인데 실행파일이 없으면 Null 이다 — 예외가 아니다."""
    got = make_narrator({"CLAUDE_BIN": "/nonexistent/claude"})
    assert isinstance(got, NullNarrator)


def test_factory_openrouter_without_key_is_null_not_exception():
    """키가 없으면 조용히 서술을 포기한다. 경보는 그대로 나간다."""
    assert isinstance(make_narrator({"OPS_NARRATOR": "openrouter"}), NullNarrator)


def test_factory_openrouter_with_key():
    got = make_narrator({"OPS_NARRATOR": "openrouter", "OPENROUTER_API_KEY": "k"})
    assert isinstance(got, OpenRouterNarrator)


def test_factory_honours_model_override():
    got = make_narrator({"OPS_NARRATOR": "openrouter", "OPENROUTER_API_KEY": "k",
                         "OPENROUTER_MODEL": "google/gemma-4-31b-it:free"})
    assert got._model == "google/gemma-4-31b-it:free"


def test_factory_openrouter_max_tokens_defaults_to_700():
    got = make_narrator({"OPS_NARRATOR": "openrouter", "OPENROUTER_API_KEY": "k"})
    assert got._max_tokens == 700


def test_factory_honours_max_tokens_override():
    got = make_narrator({"OPS_NARRATOR": "openrouter", "OPENROUTER_API_KEY": "k",
                         "OPENROUTER_MAX_TOKENS": "4000"})
    assert got._max_tokens == 4000


def test_factory_invalid_max_tokens_falls_back_to_default_not_exception():
    """make_narrator 는 절대 예외를 던지지 않는다 — 잘못된 값은 경고 후 기본값."""
    got = make_narrator({"OPS_NARRATOR": "openrouter", "OPENROUTER_API_KEY": "k",
                         "OPENROUTER_MAX_TOKENS": "많이"})
    assert isinstance(got, OpenRouterNarrator)
    assert got._max_tokens == 700


def test_factory_none_is_explicit():
    assert isinstance(make_narrator({"OPS_NARRATOR": "none"}), NullNarrator)


def test_factory_unknown_choice_is_null_not_exception():
    assert isinstance(make_narrator({"OPS_NARRATOR": "gpt-99"}), NullNarrator)


def test_factory_falls_back_to_env_local_file_when_os_environ_lacks_key(monkeypatch):
    """크론은 .env.local 을 export 하지 않는다 — 수집기들은 get_key(파일 직독)라
    멀쩡한데 서술기만 os.environ 을 봐서 크론 경로의 LLM 이 조용히 죽어 있었다
    (2026-08-16 실측: deepdive US 'OPENROUTER_API_KEY 없음'). env 인자를 명시로
    주입한 경우(테스트·의도적 격리)는 폴백하지 않는다 — 주입이 곧 전체 환경이다."""
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setenv("OPS_NARRATOR", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(narrate_mod, "get_key", lambda name: "파일키" if name == "OPENROUTER_API_KEY" else None)
    assert isinstance(make_narrator(), OpenRouterNarrator)

    # 파일에도 없으면 기존과 동일하게 Null
    monkeypatch.setattr(narrate_mod, "get_key", lambda name: None)
    assert isinstance(make_narrator(), NullNarrator)


def test_factory_injected_env_does_not_fall_back_to_file(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod, "get_key", lambda name: "파일키")
    got = make_narrator({"OPS_NARRATOR": "openrouter"})
    assert isinstance(got, NullNarrator)


def test_openrouter_retries_once_on_transient_failure(monkeypatch):
    """무료 레인은 업스트림 502 를 200-with-error 로 주는 간헐 실패가 실측됐다
    (2026-08-17 실패율 ~50%, 재시도로 성공) — 1회 재시도가 비용 0 의 보강이다."""
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    calls = []

    def flaky_poster(url, headers, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            return {"error": {"code": 502}}  # content 없음 → None 경로
        return {"choices": [{"message": {"content": "성공"}}]}

    n = OpenRouterNarrator("k", poster=flaky_poster)
    assert n.narrate("p") == "성공"
    assert len(calls) == 2


def test_openrouter_gives_up_after_second_failure(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    calls = []

    def dead_poster(url, headers, payload, timeout):
        calls.append(1)
        return {"error": {"code": 502}}

    n = OpenRouterNarrator("k", poster=dead_poster)
    assert n.narrate("p") is None
    assert len(calls) == 2


# ── 툴콜링 해석 에이전트(서브프로젝트 U) — chat_with_tools ─────────────────
#
# poster 계약이 다른 어댑터(_httpx_poster 등)와 다르다 — {"status","body",
# "headers"} 를 그대로 돌려준다(429/502 를 다르게 재시도하려면 상태 코드가
# 필요하다, chat_with_tools 상단 주석). 테스트는 이 계약으로 직접 목킹한다.

def _resp(status: int, body: dict | None = None, headers: dict | None = None) -> dict:
    return {"status": status, "body": body, "headers": headers or {}}


def _final_text_body(text: str) -> dict:
    return {"choices": [{"message": {"content": text, "tool_calls": None}}]}


def _tool_call_body(name: str, args: dict, call_id: str = "call_1") -> dict:
    return {
        "choices": [{"message": {
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)}}],
        }}],
    }


def test_chat_with_tools_round_trips_a_single_tool_call():
    calls = []

    def poster(url, headers, payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            return _resp(200, _tool_call_body("get_score_breakdown", {"symbol": "005930"}))
        return _resp(200, _final_text_body(
            '산문.\nJUDGMENT: {"direction": "bullish", "confidence": 4}'))

    seen_tool_calls = []

    def execute(name, args):
        seen_tool_calls.append((name, args))
        return json.dumps({"score100": 72})

    out = chat_with_tools(
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        tools=[{"type": "function", "function": {"name": "get_score_breakdown"}}],
        api_key="k", execute=execute, poster=poster,
    )

    assert out["text"].startswith("산문.")
    assert out["rounds"] == 2
    assert seen_tool_calls == [("get_score_breakdown", {"symbol": "005930"})]
    # 2라운드 요청엔 1라운드의 assistant 메시지 + tool 결과가 실려 있어야 한다.
    assert calls[1]["messages"][-1]["role"] == "tool"
    assert calls[1]["messages"][-1]["tool_call_id"] == "call_1"


def test_chat_with_tools_uses_tool_model_by_default():
    seen = {}

    def poster(url, headers, payload, timeout):
        seen.update(payload=payload)
        return _resp(200, _final_text_body("답"))

    chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)
    assert seen["payload"]["model"] == TOOL_MODEL


def test_chat_with_tools_returns_none_when_no_final_text_after_max_rounds():
    def poster(url, headers, payload, timeout):
        return _resp(200, _tool_call_body("x", {}))

    out = chat_with_tools(
        messages=[{"role": "user", "content": "u"}], tools=[], api_key="k",
        max_rounds=2, execute=lambda n, a: "{}", poster=poster,
    )
    assert out is None


def test_chat_with_tools_502_retries_once_then_succeeds(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    calls = []

    def poster(url, headers, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _resp(502)
        return _resp(200, _final_text_body("성공"))

    out = chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)

    assert out["text"] == "성공"
    assert len(calls) == 2


def test_chat_with_tools_429_waits_retry_after_header_capped_at_30(monkeypatch):
    """실측(2026-08-17) Retry-After 는 22초였다 — 여기선 헤더가 그보다 훨씬
    큰 값을 줘도 30초 상한을 넘지 않는지를 확인한다."""
    import quant.adapters.narrate as narrate_mod

    slept = []
    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: slept.append(s))
    calls = []

    def poster(url, headers, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _resp(429, headers={"Retry-After": "999"})
        return _resp(200, _final_text_body("성공"))

    out = chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)

    assert out["text"] == "성공"
    assert slept == [30]


def test_chat_with_tools_429_without_header_waits_default_22(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    slept = []
    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: slept.append(s))
    calls = []

    def poster(url, headers, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            return _resp(429)
        return _resp(200, _final_text_body("성공"))

    chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)

    assert slept == [22]


def test_chat_with_tools_falls_back_to_second_model_when_primary_exhausts_retries(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    seen_models = []

    def poster(url, headers, payload, timeout):
        seen_models.append(payload["model"])
        if payload["model"] == TOOL_MODEL:
            return _resp(502)  # 1순위는 재시도까지 소진해도 계속 실패
        return _resp(200, _final_text_body("폴백 성공"))

    out = chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)

    assert out["text"] == "폴백 성공"
    # 1순위 2회(최초+재시도) + 폴백 1회.
    assert seen_models == [TOOL_MODEL, TOOL_MODEL, TOOL_MODEL_FALLBACK]


def test_chat_with_tools_returns_none_when_both_models_fail(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)

    def poster(url, headers, payload, timeout):
        return _resp(500)

    out = chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=poster)
    assert out is None


def test_chat_with_tools_never_raises_on_transport_exception():
    def boom(*a):
        raise RuntimeError("network down")

    try:
        out = chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k", poster=boom)
    except RuntimeError:
        raise AssertionError("어댑터가 예외를 흘려보냈다 — 리포트 빌드가 LLM 때문에 죽는다")
    assert out is None


# ── LLM 호출 계측 (2026-08-18) ───────────────────────────────────────────
#
# 유래: 무료 레인 요청 수·실패율·지연을 아무도 기록하지 않아 한도에 걸리기
# 시작해도 몰랐다. `_record_llm_call` 을 몽키패치해 narrate()/chat_with_tools()
# 가 올바른 lane/ok 로 호출하는지, 계측 실패가 서술을 죽이지 않는지만 본다 —
# 실제 Redis 기록 계약 자체는 test_opstate.py 가 본다(이중 검증 금지).

def test_narrate_records_a_successful_call(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    OpenRouterNarrator("k", poster=lambda *a: _reply("답")).narrate("p")

    assert len(seen) == 1
    lane, ok, seconds = seen[0]
    assert lane == "narrate"
    assert ok is True
    assert seconds >= 0


def test_narrate_records_a_failed_call_after_exhausting_retries(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    OpenRouterNarrator("k", poster=lambda *a: None).narrate("p")

    assert len(seen) == 1
    assert seen[0][:2] == ("narrate", False)


def test_chat_with_tools_records_a_successful_call(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k",
                    poster=lambda *a: _resp(200, _final_text_body("답")))

    assert len(seen) == 1
    lane, ok, seconds = seen[0]
    assert lane == "tool"
    assert ok is True


def test_chat_with_tools_records_a_failed_call_once_after_fallback_also_fails(monkeypatch):
    """1순위+폴백 전체 루프를 하나의 호출로 계측한다 — lane="tool" 로 한 번만."""
    import quant.adapters.narrate as narrate_mod

    monkeypatch.setattr(narrate_mod.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    chat_with_tools(messages=[{"role": "user", "content": "u"}], tools=[], api_key="k",
                    poster=lambda *a: _resp(500))

    assert len(seen) == 1
    assert seen[0][:2] == ("tool", False)


def test_metrics_failure_does_not_break_narrate(monkeypatch):
    """계측(opstate.record_llm_call)이 죽어도 서술 결과는 그대로 나가야 한다
    — `_record_llm_call` 내부 try/except 의 계약을 실제 예외로 확인한다."""
    import quant.control.opstate as opstate_mod

    def boom(*a, **k):
        raise RuntimeError("redis 계측 죽음")

    monkeypatch.setattr(opstate_mod, "record_llm_call", boom)

    out = OpenRouterNarrator("k", poster=lambda *a: _reply("답")).narrate("p")
    assert out == "답"


# ── 사고과정 유출 가드 (2026-08-18) ─────────────────────────────────────────

def test_leak_guard_catches_english_reasoning_preamble():
    """실측 사고 재현: 발행된 Exec Summary가 'The user wants me to write...'로
    시작했다 — 추론 모델이 생각을 최종 답에 유출한 것. 한국어 서술 계약에서
    이건 실패로 취급돼야 한다."""
    from quant.adapters.narrate import looks_like_reasoning_leak

    assert looks_like_reasoning_leak(
        "The user wants me to write three paragraphs based on the provided data...")
    assert looks_like_reasoning_leak(
        "Okay, let me analyze the Korean market data first. The KOSPI rose...")


def test_leak_guard_passes_normal_korean_with_tickers():
    """티커·영문 용어(HBM, VIX)가 섞인 정상 한국어는 한글 비중이 높아 통과해야
    한다 — 가드가 정상 산문을 죽이면 섹션이 매일 비고, 그런 가드는 꺼진다."""
    from quant.adapters.narrate import looks_like_reasoning_leak

    assert not looks_like_reasoning_leak(
        "코스피는 +2.42% 상승했고 VIX는 15.4로 낮은 구간이다. SK하이닉스는 HBM "
        "수요 대응을 위해 시설투자를 35% 늘렸다.")
    assert not looks_like_reasoning_leak("")


def test_narrate_discards_leaked_output_and_retries(monkeypatch):
    """유출 응답은 None 취급 → 기존 재시도 1회를 그대로 탄다. 두 번째가
    정상이면 그걸 쓴다."""
    from quant.adapters import narrate as N

    responses = iter([
        {"choices": [{"message": {"content": "The user wants me to summarize..."}}]},
        {"choices": [{"message": {"content": "코스피는 상승 마감했다."}}]},
    ])
    monkeypatch.setattr(N.time, "sleep", lambda *_: None)
    nar = N.OpenRouterNarrator(api_key="k", poster=lambda *a, **k: next(responses))
    assert nar.narrate("요약해라") == "코스피는 상승 마감했다."


# ── 품질 레인 (2026-08-18) ──────────────────────────────────────────────
#
# 사용자 A/B 실측(compare_narrators.html): OpenRouter 무료판은 Exec Summary
# 영어 사고과정 유출·시황 산문 누락·4섹션 해석 누락, Claude 구독판은 전부
# 고품질 — "느린 건 상관없어, 정확도가 중요해" 결정에 따라 품질 민감 서술만
# Claude CLI 1순위 + OpenRouter 폴백 레인으로 옮긴다.

class _StubNarrator:
    def __init__(self, reply):
        self._reply = reply
        self.calls: list[str] = []

    def narrate(self, prompt: str):
        self.calls.append(prompt)
        return self._reply


def test_quality_fallback_uses_primary_and_skips_fallback_on_success():
    primary = _StubNarrator("1순위 답")
    fallback = _StubNarrator("폴백 답")

    out = QualityFallbackNarrator(primary, fallback).narrate("p")

    assert out == "1순위 답"
    assert fallback.calls == []


def test_quality_fallback_falls_back_when_primary_fails():
    primary = _StubNarrator(None)
    fallback = _StubNarrator("폴백 답")

    out = QualityFallbackNarrator(primary, fallback).narrate("p")

    assert out == "폴백 답"
    assert fallback.calls == ["p"]


def test_quality_fallback_returns_none_when_both_fail():
    out = QualityFallbackNarrator(_StubNarrator(None), _StubNarrator(None)).narrate("p")
    assert out is None


def test_quality_fallback_records_lane_quality(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    QualityFallbackNarrator(_StubNarrator("답"), _StubNarrator(None)).narrate("p")

    assert len(seen) == 1
    lane, ok, seconds = seen[0]
    assert lane == "quality"
    assert ok is True
    assert seconds >= 0


def test_quality_fallback_records_failure_after_both_fail(monkeypatch):
    import quant.adapters.narrate as narrate_mod

    seen = []
    monkeypatch.setattr(narrate_mod, "_record_llm_call",
                        lambda lane, ok, seconds: seen.append((lane, ok, seconds)))

    QualityFallbackNarrator(_StubNarrator(None), _StubNarrator(None)).narrate("p")

    assert seen[0][:2] == ("quality", False)


def test_make_quality_narrator_off_switch_delegates_to_make_narrator():
    """킬스위치 — 첫 실전이 틀어졌을 때 한 변수로 전량 무료 레인 복귀."""
    got = make_quality_narrator({"QUALITY_NARRATOR": "off", "OPS_NARRATOR": "openrouter",
                                 "OPENROUTER_API_KEY": "k"})
    assert isinstance(got, OpenRouterNarrator)


def test_make_quality_narrator_off_switch_is_case_insensitive_and_trims():
    got = make_quality_narrator({"QUALITY_NARRATOR": " OFF ", "OPS_NARRATOR": "none"})
    assert isinstance(got, NullNarrator)


def test_make_quality_narrator_wraps_claude_primary_and_openrouter_fallback():
    """binary 가 존재하면 1순위는 ClaudeCliNarrator, 폴백은 OpenRouterNarrator다."""
    import sys

    got = make_quality_narrator({"CLAUDE_BIN": sys.executable, "OPENROUTER_API_KEY": "k"})

    assert isinstance(got, QualityFallbackNarrator)
    assert isinstance(got._primary, ClaudeCliNarrator)
    assert isinstance(got._fallback, OpenRouterNarrator)


def test_make_quality_narrator_generous_timeout_for_long_report_prompts():
    """리포트 프롬프트(수천 자)는 기본 180s 로는 빠듯할 수 있어 넉넉히 잡는다."""
    import sys

    got = make_quality_narrator({"CLAUDE_BIN": sys.executable})
    assert got._primary._timeout == 240


def test_make_quality_narrator_falls_back_to_openrouter_only_when_binary_missing():
    got = make_quality_narrator({"CLAUDE_BIN": "/nonexistent/claude", "OPENROUTER_API_KEY": "k"})

    assert isinstance(got, QualityFallbackNarrator)
    assert isinstance(got._primary, NullNarrator)
    assert isinstance(got._fallback, OpenRouterNarrator)


def test_make_quality_narrator_is_fully_null_when_nothing_available():
    got = make_quality_narrator({"CLAUDE_BIN": "/nonexistent/claude"})

    assert isinstance(got, QualityFallbackNarrator)
    assert isinstance(got._primary, NullNarrator)
    assert isinstance(got._fallback, NullNarrator)


def test_make_quality_narrator_honours_model_override_for_fallback():
    got = make_quality_narrator(
        {"CLAUDE_BIN": "/nonexistent/claude", "OPENROUTER_API_KEY": "k"},
        model="google/gemma-4-31b-it:free",
    )
    assert got._fallback._model == "google/gemma-4-31b-it:free"
