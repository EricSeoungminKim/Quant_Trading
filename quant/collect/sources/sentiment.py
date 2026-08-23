"""시장 심리 지표 — CNN Fear & Greed · NAAIM Exposure · AAII 투자자 심리.

세 지표는 서로 독립이다. 하나가 막혀도 나머지는 살린다 — fetch_sentiment 이 각각
개별 try/except로 감싸고, 셋 다 실패했을 때만 예외를 올린다(조용히 빈 값을 주면
'오늘은 심리가 중립'과 구분이 안 된다).

엔드포인트는 전부 실측으로 찾았다(2026-08-12):
- CNN: production.dataviz.cnn.io 는 기본 요청에 418(봇 차단)을 낸다. Referer를
  edition.cnn.com 으로, Accept를 application/json 으로 채우면 200이 온다.
- NAAIM: naaim.org 페이지 자체엔 데이터가 없다(SPA 임베드 iframe만 있음). 그 중
  index.naaim.org/embeddable/table 이 서버 렌더링된 HTML 표를 낸다 — 최신 행이
  맨 위(날짜 내림차순)다. (number/chart 임베드는 클라이언트 렌더라 빈 body만 옴.)
- AAII: sent_results 페이지가 그대로 HTML 표다. 날짜에 연도가 없어서 페이지 하단
  저작권 표기(&copy; YYYY)에서 연도를 가져와 붙인다.
"""
from __future__ import annotations

# 이 일수를 넘게 묵은 값은 '최신'으로 보여주면 오도한다. NAAIM 은 실제로
# 몇 달 묵은 값을 서빙하는 경우가 있어(2026-08-12 실측: 최신 행이 4/29),
# 날짜를 밝히고 stale 플래그를 세운다 — 지우지 않는 이유는 그래도 참고는
# 되기 때문이고, 감추지 않는 이유는 현재값으로 오해되면 안 되기 때문이다.
STALE_DAYS = 21

import html
import re
from datetime import datetime

from quant.adapters.http import client

CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
NAAIM_URL = "https://index.naaim.org/embeddable/table"
AAII_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_AAII_DATE = re.compile(r"^[A-Za-z]{3} \d{1,2}$")
_AAII_YEAR = re.compile(r"(?:&copy;|©)\s*(\d{4})")


def fg_rating_ko(value: int) -> str:
    """CNN 공식 구간: 0~24 극단적 공포 / 25~44 공포 / 45~55 중립 / 56~74 탐욕 / 75~100 극단적 탐욕."""
    if value <= 24:
        return "극단적 공포"
    if value <= 44:
        return "공포"
    if value <= 55:
        return "중립"
    if value <= 74:
        return "탐욕"
    return "극단적 탐욕"


def naaim_label(value: float) -> str:
    """<20 방어적 / 20~49 중립 이하 / 50~79 강세 / >=80 적극 강세."""
    if value < 20:
        return "방어적"
    if value < 50:
        return "중립 이하"
    if value < 80:
        return "강세"
    return "적극 강세"


def _cell_text(cell: str) -> str:
    return html.unescape(_TAG.sub("", cell)).strip()


def parse_cnn_fear_greed(data: dict) -> dict:
    fg = data["fear_and_greed"]
    value = round(fg["score"])
    return {
        "value": value,
        "rating": fg["rating"].title(),
        "rating_ko": fg_rating_ko(value),
        "prev_close": round(fg["previous_close"]),
        "prev_week": round(fg["previous_1_week"]),
    }


def fetch_cnn_fear_greed() -> dict:
    with client() as c:
        resp = c.get(
            CNN_URL,
            headers={"Referer": "https://edition.cnn.com/", "Accept": "application/json"},
        )
        resp.raise_for_status()
    return parse_cnn_fear_greed(resp.json())


