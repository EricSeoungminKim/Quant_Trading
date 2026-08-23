"""자체 리포트(market_report) 엔진 페이로드 → 아침 브리핑 + 자동 편입 후보.

**왜 새로 만드나.** 지금까지 개장 전 브리핑의 입력은 회사 리포트였다 —
평문 HTTP로 받은 남의 산문을 Claude 세션에 통째로 넣고, 그 출력에서
`AUTO_WATCH:` 줄을 긁어 관심종목에 반영했다(daily_brief.sh). 회사 자산이라
의존을 끊어야 하고(2026-08-12 사용자 지시), 그 구조는 **프롬프트 주입이
자동 편입 경로에 닿는다**는 근본 약점이 있었다 — 세션의 도구를 전부 끄고
시크릿을 env에서 뺀 것도 결국 그 약점을 감수한 완화책이었다.

자체 리포트는 그 전제를 통째로 바꾼다:
- 후보 선정이 **이미 결정론적**이다. `AUTO_WATCH:` 줄은 리포트 생성 단계에서
  뉴스 건수·연속일·랭킹 편입 여부로 계산돼 나온다 — 재현 가능하고 감사 가능하다.
- 입력이 **같은 박스의 우리 파일**이다. 평문 HTTP로 받은 제3자 문서가 아니다.
- 따라서 **브리핑 경로에서 LLM이 통째로 빠진다.** 주입면이 사라지고, 600초
  타임아웃과 세션 실패 모드도 함께 사라진다(ADR-0002의 "LLM은 리포팅
  레이어에만"을 어기지 않는다 — 아예 안 쓰는 쪽이다).

**설계 원칙:**
- 이 모듈은 **순수 함수만** 둔다. 파일·네트워크·시각을 모르게 해서 테스트가
  실제 페이로드 dict 하나로 끝나게 한다. I/O는 호출자(server/scripts/)의 몫이다.
- 리포트 스키마 변화에 죽지 않는다. 키는 전부 `.get()`으로 읽고, 없으면 그 줄을
  빼되 **없다는 사실을 감추지 않는다** — 결측은 결측이라고 쓴다.
- 신선도는 호출자가 아니라 여기서 판정한다(`is_fresh`). 어제 리포트로 오늘
  종목을 편입하는 것이 가장 조용하고 비싼 실패 모드다.
- 화이트리스트·상한은 **여기서도** 건다. watch-score 게이트와 셸 캡이 뒤에 또
  있지만, 캡을 한 곳에만 두면 그 한 곳이 뚫렸을 때 막을 것이 없다.
"""
from __future__ import annotations

import re

from quant.analyze import foreign_trend

# 토큰 형식: SYMBOL[:TAG[+TAG...]] — 리포트가 `005930:NEWS+RANK+NEW` 형태로 낸다.
# daily_brief.sh의 셸 화이트리스트와 같은 문법을 쓴다(날짜 접미사는 자체 리포트가
# 내지 않으므로 받지 않는다 — 받을 이유 없는 형식을 열어두지 않는다).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9.]{1,10}(:[A-Za-z+]{1,20})?$")

# 시장별 심볼 형태. 리포트는 시장별로 따로 만들어지지만, KR 리포트에서 US 티커가
# 나오면 그것은 리포트의 시장 필터가 깨진 것이다 — 조용히 편입하지 않고 버린다.
_SYMBOL_SHAPE = {
    "KR": re.compile(r"^[0-9]{6}$"),
    "US": re.compile(r"^[A-Za-z][A-Za-z.]{0,5}$"),
}

# 셸 하드캡과 같은 값(daily_brief.sh=15, us_watch_discover.sh=5). 상한은 기존
# 운영값을 그대로 승계한다 — 리스크 한도를 요청 없이 바꾸지 않는다.
MAX_CANDIDATES = {"KR": 15, "US": 5}

