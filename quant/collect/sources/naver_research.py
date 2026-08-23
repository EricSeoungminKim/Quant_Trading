"""증권사 리서치(종목분석 리포트) 목록 — "오늘 리포트 나온 종목" 이벤트 (H-2 Task 5).

`finance.naver.com/research/` 는 robots.txt 실측상 yeti 허용 구역이다(스펙 참고) —
네이버 리서치류 경로 중 유일하게 폴링해도 되는 곳이다. **PDF 본문은 절대 열지
않는다**(스펙 명시) — 회사명·제목·증권사·작성일이 실린 목록 페이지만, 일 1회,
첫 2페이지(약 60건)만 본다.

수집 예절은 naver_sector.py/naver_theme.py 와 같다: Referer 지정, 페이지 사이
0.3s, getter/sleep 주입 패턴 재사용.

목록 페이지엔 목표가(투자의견) 컬럼이 없다(2026-08-16 EC2 실측, company_list.naver
1~2페이지 60건 전수 — 제목 문자열 안에 "목표주가" 라는 낱말이 섞여 나오는 경우는
있어도 구조화된 필드가 아니다). `target_price` 는 계약대로 "있을 때만" 채우는
optional 키로 남겨둔다 — 지금은 실제로 채워질 일이 없지만, 0/None 위장 금지
원칙을 지키고 사이트가 나중에 컬럼을 늘리면 그대로 받는다.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from quant.adapters.http import client

LIST_URL = "https://finance.naver.com/research/company_list.naver"
PAGES = 2

# 행 구조(2026-08-16 EC2 실측): 종목명(stock_item 링크의 title 속성) · 제목
# (company_read.naver 링크) · 증권사(순수 텍스트) · 첨부(pdf, 읽지 않는다) ·
# 작성일(YY.MM.DD) · 조회수(사용하지 않음). class="file" 셀 내부(pdf 링크)는
# `.*?` 로 건너뛰기만 한다 — 그 URL 을 fetch 하지 않는다.
_ROW_RE = re.compile(
    r'<a href="/item/main\.naver\?code=\d{6}"\s+title="([^"]+)"\s+class="stock_item">'
    r'[^<]*</a>\s*</td>\s*'
    r'<td><a href="company_read\.naver\?nid=\d+[^"]*">([^<]+)</a></td>\s*'
    r'<td>([^<]+)</td>\s*'
    r'<td class="file">.*?</td>\s*'
    r'<td class="date"[^>]*>([\d.]+)</td>',
    re.S,
)


def parse_research_list(html_text: str) -> list[dict]:
    out: list[dict] = []
    for stock_name, title, broker, date_raw in _ROW_RE.findall(html_text or ""):
        try:
            iso_date = datetime.strptime(date_raw, "%y.%m.%d").date().isoformat()
        except ValueError:
            continue  # 날짜 파싱 실패 — 이 행만 건너뛴다
        out.append(
            {
                "stock_name": stock_name.strip(),
                "title": title.strip(),
                "broker": broker.strip(),
                "date": iso_date,
            }
        )
    return out


def _http_get(url: str) -> str | None:
    with client() as c:
        resp = c.get(url, headers={"Referer": "https://finance.naver.com/"})
        resp.raise_for_status()
    return resp.text


def fetch_research(getter=None, sleep=None) -> list[dict]:
    """첫 `PAGES` 페이지만(스펙: 일 1회, 목록만). 페이지 하나 실패는 그 페이지만
    건너뛰고 계속한다 — 전부 실패하면 빈 리스트(예외를 밖으로 흘리지 않는다)."""
    get = getter or _http_get
    zzz = sleep or time.sleep
    rows: list[dict] = []
    for page in range(1, PAGES + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        try:
            html = get(url)
        except Exception as e:  # noqa: BLE001 — 이 페이지만 건너뛰고 계속
            print(
                f"증권사 리서치 목록 수집 실패(page={page}): {type(e).__name__}",
                file=sys.stderr,
            )
            continue
        rows.extend(parse_research_list(html or ""))
        if page < PAGES:
            zzz(0.3)
    return rows


def append_ledger(rows: list[dict], path: Path) -> int:
    """`(date, broker, title)` 기준 중복 제거 후 append. 반환값은 실제로 추가된
    건수. `dart.append_ledger` 와 같은 원자적-append 패턴이다."""
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
            seen.add((row.get("date"), row.get("broker"), row.get("title")))

    to_write: list[dict] = []
    for row in rows:
        key = (row.get("date"), row.get("broker"), row.get("title"))
        if key in seen:
            continue
        seen.add(key)
        to_write.append(row)

    if to_write:
        with path.open("a", encoding="utf-8") as f:
            for row in to_write:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    return len(to_write)
