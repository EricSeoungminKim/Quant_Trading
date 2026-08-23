# 테마 기반 종목 탐색 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 수혜주 관계의 근거를 LLM 추측에서 **네이버 테마 편입사유(출처 있는 팩트)** 로 바꾸고, 소스 선정을 테마 균형으로 바꿔 대형주 편중을 구조적으로 없앤다.

**Architecture:** collect 에 테마 수집기(목록 페이지네이션 + 상세 편입사유), analyze 에 순수 탐색 로직(뉴스→테마→대장주→수혜주), apps 의 deepdive KR 경로가 이를 쓰고 US 는 기존 LLM 경로 유지. 관계 행에 `via_theme`·`source` 를 추가해 팩트 근거와 LLM 근거를 섞지 않는다.

**Tech Stack:** 기존 그대로 (httpx via `quant.adapters.http.client`, regex 파싱, jsonl/json 아티팩트, pytest).

**스펙:** `docs/superpowers/specs/2026-08-15-theme-search-design.md` — 먼저 읽을 것.

## Global Constraints

- **trade 평면 무접촉.** `tests/test_architecture.py` 통과, `KNOWN_DEBT` 추가 금지.
- **analyze 는 순수하게.** `theme_search.py` 는 네트워크·DB·파일 I/O 금지(전부 인자 주입).
- **팩트와 추측을 섞지 않는다.** 관계 행의 `source` 는 `"naver_theme"` 또는 `"llm"`. 편입사유가 없는 종목으로 관계를 만들지 않는다(빈 이유 금지).
- **기존 소비자 무변경.** 관계 행 신규 키는 **있을 때만**. `relations.json` 기존 키(`src/dst/kind/reason/evidence_score/first_seen/last_verified`) 불변. A 의 US 경로 동작 불변.
- **실패해도 리포트는 정시.** 테마 수집 실패 → 어제 아티팩트 → 그것도 없으면 A 의 기존 LLM 경로로 폴백.
- 픽스처는 **실 HTML 조각**(추측 금지). 테스트 약화 금지. 커밋 메시지 한국어 "왜".

---

## 파일 구조

| 파일 | 역할 | 신규/수정 |
|---|---|---|
| `quant/collect/sources/naver_theme.py` | 테마 목록·상세 파싱/수집 | 신규 |
| `quant/analyze/theme_search.py` | 뉴스→테마→대장주→수혜주 (순수) | 신규 |
| `quant/apps/deepdive.py` | KR 경로 통합 + 테마 사전 적재 | 수정 |
| `quant/adapters/schema/004_relation_theme.sql` | `relation` 에 `via_theme`·`source` | 신규 |
| `quant/control/relation_store.py` | 새 컬럼 upsert | 수정 |
| 테스트 3개 | 각 단위 | 신규 |

---

### Task 1: 테마 수집기

**Files:**
- Create: `quant/collect/sources/naver_theme.py`
- Test: `tests/test_naver_theme.py`

**Interfaces:**
- Produces:
  - `THEME_LIST_URL = "https://finance.naver.com/sise/theme.naver"`, `THEME_DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}"`
  - `parse_theme_list(html) -> list[tuple[str, str]]` — `[(no, 테마명)]`
  - `parse_theme_members(html) -> list[dict]` — `[{"code","name","reason","change_pct","value_traded"}]`. **`reason` 은 "테마 편입 사유" 텍스트**; 없으면 그 종목은 **목록에서 제외**한다(빈 이유 금지).
  - `fetch_themes(getter=None, sleep=None, max_pages=None) -> dict` — `{no: {"name": str, "symbols": [...]}}`
- Task 2·3 이 소비.

**실측(2026-08-15, EC2)**: 목록 1페이지에 테마 40개(`sise_group_detail.naver?type=theme&no=185` → `정유`). 상세 행 텍스트 예: `GS 테마 편입 사유 GS 정유업체 GS칼텍스를 손자회사로 보유. 117,000 상승 9,600 +8.94% ... 805,628 93,140 942,502`. **컬럼 의미(거래량/거래대금 등)는 표 헤더로 직접 확인하고, 확신 없는 컬럼은 파싱하지 마라.**

