"""분석 저장소 CLI — `python -m quant.apps.warehouse_cli {migrate,ingest,status}`.

거래 엔진과 **별개 프로세스**다. 크론이 장 마감 후에 돌린다. 여기가 실패해도
매매·수집·리포트는 계속 돈다 — 아티팩트가 진실이고 이건 색인이기 때문이다.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from quant.adapters.db import connect, migrate
from quant.adapters.kv import make_kv
from quant.control.opstate import record_run
from quant.control.warehouse import ingest_all


def _record(job: str, ok: bool, detail: str = "") -> None:
    """기록 실패가 적재를 죽이지 않는다 — Redis 가 없으면 NullKeyValue 가 삼킨다."""
    try:
        record_run(make_kv(), job, ok=ok, detail=detail)
    except Exception:  # noqa: BLE001
        pass


def _tagger_for(cache_dir: Path):
    """시장 → 제목에서 종목코드를 뽑는 함수. 사전은 시장마다 다르다.

    지연 임포트인 이유: 사전 로딩이 네트워크를 탈 수 있고, 그게 실패해도 적재
    자체는 계속돼야 한다(태깅만 생략된다).
    """
    def build(market: str):
        from quant.analyze import entities
        if market == "KR":
            table = entities.load_table(cache_dir)
            return lambda title: [h["symbol"] for h in entities.extract(title, table)]
        table = entities.load_us_table(cache_dir)
        return lambda title: [h["symbol"] for h in entities.extract_us(title, table)]
    return build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="warehouse")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="스키마 마이그레이션 적용")
    ing = sub.add_parser("ingest", help="아티팩트를 적재")
    ing.add_argument("--root", default=".", help="저장소 루트(기본: 현재 디렉토리)")
    ing.add_argument("--no-tag", action="store_true", help="기사↔종목 태깅 생략")
    # 기본 7일: 매 실행이 전 이력을 다시 적재해 2026-08-15 부터 systemd 예산(45분)을
    # 넘겨 2주간 강제 종료됐다(warehouse.ingest_articles docstring). upsert 는
    # 멱등이라 최근 창만 훑으면 되고, 전체 재적재는 --days 0 으로 명시한다.
    ing.add_argument("--days", type=int, default=7,
                     help="최근 N일 기사만 적재(0 = 전체 재적재/백필). 기본 7")
    sub.add_parser("status", help="접속과 적재량 확인")
    a = ap.parse_args(argv)

    conn = connect()
    if conn is None:
        print("MySQL 미설정 또는 접속 불가 — MYSQL_DATABASE 를 확인한다.", file=sys.stderr)
        return 3

    try:
        if a.cmd == "migrate":
            applied = migrate(conn)
            print(f"적용: {applied or '없음(최신)'}")
            return 0

        if a.cmd == "ingest":
            migrate(conn)  # 적재 전에 스키마를 맞춘다
            root = Path(a.root)
            tagger_for = None if a.no_tag else _tagger_for(root / "data" / "cache")
            since = None if a.days <= 0 else date.today() - timedelta(days=a.days)
            stats = ingest_all(conn, root, tagger_for, since=since)
            print(" · ".join(f"{k} {v}" for k, v in sorted(stats.items())))
            # 운영 상태 기록 — 감시(`cli health`)가 읽는 유일한 입구다. 없으면 그
            # 감지기는 "기록이 없다"만 영원히 답한다(2026-08-13 감시 배포 때 드러났다).
            _record("ingest", ok=True,
                    detail=" · ".join(f"{k} {v}" for k, v in sorted(stats.items())))
            return 0

        with conn.cursor() as cur:
            for t in ("article", "article_symbol", "selection", "trade", "forward_return"):
                try:
                    cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                    print(f"  {t:<16} {cur.fetchone()[0]:>8,}")
                except Exception:
                    print(f"  {t:<16} {'(없음)':>8}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
