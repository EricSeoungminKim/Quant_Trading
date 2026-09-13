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
import re
import shutil
import time
from collections.abc import Callable

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

# Claude CLI 를 1순위로 승격(2026-09-07)한 뒤 OpenRouter가 **폴백으로만**
# 쓰이는 자리(make_narrator의 claude 선택지, make_quality_narrator)의 상한.
# Claude 가 이미 시간을 쓴 다음의 마지막 시도이므로, narrate()의 기존 1회
# 재시도 관례(2회 시도 + 2초 대기)를 포함해도 전체 벽시계가 45초를 넘지
# 않게 묶는다(2*20+2=42s) — 무료 레인이 report build(12분 상한)·tg-digest
# (90초 상한) 예산을 잠식하지 않기 위해서다.
FALLBACK_OPENROUTER_TIMEOUT_S = 20

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


def _record_llm_call(lane: str, ok: bool, seconds: float, transport: str = "openrouter") -> None:
    """LLM 호출 1건을 `quant.control.opstate` 에 계측한다 (2026-08-18,
    `transport` 추가는 2026-09-07 — Claude CLI 를 주 레인으로 전환하면서
    "무엇이 실패했나"를 lane 하나로는 답할 수 없어졌다: 같은 lane("quality"
    등) 안에서도 1순위(claude)와 폴백(openrouter)이 섞여 기록되면 "claude가
    맛이 갔다"와 "openrouter 폴백이 원래 그렇다"가 구분이 안 된다.
    `quant.control.health.llm_health_findings` 가 이 기록을 transport 별로
    읽어 임계값을 다르게 적용한다(claude=주 레인이라 엄격, openrouter=폴백이라
    참고용).

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

        record_llm_call(make_kv(), lane, ok, seconds, transport=transport)
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
        if r.returncode != 0:
            # 사유를 남긴다(2026-09-07): 08:00 KR 빌드에서 이 레인이 15회 연속 실패했는데 로그가
            # "실패 — 폴백" 한 줄뿐이라 한도 초과인지 인증인지 알 수 없었다. stderr 첫 줄만.
            head = (r.stderr or r.stdout or "").strip().splitlines()
            log.warning("claude CLI 실패 rc=%s: %s", r.returncode, (head[0][:200] if head else "(출력 없음)"))
            return None
        return r.stdout

    def narrate(self, prompt: str) -> str | None:
        # 가드는 **포트 경계**에 둔다. transport 안에 두면 주입된 구현이 던질 때
        # 그대로 새어나가고, 그러면 서술기 때문에 경보가 죽는다 —
        # 테스트가 실제로 그 구멍을 잡았다.
        out = _quietly(self._runner,
                       [self._binary, "-p", "--disallowedTools", CLAUDE_DISALLOWED_TOOLS],
                       prompt, self._timeout, what="claude")
        return (out or "").strip() or None


class CodexCliNarrator:
    """Codex 구독 CLI로 주어진 사실만 서술한다. 파일·셸·외부 도구는 끈다.

    사용자 설정/프로젝트 지시와 세션 기록을 읽지 않고 임시 빈 디렉터리에서
    실행한다. 인증은 기존 Codex 로그인으로 처리하며 키를 프롬프트에 넣지 않는다.
    """

    name = "codex"

    def __init__(self, binary: str, timeout: int = 180, runner=None, model: str | None = None):
        self._binary = binary
        self._timeout = timeout
        self._model = model
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(cmd: list[str], stdin: str, timeout: int) -> str | None:
        import signal
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory(prefix="quant-narrate-") as workdir:
            output = Path(workdir) / "final.txt"
            cmd = [*cmd, "--cd", workdir, "--output-last-message", str(output), "-"]
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=workdir, start_new_session=True,
            )
            try:
                proc.communicate(input=stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise
            if proc.returncode != 0:
                # stderr에는 프롬프트·계정 정보가 섞일 수 있으므로 원문을 로그에 싣지 않는다.
                log.warning("codex CLI 실패 rc=%s", proc.returncode)
                return None
            return output.read_text(encoding="utf-8") if output.exists() else None

    def narrate(self, prompt: str) -> str | None:
        cmd = [self._binary, "exec", "--ephemeral", "--ignore-user-config",
               "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never",
               "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
               "-c", "project_doc_max_bytes=0"]
        for feature in ("shell_tool", "unified_exec", "apps", "plugins", "multi_agent",
                        "browser_use", "computer_use", "image_generation", "view_image",
                        "code_mode", "code_mode_host", "hooks", "memories"):
            cmd += ["-c", f"features.{feature}=false"]
        if self._model:
            cmd += ["--model", self._model]
        out = _quietly(self._runner, cmd, prompt, self._timeout, what="codex")
        return (out or "").strip() or None


def cli_binary(env: dict[str, str] | None = None, *, provider: str = "codex") -> str:
    """명시 경로 → PATH → EC2 사용자 설치 경로. 주입 환경은 실제 환경과 섞지 않는다."""
    e = os.environ if env is None else env
    explicit = (e.get(f"{provider.upper()}_BIN") or "").strip()
    if explicit:
        return explicit
    return (shutil.which(provider, path=e.get("PATH", ""))
            or os.path.expanduser(f"~/.local/bin/{provider}"))


# 무료 레인 추론 모델(nemotron 계열)은 간헐적으로 **영어 사고과정을 최종 답에
# 그대로 유출**한다 — 2026-08-18 실측: 발행된 Exec Summary 가 "The user wants
# me to write three paragraphs..." 로 시작했다. 한국어 서술 계약에서 이건 실패다.
_LEAK_PREFIXES = ("the user", "i need", "i'll", "i will", "let me", "we need",
                  "okay,", "first,", "sure,", "looking at", "based on the")


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)


def _strip_reasoning_block(text: str) -> str:
    """추론 모델이 `<think>...</think>` 사고과정 블록을 `content` 안에 그대로
    흘려보내는 경우(무료 nemotron 계열, 소유자 실측 2026-09-05: KR 다이제스트
    스탠스 서술이 이 블록 때문에 매번 폐기됐다) 그 블록만 제거하고 나머지를
    돌려준다. 블록이 통째로 최종 답을 차지하면(태그 안이 전부) 빈 문자열이
    남는다 — 그건 `_narrate_once`의 "빈 문자열은 실패" 규칙이 그대로 처리한다.
    별도 `reasoning` 필드(OpenRouter 일부 모델이 `message.reasoning`으로 따로
    준다)는 애초에 `content`만 읽으므로 여기 섞이지 않는다 — 조용히 버려진다.
    태그가 없으면 원문 그대로(비용 없음)."""
    if "<think>" not in text.lower():
        return text
    return _THINK_BLOCK_RE.sub("", text).strip()


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
                 timeout: int = 60, poster=None, max_tokens: int = 1500,
                 json_mode: bool = False, temperature: float = 0.2,
                 reasoning_exclude: bool = False):
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
        self._temperature = temperature
        # `reasoning: {"exclude": true}` — 스탠스 마이크로프롬프트(2026-09-05,
        # `stance_only`) 전용 옵션. **실측 주의**: 이 플래그는 모델이 별도
        # `reasoning` 필드에 사고과정을 담는 경우 그 필드를 숨길 뿐, 사고과정을
        # `content`에 직접 이어쓰는 모델(nemotron 계열 등)의 토큰 소비 자체는
        # 막지 못한다 — 실측(2026-09-05, max_tokens=120)으로 여러 무료 모델이
        # `reasoning_tokens`가 `max_tokens`를 넘겨 `content`가 통째로 `None`이
        # 되는 것을 확인했다. 그래서 `stance_only`는 이 플래그를 켜되, 실패를
        # 전제로 폴백 모델을 따로 둔다.
        self._reasoning_exclude = reasoning_exclude

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
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            # 기본값 1500인 이유: 서술은 짧아야 한다 — 텔레그램 한 통에 들어가고,
            # 길면 사람이 안 읽는다(문자 수 상한은 narrator.py의
            # NARRATION_MAX_CHARS=700이 문장 경계로 자른다). 다만 700 토큰은
            # 한국어 3~6문장(≈600자)에 너무 빠듯했다 — 한글은 토큰당 표시
            # 글자 수가 영어보다 작아, 답이 완성되기 전에 토큰이 바닥나 문장
            # 중간에서 잘려 나갔다(실측, 2026-09-04 market-pulse: "...스프레"
            # 처럼 단어 중간 절단). 파라미터화한 이유: **추론 모델은 최종 답
            # 전에 "생각"에 토큰을 쓴다** — 기본 모델(nemotron-3-ultra)로 9K자
            # deepdive 프롬프트를 돌리면 토큰이 생각 과정에서 전부 소진돼 최종
            # 답이 잘리고 파싱 후보가 0건이 된다(실측 2026-08-15). 호출부가
            # 프롬프트 성격에 맞게 올려 쓸 수 있어야 한다.
            "max_tokens": self._max_tokens, "temperature": self._temperature,
        }
        if self._reasoning_exclude:
            # 응답에 별도 `reasoning` 필드가 있는 모델은 그 필드를 숨긴다 —
            # `content` 로의 사고과정 유출까지는 못 막는다(위 `__init__` 주석).
            payload["reasoning"] = {"exclude": True}
        body = _quietly(
            self._poster,
            OPENROUTER_URL,
            {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            payload,
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
        # <think> 블록만 제거하고 나머지는 검증한다(2026-09-05 소유자 지시) —
        # 예전엔 유출 감지 즉시 전체 폐기라 블록 뒤에 멀쩡한 한국어 답이 와도
        # 버려졌다(KR 다이제스트 스탠스 실측: 3회 중 3회 전부 이 이유로 폐기).
        if text is not None and not self._json_mode:
            text = _strip_reasoning_block(text) or None
        if text is not None and not self._json_mode and looks_like_reasoning_leak(text):
            # 블록을 벗기고도 여전히 유출 신호면(사고과정이 태그 없이 새거나
            # 블록 밖에도 남은 경우) 실패로 취급 → narrate()의 재시도 1회를
            # 그대로 탄다. 두 번째도 유출이면 None — 빈 섹션이 오염된 섹션보다 낫다.
            log.warning("OpenRouter 서술에 사고과정 유출 감지 — 폐기")
            return None
        return text


# ---------------------------------------------------------------------------
# 스탠스 전용 마이크로프롬프트(2026-09-05, tg_digest 소유자 요구 — "유료 레인이
# 꼭 필요한가? 아니다") — `Narrator` 포트가 아니다(산문 한 줄이 아니라 엄격한
# JSON 계약 `{"stance", "why"}`뿐이라 모양이 다르다, `describe_image`와 같은
# 이유로 별도 함수).
#
# **실측(2026-09-05, max_tokens=120, reasoning.exclude=true)** — 기본 모델
# (nemotron 계열)은 `reasoning_tokens`가 `max_tokens`를 그대로 다 먹거나
# 넘겨(`content`에 사고과정을 직접 이어쓰는 구조라 exclude 플래그가 못 막는다)
# `content`가 통째로 `None`이 된다. 후보 다수를 같은 조건으로 실측한 결과
# `poolside/laguna-s-2.1:free`만 3/3 `reasoning_tokens=0`으로 엄격한 JSON
# 계약을 그대로 통과했다(다른 후보 — google/gemma-4-31b-it:free 는 업스트림
# 공유 풀 429, dots-studio/dots-3-note-preview:free·liquid/lfm-2.5-2.6b:free·
# minimax/minimax-m2.7:free·nvidia/nemotron-3.5-lightning:free 는 전부
# reasoning_tokens 로 예산 소진 또는 사고과정 유출). 그래서 기본 모델을
# 1순위로 시도만 하고(성공하면 그대로 쓴다), 실패를 전제로 이 모델을
# 폴백으로 둔다.
STANCE_FALLBACK_MODEL = "poolside/laguna-s-2.1:free"
STANCE_MAX_TOKENS = 120
_STANCE_VALUES = ("방어", "중립", "공격")


def _parse_stance_json(text: str | None) -> dict | None:
    """엄격한 JSON 계약 — `{"stance": "방어"|"중립"|"공격", "why": <=60자 문자열}`
    정확히 이 두 키만 허용한다. 다른 텍스트·마크다운 코드펜스·추가 키가
    섞이거나 `stance` 값이 셋 중 하나가 아니면(예: "공격적") 통째로 거부한다
    — "절반만 맞는 JSON은 안 믿느니만 못하다"(narrator.py verify_numbers와
    같은 all-or-nothing 원칙)."""
    if not text:
        return None
    try:
        parsed = json.loads(text.strip())
    except ValueError:
        return None
    if not isinstance(parsed, dict) or set(parsed.keys()) != {"stance", "why"}:
        return None
    stance = parsed.get("stance")
    why = parsed.get("why")
    if stance not in _STANCE_VALUES:
        return None
    if not isinstance(why, str) or not why.strip() or len(why) > 60:
        return None
    return {"stance": stance, "why": why}


def stance_only(
    prompt: str, api_key: str,
    model: str = DEFAULT_OPENROUTER_MODEL, fallback_model: str = STANCE_FALLBACK_MODEL,
    poster=None, timeout: int = 20,
) -> dict | None:
    """스탠스 전용 마이크로프롬프트 1회 판정 — `{"stance", "why"}` | `None`.

    `model`(기본 모델)을 먼저 1회 시도하고, 엄격 검증에 실패하면(빈 응답,
    사고과정으로 예산 소진, JSON 계약 불일치 등) `fallback_model`(실측으로
    확인한 무추론 모델)로 1회 더 시도한다 — narrate()처럼 같은 모델을 두 번
    재시도하지 않는다(다른 모델 두 개를 각 1회씩). 둘 다 실패하면 `None` —
    LLM 스탠스는 선택 사항이다(`quant.analyze.tg_digest.Digest.
    program_stance_display()`가 결정론 스탠스는 이 함수와 무관하게 항상
    낸다). `max_tokens=120`·`temperature=0`·`reasoning.exclude=true` 고정.
    """
    t0 = time.monotonic()
    for m in (model, fallback_model):
        narrator = OpenRouterNarrator(
            api_key, model=m, timeout=timeout, poster=poster,
            max_tokens=STANCE_MAX_TOKENS, json_mode=True,
            temperature=0, reasoning_exclude=True,
        )
        result = _parse_stance_json(narrator._narrate_once(prompt))
        if result is not None:
            _record_llm_call("stance", True, time.monotonic() - t0)
            return result
    _record_llm_call("stance", False, time.monotonic() - t0)
    return None


def stance_via_claude(prompt: str, binary: str, timeout: int = 25) -> dict | None:
    """스탠스 전용 마이크로프롬프트 — Claude CLI 경로(2026-09-07, Claude CLI
    주 레인 전환). `ClaudeCliNarrator`로 1회 호출 후 `_parse_stance_json`
    (OpenRouter 경로 `stance_only`와 동일한 엄격 JSON 계약)으로 검증한다.
    프롬프트(`tg_digest._stance_prompt`)가 이미 "다른 텍스트·마크다운
    코드펜스 금지"를 명시하므로 별도 파싱 관용은 두지 않는다 — 코드펜스가
    섞여 나오면 그대로 실패(`None`)로 취급해 `stance()`가 OpenRouter로
    폴백한다("절반만 맞는 JSON은 안 믿느니만 못하다" 원칙, 모듈 상단 참고).

    실패(실행 실패·타임아웃·계약 불일치)는 예외가 아니라 `None`(narrate
    계약과 동일).
    """
    t0 = time.monotonic()
    narrator = ClaudeCliNarrator(binary, timeout=timeout)
    result = _parse_stance_json(narrator.narrate(prompt))
    _record_llm_call("stance", result is not None, time.monotonic() - t0, transport="claude")
    return result


def stance_via_codex(prompt: str, binary: str, timeout: int = 25) -> dict | None:
    """Codex도 같은 엄격 JSON 계약으로 검증하고 별도 transport로 계측한다."""
    t0 = time.monotonic()
    narrator = CodexCliNarrator(binary, timeout=timeout, model=os.environ.get("CODEX_MODEL"))
    result = _parse_stance_json(narrator.narrate(prompt))
    _record_llm_call("stance", result is not None, time.monotonic() - t0, transport="codex")
    return result


def stance(
    prompt: str, *, claude_binary: str | None = None, claude_timeout: int = 25,
    codex_binary: str | None = None, codex_timeout: int = 25,
    api_key: str | None = None, model: str = DEFAULT_OPENROUTER_MODEL,
    fallback_model: str = STANCE_FALLBACK_MODEL, poster=None, openrouter_timeout: int = 20,
) -> dict | None:
    """스탠스 전용 마이크로프롬프트 — Claude CLI 1순위, OpenRouter(`stance_only`)
    폴백(2026-09-07). `_tg_digest_stance_call`/`_channel_digest_stance_call`
    이 이 함수 하나로 두 전송 수단을 순서대로 시도한다 — 시퀀싱 로직을
    호출부(apps 레이어) 두 곳에 중복시키지 않는다.

    `claude_binary`가 `None`이거나 파일이 없으면 1순위를 건너뛴다(호출부가
    실행파일 존재를 미리 확인할 필요 없게). `api_key`가 없으면 폴백도
    건너뛴다. 계측은 `stance_via_claude`/`stance_only`가 각자
    transport("claude"/"openrouter")로 이미 기록하므로 여기서 또 기록하지
    않는다(`QualityFallbackNarrator`와 다른 점 — 그쪽은 실패해도 항상 폴백을
    시도하는 클래스라 자체 기록이 필요했지만, 여긴 두 leaf 함수가 이미
    스스로 기록하는 계약이라 얹을 필요가 없다).
    """
    if codex_binary and os.path.exists(codex_binary):
        result = stance_via_codex(prompt, codex_binary, timeout=codex_timeout)
        if result is not None:
            return result
    if claude_binary and os.path.exists(claude_binary):
        result = stance_via_claude(prompt, claude_binary, timeout=claude_timeout)
        if result is not None:
            return result
    if not api_key:
        return None
    return stance_only(prompt, api_key, model=model, fallback_model=fallback_model,
                       poster=poster, timeout=openrouter_timeout)


def make_stance_call(cli_timeout: int = 25):
    """리포트·텔레그램 스탠스가 동일한 서술기 선택/킬스위치를 따른다."""
    choice = (os.environ.get("OPS_NARRATOR") or "codex").strip().lower()
    if choice not in {"codex", "claude", "openrouter"}:
        return None
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip() or (get_key("OPENROUTER_API_KEY") or "").strip()
    if choice == "openrouter":
        return (lambda prompt: stance_only(prompt, key)) if key else None
    binary = cli_binary(provider=choice)
    if not os.path.exists(binary) and not key:
        return None
    kwargs = {f"{choice}_binary": binary, f"{choice}_timeout": cli_timeout}
    return lambda prompt: stance(prompt, api_key=key or None, **kwargs)


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
    max_tokens = 1500
    raw_max_tokens = (e.get("OPENROUTER_MAX_TOKENS") or "").strip()
    if raw_max_tokens:
        try:
            max_tokens = int(raw_max_tokens)
        except ValueError:
            # 절대 예외를 던지지 않는다 — 잘못된 값은 기본값(1500)으로
            # 조용히 떨어지고 경고만 남긴다.
            log.warning("OPENROUTER_MAX_TOKENS=%r 정수가 아니다 — 기본값 1500 사용",
                       raw_max_tokens)
            max_tokens = 1500
    chosen_model = model or (e.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL).strip()
    kwargs = {"model": chosen_model, "max_tokens": max_tokens}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenRouterNarrator(key, **kwargs)


def make_narrator(
    env: dict[str, str] | None = None, model: str | None = None, timeout: int | None = None,
    *, cli_timeout: int = 180, lane: str = "narrate",
):
    """OPS_NARRATOR: codex(기본), openrouter, none. claude는 명시 선택만 지원한다.

    Codex 구독 CLI를 먼저 쓰고 실패하면 OpenRouter 무료 레인으로 폴백한다.
    실행파일/키가 없어도 예외 없이 None 서술로 떨어져 결정론 보고를 유지한다.
    model/timeout은 OpenRouter 선택 또는 폴백에만 적용한다. Codex 모델은
    CODEX_MODEL, CLI 시간 예산은 cli_timeout, 계측 레인은 lane으로 정한다.
    """
    e = os.environ if env is None else env
    choice = (e.get("OPS_NARRATOR") or "codex").strip().lower()

    if choice == "none":
        return NullNarrator()

    if choice == "openrouter":
        narrator = _make_openrouter_narrator(env, model, timeout)
        return narrator if narrator is not None else NullNarrator()

    if choice in {"codex", "claude"}:
        binary = cli_binary(env, provider=choice)
        if os.path.exists(binary):
            primary = (CodexCliNarrator(binary, timeout=cli_timeout, model=e.get("CODEX_MODEL"))
                       if choice == "codex" else ClaudeCliNarrator(binary, timeout=cli_timeout))
        else:
            log.warning("%s 실행파일 없음(%s) — OpenRouter 폴백만 시도한다", choice, binary)
            primary = NullNarrator()
        fb_timeout = timeout if timeout is not None else FALLBACK_OPENROUTER_TIMEOUT_S
        fallback = _make_openrouter_narrator(env, model, fb_timeout) or NullNarrator()
        return QualityFallbackNarrator(primary, fallback, lane=lane, primary_transport=choice)

    log.warning("알 수 없는 OPS_NARRATOR=%r — 서술 없이 동작한다", choice)
    return NullNarrator()


def make_json_narrator(env: dict[str, str] | None = None, model: str | None = None,
                       max_tokens: int = 4000, timeout: int = 120):
    """JSON 토론/리뷰도 Codex 기본값 + OpenRouter JSON 폴백을 쓴다.

    출력 스키마 검증은 기존 소비자(ai_trader/risk_review 등)가 계속 맡는다.
    OPS_NARRATOR=none/openrouter도 존중한다. timeout은 CLI 시간 예산이며
    OpenRouter 단독에서도 기존대로 적용한다. CLI 폴백은 최대 20초/회다.
    """
    e = os.environ if env is None else env
    choice = (e.get("OPS_NARRATOR") or "codex").strip().lower()
    if choice not in {"codex", "claude", "openrouter"}:
        return NullNarrator()
    # env 를 명시하면 그 dict 만 본다(테스트 결정성) — 미지정이면 get_key 가
    # os.environ + .env.local 파일 폴백까지 본다(크론은 export 를 안 한다).
    key = (e.get("OPENROUTER_API_KEY") or "").strip()
    if not key and env is None:
        key = (get_key("OPENROUTER_API_KEY") or "").strip()
    chosen = (model or e.get("OPENROUTER_MODEL") or "").strip() or DEFAULT_OPENROUTER_MODEL
    fallback_timeout = timeout if choice == "openrouter" else min(timeout, FALLBACK_OPENROUTER_TIMEOUT_S)
    fallback = (OpenRouterNarrator(key, model=chosen, timeout=fallback_timeout,
                                   max_tokens=max_tokens, json_mode=True)
                if key else NullNarrator())
    if choice == "openrouter":
        return fallback
    binary = cli_binary(env, provider=choice)
    if not os.path.exists(binary):
        return fallback
    primary = (CodexCliNarrator(binary, timeout=timeout, model=e.get("CODEX_MODEL"))
               if choice == "codex" else ClaudeCliNarrator(binary, timeout=timeout))
    return QualityFallbackNarrator(primary, fallback, lane="narrate", primary_transport=choice)


class QualityFallbackNarrator:
    """선택한 CLI(기본 Codex) 실패 시 무료 OpenRouter로 폴백한다.

    quality/narrate/agent_interpret/ops_judge 레인이 공유한다. 2026-08에
    품질 서술용으로 도입했고, 현재 CLI 전송은 Codex가 기본이다. 각 시도의
    transport를 구분해 계측하고 성공한 전송은 last_transport에 남긴다.
    폴백 자체의 narrate 계측과 이 레인의 계측은 기존대로 함께 기록한다.
    """

    def __init__(self, primary, fallback, lane: str = "quality", primary_transport: str | None = None):
        self._primary = primary
        self._fallback = fallback
        self._lane = lane
        self._primary_transport = primary_transport or getattr(primary, "name", "claude")
        self.last_transport: str | None = None
        self.name = lane

    def narrate(self, prompt: str) -> str | None:
        t0 = time.monotonic()
        text = self._primary.narrate(prompt)
        _record_llm_call(self._lane, text is not None, time.monotonic() - t0,
                         transport=self._primary_transport)
        if text is not None:
            self.last_transport = self._primary_transport
            return text
        log.warning("%s 레인 1순위(%s) 실패 — OpenRouter 폴백", self._lane, self._primary_transport)
        t1 = time.monotonic()
        text = self._fallback.narrate(prompt)
        _record_llm_call(self._lane, text is not None, time.monotonic() - t1, transport="openrouter")
        self.last_transport = "openrouter" if text is not None else None
        return text


def make_quality_narrator(env: dict[str, str] | None = None, model: str | None = None):
    """아침판 품질 서술: OPS_NARRATOR 선택을 존중하고 CLI에 240초를 준다.

    기본은 Codex CLI + 무료 OpenRouter 폴백이다. OPS_NARRATOR=none이면
    품질 레인도 끄고, openrouter면 그 전송만 쓴다. QUALITY_NARRATOR=off는
    일반 make_narrator의 예산/레인으로 돌아간다. 무료 레인 강제가 아니다.
    model 인자는 OpenRouter 폴백 모델에만 적용한다.
    """
    e = os.environ if env is None else env
    if (e.get("QUALITY_NARRATOR") or "").strip().lower() == "off":
        return make_narrator(env, model=model)

    return make_narrator(env, model=model, cli_timeout=240, lane="quality")
