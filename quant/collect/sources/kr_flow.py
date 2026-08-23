"""한국 수급 체력 지표 — 투자자예탁금 · 신용융자잔고 · CMA잔고 · VKOSPI.

지수가 오르는데 예탁금이 마르고 신용융자만 늘면 그 상승은 체력이 없다.
회사 리포트가 이 지표들을 싣고 있어 우리도 맞춰 넣는다.

## 탐색 결과 (2026-08-12 실측)

- **FREESIS 상세 통계**(`FreeSIS.do?serviceId=...`)는 cleopatra SPA 프레임워크로
  렌더링되고, 실데이터는 `/meta/getSrvData.do` POST 로 내려온다. 이 엔드포인트는
  쿠키·Referer·Origin 을 갖춰 호출해도 항상 "일시적인 오류로 인해 서비스 연결이
  되지 않습니다" HTML 에러 페이지를 반환했다 — 브라우저 세션 밖에서는 못 뚫는다.
  이 때문에 **대차거래잔고**와 **주식형펀드 순설정**은 FREESIS 에서 가져올 방법을
  찾지 못했다 (상세 페이지에만 있고 메인 요약 위젯에는 없음).
- 반면 FREESIS **메인 페이지**(`stat/main.do`)는 서버 렌더링된 요약 위젯을
  그대로 내려준다 — 여기서 투자자예탁금·신용융자·CMA잔고를 curl 만으로 얻는다.
- **VKOSPI**: `data.krx.co.kr/comm/bldAttendant/getJsonData.cmd` 는 bld 값을
  바꿔도(MDCSTAT02701 등), OTP 다운로드 경로(`fileDn/GenerateOTP/generate.cmd`)로
  우회해도 전부 `400 LOGOUT` — 완전히 막혀 있다. 네이버금융(`sise_index.naver`,
  모바일 API, 자동완성)은 VKOSPI 를 아예 색인하지 않는다(302/404, 빈 결과).
  다음금융 API 는 KOSPI 조회조차 500 에러. yfinance 는 `^VKOSPI`/`KOSPIVIX.KS`
  등 후보 티커가 전부 미상장 처리. investing.com 은 403. KRX 오픈API 는 발급
  키가 필요한데 이 저장소엔 그 키 발급/보관 체계가 없다(`.env.local.example`
  참고 — KRX 키는 명시적으로 "불필요"라고 돼 있다). 그래서 `vkospi` 는 항상
  `None` 이다 — 추측 값을 넣지 않는다.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime

from quant.core.report_clock import KST
from quant.adapters.http import client

FREESIS_MAIN_URL = "https://freesis.kofia.or.kr/stat/main.do"

# FREESIS 메인 요약 위젯의 항목명 → 우리 출력 키. 위젯에 없는 항목(대차거래잔고,
# 주식형펀드 순설정)은 여기 없다 — 못 뚫었으므로 생략한다.
_LABEL_TO_KEY = {
    "투자자예탁금": "투자자예탁금",
    "신용융자": "신용융자잔고",
    "CMA잔고": "CMA잔고",
}

_ITEM = re.compile(
    r'<dt class="chart-name"><a[^>]*>(?P<label>[^<]+)</a></dt>\s*'
    r'<dd class="etc"><span class="dan">(?P<unit>[^<]+)</span>\s*\|\s*'
    r'<span class="date">(?P<date>[^<]+)</span></dd>\s*'
    r'<dd class="chart-num">\s*<span class="num1">(?P<value>[^<]+)</span>'
    r'(?:.*?<span class="num2[ab]">(?P<change>[^<]+)</span>)?'
    r'.*?<span class="num3">(?P<change_pct>[^<]+)</span>',
    re.S,
)


def _num(text: str) -> float:
    return float(html.unescape(text).replace(",", "").strip())


def million_won_to_trillion(value: float) -> float:
    """백만원 → 조원. 1조원 = 1,000,000백만원."""
    return value / 1_000_000


def _normalize_date(mmdd: str, today: date) -> str:
    """'MM/DD' → ISO 날짜. 연도가 없어 `today` 기준으로 추론한다.

    추론값이 오늘보다 미래면(연말에 스크랩했는데 위젯이 아직 작년 12월 데이터인
    경우) 작년으로 되돌린다.
    """
    mm, dd = (int(p) for p in mmdd.strip().split("/"))
    candidate = date(today.year, mm, dd)
    if candidate > today:
        candidate = date(today.year - 1, mm, dd)
    return candidate.isoformat()


def parse_freesis_main(text: str, today: date | None = None) -> dict[str, dict]:
    """FREESIS 메인 페이지 HTML에서 수급 체력 위젯을 파싱해 조원 단위로 반환한다."""
    if today is None:
        today = date.today()
    out: dict[str, dict] = {}
    for m in _ITEM.finditer(text):
        key = _LABEL_TO_KEY.get(m.group("label"))
        if key is None:
            continue
        unit = m.group("unit")
        if unit != "백만원":
            continue
        value = million_won_to_trillion(_num(m.group("value")))
        entry = {
            "value": round(value, 2),
            "unit": "조원",
            "as_of": _normalize_date(m.group("date"), today),
        }
        change_text = m.group("change")
        if change_text is not None:
            entry["change"] = round(million_won_to_trillion(_num(change_text)), 2)
        change_pct_text = m.group("change_pct")
        if change_pct_text is not None:
            entry["change_pct"] = _num(change_pct_text.replace("%", ""))
        out[key] = entry
    return out


def fetch_kr_flow() -> dict:
    """한국 수급 체력 지표 + VKOSPI. 항목별 실패는 흡수하고, 전부 실패하면 raise."""
    funding: dict = {}
    try:
        with client() as c:
            resp = c.get(FREESIS_MAIN_URL)
            resp.raise_for_status()
        funding = parse_freesis_main(resp.text, today=datetime.now(KST).date())
    except Exception:
        funding = {}

    # VKOSPI — 모든 무료 경로가 막혀 있음이 실측 확인됐다 (모듈 docstring 참고).
    # 추측 값을 넣지 않고 None 으로 둔다.
    vkospi = None

    if not funding and vkospi is None:
        raise ValueError("한국 수급 지표를 하나도 못 가져왔다")
    return {"funding": funding, "vkospi": vkospi}
