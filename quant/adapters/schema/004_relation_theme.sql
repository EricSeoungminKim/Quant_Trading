-- 테마 기반 관계 확장 (서브프로젝트 F). `relation` 에 근거 출처를 남긴다 —
-- via_theme(어떤 테마를 통해 연결됐나 = 어떤 부품·섹터인가), source
-- (naver_theme|llm, 근거가 네이버 편입사유인지 LLM 문장인지). 섞으면 나중에
-- 품질을 가릴 수 없다(F 설계 스펙).
--
-- 003 이 만든 relation 은 이미 행이 쌓여 있을 수 있다 — 그 행들은 전부 LLM 산
-- 관계이므로 DEFAULT 'llm' 로 소급 적용한다(NOT NULL 이라 DEFAULT 필수).
--
-- `IF NOT EXISTS` 를 ADD COLUMN 에 붙이지 않는다 — MariaDB 확장 문법이라
-- MySQL 8 에는 없다(2026-08-16 리뷰 결함 C1: 크론이 매 실행 크래시하고
-- 컬럼은 영구히 안 생기는 상태였다). 001~003 과 같은 방식대로
-- `db.migrate()` 의 `schema_migration` 파일명 단위 1회 적용이 멱등성을
-- 이미 보장한다 — 이 파일이 다시 실행될 일 자체가 없다.
ALTER TABLE relation
  ADD COLUMN via_theme VARCHAR(64) NULL,
  ADD COLUMN source VARCHAR(16) NOT NULL DEFAULT 'llm';
