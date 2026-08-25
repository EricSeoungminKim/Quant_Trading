"""뉴스에서 상장 종목을 뽑는다.

**순진한 substring 매칭은 틀린다** — 실측(2026-08-12)에서 "삼성전자, SK하이닉스
나란히 신고가... 코스맥스 실적 호조"에 대해 단순 매칭이 '이닉스'(SK하이**닉스**의
조각)와 '스맥'(코**스맥**스의 조각)을 뽑고 진짜 종목은 하나도 못 잡았다. 게다가
2글자 이하 종목명이 213개(태양·성우·유신…)라 "태양광 산업"에서 '태양'이 잡히면
언급 원장이 통째로 오염된다. 경계 검사 + 최장 우선 매칭이 필수다.
"""
from __future__ import annotations

import logging
import html
import io
import re
from pathlib import Path

import pandas as pd

from quant.collect.listed_companies import (
    fetch_kind_corp_list,
    fetch_sp500_list,
    fetch_symbol_dir,
)

logger = logging.getLogger(__name__)

MIN_NAME_LEN = 3
TRADABLE = ("유가", "코스닥")  # 코넥스 제외
_JOSA = "가는은이을를의에와과도만로으로부터까지에서"

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_CODE = re.compile(r"^\d{6}$")
_WORD = re.compile(r"[가-힣A-Za-z0-9]")
_HANGUL = re.compile(r"[가-힣]")


def parse_corp_list(raw: bytes) -> list[tuple[str, str, str]]:
    """(회사명, 종목코드, 시장구분). KIND 다운로드는 euc-kr HTML 표다."""
    text = raw.decode("euc-kr", "replace")
    out = []
    for tr in _TR.findall(text)[1:]:
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        if len(cells) >= 3 and _CODE.match(cells[2]) and cells[1] in TRADABLE:
            out.append((cells[0], cells[2], cells[1]))
    if not out:
        raise ValueError("상장법인목록 파싱 0건 — KIND 표 구조 변경 의심")
    return out


def build_table(
    recs: list[tuple[str, str, str]], min_len: int = MIN_NAME_LEN
) -> list[tuple[str, str]]:
    """긴 이름 우선 정렬 — 최장 매칭이 짧은 조각을 선점한다."""
    return sorted(
        ((n, c) for n, c, _ in recs if len(n) >= min_len), key=lambda x: -len(x[0])
    )


def extract(text: str, table: list[tuple[str, str]]) -> list[dict]:
    hits: list[dict] = []
    spans: list[tuple[int, int]] = []
    for name, code in table:
        for m in re.finditer(re.escape(name), text):
            s, e = m.span()
            if any(s < je and e > js for js, je in spans):
                continue  # 더 긴 이름이 이미 차지한 구간
            before = text[s - 1] if s else " "
            after = text[e] if e < len(text) else " "
            if _WORD.match(before):
                continue  # 앞이 글자 → 조각
            if _HANGUL.match(after) and after not in _JOSA:
                continue  # 뒤가 조사 아닌 한글 → 조각
            spans.append((s, e))
            hits.append({"name": name, "symbol": code})
            break
    return hits


def _load_records(cache_dir: Path) -> list[tuple[str, str, str]]:
    """캐시 파일을 읽어 원본 레코드를 반환한다. 캐시가 없으면 collect 가 KIND 에서
    받아 저장한다 — load_table/load_market_map 이 이 캐시 파일을 공유해 중복
    다운로드를 막는다."""
    raw = fetch_kind_corp_list(cache_dir / "kind_corplist.html")
    return parse_corp_list(raw)


