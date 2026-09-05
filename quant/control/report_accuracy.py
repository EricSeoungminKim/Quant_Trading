"""리포트 정확도 채점 — 순수 함수만 (I/O는 `quant/apps/report_cli.py`,
원장 기록은 `quant/report/collect/ledger.py`).

## 왜 만드나 (2026-09-06 소유자 지시, priority-1)

리포트가 매일 시장 방향·섹터·종목 후보·목표가를 "주장"하는데, 그 주장이 맞았는지
재는 장치가 없었다. `quant/control/selections.py`가 종목 속성 벡터 + 전방 수익률을
이미 쌓고 있어(리더보드용) 감사(2026-09-06, docs 미기록·세션 로그 참고)는 그걸
1차 자료로 썼다. 이 모듈은 그 감사를 **반복 가능한 코드**로 옮긴 것이다 — 매일
빌드 시점에 "그날 리포트가 뭐라 주장했는지"를 `data/ledger/report_claims.jsonl`에
남기고, 밤마다 만기된 주장만 채점해 `data/ledger/report_accuracy.jsonl`에 쌓는다.

## 무엇을 채점하는가, 무엇을 채점하지 않는가

- **방향콜**: 오전판 `payload["stance"]`(label/score100) — bull/bear/neutral로
  버킷화해 세션 이후 지수(KR=069500 KODEX200, US=QQQ) 실현 방향과 대조한다.
- **종목 후보**: 오전판 후보 심볼(`AUTO_WATCH`에 실제로 실린 것) + 마감판
  종가배팅 후보(`close_bet_view`) — 점수(ai_score100/trending_score100/
  close_bet score)와 태그(origin)별 전방수익률·IC·캘리브레이션.
- **섹터 픽**: 이 모듈은 다루지 않는다 — `sector_daily.jsonl`가 이미 있고
  2026-09-03에 도입돼 표본이 며칠 안 된다(감사 결과 n<20, 판단 불가). 표본이
  쌓이면 이 모듈에 같은 패턴(추출→채점)으로 추가한다.
  TODO(2026-09-06): 그 채점을 추가할 때 `sector_daily.jsonl`의 `us_link_ret`
  필드(전일 US 섹터 ETF 종가→종가 수익률, GICS 매핑을 통해 그 KR 업종에
  연결된 값 — `quant.analyze.kr_sectors.us_sector_signal` 근거: Rank IC
  mean=0.070, t=4.31, n=488)와 `composite_score` 필드(거래대금 순위 기반
  점수 + `US_LINK_WEIGHT * us_link_ret`, weight 기본값 0)도 같이 추출해
  "거래대금 순위만" vs "순위+US신호 결합"의 다음날 그 업종 KR 초과수익률
  예측력을 비교한다 — `quant.analyze.sector_daily.US_LINK_WEIGHT`를 0이
  아닌 값으로 올릴지는 이 비교 결과로 사람이 판단한다.
- **"같은 유니버스에서 무작위 추출" 대조군**: 이 모듈은 다루지 않는다 —
  `report_claims.jsonl`은 후보만 남기고(전량 유니버스를 매일 복제하면
  `selections.jsonl`과 중복이라 유지비만 커진다) 무작위 대조군은 `selections.
  jsonl` 전량을 봐야 계산된다. 그 비교가 필요하면 감사처럼 selections.jsonl을
  직접 읽는 별도 스크립트로 한다 — 이 모듈/CLI는 시장 대비 초과수익까지만 낸다.

## 거래일 근사

`quant/control/outcomes.py`와 같은 한계를 그대로 안고 간다 — 공휴일 달력이
없어 지수 종가 시퀀스(호출부가 넘기는 `calendar`)에 실제로 존재하는 날짜만
"거래일"로 센다. 그래서 `calendar`는 순수 인자다(이 모듈이 직접 거래일을
추정하지 않는다) — 호출부(`report_cli.py`)가 인덱스 시세를 채워 넘긴다.
"""
from __future__ import annotations

import math
from datetime import date

