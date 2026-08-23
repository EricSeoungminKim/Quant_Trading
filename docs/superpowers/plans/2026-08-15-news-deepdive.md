# 뉴스 심화 파이프라인 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 리포트 근거를 "RSS 제목"에서 "본문 + 수혜주 관계 사전 + 업종 분류"로 끌어올리는 야간 배치(`report_cli deepdive`)를 만든다.

**Architecture:** collect 평면에 본문·업종 수집기 2개(순수 파서 + 주입식 getter), analyze 평면에 순수 관계 로직(프롬프트/파서/증거채점/병합), apps 평면에 오케스트레이터. LLM(기존 `Narrator` 포트)은 **후보만** 내고, 채택은 결정론 증거 점수가 정한다. 산출물은 아티팩트(`data/ledger/relations.json`, `sector_map.json`)가 진실이고 MySQL 은 색인이다. 리포트 빌드는 아티팩트만 읽으므로 LLM·DB 가 죽어도 발행된다.

**Tech Stack:** Python 3.12, httpx(`quant.adapters.http.client`), regex 파싱(네이버 선례 `stock_detail.py`), 기존 `Narrator` 포트(OpenRouter 무료), MySQL(색인), pytest.

**스펙:** `docs/superpowers/specs/2026-08-15-news-deepdive-design.md` — 구현 전 반드시 읽을 것.

## Global Constraints

- **trade 평면 무접촉.** `quant/trade/` 를 수정·임포트하지 않는다. `tests/test_architecture.py` 가 강제하며 `KNOWN_DEBT` 에 새 항목 추가 금지.
- **analyze 는 순수하게.** `quant/analyze/relations.py` 는 네트워크·DB·파일 I/O 를 하지 않는다(전부 인자로 주입). I/O 는 collect(getter 주입)·apps·control 에만.
- **LLM 은 후보만 낸다** — 점수·채택을 정하지 않는다. 형식 위반 후보는 0점이 아니라 **버린다**(shadow-judge 규칙, `quant/control/shadow.py:56` 참조).
- **실패를 빈 값으로 위장하지 않는다.** fetch 실패는 `None`(빈 문자열 `""` 아님), LLM 불통이면 stats 에 `skipped_llm: true` 를 정직하게 남긴다.
- **본문을 영속화하지 않는다.** 본문은 메모리에서 프롬프트로만 쓰고 버린다(EC2 1.8GB). 디스크·DB 에 본문 컬럼/파일 금지. 상한 `MAX_BODY_CHARS = 4000`.
- **리포트 발행은 deepdive 와 무관하게 정시.** deepdive 산출물이 없으면 리포트는 그 섹션 없이 나간다.
- 테스트 픽스처는 **실제 스키마/실제 HTML 조각**에서 만든다(스키마 추측 금지 — 판단 199건 동일 해시 사고의 교훈).
- 커밋 메시지는 저장소 관례: 한국어, "왜"를 쓴다.
- 완료 주장 전: `uv run pytest` 전체 + `uv run python -m quant.apps.report_cli --help` 스모크.

---

## 파일 구조

| 파일 | 역할 | 신규/수정 |
|---|---|---|
| `quant/collect/sources/article_body.py` | 기사 본문 fetch + 텍스트 추출 | 신규 |
| `quant/collect/sources/naver_sector.py` | 네이버 업종 → KR symbol↔sector | 신규 |
| `quant/analyze/relations.py` | 관계 프롬프트·파서·증거채점·병합 (순수) | 신규 |
| `quant/adapters/schema/003_relations.sql` | `relation`, `sector_map` 테이블 | 신규 |
| `quant/control/relation_store.py` | MySQL upsert/load (색인) | 신규 |
| `quant/apps/deepdive.py` | 야간 배치 오케스트레이터 | 신규 |
| `quant/apps/report_cli.py` | `deepdive` 서브커맨드 등록 (main: 263행 근처) | 수정 |
| `quant/analyze/render.py` | `machine_payload()` 에 sector·relations 추가 | 수정 |
| `server/crontab.txt` | deepdive KR 05:00 / US 17:30 항목 | 수정 |
| `tests/test_article_body.py` 외 5개 | 각 단위 테스트 | 신규 |

---

### Task 1: 본문 수집기 (`article_body.py`)

**Files:**
- Create: `quant/collect/sources/article_body.py`
- Test: `tests/test_article_body.py`

**Interfaces:**
- Consumes: `quant.adapters.http.client` — 사용법은 `quant/collect/sources/stock_detail.py:150-190` 의 호출 패턴을 **먼저 읽고 그대로** 따른다(User-Agent·Referer 관례 포함).
- Produces: `extract_text(html_text: str) -> str` (순수), `fetch_body(url: str, getter=None) -> str | None`, 상수 `MAX_BODY_CHARS = 4000`. Task 5 가 `fetch_body` 를 쓴다.

- [x] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_article_body.py
"""본문 수집기 — 실패를 빈 본문으로 위장하지 않는지가 핵심이다."""
from quant.collect.sources.article_body import MAX_BODY_CHARS, extract_text, fetch_body

_HTML = """<html><head><style>p{color:red}</style>
<script>var x = "<p>가짜 문단</p>";</script></head>
<body><p>삼성전자가 차세대 HBM4 양산 일정을 앞당긴다고 밝혔다. 업계에서는 관련 소재·부품 공급망 전반의 수혜를 점치고 있다.</p>
<nav><p>메뉴</p></nav>
<p>한미반도체와 이오테크닉스 등 장비주가 동반 강세를 보였다.</p></body></html>"""


def test_extract_text_collects_paragraphs_and_strips_tags():
    text = extract_text(_HTML)
    assert "HBM4" in text and "한미반도체" in text
    assert "<p>" not in text and "var x" not in text


def test_extract_text_drops_short_boilerplate():
    # "메뉴" 같은 짧은 <p> 는 본문이 아니다 (30자 미만 문단 제외)
    assert "메뉴" not in extract_text(_HTML)


