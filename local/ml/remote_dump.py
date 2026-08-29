"""EC2 MySQL `selection` ⋈ `forward_return`(D+1, D+5) 내보내기 — 2026-08-30
신설, 2026-08-30 v2 확장(D+5 라벨 + `baseline_score100` 조건부 포함).

**이 파일은 EC2 에 배포되지 않는다.** `local/ml/run.sh`가 SSH 로 원격 파이썬의
표준입력에 이 스크립트 내용을 그대로 흘려보내(`python3 -`) 실행한다 — EC2
파일시스템에 아무것도 남기지 않는 읽기 전용 SELECT 한 번이다.

비밀번호는 원격 `.env.local`에서만 읽는다(`run.sh`가 원격 셸에서
`. .env.local`로 먼저 로드한 뒤 이 스크립트를 실행한다) — 로컬 커맨드라인이나
프로세스 목록에 노출되지 않는다.

`quant.analyze.ml_scorer.FEATURE_NAMES`를 그대로 재사용한다 — 운영 채점기
(`quant/apps/cli.py cmd_ml_scorer`)가 학습에 쓰는 피처 목록과 다른 목록을
로컬이 따로 정의하면 그 자체가 조용한 불일치가 된다. SQL 도 그 함수의
쿼리와 동일하다(시장/날짜 필터만 뺐다 — 오프라인 연구용이라 전체 과거를 본다).

## `baseline_score100`를 왜 조건부로만 SELECT하나

2026-08-30 확인: MySQL `selection` 테이블에는 이 컬럼이 없다
(`quant/adapters/schema/001_initial.sql`에 없고, 적재 코드
`quant/control/warehouse.SELECTION_COLS`도 옮기지 않는다 — JSONL 선정
원장에는 있는 필드지만 DB로 넘어오지 않는다). 존재를 가정하고 SELECT에 넣으면
`Unknown column` 오류로 이 스크립트 전체가 죽는다 — 바로 이 저장소가
`forward_return` 테이블에서 한 번 겪은 실패 패턴이다
(`quant/adapters/schema/005_forward_return_rebuild.sql` 참고). 그래서 매
실행 `information_schema`로 컬럼 존재를 먼저 확인하고, 있을 때만 SELECT에
넣는다 — 스키마가 나중에 갖춰지면 이 스크립트를 고치지 않아도 자동으로
켜진다(`quant/research/ml_train.py` 모듈 docstring '동기화 대상 제안' 참고).
"""
import json
import sys

from quant.adapters.db import connect
from quant.analyze import ml_scorer

conn = connect()
if conn is None:
    print(json.dumps([]))
    sys.exit(0)

import pymysql  # connect()가 이미 성공했으므로 설치돼 있다

cols_sql = ", ".join(f"s.{c}" for c in ml_scorer.FEATURE_NAMES)
try:
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'selection' "
            "AND column_name = 'baseline_score100'"
        )
        has_baseline = bool(cur.fetchone()["n"])
        baseline_sql = ", s.baseline_score100 AS baseline_score100" if has_baseline else ""

        cur.execute(
            "SELECT s.session_date AS session_date, s.symbol AS symbol, "
            f"s.market AS market, {cols_sql}{baseline_sql}, "
            "fr1.return_bps AS return_bps, fr5.return_bps AS return_bps_d5 "
            "FROM selection s "
            "JOIN forward_return fr1 "
            "ON s.market = fr1.market AND s.symbol = fr1.symbol "
            "AND s.session_date = fr1.session_date AND fr1.horizon_days = 1 "
            "LEFT JOIN forward_return fr5 "
            "ON s.market = fr5.market AND s.symbol = fr5.symbol "
            "AND s.session_date = fr5.session_date AND fr5.horizon_days = 5"
        )
        rows = cur.fetchall()
finally:
    conn.close()

for r in rows:
    r["session_date"] = str(r["session_date"])

print(json.dumps(rows, ensure_ascii=False, default=str))
