"""휘발성 저장소 계약 — **실패 방향**이 이 모듈의 전부다.

캐시가 죽었을 때 "이미 본 것이니 건너뛴다"로 읽히면 뉴스가 조용히 사라진다.
모든 실패는 일을 더 하는 쪽으로 답해야 하고, Redis 가 통째로 죽으면 시스템은
Redis 도입 이전과 똑같이 동작해야 한다.
"""
from __future__ import annotations

import pytest

from quant.adapters.kv import NullKeyValue, RedisKeyValue, make_kv
from quant.core.ports import KeyValue


class BrokenRedis:
    """모든 호출이 터지는 클라이언트 — 접속이 끊긴 Redis."""

    def __getattr__(self, _name):
        def boom(*a, **k):
            raise ConnectionError("redis 죽음")
        return boom


class FakeRedis:
    def __init__(self):
        self.kv, self.sets, self.zsets, self.ttls = {}, {}, {}, {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, ex=None):
        self.kv[k] = v
        if ex:
            self.ttls[k] = ex
        return True

    def sadd(self, k, *m):
        s = self.sets.setdefault(k, set())
        before = len(s)
        s.update(m)
        return len(s) - before

    def sismember(self, k, m):
        return m in self.sets.get(k, set())

    def expire(self, k, ttl):
        self.ttls[k] = ttl
        return True

    def zadd(self, k, scores):
        self.zsets.setdefault(k, {}).update(scores)
        return len(scores)

    def zrevrange(self, k, start, end, withscores=False):
        items = sorted(self.zsets.get(k, {}).items(), key=lambda kv: -kv[1])
        return items[start:end + 1]

    def ping(self):
        return True


# ── 계약 준수 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("impl", [NullKeyValue(), RedisKeyValue(FakeRedis())])
def test_implementations_satisfy_the_protocol(impl):
    assert isinstance(impl, KeyValue)


# ── 실패 방향 (이 파일의 핵심) ────────────────────────────────────────────

def test_sadd_failure_reports_everything_as_new():
    """0 을 주면 호출자가 '중복이니 건너뛴다'로 읽고 기사가 사라진다."""
    kv = RedisKeyValue(BrokenRedis())
    assert kv.sadd("seen", "a", "b", "c") == 3


def test_null_store_also_reports_everything_as_new():
    assert NullKeyValue().sadd("seen", "a", "b") == 2


def test_sadd_with_no_members_is_zero_not_a_lie():
    """빈 호출에 '새것 0개'는 참이다 — 실패가 아니다."""
    assert RedisKeyValue(BrokenRedis()).sadd("seen") == 0
    assert NullKeyValue().sadd("seen") == 0


def test_sismember_failure_says_not_seen():
    """'본 적 있다'고 답하면 호출자가 건너뛴다 — 모를 땐 처리하는 쪽으로."""
    assert RedisKeyValue(BrokenRedis()).sismember("seen", "x") is False


def test_get_failure_is_cache_miss_not_an_exception():
    """캐시가 죽었다고 호출자가 죽으면 캐시를 안 쓰느니만 못하다."""
    assert RedisKeyValue(BrokenRedis()).get("k") is None


def test_every_write_failure_is_a_return_value_not_an_exception():
    kv = RedisKeyValue(BrokenRedis())
    assert kv.set("k", "v") is False
    assert kv.expire("k", 60) is False
    assert kv.zadd("z", {"a": 1.0}) == 0
    assert kv.ztop("z", 3) == []
    assert kv.healthy() is False


# ── 정상 동작 ─────────────────────────────────────────────────────────────

def test_sadd_counts_only_newly_added():
    """이 반환값이 곧 '처음 보는 것이었나'다 — 중복 판정이 왕복 한 번에 끝난다."""
    kv = RedisKeyValue(FakeRedis())
    assert kv.sadd("seen", "a", "b") == 2
    assert kv.sadd("seen", "b", "c") == 1


def test_keys_are_namespaced():
    """한 Redis 를 다른 용도와 나눠 쓸 때 키가 섞이면 안 된다."""
    r = FakeRedis()
    RedisKeyValue(r, prefix="quant:").set("k", "v")
    assert "quant:k" in r.kv and "k" not in r.kv


def test_ttl_is_passed_through():
    """수집 중복 집합은 하루 지나면 자연 만료돼야 메모리가 안 샌다."""
    r = FakeRedis()
    RedisKeyValue(r).set("seen:KR:2026-08-13", "1", ttl_seconds=86400)
    assert r.ttls["quant:seen:KR:2026-08-13"] == 86400


def test_ztop_returns_descending_scores():
    kv = RedisKeyValue(FakeRedis())
    kv.zadd("rank", {"005930": 3.0, "000660": 9.0, "263750": 1.0})
    assert kv.ztop("rank", 2) == [("000660", 9.0), ("005930", 3.0)]


def test_bytes_from_redis_are_decoded():
    """실제 redis-py 는 bytes 를 돌려준다 — 호출자가 매번 decode 하면 안 된다."""
    class BytesRedis(FakeRedis):
        def get(self, k):
            return b"value"

        def zrevrange(self, k, s, e, withscores=False):
            return [(b"005930", 2.0)]

    kv = RedisKeyValue(BytesRedis())
    assert kv.get("k") == "value"
    assert kv.ztop("z", 1) == [("005930", 2.0)]


# ── 팩토리 ────────────────────────────────────────────────────────────────

def test_no_url_means_null_store_not_an_error():
    """Redis 는 선택 사항이다 — 없다고 수집·리포트가 죽으면 안 된다."""
    assert isinstance(make_kv({}), NullKeyValue)


def test_unreachable_redis_degrades_to_null_store():
    kv = make_kv({"REDIS_URL": "redis://127.0.0.1:1/0"})
    assert isinstance(kv, NullKeyValue)
    assert kv.healthy() is False
