#!/usr/bin/env python3
"""PASS 토큰(예: `000660:TREND+EVENT AAPL`, 공백 구분, 태그 없는 심볼은 콜론 생략)을
받아 종목별 배정 전략을 붙인 표시줄을 만든다.

`own_brief.sh`가 "확신도 엔진 통과 → 자동 편입" 텔레그램 메시지에 종목별 배정
전략을 보여주기 위해 부른다(2026-08-26, 소유자 조직도 역할 3). `quant.trade.
strategy.TAG_ASSIGNMENT`를 그대로 재사용한다 — 배정표가 바뀌면 이 출력도 자동
으로 따라간다.

own_brief.sh는 셸 스크립트라 이 헬퍼가 죽어도(임포트 에러 등) 기존 메시지
(심볼만 나열)로 계속 나가야 한다 — 그래서 이 스크립트는 실패하면 비정상
종료만 하고 stdout에 아무것도 찍지 않는다. 호출부(own_brief.sh)가 빈 출력을
폴백 신호로 쓴다.

사용: format_tag_assignment.py "000660:TREND+EVENT AAPL"
출력: 000660(TREND+EVENT → news_momentum·전체감시) AAPL(무태그 → 전체감시)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from quant.trade.strategy import describe_tags  # noqa: E402


def _format_one(token: str) -> str:
    if ":" in token:
        symbol, tag_str = token.split(":", 1)
        tags = [t for t in tag_str.split("+") if t]
    else:
        symbol, tags = token, []
    label = "+".join(tags) if tags else "무태그"
    return f"{symbol}({label} → {describe_tags(tags)})"


def main() -> int:
    raw = sys.argv[1] if len(sys.argv) > 1 else ""
    tokens = [t for t in raw.split() if t]
    if not tokens:
        return 1
    print(" ".join(_format_one(t) for t in tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main())
