"""`quant apps cli outcomes` 의 `--scorer-version` 기본값 — §E-2, H-2 Task 4.

베이스라인 산식을 trending_score100 상수 50 에서 baseline_score100 으로
교체했을 때 기본값을 "2"로 올렸다(v1 구 산식 표본과 섞이지 않게). 이번에는
선정 원장 속성 벡터에 `news_z`(뉴스 발행량 z-score)가 새로 추가돼 기본값을
"3"으로 다시 올린다 — `cmd_outcomes` 는 날짜 필터 없이 selections 전체를
재판단하므로(judgment.py 상단 주석), 속성 벡터가 바뀔 때마다 버킷을 새로
만들지 않으면 서로 다른 세대의 표본이 같은 producer_version 아래 섞인다
(002_judgment.sql 의 uq_judgment 가 producer_version 을 자연키에 넣는 이유와 같다).
"""
from __future__ import annotations

import sys

from quant.apps import cli


def test_outcomes_scorer_version_defaults_to_3(monkeypatch):
    captured: dict = {}

    def fake_cmd_outcomes(args):
        captured["scorer_version"] = args.scorer_version

    monkeypatch.setattr(cli, "cmd_outcomes", fake_cmd_outcomes)
    monkeypatch.setattr(sys, "argv", ["quant", "outcomes", "--dry-run"])

    cli.main()

    assert captured["scorer_version"] == "3"
