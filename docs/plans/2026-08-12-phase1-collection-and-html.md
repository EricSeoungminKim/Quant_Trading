# Phase 1 — 수집 툴킷 + HTML 발행 구현 계획

> ⚠️ **[대체됨 — 2026-08-26 감사]** 이 문서는 4평면 재설계(`docs/ARCHITECTURE.md`)
> **이전**에 쓰였고, 여기 적힌 **파일 경로는 더 이상 유효하지 않다**: 예를 들어
> `quant/apps/report_config.py`(실제: `quant/apps/config.py`),
> `quant/analyze/clock.py`, `quant/collect/sources/commodity.py` 는 존재하지 않고,
> `tests/test_*.py` 대부분은 `tests/report/` 아래로 옮겨졌다. **설계 의도와 이유
> (스냅샷→렌더 결정론, 소스 실패 격리)는 여전히 유효하므로 이력 문서로 남긴다** —
> 경로를 그대로 따라가지 말 것. 현재 구조는 `docs/CODE-TOUR.md` 와 각 디렉토리의
> `CLAUDE.md` 를 보라.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** LLM 호출 없이, KR/US 두 시장의 시황 데이터를 결정론적으로 수집해 스냅샷 JSON으로 남기고, 그 스냅샷에서 HTML 리포트를 생성한다.

**Architecture:** 소스 모듈 각각이 `SourceResult`(성공/실패·출처 URL·수집시각)를 반환하고, 수집기가 이들을 병렬 실행해 하나의 `Snapshot`으로 합친다. 한 소스의 실패는 해당 섹션만 결측으로 만들 뿐 리포트를 막지 않는다. 렌더러는 스냅샷만 읽으므로 **같은 스냅샷 → 항상 같은 HTML**이다.

**Tech Stack:** Python ≥3.12, `uv`, httpx, yfinance, pandas, Jinja2, lxml, pytest

## Global Constraints

- **발행 시각은 양쪽 모두 개장 60분 전.** KR = 09:00 KST 개장 → 08:00 KST. US = 09:30 ET 개장 → 08:30 ET (KST로 서머타임 21:30 / 표준시 22:30).
- **DST를 고정 KST 시각으로 하드코딩하지 않는다.** 항상 `America/New_York` 기준으로 계산해 KST로 환산한다.
- **한 소스의 실패가 리포트를 죽이지 않는다.** 예외는 소스 경계에서 잡아 `SourceResult(ok=False, error=...)`로 만든다. 결측을 조용히 숨기지 않고 HTML에 사유와 함께 표시한다.
- **네트워크를 타는 테스트는 `@pytest.mark.live`로 분리한다.** 기본 `pytest`는 저장된 픽스처로만 돌아 오프라인·CI에서 통과해야 한다. 픽스처는 `tests/fixtures/`에 실제 응답을 저장해 쓴다.
- **비밀값을 로그·예외·HTML에 넣지 않는다.** API 키는 `.env.local`에서만 읽고, 오류 메시지는 응답 본문을 120자로 자른다.
- **금액 단위는 원본 단위를 그대로 보존하고, 변환은 렌더 시점에만 한다.** 네이버 수급은 억원 단위다.
- 스냅샷 스키마 버전은 `SCHEMA_VERSION = 1`로 시작하며, 구조가 바뀌면 올린다.

---

## File Structure

```
market_report/
├── pyproject.toml                  # 의존성·pytest 설정
├── .env.local.example              # (완료)
├── scripts/check_keys.py           # (완료)
├── report/
│   ├── __init__.py
│   ├── config.py                   # .env.local 로딩
│   ├── clock.py                    # 발행 시각·세션 날짜 (DST)
│   ├── contracts.py                # SourceResult, Snapshot, 직렬화
│   ├── http.py                     # 공용 HTTP 클라이언트
│   ├── collect.py                  # 소스 병렬 실행 → Snapshot
│   ├── render.py                   # Snapshot → HTML
│   ├── cli.py                      # python -m quant.apps.report_cli
│   ├── sources/
│   │   ├── __init__.py             # SOURCE_REGISTRY (시장별 소스 목록)
│   │   ├── market.py               # yfinance 시세 + 2차 소스 교차검증
│   │   ├── naver_flow.py           # KRX 투자자 수급
│   │   ├── fred.py                 # 순유동성·NFCI·금리
│   │   ├── calendar.py             # FRED releases/dates + FOMC → D-day
│   │   ├── derived.py              # 옵션 P/C·MaxPain·S5FI·금/구리
│   │   ├── commodity.py            # CFTC COT + EIA 재고
│   │   └── feeds.py                # 뉴스 RSS + 유튜브 RSS
│   └── templates/report.html.j2
└── tests/
    ├── fixtures/                   # 저장된 실제 응답
    └── test_*.py
```

---

### Task 1: 프로젝트 부트스트랩 + 설정 로딩

**Files:**
- Create: `pyproject.toml`, `report/__init__.py`, `quant/apps/report_config.py`, `tests/test_config.py`

**Interfaces:**
- Consumes: 없음
- Produces: `quant.adapters.env.load_env(path: Path | None = None) -> dict[str, str]`, `quant.adapters.env.get_key(name: str) -> str | None`

- [ ] **Step 1: `pyproject.toml` 작성**

```toml
[project]
name = "market-report"
version = "0.1.0"
description = "개인 시황 리포트 — KR/US 개장 60분 전 자동 발행"
requires-python = ">=3.12"
dependencies = [
    "httpx>=0.27",
    "yfinance>=1.5.2",
    "pandas>=2.2",
    "lxml>=5.0",
    "jinja2>=3.1",
]

[dependency-groups]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: 네트워크를 타는 테스트 (기본 실행에서 제외)"]
addopts = "-m 'not live'"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["report"]
```

- [ ] **Step 2: 환경 구성**

```bash
cd ~/Documents/GitHub/market_report && uv sync && mkdir -p quant/collect/sources quant/analyze/templates tests/fixtures && touch report/__init__.py quant/collect/sources/__init__.py
```

- [ ] **Step 3: 실패하는 테스트 작성 — `tests/test_config.py`**

```python
from pathlib import Path
from quant.adapters.env import load_env


def test_load_env_parses_keys_and_ignores_comments(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text("# 주석\nFRED_API_KEY=abc123\n\nEIA_API_KEY=\n")
    env = load_env(p)
    assert env["FRED_API_KEY"] == "abc123"
    assert env["EIA_API_KEY"] == ""


def test_load_env_missing_file_returns_empty(tmp_path: Path):
    assert load_env(tmp_path / "nope") == {}


def test_load_env_keeps_equals_inside_value(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text("TOKEN=a=b=c\n")
    assert load_env(p)["TOKEN"] == "a=b=c"
```

- [ ] **Step 4: 실패 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.adapters.env'`

- [ ] **Step 5: `quant/apps/report_config.py` 구현**

```python
"""`.env.local` 로딩. 키 값은 절대 로그에 남기지 않는다."""
from __future__ import annotations

from functools import cache
from pathlib import Path

DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env.local"


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or DEFAULT_ENV
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


@cache
def _env() -> dict[str, str]:
    return load_env()


def get_key(name: str) -> str | None:
    """키가 없거나 빈 문자열이면 None — 호출부가 '결측'으로 처리하게 한다."""
    return _env().get(name) or None
```

- [ ] **Step 6: 통과 확인**

Run: `uv run pytest tests/test_config.py -v`
Expected: 3 passed

- [ ] **Step 7: 커밋**

```bash
git add pyproject.toml report/ tests/ && git commit -m "feat: 프로젝트 부트스트랩 + .env.local 로딩"
```

---

### Task 2: 발행 시각 계산 (DST)

**Files:**
- Create: `quant/analyze/clock.py`, `tests/test_clock.py`

**Interfaces:**
- Consumes: 없음
- Produces: `quant.core.report_clock.publish_at(market: str, session_date: date) -> datetime` (KST aware), `quant.core.report_clock.KST`, `quant.core.report_clock.ET`

이 태스크가 Global Constraint의 DST 규칙을 구현한다. 거래 저장소가 고정 크론으로 겪은 사고(동계에 하루 늦게 실행)를 여기서 원천 차단한다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_clock.py`**

2026년 미국 서머타임: 3월 8일 시작, 11월 1일 종료.

```python
from datetime import date
import pytest
from quant.core.report_clock import publish_at, KST


def _kst(dt):
    return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")


def test_kr_publishes_one_hour_before_0900():
    assert _kst(publish_at("KR", date(2026, 8, 12))) == "2026-08-12 08:00"


def test_kr_is_unaffected_by_us_dst():
    assert _kst(publish_at("KR", date(2026, 1, 15))) == "2026-01-15 08:00"


def test_us_summer_time_is_2130_kst():
    # EDT (UTC-4): 08:30 ET -> 12:30 UTC -> 21:30 KST
    assert _kst(publish_at("US", date(2026, 8, 12))) == "2026-08-12 21:30"


def test_us_standard_time_is_2230_kst():
    # EST (UTC-5): 08:30 ET -> 13:30 UTC -> 22:30 KST
    assert _kst(publish_at("US", date(2026, 1, 15))) == "2026-01-15 22:30"


@pytest.mark.parametrize(
    "session,expected",
    [
        (date(2026, 3, 6), "2026-03-06 22:30"),   # DST 시작 직전 (금)
        (date(2026, 3, 9), "2026-03-09 21:30"),   # DST 시작 직후 (월)
        (date(2026, 10, 30), "2026-10-30 21:30"), # DST 종료 직전 (금)
        (date(2026, 11, 2), "2026-11-02 22:30"),  # DST 종료 직후 (월)
    ],
)
def test_us_dst_transitions(session, expected):
    assert _kst(publish_at("US", session)) == expected


def test_unknown_market_raises():
    with pytest.raises(ValueError, match="market"):
        publish_at("JP", date(2026, 8, 12))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_clock.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.core.report_clock'`

- [ ] **Step 3: `quant/analyze/clock.py` 구현**

```python
"""발행 시각 계산. 개장 60분 전이 유일한 규칙이다.

US 시각을 KST로 하드코딩하지 않는다 — 서머타임 전환 때 한 시간씩 어긋나
"장전 리포트"가 개장 후에 나오는 사고를 막기 위해서다. 항상 거래소 현지
시간대에서 계산해 KST로 환산한다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

LEAD = timedelta(hours=1)
_OPEN: dict[str, tuple[time, ZoneInfo]] = {
    "KR": (time(9, 0), KST),
    "US": (time(9, 30), ET),
}


def publish_at(market: str, session_date: date) -> datetime:
    """`market` 세션의 발행 시각 (KST aware)."""
    try:
        open_time, tz = _OPEN[market]
    except KeyError:
        raise ValueError(f"unknown market: {market!r} (KR|US)") from None
    local_open = datetime.combine(session_date, open_time, tzinfo=tz)
    return (local_open - LEAD).astimezone(KST)
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_clock.py -v`
Expected: 9 passed

- [ ] **Step 5: 세션 윈도우 테스트 추가 — `tests/test_clock.py` 하단**

리포트는 "하루치"가 아니라 **직전 발행 이후**를 다룬다. 월요일 KR 리포트는 금요일
08:00 이후 72시간을 봐야 한다 — 고정 24시간이면 주말 뉴스가 통째로 사라진다.

```python
from datetime import datetime, timedelta
from quant.core.report_clock import session_window


def test_window_starts_at_previous_publish():
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    prev = datetime(2026, 8, 11, 8, 0, tzinfo=KST)
    start, end = session_window(now, prev)
    assert start == prev and end == now


def test_window_spans_weekend():
    monday = datetime(2026, 8, 17, 8, 0, tzinfo=KST)
    friday = datetime(2026, 8, 14, 8, 0, tzinfo=KST)
    start, end = session_window(monday, friday)
    assert (end - start) == timedelta(days=3)


def test_window_defaults_to_24h_when_no_previous():
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    start, end = session_window(now, None)
    assert (end - start) == timedelta(hours=24)


def test_window_ignores_future_previous():
    """직전 스냅샷이 미래면(시계 오류·수동 실행) 24시간으로 떨어진다."""
    now = datetime(2026, 8, 12, 8, 0, tzinfo=KST)
    future = datetime(2026, 8, 13, 8, 0, tzinfo=KST)
    start, end = session_window(now, future)
    assert (end - start) == timedelta(hours=24)
```

- [ ] **Step 6: 실패 확인**

Run: `uv run pytest tests/test_clock.py -k window -v`
Expected: FAIL — `ImportError: cannot import name 'session_window'`

- [ ] **Step 7: `quant/analyze/clock.py`에 세션 윈도우 추가**

```python
DEFAULT_WINDOW = timedelta(hours=24)


def session_window(
    publish_time: datetime, previous: datetime | None
) -> tuple[datetime, datetime]:
    """이번 리포트가 다룰 구간 (시작, 끝).

    시작점을 **직전 스냅샷의 생성시각**으로 잡는다. 요일이나 공휴일 달력을
    하드코딩하지 않으므로 주말·공휴일·장애로 하루 걸른 경우가 전부 자동으로
    메워진다. 직전이 없거나 미래면 24시간으로 떨어진다.
    """
    if previous is None or previous >= publish_time:
        return publish_time - DEFAULT_WINDOW, publish_time
    return previous, publish_time
```

- [ ] **Step 8: 통과 확인**

Run: `uv run pytest tests/test_clock.py -v`
Expected: 13 passed

- [ ] **Step 9: 커밋**

```bash
git add quant/analyze/clock.py tests/test_clock.py && git commit -m "feat: 개장 60분 전 발행 시각 + 세션 윈도우 (주말/공휴일 자동 확장)"
```

---

### Task 3: SourceResult / Snapshot 계약

**Files:**
- Create: `quant/collect/contracts.py`, `tests/test_contracts.py`

**Interfaces:**
- Consumes: `quant.core.report_clock.KST`
- Produces:
  - `SourceResult(key, ok, data, error, url, fetched_at, latency_ms)` — frozen dataclass
  - `Snapshot(schema_version, market, session_date, generated_at, results: dict[str, SourceResult])`
  - `Snapshot.to_json() -> str`, `Snapshot.from_json(str) -> Snapshot`
  - `Snapshot.missing() -> list[str]` — 실패한 소스 키 목록
  - `SCHEMA_VERSION: int`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_contracts.py`**

```python
from datetime import date, datetime
from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult


def _snap() -> Snapshot:
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market="KR",
        session_date=date(2026, 8, 12),
        generated_at=datetime(2026, 8, 12, 8, 0, tzinfo=KST),
        results={
            "market": SourceResult(
                key="market", ok=True, data={"KOSPI": 6345.5}, error=None,
                url="https://example.test/a", fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST),
                latency_ms=120,
            ),
            "flow": SourceResult(
                key="flow", ok=False, data=None, error="HTTP 500",
                url="https://example.test/b", fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST),
                latency_ms=8000,
            ),
        },
    )


def test_roundtrip_preserves_everything():
    snap = _snap()
    back = Snapshot.from_json(snap.to_json())
    assert back == snap


def test_missing_lists_failed_sources_only():
    assert _snap().missing() == ["flow"]