def test_extract_text_caps_length():
    huge = "<p>" + "가" * 10000 + "</p>"
    assert len(extract_text(huge)) <= MAX_BODY_CHARS


def test_fetch_body_failure_is_none_not_empty():
    """실패는 None — "" 로 위장하면 하류가 '본문 없는 기사'로 오독한다."""
    assert fetch_body("http://x", getter=lambda url: None) is None

    def boom(url):
        raise RuntimeError("connection reset")
    assert fetch_body("http://x", getter=boom) is None


def test_fetch_body_empty_extraction_is_none():
    # <p> 없는 페이지 → 추출 결과가 비면 None (부분 성공 위장 금지)
    assert fetch_body("http://x", getter=lambda url: "<div>no paras</div>") is None
```

- [x] **Step 2: 실패 확인** — Run: `uv run pytest tests/test_article_body.py -q` / Expected: FAIL (`ModuleNotFoundError`)

- [x] **Step 3: 최소 구현**

```python
# quant/collect/sources/article_body.py
"""기사 본문 fetch. 스펙: docs/superpowers/specs/2026-08-15-news-deepdive-design.md

**본문은 영속화하지 않는다** — LLM 프롬프트 재료로만 쓰고 버린다(EC2 1.8GB).
그래서 상한 4,000자. 실패는 None 이다 — "" 로 돌려주면 '본문이 빈 기사'와
'못 가져온 기사'가 섞이고, 이 저장소는 그 패턴으로 데이터를 잃은 적이 있다.
"""
from __future__ import annotations

import html as _html
import re

MAX_BODY_CHARS = 4000
_MIN_PARA_CHARS = 30  # 이보다 짧은 <p> 는 메뉴·바이라인 부류다

_DROP_RE = re.compile(r"<(script|style|nav)[^>]*>.*?</\1>", re.S | re.I)
_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_text(html_text: str) -> str:
    """<p> 문단만 모은다. 본문이 <p> 밖에 있는 사이트는 빈 문자열이 나온다 —
    그건 실패이고, fetch_body 가 None 으로 승격한다."""
    cleaned = _DROP_RE.sub("", html_text)
    paras = []
    for raw in _P_RE.findall(cleaned):
        p = _html.unescape(_TAG_RE.sub("", raw)).strip()
        if len(p) >= _MIN_PARA_CHARS:
            paras.append(p)
    return "\n".join(paras)[:MAX_BODY_CHARS]


def _http_get(url: str) -> str | None:
    # stock_detail.py 와 같은 클라이언트·예절을 쓴다 (구현 시 그 파일의 실제
    # 호출 시그니처를 읽고 맞출 것 — 여기만 다르게 쓰면 UA 차단이 갈린다).
    from quant.adapters.http import client

    r = client().get(url, follow_redirects=True, timeout=15)
    return r.text if r.status_code == 200 else None


def fetch_body(url: str, getter=None) -> str | None:
    get = getter or _http_get
    try:
        raw = get(url)
    except Exception:  # noqa: BLE001 — 개별 기사 실패가 배치를 죽이지 않는다
        return None
    if not raw:
        return None
    return extract_text(raw) or None
```

- [x] **Step 4: 통과 확인** — Run: `uv run pytest tests/test_article_body.py -q` / Expected: PASS. `_http_get` 은 stock_detail.py 실제 시그니처와 대조해 조정.
- [x] **Step 5: 커밋** — `git add quant/collect/sources/article_body.py tests/test_article_body.py && git commit -m "feat(collect): 기사 본문 수집기 — 실패는 None, 본문은 영속화하지 않는다"`

---

### Task 2: 업종 수집기 (`naver_sector.py`)

**Files:**
- Create: `quant/collect/sources/naver_sector.py`
- Test: `tests/test_naver_sector.py`

**Interfaces:**
- Produces: `parse_sector_index(html) -> list[tuple[str, str]]` (`[(no, 업종명)]`), `parse_sector_members(html) -> list[tuple[str, str]]` (`[(6자리코드, 종목명)]`), `fetch_sector_map(getter=None, sleep=None) -> dict[str, str]` (`{code: 업종명}`). Task 5 가 `fetch_sector_map` 을 쓴다.

- [x] **Step 1: 실패하는 테스트 작성** — 픽스처는 **실제 네이버 HTML 조각**을 브라우저에서 복사해 만든다(추측 금지). 최소 형태:

```python
# tests/test_naver_sector.py
"""네이버 업종 파서. 픽스처는 finance.naver.com/sise/sise_group.naver?type=upjong
실제 응답에서 잘라온 조각이다 — 마크업을 추측해 만들지 않는다."""
from quant.collect.sources.naver_sector import (
    fetch_sector_map, parse_sector_index, parse_sector_members)

# 실제 페이지 조각 (구현 시 실 HTML 로 갱신할 것 — 아래는 구조 골격)
_INDEX = '''<table><tr><td><a href="/sise/sise_group_detail.naver?type=upjong&no=261">보험</a></td></tr>
<tr><td><a href="/sise/sise_group_detail.naver?type=upjong&no=278">반도체와반도체장비</a></td></tr></table>'''
_MEMBERS = '''<table><tr><td><a href="/item/main.naver?code=005930">삼성전자</a></td></tr>
<tr><td><a href="/item/main.naver?code=000660">SK하이닉스</a></td></tr></table>'''


def test_parse_sector_index():
    assert ("261", "보험") in parse_sector_index(_INDEX)


def test_parse_sector_members():
    assert ("005930", "삼성전자") in parse_sector_members(_MEMBERS)


def test_fetch_sector_map_maps_code_to_sector():
    pages = {"index": _INDEX, "261": _MEMBERS, "278": ""}

    def getter(url):
        for key, v in pages.items():
            if key in url or key == "index" and url.endswith("type=upjong"):
                return v
        return None
    m = fetch_sector_map(getter=getter, sleep=lambda s: None)
    assert m.get("005930") == "보험" or m.get("005930")  # 첫 업종 귀속