# --- 리포트 어휘 → 엔진 어휘 번역 -------------------------------------------
# **두 저장소가 다른 말을 쓴다.** 리포트의 태그(NEWS/RANK/STREAK/NEW)는 "왜 이
# 종목이 후보인가"를 사람에게 보여주는 **증거 플래그**다. 엔진의 태그
# (TREND/REBOUND/EVENT — watch_scorer._VALID_TAGS)는 "어느 채점 프로필로 볼
# 것인가"를 고르는 **선택자**이고, news_momentum 전략은 그중 EVENT만 진입
# 대상으로 삼는다.
#
# 번역하지 않으면 무슨 일이 나는가(2026-08-13 실측): watch_scorer._parse_token은
# 모르는 태그가 하나라도 섞이면 **전체를 무태그로 강등**한다. 그래서
# `005930:NEWS+RANK+NEW`는 태그 없이 등록되고, tags_of에 EVENT가 없으니
# **news_momentum이 리포트 종목을 영영 진입시키지 못한다** — 에러도, 경고도 없이.
#
# 번역을 리포트 쪽이 아니라 여기(경계)에 두는 이유: 리포트의 증거 어휘는 리포트
# 화면에서 그 자체로 의미가 있고, 엔진의 프로필 어휘는 엔진 사정이다. 한쪽이
# 다른 쪽 어휘를 쓰기 시작하면 둘 다 망가진다.
_TAG_MAP = {
    "NEWS": "EVENT",   # 뉴스 다발 = 신선한 촉매 — news_momentum이 찾는 바로 그것
    "RANK": "TREND",   # 랭킹(거래대금/상승률) 편입 = watch_scorer의 --discover-*와 같은 축
    # STREAK/NEW/ANCHOR 는 증거이지 채점 프로필이 아니다 — 매핑하지 않는다.
    # REBOUND 는 리포트에 대응 개념이 없다.
}
# _VALID_TAGS 순서를 따른다 — 토큰이 결정론적이어야 로그 비교가 가능하다.
_ENGINE_TAG_ORDER = ("TREND", "REBOUND", "EVENT")

_MARKET_LABEL = {"KR": "🇰🇷 한국장", "US": "🇺🇸 미국장"}

# 텔레그램 4096자 제한. 호출자가 다른 문구를 덧붙일 여지를 남긴다.
MAX_BRIEF_CHARS = 3500


def is_fresh(payload: dict, today: str) -> bool:
    """리포트가 오늘 세션 것인가. `today`는 KST 기준 ISO 날짜(YYYY-MM-DD).

    어제 리포트로 오늘 종목을 편입하면 아무 에러도 없이 틀린 종목이 들어간다 —
    가장 조용한 실패 모드라 명시적으로 막는다.
    """
    return str(payload.get("session_date") or "") == today


def _evidence_rank(payload: dict) -> dict[str, tuple]:
    """심볼 → 정렬 키. 큰 값이 강한 근거다.

    상한에 걸려 잘라내야 할 때 **무엇을 남길지** 정하는 데 쓴다. 리포트가 내는
    `AUTO_WATCH` 순서는 근거 순이 아니라서(실측 2026-08-13: US 11개 중 랭킹에
    실제로 오른 INTC·SMCI가 랭킹 없는 AAPL·AMZN·BA에 밀려 잘렸다), 순서대로
    자르면 **가장 근거 좋은 종목을 버리게 된다.**

    축은 트렌딩 점수(토스 보드 순위가 반영된 값) → 뉴스 건수 → 심볼 순.
    마지막 축은 동점일 때 결정론을 보장하기 위한 것이다.
    """
    out: dict[str, tuple] = {}
    for s in payload.get("symbols") or []:
        sym = str(s.get("symbol") or "")
        if not sym:
            continue
        trending = s.get("trending_score100")
        out[sym] = (
            float(trending) if trending is not None else -1.0,
            float(s.get("news_articles_today") or 0),
        )
    return out


