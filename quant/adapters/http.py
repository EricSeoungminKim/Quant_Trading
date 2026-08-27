"""공용 HTTP 클라이언트.

HTTP/1.1을 강제한다 — fred.stlouisfed.org 가 httpx 기본 설정에서 읽기 타임아웃을
내고 curl 은 0.06초에 응답한 사례가 있었다(2026-08-12 실측).
"""
from __future__ import annotations

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def client(timeout: float = 20.0, user_agent: str | None = UA) -> httpx.Client:
    """`user_agent=None` 이면 UA 헤더를 아예 보내지 않는다(httpx 기본).

    **왜 끌 수 있어야 하나** — 위 브라우저 UA 는 봇 차단을 피하려 넣었는데
    fred.stlouisfed.org 에서는 정반대로 그 UA 가 차단 대상이다. 2026-08-28 실측
    (같은 EC2·같은 순간, 전체 시계열 CSV 268KB):

        래퍼 그대로(이 UA)      ReadTimeout 30.1초
        이 UA 만 얹은 httpx     ReadTimeout 30.1초
        UA 없는 맨몸 httpx       200, 0.1초

    UA 를 빼는 것만으로 응답이 온다 — 느린 서버가 아니라 UA 기반 차단이었다.
    (kind.krx.co.kr 403 은 이것과 **다르다**: UA 3종 전부 403 이라 IP 차단이고
    UA 로는 못 푼다. 호스트마다 실측할 것 — 한 곳의 원인을 다른 곳에 옮겨
    적용하지 마라.)
    """
    headers = {"User-Agent": user_agent} if user_agent else {}
    return httpx.Client(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
        http2=False,
    )