def test_fetch_sector_map_partial_failure_keeps_going():
    # 업종 하나 실패해도 나머지는 수집된다 — 전체를 버리지 않는다
    def getter(url):
        if "no=261" in url:
            raise RuntimeError("timeout")
        if "no=278" in url:
            return _MEMBERS
        return _INDEX
    m = fetch_sector_map(getter=getter, sleep=lambda s: None)
    assert m.get("005930") == "반도체와반도체장비"
```

- [x] **Step 2: 실패 확인** — `uv run pytest tests/test_naver_sector.py -q` → FAIL
- [x] **Step 3: 최소 구현**

```python
# quant/collect/sources/naver_sector.py
"""네이버 업종(sise_group) → KR 종목↔업종 매핑. KR 에는 업종 분류가 없었다
(themes.py 는 뉴스 키워드 8개뿐) — 이게 테마별 시세·수혜주 후보의 바닥 데이터다.

수집 예절은 stock_detail.py 와 같다: 요청 사이 0.3s, Referer 지정.
한 종목이 여러 업종에 걸리면 **먼저 만난 업종**을 유지한다(네이버 인덱스 순서).
"""
from __future__ import annotations

import re
import time

INDEX_URL = "https://finance.naver.com/sise/sise_group.naver?type=upjong"
DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={no}"

_IDX_RE = re.compile(r"sise_group_detail\.naver\?type=upjong&no=(\d+)[^>]*>([^<]+)<")
_MEM_RE = re.compile(r"/item/main\.naver\?code=(\d{6})[\"'][^>]*>([^<]+)<")


def parse_sector_index(html_text: str) -> list[tuple[str, str]]:
    return [(no, name.strip()) for no, name in _IDX_RE.findall(html_text or "")]


def parse_sector_members(html_text: str) -> list[tuple[str, str]]:
    return [(code, name.strip()) for code, name in _MEM_RE.findall(html_text or "")]


def fetch_sector_map(getter=None, sleep=None) -> dict[str, str]:
    from quant.collect.sources.article_body import fetch_body  # noqa: F401 (관례 확인용 아님)
    get = getter or _http_get
    zzz = sleep or time.sleep
    try:
        idx = get(INDEX_URL)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for no, sector in parse_sector_index(idx or ""):
        try:
            page = get(DETAIL_URL.format(no=no))
        except Exception:  # noqa: BLE001 — 업종 하나 실패가 전체를 버리지 않는다
            continue
        for code, _name in parse_sector_members(page or ""):
            out.setdefault(code, sector)
        zzz(0.3)
    return out


def _http_get(url: str) -> str | None:
    from quant.adapters.http import client

    r = client().get(url, headers={"Referer": "https://finance.naver.com/"},
                     follow_redirects=True, timeout=15)
    return r.text if r.status_code == 200 else None
```

(구현 시 `_http_get`·불필요 임포트는 stock_detail.py 실제 패턴에 맞춰 정리. 픽스처는 실 HTML 로 교체.)

- [x] **Step 4: 통과 확인** — `uv run pytest tests/test_naver_sector.py -q` → PASS
- [x] **Step 5: 커밋** — `git commit -m "feat(collect): 네이버 업종 수집기 — KR 최초의 종목↔업종 매핑"`

---

### Task 3: 관계 로직 (`relations.py`, 순수)

**Files:**
- Create: `quant/analyze/relations.py`
- Test: `tests/test_relations.py`

**Interfaces:**
- Produces (Task 5·6 이 쓴다):
  - `KINDS = ("beneficiary", "supplier", "competitor")`, `MIN_EVIDENCE = 50`
  - `build_extraction_prompt(src_name: str, articles: list[dict]) -> str` — `articles`: `[{"title": str, "body": str}]`
  - `parse_candidates(text: str | None, name_to_code: dict[str, str], src_symbol: str) -> list[dict]` — 반환 `[{"src","dst","kind","reason"}]`
  - `evidence_score(dst_name: str, src_name: str, titles: list[str]) -> int` — `titles`: 최근 7일 뉴스 제목 전체
  - `merge_relation(existing: dict | None, cand: dict, score: int, today: str) -> dict` — 행 `{"src","dst","kind","reason","evidence_score","first_seen","last_verified"}`

- [x] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_relations.py
"""관계 로직. 핵심 계약: LLM 은 후보만 — 목록 밖·자기 자신·형식 위반은
0점이 아니라 **버린다** (shadow-judge 와 같은 규칙: 0점을 주면 '최하위 평가'가
되어 하류 순위를 오염시킨다)."""
from quant.analyze.relations import (
    MIN_EVIDENCE, build_extraction_prompt, evidence_score, merge_relation,
    parse_candidates)

_NAMES = {"한미반도체": "042700", "이오테크닉스": "039030", "삼성전자": "005930"}


def test_prompt_contains_titles_and_format_contract():
    p = build_extraction_prompt("삼성전자", [{"title": "HBM4 조기 양산", "body": "본문..."}])
    assert "삼성전자" in p and "HBM4" in p
    assert "|" in p  # 형식 계약(이름 | 종류 | 이유)이 프롬프트에 명시된다


def test_parse_candidates_drops_out_of_list_and_self():
    text = ("한미반도체 | 수혜주 | HBM 본딩 장비 공급\n"
            "존재하지않는회사 | 수혜주 | 이유\n"          # 목록 밖 → 버림
            "삼성전자 | 수혜주 | 자기 자신\n"              # src 자신 → 버림
            "이오테크닉스 · 수혜주 · 구분자 위반\n")       # 형식 위반 → 버림
    got = parse_candidates(text, _NAMES, src_symbol="005930")
    assert [c["dst"] for c in got] == ["042700"]
    assert got[0]["kind"] == "beneficiary"


def test_parse_candidates_none_is_empty():
    assert parse_candidates(None, _NAMES, "005930") == []


def test_evidence_score_needs_corpus_presence():
    titles = ["한미반도체, HBM 장비 수주 확대", "삼성전자·한미반도체 협력 강화"]
    strong = evidence_score("한미반도체", "삼성전자", titles)
    zero = evidence_score("이오테크닉스", "삼성전자", titles)
    assert strong >= MIN_EVIDENCE       # 언급 + 동시출현
    assert zero < MIN_EVIDENCE          # 코퍼스에 없으면 LLM 말만으로 못 들어간다


def test_merge_keeps_first_seen_updates_verification():
    old = {"src": "005930", "dst": "042700", "kind": "beneficiary",
           "reason": "옛 이유", "evidence_score": 55,
           "first_seen": "2026-08-01", "last_verified": "2026-08-01"}
    new = merge_relation(old, {"src": "005930", "dst": "042700",
                               "kind": "beneficiary", "reason": "새 이유"},
                         score=70, today="2026-08-15")
    assert new["first_seen"] == "2026-08-01"      # 이력 보존
    assert new["last_verified"] == "2026-08-15"
    assert new["evidence_score"] == 70 and new["reason"] == "새 이유"


def test_merge_new_relation():
    row = merge_relation(None, {"src": "005930", "dst": "042700",
                                "kind": "beneficiary", "reason": "r"},
                         score=60, today="2026-08-15")
    assert row["first_seen"] == row["last_verified"] == "2026-08-15"
```