def load_table(cache_dir: Path) -> list[tuple[str, str]]:
    """뉴스→종목 매칭 사전. 하루 1회 받아 캐시한다(상장사 목록은 자주 안 바뀐다).

    KIND 가 죽으면 DART 로 폴백한다(2026-08-25). 이름 사전만 고치면 리포트는
    나오지만 **뉴스에서 종목을 하나도 못 잡아** 후보 퍼널이 통째로 빈다
    (실측: 후보 2개, 평소 50~130개). `build_table` 의 `min_len` 필터는 그대로
    통과시킨다 — 폴백이라고 매칭 규율을 느슨하게 하지 않는다."""
    try:
        recs = _load_records(cache_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("KIND 매칭 사전 실패 — DART 공시 법인목록으로 폴백: %s: %s",
                       type(e).__name__, e)
        # 시장구분은 DART 에 없다. 이 함수는 (이름, 코드)만 쓰므로 빈 문자열로
        # 채우되, 시장구분이 필요한 `load_market_map` 은 이 폴백을 쓰지 않는다.
        recs = [(name, code, "") for code, name in _dart_name_map(cache_dir).items()]
    return build_table(recs)


_YAHOO_SUFFIX = {"유가": ".KS", "코스닥": ".KQ"}


def build_market_map(recs: list[tuple[str, str, str]]) -> dict[str, str]:
    """종목코드 → 야후 심볼. 유가=.KS, 코스닥=.KQ"""
    return {
        code: code + _YAHOO_SUFFIX[market]
        for _, code, market in recs
        if market in _YAHOO_SUFFIX
    }


def load_market_map(cache_dir: Path) -> dict[str, str]:
    """load_table 과 같은 캐시 파일을 재사용한다 (재다운로드 금지)."""
    return build_market_map(_load_records(cache_dir))


# ---------------------------------------------------------------------------
# US (S&P500) — 한국어 매칭과 문제가 다르다. 한글은 공백 분리가 명확한 대신
# 조사가 붙지만, 영문은 공백 분리가 깨끗한 대신 **티커가 일반 단어와 충돌한다**
# ("IT spending" 의 IT 를 Gartner 로 잡는 식). 그래서 방어 규칙이 다르다:
# 경계 검사 대신 (1) 대문자만 티커로 인정 (2) 3글자 미만 제외 (3) 흔한 단어와
# 겹치는 티커는 하드코딩 제외목록으로 막는다.
# ---------------------------------------------------------------------------

MIN_TICKER_LEN = 3

# 실제 S&P500 티커(2026-08-12 기준)와 대조해, 흔한 영단어와 겹치는 것만 남긴
# 제외목록이다. 예: ALL(Allstate)이 "ALL of the gains" 의 ALL 을 오탐하는 식.
# 겹치지 않는 후보(ONE, ARE 아닌 CAN/NEW/OUT 등)는 애초에 위험이 없어 뺐다.
COMMON_WORD_TICKERS = {
    "ALL", "ARE", "FAST", "HAS", "KEY", "LOW", "NOW", "TECH", "WELL",
    # 2026-08-13 사전을 S&P500(503) → 미국 상장 전체(13,135)로 넓히면서 실측한
    # 오탐. 전부 **금융 약어가 티커와 충돌**하는 형태다 — 확장 전에는 해당 티커가
    # 사전에 없어 드러나지 않았다.
    #   COLA ← "cost-of-living adjustment"(물가 기사)  / Columbus Acquisition
    #   DEI  ← "diversity, equity, inclusion"           / Douglas Emmett
    #   WTI  ← 서부텍사스유                              / W&T Offshore
    "COLA", "DEI", "WTI",
    # 같은 부류로 예상되는 약어를 선제적으로 막는다. 이 중 실제 티커인 것만
    # 효과가 있고 나머지는 무해하다(사전에 없으면 애초에 매칭되지 않는다).
    "CPI", "PPI", "GDP", "PCE", "FED", "FOMC", "SEC", "IRS", "ETF", "IPO",
    "CEO", "CFO", "COO", "EPS", "ROE", "ROI", "AUM", "GAAP", "OPEC", "NATO",
    "USD", "EUR", "JPY", "KRW", "ESG", "IRA", "AI",
}

# 회사명 매칭도 방어가 필요하다. 실측 오탐(2026-08-12, 미국 원문 97건 중 4건):
#   "price target"     → Target(TGT)      대소문자 무시 매칭 탓
#   "brings good news" → News Corp(NWSA)  접미사 제거가 'News'를 남김
#   "$3M offering"     → 3M(MMM)          2글자 이름
#   "Dow Dips"         → Dow Inc(DOW)     다우존스 '지수'와 충돌
# 방어 셋: ① 회사명도 대소문자 구분(고유명사는 대문자로 시작한다)
#          ② 3글자 미만 이름 제외  ③ 지수·일반어와 겹치는 이름 제외목록
MIN_NAME_LEN = 3
# 대문자로 써도 여전히 애매한 이름들. 'Dow' 는 다우존스 지수를 가리키는 경우가
# 압도적이고, 'News'/'Target'/'Gap' 은 문장 첫머리에서 대문자가 되어 뚫린다.
AMBIGUOUS_NAMES = {"Dow", "News", "Target", "Gap"}

# Security 컬럼의 법인격 접미사를 벗겨 기본형으로 만든다. "Alphabet Inc.
# (Class A)" 처럼 괄호+접미사가 겹칠 수 있어 안정될 때까지 반복 적용한다.
_PAREN_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")
_AMP_CO_SUFFIX_RE = re.compile(r"\s*&\s*Co\.?\s*$", re.IGNORECASE)
#  \b 로 앞 경계를 걸어야 한다 — 안 그러면 "KeyCorp"(KeyCorp 은행) 같은 붙여쓴
# 사명에서 "Corp" 조각을 잘라 "Key"로 오염시킨다("y"→"C"는 둘 다 단어문자라
# \b 없이는 경계 취급을 못 받는다).
_CORP_SUFFIX_RE = re.compile(
    r"[,\s]*\b(?:Inc|Incorporated|Corp|Corporation|Co|Company|plc|Ltd|N\.V\.|SE)\.?\s*$",
    re.IGNORECASE,
)


def _clean_us_name(name: str) -> str:
    prev = None
    while prev != name:
        prev = name
        name = _PAREN_SUFFIX_RE.sub("", name)
        name = _AMP_CO_SUFFIX_RE.sub("", name)
        name = _CORP_SUFFIX_RE.sub("", name)
    return name.strip()


def parse_us_list(html_text: str) -> list[tuple[str, str]]:
    """위키피디아 S&P500 표에서 (회사명, 티커)를 뽑는다."""
    table = pd.read_html(io.StringIO(html_text))[0]
    if "Symbol" not in table.columns or "Security" not in table.columns:
        raise ValueError("S&P500 표 파싱 실패 — Symbol/Security 컬럼 없음")
    return list(zip(table["Security"].astype(str), table["Symbol"].astype(str)))


def build_us_table(recs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """회사명 접미사를 벗긴 (기본형 회사명, 티커) 목록."""
    return [(_clean_us_name(name), symbol) for name, symbol in recs]


# 후보 목록은 표 하나당 한 번만 만든다. 제목마다 26,000개를 만들고 정렬하던
# 것이 확장 사전에서 병목이었다. 표(list)는 해시 불가라 id 로 캐시하되, 표 자체를
# 함께 들고 있어 id 재사용을 막고 동일성까지 확인한다.
_US_CAND_CACHE: dict[int, tuple[list, list]] = {}


def _us_candidates(table: list[tuple[str, str]]) -> list[tuple[str, str, bool, str, str]]:
    """(매칭문자열, 심볼, 티커여부, 표시명, 매칭문자열 소문자) — 긴 것 우선."""
    key = id(table)
    cached = _US_CAND_CACHE.get(key)
    if cached is not None and cached[0] is table:
        return cached[1]

    out: list[tuple[str, str, bool, str, str]] = []
    for name, symbol in table:
        if name and len(name) >= MIN_NAME_LEN and name not in AMBIGUOUS_NAMES:
            out.append((name, symbol, False, name, name.lower()))
        if len(symbol) >= MIN_TICKER_LEN and symbol not in COMMON_WORD_TICKERS:
            out.append((symbol, symbol, True, name, symbol.lower()))
    out.sort(key=lambda c: -len(c[0]))

    _US_CAND_CACHE.clear()  # 표는 실행당 한두 개뿐이다 — 무한정 쌓지 않는다
    _US_CAND_CACHE[key] = (table, out)
    return out


def extract_us(text: str, table: list[tuple[str, str]]) -> list[dict]:
    """영문 뉴스 제목에서 S&P500 종목을 뽑는다.

    **회사명·티커 모두 대소문자를 구분한다.** 소문자 aapl 은 티커로 보지 않고,
    소문자 target 도 Target 으로 보지 않는다 — 영문 일반 텍스트에서 회사명과
    같은 철자의 보통명사가 흔해 오탐이 크다(실측: 97건 중 4건이 그런 오탐이었다).
    3글자 미만 티커·이름, `COMMON_WORD_TICKERS`, `AMBIGUOUS_NAMES` 는 제외한다.
    최장 매칭 우선 + 이미 매칭된 구간/심볼 재사용 금지.
    """
    candidates = _us_candidates(table)

    low = text.lower()
    hits: list[dict] = []
    spans: list[tuple[int, int]] = []
    seen_symbols: set[str] = set()
    for matched, symbol, is_ticker, display_name, matched_low in candidates:
        if symbol in seen_symbols:
            continue
        # 값싼 사전 필터. `\bmatched\b` 를 IGNORECASE 로 찾는 이상 matched 의
        # 소문자형이 제목에 부분문자열로 없으면 절대 매칭될 수 없다 — 의미는
        # 그대로 두고 정규식 실행을 건너뛴다. 사전을 503→13,135 로 넓히면서
        # 이 필터 없이는 제목 300건에 103초가 들었다(2026-08-13 실측).
        if matched_low not in low:
            continue
        # 대소문자 완전 구분은 과하다 — 헤드라인은 'NVIDIA' 처럼 전부 대문자로
        # 쓰기도 해서 사전의 'Nvidia' 와 안 맞는다. 규칙은 **매칭된 문자열이
        # 전부 소문자면 거부**다. 영문에서 고유명사를 전부 소문자로 쓰는 일은
        # 드물지만 보통명사('price target', 'good news')는 그렇게 쓴다.
        pattern = re.compile(rf"\b{re.escape(matched)}\b", re.IGNORECASE)
        for m in pattern.finditer(text):
            s, e = m.span()
            if any(s < je and e > js for js, je in spans):
                continue  # 더 긴 매칭이 이미 차지한 구간
            hit_text = m.group(0)
            if hit_text.islower():
                continue  # 보통명사로 쓰인 것이다 (price target / good news)
            if is_ticker and hit_text != symbol:
                continue  # 티커는 대문자 원형만 인정한다 (aapl 은 티커가 아니다)
            spans.append((s, e))
            hits.append({"name": display_name, "symbol": symbol})
            seen_symbols.add(symbol)
            break
    return hits


# --- 미국 상장 전체 목록 (S&P500 밖 종목을 잡기 위한 확장) -------------------
# 왜 필요한가(2026-08-13 실측): 추출 사전이 S&P500 503종목뿐이라, 그 밖의 종목은
# 뉴스에 아무리 나와도 잡히지 않았다. CoreWeave 는 하루치 헤드라인에 6번 등장하는데
# 사전에 없어 "뉴스 노출 상위 종목"에 영영 못 올라왔다. SpaceX 도 마찬가지다
# (다만 SpaceX 는 비상장이라 티커 자체가 없어 어떤 사전으로도 해결되지 않는다).
#
# 나스닥이 공개하는 공식 심볼 디렉터리를 쓴다 — 무료, 키 불필요, 파이프 구분 텍스트.
US_SYMBOL_DIR_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)

# 나스닥 목록의 이름은 "NVIDIA Corporation - Common Stock" 처럼 **증권 종류**가
# 붙어 있다. 이걸 안 벗기면 회사명 매칭이 통째로 실패한다(티커 매칭만 남는다).
_SECURITY_CLASS_RE = re.compile(
    r"\s*-\s*(?:Class\s+\w+\s+)?"
    r"(?:Common\s+Stock|Ordinary\s+Shares?|American\s+Depositary\s+Shares?"
    r"|Depositary\s+Shares?|Preferred\s+Stock|Warrants?|Units?|Rights?"
    r"|Subordinate\s+Voting\s+Shares?|Limited\s+Partnership).*$",
    re.IGNORECASE,
)
_TRAILING_CLASS_RE = re.compile(
    r"\s+(?:Common\s+Stock|Ordinary\s+Shares?|Class\s+\w)\s*$", re.IGNORECASE
)


def clean_listed_name(name: str) -> str:
    """나스닥 심볼 디렉터리의 Security Name → 기본형 회사명."""
    out = _SECURITY_CLASS_RE.sub("", name)
    out = _TRAILING_CLASS_RE.sub("", out)
    return _clean_us_name(out)


def parse_symbol_dir(text: str) -> list[tuple[str, str]]:
    """파이프 구분 심볼 디렉터리 → (회사명, 티커).

    테스트 종목(Test Issue=Y)과 파일 끝의 "File Creation Time" 줄은 뺀다.
    """
    lines = text.splitlines()
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split("|")]
    try:
        sym_i = next(i for i, h in enumerate(header) if h in ("Symbol", "ACT Symbol"))
        name_i = header.index("Security Name")
    except (StopIteration, ValueError):
        raise ValueError("심볼 디렉터리 형식 변경 — Symbol/Security Name 컬럼 없음")
    test_i = header.index("Test Issue") if "Test Issue" in header else None

    out: list[tuple[str, str]] = []
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) <= max(sym_i, name_i) or parts[0].startswith("File Creation"):
            continue
        if test_i is not None and len(parts) > test_i and parts[test_i].strip() == "Y":
            continue
        sym, raw = parts[sym_i].strip(), parts[name_i].strip()
        if sym and raw:
            out.append((clean_listed_name(raw), sym))
    if not out:
        raise ValueError("심볼 디렉터리 파싱 0건")
    return out


