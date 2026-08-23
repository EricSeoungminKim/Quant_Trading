# 뉴스 심화 파이프라인 (서브프로젝트 A) — 설계 스펙

> 2026-08-15 승인. 상위 워크플로우(A→E→B→C→D) 중 첫 번째.
> A가 만드는 데이터를 E(채점 루프)·B(리포트 UI)·C(엔진 자동편입)가 소비한다.

## 목표

리포트의 근거를 "RSS 제목"에서 "본문 + 수혜주 관계 + 업종 분류"로 끌어올린다.
시간 제약은 느슨하다(야간 배치, "천천히 해도 됨") — 대신 **리포트 발행은 이
파이프라인의 어떤 실패에도 막히지 않아야 한다.**

## 현재 상태 (2026-08-15 실측)

- 수집은 **RSS 제목+링크만** 읽는다. 본문 없음. KR 16피드 / US 17피드,
  하루 KR ~1,200 / US ~500건 (`quant/collect/sources/feeds.py`).
- 1차 저장 JSONL(`data/news/{market}/YYYY-MM-DD.jsonl`, 보존 7일) + MySQL 색인
  (`article`, `article_symbol`). 기사 본문 컬럼은 어디에도 없다.
- KR 종목의 업종/테마 분류가 **없다** (뉴스 키워드 테마 8개 고정 사전만,
  `quant/analyze/themes.py`).
- LLM 어댑터는 이미 있다: `quant/adapters/narrate.py` —
  `OpenRouterNarrator`(무료 모델) / `ClaudeCliNarrator`(도구 전면 차단) /
  `NullNarrator`. 모든 예외는 `None` 으로 삼켜진다(`_quietly`).
- 구조화 LLM 출력의 선례: Phase 7.4 shadow-judge — 프롬프트 → 관용 파서 →
  **형식 위반은 버린다**(0점 오염 방지). 같은 패턴을 따른다.

## 핵심 결정 — 수혜주 관계 추출 (접근 ①, 승인됨)

**LLM 후보 생성 + 결정론 검증 + 관계 사전 축적.**

1. 야간 배치가 유망 종목(뉴스 언급 상위 + 랭킹 보드 등장)의 기사 **본문**을 읽는다.
2. OpenRouter 무료 모델이 "수혜/공급/경쟁 후보 + 한줄 이유"를 제안한다.
   LLM 은 **후보만** 낸다 — 점수를 정하지 않는다.
3. 후보는 뉴스 코퍼스 재검색으로 **결정론 채점**된다(언급 수·논지 태그·동시출현).
   임계 통과분만 관계 사전에 들어간다.
4. 검증된 관계는 MySQL `relation` 테이블에 축적된다.
   **LLM 이 죽으면 신규 발견만 멈추고, 리포트는 사전 캐시로 계속 발행된다.**

기각한 대안: ② 정적 공급망 사전(초기 구축 거대·낡음), ③ 동시출현 통계만
(설명 불가·표본 수개월 필요).

## 구성요소 (평면 준수 — trade 무접촉)

| 구성요소 | 평면 | 파일 (신규) | 역할 |
|---|---|---|---|
| 본문 수집기 | collect | `quant/collect/sources/article_body.py` | 채점 대상 종목의 기사만 본문 fetch. 전량 아님. rate-limit, 실패는 건너뜀 |
| 업종/테마 수집기 | collect | `quant/collect/sources/naver_sector.py` | 네이버 업종·테마 → KR symbol↔sector/theme 매핑. US 는 기존 GICS ETF 매핑 재사용 |
| 관계 추출 배치 | analyze | `quant/analyze/relations.py` | 본문 → LLM 후보 → 코퍼스 재검증 채점 → 사전 반영. 순수 로직과 I/O 분리 |
| 스키마 | adapters | `quant/adapters/schema/003_relations.sql` | `relation`, `sector_map` 테이블 (멱등 적재) |
| 진입점 | apps | `report_cli deepdive --market {KR,US}` | 야간 배치 1커맨드. 크론에서 호출 |
| engine JSON 확장 | analyze | `render.machine_payload()` 수정 | `symbols[].sector`, `symbols[].relations[]` 추가 |

