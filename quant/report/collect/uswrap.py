"""미국장 마감 직후 종합 리포트("uswrap", 2026-08-25 소유자 지시).

미국 정규장 마감 직후(KST 새벽) 그날 미국장 흐름(지수·섹터 등락)을 정리해
독립 리포트로 발행하고, **다음날 KR 아침 리포트가 참조하는 소스**가 된다 —
리포트 순환. `build_us_wrap`이 내용을 조립하고(순수 함수, 네트워크/파일
I/O 없음), `write_us_wrap`이 `out/YYYY/MM/DD/US_wrap.json`에 저장하고,
`load_latest_us_wrap`이 다음날 KR 아침판에서 그 파일을 되읽는다.

섹터→국내 업종 매핑과 tone(상승 우위/하락 우위/혼조) 판정은
`quant.analyze.us_kr_bridge.build_us_kr_bridge`를 그대로 재사용한다 — 채점
로직을 여기서 다시 구현하지 않는다(중복 구현 금지, 2026-08-21 규칙과 동일).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from quant.analyze.us_kr_bridge import build_us_kr_bridge

# 리포트에 실을 미국 지수 — S&P500/NASDAQ/다우. quotes 딕셔너리(quant.collect.
# sources.market.fetch_quotes)의 키 그대로다.
_INDEX_SYMBOLS = ("^GSPC", "^IXIC", "^DJI")


def build_us_wrap(
    sectors_data: dict | None,
    market_data: dict | None,
    vix_data: dict | None,
    sector_members: dict | None = None,
) -> dict | None:
    """당일 미국장 종합 — 지수 등락 + 섹터 등락 + tone + KR 연결 업종·종목 + VIX.

    입력은 스냅샷 소스 3종의 `data` 그대로:
      sectors_data — `sectors` 소스(`technical.fetch_sectors`, S&P 섹터 ETF 11종).
      market_data  — `market` 소스(`market.fetch_quotes`, `{"quotes": {...}}`).
      vix_data     — `vix_term` 소스(`technical.fetch_vix_term`).
      sector_members — `data/ledger/sector_members.json`(KR 업종별 종목 원장).
        `build_us_kr_bridge`가 kr_focus 를 만드는 데 필요하다 — 설계 스펙의
        3-인자 시그니처엔 없었지만 kr_focus 계산 자체가 이 인자 없이는
        불가능해 키워드 인자로 추가했다(기존 호출부와 하위호환, 생략 시 kr_focus 만 빈다).

    반환(없으면 None — 3개 입력이 전부 비면 지어낼 게 없다):
      {"tone", "up_count", "down_count", "us_sectors"(등락순), "kr_focus",
       "indices"(있으면), "vix"(있으면)}
      `date` 필드는 여기서 채우지 않는다 — `write_us_wrap`이 저장 시점의
      `session_date`로 채운다(이 함수는 날짜를 모른다, 순수 조립만).

    없는 소스는 그 부분만 생략한다(사용자 지시 "없는 데이터를 지어내지
    않는다") — sectors 없으면 tone/us_sectors/kr_focus 없이 indices/vix 만,
    market 없으면 indices 없이 나머지만, 셋 다 없으면 None.
    """
    bridge = build_us_kr_bridge((sectors_data or {}).get("sectors"), sector_members)

    result: dict = {}
    if bridge is not None:
        result["tone"] = bridge["tone"]
        result["up_count"] = bridge["up_count"]
        result["down_count"] = bridge["down_count"]
        result["us_sectors"] = bridge["us_sectors"]
        result["kr_focus"] = bridge["focus"]

    if market_data:
        quotes = market_data.get("quotes") or {}
        indices = [
            {"symbol": sym, "label": q["label"], "change_pct": q["change_pct"]}
            for sym, q in quotes.items()
            if sym in _INDEX_SYMBOLS and q.get("change_pct") is not None
        ]
        if indices:
            result["indices"] = indices

    if vix_data:
        points = vix_data.get("points") or []
        vix_point = next((p for p in points if p.get("symbol") == "^VIX"), None)
        if vix_point is not None:
            result["vix"] = {
                "value": vix_point["value"],
                "change_pct": vix_point.get("change_pct"),
                "structure": vix_data.get("structure"),
            }

    return result or None


def _dated_dir(out_root: Path, d: date) -> Path:
    """`quant.analyze.render._dated_dir`/`quant.report.paths._engine_json_path`와
    같은 경로 규칙 — `out/YYYY/MM/DD/`."""
    return out_root / f"{d.year:04d}" / f"{d.month:02d}" / f"{d.day:02d}"


def write_us_wrap(payload: dict, out_root: Path, session_date: date) -> Path:
    """`out/YYYY/MM/DD/US_wrap.json` 저장. `date` 필드를 여기서 채운다 —
    `build_us_wrap`은 날짜를 모르는 순수 조립부다(저장 시점 책임 분리)."""
    path = _dated_dir(out_root, session_date) / "US_wrap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    full = {"date": session_date.isoformat(), **payload}
    path.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_latest_us_wrap(
    out_root: Path, before_date: date, max_back_days: int = 4,
) -> dict | None:
    """KR 아침판이 참조하는 로더 — `before_date` 자신부터 거꾸로 최대
    `max_back_days`일 탐색, 없으면 None.

    `before_date` 당일부터 포함하는 이유: uswrap(KST 새벽 발행)과 그날 KR
    아침판(KST 07:30 발행)은 **같은 KST 달력 날짜**에 일어난다 — KR 이
    `snap.session_date`(오늘)를 그대로 넘기면 오늘 새벽에 막 쓰인 파일을
    찾아야 하므로 시작점을 제외하면 매번 하루 어긋난다. 주말·휴장으로
    uswrap 이 못 돈 날은 파일이 아예 없으므로 자연히 건너뛰어진다(별도
    캘린더 판정 불필요) — `quant.analyze.delta.previous_snapshot`과 같은
    "존재하면 쓴다" 원칙.
    """
    for back in range(0, max_back_days + 1):
        d = before_date - timedelta(days=back)
        path = _dated_dir(out_root, d) / "US_wrap.json"
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None