def _load_listed_records(cache_dir: Path) -> list[tuple[str, str]]:
    """하루 1회 받아 캐시한다(S&P500 캐시와 같은 구조)."""
    out: list[tuple[str, str]] = []
    for url in US_SYMBOL_DIR_URLS:
        cache = cache_dir / f"symdir_{url.rsplit('/', 1)[-1]}"
        text = fetch_symbol_dir(url, cache)
        out.extend(parse_symbol_dir(text))
    return out


def _load_us_records(cache_dir: Path) -> list[tuple[str, str]]:
    """캐시 파일을 읽어 원본 (회사명, 티커)를 반환한다. KR 쪽 `_load_records` 와
    같은 구조 — `load_us_table` 과 `load_name_map` 이 이 캐시를 공유한다."""
    text = fetch_sp500_list(cache_dir / "sp500_list.html")
    return parse_us_list(text)


def load_us_table(cache_dir: Path) -> list[tuple[str, str]]:
    """미국 종목 추출 사전 = S&P500 + 미국 상장 전체.

    **S&P500 이름이 우선**이다. 위키피디아 표의 이름("Nvidia")이 나스닥 디렉터리의
    이름("NVIDIA Corporation - Common Stock")보다 헤드라인 표기에 가깝다. 나스닥
    목록은 S&P500 밖 종목(CoreWeave·Nebius 등)을 메우는 용도다.

    디렉터리 조회가 실패하면 S&P500 만으로 계속한다 — 사전이 좁아질 뿐 리포트가
    죽지는 않는다.
    """
    table = build_us_table(_load_us_records(cache_dir))
    seen = {sym for _, sym in table}
    try:
        for name, sym in _load_listed_records(cache_dir):
            if sym not in seen:
                seen.add(sym)
                table.append((name, sym))
    except Exception as e:  # noqa: BLE001 — 사전 확장 실패가 리포트를 막지 않는다
        print(f"미국 상장 전체 목록 건너뜀 (S&P500 만 사용): {type(e).__name__}: {e}",
              file=__import__("sys").stderr)
    return table