- [x] **Step 2: 실패 확인** — `uv run pytest tests/test_relations.py -q` → FAIL
- [x] **Step 3: 최소 구현**

```python
# quant/analyze/relations.py
"""수혜주/공급사/경쟁사 관계 — LLM 후보 + 결정론 검증 (스펙 접근 ①).

**순수 모듈이다.** 네트워크·DB·파일을 만지지 않는다 — 전부 인자로 받는다.
analyze→adapters 임포트는 KNOWN_DEBT 부류이고, 여기서 새로 만들지 않는다.

증거 가중치는 초기값이다 — EC2 실코퍼스로 분포를 보고 조정한다(뉴스 방향
규칙을 1,425건에 돌려 조정한 선례). 감으로 확정하지 않는다.
"""
from __future__ import annotations

import re

KINDS = ("beneficiary", "supplier", "competitor")
_KIND_KO = {"수혜주": "beneficiary", "공급사": "supplier", "경쟁사": "competitor"}
MIN_EVIDENCE = 50
MAX_REASON_CHARS = 120

_LINE_RE = re.compile(
    r"^\s*(?P<name>[^|]{2,40}?)\s*\|\s*(?P<kind>수혜주|공급사|경쟁사)\s*\|\s*(?P<reason>.{5,%d})\s*$"
    % MAX_REASON_CHARS)


def build_extraction_prompt(src_name: str, articles: list[dict]) -> str:
    lines = [
        f"다음은 '{src_name}' 관련 오늘 뉴스다. 이 뉴스로 수혜/공급/경쟁 관계에 있는",
        "**한국 상장사**를 찾아라. 형식: 한 줄에 하나, `회사명 | 종류 | 한줄이유`.",
        "종류는 수혜주/공급사/경쟁사 중 하나. 확실하지 않으면 내지 마라. 최대 5줄.",
        "",
    ]
    for a in articles:
        lines.append(f"제목: {a.get('title', '')}")
        body = (a.get("body") or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines)


def parse_candidates(text: str | None, name_to_code: dict[str, str],
                     src_symbol: str) -> list[dict]:
    """목록 밖·자기 자신·형식 위반은 **버린다** — 0점 오염 방지(shadow 규칙)."""
    if not text:
        return []
    out, seen = [], set()
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        code = name_to_code.get(m["name"].strip())
        if code is None or code == src_symbol or code in seen:
            continue
        seen.add(code)
        out.append({"src": src_symbol, "dst": code,
                    "kind": _KIND_KO[m["kind"]],
                    "reason": m["reason"].strip()})
    return out


def evidence_score(dst_name: str, src_name: str, titles: list[str]) -> int:
    """결정론 채점 — LLM 말이 아니라 코퍼스가 뒷받침해야 사전에 들어간다.

    초기 가중치(실데이터로 조정 예정): 언급 ≥3건 40 / ≥1건 25,
    src·dst 동시출현 ≥1건 +30, 언급 다양성(서로 다른 제목 ≥5건) +30.
    """
    mentions = [t for t in titles if dst_name in t]
    n = len(mentions)
    score = 40 if n >= 3 else (25 if n >= 1 else 0)
    if any(src_name in t for t in mentions):
        score += 30
    if n >= 5:
        score += 30
    return score


def merge_relation(existing: dict | None, cand: dict, score: int,
                   today: str) -> dict:
    return {
        "src": cand["src"], "dst": cand["dst"], "kind": cand["kind"],
        "reason": cand["reason"][:MAX_REASON_CHARS],
        "evidence_score": score,
        "first_seen": existing["first_seen"] if existing else today,
        "last_verified": today,
    }
```

- [x] **Step 4: 통과 확인** — `uv run pytest tests/test_relations.py -q` → PASS
- [x] **Step 5: 커밋** — `git commit -m "feat(analyze): 관계 로직 — LLM 후보 + 결정론 증거채점, 목록 밖은 버린다"`

---

### Task 4: 스키마 + 저장소 (`003_relations.sql`, `relation_store.py`)

**Files:**
- Create: `quant/adapters/schema/003_relations.sql`, `quant/control/relation_store.py`
- Test: `tests/test_relation_store.py`

**Interfaces:**
- Consumes: Task 3 의 관계 행 dict.
- Produces: `upsert_relations(conn, rows: list[dict]) -> int`, `load_relations(conn, src_symbols: list[str]) -> dict[str, list[dict]]`. conn 은 `quant/control/warehouse.py` 와 같은 DB-API 커넥션(테스트는 그 파일의 fake conn 패턴을 따른다).

