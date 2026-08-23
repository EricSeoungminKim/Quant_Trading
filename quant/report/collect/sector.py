"""업종/테마 시세 + 외국인 수급 탑다운 뷰 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import sys
from pathlib import Path

from quant.analyze import foreign_trend
from quant.control import frgn_flow as frgn_flow_ledger

from quant.report.paths import _load_artifact


def _load_sector_data(root: Path) -> tuple[dict, list[dict], dict]:
    """업종 아티팩트(`sector_map.json`/`sector_members.json`) + 실시간 등락률.

    어느 하나가 없거나 실패해도 나머지로 최대한 진행한다 — 호출부가 각
    빈 값을 받아 알아서 섹션을 생략한다(spec 실패 모드 표).
    """
    from quant.collect.sources.naver_sector import fetch_sector_quotes

    sector_map = _load_artifact(root / "data" / "ledger" / "sector_map.json") or {}
    sector_members = _load_artifact(root / "data" / "ledger" / "sector_members.json") or {}
    try:
        sector_quotes = fetch_sector_quotes()
    except Exception as e:  # noqa: BLE001 — 업종 등락률 실패가 리포트를 막지 않는다
        print(f"업종 등락률 조회 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        sector_quotes = []
    return sector_map, sector_quotes, sector_members


def _build_sector_view(
    sector_map: dict, sector_quotes: list[dict], sector_members: dict,
    cont: dict, sym_quotes: dict, relations,
) -> list[dict]:
    """테마별 시세(§Task 5 / H-1c). sector_map 없음/등락률 조회 실패 어느 쪽이
    죽어도 섹션을 생략할 뿐 리포트 전체는 죽지 않는다(spec 실패 모드 표)."""
    from quant.analyze.sector_view import build_sector_view

    if not sector_map:
        return []
    symbols = {
        symbol: {"name": c.get("name", symbol),
                 "change_pct": (sym_quotes.get(symbol) or {}).get("change_pct")}
        for symbol, c in cont.items()
    }
    try:
        return build_sector_view(sector_map, sector_quotes, symbols, relations,
                                  sector_members=sector_members)
    except Exception as e:  # noqa: BLE001 — 뷰 조립 실패가 리포트를 막지 않는다
        print(f"테마별 시세 조립 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _build_top_movers(sector_quotes: list[dict], themes: dict | None, sector_members: dict) -> dict:
    """업종상위/테마상위 카드(H-1c). 어느 입력이 비어도 조립 실패가 리포트를
    막지 않는다 — 실패 시 카드 섹션 자체를 생략(빈 dict)."""
    from quant.analyze.sector_view import build_top_movers

    try:
        return build_top_movers(sector_quotes, themes or {}, sector_members=sector_members)
    except Exception as e:  # noqa: BLE001 — 카드 조립 실패가 리포트를 막지 않는다
        print(f"업종상위/테마상위 조립 건너뜀: {type(e).__name__}: {e}", file=sys.stderr)
        return {}


def _build_foreign_view(
    root: Path, snap, cont: dict, sector_map: dict, payload: dict,
) -> dict | None:
    """외국인 수급 추종 뷰(서브프로젝트 I) — 탑다운 섹터 → 종목.

    **사용자 원칙(2026-08-17)**: "한국주식은 기관 매수세는 노이즈 — 메인 리서치와
    비리 없는 매수는 오로지 외국인 매수 추세." 라벨 판단은 `foreign_trend.classify()`
    가 하고, 이 함수는 후보를 추리고 섹터로 묶기만 한다.

    KR 전용 — US 는 `frgn_flow.jsonl` 원장 자체가 없다(stock_detail 이 KR
    종목에만 수급을 수집한다). 후보 집합은 `cont`(오늘 리포트가 아는 종목
    전체) 중 원장에 시계열이 있는 것만 — 실제로는 그날 `fetch_many` 상위
    20종목만 채워진다(`_record_frgn_flow`). `sector_map`({코드: 업종명})은
    호출부가 이미 로드한 것을 재사용한다 — 4,029종목 사전을 여기서 다시
    읽지 않는다.

    데이터가 하나도 없으면(원장이 아직 없거나 후보가 전부 빠짐) `None` —
    호출부·템플릿 모두 "섹션 생략"으로 처리한다(0/빈 리스트로 위장하지 않는다).
    """
    if snap.market != "KR":
        return None

    path = root / "data" / "ledger" / "frgn_flow.jsonl"
    rows: list[dict] = []
    for symbol, c in cont.items():
        series = frgn_flow_ledger.load_series(path, symbol, days=20)
        if not series:
            continue
        result = foreign_trend.classify(series)
        rows.append({
            "symbol": symbol,
            "name": c.get("name", symbol),
            "sector": sector_map.get(symbol),
            "label": result["label"],
            "residual": result["residual"],
            "inst_follows": result["inst_follows"],
            "days": result["days"],
            "series": [
                {"date": r["date"], "foreign_net": r.get("foreign_net") or 0}
                for r in series[-10:]
            ],
        })

    if not rows:
        return None

    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["sector"] or "미분류", []).append(row)

    sectors = [
        {
            "name": name,
            "net_sum": sum(row["residual"] for row in members),
            "rows": sorted(members, key=lambda row: -row["residual"]),
        }
        for name, members in groups.items()
    ]
    sectors.sort(key=lambda s: -s["net_sum"])

    features = payload.get("features") or {}
    flow_result = snap.results.get("kospi_flow")
    flow_data = (
        flow_result.data
        if flow_result is not None and flow_result.ok and flow_result.data
        else None
    )
    market_date = (flow_data.get("rows") or [{}])[0].get("date") if flow_data else None

    return {
        "market_foreign_net": features.get("foreign_net_100m_krw"),
        "market_date": market_date,
        "sectors": sectors,
    }
