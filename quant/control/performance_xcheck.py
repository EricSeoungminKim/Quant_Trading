"""공개 사이트 payload ↔ 스코어보드 "에폭 이후" 절 교차 대조 (2026-09-06, Phase 5).

`quant.control.performance_contract`가 payload **한 장**의 내부 정합성(구조·타입·
자기모순)을 본다면, 이 모듈은 **같은 원장에서 독립적으로 계산된 두 결과물**이
서로 맞는지를 본다:

- **스코어보드 경로**: `quant.control.ledger.round_trips_since_epoch` — `cli
  scoreboard`(텔레그램 주간 크론)가 실제로 부르는 함수.
- **사이트 payload 경로**: `quant.control.performance._epoch_trades` +
  `quant.control.ledger.round_trips` — `build_performance_payload`의
  `paper_epoch` 서브트리(`_build_paper_epoch`)가 내부적으로 쓰는 스코핑.

두 경로는 서로 다른 모듈에 따로 구현돼 있지만(에폭 경계 필터링을 각자 코드로
짰다) 저수준 함수(`round_trips`)는 공유한다 — "같은 함수, 독립된 경로"라는 뜻은
이것이다: 트립을 왕복으로 묶는 산수(`round_trips`)는 하나뿐이지만, "어느 체결이
에폭 이후인가"를 고르는 필터링은 두 번 따로 짜여 있다. 두 필터링이 실제로
같은 체결 집합을 골라내는지가 이 모듈이 답하는 질문이다 — 다르면 사이트 숫자와
텔레그램 스코어보드 숫자가 갈라진다(오너가 둘 다 본다).

세 번째 대조는 `build_performance_payload`가 실제로 낸 JSON 자체(사이트에 나갈
그 값)를 위 재계산과 대조한다 — payload 조립 단계(`_epoch_market_curve`의 날짜별
합산·반올림)가 트립 집합 자체와는 다른 곳에서 숫자를 깨뜨릴 수 있어서다.

마지막으로 `paper_epoch.overall`(모든 계좌를 KRW로 합산한 지분곡선)의 최종
누적값이 전략별 곡선(각자 통화)의 최종 누적값을 KRW로 환산해 합한 것과 같은지
본다 — "여러 독립 계좌의 합"이라는 오너의 정의(2026-09-06)가 실제로 지켜지는지.
날짜별로 각각 반올림해 누적하는 두 경로(전략별 곡선 vs 전체 곡선)라 부동소수점
반올림만큼의 오차는 있을 수 있다("within rounding") — `_OVERALL_SUM_TOLERANCE_KRW`
가 그 여유를 정의한다.

순수 함수 — 파일 I/O 없음. 원장(`ledger.load_trades` 출력)과 설정 블록만 받는다.
"""
from __future__ import annotations

from quant.control.ledger import paper_epoch_ts, round_trips, round_trips_since_epoch
from quant.control.performance import FX_KRW_PER_USD, _epoch_trades, build_performance_payload
from quant.control.performance_contract import Finding

__all__ = ["cross_check"]

# 승률/기대값/손익 비교 허용오차 — 같은 트립 리스트를 같은 산수로 두 번 돌리면
# 이론상 바이트 단위로 같아야 하지만, 합산 순서가 달라질 수 있는 부동소수점
# 연산이라 아주 작은 여유를 둔다. 실제 데이터 결함(트립 누락/중복)은 이 여유보다
# 몇 자릿수 큰 차이를 낸다.
_STAT_TOLERANCE = 1e-6
# 공개 JSON 의 `cum_native` 는 통화 소수 2자리로 반올림돼 나간다(performance.py) — 재계산은 전체
# 정밀도라 1e-6 로 비교하면 반올림 자체가 "불일치"가 된다. 2026-09-07 16:25 첫 에폭 발행이
# 정확히 이 오탐(json=-58398.84 vs 재계산=-58398.8417…)으로 push 가 막혔다. 반올림 단위(0.01)
# 의 절반보다 넉넉한 0.01 을 허용한다 — 진짜 결함(트립 누락·경계 오류)은 원 단위 이상 어긋난다.
_CUM_NATIVE_TOLERANCE = 0.01