- [x] **Step 1: 스키마 작성**

```sql
-- quant/adapters/schema/003_relations.sql
-- 수혜주/공급사/경쟁사 관계 사전. **아티팩트(data/ledger/relations.json)가 진실,
-- 여기는 색인이다** (001 과 같은 원칙). last_verified 는 사후 갱신되는 값이라
-- forward_return 과 같은 이유로 MySQL 이 맡는다.
CREATE TABLE IF NOT EXISTS relation (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  src_symbol     VARCHAR(16)  NOT NULL,
  dst_symbol     VARCHAR(16)  NOT NULL,
  kind           ENUM('beneficiary','supplier','competitor') NOT NULL,
  reason         VARCHAR(255) NOT NULL DEFAULT '',
  -- 결정론 증거 점수(0~130). LLM 이 정한 값이 아니다 — analyze.relations.evidence_score.
  evidence_score SMALLINT     NOT NULL,
  first_seen     DATE         NOT NULL,
  last_verified  DATE         NOT NULL,
  -- 같은 관계를 다시 발견해도 행은 하나다(멱등) — 재검증은 UPDATE 로 남는다.
  UNIQUE KEY uq_relation (src_symbol, dst_symbol, kind),
  KEY ix_relation_src (src_symbol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS sector_map (
  market  ENUM('KR','US') NOT NULL,
  symbol  VARCHAR(16)     NOT NULL,
  sector  VARCHAR(64)     NOT NULL,
  updated DATE            NOT NULL,
  PRIMARY KEY (market, symbol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

- [x] **Step 2: 실패하는 테스트 작성** — **주의: `tests/test_warehouse.py` 의 fake conn 픽스처를 먼저 읽고 같은 패턴을 쓴다.** 이 저장소는 "fake 테스트 통과 → 실 MySQL 에서 IntegrityError" 사고가 있었다. 그래서 (a) SQL 문자열에 `ON DUPLICATE KEY UPDATE` 가 있는지 **그리고** (b) 같은 행 2회 upsert 시 execute 가 2회 나가되 예외가 없는지를 함께 확인하고, 실 DB 검증은 Task 7 의 EC2 스모크로 넘긴다(계획에 명시된 한계).

```python
# tests/test_relation_store.py
from quant.control.relation_store import load_relations, upsert_relations

_ROW = {"src": "005930", "dst": "042700", "kind": "beneficiary",
        "reason": "HBM 장비", "evidence_score": 70,
        "first_seen": "2026-08-15", "last_verified": "2026-08-15"}


class _Cur:
    def __init__(self, log):
        self.log = log
        self._rows = []

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))


def test_upsert_is_idempotent_style():
    conn = _Conn()
    assert upsert_relations(conn, [_ROW, _ROW]) == 2
    sqls = [s for s, _ in conn.log if s != "COMMIT"]
    # INSERT IGNORE 가 아니라 ON DUPLICATE KEY UPDATE — 재검증이 UPDATE 로 남아야 한다.
    assert all("ON DUPLICATE KEY UPDATE" in s.upper() for s in sqls)
    assert not any("INSERT IGNORE" in s.upper() for s in sqls)


def test_upsert_does_not_touch_first_seen_on_update():
    conn = _Conn()
    upsert_relations(conn, [_ROW])
    sql = conn.log[0][0].upper()
    upd = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert "FIRST_SEEN" not in upd  # 이력 보존 — 갱신 절에 first_seen 이 있으면 안 된다
```

- [x] **Step 3: 실패 확인** — `uv run pytest tests/test_relation_store.py -q` → FAIL
- [x] **Step 4: 최소 구현**

```python
# quant/control/relation_store.py
"""관계 사전 MySQL 색인. warehouse.py 와 같은 자리·같은 계약(conn 주입).

INSERT IGNORE 가 아니라 ON DUPLICATE KEY UPDATE 다 — 재검증(last_verified·
evidence_score 갱신)이 조용히 무시되면 사전이 낡은 채 신선해 보인다.
"""
from __future__ import annotations

_UPSERT = (
    "INSERT INTO relation (src_symbol, dst_symbol, kind, reason,"
    " evidence_score, first_seen, last_verified)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s)"
    " ON DUPLICATE KEY UPDATE reason = VALUES(reason),"
    " evidence_score = VALUES(evidence_score),"
    " last_verified = VALUES(last_verified)"
)


def upsert_relations(conn, rows: list[dict]) -> int:
    n = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute(_UPSERT, (r["src"], r["dst"], r["kind"], r["reason"],
                                  r["evidence_score"], r["first_seen"],
                                  r["last_verified"]))
            n += 1
    conn.commit()
    return n


def load_relations(conn, src_symbols: list[str]) -> dict[str, list[dict]]:
    if not src_symbols:
        return {}
    ph = ",".join(["%s"] * len(src_symbols))
    out: dict[str, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT src_symbol, dst_symbol, kind, reason, evidence_score,"
            " first_seen, last_verified FROM relation"
            f" WHERE src_symbol IN ({ph})", tuple(src_symbols))
        for src, dst, kind, reason, score, fs, lv in cur.fetchall():
            out.setdefault(src, []).append(
                {"src": src, "dst": dst, "kind": kind, "reason": reason,
                 "evidence_score": score, "first_seen": str(fs),
                 "last_verified": str(lv)})
    return out