def test_json_is_stable_for_same_snapshot():
    # 같은 스냅샷은 항상 같은 바이트여야 재현성 검증이 가능하다
    assert _snap().to_json() == _snap().to_json()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.contracts'`

- [ ] **Step 3: `quant/collect/contracts.py` 구현**

```python
"""수집 계층의 데이터 계약.

`SourceResult`가 이 시스템의 핵심 불변식을 담는다: **실패도 결과다.** 예외를
위로 던져 리포트를 죽이는 대신 ok=False 로 기록하고, 렌더러가 "결측 — 사유"로
표시한다. 결측을 조용히 숨기면 3층 채점이 오염된다.
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
                k: SourceResult(**{**v, "fetched_at": datetime.fromisoformat(v["fetched_at"])})
                for k, v in d["results"].items()
            },
        )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_contracts.py -v`
Expected: 3 passed

- [ ] **Step 5: 커밋**

```bash
git add quant/collect/contracts.py tests/test_contracts.py && git commit -m "feat: SourceResult/Snapshot 계약 + 직렬화"
```

---

### Task 4: HTTP 클라이언트 + 병렬 수집기

**Files:**
- Create: `quant/adapters/http.py`, `quant/collect/snapshot.py`, `tests/test_collect.py`

**Interfaces:**
- Consumes: `quant.collect.contracts.{SourceResult, Snapshot, SCHEMA_VERSION}`, `quant.core.report_clock.KST`
- Produces:
  - `quant.adapters.http.client() -> httpx.Client` — 공용 설정 (UA, 타임아웃, HTTP/1.1 강제)
  - `quant.collect.snapshot.run_source(key: str, url: str, fn: Callable[[], dict]) -> SourceResult`
  - `quant.collect.snapshot.collect(market: str, session_date: date, sources: dict[str, tuple[str, Callable[[], dict]]]) -> Snapshot`

> FRED가 httpx 기본 설정에서 타임아웃하고 curl은 즉시 응답한 사례가 있었다(2026-08-12 실측). HTTP/2 협상 문제로 보이므로 공용 클라이언트에서 **HTTP/1.1을 강제**한다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_collect.py`**

```python
from datetime import date
from quant.collect.snapshot import collect, run_source


def test_run_source_captures_success():
    r = run_source("x", "https://example.test", lambda: {"v": 1})
    assert r.ok and r.data == {"v": 1} and r.error is None
    assert r.latency_ms >= 0


def test_run_source_converts_exception_to_failed_result():
    def boom():
        raise RuntimeError("네트워크 끊김")

    r = run_source("x", "https://example.test", boom)
    assert not r.ok and r.data is None
    assert "RuntimeError" in r.error and "네트워크 끊김" in r.error


def test_run_source_truncates_long_errors():
    def boom():
        raise RuntimeError("A" * 500)

    assert len(run_source("x", "u", boom).error) <= 160


def test_collect_isolates_failures():
    snap = collect(
        "KR",
        date(2026, 8, 12),
        {
            "good": ("https://example.test/g", lambda: {"a": 1}),
            "bad": ("https://example.test/b", lambda: (_ for _ in ()).throw(ValueError("nope"))),
        },
    )
    assert snap.results["good"].ok
    assert not snap.results["bad"].ok
    assert snap.missing() == ["bad"]
    assert snap.market == "KR"


def test_collect_result_is_json_roundtrippable():
    from quant.collect.contracts import Snapshot

    snap = collect("US", date(2026, 8, 12), {"g": ("u", lambda: {"a": 1})})
    assert Snapshot.from_json(snap.to_json()) == snap
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_collect.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.snapshot'`

- [ ] **Step 3: `quant/adapters/http.py` 구현**

```python
"""공용 HTTP 클라이언트.

HTTP/1.1을 강제한다 — fred.stlouisfed.org 가 httpx 기본 설정에서 읽기 타임아웃을
내고 curl 은 0.06초에 응답한 사례가 있었다(2026-08-12 실측).
"""
from __future__ import annotations

import httpx

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": UA},
        timeout=timeout,
        follow_redirects=True,
        http2=False,
    )
```

- [ ] **Step 4: `quant/collect/snapshot.py` 구현**

```python
"""소스들을 병렬 실행해 하나의 Snapshot으로 합친다.

**한 소스의 실패가 리포트를 죽이지 않는다** — 모든 예외를 소스 경계에서 잡아
SourceResult(ok=False)로 만든다. 이게 이 모듈의 유일한 존재 이유다.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Callable

from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult

MAX_WORKERS = 8
_ERROR_MAX = 160


def run_source(key: str, url: str, fn: Callable[[], dict]) -> SourceResult:
    started = time.monotonic()
    try:
        data, error, ok = fn(), None, True
    except Exception as e:  # 소스 경계 — 여기서 삼킨다
        data, ok = None, False
        error = f"{type(e).__name__}: {e}"[:_ERROR_MAX]
    return SourceResult(
        key=key,
        ok=ok,
        data=data,
        error=error,
        url=url,
        fetched_at=datetime.now(KST),
        latency_ms=int((time.monotonic() - started) * 1000),
    )


def collect(
    market: str,
    session_date: date,
    sources: dict[str, tuple[str, Callable[[], dict]]],
) -> Snapshot:
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            key: pool.submit(run_source, key, url, fn)
            for key, (url, fn) in sources.items()
        }
        results = {key: f.result() for key, f in futures.items()}
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        market=market,
        session_date=session_date,
        generated_at=datetime.now(KST),
        results=results,
    )
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_collect.py -v`
Expected: 5 passed

- [ ] **Step 6: 커밋**

```bash
git add quant/adapters/http.py quant/collect/snapshot.py tests/test_collect.py && git commit -m "feat: HTTP 클라이언트 + 실패 격리 병렬 수집기"
```

---

### Task 5: 투자자 수급 (네이버금융) — 컬럼 매핑 검증 포함

**Files:**
- Create: `quant/collect/sources/naver_flow.py`, `tests/test_naver_flow.py`, `tests/fixtures/naver_kospi_20260811.html`
- Test: `tests/test_naver_flow.py`

**Interfaces:**
- Consumes: `quant.adapters.http.client`
- Produces: `quant.collect.sources.naver_flow.parse_flow(html_bytes: bytes) -> list[dict]`, `quant.collect.sources.naver_flow.fetch_flow(sosok: str, bizdate: str) -> dict`, `FLOW_COLUMNS: tuple[str, ...]`, `URL_TEMPLATE: str`

> **이 태스크가 Phase 1에서 가장 조용히 틀리기 쉬운 곳이다.** 네이버 표는 `<th>` 12개
> 대 데이터 셀 11개로 어긋난다(`기관` 이 그룹 헤더). 컬럼을 한 칸 밀려 읽으면
> "외국인 순매수"가 틀린 값이 되고 아무도 눈치채지 못한다.
>
> 방어책: **기관계 == 6개 기관 세부항목의 합**이라는 항등식이 성립한다. 실측 확인
> (KOSPI 26.08.11): -2089 + -65 + 2717 + -76 + 151 + -426 = 212 = 기관계.
> KOSDAQ은 -955 vs -954로 반올림 오차가 있으므로 허용 오차 ±2를 둔다.

- [ ] **Step 1: 픽스처 저장**

```bash
cd ~/Documents/GitHub/market_report && curl -s -A "Mozilla/5.0" \
  "https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate=20260811&sosok=01" \
  -o tests/fixtures/naver_kospi_20260811.html && wc -c tests/fixtures/naver_kospi_20260811.html
```

- [ ] **Step 2: 실패하는 테스트 작성 — `tests/test_naver_flow.py`**

```python
from pathlib import Path
import pytest
from quant.collect.sources.naver_flow import FLOW_COLUMNS, parse_flow

FIXTURE = Path(__file__).parent / "fixtures" / "naver_kospi_20260811.html"


@pytest.fixture
def rows():
    return parse_flow(FIXTURE.read_bytes())


def test_parses_rows(rows):
    assert len(rows) >= 5
    assert set(rows[0]) == {"date", *FLOW_COLUMNS}


def test_latest_row_matches_known_values(rows):
    # 2026-08-11 KOSPI, 억원 단위 — 네이버 화면 표시값과 대조해 고정한 값이다
    r = next(r for r in rows if r["date"] == "2026-08-11")
    assert r["개인"] == -708
    assert r["외국인"] == 535
    assert r["기관계"] == 212
    assert r["금융투자"] == -2089
    assert r["기타법인"] == -40


def test_institution_subtotals_sum_to_total(rows):
    """컬럼 밀림 탐지 — 기관계는 6개 세부항목의 합이어야 한다."""
    subs = ("금융투자", "보험", "투신", "은행", "기타금융", "연기금등")
    for r in rows:
        assert abs(sum(r[s] for s in subs) - r["기관계"]) <= 2, r


def test_date_is_iso_normalized(rows):
    assert all(len(r["date"]) == 10 and r["date"][4] == "-" for r in rows)


@pytest.mark.live
def test_live_fetch_kospi_and_kosdaq():
    from quant.collect.sources.naver_flow import fetch_flow

    for sosok in ("01", "02"):
        d = fetch_flow(sosok, "20260811")
        assert d["rows"], sosok
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/test_naver_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.naver_flow'`

- [ ] **Step 4: `quant/collect/sources/naver_flow.py` 구현**

```python
"""KRX 투자자별 매매동향 — 네이버금융 경로.

data.krx.co.kr 의 getJsonData.cmd 는 400 LOGOUT 으로 막혀 있다(2026-08-12 실측).
회사 리포트도 실제로는 이 네이버 경로를 쓰고 있었으므로 동일 경로를 택한다.

단위는 억원. 원본 단위를 보존하고 변환은 렌더 시점에만 한다.
"""
from __future__ import annotations

import html
import re

from quant.adapters.http import client

URL_TEMPLATE = (
    "https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={bizdate}&sosok={sosok}"
)

# 데이터 행의 실제 컬럼 순서. <th> 순서와 다르다 — 헤더에 colspan 그룹이 섞여 있어
# 그대로 믿으면 한 칸씩 밀린다. 순서 변경 시 test_institution_subtotals_sum_to_total 이 잡는다.
FLOW_COLUMNS = (
    "개인", "외국인", "기관계",
    "금융투자", "보험", "투신", "은행", "기타금융", "연기금등",
    "기타법인",
)

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})$")


def _decode(raw: bytes) -> str:
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _num(text: str) -> int:
    return int(text.replace(",", "").replace("+", ""))


def parse_flow(raw: bytes) -> list[dict]:
    text = _decode(raw)
    rows: list[dict] = []
    for tr in _TR.findall(text):
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        cells = [c for c in cells if c and c != "\xa0"]
        if len(cells) != len(FLOW_COLUMNS) + 1:
            continue
        m = _DATE.match(cells[0])
        if not m:
            continue
        yy, mm, dd = m.groups()
        row: dict = {"date": f"20{yy}-{mm}-{dd}"}
        try:
            for name, cell in zip(FLOW_COLUMNS, cells[1:]):
                row[name] = _num(cell)
        except ValueError:
            continue
        rows.append(row)
    return rows


def fetch_flow(sosok: str, bizdate: str) -> dict:
    """sosok: '01'=KOSPI, '02'=KOSDAQ. bizdate: 'YYYYMMDD'."""
    url = URL_TEMPLATE.format(bizdate=bizdate, sosok=sosok)
    with client() as c:
        resp = c.get(url)
        resp.raise_for_status()
    rows = parse_flow(resp.content)
    if not rows:
        raise ValueError(f"투자자 수급 파싱 결과 0행 (sosok={sosok}) — 표 구조 변경 의심")
    return {"market": {"01": "KOSPI", "02": "KOSDAQ"}[sosok], "unit": "억원", "rows": rows}
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_naver_flow.py -v`
Expected: 4 passed, 1 deselected (live)

- [ ] **Step 6: 라이브 경로 확인**

Run: `uv run pytest tests/test_naver_flow.py -m live -v`
Expected: PASS (KOSPI·KOSDAQ 둘 다)

- [ ] **Step 7: 커밋**

```bash
git add quant/collect/sources/naver_flow.py tests/test_naver_flow.py tests/fixtures/ && git commit -m "feat: 네이버금융 투자자 수급 수집 + 컬럼 밀림 탐지 테스트"
```

---

### Task 6: 시세 백본 (yfinance) + 2차 소스 교차검증

**Files:**
- Create: `quant/collect/sources/market.py`, `tests/test_market.py`

**Interfaces:**
- Consumes: `quant.adapters.http.client`
- Produces: `quant.collect.sources.market.fetch_quotes(market: str) -> dict`, `quant.collect.sources.market.crosscheck(symbol_to_price: dict[str, float]) -> dict`, `TICKERS: dict[str, dict[str, str]]`, `CROSSCHECK_TOLERANCE_PCT: float`

> yfinance는 야후의 **비공식** 스크레이퍼라 야후가 엔드포인트를 바꾸면 깨진다. 핵심
> 시계열은 Stooq(무료·키 불필요)로 한 번 더 받아 괴리가 크면 경고한다.
> *조용히 틀린 숫자보다 시끄럽게 결측인 편이 낫다.*

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_market.py`**

```python
import pytest
from quant.collect.sources.market import CROSSCHECK_TOLERANCE_PCT, TICKERS, crosscheck


def test_both_markets_declare_tickers():
    assert set(TICKERS) == {"KR", "US"}
    assert TICKERS["KR"] and TICKERS["US"]


def test_crosscheck_flags_nothing_when_within_tolerance():
    out = crosscheck({"^GSPC": 7728.20}, {"^GSPC": 7728.90})
    assert out["warnings"] == []


def test_crosscheck_flags_large_divergence():
    out = crosscheck({"^GSPC": 7728.20}, {"^GSPC": 6500.00})
    assert len(out["warnings"]) == 1
    assert "^GSPC" in out["warnings"][0]


def test_crosscheck_ignores_symbols_missing_from_secondary():
    assert crosscheck({"^GSPC": 100.0}, {})["warnings"] == []


def test_tolerance_is_conservative():
    assert 0 < CROSSCHECK_TOLERANCE_PCT <= 2.0


@pytest.mark.live
def test_live_quotes_have_expected_keys():
    from quant.collect.sources.market import fetch_quotes

    d = fetch_quotes("KR")
    assert d["quotes"], "빈 시세"
    assert all("close" in v for v in d["quotes"].values())
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_market.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.market'`

- [ ] **Step 3: `quant/collect/sources/market.py` 구현**

