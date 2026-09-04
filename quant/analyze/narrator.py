"""L2 서술 — 결정론적 사실(facts)을 사람이 읽는 산문으로. `quant/analyze` 평면.

## 왜 이 파일이 있나 (2026-09-04 소유자 요구)

"텔레그램 메시지가 자연스러운 챗봇처럼 느껴져야 한다. 숫자가 산문으로 둔갑한
느낌이 아니라." L1(`quant/core/tgfmt.py`)이 서식만 바꾼다면, 이 파일은 판단을
문장으로 바꾼다 — 단, **판단 자체는 절대 여기서 하지 않는다.** 이미 확정된
`facts` dict를 받아 그대로 재진술하는 문장만 만든다. 이건 ADR-0002(거래
핫패스에 LLM/네트워크 금지)의 경계이기도 하다 — 이 모듈은 리포팅 레인
(`quant/analyze`, `quant/control`, `quant/apps`)에서만 쓰이고 `quant/trade`는
이 파일을 임포트할 수 없다(`tests/test_architecture.py`가 강제).

## 왜 `quant/adapters/narrate.py`를 여기서 직접 부르지 않는가

`tests/test_architecture.py`의 FORBIDDEN 집합에 `("quant.analyze",
"quant.adapters")`가 있다 — analyze 평면은 어댑터(네트워크)를 임포트할 수
없다. 그래서 실제 LLM 호출기(OpenRouter 등)는 이 모듈이 만들지 않는다:
호출부(`quant.apps`, 어댑터를 알아도 되는 평면)가
`quant.adapters.narrate.make_narrator(...).narrate`를 만들어 `call` 인자로
주입한다 — `quant.analyze.manual_recs`의 `PriceLookup` 주입과 같은 관례다.
이 파일이 하는 일은 둘뿐이다: (1) 프롬프트 조립 (2) 응답의 숫자 검증. 둘 다
순수 함수라 네트워크 없이 테스트할 수 있다.

## 계약 — 절대 예외를 던지지 않는다

- `call`이 `None`이면(주입 안 함, 즉 게이트가 꺼져 있음) 서술 없이 `None`.
- `call(prompt)`이 예외를 던지거나 빈 문자열/`None`을 돌려주면 `None`.
- 응답에 등장하는 숫자 중 **하나라도** `facts`에 없으면(지어낸 숫자) 문장
  전체를 폐기하고 `None` — 절반만 믿을 수 있는 문장은 안 믿느니만 못하다.
  숫자만 검증하고 문장 자체(어순·조사)는 신뢰한다 — 완벽한 사실 검증은
  아니지만, LLM이 가장 잘 지어내는 것이 숫자이기 때문에 이 축만 방어한다.

호출부는 실패를 구분할 필요가 없다 — `None`이면 결정론적 메시지만 단독으로
나간다(모듈 상단 소유자 요구의 "Fallback" 조항).
"""
from __future__ import annotations

import re
from collections.abc import Callable

# 부호가 있는 숫자만 부호를 인정한다 — 날짜("2026-09-04")처럼 숫자 뒤에 바로
# 붙는 '-'는 뺄셈/음수가 아니라 구분자다. 앞이 숫자가 아닐 때만(공백·괄호·
# 문장 시작 등) 부호로 인정한다.
_NUMBER_RE = re.compile(r"(?<![0-9])[+-]?\d[\d,]*\.?\d*")

# 서술 문단 예산(문자 수) — "3~6개의 짧은 문장"(시스템 프롬프트) 한 문단은
# 보통 600자 안팎이다. 이보다 넉넉히 잡아두고, 그래도 넘치면(모델이 규칙을
# 어기고 길게 쓰거나 사고과정 일부가 섞여도) 마지막 문장 종결부호 뒤에서만
# 자른다 — 단어 중간에서 자르면 안 된다(실측, 2026-09-04 market-pulse: "...금리
# 10년물 라벨은 없음, 스프레"처럼 잘려 나갔었다). 텔레그램 어댑터(`quant/
# adapters/notify/telegram.py`)의 4096자 하드컷과는 별개 계층 — 그쪽은 전체
# 메시지 바이트 상한, 이건 서술 한 문단의 문장 경계 보장이다.
NARRATION_MAX_CHARS = 700
_SENTENCE_END_CHARS = ".!?。"