# 리포트 방향콜/종목 후보를 재는 지평(거래일). judgment.py의 HOLD_HORIZONS
# (1,5,20 — LLM 리더보드용)와 의도적으로 다르다 — 이건 소유자가 명시한
# 1일/3일/5일이다.
HORIZONS = (1, 3, 5)

SCHEMA = 1

# 지수 프록시 — CLAUDE.md 국면(regime) 절과 같다: US=QQQ, KR=KODEX200(069500).
INDEX_SYMBOL = {"US": "QQQ", "KR": "069500"}


# ── 방향콜 버킷화 ────────────────────────────────────────────────────────

def direction_bucket(label: str | None, score100: int | float | None) -> str | None:
    """방향콜 라벨/점수 → bull/bear/neutral. 라벨과 점수 둘 다 없으면 None
    (그날 스탠스를 못 구했다 — 중립으로 위장하지 않는다)."""
    if label is None and score100 is None:
        return None
    txt = label or ""
    if "상승" in txt or (score100 is not None and score100 >= 60):
        return "bull"
    if "하락" in txt or (score100 is not None and score100 <= 40):
        return "bear"
    return "neutral"


# ── 리포트 payload → 주장(claims) 추출 (순수) ───────────────────────────

def extract_open_claims(payload: dict, candidate_symbols: set[str]) -> dict | None:
    """오전판 engine payload → 청구(claims) 한 행. `symbols`/`stance`가 전혀
    없으면 None(빈 리포트 — 남길 게 없다).

    `candidate_symbols`: `AUTO_WATCH` 줄에 실제로 실린 심볼 집합 — 호출부
    (`ledger.py`)가 `quant.report.collect.intraday._candidate_symbols(payload)`
    로 이미 계산해 `selections.build_rows`에도 넘기는 바로 그 값이다. 여기서
    `AUTO_WATCH` 문자열을 다시 파싱하지 않는다 — 두 번 계산하면 두 원장
    (`selections.jsonl`/`report_claims.jsonl`)의 후보 판정이 갈릴 수 있다."""
    symbols = payload.get("symbols") or []
    stance = payload.get("stance") or {}
    if not symbols and not stance:
        return None
    candidates = []
    for sym in symbols:
        symbol = sym.get("symbol")
        if not symbol or symbol not in candidate_symbols:
            continue
        candidates.append({
            "symbol": symbol,
            "name": sym.get("name"),
            "score": sym.get("ai_score100"),
            "score_kind": "ai_score100",
            "trending_score100": sym.get("trending_score100"),
            "origin": sym.get("origin"),
            "upside_pct": sym.get("upside_pct"),
        })
    return {
        "schema": SCHEMA,
        "date": payload.get("session_date"),
        "market": payload.get("market"),
        "session": "open",
        "recorded_at": payload.get("generated_at"),
        "direction": (
            {"label": stance.get("label"), "score100": stance.get("score100")}
            if stance else None
        ),
        "candidates": candidates,
    }


def extract_close_claims(payload: dict) -> dict | None:
    """마감판 engine payload → 청구 한 행. `close_bet_view`가 없으면 None.
    마감판은 시장 방향콜을 새로 내지 않는다(엔진 payload에 `stance` 키 자체가
    없다) — `direction`은 항상 None."""
    close_bet = payload.get("close_bet_view") or []
    if not close_bet:
        return None
    candidates = [
        {
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "score": row.get("score"),
            "score_kind": "close_bet_score",
            "trending_score100": None,
            "origin": "close_bet",
            "upside_pct": None,
        }
        for row in close_bet
    ]
    return {
        "schema": SCHEMA,
        "date": payload.get("session_date"),
        "market": payload.get("market"),
        "session": "close",
        "recorded_at": payload.get("generated_at"),
        "direction": None,
        "candidates": candidates,
    }


# ── 거래일 산술 (순수 — calendar는 호출부가 채운 정렬된 날짜 문자열 리스트) ──