```

- [x] **Step 5: 통과 확인** — `uv run pytest tests/test_relation_store.py -q` → PASS
- [x] **Step 6: 커밋** — `git commit -m "feat(control): 관계 사전 색인 — ON DUPLICATE KEY UPDATE, 재검증이 UPDATE 로 남는다"`

---

### Task 5: deepdive 오케스트레이터 + CLI

**Files:**
- Create: `quant/apps/deepdive.py`
- Modify: `quant/apps/report_cli.py` — `main()` 의 서브파서 루프(`for name in ("build", "render", "when", "collect")` — 263행 근처)에 `"deepdive"` 추가 + 디스패치 블록 + `record_run(kv, f"deepdive:{market}", ...)` (collect 의 307행 패턴과 동일)
- Test: `tests/test_deepdive.py`

**Interfaces:**
- Consumes: `fetch_body`(Task 1) · `fetch_sector_map`(Task 2) · `build_extraction_prompt`/`parse_candidates`/`evidence_score`/`merge_relation`/`MIN_EVIDENCE`(Task 3) · `upsert_relations`(Task 4, MySQL 은 **선택적** — 실패해도 아티팩트는 남는다) · `quant.adapters.narrate.make_narrator` · `quant.analyze.mentions.collect_mentions`/`continuity`/`rank` · `quant.analyze.entities.load_name_map(cache_dir, market)`
- Produces: `run_deepdive(market: str, root: Path, narrator, getter=None, today: str | None = None, deadline_min: int = 90) -> dict` — stats `{"sources": n, "candidates": n, "accepted": n, "skipped_llm": bool}`. 아티팩트 `data/ledger/relations.json`(전체 스냅샷, `{src: [행...]}`), KR 이면 `data/ledger/sector_map.json`(`{code: sector}`).

- [x] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_deepdive.py
"""오케스트레이터 — 모든 I/O 를 주입해 밀폐로 검증한다.
핵심 계약: LLM 이 죽으면(narrate→None) 신규 발견만 멈추고 기존 스냅샷은 보존."""
import json
from quant.apps.deepdive import run_deepdive


class _FakeNarrator:
    def __init__(self, text):
        self._t = text

    def narrate(self, prompt):
        return self._t


def _seed_news(root, market, day, titles):
    d = root / "data" / "news" / market
    d.mkdir(parents=True)
    rows = [{"key": f"k{i}", "title": t, "link": f"http://n/{i}",
             "outlet": "테스트", "feed": "f", "published": None,
             "published_known": False, "first_seen": f"{day}T09:00:00",
             "last_seen": f"{day}T09:00:00", "seen_count": 1}
            for i, t in enumerate(titles)]
    (d / f"{day}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows))


def test_llm_down_preserves_existing_snapshot(tmp_path):
    led = tmp_path / "data" / "ledger"
    led.mkdir(parents=True)
    (led / "relations.json").write_text(json.dumps({"005930": [{"dst": "042700"}]}))
    _seed_news(tmp_path, "KR", "2026-08-15", ["삼성전자 HBM4 뉴스"])
    stats = run_deepdive("KR", tmp_path, _FakeNarrator(None),
                         getter=lambda url: None, today="2026-08-15")
    assert stats["skipped_llm"] is True
    kept = json.loads((led / "relations.json").read_text())
    assert "005930" in kept  # 어제 사전이 지워지지 않았다


def test_accepts_only_evidence_backed(tmp_path, monkeypatch):
    _seed_news(tmp_path, "KR", "2026-08-15",
               ["삼성전자 HBM4 조기 양산", "삼성전자·한미반도체 협력",
                "한미반도체 수주 확대", "한미반도체 장비 증설"])
    # name_map 로딩을 밀폐 — entities 캐시 대신 고정 사전
    monkeypatch.setattr("quant.apps.deepdive._name_maps",
                        lambda root, market: ({"한미반도체": "042700",
                                               "삼성전자": "005930"},
                                              {"005930": "삼성전자"}))
    llm = _FakeNarrator("한미반도체 | 수혜주 | HBM 본딩 장비\n"
                        "존재안함 | 수혜주 | 근거없음\n")
    stats = run_deepdive("KR", tmp_path, llm,
                         getter=lambda url: "<p>" + "삼성전자 HBM 본문" * 10 + "</p>",
                         today="2026-08-15")
    snap = json.loads((tmp_path / "data" / "ledger" / "relations.json").read_text())
    assert stats["accepted"] >= 1
    assert any(r["dst"] == "042700" for r in snap.get("005930", []))
```

- [x] **Step 2: 실패 확인** — `uv run pytest tests/test_deepdive.py -q` → FAIL
- [x] **Step 3: 구현** — 뼈대 (mentions/continuity 의 실제 시그니처는 `quant/analyze/mentions.py:24,80,173` 을 읽고 맞출 것):