def _sentence_safe_truncate(text: str, limit: int) -> str:
    """`text`가 `limit`자를 넘으면 그 안에서 마지막 문장 종결부호(., !, ?, 。)
    뒤까지만 남기고 자른다. 종결부호를 못 찾으면(문장 하나가 limit보다 길거나
    구두점이 아예 없음) 안전망으로 `limit`에서 그냥 자른다 — 그런 극단적인
    경우까지 완벽을 기하기보다, 정상적인 3~6문장 응답이 절대 단어 중간에서
    잘리지 않게 하는 것이 목적이다."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max((head.rfind(ch) for ch in _SENTENCE_END_CHARS), default=-1)
    if cut <= 0:
        return head.rstrip()
    return head[: cut + 1].rstrip()

_SYSTEM_PROMPT_KO = (
    "당신은 차분하고 친근한 퀀트 트레이딩 어시스턴트입니다. 소유자에게 오늘 "
    "결과를 말하듯 자연스러운 한국어로 3~6개의 짧은 문장을 쓰세요.\n"
    "규칙:\n"
    "1. 아래 [사실]에 있는 내용만 다른 말로 풀어 쓰세요 — 숫자는 [사실]에 적힌 "
    "그대로(자릿수·부호까지) 등장해야 합니다.\n"
    "2. 앞으로 어떻게 될지 예측하지 마세요. 무엇을 하라고 조언하지 마세요.\n"
    "3. [사실]에 없는 숫자를 절대 지어내지 마세요.\n"
    "4. 마지막 한 문장은 반드시 '오늘 눈여겨볼 것: '으로 시작하고, [사실] 안의 "
    "내용에서만 뽑아 마무리하세요.\n"
    "5. [사실]에 없는 항목(값이 비어 있어 아예 나열되지 않은 항목 포함)은 "
    "언급하지 마세요.\n"
)


def _flatten_facts(facts: dict) -> list[str]:
    """dict를 "경로: 값" 줄 목록으로 평탄화 — 프롬프트에 그대로 박아 넣는다."""
    lines: list[str] = []

    def _walk(prefix: str, value: object) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                _walk(f"{prefix}.{k}" if prefix else str(k), v)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                _walk(f"{prefix}[{i}]", v)
        else:
            # None/빈 문자열은 아예 줄을 만들지 않는다 — "없음"으로 치환했더니
            # 모델이 그 문구를 그대로 따라 써 "금리 10년물 라벨은 없음"처럼
            # 있지도 않은 사실을 언급했다(실측, 2026-09-04 market-pulse
            # 실호출). 값이 없으면 애초에 [사실]에 등장시키지 않는다 — 시스템
            # 프롬프트 규칙 5와 짝(둘 다 있어야 한다: 규칙만으로는 모델이
            # 프롬프트에 있는 줄 자체를 무시하지 못할 때가 있었다).
            if value is None or value == "":
                return
            lines.append(f"{prefix}: {value}")

    _walk("", facts)
    return lines


def build_prompt(kind: str, facts: dict, lang: str = "ko") -> str:
    """`kind`(서술 종류, 예: "session_pnl")와 `facts`로 LLM 프롬프트 조립.
    `lang`은 현재 한국어 고정이지만(실제 수신자가 한국어뿐), 향후 확장을 위해
    시그니처에 남긴다(`market_pulse.render_telegram`의 `lang` 파라미터와 같은 자리)."""
    body = "\n".join(_flatten_facts(facts)) or "(없음)"
    return f"{_SYSTEM_PROMPT_KO}\n[종류]\n{kind}\n\n[사실]\n{body}\n"


def _normalize_number(token: str) -> str:
    """"1,234.50"과 "1234.5"를 같은 숫자로, "+5"와 "5"를 다른 숫자로(부호는
    의미가 있다 — 손익 부호를 실수로 뒤집는 것이 가장 위험한 환각이다)."""
    token = token.replace(",", "")
    sign = ""
    if token.startswith("+"):
        token = token[1:]
    elif token.startswith("-"):
        sign, token = "-", token[1:]
    try:
        f = float(token)
    except ValueError:
        return sign + token
    body = str(int(f)) if f == int(f) else str(f)
    return sign + body


def _numbers_in(text: str) -> set[str]:
    return {_normalize_number(m) for m in _NUMBER_RE.findall(text)}


def _fact_numbers(facts: dict) -> set[str]:
    return _numbers_in(" ".join(_flatten_facts(facts)))


def verify_numbers(text: str, facts: dict) -> bool:
    """`text`에 등장하는 모든 숫자가 `facts`에도 등장하는가(정규화 후 비교)."""
    allowed = _fact_numbers(facts)
    return all(n in allowed for n in _numbers_in(text))


def narrate(
    kind: str,
    facts: dict,
    *,
    lang: str = "ko",
    call: Callable[[str], str | None] | None = None,
) -> str | None:
    """`facts`를 산문으로. 실패·검증 실패는 예외가 아니라 `None`.

    `call`은 호출부(`quant.apps`)가 주입하는 실제 LLM 호출기 —
    `quant.adapters.narrate.make_narrator(...).narrate`처럼 `프롬프트문자열 ->
    답 또는 None`인 콜러블이면 무엇이든 된다. 주입하지 않으면(`None`, 게이트가
    꺼져 있을 때 호출부가 이렇게 부른다) 서술 자체를 시도하지 않는다.
    """
    if call is None:
        return None
    prompt = build_prompt(kind, facts, lang)
    try:
        text = call(prompt)
    except Exception:  # noqa: BLE001 — 서술 실패가 리포트를 죽이면 안 된다
        return None
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    if not verify_numbers(text, facts):
        return None
    # 숫자 검증 다음에 자른다 — 뒤쪽만 잘라내므로 새 숫자가 생길 일은 없다
    # (재검증 불필요).
    return _sentence_safe_truncate(text, NARRATION_MAX_CHARS)