def auto_watch_tokens(payload: dict, market: str) -> list[str]:
    """`AUTO_WATCH:` 줄에서 유효 토큰만 뽑는다 — 형식·시장·상한 3중 검증.

    리포트가 우리 것이라도 검증은 한다: 리포트 생성기의 버그(시장 필터 붕괴,
    심볼 추출 오작동)가 관심종목 파일에 그대로 흘러드는 경로를 막는 마지막 지점이
    여기다. 통과하지 못한 토큰은 조용히 사라지지 않고 `rejected_tokens`로 셀 수 있다.

    상한을 넘으면 **근거가 약한 쪽부터** 버린다(`_evidence_rank`) — 리포트 순서대로
    자르면 트렌딩이 확인된 종목이 먼저 잘려나간다.
    """
    line = str(payload.get("auto_watch") or "")
    body = line.split(":", 1)[1] if line.startswith("AUTO_WATCH:") else ""
    shape = _SYMBOL_SHAPE.get(market)
    cap = MAX_CANDIDATES.get(market, 5)

    valid: list[str] = []
    seen: set[str] = set()
    for tok in body.split():
        if not _TOKEN_RE.match(tok):
            continue
        sym = tok.split(":", 1)[0]
        if shape is not None and not shape.match(sym):
            continue
        if sym in seen:
            continue
        seen.add(sym)
        valid.append(tok)

    if len(valid) <= cap:
        return valid

    rank = _evidence_rank(payload)

    def _key(tok: str) -> tuple:
        sym = tok.split(":", 1)[0]
        trending, articles = rank.get(sym, (-1.0, 0.0))
        return (-trending, -articles, sym)  # 동점은 심볼 오름차순 — 결정론 보장

    kept = set(sorted(valid, key=_key)[:cap])
    # 남길 것을 근거로 고르되, **출력 순서는 리포트 순서를 유지**한다 — 사람이
    # 리포트와 브리핑을 나란히 볼 때 순서가 달라지면 대조가 어렵다.
    return [t for t in valid if t in kept]


def rejected_tokens(payload: dict, market: str) -> list[str]:
    """검증에서 떨어진 토큰. 상한 초과분은 포함하지 않는다(탈락이 아니라 절단)."""
    line = str(payload.get("auto_watch") or "")
    body = line.split(":", 1)[1] if line.startswith("AUTO_WATCH:") else ""
    shape = _SYMBOL_SHAPE.get(market)
    bad: list[str] = []
    for tok in body.split():
        sym = tok.split(":", 1)[0]
        if not _TOKEN_RE.match(tok) or (shape is not None and not shape.match(sym)):
            bad.append(tok)
    return bad


def engine_tokens(payload: dict, market: str) -> list[str]:
    """watch-score에 넘길 토큰 — 리포트 어휘를 엔진 어휘로 번역한 형태.

    출력 형식은 `SYMBOL[:TAGS[:YYYYMMDD]]` (watch_scorer._parse_token 계약).
    날짜 접미사는 **EVENT일 때만** 붙인다: EVENT 프로필의 "뉴스 신선도" 항목이
    100점 중 30점을 차지하는데, 날짜가 없으면 무조건 0점이라 뉴스 종목이
    구조적으로 손해를 본다(_event_score 참고). 리포트의 session_date가 곧 그
    뉴스가 집계된 날이므로 이 값이 정확히 맞다. TREND 프로필은 날짜를 안 보므로
    붙이지 않는다 — 의미 없는 필드를 실어보내지 않는다.

    매핑되는 태그가 하나도 없으면 무태그로 낸다 → watch_scorer가 세 프로필
    best-of로 채점한다(기존 무태그 동작 그대로).
    """
    session_date = str(payload.get("session_date") or "")
    yyyymmdd = session_date.replace("-", "") if len(session_date) == 10 else ""

    out: list[str] = []
    for tok in auto_watch_tokens(payload, market):
        sym, _, raw = tok.partition(":")
        mapped = {_TAG_MAP[t] for t in raw.split("+") if t in _TAG_MAP}
        tags = [t for t in _ENGINE_TAG_ORDER if t in mapped]
        if not tags:
            out.append(sym)
            continue
        token = f"{sym}:{'+'.join(tags)}"
        if "EVENT" in tags and yyyymmdd:
            token += f":{yyyymmdd}"
        out.append(token)
    return out