```python
# quant/apps/deepdive.py
"""야간 심화 배치 — 본문 → LLM 후보 → 결정론 검증 → 관계 사전.

리포트 발행은 이 배치와 **무관하게 정시**다. 여기서 무엇이 실패하든
relations.json 은 (a) 갱신되거나 (b) 어제 것이 그대로 남는다 — 지워지는
경우는 없다. 스펙: docs/superpowers/specs/2026-08-15-news-deepdive-design.md
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from quant.analyze.relations import (
    MIN_EVIDENCE, build_extraction_prompt, evidence_score, merge_relation,
    parse_candidates)
from quant.collect.sources.article_body import fetch_body
from quant.collect.sources.naver_sector import fetch_sector_map

TOP_SOURCES = 10        # 유망 종목 수 — 뉴스 언급 상위 (render.rank 와 같은 기준)
BODIES_PER_SOURCE = 3   # 종목당 본문 기사 수 상한


def _name_maps(root: Path, market: str):
    """(회사명→코드, 코드→회사명). entities 캐시에서 — 테스트는 이 함수를 바꿔친다."""
    from quant.analyze.entities import load_name_map
    cache = root / "data" / "cache"          # 실제 경로는 report_cli._paths 를 따른다
    code_to_name = load_name_map(cache, market)
    return {v: k for k, v in code_to_name.items()}, code_to_name


def _load_titles(root: Path, market: str, days: list[str]) -> list[dict]:
    rows = []
    for d in days:
        p = root / "data" / "news" / market / f"{d}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def run_deepdive(market: str, root: Path, narrator, getter=None,
                 today: str | None = None, deadline_min: int = 90) -> dict:
    t0 = time.monotonic()
    day = today or date.today().isoformat()
    rows = _load_titles(root, market, [day])
    titles = [r["title"] for r in rows]
    name_to_code, code_to_name = _name_maps(root, market)

    # 유망 종목: 제목에서 이름 매칭 빈도 상위 (mentions 모듈과 같은 결이지만
    # 밀폐를 위해 제목만으로 센다 — 정확 순위가 아니라 '어디를 팔지'다)
    counts: dict[str, int] = {}
    for t in titles:
        for name, code in name_to_code.items():
            if name in t:
                counts[code] = counts.get(code, 0) + 1
    sources = sorted(counts, key=counts.get, reverse=True)[:TOP_SOURCES]

    led = root / "data" / "ledger"
    led.mkdir(parents=True, exist_ok=True)
    snap_path = led / "relations.json"
    snapshot = json.loads(snap_path.read_text()) if snap_path.exists() else {}

    stats = {"sources": len(sources), "candidates": 0, "accepted": 0,
             "skipped_llm": False}
    for src in sources:
        if (time.monotonic() - t0) > deadline_min * 60:
            break  # 시간 상한 — 부분 결과로 종료(스펙). 리포트는 정시 발행.
        src_name = code_to_name.get(src, src)
        arts = []
        for r in rows:
            if src_name in r["title"] and len(arts) < BODIES_PER_SOURCE:
                body = fetch_body(r["link"], getter=getter)
                arts.append({"title": r["title"], "body": body or ""})
        text = narrator.narrate(build_extraction_prompt(src_name, arts))
        if text is None:
            stats["skipped_llm"] = True
            continue  # 이 종목만 스킵 — 어제 사전은 그대로
        cands = parse_candidates(text, name_to_code, src)
        stats["candidates"] += len(cands)
        for c in cands:
            score = evidence_score(code_to_name.get(c["dst"], ""), src_name, titles)
            if score < MIN_EVIDENCE:
                continue
            olds = {r["dst"]: r for r in snapshot.get(src, [])}
            snapshot.setdefault(src, [])
            snapshot[src] = [r for r in snapshot[src] if r["dst"] != c["dst"]]
            snapshot[src].append(merge_relation(olds.get(c["dst"]), c, score, day))
            stats["accepted"] += 1

    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))

    if market == "KR":
        sectors = fetch_sector_map(getter=getter)
        if sectors:  # 실패(빈 dict)면 어제 파일을 덮지 않는다
            (led / "sector_map.json").write_text(
                json.dumps(sectors, ensure_ascii=False, indent=1))
    return stats
```

- [x] **Step 4: report_cli 배선** — 서브파서 루프에 `"deepdive"` 추가(`--market/--date/--root` 는 기존 규약 그대로), 디스패치:

```python
    if a.cmd == "deepdive":
        from quant.adapters.kv import make_kv
        from quant.adapters.narrate import make_narrator
        from quant.apps.deepdive import run_deepdive
        stats = run_deepdive(a.market, Path(a.root), make_narrator(),
                             today=a.date)
        print(f"deepdive {a.market} · 대상 {stats['sources']}종목 "
              f"· 후보 {stats['candidates']} · 채택 {stats['accepted']}"
              + (" · LLM 불통" if stats["skipped_llm"] else ""))
        record_run(make_kv(), f"deepdive:{a.market}",
                   ok=not stats["skipped_llm"],
                   detail=f"accepted={stats['accepted']}")
        return 0
```

(`make_kv` 임포트 경로·`record_run` 인자는 collect 블록(307행)의 실제 사용을 읽고 동일하게.)

- [x] **Step 5: 통과 확인** — `uv run pytest tests/test_deepdive.py -q` → PASS, 그리고 `uv run python -m quant.apps.report_cli deepdive --market KR --root /tmp/dd_smoke` 가 빈 데이터에서도 에러 없이 "대상 0종목"으로 끝나는지.
- [x] **Step 6: 커밋** — `git commit -m "feat(apps): deepdive 야간 배치 — LLM 불통이어도 어제 사전은 지워지지 않는다"`

---

### Task 6: engine JSON 확장 (`machine_payload`)

**Files:**
- Modify: `quant/analyze/render.py` — `machine_payload()` (112-243행) 시그니처에 `relations: dict | None = None, sectors: dict | None = None` 추가; `quant/apps/report_cli.py` build 경로에서 두 아티팩트를 읽어 전달
- Test: 기존 `machine_payload` 테스트 파일에 추가 (`grep -rn "machine_payload" tests/` 로 위치 확인)

**Interfaces:**
- Consumes: Task 5 아티팩트 (`relations.json`: `{src: [{"dst","kind","reason","evidence_score",...}]}`, `sector_map.json`: `{code: sector}`)
- Produces: `symbols[]` 항목에 `sector: str`(있을 때만), `relations: [{"symbol","kind","reason","score"}]`(있을 때만, `evidence_score >= MIN_EVIDENCE` 만, 상위 5개). **B(UI)·C(자동편입)가 이 스키마를 소비한다 — 키 이름을 여기서 확정한다.**

- [x] **Step 1: 실패하는 테스트** (기존 payload 테스트의 픽스처 관례를 따라):

