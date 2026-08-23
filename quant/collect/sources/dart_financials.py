"""DART OpenAPI 재무제표 — 장타(가치·배당) 팩터 전략의 전제 데이터 (2026-08-19).

`dart.py`는 공시 목록(`list.json`)만 본다. 여기는 그 옆에 붙는 **재무 수치**
축이다: ROE 계산에 필요한 자본총계·당기순이익, 부채비율(부채총계/자본총계),
BPS(자본총계/발행주식수). 세 개의 DART API를 순서대로 쓴다:

1. `corpCode.xml` — 종목코드(6자리) → DART 고유번호(corp_code, 8자리) 매핑.
   ZIP 안에 전체 상장·비상장 법인이 든 XML 하나가 있다(2026-08-19 실측
   30MB). **재무제표/주식총수 API는 종목코드가 아니라 corp_code로만 조회
   된다** — 이게 없으면 그다음 두 API를 부를 수 없다. 자주 바뀌지 않는
   데이터라 `data/cache/dart_corp_codes.json`에 캐시하고 재사용한다(캐시는
   순수 성능 최적화다 — `collector.py`의 feed_headers_cache와 같은 원칙,
   정확성을 캐시에 기대지 않는다. 없거나 깨졌으면 다시 받는다).
2. `fnlttSinglAcnt.json` — 단일회사 주요계정(재무상태표·손익계산서 핵심
   항목). 자본총계·부채총계·당기순이익(손실)을 여기서 뽑는다. 연결(CFS)과
   개별(OFS)이 같이 오면 **연결을 우선**한다(지주사 구조에서 개별 재무제표는
   자회사 실적을 반영하지 않아 실질을 왜곡한다).
3. `stockTotqySttus.json` — 주식의 총수 현황. BPS 분모(발행주식총수)는
   재무제표 계정이 아니라 여기서만 나온다. `se == "보통주"` 행의
   `istc_totqy`(발행주식의 총수)를 쓴다 — 우선주는 배당·잔여재산 청구권이
   보통주와 달라 표준 BPS 계산에서 제외한다.

**실측(2026-08-19, corp_code=00126380 삼성전자, bsns_year=2025,
reprt_code=11011)**: 세 API 모두 status "000"으로 정상 응답했고, 사업연도는
2025(뉴스 시점 기준 최신 사업보고서, `rcept_dt` 2026-03-10)까지 조회됐다 —
사업연도를 바꿔가며 과거 사업보고서를 그대로 다시 부를 수 있어(DART 공식
문서: "2015년 이후부터 정보제공") 소급 조회가 가능하다.

**필드 부재/오류를 0으로 위장하지 않는다.** `dart.py`의 status 관례를
그대로 따른다: "000"=정상, "013"=조회된 데이터 없음(에러 아님, 빈 리스트),
그 외(예: "020"=요청 제한, "010"=키 오류)는 에러 문자열로 드러낸다. 계정이
안 잡히면(예상 밖 재무제표 구조) 해당 값은 `None`으로 남긴다 — 0으로
채우면 "부채가 0인 회사"라는 거짓 사실이 만들어진다.

**PER/PBR/배당수익률은 이 모듈의 범위 밖이다.** PER은 이미 `naver_quant.py`
가 저장한다. PBR은 `naver_quant`의 시가총액과 이 모듈의 자본총계/BPS를
나중에 조인해 파생시킬 수 있어 별도 수집이 필요 없다. 배당수익률은 이번
작업 범위에 없다(사용자 지시: 필요한 계정만, 과도한 일반화 금지).

**키 값은 절대 로그/에러 메시지에 넣지 않는다** (dart.py와 같은 원칙).
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from quant.adapters.env import get_key
from quant.adapters.http import client

CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
FINANCIALS_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
SHARES_URL = "https://opendart.fss.or.kr/api/stockTotqySttus.json"

# fnlttSinglAcnt 응답의 fs_div. 연결(CFS)이 있으면 우선, 없으면 개별(OFS).
_FS_DIV_PRIORITY = ("CFS", "OFS")

# 재무상태표(BS) 계정명 — DART XBRL 표준 계정명 그대로(2026-08-19 실측,
# 005930 사업보고서). 당기순이익은 "당기순이익(손실)"이 표준 표기라
# 부호(흑자/적자)와 무관하게 이 라벨로 온다 — 접두어 매칭으로 변형에 대비한다.
_ACCOUNT_CAPITAL_TOTAL = "자본총계"
_ACCOUNT_LIABILITIES_TOTAL = "부채총계"
_ACCOUNT_NET_INCOME_PREFIX = "당기순이익"


# ---------------------------------------------------------------------------
# 1) corp_code 매핑 (corpCode.xml)
# ---------------------------------------------------------------------------


def parse_corp_codes(xml_bytes: bytes) -> list[dict]:
    """상장(종목코드 6자리) 법인만 남긴다. 비상장은 `stock_code`가 공백(" ")
    또는 빈 문자열이다(2026-08-19 실측)."""
    root = ElementTree.fromstring(xml_bytes)
    out: list[dict] = []
    for item in root.findall("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        if len(stock_code) != 6:
            continue
        out.append(
            {
                "corp_code": (item.findtext("corp_code") or "").strip(),
                "corp_name": (item.findtext("corp_name") or "").strip(),
                "stock_code": stock_code,
            }
        )
    return out


def _http_get_corp_code_zip(key: str) -> bytes:
    with client(timeout=30.0) as c:
        resp = c.get(CORP_CODE_URL, params={"crtfc_key": key})
        resp.raise_for_status()
    return resp.content


def fetch_corp_codes(api_key: str | None = None, getter=None) -> tuple[list[dict], str | None]:
    """corpCode.xml을 받아 상장 법인만 돌려준다. 응답은 ZIP(binary) — 안의
    XML 파일 하나를 풀어 파싱한다. 키가 없으면 네트워크를 타지 않는다
    (`dart.fetch_disclosures`와 같은 관례)."""
    key = api_key if api_key is not None else get_key("DART_API_KEY")
    if not key:
        return [], "no key"

    get = getter or _http_get_corp_code_zip
    try:
        raw = get(key)
    except Exception as e:  # noqa: BLE001 — 네트워크 예외 삼키고 에러 문자열로(키 값 미포함)
        return [], f"{type(e).__name__}"

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            xml_bytes = z.read(z.namelist()[0])
        rows = parse_corp_codes(xml_bytes)
    except (zipfile.BadZipFile, ElementTree.ParseError, IndexError) as e:
        return [], f"corpCode 응답 파싱 실패: {type(e).__name__}"

    return rows, None


def load_corp_code_cache(path: Path) -> list[dict] | None:
    """캐시 읽기 실패는 `None`(캐시 없음과 동일 취급) — 호출부가 재수집한다.
    캐시는 정확성의 근거가 아니라 순수 성능 최적화다."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def save_corp_code_cache(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def build_stock_to_corp(rows: list[dict]) -> dict[str, dict]:
    """`stock_code` → {corp_code, corp_name}. 중복 종목코드는 먼저 만난 것을
    유지한다(corpCode.xml 안에서 실측상 종목코드 중복은 관측되지 않았지만,
    naver_sector.py의 "먼저 만난 값 유지" 관례를 그대로 따른다)."""
    out: dict[str, dict] = {}
    for row in rows:
        code = row.get("stock_code")
        if not code or code in out:
            continue
        out[code] = {"corp_code": row["corp_code"], "corp_name": row["corp_name"]}
    return out


