# quant/report/

## 한 줄 정의

**자체 리포트 파이프라인** — KR 08:00 / US 20:00 발행되는 데일리 마켓 리포트의
조립+렌더 계층(2026-08-13 별도 `market_report` 저장소를 흡수). `quant/analyze/`
바로 위, `quant/collect/`가 만든 원자재를 소비하는 쪽. 틀리면: 리포트가 안
나가거나(발행 실패), 유니버스 자동 편입의 입력(engine.json)이 틀려서
`own_brief.sh`가 나쁜 종목을 편입한다.

## 주요 파일 지도

- `model.py` — `ReportModel`: 리포트가 그릴 대상 전부를 담는 순수 DTO.
- `paths.py` — 스냅샷/출력/캐시/원장 디렉터리와 `engine.json` 경로 헬퍼.
- `collect/` — 리포트 세션별 데이터 조립(수집이 아니라 **조립** — 실제 원자재
  수집은 `quant/collect/`가 함). `snapshot.py`(수집 타이밍), `core.py`(스냅샷→
  파생물: 언급/랭킹/트렌딩/시세/베이스라인/스탠스/machine_payload), `news.py`
  (뉴스/공시/리서치/시황/Executive Summary/섹션 AI 해석), `sector.py`(업종/테마
  시세 + 외국인 수급 탑다운), `quotes.py`(KR 시세 조회, KIND 부재 폴백),
  `intraday.py`(당일 단타 스코어러 후보), `midterm.py`(중기 관심 종목),
  `index_outlook.py`(지수 전망 조립), `telegram.py`(공개채널 카드 뷰),
  `briefs.py`(유튜브/블로그/텔레그램 브리핑 수집기), `close.py`(마감 포지션
  리포트 전용 뷰), `uswrap.py`(마감 종합: 미국장+전일 한국장), `carryover.py`/
  `holiday_synthesis.py`(휴장 기간 집계), `ledger.py`(선정/수급/외국인수급/
  뉴스겹침 원장 기록), `agent_interpret.py`(AI 심층 해석 입력 수집 + 에이전트 실행).
- `render/` — `html.py`(HTML/`engine.json` 렌더러 — `ReportModel`만 보고 그림),
  `telegram.py`(발행 알림 결정론 요약 렌더러).

## 핵심 불변식

- `collect/`(이 디렉터리 하위, 조립 책임)와 최상위 `quant/collect/`(원자재
  수집 책임)를 혼동하지 않는다 — 이름이 같지만 역할이 다르다.
- `render/html.py`는 `ReportModel`/`CloseReportModel`만 보고 그린다 — 렌더 단계에서
  새로운 데이터를 조회하지 않는다(조립과 렌더의 책임 분리).
- `engine.json`은 사람용 HTML과 별도로 **기계가 읽는 계약**이다 — 필드명을
  바꾸면 `server/scripts/own_brief.sh`(자동 편입)와 `quant/analyze/market_brief.py`
  (어휘 번역)가 조용히 끊긴다.

## 데이터 흐름

**상류**: `quant/collect/*`(원자재), `quant/analyze/*`(점수/등급/요약).
**하류**: `quant/apps/report_cli.py`가 이 평면을 호출해 HTML+`engine.json`을
`out/YYYY/MM/DD/{KR,US}_engine.json`에 쓰고, `server/scripts/own_brief.sh`가
그 JSON을 읽어 `watch-score`로 유니버스에 자동 편입한다.

## 손대기 전에

- `uv run python -m quant.apps.report_cli --help` — 리포트 스모크(루트 CLAUDE.md
  필수 검증 커맨드).
- `engine.json`의 필드를 바꿨다면 `quant/analyze/market_brief.py`(어휘 번역)와
  `server/scripts/own_brief.sh`/`brief_from_report.py`도 함께 확인 — 리포트
  어휘(NEWS/RANK)를 엔진 어휘(EVENT/TREND)로 번역하는 지점이 끊기면
  news_momentum이 리포트 종목을 조용히 못 잡는다(server/CLAUDE.md 실사고 기록).
