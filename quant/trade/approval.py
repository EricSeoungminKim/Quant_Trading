"""신규 진입 승인 게이트 — 텔레그램 버튼 승인 요청의 로컬 상태를 영속화한다.

control.py와 같은 역할·같은 패턴이다: 파일이 단일 진실 소스라 모든 read가 매번
디스크에서 다시 로드하고, 쓰기는 tmp-write-then-replace 원자적 패턴을 쓴다. 엔진
프로세스와 별도 프로세스(텔레그램 브리지 등)가 같은 파일을 볼 수 있어야 하고,
쓰다 죽어도 반쯤 쓰인 JSON이 남으면 안 되기 때문이다.

**여기서 다루는 것은 신규 진입뿐이다.** 청산(EXIT_LONG/SCALE_OUT)과 kill-switch
flatten은 이 게이트를 절대 통과하지 않는다 — 사용자가 자고 있을 때 손절이 승인
대기로 막히면 3배 레버리지 ETF에 갇힌다. 그건 이 게이트가 막으려는 어떤 위험보다
나쁘다. 그 강제는 호출부(app/loop.py의 `_ENTRY_ACTIONS` 분기)에 있고, 이 모듈은
진입 요청 레코드만 관리한다 — 여기에 청산 관련 상태가 들어오면 안 된다.

상태 전이는 단방향이다:
    pending ──approve──> approved ──take_approved──> executed
       ├─────reject────> rejected
       └─────expire────> expired
종결(approved 이외)된 레코드에 대한 결정은 전부 no-op이다 — 사용자가 버튼을 두 번
눌러도 두 번 사면 안 된다(멱등).

네트워크/브로커 임포트 금지 — control.py와 동일하게 순수 로컬 상태 저장소다.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, fields
from pathlib import Path

from quant.core.models import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    ApprovalRequest,
)

# 재수출 — ApprovalRequest/STATUS_* 는 quant.core.models 로 옮겨졌다(어댑터가
# 이 모듈을 몰라도 되게: telegram_approval.py 등은 core에서 직접 임포트한다).
# 기존 호출부(quant/trade/loop.py 등)는 여기서 그대로 가져다 쓴다.
__all__ = [
    "ApprovalGate",
    "ApprovalRequest",
    "STATUS_APPROVED",
    "STATUS_EXECUTED",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_REJECTED",
]

DEFAULT_STATE_PATH = Path("data/state/approvals.json")

# 요청 ID 길이. 텔레그램 callback_data는 1~64바이트 제한이고 우리는 "ok:<id>"
# 형태로 접두사 3바이트를 쓴다 — uuid4 앞 12자면 충돌 확률이 사실상 0이면서
# 한도의 4분의 1도 쓰지 않는다.
_ID_LENGTH = 12

_FIELD_NAMES = frozenset(f.name for f in fields(ApprovalRequest))


class ApprovalGate:
    def __init__(self, state_path: Path = DEFAULT_STATE_PATH):
        self.state_path = Path(state_path)
        self._requests: dict[str, ApprovalRequest] = {}
        self._load()

    # ------------------------------------------------------------ 영속화
    def _load(self) -> None:
        if not self.state_path.exists():
            self._requests = {}
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # 파일이 깨졌으면 메모리 상태를 유지한다(control.py와 동일) — 깨진
            # 파일 때문에 승인 대기 중인 요청을 "없던 일"로 만들지 않는다.
            return
        loaded: dict[str, ApprovalRequest] = {}
        for raw in data.get("requests", []):
            if not isinstance(raw, dict):
                continue
            try:
                req = ApprovalRequest(**{k: v for k, v in raw.items() if k in _FIELD_NAMES})
            except TypeError:
                # 스키마가 바뀌기 전에 쓰인 낡은 레코드 — 조용히 건너뛴다.
                continue
            loaded[req.id] = req
        self._requests = loaded

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"requests": [asdict(r) for r in self._sorted()]}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def _sorted(self) -> list[ApprovalRequest]:
        return sorted(self._requests.values(), key=lambda r: (r.created_at, r.id))

    # ------------------------------------------------------------ 쓰기
    def create(
        self,
        strategy_id: str,
        symbol: str,
        action: str,
        qty: float,
        price: float,
        reason: str = "",
        target_weight: float = 0.0,
        stop: float | None = None,
        target: float | None = None,
        state_update: dict | None = None,
        now: float | None = None,
    ) -> ApprovalRequest:
        self._load()
        req = ApprovalRequest(
            id=uuid.uuid4().hex[:_ID_LENGTH],
            strategy_id=strategy_id,
            symbol=symbol,
            action=action,
            qty=qty,
            price=price,
            reason=reason,
            created_at=time.time() if now is None else now,
            target_weight=target_weight,
            stop=stop,
            target=target,
            state_update=dict(state_update or {}),
        )
        self._requests[req.id] = req
        self._save()
        return req

    def decide(self, req_id: str, approved: bool, now: float | None = None) -> ApprovalRequest | None:
        """pending 요청에만 결정을 적용한다. 이미 종결됐거나 없으면 None(멱등).

        None을 받은 호출자는 "이미 처리된 요청"으로 응답해야 한다 — 다시 실행하면
        안 된다. 버튼 연타/재전송된 콜백이 두 번 사는 것을 막는 지점이 여기다."""
        self._load()
        req = self._requests.get(req_id)
        if req is None or not req.is_pending:
            return None
        req.status = STATUS_APPROVED if approved else STATUS_REJECTED
        req.decided_at = time.time() if now is None else now
        self._save()
        return req

    def expire(self, req_id: str, now: float | None = None) -> ApprovalRequest | None:
        """pending 요청 하나를 만료시킨다. 이미 종결됐으면 None.

        시간 경과 외에도 쓴다: 텔레그램 전송이 실패해 사용자가 볼 수 없게 된 요청은
        즉시 만료시켜야 유령 pending으로 남지 않는다(fail-closed)."""
        self._load()
        req = self._requests.get(req_id)
        if req is None or not req.is_pending:
            return None
        req.status = STATUS_EXPIRED
        req.decided_at = time.time() if now is None else now
        self._save()
        return req

    def take_approved(self) -> list[ApprovalRequest]:
        """approved인 요청을 반환하고 즉시 executed로 표시한다 (one-shot).

        TradingControl.consume_flatten()과 같은 계약이다: 반환받은 호출자는 그
        사이클에 집행(또는 명시적 취소)을 책임진다. 여기서 상태를 먼저 넘기는 것은
        집행 도중 프로세스가 죽어도 재시작 후 같은 주문이 다시 나가지 않게 하기
        위해서다 — 중복 매수보다 미체결이 낫다."""
        self._load()
        taken = [r for r in self._sorted() if r.status == STATUS_APPROVED]
        if taken:
            for req in taken:
                req.status = STATUS_EXECUTED
            self._save()
        return taken

    def expire_stale(self, expire_seconds: float, now: float | None = None) -> list[ApprovalRequest]:
        """created_at 기준으로 만료된 pending 요청을 expired로 바꾸고 반환한다.

        판정 기준을 created_at(디스크에 저장된 값)으로 두는 이유: 신호는 시간에
        민감하다. 09:35 진입 신호를 10:30에 승인해 실행하면 그건 다른 거래다.
        엔진이 재시작돼도 만료 시계가 리셋되면 안 된다."""
        ts = time.time() if now is None else now
        self._load()
        stale = [
            r for r in self._sorted()
            if r.is_pending and ts - r.created_at >= expire_seconds
        ]
        if stale:
            for req in stale:
                req.status = STATUS_EXPIRED
                req.decided_at = ts
            self._save()
        return stale

    def set_message_id(self, req_id: str, message_id: int) -> None:
        self._load()
        req = self._requests.get(req_id)
        if req is None:
            return
        req.message_id = message_id
        self._save()

    def prune(self, keep_seconds: float, now: float | None = None) -> int:
        """종결된(pending이 아닌) 오래된 레코드를 지운다. 지운 개수 반환.

        pending은 아무리 오래돼도 지우지 않는다 — 만료 판정은 expire_stale의
        몫이고, 여기서 지워버리면 사용자 버튼이 조용한 no-op이 된다."""
        ts = time.time() if now is None else now
        self._load()
        doomed = [
            r.id for r in self._requests.values()
            if not r.is_pending and ts - r.created_at >= keep_seconds
        ]
        if doomed:
            for req_id in doomed:
                del self._requests[req_id]
            self._save()
        return len(doomed)

    # ------------------------------------------------------------ 읽기
    def pending(self) -> list[ApprovalRequest]:
        self._load()
        return [r for r in self._sorted() if r.is_pending]

    def get(self, req_id: str) -> ApprovalRequest | None:
        self._load()
        return self._requests.get(req_id)

    def all(self) -> list[ApprovalRequest]:
        self._load()
        return self._sorted()