# --- 서브프로젝트 T (2026-08-17) — news_scalp(EVENT_SCALP)·frgn_accumulate(FRGN/
# FRGN_EXIT) 태그 배선. **실제 경로 추적 결과**(추측 아님, quant/apps/report_cli.py
# 코드 확인): 당일 단타 후보(intraday_scorer v4, `_build_intraday_view`)는
# **close_engine.json(오후판)에만** `"intraday_view"` 키로 실린다 — 아침
# `{MARKET}_engine.json`의 payload에는 write_machine 직전까지 이 값이 조립돼도
# 최종 dict에 추가되지 않는다(HTML 전용). 외국인 수급 라벨(`foreign_trend.classify`
# 의 "매수 시그널(재유입)"/"이탈 추세")은 **아침/오후 어느 engine.json에도** 실리지
# 않는다 — `_build_foreign_view`의 결과(`foreign_view`)는 HTML 렌더·Exec Summary
# 입력으로만 쓰이고 write_machine에 넘기는 payload에 병합되지 않는다(오후판은
# foreign_view 자체를 계산하지도 않는다, foreign_view=None으로 호출).
#
# 그래서 아래 두 함수는 engine.json 딕셔너리가 아니라 **원장을 직접** 본다:
# - `intraday_scalp_tokens`는 close payload의 `intraday_view`(있으면)를 읽는다 —
#   아침 payload는 이 키가 없으므로 자연히 빈 리스트(세션 무관 안전 호출).
# - 외국인 라벨은 `data/ledger/frgn_flow.jsonl`을 호출부(brief_from_report.py)가
#   직접 읽어 `foreign_trend.classify()`(순수 함수, report_cli.py가 그 라벨을 만들
#   때 쓰는 바로 그 함수)를 재적용한 결과를 `foreign_flow_tokens`에 넘긴다 — report_cli.py
#   를 건드리지 않고도(다른 워커 작업 중) 같은 판정 로직을 재사용한다.


def intraday_scalp_tokens(payload: dict) -> list[str]:
    """close_engine.json의 `intraday_view`(당일 단타 후보 top-N, intraday_scorer v4)
    에서 EVENT_SCALP 토큰을 뽑는다. 이미 top-N으로 잘려 있으므로(rank_intraday) 여기서
    추가로 자르지 않는다. `intraday_view` 키가 없는 payload(아침판)는 빈 리스트."""
    out: list[str] = []
    for item in payload.get("intraday_view") or []:
        sym = item.get("symbol") if isinstance(item, dict) else None
        if sym:
            out.append(f"{sym}:EVENT_SCALP")
    return out


def foreign_flow_candidate_symbols(payload: dict, market: str) -> set[str]:
    """외국인 수급 라벨을 조회할 심볼 후보 — KR 전용, 이 리포트가 이미 근거 있다고
    본 종목만(AUTO_WATCH 후보 + 단타 후보) 본다. 시장 전체를 훑지 않는다 — 근거 없는
    종목까지 수급 라벨 대상으로 삼으면 candidates_line의 근거 원칙과 어긋난다."""
    if market != "KR":
        return set()
    syms = {t.split(":", 1)[0] for t in auto_watch_tokens(payload, market)}
    syms |= {
        item.get("symbol") for item in (payload.get("intraday_view") or [])
        if isinstance(item, dict) and item.get("symbol")
    }
    return syms


