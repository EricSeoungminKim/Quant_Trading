# 네이버 국내증시 패리티 (서브프로젝트 H-1) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.
> 스펙 겸 계획 — 사용자가 2026-08-16 스크린샷으로 지정한 결함·요구가 설계다.
> (뉴스 수집 고도화는 H-2 로 별도 — 외부 리서치 결과 대기 중)

**Goal:** 업종별 시세가 네이버처럼 **소속 종목 전체**를 보여주고, 리포트에 **업종상위/테마상위** 카드(+더보기 전체)를 추가한다.

**사용자가 본 결함(실측)**: 무선통신서비스가 네이버(`sise_group_detail.naver?type=upjong&no=333`)에선 4종목(SK텔레콤·LG유플러스·프리티·와이어블)인데 우리 리포트는 "상승 2·하락 1" 요약과 함께 **1종목만** 나열 — 요약과 목록이 모순돼 보인다.

**근본 원인 3개(코드 확인됨)**:
1. `sector_view.build_sector_view` 가 멤버를 유니버스 교집합으로만 그림(`sector_view.py:31-36`)
2. `fetch_sector_map` 이 업종 상세 79페이지를 **이미 매일 열면서** 멤버 시세를 버림 — 같은 페이지 타입을 `naver_theme.parse_theme_members` 는 시세까지 파싱한다
3. `parse_theme_list` 가 테마별 등락률을 안 긁음(목록 페이지에 있다 — 네이버 홈의 "테마상위 정유 +6.48%" 가 그것)

## Global Constraints
- 기존 아티팩트 계약 불변(additive): `sector_map.json`(`{code:업종명}`) 유지, 새 데이터는 **새 아티팩트/새 키**로.
- 추가 네트워크 0 이 원칙: 업종 멤버 시세는 **이미 여는 79페이지에서** 파싱(H-1a). 테마 등락률은 이미 여는 목록 7페이지에서(H-1b).
- 없는 값 0 위장 금지, 실패 시 섹션/칸 생략, `|safe` 금지, 픽스처는 실 HTML, 테스트 약화 금지, trade 무접촉, 커밋 한국어 "왜".

---

### Task 1: 업종 멤버 시세 수집 (H-1a)

**Files:** Modify `quant/collect/sources/naver_sector.py` / Test `tests/test_naver_sector.py`

**Interfaces:**
- `parse_sector_detail_members(html) -> list[dict]` — `[{"code","name","change_pct"}]`. **`naver_theme.parse_theme_members` 가 같은 페이지 타입(sise_group_detail)을 파싱하는 방식을 먼저 읽고**, 재사용 가능하면 공용화(단 테마 쪽 계약 불변 — reason 필수 규칙은 테마 전용이다. 업종 상세엔 편입사유가 없다).
- `fetch_sector_map` 은 계약 불변. **새 함수** `fetch_sector_data(getter=None, sleep=None) -> tuple[dict, dict]` — `(sector_map, sector_members)` 를 한 크롤에서. `sector_members = {업종명: [{"code","name","change_pct"}]}`. 기존 `fetch_sector_map` 은 새 함수를 감싸 첫 원소만 반환(하위호환).
- deepdive 가 `fetch_sector_data` 로 전환, `data/ledger/sector_members.json` 아티팩트 추가(tmp+replace).

- [ ] Step 1: 실 HTML(무선통신서비스 no=333 — 4종목: SK텔레콤 +10.32%, LG유플러스 +2.45%, 프리티 0.00%, 와이어블 -1.03%)로 실패 테스트. **보합 0.00%·코스닥 별표(`와이어블 *`) 처리 포함**
- [ ] Step 2: 실패 확인 → Step 3: 구현 → Step 4: deepdive 배선(+아티팩트 테스트) → Step 5: 회귀 `-k "sector or naver or deepdive"` + 전체 → Step 6: 커밋

### Task 2: 테마 등락률 수집 (H-1b)

**Files:** Modify `quant/collect/sources/naver_theme.py` / Test `tests/test_naver_theme.py`

**Interfaces:**
- `parse_theme_quotes(html) -> list[dict]` — `[{"no","name","change_pct"}]` (목록 페이지의 테마별 전일대비 — 실 HTML 로 컬럼 확인, 3일 등락률 등 다른 컬럼과 혼동 금지). `fetch_themes` 산출 dict 의 각 테마에 `change_pct` 키 추가(additive — 기존 키 불변, 소비자 `theme_search` 무영향 확인).

- [ ] Step 1~6: Task 1 과 같은 규율 (실 HTML 픽스처, 실패 확인, 회귀 `-k "theme"` — 특히 기존 theme_search 테스트 그린 유지)

### Task 3: 업종상위/테마상위 카드 + 업종별 시세 전체 멤버 (H-1c)

**Files:** Modify `quant/analyze/sector_view.py`, `quant/analyze/render.py`, `report.html.j2`, `quant/apps/report_cli.py` / Test 기존 파일들

**Interfaces:**
- `build_sector_view(sector_map, sector_quotes, symbols, relations, sector_members=None)` — `sector_members` 있으면 각 업종의 `symbols` 를 **전체 멤버**로(등락률 포함), 유니버스 종목은 `in_universe: True` 표시+시세·관계 enrich. 없으면 기존 동작(하위호환). **표시 업종 선정 기준 변경**: 유니버스 보유 업종 ∪ 등락률 상위 10 업종(전체 79 를 다 그리진 않되 네이버 홈처럼 "오르는 업종"은 유니버스 무관하게 보인다).
- `build_top_movers(sector_quotes, themes) -> dict` (순수, 신규) — `{"sectors": [...상위3, 각 대표 종목 2(등락률순)], "themes": [...상위3]}` — 네이버 홈 "업종상위/테마상위" 카드와 같은 구조.
- 템플릿: 업종별 시세 섹션 **위에** 업종상위/테마상위 2열 카드(각 3행: 이름·등락률·대표 2종목) + "더보기" `<details>` 로 전체 업종/테마 목록. 업종 상세의 멤버 목록에서 유니버스 종목은 굵게+수혜주 트리, 비유니버스는 이름·등락률만.
- 요약 수치(상승 N·하락 N)와 목록 개수가 **일치**해야 한다(사용자가 본 모순 해소) — 테스트로 고정.

- [ ] Step 1: 실패 테스트(전체 멤버 표시 / 상위 카드 구조 / 더보기 전체 / 요약-목록 일치 / 하위호환) → Step 2~6: 규율 동일. E2E 는 컨트롤러가.

### Task 4: EC2 E2E + 문서
- [ ] 배포 → deepdive(업종 멤버 수집 포함) → 빌드 → **무선통신서비스에 4종목 확인**(사용자 재현 케이스) → 변경기록·체크박스 → 커밋

## Self-Review
- 사용자 지적 3건 전부 매핑: 멤버 전체(T1+T3), 업종상위/테마상위+더보기(T2+T3), 요약-목록 모순(T3 테스트).
- 추가 네트워크 0(기존 크롤에서 파싱만 추가) — EC2 부담 불변.
- 타입: `sector_members` dict(T1 산출→T3 소비), `change_pct` additive(T2 산출→T3 소비).