```python
"""시세 백본 — yfinance.

회사 리포트는 EODHD·Infomax 같은 유료 벤더를 쓴다. 우리는 못 쓰므로 yfinance가
백본이 되고, 그만큼 단일 벤더 의존이 이 시스템의 최대 취약점이다. 핵심 심볼은
Stooq로 교차검증해 괴리를 경고로 드러낸다.
"""
from __future__ import annotations

import csv
import io
import warnings

warnings.filterwarnings("ignore", module="yfinance")

from quant.adapters.http import client

CROSSCHECK_TOLERANCE_PCT = 1.0

_COMMON = {
    "^GSPC": "S&P500", "^IXIC": "NASDAQ", "^VIX": "VIX",
    "DX-Y.NYB": "DXY", "KRW=X": "USD/KRW",
    "GC=F": "금", "HG=F": "구리", "CL=F": "WTI", "BZ=F": "브렌트",
    "BTC-USD": "비트코인",
}
TICKERS: dict[str, dict[str, str]] = {
    "KR": {**_COMMON, "^KS11": "KOSPI", "^KQ11": "KOSDAQ", "^N225": "니케이"},
    "US": {**_COMMON, "^DJI": "다우", "^TNX": "미국10년", "SI=F": "은", "NG=F": "천연가스"},
}

# 교차검증 대상 — yfinance 심볼 → Stooq 심볼
_STOOQ = {"^GSPC": "^spx", "^KS11": "^kospi", "KRW=X": "usdkrw", "^VIX": "^vix"}


def fetch_quotes(market: str) -> dict:
    import yfinance as yf

    tickers = TICKERS[market]
    df = yf.download(
        list(tickers), period="5d", progress=False, auto_adjust=False, threads=True
    )["Close"]
    quotes: dict[str, dict] = {}
    for sym, label in tickers.items():
        if sym not in df.columns:
            continue
        series = df[sym].dropna()
        if series.empty:
            continue
        close = float(series.iloc[-1])
        prev = float(series.iloc[-2]) if len(series) > 1 else close
        quotes[sym] = {
            "label": label,
            "close": close,
            "prev": prev,
            "change_pct": round((close / prev - 1) * 100, 3) if prev else 0.0,
        }
    secondary = _fetch_stooq({s for s in _STOOQ if s in quotes})
    check = crosscheck({s: v["close"] for s, v in quotes.items()}, secondary)
    return {"quotes": quotes, "crosscheck": check}


def _fetch_stooq(symbols: set[str]) -> dict[str, float]:
    """Stooq 일별 CSV — 키 불필요. 실패해도 교차검증만 생략된다."""
    out: dict[str, float] = {}
    with client(timeout=10.0) as c:
        for sym in symbols:
            try:
                r = c.get(f"https://stooq.com/q/d/l/?s={_STOOQ[sym]}&i=d")
                rows = list(csv.DictReader(io.StringIO(r.text)))
                if rows:
                    out[sym] = float(rows[-1]["Close"])
            except Exception:
                continue  # 2차 소스 실패는 경고 대상이 아니다
    return out


def crosscheck(primary: dict[str, float], secondary: dict[str, float]) -> dict:
    warns: list[str] = []
    for sym, p in primary.items():
        s = secondary.get(sym)
        if s is None or not p:
            continue
        diff = abs(p / s - 1) * 100
        if diff > CROSSCHECK_TOLERANCE_PCT:
            warns.append(f"{sym}: yfinance {p:,.2f} vs stooq {s:,.2f} ({diff:.1f}% 괴리)")
    return {"checked": sorted(secondary), "warnings": warns}
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_market.py -v`
Expected: 5 passed, 1 deselected

- [ ] **Step 5: 라이브 확인**

Run: `uv run pytest tests/test_market.py -m live -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/market.py tests/test_market.py && git commit -m "feat: yfinance 시세 백본 + Stooq 교차검증"
```

---

### Task 7: FRED — 순유동성·NFCI·금리

**Files:**
- Create: `quant/collect/sources/fred.py`, `tests/test_fred.py`

**Interfaces:**
- Consumes: `quant.adapters.env.get_key`, `quant.adapters.http.client`
- Produces: `quant.collect.sources.fred.fetch_macro() -> dict`, `quant.collect.sources.fred.net_liquidity(walcl, tga, rrp) -> float`, `SERIES: dict[str, str]`

> 순유동성 = WALCL − TGA − RRP (단위 백만달러). 셋 다 발표 주기가 달라 **가장 최근
> 공통 날짜**로 맞춰야 한다 — 날짜가 어긋난 채 빼면 수천억 달러가 튄다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_fred.py`**

```python
import pytest
from quant.collect.sources.fred import SERIES, align_latest_common, net_liquidity


def test_net_liquidity_formula():
    assert net_liquidity(6_748_567, 800_000, 100_000) == 5_848_567


def test_align_picks_latest_date_present_in_all_series():
    series = {
        "WALCL": {"2026-08-05": 10.0, "2026-07-29": 9.0},
        "WTREGEN": {"2026-08-05": 2.0, "2026-07-29": 1.5},
        "RRPONTSYD": {"2026-07-29": 0.5},  # 8/5 없음
    }
    date, values = align_latest_common(series)
    assert date == "2026-07-29"
    assert values == {"WALCL": 9.0, "WTREGEN": 1.5, "RRPONTSYD": 0.5}


def test_align_raises_when_no_common_date():
    with pytest.raises(ValueError, match="공통"):
        align_latest_common({"A": {"2026-01-01": 1.0}, "B": {"2026-02-01": 2.0}})


def test_series_covers_net_liquidity_components():
    assert {"WALCL", "WTREGEN", "RRPONTSYD"} <= set(SERIES)


@pytest.mark.live
def test_live_macro_needs_key():
    from quant.adapters.env import get_key
    from quant.collect.sources.fred import fetch_macro

    if not get_key("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY 미설정")
    d = fetch_macro()
    assert d["net_liquidity"]["value"] > 0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_fred.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.fred'`

- [ ] **Step 3: `quant/collect/sources/fred.py` 구현**

```python
"""FRED — 순유동성·금융환경·금리.

순유동성(WALCL − TGA − RRP)은 발표 주기가 서로 다른 세 시계열의 차이라,
날짜를 맞추지 않고 빼면 값이 수천억 달러 단위로 튄다. align_latest_common 이
그 정합을 강제한다.
"""
from __future__ import annotations

from quant.adapters.env import get_key
from quant.adapters.http import client

BASE = "https://api.stlouisfed.org/fred"

SERIES = {
    "WALCL": "연준 총자산",
    "WTREGEN": "재무부 일반계정(TGA)",
    "RRPONTSYD": "역레포(RRP)",
    "NFCI": "시카고연준 금융환경지수",
    "DGS10": "미국 10년",
    "DGS2": "미국 2년",
}


def net_liquidity(walcl: float, tga: float, rrp: float) -> float:
    return walcl - tga - rrp


def align_latest_common(series: dict[str, dict[str, float]]) -> tuple[str, dict[str, float]]:
    common = set.intersection(*(set(v) for v in series.values())) if series else set()
    if not common:
        raise ValueError("시계열 간 공통 날짜가 없다 — 순유동성 계산 불가")
    latest = max(common)
    return latest, {k: v[latest] for k, v in series.items()}


def _observations(c, series_id: str, key: str, limit: int = 60) -> dict[str, float]:
    r = c.get(
        f"{BASE}/series/observations",
        params={
            "series_id": series_id, "api_key": key, "file_type": "json",
            "sort_order": "desc", "limit": limit,
        },
    )
    r.raise_for_status()
    return {
        o["date"]: float(o["value"])
        for o in r.json()["observations"]
        if o["value"] not in (".", "")
    }


def fetch_macro() -> dict:
    key = get_key("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정")
    with client(timeout=30.0) as c:
        obs = {sid: _observations(c, sid, key) for sid in SERIES}

    nl_date, nl = align_latest_common(
        {k: obs[k] for k in ("WALCL", "WTREGEN", "RRPONTSYD")}
    )
    latest = {
        sid: {"label": SERIES[sid], "date": max(v), "value": v[max(v)]}
        for sid, v in obs.items() if v
    }
    return {
        "series": latest,
        "net_liquidity": {
            "date": nl_date,
            "value": net_liquidity(nl["WALCL"], nl["WTREGEN"], nl["RRPONTSYD"]),
            "unit": "백만달러",
        },
    }
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_fred.py -v`
Expected: 4 passed, 1 deselected

- [ ] **Step 5: 라이브 확인**

Run: `uv run pytest tests/test_fred.py -m live -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/fred.py tests/test_fred.py && git commit -m "feat: FRED 순유동성·NFCI·금리 수집"
```

---

### Task 8: 이벤트 캘린더 — FRED 발표일정 + FOMC + D-day

**Files:**
- Create: `quant/collect/sources/calendar.py`, `tests/test_calendar.py`

**Interfaces:**
- Consumes: `quant.adapters.env.get_key`, `quant.adapters.http.client`, `quant.core.report_clock.{KST, ET}`
- Produces: `quant.collect.sources.calendar.fetch_calendar(today: date, horizon_days: int = 14) -> dict`, `quant.collect.sources.calendar.parse_fomc(html: str) -> list[dict]`, `quant.collect.sources.calendar.to_dday(events, today) -> list[dict]`, `HIGH_IMPACT: frozenset[str]`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_calendar.py`**

```python
from datetime import date
import pytest
from quant.collect.sources.calendar import HIGH_IMPACT, to_dday


def test_dday_sorts_ascending_and_labels():
    events = [
        {"name": "FOMC Meeting", "date": "2026-08-20"},
        {"name": "Consumer Price Index", "date": "2026-08-14"},
    ]
    out = to_dday(events, date(2026, 8, 12))
    assert [e["name"] for e in out] == ["Consumer Price Index", "FOMC Meeting"]
    assert out[0]["dday"] == "D-2"
    assert out[1]["dday"] == "D-8"


def test_dday_marks_today_and_tomorrow():
    out = to_dday(
        [{"name": "X", "date": "2026-08-12"}, {"name": "Y", "date": "2026-08-13"}],
        date(2026, 8, 12),
    )
    assert out[0]["dday"] == "D-DAY"
    assert out[1]["dday"] == "D-1"


def test_dday_drops_past_events():
    assert to_dday([{"name": "X", "date": "2026-08-01"}], date(2026, 8, 12)) == []


def test_high_impact_is_flagged():
    out = to_dday([{"name": "Consumer Price Index", "date": "2026-08-14"}], date(2026, 8, 12))
    assert out[0]["high_impact"] is True


def test_non_high_impact_not_flagged():
    out = to_dday([{"name": "Wholesale Trade", "date": "2026-08-14"}], date(2026, 8, 12))
    assert out[0]["high_impact"] is False


def test_high_impact_covers_market_movers():
    joined = " ".join(HIGH_IMPACT)
    for kw in ("Consumer Price Index", "Employment Situation", "FOMC", "Personal Income"):
        assert kw in joined


@pytest.mark.live
def test_live_calendar_returns_events():
    from quant.adapters.env import get_key
    from quant.collect.sources.calendar import fetch_calendar

    if not get_key("FRED_API_KEY"):
        pytest.skip("FRED_API_KEY 미설정")
    d = fetch_calendar(date.today())
    assert d["events"]
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_calendar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.calendar'`

- [ ] **Step 3: `quant/collect/sources/calendar.py` 구현**

```python
"""이벤트 캘린더 — 예정일을 미리 정리한다.

Investing.com(403)과 BLS(403)를 스크레이핑하지 않는다. 같은 정보를 FRED가
공식 API로 주고(releases/dates), FOMC는 연준 공식 페이지가 그대로 열린다.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from quant.adapters.env import get_key
from quant.adapters.http import client

FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

HIGH_IMPACT = frozenset({
    "Consumer Price Index",
    "Employment Situation",
    "Personal Income and Outlays",
    "Producer Price Index",
    "Gross Domestic Product",
    "Advance Monthly Sales for Retail and Food Services",
    "FOMC Meeting",
})


def to_dday(events: list[dict], today: date) -> list[dict]:
    out = []
    for e in events:
        d = date.fromisoformat(e["date"])
        delta = (d - today).days
        if delta < 0:
            continue
        out.append({
            **e,
            "dday": "D-DAY" if delta == 0 else f"D-{delta}",
            "days_ahead": delta,
            "high_impact": any(k in e["name"] for k in HIGH_IMPACT),
        })
    return sorted(out, key=lambda e: (e["days_ahead"], e["name"]))


def _fred_release_dates(c, key: str, today: date, horizon_days: int) -> list[dict]:
    r = c.get(FRED_DATES, params={
        "api_key": key, "file_type": "json",
        "realtime_start": today.isoformat(),
        "realtime_end": (today + timedelta(days=horizon_days)).isoformat(),
        "include_release_dates_with_no_data": "true",
        "limit": 1000,
    })
    r.raise_for_status()
    return [
        {"name": d["release_name"], "date": d["date"], "source": "FRED"}
        for d in r.json().get("release_dates", [])
    ]


def parse_fomc(html: str) -> list[dict]:
    """연준 캘린더에서 회의 날짜를 뽑는다. 'August 18-19, 2026' 형태를 종료일 기준으로 잡는다."""
    months = ("January February March April May June July August "
              "September October November December").split()
    events = []
    pattern = re.compile(
        r"(" + "|".join(months) + r")\s+(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?,?\s+(\d{4})"
    )
    for mon, d1, d2, year in pattern.findall(html):
        day = int(d2 or d1)
        try:
            events.append({
                "name": "FOMC Meeting",
                "date": date(int(year), months.index(mon) + 1, day).isoformat(),
                "source": "Federal Reserve",
            })
        except ValueError:
            continue
    seen, unique = set(), []
    for e in events:
        if e["date"] not in seen:
            seen.add(e["date"])
            unique.append(e)
    return unique


def fetch_calendar(today: date, horizon_days: int = 14) -> dict:
    key = get_key("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정")
    with client(timeout=30.0) as c:
        events = _fred_release_dates(c, key, today, horizon_days)
        try:
            events += parse_fomc(c.get(FOMC_URL).text)
        except Exception:
            pass  # FOMC 실패는 캘린더 전체를 죽이지 않는다
    horizon = to_dday(events, today)
    return {
        "horizon_days": horizon_days,
        "events": [e for e in horizon if e["days_ahead"] <= horizon_days],
    }
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_calendar.py -v`
Expected: 6 passed, 1 deselected

- [ ] **Step 5: 라이브 확인**

Run: `uv run pytest tests/test_calendar.py -m live -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/calendar.py tests/test_calendar.py && git commit -m "feat: FRED 발표일정 + FOMC 이벤트 캘린더 (D-day)"
```

---

### Task 9: 자체 계산 지표 — 옵션 P/C·MaxPain·금/구리

**Files:**
- Create: `quant/collect/sources/derived.py`, `tests/test_derived.py`

**Interfaces:**
- Consumes: 없음 (yfinance 직접)
- Produces: `quant.collect.sources.derived.put_call(calls: list[dict], puts: list[dict]) -> dict`, `quant.collect.sources.derived.max_pain(calls, puts) -> float`, `quant.collect.sources.derived.fetch_options(symbols: tuple[str, ...] = ("SPY", "QQQ")) -> dict`, `quant.collect.sources.derived.ratios(quotes: dict) -> dict`, `quant.collect.sources.derived.pct_above_ma(closes_by_symbol: dict[str, list[float]], window: int = 200) -> dict`, `quant.collect.sources.derived.fetch_breadth() -> dict`

> 회사 리포트는 이걸 유료 EODHD로 받는다. 우리는 옵션 체인에서 직접 계산한다.
> MaxPain = 만기 시 콜·풋 내재가치 합계가 최소가 되는 행사가.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_derived.py`**

```python
import pytest
from quant.collect.sources.derived import max_pain, put_call, ratios


