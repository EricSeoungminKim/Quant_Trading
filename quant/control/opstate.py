"""운영 상태 — "무엇이 마지막으로 언제 성공했나".

## 왜 Redis 인가 (그리고 왜 수집 dedup 은 아닌가)

처음엔 수집기의 중복 판정을 Redis 집합으로 옮기려 했다. **따져보니 이득이 없다** —
수집기는 `seen_count`/`first_seen` 을 갱신해야 해서 어차피 그날 JSONL 을 통째로
읽는다. 집합은 "봤나"만 답하므로 그 로드를 없애지 못하고, 날짜를 넘겨 중복을
지우면 `streak_days`(연속 등장) 근거가 깨진다.

반면 운영 상태는 Redis 의 성질과 정확히 맞는다:

- **휘발돼도 된다.** 잃으면 다음 실행이 다시 쓴다. 돈이 걸린 상태가 아니다.
- **TTL 이 곧 의미다.** "5분 안에 갱신됐나"가 살아있음의 정의다 — 만료가 곧 경보다.
- **쓰는 쪽과 읽는 쪽이 다른 프로세스다.** 수집기·리포트가 쓰고, 감시 에이전트가
  읽는다. 파일로 하면 각자 락과 형식을 만들어야 한다.

## 계약

기록은 **절대 호출자를 죽이지 않는다.** Redis 가 없으면 `NullKeyValue` 가 조용히
받아 넘기고, 시스템은 Redis 도입 이전과 똑같이 동작한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

# 살아있음의 정의 = 이 시간 안에 갱신됐나. 만료가 곧 경보다.
#
# 2026-08-13: 900 → 3600. **원래 값이 주석과 모순이었다.** "30분 주기 작업이 한 번
# 걸러도 살아있다고 본다"면 최소 60분이 필요한데 15분이었다 — 정상적으로 도는
# 30분 작업도 매 주기 후반 15분은 "최근 성공이 없다"로 보였다. 감시를 붙이자마자
# 이 거짓 경보가 나왔고, **거짓 경보가 오는 감시는 꺼진다.**
# 지금 가장 짧은 주기 작업이 30분(뉴스 수집)이라 그것의 2배 + 여유로 잡는다.
HEARTBEAT_TTL = 3600         # 1시간 — 30분 주기 작업이 한 번 걸러도 살아있다고 본다
FEED_HEALTH_TTL = 86_400     # 하루
RUN_TTL = 7 * 86_400         # 일주일 — 주간 리뷰까지 남는다

# 잡별 신선도 창 오버라이드. 대부분의 잡은 HEARTBEAT_TTL(1시간, 30분 주기 기준)을
# 그대로 쓴다 — 여기 없는 잡의 판정은 이 파일을 고치기 전과 동일하다. deepdive는
# 하루 1회(KR 05:00 / US 17:30)라 1시간짜리 창을 쓰면 실행 직후를 빼고 항상
# "최근 성공 없음"으로 오경보된다. close-report도 하루 1회(outcomes 16:00 직후)라
# 같은 이유로 같은 창을 쓴다.
# deepdive/close-report와 같은 이유로 report_close:KR(오후 마감 리포트, KR 13:40
# 1일 1회)도 같은 창을 쓴다.
#
# 2026-08-17 실측 추가: report:KR/US(아침·저녁 리포트, 일 1회)·backup(03:30 일
# 1회)·ingest(KR 16:10 / US 07:10, 일 2회지만 간격 최대 ~15시간)도 전부 1시간
# 창으로는 하루 대부분이 "최근 성공 없음"이었다 — 그동안은 make_kv 의 REDIS_URL
# 결측(kv.py 폴백 참조)으로 기록 자체가 유실돼 unknown 으로만 보였고, 그 버그를
# 고치자 이 오경보가 드러났다. 거짓 경보가 오는 감시는 꺼진다.
JOB_HEARTBEAT_TTL: dict[str, int] = {
    "deepdive:KR": 30 * 3600,
    "deepdive:US": 30 * 3600,
    "close-report": 30 * 3600,
    "report_close:KR": 30 * 3600,
    "report:KR": 30 * 3600,
    "report:US": 30 * 3600,
    "backup": 30 * 3600,
    "ingest": 30 * 3600,
    # ops-judge(판단 워치독, 2026-08-19)는 하루 2회(KR 13:10 / US 01:45 KST) —
    # 정상 간격은 약 12시간이다. 20시간이면 한 번 걸러도(연휴·일시 장애) 오경보하지
    # 않으면서, 이 잡 자체가 조용히 멈추는 사고(크론 삭제, LLM 자격증명 만료로
    # 계속 실패 등)는 하루 안에 잡는다 — 이 감시는 스스로를 감시하지 못하므로
    # 기존 규칙 기반 job_findings 가 대신 지킨다.
    "ops-judge": 20 * 3600,
    # 자동 판정 루프(2026-08-24)는 매일 16:30 KST — 정상 간격 24시간. 30시간이면
    # 하루 걸러도 오경보하지 않으면서 조용한 죽음은 다음 날 잡는다. **이 잡이
    # 멈춘 것과 "판정할 게 없는 것"은 겉보기가 같다**(둘 다 텔레그램 무소식) —
    # 그래서 하트비트로 구분한다. 소유자가 자리를 비운 동안 이 구분이 특히 중요하다.
    "experiments": 30 * 3600,
    # 주간 재검토(2026-08-26)는 주 1회(토 06:25) — 8일이면 한 번 걸러도 다음
    # 주말 전에 잡는다.
    "weekly-review": 8 * 24 * 3600,
    # equity-snapshot(자본 곡선 1점 기록, 2026-08-24)은 하루 2회(KR/US 세션
    # 마감 후) — 정상 간격은 약 12시간이지만, 주말엔 정규장이 없어 금요일 마감
    # 이후 월요일 마감까지 최대 약 3일 공백이 생긴다. 30시간이면 매주 오경보한다
    # — 주말 최장 공백(3일)에 하루치 결측(연휴·일시 장애) 여유를 더해 76시간으로
    # 잡는다(3일 24h + 4h 여유가 아니라 넉넉히 24h*3 + 4h ≈ 76h).
    "equity-snapshot": 76 * 3600,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_run(kv, job: str, ok: bool, detail: str = "", now: str | None = None) -> None:
    """작업 한 번의 결과. `job` 은 `collect:KR`, `report:US`, `ingest` 같은 이름."""
    stamp = now or _now()
    kv.set(f"run:{job}:last", stamp, ttl_seconds=RUN_TTL)
    kv.set(f"run:{job}:ok", "1" if ok else "0", ttl_seconds=RUN_TTL)
    if ok:
        # 성공만 짧은 TTL 로 따로 둔다 — **만료 자체가 "요즘 성공한 적 없다"는 신호**다.
        kv.set(f"run:{job}:last_ok", stamp,
               ttl_seconds=JOB_HEARTBEAT_TTL.get(job, HEARTBEAT_TTL))
    if detail:
        kv.set(f"run:{job}:detail", detail[:500], ttl_seconds=RUN_TTL)


def record_feed_health(kv, market: str, alive: list[str], dead: list[str],
                       now: str | None = None) -> None:
    """피드별 마지막 성공 시각.

    죽은 피드를 **지우지 않는다** — 지우면 "원래 없던 피드"와 "오늘 죽은 피드"가
    구분되지 않는다. 마지막 성공 시각이 낡아가는 것으로 표현한다.
    """
    stamp = now or _now()
    if alive:
        kv.zadd(f"feed:{market}:last_ok", {name: _epoch(stamp) for name in alive})
    kv.set(f"feed:{market}:dead", ",".join(sorted(dead)), ttl_seconds=FEED_HEALTH_TTL)
    kv.set(f"feed:{market}:checked_at", stamp, ttl_seconds=FEED_HEALTH_TTL)


def _epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def stale_feeds(kv, market: str, now: datetime, older_than_hours: int = 6) -> list[str]:
    """마지막 성공이 오래된 피드. 감시 에이전트가 읽는 입구다.

    Redis 가 죽어 있으면 빈 목록 — **"이상 없음"이 아니라 "모른다"**이므로,
    호출자는 `kv.healthy()` 를 함께 보고 판단해야 한다.
    """
    cutoff = now.timestamp() - older_than_hours * 3600
    return [name for name, ts in kv.ztop(f"feed:{market}:last_ok", 200) if ts < cutoff]


def snapshot(kv, jobs: list[str]) -> dict:
    """감시 에이전트용 한 판 읽기. Redis 가 죽었으면 `available: False`."""
    if not kv.healthy():
        return {"available": False, "jobs": {}}
    out = {}
    for job in jobs:
        out[job] = {
            "last": kv.get(f"run:{job}:last"),
            "ok": kv.get(f"run:{job}:ok") == "1",
            # last_ok 가 None 이면 HEARTBEAT_TTL 안에 성공한 적이 없다는 뜻이다
            "fresh": kv.get(f"run:{job}:last_ok") is not None,
            "detail": kv.get(f"run:{job}:detail"),
        }
    return {"available": True, "jobs": out}


# ── LLM 호출 계측 (narrate 어댑터가 기록, health 가 읽는다) ────────────────
#
# 유래: 무료 레인(OpenRouter :free) 요청 수·실패율·지연을 아무도 기록하지
# 않아 한도에 걸리기 시작해도 몰랐다. `quant/adapters/narrate.py` 의
# `narrate()`/`chat_with_tools()` 가 매 호출 끝에 `record_llm_call` 을 부른다.

# 24시간 집계 + 여유 1시간. `record_llm_call` 이 호출마다 이 TTL 로 키를
# 갱신하므로 호출이 계속 들어오는 한 zset 자체는 만료되지 않는다
# (`record_feed_health` 와 같은 관례) — 읽는 쪽(`llm_stats`)이 스코어(epoch)로
# 24시간 창 밖은 걸러낸다.
LLM_CALL_TTL = 25 * 3600


def record_llm_call(kv, lane: str, ok: bool, seconds: float, now: str | None = None) -> None:
    """LLM 호출 1건 계측(`narrate.py` 의 `narrate()`/`chat_with_tools()` 결과).

    zset(멤버=호출 1건, 스코어=epoch 초)에 담는다 — `record_feed_health` 와
    같은 자료구조 선택 이유(정렬된 최근순 조회가 `ztop` 하나로 된다).

    실패해도 호출자를 죽이지 않는다 — `kv.py` 의 `zadd`/`expire` 자체가 이미
    모든 예외를 삼킨다(`RedisKeyValue`), 여기서 또 감싸지 않는다(경계는
    어댑터 한 곳, `record_run` 과 같은 계약).
    """
    stamp = now or _now()
    epoch = _epoch(stamp)
    # 멤버는 유일해야 zadd 가 같은 초의 다른 호출을 덮어쓰지 않는다 — uuid
    # 접미사로 충돌을 막는다(판정에는 쓰이지 않는다, 그냥 키 유일성용).
    member = f"{stamp}|{'1' if ok else '0'}|{seconds:.3f}|{uuid.uuid4().hex[:8]}"
    kv.zadd(f"llm:{lane}:calls", {member: epoch})
    kv.expire(f"llm:{lane}:calls", LLM_CALL_TTL)


def llm_stats(kv, lane: str, now: datetime, window_hours: int = 24,
             sample: int = 2000) -> dict | None:
    """`lane` 의 최근 `window_hours`시간 호출 집계 — 감시 에이전트가 읽는 입구.

    Redis 가 죽었으면 `None`(**"호출 없음"이 아니라 "모른다"**, 이 모듈의
    반복되는 규율). `ztop` 은 최신순 상위 `sample`건만 보므로, 그 창 안에
    24시간보다 오래된 멤버가 섞여도 스코어(epoch)로 다시 거른다 — 무료 레인
    호출 빈도(하루 수십 건 수준)에서 `sample=2000` 이면 창을 놓칠 일이 없다.
    """
    if not kv.healthy():
        return None
    cutoff = now.timestamp() - window_hours * 3600
    total = failed = 0
    for member, ts in kv.ztop(f"llm:{lane}:calls", sample):
        if ts < cutoff:
            continue
        total += 1
        parts = member.split("|")
        if len(parts) >= 2 and parts[1] == "0":
            failed += 1
    return {"total": total, "failed": failed}