def load_name_map(cache_dir: Path, market: str) -> dict[str, str]:
    """종목코드/티커 → 표시용 회사명.

    **왜 필요한가.** 토스 랭킹 API는 이름을 내려주지 않는다(`symbol`·`price`·
    `change_pct`·`rank`뿐) — 그래서 실시간 랭킹 표가 종목코드만 보여줬다.
    뉴스에서 잡힌 종목은 추출 과정에서 이름을 함께 얻지만 랭킹은 그 경로를 안 탄다.

    이름을 얻으려고 **네트워크를 더 쓰지 않는다** — 종목 추출이 이미 받아 캐시해 둔
    같은 파일(KIND 상장법인목록 / S&P500 목록)을 재사용한다.

    `build_table` 의 `min_len` 필터는 거치지 않는다: 그 필터는 뉴스 본문에서 짧은
    이름이 오탐을 내는 것을 막는 장치이고, 여기서는 **이미 확정된 코드**를 이름으로
    바꾸는 것뿐이라 오탐 위험이 없다.

    사전에 없는 심볼(ETF·리츠·신주인수권 등 상장법인목록 밖)은 **넣지 않는다** —
    호출부가 코드를 그대로 보여주게 둔다. 모르는 이름을 지어내지 않는다.
    """
    if market == "US":
        return {sym: name for name, sym in build_us_table(_load_us_records(cache_dir))}
    try:
        out = {code: name for name, code, _ in _load_records(cache_dir)}
    except Exception as e:  # noqa: BLE001
        # KIND 가 죽어도 리포트는 나와야 한다(2026-08-25 실측: KRX 403 차단으로
        # 아침 리포트가 사흘간 전멸). **이름 사전은 표시용 보조 데이터**지
        # 리포트의 전제가 아니다 — 다른 소비처 4곳은 이미 예외를 삼키고
        # "생략"으로 넘어가는데, 이 함수만 전파해 HTML 생성 직전에 죽였다.
        logger.warning("KIND 이름 사전 실패 — DART 공시 법인목록으로 폴백: %s: %s",
                       type(e).__name__, e)
        out = _dart_name_map(cache_dir)
    out.update(_preferred_share_names(out))
    return out