def get_corp_code_map(
    root: Path, api_key: str | None = None, getter=None, force_refresh: bool = False
) -> tuple[dict[str, dict], str | None]:
    """캐시 우선, 없거나 `force_refresh`면 재수집 후 캐시 갱신."""
    cache_path = root / "data" / "cache" / "dart_corp_codes.json"
    if not force_refresh:
        cached = load_corp_code_cache(cache_path)
        if cached is not None:
            return build_stock_to_corp(cached), None

    rows, err = fetch_corp_codes(api_key=api_key, getter=getter)
    if err:
        return {}, err
    save_corp_code_cache(rows, cache_path)
    return build_stock_to_corp(rows), None


# ---------------------------------------------------------------------------
# 2) 재무제표 핵심 계정 (fnlttSinglAcnt)
# ---------------------------------------------------------------------------


def parse_financial_payload(payload: dict) -> tuple[list[dict], str | None]:
    """`dart.parse_disclosures`와 같은 status 관례 — "013"은 정당한
    "데이터 없음"(에러 아님), 그 외 실패는 에러 문자열로 드러낸다."""
    status = payload.get("status")
    if status == "013":
        return [], None
    if status != "000":
        message = payload.get("message", "")
        return [], f"{status} {message}".strip()
    return payload.get("list", []) or [], None


