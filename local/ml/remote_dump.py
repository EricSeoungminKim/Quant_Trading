"""EC2 MySQL `selection` ⋈ `forward_return`(D+1) 내보내기 — 2026-08-30 신설.

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
            "SELECT s.session_date AS session_date, s.symbol AS symbol, "
            f"s.market AS market, {cols_sql}, fr.return_bps AS return_bps "
            "FROM selection s JOIN forward_return fr "
            "ON s.market = fr.market AND s.symbol = fr.symbol "
            "AND s.session_date = fr.session_date "
            "WHERE fr.horizon_days = 1"
        )
        rows = cur.fetchall()
finally:
    conn.close()

for r in rows:
    r["session_date"] = str(r["session_date"])

print(json.dumps(rows, ensure_ascii=False, default=str))