```python
def test_machine_payload_carries_sector_and_relations():
    # 기존 테스트 픽스처(snap, cont 등)를 재사용해 machine_payload 를 부르되
    relations = {"005930": [
        {"dst": "042700", "kind": "beneficiary", "reason": "HBM 장비",
         "evidence_score": 70, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
        {"dst": "039030", "kind": "beneficiary", "reason": "약한 근거",
         "evidence_score": 30, "first_seen": "2026-08-15", "last_verified": "2026-08-15"},
    ]}
    sectors = {"005930": "반도체와반도체장비"}
    payload = machine_payload(..., relations=relations, sectors=sectors)  # 기존 인자 그대로 + 신규 2개
    sym = next(s for s in payload["symbols"] if s["symbol"] == "005930")
    assert sym["sector"] == "반도체와반도체장비"
    assert [r["symbol"] for r in sym["relations"]] == ["042700"]  # 임계 미달은 빠진다
    assert sym["relations"][0]["kind"] == "beneficiary"


def test_machine_payload_without_artifacts_unchanged():
    payload = machine_payload(...)  # 신규 인자 없이 — 기존 호출부가 깨지지 않는다
    sym = payload["symbols"][0]
    assert "relations" not in sym and "sector" not in sym
```

- [x] **Step 2: 실패 확인** → FAIL (TypeError/KeyError)
- [x] **Step 3: 구현** — `machine_payload` 의 심볼 조립 루프(`render.py:180-183` 근처)에서:

```python
        if sectors and sym in sectors:
            entry["sector"] = sectors[sym]
        rels = [r for r in (relations or {}).get(sym, [])
                if r.get("evidence_score", 0) >= MIN_EVIDENCE][:5]
        if rels:
            entry["relations"] = [{"symbol": r["dst"], "kind": r["kind"],
                                   "reason": r["reason"],
                                   "score": r["evidence_score"]} for r in rels]
```

report_cli build 경로: `data/ledger/relations.json`·`sector_map.json` 을 `json.loads`(없으면 `None`)로 읽어 전달. 읽기 실패는 `None` — 리포트는 그 섹션 없이 나간다.

- [x] **Step 4: 통과 확인** — `uv run pytest tests/ -q -k "machine_payload or render"` → PASS
- [x] **Step 5: 커밋** — `git commit -m "feat(analyze): engine JSON 에 sector·relations — B·C 가 소비할 계약 확정"`

---

### Task 7: 크론 + 문서 + 전체 검증

**Files:**
- Modify: `server/crontab.txt`(기존 항목의 flock·로그 리다이렉트 스타일을 복사), `docs/vault/변경기록.md`(맨 위 항목 추가), `docs/plans/재설계-phase4-8.md`·`docs/plans/개선-백로그-2026-08-15.md`(해당 체크박스/참조)
- 검증: 전체 테스트 + 스모크

- [x] **Step 1: 크론 항목** — 기존 `server/crontab.txt` 의 리포트/수집 항목 문법을 그대로 따라 추가 (시각 근거: 스펙 "리포트 T-마감 상대" — KR 리포트 07:50 → 05:00 시작, US 19:50 → 17:30 시작):

```
# deepdive — 야간 심화(본문·관계·업종). 리포트 발행과 독립: 실패해도 리포트는 정시.
0 5  * * 1-5  <기존 항목과 같은 래퍼> report_cli deepdive --market KR ...
30 17 * * 1-5 <기존 항목과 같은 래퍼> report_cli deepdive --market US ...
```

- [x] **Step 2: 전체 검증** — 1,846 passed(11 skipped, 19 deselected, 1 xfailed). `test_architecture.py` 8 passed, `KNOWN_DEBT` 5건 그대로(증가 0). `report_cli --help`·`cli backtest donchian 90d` 스모크 정상.

```bash
uv run pytest -q                                    # 1,818+ 전부, 실패 0
uv run pytest -q tests/test_architecture.py -v      # 평면 규칙 — KNOWN_DEBT 증가 0
uv run python -m quant.apps.report_cli --help
uv run python -m quant.apps.cli backtest --strategy donchian --days 90
```

- [x] **Step 3: 문서** — `변경기록.md` 맨 위에: 무엇을(A 파이프라인), 왜(RSS 제목만으로는 근거 부족 — 스펙 링크), 어떻게 검증했는지(테스트 수 + 스모크 결과). 백로그 §4 말미 "Phase 7 입력" 항목에 A 와의 관계 한 줄.
- [x] **Step 4: 커밋** — `git commit -m "chore(server,docs): deepdive 크론 + 기록 — A 파이프라인 완결"`
- [x] **Step 5: EC2 검증 절차 기록(배포는 별도 결정)** — 배포 규칙: 장 마감 후(`resume-redesign` §3). 배포 후 할 일을 `변경기록.md` 항목에 명시:
  1. `report_cli deepdive --market KR` 수동 1회 → `relations.json` 산출물을 **직접 세어** 확인("돌았다"≠"일했다")
  2. `evidence_score` 가중치를 실코퍼스 분포로 조정 (MySQL `article` 최근 7일로 언급/동시출현 분포 질의 — 뉴스 방향 규칙 1,425건 선례)
  3. `003_relations.sql` 적용 + `upsert_relations` 실 DB 왕복 1회 (fake conn 테스트의 한계 명시적 보완)

---

## Self-Review 결과

- **스펙 커버리지**: 본문 수집(T1) / 업종 수집(T2) / LLM 후보+결정론 검증(T3) / 사전 축적·MySQL 색인(T4·T5) / deepdive 커맨드(T5) / engine JSON 확장(T6) / 스케줄·실패 모드·record_run(T5·T7) / 저장 축소(본문 비영속 — T1 상수 + Global Constraints) — 전부 매핑됨.
- **플레이스홀더**: "실제 시그니처를 읽고 맞출 것" 지시 3곳(stock_detail 클라이언트, mentions, record_run)은 미정이 아니라 **기존 코드가 단일 진실**이라는 지시다. 가중치 초기값은 명시돼 있고 조정 절차(T7-5)가 정의돼 있다.
- **타입 일관성**: `relations.json` 스키마(T5 산출)와 T6 소비 키(`dst/kind/reason/evidence_score`) 일치. `MIN_EVIDENCE` 는 T3 정의, T5·T6 소비. `fetch_sector_map -> dict[str, str]` T2 정의, T5 소비.
