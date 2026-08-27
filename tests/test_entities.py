"""KR 제목→종목 추출(`entities.extract`) 계약.

2026-08-28 에 값싼 사전 필터(`name not in text` 조기 탈락)를 넣으면서 만든다 —
그 최적화가 **판정을 바꾸지 않는다**는 것을 고정하는 게 목적이다. 최적화 자체의
근거(태깅이 7일치에 25분+)는 `entities.extract` 주석에 있다.
"""
from __future__ import annotations

from quant.analyze.entities import extract

# (이름, 종목코드) — 실제 테이블과 같은 형태. 긴 이름 우선 정렬이 전제다.
TABLE = [("삼성전자", "005930"), ("한미반도체", "042700"), ("태양", "053620")]


def test_extract_finds_name_in_title():
    hits = extract("삼성전자, HBM 공급 확대", TABLE)
    assert [h["symbol"] for h in hits] == ["005930"]


def test_extract_allows_korean_particle_after_name():
    """조사(은/는/이/가…)가 붙어도 같은 종목이다 — 한국어 제목의 기본형."""
    hits = extract("삼성전자가 신고가를 썼다", TABLE)
    assert [h["symbol"] for h in hits] == ["005930"]


def test_extract_rejects_fragment_inside_longer_word():
    """'태양광 산업'의 '태양'은 종목이 아니다 — 뒤가 조사 아닌 한글이면 조각."""
    assert extract("태양광 산업 전망 밝다", TABLE) == []


def test_extract_returns_empty_when_no_name_present():
    """사전 필터가 조기 탈락시키는 경로 — 결과는 여전히 빈 목록이어야 한다."""
    assert extract("코스피 상승 마감", TABLE) == []


def test_extract_finds_multiple_distinct_symbols():
    hits = extract("삼성전자와 한미반도체 동반 강세", TABLE)
    assert sorted(h["symbol"] for h in hits) == ["005930", "042700"]