# "전략별 합 vs 전체 지분" 허용오차 — 전략별 곡선(`_epoch_market_curve`)과 전체
# 곡선(`_epoch_overall_rows`)은 각자 날짜별로 반올림(소수 2자리)해 누적하므로
# 최종값에 반올림 오차가 실린다. US 계좌 하나당 최대 0.005(USD 반올림 폭) ×
# FX_KRW_PER_USD ≈ 6.9원, KR 계좌 하나당 최대 0.5원 — 계좌 수에 비례해 여유를 둔다.
_OVERALL_SUM_BASE_TOLERANCE_KRW = 5.0
_OVERALL_SUM_PER_US_ACCOUNT_KRW = 10.0
_OVERALL_SUM_PER_KR_ACCOUNT_KRW = 1.0


def _group_stats(trips: list[dict]) -> dict[tuple[str, str], dict]:
    """트립 목록(`ledger.round_trips` 출력) → (전략, 시장)별 {n, win_rate,
    expectancy_bp, pnl}. `pnl_known=True`만 센다 — `ledger._strategy_block`/
    `performance._round_trip_stats`와 같은 승률·기대값 산식(bps 축)이지만
    이 모듈에서 세 번째로 독립 구현한다."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for t in trips:
        if not t.get("pnl_known"):
            continue
        by_key.setdefault((str(t["strategy"]), str(t["market"])), []).append(t)

    out: dict[tuple[str, str], dict] = {}
    for key, ts in by_key.items():
        n = len(ts)
        wins = [t for t in ts if t["pnl"] > 0]
        losses = [t for t in ts if t["pnl"] <= 0]
        wr = len(wins) / n
        aw = sum(t["bps"] for t in wins) / len(wins) if wins else 0.0
        al = abs(sum(t["bps"] for t in losses) / len(losses)) if losses else 0.0
        out[key] = {
            "n": n,
            "win_rate": wr,
            "expectancy_bp": wr * aw - (1 - wr) * al,
            "pnl": sum(t["pnl"] for t in ts),
        }
    return out


def cross_check(
    trades: list[dict],
    strategies_cfg: dict | None = None,
    execution_cfg: dict | None = None,
) -> list[Finding]:
    """`trades`(원장 전체) → `Finding` 목록. 빈 목록이면 두 경로가 완전히 일치.

    에폭 마커(`cli paper-epoch`)가 아직 없으면(`paper_epoch_ts`가 None) 대조
    대상 자체가 없다 — 빈 목록을 낸다(에러 아님, 아직 이 시대가 시작 안 함).
    """
    findings: list[Finding] = []

    epoch_ts = paper_epoch_ts(trades)
    if epoch_ts is None:
        return findings

    # 경로 A: 스코어보드가 실제로 쓰는 함수 (quant.control.ledger).
    scoreboard_stats = _group_stats(round_trips_since_epoch(trades))

    # 경로 B: 사이트 payload가 내부적으로 쓰는 스코핑 (quant.control.performance) —
    # 별도 모듈, 별도 필터링 구현. 저수준 round_trips만 공유한다.
    site_trips = round_trips(_epoch_trades(trades, epoch_ts))
    site_stats = _group_stats(site_trips)

    for key in sorted(set(scoreboard_stats) | set(site_stats)):
        sid, market = key
        path = f"paper_epoch.strategies[id={sid}].by_market.{market}"
        a, b = scoreboard_stats.get(key), site_stats.get(key)
        if a is None or b is None:
            findings.append(Finding(
                "error", path,
                f"한쪽 경로에만 트립 존재 — scoreboard={'있음' if a else '없음'} "
                f"site={'있음' if b else '없음'}",
            ))
            continue
        if a["n"] != b["n"]:
            findings.append(Finding(
                "error", f"{path}.trips", f"트립 수 불일치: scoreboard={a['n']} site={b['n']}",
            ))
        if abs(a["win_rate"] - b["win_rate"]) > _STAT_TOLERANCE:
            findings.append(Finding(
                "error", f"{path}.win_rate",
                f"승률 불일치: scoreboard={a['win_rate']:.6f} site={b['win_rate']:.6f}",
            ))
        if abs(a["expectancy_bp"] - b["expectancy_bp"]) > _STAT_TOLERANCE:
            findings.append(Finding(
                "error", f"{path}.expectancy_bp",
                f"기대값 불일치: scoreboard={a['expectancy_bp']:.6f}bp site={b['expectancy_bp']:.6f}bp",
            ))
        if abs(a["pnl"] - b["pnl"]) > _STAT_TOLERANCE:
            findings.append(Finding(
                "error", f"{path}.pnl", f"손익 불일치: scoreboard={a['pnl']:.6f} site={b['pnl']:.6f}",
            ))

    # 경로 C: build_performance_payload가 실제로 낼 JSON(공개 사이트에 나갈 그
    # 값) — 트립 집합 자체는 맞아도 payload 조립 단계(날짜별 합산·반올림)에서
    # 깨질 수 있어 별도로 대조한다.
    payload = build_performance_payload(trades, execution_cfg or {}, strategies_cfg=strategies_cfg)
    pe = payload.get("paper_epoch") or {}
    strategy_rows = pe.get("strategies") or []

    for row in strategy_rows:
        sid = row.get("id")
        curve = row.get("curve") or {}
        for mkey, market in (("asia", "KR"), ("us", "US")):
            points = curve.get(mkey) or []
            payload_n = points[-1]["trips"] if points else 0
            payload_pnl = points[-1]["cum_native"] if points else 0.0
            stats = site_stats.get((sid, market))
            expected_n = stats["n"] if stats else 0
            expected_pnl = stats["pnl"] if stats else 0.0
            path = f"paper_epoch.strategies[id={sid}].curve.{mkey}"
            if payload_n != expected_n:
                findings.append(Finding(
                    "error", f"{path}[-1].trips",
                    f"공개 JSON 트립 수 불일치: json={payload_n} 재계산={expected_n}",
                ))
            if abs(payload_pnl - expected_pnl) > _CUM_NATIVE_TOLERANCE:
                findings.append(Finding(
                    "error", f"{path}[-1].cum_native",
                    f"공개 JSON 손익 불일치: json={payload_pnl} 재계산={expected_pnl}",
                ))

    # 전체 지분(overall) = 전략별 계좌 합 (오너 정의 2026-09-06: "사이트 전체
    # 지분곡선은 여러 독립 계좌의 합").
    overall_rows = (pe.get("overall") or {}).get("rows") or []
    if overall_rows and strategy_rows:
        declared_cum_krw = overall_rows[-1].get("cum_krw")
        computed = 0.0
        n_kr_accounts = 0
        n_us_accounts = 0
        for row in strategy_rows:
            curve = row.get("curve") or {}
            asia_pts = curve.get("asia") or []
            us_pts = curve.get("us") or []
            if asia_pts:
                computed += asia_pts[-1]["cum_native"]
                n_kr_accounts += 1
            if us_pts:
                computed += us_pts[-1]["cum_native"] * FX_KRW_PER_USD
                n_us_accounts += 1
        tolerance = (
            _OVERALL_SUM_BASE_TOLERANCE_KRW
            + _OVERALL_SUM_PER_KR_ACCOUNT_KRW * n_kr_accounts
            + _OVERALL_SUM_PER_US_ACCOUNT_KRW * n_us_accounts
        )
        if declared_cum_krw is not None and abs(computed - declared_cum_krw) > tolerance:
            findings.append(Finding(
                "error", "paper_epoch.overall.rows[-1].cum_krw",
                f"전략별 계좌 합({computed:.2f}원)과 전체 지분({declared_cum_krw}원) 불일치 "
                f"— 허용오차 {tolerance:.2f}원 초과",
            ))

    return findings