- [x] **Step 1: 실 HTML 확보 + 실패 테스트** — 목록/상세 실 응답에서 조각을 잘라 픽스처로. 의도: ① 목록 파싱 ② 편입사유 있는 행 파싱(사유 텍스트가 종목명 반복을 포함할 수 있다 — 정리 규칙을 테스트로 고정) ③ **편입사유 없는 행은 제외** ④ 페이지네이션: 다음 페이지가 없으면 멈춘다(무한 루프 금지) ⑤ 개별 테마 실패 시 나머지는 계속
- [x] **Step 2: 실패 확인** — `uv run pytest -q tests/test_naver_theme.py` → FAIL
- [x] **Step 3: 구현** — 예절은 `naver_sector.py` 와 동일(요청 간 0.3s, `_http_get` 패턴 재사용 또는 동일 방식). 전체 테마 수가 많으므로 `max_pages` 로 상한을 두되 기본은 끝까지.
- [x] **Step 4: 통과 + 회귀** — `uv run pytest -q -k "theme or naver"` + `tests/test_architecture.py`
- [x] **Step 5: 커밋** — `feat(collect): 네이버 테마 수집 — 편입사유가 '왜 수혜주인가'의 출처다`

---

### Task 1b: 거래상위 수집 (`sise_quant`)

> 2026-08-15 사용자 지정 소스 3종 중 마지막: 업종(B-T1 완료)·테마(F-T1 완료)·
> **거래상위(`http://finance.naver.com/sise/sise_quant.naver`)**. 시장 전체에서
> "오늘 돈이 몰린 종목"을 주는 페이지다 — 테마 밖의 대장주를 놓치지 않는 보완 신호.

**Files:**
- Create: `quant/collect/sources/naver_quant.py`
- Test: `tests/test_naver_quant.py`

**Interfaces:**
- Produces: `parse_quant_rows(html) -> list[dict]` — `[{"code","name","change_pct","value_traded"}]`(컬럼 의미는 실 `<th>` 헤더로 확인, 확신 없는 컬럼은 제외 — Task 1 과 같은 규율), `fetch_quant_top(getter=None, pages=1) -> list[dict]`(기본 1페이지 상한).
- Task 2 의 `select_sources` 가 보조 신호로 소비(테마 대장주 + 시장 거래상위 합집합).

- [x] **Step 1: 실 HTML 확보 + 실패 테스트** — 실 응답 조각 픽스처. 의도: ① 행 파싱(코드·등락률·거래대금) ② 깨진 행 스킵(0 채움 금지) + 스킵 로그(naver_sector 관례) ③ 실패 시 `[]`
- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — `_http_get`·예절은 기존 관례 그대로.
- [x] **Step 4: 통과 + 회귀** — `uv run pytest -q -k "quant_rows or naver"` + `tests/test_architecture.py`
- [x] **Step 5: 커밋** — `feat(collect): 거래상위 수집 — 테마 밖 대장주를 놓치지 않는다`

---

### Task 2: 탐색 로직 (순수)

**Files:**
- Create: `quant/analyze/theme_search.py`
- Test: `tests/test_theme_search.py`

**Interfaces:**
- Consumes: Task 1 의 `themes` dict, 기존 `quant/analyze/relations.match_codes` 결과(제목별 코드 집합).
- Produces:
  - `theme_index(themes) -> dict[str, list[str]]` — 종목코드 → 소속 테마 번호들
  - `hot_themes(themes, title_matches, limit) -> list[str]` — 그날 기사에서 매칭된 종목들의 소속 테마를 집계해 상위 N. **종목이 아니라 테마 단위로 센다**(대형주 편중 완화)
  - `leaders(themes, theme_no, top) -> list[dict]` — 거래대금 상위 종목(대장주)
  - `beneficiaries(themes, theme_no, src_code, limit) -> list[dict]` — 같은 테마의 다른 종목 + **편입사유(reason)**. `via_theme` 를 함께 실는다
  - `select_sources(themes, title_matches, max_themes, per_theme) -> list[dict]` — 최종 소스 목록. **한 테마가 전체를 먹지 못한다**(테마당 상한)
- Task 3 이 소비.

- [x] **Step 1: 실패 테스트** — ① 종목→테마 역색인 ② 반도체 뉴스가 아무리 많아도 `select_sources` 결과의 테마 수가 `max_themes` 를 넘지 않고 **한 테마 종목이 `per_theme` 를 넘지 않는다**(사용자 요구: "막무가내로 삼성·하이닉스만" 방지 — 이 테스트가 그 계약이다) ③ `beneficiaries` 가 src 자신을 제외하고 편입사유를 그대로 싣는다 ④ 편입사유 없는 종목은 애초에 Task 1 에서 빠지므로 여기서는 빈 이유가 나올 수 없다(방어적으로 한 번 더 거른다)
- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — 순수. 정렬 기준(거래대금)이 없으면 그 종목은 뒤로 보내되 제외하지는 않는다.
- [x] **Step 4: 통과 + 회귀** — `uv run pytest -q -k "theme"` + `tests/test_architecture.py`
- [x] **Step 5: 커밋** — `feat(analyze): 테마 기반 탐색 — 테마당 상한으로 대형주 독식을 구조적으로 막는다`

