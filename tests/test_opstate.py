"""운영 상태 — 감시 에이전트(Phase 5)가 읽을 층.

핵심 성질 둘:
① 기록이 호출자를 절대 죽이지 않는다 (Redis 는 선택 사항이다).
② **모른다와 이상 없음을 섞지 않는다** — Redis 가 죽었을 때 "정상"으로 읽히면
   감시 층이 있으나 마나다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quant.adapters.kv import NullKeyValue, RedisKeyValue
from quant.control.opstate import (
    HEARTBEAT_TTL,
    LLM_CALL_TTL,
    llm_stats,
    record_feed_health,
    record_llm_call,
    record_run,
    snapshot,
    stale_feeds,
)
from tests.test_kv import BrokenRedis, FakeRedis

UTC = timezone.utc
NOW = datetime(2026, 8, 13, 7, 0, tzinfo=UTC)


def _kv():
    return RedisKeyValue(FakeRedis())


# ── 기록이 호출자를 죽이지 않는다 ─────────────────────────────────────────

def test_recording_survives_a_dead_redis():
    """수집기가 상태 기록 실패로 죽으면, 캐시를 안 쓰느니만 못하다."""
    kv = RedisKeyValue(BrokenRedis())
    record_run(kv, "collect:KR", ok=True, detail="955건")
    record_feed_health(kv, "KR", alive=["연합뉴스"], dead=["CNN"])


def test_recording_survives_no_redis_at_all():
    kv = NullKeyValue()
    record_run(kv, "ingest", ok=False, detail="접속 실패")
    record_feed_health(kv, "US", alive=[], dead=["Bloomberg"])


# ── 모른다 vs 이상 없음 ───────────────────────────────────────────────────

def test_snapshot_says_unavailable_when_redis_is_down():
    """빈 결과를 '이상 없음'으로 읽으면 감시가 조용히 눈이 먼다."""
    assert snapshot(RedisKeyValue(BrokenRedis()), ["collect:KR"])["available"] is False
    assert snapshot(NullKeyValue(), ["collect:KR"])["available"] is False


def test_snapshot_reports_job_outcome():
    kv = _kv()
    record_run(kv, "collect:KR", ok=True, detail="955건", now=NOW.isoformat())
    snap = snapshot(kv, ["collect:KR"])
    assert snap["available"] is True
    job = snap["jobs"]["collect:KR"]
    assert job["ok"] is True and job["fresh"] is True and job["detail"] == "955건"


def test_failed_run_is_not_fresh():
    """실패는 last_ok 를 갱신하지 않는다 — 만료가 곧 '요즘 성공한 적 없다'다."""
    kv = _kv()
    record_run(kv, "ingest", ok=False, detail="접속 실패", now=NOW.isoformat())
    job = snapshot(kv, ["ingest"])["jobs"]["ingest"]
    assert job["ok"] is False and job["fresh"] is False
    assert job["last"] is not None, "실패해도 '언제 시도했나'는 남아야 한다"


# ── TTL 이 곧 살아있음의 정의 ─────────────────────────────────────────────

def test_success_marker_expires_so_silence_becomes_an_alarm():
    r = FakeRedis()
    record_run(RedisKeyValue(r), "collect:KR", ok=True, now=NOW.isoformat())
    assert r.ttls["quant:run:collect:KR:last_ok"] == HEARTBEAT_TTL


def test_job_specific_heartbeat_ttl_does_not_affect_other_jobs():
    """deepdive 는 일 1회 잡이라 30시간 창을 쓴다(I1c) — 다른 잡의 판정(기본
    HEARTBEAT_TTL)은 그대로여야 한다."""
    from quant.control.opstate import JOB_HEARTBEAT_TTL

    r = FakeRedis()
    kv = RedisKeyValue(r)
    record_run(kv, "deepdive:KR", ok=True, now=NOW.isoformat())
    record_run(kv, "collect:KR", ok=True, now=NOW.isoformat())
    assert r.ttls["quant:run:deepdive:KR:last_ok"] == JOB_HEARTBEAT_TTL["deepdive:KR"]
    assert r.ttls["quant:run:deepdive:KR:last_ok"] != HEARTBEAT_TTL
    assert r.ttls["quant:run:collect:KR:last_ok"] == HEARTBEAT_TTL


def test_detail_is_truncated_so_a_stack_trace_cannot_blow_up_the_store():
    r = FakeRedis()
    record_run(RedisKeyValue(r), "ingest", ok=False, detail="x" * 5000)
    assert len(r.kv["quant:run:ingest:detail"]) == 500


# ── 피드 건강도 ───────────────────────────────────────────────────────────

def test_dead_feed_is_not_erased_but_ages():
    """지우면 '원래 없던 피드'와 '오늘 죽은 피드'가 구분되지 않는다."""
    kv = _kv()
    old = (NOW - timedelta(days=2)).isoformat()
    record_feed_health(kv, "KR", alive=["연합뉴스", "한경"], dead=[], now=old)
    record_feed_health(kv, "KR", alive=["연합뉴스"], dead=["한경"], now=NOW.isoformat())
    assert stale_feeds(kv, "KR", NOW, older_than_hours=6) == ["한경"]


def test_recently_successful_feeds_are_not_stale():
    kv = _kv()
    record_feed_health(kv, "KR", alive=["연합뉴스"], dead=[], now=NOW.isoformat())
    assert stale_feeds(kv, "KR", NOW, older_than_hours=6) == []


def test_stale_feeds_is_empty_when_redis_is_down():
    """빈 목록이 '이상 없음'이 아니다 — 호출자가 healthy() 를 함께 봐야 한다."""
    kv = RedisKeyValue(BrokenRedis())
    assert stale_feeds(kv, "KR", NOW) == []
    assert kv.healthy() is False


# ── LLM 호출 계측 ─────────────────────────────────────────────────────────
#
# 유래: 무료 레인 요청 수·실패율·지연을 아무도 기록하지 않아 한도에 걸리기
# 시작해도 몰랐다.

def test_recording_llm_call_survives_a_dead_redis():
    kv = RedisKeyValue(BrokenRedis())
    record_llm_call(kv, "narrate", ok=True, seconds=1.2)


def test_recording_llm_call_survives_no_redis_at_all():
    kv = NullKeyValue()
    record_llm_call(kv, "narrate", ok=False, seconds=0.5)


def test_llm_call_key_refreshes_its_ttl():
    r = FakeRedis()
    record_llm_call(RedisKeyValue(r), "narrate", ok=True, seconds=1.0, now=NOW.isoformat())
    assert r.ttls["quant:llm:narrate:calls"] == LLM_CALL_TTL


def test_llm_stats_counts_ok_and_failed_calls_within_window():
    kv = _kv()
    record_llm_call(kv, "narrate", ok=True, seconds=1.0, now=NOW.isoformat())
    record_llm_call(kv, "narrate", ok=True, seconds=1.5, now=NOW.isoformat())
    record_llm_call(kv, "narrate", ok=False, seconds=30.0, now=NOW.isoformat())

    stats = llm_stats(kv, "narrate", NOW)
    assert stats == {"total": 3, "failed": 1}


def test_llm_stats_excludes_calls_older_than_the_window():
    kv = _kv()
    old = (NOW - timedelta(hours=25)).isoformat()
    record_llm_call(kv, "narrate", ok=True, seconds=1.0, now=old)
    record_llm_call(kv, "narrate", ok=True, seconds=1.0, now=NOW.isoformat())

    assert llm_stats(kv, "narrate", NOW) == {"total": 1, "failed": 0}


def test_llm_stats_different_lanes_are_independent():
    kv = _kv()
    record_llm_call(kv, "narrate", ok=True, seconds=1.0, now=NOW.isoformat())
    record_llm_call(kv, "tool", ok=False, seconds=1.0, now=NOW.isoformat())

    assert llm_stats(kv, "narrate", NOW) == {"total": 1, "failed": 0}
    assert llm_stats(kv, "tool", NOW) == {"total": 1, "failed": 1}


def test_llm_stats_with_no_calls_yet_is_zero_not_unknown():
    kv = _kv()
    assert llm_stats(kv, "narrate", NOW) == {"total": 0, "failed": 0}


def test_llm_stats_is_none_when_redis_is_down():
    """0건과 '모른다'는 다른 사건이다 — kv 가 죽으면 None."""
    assert llm_stats(RedisKeyValue(BrokenRedis()), "narrate", NOW) is None
    assert llm_stats(NullKeyValue(), "narrate", NOW) is None
