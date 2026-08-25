#!/usr/bin/env python3
"""자체 리포트 엔진 JSON을 읽어 브리핑 본문 + 후보 토큰을 낸다 (own_brief.sh 전용).

`quant.analyze.market_brief`(순수 함수)에 판단을 맡기고, 여기서는 파일을 찾아
읽는 I/O만 한다. run.py에 서브커맨드를 더하지 않은 이유는 이것이 거래 엔진 CLI가
아니라 **리포팅 레이어 도구**이기 때문이다 — us_watch_discover.sh 옆에 두는 편이
경로를 읽는 사람에게 정직하다.

출력 계약 (own_brief.sh가 의존한다):
  - stdout 본문   = 텔레그램에 그대로 보낼 브리핑
  - TOKENS 줄     = "TOKENS: <SYM[:TAG]> ..."  (없으면 "TOKENS:") — watch-score로
    가는 후보(NEWS/RANK→EVENT/TREND 번역 + EVENT_SCALP + FRGN, 서브프로젝트 T)
  - FRGN_EXIT 줄  = "FRGN_EXIT: <SYM> ..."  (없으면 줄 자체를 생략) — watch-score를
    타지 않는다. 이미 등록된 종목의 태그 갱신 전용(own_brief.sh가 직접 watch-add
    --tags-only로 처리) — market_brief.foreign_flow_tokens docstring 참고.
  - exit 0=정상 / 3=리포트 없음 / 4=리포트가 오늘 것이 아님 / 5=JSON 파손

종료코드를 나눠 두는 이유: 셸이 "리포트가 없었다"와 "리포트가 낡았다"를 구분해
알릴 수 있어야 한다. 둘 다 '후보 0건'으로 뭉뚱그리면 리포트 파이프라인이 조용히
죽어도 아무도 모른다.

**--session close** (서브프로젝트 T Task 2): 13:55 오후 own_brief.sh가 읽는
`{MARKET}_close_engine.json` 전용 분기. 스키마가 아침판과 달라(auto_watch/symbols
없음, 대신 intraday_view) 브리핑 본문도 `market_brief.close_brief_text()`로 따로
포맷한다 — 아침용 `brief_text()`를 그대로 쓰면 "자동 편입 후보: 없음"이 항상 찍힌다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from quant.analyze import foreign_trend  # noqa: E402
from quant.analyze.market_brief import (  # noqa: E402
    brief_text,
    close_brief_text,
    close_bet_tokens,
    engine_tokens,
    foreign_flow_candidate_symbols,
    foreign_flow_tokens,
    intraday_scalp_tokens,
    is_fresh,
)
from quant.control import frgn_flow as frgn_flow_ledger  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
# 2026-08-13 저장소 흡수 후 리포트는 이 저장소의 out/ 에 쌓인다 — 기본값이 옛
# ~/market_report 체크아웃을 가리켜 08-14부터 매일 "리포트 없음" 폴백이 돌았다
# (2026-08-17 발견). 호출자(own_brief.sh)가 저장소 루트로 cd 한 뒤 부르므로
# 상대경로 "." 이 이 저장소다.
DEFAULT_REPORT_DIR = Path(os.environ.get("MARKET_REPORT_DIR", "."))


def _foreign_flow_labels(report_dir: Path, symbols: set[str], days: int = 20) -> dict[str, str]:
    """`symbols`(KR)의 외국인 수급 라벨. `data/ledger/frgn_flow.jsonl`을 직접 읽어
    report_cli.py의 `_build_foreign_view`와 같은 함수(`foreign_trend.classify`)를
    재적용한다 — report_cli.py는 이 라벨을 engine.json에 싣지 않으므로(market_brief.py
    "서브프로젝트 T" 절 참고) 원장에서 직접 재구성하는 것이 유일한 경로다. 시계열이
    없는 심볼(그날 fetch_many 상위에 안 들었던 종목)은 결과에서 빠진다 — 0으로
    위장하지 않는다."""
    path = report_dir / "data" / "ledger" / "frgn_flow.jsonl"
    out: dict[str, str] = {}
    for sym in symbols:
        series = frgn_flow_ledger.load_series(path, sym, days=days)
        if series:
            out[sym] = foreign_trend.classify(series)["label"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--market", required=True, choices=["KR", "US"])
    ap.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR),
                    help="market_report 저장소 루트 (기본: $MARKET_REPORT_DIR 또는 ~/market_report)")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (기본: 오늘 KST)")
    ap.add_argument("--url-base", default="", help="예: https://ip-172-31-63-20.tailfee6e9.ts.net")
    ap.add_argument("--session", default="live", choices=["live", "close"],
                    help="live(기본, 개장 전 08:12/21:50) | close(13:55 마감 포지션 리포트)")
    a = ap.parse_args()

    today = a.date or datetime.now(KST).date().isoformat()
    y, m, d = today.split("-")
    filename = f"{a.market}_close_engine.json" if a.session == "close" else f"{a.market}_engine.json"
    path = Path(a.report_dir) / "out" / y / m / d / filename

    if not path.exists():
        print(f"리포트 없음: {path}", file=sys.stderr)
        print("TOKENS:")
        return 3
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"리포트 파손: {path} ({exc})", file=sys.stderr)
        print("TOKENS:")
        return 5
    if not isinstance(payload, dict):
        print(f"리포트 형식 오류: {path}", file=sys.stderr)
        print("TOKENS:")
        return 5

    if not is_fresh(payload, today):
        print(f"리포트가 오늘({today}) 것이 아님: session_date="
              f"{payload.get('session_date')!r}", file=sys.stderr)
        print("TOKENS:")
        return 4

    if a.session == "close":
        print(close_brief_text(payload, a.market))
    else:
        url = f"{a.url_base.rstrip('/')}/{y}/{m}/{d}/{a.market}_report.html" if a.url_base else None
        print(brief_text(payload, a.market, url=url))

    # 엔진 어휘로 번역해 내보낸다 — 리포트의 NEWS/RANK를 그대로 넘기면
    # watch_scorer가 "알 수 없는 태그"로 전체를 무태그 강등한다.
    tokens = engine_tokens(payload, a.market)
    # EVENT_SCALP(당일 단타 후보) — close payload에만 intraday_view가 있다(세션
    # 무관 안전 호출, 아침 payload는 자연히 빈 리스트).
    tokens += intraday_scalp_tokens(payload)
    # 종가배팅(2026-08-25) — close payload 에만 close_bet_view 가 있다(아침판은
    # 자연히 빈 리스트). close_bet_tokens 는 엔진 어휘(CLOSE_BET)를 직접 발행한다 — 이 줄은 번역을 거치지 않는다.
    tokens += close_bet_tokens(payload)

    frgn_exit_symbols: list[str] = []
    candidate_syms = foreign_flow_candidate_symbols(payload, a.market)
    if candidate_syms:
        labels = _foreign_flow_labels(Path(a.report_dir), candidate_syms)
        frgn_tokens, frgn_exit_symbols = foreign_flow_tokens(labels)
        tokens += frgn_tokens

    print("TOKENS: " + " ".join(tokens))
    if frgn_exit_symbols:
        # watch-score를 타지 않는다 — own_brief.sh가 이 줄을 별도로 파싱해
        # watch-add --tags-only로 이미 등록된 종목만 갱신한다.
        print("FRGN_EXIT: " + " ".join(frgn_exit_symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
