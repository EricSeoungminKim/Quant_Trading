"""네이버 테마(sise_group?type=theme) → 테마별 종목 + 편입사유. "왜 수혜주인가"가
네이버가 큐레이션한 편입사유 텍스트로 출처를 갖는다 — LLM이 짓지 않는다.

수집 예절은 naver_sector.py 와 같다: 요청 사이 0.3s, Referer 지정, _http_get 패턴
재사용. 개별 테마 상세 조회 실패는 그 테마만 건너뛴다 — 전체를 버리지 않는다.

편입사유가 없는 종목은 목록에서 제외한다(빈 이유로 관계를 만들지 않는다). 2026-08-15
EC2 전수 크롤(266개 테마·6,419종목)에서 실제로 이유 없는 행은 없었다 — 방어
분기이지 오늘 기준으로 상시 발동하는 경로는 아니다.
"""
from __future__ import annotations

import re
import sys
import time

from quant.adapters.http import client

THEME_LIST_URL = "https://finance.naver.com/sise/theme.naver"
THEME_DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}"

# 목록 페이지: 테마명 링크. 페이지당 40개(2026-08-15 EC2 실측, 7페이지/266개 테마).
_LIST_RE = re.compile(r'/sise/sise_group_detail\.naver\?type=theme&no=(\d+)">([^<]+)</a>')

# 목록 페이지 행의 등락률 — col_type2 = 전일대비(네이버 홈 "테마상위 정유 +6.48%"
# 가 이 값), col_type3 = 최근 3일 등락률(별개 컬럼, 혼동 금지 — 2026-08-15 EC2
# 실측 정유: 전일대비 +6.48% vs 3일 +0.98%, 값이 달라 착각하면 바로 드러난다).
# 보합(부호·색상 클래스 없음)은 sector 쪽과 같은 규약으로 span 클래스를 고정하지
# 않는다. 값 span 이 비어 있으면(그룹 3 None) 그 테마는 건너뛴다 — 0 위장 금지.
_QUOTE_RE = re.compile(
    r'/sise/sise_group_detail\.naver\?type=theme&no=(\d+)">([^<]+)</a></td>\s*'
    r'<td class="number col_type2">\s*(?:<span[^>]*>\s*([+\-]?[\d.]+)%\s*</span>)?\s*</td>'
)

# 상세 페이지 멤버 테이블(class="type_5") 행 단위 파싱. <thead> 실측(2026-08-15
# EC2, no=185 정유): 종목명(colspan=2, 코드+편입사유) 현재가 전일비 등락률 매수호가
# 매도호가 거래량 거래대금 전일거래량 토론. class="number" td 는 8개 —
# 등락률=3번째(index 2), 거래대금=7번째(index 6). 매수호가/매도호가/거래량/
# 전일거래량은 확신은 있으나 브리프 범위 밖이라 파싱하지 않는다.
_ROW_RE = re.compile(r"<tr[^>]*mouseOver[^>]*>(.*?)</tr>", re.S)
_CODE_NAME_RE = re.compile(
    r'<td class="name"><div class="name_area"><a href="/item/main\.naver\?code=(\d{6})">([^<]+)</a>'
)
# 편입사유 본문에 <게임/도서 제목> 같은 리터럴 꺾쇠가 그대로 섞여 나온다(2026-08-15
# EC2 실측, 테마 42 게임 — "<승리의 여신: 니케>" 등). [^<]* 로 잡으면 그 지점에서
# 끊겨 매치 자체가 실패해 사유가 있는 종목을 없는 것으로 오판한다. 실제 닫는
# </p> 까지 non-greedy 로 잡는다.
_REASON_RE = re.compile(r'<p class="info_txt">(.*?)</p>', re.S)
_NUMBER_TD_RE = re.compile(r'<td class="number"[^>]*>(.*?)</td>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_theme_list(html_text: str) -> list[tuple[str, str]]:
    return [(no, name.strip()) for no, name in _LIST_RE.findall(html_text or "")]


def parse_theme_quotes(html_text: str) -> list[dict]:
    """목록 페이지의 테마별 전일대비 등락률(네이버 홈 "테마상위" 카드와 같은 값).
    최근 3일 등락률(col_type3)과는 별개 컬럼 — 섞지 않는다.
    """
    out: list[dict] = []
    for no, name, change in _QUOTE_RE.findall(html_text or ""):
        if not change:
            continue  # 등락률 없음/깨진 행 — 건너뛴다
        out.append({"no": no, "name": name.strip(), "change_pct": float(change)})
    return out


def parse_theme_members(html_text: str) -> list[dict]:
    out: list[dict] = []
    total = 0
    for row in _ROW_RE.findall(html_text or ""):
        code_name = _CODE_NAME_RE.search(row)
        if not code_name:
            continue
        total += 1
        reason_match = _REASON_RE.search(row)
        reason = reason_match.group(1).strip() if reason_match else ""
        if not reason:
            continue  # 편입사유 없음 — 빈 이유로 관계를 만들지 않는다
        numbers = [_TAG_RE.sub("", cell).strip() for cell in _NUMBER_TD_RE.findall(row)]
        if len(numbers) < 7:
            continue  # 예상 컬럼 수 미달 — 다른 레이아웃의 행, 건너뛴다
        try:
            change_pct = float(numbers[2].replace("%", ""))
            value_traded = int(numbers[6].replace(",", ""))  # 단위: 백만원
        except ValueError:
            continue
        code, name = code_name.group(1), code_name.group(2).strip()
        out.append(
            {
                "code": code,
                "name": name,
                "reason": reason,
                "change_pct": change_pct,
                "value_traded": value_traded,
            }
        )
    skipped = total - len(out)
    if skipped:
        print(f"테마 멤버 파싱 {len(out)}/{total}건 (편입사유 없음 등으로 건너뜀 {skipped}건)", file=sys.stderr)
    return out


def _has_next_page(html_text: str, current_page: int) -> bool:
    return f"page={current_page + 1}" in (html_text or "")


def _http_get(url: str) -> str | None:
    with client() as c:
        resp = c.get(url, headers={"Referer": "https://finance.naver.com/"})
        resp.raise_for_status()
    return resp.text


def fetch_themes(getter=None, sleep=None, max_pages=None) -> dict:
    get = getter or _http_get
    zzz = sleep or time.sleep
    themes: dict[str, dict] = {}
    seen_no: set[str] = set()
    page = 1
    while max_pages is None or page <= max_pages:
        url = THEME_LIST_URL if page == 1 else f"{THEME_LIST_URL}?page={page}"
        try:
            list_html = get(url)
        except Exception:  # noqa: BLE001 — 목록 페이지 실패, 여기까지 모은 걸로 종료
            break
        if not list_html:
            break
        quotes = {q["no"]: q["change_pct"] for q in parse_theme_quotes(list_html)}
        for no, name in parse_theme_list(list_html):
            if no in seen_no:
                continue
            seen_no.add(no)
            try:
                detail_html = get(THEME_DETAIL_URL.format(no=no))
            except Exception:  # noqa: BLE001 — 테마 하나 실패가 전체를 버리지 않는다
                continue
            theme = {"name": name, "symbols": parse_theme_members(detail_html or "")}
            if no in quotes:
                theme["change_pct"] = quotes[no]  # 값을 못 얻으면 키 부재 — 0 위장 금지
            themes[no] = theme
            zzz(0.3)
        if not _has_next_page(list_html, page):
            break
        page += 1
        zzz(0.3)
    return themes
