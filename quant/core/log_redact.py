"""로그에서 시크릿을 지우는 필터.

**왜 필요한가 (2026-08-13 실측).** `journalctl -u quant-engine` 에 텔레그램 봇
토큰이 이틀간 **381번** 평문으로 남아 있었다:

    INFO HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage

httpx 가 요청 URL 을 INFO 로 찍는데 텔레그램은 토큰을 **경로에** 담기 때문이다.
저널 읽기 권한만 있으면 봇을 그대로 탈취할 수 있었다. 같은 측정에서 키움·토스
앱키는 0건이었다(본문에 담겨 URL 에 안 드러난다) — 즉 문제는 "시크릿을 다루는
방식"이 아니라 **URL 에 실리는 종류의 시크릿**이었다.

**설계 원칙 — 로그를 끄지 않는다.** httpx 로거를 WARNING 으로 내리면 토큰은
사라지지만 어젯빔 키움/토스 장애를 진단할 때 결정적이었던 요청 로그도 함께
사라진다. 그래서 레벨을 낮추는 대신 **값만 가린다**.

**2단 방어:**
1. **값 기반** — 프로세스가 아는 실제 시크릿 문자열을 그대로 치환한다. 형식을
   몰라도 잡히므로 이게 주 방어다. 환경변수 이름으로 시크릿을 식별한다.
2. **형태 기반** — 런타임에 발급받는 토큰(키움/토스 access token, `Bearer ...`)은
   값 목록에 없으므로 형태로 잡는다.

**한계를 분명히 한다.** 이건 *로그* 유출만 막는다. `.env.local` 자체는 평문으로
디스크에 있고(600), 그걸 진짜로 없애려면 EC2 에 IAM 역할을 붙여 SSM Parameter
Store 를 쓰는 수밖에 없다 — 로컬에서 암호화해봐야 복호화 재료가 같은 박스에
있으므로 난독화에 지나지 않는다.
"""
from __future__ import annotations

import logging
import os
import re

# 이 조각이 이름에 있으면 그 환경변수 값을 시크릿으로 본다.
_SECRET_NAME_HINTS = (
    "TOKEN", "SECRET", "APP_KEY", "APPKEY", "API_KEY", "APIKEY", "PASSWORD", "PASSWD",
)

# 너무 짧은 값은 치환하면 오히려 로그가 망가진다("N", "1" 같은 설정값이
# 시크릿 이름에 걸릴 수 있다).
MIN_SECRET_LEN = 12

REDACTED = "<REDACTED>"

# 런타임 발급 토큰은 값 목록에 없다 — 형태로 잡는다.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 텔레그램: 경로에 토큰이 들어간다. 실제 유출 사례.
    (re.compile(r"(/bot)\d{6,}:[A-Za-z0-9_-]{20,}"), r"\1" + REDACTED),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1" + REDACTED),
    # JSON/쿼리 본문에 실린 키 — 우리 코드가 찍지 않아도 서드파티가 찍을 수 있다.
    (re.compile(r"((?:appkey|secretkey|secret_key|api_key|access_token|token)"
                r"[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
     r"\1" + REDACTED),
    # URL 쿼리스트링의 `*key=값` 전반 — 위 목록은 이름을 일일이 등재해야 하는데
    # DART의 `crtfc_key`가 실제로 빠져 있었다(2026-08-19 실측, httpx INFO 로그에
    # `?crtfc_key=<평문>`이 그대로 남음). 새 API가 다른 이름의 `_key` 쿼리 파라미터를
    # 쓰기 시작해도 이름을 추가하지 않고 잡히도록 일반화한다.
    (re.compile(r"([?&][A-Za-z_][A-Za-z0-9_]*key=)[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
     r"\1" + REDACTED),
)


def known_secrets(env: dict[str, str] | None = None) -> list[str]:
    """환경에서 시크릿로 보이는 값들. 긴 것부터 — 부분 치환으로 조각이 남지 않게."""
    src = os.environ if env is None else env
    vals = {
        v for k, v in src.items()
        if any(h in k.upper() for h in _SECRET_NAME_HINTS) and len(v) >= MIN_SECRET_LEN
    }
    return sorted(vals, key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    """알려진 시크릿 값 + 알려진 시크릿 형태를 모두 가린다."""
    for s in secrets if secrets is not None else known_secrets():
        if s in text:
            text = text.replace(s, REDACTED)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """포맷된 메시지에서 시크릿을 지운다.

    핸들러에 붙인다 — 로거에 붙이면 하위 로거(httpx 등)가 상위로 전파한 레코드를
    놓친다. 실제 유출이 httpx 로거였으므로 이 구분이 중요하다.

    **`os.environ` 기반 값은 매 호출마다 새로 읽는다** — 생성 시점에 한 번만
    캐시하면(예전 구현) `install()`이 로거 초기화 직후(값이 로드되기 전)에 한 번
    불리고 이후 `load_settings()`가 `.env.local`을 `os.environ`에 채운 뒤 다시
    `install()`을 불러도 "이미 설치됨"으로 no-op 처리돼 새 값이 영영 안 잡히는
    구조적 결함이 있었다(2026-08-19 실측: `known_secrets()`가 비어 있는 상태로
    캐시된 채 굳어 있었다 — "만든 것과 배선된 것은 다르다"). 매 호출 스캔은
    `os.environ` 수십 개 항목 훑기라 비용이 무시할 만하다.

    `extra_secrets`는 `os.environ`에 없는 시크릿(예: `.env.local`만 읽고
    `os.environ`을 채우지 않는 `quant.adapters.env.get_key()` 경로의 값)을 호출부가
    직접 주입하는 통로다 — `quant/core/`는 `quant/adapters/`를 임포트할 수 없어
    `.env.local`을 스스로 읽을 수 없다(4평면 규칙).
    """

    def __init__(self, extra_secrets: list[str] | None = None) -> None:
        super().__init__()
        self._extra_secrets: list[str] = list(extra_secrets) if extra_secrets else []

    def add_secrets(self, more: list[str]) -> None:
        """생성 이후 추가로 알게 된 시크릿을 더한다(중복 무시)."""
        self._extra_secrets.extend(s for s in more if s not in self._extra_secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — 포맷 실패가 로깅을 죽이면 안 된다
            return True
        secrets = sorted(set(known_secrets()) | set(self._extra_secrets), key=len, reverse=True)
        cleaned = redact(message, secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def install(logger: logging.Logger | None = None, extra_secrets: list[str] | None = None) -> int:
    """루트(또는 지정) 로거의 **모든 핸들러**에 필터를 건다. 새로 설치한 핸들러 수 반환.

    이미 설치돼 있으면 중복으로 걸지 않는다(테스트에서 반복 호출해도 안전) — 대신
    `extra_secrets`가 주어지면 기존 필터에 병합한다. `os.environ` 기반 값은
    `SecretRedactingFilter`가 매 로그마다 새로 읽으므로 여기서 신경 쓸 필요 없다.
    """
    target = logger or logging.getLogger()
    installed = 0
    for handler in target.handlers:
        existing = next(
            (f for f in handler.filters if isinstance(f, SecretRedactingFilter)), None
        )
        if existing is not None:
            if extra_secrets:
                existing.add_secrets(extra_secrets)
            continue
        handler.addFilter(SecretRedactingFilter(extra_secrets))
        installed += 1
    return installed
