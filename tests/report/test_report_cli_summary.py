"""`report_cli summary` — 발행 알림에 붙는 결정론 요약 (G Task 5).

`run_report.sh` 의 `notify()` 가 `$PY -m quant.apps.report_cli summary --market
$MARKET` 를 호출해 텔레그램 본문에 덧붙인다. 알림은 부가 기능이므로 엔진
JSON 이 없거나 깨졌을 때도 CLI 자체는 exit 0 이어야 한다(실패가 발송을 막지
않는다 — 모듈 계약).

출력은 stdout 3줄 이내:
  1행 `후보 N개` — `auto_watch` 문자열의 토큰 수("AUTO_WATCH: 없음" → 0).
  2행 `상위: 이름(점수) · 이름(점수) · 이름(점수)` — `symbols` 를
      `baseline_score100` 내림차순 상위 3, `None` 은 제외(0 위장 금지),
      동점은 심볼코드 오름차순(결정론). 채점된 종목이 하나도 없으면 2행 자체를
      생략한다.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from quant.apps import report_cli


def _write_engine_json(out_root: Path, market: str, d: date, payload: dict) -> None:
    path = out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{market}_engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _run(tmp_path: Path, market: str, d: date) -> str:
    """`main()` 을 통해 실제 CLI 배선(`report summary --market … --root …`)을 태운다."""
    rc = report_cli.main(
        ["summary", "--market", market, "--date", d.isoformat(), "--root", str(tmp_path)]
    )
    assert rc == 0
    return rc


def test_summary_prints_candidate_count_and_top3_by_baseline_score_desc(
    tmp_path, capsys,
):
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-08-17",
        "auto_watch": "AUTO_WATCH: 005930:NEWS+RANK 000660:RANK 035420:NEWS",
        "symbols": [
            {"symbol": "005930", "name": "삼성전자", "baseline_score100": 70},
            {"symbol": "000660", "name": "SK하이닉스", "baseline_score100": 85},
            {"symbol": "035420", "name": "네이버", "baseline_score100": 60},
            # baseline 없는 심볼 — 상위 3 산정에서 빠져야 한다(0 위장 금지)
            {"symbol": "051910", "name": "LG화학"},
        ],
    }
    _write_engine_json(tmp_path / "out", "KR", date(2026, 8, 17), payload)

    _run(tmp_path, "KR", date(2026, 8, 17))
    out = capsys.readouterr().out.strip("\n")

    lines = out.split("\n")
    assert lines[0] == "후보 3개"
    assert lines[1] == "상위: SK하이닉스(85) · 삼성전자(70) · 네이버(60)"
    assert len(lines) == 2  # 3줄 이내(여기선 2줄)


def test_summary_missing_engine_json_prints_nothing_and_exits_zero(tmp_path, capsys):
    rc = report_cli.main(
        ["summary", "--market", "US", "--date", "2026-08-17", "--root", str(tmp_path)]
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_summary_no_candidates_reports_zero(tmp_path, capsys):
    payload = {
        "schema": 1, "market": "US", "session_date": "2026-08-17",
        "auto_watch": "AUTO_WATCH: 없음",
        "symbols": [],
    }
    _write_engine_json(tmp_path / "out", "US", date(2026, 8, 17), payload)

    _run(tmp_path, "US", date(2026, 8, 17))
    out = capsys.readouterr().out.strip("\n")

    assert out == "후보 0개"  # 2행(상위) 자체가 없다 — 채점된 종목이 없으므로


def test_summary_omits_top_line_when_all_baselines_are_none(tmp_path, capsys):
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-08-17",
        "auto_watch": "AUTO_WATCH: 005930:NEWS 000660:NEWS",
        "symbols": [
            {"symbol": "005930", "name": "삼성전자"},
            {"symbol": "000660", "name": "SK하이닉스"},
        ],
    }
    _write_engine_json(tmp_path / "out", "KR", date(2026, 8, 17), payload)

    _run(tmp_path, "KR", date(2026, 8, 17))
    out = capsys.readouterr().out.strip("\n")

    assert out == "후보 2개"


def test_summary_tie_breaks_equal_scores_by_change_pct_then_symbol_code(tmp_path, capsys):
    """동점 1순위는 당일 등락률 내림차순 — baseline 포화(08-16 실측 100점 9개,
    백로그 P1-b) 상태에서 코드순만 쓰면 당일 -5.8% 종목이 텔레그램 요약 맨
    앞에 오는 실사고가 있었다. 등락률까지 같으면 심볼코드 오름차순(결정론)."""
    payload = {
        "schema": 1, "market": "KR", "session_date": "2026-08-17",
        "auto_watch": "AUTO_WATCH: BBB:NEWS AAA:NEWS CCC:NEWS DDD:NEWS",
        "symbols": [
            # 코드순이라면 금호전기류(하락)가 먼저다 — 등락률이 이겨야 한다
            {"symbol": "AAA", "name": "하락동점", "baseline_score100": 50, "change_pct": -5.8},
            {"symbol": "BBB", "name": "상승동점", "baseline_score100": 50, "change_pct": 11.9},
            # 등락률 없는 동점은 맨 뒤(모르는 값을 유리하게 치지 않는다)
            {"symbol": "CCC", "name": "무등락동점", "baseline_score100": 50},
            {"symbol": "DDD", "name": "저점수상승", "baseline_score100": 40, "change_pct": 20.0},
        ],
    }
    _write_engine_json(tmp_path / "out", "KR", date(2026, 8, 17), payload)

    _run(tmp_path, "KR", date(2026, 8, 17))
    out = capsys.readouterr().out.strip("\n")

    # 점수가 먼저(40점 20% 상승이 50점들을 앞지르지 않는다), 동점 안에서만 등락률
    assert out.split("\n")[1] == "상위: 상승동점(50) · 하락동점(50) · 무등락동점(50)"
