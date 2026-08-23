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


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=timeout,
        follow_redirects=True,
        http2=False,
    )
