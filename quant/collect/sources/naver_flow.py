"""KRX 투자자별 매매동향 — 네이버금융 경로.

data.krx.co.kr 의 getJsonData.cmd 는 400 LOGOUT 으로 막혀 있다(2026-08-12 실측).
2026-10-23: 기존 investorDealTrendDay HTML 경로가 410으로 폐기되어
네이버 국내시장 투자자동향 화면의 JSON API로 전환했다. 과거 HTML 파서는
저장된 스냅샷 재현용으로 유지한다.

단위는 억원. 원본 단위를 보존하고 변환은 렌더 시점에만 한다.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime

from quant.adapters.http import client

URL_TEMPLATE = (
    "https://stock.naver.com/api/domestic/market/trend/daily?tradeType=KRX"
    "&marketType={market}&bizdate={bizdate}&startIdx=0&pageSize=20"
)

# 데이터 행의 실제 컬럼 순서. <th> 순서와 다르다 — 헤더에 colspan 그룹이 섞여 있어
# 그대로 믿으면 한 칸씩 밀린다. 순서 변경 시 test_institution_subtotals_sum_to_total 이 잡는다.
FLOW_COLUMNS = (
    "개인", "외국인", "기관계",
    "금융투자", "보험", "투신", "은행", "기타금융", "연기금등",
    "기타법인",
)

# 기관계 항등식 검산용 — 기관계는 이 6개 세부항목의 합과 같아야 한다.
INSTITUTION_SUBS = ("금융투자", "보험", "투신", "은행", "기타금융", "연기금등")

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{2})$")


def _decode(raw: bytes) -> str:
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _num(text: str) -> int:
    return int(text.replace(",", "").replace("+", ""))


def parse_flow(raw: bytes) -> list[dict]:
    if raw.lstrip().startswith(b"{"):
        return _parse_json_flow(json.loads(raw))
    text = _decode(raw)
    rows: list[dict] = []
    for tr in _TR.findall(text):
        cells = [html.unescape(_TAG.sub("", c)).strip() for c in _TD.findall(tr)]
        cells = [c for c in cells if c and c != "\xa0"]
        if len(cells) != len(FLOW_COLUMNS) + 1:
            continue
        m = _DATE.match(cells[0])
        if not m:
            continue
        yy, mm, dd = m.groups()
        row: dict = {"date": f"20{yy}-{mm}-{dd}"}
        try:
            for name, cell in zip(FLOW_COLUMNS, cells[1:]):
                row[name] = _num(cell)
        except ValueError:
            continue
        rows.append(row)
    return rows


def _parse_json_flow(data: dict) -> list[dict]:
    """네이버 새 투자자 동향 API의 원 단위 값을 억원으로 변환한다.

    공식 /market/stock/kr/trend/trader 화면과 동일하게 사모→투신,
    기타외국인→외국인, 국가→연기금등으로 묶는다. 기관계는 원 단위에서
    6개 구성항목을 합산한다. 필수 그룹 결측은 0으로 대체하지 않는다.
    """
    groups = {
        "개인": ("8000",), "외국인": ("9000", "9001"),
        "금융투자": ("1000",), "보험": ("2000",), "투신": ("3000", "3100"),
        "은행": ("4000",), "기타금융": ("5000",), "연기금등": ("6000", "7000"),
        "기타법인": ("7100",),
    }
    rows = []
    for item in data.get("content") or []:
        try:
            day = datetime.strptime(item["bizdate"], "%Y%m%d").date().isoformat()
            amounts = {entry["investorGubun"]: int(entry["diffValue"])
                       for entry in item["netAmounts"]}
            values = {name: sum(amounts[code] for code in codes)
                      for name, codes in groups.items()}
            values["기관계"] = sum(values[name] for name in INSTITUTION_SUBS)
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({"date": day, **{name: value / 100_000_000 for name, value in values.items()}})
    return sorted(rows, key=lambda row: row["date"], reverse=True)


def fetch_flow(sosok: str, bizdate: str) -> dict:
    """sosok: '01'=KOSPI, '02'=KOSDAQ. bizdate: 'YYYYMMDD'."""
    market = {"01": "KOSPI", "02": "KOSDAQ"}[sosok]
    cutoff = datetime.strptime(bizdate, "%Y%m%d").date().isoformat()
    url = URL_TEMPLATE.format(bizdate=bizdate, market=market)
    with client() as c:
        resp = c.get(url)
        resp.raise_for_status()
    rows = [row for row in parse_flow(resp.content) if row["date"] <= cutoff]
    if not rows:
        raise ValueError(f"투자자 수급 파싱 결과 0행 (sosok={sosok}) — 표 구조 변경 의심")
    return {"market": market, "unit": "억원", "rows": rows}
