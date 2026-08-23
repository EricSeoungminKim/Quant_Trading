# 뉴스 수집 고도화 (서브프로젝트 H-2) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.
> 스펙: `docs/superpowers/specs/2026-08-16-news-scale-design.md`. 순서 1→3→4→6→5,
> 스펙 구성요소 2(후보 종목별 동적 RSS)는 **이번 웨이브에서 뺀다** — 피드 수 증가라
> EC2 부하 실측 후 단계 도입(스펙 명시).

**Goal:** 공식/허용 경로(DART API·RSS 조건부 GET·네이버 리서치 목록)로 뉴스 축을
넓히고, 같은 사건 다매체 기사를 클러스터로 묶어 발행량 급증(뉴스 모멘텀)을
피처화한다.

**Architecture:** 수집기는 `quant/collect/sources/`(네트워크 허용), 순수 클러스터/
피처는 `quant/analyze/`, 원장은 `data/ledger/` append-only, 리포트 배선은
`report_cli`+템플릿. 거래 평면 무접촉. 실패는 빈 값 위장 없이 생략+로그.

## Global Constraints
- 시크릿 값 출력 금지 — DART 키는 `.env.local` 의 `DART_API_KEY`, 코드는 env 로만
  읽고 로그에 키를 남기지 않는다. 테스트는 전부 mock (실호출 스모크 1회만, 응답
  건수만 출력).
- 네이버 뉴스/종목뉴스/공시 페이지 폴링 금지(robots 실측 — 스펙). `/research/` 는
  yeti 허용 구역이라 일 1회 목록만.
- `KNOWN_DEBT` 추가 금지, 테스트 약화 금지, `|safe` 금지, 커밋 한국어 '왜'.
- 새 의존성 금지 — MinHash 는 표준 라이브러리 shingle+자카드로 시작(스펙).
- 완료 주장 전 `uv run pytest` 전체 + `report_cli --help`.

---

### Task 1: DART 공시 수집기 (팩트 이벤트 축)

**Files:** Create `quant/collect/sources/dart.py`, `server/scripts/dart_collect.sh` / Modify `server/crontab.txt`, `scripts/check_keys.py` / Test `tests/test_dart.py`

**Interfaces:**
- `parse_disclosures(payload: dict) -> list[dict]` (순수) — DART `list.json` 응답에서
  `[{"rcept_no","stock_code","corp_name","report_nm","rcept_dt"}]`. `stock_code` 6자리
  없는 비상장은 버린다. status!="000" 이면 빈 리스트 + 이유를 튜플/필드로 드러낸다
  (조용한 빈값 금지 — 반환형은 `tuple[list[dict], str|None]` (rows, error)).
- `fetch_disclosures(bgn_de: str, end_de: str, api_key: str | None = None, getter=None) -> tuple[list[dict], str|None]`
  — `https://opendart.fss.or.kr/api/list.json` GET (crtfc_key, bgn_de, end_de,
  page_no 순회 ≤10, page_count=100, corp_cls Y/K 두 번). 키 없으면 `([], "no key")`.
  네트워크 예외는 삼키고 error 로. httpx timeout 10s.
- `append_ledger(rows, path: Path) -> int` — `data/ledger/disclosures.jsonl` 에
  `rcept_no` 중복 제거 append(기존 rcept_no 집합 로드 후). 반환 = 추가 건수.
- `dart_collect.sh`: `.venv/bin/python -m quant.apps.report_cli dart-collect` 를 불러
  어제~오늘 공시를 원장에 append (report_cli 에 서브커맨드 추가 — `_load_artifact`
  패턴, 실패 exit 0 + stderr). 크론 `20 7 * * *` 매일 (KR 리포트 07:50 전).
- `scripts/check_keys.py` 에 DART_API_KEY 도달 확인 추가(값 출력 없음, 기존 패턴).
- 리포트 배선(최소): `_derive`/`machine_payload` 는 건드리지 않고, `_emit` 에서
  disclosures.jsonl 의 **오늘·전일 공시 중 리포트 symbols 와 교집합**을
  `render(..., disclosures=...)` 로 전달, 템플릿 종목 카드에 `공시` 칩 + report_nm
  나열(`<details>` 3건 초과 시 접기). 교집합 0 이면 아무 것도 안 그림.

- [x] Step 1: 실패 테스트(실 응답 형태 픽스처 — status 000/013(no data)/020(키 한도), 비상장 필터, 중복 rcept_no, 페이지 순회) → Step 2: 실패 확인 → Step 3: 구현 → Step 4: 로컬 실호출 스모크 1회(건수만 출력) + 전체 테스트 → Step 5: 셸+크론+check_keys → Step 6: 커밋 `feat(collect): DART 공시 수집 — 뉴스보다 정확한 팩트 이벤트 축 (H-2)`

### Task 2: RSS 조건부 GET (기존 33피드 재전송 비용 절감)

**Files:** Modify `quant/collect/sources/feeds.py`, `quant/collect/collector.py` / Test `tests/test_feeds*.py` 기존 파일

**Interfaces:**
- feeds `_fetch_one(url)` → `fetch_conditional(url, cache: dict) -> tuple[list[dict], dict]`
  형태로 확장: `cache[url] = {"etag":…, "last_modified":…}` 를 받아
  `If-None-Match`/`If-Modified-Since` 헤더 전송, 304 면 `([], 그대로)` — collector 의
  기존 중복 판정이 어차피 안전망이라 304=신규 없음으로 취급해도 유실이 없다
  (store 는 append-only). 200 이면 응답 헤더에서 갱신.
