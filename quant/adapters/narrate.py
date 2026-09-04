"""서술기 어댑터 — 결정론적 판정을 산문으로. Phase 5.4.

`quant.core.ports.Narrator` 구현체들. `kv.py` 와 같은 모양이다: **Null 구현 +
실물 + 절대 예외를 던지지 않는 팩토리.** 이유도 같다 — 서술은 선택 사항이고,
없다고 경보가 멈추면 서술기를 안 쓰느니만 못하다.

## 무엇이 여기 없나

**판단이 없다.** 무엇을 경보할지는 `quant.control.health` 의 순수 함수가 이미
정했고, 여기 들어오는 건 확정된 판정문이다. 구현체가 할 수 있는 일은 문장을
돌려주는 것뿐이고, 실패하면 `None` 이다.

이건 ADR-0002 의 선을 지키는 방식이기도 하다: LLM 은 리포팅 레이어에만 있고,
거래 평면은 이 모듈을 임포트할 수 없다(`tests/test_architecture.py` 가 막는다 —
`quant.trade` 는 `quant.adapters` 를 임포트하지 못한다).

## 무료 모델을 쓴다

OpenRouter 의 `:free` 레인(2026-08-13 실측 15개). 기본값은
`nvidia/nemotron-3-ultra-550b-a55b:free` — 같은 날 API 로 `pricing
{prompt: "0", completion: "0"}`, 컨텍스트 1,000,000 을 확인했다.
**모델 ID 에 날짜 접미사를 붙이지 않는다** — 웹 URL 에는 `-20260604` 같은 게
붙어 보이지만 실제 ID 에는 없고, 넣으면 404 다.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable

from quant.adapters.env import get_key

log = logging.getLogger(__name__)

# 실측(2026-08-13, https://openrouter.ai/api/v1/models): pricing 0/0, ctx 1,000,000.
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# 툴콜링 해석 에이전트(서브프로젝트 U, 2026-08-17) 전용 모델 — 서술기
# (DEFAULT_OPENROUTER_MODEL)와 다른 모델이다: 멀티턴 도구 호출 능력이 필요해서
# 별도로 실측했다(OpenRouter 실호출 3회/모델, 멀티턴 도구 루프):
#
#   nvidia/nemotron-3-super-120b-a12b:free — tool_calls 3/3, 한국어 해석 도달
#     3/3, 평균 16.2s, 에러 0 → 1순위.
#   dots-studio/dots-3-note-preview:free   — tool_calls 3/3, 해석 도달 3/3,
#     평균 9.6s, 간헐 한자 혼입 → 2순위(폴백).
#
# 서술기 기본값(nemotron-3-ultra-550b)은 이 실측에서 2/3(502 1/3, 평균 35.8s)로
# 상시 병렬 도구 호출에는 부적합해 서술기 용도만 유지한다 — 근거:
# docs/superpowers/specs/2026-08-17-tool-calling-agent-design.md.
TOOL_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
TOOL_MODEL_FALLBACK = "dots-studio/dots-3-note-preview:free"

# 429 실측(2026-08-17) Retry-After 헤더 값 22초 — 헤더가 없거나 파싱 불가하면
# 이 기본값을 쓴다. 30초는 상한(무한정 기다리지 않는다).
TOOL_RETRY_AFTER_DEFAULT = 22
TOOL_RETRY_AFTER_CAP = 30

# Claude Code CLI 를 부를 때 **전면 차단**하는 도구 목록. 서술만 하면 되므로 파일·셸·
# 네트워크에 닿을 이유가 없고, 프롬프트에 로그 내용(신뢰 불가 입력)이 들어가므로
# 도구가 살아 있으면 프롬프트 주입이 실행으로 이어질 수 있다. daily_brief.sh 와 같은 계약.
CLAUDE_DISALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task,Agent,TodoWrite"
)


def _quietly(fn, *args, what: str):
    """어떤 예외도 `None` 으로 바꾼다.

    포트 계약(`core.ports.Narrator`)이 "실패는 예외가 아니라 None"이므로 그 보장을
    **경계 한 곳**에서 만든다. transport 구현마다 try 를 심으면 하나 빠뜨리는 순간
    경보가 서술기 때문에 죽는다.
    """
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 — 어댑터가 삼키는 지점이다
        log.warning("%s 서술 실패(%s)", what, type(e).__name__)
        return None


def _record_llm_call(lane: str, ok: bool, seconds: float) -> None:
    """OpenRouter 무료 레인 호출 1건을 `quant.control.opstate` 에 계측한다
    (2026-08-18). 요청 수·실패율·지연을 아무도 기록하지 않으면 무료 레인
    한도에 걸리기 시작해도 모른다 — `quant.control.health.llm_health_findings`
    가 이 기록을 읽는다.

    `quant.adapters` → `quant.control` 임포트는 아키텍처 규칙상 허용된다
    (`tests/test_architecture.py` 의 FORBIDDEN 목록에 이 방향은 없다 — 금지된
    건 `quant.control` → `quant.trade` 뿐이다).

    계측 실패가 서술/해석 자체를 죽이면 안 된다 — `opstate.record_llm_call`
    이 이미 `kv.py` 경계에서 예외를 삼키지만, `make_kv()`/임포트 자체가
    실패하는 경우까지 한 번 더 막는다(`_quietly` 와 같은 원칙: 경계는 한
    곳에서 보장한다).
    """
    try:
        from quant.adapters.kv import make_kv
        from quant.control.opstate import record_llm_call

        record_llm_call(make_kv(), lane, ok, seconds)
    except Exception as e:  # noqa: BLE001 — 계측 실패가 호출자를 죽이면 안 된다
        log.debug("LLM 호출 계측 실패(%s)", type(e).__name__)


class NullNarrator:
    """서술하지 않는다. 호출자는 결정론적 형식으로 떨어진다."""

    name = "none"

    def narrate(self, prompt: str) -> str | None:
        return None


class ClaudeCliNarrator:
    """로컬 Claude Code CLI (`claude -p`). 도구를 전면 차단해 부른다.

    `runner` 를 주입받는 이유는 테스트다 — 서브프로세스를 띄우지 않고 계약을 검증한다.
    """

    name = "claude"

    def __init__(self, binary: str, timeout: int = 180, runner=None):
        self._binary = binary
        self._timeout = timeout
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(cmd: list[str], stdin: str, timeout: int) -> str | None:
        import subprocess

        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None

    def narrate(self, prompt: str) -> str | None:
        # 가드는 **포트 경계**에 둔다. transport 안에 두면 주입된 구현이 던질 때
        # 그대로 새어나가고, 그러면 서술기 때문에 경보가 죽는다 —
        # 테스트가 실제로 그 구멍을 잡았다.
        out = _quietly(self._runner,
                       [self._binary, "-p", "--disallowedTools", CLAUDE_DISALLOWED_TOOLS],
                       prompt, self._timeout, what="claude")
        return (out or "").strip() or None


# 무료 레인 추론 모델(nemotron 계열)은 간헐적으로 **영어 사고과정을 최종 답에
# 그대로 유출**한다 — 2026-08-18 실측: 발행된 Exec Summary 가 "The user wants
# me to write three paragraphs..." 로 시작했다. 한국어 서술 계약에서 이건 실패다.
_LEAK_PREFIXES = ("the user", "i need", "i'll", "i will", "let me", "we need",
                  "okay,", "first,", "sure,", "looking at", "based on the")


def looks_like_reasoning_leak(text: str) -> bool:
    """한국어 서술이어야 할 출력이 영어 사고과정으로 시작하는가.

    두 신호 중 하나면 유출로 본다: (1) 전형적 추론 서두("The user wants...")로
    시작, (2) 앞부분이 사실상 영문 — 한글이 5% 미만이면서 영문자가 절반 이상.
    티커·용어(HBM, VIX)가 섞인 정상 한국어 문장은 한글 비중이 높아 걸리지 않는다.
    """
    head = text.strip()[:300]
    if not head:
        return False
    if head.lower().startswith(_LEAK_PREFIXES):
        return True
    # 비율 판정 전에 구조화 꼬리(JUDGMENT: {...} — agent_interpret 계약)를 잘라낸다
    # — JSON 키가 전부 영문이라 짧은 산문에서 오탐을 만든다(테스트가 실제로 잡았다).
    marker = head.find("JUDGMENT:")
    if marker != -1:
        head = head[:marker].strip()
    if len(head) < 20:
        # 표본이 너무 짧으면 비율은 소음이다 — 서두 검사만으로 충분하다.
        return False
    hangul = sum(1 for c in head if "가" <= c <= "힣")
    ascii_alpha = sum(1 for c in head if c.isascii() and c.isalpha())
    return hangul < len(head) * 0.05 and ascii_alpha > len(head) * 0.5


class OpenRouterNarrator:
    """OpenRouter chat/completions. 무료 레인 기본값.

    `poster` 를 주입받는다 — 테스트가 네트워크를 타지 않게 하려는 것이고, 이 저장소는
    "가짜 연결 테스트가 통과했는데 실환경에서 죽은" 경험이 있으므로 실제 스모크는
    별도로 사람이 돌린다(`cli narrate --self-test`).
    """

    name = "openrouter"

    def __init__(self, api_key: str, model: str = DEFAULT_OPENROUTER_MODEL,
                 timeout: int = 60, poster=None, max_tokens: int = 700,
                 json_mode: bool = False):
        self._key = api_key
        self._model = model
        self._timeout = timeout
        self._poster = poster or self._httpx_poster
        self._max_tokens = max_tokens
        # JSON 계약 소비자(ai_trader 토론)용 — 산문 가드(사고과정 유출 폐기)를
        # 끈다. JSON 은 키가 전부 영문이라 한글 비중 휴리스틱이 구조적으로
        # 오탐한다(2026-08-26 실 E2E에서 확인). 방어는 소비자의 엄격한 JSON
        # 파싱이 맡는다 — 쓰레기는 거기서 None(결근)이 된다.
        self._json_mode = json_mode

    @staticmethod
    def _httpx_poster(url: str, headers: dict, payload: dict, timeout: int) -> dict | None:
        import httpx

        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def narrate(self, prompt: str) -> str | None:
        # 무료 레인(nemotron)은 업스트림 502 를 200-with-error 로 돌려주는 간헐
        # 실패가 실측됐다(2026-08-17: 같은 프롬프트가 1차 실패 → 재시도 성공,
        # 당일 실패율 ~50%). 유료 전환 대신 재시도 1회가 비용 0 의 해법이다
        # (사용자 결정: 큰 차이 없으면 무료 유지).
        #
        # 계측(2026-08-18): 재시도까지 포함한 전체 소요를 재서 lane="narrate"
        # 로 기록한다(`_record_llm_call`) — 무료 레인 요청 수·실패율을 아무도
        # 안 보면 한도에 걸리기 시작해도 모른다.
        t0 = time.monotonic()
        text = None
        for attempt in range(2):
            text = self._narrate_once(prompt)
            if text is not None:
                break
            if attempt == 0:
                log.warning("OpenRouter 1차 실패 — 1회 재시도")
                time.sleep(2)
        _record_llm_call("narrate", text is not None, time.monotonic() - t0)
        return text

    def _narrate_once(self, prompt: str) -> str | None:
        body = _quietly(
            self._poster,
            OPENROUTER_URL,
            {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            {"model": self._model,
             "messages": [{"role": "user", "content": prompt}],
             # 기본값 700인 이유: 서술은 짧아야 한다 — 텔레그램 한 통에 들어가고,
             # 길면 사람이 안 읽는다. 파라미터화한 이유: **추론 모델은 최종 답
             # 전에 "생각"에 토큰을 쓴다** — 기본 모델(nemotron-3-ultra)로 9K자
             # deepdive 프롬프트를 돌리면 700 토큰이 생각 과정에서 전부 소진돼
             # 최종 답이 잘리고 파싱 후보가 0건이 된다(실측 2026-08-15). 호출부가
             # 프롬프트 성격에 맞게 올려 쓸 수 있어야 한다.
             "max_tokens": self._max_tokens, "temperature": 0.2},
            self._timeout,
            what="OpenRouter",
        )
        if not isinstance(body, dict):
            return None
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            # 형태가 다르면 조용히 빈 문장을 내지 않는다 — 못 읽었다고 말한다.
            log.warning("OpenRouter 응답 형태가 예상과 다르다")
            return None
        text = (text or "").strip() or None
        if text is not None and not self._json_mode and looks_like_reasoning_leak(text):
            # 실패로 취급 → narrate() 의 재시도 1회를 그대로 탄다. 두 번째도
            # 유출이면 None — 빈 섹션이 오염된 섹션보다 낫다.
            log.warning("OpenRouter 서술에 사고과정 유출 감지 — 폐기")
            return None
        return text


# 텔레그램 채널 사진 해석(서브프로젝트 S part 3, 2026-08-17) — `Narrator` 포트가
# 아니다(입력이 프롬프트 문자열이 아니라 이미지 URL이라 모양이 다르다). OpenRouter
# 무료 레인 중 실제 vision-capable 모델을 실측 확인(2026-08-17,
# https://openrouter.ai/api/v1/models, pricing.prompt=="0" and pricing.
# completion=="0" and "image" in architecture.input_modalities로 필터) —
# nvidia/nemotron-nano-12b-v2-vl:free 를 골랐다: DEFAULT_OPENROUTER_MODEL과 같은
# nvidia/nemotron 계열이고(운영 이미 신뢰), 이름 자체가 "VL"(vision-language)로
# 용도가 명시적이다. 실제 텔레그램 사진(피카츄 아저씨 채널, cdn5.telesco.pe)으로
# 스모크 확인 — 200 OK, pricing 0/0, 한국어 응답.
VISION_MODEL = "nvidia/nemotron-nano-12b-v2-vl:free"
_VISION_PROMPT = (
    "다음은 텔레그램 채널에 올라온 이미지다. 무엇을 보여주는지 한국어 한 문장으로 "
    "설명하라. 차트/표라면 핵심 수치나 방향성 위주로 설명하고, 이미지에 없는 새 "
    "사실은 지어내지 않는다."
)


def describe_image(url: str, api_key: str, poster=None, timeout: int = 30) -> str | None:
    """이미지 URL 1건 → 한국어 1문장 해석. 실패는 예외가 아니라 `None`
    (`OpenRouterNarrator`와 같은 `_quietly` 경계 — 호출부가 예산(최대 3장)을
    쓰는 곳이므로 재시도는 하지 않는다, `OpenRouterNarrator.narrate`의 1회
    재시도와 다른 점).
    """
    poster = poster or OpenRouterNarrator._httpx_poster
    body = _quietly(
        poster,
        OPENROUTER_URL,
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        {"model": VISION_MODEL,
         "messages": [{"role": "user", "content": [
             {"type": "text", "text": _VISION_PROMPT},
             {"type": "image_url", "image_url": {"url": url}},
         ]}],
         "max_tokens": 200, "temperature": 0.2},
        timeout,
        what="OpenRouter(vision)",
    )
    if not isinstance(body, dict):
        return None
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("OpenRouter(vision) 응답 형태가 예상과 다르다")
        return None
    return (text or "").strip() or None


# ---------------------------------------------------------------------------
# 툴콜링 해석 에이전트(서브프로젝트 U) — OpenAI 호환 tool calling.
# ---------------------------------------------------------------------------
#
# `Narrator` 포트가 아니다 — 프롬프트 하나에 문장 하나가 아니라, 모델이 스스로
# 도구를 골라 여러 라운드 호출하는 루프다(`describe_image`처럼 별도 함수로 둔다).


def _tool_httpx_poster(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    """`{"status", "body", "headers"}`. 다른 poster(`_httpx_poster` 등)와 달리
    상태 코드를 그대로 드러낸다 — 429(Retry-After)·502 를 서로 다르게
    재시도하려면 호출부가 상태 코드를 봐야 하고, `raise_for_status()` 로 예외에
    묻으면 그 구분이 사라진다."""
    import httpx

    r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = None
    return {"status": r.status_code, "body": body, "headers": dict(r.headers)}


def _retry_after_seconds(headers: dict) -> int:
    raw = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = TOOL_RETRY_AFTER_DEFAULT
    return max(1, min(seconds, TOOL_RETRY_AFTER_CAP))


def _tool_completion(
    messages: list[dict], tools: list[dict], api_key: str, model: str,
    poster, timeout: int,
) -> dict | None:
    """OpenRouter chat/completions 1회 호출(도구 루프의 스텝 1개). 실패 유형별
    재시도는 최대 1회뿐이다(모듈 상단 TOOL_MODEL 주석의 실측 근거) — 그래도
    안 되면 `None`(호출부가 폴백 모델로 전체 루프를 다시 돈다)."""
    body_payload = {"model": model, "messages": messages, "tools": tools, "temperature": 0.2}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    for attempt in range(2):
        try:
            resp = poster(OPENROUTER_URL, headers, body_payload, timeout)
        except Exception as e:  # noqa: BLE001 — 어댑터 경계, 예외를 밖으로 흘리지 않는다
            log.warning("OpenRouter(tool) 요청 실패(%s)", type(e).__name__)
            return None
        status = resp.get("status") if isinstance(resp, dict) else None
        body = resp.get("body") if isinstance(resp, dict) else None
        if status == 200 and isinstance(body, dict):
            return body
        if status == 429 and attempt == 0:
            wait = _retry_after_seconds(resp.get("headers") or {})
            log.warning("OpenRouter(tool) 429 — %ds 대기 후 재시도", wait)
            time.sleep(wait)
            continue
        if status == 502 and attempt == 0:
            log.warning("OpenRouter(tool) 502 — 재시도")
            time.sleep(2)
            continue
        log.warning("OpenRouter(tool) 실패(status=%r)", status)
        return None
    return None


def _run_tool_loop(
    messages: list[dict], tools: list[dict], api_key: str, model: str, max_rounds: int,
    execute: Callable[[str, dict], str], poster, timeout: int,
) -> dict | None:
    convo = list(messages)
    for round_n in range(1, max_rounds + 1):
        body = _tool_completion(convo, tools, api_key, model, poster, timeout)
        if body is None:
            return None
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            log.warning("OpenRouter(tool) 응답 형태가 예상과 다르다")
            return None
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            text = (msg.get("content") or "").strip()
            if text and looks_like_reasoning_leak(text):
                # 최종 답이 영어 사고과정이면 실패 취급 — 호출부가 폴백 모델로
                # 전체 루프를 다시 돈다(발행물에 유출문이 실리는 것보다 낫다).
                log.warning("OpenRouter(tool) 최종 답에 사고과정 유출 — 폐기")
                return None
            return {"text": text, "rounds": round_n} if text else None
        convo.append(msg)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            result = execute(name, args)
            convo.append({"role": "tool", "tool_call_id": tc.get("id"), "content": result})
    log.warning("OpenRouter(tool) 최대 라운드(%d) 소진 — 최종 텍스트 없음", max_rounds)
    return None


def chat_with_tools(
    messages: list[dict], tools: list[dict], api_key: str,
    model: str = TOOL_MODEL, max_rounds: int = 5,
    execute: Callable[[str, dict], str] | None = None,
    poster=None, timeout: int = 60,
) -> dict | None:
    """OpenAI 호환 tool calling 루프(서브프로젝트 U) — 모델이 `tools` 중 필요한
    것을 스스로 골라 호출하며 최대 `max_rounds` 라운드를 돈다. 라운드마다
    `execute(name, args)` 로 도구를 실행해 그 결과를 tool 메시지로 돌려준다.

    성공하면 `{"text": 최종 답, "rounds": 실제 라운드 수}`. **실패는 예외가
    아니라 `None`**(narrate 계약과 동일 — 리포트 빌드가 LLM 때문에 죽지 않는다).

    1순위 모델(`model`, 기본 `TOOL_MODEL`)이 최종 실패하면(라운드별 502/429
    1회 재시도까지 소진, 응답 형태 이상, 또는 `max_rounds` 소진) 폴백 모델
    (`TOOL_MODEL_FALLBACK`)로 **전체 루프를 1회 다시 돈다**(실측 근거는 모듈
    상단 TOOL_MODEL 주석).
    """
    poster = poster or _tool_httpx_poster
    if execute is None:
        execute = lambda name, args: "{}"  # noqa: E731 — 도구 없이 부르는 호출부(테스트 등)용
    # 계측(2026-08-18): 1순위+폴백 전체 루프를 하나의 호출로 재서 lane="tool"
    # 로 기록한다(`_record_llm_call`) — narrate() 와 같은 이유.
    t0 = time.monotonic()
    result = _run_tool_loop(messages, tools, api_key, model, max_rounds, execute, poster, timeout)
    if result is not None:
        _record_llm_call("tool", True, time.monotonic() - t0)
        return result
    log.warning("OpenRouter(tool) 1순위 모델(%s) 실패 — 폴백(%s) 재시도", model, TOOL_MODEL_FALLBACK)
    result = _run_tool_loop(
        messages, tools, api_key, TOOL_MODEL_FALLBACK, max_rounds, execute, poster, timeout,
    )
    _record_llm_call("tool", result is not None, time.monotonic() - t0)
    return result


def _make_openrouter_narrator(
    env: dict[str, str] | None, model: str | None = None, timeout: int | None = None,
):
    """OpenRouter 레인 조립 — `make_narrator`(기본 레인)와 `make_quality_narrator`
    (품질 레인의 폴백)가 공유한다(2026-08-18, 중복 방지). 키가 없으면 `None`
    (호출부가 `NullNarrator`로 낙착).

    `timeout`(2026-09-04, L2 서술) — 기본(60s)보다 짧게 강제하고 싶은 호출부용
    (예: 텔레그램 리포트 서술은 발행 파이프라인이 분 단위라 20s 상한이 필요하다,
    `quant.analyze.narrator` 모듈 docstring 참고). `None`이면 `OpenRouterNarrator`
    기본값을 그대로 쓴다."""
    e = os.environ if env is None else env
    key = (e.get("OPENROUTER_API_KEY") or "").strip()
    if not key and env is None:
        # 크론은 .env.local 을 export 하지 않는다 — 수집기들은 get_key(파일
        # 직독)라 멀쩡한데 서술기만 os.environ 을 봐서 크론 경로의 LLM 이
        # 조용히 죽어 있었다(2026-08-16 실측: deepdive US 'OPENROUTER_API_KEY
        # 없음'). env 를 명시 주입한 경우는 폴백하지 않는다 — 주입이 곧
        # 전체 환경이다(테스트·의도적 격리의 계약).
        key = (get_key("OPENROUTER_API_KEY") or "").strip()
    if not key:
        log.warning("OPENROUTER_API_KEY 없음 — 서술 없이 동작한다")
        return None
    max_tokens = 700
    raw_max_tokens = (e.get("OPENROUTER_MAX_TOKENS") or "").strip()
    if raw_max_tokens:
        try:
            max_tokens = int(raw_max_tokens)
        except ValueError:
            # 절대 예외를 던지지 않는다 — 잘못된 값은 기본값(700)으로
            # 조용히 떨어지고 경고만 남긴다.
            log.warning("OPENROUTER_MAX_TOKENS=%r 정수가 아니다 — 기본값 700 사용",
                       raw_max_tokens)
            max_tokens = 700
    chosen_model = model or (e.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL).strip()
    kwargs = {"model": chosen_model, "max_tokens": max_tokens}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenRouterNarrator(key, **kwargs)


def make_narrator(
    env: dict[str, str] | None = None, model: str | None = None, timeout: int | None = None,
):
    """`OPS_NARRATOR` 로 고른다. **절대 예외를 던지지 않는다** — 고를 수 없으면
    `NullNarrator` 이고, 호출자는 결정론적 형식으로 떨어진다.

    `kv.py: make_kv()` 와 같은 계약이다.

    `model`(선택) — openrouter 선택 시 `OPENROUTER_MODEL` 환경변수/기본값보다
    **우선**한다(서브프로젝트 W part 2, 2026-08-17: 중기 관심종목 산문 요약은
    U(툴콜링 해석 에이전트)가 실측한 1순위 모델(`TOOL_MODEL`)을 명시적으로
    쓰고 싶은 호출부가 있다 — `make_narrator(model=TOOL_MODEL)`). claude 선택지는
    모델 개념이 없어 무시한다.

    `timeout`(선택, 2026-09-04) — openrouter 선택 시에만 의미가 있다(claude
    CLI는 별도 timeout 인자를 이미 생성자에서 받는다). 텔레그램 리포트 서술처럼
    짧은 상한이 필요한 호출부가 쓴다.
    """
    e = os.environ if env is None else env
    choice = (e.get("OPS_NARRATOR") or "claude").strip().lower()

    if choice == "none":
        return NullNarrator()

    if choice == "openrouter":
        narrator = _make_openrouter_narrator(env, model, timeout)
        return narrator if narrator is not None else NullNarrator()

    if choice == "claude":
        binary = (e.get("CLAUDE_BIN") or "").strip() or os.path.expanduser("~/.local/bin/claude")
        if not os.path.exists(binary):
            log.warning("claude 실행파일 없음(%s) — 서술 없이 동작한다", binary)
            return NullNarrator()
        return ClaudeCliNarrator(binary)

    log.warning("알 수 없는 OPS_NARRATOR=%r — 서술 없이 동작한다", choice)
    return NullNarrator()


def make_json_narrator(env: dict[str, str] | None = None, model: str | None = None,
                       max_tokens: int = 4000, timeout: int = 120):
    """JSON 계약용 좁은 팩토리 (2026-08-26, ai_trader 토론) — `make_narrator` 와
    같은 절대-예외-없음 계약. OpenRouter 전용이다(claude CLI 경로는 산문 계약).

    산문용 기본값과 다른 이유:
    - `json_mode=True` — 사고과정 유출 가드가 JSON(영문 키 위주)을 오탐한다.
    - `max_tokens=4000` — 종목 30개 verdict JSON 은 700 토큰에 잘린다(추론
      모델은 "생각"에도 토큰을 쓴다 — `_narrate_once` 주석 참고).
    - `timeout=120` — 긴 출력 + 무료 레인 지연.

    키가 없으면 NullNarrator — 호출부(ai_trader)는 결근 처리로 떨어진다.
    """
    e = os.environ if env is None else env
    # env 를 명시하면 그 dict 만 본다(테스트 결정성) — 미지정이면 get_key 가
    # os.environ + .env.local 파일 폴백까지 본다(크론은 export 를 안 한다).
    key = (e.get("OPENROUTER_API_KEY") or "").strip()
    if not key and env is None:
        key = (get_key("OPENROUTER_API_KEY") or "").strip()
    if not key:
        log.warning("OPENROUTER_API_KEY 없음 — JSON 서술 없이 동작한다(ai_trader 결근)")
        return NullNarrator()
    chosen = (model or e.get("OPENROUTER_MODEL") or "").strip() or DEFAULT_OPENROUTER_MODEL
    return OpenRouterNarrator(key, model=chosen, timeout=timeout,
                              max_tokens=max_tokens, json_mode=True)


class QualityFallbackNarrator:
    """품질 레인(2026-08-18) — Claude CLI(구독) 1순위, 실패하면 OpenRouter
    무료 레인 폴백. `make_quality_narrator()`가 조립해 돌려준다.

    사용자 A/B 실측 근거(`compare_narrators.html`, 같은 스냅샷): OpenRouter
    무료판은 Exec Summary 영어 사고과정 유출·시황 산문 누락·4섹션 해석
    누락이 있었고, Claude 구독판은 전부 고품질이었다 — "느린 건 상관없어,
    정확도가 중요해"라는 사용자 결정에 따라 품질 민감 서술만 이 레인으로 옮긴다.

    이 클래스 자체는 가드·재시도 로직을 갖지 않는다 — 그건 이미 `primary`/
    `fallback`(`ClaudeCliNarrator`/`OpenRouterNarrator`) 각자가 갖고 있다.
    여기서 하는 일은 둘뿐이다: (1) 1순위 실패 시 폴백으로 넘기기 (2) 그
    결과를 lane="quality"로 계측하기 — `ClaudeCliNarrator`엔 계측이 없어서다.
    폴백이 실제로 쓰인 호출은 `OpenRouterNarrator.narrate`가 이미 자체적으로
    lane="narrate"를 기록하므로 이중 기록된다 — 의도한 동작이다: "narrate"
    레인은 OpenRouter 실사용량을, "quality"는 품질 레인 전체 성공률을 답한다.
    """

    name = "quality"

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback

    def narrate(self, prompt: str) -> str | None:
        t0 = time.monotonic()
        text = self._primary.narrate(prompt)
        if text is None:
            log.warning("품질 레인 1순위(Claude CLI) 실패 — OpenRouter 폴백")
            text = self._fallback.narrate(prompt)
        _record_llm_call("quality", text is not None, time.monotonic() - t0)
        return text


def make_quality_narrator(env: dict[str, str] | None = None, model: str | None = None):
    """품질 민감 서술 전용 레인(2026-08-18) — `report_cli._emit`(아침판)의
    Exec Summary/시황 다이제스트/4섹션 AI 해석/중기 관심종목 산문 4곳 전용.
    오후판(`_emit_close`)은 13:50 발행→13:55 자동편입→14:00 유니버스 롤
    체인이 분 단위라 속도가 정확도의 전제라서 기존 무료 레인(`make_narrator`)을
    그대로 쓴다 — 이 함수를 부르지 않는다.

    킬스위치: env `QUALITY_NARRATOR=off`면 `make_narrator(env, model=model)`를
    그대로 반환한다(전량 무료 레인 복귀 — 첫 실전이 틀어졌을 때 한 변수로
    롤백할 수 있게).

    Claude CLI 실행파일이 없거나(미설치) 인증 실패·미로그인 등으로
    `narrate()`가 실패해도 예외 없이 `None`을 돌려주므로(`ClaudeCliNarrator`
    의 `_quietly` 계약) 그대로 OpenRouter 폴백으로 흐른다 — 여기서 추가로
    감쌀 필요가 없다.

    `model`(선택) — `make_narrator(model=...)`와 같은 계약. OpenRouter
    폴백에 강제할 모델을 넘긴다.
    """
    e = os.environ if env is None else env
    if (e.get("QUALITY_NARRATOR") or "").strip().lower() == "off":
        return make_narrator(env, model=model)

    binary = (e.get("CLAUDE_BIN") or "").strip() or os.path.expanduser("~/.local/bin/claude")
    if os.path.exists(binary):
        # 리포트 프롬프트(수천 자)는 기본 180s 로는 빠듯할 수 있어 넉넉히
        # 잡는다 — EC2 실측 콜당 소요는 아직 없어 보수적으로 240s.
        primary = ClaudeCliNarrator(binary, timeout=240)
    else:
        log.warning("claude 실행파일 없음(%s) — 품질 레인은 OpenRouter 단독", binary)
        primary = NullNarrator()

    fallback = _make_openrouter_narrator(env, model) or NullNarrator()
    return QualityFallbackNarrator(primary, fallback)
