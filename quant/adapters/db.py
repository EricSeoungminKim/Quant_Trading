"""분석 저장소(MySQL) 어댑터 — 연결과 마이그레이션.

**거래 평면은 이 모듈을 임포트할 수 없다** (`tests/test_architecture.py` 가
`quant.trade` 의 DB 드라이버 임포트를 금지한다). 09:15 에 MySQL 이 딸꾹질했다고
매매가 멈추면 안 되기 때문이다. 엔진은 항상 로컬 JSONL 에 쓰고, 적재기
(`quant/control/warehouse.py`)가 그걸 옮긴다.

연결 정보가 없으면 `None` 을 돌려준다 — 예외가 아니다. DB 는 선택 사항이고,
없다고 리포트나 수집이 죽으면 안 된다.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"

_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migration (
  name       VARCHAR(128) PRIMARY KEY,
  applied_at DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def dsn_from_env(env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """환경변수에서 접속 정보. `MYSQL_DATABASE` 가 없으면 None(=DB 미사용)."""
    e = os.environ if env is None else env
    database = (e.get("MYSQL_DATABASE") or "").strip()
    if not database:
        return None
    return {
        "host": (e.get("MYSQL_HOST") or "127.0.0.1").strip(),
        "port": int((e.get("MYSQL_PORT") or "3306").strip()),
        "user": (e.get("MYSQL_USER") or "quant").strip(),
        "password": e.get("MYSQL_PASSWORD") or "",
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
    }


def connect(env: dict[str, str] | None = None):
    """연결하거나 None. 드라이버 미설치·접속 실패 모두 None 이고 경고만 남긴다."""
    dsn = dsn_from_env(env)
    if dsn is None:
        return None
    try:
        import pymysql
    except ImportError:
        log.warning("pymysql 미설치 — 분석 저장소 적재를 건너뛴다")
        return None
    try:
        return pymysql.connect(**dsn)
    except Exception as e:  # 접속 실패가 호출자를 죽이지 않는다
        log.warning("MySQL 접속 실패(%s) — 분석 저장소 적재를 건너뛴다", type(e).__name__)
        return None


def split_statements(sql: str) -> list[str]:
    """`--` 주석을 걷어내고 `;` 로 나눈다.

    주석을 먼저 지우는 이유: 주석 안의 `;` 하나가 DDL 을 반토막 내는데, 그러면
    테이블이 절반만 생기고 에러는 엉뚱한 곳에서 난다.
    """
    body = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in body.split(";") if s.strip()]


def pending_migrations(applied: set[str], schema_dir: Path | None = None) -> list[Path]:
    """아직 안 적용된 마이그레이션을 파일명 순으로."""
    d = SCHEMA_DIR if schema_dir is None else schema_dir
    return sorted(p for p in d.glob("*.sql") if p.name not in applied)


def migrate(conn, schema_dir: Path | None = None) -> list[str]:
    """미적용 마이그레이션을 순서대로 적용하고 적용한 이름을 돌려준다.

    각 파일은 자기 트랜잭션에서 돈다. MySQL 의 DDL 은 암묵적 커밋이라 롤백이
    안 되므로, 한 파일이 실패하면 그 파일은 기록되지 않고 다음 실행에서 다시
    시도된다 — 그래서 DDL 은 전부 `IF NOT EXISTS` 여야 한다.
    """
    applied_names: list[str] = []
    with conn.cursor() as cur:
        cur.execute(_MIGRATION_TABLE)
        conn.commit()
        cur.execute("SELECT name FROM schema_migration")
        applied = {row[0] for row in cur.fetchall()}

    for path in pending_migrations(applied, schema_dir):
        with conn.cursor() as cur:
            for stmt in split_statements(path.read_text(encoding="utf-8")):
                cur.execute(stmt)
            cur.execute(
                "INSERT INTO schema_migration (name, applied_at) VALUES (%s, NOW())",
                (path.name,),
            )
        conn.commit()
        applied_names.append(path.name)
        log.info("마이그레이션 적용: %s", path.name)
    return applied_names


def upsert_sql(table: str, columns: list[str], update: list[str]) -> str:
    """멱등 INSERT. 같은 아티팩트를 두 번 적재해도 행이 두 배가 되면 안 된다.

    `update` 는 충돌 시 갱신할 컬럼 — 여기 안 든 컬럼은 최초 값이 유지된다
    (예: `first_seen` 은 그대로 두고 `last_seen`/`seen_count` 만 올린다).

    `update` 가 비면 **`INSERT IGNORE`** 다. 갱신할 게 없는 불변 행(체결)을
    그냥 `INSERT` 로 두면 두 번째 적재가 `IntegrityError` 로 죽는다 — 2026-08-13
    실환경에서 실제로 그랬다. 가짜 연결 테스트는 SQL 문자열만 보느라 이걸 못
    잡았다("ON DUPLICATE KEY 가 없다"는 확인이 "멱등하다"를 뜻하지 않는다).
    """
    cols = ", ".join(f"`{c}`" for c in columns)
    marks = ", ".join(["%s"] * len(columns))
    if not update:
        return f"INSERT IGNORE INTO `{table}` ({cols}) VALUES ({marks})"
    sets = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in update)
    return f"INSERT INTO `{table}` ({cols}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {sets}"
