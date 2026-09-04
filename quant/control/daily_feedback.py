"""일일 피드백 — 오늘 진입한 체결의 **타이밍**을 규칙 기반으로 판정한다
(2026-08-26 소유자 조직도 역할 5).

> "오늘의 거래 및 종목 선정 기록이 어디가 잘못됐는지... 오늘 만든 거래에 당시
> 들어갈 때 트레이더가 남겨둔 진입 시그널이 어디에서 나온 건지 당시 상황을
> 보면서, 장이 종료됐을 때 상황과 결과를 바탕으로 피드백을 각 전략들한테
> 주는 거야 (예: 진입을 너무 늦게 함 고점매수, 거래량이 높았던 종목인데
> 막상 거래가 끊겼을 때 들어감)."

`quant/control/forensics.py`와 역할이 다르다: forensics는 청산까지 포함한
"왜 졌나"(MFE/MAE·청산 효율)를 재생한다. 이건 **진입 그 순간**만 본다 — 세
가지 규칙(고점매수/거래 소강 진입/늦은 진입)은 전부 소유자가 예시로 든 패턴
그대로다. 대표 사례에 forensics의 MFE/MAE·청산 효율을 곁들여 "타이밍만 나쁜
건지, 청산도 나쁜 건지"를 한 화면에서 보게 한다(forensics 재사용).

## 정직성 규칙 (forensics.py 와 같은 계약)

- **문제를 지어내지 않는다.** 진입 전 봉이 5개 미만이면 세 규칙 다 판정하지
  않는다 — 표본이 적으면 위치·거래량 비율이 소음이다.
- **look-ahead 없음(진입시점 기준 규칙).** 고점매수의 레인지 위치·늦은 진입의
  "당일 고점"은 **진입 시점까지의 봉만** 쓴다. 다만 고점매수의 "이후 MFE"는
  사후 피드백이라 의도적으로 미래 봉을 본다(forensics의 mfe_session_bp와
  같은 성격 — 실시간 필터가 아니라 그날 끝난 뒤의 회고).
- **임계는 [미검증 초기값]이다.** 렌더 텍스트 푸터에 항상 명시한다 — 이
  피드백 자체가 맞았는지 틀렸는지 표본이 쌓이면 사람이 조정한다.

## 평면

`quant/control/` 소속 — 원장을 읽어 전략에게 피드백을 주는 층이다. 거래
평면을 임포트하지 않는다. 순수 함수만 여기 있다(파일/봉 로딩은 CLI 호출부
몫) — forensics.py와 같은 설계 원칙.
"""
from __future__ import annotations

import pandas as pd

from quant.control.forensics import replay_all, summarize
from quant.control.ledger import round_trips as _round_trips
from quant.core import tgfmt

# 진입 전/후 판정에 필요한 최소 봉 수 — forensics.entry_range_control 과 같은
# 기준(5). 이보다 적으면 레인지 위치·거래량 비율·고점 시각이 소음이다.
MIN_BARS_BEFORE = 5

# 규칙별 임계 — 전부 [미검증 초기값]. 소유자가 준 예시를 수치화한 것일 뿐,
# 이 피드백이 실제로 유용했는지는 표본이 쌓여야 안다.
HIGH_RANGE_THRESHOLD = 0.90  # 진입 시점 레인지 위치 90% 이상 = "고점"
STALL_MFE_BP = 30.0  # 진입가의 +0.3% = 30bp. 이후 세션에 이보다 못 가면 "더 못 갔다"
VOLUME_LULL_RATIO = 0.50  # 진입 직전 5분 평균거래량 < 세션 평균의 50%
LATE_ENTRY_MINUTES = 30.0  # 당일 고점(진입시점까지) 형성 후 30분+ 경과


