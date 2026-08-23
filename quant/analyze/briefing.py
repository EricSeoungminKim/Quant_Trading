"""'오늘 확인할 것' 도출 — 규칙 기반, LLM 없음.

리포트의 맨 앞 칸이다. 지표를 나열하는 대신 **데이터가 직접 말하는 것**만 올린다.
Phase 1에서 근거 없는 서술을 흉내내면 그게 곧 짜깁기가 되므로, 여기 들어가는
문장은 전부 스냅샷의 특정 숫자에서 기계적으로 유도된다. 해석과 코칭 서술은
Phase 2에서 LLM이 이 위에 얹는다.

각 항목은 `level`을 갖는다:
  watch  — 확인 필요 (기본)
  risk   — 방향성 베팅을 줄일 근거
  signal — 종목/수급에서 나온 적극적 단서
"""
from __future__ import annotations

from quant.collect.contracts import Snapshot
from quant.core.terms import event_full, event_term
from quant.analyze.scoring import label_100, to_100


def josa(word: str, with_batchim: str, without: str) -> str:
    """앞 단어의 받침 유무로 조사를 고른다.

    'CPI 오늘' + '가' → '오늘가' 처럼 깨지는 걸 막는다. 한글이 아니면
    (영문 약어·숫자로 끝나면) 받침 없음으로 취급한다 — 'D-8이'보다
    'D-8가'가 자연스럽다.
    """
    if not word:
        return without
    last = word.strip()[-1]
    if not ("가" <= last <= "힣"):
        return without
    return with_batchim if (ord(last) - 0xAC00) % 28 else without

VIX_CALM = 15.0
VIX_STRESS = 22.0
FLOW_BIG = 3000  # 억원. KR 수급에서 '큰 손' 판단 임계
EVENT_NEAR_DAYS = 2


INDEX_STRONG_PCT = 1.0
ANCHOR_STRONG_PCT = 3.0
# 가점 가능한 요인 5개(지수 모멘텀, 외국인, 기관계, VIX, 앵커)의 고정 분모.
STANCE_SPAN = 5

_MAIN_INDEX = {"KR": "^KS11", "US": "^GSPC"}


INDEX_DAILY_LIMIT_PCT = 10.0
_INDEX_SYMBOLS = ("^KS11", "^KQ11", "^GSPC", "^IXIC", "^DJI", "^N225")


def implausible_moves(quotes: dict) -> list[str]:
    """지수 시계열에서 물리적으로 어려운 일간 변동을 찾아낸다.

    지수는 개별 종목과 달리 하루 10%를 넘기기 어렵다. 그런 값이 보이면 원천
    데이터를 의심해야 한다 — 실측(2026-08-12)에서 yfinance 의 ^KS11 이
    2026-07-31 에 +17.91%를 보고했고, 재조회해도 동일했다. **우리 쪽 버그가
    아니라 원천 값이 그렇다.**

    조용히 믿지도, 조용히 고치지도 않는다. 값을 손대면 그게 시장 데이터 조작이고,
    묻어두면 파생 지표(이동평균·변동성)가 오염된 채 판단에 들어간다. 드러낸다.

    KOSPI 는 FRED 에 대응 시리즈가 없어 2차 소스 교차검증 사각지대다 — 이 가드가
    사실상 유일한 방어선이다.
    """
    out: list[str] = []
    for sym in _INDEX_SYMBOLS:
        q = quotes.get(sym)
        hist = (q or {}).get("history") or []
        if len(hist) < 2:
            continue
        worst_pct, worst_i = 0.0, 0
        for i in range(1, len(hist)):
            prev = hist[i - 1]
            if not prev:
                continue
            pct = (hist[i] / prev - 1) * 100
            if abs(pct) > abs(worst_pct):
                worst_pct, worst_i = pct, i
        if abs(worst_pct) > INDEX_DAILY_LIMIT_PCT:
            out.append(
                f"{q.get('label', sym)} 과거 시계열에 {worst_pct:+.1f}% 일간 변동 "
                f"({worst_i + 1}/{len(hist)}번째 관측) — 원천 데이터 의심, "
                f"이동평균·변동성 지표를 그대로 신뢰하지 않는다"
            )
    return out