def parse_naaim_table(text: str) -> list[dict]:
    """임베드 표 컬럼: Date, NAAIM Number, Bearish, Q1, Q2, Q3, Bullish, Deviation.
    맨 위 행이 최신이다(날짜 내림차순)."""
    rows: list[dict] = []
    for tr in _TR.findall(text):
        cells = [_cell_text(c) for c in _TD.findall(tr)]
        if len(cells) != 8:
            continue
        try:
            date = datetime.strptime(cells[0], "%m/%d/%Y").date().isoformat()
            value = float(cells[1])
        except ValueError:
            continue
        rows.append({"date": date, "value": value})
    return rows


def fetch_naaim() -> dict:
    with client() as c:
        resp = c.get(NAAIM_URL)
        resp.raise_for_status()
    rows = parse_naaim_table(resp.text)
    if len(rows) < 2:
        raise ValueError(f"NAAIM 표 파싱 결과 {len(rows)}행 — 최소 2행(최신+직전) 필요")
    latest, prev = rows[0], rows[1]
    return {
        "value": latest["value"],
        "label": naaim_label(latest["value"]),
        "prev": prev["value"],
        "as_of": latest["date"],
    }


def parse_aaii_table(text: str) -> list[dict]:
    """sent_results 표 컬럼: Reported Date, Bullish, Neutral, Bearish. 연도는 안 담겨 있다."""
    rows: list[dict] = []
    for tr in _TR.findall(text):
        cells = [_cell_text(c) for c in _TD.findall(tr)]
        if len(cells) != 4 or not _AAII_DATE.match(cells[0]):
            continue
        try:
            rows.append(
                {
                    "date_raw": cells[0],
                    "bull_pct": float(cells[1].rstrip("%")),
                    "neutral_pct": float(cells[2].rstrip("%")),
                    "bear_pct": float(cells[3].rstrip("%")),
                }
            )
        except ValueError:
            continue
    return rows


def _aaii_year(text: str) -> int:
    m = _AAII_YEAR.search(text)
    if not m:
        raise ValueError("AAII 페이지에서 저작권 연도를 찾을 수 없음 — 날짜 파싱 불가")
    return int(m.group(1))


def fetch_aaii() -> dict:
    with client() as c:
        resp = c.get(AAII_URL)
        resp.raise_for_status()
    text = resp.text
    rows = parse_aaii_table(text)
    if len(rows) < 2:
        raise ValueError(f"AAII 표 파싱 결과 {len(rows)}행 — 최소 2행(최신+직전) 필요")
    year = _aaii_year(text)
    latest, prev = rows[0], rows[1]
    as_of = datetime.strptime(f"{latest['date_raw']} {year}", "%b %d %Y").date().isoformat()
    return {
        "bull_pct": latest["bull_pct"],
        "bear_pct": latest["bear_pct"],
        "neutral_pct": latest["neutral_pct"],
        "bull_change": round(latest["bull_pct"] - prev["bull_pct"], 1),
        "bear_change": round(latest["bear_pct"] - prev["bear_pct"], 1),
        "spread": round(latest["bull_pct"] - latest["bear_pct"], 1),
        "as_of": as_of,
    }


def fetch_sentiment() -> dict:
    result: dict = {}
    for key, fn in (
        ("cnn_fear_greed", fetch_cnn_fear_greed),
        ("naaim", fetch_naaim),
        ("aaii", fetch_aaii),
    ):
        try:
            result[key] = fn()
        except Exception:
            result[key] = None
    if all(v is None for v in result.values()):
        raise ValueError("시장 심리 지표를 하나도 못 가져왔다")

    from datetime import date as _d

    today = _d.today()
    return {k: mark_stale(v, today) for k, v in result.items()}


def mark_stale(entry: dict | None, today, days: int = STALE_DAYS) -> dict | None:
    """`as_of` 가 `days` 보다 오래됐으면 stale=True 를 붙인다."""
    if not entry or not entry.get("as_of"):
        return entry
    from datetime import date as _date

    try:
        age = (today - _date.fromisoformat(entry["as_of"])).days
    except ValueError:
        return entry
    return {**entry, "age_days": age, "stale": age > days}