def foreign_flow_tokens(labels: dict[str, str]) -> tuple[list[str], list[str]]:
    """`labels`(symbol → `foreign_trend.classify()`의 `"label"`)를 FRGN/FRGN_EXIT로
    번역한다. 반환: (FRGN 토큰 목록 `SYMBOL:FRGN`, FRGN_EXIT 심볼 목록 — 태그 없이
    맨 심볼만. FRGN_EXIT는 watch-score를 타지 않고 이미 등록된 종목의 태그만
    갱신하므로(own_brief.sh) 토큰 형식이 필요 없다).

    매수 시그널(`LABEL_INFLOW`)만 FRGN — 관망 라벨들은 근거가 얇아(모듈 docstring
    "관망" 절) 신규/갱신 어느 쪽도 트리거하지 않는다. 이탈 추세(`LABEL_OUTFLOW_TREND`)
    만 FRGN_EXIT — 단발 이탈(`LABEL_WATCH_RESIDUAL`)은 "잔여 자본이 남아 있다"는
    관망 신호일 뿐 청산 신호가 아니다(foreign_trend.py 모듈 docstring)."""
    frgn: list[str] = []
    frgn_exit: list[str] = []
    for sym in sorted(labels):
        label = labels[sym]
        if label == foreign_trend.LABEL_INFLOW:
            frgn.append(f"{sym}:FRGN")
        elif label == foreign_trend.LABEL_OUTFLOW_TREND:
            frgn_exit.append(sym)
    return frgn, frgn_exit


def close_brief_text(payload: dict, market: str) -> str:
    """마감 포지션 리포트(close_engine.json) 전용 요약 — `brief_text()`와 스키마가
    다르다(auto_watch/symbols/stance가 없고 대신 intraday_view/market_flow/rankings/
    news가 있다, `report_cli._emit_close`의 close_payload 참고). 전량 재사용하지 않고
    별도 함수로 두는 이유는 그 차이 — `brief_text()`를 그대로 쓰면 "자동 편입 후보:
    없음"이 항상 찍혀 실제로는 EVENT_SCALP/FRGN 후보가 있는데 없다고 오해하게 된다."""
    label = _MARKET_LABEL.get(market, market)
    date = payload.get("session_date") or "?"
    lines = [f"{label} 마감 포지션 브리핑 · {date}"]

    intraday = payload.get("intraday_view") or []
    if intraday:
        lines.append(f"\n🎯 당일 단타 후보(EVENT_SCALP) {len(intraday)}종목:")
        for item in intraday[:8]:
            name = item.get("name") or item.get("symbol")
            lines.append(f"  {name}({item.get('symbol')}) {item.get('score100')}점/100점")
    else:
        lines.append("\n🎯 당일 단타 후보 없음")

    flow = payload.get("market_flow") or {}
    if flow:
        foreign = flow.get("foreign_net")
        inst = flow.get("institution_net")
        parts = [
            f"외국인 {float(foreign):+,.0f}억" if foreign is not None else None,
            f"기관 {float(inst):+,.0f}억" if inst is not None else None,
        ]
        parts = [p for p in parts if p]
        if parts:
            lines.append("\n📈 당일 잠정 수급(시장 전체) " + " · ".join(parts))

    missing = payload.get("missing") or []
    if missing:
        lines.append(f"\n🧩 결측 소스 {len(missing)}개: {', '.join(str(m) for m in missing)}")

    return "\n".join(lines)[:MAX_BRIEF_CHARS]


def truncated_count(payload: dict, market: str) -> int:
    """상한에 잘려 나간 유효 후보의 수.

    **조용한 절단은 "다 봤다"로 읽힌다.** US 하드캡은 5인데 리포트가 6개를 내는
    일이 실제로 있었다(2026-08-13 실측) — 잘린 사실이 안 보이면 사용자는 리포트가
    5개만 냈다고 오해한다. 캡을 올릴지 말지는 리스크 판단이라 사용자 몫이고,
    이 함수는 그 판단에 필요한 사실만 드러낸다.
    """
    line = str(payload.get("auto_watch") or "")
    body = line.split(":", 1)[1] if line.startswith("AUTO_WATCH:") else ""
    shape = _SYMBOL_SHAPE.get(market)
    valid: set[str] = set()
    for tok in body.split():
        sym = tok.split(":", 1)[0]
        if _TOKEN_RE.match(tok) and (shape is None or shape.match(sym)):
            valid.add(sym)
    return max(0, len(valid) - MAX_CANDIDATES.get(market, 5))


def _fmt_pct(v: object) -> str | None:
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return None


def _fmt_num(v: object, decimals: int = 1) -> str | None:
    try:
        return f"{float(v):,.{decimals}f}"
    except (TypeError, ValueError):
        return None


