"""운영 이상 감지 — Phase 5.3.

이 테스트 파일의 중심 주장은 하나다: **"모른다"는 "이상 없음"이 아니다.**
이 저장소가 반복해서 다친 모양이고(Redis 가 죽으면 빈 목록, coverage 실패면 None,
캐시 실패면 0), 감시가 그 혼동을 하면 감시가 없는 것보다 나쁘다 — 사람이 "초록불"을
보고 안심하기 때문이다.

그래서 각 감지기마다 세 경우를 고정한다: 정상(발견 0건) / 이상(alert) / 모름(unknown).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quant.control.health import (
    ALERT,
    UNKNOWN,
    alert_fingerprint,
    backup_findings,
    bar_findings,
    bar_sanity_findings,
    clock_findings,
    dedupe_repeat_alerts,
    feed_findings,
    Finding,
    flow_anomaly_findings,
    frgn_flow_degenerate_findings,
    install_drift_findings,
    intraday_history_findings,
    job_findings,
    ledger_findings,
    ledger_portfolio_findings,
    llm_health_findings,
    regime_findings,
    report_findings,
    report_intake_findings,
    report_quality_findings,
    secret_findings_for,
    positions_from_trades,
    secret_findings,
    selection_dup_findings,
    summarize,
    telegram_silence_findings,
    timer_findings,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _levels(findings) -> list[str]:
    return [f.level for f in findings]


# ── 작업 성공 여부 ────────────────────────────────────────────────────────

def test_jobs_ok_produces_no_findings():
    snap = {"available": True,
            "jobs": {"collect:KR": {"fresh": True, "ok": True,
                                    "last": "2026-08-13T11:50:00+00:00"}}}
    assert job_findings(snap) == []


def test_jobs_unavailable_is_unknown_not_ok():
    """Redis 가 죽었다. 작업이 성공했는지 **모르는** 것이고 정상이 아니다."""
    findings = job_findings({"available": False, "jobs": {}})

    assert _levels(findings) == [UNKNOWN]
    assert "모른다" in findings[0].detail


def test_job_without_recent_success_is_alert():
    snap = {"available": True,
            "jobs": {"ingest": {"fresh": False, "ok": True, "last": "2026-08-01T00:00:00+00:00"}}}
    findings = job_findings(snap)

    assert _levels(findings) == [ALERT]
    assert "ingest" in findings[0].detail


def test_job_with_no_record_at_all_is_unknown_not_alert():
    """계측이 아직 안 붙은 작업까지 "실패"로 내면 매번 거짓 경보가 온다.

    (2026-08-13 현재 `record_run` 을 부르는 프로덕션 경로가 없다 — 테스트뿐이다.
    "기록이 없다"와 "돌았는데 성공을 못 했다"는 다른 사건이다.)
    """
    snap = {"available": True, "jobs": {"ingest": {"fresh": False, "ok": False, "last": None}}}
    findings = job_findings(snap)

    assert _levels(findings) == [UNKNOWN]
    assert "계측" in findings[0].detail


def test_job_last_run_failed_is_alert():
    snap = {"available": True,
            "jobs": {"report:US": {"fresh": True, "ok": False, "detail": "접속 실패",
                                   "last": "2026-08-13T11:50:00+00:00"}}}
    findings = job_findings(snap)

    assert _levels(findings) == [ALERT]
    assert "접속 실패" in findings[0].detail


# ── 피드 ─────────────────────────────────────────────────────────────────

def test_no_stale_feeds_is_ok():
    assert feed_findings("KR", stale=[], store_healthy=True) == []


def test_empty_stale_list_from_dead_store_is_unknown():
    """**빈 목록이 "죽은 피드 없음"을 뜻하지 않는다.**

    `opstate.stale_feeds()` 는 Redis 가 죽어도 빈 목록을 돌려준다 — 그 계약을
    감시가 오해하면 피드가 통째로 죽은 채 초록불이 켜진다.
    """
    findings = feed_findings("KR", stale=[], store_healthy=False)

    assert _levels(findings) == [UNKNOWN]


def test_stale_feeds_are_alert():
    findings = feed_findings("US", stale=["Bloomberg", "CNN"], store_healthy=True)

    assert _levels(findings) == [ALERT]
    assert "Bloomberg" in findings[0].detail


def test_feed_removed_from_config_is_not_reported_as_dead():
    """**"죽은 피드"와 "설정에서 빠진 피드"는 다른 사건이다.**

    2026-08-13 실측: 감시가 `연합뉴스`·`한경` 을 "오래 성공하지 못한 피드"로 경보했다.
    실제로는 피드 이름이 `연합뉴스_경제`·`한국경제_경제` 로 **개편돼 설정에서 사라진**
    것이었고, 마지막 성공 시각 zset 에는 옛 이름이 남아 있었다. 그대로 두면 영구
    거짓 경보다 — 그리고 거짓 경보가 오는 감시는 꺼진다.
    """
    findings = feed_findings("KR", stale=["연합뉴스", "한경"], store_healthy=True,
                             configured=["연합뉴스_경제", "한국경제_경제"])

    assert findings == []


def test_configured_feed_that_went_stale_is_still_alert():
    """설정에 있는데 오래 실패한 건 그대로 경보다 — 필터가 감지를 삼키면 안 된다."""
    findings = feed_findings("KR", stale=["연합뉴스_경제", "없어진피드"], store_healthy=True,
                             configured=["연합뉴스_경제", "한국경제_경제"])

    assert _levels(findings) == [ALERT]
    assert "연합뉴스_경제" in findings[0].detail
    assert "없어진피드" not in findings[0].detail


def test_without_roster_every_stale_feed_is_reported():
    """설정 목록을 못 구했으면 걸러내지 않는다 — 모르면서 조용해지는 쪽이 더 나쁘다."""
    findings = feed_findings("KR", stale=["연합뉴스"], store_healthy=True, configured=None)

    assert _levels(findings) == [ALERT]


# ── 봉 신선도 ─────────────────────────────────────────────────────────────

def test_fresh_bars_are_ok():
    assert bar_findings({"QQQ 1d": "2026-08-12T04:00:00+00:00"}, NOW, timedelta(days=6)) == []


def test_stale_bars_are_alert_and_say_why_it_matters():
    """사이징에 걸린 파일이라는 걸 경보가 말해야 한다 — 안 그러면 "백테스트 데이터"로
    읽고 미룬다. 실제로 그렇게 미뤄져 13일간 국면이 뒤집혀 있었다."""
    findings = bar_findings({"QQQ 1d": "2026-07-31T04:00:00+00:00"}, NOW, timedelta(days=6))

    assert _levels(findings) == [ALERT]
    assert "regime" in findings[0].detail


def test_unreadable_coverage_is_unknown_not_ok():
    """`coverage()` 는 DuckDB 가 없으면 None 이다 — "봉 0개"가 아니라 "모른다"다."""
    findings = bar_findings({"QQQ 1d": None}, NOW, timedelta(days=6))

    assert _levels(findings) == [UNKNOWN]


# ── 1분봉 적재 신선도 (scalp_1m 표본 축적) ───────────────────────────────

def test_intraday_history_no_directories_yet_is_silent_not_unknown():
    """1m 디렉토리가 하나도 없으면(백필 크론 05:40 첫 가동 전 신규 설치 상태)
    UNKNOWN도 내지 않고 조용히 빈 목록 — 그때마다 경보가 오면 감시가 꺼진다."""
    assert intraday_history_findings({}, NOW) == []


def test_intraday_history_stale_bar_is_alert():
    findings = intraday_history_findings(
        {"TQQQ": (NOW - timedelta(days=3)).isoformat()}, NOW, timedelta(days=2))

    assert _levels(findings) == [ALERT]
    assert "TQQQ" in findings[0].detail
    assert "scalp_1m" in findings[0].detail


def test_intraday_history_unreadable_symbol_is_unknown_not_ok():
    """`coverage()` 실패로 값이 `None`이면 "적재 없음"이 아니라 "모른다"다 —
    디렉토리가 존재해 최소 한 번은 적재됐다는 것 자체는 이미 알고 있으므로."""
    findings = intraday_history_findings({"TQQQ": None}, NOW)

    assert _levels(findings) == [UNKNOWN]
    assert "TQQQ" in findings[0].detail


# ── 시계 ─────────────────────────────────────────────────────────────────

def test_kst_host_with_fresh_heartbeat_is_ok():
    stamp = (NOW - timedelta(seconds=30)).isoformat()
    assert clock_findings(9 * 3600, stamp, NOW) == []


def test_non_kst_timezone_is_alert():
    """편입 데드라인·세션 경계가 전부 KST 전제다."""
    stamp = (NOW - timedelta(seconds=30)).isoformat()
    findings = clock_findings(0, stamp, NOW)

    assert ALERT in _levels(findings)
    assert "KST" in findings[0].detail


def test_engine_stamp_far_from_observer_is_alert():
    findings = clock_findings(9 * 3600, (NOW - timedelta(hours=3)).isoformat(), NOW)

    assert _levels(findings) == [ALERT]


def test_missing_heartbeat_is_unknown():
    assert _levels(clock_findings(9 * 3600, None, NOW)) == [UNKNOWN]


# ── 원장 ↔ 포트폴리오 ────────────────────────────────────────────────────

def test_positions_are_reconstructed_from_signed_sides():
    trades = [
        {"symbol": "TQQQ", "side": "buy", "qty": 10},
        {"symbol": "TQQQ", "side": "sell", "qty": 4},
        {"symbol": "SQQQ", "side": "buy", "qty": 3},
    ]
    assert positions_from_trades(trades) == {"TQQQ": 6.0, "SQQQ": 3.0}


def test_matching_ledger_and_portfolio_is_ok():
    trades = [{"symbol": "TQQQ", "side": "buy", "qty": 10},
              {"symbol": "TQQQ", "side": "sell", "qty": 10}]
    portfolio = {"positions": {"TQQQ": {"qty": 0.0}}}

    assert ledger_portfolio_findings(trades, portfolio) == []


def test_partial_fill_style_mismatch_is_alert():
    """실계좌에서 20주 중 8주만 채워지면 원장은 20주로 안다 — paper 는 즉시 체결이라
    이 불일치가 안 보인다. Phase 6(OMS)가 근본 해결이고 여기선 감지만 한다."""
    trades = [{"symbol": "TQQQ", "side": "buy", "qty": 20}]
    portfolio = {"positions": {"TQQQ": {"qty": 8.0}}}

    findings = ledger_portfolio_findings(trades, portfolio)

    assert _levels(findings) == [ALERT]
    assert "20" in findings[0].detail and "8" in findings[0].detail


def test_unreadable_portfolio_is_unknown():
    assert _levels(ledger_portfolio_findings([], None)) == [UNKNOWN]


# ── 실계좌 이식 경계(2026-09-04) ────────────────────────────────────────────
# cmd_seed_real이 남기는 "실계좌 이식 정리" 매도 이후로는 포트폴리오 전체가
# 실계좌 스냅샷으로 갈아끼워진다 — 그 이전 원장 잔량을 계속 반영하면 이미
# 이관 정리로 청산된 종목이 "원장 재구성 N vs 포트폴리오 0"으로 영원히
# 오경보된다(실측: 452회 누적, 하루 10회). round_trips와 같은 경계를 쓴다.

_SEED_REASON = "실계좌 이식 정리 — 소유자 지시 2026-09-01: 005930만 보유 유지, 나머지 정리"


def test_positions_from_trades_ignores_trades_at_or_before_boundary():
    boundary = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    trades = [
        {"symbol": "000500", "side": "buy", "qty": 10, "ts": "2026-08-20T01:00:00+00:00"},
        {"symbol": "000500", "side": "sell", "qty": 7, "ts": "2026-09-01T03:00:00+00:00"},
    ]
    assert positions_from_trades(trades, boundary_ts=boundary) == {}


def test_positions_from_trades_counts_only_after_boundary():
    boundary = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
    trades = [
        {"symbol": "005930", "side": "buy", "qty": 100, "ts": "2026-08-01T01:00:00+00:00"},
        {"symbol": "005930", "side": "buy", "qty": 5, "ts": "2026-09-02T01:00:00+00:00"},
    ]
    assert positions_from_trades(trades, boundary_ts=boundary) == {"005930": 5.0}


def test_ledger_portfolio_findings_uses_seeding_boundary_for_pre_transplant_symbols():
    """이식 이전 paper 매매 잔량이 이관 정리 매도와 이중으로 잡혀 "원장 재구성
    N vs 포트폴리오 0"을 오경보하던 결함 — 경계 이후 거래가 없는 종목은 더
    이상 알리지 않는다."""
    trades = [
        {"symbol": "000500", "side": "buy", "qty": 10, "ts": "2026-08-20T01:00:00+00:00"},
        {"symbol": "000500", "side": "sell", "qty": 7, "ts": "2026-09-01T03:00:00+00:00",
         "reason": _SEED_REASON},
    ]
    portfolio = {"positions": {}}  # 이관 정리 후 실제 보유 없음

    assert ledger_portfolio_findings(trades, portfolio) == []


def test_ledger_portfolio_findings_still_alerts_on_genuine_post_boundary_mismatch():
    """경계 자체를 고쳐도 **경계 이후의 진짜 불일치**는 그대로 잡혀야 한다."""
    trades = [
        {"symbol": "000500", "side": "sell", "qty": 7, "ts": "2026-09-01T03:00:00+00:00",
         "reason": _SEED_REASON},
        {"symbol": "005930", "side": "buy", "qty": 3, "ts": "2026-09-02T01:00:00+00:00"},
    ]
    portfolio = {"positions": {"005930": {"qty": 0.0}}}  # 실제론 매도됐는데 원장은 3주

    findings = ledger_portfolio_findings(trades, portfolio)

    assert _levels(findings) == [ALERT]
    assert "005930" in findings[0].detail


# ── 캐리오버(이월 보유) 원장 행(2026-09-05 D3) ───────────────────────────────
# `cmd_seed_real`이 유지 종목(005930)의 이월 수량에 원장 행을 남기지 않아
# 그 종목만 영구히 "원장 재구성 -N vs 포트폴리오 0"으로 오탐했다(005930 실측).

_CARRY_REASON = "실계좌 이식 이월 — 소유자 지시 2026-09-01: 005930 이월 보유"


def test_positions_from_trades_includes_carry_row_even_at_boundary():
    """캐리오버 합성 buy는 그 정의상 경계 시각 그 자체(또는 그 이전)에 찍힌다 —
    일반 규칙(ts<=boundary면 제외)을 그대로 적용하면 이 행마저 걸러져 시작
    잔량이 0으로 재구성된다. 캐리 행은 시각 필터를 건너뛰어야 한다."""
    boundary = datetime(2026, 9, 1, 14, 1, 8, tzinfo=timezone.utc)
    trades = [
        {"symbol": "005930", "side": "buy", "qty": 6, "ts": boundary.isoformat(),
         "reason": _CARRY_REASON, "strategy_id": "seed"},
    ]
    assert positions_from_trades(trades, boundary_ts=boundary) == {"005930": 6.0}


def test_ledger_portfolio_findings_clears_005930_once_carry_row_present():
    """실측 시나리오(D3): 정리매도 7건 + 005930 이월 buy가 전부 같은 순간(이관
    시점)에 원장에 남는다 — 캐리오버 행이 경계와 같은 시각이어도 재구성에
    포함돼야 그 뒤 매도가 실제 보유수량에서 이어진다."""
    boundary_ts = "2026-09-01T14:01:08+00:00"
    trades = [
        {"symbol": "000500", "side": "sell", "qty": 7, "ts": boundary_ts,
         "reason": _SEED_REASON},
        {"symbol": "005930", "side": "buy", "qty": 6, "ts": boundary_ts,
         "reason": _CARRY_REASON, "strategy_id": "seed"},
        {"symbol": "005930", "side": "sell", "qty": 1, "ts": "2026-09-02T01:00:00+00:00"},
    ]
    portfolio = {"positions": {"005930": {"qty": 5.0}}}

    assert ledger_portfolio_findings(trades, portfolio) == []


def test_ledger_portfolio_findings_still_alerts_if_carry_row_missing():
    """캐리오버 행이 없으면(D3 결함 그대로 재현) 여전히 오탐이 나야 한다 —
    이 테스트가 회귀를 잡는다(캐리 행 도입 자체가 감지를 무디게 만들면 안 된다)."""
    boundary_ts = "2026-09-01T14:01:08+00:00"
    trades = [
        {"symbol": "000500", "side": "sell", "qty": 7, "ts": boundary_ts,
         "reason": _SEED_REASON},
        {"symbol": "005930", "side": "sell", "qty": 1, "ts": "2026-09-02T01:00:00+00:00"},
    ]
    portfolio = {"positions": {"005930": {"qty": 5.0}}}

    findings = ledger_portfolio_findings(trades, portfolio)
    assert _levels(findings) == [ALERT]
    assert "005930" in findings[0].detail


# ── 반복 알림 억제(2026-09-04) ──────────────────────────────────────────────

def test_dedupe_repeat_alerts_first_occurrence_passes():
    f = Finding("ledger", ALERT, "005930: 원장 재구성 3 vs 포트폴리오 0")

    to_notify, suppressed, updated = dedupe_repeat_alerts([f], {}, NOW)

    assert to_notify == [f]
    assert suppressed == []
    assert updated[alert_fingerprint(f)] == NOW.isoformat()


def test_dedupe_repeat_alerts_suppresses_within_24h():
    f = Finding("ledger", ALERT, "005930: 원장 재구성 3 vs 포트폴리오 0")
    last_alerted = {alert_fingerprint(f): (NOW - timedelta(hours=1)).isoformat()}

    to_notify, suppressed, updated = dedupe_repeat_alerts([f], last_alerted, NOW)

    assert to_notify == []
    assert suppressed == [f]
    assert updated == last_alerted  # 억제된 항목은 재알림 시계를 리셋하지 않는다


def test_dedupe_repeat_alerts_passes_again_after_24h():
    f = Finding("ledger", ALERT, "005930: 원장 재구성 3 vs 포트폴리오 0")
    fp = alert_fingerprint(f)
    last_alerted = {fp: (NOW - timedelta(hours=25)).isoformat()}

    to_notify, suppressed, updated = dedupe_repeat_alerts([f], last_alerted, NOW)

    assert to_notify == [f]
    assert suppressed == []
    assert updated[fp] == NOW.isoformat()


def test_dedupe_repeat_alerts_new_finding_passes_even_if_sibling_suppressed():
    stale = Finding("ledger", ALERT, "005930: 원장 재구성 3 vs 포트폴리오 0")
    fresh = Finding("bars", ALERT, "QQQ 1d: 마지막 봉이 5일 낡았다")
    last_alerted = {alert_fingerprint(stale): (NOW - timedelta(hours=1)).isoformat()}

    to_notify, suppressed, _ = dedupe_repeat_alerts([stale, fresh], last_alerted, NOW)

    assert to_notify == [fresh]
    assert suppressed == [stale]


# ── 시크릿 ───────────────────────────────────────────────────────────────

def test_clean_log_lines_produce_no_findings():
    assert secret_findings(["2026-08-13 INFO 사이클 완료", "INFO 주문 접수"]) == []


def test_leaked_telegram_token_is_alert_and_never_echoed():
    """실제 유출 사례 형태(2026-08-13: 이틀간 381번 평문).

    **경보가 값을 그대로 실으면 경보 자체가 유출 경로가 된다.**
    """
    token = "1234567890:AAfakeShapeOnlyTokenForRedactTest000000"
    line = f"HTTP Request: POST https://api.telegram.org/bot{token}/sendMessage"

    findings = secret_findings([line])

    assert _levels(findings) == [ALERT]
    assert token not in findings[0].detail
    assert "381" not in findings[0].detail  # 개수는 실제 관측치여야 한다
    assert "1줄" in findings[0].detail


# ── 타이머 ───────────────────────────────────────────────────────────────

def test_all_expected_timers_present_is_ok():
    units = {"news-collect-kr.timer": "2026-08-13T09:00:00+00:00"}
    assert timer_findings(units, ["news-collect-kr.timer"]) == []


def test_missing_timer_unit_is_alert():
    findings = timer_findings({}, ["warehouse-ingest.timer"])

    assert _levels(findings) == [ALERT]
    assert "warehouse-ingest.timer" in findings[0].detail


# ── 설치본 드리프트 ──────────────────────────────────────────────────────

def test_identical_crontab_ignoring_comments_is_ok():
    """주석·빈 줄·순서 차이로 경보를 내면 사람이 경보를 끈다."""
    installed = "# 주석\n\n0 7 * * * run report\n30 8 * * 1-5 run watch\n"
    repo = "30 8 * * 1-5 run   watch\n# 다른 주석\n0 7 * * * run report\n"

    assert install_drift_findings(installed, repo) == []


def test_stale_installed_crontab_is_alert():
    """2026-08-13 실측: 설치본이 없어진 모듈(`quant_engine.run`)을 부르고 있었다.
    저장소는 옳고 설치본만 낡는 부류는 테스트도 배포도 안 잡는다."""
    installed = "0 7 * * * python -m quant_engine.run report\n"
    repo = "0 7 * * * python -m quant.apps.cli report\n"

    findings = install_drift_findings(installed, repo)

    assert _levels(findings) == [ALERT]
    assert "quant" in findings[0].detail


def test_unreadable_installed_config_is_unknown():
    assert _levels(install_drift_findings(None, "x")) == [UNKNOWN]


# ── 원장 신선도 (H-2: DART·텔레그램·외국인 수급·선정) ────────────────────

def test_fresh_ledger_is_ok():
    last = {"frgn_flow": (NOW - timedelta(hours=6)).isoformat()}
    assert ledger_findings(last, NOW, {"frgn_flow": timedelta(days=4)}) == []


def test_stale_ledger_is_alert_and_says_why_it_matters():
    """원장이 마른 사실만이 아니라 **왜 신경써야 하나**를 경보에 같이 낸다 —
    안 그러면 사람이 "장중이라 그런가"로 미룬다(2026-08-14 리포트 결측 사고)."""
    last = {"frgn_flow": (NOW - timedelta(days=10)).isoformat()}
    findings = ledger_findings(last, NOW, {"frgn_flow": timedelta(days=4)})

    assert _levels(findings) == [ALERT]
    assert "10일" in findings[0].detail and "4일" in findings[0].detail
    assert "외국인" in findings[0].detail  # 왜 중요한지 한 줄


def test_missing_ledger_is_unknown_not_ok():
    """원장을 못 읽었다 = "안 쌓인다"가 아니라 "쌓이는지 모른다"다."""
    findings = ledger_findings({"selections": None}, NOW, {"selections": timedelta(days=4)})

    assert _levels(findings) == [UNKNOWN]
    assert "모른다" in findings[0].detail


def test_date_only_timestamp_is_parsed_as_midnight():
    """`frgn_flow`/`selections` 는 시각 없이 날짜만 쌓는다(`"2026-08-12"`) — 자정
    UTC로 해석돼도 임계가 일 단위라 판정이 흔들리지 않는다."""
    last = {"selections": "2026-08-12"}
    assert ledger_findings(last, NOW, {"selections": timedelta(days=4)}) == []

    stale = {"selections": "2026-08-01"}
    findings = ledger_findings(stale, NOW, {"selections": timedelta(days=4)})
    assert _levels(findings) == [ALERT]


# ── 백업 ─────────────────────────────────────────────────────────────────

def test_recent_bundle_and_recent_pull_is_ok():
    assert backup_findings((NOW - timedelta(hours=9)).isoformat(),
                           (NOW - timedelta(days=1)).isoformat(), NOW) == []


def test_old_bundle_is_alert():
    findings = backup_findings((NOW - timedelta(days=5)).isoformat(),
                              (NOW - timedelta(days=1)).isoformat(), NOW)
    assert _levels(findings) == [ALERT]


def test_bundle_fresh_but_never_pulled_offsite_is_flagged():
    """번들만 최신이면 아직 **EC2 디스크 한 곳**이다 — 그게 원래 위험이었다."""
    findings = backup_findings((NOW - timedelta(hours=2)).isoformat(), None, NOW)

    assert _levels(findings) == [UNKNOWN]
    assert "한 곳" in findings[0].detail


def test_stale_offsite_pull_is_alert():
    findings = backup_findings((NOW - timedelta(hours=2)).isoformat(),
                              (NOW - timedelta(days=30)).isoformat(), NOW)
    assert _levels(findings) == [ALERT]


# ── 합산 ─────────────────────────────────────────────────────────────────

def test_summarize_does_not_fold_unknown_into_ok():
    """**이 테스트가 이 모듈의 존재 이유다.**"""
    findings = job_findings({"available": False, "jobs": {}})

    summary = summarize(findings)

    assert summary["verdict"] == UNKNOWN
    assert summary["verdict"] != "ok"
    assert summary["n_unknown"] == 1


def test_summarize_reports_ok_only_when_nothing_was_found():
    assert summarize([])["verdict"] == "ok"


def test_alert_outranks_unknown():
    """이상과 모름이 섞이면 이상이 이긴다 — 사람이 먼저 봐야 할 쪽이다."""
    findings = (job_findings({"available": False, "jobs": {}})
                + feed_findings("KR", ["CNN"], store_healthy=True))

    assert summarize(findings)["verdict"] == ALERT


# ============================ 리포트 결측 · 시크릿 도달 (2026-08-14)
#
# **왜 이걸 감시에 넣나.** 리포트가 API 키를 못 읽어 소스 5개가 결측인 채로 며칠
# 발행됐다. 리포트는 그 사실을 `engine.json` 의 `missing` 에 **이미 기록하고 있었는데
# 아무도 읽지 않았다.** 사람이 빌드 출력의 "결측 5건"을 보고도 "장중이라 그런가"로
# 미뤘다 — 그게 정확히 이 저장소가 반복해서 다친 "모른다를 정상으로 읽기"다.
#
# 그리고 원인은 더 고약했다: 검증 도구(`scripts/check_keys.py`)는 자기 로더로 파일을
# 읽어 "필수 키 확인 완료"를 냈고, 앱은 `DEFAULT_ENV` 가 엉뚱한 경로라 결측이었다.
# **검증과 애플리케이션이 다른 코드 경로로 같은 파일을 읽으면 둘 다 "정상"일 수 있다.**

def test_report_with_no_missing_sources_is_ok():
    assert report_findings({"KR": []}, required=["macro", "toss_rankings"]) == []


def test_missing_required_source_is_alert_not_ignored():
    """리포트가 스스로 기록한 결측을 감시가 읽는다."""
    findings = report_findings({"KR": ["macro", "toss_rankings"]},
                              required=["macro", "toss_rankings"])

    assert _levels(findings) == [ALERT]
    assert "macro" in findings[0].detail


def test_missing_optional_source_is_not_an_alert():
    """장중·휴장 등으로 정상적으로 빠지는 소스가 있다(after_hours). 그걸 alert 로
    내면 거짓 경보가 되고, 거짓 경보가 오는 감시는 꺼진다."""
    assert report_findings({"KR": ["after_hours"]}, required=["macro"]) == []


def test_unreadable_report_is_unknown_not_ok():
    """오늘 리포트를 못 읽었다 = 발행 실패일 수 있다. **"결측 없음"이 아니다.**"""
    findings = report_findings({"KR": None}, required=["macro"])

    assert _levels(findings) == [UNKNOWN]


def test_secret_readable_through_the_app_code_path():
    """**앱이 실제로 쓰는 경로로** 읽히는지 본다 — 파일에 있는지가 아니다.

    2026-08-14: 파일은 루트에 있었고 `check_keys.py` 는 "완료"라 했지만, 앱의
    `DEFAULT_ENV` 는 `quant/.env.local` 을 봐서 전부 결측이었다.
    """
    assert secret_findings_for({"FRED_API_KEY": "x"}, required=["FRED_API_KEY"]) == []


def test_missing_required_secret_is_alert_and_value_never_echoed():
    findings = secret_findings_for({"FRED_API_KEY": ""}, required=["FRED_API_KEY", "TOSS_CLIENT_ID"])

    assert _levels(findings) == [ALERT]
    assert "FRED_API_KEY" in findings[0].detail
    assert "TOSS_CLIENT_ID" in findings[0].detail


def test_unreadable_env_is_unknown():
    """env 를 아예 못 읽었으면 "키 없음"이 아니라 "모른다"다."""
    assert _levels(secret_findings_for(None, required=["FRED_API_KEY"])) == [UNKNOWN]


# ── 봉 값 자체의 타당성 ───────────────────────────────────────────────────
#
# 유래: yfinance KR 1d 가 tz 변환으로 하루 어긋난 날짜를 저장한 실측 사고
# (e87efa4) — regime 이 이 파일로 사이징 배수를 계산하다 13일간 잘못 사이징했다.
# "봉이 최신이다"(bar_findings)만으로는 안 잡히고, 값 자체를 봐야 한다.

def _bar(ts: str, close: float, high: float | None = None, low: float | None = None) -> dict:
    return {"ts": ts, "open": close, "high": high if high is not None else close,
            "low": low if low is not None else close, "close": close}


def test_normal_bars_are_ok():
    bars = {"QQQ 1d": [_bar("2026-08-11T13:00:00+00:00", 700), _bar("2026-08-12T13:00:00+00:00", 703)]}

    assert bar_sanity_findings(bars, NOW) == []


def test_contaminated_close_is_alert():
    """close<=0 또는 high<low 는 데이터 오염이다."""
    bars = {"QQQ 1d": [_bar("2026-08-11T13:00:00+00:00", 700),
                       _bar("2026-08-12T13:00:00+00:00", 1, high=1, low=2)]}
    findings = bar_sanity_findings(bars, NOW)

    assert _levels(findings) == [ALERT]
    assert "오염" in findings[0].detail


def test_future_bar_date_is_alert():
    """마지막 봉 날짜가 관측 시각보다 미래다 — tz 재해석 사고 재발 신호."""
    bars = {"QQQ 1d": [_bar("2026-08-11T13:00:00+00:00", 700),
                       _bar("2026-08-14T00:00:00+00:00", 703)]}
    findings = bar_sanity_findings(bars, NOW)

    assert _levels(findings) == [ALERT]
    assert "미래" in findings[0].detail


def test_return_spike_over_threshold_is_alert():
    """지수 ETF 앵커가 하루 25%보다 크게 움직이면 단위 오류/스플릿 미조정 의심."""
    bars = {"QQQ 1d": [_bar("2026-08-11T13:00:00+00:00", 100),
                       _bar("2026-08-12T13:00:00+00:00", 140)]}
    findings = bar_sanity_findings(bars, NOW)

    assert _levels(findings) == [ALERT]


def test_unreadable_bars_is_unknown_not_ok():
    """봉을 못 읽으면 UNKNOWN — "봉이 0개"와 다른 사건이다."""
    findings = bar_sanity_findings({"QQQ 1d": None, "069500 1d": []}, NOW)

    assert _levels(findings) == [UNKNOWN, UNKNOWN]


# ── 뉴스 유량 이상 ────────────────────────────────────────────────────────
#
# 유래: 수집기가 조용히 죽거나(0건) dedup 이 깨져 폭증하는(중복 미제거) 두
# 방향 모두 이 저장소 전례가 있다.

def test_normal_flow_is_ok():
    trailing = [25, 30, 22, 28, 26, 24, 27]

    assert flow_anomaly_findings(25, trailing, "KR", zero_check_active=True) == []


def test_zero_articles_after_median_high_and_afternoon_is_alert():
    trailing = [25, 25, 25, 25, 25, 25, 25]
    findings = flow_anomaly_findings(0, trailing, "KR", zero_check_active=True)

    assert _levels(findings) == [ALERT]
    assert "0건" in findings[0].detail


def test_zero_articles_before_afternoon_is_not_alert():
    """오전엔 아직 쌓이는 중이라 0건이 정상이다 — 호출부가 시각 게이트를 준다."""
    trailing = [25, 25, 25, 25, 25, 25, 25]

    assert flow_anomaly_findings(0, trailing, "KR", zero_check_active=False) == []


def test_surge_over_8x_median_is_alert_regardless_of_time():
    """폭증 검사는 상시다 — dedup 은 하루 중 언제든 깨질 수 있다."""
    trailing = [10, 10, 10, 10, 10, 10, 10]
    findings = flow_anomaly_findings(90, trailing, "US", zero_check_active=False)

    assert _levels(findings) == [ALERT]
    assert "dedup" in findings[0].detail


def test_fewer_than_three_trailing_days_is_empty_not_unknown():
    """신규 설치 직후 매시간 unknown 이 오면 사람이 감시를 끈다."""
    assert flow_anomaly_findings(0, [25, 25], "KR", zero_check_active=True) == []


def test_unreadable_today_count_is_unknown():
    trailing = [25, 25, 25, 25, 25, 25, 25]
    findings = flow_anomaly_findings(None, trailing, "KR", zero_check_active=True)

    assert _levels(findings) == [UNKNOWN]


# ── 텔레그램 전채널 동시 침묵 ─────────────────────────────────────────────
#
# 유래: t.me/s 웹 프리뷰는 채널 단위로 차단될 수 있다(12방 중 1방은 이미 차단
# 실측, 2026-08-17). 개별 방 침묵은 정상, 전 채널 동시 침묵만 신호다.

def test_one_fresh_channel_among_stale_is_ok():
    newest = {
        "a": "2026-08-10T00:00:00+00:00",  # 84시간 전 — 낡음
        "b": "2026-08-09T00:00:00+00:00",  # 낡음
        "c": "2026-08-13T06:00:00+00:00",  # 6시간 전 — 신선
    }

    assert telegram_silence_findings(newest, NOW) == []


def test_all_channels_silent_over_threshold_is_alert():
    newest = {
        "a": "2026-08-10T00:00:00+00:00",
        "b": "2026-08-09T00:00:00+00:00",
        "c": "2026-08-08T00:00:00+00:00",
    }
    findings = telegram_silence_findings(newest, NOW)

    assert _levels(findings) == [ALERT]
    assert "3개" in findings[0].detail


def test_fewer_than_three_observed_channels_is_empty():
    newest = {"a": "2026-08-10T00:00:00+00:00", "b": "2026-08-09T00:00:00+00:00"}

    assert telegram_silence_findings(newest, NOW) == []


def test_unreadable_telegram_ledger_is_unknown():
    findings = telegram_silence_findings(None, NOW)

    assert _levels(findings) == [UNKNOWN]


# ── 외국인 수급 원장 퇴화 ────────────────────────────────────────────────
#
# 유래: 상류 파싱 필드가 개편되면 0으로 조용히 채워지는 유형(연합뉴스 피드
# 개명 사고와 같은 모양). 전부 0이면 단타 점수의 외국인 축이 조용히 무의미해진다.

def test_normal_frgn_flow_is_ok():
    rows = [
        {"date": "2026-08-11", "symbol": "005930", "foreign_net": 1000},
        {"date": "2026-08-12", "symbol": "005930", "foreign_net": -500},
        {"date": "2026-08-13", "symbol": "005930", "foreign_net": 0},  # 종목 하나는 0일 수 있다
    ]

    assert frgn_flow_degenerate_findings(rows) == []


def test_all_zero_across_three_plus_days_is_alert():
    """시장 전체가 여러 날 연속 전 종목 순매수 0일 수는 없다."""
    rows = [
        {"date": "2026-08-11", "symbol": "005930", "foreign_net": 0},
        {"date": "2026-08-12", "symbol": "005930", "foreign_net": 0},
        {"date": "2026-08-13", "symbol": "005930", "foreign_net": 0},
    ]
    findings = frgn_flow_degenerate_findings(rows)

    assert _levels(findings) == [ALERT]
    assert "foreign_net=0" in findings[0].detail


def test_empty_frgn_flow_rows_is_empty_not_unknown():
    """신선도는 ledger_findings 가 이미 본다 — 이중 경보 금지."""
    assert frgn_flow_degenerate_findings([]) == []


def test_all_zero_but_fewer_than_three_days_is_empty():
    rows = [
        {"date": "2026-08-12", "symbol": "005930", "foreign_net": 0},
        {"date": "2026-08-13", "symbol": "005930", "foreign_net": 0},
    ]

    assert frgn_flow_degenerate_findings(rows) == []


# ── 선정 원장 중복 자연키 ────────────────────────────────────────────────
#
# 유래: producer_version 규율(같은 (date,market,symbol,producer)은 한 번) —
# dedup 이 깨지면 검증 하네스 표본이 오염돼 n≥30 승격 판정이 무의미해진다.

def test_no_duplicate_keys_is_ok():
    rows = [
        {"date": "2026-08-13", "market": "KR", "symbol": "005930"},
        {"date": "2026-08-13", "market": "KR", "symbol": "000660"},
    ]

    assert selection_dup_findings(rows) == []


def test_duplicate_natural_key_is_alert():
    rows = [
        {"date": "2026-08-13", "market": "KR", "symbol": "005930"},
        {"date": "2026-08-13", "market": "KR", "symbol": "005930"},
    ]
    findings = selection_dup_findings(rows)

    assert _levels(findings) == [ALERT]
    assert "005930" in findings[0].detail


def test_same_symbol_different_producer_is_not_a_duplicate():
    """producer 가 자연키에 포함된다 — 서로 다른 producer 는 독립 표본이다."""
    rows = [
        {"date": "2026-08-13", "market": "KR", "symbol": "005930", "producer": "report"},
        {"date": "2026-08-13", "market": "KR", "symbol": "005930", "producer": "intraday_scorer"},
    ]

    assert selection_dup_findings(rows) == []


def test_empty_selections_rows_is_empty():
    assert selection_dup_findings([]) == []


# ── 발행↔편입 정합 ────────────────────────────────────────────────────────
#
# 유래: 2026-08-14~17 나흘간 own_brief 의 리포트 경로 기본값이 옛 체크아웃을
# 가리켜 매일 rc=3(리포트 없음) → 랭킹 폴백만 돌았다. 리포트는 정상 발행됐고
# 편입도 "성공"했으므로 기존 감시는 전부 정상이었다 — 아무도 몰랐다.

def test_report_tags_present_alongside_trend_is_ok():
    findings = report_intake_findings(
        {"KR": True}, {"KR": ["EVENT", "TREND"]})

    assert findings == []


def test_trend_only_intake_despite_report_existing_is_alert():
    """리포트는 발행됐는데(engine.json 있음) 오늘 편입이 전부 랭킹 유래뿐이다
    — 리포트 읽기 실패의 모양과 같다."""
    findings = report_intake_findings(
        {"KR": True}, {"KR": ["TREND"]})

    assert _levels(findings) == [ALERT]
    assert "TREND" in findings[0].detail


def test_event_scalp_or_frgn_alone_also_counts_as_report_origin():
    for tag in ("EVENT_SCALP", "FRGN"):
        findings = report_intake_findings({"US": True}, {"US": [tag, "TREND"]})
        assert findings == [], f"{tag} 는 리포트 유래 태그다"


def test_no_intake_at_all_today_is_not_alert():
    """오늘 신규 편입 자체가 없으면 판단 대상이 아니다 — 그건 후보 0건 축
    (report_quality_findings)이 본다."""
    findings = report_intake_findings({"KR": True}, {"KR": []})

    assert findings == []


def test_report_missing_today_skips_the_check_entirely():
    """주말·휴장이라 오늘 engine.json 자체가 없으면 검사를 건너뛴다 — 매번
    울리면 거짓 경보가 되고, 거짓 경보가 오는 감시는 꺼진다."""
    findings = report_intake_findings({"KR": False}, {"KR": None})

    assert findings == []


def test_unreadable_intake_tags_is_unknown():
    findings = report_intake_findings({"US": True}, {"US": None})

    assert _levels(findings) == [UNKNOWN]


# ── 리포트 품질 회귀 ──────────────────────────────────────────────────────
#
# 유래: 2026-08-14 결측 사고도 engine.json 의 missing 에 이미 기록돼 있었는데
# 읽는 사람이 없었다 — 이 축은 후보 수·AI 해석 상태가 조용히 나빠지는 걸 본다.

def _summary(candidates=10, midterm=5, agent_interpret="ok", missing=0) -> dict:
    return {"candidates": candidates, "midterm": midterm,
            "agent_interpret": agent_interpret, "missing": missing}


def test_steady_candidates_and_ok_status_is_fine():
    trailing = [_summary(candidates=c) for c in (10, 12, 9)]

    assert report_quality_findings("KR", _summary(candidates=11), trailing) == []


def test_candidates_crash_to_zero_from_healthy_median_is_alert():
    trailing = [_summary(candidates=c) for c in (10, 12, 9)]
    findings = report_quality_findings("KR", _summary(candidates=0), trailing)

    assert _levels(findings) == [ALERT]
    assert "전체 후보" in findings[0].detail


def test_midterm_crash_to_zero_is_also_alert():
    trailing = [_summary(midterm=m) for m in (5, 6, 4)]
    findings = report_quality_findings("US", _summary(midterm=0), trailing)

    assert _levels(findings) == [ALERT]
    assert "중기" in findings[0].detail


def test_agent_interpret_failed_is_alert():
    trailing = [_summary() for _ in range(3)]
    findings = report_quality_findings("KR", _summary(agent_interpret="failed"), trailing)

    assert _levels(findings) == [ALERT]
    assert "failed" in findings[0].detail


def test_agent_interpret_failed_midterm_fallback_is_also_alert():
    trailing = [_summary() for _ in range(3)]
    findings = report_quality_findings(
        "US", _summary(agent_interpret="failed_midterm_fallback"), trailing)

    assert _levels(findings) == [ALERT]


def test_agent_interpret_skipped_no_key_is_not_alert():
    """무료 API 키 미설정은 정상 상태다 — alert 로 내면 거짓 경보가 된다."""
    trailing = [_summary() for _ in range(3)]

    assert report_quality_findings(
        "KR", _summary(agent_interpret="skipped_no_key"), trailing) == []


def test_agent_interpret_skipped_no_candidates_is_not_alert():
    trailing = [_summary() for _ in range(3)]

    assert report_quality_findings(
        "KR", _summary(agent_interpret="skipped_no_candidates"), trailing) == []


def test_fewer_than_three_trailing_days_is_empty_not_unknown_for_report_quality():
    """신규 설치 직후 소음 방지 — flow_anomaly_findings 와 같은 규율.
    표본 부족이면 today 가 아무리 나빠도(0건 + failed) 빈 목록이다."""
    trailing = [_summary(candidates=10), _summary(candidates=10)]

    assert report_quality_findings(
        "KR", _summary(candidates=0, agent_interpret="failed"), trailing) == []


def test_unreadable_today_with_enough_trailing_history_is_unknown():
    trailing = [_summary() for _ in range(3)]

    findings = report_quality_findings("KR", None, trailing)

    assert _levels(findings) == [UNKNOWN]


def test_candidates_already_near_zero_median_does_not_alert_on_zero():
    """중앙값 자체가 0 근처인 시장(median < 1)에서는 오늘 0건이 급감이 아니다."""
    trailing = [_summary(candidates=c) for c in (0, 0, 1)]

    assert report_quality_findings("KR", _summary(candidates=0), trailing) == []


# ── LLM 호출 계측 ─────────────────────────────────────────────────────────
#
# 유래: 무료 레인 요청 수·실패율·지연을 아무도 기록하지 않아 한도에 걸리기
# 시작해도 몰랐다.

def test_no_calls_is_quiet_not_alert():
    """narrate 는 상시 도는 경로가 아니다 — 조용한 날마다 경보가 오면 꺼진다."""
    findings = llm_health_findings({"narrate": {"total": 0, "failed": 0}})

    assert findings == []


def test_healthy_failure_rate_under_threshold_is_ok():
    findings = llm_health_findings({"narrate": {"total": 10, "failed": 4}})  # 40%

    assert findings == []


def test_failure_rate_over_50_percent_is_alert():
    findings = llm_health_findings({"tool": {"total": 10, "failed": 6}})  # 60%

    assert _levels(findings) == [ALERT]
    assert "tool" in findings[0].detail


def test_unreadable_lane_stats_is_unknown():
    findings = llm_health_findings({"narrate": None})

    assert _levels(findings) == [UNKNOWN]


def test_multiple_lanes_are_judged_independently():
    findings = llm_health_findings({
        "narrate": {"total": 10, "failed": 1},
        "tool": {"total": 10, "failed": 9},
    })

    assert _levels(findings) == [ALERT]
    assert "tool" in findings[0].detail


# ── 국면(regime) 강등 지속 ───────────────────────────────────────────────
#
# 유래: 2026-08-18~19, US 국면이 지표 5개 중 2개만 유효한 채로 하루 종일
# aggressive(1.5x)를 유지했다. provider가 이미 degraded 플래그로 알고 있던
# 사실을 보는 사람이 없었다 — 감시가 이 신호를 읽어야 한다.

def test_no_snapshot_is_unknown_not_ok():
    """regime.json이 없거나 파싱 실패 — "강등 없음"이 아니라 모르는 것이다."""
    assert _levels(regime_findings(None, NOW)) == [UNKNOWN]


def test_not_degraded_is_quiet():
    snap = {"markets": {
        "US": {"degraded": False, "computed_at": "2026-08-13T00:00:00+00:00"},
        "KR": {"degraded": False, "computed_at": "2026-08-13T00:00:00+00:00"},
    }}
    assert regime_findings(snap, NOW) == []


def test_recently_degraded_is_quiet_not_alert():
    """방금 강등됐다 — 아직 persist 임계(기본 2시간)를 안 넘었으면 정상적인
    일시 지연일 수 있다(세션당 국면은 하루 1회만 갱신되므로 즉시 경보하면
    매번 울린다)."""
    snap = {"markets": {"US": {
        "degraded": True, "computed_at": (NOW - timedelta(minutes=30)).isoformat(),
        "reasons": ["국채 10년 금리 조회 실패 — 지표 제외"],
    }}}
    assert regime_findings(snap, NOW) == []


def test_degraded_persisting_past_threshold_is_alert():
    snap = {"markets": {"US": {
        "degraded": True, "computed_at": (NOW - timedelta(hours=3)).isoformat(),
        "reasons": ["국채 10년 금리 조회 실패 — 지표 제외", "코스피 조회 실패 — 지표 제외"],
    }}}
    findings = regime_findings(snap, NOW)
    assert _levels(findings) == [ALERT]
    assert "US" in findings[0].detail
    assert "3.0시간" in findings[0].detail


def test_markets_judged_independently():
    snap = {"markets": {
        "US": {"degraded": True, "computed_at": (NOW - timedelta(hours=5)).isoformat(), "reasons": []},
        "KR": {"degraded": False, "computed_at": (NOW - timedelta(hours=5)).isoformat(), "reasons": []},
    }}
    findings = regime_findings(snap, NOW)
    assert _levels(findings) == [ALERT]
    assert "US" in findings[0].detail
    assert "KR" not in findings[0].detail


def test_degraded_with_unparseable_timestamp_is_unknown():
    snap = {"markets": {"US": {"degraded": True, "computed_at": "not-a-timestamp", "reasons": []}}}
    assert _levels(regime_findings(snap, NOW)) == [UNKNOWN]


def test_legacy_snapshot_without_markets_key_reads_top_level_as_us():
    """구버전 캐시(markets 키 없음)는 최상위 필드가 US 상태다."""
    snap = {"degraded": True, "computed_at": (NOW - timedelta(hours=3)).isoformat(), "reasons": []}
    findings = regime_findings(snap, NOW)
    assert _levels(findings) == [ALERT]
    assert "US" in findings[0].detail