def _dart_name_map(cache_dir: Path) -> dict[str, str]:
    """DART 공시 법인목록 캐시(`dart_corp_codes.json`) → {종목코드: 회사명}.

    KIND 폴백 전용이다. 이 캐시는 `collect/sources/dart_financials.py` 가 이미
    매일 갱신하므로 **새 네트워크 의존이 생기지 않는다** — 있는 데이터를 재사용할
    뿐이다. 시장구분(유가/코스닥)은 DART 에 없어 못 채운다; 시장구분이 필요한
    `load_market_map` 은 이 폴백을 쓰지 않고 기존대로 실패한다(모르는 것을
    아는 척하지 않는다).

    캐시가 없거나 깨졌으면 **빈 사전**을 돌려준다 — 이름이 없으면 호출부가
    종목코드를 그대로 보여주면 되고, 그게 리포트가 죽는 것보다 낫다."""
    import json

    path = cache_dir / "dart_corp_codes.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("DART 폴백도 불가 — 이름 없이 진행(종목코드 표시): %s", e)
        return {}
    out: dict[str, str] = {}
    for r in rows if isinstance(rows, list) else []:
        code = (r.get("stock_code") or "").strip()
        name = (r.get("corp_name") or "").strip()
        if _CODE.match(code) and name:
            out.setdefault(code, name)
    logger.info("DART 폴백 이름 사전 %d건", len(out))
    return out