def _market_lines(features: dict, market: str) -> list[str]:
    """시장 지표 줄. **단위 없는 숫자는 쓰지 않는다**(2026-08-12 사용자 원칙)."""
    lines: list[str] = []
    if market == "KR":
        kospi = _fmt_pct(features.get("kospi_change_pct"))
        kosdaq = _fmt_pct(features.get("kosdaq_change_pct"))
        if kospi or kosdaq:
            parts = [p for p in (f"코스피 {kospi}" if kospi else None,
                                 f"코스닥 {kosdaq}" if kosdaq else None) if p]
            lines.append("· " + " · ".join(parts))
        foreign = features.get("foreign_net_100m_krw")
        inst = features.get("institution_net_100m_krw")
        flow = [
            f"외국인 {float(foreign):+,.0f}억" if foreign is not None else None,
            f"기관 {float(inst):+,.0f}억" if inst is not None else None,
        ]
        flow = [f for f in flow if f]
        if flow:
            lines.append("· 수급 " + " · ".join(flow))
    else:
        sp = _fmt_pct(features.get("sp500_change_pct"))
        if sp:
            lines.append(f"· S&P500 {sp}")
        liq = features.get("net_liquidity_musd")
        if liq is not None:
            # FRED는 백만 달러 단위 — 조 달러로 환산해 읽히게 한다.
            lines.append(f"· 순유동성 {float(liq) / 1_000_000:.2f}조 달러")

    vix = _fmt_num(features.get("vix"))
    fx = _fmt_num(features.get("usdkrw"), 0)
    tail = [f"VIX {vix}" if vix else None, f"원/달러 {fx}원" if fx else None]
    tail = [t for t in tail if t]
    if tail:
        lines.append("· " + " · ".join(tail))
    return lines


def _trending_evidence(s: dict) -> str | None:
    """트렌딩 근거 — **언급됐다**와 **실제로 거래가 몰린다**를 구분한다.

    뉴스에 나왔다는 것만으로는 오늘 시장이 그 종목을 보고 있다는 뜻이 아니다
    (2026-08-13 사용자 지시). 리포트가 토스 랭킹 보드에서의 순위를 실어주므로
    "거래대금 1위"처럼 **어느 보드에서 몇 위인지**를 그대로 보여준다.
    """
    boards = s.get("trending_boards") or {}
    if not boards:
        return None
    # 순위가 좋은 순으로 최대 2개 — 보드 이름이 길어 3개부터는 줄이 넘친다.
    top = sorted(boards.items(), key=lambda kv: kv[1])[:2]
    return " ".join(f"{name} {int(rank)}위" for name, rank in top)


def _symbol_line(s: dict) -> str:
    """종목 한 줄 — 근거를 숫자로 남긴다. 엔진이 왜 이 종목을 봤는지가 보여야 한다."""
    name = s.get("name") or s.get("symbol")
    bits: list[str] = [f"{name}({s.get('symbol')})"]
    score = s.get("ai_score100")
    if score is not None:
        bits.append(f"{int(score)}점/100점")
    trend = s.get("trending_score100")
    if trend is not None:
        bits.append(f"트렌딩 {int(trend)}점")
    ev: list[str] = []
    n_art = s.get("news_articles_today")
    if n_art:
        ev.append(f"뉴스 {int(n_art)}건")
    streak = s.get("news_streak_days")
    if streak and int(streak) >= 2:
        ev.append(f"{int(streak)}일 연속")
    boards = _trending_evidence(s)
    if boards:
        ev.append(boards)
    elif s.get("in_ranking"):
        ev.append("랭킹")
    rvol = s.get("relative_volume")
    if rvol is not None:
        ev.append(f"RVOL {float(rvol):.1f}배")
    chg = _fmt_pct(s.get("change_pct"))
    if chg:
        ev.append(chg)
    if ev:
        bits.append("· " + " ".join(ev))
    return "  " + " ".join(bits)


