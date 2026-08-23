"""상장 종목 목록 원본을 받아 캐시한다 — KIND 상장사 목록, 나스닥 SymDir, S&P500.

`quant/analyze/entities.py`가 이 캐시 파일들을 파싱해 종목 추출 사전을 만든다.
네트워크로 받아오는 일(수집)과 텍스트를 파싱하는 일(분석)을 나눈다 — analyze
평면은 스크래핑하지 않는다(4평면 표, collect만 네트워크/재시도를 다룬다).
"""
from __future__ import annotations

from pathlib import Path

from quant.adapters.http import client

KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
SP500_LIST_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def fetch_kind_corp_list(cache: Path) -> bytes:
    """KIND 상장사 목록. 캐시가 있으면 재다운로드하지 않는다."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with client(timeout=30.0) as c:
            cache.write_bytes(c.get(KIND_URL).content)
    return cache.read_bytes()


def fetch_sp500_list(cache: Path) -> str:
    """S&P500 구성종목(위키피디아). 캐시가 있으면 재다운로드하지 않는다."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with client(timeout=20.0) as c:
            resp = c.get(SP500_LIST_URL)
            resp.raise_for_status()
        cache.write_text(resp.text, encoding="utf-8")
    return cache.read_text(encoding="utf-8")


def fetch_symbol_dir(url: str, cache: Path) -> str:
    """나스닥 공식 심볼 디렉터리(파이프 구분 텍스트). 캐시가 있으면 재다운로드하지 않는다."""
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with client(timeout=30.0) as c:
            resp = c.get(url)
            resp.raise_for_status()
        cache.write_text(resp.text, encoding="utf-8")
    return cache.read_text(encoding="utf-8")
