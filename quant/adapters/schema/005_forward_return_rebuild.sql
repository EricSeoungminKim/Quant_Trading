-- forward_return 스키마 정합 — 001 과 002 가 **같은 이름의 다른 테이블**을
-- 정의했고, 둘 다 `CREATE TABLE IF NOT EXISTS` 라 002 가 조용히 무시됐다.
--
-- 그래서 DB 에는 001 의 참조형(selection_id FK + ret_pct/base_price/end_price)이
-- 남았는데 적재 코드(`warehouse.FORWARD_COLS`)는 002 의 독립형(market/symbol/
-- session_date/return_bps/asof_date)을 쓴다 → 매 적재가
-- `Unknown column 'market' in 'field list'` 로 죽었다. 2026-08-28 실측: 이
-- 테이블은 **0행**이다(한 번도 성공한 적이 없다는 뜻) — 그래서 버릴 데이터가 없다.
--
-- 정본은 002 다: 코드가 그 형태를 쓰고, 지평별 행 분리("D+1 은 채웠는데 D+20 은
-- 아직"이 정상 상태)라는 설계 근거가 그 파일에 문서화돼 있다. 001 의 FK 형은
-- selection.id 를 알아야 해서 파일 원장(자연키)에서 곧장 적재할 수 없다.
--
-- 교훈: `IF NOT EXISTS` 는 스키마 진화를 조용히 삼킨다. 같은 이름을 다시 정의할
-- 때는 이 파일처럼 **명시적 교체 마이그레이션**을 써야 한다.
DROP TABLE IF EXISTS forward_return;

CREATE TABLE forward_return (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  market        ENUM('KR','US')  NOT NULL,
  symbol        VARCHAR(16)      NOT NULL,
  session_date  DATE             NOT NULL,
  horizon_days  SMALLINT         NOT NULL,   -- 1 / 5 / 20 (거래일)
  return_bps    DOUBLE           NOT NULL,
  -- 평일 근사로 센 거래일이라 실제 기준 날짜를 함께 남긴다(002 의 근거 그대로).
  asof_date     DATE             NOT NULL,
  UNIQUE KEY uq_fwd (market, symbol, session_date, horizon_days),
  KEY ix_fwd_day (session_date, horizon_days)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