def _to_int(text: str | None) -> int | None:
    if not text or text in ("-", "N/A"):
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def extract_key_accounts(rows: list[dict]) -> dict:
    """`자본총계`/`부채총계`/`당기순이익(손실)` 을 뽑는다. 연결(CFS)이 있으면
    우선하고, 없으면 개별(OFS)로 폴백한다. 못 찾은 계정은 `None`(0 위장 금지).
    `rcept_no`(공시 접수번호)도 같이 돌려준다 — 첫 8자리가 공시된 날짜라
    "이 수치를 언제부터 알 수 있었나"의 근거가 된다(look-ahead 방지)."""
    by_fs: dict[str, dict[str, str]] = {}
    rcept_no: str | None = None
    for row in rows:
        if row.get("sj_div") not in ("BS", "IS"):
            continue
        fs_div = row.get("fs_div")
        if fs_div not in _FS_DIV_PRIORITY:
            continue
        rcept_no = rcept_no or row.get("rcept_no")
        account_nm = (row.get("account_nm") or "").strip()
        bucket = by_fs.setdefault(fs_div, {})
        if account_nm == _ACCOUNT_CAPITAL_TOTAL and "capital_total" not in bucket:
            bucket["capital_total"] = row.get("thstrm_amount")
        elif account_nm == _ACCOUNT_LIABILITIES_TOTAL and "liabilities_total" not in bucket:
            bucket["liabilities_total"] = row.get("thstrm_amount")
        elif account_nm.startswith(_ACCOUNT_NET_INCOME_PREFIX) and "net_income" not in bucket:
            bucket["net_income"] = row.get("thstrm_amount")

    for fs_div in _FS_DIV_PRIORITY:
        bucket = by_fs.get(fs_div)
        if bucket and {"capital_total", "liabilities_total", "net_income"} & bucket.keys():
            return {
                "fs_div": fs_div,
                "capital_total": _to_int(bucket.get("capital_total")),
                "liabilities_total": _to_int(bucket.get("liabilities_total")),
                "net_income": _to_int(bucket.get("net_income")),
                "rcept_no": rcept_no,
            }
    return {
        "fs_div": None,
        "capital_total": None,
        "liabilities_total": None,
        "net_income": None,
        "rcept_no": rcept_no,
    }


def _http_get_json(url: str, params: dict) -> dict:
    with client(timeout=10.0) as c:
        resp = c.get(url, params=params)
        resp.raise_for_status()
    return resp.json()


def fetch_financials(
    corp_code: str, bsns_year: str, reprt_code: str, api_key: str | None = None, getter=None
) -> tuple[dict, str | None]:
    key = api_key if api_key is not None else get_key("DART_API_KEY")
    if not key:
        return {}, "no key"

    get = getter or _http_get_json
    params = {
        "crtfc_key": key,
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    }
    try:
        payload = get(FINANCIALS_URL, params)
    except Exception as e:  # noqa: BLE001 — 키 값 미포함 에러로 변환
        return {}, f"{type(e).__name__}"

    rows, err = parse_financial_payload(payload)
    if err:
        return {}, err
    return extract_key_accounts(rows), None


# ---------------------------------------------------------------------------
# 3) 발행주식총수 (stockTotqySttus)
# ---------------------------------------------------------------------------


def extract_shares_outstanding(rows: list[dict]) -> int | None:
    """`se == "보통주"` 행의 `istc_totqy`(발행주식의 총수). 우선주는 표준 BPS
    분모에서 제외한다."""
    for row in rows:
        if row.get("se") == "보통주":
            return _to_int(row.get("istc_totqy"))
    return None


def fetch_shares_outstanding(
    corp_code: str, bsns_year: str, reprt_code: str, api_key: str | None = None, getter=None
) -> tuple[int | None, str | None]:
    key = api_key if api_key is not None else get_key("DART_API_KEY")
    if not key:
        return None, "no key"

    get = getter or _http_get_json
    params = {
        "crtfc_key": key,
        "corp_code": corp_code,
        "bsns_year": bsns_year,
        "reprt_code": reprt_code,
    }
    try:
        payload = get(SHARES_URL, params)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}"

    rows, err = parse_financial_payload(payload)  # 같은 status 관례 재사용
    if err:
        return None, err
    return extract_shares_outstanding(rows), None


# ---------------------------------------------------------------------------
# 4) 파생 팩터 + 원장
# ---------------------------------------------------------------------------


