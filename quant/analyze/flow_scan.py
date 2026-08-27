"""장중 거래대금 발굴 — 아침 리포트가 못 잡은 종목이라도 장중에 거래대금이
쏠리면 워치리스트 후보로 뽑는다 (2026-08-28 소유자 지시: "장중 거래대금 발굴 →
확신도 게이트 → 자동 편입" 체인의 발굴 단계).

여기는 **순수 함수만** 둔다 — quant/analyze/ 는 quant/trade/ 를 임포트하면 안
되고(tests/test_architecture.py가 강제), 네트워크(httpx 등)도 이 모듈엔 없다.
Toss 클라이언트 조립·랭킹 호출은 quant/apps/cli.py의 flow-scan 서브커맨드가
한다. 뽑힌 후보는 여기서 바로 워치리스트에 들어가지 않는다 — watch-score
확신도 게이트를 반드시 거친다(CLAUDE.md ② "아무거나 선정하지 않는다").
"""
from __future__ import annotations

import re

_KR_SHAPE = re.compile(r"^[0-9]{6}$")
# ai_trader.sh / own_brief.sh 의 SHAPE 정규식과 동일 — 셸과 파이썬 양쪽에서 같은
# 형태를 강제해야 한쪽만 느슨해져 이상한 심볼이 새는 사고를 막는다.
_US_SHAPE = re.compile(r"^[A-Za-z][A-Za-z.]{0,5}$")


def flow_candidates(
    rankings_rows: list[dict], existing: set[str], market: str, top: int = 30,
) -> list[str]:
    """Toss `GET /api/v1/rankings` 의 `rankings` 배열 → 신규 발굴 후보 심볼 목록.

    필터 순서(전부 통과해야 후보로 남는다):
      ① `existing`(현재 워치리스트 + 전략 앵커 심볼)에 이미 있으면 제외 — 발굴은
         *새* 정보에만 의미가 있다.
      ② 시장 형태 검증(KR: `^[0-9]{6}$`, US: `^[A-Za-z][A-Za-z.]{0,5}$`) — 랭킹
         응답에 다른 시장 심볼이나 이상값이 섞여도 잘못된 시장으로 편입되지 않는다.
      ③ 랭킹 순서(=입력 순서, 거래대금 내림차순) 보존한 채 상위 `top`개로 절단.
         중복 심볼은 첫 등장만 남긴다.
    """
    shape = _KR_SHAPE if market == "KR" else _US_SHAPE
    out: list[str] = []
    seen: set[str] = set()
    for row in rankings_rows:
        symbol = str(row.get("symbol", "")).strip()
        if not symbol or symbol in seen or symbol in existing:
            continue
        if not shape.match(symbol):
            continue
        seen.add(symbol)
        out.append(symbol)
        if len(out) >= top:
            break
    return out
