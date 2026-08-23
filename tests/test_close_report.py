"""장마감 결과 리포트 — Task 5(§E-3). 순수 조립 함수 + CLI 배선 테스트.

`quant.apps.cli outcomes`(16:00 크론)가 그날 만기가 된 지평의 전방 수익률을
선정 원장에 채운다. 이 모듈은 그중 **오늘 채워진 것만** 골라 상위/하위 성과와
표본 수를 사람이 읽는 문장으로 조립한다 — 시세 조회도 파일 I/O도 하지 않는다.

## CLI 배선 회귀(2026-08-15 리뷰)

`cmd_close_report`가 `build_close_report` 결과를 들고 **narrate 를 먼저 부른
뒤에야 print** 하던 초판은 "서술기가 죽어도 결정론 요약은 나간다"는 계약을
실제로 지키지 못했다 — 서술기(로컬 Claude CLI, 기본 timeout 180s)가 셸 래퍼의
timeout 보다 오래 걸리면 SIGTERM 으로 프로세스가 죽고 stdout 이 통째로 비어
"생성 실패"가 발송됐다. 아래 `test_cmd_close_report_flushes_report_before_narrate_runs`
가 이 순서(print+flush 가 narrate 호출보다 먼저)를 고정한다.
"""
from __future__ import annotations

import argparse
import json

import pytest

from quant.control.close_report import build_close_report, matured_today
from quant.control.leaderboard import Verdict


def _row(symbol: str, market: str, date: str, **outcomes) -> dict:
    row = {"symbol": symbol, "market": market, "date": date}
    row.update(outcomes)
    return row


def _verdict(reason: str) -> Verdict:
    return Verdict(
        verdict="no_edge", promote=False, mean_ic=0.01, t_stat=0.5, required_t=1.96,
        n_days=5, n_days_dropped=0, n_trials=1, reason=reason,
    )


# ── matured_today: 오늘 만기 채워진 지평만 뽑는다 ──────────────────────────

def test_matured_today_picks_rows_whose_asof_is_today():
    rows = [
        _row("005930", "KR", "2026-08-10", outcome_d5_bps=120.0, outcome_d5_asof="2026-08-17"),
        _row("TQQQ", "US", "2026-08-14", outcome_d1_bps=-80.0, outcome_d1_asof="2026-08-17"),
        # 다른 날 만기 — 오늘 리포트에 나오면 안 된다
        _row("SQQQ", "US", "2026-08-13", outcome_d1_bps=30.0, outcome_d1_asof="2026-08-14"),
    ]
    matured = matured_today(rows, "2026-08-17")
    assert len(matured) == 2
    assert {m["symbol"] for m in matured} == {"005930", "TQQQ"}
    assert {m["bps"] for m in matured} == {120.0, -80.0}


def test_matured_today_returns_empty_when_nothing_matured():
    rows = [_row("SQQQ", "US", "2026-08-13", outcome_d1_bps=30.0, outcome_d1_asof="2026-08-14")]
    assert matured_today(rows, "2026-08-17") == []


def test_matured_today_ignores_asof_without_bps():
    # 방어적 케이스: asof 만 있고 bps 가 없는 상태는 정상 경로에선 나오지 않지만
    # 걸러야 한다 — 값 없는 행이 순위에 섞이면 안 된다.
    rows = [_row("005930", "KR", "2026-08-10", outcome_d5_asof="2026-08-17")]
    assert matured_today(rows, "2026-08-17") == []


# ── build_close_report: 상위/하위 + 표본 수 + verdict.reason ──────────────

def test_report_shows_top_bottom_and_sample_count():
    matured = [
        {"symbol": "005930", "market": "KR", "horizon": 5, "bps": 120.0},
        {"symbol": "TQQQ", "market": "US", "horizon": 1, "bps": -80.0},
    ]
    text = build_close_report(matured, {}, "")
    assert "오늘 만기 2건" in text
    assert "005930" in text and "+120" in text
    assert "TQQQ" in text and "-80" in text


