"""KRX 투자자별 매매동향 — 네이버금융 경로.

data.krx.co.kr 의 getJsonData.cmd 는 400 LOGOUT 으로 막혀 있다(2026-08-12 실측).
회사 리포트도 실제로는 이 네이버 경로를 쓰고 있었으므로 동일 경로를 택한다.

단위는 억원. 원본 단위를 보존하고 변환은 렌더 시점에만 한다.
"""
from __future__ import annotations

import html
import re

from quant.adapters.http import client

URL_TEMPLATE = (
    "https://finance.naver.com/sise/investorDealTrendDay.naver?bizdate={bizdate}&sosok={sosok}"
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


def fetch_flow(sosok: str, bizdate: str) -> dict:
    """sosok: '01'=KOSPI, '02'=KOSDAQ. bizdate: 'YYYYMMDD'."""
    url = URL_TEMPLATE.format(bizdate=bizdate, sosok=sosok)
    with client() as c:
        resp = c.get(url)
        resp.raise_for_status()
    rows = parse_flow(resp.content)
    if not rows:
        raise ValueError(f"투자자 수급 파싱 결과 0행 (sosok={sosok}) — 표 구조 변경 의심")
    return {"market": {"01": "KOSPI", "02": "KOSDAQ"}[sosok], "unit": "억원", "rows": rows}