def test_put_call_ratios():
    calls = [{"strike": 100, "volume": 10, "openInterest": 100}]
    puts = [{"strike": 100, "volume": 20, "openInterest": 50}]
    out = put_call(calls, puts)
    assert out["volume_pc"] == 2.0
    assert out["oi_pc"] == 0.5


def test_put_call_handles_zero_calls():
    out = put_call([], [{"strike": 100, "volume": 5, "openInterest": 5}])
    assert out["volume_pc"] is None and out["oi_pc"] is None


def test_max_pain_picks_min_total_intrinsic():
    # OI가 콜 110에 몰려 있으면 고통 최소점은 그 아래다
    calls = [{"strike": 100, "openInterest": 0}, {"strike": 110, "openInterest": 1000}]
    puts = [{"strike": 100, "openInterest": 0}, {"strike": 110, "openInterest": 0}]
    assert max_pain(calls, puts) == 100


def test_max_pain_balances_both_sides():
    calls = [{"strike": 90, "openInterest": 100}, {"strike": 110, "openInterest": 100}]
    puts = [{"strike": 90, "openInterest": 100}, {"strike": 110, "openInterest": 100}]
    assert max_pain(calls, puts) in (90, 110)


def test_max_pain_empty_returns_none():
    assert max_pain([], []) is None


def test_ratios_computes_gold_copper_and_brent_wti():
    out = ratios({"GC=F": {"close": 4461.3}, "HG=F": {"close": 5.0},
                  "BZ=F": {"close": 86.0}, "CL=F": {"close": 83.8}})
    assert out["gold_copper"] == pytest.approx(892.26, rel=1e-3)
    assert out["brent_wti_spread"] == pytest.approx(2.2, rel=1e-3)


def test_ratios_skips_missing_symbols():
    assert ratios({"GC=F": {"close": 100.0}}) == {}


@pytest.mark.live
def test_live_options():
    from quant.collect.sources.derived import fetch_options

    d = fetch_options(("SPY",))
    assert d["SPY"]["volume_pc"] is not None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_derived.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.derived'`

- [ ] **Step 3: `quant/collect/sources/derived.py` 구현**

```python
"""자체 계산 지표 — 유료 벤더 없이 만드는 것들.

옵션 P/C와 MaxPain은 회사 리포트가 EODHD(유료)로 받는 값이다. 옵션 체인이
무료로 열려 있으므로 직접 계산한다.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", module="yfinance")


def _sum(rows: list[dict], field: str) -> float:
    return float(sum(r.get(field) or 0 for r in rows))


def put_call(calls: list[dict], puts: list[dict]) -> dict:
    cv, pv = _sum(calls, "volume"), _sum(puts, "volume")
    co, po = _sum(calls, "openInterest"), _sum(puts, "openInterest")
    return {
        "call_volume": cv, "put_volume": pv,
        "call_oi": co, "put_oi": po,
        "volume_pc": round(pv / cv, 3) if cv else None,
        "oi_pc": round(po / co, 3) if co else None,
    }


def max_pain(calls: list[dict], puts: list[dict]) -> float | None:
    """만기 시 옵션 보유자 총 내재가치가 최소가 되는 행사가."""
    strikes = sorted({r["strike"] for r in (*calls, *puts)})
    if not strikes:
        return None
    best, best_pain = None, None
    for s in strikes:
        pain = sum(max(s - c["strike"], 0) * (c.get("openInterest") or 0) for c in calls)
        pain += sum(max(p["strike"] - s, 0) * (p.get("openInterest") or 0) for p in puts)
        if best_pain is None or pain < best_pain:
            best, best_pain = s, pain
    return best


def fetch_options(symbols: tuple[str, ...] = ("SPY", "QQQ")) -> dict:
    import yfinance as yf

    out: dict[str, dict] = {}
    for sym in symbols:
        t = yf.Ticker(sym)
        expiries = t.options
        if not expiries:
            continue
        # 당일 만기는 OI가 0으로 정산돼 있어 MaxPain이 무의미하다 — 다음 만기를 쓴다
        expiry = expiries[1] if len(expiries) > 1 else expiries[0]
        chain = t.option_chain(expiry)
        calls = chain.calls.to_dict("records")
        puts = chain.puts.to_dict("records")
        out[sym] = {"expiry": expiry, **put_call(calls, puts), "max_pain": max_pain(calls, puts)}
    return out


def ratios(quotes: dict) -> dict:
    """원자재 파생 지표. 필요한 심볼이 없으면 해당 항목을 조용히 생략한다."""
    out: dict[str, float] = {}

    def close(sym: str) -> float | None:
        v = quotes.get(sym)
        return float(v["close"]) if v and v.get("close") else None

    gold, copper = close("GC=F"), close("HG=F")
    if gold and copper:
        out["gold_copper"] = round(gold / copper, 2)
    brent, wti = close("BZ=F"), close("CL=F")
    if brent and wti:
        out["brent_wti_spread"] = round(brent - wti, 2)
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_derived.py -v`
Expected: 7 passed, 1 deselected

- [ ] **Step 5: S5FI 실패 테스트 추가 — `tests/test_derived.py` 하단에 덧붙인다**

S5FI = S&P 500 구성종목 중 200일 이동평균 위에 있는 비율. 회사 리포트는 이걸
TradingView Playwright 스크레이핑으로 얻는다 — 우리는 종가에서 직접 센다.

```python
def test_pct_above_ma_counts_correctly():
    closes = {
        "A": [1.0] * 199 + [100.0],   # 200MA 위
        "B": [100.0] * 199 + [1.0],   # 200MA 아래
    }
    out = pct_above_ma(closes, window=200)
    assert out["value"] == 50.0
    assert out["counted"] == 2


def test_pct_above_ma_skips_short_series():
    closes = {"A": [1.0] * 199 + [100.0], "SHORT": [1.0, 2.0]}
    out = pct_above_ma(closes, window=200)
    assert out["counted"] == 1
    assert out["skipped"] == 1


def test_pct_above_ma_all_empty_returns_none():
    out = pct_above_ma({}, window=200)
    assert out["value"] is None and out["counted"] == 0


@pytest.mark.live
def test_live_breadth():
    from quant.collect.sources.derived import fetch_breadth

    d = fetch_breadth()
    assert 0 <= d["value"] <= 100
    assert d["counted"] > 400
```

`pct_above_ma`를 import 목록에 추가한다: `from quant.collect.sources.derived import max_pain, pct_above_ma, put_call, ratios`

- [ ] **Step 6: 실패 확인**

Run: `uv run pytest tests/test_derived.py -k pct_above -v`
Expected: FAIL — `ImportError: cannot import name 'pct_above_ma'`

- [ ] **Step 7: `quant/collect/sources/derived.py`에 S5FI 추가**

파일 끝에 덧붙인다:

```python
SP500_LIST_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"


def pct_above_ma(closes_by_symbol: dict[str, list[float]], window: int = 200) -> dict:
    """200일선 위 종목 비중. 데이터가 짧은 종목은 세지 않고 skipped 로 남긴다 —
    조용히 분모에서 빼면 값이 왜곡되고 아무도 눈치채지 못한다."""
    above = counted = skipped = 0
    for closes in closes_by_symbol.values():
        if len(closes) < window:
            skipped += 1
            continue
        ma = sum(closes[-window:]) / window
        counted += 1
        if closes[-1] > ma:
            above += 1
    return {
        "value": round(above / counted * 100, 1) if counted else None,
        "above": above,
        "counted": counted,
        "skipped": skipped,
        "window": window,
    }


def fetch_breadth() -> dict:
    """S&P 500 구성종목을 일괄 조회해 S5FI를 계산한다.

    500종목 × 1년치라 한 번에 30~90초 걸린다. 하루 한 번이므로 감수하되,
    실패해도 SourceResult(ok=False)로 격리돼 리포트는 발행된다.
    """
    import pandas as pd
    import yfinance as yf

    symbols = [
        s.replace(".", "-")  # BRK.B -> BRK-B (야후 표기)
        for s in pd.read_html(SP500_LIST_URL)[0]["Symbol"].tolist()
    ]
    df = yf.download(symbols, period="1y", progress=False, auto_adjust=False, threads=True)["Close"]
    closes = {
        sym: df[sym].dropna().tolist() for sym in df.columns if not df[sym].dropna().empty
    }
    out = pct_above_ma(closes)
    if out["value"] is None:
        raise ValueError("S5FI 계산 실패 — 유효 종목 0개")
    return {"s5fi": out, "universe": len(symbols)}
```

- [ ] **Step 8: 통과 확인**

Run: `uv run pytest tests/test_derived.py -v`
Expected: 10 passed, 2 deselected

- [ ] **Step 9: 라이브 확인 (30~90초 소요)**

Run: `uv run pytest tests/test_derived.py -m live -v`
Expected: PASS

- [ ] **Step 10: 커밋**

```bash
git add quant/collect/sources/derived.py tests/test_derived.py && git commit -m "feat: 옵션 P/C·MaxPain·원자재 비율·S5FI 자체 계산"
```

---

### Task 10: 원자재 포지셔닝·재고 (CFTC + EIA)

**Files:**
- Create: `quant/collect/sources/commodity.py`, `tests/test_commodity.py`

**Interfaces:**
- Consumes: `quant.adapters.env.get_key`, `quant.adapters.http.client`
- Produces: `quant.collect.sources.commodity.fetch_cot() -> dict`, `quant.collect.sources.commodity.fetch_crude_stocks() -> dict`, `quant.collect.sources.commodity.percentile(series: list[float], value: float) -> float`, `COT_MARKETS: dict[str, str]`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_commodity.py`**

```python
import pytest
from quant.collect.sources.commodity import COT_MARKETS, percentile


def test_percentile_at_extremes():
    assert percentile([1, 2, 3, 4, 5], 5) == 100.0
    assert percentile([1, 2, 3, 4, 5], 1) == 20.0


def test_percentile_middle():
    assert percentile([10, 20, 30, 40], 25) == 50.0


def test_percentile_empty_returns_none():
    assert percentile([], 5) is None


def test_percentile_above_all_history():
    assert percentile([1, 2, 3], 99) == 100.0


def test_cot_markets_cover_energy_and_metals():
    joined = " ".join(COT_MARKETS.values()).lower()
    assert "crude" in joined and "gold" in joined


@pytest.mark.live
def test_live_cot():
    from quant.collect.sources.commodity import fetch_cot

    assert fetch_cot()["markets"]


@pytest.mark.live
def test_live_crude_stocks():
    from quant.adapters.env import get_key
    from quant.collect.sources.commodity import fetch_crude_stocks

    if not get_key("EIA_API_KEY"):
        pytest.skip("EIA_API_KEY 미설정")
    assert fetch_crude_stocks()["latest"] is not None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_commodity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.commodity'`

- [ ] **Step 3: `quant/collect/sources/commodity.py` 구현**

```python
"""원자재 흐름 — 가격만으로는 흐름이 안 보이므로 포지셔닝과 재고를 함께 싣는다.

COT 순포지션은 절대 계약수보다 **최근 3년 대비 백분위**가 신호다 — 극단 여부가
중요하지 크기가 중요한 게 아니다.
"""
from __future__ import annotations

from quant.adapters.env import get_key
from quant.adapters.http import client

COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
EIA_URL = "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"

COT_MARKETS = {
    "067651": "GOLD - COMMODITY EXCHANGE INC.",
    "067411": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
    "084691": "COPPER- #1 - COMMODITY EXCHANGE INC.",
}


def percentile(series: list[float], value: float) -> float | None:
    if not series:
        return None
    below = sum(1 for x in series if x <= value)
    return round(below / len(series) * 100, 1)


def fetch_cot() -> dict:
    """시장별 투기적 순포지션 + 3년 백분위. 주간 데이터라 156주를 본다."""
    out: dict[str, dict] = {}
    with client(timeout=30.0) as c:
        for code, name in COT_MARKETS.items():
            r = c.get(COT_URL, params={
                "cftc_contract_market_code": code,
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": 156,
            })
            r.raise_for_status()
            rows = r.json()
            if not rows:
                continue
            nets = [
                float(x["noncomm_positions_long_all"]) - float(x["noncomm_positions_short_all"])
                for x in rows
                if x.get("noncomm_positions_long_all") and x.get("noncomm_positions_short_all")
            ]
            if not nets:
                continue
            out[name] = {
                "as_of": rows[0]["report_date_as_yyyy_mm_dd"][:10],
                "net_spec": nets[0],
                "percentile_3y": percentile(nets, nets[0]),
                "weeks": len(nets),
            }
    if not out:
        raise ValueError("COT 데이터 0건")
    return {"markets": out}


def fetch_crude_stocks() -> dict:
    key = get_key("EIA_API_KEY")
    if not key:
        raise RuntimeError("EIA_API_KEY 미설정")
    with client(timeout=30.0) as c:
        r = c.get(EIA_URL, params={
            "api_key": key, "frequency": "weekly", "data[0]": "value",
            "sort[0][column]": "period", "sort[0][direction]": "desc", "length": 12,
        })
        r.raise_for_status()
        rows = r.json()["response"]["data"]
    if not rows:
        raise ValueError("EIA 재고 데이터 0건")
    latest, prev = rows[0], rows[1] if len(rows) > 1 else rows[0]
    return {
        "latest": {"period": latest["period"], "value": latest["value"]},
        "delta": float(latest["value"]) - float(prev["value"]),
        "unit": latest.get("units", ""),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_commodity.py -v`
Expected: 5 passed, 2 deselected

- [ ] **Step 5: 라이브 확인**

Run: `uv run pytest tests/test_commodity.py -m live -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/commodity.py tests/test_commodity.py && git commit -m "feat: CFTC COT 포지셔닝 + EIA 원유재고"
```

---

### Task 11: 뉴스·유튜브 RSS

**Files:**
- Create: `quant/collect/sources/feeds.py`, `tests/test_feeds.py`, `tests/fixtures/sample_feed.xml`

**Interfaces:**
- Consumes: `quant.adapters.http.client`
- Produces: `quant.collect.sources.feeds.parse_feed(xml: str, limit: int) -> list[dict]`, `quant.collect.sources.feeds.fetch_news(market: str) -> dict`, `quant.collect.sources.feeds.fetch_youtube(market: str) -> dict`, `NEWS_FEEDS: dict[str, dict[str, str]]`, `YOUTUBE_CHANNELS: dict[str, dict[str, str]]`

> 출처명·제목·링크·게시시각을 모두 보존한다 — 리포트에 링크로 렌더해야 한다.

- [ ] **Step 1: 픽스처 저장**

```bash
cd ~/Documents/GitHub/market_report && curl -s -A "Mozilla/5.0" \
  "https://www.youtube.com/feeds/videos.xml?channel_id=UCnpekFV93kB1O0rVqEKSumg" \
  -o tests/fixtures/sample_feed.xml && wc -c tests/fixtures/sample_feed.xml
```

- [ ] **Step 2: 실패하는 테스트 작성 — `tests/test_feeds.py`**