---

### Task 3: deepdive 통합 + 스키마 확장

**Files:**
- Create: `quant/adapters/schema/004_relation_theme.sql`
- Modify: `quant/apps/deepdive.py`, `quant/control/relation_store.py`
- Test: `tests/test_deepdive.py`(추가), `tests/test_relation_store.py`(추가)

**Interfaces:**
- 스키마: `ALTER TABLE relation ADD COLUMN via_theme VARCHAR(64) NULL, ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'llm';` (**기존 행이 있으므로 DEFAULT 필수** — 적용 전 `relation` 이 비어 있는지와 무관하게 안전해야 한다)
- `relation_store.upsert_relations` 가 새 컬럼을 함께 쓴다. 행에 키가 없으면 `via_theme=None`, `source="llm"`.
- `deepdive.run_deepdive(market="KR")`: 테마 아티팩트(`data/ledger/themes.json`)를 읽고
  1. 없으면 Task 1 수집기로 만든다(야간 배치 안에서 1회)
  2. `select_sources` 로 소스 결정(기존 언급 수 상위 10 대체)
  3. 각 소스의 `beneficiaries` 를 관계 후보로 만들고 `evidence_score` 로 채점
  4. `source="naver_theme"`, `via_theme=테마명`, `reason=편입사유`
  5. **LLM 은 그대로 호출하되**(테마 밖 관계용) 그 결과는 `source="llm"` 로 남긴다
  6. 테마 수집·읽기가 모두 실패하면 **A 의 기존 경로로 폴백**
- US 경로는 **변경 없음**.

- [x] **Step 1: 실패 테스트** — ① KR: 테마 아티팩트가 있으면 `source="naver_theme"` 관계가 생기고 `via_theme` 가 채워진다 ② 테마가 없으면 기존 LLM 경로로 폴백해 여전히 관계가 나온다 ③ US 는 테마를 보지 않는다 ④ `upsert_relations` 가 새 컬럼을 SQL 에 싣고, 키가 없는 행은 기본값으로 간다
- [x] **Step 2: 실패 확인** → FAIL
- [x] **Step 3: 구현** — `run_deepdive` 가 이미 길다. 테마 경로를 별도 함수로 빼서 읽을 수 있게 하라(파일이 커지면 분리도 검토).
- [x] **Step 4: 통과 + 회귀** — `uv run pytest -q` 전체 + `tests/test_architecture.py`
- [x] **Step 5: 커밋** — `feat(apps): deepdive KR 을 테마 기반으로 — 근거의 출처가 LLM 이 아니라 네이버 편입사유`

---

### Task 4: 문서 + EC2 E2E

- [x] **Step 1: 전체 검증** — `uv run pytest -q` / `tests/test_architecture.py -v` / `report_cli --help` / `cli backtest --strategy donchian --days 90`
- [x] **Step 2: EC2 배포·실행** — 장 마감 후 규칙 준수(주말이면 즉시 가능). 스키마 004 적용 → `report_cli deepdive --market KR` 1회 → **직접 센다**: `themes.json` 테마·종목 수, 관계 중 `source=naver_theme` 비율, **소스로 뽑힌 종목이 몇 개 테마에 분산됐는지**(사용자 요구의 검증 지표), 편입사유가 실제로 화면에 쓸 만한지 표본 5건 육안 확인
- [x] **Step 3: 문서** — `docs/vault/변경기록.md` 항목(왜/뭘/실측 수치/여파), 계획 체크박스, `docs/plans/개선-백로그-2026-08-15.md` 에 A 의 LLM 경로가 KR 에서 보조로 밀렸음을 한 줄
- [x] **Step 4: 커밋** — `chore(docs): 테마 기반 탐색(F) 완결 — 실측 분산 수치`

---

## Self-Review

- 스펙 커버리지: 팩트 앵커(T1 편입사유) / 대장주·섹터 균형(T2 select_sources 상한) / via_theme·source 분리(T3 스키마·행) / 폴백(T3) / E2E 분산 검증(T4) — 전부 매핑.
- 플레이스홀더: "표 헤더로 직접 확인" 류는 미정이 아니라 **추측 금지** 지시(A·B 계획과 같은 규약).
- 타입 일관성: `fetch_themes -> dict[no, {name, symbols}]`(T1) → `theme_index`/`hot_themes`/`select_sources`(T2) → `run_deepdive`(T3). 관계 행 신규 키 `via_theme`·`source` 는 T3 한 곳에서 정의하고 스키마와 이름이 같다.
