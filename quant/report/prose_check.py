"""산문 근거 검증 — 리포트에 실린 LLM 산문이 같은 리포트의 결정론 사실
(engine.json/payload)을 벗어나지 않는지 사후에 검증하는 순수 함수 모음.

## 왜 필요한가

`quant/report/lint.py`는 payload 자체의 구조적 결함(NaN 누출, 방향 모순,
필수 섹션 공백)을 본다. 이 모듈은 그보다 좁고 다른 것 — **LLM이 쓴 문장 하나
하나가 같은 리포트의 숫자·종목·방향·스탠스와 실제로 맞는가**를 본다. 실측
감사(2026-09-07, `results/report_prose_audit/SUMMARY.md`)에서 실제로 걸린
사례: 2026-08-25 KR 중기 관심종목(373220 LG에너지솔루션) 산문이 "기관 -1.2조
순매도"라고 썼지만 같은 날 engine.json의 `institution_net_100m_krw`는
+11708(순매수 1.17조)이었다 — 부호가 정반대다. 프롬프트에 텔레그램 원문
스니펫만 들어가고 이 수치 자체가 안 들어갔으니, 모델이 자기 사전지식으로
지어낸 값이다.

## 검사 종류 (a)~(e) — (f) 템플릿 반복은 여러 날짜 코퍼스가 필요해 여기
포함하지 않는다(감사 하네스가 별도로 계산한다).

- (a) `unsupported_number` — 문장 속 숫자가 payload 어디에도 없다(반올림
  허용). WARN — 숫자 추출이 휴리스틱이라 오탐 여지가 있다.
- (b) `hallucinated_entity` — 문장에 등장한 KR 6자리 코드가 payload의 알려진
  종목 집합에 없다. ERROR — 코드 형식이 명확해 오탐이 적다.
- (c) `direction_contradiction` — 상승/하락·순매수/순매도 단어가 그 종목·
  지수의 실제 부호와 반대다. ERROR.
- (d) `unsupported_causality` — "~때문에"/"~영향으로" 등 인과 표현에 근거
  텍스트(뉴스·이유·텔레그램 스니펫)나 숫자 인용이 전혀 없다. WARN.
- (e) `contradicts_stance` — 강세/약세 톤이 `stance.tier`/`label`과 반대다.
  ERROR.

## 판단이 아니라 검증이다

무엇을 쓸지는 narrator/LLM이 이미 정했다. 이 모듈은 그 결과물을 결정론
규칙으로 재확인만 한다 — LLM을 다시 부르지 않고, 새 사실을 추가하지 않는다.
"""
from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass

ERROR = "error"
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warn"
    section: str
    message: str

    def __str__(self) -> str:  # pragma: no cover — 사람이 읽는 편의 포맷
        return f"[{self.severity}] {self.section}: {self.message}"


# ──────────────────────────────────────────────────────────── 문장 분리

# 한국어 문장 종결(다/요/음/함 + 마침표, !, ?) 뒤 공백, 또는 줄바꿈 기준.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(text.strip()) if p.strip()]
    return parts or ([text.strip()] if text.strip() else [])


# ──────────────────────────────────────────────────────────── (a) 숫자 근거

_NUM_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*")
# 순번·확신도(1~5)·척도(0/100) 등 문맥상 항상 등장하는 작은 정수는 대조하지
# 않는다 — 대조해봐야 소음만 늘어난다.
_SKIP_NUMBERS = {0.0, 1.0, 2.0, 3.0, 4.0, 5.0}


def _numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text):
        raw = m.group(0).replace(",", "")
        if raw in ("", "-", "+", "."):
            continue
        try:
            out.append(float(raw))
        except ValueError:
            continue
    return out