```python
from pathlib import Path
import pytest
from quant.collect.sources.feeds import NEWS_FEEDS, YOUTUBE_CHANNELS, parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


def test_parse_atom_feed_extracts_entries():
    items = parse_feed(FIXTURE.read_text(), limit=5)
    assert 0 < len(items) <= 5
    first = items[0]
    assert first["title"] and first["link"].startswith("http")
    assert "published" in first


def test_parse_respects_limit():
    assert len(parse_feed(FIXTURE.read_text(), limit=2)) == 2


def test_parse_rss_2_0():
    xml = """<?xml version="1.0"?><rss><channel>
    <item><title>제목A</title><link>https://a.test/1</link>
    <pubDate>Tue, 11 Aug 2026 09:00:00 +0900</pubDate></item>
    </channel></rss>"""
    items = parse_feed(xml, limit=5)
    assert items[0]["title"] == "제목A"
    assert items[0]["link"] == "https://a.test/1"


def test_parse_unescapes_entities():
    xml = ("""<?xml version="1.0"?><rss><channel><item>"""
           """<title>A &amp; B</title><link>https://a.test/1</link></item>"""
           """</channel></rss>""")
    assert parse_feed(xml, limit=1)[0]["title"] == "A & B"


def test_parse_malformed_returns_empty():
    assert parse_feed("<not-xml", limit=5) == []


def test_both_markets_have_feeds_and_channels():
    assert set(NEWS_FEEDS) == {"KR", "US"} == set(YOUTUBE_CHANNELS)
    assert all(v for v in NEWS_FEEDS.values())


@pytest.mark.live
def test_live_youtube_us():
    from quant.collect.sources.feeds import fetch_youtube

    assert fetch_youtube("US")["channels"]
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/test_feeds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.collect.sources.feeds'`

- [ ] **Step 4: `quant/collect/sources/feeds.py` 구현**

```python
"""뉴스·유튜브 RSS. 둘 다 API 키가 필요 없다.

출처명·제목·링크·게시시각을 모두 보존한다 — 리포트에 링크로 렌더해야 하기 때문이다.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from quant.adapters.http import client

NEWS_FEEDS: dict[str, dict[str, str]] = {
    "KR": {
        "한국경제": "https://www.hankyung.com/feed/economy",
        "매일경제": "https://www.mk.co.kr/rss/30100041/",
        "구글_코스피": "https://news.google.com/rss/search?q=코스피+when:1d&hl=ko&gl=KR&ceid=KR:ko",
        "구글_환율": "https://news.google.com/rss/search?q=환율+달러+when:1d&hl=ko&gl=KR&ceid=KR:ko",
    },
    "US": {
        "BBC_Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "CNBC_Economy": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
        "Guardian_Business": "https://www.theguardian.com/uk/business/rss",
        "구글_Reuters": "https://news.google.com/rss/search?q=reuters+markets+when:1d&hl=en-US&gl=US&ceid=US:en",
    },
}

YOUTUBE_CHANNELS: dict[str, dict[str, str]] = {
    # 채널 ID는 채널 페이지 소스의 `channelId` 값이다. KR 채널은 Phase 1 검증 중
    # 사용자가 원하는 채널을 확인해 추가한다 — 빈 dict 여도 정상 동작한다.
    "US": {
        "Aswath Damodaran": "UCnpekFV93kB1O0rVqEKSumg",
        "Bloomberg Television": "UCIALMKvObZNtJ6AmdCLP7Lg",
    },
    "KR": {},
}

_FEED_LIMIT = 8
_YT_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


def _text(el, *paths: str) -> str:
    for p in paths:
        found = el.find(p)
        if found is not None:
            if found.text:
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""


def parse_feed(xml: str, limit: int) -> list[dict]:
    """RSS 2.0과 Atom을 모두 받는다. 깨진 XML은 빈 목록 — 소스 하나가 리포트를 막지 않는다."""
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return []
    ns = "{http://www.w3.org/2005/Atom}"
    entries = root.findall(f".//{ns}entry") or root.findall(".//item")
    out = []
    for e in entries[:limit]:
        title = _text(e, f"{ns}title", "title")
        link = _text(e, f"{ns}link", "link")
        published = _text(e, f"{ns}published", "pubDate", f"{ns}updated")
        if title and link:
            out.append({"title": title, "link": link, "published": published})
    return out


def _fetch_many(feeds: dict[str, str]) -> dict:
    out: dict[str, list[dict]] = {}
    with client(timeout=15.0) as c:
        for name, url in feeds.items():
            try:
                out[name] = parse_feed(c.get(url).text, _FEED_LIMIT)
            except Exception:
                out[name] = []  # 피드 하나 실패가 섹션 전체를 죽이지 않는다
    return out


def fetch_news(market: str) -> dict:
    return {"feeds": _fetch_many(NEWS_FEEDS[market])}


def fetch_youtube(market: str) -> dict:
    feeds = {n: _YT_URL.format(cid=c) for n, c in YOUTUBE_CHANNELS[market].items()}
    return {"channels": _fetch_many(feeds)}
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_feeds.py -v`
Expected: 6 passed, 1 deselected

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/feeds.py tests/test_feeds.py tests/fixtures/sample_feed.xml && git commit -m "feat: 뉴스·유튜브 RSS 수집"
```

---

### Task 12: 소스 레지스트리 + 스냅샷 저장

**Files:**
- Create: `quant/collect/sources/__init__.py`, `tests/test_registry.py`
- Modify: `quant/collect/snapshot.py` (스냅샷 저장 함수 추가)

**Interfaces:**
- Consumes: 모든 `quant.collect.sources.*` 모듈, `quant.collect.contracts.Snapshot`
- Produces: `quant.collect.sources.build_sources(market: str, session_date: date) -> dict[str, tuple[str, Callable[[], dict]]]`, `quant.collect.snapshot.save_snapshot(snap: Snapshot, root: Path) -> Path`, `quant.collect.snapshot.load_snapshot(path: Path) -> Snapshot`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_registry.py`**

```python
from datetime import date
from pathlib import Path
import pytest
from quant.collect.snapshot import collect, load_snapshot, save_snapshot
from quant.collect.sources import build_sources


@pytest.mark.parametrize("market", ["KR", "US"])
def test_build_sources_returns_callables(market):
    srcs = build_sources(market, date(2026, 8, 12))
    assert srcs
    for key, (url, fn) in srcs.items():
        assert isinstance(url, str) and url.startswith("http"), key
        assert callable(fn), key


def test_kr_includes_investor_flow_and_us_does_not():
    kr = build_sources("KR", date(2026, 8, 12))
    us = build_sources("US", date(2026, 8, 12))
    assert any("flow" in k for k in kr)
    assert not any("flow" in k for k in us)


def test_both_include_calendar_and_market():
    for m in ("KR", "US"):
        keys = build_sources(m, date(2026, 8, 12))
        assert "calendar" in keys and "market" in keys


def test_save_and_load_roundtrip(tmp_path: Path):
    snap = collect("KR", date(2026, 8, 12), {"g": ("https://x.test", lambda: {"a": 1})})
    path = save_snapshot(snap, tmp_path)
    assert path.exists()
    assert load_snapshot(path) == snap


def test_save_path_layout(tmp_path: Path):
    snap = collect("US", date(2026, 8, 12), {"g": ("https://x.test", lambda: {"a": 1})})
    path = save_snapshot(snap, tmp_path)
    assert path.relative_to(tmp_path).as_posix() == "US/2026-08-12.json"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'build_sources'`

- [ ] **Step 3: `quant/collect/sources/__init__.py` 구현**

```python
"""시장별 소스 레지스트리.

각 항목은 `key -> (출처 URL, 무인자 호출가능객체)`다. URL은 리포트에 출처로
그대로 렌더되므로 실제 조회 주소를 넣는다.
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from quant.collect.sources import calendar, commodity, derived, feeds, fred, market, naver_flow


def build_sources(
    market_code: str, session_date: date
) -> dict[str, tuple[str, Callable[[], dict]]]:
    bizdate = session_date.strftime("%Y%m%d")

    common: dict[str, tuple[str, Callable[[], dict]]] = {
        "market": ("https://finance.yahoo.com", lambda: market.fetch_quotes(market_code)),
        "calendar": (calendar.FRED_DATES, lambda: calendar.fetch_calendar(session_date)),
        "macro": ("https://api.stlouisfed.org/fred", fred.fetch_macro),
        "cot": (commodity.COT_URL, commodity.fetch_cot),
        "news": ("https://news.google.com", lambda: feeds.fetch_news(market_code)),
        "youtube": ("https://www.youtube.com", lambda: feeds.fetch_youtube(market_code)),
    }

    if market_code == "KR":
        return {
            **common,
            "kospi_flow": (
                naver_flow.URL_TEMPLATE.format(bizdate=bizdate, sosok="01"),
                lambda: naver_flow.fetch_flow("01", bizdate),
            ),
            "kosdaq_flow": (
                naver_flow.URL_TEMPLATE.format(bizdate=bizdate, sosok="02"),
                lambda: naver_flow.fetch_flow("02", bizdate),
            ),
        }
    return {
        **common,
        "options": ("https://finance.yahoo.com", derived.fetch_options),
        "breadth": (derived.SP500_LIST_URL, derived.fetch_breadth),
        "crude_stocks": (commodity.EIA_URL, commodity.fetch_crude_stocks),
    }
```

- [ ] **Step 4: `quant/collect/snapshot.py`에 저장/로드 추가**

파일 끝에 덧붙인다:

```python
from pathlib import Path


def save_snapshot(snap: Snapshot, root: Path) -> Path:
    path = root / snap.market / f"{snap.session_date.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snap.to_json(), encoding="utf-8")
    return path


def load_snapshot(path: Path) -> Snapshot:
    return Snapshot.from_json(path.read_text(encoding="utf-8"))
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_registry.py -v`
Expected: 6 passed

- [ ] **Step 6: 커밋**

```bash
git add quant/collect/sources/__init__.py quant/collect/snapshot.py tests/test_registry.py && git commit -m "feat: 시장별 소스 레지스트리 + 스냅샷 저장/로드"
```

---

### Task 13: HTML 렌더링

**Files:**
- Create: `quant/analyze/render.py`, `quant/analyze/templates/report.html.j2`, `tests/test_render.py`

**Interfaces:**
- Consumes: `quant.collect.contracts.Snapshot`
- Produces: `quant.analyze.render.render(snap: Snapshot) -> str`, `quant.analyze.render.write_html(snap: Snapshot, root: Path) -> Path`

> 렌더러는 **스냅샷만 읽는다.** 네트워크를 타지 않으므로 같은 스냅샷 → 항상 같은 HTML이다.
> 결측 섹션은 숨기지 않고 사유와 함께 표시한다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_render.py`**

```python
from datetime import date, datetime
from pathlib import Path
from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.render import render, write_html


def _snap(**overrides) -> Snapshot:
    results = {
        "market": SourceResult(
            key="market", ok=True,
            data={"quotes": {"^KS11": {"label": "KOSPI", "close": 6345.5,
                                       "prev": 6300.0, "change_pct": 0.72}},
                  "crosscheck": {"checked": ["^KS11"], "warnings": []}},
            error=None, url="https://finance.yahoo.com",
            fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST), latency_ms=100,
        ),
        "calendar": SourceResult(
            key="calendar", ok=True,
            data={"horizon_days": 14, "events": [
                {"name": "Consumer Price Index", "date": "2026-08-14",
                 "dday": "D-2", "days_ahead": 2, "high_impact": True, "source": "FRED"}]},
            error=None, url="https://api.stlouisfed.org/fred/releases/dates",
            fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST), latency_ms=200,
        ),
        "macro": SourceResult(
            key="macro", ok=False, data=None, error="RuntimeError: FRED_API_KEY 미설정",
            url="https://api.stlouisfed.org/fred",
            fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST), latency_ms=5,
        ),
    }
    results.update(overrides)
    return Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=date(2026, 8, 12),
        generated_at=datetime(2026, 8, 12, 8, 0, tzinfo=KST), results=results,
    )


def test_render_is_deterministic():
    assert render(_snap()) == render(_snap())


def test_render_includes_quotes():
    html = render(_snap())
    assert "KOSPI" in html and "6,345.5" in html


def test_render_shows_missing_section_with_reason():
    html = render(_snap())
    assert "결측" in html
    assert "FRED_API_KEY 미설정" in html


def test_render_shows_calendar_dday():
    html = render(_snap())
    assert "D-2" in html and "Consumer Price Index" in html


def test_render_escapes_source_text():
    evil = SourceResult(
        key="news", ok=True,
        data={"feeds": {"X": [{"title": "<script>alert(1)</script>",
                               "link": "https://a.test", "published": ""}]}},
        error=None, url="https://news.google.com",
        fetched_at=datetime(2026, 8, 12, 7, 59, tzinfo=KST), latency_ms=10,
    )
    html = render(_snap(news=evil))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_write_html_path_layout(tmp_path: Path):
    path = write_html(_snap(), tmp_path)
    assert path.relative_to(tmp_path).as_posix() == "2026/08/12/KR_report.html"
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_render.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.analyze.render'`

- [ ] **Step 3: `quant/analyze/templates/report.html.j2` 작성**