def prev_trading_day(calendar: list[str], d: str) -> str | None:
    """`d`(달력일) 이전 마지막 거래일. 실측(2026-09-06 감사, 무작위 800건):
    리포트가 기록한 기준가는 빌드일의 '전일 종가'와 거의 항상 일치한다
    (KR 298/370·US 370/430 이 정확히 -1 거래일). `close_date`가 없는 행은
    이 함수로 기준 세션을 역산해야 한다 — 빌드일 자체를 기준으로 쓰면
    D+1 계산이 하루 밀린다."""
    earlier = [x for x in calendar if x < d]
    return earlier[-1] if earlier else None


def trading_day_plus(calendar: list[str], base_date: str, h: int) -> str | None:
    """`base_date` 가 속한 거래일 인덱스 + h(거래일 수). 없으면 None.
    `base_date` 자체가 거래일이 아니면(휴장) 그 다음 첫 거래일을 0번째로 본다."""
    if base_date in calendar:
        idx = calendar.index(base_date)
    else:
        later = [x for x in calendar if x > base_date]
        if not later:
            return None
        idx = calendar.index(later[0])
    tgt = idx + h
    if 0 <= tgt < len(calendar):
        return calendar[tgt]
    return None


# ── 통계 유틸 (순수) ─────────────────────────────────────────────────────

