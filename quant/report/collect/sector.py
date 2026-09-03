"""업종/테마 시세 + 외국인 수급 탑다운 뷰 수집기.

Phase D 엔진 분리(2026-08-19) — `quant/apps/report_cli.py`에서 그대로 옮겼다.
동작 변경 없음, 순수 구조 이동.
"""
from __future__ import annotations

import json
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


# ------------------------------------------------------------------ 주도 섹터
# (소유자 철학 지시 B, 2026-09-03) — 거래대금 상위 업종 + 외국인 수급 + 5일
# 순위 추이. 순수 계산은 quant.analyze.sector_daily(build_sector_daily_rows/
# rank_with_trend) 몫이고, 여기는 그 입력을 읽고(fundamentals_naver.jsonl/
# frgn_flow.jsonl) sector_daily.jsonl에 적재하는 I/O만 한다.

def _load_turnover_today(root: Path) -> tuple[str | None, dict[str, int]]:
    """`fundamentals_naver.jsonl`(naver_quant, 거래대금 상위 100종목)에서 가장
    최근 관측일의 종목별 거래대금(KRW)을 읽는다. `value_traded`는 백만원
    단위라 1,000,000을 곱해 원 단위로 맞춘다(naver_quant.py 헤더 실측 참고).
    원장이 없거나 비어 있으면 `(None, {})`."""
    path = root / "data" / "ledger" / "fundamentals_naver.jsonl"
    if not path.exists():
        return None, {}
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("date") and row.get("code"):
            rows.append(row)
    if not rows:
        return None, {}
    latest_date = max(row["date"] for row in rows)
    turnover: dict[str, int] = {}
    for row in rows:
        if row["date"] != latest_date:
            continue
        value_traded = row.get("value_traded")
        if value_traded is None:
            continue
        turnover[row["code"]] = int(value_traded) * 1_000_000
    return latest_date, turnover


def _load_foreign_net_for_date(root: Path, date_str: str) -> dict[str, int]:
    """`frgn_flow.jsonl`에서 그 날짜의 종목별 외국인 순매수(주식 수)를 읽는다.
    그날 fetch_many 상위 20종목만 채워지므로(`_build_foreign_view` docstring
    참고) 대부분의 종목은 이 사전에 없다 — 0으로 위장하지 않는다."""
    path = root / "data" / "ledger" / "frgn_flow.jsonl"
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("date") != date_str:
            continue
        symbol = row.get("symbol")
        net = row.get("foreign_net")
        if symbol and net is not None:
            out[symbol] = int(net)
    return out


def _append_sector_daily(path: Path, rows: list[dict]) -> None:
    """`(date, market, sector)` 키로 upsert — 같은 날 리포트를 여러 번 빌드해도
    행이 늘지 않는다(`quant.control.frgn_flow.append_daily`와 같은 관례).
    tmp + `os.replace`로 원자적 치환(쓰다 죽어도 원본이 남는다)."""
    existing: dict[tuple, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("date") and row.get("sector"):
                existing[(row["date"], row.get("market"), row["sector"])] = row
    for row in rows:
        existing[(row["date"], row["market"], row["sector"])] = row

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in existing.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _load_sector_daily_history(
    path: Path, market: str, before_date: str, days: int = 5,
) -> list[dict]:
    """`before_date`(오늘) 이전 최근 `days`거래일치 rows. 파일이 없으면 빈 리스트."""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (isinstance(row, dict) and row.get("market") == market
                and row.get("date") and row["date"] < before_date):
            rows.append(row)
    recent_dates = sorted({row["date"] for row in rows})[-days:]
    return [row for row in rows if row["date"] in recent_dates]


def _build_sector_daily_view(root: Path, market: str) -> dict | None:
    """일일 주도 섹터(§3, 2026-09-03 소유자 철학 지시 B). 거래대금 원장·업종
    멤버십 원장 어느 하나가 없으면 `None` — 호출부(report_cli)가 "결측 —
    섹터 데이터 없음"으로 렌더한다(§C). `market != "KR"`이면 호출부가 애초에
    부르지 않는다(US는 naver 거래대금/frgn_flow 원장 자체가 없다)."""
    from quant.analyze.sector_daily import build_sector_daily_rows, rank_with_trend

    sector_members = _load_artifact(root / "data" / "ledger" / "sector_members.json") or {}
    if not sector_members:
        print("주도 섹터 생략: sector_members.json 없음", file=sys.stderr)
        return None

    turnover_date, turnover_by_symbol = _load_turnover_today(root)
    if turnover_date is None or not turnover_by_symbol:
        print("주도 섹터 생략: fundamentals_naver.jsonl 거래대금 데이터 없음", file=sys.stderr)
        return None

    foreign_net_by_symbol = _load_foreign_net_for_date(root, turnover_date)
    today_rows = build_sector_daily_rows(
        turnover_date, market, sector_members, turnover_by_symbol, foreign_net_by_symbol,
    )
    if not today_rows:
        print("주도 섹터 생략: 업종별 거래대금 합산 결과 없음", file=sys.stderr)
        return None

    ledger_path = root / "data" / "ledger" / "sector_daily.jsonl"
    try:
        _append_sector_daily(ledger_path, today_rows)
    except Exception as e:  # noqa: BLE001 — 원장 쓰기 실패가 리포트 표시를 막지 않는다
        print(f"주도 섹터 원장 쓰기 실패(표시는 계속): {type(e).__name__}: {e}", file=sys.stderr)

    history_rows = _load_sector_daily_history(ledger_path, market, before_date=turnover_date, days=5)
    ranked = rank_with_trend(today_rows, history_rows)
    return {"date": turnover_date, "sectors": ranked[:8]}