def collect_numeric_facts(payload: dict) -> frozenset[float]:
    """payload 전체를 재귀로 훑어 숫자 리프를 모은다. 반올림 변형(0/1/2자리)
    과 확률→퍼센트 변형(0.62 → 62)까지 함께 넣어, 비교는 단순 반올림
    멤버십으로 처리한다(허용 오차는 아래 `_unsupported_numbers`가 추가로
    상대오차 1%까지 봐준다)."""
    facts: set[float] = set()

    def add(v: float) -> None:
        if math.isnan(v) or math.isinf(v):
            return
        for nd in (0, 1, 2):
            facts.add(round(v, nd))
        if -1.0 <= v <= 1.0:
            for nd in (0, 1):
                facts.add(round(v * 100, nd))

    def walk(obj: object) -> None:
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            add(float(obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj[:2000]:
                walk(v)

    walk(payload)
    return frozenset(facts)


# 실측(2026-09-07, data/ledger/report_lint.jsonl) 오탐 두 가지 — (a) 종목명
# 뒤 괄호 안 6자리 코드("LG전자(066570)")가 숫자 주장으로 오인됐다: 아래
# `_unsupported_numbers`의 `known_codes` 인자가 처리한다(6자리 그대로 —
# float 변환 시 선행 0이 사라지므로 원문 문자열 그대로 비교). (b) "503개
# 종목 중 신고가 7개" 같은 개수(단위: 개/건/명/종목/주/회) 표현은 숫자
# 필드가 아니라 뉴스 제목·근거·텔레그램 원문 같은 **텍스트**에만 등장하는
# 경우가 많다 — `text_facts`(payload 문자열 전체를 재귀로 훑어 뽑은 숫자,
# `collect_text_numeric_facts`)에 그 숫자가 있으면 봐준다. 통화·등락률처럼
# 방향/크기가 중요한 소수는 여전히 숫자 리프(`facts`)만 근거로 인정한다 —
# 안 그러면 아무 뉴스 제목에나 우연히 박힌 숫자로 진짜 환각(예: "기관
# -1.2조 순매도")까지 다 봐주게 돼 검사기 자체가 무력화된다.
_COUNT_SUFFIXES = ("개", "건", "명", "종목", "주", "회")


def _is_count_number(sentence: str, raw: str, end: int) -> bool:
    """정수(소수점 없음) 뒤에 개수 단위가 바로 붙는지 — "503개"/"7건" 등."""
    if "." in raw:
        return False
    return sentence[end:end + 3].startswith(_COUNT_SUFFIXES)


def _unsupported_numbers(
    sentence: str, facts: frozenset[float], known_codes: frozenset[str] = frozenset(),
    text_facts: frozenset[float] = frozenset(),
) -> list[float]:
    bad = []
    for m in _NUM_RE.finditer(sentence):
        raw = m.group(0).replace(",", "")
        if raw in ("", "-", "+", "."):
            continue
        if re.fullmatch(r"\d{6}", raw) and raw in known_codes:
            continue  # 종목코드 — 숫자 주장이 아니라 식별자다.
        try:
            n = float(raw)
        except ValueError:
            continue
        if n in _SKIP_NUMBERS:
            continue
        if round(n, 0) in facts or round(n, 1) in facts or round(n, 2) in facts:
            continue
        # 상대오차 1%(최소 0.05) 허용 — 서로 다른 소수 자릿수로 반올림된 값.
        if any(abs(n - f) <= max(0.05, abs(f) * 0.01) for f in facts):
            continue
        if _is_count_number(sentence, raw, m.end()) and round(n, 0) in text_facts:
            continue
        bad.append(n)
    return bad


def collect_text_numeric_facts(payload: dict) -> frozenset[float]:
    """`collect_numeric_facts`(숫자 리프 전용)와 별도로, payload 의 **문자열**
    값을 재귀로 훑어 그 안에 박힌 숫자까지 뽑는다(뉴스 제목·reasons·텔레그램
    원문 등). `_unsupported_numbers`가 개수(카운트) 표현에만 이 확장 근거를
    쓴다 — 모듈 상단 주석 참고."""
    facts: set[float] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, str):
            facts.update(_numbers_in(obj))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj[:2000]:
                walk(v)

    walk(payload)
    return frozenset(facts)


# ──────────────────────────────────────────────────────────── (b) 종목/개체

_KR_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")

# US 티커(대문자 2~5글자) 정규식으로 개체 환각을 잡으려는 시도는 버렸다 —
# 08-13~09-06 아카이브 62건 감사(2026-09-07, `results/report_prose_audit/
# SUMMARY.md`)에서 217건이 걸렸는데 전수 조사 결과 실제로 지어낸 티커는
# 0건이었다. 전부 금융/반도체 약어(NFCI·AAII·NAAIM·PPI·PMI·ASIC·EUV·NAND
# 등)나 실존 비교종목 티커(NVDA·AVGO 등 — 후보 종목이 아닌 회사를 비교
# 근거로 언급하는 건 정상적인 애널리스트 서술이다)였다. 신뢰할 수 있는
# 실제 티커+약어 사전 없이는 이 정규식이 신호보다 소음을 압도적으로 더
# 만든다 — 그래서 KR 6자리 코드(포맷이 훨씬 명확해 오탐이 0건이었다)만
# 남기고 뺐다.


def collect_known_names(payload: dict) -> tuple[frozenset[str], frozenset[str]]:
    """(codes, names) — payload 전체에서 알려진 종목코드·티커·이름 집합."""
    codes: set[str] = set()
    names: set[str] = set()

    def add_sym(s: object) -> None:
        if not isinstance(s, dict):
            return
        code = s.get("symbol")
        name = s.get("name")
        if code:
            codes.add(str(code))
        if name:
            names.add(str(name))

    for s in payload.get("symbols") or []:
        add_sym(s)
    for s in payload.get("agent_interpret_view") or []:
        add_sym(s)
    for s in payload.get("midterm_watch") or []:
        add_sym(s)
    bridge = payload.get("us_kr_bridge")
    if isinstance(bridge, dict):
        for sec in bridge.get("us_sectors") or []:
            for st in sec.get("stocks") or []:
                add_sym(st)
    for item in payload.get("us_news_kr_map") or []:
        for st in item.get("stocks") or []:
            add_sym(st)
    return frozenset(codes), frozenset(names)


def _hallucinated_codes(sentence: str, known_codes: frozenset[str]) -> list[str]:
    return [c for c in _KR_CODE_RE.findall(sentence) if c not in known_codes]


# ──────────────────────────────────────────────────────────── (c) 방향

_UP_WORDS = ("상승", "급등", "올랐", "오르는", "오름세", "강세", "순매수", "매수 우위")
# "내렸다"/"내리는"은 뺐다 — "투자 결정을 내리는 것이 바람직하다"처럼 "결정을/
# 판단을 내리다"(make a decision) 관용구에 압도적으로 더 많이 쓰여 하락 어조로
# 오판하기 쉽다(2026-09-07 감사에서 실측 오탐 확인). "내림세"는 관용구
# 충돌이 없어 그대로 둔다.
_DOWN_WORDS = ("하락", "급락", "내림세", "약세", "순매도", "매도 우위")
# 주가 방향 전용 어휘(순매수/순매도는 수급 어휘라 뺀다) + 문장 주어 판별용 어휘(2026-09-07).
_PRICE_UP_WORDS = ("상승", "급등", "올랐", "오르는", "오름세", "강세")
_PRICE_DOWN_WORDS = ("하락", "급락", "내림세", "약세")
_NON_PRICE_SUBJECTS = (
    "외국인", "기관", "수급", "순매수", "순매도", "이탈", "유입", "추세 점수", "매도세", "매수세",
    "호재", "악재", "뉴스", "실적", "점수", "라벨",
)
_PRICE_SUBJECTS = ("주가", "종가", "등락", "마감", "시초가", "장중")


def _mentions_any(sentence: str, words: tuple[str, ...]) -> bool:
    return any(w in sentence for w in words)


def _flow_contradiction(sentence: str, actor: str, net: float | None) -> str | None:
    """`actor`("기관"/"외국인")가 문장에 등장하고 그 근처 문맥에 순매수/순매도
    단어가 있을 때만 `net`(그 주체의 실제 순매수 부호)과 대조한다. 주체 단어
    없이 "순매도"만 보고 대조하면 다른 주체(예: 외국인) 이야기를 기관 수급과
    비교하는 오귀속이 난다 — 2026-09-07 감사에서 실측(373220 08-26/27:
    문장은 "외국인 순매도"만 말했는데 기관 수급과 대조해 오탐)."""
    if net is None or actor not in sentence:
        return None
    if net > 0 and "순매도" in sentence:
        return f"{actor} 실제 수급은 순매수(+{net:,.0f})인데 문장은 '{actor} 순매도' 주장"
    if net < 0 and "순매수" in sentence:
        return f"{actor} 실제 수급은 순매도({net:,.0f})인데 문장은 '{actor} 순매수' 주장"
    return None


def _direction_contradiction(
    sentence: str, *, direction: str | None, change_pct: float | None,
    institution_net: float | None = None, foreign_net: float | None = None,
) -> str | None:
    """`direction`(bullish/bearish)·`change_pct`(등락률)와 문장의 방향 단어가
    반대면, 또는 "기관"/"외국인" 순매수·순매도 주장이 각자의 실제 부호와
    반대면 메시지를 돌려준다."""
    # 주가 방향 판정은 **주가를 말하는 문장**에만 건다(2026-09-07 로컬 실빌드에서 확인한 오탐:
    # "외국인 추세 점수 8/28, 이탈 추세" 처럼 수급·뉴스 어조를 말하는 문장이 그 종목의
    # 등락률과 반대라는 이유로 치환됐다 — 수급 문장은 아래 `_flow_contradiction` 이 실제
    # 수급 부호와 대조한다). 수급·뉴스·점수가 주어인 문장은 주가 단어(주가/종가/등락/마감)를
    # 명시할 때만 주가 방향을 판정한다.
    flow_or_news_subject = _mentions_any(sentence, _NON_PRICE_SUBJECTS)
    price_subject = _mentions_any(sentence, _PRICE_SUBJECTS)
    if flow_or_news_subject and not price_subject:
        return _flow_contradiction(sentence, "기관", institution_net) or _flow_contradiction(
            sentence, "외국인", foreign_net
        )
    up = _mentions_any(sentence, _PRICE_UP_WORDS)
    down = _mentions_any(sentence, _PRICE_DOWN_WORDS)
    if up != down:  # 둘 다 없거나 둘 다 있으면(예: "상승 후 하락") 판정하지 않는다
        if direction == "bullish" and down:
            return "direction=bullish인데 문장은 하락/순매도 어조"
        if direction == "bearish" and up:
            return "direction=bearish인데 문장은 상승/순매수 어조"
        if change_pct is not None:
            if change_pct > 0 and down and not up:
                return f"change_pct={change_pct:+.2f}%(상승)인데 문장은 하락 어조"
            if change_pct < 0 and up and not down:
                return f"change_pct={change_pct:+.2f}%(하락)인데 문장은 상승 어조"

    msg = _flow_contradiction(sentence, "기관", institution_net)
    if msg:
        return msg
    return _flow_contradiction(sentence, "외국인", foreign_net)


# ──────────────────────────────────────────────────────────── (d) 인과

_CAUSAL_MARKERS = ("때문에", "영향으로", "덕분에", "여파로", "기대감", "우려로", "우려가", "탓에")


def _has_causal_claim(sentence: str) -> bool:
    return any(m in sentence for m in _CAUSAL_MARKERS)


def _has_supporting_evidence(sentence: str, evidence_texts: tuple[str, ...]) -> bool:
    """인과 표현을 뒷받침할 근거가 있는가 — 숫자 인용, 또는 근거 텍스트
    (뉴스 제목·grade reasons·텔레그램 스니펫 등)와 2어절 이상 겹치는 부분
    문자열이 있는가. 완벽한 NLU가 아니라 "완전히 맨손으로 인과를 지어내진
    않았다"는 최소 확인이다."""
    if _numbers_in(sentence):
        return True
    for ev in evidence_texts:
        if not ev:
            continue
        # 근거 텍스트의 4자 이상 부분열이 문장에 그대로 나타나면 겹침으로 본다.
        ev = str(ev)
        for i in range(0, max(len(ev) - 4, 0) + 1, 2):
            chunk = ev[i:i + 4]
            if len(chunk) == 4 and chunk in sentence:
                return True
    return False


# ──────────────────────────────────────────────────────────── (e) 스탠스

_BULLISH_TONE = ("낙관", "강세 국면", "공격적 대응", "매수 우위", "적극적으로 대응")
_BEARISH_TONE = ("비관", "약세 국면", "방어적", "보수적으로 대응", "매도 우위", "조심스러운 접근")


def _stance_contradiction(sentence: str, stance: dict) -> str | None:
    tier = stance.get("tier")
    label = stance.get("label")
    bullish_tone = _mentions_any(sentence, _BULLISH_TONE)
    bearish_tone = _mentions_any(sentence, _BEARISH_TONE)
    if bullish_tone == bearish_tone:
        return None
    is_defensive = tier == "방어" or label == "하락 신호"
    is_aggressive = tier == "공격" or label == "상승 신호"
    if is_defensive and bullish_tone:
        return f"stance={tier or label}(방어적)인데 문장은 낙관/공격적 어조"
    if is_aggressive and bearish_tone:
        return f"stance={tier or label}(공격적)인데 문장은 비관/방어적 어조"
    return None


# ──────────────────────────────────────────────────────────── 심볼 조회

def _symbol_row(payload: dict, symbol: str | None) -> dict | None:
    if not symbol:
        return None
    for s in payload.get("symbols") or []:
        if isinstance(s, dict) and s.get("symbol") == symbol:
            return s
    return None


# ──────────────────────────────────────────────────────────── 핵심 검사

def _check_sentence(
    sentence: str, *, numeric_facts: frozenset[float], known_codes: frozenset[str],
    own_symbol: str | None, direction: str | None,
    change_pct: float | None, institution_net: float | None, foreign_net: float | None,
    stance: dict, evidence_texts: tuple[str, ...],
    text_facts: frozenset[float] = frozenset(),
) -> list[tuple[str, str]]:
    """`(severity, category: message)` 목록을 돌려준다."""
    out: list[tuple[str, str]] = []

    bad_numbers = _unsupported_numbers(sentence, numeric_facts, known_codes, text_facts)
    if bad_numbers:
        out.append((WARN, f"unsupported_number: 근거 없는 숫자 {bad_numbers}"))

    bad_codes = [c for c in _hallucinated_codes(sentence, known_codes) if c != own_symbol]
    for c in bad_codes:
        out.append((ERROR, f"hallucinated_entity: 알 수 없는 종목코드 {c}"))

    dir_msg = _direction_contradiction(
        sentence, direction=direction, change_pct=change_pct,
        institution_net=institution_net, foreign_net=foreign_net,
    )
    if dir_msg:
        out.append((ERROR, f"direction_contradiction: {dir_msg}"))

    if _has_causal_claim(sentence) and not _has_supporting_evidence(sentence, evidence_texts):
        out.append((WARN, "unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음"))

    stance_msg = _stance_contradiction(sentence, stance)
    if stance_msg:
        out.append((ERROR, f"contradicts_stance: {stance_msg}"))

    return out


def _check_block_raw(
    section: str, text: str | None, payload: dict, *, numeric_facts, known_codes,
    own_symbol: str | None = None, direction: str | None = None,
    change_pct: float | None = None, institution_net: float | None = None,
    foreign_net: float | None = None, evidence_texts: tuple[str, ...] = (),
    text_facts: frozenset[float] = frozenset(),
) -> list[tuple[Finding, str]]:
    """`(Finding, 그 근거가 된 문장)` 목록 — `redact_prose`가 문장 단위
    치환에 쓰려고 원문 문장을 함께 돌려준다(공개 API인 `check_prose`는
    `Finding`만 노출한다)."""
    if not text:
        return []
    stance = payload.get("stance") or {}
    out: list[tuple[Finding, str]] = []
    for sentence in split_sentences(text):
        for severity, message in _check_sentence(
            sentence, numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            own_symbol=own_symbol, direction=direction,
            change_pct=change_pct, institution_net=institution_net,
            foreign_net=foreign_net, stance=stance,
            evidence_texts=evidence_texts,
        ):
            snippet = sentence if len(sentence) <= 160 else sentence[:157] + "..."
            out.append((Finding(severity, section, f"{message} — {snippet!r}"), sentence))
    return out


def _flatten_reasons(obj: dict) -> tuple[str, ...]:
    out: list[str] = []
    for key in ("reasons",):
        v = obj.get(key)
        if isinstance(v, list):
            out.extend(str(x) for x in v)
    flow = obj.get("flow") or {}
    cash = obj.get("cash") or {}
    for sub in (flow, cash):
        if isinstance(sub, dict) and isinstance(sub.get("reasons"), list):
            out.extend(str(x) for x in sub["reasons"])
    return tuple(out)


def check_prose(payload: dict) -> list[Finding]:
    """payload(=engine.json 그대로, 또는 그 위에 렌더 전용 산문 필드를 얹은
    사본) 안의 알려진 산문 블록을 전부 찾아 (a)~(e)를 검사한다.

    현재 실제 engine.json에는 `money_flow.prose`/`midterm_watch[].prose`/
    `holiday_synthesis.prose`만 실린다 — `exec_summary`/`digest_prose`/
    `section_advice`/`stance_prose`/`agent_interpret_view`는 렌더 전용
    (`ReportModel`에만 있고 payload에는 없다, 2026-09-07 감사에서 확인한
    간극 — `results/report_prose_audit/SUMMARY.md` §grounding-gaps 참고).
    이 함수는 그 키들도 이미 지원한다 — payload에 실리는 순간(예: `lint_report`
    가 `ReportModel.payload`에 병합해 넘기거나, 두 키가 나중에 engine.json에
    실리게 되면) 추가 코드 변경 없이 검사가 시작된다."""
    numeric_facts = collect_numeric_facts(payload)
    known_codes, known_names = collect_known_names(payload)
    del known_names  # 현재 검사(코드/티커 기반)는 이름 집합을 쓰지 않는다 — 향후 확장용으로 수집만.
    text_facts = collect_text_numeric_facts(payload)

    findings: list[Finding] = []

    money_flow = payload.get("money_flow")
    if isinstance(money_flow, dict):
        raw = _check_block_raw(
            "money_flow.prose", money_flow.get("prose"), payload,
            numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            evidence_texts=_flatten_reasons(money_flow),
        )
        findings.extend(f for f, _ in raw)

    stance = payload.get("stance") or {}
    raw = _check_block_raw(
        "stance_prose", payload.get("stance_prose"), payload,
        numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
        evidence_texts=tuple((stance.get("positives") or []) + (stance.get("negatives") or [])),
    )
    findings.extend(f for f, _ in raw)

    holiday = payload.get("holiday_synthesis")
    if isinstance(holiday, dict):
        raw = _check_block_raw(
            "holiday_synthesis.prose", holiday.get("prose"), payload,
            numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
        )
        findings.extend(f for f, _ in raw)

    exec_summary = payload.get("exec_summary")
    if isinstance(exec_summary, dict):
        for part in ("market", "flow", "catalyst"):
            raw = _check_block_raw(
                f"exec_summary.{part}", exec_summary.get(part), payload,
                numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            )
            findings.extend(f for f, _ in raw)

    digest_prose = payload.get("digest_prose")
    if isinstance(digest_prose, dict):
        for part in ("domestic_prose", "us_prose"):
            raw = _check_block_raw(
                f"digest_prose.{part}", digest_prose.get(part), payload,
                numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            )
            findings.extend(f for f, _ in raw)

    section_advice = payload.get("section_advice")
    if isinstance(section_advice, dict):
        for part in ("supply", "sentiment", "technical", "liquidity"):
            raw = _check_block_raw(
                f"section_advice.{part}", section_advice.get(part), payload,
                numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            )
            findings.extend(f for f, _ in raw)

    for item in payload.get("agent_interpret_view") or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        sym_row = _symbol_row(payload, symbol)
        raw = _check_block_raw(
            f"agent_interpret_view[{symbol}]", item.get("prose"), payload,
            numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            own_symbol=symbol, direction=item.get("direction"),
            change_pct=(sym_row or {}).get("change_pct"),
        )
        findings.extend(f for f, _ in raw)

    for item in payload.get("midterm_watch") or []:
        if not isinstance(item, dict):
            continue
        symbol = item.get("symbol")
        sym_row = _symbol_row(payload, symbol)
        features = payload.get("features") or {}
        raw = _check_block_raw(
            f"midterm_watch[{symbol}]", item.get("prose"), payload,
            numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
            own_symbol=symbol, change_pct=(sym_row or {}).get("change_pct"),
            institution_net=features.get("institution_net_100m_krw"),
            foreign_net=features.get("foreign_net_100m_krw"),
            evidence_texts=tuple((item.get("reasons") or []) + (item.get("telegram_snippets") or [])),
        )
        findings.extend(f for f, _ in raw)

    return findings


# ──────────────────────────────────────────────────────────── 발행 전 치환

REDACTED_TEXT = "근거 부족으로 생략"


def redact_prose(
    section: str, text: str | None, payload: dict, *, own_symbol: str | None = None,
    direction: str | None = None, change_pct: float | None = None,
    institution_net: float | None = None, foreign_net: float | None = None,
    evidence_texts: tuple[str, ...] = (),
) -> tuple[str | None, list[Finding]]:
    """발행 전 산문 필터 — `check_prose`와 같은 규칙으로 문장 단위 검사하되,
    `severity=error`인 문장만 `REDACTED_TEXT`로 치환한 새 텍스트를 돌려준다
    (경고는 사람이 볼 몫이라 텍스트에는 그대로 둔다). 모든 문장이 치환되면
    전체를 `None`으로 돌려 렌더가 그 섹션 자체를 생략하게 한다(무LLM 폴백과
    같은 관례).

    호출부(`quant/report/collect/{agent_interpret,midterm,news}.py`)가
    narrator 응답을 payload/engine.json에 싣기 **전에** 부른다 — 템플릿은
    이미 걸러진 텍스트만 받는다."""
    if not text:
        return text, []
    numeric_facts = collect_numeric_facts(payload)
    known_codes, _ = collect_known_names(payload)
    text_facts = collect_text_numeric_facts(payload)
    raw = _check_block_raw(
        section, text, payload, numeric_facts=numeric_facts, known_codes=known_codes, text_facts=text_facts,
        own_symbol=own_symbol, direction=direction, change_pct=change_pct,
        institution_net=institution_net, foreign_net=foreign_net,
        evidence_texts=evidence_texts,
    )
    findings = [f for f, _ in raw]
    bad_sentences = {sentence for f, sentence in raw if f.severity == ERROR}
    # 치환된 문장을 stderr 에 남긴다(2026-09-07) — 리포트 로그(report.log)에서 "무엇이 왜
    # 빠졌는지"를 사후 감사할 수 있어야 한다. 렌더된 HTML 에는 "근거 부족으로 생략"만 남는다.
    for f, sentence in raw:
        if f.severity == ERROR:
            print(f"[prose:redacted] {section}: {f.message}", file=sys.stderr)
    if not bad_sentences:
        return text, findings

    sentences = split_sentences(text)
    kept = [REDACTED_TEXT if s in bad_sentences else s for s in sentences]
    # 연속 치환은 하나로 접는다(같은 문구가 여러 번 나오지 않게).
    collapsed: list[str] = []
    for s in kept:
        if s == REDACTED_TEXT and collapsed and collapsed[-1] == REDACTED_TEXT:
            continue
        collapsed.append(s)
    if all(s == REDACTED_TEXT for s in collapsed):
        return None, findings
    return " ".join(collapsed), findings
