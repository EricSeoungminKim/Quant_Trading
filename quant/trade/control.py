"""수동 킬 스위치 — halt/resume/flatten 상태를 디스크에 영속화한다.

엔진 프로세스(quant-engine.service)와 텔레그램 브리지(tg-bridge.service)는 별도
프로세스로 뜬다. 브리지가 /halt·/flatten을 받아 이 상태를 갱신하면, 엔진은 다음
사이클에 파일을 다시 읽어 반영한다 — 그래서 모든 read 메서드가 매번 디스크에서
다시 로드한다(파일이 곧 단일 진실 소스).

halt는 신규 진입만 막는다. 열린 포지션의 관리·청산은 절대 막지 않는다 — 그게
이 모듈 전체의 존재 이유다. halt 상태에서도 손절/익절/마감청산은 그대로 동작해야
포지션이 무방비로 방치되지 않는다.

Portfolio(quant/trade/portfolio/portfolio.py)와 동일한 tmp-write-then-replace
원자적 쓰기 패턴을 그대로 재사용한다 — 새 영속화 방식을 만들지 않는다.

네트워크/브로커 임포트 금지 — 이 모듈은 순수 로컬 상태 저장소다.
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_STATE_PATH = Path("data/state/control.json")


class TradingControl:
    def __init__(self, state_path: Path = DEFAULT_STATE_PATH):
        self.state_path = Path(state_path)
        self._halted = False
        self._halt_reason = ""
        self._halted_by = "manual"
        self._flatten_requested = False
        self._flatten_scope = "all"
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        self._halted = bool(data.get("halted", False))
        self._halt_reason = data.get("halt_reason", "")
        # halted_by: 소유자가 수동으로 정지(REST)했는지, 회로차단기가 자동으로
        # 중단했는지 구분한다. 이 필드가 생기기 전 control.json은 값이 없으므로
        # "manual"을 기본값으로 둔다(구 스키마 하위호환).
        self._halted_by = data.get("halted_by", "manual")
        self._flatten_requested = bool(data.get("flatten_requested", False))
        # flatten_scope: "all"(전량) 또는 "day"(단타 전략 보유분만). 이 필드가
        # 생기기 전 데이터는 항상 전량 flatten이었으므로 기본값은 "all".
        self._flatten_scope = data.get("flatten_scope", "all")

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "halted": self._halted,
            "halt_reason": self._halt_reason,
            "halted_by": self._halted_by,
            "flatten_requested": self._flatten_requested,
            "flatten_scope": self._flatten_scope,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_path)

    def halt(self, reason: str = "", by: str = "manual") -> None:
        """거래를 중단한다. `by`는 "manual"(소유자 /halt·/rest) 또는 "auto"(회로차단기
        — 브로커 대사 불일치·연속 사이클 실패 등)만 쓴다. `/status`가 이 값으로
        수동 정지(REST)와 자동 중단을 구분해 보여준다."""
        self._load()
        self._halted = True
        self._halt_reason = reason
        self._halted_by = by
        self._save()

    def resume(self) -> None:
        self._load()
        self._halted = False
        self._halt_reason = ""
        self._halted_by = "manual"
        self._save()

    def is_halted(self) -> bool:
        self._load()
        return self._halted

    def halt_reason(self) -> str:
        self._load()
        return self._halt_reason

    def halted_by(self) -> str:
        """"manual"(소유자 REST) 또는 "auto"(회로차단기 자동 중단). halt 중이 아닐 때는
        의미 없다 — 호출 전 `is_halted()`로 확인할 것."""
        self._load()
        return self._halted_by

    def request_flatten(self, scope: str = "all") -> None:
        """`scope="all"`(기본, 전량) 또는 `scope="day"`(단타 전략 보유분만)."""
        self._load()
        self._flatten_requested = True
        self._flatten_scope = scope
        self._save()

    def consume_flatten(self) -> bool:
        """flatten 요청 여부를 반환하고 즉시 클리어한다 (one-shot, 하위호환용 bool 버전).

        scope까지 필요한 호출부는 `consume_flatten_scope()`를 쓴다 — 이 메서드는
        내부적으로 그걸 위임 호출하므로 one-shot 계약은 하나로 유지된다."""
        return self.consume_flatten_scope() != ""

    def consume_flatten_scope(self) -> str:
        """flatten 요청 여부와 scope를 반환하고 즉시 클리어한다 (one-shot).

        반환값: 요청 없으면 `""`, 있으면 `"all"` 또는 `"day"`.

        엔진이 매 사이클 이걸 호출해 flatten을 소비한다. 빈 문자열이 아닌 값을
        받은 호출자는 반드시 그 사이클에 실제로 해당 scope의 청산을 실행해야
        한다 — 여기서는 플래그만 관리하고 실행은 하지 않는다(네트워크/브로커
        임포트 금지 제약)."""
        self._load()
        requested = self._flatten_requested
        scope = self._flatten_scope if requested else ""
        if requested:
            self._flatten_requested = False
            self._flatten_scope = "all"
            self._save()
        return scope