def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """적중 k/n 의 Wilson 95% 신뢰구간. n=0 이면 (nan, nan)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1 + z * z / n
    center = p + z * z / (2 * n)
    adj = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return ((center - adj) / denom, (center + adj) / denom)


def spearman_ic(pairs: list[tuple[float, float]]) -> float | None:
    """Spearman 순위상관 — scipy 없이 직접 구현(quant/control은 무거운 선택적
    의존을 늘리지 않는다). n<2 면 None. 동순위는 평균 순위로 처리한다."""
    n = len(pairs)
    if n < 2:
        return None

    def _ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    rx = _ranks(xs)
    ry = _ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


# ── 채점 (순수 — price_lookup/index_lookup은 호출부가 채운 딕셔너리) ────
#
# price_lookup: {(market, symbol, date_str): (open, close)} — 없는 키는 결측.
# index_lookup: {(market, date_str): close} — 지수 프록시 종가.

def score_direction_claims(claims: list[dict], calendar_by_market: dict[str, list[str]],
                           index_lookup: dict[tuple[str, str], float]) -> dict:
    """방향콜 채점 — 지평별 {n, hit, ci_lo, ci_hi, rate}. 방향콜이 neutral/None
    이거나 지수 시세가 없는 행은 표본에서 뺀다(지어내지 않는다)."""
    out: dict[int, dict] = {}
    for h in HORIZONS:
        hits = 0
        n = 0
        for c in claims:
            d = c.get("direction")
            if not d:
                continue
            bucket = direction_bucket(d.get("label"), d.get("score100"))
            if bucket not in ("bull", "bear"):
                continue
            market = c.get("market")
            cal = calendar_by_market.get(market) or []
            ref = prev_trading_day(cal, c.get("date") or "") or c.get("date")
            tgt = trading_day_plus(cal, ref, h) if ref else None
            if tgt is None:
                continue
            base = index_lookup.get((market, ref))
            future = index_lookup.get((market, tgt))
            if base is None or future is None or base <= 0:
                continue
            realized = (future - base) / base
            hit = (bucket == "bull" and realized > 0) or (bucket == "bear" and realized < 0)
            n += 1
            hits += int(hit)
        lo, hi = wilson_ci(hits, n)
        out[h] = {"n": n, "hits": hits, "rate": (hits / n) if n else None,
                  "ci_lo": lo, "ci_hi": hi}
    return out


def _forward_return_bps(base: float | None, future: float | None) -> float | None:
    if not base or not future or base <= 0:
        return None
    return (future - base) / base * 10_000


def score_candidate_claims(
    claims: list[dict],
    calendar_by_market: dict[str, list[str]],
    price_lookup: dict[tuple[str, str, str], tuple[float | None, float | None]],
) -> dict:
    """종목 후보 채점 — 지평별 {n, mean_bps, hitrate}, 점수 IC, 태그별 성과.

    `price_lookup[(market, symbol, date)]` = (open, close). 기준가는 종목이
    선정된 세션의 직전 거래일 종가(오전판 규약, 위 `prev_trading_day` 참고).
    close_bet(마감판) 후보는 세션 자체가 이미 마감 시점 스코어라 같은 규약을
    쓴다 — 기준가만 하루 어긋나도 지평 전체가 밀리므로 오전판/마감판을 다른
    규칙으로 다루지 않는다(단일 규약 = 단일 버그 표면)."""
    horizon_stats: dict[int, dict] = {h: {"n": 0, "sum_bps": 0.0, "hits": 0} for h in HORIZONS}
    score_pairs: dict[int, list[tuple[float, float]]] = {h: [] for h in HORIZONS}
    tag_stats: dict[str, dict[int, dict]] = {}
    calib_bins = [(-1, 40), (40, 55), (55, 70), (70, 85), (85, 101)]
    calib: dict[str, dict] = {
        f"{lo}-{hi - 1}": {"n": 0, "sum_bps": 0.0, "hits": 0} for lo, hi in calib_bins
    }

    for c in claims:
        market = c.get("market")
        cal = calendar_by_market.get(market) or []
        ref = prev_trading_day(cal, c.get("date") or "") or c.get("date")
        if not ref:
            continue
        for cand in c.get("candidates") or []:
            symbol = cand.get("symbol")
            if not symbol:
                continue
            base_px = price_lookup.get((market, symbol, ref))
            base_close = base_px[1] if base_px else None
            if base_close is None:
                continue
            origin = cand.get("origin") or "(none)"
            score = cand.get("score")
            for h in HORIZONS:
                tgt = trading_day_plus(cal, ref, h)
                if tgt is None:
                    continue
                fut_px = price_lookup.get((market, symbol, tgt))
                fut_close = fut_px[1] if fut_px else None
                bps = _forward_return_bps(base_close, fut_close)
                if bps is None:
                    continue
                st = horizon_stats[h]
                st["n"] += 1
                st["sum_bps"] += bps
                st["hits"] += int(bps > 0)

                if score is not None:
                    score_pairs[h].append((float(score), bps))

                tag_h = tag_stats.setdefault(origin, {h2: {"n": 0, "sum_bps": 0.0, "hits": 0} for h2 in HORIZONS})
                tag_h[h]["n"] += 1
                tag_h[h]["sum_bps"] += bps
                tag_h[h]["hits"] += int(bps > 0)

                if h == 1 and score is not None:
                    for lo, hi in calib_bins:
                        if lo < score < hi or (lo == -1 and score <= 0):
                            key = f"{lo}-{hi - 1}"
                            calib[key]["n"] += 1
                            calib[key]["sum_bps"] += bps
                            calib[key]["hits"] += int(bps > 0)
                            break

    def _finish(st: dict) -> dict:
        n = st["n"]
        return {
            "n": n,
            "mean_bps": (st["sum_bps"] / n) if n else None,
            "hitrate": (st["hits"] / n) if n else None,
        }

    return {
        "horizons": {h: _finish(st) for h, st in horizon_stats.items()},
        "ic": {h: spearman_ic(pairs) for h, pairs in score_pairs.items()},
        "ic_n": {h: len(pairs) for h, pairs in score_pairs.items()},
        "by_tag": {tag: {h: _finish(st) for h, st in hs.items()} for tag, hs in tag_stats.items()},
        "calibration_d1": {k: _finish(v) for k, v in calib.items()},
    }


MIN_N = 20  # 이 밑이면 "판단 불가" — 루트 CLAUDE.md·소유자 지시.


def build_scorecard(claims: list[dict], calendar_by_market: dict[str, list[str]],
                    index_lookup: dict[tuple[str, str], float],
                    price_lookup: dict[tuple[str, str, str], tuple[float | None, float | None]],
                    as_of: str) -> dict:
    """평가 원장(`report_accuracy.jsonl`) 한 행 + 스코어카드 본문."""
    direction = score_direction_claims(claims, calendar_by_market, index_lookup)
    candidates = score_candidate_claims(claims, calendar_by_market, price_lookup)
    return {
        "schema": SCHEMA,
        "as_of": as_of,
        "n_claims": len(claims),
        "direction": direction,
        "candidates": candidates,
        "min_n": MIN_N,
    }


def render_markdown(scorecard: dict) -> str:
    """스코어카드 → Markdown. n<MIN_N 인 행은 "판단 불가"로 표시한다."""
    min_n = scorecard.get("min_n", MIN_N)
    lines = [f"# 리포트 정확도 스코어카드 ({scorecard.get('as_of')})", ""]
    lines.append(f"청구(claims) {scorecard.get('n_claims')}건 채점")
    lines.append("")
    lines.append("## 시장 방향콜")
    lines.append("| 지평 | n | 적중률 | 95% CI |")
    lines.append("|---|---|---|---|")
    for h, st in (scorecard.get("direction") or {}).items():
        n = st["n"]
        if n < min_n:
            lines.append(f"| D+{h} | {n} | 판단 불가 (n<{min_n}) | - |")
            continue
        rate = st["rate"]
        lines.append(f"| D+{h} | {n} | {rate:.1%} | [{st['ci_lo']:.1%}, {st['ci_hi']:.1%}] |")
    lines.append("")
    lines.append("## 종목 후보 — 지평별 전방수익률")
    lines.append("| 지평 | n | 평균(bp) | 적중률 |")
    lines.append("|---|---|---|---|")
    cand = scorecard.get("candidates") or {}
    for h, st in (cand.get("horizons") or {}).items():
        n = st["n"]
        if n < min_n:
            lines.append(f"| D+{h} | {n} | 판단 불가 (n<{min_n}) | - |")
            continue
        lines.append(f"| D+{h} | {n} | {st['mean_bps']:+.0f} | {st['hitrate']:.1%} |")
    lines.append("")
    lines.append("## 점수 IC (Spearman, score vs D+h bps)")
    for h in HORIZONS:
        n = (cand.get("ic_n") or {}).get(h, 0)
        ic = (cand.get("ic") or {}).get(h)
        if n < min_n or ic is None:
            lines.append(f"- D+{h}: 판단 불가 (n={n})")
        else:
            lines.append(f"- D+{h}: IC={ic:+.3f} (n={n})")
    lines.append("")
    lines.append("## 태그(origin)별 성과, D+1")
    lines.append("| origin | n | 평균(bp) | 적중률 |")
    lines.append("|---|---|---|---|")
    for tag, hs in (cand.get("by_tag") or {}).items():
        st = hs.get(1) or {"n": 0}
        n = st["n"]
        if n < min_n:
            lines.append(f"| {tag} | {n} | 판단 불가 (n<{min_n}) | - |")
        else:
            lines.append(f"| {tag} | {n} | {st['mean_bps']:+.0f} | {st['hitrate']:.1%} |")
    return "\n".join(lines) + "\n"


# ── 리포트 노출용 압축 요약 (2026-09-06, 소유자 지시 priority-1 §2) ─────────
#
# 아래 함수는 데이터를 새로 채점하지 않는다 — `report_accuracy.jsonl` 마지막
# 행(`build_scorecard` 출력, 호출부가 읽어 넘긴다)을 리포트 HTML의 "리포트
# 정확도" 박스 + 방향콜 라벨 옆 한 줄 + 텔레그램 발행 요약 한 줄이 공유하는
# 압축 형태로 접는다. 파일 I/O는 여전히 호출부(`quant/report/collect/core.py`)
# 몫이다(모듈 상단 규약 유지).

# 방향콜 라벨 옆 한 줄에 쓰는 지평 — 소유자가 예시로 든 D+3("최근 n=27, D+3").
STANCE_HORIZON = 3


def _horizon_get(d: dict, h: int):
    """지평 키 조회 — int(라이브 파이썬 dict)/str(jsonl 라운드트립 후) 둘 다 받는다."""
    return d.get(h, d.get(str(h)))


def report_summary(latest: dict | None) -> dict:
    """`latest`(`report_accuracy.jsonl` 마지막 행) → 리포트 노출용 압축 요약.

    `latest`가 없으면(원장이 아직 한 번도 안 돔) 전부 "measured": False 로
    채운 dict를 낸다 — 빈 dict가 아니라, 그래야 호출부/템플릿이 존재 유무를
    매번 분기하지 않고 `stance_line`/`telegram_line`이 항상 "정확도 미측정"
    문자열을 낸다.

    표본이 `min_n`(원장에 기록된 값, 없으면 `MIN_N`) 미만인 지평은 지어내지
    않는다 — "measured": False 로만 남긴다(소유자 지시: 방향콜을 여러 날짜에
    걸친 확신으로 포장하지 않는다).

    **"같은 유니버스 무작위 대조군"(후보 D+0 시가→종가 vs 유니버스 전체)은
    여기 없다** — `report_claims.jsonl`이 후보만 남기고 비후보 유니버스를
    기록하지 않아 이 원장만으로는 계산할 수 없다(모듈 상단 "무엇을 채점하지
    않는가" 참고). 필요하면 감사처럼 `selections.jsonl`을 직접 읽는 별도
    스크립트로 잰다 — 이 요약은 원장에 실제로 있는 것만 낸다.
    """
    if not latest:
        empty = {h: {"n": 0, "measured": False} for h in HORIZONS}
        return {
            "measured": False, "as_of": None, "n_claims": 0, "min_n": MIN_N,
            "stance_line": "정확도 미측정", "telegram_line": "정확도 미측정",
            "direction": dict(empty), "candidates": {h: dict(v) for h, v in empty.items()},
            "ic": {h: dict(v) for h, v in empty.items()},
        }

    min_n = latest.get("min_n", MIN_N)
    direction_raw = latest.get("direction") or {}
    cand_raw = latest.get("candidates") or {}
    horizons_raw = cand_raw.get("horizons") or {}
    ic_raw = cand_raw.get("ic") or {}
    ic_n_raw = cand_raw.get("ic_n") or {}

    direction: dict[int, dict] = {}
    for h in HORIZONS:
        d = _horizon_get(direction_raw, h) or {}
        n = d.get("n") or 0
        rate = d.get("rate")
        if n >= min_n and rate is not None:
            direction[h] = {"n": n, "measured": True, "rate": rate,
                            "ci_lo": d.get("ci_lo"), "ci_hi": d.get("ci_hi")}
        else:
            direction[h] = {"n": n, "measured": False}

    candidates: dict[int, dict] = {}
    for h in HORIZONS:
        c = _horizon_get(horizons_raw, h) or {}
        n = c.get("n") or 0
        mean_bps = c.get("mean_bps")
        if n >= min_n and mean_bps is not None:
            candidates[h] = {"n": n, "measured": True, "mean_bps": mean_bps,
                             "hitrate": c.get("hitrate")}
        else:
            candidates[h] = {"n": n, "measured": False}

    ic: dict[int, dict] = {}
    for h in HORIZONS:
        n = _horizon_get(ic_n_raw, h) or 0
        val = _horizon_get(ic_raw, h)
        if n >= min_n and val is not None:
            ic[h] = {"n": n, "measured": True, "value": val}
        else:
            ic[h] = {"n": n, "measured": False}

    stance_d = direction.get(STANCE_HORIZON) or {}
    if stance_d.get("measured"):
        stance_line = (
            f"방향 판정 정확도(최근 n={stance_d['n']}, D+{STANCE_HORIZON}): "
            f"{stance_d['rate']:.0%} — 참고용"
        )
    else:
        stance_line = "정확도 미측정"

    tg_parts = [
        f"D+{h} {direction[h]['rate']:.0%}(n={direction[h]['n']})"
        for h in HORIZONS if direction[h]["measured"]
    ]
    telegram_line = ("리포트 정확도 방향 " + " · ".join(tg_parts)) if tg_parts else "정확도 미측정"

    return {
        "measured": any(direction[h]["measured"] for h in HORIZONS),
        "as_of": latest.get("as_of"),
        "n_claims": latest.get("n_claims"),
        "min_n": min_n,
        "stance_line": stance_line,
        "telegram_line": telegram_line,
        "direction": direction,
        "candidates": candidates,
        "ic": ic,
    }
