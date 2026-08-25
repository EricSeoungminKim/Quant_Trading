"""마감 종합 리포트 — 미국장 + 전일 한국장 (2026-08-25 소유자 지시, 당일 확장).

소유자: "단순 미국장만 끝난 리포트가 아니라 한국장과 미국장을 둘 다 고려한,
다음날의 흐름을 파악할 수 있는 리포트" — US 절반은 섹터·지수·VIX,
KR 절반은 전일 세션 패턴(초반강세지속/후반전고돌파/후반매수파동,
quant.analyze.kr_wrap)과 외인·기관 흐름 종합이다. CLI 이름(uswrap)과
파일명(US_wrap.json)은 호환을 위해 유지한다.

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
from datetime import date, time as dtime, timedelta
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


# KR 정규장 경계(KST). 파티션에는 장전(08:00~)·시간외(~20:00) 봉이 함께 들어 있다.
_KR_SESSION_OPEN = dtime(9, 0)
_KR_SESSION_CLOSE = dtime(15, 30)
# 패턴 판정 하한(classify_session 과 같은 값) — 이보다 적으면 넘기지 않는다.
_MIN_SESSION_BARS = 120


def gather_kr_wrap(root: Path, kr_day: date) -> dict | None:
    """전일 KR 세션 재료를 모아 kr_wrap 을 조립한다 (마감 종합의 KR 절반).

    - 1분봉: `data/history/{6자리}/{YYYY}/{MM}.parquet` — 05:40 백필이 어제
      세션까지 채운 뒤(크론 05:50)라 로컬 파케이만 읽는다(네트워크 0).
      심볼은 디렉토리에서 자동 발견(그날 봉이 있는 KR 종목 전부 = 그날
      워치리스트+보유 이력). Toss 1분봉은 4거래일 롤링이라 백필을 놓친 날은
      여기서도 없다 — 없는 날은 없는 대로(지어내지 않는다).
    - 이름: entities.load_name_map (KIND→DART 폴백 포함).
    - 수급: frgn_flow 원장 → flow_day_summary(그날 행만).
    재료가 전부 비면 None.
    """
    import pandas as pd

    from quant.analyze.kr_wrap import build_kr_session_wrap, flow_day_summary
    from quant.control import frgn_flow as frgn_flow_ledger

    history = root / "data" / "history"
    bars_by_symbol: dict[str, pd.DataFrame] = {}
    if history.exists():
        for sym_dir in sorted(history.iterdir()):
            sym = sym_dir.name
            if not (sym.isdigit() and len(sym) == 6):
                continue
            part = sym_dir / str(kr_day.year) / f"{kr_day.month:02d}.parquet"
            if not part.exists():
                continue
            try:
                df = pd.read_parquet(part)
            except Exception:  # noqa: BLE001 — 파티션 하나가 KR 절반 전체를 막으면 안 된다
                continue
            if df.empty or not isinstance(df.index, pd.DatetimeIndex):
                continue
            idx = df.index.tz_convert("Asia/Seoul") if df.index.tz is not None else df.index
            # **정규장만** 넘긴다(09:00~15:30). Toss 1분봉 파티션의 하루는 실제로
            # 08:01~20:00(720봉, 장전+시간외 포함)이고, kr_wrap.classify_session 은
            # docstring 이 못 박은 대로 "정규장 381분" 프레임을 전제한다 — 날짜로만
            # 거르면 "초반 60분"이 프리마켓, "마지막 60분"이 시간외가 돼 세 패턴이
            # 전부 엉뚱한 창에서 계산된다(2026-08-26 실데이터 확인: 8/25 세션 패턴
            # 0건 → wrap 의 KR 절반 통째 누락 → 아침 리포트 합류 0건).
            mask = (
                (pd.Index(idx.date) == kr_day)
                & (idx.time >= _KR_SESSION_OPEN)
                & (idx.time <= _KR_SESSION_CLOSE)
            )
            day = df[mask]
            # 반나절 데이터로 패턴을 지어내지 않는다(classify_session 의 120봉
            # 하한과 같은 원칙) — 정규장 봉이 없으면 그 심볼은 없는 것이다.
            if len(day) >= _MIN_SESSION_BARS:
                bars_by_symbol[sym] = day

    names: dict[str, str] = {}
    try:
        from quant.analyze.entities import load_name_map

        names = load_name_map(root / "data" / "cache", "KR")
    except Exception:  # noqa: BLE001 — 이름은 표시용, 없으면 코드로 표기
        pass

    flow = None
    try:
        rows = frgn_flow_ledger._load(root / "data" / "ledger" / "frgn_flow.jsonl")
        flow = flow_day_summary(rows, kr_day.isoformat())
    except Exception:  # noqa: BLE001
        pass

    return build_kr_session_wrap(bars_by_symbol, names=names, flow_summary=flow)