def compute_factors(
    capital_total: int | None,
    liabilities_total: int | None,
    net_income: int | None,
    shares_outstanding: int | None,
) -> dict:
    """부채비율(%)/ROE(%)/BPS(원). 분모가 없거나 0이면 `None` — 0으로
    위장하지 않는다(0으로 나누기는 "부채비율 0%"가 아니라 "계산 불가"다)."""
    debt_ratio = (
        round(liabilities_total / capital_total * 100, 2)
        if capital_total not in (None, 0) and liabilities_total is not None
        else None
    )
    roe = (
        round(net_income / capital_total * 100, 2)
        if capital_total not in (None, 0) and net_income is not None
        else None
    )
    bps = (
        round(capital_total / shares_outstanding, 2)
        if shares_outstanding not in (None, 0) and capital_total is not None
        else None
    )
    return {"debt_ratio": debt_ratio, "roe": roe, "bps": bps}


def append_ledger(rows: list[dict], path: Path) -> int:
    """`(stock_code, bsns_year, reprt_code)` 기준 중복 제거 후 append.
    `naver_research.append_ledger`와 같은 원자적-append 패턴이다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[tuple] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            seen.add((row.get("stock_code"), row.get("bsns_year"), row.get("reprt_code")))

    to_write: list[dict] = []
    for row in rows:
        key = (row.get("stock_code"), row.get("bsns_year"), row.get("reprt_code"))
        if key in seen:
            continue
        seen.add(key)
        to_write.append(row)

    if to_write:
        with path.open("a", encoding="utf-8") as f:
            for row in to_write:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(to_write)


def fetch_and_persist(
    stock_codes: list[str],
    bsns_year: str,
    reprt_code: str,
    root: Path,
    api_key: str | None = None,
    corp_code_getter=None,
    financials_getter=None,
    shares_getter=None,
) -> dict:
    """종목코드 목록에 대해 corp_code 매핑 → 재무제표 → 발행주식수 → 파생
    팩터 순으로 조회해 `data/ledger/fundamentals_dart.jsonl`에 적재한다.

    한 종목의 실패가 나머지를 막지 않는다(`errors`에 이유를 남기고 계속) —
    이 배치가 리포트/수집 파이프라인을 막으면 안 된다는 다른 수집기들의
    관례와 같다.
    """
    key = api_key if api_key is not None else get_key("DART_API_KEY")
    corp_map, err = get_corp_code_map(root, api_key=key, getter=corp_code_getter)
    if err:
        return {"requested": len(stock_codes), "added": 0, "errors": [f"corp_code 매핑 실패: {err}"]}

    errors: list[str] = []
    ledger_rows: list[dict] = []
    for stock_code in stock_codes:
        corp = corp_map.get(stock_code)
        if not corp:
            errors.append(f"{stock_code}: corp_code 매핑 없음")
            continue

        accounts, fin_err = fetch_financials(
            corp["corp_code"], bsns_year, reprt_code, api_key=key, getter=financials_getter
        )
        if fin_err:
            errors.append(f"{stock_code}: 재무제표 조회 실패({fin_err})")
            continue

        shares, shares_err = fetch_shares_outstanding(
            corp["corp_code"], bsns_year, reprt_code, api_key=key, getter=shares_getter
        )
        if shares_err:
            print(
                f"{stock_code}: 발행주식수 조회 실패({shares_err}) — BPS 없이 진행",
                file=sys.stderr,
            )

        factors = compute_factors(
            accounts.get("capital_total"), accounts.get("liabilities_total"),
            accounts.get("net_income"), shares,
        )
        ledger_rows.append(
            {
                "stock_code": stock_code,
                "corp_code": corp["corp_code"],
                "corp_name": corp["corp_name"],
                "bsns_year": bsns_year,
                "reprt_code": reprt_code,
                "fs_div": accounts.get("fs_div"),
                "capital_total": accounts.get("capital_total"),
                "liabilities_total": accounts.get("liabilities_total"),
                "net_income": accounts.get("net_income"),
                "shares_outstanding": shares,
                "debt_ratio": factors["debt_ratio"],
                "roe": factors["roe"],
                "bps": factors["bps"],
                "rcept_no": accounts.get("rcept_no"),
                "rcept_dt": (accounts.get("rcept_no") or "")[:8] or None,
                "source": "dart_financials",
            }
        )

    path = root / "data" / "ledger" / "fundamentals_dart.jsonl"
    added = append_ledger(ledger_rows, path)
    return {"requested": len(stock_codes), "added": added, "errors": errors}
