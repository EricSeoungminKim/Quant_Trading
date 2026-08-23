"""수집 계층의 데이터 계약.

`SourceResult`가 이 시스템의 핵심 불변식을 담는다: **실패도 결과다.** 예외를
위로 던져 리포트를 죽이는 대신 ok=False 로 기록하고, 렌더러가 "결측 — 사유"로
표시한다. 결측을 조용히 숨기면 이후 채점 계층이 오염된다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SourceResult:
    key: str
    ok: bool
    data: dict | None
    error: str | None
    url: str
    fetched_at: datetime
    latency_ms: int


@dataclass(frozen=True)
class Snapshot:
    schema_version: int
    market: str
    session_date: date
    generated_at: datetime
    results: dict[str, SourceResult]

    def missing(self) -> list[str]:
        return sorted(k for k, r in self.results.items() if not r.ok)

    def to_json(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "market": self.market,
            "session_date": self.session_date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "results": {
                k: {**asdict(r), "fetched_at": r.fetched_at.isoformat()}
                for k, r in sorted(self.results.items())
            },
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Snapshot":
        d = json.loads(raw)
        return cls(
            schema_version=d["schema_version"],
            market=d["market"],
            session_date=date.fromisoformat(d["session_date"]),
            generated_at=datetime.fromisoformat(d["generated_at"]),
            results={
                k: SourceResult(
                    **{**v, "fetched_at": datetime.fromisoformat(v["fetched_at"])}
                )
                for k, v in d["results"].items()
            },
        )