```jinja
<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ snap.market }} 시황 리포트 — {{ snap.session_date }}</title>
<style>
:root{--bg:#f7f8fa;--card:#fff;--line:#e2e8f0;--tx:#1a202c;--mut:#718096;
      --up:#e53e3e;--dn:#3182ce;--warn:#dd6b20}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"Noto Sans KR",sans-serif;background:var(--bg);
     color:var(--tx);line-height:1.6;font-size:14px}
.wrap{max-width:1000px;margin:0 auto;padding:32px 20px}
h1{font-size:26px;margin-bottom:4px}
.meta{color:var(--mut);font-size:12px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:20px 24px;margin-bottom:18px}
h2{font-size:17px;margin-bottom:14px;padding-bottom:8px;border-bottom:2px solid var(--tx)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:var(--tx);color:#fff;padding:8px 10px;text-align:left;font-weight:500}
td{padding:8px 10px;border-bottom:1px solid var(--line)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.up{color:var(--up)}.dn{color:var(--dn)}
.missing{border-left:4px solid var(--warn);background:#fffaf0}
.missing .why{color:var(--warn);font-size:12.5px;font-family:ui-monospace,monospace}
.hi{font-weight:700}
.dday{display:inline-block;min-width:52px;padding:2px 8px;border-radius:4px;
      background:var(--tx);color:#fff;font-size:11px;text-align:center}
.dday.hi{background:var(--warn)}
.src{color:var(--mut);font-size:11px;margin-top:10px;word-break:break-all}
ul{margin-left:18px}
a{color:#2b6cb0;text-decoration:none}a:hover{text-decoration:underline}
</style></head><body><div class="wrap">

<h1>{{ snap.market }} 시황 리포트</h1>
<div class="meta">
  세션 {{ snap.session_date }} · 생성 {{ snap.generated_at.strftime('%Y-%m-%d %H:%M %Z') }}
  · 스키마 v{{ snap.schema_version }}
  {%- if snap.missing() %} · <b>결측 {{ snap.missing()|length }}건</b>{% endif %}
</div>

{%- macro section(key, title) %}
{%- set r = snap.results.get(key) %}
{%- if r is none %}{% elif not r.ok %}
<div class="card missing"><h2>{{ title }}</h2>
  <div class="why">결측 — {{ r.error }}</div>
  <div class="src">{{ r.url }}</div></div>
{%- else %}
<div class="card"><h2>{{ title }}</h2>{{ caller(r.data) }}
  <div class="src">출처 {{ r.url }} · {{ r.fetched_at.strftime('%H:%M:%S') }}
    · {{ r.latency_ms }}ms</div></div>
{%- endif %}
{%- endmacro %}

{% call(d) section('calendar', '다가오는 이벤트') %}
<table><tr><th>D-day</th><th>이벤트</th><th>날짜</th></tr>
{%- for e in d.events %}
<tr><td><span class="dday{% if e.high_impact %} hi{% endif %}">{{ e.dday }}</span></td>
    <td{% if e.high_impact %} class="hi"{% endif %}>{{ e.name }}</td>
    <td>{{ e.date }}</td></tr>
{%- endfor %}</table>
{% endcall %}

{% call(d) section('market', '시세') %}
<table><tr><th>종목</th><th class="num">종가</th><th class="num">전일</th><th class="num">등락</th></tr>
{%- for sym, q in d.quotes.items() %}
<tr><td>{{ q.label }} <span style="color:var(--mut)">{{ sym }}</span></td>
    <td class="num">{{ '{:,.2f}'.format(q.close) }}</td>
    <td class="num">{{ '{:,.2f}'.format(q.prev) }}</td>
    <td class="num {{ 'up' if q.change_pct > 0 else 'dn' }}">
      {{ '{:+.2f}'.format(q.change_pct) }}%</td></tr>
{%- endfor %}</table>
{%- if d.crosscheck.warnings %}
<p style="color:var(--warn);margin-top:10px">⚠ 2차 소스 괴리:
{% for w in d.crosscheck.warnings %}{{ w }}{% if not loop.last %} / {% endif %}{% endfor %}</p>
{%- endif %}
{% endcall %}

{% call(d) section('kospi_flow', '투자자 수급 — KOSPI (억원)') %}
{{ flow_table(d) }}
{% endcall %}

{% call(d) section('kosdaq_flow', '투자자 수급 — KOSDAQ (억원)') %}
{{ flow_table(d) }}
{% endcall %}

{% call(d) section('macro', '유동성·금리') %}
<p><b>순유동성</b> {{ '{:,.0f}'.format(d.net_liquidity.value) }}
   {{ d.net_liquidity.unit }} ({{ d.net_liquidity.date }})</p>
<table><tr><th>지표</th><th class="num">값</th><th>기준일</th></tr>
{%- for sid, s in d.series.items() %}
<tr><td>{{ s.label }}</td><td class="num">{{ '{:,.2f}'.format(s.value) }}</td>
    <td>{{ s.date }}</td></tr>
{%- endfor %}</table>
{% endcall %}

{% call(d) section('options', '옵션 포지셔닝') %}
<table><tr><th>종목</th><th>만기</th><th class="num">Vol P/C</th>
<th class="num">OI P/C</th><th class="num">MaxPain</th></tr>
{%- for sym, o in d.items() %}
<tr><td>{{ sym }}</td><td>{{ o.expiry }}</td>
    <td class="num">{{ o.volume_pc if o.volume_pc is not none else '—' }}</td>
    <td class="num">{{ o.oi_pc if o.oi_pc is not none else '—' }}</td>
    <td class="num">{{ o.max_pain if o.max_pain is not none else '—' }}</td></tr>
{%- endfor %}</table>
{% endcall %}

{% call(d) section('breadth', '시장 폭 — S5FI') %}
<p><b>{{ d.s5fi.value }}%</b> 의 S&P 500 종목이 {{ d.s5fi.window }}일선 위
  ({{ d.s5fi.above }}/{{ d.s5fi.counted }}종목
  {%- if d.s5fi.skipped %}, 데이터 부족 {{ d.s5fi.skipped }}종목 제외{% endif %})</p>
{% endcall %}

{% call(d) section('cot', '원자재 포지셔닝 (CFTC COT)') %}
<table><tr><th>시장</th><th class="num">투기 순포지션</th>
<th class="num">3년 백분위</th><th>기준일</th></tr>
{%- for name, m in d.markets.items() %}
<tr><td>{{ name }}</td><td class="num">{{ '{:,.0f}'.format(m.net_spec) }}</td>
    <td class="num">{{ m.percentile_3y }}%</td><td>{{ m.as_of }}</td></tr>
{%- endfor %}</table>
{% endcall %}

{% call(d) section('crude_stocks', '원유 재고') %}
<p>{{ d.latest.period }} — {{ '{:,.0f}'.format(d.latest.value|float) }} {{ d.unit }}
   (전주 대비 {{ '{:+,.0f}'.format(d.delta) }})</p>
{% endcall %}

{% call(d) section('news', '뉴스') %}
{%- for name, items in d.feeds.items() %}
<h3 style="font-size:14px;margin:14px 0 6px">{{ name }}</h3>
{%- if items %}<ul>{% for i in items %}
<li><a href="{{ i.link }}" target="_blank" rel="noopener">{{ i.title }}</a>
{% if i.published %}<span style="color:var(--mut);font-size:11px">{{ i.published }}</span>{% endif %}</li>
{% endfor %}</ul>{% else %}<p style="color:var(--mut)">수집 실패</p>{% endif %}
{%- endfor %}
{% endcall %}

{% call(d) section('youtube', '영상') %}
{%- for name, items in d.channels.items() %}
<h3 style="font-size:14px;margin:14px 0 6px">{{ name }}</h3>
{%- if items %}<ul>{% for i in items %}
<li><a href="{{ i.link }}" target="_blank" rel="noopener">{{ i.title }}</a>
{% if i.published %}<span style="color:var(--mut);font-size:11px">{{ i.published }}</span>{% endif %}</li>
{% endfor %}</ul>{% else %}<p style="color:var(--mut)">수집 실패</p>{% endif %}
{%- endfor %}
{% endcall %}

</div></body></html>
```

- [ ] **Step 4: `quant/analyze/render.py` 구현**

```python
"""Snapshot → HTML.

렌더러는 **스냅샷만 읽는다** — 네트워크를 타지 않으므로 같은 스냅샷은 항상 같은
HTML을 낸다(재현성 검증의 근거). 결측 섹션은 숨기지 않고 사유와 함께 표시한다.
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from quant.collect.contracts import Snapshot

_TEMPLATES = Path(__file__).resolve().parent / "templates"


def _flow_table(data: dict) -> str:
    from markupsafe import Markup, escape

    cols = ("개인", "외국인", "기관계", "금융투자", "투신", "연기금등", "기타법인")
    head = "".join(f'<th class="num">{escape(c)}</th>' for c in cols)
    body = []
    for row in data["rows"][:5]:
        cells = "".join(
            f'<td class="num {"up" if row[c] > 0 else "dn"}">{row[c]:,}</td>' for c in cols
        )
        body.append(f"<tr><td>{escape(row['date'])}</td>{cells}</tr>")
    return Markup(f"<table><tr><th>날짜</th>{head}</tr>{''.join(body)}</table>")


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals["flow_table"] = _flow_table
    return env


def render(snap: Snapshot) -> str:
    return _env().get_template("report.html.j2").render(snap=snap)


def write_html(snap: Snapshot, root: Path) -> Path:
    d = snap.session_date
    path = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{snap.market}_report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(snap), encoding="utf-8")
    return path
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_render.py -v`
Expected: 6 passed

- [ ] **Step 6: 커밋**

```bash
git add quant/analyze/render.py quant/analyze/templates/ tests/test_render.py && git commit -m "feat: 스냅샷 → HTML 렌더링 (결측 표시·XSS 이스케이프)"
```

---

### Task 14: CLI + 엔드투엔드 검증

**Files:**
- Create: `quant/apps/report_cli.py`, `report/__main__.py`, `tests/test_cli.py`
- Modify: `README.md` (신규)

**Interfaces:**
- Consumes: `quant.collect.snapshot.{collect, save_snapshot, load_snapshot}`, `quant.collect.sources.build_sources`, `quant.analyze.render.write_html`, `quant.core.report_clock.publish_at`
- Produces: CLI `python -m quant.apps.report_cli build --market {KR|US} [--date YYYY-MM-DD]`, `python -m quant.apps.report_cli render --market ... --date ...`, `python -m quant.apps.report_cli when --market ... --date ...`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_cli.py`**

```python
from datetime import date
from pathlib import Path
import pytest
from quant.apps.report_cli import main


def test_when_prints_publish_time(capsys):
    assert main(["when", "--market", "US", "--date", "2026-08-12"]) == 0
    assert "21:30" in capsys.readouterr().out


def test_when_kr(capsys):
    assert main(["when", "--market", "KR", "--date", "2026-08-12"]) == 0
    assert "08:00" in capsys.readouterr().out


def test_build_writes_snapshot_and_html(tmp_path: Path, monkeypatch):
    import quant.apps.report_cli as cli

    monkeypatch.setattr(
        cli, "build_sources",
        lambda m, d: {"market": ("https://x.test", lambda: {
            "quotes": {"^KS11": {"label": "KOSPI", "close": 1.0, "prev": 1.0, "change_pct": 0.0}},
            "crosscheck": {"checked": [], "warnings": []}})},
    )
    rc = main(["build", "--market", "KR", "--date", "2026-08-12", "--root", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "data" / "snapshots" / "KR" / "2026-08-12.json").exists()
    assert (tmp_path / "out" / "2026" / "08" / "12" / "KR_report.html").exists()


def test_render_reuses_existing_snapshot(tmp_path: Path, monkeypatch):
    import quant.apps.report_cli as cli

    monkeypatch.setattr(
        cli, "build_sources",
        lambda m, d: {"market": ("https://x.test", lambda: {
            "quotes": {}, "crosscheck": {"checked": [], "warnings": []}})},
    )
    main(["build", "--market", "KR", "--date", "2026-08-12", "--root", str(tmp_path)])
    html = (tmp_path / "out" / "2026" / "08" / "12" / "KR_report.html").read_text()
    assert main(["render", "--market", "KR", "--date", "2026-08-12", "--root", str(tmp_path)]) == 0
    assert (tmp_path / "out" / "2026" / "08" / "12" / "KR_report.html").read_text() == html


def test_render_without_snapshot_fails(tmp_path: Path):
    assert main(["render", "--market", "KR", "--date", "2026-08-12", "--root", str(tmp_path)]) == 1


def test_unknown_market_rejected():
    with pytest.raises(SystemExit):
        main(["when", "--market", "JP", "--date", "2026-08-12"])
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.apps.report_cli'`

- [ ] **Step 3: `quant/apps/report_cli.py` 구현**

```python
"""CLI. Phase 1은 크론 없이 손으로 돌려 검증한다."""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from quant.core.report_clock import publish_at
from quant.collect.snapshot import collect, load_snapshot, save_snapshot
from quant.analyze.render import write_html
from quant.collect.sources import build_sources


def _paths(root: Path) -> tuple[Path, Path]:
    return root / "data" / "snapshots", root / "out"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="report")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("build", "render", "when"):
        s = sub.add_parser(name)
        s.add_argument("--market", choices=["KR", "US"], required=True)
        s.add_argument("--date", default=date.today().isoformat())
        if name != "when":
            s.add_argument("--root", default=".")
    a = p.parse_args(argv)
    session = date.fromisoformat(a.date)

    if a.cmd == "when":
        print(f"{a.market} {session} 발행: {publish_at(a.market, session):%Y-%m-%d %H:%M %Z}")
        return 0

    snap_root, out_root = _paths(Path(a.root))

    if a.cmd == "build":
        snap = collect(a.market, session, build_sources(a.market, session))
        sp = save_snapshot(snap, snap_root)
        hp = write_html(snap, out_root)
        print(f"스냅샷 {sp}\nHTML   {hp}")
        if snap.missing():
            print(f"결측 {len(snap.missing())}건: {', '.join(snap.missing())}", file=sys.stderr)
        return 0

    sp = snap_root / a.market / f"{session.isoformat()}.json"
    if not sp.exists():
        print(f"스냅샷 없음: {sp} — 먼저 build 를 돌린다", file=sys.stderr)
        return 1
    print(f"HTML   {write_html(load_snapshot(sp), out_root)}")
    return 0
```

- [ ] **Step 4: `report/__main__.py` 구현**

```python
import sys

from quant.apps.report_cli import main

sys.exit(main())
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_cli.py -v`
Expected: 6 passed

- [ ] **Step 6: 전체 테스트**

Run: `uv run pytest -v`
Expected: 전부 통과 (live 는 deselected)

- [ ] **Step 7: 실제 리포트 생성 — 엔드투엔드**

```bash
cd ~/Documents/GitHub/market_report && uv run python -m quant.apps.report_cli build --market KR && uv run python -m quant.apps.report_cli build --market US
```

Expected: 스냅샷 2개 + HTML 2개 생성. 결측이 있으면 stderr에 목록이 뜬다 (실패가 아니라 정상 동작).

- [ ] **Step 8: 재현성 확인**

```bash
cd ~/Documents/GitHub/market_report && D=$(date +%Y/%m/%d) && M=$(date +%Y-%m-%d) \
 && cp "out/$D/KR_report.html" /tmp/first.html \
 && uv run python -m quant.apps.report_cli render --market KR --date "$M" \
 && diff -q /tmp/first.html "out/$D/KR_report.html" && echo "재현성 OK"
```

Expected: `재현성 OK` — 같은 스냅샷에서 같은 HTML이 나온다.

- [ ] **Step 9: 브라우저 확인**

```bash
open ~/Documents/GitHub/market_report/out/$(date +%Y/%m/%d)/KR_report.html
```

§10 검증 기준 5개(커버리지·정확성·가독성·차별성·재현성)로 회사 리포트와 대조한다.

- [ ] **Step 10: `README.md` 작성**

```markdown
# market_report

KR/US 개장 60분 전 개인 시황 리포트. 설계: `docs/specs/`, 계획: `docs/plans/`.

## 사용

```bash
uv sync
cp .env.local.example .env.local && chmod 600 .env.local   # 키 채우기
python3 scripts/check_keys.py                              # 키 검증

uv run python -m quant.apps.report_cli build --market KR    # 수집 + HTML
uv run python -m quant.apps.report_cli build --market US
uv run python -m quant.apps.report_cli render --market KR --date 2026-08-12   # 스냅샷 재렌더
uv run python -m quant.apps.report_cli when --market US --date 2026-08-12     # 발행 시각 확인
```

## 발행 시각

| 시장 | 개장 | 발행 |
|---|---|---|
| KR | 09:00 KST | 08:00 KST |
| US | 09:30 ET | 08:30 ET (서머타임 21:30 / 표준시 22:30 KST) |

## 테스트

```bash
uv run pytest            # 오프라인 (픽스처 기반)
uv run pytest -m live    # 네트워크 포함
```
```

- [ ] **Step 11: 커밋**

```bash
git add quant/apps/report_cli.py report/__main__.py tests/test_cli.py README.md && git commit -m "feat: CLI (build/render/when) + README"
```

---

---

### Task 15: 종목 사전 + 경계 인식 엔티티 추출

**Files:**
- Create: `quant/analyze/entities.py`, `tests/test_entities.py`, `tests/fixtures/kind_corplist.html`

**Interfaces:**
- Consumes: `quant.adapters.http.client`
- Produces: `quant.analyze.entities.parse_corp_list(raw: bytes) -> list[tuple[str, str, str]]`, `quant.analyze.entities.build_table(recs, min_len: int = 3) -> list[tuple[str, str]]`, `quant.analyze.entities.extract(text: str, table) -> list[dict]`, `quant.analyze.entities.load_table(cache_dir: Path) -> list[tuple[str, str]]`, `MIN_NAME_LEN: int`, `KIND_URL: str`

> **순진한 substring 매칭은 확실히 틀린다 (2026-08-12 실측).** "삼성전자, SK하이닉스
> 나란히 신고가... 코스맥스 실적 호조"에서 단순 매칭은 `이닉스`(SK하이**닉스**의
> 조각)와 `스맥`(코**스맥**스의 조각)을 뽑고 진짜 종목은 하나도 못 잡았다.
> 게다가 2글자 이하 종목명이 213개(`태양`·`성우`·`유신`·`웹스`…)라 일반 명사와
> 충돌한다 — "태양광 산업"에서 `태양`이 잡히면 원장이 통째로 오염된다.
>
> 방어: ① 긴 이름 우선 + 이미 먹힌 구간 재매칭 금지 ② 앞 경계가 글자면 조각으로
> 간주해 버림 ③ 뒤가 한글이면 조사(가/는/이/을/를/의/에/…)일 때만 허용
> ④ 2글자 이하 이름 기본 제외.