def brief_text(payload: dict, market: str, url: str | None = None,
               max_symbols: int = 6) -> str:
    """개장 전 텔레그램 브리핑 본문. 전부 리포트 페이로드에서 결정론적으로 나온다.

    "무엇이 오늘 걸리는가"를 위에, 근거 숫자를 아래에 둔다 — 스코어 한 줄만 보고
    닫아도 판단이 서게.
    """
    label = _MARKET_LABEL.get(market, market)
    date = payload.get("session_date") or "?"
    lines = [f"{label} 개장 전 브리핑 · {date}"]

    stance = payload.get("stance") or {}
    score100 = stance.get("score100")
    if score100 is not None:
        lines.append(f"\n📊 종합 {int(score100)}점/100점 — {stance.get('label') or '?'}")
    if stance.get("line"):
        lines.append(str(stance["line"]))

    pos = [str(p) for p in (stance.get("positives") or [])]
    neg = [str(n) for n in (stance.get("negatives") or [])]
    if pos:
        lines.append("✅ " + " · ".join(pos))
    if neg:
        lines.append("⚠️ " + " · ".join(neg))

    features = payload.get("features") or {}
    mkt = _market_lines(features, market)
    if mkt:
        lines.append("\n📈 시장")
        lines += mkt

    symbols = payload.get("symbols") or []
    if symbols:
        # 리포트가 이미 정렬해 내려주지만, 여기서 다시 점수순으로 세운다 —
        # 상위 몇 개만 자르는 이상 정렬을 남의 계약에 의존하지 않는다.
        ranked = sorted(
            symbols,
            key=lambda s: (s.get("ai_score100") if s.get("ai_score100") is not None else -1),
            reverse=True,
        )
        lines.append(f"\n🔎 주목 종목 {len(symbols)}개 중 상위 {min(max_symbols, len(ranked))}")
        lines += [_symbol_line(s) for s in ranked[:max_symbols]]

    div = payload.get("news_diversity") or {}
    if div.get("total_articles"):
        # 표본의 크기와 **출처 쏠림**을 같이 보여준다 — 한 매체가 대부분을 차지하면
        # "여러 곳에서 다뤘다"가 아니라 "한 곳이 여러 번 썼다"다.
        line = f"\n📰 뉴스 {int(div['total_articles'])}건 · 매체 {int(div.get('distinct_outlets') or 0)}곳"
        by_outlet = div.get("by_outlet") or {}
        if by_outlet:
            top_outlet, top_n = max(by_outlet.items(), key=lambda kv: kv[1])
            share = top_n / div["total_articles"] * 100
            line += f" (최다 {top_outlet} {int(top_n)}건 · {share:.0f}%)"
        lines.append(line)

    missing = payload.get("missing") or []
    if missing:
        # 결측을 숨기지 않는다 — 빠진 소스가 있으면 스코어의 신뢰도가 다르다.
        lines.append(f"\n🧩 결측 소스 {len(missing)}개: {', '.join(str(m) for m in missing)}")

    toks = auto_watch_tokens(payload, market)
    bad = rejected_tokens(payload, market)
    cut = truncated_count(payload, market)
    lines.append("\n🎯 자동 편입 후보: " + (" ".join(toks) if toks else "없음"))
    if bad:
        lines.append(f"   (형식/시장 불일치로 제외 {len(bad)}개: {' '.join(bad[:5])})")
    if cut:
        lines.append(f"   ⚠️ 상한 {MAX_CANDIDATES.get(market)}개에 걸려 {cut}개 잘림")
    if toks:
        # 번역 결과를 눈에 보이게 둔다 — NEWS→EVENT가 조용히 끊기면 news_momentum이
        # 리포트 종목을 영영 안 잡는데, 로그에도 알림에도 아무 흔적이 없었다.
        lines.append("   엔진 토큰: " + " ".join(engine_tokens(payload, market)))
    lines.append("   → 확신도 엔진(watch-score) 통과분만 실제 등록된다")

    if url:
        lines.append(f"\n📄 전체 리포트: {url}")

    return "\n".join(lines)[:MAX_BRIEF_CHARS]