def _data(snap: Snapshot, key: str) -> dict | None:
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def stance(snap: Snapshot, cont: dict[str, dict], delta: dict) -> dict:
    """오늘의 스탠스 한 줄 — 회사 리포트의 executive summary 자리.

    **지어내지 않는다.** 각 요인은 스냅샷의 특정 숫자에서 나오고, 문장은 그
    요인들을 잇기만 한다. 점수와 요인 목록을 함께 남기므로 나중에 "그날 스탠스가
    맞았나"를 채점할 수 있다 — 산문만 남기면 채점이 불가능하다.
    """
    plus: list[str] = []
    minus: list[str] = []
    score = 0

    quotes = (_data(snap, "market") or {}).get("quotes", {})
    idx_sym = _MAIN_INDEX.get(snap.market)
    idx = quotes.get(idx_sym) if idx_sym else None
    if idx and idx.get("change_pct") is not None:
        pct = idx["change_pct"]
        if pct >= INDEX_STRONG_PCT:
            score += 1
            plus.append(f"{idx['label']} {pct:+.2f}%")
        elif pct <= -INDEX_STRONG_PCT:
            score -= 1
            minus.append(f"{idx['label']} {pct:+.2f}%")

    flow = _data(snap, "kospi_flow")
    if flow and flow.get("rows"):
        row = flow["rows"][0]
        for actor in ("외국인", "기관계"):
            v = row.get(actor)
            if v is None or abs(v) < FLOW_BIG:
                continue
            if v > 0:
                score += 1
                plus.append(f"{actor} {v:+,}억 순매수")
            else:
                score -= 1
                minus.append(f"{actor} {v:+,}억 순매도")

    vix = (quotes.get("^VIX") or {}).get("close")
    if vix is not None:
        if vix <= VIX_CALM:
            score += 1
            plus.append(f"VIX {vix:.1f}")
        elif vix >= VIX_STRESS:
            score -= 1
            minus.append(f"VIX {vix:.1f}")

    anchors = (_data(snap, "market") or {}).get("anchors", {})
    pcts = [a["change_pct"] for a in anchors.values() if a.get("change_pct") is not None]
    if pcts:
        avg = sum(pcts) / len(pcts)
        if avg >= ANCHOR_STRONG_PCT:
            score += 1
            names = "·".join(a["label"] for a in anchors.values())
            plus.append(f"{names} 평균 {avg:+.1f}%")
        elif avg <= -ANCHOR_STRONG_PCT:
            score -= 1
            minus.append(f"반도체 평균 {avg:+.1f}%")

    # 임박한 고영향 이벤트는 방향과 무관하게 관망 압력이다
    cal = _data(snap, "calendar") or {}
    imminent = [
        e for e in cal.get("events", [])
        if e.get("high_impact") and e.get("days_ahead", 99) <= 1
    ]
    if imminent:
        score -= 1
        minus.append(f"{event_term(imminent[0]['name'])} {imminent[0]['dday']}")

    score100 = to_100(score, STANCE_SPAN)
    label = label_100(score100, "상승 신호", "하락 신호")
    if imminent:
        # 이벤트 이름은 바로 앞 절에 이미 나온다 — 반복하면 문장이 늘어진다
        action = "발표를 확인한 뒤 진입한다."
    elif score100 >= 75:
        action = "추세 추종에 우호적 — 돌파 진입이 유효하다."
    elif score100 >= 60:
        action = "상승 쪽이 우세하나 근거가 얇다 — 분할 진입으로 대응한다."
    elif score100 <= 25:
        action = "신규 진입을 접고 보유 비중을 줄인다."
    elif score100 <= 40:
        action = "신규 진입을 줄이고 손절폭을 넓게 잡는다."
    else:
        action = "지수 방향에 베팅하기보다 개별 종목 근거를 우선한다."

    head = ", ".join(plus[:3]) if plus else ""
    tail = ", ".join(minus[:2]) if minus else ""
    if head and tail:
        line = f"{head}로 우호적이나 {tail}{josa(tail, '이', '가')} 걸린다 — {action}"
    elif head:
        line = f"{head} — {action}"
    elif tail:
        line = f"{tail} — {action}"
    else:
        line = f"임계를 넘긴 요인이 없다 — {action}"

    return {
        "label": label,
        "score": score,
        "score100": score100,
        "line": line,
        "positives": plus,
        "negatives": minus,
    }


def stance_macro_context(snap: Snapshot) -> dict:
    """`stance()` 헤드라인 근거 밀도 보강용 매크로 맥락(P0, 2026-08-19).

    `stance()`의 5요인(지수 모멘텀·외국인/기관 수급·VIX·앵커·임박 이벤트)은
    실측상 그날 임계를 넘는 요인이 1~2개뿐인 날이 잦아 헤드라인 근거가
    얇다("기관 순매도, 내일 지표"뿐). 이미 수집 중인 FRED 금리(10년-2년
    스프레드)·NFCI·순유동성·유가(WTI/브렌트)·VIX 기간구조를 그대로 붙여
    AI 서술(`_build_stance_prose`)의 근거를 넓힌다 — **새 수집기 없음**,
    스냅샷에 이미 있는 `macro`/`vix_term`/`market` 소스만 읽는 순수 함수다.

    값이 없는 항목은 키 자체를 만들지 않는다(다른 `_data`류 헬퍼와 같은
    결측 계약 — 0/None 위장 금지).
    """
    out: dict = {}

    macro = _data(snap, "macro")
    if macro:
        if macro.get("curve"):
            out["curve"] = macro["curve"]
        if macro.get("nfci_label"):
            out["nfci_label"] = macro["nfci_label"]
        nl = macro.get("net_liquidity")
        if nl and nl.get("change") is not None:
            out["net_liquidity_change"] = nl["change"]

    vix_term = _data(snap, "vix_term")
    if vix_term and vix_term.get("structure"):
        out["vix_structure"] = vix_term["structure"]
        if vix_term.get("spread") is not None:
            out["vix_spread"] = vix_term["spread"]

    quotes = (_data(snap, "market") or {}).get("quotes", {})
    oil: dict[str, float] = {}
    for sym, label in (("CL=F", "WTI"), ("BZ=F", "브렌트")):
        q = quotes.get(sym)
        if q and q.get("change_pct") is not None:
            oil[label] = q["change_pct"]
    if oil:
        out["oil"] = oil

    return out