### `relation` 테이블

```
relation(src_symbol, dst_symbol, kind ENUM('beneficiary','supplier','competitor'),
         reason VARCHAR,          -- LLM 한줄 이유 (표시용)
         evidence_score INT,      -- 결정론 채점 결과 (0~100)
         first_seen, last_verified,
         UNIQUE(src_symbol, dst_symbol, kind))
```

- `last_verified` 가 오래되면(기준: 30일) 재검증 대상. 재검증 실패 시 점수만
  깎고 행은 남긴다(이력 보존) — 리포트 노출은 임계 이상만.

### 검증 채점 (결정론, 순수 함수)

후보 (src, dst, kind, reason)에 대해:
- dst 가 상장 매핑(KIND/티커)에 존재하는가 — 없으면 즉시 폐기 (shadow-judge 와
  같은 "목록 밖은 버린다" 규칙)
- 코퍼스 증거: 최근 N일 내 dst 언급 기사 수, src·dst 동시출현 수, dst 기사의
  논지 태그(TREND/EVENT) 분포
- 가중치·임계는 **구현 시 실데이터로 조정한다** (감으로 정하지 않는다 —
  뉴스 방향 규칙을 1,425건에 돌려 조정했던 선례를 따른다)

### 저장 축소 (요구 4)

본문은 추출·채점이 끝나면 **폐기**한다. 남기는 것: `link`, `link_sha`,
구조화 결과(관계·태그·점수). 기존 retention 7일 유지. 본문을 디스크에
영속화하는 테이블/파일을 만들지 않는다(EC2 1.8GB).

## 스케줄

- `deepdive KR`: 리포트(07:50) 전 야간, 05:00 시작 상한 07:00.
- `deepdive US`: 리포트(19:50) 전, 17:30 시작 상한 19:30.
- 상한 초과 시 부분 결과로 종료(그때까지 검증된 관계만 반영). 리포트는
  deepdive 완료 여부와 무관하게 정시 발행.
- 시장 시간 재조정(서브프로젝트 D — NXT 08:00 / 9월 KRX 07:00 확대)이 오면
  이 시각만 이동한다. 설계는 "리포트 발행 T-마감" 상대 시각으로 기술.

## 실패 모드

| 실패 | 동작 |
|---|---|
| OpenRouter 불통 | 신규 후보 생성 스킵. 사전 캐시로 리포트 발행. `missing_sources` 에 표기 |
| 본문 fetch 실패(개별) | 그 기사만 건너뜀. 실패를 빈 본문으로 위장하지 않는다 |
| LLM 형식 위반 | 그 후보만 버림 (0점 아님 — shadow-judge 규칙) |
| deepdive 크론 자체 실패 | `record_run()` 으로 감시에 잡힘. 리포트는 어제 사전으로 발행 |

## 비목표 (다른 서브프로젝트)

- 리포트 UI(뉴스 전량 탭·트리 렌더·테마 시세 화면·수급 기간 뷰) → **B**
- 엔진 자동편입·장타/단타 분류 → **C** (단, A 는 태그 어휘 TREND/EVENT 를 보존해 C 의 기반을 남긴다)
- 장시간 재조정 → **D**
- 전 종목 기준가 + forward_return 채점 → **E**
- 기사 본문의 영속 보관, 웹 프레임워크 도입(ADR-0002 유지)

## 검증

- 파서·채점기·사전 반영 로직은 순수 함수 + **실스키마 픽스처**(스키마 추측 금지)
- `tests/test_architecture.py` 통과 — collect/analyze 는 trade 를 임포트하지 않는다
- 밀폐: 네트워크 없는 단위 테스트 + 실환경(EC2) 1회 수동 실행으로 산출물 직접 세기
  ("돌았다"≠"일했다")
