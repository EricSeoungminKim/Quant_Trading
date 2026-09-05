"""텔레그램 인텔리전스 다이제스트 — 정규장 중 30분마다 신규 메시지만 모아
KR/US 스탠스 힌트·관심종목 후보·방별 요약을 만든다 (2026-09-05, "텔레그램
인텔리전스 레인" 착수, 소유자 요구 (4)). `quant/analyze/` 평면 — `quant/trade/`
임포트 금지, LLM 호출은 주입만(직접 하지 않음).

## 계약 — narrator.py 와 같은 순수 + 주입 패턴

`build_digest`는 순수 함수다: 네트워크·디스크 I/O 없음. 메시지는 호출부
(`quant.apps.cli tg-digest`)가 `quant.collect.sources.telegram_channels.
load_window(path, since, until)`로 읽어 넘긴다. LLM 호출은 `llm_call`(선택,
`프롬프트 -> 응답|None`)로 주입한다 — `quant.analyze.narrator.narrate`와 같은
all-or-nothing 계약: 실패/빈 응답/마커 누락/숫자 위조는 전부 결정론 다이제스트만
단독으로 나가는 것으로 통일한다(narrator.py 모듈 docstring 참고).

## 숫자 검증(소유자 요구 (2): "채널 숫자를 그대로 사실로 베끼지 않는다")

이 계약을 두 층에서 지킨다:

1. **결정론 숫자 클레임**(`NumberClaim`) — 단위(원/만원/억/달러/$/%/bp/포인트/p)가
   붙거나, 유효숫자 3자리 이상이면서 가격 단어(목표가/주가/종가/저가/고가)
   근방(15자)인 숫자만 클레임으로 인정한다(2026-09-05 소유자 지시 — 날짜·
   작은 정수를 걸러낸다). 가격형 단위(원/만원/억/달러/$)만 `quotes_lookup`
   (선택, 심볼 → 현재가 콜러블)과 대조해 오차 2% 이내면 "✓", 아니면 "✗".
   %/bp/포인트/p 나 대조 불가(quotes_lookup 없음·시세 결측)는 "미확인"으로
   남고, 렌더링에서 항상 "채널 주장"으로 표시한다 — 우리 진술로 격상하지
   않는다. 검증 가능한 클레임이 하나도 없으면 그렇다고 명시한다.
2. **LLM 서술** — `quant.analyze.narrator.verify_numbers`(숫자 검증 가드)를
   그대로 재사용해, LLM 응답에 등장하는 숫자가 하나라도 그 창의 원문 메시지에
   없으면 응답 전체를 폐기한다("절반만 믿을 수 있는 문장은 안 믿느니만 못하다").

## 후보 정밀도(소유자 요구, 2026-09-05 EC2 원장 실측 리뷰)

실제 원장으로 돌려보니 두 가지 잡음이 나왔다: (1) 목록형 메시지(오늘의
상한가 등) 하나에 실려 종목명이 스쳐가듯 한 번만 언급되는 경우, (2) 미국
쪽은 흔한 금융/기술 약어(HBM, MBS, ASIC, DRAM...)가 실제 상장 티커와
우연히 겹치는 경우. 방어:

- **랭킹 기준을 채널 수가 아니라 "서로 다른 메시지 수"로 바꾼다** — 목록형
  메시지 하나에 90개 종목이 스쳐가도 그 종목은 "메시지 1건"일 뿐이다.
  `CANDS`는 서로 다른 메시지 2건 이상 언급된 종목만, 최대 10개(게이트가
  각각 API 호출을 하므로 상한이 곧 비용이다).
- **US**: 바로 매칭된 게 티커 원형(예: "HBM")이면 `$` 접두 또는 근방(12자)
  가격/종목 맥락이 있을 때만 인정한다. 회사명(예: "Microsoft")으로 매칭됐으면
  그대로 인정한다. 실측 오탐 + 흔한 약어 정적 스톱리스트(`US_ACRONYM_STOPLIST`)
  는 무조건 제외한다.
- **KR**: 그 자체로 일반 단어이기도 한 짧은 상호(유니온/동양/한국/대한/삼성/
  현대/서울/성장/우리/신한 — `KR_GENERIC_NAMES`)는 전자/증권/생명/보험/화학
  같은 법인 접미사가 바로 붙어 있거나 종목코드가 문장에 같이 있을 때만 인정.

## 리스크 항목 — 보일러플레이트 제거 + 중복 축소

일부 채널(rafikiresearch 등)은 "수집 완료: … 출처: …" 꼬리, "[섹션 제목]…
금일 발언 없음(…)" 반복 블록을 매 메시지에 싣는다 — 분석 전에 제거한다
(`_strip_boilerplate`). 같은 메시지에서 같은 리스크 태그가 여러 문장에 걸쳐
반복되면(예: 국채 얘기가 4문장 연속) 메시지당 태그당 1건만 남긴다. 렌더링은
태그로 묶고 채널명은 그룹당 한 번만, 최대 6개 태그 그룹.

## 스탠스 — 서술기가 없어도 조용히 사라지지 않는다

LLM 이 꺼져 있거나 실패하면(`stance is None`) `Digest.stance_display()`가
"서술기 미가용 — 리스크 태그 …(자동 판정 안 함)"을 대신 돌려준다 — 섹션
자체를 생략하지 않는다(소유자 지시: 정직하게 "판정 안 함"을 보여줄 것).

## data/state/tg_stance.json — 참고용, 엔진이 읽지 않는다

`persist_stance`가 매 실행마다 `{market, stance, why, at, sources}`를 이
파일에 남긴다. **엔진(`quant.trade`)은 이 파일을 읽지 않는다** — 스탠스는
소유자가 텔레그램에서 참고하는 조언일 뿐, 자동매매 사이징에 반영되지 않는다
(`quant.analyze.market_pulse`와 같은 원칙 — "순수 참고용"). 이 스탠스의 예측력을
재는 것은 후속 과제다(파일에 시계열로 쌓이므로 나중에 승률/상관 분석이 가능하다).

## 프로그램 스탠스 — 유료 레인 없이도 항상 나온다(소유자 결정, 2026-09-05)

소유자 질문 "유료 레인이 꼭 필요한가?"의 답은 "아니다"다. `Digest.
program_stance_display()`는 **결정론**(LLM 무관)이다 — `regime`(호출부가
`data/state/regime.json`에서 그 시장의 sub-dict를 읽어 주입, 이 모듈은 파일을
읽지 않는다 — "네트워크·디스크 I/O 없음" 계약 유지)의 `label`/`risk_multiplier`/
`reasons`를 그대로 문장으로 옮기고, 채널 리스크 태그 집계를 덧붙인다. `regime`이
없으면(파일 없음/그 시장 상태 아직 없음) 정직하게 "판정 불가"라고 말한다 —
`stance_display()`의 "서술기 미가용" 관례와 같다. `render_telegram`/리포트
템플릿 둘 다 이 줄을 [스탠스] 섹션의 **첫 줄**로, 기존 `stance_display()`
(LLM, 선택)를 그 아래 줄로 보여준다 — "결정론이 먼저, LLM은 있으면 덧붙이는
것"(소유자 지시 그대로).

## LLM 스탠스 — 스탠스 전용 마이크로프롬프트(2026-09-05)

기존 `llm_call`(방별 요약·후보 이유까지 함께 묻는 큰 프롬프트, 아래 "계약"
절)은 스탠스도 `[STANCE]` 마커로 같이 물었었지만, 실측(2026-09-05 EC2 KR
다이제스트)으로 무료 추론 모델이 사고과정에 토큰을 다 써 스탠스 서술이
거의 매번 폐기되는 게 확인됐다(`quant.adapters.narrate` 모듈독스트링 참고).
`stance_llm_call`(선택, `프롬프트 -> {"stance","why"}|None`, 이미 엄격
검증된 dict — 이 모듈은 파싱하지 않는다, `quant.adapters.narrate.stance_only`가
담당)은 그 문제를 우회하는 **별도**의 작고 안정적인 경로다: 채널 메시지만
근거로 스탠스 하나만 묻는다. `llm_call`이 이미 유효한 스탠스를 만들었으면
그걸 그대로 쓰고(하위호환 — 기존 동작 유지), 없을 때만(=거의 항상)
`stance_llm_call`이 보강한다 — 두 경로 모두 실패하면 `stance_display()`가
"서술기 미가용"으로 정직하게 떨어진다.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from quant.analyze.entities import extract as _extract_kr
from quant.analyze.entities import extract_us as _extract_us
from quant.analyze.narrator import verify_numbers
from quant.collect.sources.feeds import parse_published
from quant.collect.sources.telegram_channels import channels_for
from quant.core import tgfmt
from quant.core.report_clock import KST

# 채널당 노출 상한 — telegram_view.ITEMS_PER_CHANNEL(5)과 같은 관례.
CHANNEL_ITEM_CAP = 5
SNIPPET_LEN = 160
CANDIDATE_DISPLAY_CAP = 8
RISK_GROUP_CAP = 6
NUMBER_DISPLAY_CAP = 8
DIGEST_MAX_CHARS = 3500  # tgfmt.MAX_CHARS(4096, 텔레그램 하드컷)보다 낮은 자체 예산.
PRICE_TOLERANCE = 0.02  # ±2% 이내면 채널 숫자를 "✓"로 인정.
PRICE_BAND_LO, PRICE_BAND_HI = 0.5, 2.0  # 이 배수 밖이면 가격 주장으로 보지 않는다(→ "미확인").
CANDS_MIN_MESSAGES = 2  # CANDS 채택 최소 — 서로 다른 메시지 수 기준.
CANDS_MAX_SYMBOLS = 10  # 게이트가 각각 API 호출 — 상한이 곧 비용(소유자 지시).

# 리스크 키워드(소유자 예시 + daegurr/hanwhastrategy 실측 표본에서 자주 나오는
# 매크로·지정학 축을 더했다 — "월러 연준 이사 발언", "국고채 3년물" 등).
RISK_KEYWORDS: tuple[str, ...] = (
    "금리", "관세", "지정학", "실적", "규제", "유동성", "환율", "인플레이션",
    "긴축", "완화", "무역분쟁", "전쟁", "제재", "신용등급", "디폴트", "파산",
    "공급망", "연준", "국채",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")

STANCES = ("방어", "중립", "공격")

# regime.json 의 label(quant.trade.regime.models.RegimeState) → 한국어.
# program_stance_display()가 쓴다.
_REGIME_LABEL_KR = {"defensive": "방어", "neutral": "중립", "aggressive": "공격"}

# ---------------------------------------------------------------------------
# 후보 정밀도(소유자 요구, 2026-09-05) — US 약어 스톱리스트 + 가격 맥락 판정,
# KR 일반명사 상호 판정.
# ---------------------------------------------------------------------------

# 실측 오탐(EC2 원장 dry-run: GPT/AGI/API/PRE/HBM/MBS/ASIC/DRAM/EUV/FOR/HIS/
# NEAR/PS/BOE/OLED/AAC/GAA/QLC/WIP/RNA/KEX/OIS/NPO/UTG/PBP) + 흔한 금융·반도체
# 약어(entities.py 의 COMMON_WORD_TICKERS 와 별개 — 이쪽은 tg_digest 전용
# 문맥에서만 적용한다, 뉴스 헤드라인 추출 자체를 바꾸지 않는다).
US_ACRONYM_STOPLIST: frozenset[str] = frozenset({
    "GPT", "AGI", "API", "PRE", "HBM", "MBS", "ASIC", "DRAM", "EUV", "FOR", "HIS",
    "NEAR", "PS", "BOE", "OLED", "AAC", "GAA", "QLC", "WIP", "RNA", "KEX", "OIS",
    "NPO", "UTG", "PBP",
    "AI", "ETF", "CEO", "CFO", "COO", "GDP", "CPI", "PMI", "FOMC", "EPS", "PER",
    "PBR", "ROE", "IPO", "YOY", "QOQ", "NAND", "LCD", "EV", "ESS", "SMR", "LLM",
    "GPU", "CPU", "NPU", "ASP", "SEC", "IRS", "NATO", "OPEC", "IRA", "ESG",
    "USD", "EUR", "JPY", "KRW",
})
_US_PRICE_CONTEXT_WORDS = ("주가", "종목", "실적", "목표가")
_US_PRICE_CONTEXT_NUMBER_RE = re.compile(r"[+-]?\d[\d,]*\.?\d*\s*(?:%|원|달러)|\$\s?\d")
_US_CONTEXT_WINDOW = 12

# KR: 그 자체로 흔한 일반 단어이기도 한 짧은 상호 — 법인 접미사나 코드가
# 같이 있을 때만 인정한다(소유자 실측: "유니온" 단독 매칭이 노이즈였다).
KR_GENERIC_NAMES: frozenset[str] = frozenset({
    "유니온", "동양", "한국", "대한", "삼성", "현대", "서울", "성장", "우리", "신한",
})
KR_CORP_SUFFIXES: tuple[str, ...] = ("전자", "증권", "생명", "보험", "화학")

# ---------------------------------------------------------------------------
# 숫자 클레임(소유자 요구, 2026-09-05) — 단위가 있거나(원/만원/억/달러/$/%/bp/
# 포인트/p) 유효숫자 3자리 이상 + 가격 단어 근방일 때만 인정한다. 날짜·작은
# 정수(2026, 9, 4, -6 등)는 걸러낸다.
# ---------------------------------------------------------------------------

_UNIT_SUFFIX_RE = re.compile(r"([+-]?\d[\d,]*\.?\d*)\s*(만원|원|억|달러|bp|포인트|%)")
_UNIT_PREFIX_RE = re.compile(r"\$\s?([+-]?\d[\d,]*\.?\d*)")
_UNIT_P_RE = re.compile(r"([+-]?\d[\d,]*\.?\d*)\s*p\b")
_PRICE_WORD_RE = re.compile(r"목표가|주가|종가|저가|고가")
_BARE_NUMBER_RE = re.compile(r"(?<![0-9])[+-]?\d[\d,]*\.?\d*")
_PRICE_UNIT_MULTIPLIER = {"만원": 10_000, "원": 1, "억": 100_000_000, "달러": 1}
_PRICE_CONTEXT_WINDOW = 15

# ---------------------------------------------------------------------------
# 리스크 항목 보일러플레이트(소유자 요구, 2026-09-05, rafikiresearch 실측) —
# "수집 완료: … 출처: … 다음 브리핑: …" 꼬리와 "[섹션]…금일 발언 없음(…)"
# 반복 블록은 분석 전에 제거한다.
# ---------------------------------------------------------------------------

_BOILERPLATE_TAIL_RE = re.compile(r"수집\s*완료(?:일)?\s*[:：].*$", re.S)
_NO_COMMENT_RE = re.compile(r"금일\s*발언\s*없음\s*(?:\([^)]*\))?")


def _strip_boilerplate(text: str) -> str:
    text = _BOILERPLATE_TAIL_RE.sub("", text)
    text = _NO_COMMENT_RE.sub("", text)
    return text


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _hhmm(published: str | None) -> str | None:
    dt = parse_published(published)
    if dt is None:
        return None
    return dt.astimezone(KST).strftime("%H:%M")


def _snippet(text: str, limit: int = SNIPPET_LEN) -> str:
    raw = " ".join((text or "").split())
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# 데이터 모델
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelEntry:
    handle: str
    published_hhmm: str | None
    snippet: str
    link: str | None


@dataclass(frozen=True)
class Candidate:
    symbol: str
    name: str | None
    sentence: str
    channels: tuple[str, ...]
    message_count: int = 1  # 서로 다른 (handle, msg_id) 언급 수 — CANDS 채택 기준.


@dataclass(frozen=True)
class RiskItem:
    keyword: str
    handle: str
    sentence: str


@dataclass(frozen=True)
class NumberClaim:
    symbol: str
    value: str
    handle: str
    sentence: str
    status: str  # "✓" | "✗" | "미확인"


@dataclass(frozen=True)
class Digest:
    market: str
    since: datetime
    until: datetime
    channel_entries: dict[str, list[ChannelEntry]] = field(default_factory=dict)
    # handle -> "미리보기 없음" 등 명시적 사유. 정상 채널(신규 메시지가 있고
    # 본문도 있음)은 이 dict 에 없다 — coordinator 요구: 조용히 넘기지 않는다.
    channel_notices: dict[str, str] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    # CANDS 채택 심볼(서로 다른 메시지 ≥2건, 최대 10개) — CLI/셸이 그대로 쓴다.
    cands: tuple[str, ...] = field(default_factory=tuple)
    risk_items: list[RiskItem] = field(default_factory=list)
    number_claims: list[NumberClaim] = field(default_factory=list)
    stance: str | None = None
    stance_why: str | None = None
    room_summary: str | None = None
    candidate_reasons: dict[str, str] = field(default_factory=dict)
    llm_used: bool = False
    # data/state/regime.json 의 그 시장 sub-dict({"label","risk_multiplier",
    # "reasons",...}) — 호출부(CLI/report_cli)가 읽어 주입한다(모듈 docstring
    # "프로그램 스탠스" 절, "네트워크·디스크 I/O 없음" 계약 유지). 없으면 None.
    regime: dict | None = None

    def has_content(self) -> bool:
        return bool(self.channel_entries)

    def program_stance_display(self) -> str:
        """결정론 프로그램 스탠스 한 줄 — LLM 없이 항상 나온다(소유자 결정:
        유료 레인 불필요, 모듈 docstring "프로그램 스탠스" 절). `regime`을
        못 읽었으면(파일 없음/그 시장 상태 없음) 정직하게 "판정 불가"를
        보여준다 — `stance_display()`의 "서술기 미가용" 관례와 같다."""
        if not isinstance(self.regime, dict) or not self.regime.get("label"):
            return "프로그램 스탠스: 판정 불가 (regime.json 없음)"
        label_kr = _REGIME_LABEL_KR.get(self.regime["label"], self.regime["label"])
        mult = self.regime.get("risk_multiplier")
        mult_str = f"{float(mult):.1f}x" if isinstance(mult, (int, float)) else "?x"
        reasons = self.regime.get("reasons") or []
        reasons_str = ", ".join(str(r) for r in reasons) if reasons else "근거 없음"
        line = f"프로그램 스탠스: {label_kr}({mult_str}) — {reasons_str}"
        counts = Counter(r.keyword for r in self.risk_items)
        if counts:
            tag_line = "·".join(
                f"{kw} {n}" for kw, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            )
            line += f" · 리스크 태그 {tag_line}"
        return line

    def stance_display(self) -> str:
        """LLM 스탠스 한 줄 — 성공이면 실제 판정, 실패/미가용이면 자동 판정을
        하지 않았다는 사실 + 리스크 태그 집계만 정직하게 보여준다(섹션 자체를
        생략하지 않는다, 소유자 지시 2026-09-05). `program_stance_display()`
        (결정론, 항상 나옴)와 짝을 이루는 **선택** 한 줄이다."""
        if self.stance:
            return f"{self.stance} — {self.stance_why}" if self.stance_why else self.stance
        counts = Counter(r.keyword for r in self.risk_items)
        if not counts:
            return "서술기 미가용 — 리스크 태그 없음 (자동 판정 안 함)"
        tag_line = "·".join(
            f"{kw} {n}" for kw, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        return f"서술기 미가용 — 리스크 태그 {tag_line} (자동 판정 안 함)"

    def risk_groups(self, cap: int = RISK_GROUP_CAP) -> list[tuple[str, str]]:
        """`[(태그, "채널: 문장 / 문장 · 채널2: 문장"), ...]` — 태그로 묶고 그룹
        안에서 채널명은 한 번만(소유자 지시 2026-09-05). `render_telegram`과
        리포트 템플릿(`report.html.j2`/`close_report.html.j2`)이 이 메서드
        하나를 공유해 그루핑 로직이 두 곳에서 따로 자라지 않게 한다."""
        grouped: dict[str, list[RiskItem]] = {}
        for r in self.risk_items:
            grouped.setdefault(r.keyword, []).append(r)
        out: list[tuple[str, str]] = []
        for keyword, items in list(grouped.items())[:cap]:
            by_handle: dict[str, list[str]] = {}
            for it in items:
                by_handle.setdefault(it.handle, []).append(_snippet(it.sentence, 80))
            parts = [f"{handle}: " + " / ".join(snips) for handle, snips in by_handle.items()]
            out.append((keyword, " · ".join(parts)))
        return out


# ---------------------------------------------------------------------------
# 후보 추출 — 시장별 정밀도 게이트(모듈 docstring "후보 정밀도" 절)
# ---------------------------------------------------------------------------


def _has_us_price_context(sentence: str, start: int, end: int) -> bool:
    around = sentence[max(0, start - _US_CONTEXT_WINDOW): end + _US_CONTEXT_WINDOW]
    if any(w in around for w in _US_PRICE_CONTEXT_WORDS):
        return True
    return bool(_US_PRICE_CONTEXT_NUMBER_RE.search(around))


def _us_candidates_in_sentence(sentence: str, table: list[tuple[str, str]]) -> list[dict]:
    """`entities.extract_us` 결과를 재사용하되, 매칭이 회사명(예: Microsoft)이
    아니라 티커 원형(예: HBM)뿐이면 `$` 접두 또는 근방(12자) 가격/종목 맥락이
    있을 때만 받아들인다. 정적 스톱리스트(`US_ACRONYM_STOPLIST`)는 매칭
    방식과 무관하게 항상 제외한다."""
    if not table:
        return []
    hits = _extract_us(sentence, table)
    accepted: list[dict] = []
    for hit in hits:
        symbol, name = hit["symbol"], hit.get("name") or ""
        if symbol in US_ACRONYM_STOPLIST:
            continue
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", sentence, re.IGNORECASE):
            accepted.append(hit)
            continue
        if re.search(rf"\${re.escape(symbol)}\b", sentence):
            accepted.append(hit)
            continue
        m = re.search(rf"\b{re.escape(symbol)}\b", sentence)
        if m and _has_us_price_context(sentence, m.start(), m.end()):
            accepted.append(hit)
    return accepted


def _kr_candidates_in_sentence(sentence: str, table: list[tuple[str, str]]) -> list[dict]:
    """`entities.extract`(이미 3글자 미만·조각 매칭을 방어) 결과에서, 그 자체로
    일반 단어이기도 한 짧은 상호(`KR_GENERIC_NAMES`)는 법인 접미사가 바로
    붙어 있거나 종목코드가 문장에 같이 있을 때만 받아들인다."""
    if not table:
        return []
    hits = _extract_kr(sentence, table)
    accepted: list[dict] = []
    for hit in hits:
        name, symbol = hit["name"], hit["symbol"]
        if len(name) < 3:
            continue
        if name in KR_GENERIC_NAMES:
            suffix_re = re.compile(re.escape(name) + "(?:" + "|".join(KR_CORP_SUFFIXES) + ")")
            if not suffix_re.search(sentence) and symbol not in sentence:
                continue
        accepted.append(hit)
    return accepted


def _candidates_in_sentence(market: str, sentence: str, table: list[tuple[str, str]]) -> list[dict]:
    if market == "KR":
        return _kr_candidates_in_sentence(sentence, table)
    return _us_candidates_in_sentence(sentence, table)


# ---------------------------------------------------------------------------
# 숫자 클레임 추출 — 모듈 docstring "숫자 클레임" 절
# ---------------------------------------------------------------------------


def _sig_digits(num_str: str) -> int:
    digits = re.sub(r"[^0-9]", "", num_str).lstrip("0")
    return len(digits)


def _number_claims_in_sentence(sentence: str) -> list[tuple[str, float | None]]:
    """`(표시값, 가격형_숫자|None)` 목록. 가격형(원/만원/억/달러/$)만 두 번째
    값이 채워진다(만원/억은 원화로 환산) — %/bp/포인트/p 나 가격 단어 근방
    3자리+ 숫자는 표시는 하되 항상 "미확인"(두 번째 값 None)."""
    claims: list[tuple[str, float | None]] = []
    spans: list[tuple[int, int]] = []

    def _taken(s: int, e: int) -> bool:
        return any(s < je and e > js for js, je in spans)

    for m in _UNIT_SUFFIX_RE.finditer(sentence):
        s, e = m.span()
        if _taken(s, e):
            continue
        spans.append((s, e))
        num_str, unit = m.group(1), m.group(2)
        display = f"{num_str}{unit}"
        if unit in _PRICE_UNIT_MULTIPLIER:
            try:
                val = float(num_str.replace(",", "")) * _PRICE_UNIT_MULTIPLIER[unit]
            except ValueError:
                val = None
            claims.append((display, val))
        else:  # %/bp/포인트 — 가격과 직접 비교 불가, 항상 미확인
            claims.append((display, None))

    for m in _UNIT_PREFIX_RE.finditer(sentence):
        s, e = m.span()
        if _taken(s, e):
            continue
        spans.append((s, e))
        num_str = m.group(1)
        try:
            val = float(num_str.replace(",", ""))
        except ValueError:
            val = None
        claims.append((f"${num_str}", val))

    for m in _UNIT_P_RE.finditer(sentence):
        s, e = m.span()
        if _taken(s, e):
            continue
        spans.append((s, e))
        claims.append((f"{m.group(1)}p", None))

    price_word_spans = [pm.span() for pm in _PRICE_WORD_RE.finditer(sentence)]
    if price_word_spans:
        for m in _BARE_NUMBER_RE.finditer(sentence):
            s, e = m.span()
            if _taken(s, e):
                continue
            num_str = m.group(0)
            if _sig_digits(num_str) < 3:
                continue  # 날짜·작은 정수(2026, 9, 4, -6 등) 배제
            near = any(
                abs(s - pe) <= _PRICE_CONTEXT_WINDOW or abs(ps - e) <= _PRICE_CONTEXT_WINDOW
                for ps, pe in price_word_spans
            )
            if not near:
                continue
            spans.append((s, e))
            try:
                val = float(num_str.replace(",", ""))
            except ValueError:
                val = None
            claims.append((num_str, val))

    return claims


def _verify_price_value(claimed: float | None, symbol: str, quotes_lookup) -> str:
    if claimed is None or quotes_lookup is None:
        return "미확인"
    try:
        current = quotes_lookup(symbol)
    except Exception:  # noqa: BLE001 — 시세 조회 실패가 다이제스트를 막지 않는다
        return "미확인"
    if current is None or current == 0:
        return "미확인"
    ratio = claimed / current
    # 가격 범위(현재가의 0.5~2배) 밖의 숫자는 가격 주장이 아니다(시총·매출·거래량 등)
    # — "✗"(틀린 가격) 로 낙인찍지 않고 "미확인" 으로 둔다. ✗ 는 가격처럼 보이는데
    # 2% 밖일 때만(낡았거나 틀린 가격) 준다.
    if not (PRICE_BAND_LO <= ratio <= PRICE_BAND_HI):
        return "미확인"
    return "✓" if abs(claimed - current) / abs(current) <= PRICE_TOLERANCE else "✗"


# ---------------------------------------------------------------------------
# 결정론 빌드
# ---------------------------------------------------------------------------


def build_digest(
    messages: list[dict],
    market: str,
    now: datetime,
    *,
    since: datetime | None = None,
    name_table: list[tuple[str, str]] | None = None,
    quotes_lookup: Callable[[str], float | None] | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    stance_llm_call: Callable[[str], dict | None] | None = None,
    regime: dict | None = None,
) -> Digest:
    """`messages`(`telegram_channels.load_window`가 돌려주는, 최신순 원장 행
    리스트)에서 `market`(KR/US) 다이제스트를 만든다.

    `name_table`은 `entities.load_table`(KR)/`entities.load_us_table`(US)와
    같은 `[(name, code), ...]` 형태 — 호출부가 캐시에서 읽어 주입한다(네트워크는
    이 함수 밖). 없으면(`None`/빈 리스트) 종목 후보·숫자 클레임은 비어 있는
    채로(리스크 키워드·채널별 원문은 그대로) 돌려준다 — 예외를 던지지 않는다.

    `regime`은 `data/state/regime.json`의 그 시장 sub-dict(호출부가 읽어
    주입, 모듈 docstring "프로그램 스탠스" 절) — `Digest.program_stance_display()`
    가 쓴다. `stance_llm_call`(선택)은 `llm_call`과 별개의, 스탠스 전용
    마이크로프롬프트 경로다(모듈 docstring "LLM 스탠스" 절) — `llm_call`이
    이미 유효한 스탠스를 만들었으면 건너뛴다.
    """
    relevant = {c["handle"] for c in channels_for(market)}
    by_channel: dict[str, list[dict]] = {h: [] for h in relevant}
    for m in messages:
        h = m.get("handle")
        if h in by_channel:
            by_channel[h].append(m)

    table = name_table or []

    channel_entries: dict[str, list[ChannelEntry]] = {}
    channel_notices: dict[str, str] = {}
    # symbol -> {"name", "sentence", "channels": [...], "messages": {(handle,msg_id), ...}}
    cand_data: dict[str, dict] = {}
    risk_items: list[RiskItem] = []
    seen_message_keywords: dict[tuple[str, str], set[str]] = {}
    number_claims: list[NumberClaim] = []
    seen_number_claims: set[tuple[str, str, str]] = set()

    for handle, rows in by_channel.items():
        if not rows:
            continue
        usable = [r for r in rows if (r.get("text") or "").strip()]
        if not usable:
            # fetch_all/`_parse_messages_with_reason`이 채널 단위로 남기는
            # error 사유는 원장 행 자체엔 없다(그 사이클의 진단일 뿐) — 그래도
            # "신규 메시지는 있는데 전부 빈 본문"은 여기서 조용히 넘기지
            # 않는다(coordinator 요구: clawnewssummary 의 text_not_supported
            # 처럼 프리뷰가 본문을 못 주는 채널을 명시적으로 드러낸다).
            channel_notices[handle] = "미리보기 없음"
            continue

        entries = [
            ChannelEntry(
                handle=handle,
                published_hhmm=_hhmm(r.get("published")),
                snippet=_snippet(r.get("text", "")),
                link=(f"https://t.me/{handle}/{r['msg_id']}" if r.get("msg_id") else None),
            )
            for r in usable[:CHANNEL_ITEM_CAP]
        ]
        channel_entries[handle] = entries

        for row in usable:
            msg_id = row.get("msg_id")
            msg_key = (handle, msg_id)
            # 보일러플레이트(수집 완료 꼬리·금일 발언 없음 반복 블록) 제거 후
            # 분석한다 — 리스크/후보/숫자 클레임 전부 이 정리된 텍스트를 쓴다
            # (모듈 docstring "리스크 항목" 절).
            clean_text = _strip_boilerplate(row.get("text") or "")
            seen_keywords_for_msg = seen_message_keywords.setdefault(msg_key, set())

            for sentence in _sentences(clean_text):
                # 리스크 키워드는 종목 언급과 무관하게 매 문장 검사한다 —
                # "미국 금리 발표 예정, 지정학 리스크 확대" 처럼 종목명이 아예
                # 없는 매크로 문장도 리스크 항목이어야 한다(실측, 2026-09-05
                # daegurr 표본). 한 문장에 서로 다른 키워드가 여럿이면(위
                # 예시처럼 금리+지정학) 둘 다 남긴다 — 서로 다른 리스크
                # 축이다. 다만 메시지당 같은 키워드는 한 번만(같은 메시지에서
                # 같은 주제가 여러 문장에 걸쳐 반복되는 실측 잡음 축소,
                # 소유자 지시 — aetherjapanresearch NBIM 기사 표본: "국채"가
                # 연속 4문장에서 반복됐다).
                for keyword in RISK_KEYWORDS:
                    if keyword in sentence and keyword not in seen_keywords_for_msg:
                        risk_items.append(RiskItem(keyword=keyword, handle=handle, sentence=sentence))
                        seen_keywords_for_msg.add(keyword)

                hits = _candidates_in_sentence(market, sentence, table)
                for hit in hits:
                    symbol = hit["symbol"]
                    d = cand_data.setdefault(
                        symbol, {"name": hit.get("name"), "sentence": sentence, "channels": [], "messages": set()},
                    )
                    if handle not in d["channels"]:
                        d["channels"].append(handle)
                    d["messages"].add(msg_key)

                    for value, price_val in _number_claims_in_sentence(sentence):
                        key = (symbol, value, handle)
                        if key in seen_number_claims:
                            continue
                        seen_number_claims.add(key)
                        number_claims.append(NumberClaim(
                            symbol=symbol, value=value, handle=handle, sentence=sentence,
                            status=_verify_price_value(price_val, symbol, quotes_lookup),
                        ))

    ranked_candidates = [
        Candidate(
            symbol=sym, name=d["name"], sentence=d["sentence"],
            channels=tuple(d["channels"]), message_count=len(d["messages"]),
        )
        for sym, d in cand_data.items()
    ]
    ranked_candidates.sort(key=lambda c: (-c.message_count, c.symbol))

    cands = tuple(
        c.symbol for c in ranked_candidates if c.message_count >= CANDS_MIN_MESSAGES
    )[:CANDS_MAX_SYMBOLS]

    digest = Digest(
        market=market, since=since or now, until=now,
        channel_entries=channel_entries, channel_notices=channel_notices,
        candidates=ranked_candidates, cands=cands,
        risk_items=risk_items, number_claims=number_claims,
        regime=regime,
    )

    if llm_call is not None and channel_entries:
        digest = _apply_llm(digest, messages, llm_call)

    # 스탠스 전용 마이크로프롬프트(모듈 docstring "LLM 스탠스" 절) — 위
    # llm_call 이 이미 유효한 스탠스를 만들었으면 건너뛴다(중복 호출 방지,
    # 하위호환: 기존에 [STANCE] 마커로 성공하던 호출부는 그대로 그 결과를 쓴다).
    if stance_llm_call is not None and channel_entries and digest.stance is None:
        digest = _apply_llm_stance(digest, stance_llm_call)

    return digest


# ---------------------------------------------------------------------------
# LLM 서술(선택, all-or-nothing) — narrator.py/telegram_view.narrate_channels 관례
# ---------------------------------------------------------------------------

_MARKET_LABEL = {"KR": "한국", "US": "미국"}


def _llm_prompt(digest: Digest) -> str:
    label = _MARKET_LABEL.get(digest.market, digest.market)
    lines = [
        f"다음은 텔레그램 공개 채널에서 수집한 {label} 시장 관련 최근 메시지다.",
        "제시된 메시지 내용만 근거로 답하고, 새 사실을 지어내지 마라. 숫자는",
        "메시지에 등장한 것만 그대로(자릿수·부호까지) 써라. 매수/매도 지시나",
        "단정적 추천은 하지 마라.",
        "",
        "다음 형식으로 정확히 답하라(마커 그대로, 다른 텍스트 없이):",
        "[STANCE]",
        "방어 또는 중립 또는 공격 — 이유 한 문장",
        "[SUMMARY]",
        "방별 동향 3~5줄",
    ]
    if digest.candidates:
        lines.append("[CANDIDATES]")
        lines.append("각 줄: <종목코드>: <한 줄 이유>")
    lines.append("")
    lines.append("[메시지]")
    for handle, entries in digest.channel_entries.items():
        for e in entries:
            lines.append(f"[{handle}] {e.snippet}")
    if digest.candidates:
        lines.append("")
        lines.append("[후보 종목]")
        for c in digest.candidates[:CANDIDATE_DISPLAY_CAP]:
            lines.append(f"{c.symbol} ({c.name or c.symbol}): {c.sentence}")
    return "\n".join(lines)


def _parse_llm_response(text: str, has_candidates: bool) -> dict | None:
    text = text.strip()
    markers = ["[STANCE]", "[SUMMARY]"] + (["[CANDIDATES]"] if has_candidates else [])
    positions: list[tuple[int, str]] = []
    for marker in markers:
        idx = text.find(marker)
        if idx == -1:
            return None
        positions.append((idx, marker))
    positions.sort()

    blocks: dict[str, str] = {}
    for i, (idx, marker) in enumerate(positions):
        start = idx + len(marker)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        content = text[start:end].strip()
        if not content:
            return None
        blocks[marker] = content

    stance_line = blocks["[STANCE]"].splitlines()[0]
    stance = next((s for s in STANCES if s in stance_line), None)
    if stance is None:
        return None
    why = stance_line.split("—", 1)[1].strip() if "—" in stance_line else stance_line

    reasons: dict[str, str] = {}
    if has_candidates:
        for line in blocks["[CANDIDATES]"].splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            symbol, reason = line.split(":", 1)
            symbol = symbol.strip()
            if symbol:
                reasons[symbol] = reason.strip()

    return {"stance": stance, "why": why, "summary": blocks["[SUMMARY]"], "reasons": reasons}


def _apply_llm(digest: Digest, messages: list[dict], llm_call) -> Digest:
    prompt = _llm_prompt(digest)
    try:
        text = llm_call(prompt)
    except Exception:  # noqa: BLE001 — LLM 실패가 다이제스트를 막지 않는다
        return digest
    if not text:
        return digest

    parsed = _parse_llm_response(text, has_candidates=bool(digest.candidates))
    if parsed is None:
        return digest

    combined = f"{parsed['why']}\n{parsed['summary']}\n" + "\n".join(parsed["reasons"].values())
    facts = {"messages": [m.get("text") for m in messages if m.get("text")]}
    if not verify_numbers(combined, facts):
        return digest

    return Digest(
        market=digest.market, since=digest.since, until=digest.until,
        channel_entries=digest.channel_entries, channel_notices=digest.channel_notices,
        candidates=digest.candidates, cands=digest.cands, risk_items=digest.risk_items,
        number_claims=digest.number_claims, regime=digest.regime,
        stance=parsed["stance"], stance_why=parsed["why"], room_summary=parsed["summary"],
        candidate_reasons=parsed["reasons"], llm_used=True,
    )


# ---------------------------------------------------------------------------
# 스탠스 전용 마이크로프롬프트(선택, 모듈 docstring "LLM 스탠스" 절) — `llm_call`
# 의 [STANCE] 마커가 실패했을 때(실측: 거의 항상)만 이걸로 보강한다.
# `stance_llm_call`은 이미 엄격 검증된 `{"stance","why"}` dict|None을 돌려주는
# 계약이다(실제 OpenRouter 호출·재시도·모델 폴백은 `quant.adapters.narrate.
# stance_only`가 담당 — 이 모듈은 프롬프트만 만들고 네트워크를 모른다).
# ---------------------------------------------------------------------------


def _stance_prompt(digest: Digest) -> str:
    label = _MARKET_LABEL.get(digest.market, digest.market)
    lines = [
        f"다음은 텔레그램 공개 채널에서 수집한 {label} 시장 관련 최근 메시지다.",
        "제시된 메시지 내용만 근거로 오늘의 시장 스탠스를 판단하라. 새 사실을",
        "지어내지 말고, 숫자를 인용하지 마라.",
        "",
        "다음 JSON 객체 하나만 답하라(다른 텍스트·설명·마크다운 코드펜스 금지):",
        '{"stance":"방어 또는 중립 또는 공격 중 하나","why":"60자 이내 이유"}',
        "",
        "[메시지]",
    ]
    for handle, entries in digest.channel_entries.items():
        for e in entries:
            lines.append(f"[{handle}] {e.snippet}")
    return "\n".join(lines)


def _apply_llm_stance(digest: Digest, stance_llm_call) -> Digest:
    try:
        result = stance_llm_call(_stance_prompt(digest))
    except Exception:  # noqa: BLE001 — 스탠스 마이크로프롬프트 실패가 다이제스트를 막지 않는다
        return digest
    if not isinstance(result, dict):
        return digest
    stance = result.get("stance")
    why = result.get("why")
    # `stance_only`가 이미 이 두 조건을 검증하지만, 다른 콜러블이 주입될 수도
    # 있으므로(테스트 등) 여기서도 한 번 더 확인한다 — "절반만 믿을 수 있는
    # 판정은 안 믿느니만 못하다"(모듈 docstring 원칙).
    if stance not in STANCES or not isinstance(why, str) or not why:
        return digest
    return Digest(
        market=digest.market, since=digest.since, until=digest.until,
        channel_entries=digest.channel_entries, channel_notices=digest.channel_notices,
        candidates=digest.candidates, cands=digest.cands, risk_items=digest.risk_items,
        number_claims=digest.number_claims, regime=digest.regime,
        stance=stance, stance_why=why,
        room_summary=digest.room_summary, candidate_reasons=digest.candidate_reasons,
        llm_used=digest.llm_used,
    )


# ---------------------------------------------------------------------------
# 텔레그램 렌더링
# ---------------------------------------------------------------------------


def _render_capped(header: str, sections: list[str], footer: str | None, limit: int = DIGEST_MAX_CHARS) -> str:
    text = tgfmt.compose(header, sections, footer)
    if len(text) <= limit:
        return text
    kept = list(sections)
    while kept:
        kept.pop()
        text = tgfmt.compose(header, kept, footer)
        if len(text) <= limit:
            return text
    text = tgfmt.compose(header, [], footer)
    return text if len(text) <= limit else text[:limit]


def render_telegram(digest: Digest, report_url: str | None = None) -> str:
    """다이제스트 → 텔레그램 HTML 메시지(≤`DIGEST_MAX_CHARS`, tgfmt)."""
    label = _MARKET_LABEL.get(digest.market, digest.market)
    since_hhmm = digest.since.astimezone(KST).strftime("%H:%M")
    until_hhmm = digest.until.astimezone(KST).strftime("%H:%M")
    header = tgfmt.b(f"📡 텔레그램 다이제스트 — {label} {since_hhmm}~{until_hhmm} KST")

    if not digest.has_content() and not digest.channel_notices:
        return tgfmt.compose(header, [tgfmt.esc("새 메시지 없음")])

    sections: list[str] = []

    # 스탠스는 늘 보인다 — 결정론 프로그램 스탠스(a, 항상 첫 줄, LLM 무관)가
    # 먼저, 그 아래 LLM 스탠스(b, 선택 — 성공이면 실제 판정, 아니면 정직한
    # 미가용 표시 + 리스크 태그 집계)가 둘째 줄이다(소유자 지시, 섹션을
    # 생략하지 않는다, 2026-09-05 "유료 레인 불필요" 결정).
    stance_lines = [digest.program_stance_display(), digest.stance_display()]
    sections.append(f"{tgfmt.b('[스탠스]')}\n" + tgfmt.esc("\n".join(stance_lines)))

    if digest.room_summary:
        sections.append(f"{tgfmt.b('[방별 동향]')}\n{tgfmt.esc(digest.room_summary)}")

    if digest.candidates:
        lines = []
        for c in digest.candidates[:CANDIDATE_DISPLAY_CAP]:
            reason = digest.candidate_reasons.get(c.symbol)
            chs = "·".join(c.channels)
            tail = reason if reason else f"언급 {c.message_count}건: {chs}"
            name = f" {c.name}" if c.name else ""
            lines.append(f"{c.symbol}{name} — {tail}")
        sections.append(f"{tgfmt.b('[관심 후보]')}\n" + tgfmt.esc("\n".join(lines)))

    if digest.risk_items:
        # 태그로 묶고, 그룹 안에서는 채널명을 한 번만 보여준다(소유자 지시) —
        # `Digest.risk_groups()` 하나를 리포트 템플릿과 공유한다.
        lines = [f"[{keyword}] {text}" for keyword, text in digest.risk_groups()]
        sections.append(f"{tgfmt.b('[리스크 항목]')}\n" + tgfmt.esc("\n".join(lines)))

    if digest.number_claims:
        lines = [f"{n.symbol} {n.value} ({n.handle}) — {n.status} 채널 주장"
                 for n in digest.number_claims[:NUMBER_DISPLAY_CAP]]
        sections.append(f"{tgfmt.b('[숫자 검증]')}\n" + tgfmt.esc("\n".join(lines)))
    else:
        sections.append(f"{tgfmt.b('[숫자 검증]')}\n" + tgfmt.esc("검증 가능한 수치 주장 없음"))

    if digest.channel_entries or digest.channel_notices:
        raw_lines = []
        for handle, entries in digest.channel_entries.items():
            raw_lines.append(f"[{handle}]")
            for e in entries:
                hhmm = e.published_hhmm or "시각 미상"
                raw_lines.append(f"{hhmm} {e.snippet}" + (f" {e.link}" if e.link else ""))
        for handle, notice in digest.channel_notices.items():
            raw_lines.append(f"[{handle}] {notice}")
        sections.append(f"{tgfmt.b('[원문 발췌]')}\n" + tgfmt.quote("\n".join(raw_lines), expandable=True))

    footer = tgfmt.i("⚠️ 채널 발 숫자·주장은 검증 전 참고용입니다 — ✓ 표시만 우리 시세로 확인됨.")
    if report_url:
        footer += "\n" + tgfmt.link("전체 리포트", report_url)

    return _render_capped(header, sections, footer)


# ---------------------------------------------------------------------------
# 스탠스 영속화(참고용, 엔진 미독) — 모듈 docstring 참고
# ---------------------------------------------------------------------------


def persist_stance(path: Path, digest: Digest) -> None:
    """다이제스트의 스탠스만 참고용으로 남긴다 — `quant.trade`는 이 파일을
    읽지 않는다(모듈 docstring 참고). LLM 게이트가 꺼져 있어(`--no-narrate` 등)
    `stance`가 `None`이면 파일을 건드리지 않는다 — 마지막으로 유효했던 스탠스를
    빈 값으로 덮어쓰지 않는다."""
    if digest.stance is None:
        return
    payload = {
        "market": digest.market,
        "stance": digest.stance,
        "why": digest.stance_why,
        "at": digest.until.isoformat(),
        "sources": sorted(digest.channel_entries.keys()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
