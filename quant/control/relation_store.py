"""관계 사전(수혜주/공급사/경쟁사) MySQL 색인 — `warehouse.py` 와 같은 자리·같은 계약.

**아티팩트가 진실, 여기는 색인이다** (001/003 과 같은 원칙). `upsert_sql`(adapters/db.py)을
그대로 재사용한다 — 손으로 SQL 을 다시 쓰면 그 함수가 이미 겪은 사고
(INSERT IGNORE 로 갱신이 조용히 무시된 2026-08-13 실환경 회귀)를 재발명할 위험이 있다.

INSERT IGNORE 가 아니라 ON DUPLICATE KEY UPDATE 다 — 재검증(evidence_score·
last_verified 갱신)이 조용히 무시되면 사전이 낡은 채 신선해 보인다.
"""
from __future__ import annotations

from quant.adapters.db import upsert_sql

RELATION_COLS = ["src_symbol", "dst_symbol", "kind", "reason",
                  "evidence_score", "first_seen", "last_verified",
                  "via_theme", "source"]
# first_seen 은 최초값을 지킨다 — 갱신절에 넣으면 "언제부터 성립한 관계인가"를 잃는다.
# via_theme·source 는 갱신절에 있다 — 같은 (src,dst,kind) 를 naver_theme 이
# 나중에 다시 발견하면(F 설계: naver_theme 이 llm 을 이긴다) 출처가 갱신돼야
# 한다. 안 그러면 행이 새로 안 생기니(UNIQUE KEY) 예전 llm 출처가 영구히 남는다.
RELATION_UPDATE = ["reason", "evidence_score", "last_verified", "via_theme", "source"]


def upsert_relations(conn, rows: list[dict]) -> int:
    """관계 행 upsert. 입력 키는 Task 3 `merge_relation` 과 같은 어휘
    (`src`/`dst`/`kind`/`reason`/`evidence_score`/`first_seen`/`last_verified`)다.
    `via_theme`/`source` 는 004 스키마 확장(서브프로젝트 F) — 행에 키가 없으면
    `via_theme=None`, `source="llm"`(기존 관계는 전부 LLM 산이었다는 뜻).
    """
    if not rows:
        return 0
    tuples = [
        (r["src"], r["dst"], r["kind"], r["reason"], r["evidence_score"],
         r["first_seen"], r["last_verified"], r.get("via_theme"), r.get("source", "llm"))
        for r in rows
    ]
    with conn.cursor() as cur:
        cur.executemany(upsert_sql("relation", RELATION_COLS, RELATION_UPDATE), tuples)
    conn.commit()
    return len(tuples)


def load_relations(conn, src_symbols: list[str]) -> dict[str, list[dict]]:
    """src_symbol 별 관계 목록. 반환 dict 의 행 어휘는 `upsert_relations` 입력과 같다."""
    if not src_symbols:
        return {}
    marks = ", ".join(["%s"] * len(src_symbols))
    out: dict[str, list[dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT src_symbol, dst_symbol, kind, reason, evidence_score,"
            f" first_seen, last_verified FROM relation WHERE src_symbol IN ({marks})",
            tuple(src_symbols),
        )
        for src, dst, kind, reason, score, first_seen, last_verified in cur.fetchall():
            out.setdefault(src, []).append({
                "src": src, "dst": dst, "kind": kind, "reason": reason,
                "evidence_score": score,
                "first_seen": str(first_seen), "last_verified": str(last_verified),
            })
    return out
