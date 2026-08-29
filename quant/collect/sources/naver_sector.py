"""네이버 업종(sise_group) → KR 종목↔업종 매핑. KR 에는 업종 분류가 없었다
(themes.py 는 뉴스 키워드 8개뿐) — 이게 테마별 시세·수혜주 후보의 바닥 데이터다.

수집 예절은 stock_detail.py 와 같다: 요청 사이 0.3s, Referer 지정.
한 종목이 여러 업종에 걸리면 **먼저 만난 업종**을 유지한다(네이버 인덱스 순서).
"""
from __future__ import annotations

import re
import sys
import time

from quant.adapters.http import client

INDEX_URL = "https://finance.naver.com/sise/sise_group.naver?type=upjong"
DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={no}"

_IDX_RE = re.compile(r"sise_group_detail\.naver\?type=upjong&no=(\d+)[^>]*>([^<]+)<")
_MEM_RE = re.compile(r"/item/main\.naver\?code=(\d{6})[\"'][^>]*>([^<]+)<")
# 인덱스 행 전체: 업종명 + 전일대비(등락률) + 전체/상승/보합/하락 (헤더 실측,
# 2026-08-15 EC2). 등락률 span 이 비어 있으면(3번째 그룹 None) 그 행은
# parse_sector_quotes 에서 건너뛴다 — 0.0 으로 채우지 않는다.
# 보합(0.00%)은 부호가 없다 — 인덱스 페이지 자체에서 오늘 실측하진 못했지만
# (79개 업종 전부 0이 아니었음), 같은 사이트의 업종 상세 페이지
# (sise_group_detail.naver, 2026-08-15 EC2 실측)에서 보합 종목이
# `<span class="tah p11">0.00%</span>` — class 접미사(red01/nv01) 없이,
# 부호 없이 렌더링되는 걸 확인했다. 같은 템플릿 규약이라고 보고 부호를
# 선택적으로 둔다.
_QUOTE_RE = re.compile(
    r'sise_group_detail\.naver\?type=upjong&no=(\d+)"[^>]*>([^<]+)</a></td>'
    r'\s*<td class="number">\s*(?:<span[^>]*>\s*([+\-]?[\d.]+)%\s*</span>)?\s*</td>'
    r'\s*<td class="number">\d+</td>'  # 전체 — 스키마에 없어 버림
    r'\s*<td class="number">(\d+)</td>'  # 상승
    r'\s*<td class="number">(\d+)</td>'  # 보합
    r'\s*<td class="number">(\d+)</td>',  # 하락
    re.S,
)


def parse_sector_index(html_text: str) -> list[tuple[str, str]]:
    return [(no, name.strip()) for no, name in _IDX_RE.findall(html_text or "")]


def parse_sector_quotes(html_text: str) -> list[dict]:
    matches = _QUOTE_RE.findall(html_text or "")
    out: list[dict] = []
    for no, name, change, up, flat, down in matches:
        if not change:
            continue  # 등락률 없음/깨진 행 — 건너뛴다
        out.append(
            {
                "no": no,
                "name": name.strip(),
                "change_pct": float(change),
                "up": int(up),
                "flat": int(flat),
                "down": int(down),
            }
        )
    skipped = len(matches) - len(out)
    if skipped:
        print(f"업종 등락률 파싱 {len(out)}/{len(matches)}건 (건너뜀 {skipped}건)", file=sys.stderr)
    return out


def parse_sector_members(html_text: str) -> list[tuple[str, str]]:
    return [(code, name.strip()) for code, name in _MEM_RE.findall(html_text or "")]


# 업종 상세 페이지(sise_group_detail.naver?type=upjong)의 멤버 시세 —
# naver_theme.parse_theme_members 가 같은 페이지 타입(sise_group_detail)을
# 파싱하는 방식과 행 구조(mouseOver 트리거)·종목명 마크업이 동일하다. 다만
# 업종 상세엔 편입사유(reason) 컬럼이 없어 그 파싱 로직은 재사용하지 않는다
# — 여기선 등락률만 뽑는다.
_DETAIL_ROW_RE = re.compile(r"<tr[^>]*mouseOver[^>]*>(.*?)</tr>", re.S)
_DETAIL_CODE_NAME_RE = re.compile(
    r'<td class="name"><div class="name_area"><a href="/item/main\.naver\?code=(\d{6})">([^<]+)</a>'
)
# 등락률 span 만 매치한다(가격·전일비 span 엔 '%' 가 없어 겹치지 않는다).
# 보합(프리티 0.00%, 2026-08-16 EC2 실측)은 부호·색상 클래스 접미사가 없다 —
# 위 _QUOTE_RE 주석과 같은 규약. 코스닥 별표(와이어블 *, name_area 안
# <span class="dot">*</span>)는 종목명 뒤에 붙어 이 regex 와 무관하다 —
# 이름 캡처가 </a> 에서 끝나 별표를 건드리지 않는다.
_DETAIL_CHANGE_RE = re.compile(r'<span class="tah p11[^"]*">\s*([+\-]?[\d.]+)%\s*</span>')


def parse_sector_detail_members(html_text: str) -> list[dict]:
    out: list[dict] = []
    for row in _DETAIL_ROW_RE.findall(html_text or ""):
        code_name = _DETAIL_CODE_NAME_RE.search(row)
        change = _DETAIL_CHANGE_RE.search(row)
        if not code_name or not change:
            continue  # 코드/이름 또는 등락률 파싱 실패 — 이 종목만 건너뛴다
        out.append({
            "code": code_name.group(1),
            "name": code_name.group(2).strip(),
            "change_pct": float(change.group(1)),
        })
    return out


def _http_get(url: str) -> str | None:
    with client() as c:
        resp = c.get(url, headers={"Referer": "https://finance.naver.com/"})
        resp.raise_for_status()
    return resp.text


def fetch_sector_quotes(getter=None) -> list[dict]:
    """업종 등락률 — 인덱스 1페이지만 (업종 상세는 열지 않는다, fetch_sector_map 과 다름)."""
    get = getter or _http_get
    try:
        idx = get(INDEX_URL)
    except Exception:  # noqa: BLE001 — 인덱스 자체 실패는 전체 실패, 빈 리스트
        return []
    return parse_sector_quotes(idx or "")


def fetch_sector_data(getter=None, sleep=None) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """업종 상세 79페이지를 한 번 크롤해 `(sector_map, sector_members)` 를 함께
    낸다 — 이미 열던 페이지에서 멤버 시세까지 파싱해 추가 네트워크 0 을 지킨다.
    `sector_map`: `{종목코드: 업종명}` (기존 fetch_sector_map 과 동일 계약).
    `sector_members`: `{업종명: [{"code","name","change_pct"}, ...]}`.
    """
    get = getter or _http_get
    zzz = sleep or time.sleep
    try:
        idx = get(INDEX_URL)
    except Exception:  # noqa: BLE001 — 인덱스 자체 실패는 전체 실패, 빈 값
        return {}, {}
    sector_map: dict[str, str] = {}
    sector_members: dict[str, list[dict]] = {}
    for no, sector in parse_sector_index(idx or ""):
        try:
            page = get(DETAIL_URL.format(no=no))
        except Exception:  # noqa: BLE001 — 업종 하나 실패가 전체를 버리지 않는다
            continue
        for code, _name in parse_sector_members(page or ""):
            sector_map.setdefault(code, sector)
        sector_members[sector] = parse_sector_detail_members(page or "")
        zzz(0.3)
    return sector_map, sector_members
