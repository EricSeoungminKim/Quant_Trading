"""DART OpenAPI(전자공시) 공시 목록 — 뉴스보다 정확한 팩트 이벤트 축 (H-2 Task 1).

네이버 공시 페이지는 robots.txt 전면 비허용이라(스펙 참고) 스크레이핑 경로가
없다 — 공식 API가 유일한 접근로다. `list.json` 은 상장·비상장 법인을 가리지
않고 섞어 주므로, 여기서 `stock_code` 6자리가 없는(비상장) 행은 버린다.

status 코드 관례(DART 공식 문서): "000"=정상, "013"=조회된 데이터 없음(정상적인
"오늘 공시 없음" 응답이라 에러로 취급하지 않는다), 그 외(예: "020"=요청 제한 초과,
"010"=등록되지 않은 키)는 에러로 드러낸다 — 조용한 빈 리스트로 위장하지 않는다.

**키 값은 절대 로그/에러 메시지에 넣지 않는다.**
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from quant.adapters.env import get_key
from quant.adapters.http import client

LIST_URL = "https://opendart.fss.or.kr/api/list.json"
MAX_PAGES = 10
PAGE_COUNT = 100
CORP_CLASSES = ("Y", "K")  # 유가증권(Y) + 코스닥(K) — 코넥스(N)는 범위 밖


def parse_disclosures(payload: dict) -> tuple[list[dict], str | None]:
    """DART `list.json` 응답 → (rows, error).

    `status == "013"`(조회된 데이터 없음)은 정당한 "공시 없음" 응답이라
    에러가 아니다 — `([], None)`. 그 외 `status != "000"` 은 에러로 드러낸다.
    비상장(`stock_code` 6자리 없음)은 조용히 걸러낸다.
    """
    status = payload.get("status")
    if status == "013":
        return [], None
    if status != "000":
        message = payload.get("message", "")
        return [], f"{status} {message}".strip()

    rows: list[dict] = []
    for item in payload.get("list", []) or []:
        stock_code = (item.get("stock_code") or "").strip()
        if len(stock_code) != 6:
            continue  # 비상장 — 종목코드가 없거나 형식이 다르다
        rows.append(
            {
                "rcept_no": item.get("rcept_no"),
                "stock_code": stock_code,
                "corp_name": item.get("corp_name"),
                "report_nm": item.get("report_nm"),
                "rcept_dt": item.get("rcept_dt"),
            }
        )
    return rows, None


def _http_get(params: dict) -> dict:
    with client(timeout=10.0) as c:
        resp = c.get(LIST_URL, params=params)
        resp.raise_for_status()
    return resp.json()


def fetch_disclosures(
    bgn_de: str, end_de: str, api_key: str | None = None, getter=None
) -> tuple[list[dict], str | None]:
    """`corp_cls` Y/K 를 각각 최대 `MAX_PAGES` 페이지까지 순회해 합친다.

    키가 없으면 네트워크를 타지 않고 `([], "no key")`. 한 `corp_cls` 에서
    에러가 나도 다른 `corp_cls` 조회는 계속한다 — 코스닥 쪽이 막혀도 유가증권
    쪽은 살릴 수 있다. 네트워크 예외는 여기서 삼키고 에러 문자열로 드러낸다
    (키 값은 담지 않는다).
    """
    key = api_key if api_key is not None else get_key("DART_API_KEY")
    if not key:
        return [], "no key"

    get = getter or _http_get
    rows: list[dict] = []
    errors: list[str] = []

    for corp_cls in CORP_CLASSES:
        page_no = 1
        while page_no <= MAX_PAGES:
            params = {
                "crtfc_key": key,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "corp_cls": corp_cls,
                "page_no": page_no,
                "page_count": PAGE_COUNT,
            }
            try:
                payload = get(params)
            except Exception as e:  # noqa: BLE001 — 네트워크 예외 삼키고 error 로
                errors.append(f"{corp_cls}: {type(e).__name__}")
                break
            page_rows, err = parse_disclosures(payload)
            if err:
                errors.append(f"{corp_cls} p{page_no}: {err}")
                break
            rows.extend(page_rows)
            total_page = payload.get("total_page") or 1
            if page_no >= total_page:
                break
            page_no += 1
            time.sleep(0.2)

    return rows, "; ".join(errors) if errors else None


def append_ledger(rows: list[dict], path: Path) -> int:
    """`rcept_no` 기준 중복 제거 후 append. 반환값은 실제로 추가된 건수.

    기존 원장을 먼저 읽어 이미 있는 `rcept_no` 집합을 만든다 — 매일 어제~오늘
    창을 겹쳐 조회해도 원장은 append-only 로 한 번만 쌓인다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rcept_no = json.loads(line).get("rcept_no")
            except ValueError:
                continue
            if rcept_no:
                seen.add(rcept_no)

    to_write: list[dict] = []
    for row in rows:
        rcept_no = row.get("rcept_no")
        if not rcept_no or rcept_no in seen:
            continue
        seen.add(rcept_no)
        to_write.append(row)

    if to_write:
        with path.open("a", encoding="utf-8") as f:
            for row in to_write:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(to_write)


# 공시 유형 태그 — report_nm 키워드 매핑 (서브프로젝트 I Part B, 2026-08-17
# 사용자 원칙 3: "DART 공시는 후발주자 리스크... 그래도 중요하고 믿음직").
# 단일 튜닝 지점 — 새 유형은 이 dict 에 키워드만 추가하면 된다. 순서가
# 매칭 우선순위다(먼저 매치되는 키워드가 이긴다).
REPORT_TYPE_KEYWORDS: dict[str, str] = {
    "수주": "수주",
    "공급계약": "수주",
    "임상": "임상·승인",
    "FDA": "임상·승인",
    "품목허가": "임상·승인",
    "유상증자": "유상증자",
    "무상증자": "무상증자",
    "전환사채": "CB",
    "CB": "CB",
    "배당": "배당",
    "실적": "실적",
    "자기주식": "자사주",
    "최대주주": "지분변동",
    "지분": "지분변동",
}


def classify_report(report_nm: str) -> str | None:
    """공시 제목(`report_nm`)에서 유형 태그를 뽑는다.

    `REPORT_TYPE_KEYWORDS` 의 키워드가 `report_nm` 에 부분 문자열로 있으면
    그 유형을 돌려준다(딕셔너리 순서 = 우선순위). 매치가 없으면 `None` —
    태그를 지어내지 않고 정직하게 "태그 없음"을 알린다.
    """
    if not report_nm:
        return None
    for keyword, label in REPORT_TYPE_KEYWORDS.items():
        if keyword in report_nm:
            return label
    return None