- [ ] **Step 1: 픽스처 저장**

```bash
cd ~/Documents/GitHub/market_report && curl -s -A "Mozilla/5.0" \
  "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13" \
  -o tests/fixtures/kind_corplist.html && wc -c tests/fixtures/kind_corplist.html
```

- [ ] **Step 2: 실패하는 테스트 작성 — `tests/test_entities.py`**

```python
from pathlib import Path
import pytest
from quant.analyze.entities import MIN_NAME_LEN, build_table, extract, parse_corp_list

FIXTURE = Path(__file__).parent / "fixtures" / "kind_corplist.html"


@pytest.fixture(scope="module")
def table():
    return build_table(parse_corp_list(FIXTURE.read_bytes()))


def test_parse_yields_kospi_and_kosdaq_only(table):
    recs = parse_corp_list(FIXTURE.read_bytes())
    assert len(recs) > 2000
    assert {m for _, _, m in recs} == {"유가", "코스닥"}


def test_known_symbols_present(table):
    lookup = dict(table)
    assert lookup["삼성전자"] == "005930"
    assert lookup["SK하이닉스"] == "000660"
    assert lookup["코스맥스"] == "192820"


def test_short_names_excluded(table):
    assert all(len(n) >= MIN_NAME_LEN for n, _ in table)


def test_extract_finds_all_three(table):
    hits = extract("삼성전자, SK하이닉스 나란히 신고가... 코스맥스 실적 호조에 급등", table)
    assert {h["symbol"] for h in hits} == {"005930", "000660", "192820"}


def test_extract_handles_korean_particles(table):
    assert [h["symbol"] for h in extract("삼성전자가 실적을 발표했다", table)] == ["005930"]


def test_extract_rejects_substring_fragments(table):
    """'태양광'에서 '태양'이 잡히면 원장이 오염된다."""
    assert extract("태양광 산업 전반이 부진하다", table) == []


def test_extract_rejects_inner_fragment(table):
    """'SK하이닉스' 안의 '이닉스' 같은 조각이 잡히면 안 된다."""
    hits = extract("SK하이닉스 신고가", table)
    assert [h["name"] for h in hits] == ["SK하이닉스"]


def test_extract_is_deduplicated(table):
    hits = extract("삼성전자 실적, 삼성전자 신고가", table)
    assert len(hits) == 1
```

- [ ] **Step 3: 실패 확인**

Run: `uv run pytest tests/test_entities.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.analyze.entities'`

- [ ] **Step 4: `quant/analyze/entities.py` 구현**

```python
"""뉴스에서 상장 종목을 뽑는다.

**순진한 substring 매칭은 틀린다** — 실측(2026-08-12)에서 "SK하이닉스"의 조각
'이닉스', "코스맥스"의 조각 '스맥'이 잡히고 진짜 종목은 하나도 안 잡혔다.
경계 검사와 최장 우선 매칭이 필수다.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from quant.adapters.http import client

KIND_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download&searchType=13"
MIN_NAME_LEN = 3          # 2글자 이하는 일반명사와 충돌 (태양·성우·유신…)
TRADABLE = ("유가", "코스닥")   # 코넥스 제외
_JOSA = "가는은이을를의에와과도만로으로부터까지에서"

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"^\d{6}$")
_WORD = re.compile(r"[가-힣A-Za-z0-9]")
_HANGUL = re.compile(r"[가-힣]")


def parse_corp_list(raw: bytes) -> list[tuple[str, str, str]]:
    """(회사명, 종목코드, 시장구분). KIND 다운로드는 euc-kr HTML 표다."""
    text = raw.decode("euc-kr", "replace")
    out = []
    for tr in _TR.findall(text)[1:]:
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        if len(cells) >= 3 and _CODE.match(cells[2]) and cells[1] in TRADABLE:
            out.append((cells[0], cells[2], cells[1]))
    if not out:
        raise ValueError("상장법인목록 파싱 0건 — KIND 표 구조 변경 의심")
    return out


def build_table(
    recs: list[tuple[str, str, str]], min_len: int = MIN_NAME_LEN
) -> list[tuple[str, str]]:
    """긴 이름 우선으로 정렬 — 최장 매칭이 짧은 조각을 선점한다."""
    return sorted(
        ((n, c) for n, c, _ in recs if len(n) >= min_len), key=lambda x: -len(x[0])
    )


def extract(text: str, table: list[tuple[str, str]]) -> list[dict]:
    hits: list[dict] = []
    spans: list[tuple[int, int]] = []
    for name, code in table:
        for m in re.finditer(re.escape(name), text):
            s, e = m.span()
            if any(s < je and e > js for js, je in spans):
                continue  # 더 긴 이름이 이미 차지한 구간
            before = text[s - 1] if s else " "
            after = text[e] if e < len(text) else " "
            if _WORD.match(before):
                continue  # 앞이 글자 → 조각
            if _HANGUL.match(after) and after not in _JOSA:
                continue  # 뒤가 조사 아닌 한글 → 조각
            spans.append((s, e))
            hits.append({"name": name, "symbol": code})
            break
    return hits


def load_table(cache_dir: Path) -> list[tuple[str, str]]:
    """하루 1회 받아 캐시한다. 상장사 목록은 자주 바뀌지 않는다."""
    cache = cache_dir / "kind_corplist.html"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with client(timeout=30.0) as c:
            cache.write_bytes(c.get(KIND_URL).content)
    return build_table(parse_corp_list(cache.read_bytes()))
```

- [ ] **Step 5: 통과 확인**

Run: `uv run pytest tests/test_entities.py -v`
Expected: 8 passed

- [ ] **Step 6: 커밋**

```bash
git add quant/analyze/entities.py tests/test_entities.py tests/fixtures/kind_corplist.html && git commit -m "feat: 종목 사전 + 경계 인식 엔티티 추출 (조각 오탐 방어)"
```

---

### Task 16: 언급 원장 + 연속성 지표

**Files:**
- Create: `quant/analyze/mentions.py`, `tests/test_mentions.py`

**Interfaces:**
- Consumes: `quant.analyze.entities.extract`, `quant.collect.contracts.Snapshot`
- Produces: `quant.analyze.mentions.collect_mentions(snap, table) -> list[dict]`, `quant.analyze.mentions.append_ledger(rows, path) -> int`, `quant.analyze.mentions.load_ledger(path) -> list[dict]`, `quant.analyze.mentions.continuity(ledger, today, lookback: int = 10) -> dict[str, dict]`

> **여기가 "호재가 쌓이면 conviction이 오른다"의 결정론적 절반이다.** LLM은 나중에
> 하루치 호재/악재 판정만 하고, 며칠 연속인지 세는 건 이 모듈이 한다. 그래야 같은
> 원장에서 같은 수치가 재현되고 3층에서 채점할 수 있다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_mentions.py`**

```python
from datetime import date
from pathlib import Path
from quant.analyze.mentions import append_ledger, continuity, load_ledger


def _row(d: str, sym: str, title: str = "제목"):
    return {"date": d, "symbol": sym, "name": "X", "title": title,
            "link": f"https://a.test/{title}", "feed": "F"}


def test_append_and_load_roundtrip(tmp_path: Path):
    p = tmp_path / "mentions.jsonl"
    assert append_ledger([_row("2026-08-12", "005930")], p) == 1
    assert load_ledger(p) == [_row("2026-08-12", "005930")]


def test_append_is_idempotent_on_same_link(tmp_path: Path):
    """같은 기사를 두 번 수집해도 언급이 부풀지 않아야 한다."""
    p = tmp_path / "mentions.jsonl"
    append_ledger([_row("2026-08-12", "005930", "A")], p)
    assert append_ledger([_row("2026-08-12", "005930", "A")], p) == 0
    assert len(load_ledger(p)) == 1


def test_continuity_counts_streak():
    ledger = [_row(d, "005930") for d in ("2026-08-10", "2026-08-11", "2026-08-12")]
    out = continuity(ledger, date(2026, 8, 12))
    assert out["005930"]["streak_days"] == 3
    assert out["005930"]["total"] == 3


def test_continuity_streak_breaks_on_gap():
    ledger = [_row("2026-08-08", "005930"), _row("2026-08-12", "005930")]
    out = continuity(ledger, date(2026, 8, 12))
    assert out["005930"]["streak_days"] == 1


def test_continuity_marks_new_symbol():
    out = continuity([_row("2026-08-12", "005930")], date(2026, 8, 12))
    assert out["005930"]["is_new"] is True


def test_continuity_not_new_when_seen_before():
    ledger = [_row("2026-08-05", "005930"), _row("2026-08-12", "005930")]
    assert continuity(ledger, date(2026, 8, 12))["005930"]["is_new"] is False


def test_continuity_excludes_symbols_absent_today():
    ledger = [_row("2026-08-11", "000660")]
    assert continuity(ledger, date(2026, 8, 12)) == {}


