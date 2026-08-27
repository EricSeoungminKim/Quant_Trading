"""FRED(세인트루이스 연방준비은행) 매크로 시계열 수집 어댑터.

2026-08-28 소유자 지시 — "시그널이 차트만 보는 게 아니라 금리 등 주가에 영향
주는 rate 를 함께 보고, 데이터를 미리 수집해 시기별로 ML 학습". `TossIndicatorClient`
(quant/adapters/regime_indicators.py)는 국채(KR_BOND_*)를 애초에 구현하지 않았고
Toss API 자체도 국내 지표만 지원한다 — 여기서 그 구멍을 메운다.

`https://fred.stlouisfed.org/graph/fredgraph.csv?id=<ID>` — 인증 불필요, 시리즈의
**전체 과거**를 한 번에 CSV 로 준다(2026-08-28 EC2 실측: DGS10/DGS2/T10Y2Y/
VIXCLS/DTWEXBGS/DEXKOUS 전부 확인, 시리즈당 5천~1.3만 행). 형식: 헤더 1줄 +
`날짜,값` 행, 결측은 `.`.

이 어댑터의 소비자는 두 갈래다:
- `quant/apps/cli.py`의 `macro-collect` 커맨드 — 전체 과거(또는 최근 N일)를 받아
  `data/ledger/macro_rates.jsonl`에 적재(백필/일일 갱신).
- `quant/adapters/regime_indicators.FileMacroIndicatorClient` — 그 원장 파일만
  읽어 국면(`quant/trade/regime/`)에 최신값을 공급한다. **국면은 네트워크를 직접
  만지지 않는다** — 거래 평면은 httpx 금지(tests/test_architecture.py
  FORBIDDEN_EXTERNAL) 이므로, 여기(어댑터 평면, 배치 CLI 경로)에서만 FRED를
  호출하고 결과는 파일을 거쳐서만 국면에 닿는다.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from quant.adapters.http import client as http_client

logger = logging.getLogger(__name__)

_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# 우리 이름 → FRED 시리즈 ID. 호출부는 왼쪽 이름으로만 이야기한다 — FRED ID는
# 이 파일 밖으로 새지 않는다(regime_indicators.FileMacroIndicatorClient도 "us_10y"
# 로만 요청한다).
SERIES: dict[str, str] = {
    "us_10y": "DGS10",
    "us_2y": "DGS2",
    "term_spread_10y2y": "T10Y2Y",
    "vix": "VIXCLS",
    "dollar_index": "DTWEXBGS",
    "usdkrw": "DEXKOUS",
}

DEFAULT_LEDGER_PATH = "data/ledger/macro_rates.jsonl"


def parse_fred_csv(text: str) -> list[tuple[str, float]]:
    """FRED CSV 본문 → (ISO 날짜, 값) 오름차순 리스트. 순수 함수(네트워크 없음).

    헤더 1줄 스킵, 결측(`.`) 행은 제외한다. 형식이 깨진 행(콤마 개수 불일치,
    숫자 파싱 실패)은 **건너뛴다** — 한 줄 때문에 시리즈 전체를 버리지 않는다
    (quant/control/symbol_log.py의 "깨진 줄 건너뜀"과 같은 관례)."""
    out: list[tuple[str, float]] = []
    lines = text.splitlines()
    for line in lines[1:]:  # 헤더 스킵
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 2:
            continue
        date_str, value_str = parts
        if value_str == ".":  # FRED 결측 표기
            continue
        try:
            value = float(value_str)
        except ValueError:
            continue
        out.append((date_str, value))
    return out


def fetch_series(series_id: str, *, timeout: float = 30.0) -> list[tuple[str, float]] | None:
    """`series_id`(FRED ID)의 전체 과거 시계열. (ISO 날짜, 값) 오름차순.

    네트워크 예외는 여기서 삼키고 None(어댑터 계약 — regime_indicators.py의
    TossIndicatorClient/UpbitBitcoinAdapter와 동일 패턴). 로그는 warning."""
    try:
        with http_client(timeout=timeout) as c:
            resp = c.get(_BASE_URL, params={"id": series_id})
            resp.raise_for_status()
            text = resp.text
    except Exception:
        logger.warning("macro: FRED %s 조회 실패", series_id, exc_info=True)
        return None
    return parse_fred_csv(text)


def append_macro_rows(rows: list[dict], path: str | Path = DEFAULT_LEDGER_PATH) -> int:
    """`data/ledger/macro_rates.jsonl`에 멱등 append — 같은 (date, series) 행은
    **새 값으로 갱신**한다.

    quant/control/symbol_log.py의 append_scores는 같은 키면 "먼저 온 값이
    이긴다"(스킵)지만, 여긴 반대 계약이다 — FRED는 최근 며칠 값을 뒤늦게 정정
    발표하는 경우가 있어(예: 국채수익률 가결산 정정) "나중 값이 이긴다"(덮어쓰기)
    가 맞다. 전체 파일을 메모리에 올려 다시 쓴다 — 시리즈 6개 × 최대 1.3만 행
    수준(2026-08-28 실측)이라 부담이 없다. `regime_indicators.FileMacroIndicatorClient`
    가 이 파일만 읽는 파일 경유 계약과 짝을 이룬다.

    반환값은 **새로 추가된**(이전에 없던) (date, series) 키 개수 — 값만 갱신된
    기존 키는 세지 않는다. 호출부(macro-collect CLI)가 "이번에 몇 건 새로
    들어왔나"를 보고할 때 쓴다.
    """
    if not rows:
        return 0
    p = Path(path)
    existing: dict[tuple, dict] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            existing[(r.get("date"), r.get("series"))] = r

    added = 0
    for row in rows:
        key = (row["date"], row["series"])
        if key not in existing:
            added += 1
        existing[key] = row

    p.parent.mkdir(parents=True, exist_ok=True)
    # 원자적 tmp-replace(regime/provider.py._save_cache와 같은 관례) — 쓰다
    # 죽어도 원본 원장이 깨지지 않는다.
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for key in sorted(existing.keys()):
            f.write(json.dumps(existing[key], ensure_ascii=False) + "\n")
    tmp.replace(p)
    return added
