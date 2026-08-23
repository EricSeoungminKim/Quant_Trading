"""휘발성 키-값 저장소 어댑터 (`core.ports.KeyValue` 구현).

**거래 평면은 이 모듈을 임포트할 수 없다** — `tests/test_architecture.py` 가
막는다. Redis 는 휘발성이라 돈이 걸린 상태(포지션·주문·control·체결 원장)를
담을 수 없고, 그건 파일이 진실이다.

## 실패 방향이 이 모듈의 핵심 설계다

캐시가 죽었을 때 **"이미 본 것이니 건너뛴다"로 읽히면 뉴스가 조용히 사라진다.**
그래서 모든 실패는 **일을 더 하는 쪽**으로 답한다:

- `sadd` 실패 → "전부 새것"(`len(members)`). 중복을 한 번 더 처리하는 건 무해하다
  (JSONL 저장소가 어차피 다시 걸러낸다). 새 기사를 버리는 건 무해하지 않다.
- `sismember` 실패 → `False`("본 적 없다") → 호출자가 처리한다.
- `get` 실패 → `None`("캐시 없음") → 호출자가 원본을 읽는다.

즉 Redis 가 통째로 죽으면 시스템은 **Redis 도입 이전과 똑같이** 동작한다.
그게 캐시가 지켜야 할 유일한 계약이다.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class NullKeyValue:
    """Redis 가 없을 때. 모든 조회가 "모른다"고 답해 호출자를 원본 경로로 보낸다."""

    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        return False

    def sadd(self, key: str, *members: str) -> int:
        return len(members)  # 전부 새것 — 실패는 일을 더 하는 쪽으로

    def sismember(self, key: str, member: str) -> bool:
        return False

    def expire(self, key: str, ttl_seconds: int) -> bool:
        return False

    def zadd(self, key: str, scores: dict[str, float]) -> int:
        return 0

    def ztop(self, key: str, n: int) -> list[tuple[str, float]]:
        return []

    def healthy(self) -> bool:
        return False


class RedisKeyValue:
    """Redis 구현. 예외를 삼키고 실패를 반환값으로 표현한다."""

    def __init__(self, client, prefix: str = "quant:"):
        self._r = client
        self._p = prefix

    def _k(self, key: str) -> str:
        return f"{self._p}{key}"

    def get(self, key: str) -> str | None:
        try:
            v = self._r.get(self._k(key))
        except Exception as e:
            log.debug("redis get 실패: %s", type(e).__name__)
            return None
        return v.decode() if isinstance(v, (bytes, bytearray)) else v

    def set(self, key: str, value: str, ttl_seconds: int | None = None) -> bool:
        try:
            self._r.set(self._k(key), value, ex=ttl_seconds)
            return True
        except Exception as e:
            log.debug("redis set 실패: %s", type(e).__name__)
            return False

    def sadd(self, key: str, *members: str) -> int:
        if not members:
            return 0
        try:
            return int(self._r.sadd(self._k(key), *members))
        except Exception as e:
            # **전부 새것으로 답한다** — 여기서 0 을 주면 호출자가 "중복이니
            # 건너뛴다"로 읽고 기사가 사라진다.
            log.debug("redis sadd 실패(전부 새것으로 처리): %s", type(e).__name__)
            return len(members)

    def sismember(self, key: str, member: str) -> bool:
        try:
            return bool(self._r.sismember(self._k(key), member))
        except Exception:
            return False

    def expire(self, key: str, ttl_seconds: int) -> bool:
        try:
            return bool(self._r.expire(self._k(key), ttl_seconds))
        except Exception:
            return False

    def zadd(self, key: str, scores: dict[str, float]) -> int:
        if not scores:
            return 0
        try:
            return int(self._r.zadd(self._k(key), scores))
        except Exception:
            return 0

    def ztop(self, key: str, n: int) -> list[tuple[str, float]]:
        try:
            raw = self._r.zrevrange(self._k(key), 0, max(0, n - 1), withscores=True)
        except Exception:
            return []
        out = []
        for member, score in raw:
            out.append((member.decode() if isinstance(member, (bytes, bytearray)) else member,
                        float(score)))
        return out

    def healthy(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False


def make_kv(env: dict[str, str] | None = None):
    """`REDIS_URL` 이 없거나 접속 불가면 `NullKeyValue`.

    예외를 던지지 않는 이유: Redis 는 선택 사항이다. 없다고 수집·리포트가
    죽으면 캐시를 안 쓰느니만 못하다.
    """
    e = os.environ if env is None else env
    url = (e.get("REDIS_URL") or "").strip()
    if not url and env is None:
        # os.environ 에 없으면 .env.local 을 앱 경로로 읽는다(narrate.get_key 와
        # 같은 폴백, 같은 사고 유형). systemd 리포트 유닛·크론은 .env.local 을
        # 로드하지 않아 REDIS_URL 이 비고, 그러면 record_run 이 NullKeyValue 로
        # **조용히 유실**됐다 — 2026-08-17 실측: 타이머/크론 잡의 opstate 기록이
        # Phase 5 이후 전부 빠졌고 수동 실행만 남아, 감시가 "최근 성공 없음"을
        # 외치는데 리포트는 정상 발행되고 있었다.
        try:
            from quant.adapters.env import get_key
            url = (get_key("REDIS_URL") or "").strip()
        except Exception:  # noqa: BLE001 — 폴백 실패가 호출자를 죽이지 않는다
            url = ""
    if not url:
        return NullKeyValue()
    try:
        import redis
    except ImportError:
        log.warning("redis 미설치 — 캐시 없이 동작한다")
        return NullKeyValue()
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        if not client.ping():
            raise RuntimeError("ping 실패")
    except Exception as ex:
        log.warning("Redis 접속 실패(%s) — 캐시 없이 동작한다", type(ex).__name__)
        return NullKeyValue()
    return RedisKeyValue(client)