def build(snap: Snapshot, cont: dict[str, dict], delta: dict) -> list[dict]:
    """(level, text, detail) 목록. 위에서부터 중요도 순."""
    out: list[dict] = []

    # --- 임박한 고영향 이벤트: 방향성 베팅을 줄일 가장 명확한 근거 ---
    cal = _data(snap, "calendar")
    if cal:
        near = [
            e for e in cal.get("events", [])
            if e.get("high_impact") and e.get("days_ahead", 99) <= EVENT_NEAR_DAYS
        ]
        # 같은 사유를 여러 줄로 반복하면 읽는 사람이 그 칸을 통째로 건너뛴다.
        # 한 항목으로 합치고 이벤트는 나열한다.
        if near:
            names = ", ".join(f"{event_full(e['name'])} {e['dday']}" for e in near[:4])
            out.append({
                "level": "risk",
                "text": f"주요 지표 {len(near)}건 발표 임박",
                "detail": f"{names} — 발표 전 방향성 베팅은 근거가 약해진다.",
            })

    # --- 변동성 국면 ---
    q = _data(snap, "market")
    quotes = (q or {}).get("quotes", {})
    vix = quotes.get("^VIX", {}).get("close")
    if vix is not None:
        if vix >= VIX_STRESS:
            out.append({
                "level": "risk",
                "text": f"VIX {vix:.1f} — 스트레스 구간",
                "detail": f"{VIX_STRESS} 이상. 갭 위험이 커 단타 손절폭을 넓게 잡아야 한다.",
            })
        elif vix <= VIX_CALM:
            out.append({
                "level": "watch",
                "text": f"VIX {vix:.1f} — 안정 구간",
                "detail": f"{VIX_CALM} 이하. 추세 추종이 상대적으로 유리한 환경.",
            })

    # --- 수급 반전: 방향이 바뀐 주체는 그 자체가 사건이다 ---
    for actor, f in (delta.get("flow") or {}).items():
        if f.get("flipped"):
            direction = "순매수 전환" if f["current"] > 0 else "순매도 전환"
            out.append({
                "level": "signal",
                "text": f"{actor} {direction}",
                "detail": f"직전 {f['previous']:+,}억 → 이번 {f['current']:+,}억.",
            })

    flow = _data(snap, "kospi_flow")
    if flow and flow.get("rows"):
        row = flow["rows"][0]
        for actor in ("외국인", "기관계"):
            v = row.get(actor)
            if v is not None and abs(v) >= FLOW_BIG:
                side = "순매수" if v > 0 else "순매도"
                out.append({
                    "level": "signal",
                    "text": f"{actor} 대규모 {side} {v:+,}억",
                    "detail": f"{FLOW_BIG:,}억 임계 초과 ({row['date']} 기준).",
                })

    # --- 뉴스 노출 집중: 단타 후보의 출발점 ---
    if cont:
        top = sorted(
            cont.items(),
            key=lambda kv: (-kv[1]["today_articles"], -kv[1]["streak_days"]),
        )
        sym, c = top[0]
        out.append({
            "level": "signal",
            "text": f"{c['name']} 뉴스 최다 노출 {c['today_articles']}건",
            "detail": f"최근 {c['days']}일 중 등장, 연속 {c['streak_days']}일."
                      + (" 원장 최초 등장." if c["is_new"] else ""),
        })
        streaks = [(s, c2) for s, c2 in cont.items() if c2["streak_days"] >= 3]
        for s, c2 in sorted(streaks, key=lambda kv: -kv[1]["streak_days"])[:2]:
            out.append({
                "level": "signal",
                "text": f"{c2['name']} {c2['streak_days']}일 연속 노출",
                "detail": "근거가 누적되는 중 — 호재/악재 판정은 아직 붙지 않았다.",
            })

    # --- 데이터 신뢰도: 숨기면 안 되는 것들 ---
    warns = (q or {}).get("crosscheck", {}).get("warnings", [])
    for w in warns[:2]:
        out.append({
            "level": "risk",
            "text": "시세 2차 소스 괴리",
            "detail": w + " — 이 값에 기반한 판단은 보류한다.",
        })

    odd = implausible_moves(quotes)
    if odd:
        out.append({
            "level": "risk",
            "text": f"시계열 이상치 {len(odd)}건",
            "detail": " / ".join(odd[:2]),
        })

    if snap.missing():
        out.append({
            "level": "risk",
            "text": f"데이터 {len(snap.missing())}건 결측",
            "detail": "결측: " + ", ".join(snap.missing()),
        })

    if not out:
        out.append({
            "level": "watch",
            "text": "특이 신호 없음",
            "detail": "임계를 넘긴 항목이 없다. 평상 국면으로 본다.",
        })
    return out