def test_report_names_zero_maturities_instead_of_silence():
    """만기 0건이면 침묵이 아니라 명시한다 — 조용한 빈 리포트는 "고장"과
    "오늘 정말 아무 일도 없었다"를 구분하지 못한다."""
    text = build_close_report([], {}, "")
    assert "오늘 만기 지평 없음" in text


def test_report_includes_verdict_reason():
    v = _verdict("평균 IC +0.0500, t 2.10 >= 요구 1.96 (거래일 20일, 시행 1회 보정). "
                "비용·체결을 통과하는지는 별도 확인이 필요하다")
    text = build_close_report([], {"watch_scorer/2": v}, "")
    assert v.reason in text


def test_report_says_no_verdicts_when_none_available():
    text = build_close_report([], {}, "")
    assert "판정 없음" in text


def test_report_appends_scoreboard_text_when_given():
    text = build_close_report([], {}, "📊 누적 스코어보드 (종결 40건)")
    assert "📊 누적 스코어보드 (종결 40건)" in text


# ── cmd_close_report(CLI): narrate 가 죽어도 결정론 요약은 이미 나가 있다 ──

@pytest.mark.parametrize("narrator_behavior", ["raises", "returns_none", "returns_text"])
def test_cmd_close_report_flushes_report_before_narrate_runs(
    tmp_path, monkeypatch, capsys, narrator_behavior,
):
    """narrate 를 실제로 호출하는 그 시점에 capsys 가 이미 결정론 요약을 잡고
    있어야 한다 — `cmd_close_report`가 `print(report)` + `flush()` 를 narrate
    호출보다 먼저 실행한다는 배선 순서를 직접 고정한다(2026-08-15 리뷰 결함).

    narrate 가 예외를 던지든(포트 계약 위반이지만 방어적으로) None 을 돌려주든
    (정상 계약) 텍스트를 돌려주든(성공 경로) 셋 다 커버한다 — 성공 경로는
    Task 5 진행 기록의 커버리지 공백이었다(2026-08-15 리뷰 M3+T5).
    """
    ledger_dir = tmp_path / "data" / "ledger"
    ledger_dir.mkdir(parents=True)
    row = {
        "schema": 1, "date": "2026-08-10", "market": "KR", "symbol": "005930",
        "close": 70000, "outcome_d5_bps": 42.0, "outcome_d5_asof": "2026-08-17",
        "outcome_filled": False,
    }
    (ledger_dir / "selections.jsonl").write_text(
        json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8",
    )

    captured: dict[str, str] = {}

    class _FakeNarrator:
        def narrate(self, prompt: str) -> str | None:
            # 이 시점에 이미 report 가 flush 돼 있어야 한다 — readouterr()는
            # 그때까지 stdout 에 쓰인 것만 잡는다.
            captured["out_before_narrate"] = capsys.readouterr().out
            if narrator_behavior == "raises":
                raise RuntimeError("서술기 죽음(테스트)")
            if narrator_behavior == "returns_none":
                return None
            return "오늘도 표본 부족 — 판단 보류가 맞다."

    monkeypatch.setattr("quant.adapters.narrate.make_narrator", lambda: _FakeNarrator())

    from quant.apps.cli import cmd_close_report

    args = argparse.Namespace(root=str(tmp_path), date="2026-08-17", horizon=5, trials=1)
    cmd_close_report(args)  # narrate 가 raise 해도 여기서 죽지 않아야 한다

    before = captured["out_before_narrate"]
    assert "오늘 만기 1건" in before
    assert "005930" in before

    after = capsys.readouterr().out
    if narrator_behavior == "returns_text":
        # narrate 성공 경로 — 이미 나간 요약 뒤에 💬 코멘트가 붙는다. before 에는
        # 아직 없고(narrate 호출 시점 캡처) after 에만 잡혀야 순서가 맞다.
        assert "💬" not in before
        assert "💬 오늘도 표본 부족" in after
    else:
        # narrate 가 죽었거나(예외) 아무것도 안 줬으니(None) 코멘트가 안 붙는다.
        assert "💬" not in after
