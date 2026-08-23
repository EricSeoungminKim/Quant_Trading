"""장마감 결과 리포트 — 채점 루프(E) Task 5(§E-3). 순수 조립 함수만.

## 무엇을 요약하는가

`quant.apps.cli outcomes`(16:00 KST 크론)가 그날 만기가 된 지평의 전방 수익률을
선정 원장(`quant/control/selections.py`)에 채운다. 이 모듈은 그 결과 중 **오늘
채워진 것만** 골라 상위/하위 성과와 표본 수를 사람이 읽는 문장으로 조립한다.

## 왜 순수인가

`build_close_report`는 시세 조회도 파일 I/O도 하지 않는다 — 호출부(`cli
close-report`)가 이미 계산한 값(오늘 만기 레코드, 리더보드 판정, 누적
스코어보드 문자열)만 받아 문장으로 조립한다. 네트워크·리더보드 계산 실패가
리포트 조립 자체를 막지 않고, 테스트가 픽스처만으로 전체 문구를 검증할 수 있다.

**이 모듈은 narrate(서술기)를 전혀 모른다** — 여기서 리턴한 문자열이 "서술기가
죽어도 나가는 결정론 요약"이 되려면, 호출부가 이 문자열을 narrate 호출 **전에**
찍고 flush 해야 한다(순서를 책임지는 곳은 `quant/apps/cli.py`의
`cmd_close_report`다 — 2026-08-15 리뷰: narrate 를 먼저 부르면 그 서브프로세스가
셸 timeout 보다 오래 걸릴 때 SIGTERM 으로 죽어 stdout 이 통째로 빈다).

## 표본 수는 항상 병기한다

저장소 규율("성과를 말할 때는 항상 표본 수를 같이 말한다")에 따라 만기 0건이면
침묵하지 않고 "오늘 만기 지평 없음"이라고 명시한다 — 조용한 빈 리포트는 "오늘
정말 아무 일도 없었다"와 "리포트가 고장났다"를 구분하지 못한다.
"""
from __future__ import annotations

from quant.control.judgment import HOLD_HORIZONS
from quant.control.leaderboard import Verdict


def matured_today(rows: list[dict], today: str) -> list[dict]:
    """선정 원장 행들 중 **오늘** 만기가 채워진 지평만 뽑아 평평한 레코드로.

    `outcomes.apply_outcome`이 지평을 채울 때 `outcome_d{h}_asof`에 실제 기준
    날짜를 남긴다 — "그 지평이 오늘 채워졌나"는 이 필드로 판단한다(선정 당시
    날짜인 `row["date"]`가 아니다).

    한 행이 여러 지평을 같은 날 동시에 채우는 것도(근사 거래일 계산이 겹치면)
    이론상 가능하므로 지평별로 별도 레코드를 낸다.
    """
    out: list[dict] = []
    for row in rows:
        for h in HOLD_HORIZONS:
            if str(row.get(f"outcome_d{h}_asof")) != today:
                continue
            bps = row.get(f"outcome_d{h}_bps")
            if bps is None:
                continue
            out.append({
                "symbol": row.get("symbol"),
                "market": row.get("market"),
                "horizon": h,
                "bps": float(bps),
            })
    return out


def _rank_line(label: str, records: list[dict]) -> str:
    parts = [f"{r['symbol']} {r['bps']:+.0f}bp(D+{r['horizon']})" for r in records]
    return f"{label}: " + ", ".join(parts)


def build_close_report(matured: list[dict], verdicts: dict[str, Verdict],
                       scoreboard: str) -> str:
    """오늘 만기 레코드 + 리더보드 판정 + 누적 스코어보드 문자열 → 리포트 문장.

    `matured`: `matured_today()`의 결과(또는 같은 모양 — symbol/market/horizon/bps).
    `verdicts`: {생산자 라벨: `leaderboard.Verdict`} — 라벨은 호출부가 짓는다
    (예: "watch_scorer/2"). 아직 판단 표본이 없으면 빈 dict를 넘긴다.
    `scoreboard`: 이미 포맷된 텍스트(`ledger.scoreboard_text()` 결과). 빈 문자열이면
    생략한다.
    """
    lines = ["📋 장마감 결과 리포트"]

    n = len(matured)
    if n == 0:
        lines.append("오늘 만기 지평 없음 (표본 0건)")
    else:
        lines.append(f"오늘 만기 {n}건")
        ranked_desc = sorted(matured, key=lambda r: r["bps"], reverse=True)
        ranked_asc = sorted(matured, key=lambda r: r["bps"])
        lines.append(_rank_line("상위", ranked_desc[:3]))
        lines.append(_rank_line("하위", ranked_asc[:3]))

    lines.append("")
    lines.append("리더보드 판정:")
    if verdicts:
        for label in sorted(verdicts):
            v = verdicts[label]
            lines.append(f"  [{label}] {v.verdict} — {v.reason}")
    else:
        lines.append("  판정 없음 (판단 표본이 아직 없음)")

    if scoreboard:
        lines.append("")
        lines.append(scoreboard)

    return "\n".join(lines)
