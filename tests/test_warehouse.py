"""분석 저장소 적재 — 아티팩트가 진실이고 DB 는 색인이라는 규약의 테스트.

행 변환기는 순수 함수라 DB 없이 전부 검증한다. SQL 계층은 실행된 문장을 받아
적는 가짜 연결로 확인한다 — **멱등성(같은 파일을 두 번 적재해도 행이 늘지 않는다)**
이 여기서 가장 중요한 성질이다.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from quant.adapters.db import dsn_from_env, split_statements, upsert_sql
from quant.control.warehouse import (
    ARTICLE_COLS,
    ARTICLE_UPDATE,
    TRADE_COLS,
    article_row,
    fill_sha,
    ingest_articles,
    ingest_selections,
    ingest_trades,
    read_jsonl,
    selection_row,
    trade_row,
)


# ── 가짜 연결 ─────────────────────────────────────────────────────────────

class FakeCursor:
    def __init__(self, log: list, rows: list):
        self._log, self._rows = log, rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._log.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, seq):
        self._log.append(("executemany", " ".join(sql.split()), list(seq)))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows: list | None = None):
        self.log: list = []
        self.commits = 0
        self._rows = rows or []

    def cursor(self):
        return FakeCursor(self.log, self._rows)

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def statements(self, kind: str = "executemany") -> list[str]:
        return [s for k, s, _ in self.log if k == kind]

    def params(self, kind: str = "executemany") -> list:
        return [p for k, _, p in self.log if k == kind]


# ── 접속 설정 ─────────────────────────────────────────────────────────────

def test_no_database_configured_means_db_is_optional():
    """DB 는 선택 사항이다 — 설정이 없으면 예외가 아니라 None 이어야 수집·리포트가 산다."""
    assert dsn_from_env({}) is None
    assert dsn_from_env({"MYSQL_HOST": "x"}) is None


def test_dsn_defaults_are_local():
    d = dsn_from_env({"MYSQL_DATABASE": "quant"})
    assert d["host"] == "127.0.0.1" and d["port"] == 3306 and d["database"] == "quant"


def test_autocommit_is_off():
    """적재는 배치 단위로 커밋한다 — 중간에 죽으면 반쪽 상태가 남으면 안 된다."""
    assert dsn_from_env({"MYSQL_DATABASE": "q"})["autocommit"] is False


# ── DDL 분할 ──────────────────────────────────────────────────────────────

def test_comments_are_stripped_before_splitting():
    """주석 속 세미콜론 하나가 DDL 을 반토막 내면 테이블이 절반만 생긴다."""
    sql = "CREATE TABLE a (x INT); -- 주의; 세미콜론\nCREATE TABLE b (y INT);"
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert all(s.startswith("CREATE TABLE") for s in stmts)


def test_real_schema_file_splits_into_create_statements():
    from quant.adapters.db import SCHEMA_DIR
    stmts = split_statements((SCHEMA_DIR / "001_initial.sql").read_text(encoding="utf-8"))
    assert len(stmts) == 5, "article/article_symbol/selection/trade/forward_return"
    assert all(s.upper().startswith("CREATE TABLE IF NOT EXISTS") for s in stmts), \
        "IF NOT EXISTS 가 아니면 실패한 마이그레이션을 재시도할 수 없다"


def test_schema_files_never_use_add_column_if_not_exists():
    """`ADD COLUMN IF NOT EXISTS` 는 MariaDB 전용 확장 문법 — MySQL 8 에는
    없다(2026-08-16 리뷰 결함 C1: 004 가 이 문법을 썼다가 `migrate()` 가 예외를
    삼키지 않아 크론이 매 실행 크래시하고 컬럼이 영구히 안 생겼다). 마이그레이션
    멱등성은 `db.migrate()` 의 `schema_migration` 파일명 단위 1회 적용이 이미
    보장하므로(001~003 방식) ALTER 문에 이 문법이 나올 이유가 없다."""
    import re
    from quant.adapters.db import SCHEMA_DIR
    pattern = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS", re.IGNORECASE)
    offenders = [
        p.name for p in SCHEMA_DIR.glob("*.sql")
        if pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"MariaDB 전용 문법이 있는 스키마 파일: {offenders}"


# ── 멱등성 ────────────────────────────────────────────────────────────────

def test_upsert_updates_only_listed_columns():
    sql = upsert_sql("article", ["a", "b", "c"], ["b"])
    assert "ON DUPLICATE KEY UPDATE `b`=VALUES(`b`)" in sql
    assert "`a`=VALUES" not in sql and "`c`=VALUES" not in sql


def test_upsert_without_update_columns_uses_insert_ignore():
    """체결은 불변이라 갱신할 게 없다 — 그렇다고 맨 INSERT 면 재적재가 죽는다.

    회귀(2026-08-13 실환경): 맨 `INSERT` 를 쓰다가 두 번째 적재에서
    `IntegrityError: Duplicate entry ... for key 'trade.uq_trade'` 로 죽었다.
    "ON DUPLICATE KEY 가 없다"를 확인하던 옛 테스트는 이걸 못 잡았다 — 그 확인은
    멱등성이 아니라 문자열 부재를 본 것이었다.
    """
    sql = upsert_sql("trade", ["a"], [])
    assert sql.startswith("INSERT IGNORE INTO"), "재적재가 예외로 죽으면 안 된다"
    assert "ON DUPLICATE KEY" not in sql


def test_article_first_seen_is_never_overwritten():
    """처음 본 시각이 바뀌면 '언제부터 돌던 기사인가'를 잃는다."""
    assert "first_seen" in ARTICLE_COLS
    assert "first_seen" not in ARTICLE_UPDATE
    assert {"last_seen", "seen_count"} <= set(ARTICLE_UPDATE)


def test_fill_sha_is_stable_and_content_addressed():
    a = {"ts": "2026-08-13T03:35:03+00:00", "symbol": "263750", "qty": 24}
    assert fill_sha(a) == fill_sha(dict(reversed(list(a.items())))), "키 순서에 흔들리면 안 된다"
    assert fill_sha(a) != fill_sha({**a, "qty": 25})


def test_same_ledger_ingested_twice_sends_identical_rows(tmp_path: Path):
    """같은 파일을 두 번 적재해도 같은 행 — 유니크키가 중복을 흡수한다."""
    led = tmp_path / "data" / "state"
    led.mkdir(parents=True)
    rec = {"ts": "2026-08-13T03:35:03+00:00", "strategy_id": "intraday_scan",
           "symbol": "263750", "side": "buy", "qty": 24, "price": 31907.9,
           "fee": 114.8, "realized_pnl": 0.0, "market": "KR"}
    (led / "trades.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")

    first, second = FakeConn(), FakeConn()
    ingest_trades(first, tmp_path)
    ingest_trades(second, tmp_path)
    assert first.params() == second.params(), "같은 입력이면 같은 행이어야 유니크키가 흡수한다"
    # 체결은 불변이므로 갱신절이 없어야 한다 — 있으면 재적재가 값을 덮어쓴다
    assert "ON DUPLICATE KEY" not in first.statements()[0]


def test_two_fills_in_the_same_second_are_distinct_rows():
    """(ts, 전략, 심볼, 방향) 자연키였다면 여기서 하나가 사라진다."""
    base = {"ts": "2026-08-13T03:35:03+00:00", "strategy_id": "orb_scan",
            "symbol": "005930", "side": "buy", "market": "KR"}
    a = trade_row({**base, "qty": 10, "price": 70000})
    b = trade_row({**base, "qty": 3, "price": 70100})
    assert a[0] != b[0]


# ── 행 변환 ───────────────────────────────────────────────────────────────

def _article(**kw):
    base = {"key": "https://x.com/1", "link": "https://x.com/1?utm_source=rss",
            "title": "삼성전자 신고가", "outlet": "연합뉴스",
            "published": "2026-08-13T09:00:00+09:00", "published_known": True,
            "first_seen": "2026-08-13T00:10:00+00:00",
            "last_seen": "2026-08-13T03:10:00+00:00", "seen_count": 3}
    return {**base, **kw}


def test_article_row_stores_times_as_utc_naive():
    """이 저장소는 이미 타임존으로 9시간 어긋난 적이 있다 — 전부 UTC 로 통일한다."""
    row = dict(zip(ARTICLE_COLS, article_row(_article(), "KR", date(2026, 8, 13))))
    assert row["published"] == datetime(2026, 8, 13, 0, 0)   # 09:00 KST = 00:00 UTC
    assert row["first_seen"] == datetime(2026, 8, 13, 0, 10)


def test_article_row_keeps_unknown_publish_time_null():
    """미상을 수집시각으로 위장하면 어느 근거로 시간창을 통과했는지 알 수 없다."""
    row = dict(zip(ARTICLE_COLS, article_row(
        _article(published=None, published_known=False), "KR", date(2026, 8, 13))))
    assert row["published"] is None and row["published_known"] == 0
    assert row["first_seen"] is not None


def test_article_row_dedup_key_ignores_tracking_params():
    """정규화 링크(key)로 해시한다 — utm 만 다른 같은 기사가 두 건이 되면 안 된다."""
    a = article_row(_article(link="https://x.com/1?utm_source=a"), "KR", date(2026, 8, 13))
    b = article_row(_article(link="https://x.com/1?utm_source=b"), "KR", date(2026, 8, 13))
    assert a[ARTICLE_COLS.index("link_sha")] == b[ARTICLE_COLS.index("link_sha")]


@pytest.mark.parametrize("bad", [{"title": ""}, {"key": "", "link": ""}, {"first_seen": None}])
def test_article_row_rejects_incomplete_records(bad):
    assert article_row(_article(**bad), "KR", date(2026, 8, 13)) is None


def test_trade_row_infers_market_when_absent():
    row = dict(zip(TRADE_COLS, trade_row(
        {"ts": "2026-08-13T03:00:00+00:00", "symbol": "005930", "side": "buy"})))
    assert row["market"] == "KR"
    row = dict(zip(TRADE_COLS, trade_row(
        {"ts": "2026-08-13T03:00:00+00:00", "symbol": "TQQQ", "side": "sell"})))
    assert row["market"] == "US"


@pytest.mark.parametrize("bad", [
    {"side": "hold"}, {"symbol": ""}, {"ts": "쓰레기"},
])
def test_trade_row_rejects_malformed(bad):
    rec = {"ts": "2026-08-13T03:00:00+00:00", "symbol": "005930", "side": "buy", **bad}
    assert trade_row(rec) is None


def test_selection_row_keeps_missing_scores_null_not_zero():
    """'트렌딩 0점'과 '트렌딩을 계산 못 했다'는 전혀 다른 사건이다."""
    from quant.control.warehouse import SELECTION_COLS
    row = dict(zip(SELECTION_COLS, selection_row(
        {"date": "2026-08-13", "market": "KR", "symbol": "005930"})))
    assert row["trending_score100"] is None
    assert row["ai_score100"] is None
    assert row["is_candidate"] == 0


def test_selection_row_serialises_params_deterministically():
    from quant.control.warehouse import SELECTION_COLS
    args = {"date": "2026-08-13", "market": "KR", "symbol": "005930"}
    a = selection_row({**args, "params": {"b": 2, "a": 1}})
    b = selection_row({**args, "params": {"a": 1, "b": 2}})
    assert a[SELECTION_COLS.index("params")] == b[SELECTION_COLS.index("params")]


def test_selection_row_requires_market():
    assert selection_row({"date": "2026-08-13", "market": "JP", "symbol": "7203"}) is None


# ── 파일 읽기 ─────────────────────────────────────────────────────────────

def test_broken_line_does_not_stop_ingest(tmp_path: Path):
    """한 줄이 망가졌다고 적재 전체가 멈추면, 원장이 클수록 취약해진다."""
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\n망가진줄\n\n{"a":2}\n', encoding="utf-8")
    assert [r["a"] for r in read_jsonl(p)] == [1, 2]


def test_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert read_jsonl(tmp_path / "없음.jsonl") == []


def test_ingest_selections_scans_ledger_dir(tmp_path: Path):
    d = tmp_path / "data" / "ledger"
    d.mkdir(parents=True)
    (d / "selections-2026-08.jsonl").write_text(
        json.dumps({"date": "2026-08-13", "market": "KR", "symbol": "005930"}) + "\n",
        encoding="utf-8")
    conn = FakeConn()
    assert ingest_selections(conn, tmp_path) == {"selections": 1}
    assert conn.commits == 1


def test_empty_artifacts_commit_nothing(tmp_path: Path):
    """빈 적재가 빈 트랜잭션을 남기지 않는다."""
    conn = FakeConn()
    assert ingest_trades(conn, tmp_path) == {"trades": 0}
    assert conn.commits == 0 and conn.log == []


# ── 기사 적재 창(증분) ────────────────────────────────────────────────────
# 2026-08-28: 매 실행이 전 이력을 재적재해 systemd 예산(45분)을 2주간 초과,
# DB 가 8/24 에 멈춘 채 조용히 실패했다. 파일이 진실 원본이고 upsert 는 멱등이라
# 최근 창만 훑는다.

def _news_day(root: Path, market: str, day: str, title: str) -> None:
    d = root / "data" / "news" / market
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.jsonl").write_text(
        json.dumps({"link": f"https://x/{day}", "title": title, "outlet": "t",
                    "first_seen": f"{day}T09:00:00+09:00"}) + "\n",
        encoding="utf-8")


def test_ingest_articles_since_skips_older_days(tmp_path: Path):
    _news_day(tmp_path, "KR", "2026-08-01", "옛날 기사")
    _news_day(tmp_path, "KR", "2026-08-27", "어제 기사")
    conn = FakeConn()
    assert ingest_articles(conn, tmp_path, "KR", since=date(2026, 8, 25)) == {
        "articles": 1, "tagged": 0}


def test_ingest_articles_without_since_ingests_everything(tmp_path: Path):
    """백필 경로(--days 0)는 전체를 다시 적재한다 — 기존 계약 보존."""
    _news_day(tmp_path, "KR", "2026-08-01", "옛날 기사")
    _news_day(tmp_path, "KR", "2026-08-27", "어제 기사")
    conn = FakeConn()
    assert ingest_articles(conn, tmp_path, "KR") == {"articles": 2, "tagged": 0}


def test_ingest_articles_since_boundary_is_inclusive(tmp_path: Path):
    """경계일 자체는 포함한다 — 하루가 조용히 빠지면 그날 뉴스가 영영 안 들어온다."""
    _news_day(tmp_path, "KR", "2026-08-25", "경계일 기사")
    conn = FakeConn()
    assert ingest_articles(conn, tmp_path, "KR", since=date(2026, 8, 25))["articles"] == 1