def entry_timing_findings(
    entry_ts,
    entry_price: float,
    day_bars,
) -> list[str]:
    """진입 1건의 타이밍 판정. 규칙에 안 걸리면 빈 리스트 — 지어내지 않는다.

    `day_bars`: 그 종목·그날 1분봉 전체(오름차순 인덱스, open/high/low/close/
    volume 컬럼) — forensics.replay_trip의 `day`와 같은 계약."""
    if day_bars is None or entry_price is None or entry_price <= 0 or len(day_bars) == 0:
        return []

    ts = pd.Timestamp(entry_ts)
    before = day_bars[day_bars.index <= ts]  # 진입 시점까지(레인지·고점 판정용)
    before_excl = day_bars[day_bars.index < ts]  # 진입 직전(진입 봉 자체는 제외)
    after = day_bars[day_bars.index >= ts]

    findings: list[str] = []

    # 규칙 1: 고점매수 — 진입 시점까지 레인지 위치 >=90% AND 이후 세션 MFE<30bp.
    if len(before) >= MIN_BARS_BEFORE:
        hi, lo = float(before["high"].max()), float(before["low"].min())
        if hi > lo:
            range_pos = (entry_price - lo) / (hi - lo)
            if range_pos >= HIGH_RANGE_THRESHOLD and len(after):
                mfe_bp = (float(after["high"].max()) - entry_price) / entry_price * 1e4
                if mfe_bp < STALL_MFE_BP:
                    findings.append(
                        f"고점매수 — 진입 시점 레인지 위치 {range_pos:.0%}, "
                        f"이후 세션 최대유리 {mfe_bp:+.1f}bp (고점에서 사서 더 못 갔다) "
                        "[미검증 초기값]"
                    )

    # 규칙 2: 거래 소강 진입 — 진입 직전 5분 평균거래량 < 그때까지 세션 평균의 50%.
    if len(before_excl) >= MIN_BARS_BEFORE:
        last5 = before_excl.tail(5)
        recent_avg_vol = float(last5["volume"].mean())
        session_avg_vol = float(before_excl["volume"].mean())
        if session_avg_vol > 0 and recent_avg_vol < session_avg_vol * VOLUME_LULL_RATIO:
            findings.append(
                f"거래 소강 진입 — 진입 직전 5분 평균거래량 {recent_avg_vol:.0f} "
                f"vs 그때까지 세션 평균 {session_avg_vol:.0f} "
                f"({recent_avg_vol / session_avg_vol:.0%}, 거래량 높았던 종목인데 "
                "거래가 끊겼을 때 들어감) [미검증 초기값]"
            )

    # 규칙 3: 늦은 진입 — 당일 고점(진입시점까지)이 30분+ 전에 이미 형성됐다.
    if len(before) >= MIN_BARS_BEFORE:
        peak_ts = before["high"].idxmax()
        gap_min = (ts - peak_ts).total_seconds() / 60.0
        if gap_min >= LATE_ENTRY_MINUTES:
            findings.append(
                f"늦은 진입 — 당일 고점(진입 시점까지) {gap_min:.0f}분 전 형성 후 진입"
                " (모멘텀이 이미 소진된 뒤) [미검증 초기값]"
            )

    return findings


def todays_round_trips(trades: list[dict], market: str, on: str) -> list[dict]:
    """원장(`ledger.load_trades()` 출력)에서 그 날·그 시장 체결만 골라 라운드
    트립으로 묶고, 진입가·진입 근거(reason)를 원본 체결에서 되찾아 붙인다.

    `ledger.round_trips()`가 트립의 `entry_ts`를 그 트립 첫 체결의 `ts` 그대로
    쓰는 것을 이용해 (전략, 종목, ts) 키로 원본 체결을 역참조한다 — round_trips
    자체는 고치지 않는다(스코어보드가 그 스키마를 전제로 읽어서, 거기에 필드를
    더하면 다른 호출부가 영향받는다)."""
    todays = [
        t for t in trades
        if str(t.get("market")) == market and str(t.get("ts") or "")[:10] == on
    ]
    trips = _round_trips(todays)
    fill_by_key = {
        (str(f.get("strategy_id")), str(f.get("symbol")), str(f.get("ts"))): f
        for f in todays
    }
    out: list[dict] = []
    for tr in trips:
        key = (tr["strategy"], tr["symbol"], str(tr["entry_ts"]))
        f = fill_by_key.get(key)
        enriched = dict(tr)
        enriched["entry_price"] = float(f["price"]) if f and f.get("price") is not None else None
        enriched["reason"] = (f.get("reason") or "") if f else ""
        out.append(enriched)
    return out


def strategy_feedback(trades_today: list[dict], bars_by_symbol: dict) -> dict[str, dict]:
    """오늘 진입 트립(`todays_round_trips()` 출력)을 전략별로 묶어 판정한다.

    `bars_by_symbol`: {종목코드: 그날 1분봉 전체 또는 None(없음)} — 호출부(CLI)가
    디스크에서 미리 읽어 넘긴다(이 함수는 파일을 모른다).

    반환: {전략ID: {n_entries, finding_counts, examples, forensics, forensics_skipped,
    bars_missing}}. `finding_counts`는 판정 태그(규칙 이름)별 건수, `examples`는
    태그별 대표 사례 1건(심볼·시각·reason·판정 문구 인용) — 판단은 사람이 하도록
    숫자와 사례만 낸다."""

    def _load(symbol, _ts):
        return bars_by_symbol.get(symbol)

    by_strategy: dict[str, list[dict]] = {}
    for t in trades_today:
        by_strategy.setdefault(t.get("strategy") or "?", []).append(t)

    out: dict[str, dict] = {}
    for strategy, trips in sorted(by_strategy.items()):
        finding_counts: dict[str, int] = {}
        examples: dict[str, dict] = {}
        bars_missing = 0
        for t in trips:
            bars = bars_by_symbol.get(t.get("symbol"))
            entry_price = t.get("entry_price")
            if bars is None:
                bars_missing += 1
                continue
            if entry_price is None:
                continue
            for f in entry_timing_findings(t["entry_ts"], float(entry_price), bars):
                tag = f.split(" — ")[0]
                finding_counts[tag] = finding_counts.get(tag, 0) + 1
                examples.setdefault(tag, {
                    "symbol": t.get("symbol"),
                    "entry_ts": str(t.get("entry_ts")),
                    "reason": t.get("reason", ""),
                    "finding": f,
                })

        rows, skipped = replay_all(trips, _load)
        # 부검 제외분의 원장 실현 합 — 재생 가능한 트립만 집계하면 초단타
        # (보유가 1분봉 2개 미만 = 즉시 손절류)가 통째로 빠져 좋은 거래만 남는
        # 선택 편향이 생긴다. 2026-08-27 실측: 스킵 5건에 -195.4bp 가 숨어
        # MFE 중앙 +105bp 가 합 -215bp 의 하루를 미화했다. 재생분의 ledger_bps
        # 를 종결 전체 합에서 빼서 구한다(forensics 반환 스키마 불변).
        skipped_ledger_bp = None
        if skipped:
            closed_sum = sum(
                t["bps"] for t in trips
                if t.get("exit_ts") and t.get("bps") is not None
            )
            replayed_sum = sum(
                r["ledger_bps"] for r in rows if r.get("ledger_bps") is not None
            )
            skipped_ledger_bp = closed_sum - replayed_sum
        out[strategy] = {
            "n_entries": len(trips),
            "finding_counts": finding_counts,
            "examples": examples,
            "forensics": summarize(rows),
            "forensics_skipped": skipped,
            "bars_missing": bars_missing,
            "skipped_ledger_bp": skipped_ledger_bp,
        }
    return out


