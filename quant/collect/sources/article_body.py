"""기사 본문 fetch. 스펙: docs/superpowers/specs/2026-08-15-news-deepdive-design.md

**본문은 영속화하지 않는다** — LLM 프롬프트 재료로만 쓰고 버린다(EC2 1.8GB).
그래서 상한 4,000자. 실패는 None 이다 — "" 로 돌려주면 '본문이 빈 기사'와
'못 가져온 기사'가 섞이고, 이 저장소는 그 패턴으로 데이터를 잃은 적이 있다.
"""
from __future__ import annotations

import html as _html
import re

from quant.adapters.http import client

MAX_BODY_CHARS = 4000
_MIN_PARA_CHARS = 30  # 이보다 짧은 <p> 는 메뉴·바이라인 부류다

_DROP_RE = re.compile(r"<(script|style|nav)[^>]*>.*?</\1>", re.S | re.I)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_text(html_text: str) -> str:
    """<p> 문단만 모은다. 본문이 <p> 밖에 있는 사이트는 빈 문자열이 나온다 —
    그건 실패이고, fetch_body 가 None 으로 승격한다."""
    cleaned = _DROP_RE.sub("", html_text)
    paras = []
    for raw in _P_RE.findall(cleaned):
        p = _html.unescape(_TAG_RE.sub("", raw)).strip()
        if len(p) >= _MIN_PARA_CHARS:
            paras.append(p)
    return "\n".join(paras)[:MAX_BODY_CHARS]


def _http_get(url: str) -> str | None:
    # stock_detail.py 와 같은 클라이언트·예절: with client() as c: ... resp.raise_for_status()
    with client() as c:
        resp = c.get(url)
        resp.raise_for_status()
    return resp.text


def fetch_body(url: str, getter=None) -> str | None:
    get = getter or _http_get
    try:
        raw = get(url)
    except Exception:  # noqa: BLE001 — 개별 기사 실패가 배치를 죽이지 않는다
        return None
    if not raw:
        return None
    return extract_text(raw) or None