- `collect_once` 가 `data/cache/feed_headers.json` 을 로드/저장(tmp+os.replace,
  파싱 실패 시 빈 dict — 캐시는 성능 최적화일 뿐 정확성에 관여하지 않는다는
  주석 필수).
- 하위호환: cache 인자 없이 부르면 기존과 동일 동작.

- [x] Step 1: 실패 테스트(304 → 빈 목록+캐시 유지, 200 → 파싱+캐시 갱신, 캐시 파일 깨짐 → 빈 dict 재시작, 헤더 없는 서버 → 캐시 미저장) → Step 2~4: TDD → Step 5: 커밋 `feat(collect): RSS 조건부 GET — 30분 주기 33피드 재전송 비용 절감 (H-2)`

### Task 3: 제목 shingle 클러스터 (같은 사건 묶기 + 뉴스 모멘텀)

**Files:** Create `quant/analyze/news_cluster.py` / Modify `quant/analyze/render.py`(뉴스 목록 조립부), `report.html.j2` / Test `tests/test_news_cluster.py`

**Interfaces:**
- `cluster_titles(items: list[dict], threshold: float = 0.6) -> list[list[int]]` (순수,
  stdlib only) — 제목 문자 2-gram shingle 집합의 자카드 유사도 ≥ threshold 를 같은
  클러스터로(greedy, 입력 순서 결정론). 반환은 인덱스 그룹.
- `dedup_with_counts(items) -> list[dict]` — 각 클러스터 대표(최신 published) 1건에
  `dup_count: N`(클러스터 크기) 부여.
- 리포트 뉴스 섹션: 같은 클러스터는 대표만 보이고 `외 N개 매체` 배지(기존
  `<details>` 전량 보기 안에는 전체 유지 — 정보 소실 금지).

- [x] Step 1: 실패 테스트(동일 사건 두 제목 묶임/다른 사건 안 묶임/threshold 경계/순서 결정론/빈 입력) → Step 2~4: TDD + 실데이터 스모크(오늘 store 로 클러스터 분포 출력) → Step 5: 커밋 `feat(analyze): 제목 클러스터 — 다매체 중복을 사건 단위로 (H-2)`

### Task 4: 뉴스 z-score 피처 (selections 속성 — producer_version 승급)

**Files:** Create `quant/analyze/news_momentum.py` / Modify `quant/apps/report_cli.py`(`_selection_params`/속성 조립부), `quant/control/selections.py`(producer_version) / Test `tests/test_news_momentum.py` + 기존 selections 테스트

**Interfaces:**
- `news_zscore(counts: list[int]) -> float | None` (순수) — 종목별 일 언급 건수
  시계열(과거→오늘, 오늘 포함 최소 5일)에서 오늘 건수의 z-score. 표준편차 0 이면
  None(0 위장 금지). `mentions.jsonl` 을 날짜×심볼로 집계하는
  `daily_counts(path, symbol, upto: date, days: int = 20) -> list[int]` 동반.
- selections 속성에 `news_z` 추가(None 이면 키 자체 생략) → **producer_version 을
  한 단계 올린다**(현재 값을 읽고 +1; 스펙 명시 규율: 새 버전 산출물이 이전 버전
  버킷으로 새는 폴백 금지 — E 재설계 때와 동일). natural_key 는 불변.

- [x] Step 1: 실패 테스트(z 계산/표본 부족 None/σ=0 None/속성 생략/버전 승급이 기존 버킷과 안 섞임) → Step 2~4: TDD → Step 5: 커밋 `feat(analyze): 뉴스 발행량 z-score 를 선정 속성으로 — 리더보드가 유용성을 채점 (H-2)`

### Task 5: 증권사 리서치 목록 (일 1회, 목록만)

**Files:** Create `quant/collect/sources/naver_research.py` / Modify `quant/apps/deepdive.py`(일 배치에 합류), `report.html.j2` / Test `tests/test_naver_research.py`

**Interfaces:**
- `parse_research_list(html) -> list[dict]` — `finance.naver.com/research/company_list.naver`
  (yeti 허용 구역) 목록에서 `[{"stock_name","title","broker","target_price"(없으면 생략),"date"}]`.
  실 HTML 픽스처 필수. PDF 본문은 읽지 않는다(스펙).
- `fetch_research(getter=None) -> list[dict]` — 첫 2페이지만, 실패 시 빈 리스트+로그.
- deepdive 일 배치에서 `data/ledger/research.jsonl` append(제목+날짜+증권사 중복
  제거), 리포트 종목 카드에 "오늘 리포트: 증권사 — 제목" 줄(교집합만).

- [x] Step 1: 실패 테스트(실 HTML 픽스처, 목표가 결측 생략, 중복) → Step 2~4: TDD → Step 5: 커밋 `feat(collect): 증권사 리서치 목록 — '오늘 리포트 나온 종목' 이벤트 (H-2)`

### Task 6: EC2 배포 + E2E (컨트롤러 직접)
- [x] DART 키 EC2 등록 확인(사용자 액션 — count 만 확인) 후 배포, dart-collect 실호출 1회(건수만), 크론 설치
- [x] 리포트 재빌드로 공시 칩·클러스터 배지·리서치 줄 실측, 변경기록 + 스펙/백로그 갱신

## Self-Review
- 스펙 1→3→4→6→5 전부 태스크로 매핑, 2(동적 RSS)는 명시적 비포함(부하 실측 후).
- 신규 의존성 0(DART 는 httpx 기존, 클러스터는 stdlib). robots 비허용 경로 접근 없음.
- producer_version 규율(T4)과 조용한 빈값 금지(T1 error 필드)가 기존 원칙과 정합.
- 타입 배선: T1 rows → report 칩(교집합), T3 dup_count → 배지, T4 news_z → selections.
