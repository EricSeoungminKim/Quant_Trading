-- 판단 귀속 + 전방 수익률. Phase 7.
--
-- **아티팩트가 진실, 여기는 색인이다** (001 과 같은 원칙). 판단은 JSONL 에 먼저 쌓이고
-- 적재기가 옮긴다.
--
-- 이 테이블이 푸는 문제: "결정론적 스코어러와 LLM 중 누가 더 잘 골랐나"를 SQL 한 방으로.
-- 그러려면 **같은 입력을 본 판단끼리** 묶여야 하고, 그 열이 `input_hash` 다.

CREATE TABLE IF NOT EXISTS judgment (
  id               BIGINT AUTO_INCREMENT PRIMARY KEY,
  -- 누가 판단했나. 같은 모델의 프롬프트 변경도 다른 생산자로 취급한다 —
  -- 안 그러면 프롬프트를 바꾼 전후가 한 표본으로 섞여 성적이 뭉개진다.
  producer         VARCHAR(64)      NOT NULL,
  producer_version VARCHAR(32)      NOT NULL,
  -- **이 열이 리더보드를 가능하게 한다.** 같은 입력을 본 판단끼리만 비교할 수 있다.
  -- 생산자 이름도 판단 결과도 이 해시에 들어가지 않는다(core.models.input_hash).
  input_hash       CHAR(64)         NOT NULL,
  market           ENUM('KR','US')  NOT NULL,
  symbol           VARCHAR(16)      NOT NULL,
  session_date     DATE             NOT NULL,
  -- 생산자별 척도다 — 그대로 비교하지 않고 순위로 환산해 쓴다(leaderboard.daily_rank_ic).
  -- NULL 은 "채점 못 함"이고 0 과 다르다.
  score            DOUBLE           NULL,
  verdict          ENUM('pass','reject') NOT NULL,
  rationale        VARCHAR(255)     NOT NULL DEFAULT '',
  ts               VARCHAR(32)      NOT NULL DEFAULT '',
  -- 같은 생산자가 같은 입력을 같은 날 두 번 봐도 판단은 하나다(멱등 적재).
  UNIQUE KEY uq_judgment (producer, producer_version, input_hash, symbol, session_date),
  -- 리더보드 질의: 생산자별·날짜별로 긁는다.
  KEY ix_judgment_producer_day (producer, session_date),
  -- 같은 입력을 본 판단 묶기.
  KEY ix_judgment_input (input_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- 전방 수익률. **사후에 갱신된다** — append-only JSONL 이 잘 못 하는 일이고,
-- 착수보고서가 이걸 MySQL 도입의 근거로 들었다.
--
-- 지평별로 행을 나눈 이유: D+1 은 채웠는데 D+20 은 아직인 상태가 정상이다.
-- 한 행에 열 3개로 두면 "아직 안 채움"과 "0"이 섞이기 쉽다.
CREATE TABLE IF NOT EXISTS forward_return (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  market        ENUM('KR','US')  NOT NULL,
  symbol        VARCHAR(16)      NOT NULL,
  session_date  DATE             NOT NULL,
  horizon_days  SMALLINT         NOT NULL,   -- 1 / 5 / 20 (거래일)
  return_bps    DOUBLE           NOT NULL,
  -- **평일 근사로 센 거래일이라 실제 기준 날짜를 함께 남긴다.** 공휴일에 하루씩
  -- 밀릴 수 있고, 그때 "D+5 가 실제로 며칠 뒤였나"를 되짚을 수 있어야 한다.
  asof_date     DATE             NOT NULL,
  UNIQUE KEY uq_fwd (market, symbol, session_date, horizon_days),
  KEY ix_fwd_day (session_date, horizon_days)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
