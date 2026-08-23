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