def test_continuity_respects_lookback():
    ledger = [_row("2026-07-01", "005930"), _row("2026-08-12", "005930")]
    out = continuity(ledger, date(2026, 8, 12), lookback=10)
    assert out["005930"]["total"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_mentions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.analyze.mentions'`

- [ ] **Step 3: `quant/analyze/mentions.py` 구현**

```python
"""종목 언급 원장 + 연속성 지표.

수집물을 일회성으로 버리지 않는다 — 매일 종목별 뉴스 등장을 append-only 원장에
쌓고, 거기서 "며칠 연속 등장했나"를 센다. 이 숫자가 conviction 가중치의 근거가
되며, LLM이 아니라 코드가 계산하므로 언제나 재현된다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from quant.collect.contracts import Snapshot
from quant.analyze.entities import extract


def collect_mentions(snap: Snapshot, table: list[tuple[str, str]]) -> list[dict]:
    """스냅샷의 뉴스 섹션에서 종목 언급을 뽑는다."""
    news = snap.results.get("news")
    if news is None or not news.ok or not news.data:
        return []
    rows: list[dict] = []
    for feed, items in news.data.get("feeds", {}).items():
        for item in items:
            for hit in extract(item["title"], table):
                rows.append({
                    "date": snap.session_date.isoformat(),
                    "symbol": hit["symbol"],
                    "name": hit["name"],
                    "title": item["title"],
                    "link": item["link"],
                    "feed": feed,
                })
    return rows


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def append_ledger(rows: list[dict], path: Path) -> int:
    """(symbol, link) 중복은 건너뛴다 — 같은 기사를 두 번 수집해도 부풀지 않는다."""
    existing = {(r["symbol"], r["link"]) for r in load_ledger(path)}
    fresh = [r for r in rows if (r["symbol"], r["link"]) not in existing]
    if fresh:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in fresh:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(fresh)


def continuity(ledger: list[dict], today: date, lookback: int = 10) -> dict[str, dict]:
    """오늘 등장한 종목에 대해 누적 언급·연속 일수·신규 여부를 센다."""
    cutoff = today - timedelta(days=lookback)
    today_iso = today.isoformat()

    dates_by_symbol: dict[str, set[date]] = defaultdict(set)
    all_dates: dict[str, set[date]] = defaultdict(set)
    for r in ledger:
        d = date.fromisoformat(r["date"])
        all_dates[r["symbol"]].add(d)
        if cutoff <= d <= today:
            dates_by_symbol[r["symbol"]].add(d)

    out: dict[str, dict] = {}
    for symbol, dates in dates_by_symbol.items():
        if today not in dates:
            continue  # 오늘 안 나온 종목은 오늘 리포트의 관심사가 아니다
        streak, cursor = 0, today
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        titles = [r["title"] for r in ledger
                  if r["symbol"] == symbol and r["date"] == today_iso]
        out[symbol] = {
            "total": len(dates),
            "streak_days": streak,
            "is_new": all_dates[symbol] == {today},
            "today_titles": titles,
        }
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_mentions.py -v`
Expected: 8 passed

- [ ] **Step 5: 커밋**

```bash
git add quant/analyze/mentions.py tests/test_mentions.py && git commit -m "feat: 종목 언급 원장 + 연속성 지표 (누적·연속·신규)"
```

---

### Task 17: 지표 델타 (전 세션 대비 변화)

**Files:**
- Create: `quant/analyze/delta.py`, `tests/test_delta.py`

**Interfaces:**
- Consumes: `quant.collect.contracts.Snapshot`
- Produces: `quant.analyze.delta.compare(current: Snapshot, previous: Snapshot | None) -> dict`, `quant.analyze.delta.previous_snapshot(market: str, session_date: date, root: Path) -> Snapshot | None`

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_delta.py`**

```python
from datetime import date, datetime
from quant.core.report_clock import KST
from quant.collect.contracts import SCHEMA_VERSION, Snapshot, SourceResult
from quant.analyze.delta import compare


def _snap(day: int, kospi: float, foreign: int) -> Snapshot:
    at = datetime(2026, 8, day, 8, 0, tzinfo=KST)
    return Snapshot(
        schema_version=SCHEMA_VERSION, market="KR", session_date=date(2026, 8, day),
        generated_at=at,
        results={
            "market": SourceResult("market", True,
                {"quotes": {"^KS11": {"label": "KOSPI", "close": kospi,
                                      "prev": kospi, "change_pct": 0.0}},
                 "crosscheck": {"checked": [], "warnings": []}},
                None, "https://x.test", at, 10),
            "kospi_flow": SourceResult("kospi_flow", True,
                {"market": "KOSPI", "unit": "억원",
                 "rows": [{"date": f"2026-08-{day:02d}", "외국인": foreign}]},
                None, "https://y.test", at, 10),
        },
    )


def test_compare_without_previous_is_empty():
    assert compare(_snap(12, 6345.0, 535), None) == {"quotes": {}, "flow": {}}


def test_compare_reports_quote_change():
    out = compare(_snap(12, 6600.0, 535), _snap(11, 6345.0, 535))
    assert out["quotes"]["^KS11"]["prev_close"] == 6345.0
    assert round(out["quotes"]["^KS11"]["pct"], 2) == 4.02


def test_compare_reports_foreign_flow_shift():
    out = compare(_snap(12, 6345.0, -700), _snap(11, 6345.0, 535))
    assert out["flow"]["외국인"]["current"] == -700
    assert out["flow"]["외국인"]["previous"] == 535
    assert out["flow"]["외국인"]["flipped"] is True


def test_flow_not_flipped_when_same_sign():
    out = compare(_snap(12, 6345.0, 300), _snap(11, 6345.0, 535))
    assert out["flow"]["외국인"]["flipped"] is False


def test_compare_skips_failed_sections():
    cur, prev = _snap(12, 6345.0, 535), _snap(11, 6345.0, 535)
    broken = SourceResult("market", False, None, "boom", "https://x.test",
                          cur.generated_at, 5)
    cur = Snapshot(cur.schema_version, cur.market, cur.session_date,
                   cur.generated_at, {**cur.results, "market": broken})
    assert compare(cur, prev)["quotes"] == {}
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_delta.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'quant.analyze.delta'`

- [ ] **Step 3: `quant/analyze/delta.py` 구현**

```python
"""전 세션 대비 변화. 리포트가 하루하루 따로 놀지 않게 하는 최소 장치."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from quant.collect.contracts import Snapshot

_LOOKBACK_DAYS = 10


def _ok(snap: Snapshot | None, key: str) -> dict | None:
    if snap is None:
        return None
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def compare(current: Snapshot, previous: Snapshot | None) -> dict:
    quotes: dict[str, dict] = {}
    cur_q, prev_q = _ok(current, "market"), _ok(previous, "market")
    if cur_q and prev_q:
        for sym, q in cur_q.get("quotes", {}).items():
            p = prev_q.get("quotes", {}).get(sym)
            if not p or not p.get("close"):
                continue
            quotes[sym] = {
                "label": q.get("label", sym),
                "close": q["close"],
                "prev_close": p["close"],
                "pct": (q["close"] / p["close"] - 1) * 100,
            }

    flow: dict[str, dict] = {}
    cur_f, prev_f = _ok(current, "kospi_flow"), _ok(previous, "kospi_flow")
    if cur_f and prev_f and cur_f.get("rows") and prev_f.get("rows"):
        c_row, p_row = cur_f["rows"][0], prev_f["rows"][0]
        for actor in ("개인", "외국인", "기관계"):
            if actor not in c_row or actor not in p_row:
                continue
            c, p = c_row[actor], p_row[actor]
            flow[actor] = {
                "current": c,
                "previous": p,
                "flipped": (c > 0) != (p > 0),
            }
    return {"quotes": quotes, "flow": flow}


def previous_snapshot(market: str, session_date: date, root: Path) -> Snapshot | None:
    """직전 스냅샷을 찾는다 — 주말·공휴일·장애로 며칠 비어도 거슬러 올라간다."""
    from quant.collect.snapshot import load_snapshot

    for back in range(1, _LOOKBACK_DAYS + 1):
        p = root / market / f"{(session_date - timedelta(days=back)).isoformat()}.json"
        if p.exists():
            return load_snapshot(p)
    return None
```

- [ ] **Step 4: 통과 확인**

Run: `uv run pytest tests/test_delta.py -v`
Expected: 5 passed

- [ ] **Step 5: 커밋**

```bash
git add quant/analyze/delta.py tests/test_delta.py && git commit -m "feat: 전 세션 대비 지표 델타 (수급 반전 탐지)"
```

---

### Task 18: 흐름 칸 렌더 + 엔진 호환 산출물

**Files:**
- Modify: `quant/analyze/render.py`, `quant/analyze/templates/report.html.j2`, `quant/apps/report_cli.py`
- Create: `tests/test_candidates.py`

**Interfaces:**
- Consumes: `quant.analyze.mentions.continuity`, `quant.analyze.delta.compare`
- Produces: `quant.analyze.render.candidates_line(cont: dict[str, dict]) -> str`, `quant.analyze.render.write_candidates(cont, snap, root) -> Path`, `quant.analyze.render.render(snap, cont=None, delta=None) -> str`

> 엔진 호환 형식은 기존 파이프라인과 같은 `AUTO_WATCH: SYMBOL[:TAG[+TAG]]` 이다.
> 태그는 근거를 담는다: `NEWS`(오늘 뉴스 언급), `STREAK`(2일 이상 연속),
> `NEW`(원장 최초 등장). **파일로 내보내기만 하고 자동 배선은 하지 않는다**
> (스펙 §11) — 3층 채점이 유효성을 증명한 뒤에 연결한다.

- [ ] **Step 1: 실패하는 테스트 작성 — `tests/test_candidates.py`**

```python
from quant.analyze.render import candidates_line


def test_line_is_empty_marker_when_no_symbols():
    assert candidates_line({}) == "AUTO_WATCH: 없음"


def test_news_tag_for_single_day():
    cont = {"005930": {"total": 1, "streak_days": 1, "is_new": False, "today_titles": ["t"]}}
    assert candidates_line(cont) == "AUTO_WATCH: 005930:NEWS"


def test_streak_tag_when_consecutive():
    cont = {"005930": {"total": 3, "streak_days": 3, "is_new": False, "today_titles": ["t"]}}
    assert candidates_line(cont) == "AUTO_WATCH: 005930:NEWS+STREAK"


def test_new_tag_on_first_appearance():
    cont = {"005930": {"total": 1, "streak_days": 1, "is_new": True, "today_titles": ["t"]}}
    assert candidates_line(cont) == "AUTO_WATCH: 005930:NEWS+NEW"


def test_sorted_by_streak_then_symbol():
    cont = {
        "000660": {"total": 1, "streak_days": 1, "is_new": False, "today_titles": ["t"]},
        "005930": {"total": 3, "streak_days": 3, "is_new": False, "today_titles": ["t"]},
    }
    assert candidates_line(cont) == "AUTO_WATCH: 005930:NEWS+STREAK 000660:NEWS"


def test_token_format_matches_engine_contract():
    """기존 파이프라인 정규식: ^[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?(:[0-9]{8})?$"""
    import re

    cont = {"005930": {"total": 3, "streak_days": 3, "is_new": True, "today_titles": ["t"]}}
    token = candidates_line(cont).split(": ", 1)[1]
    assert re.fullmatch(r"[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?(:[0-9]{8})?", token)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run pytest tests/test_candidates.py -v`
Expected: FAIL — `ImportError: cannot import name 'candidates_line'`

- [ ] **Step 3: `quant/analyze/render.py`에 추가**

```python
def candidates_line(cont: dict[str, dict]) -> str:
    """엔진 호환 한 줄. 기존 watch-score 입력 형식과 동일하다."""
    if not cont:
        return "AUTO_WATCH: 없음"
    ordered = sorted(cont.items(), key=lambda kv: (-kv[1]["streak_days"], kv[0]))
    tokens = []
    for symbol, c in ordered:
        tags = ["NEWS"]
        if c["streak_days"] >= 2:
            tags.append("STREAK")
        if c["is_new"]:
            tags.append("NEW")
        tokens.append(f"{symbol}:{'+'.join(tags)}")
    return "AUTO_WATCH: " + " ".join(tokens)


def write_candidates(cont: dict[str, dict], snap: Snapshot, root: Path) -> Path:
    d = snap.session_date
    path = (root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"
            / f"{snap.market}_candidates.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidates_line(cont) + "\n", encoding="utf-8")
    return path
```

`render`와 `write_html` 시그니처를 확장한다 (기본값이 있어 기존 호출과 호환):

```python
def render(snap: Snapshot, cont: dict | None = None, delta: dict | None = None) -> str:
    return _env().get_template("report.html.j2").render(
        snap=snap, cont=cont or {}, delta=delta or {"quotes": {}, "flow": {}}
    )


def write_html(snap: Snapshot, root: Path, cont: dict | None = None,
               delta: dict | None = None) -> Path:
    d = snap.session_date
    path = root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{snap.market}_report.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(snap, cont, delta), encoding="utf-8")
    return path
```

- [ ] **Step 4: 템플릿에 흐름 칸 추가**

`quant/analyze/templates/report.html.j2`의 `{% call(d) section('calendar', ...) %}` 블록 **바로 앞**에 삽입한다:

```jinja
{%- if cont %}
<div class="card"><h2>근거가 쌓인 종목</h2>
<table><tr><th>종목</th><th class="num">연속</th><th class="num">{{ '누적' }}</th>
<th>표시</th><th>오늘 기사</th></tr>
{%- for sym, c in cont|dictsort(by='value', reverse=true) %}
<tr><td>{{ sym }}</td>
    <td class="num">{{ c.streak_days }}일</td>
    <td class="num">{{ c.total }}건</td>
    <td>{% if c.streak_days >= 2 %}<span class="dday hi">STREAK</span>{% endif %}
        {% if c.is_new %}<span class="dday">NEW</span>{% endif %}</td>
    <td>{% for t in c.today_titles[:2] %}{{ t }}<br>{% endfor %}</td></tr>
{%- endfor %}</table>
<p style="color:var(--mut);font-size:12px;margin-top:10px">
  연속 등장은 근거 누적을 뜻할 뿐 호재/악재 판정이 아니다 — 판정은 Phase 2에서 붙는다.</p>
</div>
{%- endif %}

{%- if delta.quotes or delta.flow %}
<div class="card"><h2>전 세션 대비 변화</h2>
{%- if delta.quotes %}
<table><tr><th>종목</th><th class="num">현재</th><th class="num">직전</th><th class="num">변화</th></tr>
{%- for sym, q in delta.quotes.items() %}
<tr><td>{{ q.label }}</td><td class="num">{{ '{:,.2f}'.format(q.close) }}</td>
    <td class="num">{{ '{:,.2f}'.format(q.prev_close) }}</td>
    <td class="num {{ 'up' if q.pct > 0 else 'dn' }}">{{ '{:+.2f}'.format(q.pct) }}%</td></tr>
{%- endfor %}</table>
{%- endif %}
{%- if delta.flow %}
<h3 style="font-size:14px;margin:14px 0 6px">투자자 수급 (억원)</h3>
<table><tr><th>주체</th><th class="num">이번</th><th class="num">직전</th><th>반전</th></tr>
{%- for actor, f in delta.flow.items() %}
<tr><td>{{ actor }}</td>
    <td class="num {{ 'up' if f.current > 0 else 'dn' }}">{{ '{:+,}'.format(f.current) }}</td>
    <td class="num">{{ '{:+,}'.format(f.previous) }}</td>
    <td>{% if f.flipped %}<span class="dday hi">방향 전환</span>{% endif %}</td></tr>
{%- endfor %}</table>
{%- endif %}
</div>
{%- endif %}
```

- [ ] **Step 5: `quant/apps/report_cli.py`의 `build` 분기를 확장**

```python
    if a.cmd == "build":
        from quant.analyze.delta import compare, previous_snapshot
        from quant.analyze.entities import load_table
        from quant.analyze.mentions import append_ledger, collect_mentions, continuity
        from quant.analyze.render import write_candidates

        root = Path(a.root)
        snap = collect(a.market, session, build_sources(a.market, session))
        sp = save_snapshot(snap, snap_root)

        cont, delta = {}, {"quotes": {}, "flow": {}}
        if a.market == "KR":
            table = load_table(root / "data" / "cache")
            ledger_path = root / "data" / "ledger" / "mentions.jsonl"
            added = append_ledger(collect_mentions(snap, table), ledger_path)
            from quant.analyze.mentions import load_ledger

            cont = continuity(load_ledger(ledger_path), session)
            print(f"언급 {added}건 추가, 오늘 종목 {len(cont)}개")
        delta = compare(snap, previous_snapshot(a.market, session, snap_root))

        hp = write_html(snap, out_root, cont, delta)
        cp = write_candidates(cont, snap, out_root)
        print(f"스냅샷 {sp}\nHTML   {hp}\n후보   {cp}")
        if snap.missing():
            print(f"결측 {len(snap.missing())}건: {', '.join(snap.missing())}", file=sys.stderr)
        return 0
```

- [ ] **Step 6: 전체 테스트**

Run: `uv run pytest -v`
Expected: 전부 통과

- [ ] **Step 7: 커밋**

```bash
git add quant/analyze/render.py quant/analyze/templates/ quant/apps/report_cli.py tests/test_candidates.py && git commit -m "feat: 흐름 칸 렌더 + 엔진 호환 후보 산출물"
```

---

## Phase 1 완료 기준

- [ ] `uv run pytest` 전부 통과 (오프라인)
- [ ] `uv run pytest -m live` 통과 (네트워크)
- [ ] `python -m quant.apps.report_cli build --market KR` / `--market US` 둘 다 HTML 생성
- [ ] 같은 스냅샷 재렌더 시 바이트 동일 (재현성)
- [ ] 소스 하나를 일부러 실패시켜도 리포트가 생성되고 "결측 — 사유"로 표시됨
- [ ] 회사 리포트와 나란히 놓고 §10 검증 기준 5개 통과
- [ ] 이틀 이상 연속 실행 시 "근거가 쌓인 종목" 칸에 연속/누적이 반영됨
- [ ] `KR_candidates.txt` 토큰이 기존 엔진 정규식(`^[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?$`)을 통과
- [ ] 금요일 스냅샷이 있는 상태에서 월요일 실행 시 세션 윈도우가 72시간으로 계산됨

## Phase 1에서 하지 않는 것

**계층상 이후 단계:** LLM 호출, 예측 구조체, 채점 원장, 하네스, 서버 배포, 크론, 텔레그램.

**소스 중 의도적으로 미룬 것** — 스펙 §5에서 미검증/미해결로 분류된 항목들이다.
전부 브라우저 자동화나 추가 조사가 필요해 Phase 1의 "돌아가는 리포트 먼저"라는
목표를 지연시킨다. 결측으로 표시되며, Phase 1 검증 후 사용자가 아쉽다고 한 것만
Phase 1.5로 추가한다:

| 항목 | 미룬 이유 |
|---|---|
| CME FedWatch (금리 확률) | CME 403. Investing.com Fed Rate Monitor 경로 조사 필요 |
| 금투협 FREESIS (예탁금·신용융자) | 엔드포인트 미검증 |
| 한국 CDS · VKOSPI | KRX 파생 계열, 접근 경로 미확보 |
| S&P 신고가/신저가 | S5FI로 시장 폭은 이미 커버됨 |
| 유튜브 **자막** 요약 | 자막 원문은 Phase 2 LLM 요약과 함께 넣는 게 자연스럽다 (Phase 1은 제목·링크까지) |
| 야간 K200 · NDF | 회사는 유료 Infomax를 쓴다. 무료 대체 소스 조사 필요 |