def _preferred_share_names(base: dict[str, str]) -> dict[str, str]:
    """구형 우선주 코드 → "{보통주명}우".

    KIND 상장법인목록은 **법인** 목록이라 우선주 종목코드가 없다. 그런데 토스
    랭킹에는 우선주가 자주 올라온다(2026-08-13 상승률 보드의 002995). KRX 관례상
    구형 우선주는 보통주 코드의 **끝자리 0을 5로 바꾼 코드**를 쓴다
    (금호건설 002990 → 금호건설우 002995).

    유추가 아니라 규약이지만, 그래도 **보통주가 사전에 실제로 있을 때만** 만든다 —
    끝자리가 5인 아무 코드에나 이름을 붙이지 않는다. 2우선주(끝자리 7)·신형우선주
    (끝에 K/L 등 문자)는 규약이 갈려 손대지 않는다.
    """
    return {
        code[:-1] + "5": name + "우"
        for code, name in base.items()
        # **6자리 숫자만.** 문자가 섞인 코드(0193L0 같은 신주인수권류)는 이 규약의
        # 대상이 아닌데 끝자리가 0이라 그냥 두면 걸려든다.
        if _CODE.match(code) and code.endswith("0") and code[:-1] + "5" not in base
    }


def make_symbol_resolver(market_code: str, cache_dir: "Path | None"):
    """symbol -> 회사명 resolver. 부채 상환(2026-08-24)으로 collect 의
    `_make_resolver`가 여기로 왔다 — 종목 사전(fuzzy 정제 로직 포함)은 분석
    평면의 소유물이고, 수집은 이 함수를 **주입받아** 쓴다(collect → analyze
    임포트 절단, tests/test_architecture.py KNOWN_DEBT 였던 엣지).

    cache_dir 이 없으면(테스트·기존 호환) 이름 해석 없이 None 만 돌려준다.
    테이블 로딩은 이 함수를 호출한 시점에 일어난다 — 호출부(collect 의 소스
    람다)가 스레드풀 안에서 부르므로 laziness 계약은 그대로다."""
    if cache_dir is None:
        return lambda symbol: None
    table = load_us_table(cache_dir) if market_code == "US" else load_table(cache_dir)
    lookup = {code: name for name, code in table}
    return lambda symbol: lookup.get(symbol)
