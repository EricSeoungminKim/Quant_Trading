"""관계 사전 MySQL 색인 테스트 — `test_warehouse.py` 의 가짜 연결 관례를 따른다.

SQL 문자열은 `quant.adapters.db.upsert_sql` 이 만든다(그 함수의 계약은
`test_warehouse.py::test_upsert_without_update_columns_uses_insert_ignore` 등이
이미 검증한다). 여기서는 그 함수가 관계 테이블에도 올바르게 배선됐는지 —
ON DUPLICATE KEY UPDATE 를 쓰고 first_seen 을 갱신절에서 뺐는지 — 만 본다.

**한계**: 이 가짜 연결은 SQL 문자열과 호출 횟수만 본다. 실제 MySQL 유니크키
충돌·자료형 강제는 보지 않는다 — 그건 Task 7 의 EC2 스모크로 넘긴다.
"""
from __future__ import annotations

from quant.control.relation_store import load_relations, upsert_relations

_ROW = {"src": "005930", "dst": "042700", "kind": "beneficiary",
        "reason": "HBM 장비", "evidence_score": 70,
        "first_seen": "2026-08-15", "last_verified": "2026-08-15"}


class _Cur:
    def __init__(self, log: list, rows: list):
        self._log, self._rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._log.append(("execute", sql, params))

    def executemany(self, sql, seq):
        self._log.append(("executemany", sql, list(seq)))

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, rows: list | None = None):
        self.log: list = []
        self.commits = 0
        self._rows = rows or []

    def cursor(self):
        return _Cur(self.log, self._rows)

    def commit(self):
        self.commits += 1


def test_upsert_is_idempotent_style():
    """같은 행을 두 번 넣어도 예외가 없다 — 재검증이 죽으면 안 된다."""
    conn = _Conn()
    assert upsert_relations(conn, [_ROW, _ROW]) == 2
    calls = [c for c in conn.log if c[0] == "executemany"]
    assert len(calls) == 1
    sql = calls[0][1].upper()
    # INSERT IGNORE 가 아니라 ON DUPLICATE KEY UPDATE — 재검증이 UPDATE 로 남아야 한다.
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "INSERT IGNORE" not in sql
    assert conn.commits == 1


def test_upsert_does_not_touch_first_seen_on_update():
    conn = _Conn()
    upsert_relations(conn, [_ROW])
    sql = conn.log[0][1].upper()
    upd = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
    assert "FIRST_SEEN" not in upd  # 이력 보존 — 갱신 절에 first_seen 이 있으면 안 된다


def test_upsert_empty_rows_commits_nothing():
    conn = _Conn()
    assert upsert_relations(conn, []) == 0
    assert conn.log == [] and conn.commits == 0


def test_load_relations_groups_rows_by_src_symbol():
    rows = [("005930", "042700", "beneficiary", "HBM 장비", 70,
              "2026-08-15", "2026-08-15")]
    conn = _Conn(rows=rows)
    out = load_relations(conn, ["005930"])
    assert out == {"005930": [_ROW]}


def test_load_relations_empty_symbols_skips_query():
    conn = _Conn()
    assert load_relations(conn, []) == {}
    assert conn.log == []


def test_upsert_sends_via_theme_and_source_columns():
    """F(테마 기반 탐색) 확장 — 004 스키마의 via_theme/source 가 upsert SQL 에
    실려야 한다. 값이 있는 행은 그대로 전달된다."""
    conn = _Conn()
    row = dict(_ROW, via_theme="정유", source="naver_theme")
    upsert_relations(conn, [row])
    sql, params = conn.log[0][1], conn.log[0][2][0]
    assert "`via_theme`" in sql and "`source`" in sql
    assert params[-2:] == ("정유", "naver_theme")


def test_upsert_defaults_via_theme_none_and_source_llm_when_missing():
    """키가 없는 행(기존 LLM 경로 산출물)은 via_theme=None, source='llm' 기본값으로
    간다 — 과거 호출부(LLM 전용 경로)를 건드리지 않고도 새 컬럼을 채운다."""
    conn = _Conn()
    upsert_relations(conn, [_ROW])  # _ROW 에는 via_theme/source 키가 없다
    params = conn.log[0][2][0]
    assert params[-2:] == (None, "llm")
