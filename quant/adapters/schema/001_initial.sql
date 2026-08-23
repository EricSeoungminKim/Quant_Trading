-- 분석 저장소 초기 스키마.
--
-- **아티팩트가 진실, 여기는 색인이다.** 거래 엔진은 이 DB 를 모른다 — 09:15 에
-- MySQL 이 딸꾹질했다고 매매가 멈추면 안 된다. 엔진은 항상 로컬 JSONL 에 쓰고,
-- 적재기가 그걸 옮긴다. DB 가 죽으면 분석이 뒤처질 뿐이다.
--
-- 이 DB 가 실제로 푸는 문제: "이 체결로 이어진 기사가 뭐였나"를 SQL 한 방으로.
-- 지금은 JSONL 여러 개를 손으로 조인해야 하고, 그게 개선 루프의 핵심 질문이다.

CREATE TABLE IF NOT EXISTS article (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  market        ENUM('KR','US')  NOT NULL,
  -- 정규화 링크의 SHA-256. 링크 자체는 길이가 들쭉날쭉해 인덱스를 못 건다.
  link_sha      CHAR(64)         NOT NULL,
  link          VARCHAR(1024)    NOT NULL,
  title         VARCHAR(512)     NOT NULL,
  outlet        VARCHAR(128)     NOT NULL DEFAULT '',
  -- 발행시각 미상을 수집시각으로 위장하지 않는다. NULL 은 "모른다"다.
  published     DATETIME         NULL,
  published_known TINYINT(1)     NOT NULL DEFAULT 0,
  first_seen    DATETIME         NOT NULL,
  last_seen     DATETIME         NOT NULL,
  -- 중복은 버리지 않고 센다 — 3시간째 재보도되는 기사는 다른 사건이다.
  seen_count    INT              NOT NULL DEFAULT 1,
  collect_date  DATE             NOT NULL,
  UNIQUE KEY uq_article (market, link_sha),
  KEY ix_article_day (market, collect_date),
  KEY ix_article_published (market, published)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS article_symbol (
  article_id    BIGINT           NOT NULL,
  symbol        VARCHAR(16)      NOT NULL,
  PRIMARY KEY (article_id, symbol),
  KEY ix_as_symbol (symbol),
  CONSTRAINT fk_as_article FOREIGN KEY (article_id) REFERENCES article(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 리포트가 그날 그 종목을 어떻게 봤는지의 스냅샷. 후보로 안 뽑힌 종목도 남긴다
-- — 안 산 종목이 올랐는지를 모르면 선정 규칙을 채점할 수 없다.
CREATE TABLE IF NOT EXISTS selection (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  session_date  DATE             NOT NULL,
  market        ENUM('KR','US')  NOT NULL,
  symbol        VARCHAR(16)      NOT NULL,
  name          VARCHAR(128)     NULL,
  is_candidate  TINYINT(1)       NOT NULL DEFAULT 0,
  -- 점수는 전부 NULL 허용. "0점"과 "계산 못 했다"는 전혀 다른 사건이다.
  ai_score100          SMALLINT  NULL,
  trending_score100    SMALLINT  NULL,
  trending_label       VARCHAR(32) NULL,
  news_articles_today  INT       NULL,
  news_streak_days     INT       NULL,
  in_ranking           TINYINT(1) NULL,
  ranking_bullish      TINYINT(1) NULL,
  best_board_rank      INT       NULL,
  n_boards             INT       NULL,
  relative_volume      DECIMAL(10,3) NULL,
  foreign_buy_streak   INT       NULL,
  inst_buy_streak      INT       NULL,
  upside_pct           DECIMAL(8,2)  NULL,
  close_price          DECIMAL(18,4) NULL,   -- 전방 수익률의 기준가
  change_pct           DECIMAL(8,2)  NULL,
  origin               VARCHAR(16)   NULL,
  -- 선정 당시 파라미터 스냅샷. 나중에 "그때 문턱이 뭐였나"를 답해야 한다.
  params        JSON             NULL,
  UNIQUE KEY uq_selection (session_date, market, symbol),
  KEY ix_selection_symbol (symbol, session_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS trade (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  -- 원장 한 줄의 내용 해시. 원장에 id 가 없어서 자연키가 없다 — 내용 해시를
  -- 쓰면 같은 파일을 두 번 적재해도 행이 두 배가 되지 않는다.
  fill_sha      CHAR(64)         NOT NULL,
  ts            DATETIME         NOT NULL,
  trade_date    DATE             NOT NULL,
  strategy_id   VARCHAR(64)      NOT NULL,
  symbol        VARCHAR(16)      NOT NULL,
  market        ENUM('KR','US')  NOT NULL,
  side          ENUM('buy','sell') NOT NULL,
  qty           DECIMAL(18,4)    NOT NULL,
  price         DECIMAL(18,4)    NOT NULL,
  fee           DECIMAL(18,4)    NOT NULL DEFAULT 0,
  realized_pnl  DECIMAL(18,4)    NOT NULL DEFAULT 0,
  reason        VARCHAR(255)     NULL,
  UNIQUE KEY uq_trade (fill_sha),
  KEY ix_trade_symbol (symbol, ts),
  KEY ix_trade_strategy (strategy_id, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- **사후에 갱신되는 유일한 테이블.** append-only JSONL 이 못 하는 일이고,
-- 판단 리더보드(Phase 7)가 반드시 필요로 하는 것이다.
CREATE TABLE IF NOT EXISTS forward_return (
  selection_id  BIGINT           NOT NULL,
  horizon_days  SMALLINT         NOT NULL,   -- 1 / 5 / 20
  ret_pct       DECIMAL(10,4)    NOT NULL,
  base_price    DECIMAL(18,4)    NOT NULL,
  end_price     DECIMAL(18,4)    NOT NULL,
  filled_at     DATETIME         NOT NULL,
  PRIMARY KEY (selection_id, horizon_days),
  CONSTRAINT fk_fr_selection FOREIGN KEY (selection_id) REFERENCES selection(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
