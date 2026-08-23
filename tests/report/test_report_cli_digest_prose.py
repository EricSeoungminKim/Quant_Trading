"""`report_cli._build_digest_prose` — 시황 다이제스트 LLM 요약 배선(스펙 §2).

`_emit`(전체 리포트 발행 파이프라인)을 통째로 태우는 대신 이 헬퍼 하나를
단위로 검증한다 — `_emit` 은 섹터/공시/리서치 등 무관한 부수 효과를 잔뜩
끌고 다녀 여기서 보고 싶은 것(narrator 배선)과 무관한 고정 비용이 크다.
"""
from __future__ import annotations

from quant.apps.report_cli import _build_digest_prose


class _FakeNarrator:
    def __init__(self, reply):
        self._reply = reply
        self.called_with: str | None = None

    def narrate(self, prompt: str):
        self.called_with = prompt
        return self._reply


def _digest(domestic=None, us_impact=None):
    return {"domestic": domestic or [], "us_impact": us_impact or []}


def test_build_digest_prose_calls_make_narrator_and_returns_parsed_prose(monkeypatch):
    narrator = _FakeNarrator(
        "① 국내 시장 요약: 오늘 국내 증시는 잠잠했다.\n\n"
        "② 미국발 재료가 한국 증시에 주는 영향: 미국발 재료도 크지 않았다.",
    )
    monkeypatch.setattr("quant.adapters.narrate.make_narrator", lambda: narrator)

    digest = _digest(domestic=[{"title": "사건1", "link": "https://a", "dup_count": 1}])
    out = _build_digest_prose(digest)

    assert narrator.called_with is not None
    assert "사건1" in narrator.called_with
    assert out == {
        "domestic_prose": "오늘 국내 증시는 잠잠했다.",
        "us_prose": "미국발 재료도 크지 않았다.",
    }


def test_build_digest_prose_skips_narrator_when_digest_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "quant.adapters.narrate.make_narrator",
        lambda: calls.append("called") or _FakeNarrator("무시됨"),
    )

    out = _build_digest_prose(_digest())

    assert out is None
    assert calls == []  # make_narrator 자체를 부르지 않는다 — 빈 다이제스트로 LLM 을 안 부른다


def test_build_digest_prose_returns_none_when_narrator_fails(monkeypatch):
    monkeypatch.setattr(
        "quant.adapters.narrate.make_narrator", lambda: _FakeNarrator(None),
    )

    digest = _digest(domestic=[{"title": "사건1", "link": "https://a", "dup_count": 1}])
    assert _build_digest_prose(digest) is None