def already_recorded(existing_rows: list[dict], target_date: str, market: str) -> bool:
    """`data/ledger/daily_feedback.jsonl` 멱등 append 판정 — 같은 (날짜, 시장)이
    이미 있으면 True. CLI가 이 결과로 append 여부를 정한다(I/O는 CLI 몫)."""
    return any(r.get("date") == target_date and r.get("market") == market for r in existing_rows)


def render_feedback_text(target_date, market: str, feedback: dict[str, dict],
                         report_url: str | None = None) -> str:
    """텔레그램 HTML 피드백 텍스트(tgfmt, 2026-09-04). 판정 0건인 전략은
    "특이사항 없음" 한 줄 — 없는 문제를 지어내지 않는다. 전략별 상세(판정·예시·
    부검)는 길어질 수 있어 접이식 인용 블록에 담는다. `report_url`이 있으면
    (그날의 회사 리포트 HTML) 맨 끝에 링크를 붙인다."""
    header = tgfmt.b(f"📋 일일 피드백 — {market} {target_date.isoformat()}")
    footer = tgfmt.i("※ 임계는 [미검증 초기값] — 이 피드백 자체의 적중도 표본이 쌓이면 조정한다.")
    if report_url:
        footer += "\n" + tgfmt.link("전체 리포트", report_url)

    if not feedback:
        return tgfmt.compose(header, ["오늘 진입 체결 없음"], footer)

    sections = []
    for strategy, d in sorted(feedback.items()):
        detail_lines: list[str] = []
        if not d["finding_counts"]:
            detail_lines.append("특이사항 없음")
        else:
            for tag, n in sorted(d["finding_counts"].items(), key=lambda kv: -kv[1]):
                detail_lines.append(f"{tag}: {n}건")
                ex = d["examples"].get(tag)
                if ex:
                    detail_lines.append(f"  예) {ex['symbol']} {ex['entry_ts']} — {ex['finding']}")
                    if ex.get("reason"):
                        detail_lines.append(f"  진입 근거: {ex['reason']}")
        fx = d.get("forensics") or {}
        if fx.get("n"):
            eff = fx.get("exit_efficiency_median")
            detail_lines.append(
                f"청산: MFE 중앙 {fx['mfe_bp_median']:+.1f}bp · "
                f"MAE 중앙 {fx['mae_bp_median']:+.1f}bp · "
                f"청산효율 {('%.2f' % eff) if eff is not None else '측정 불가'}"
            )
        skipped = d.get("forensics_skipped", 0)
        missing = d.get("bars_missing", 0)
        if skipped or missing:
            # 제외분의 원장 실현 합을 같이 보여야 부검 요약이 하루를 미화하지
            # 않는다 — 스킵되는 건 주로 즉시 손절류 초단타다(위 주석 실측).
            sk_bp = d.get("skipped_ledger_bp")
            tail = f" · 제외분 원장 실현 합 {sk_bp:+.1f}bp" if sk_bp is not None else ""
            detail_lines.append(
                f"(부검 제외 — 진입타이밍 {missing}건(봉 없음) · "
                f"청산부검 {skipped}건(봉 없음/보유가 1분봉 해상도 미만){tail})"
            )
        sections.append(
            tgfmt.b(f"[{strategy}] 진입 {d['n_entries']}건") + "\n"
            + tgfmt.quote("\n".join(detail_lines), expandable=True)
        )

    return tgfmt.compose(header, sections, footer)
