"""텔레그램 포럼 토픽 "레인" 라우팅 — 단일 정의(2026-09-05).

**왜 core 인가.** 파이썬 노티파이어(`quant/adapters/notify/telegram.py`)와 셸
게이트(`server/scripts/lib/notify.sh`)가 "레인 id → (chat_id, thread_id)" 판정을
각자 구현하면 갈라진다 — 위 파일 `quant/core/CLAUDE.md`의 `strategy_ids.py`와
같은 이유다. 이 모듈은 그 판정 규칙의 순수 함수 형태(외부 의존 0)를 정의한다.
셸 쪽은 파이썬 프로세스를 임포트하지 않고 JSON 파싱만 `python3 -c`로 복제하지만
(크론 환경에서 `quant` 패키지가 항상 임포트 가능하다고 보장할 수 없어서),
그 판정 로직은 여기 함수와 정확히 같아야 한다 — `tests/test_notify_gate.py`가
둘의 결과를 대조한다.

## 배경

오너가 텔레그램 슈퍼그룹 하나에 포럼 토픽(Forum Topics) 5개를 만들고, 각 발신
스크립트/엔진 알림이 정해진 토픽(`message_thread_id`)으로 가게 한다 — 지금은
알림 전부가 한 채팅방에 섞여 있다(2026-08-28 게이트 도입 당시의 불만: "너무
복잡하다"의 다음 단계). 매핑은 `data/state/tg_lanes.json`에 브리지가 쓰고
(`/here <레인>` 명령), 모두가 읽는다.

## 레인이 바인딩되지 않았을 때

매핑 파일이 아직 없거나(오너가 아직 토픽을 안 만들었다), 특정 레인만 바인딩이
안 됐으면 `resolve()`는 레거시 단일 채팅(`legacy_chat_id`, 기존 `TELEGRAM_CHAT_ID`)
으로 폴백한다 — **아무것도 깨지지 않는다**. 폴백된 메시지에는 어느 레인 것인지
알 수 있게 한 줄 헤더(`header()`)를 붙인다 — 단, 그 판단(`is_bound`)은 호출부
(어댑터/셸) 책임이다: 이 모듈은 "언제 헤더를 붙일지"를 결정하지 않고 재료만 준다.
"""
from __future__ import annotations

# 레인 id → (헤더 이모지, 한국어 표시명). 순서가 `/lanes` 표시 순서다.
LANES: dict[str, tuple[str, str]] = {
    "control": ("🎛", "제어실"),
    "trades": ("📈", "매매"),
    "briefs": ("📰", "브리핑"),
    "intel": ("📡", "채널 인텔"),
    "ops": ("🚨", "운영"),
}


def header(lane: str) -> str:
    """레인의 한 줄 표시 헤더(이모지 + 이름). 등록되지 않은 레인 id면 그대로 돌려준다
    (호출부가 오타를 감췄다고 오판하지 않도록 — 조용히 빈 문자열을 주지 않는다)."""
    emoji_name = LANES.get(lane)
    if emoji_name is None:
        return lane
    emoji, name = emoji_name
    return f"{emoji} {name}"


def is_bound(mapping: dict | None) -> bool:
    """매핑 파일이 **최소 한 레인이라도** 바인딩했는가.

    호출부(어댑터/셸)가 "레거시 채팅으로 떨어지는 메시지에 헤더를 붙일지"를
    판단하는 데 쓴다 — 오너가 아직 한 번도 `/here`를 안 쳤으면(마이그레이션
    이전) 기존 동작 그대로 헤더 없이 나가야 하고, 어느 토픽이든 하나라도
    바인딩된 뒤에는 레거시 채팅이 "여러 레인이 섞이는 방"이 되므로 헤더로
    구분해야 한다.
    """
    return bool(mapping and mapping.get("chat_id") and mapping.get("threads"))


def resolve(
    lane: str,
    mapping: dict | None,
    legacy_chat_id: object = None,
) -> tuple[object, int | None]:
    """`(chat_id, thread_id)`를 돌려준다.

    `mapping`이 `{"chat_id": ..., "threads": {lane: thread_id, ...}}` 형태이고
    `lane`이 그 안에 바인딩돼 있으면 그 `(chat_id, thread_id)`를, 아니면(매핑이
    없거나, 레인이 아직 바인딩 안 됐으면) `(legacy_chat_id, None)`을 돌려준다 —
    thread_id가 `None`이면 "레거시 채팅으로 폴백했다"는 뜻이다.

    `chat_id`/`legacy_chat_id`의 타입은 신경 쓰지 않는다(파이썬 쪽은 문자열
    env 값을, 브리지는 정수를 쓴다) — 이 함수는 그대로 통과시킬 뿐이다.
    """
    if mapping:
        chat_id = mapping.get("chat_id")
        threads = mapping.get("threads") or {}
        thread_id = threads.get(lane)
        if chat_id is not None and thread_id is not None:
            return chat_id, thread_id
    return legacy_chat_id, None
