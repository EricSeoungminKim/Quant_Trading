"""개별 종목 투자자 수급 + 애널리스트 컨센서스 — 네이버금융 / 컴퍼니가이드.

수급은 finance.naver.com/item/frgn.naver(외국인/기관 매매동향), 컨센서스는
navercomp.wisereport.co.kr 의 컴퍼니가이드(c1010001.aspx)를 쓴다. 둘 다 실측
검증됨(2026-08-12). 컨센서스는 상장 초기·소형주에서 흔히 비어 있으므로 실패해도
수급까지 죽이면 안 된다 — fetch_stock_detail 이 개별로 감싼다.
"""
from __future__ import annotations

import html
import re
import time

from quant.adapters.http import client

FRGN_URL = "https://finance.naver.com/item/frgn.naver?code={code}"
CONSENSUS_URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
OPINION_LABELS = ("강력매도", "매도", "중립", "매수", "강력매수")

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_DATE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$")
_CTB15 = re.compile(r'<table[^>]*id="cTB15"[^>]*>(.*?)</table>', re.S)


def _decode(raw: bytes) -> str:
    for enc in ("euc-kr", "cp949", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _cell_text(cell: str) -> str:
    return html.unescape(_TAG.sub("", cell)).strip()


def _int(text: str) -> int:
    return int(re.sub(r"[,+%\s]", "", text))


def _float(text: str) -> float:
    return float(re.sub(r"[,+%\s]", "", text))


def _safe_num(caster, text: str):
    try:
        return caster(text)
    except ValueError:
        return None


def opinion_label(score: float) -> str:
    if score < 1.5:
        return OPINION_LABELS[0]
    if score < 2.5:
        return OPINION_LABELS[1]
    if score < 3.5:
        return OPINION_LABELS[2]
    if score < 4.5:
        return OPINION_LABELS[3]
    return OPINION_LABELS[4]


def parse_investor_flow(raw: bytes) -> list[dict]:
    text = _decode(raw)
    rows: list[dict] = []
    for tr in _TR.findall(text):
        cells = [_cell_text(c) for c in _TD.findall(tr)]
        cells = [c for c in cells if c and c != "\xa0"]
        if len(cells) != 9:
            continue
        m = _DATE.match(cells[0])
        if not m:
            continue
        yyyy, mm, dd = m.groups()
        try:
            rows.append(
                {
                    "date": f"{yyyy}-{mm}-{dd}",
                    "close": _int(cells[1]),
                    "change_pct": _float(cells[3]),
                    "volume": _int(cells[4]),
                    "inst_net": _int(cells[5]),
                    "foreign_net": _int(cells[6]),
                    "foreign_hold_pct": _float(cells[8]),
                }
            )
        except ValueError:
            continue
    if not rows:
        raise ValueError("투자자 수급 파싱 결과 0행 — 표 구조 변경 의심")
    return rows


def parse_consensus(html_text: str) -> dict:
    m = _CTB15.search(html_text)
    if not m:
        raise ValueError("컨센서스 표(cTB15)를 찾을 수 없음 — 컨센서스 없는 종목이거나 표 구조 변경")

    data_row = None
    for tr in _TR.findall(m.group(1)):
        if "<th" not in tr:
            data_row = tr
            break
    if data_row is None:
        raise ValueError("컨센서스 표에서 데이터 행을 찾을 수 없음")

    cells = [_cell_text(c) for c in _TD.findall(data_row)]
    if len(cells) != 5:
        raise ValueError(f"컨센서스 표 컬럼 수 불일치: {len(cells)}")

    opinion_score = _safe_num(_float, cells[0])
    target_price = _safe_num(_int, cells[1])
    eps = _safe_num(_int, cells[2])
    per = _safe_num(_float, cells[3])
    analyst_count = _safe_num(_int, cells[4])

    if all(v is None for v in (opinion_score, target_price, eps, per, analyst_count)):
        raise ValueError("컨센서스 필드 전부 파싱 실패")

    return {
        "opinion_score": opinion_score,
        "opinion_label": opinion_label(opinion_score) if opinion_score is not None else None,
        "target_price": target_price,
        "eps": eps,
        "per": per,
        "analyst_count": analyst_count,
    }


def fetch_stock_detail(code: str) -> dict:
    with client() as c:
        resp = c.get(FRGN_URL.format(code=code))
        resp.raise_for_status()
    parsed = parse_investor_flow(resp.content)
    flow = parsed[:10]
    # 서브프로젝트 I — 5일 합·연속일수로 축약하고 버리던 일별 행을 최대 20일치
    # 보존한다(신규→과거 순). 외국인 수급 추종 라벨러(quant.analyze.foreign_trend)
    # 가 날짜별 유입/이탈 반전을 판정하려면 일별 원본이 있어야 하기 때문이다.
    flow_daily = [
        {"date": r["date"], "foreign_net": r["foreign_net"], "inst_net": r["inst_net"]}
        for r in parsed[:20]
    ]

    foreign_buy_streak = 0
    for r in flow:
        if r["foreign_net"] <= 0:
            break
        foreign_buy_streak += 1

    inst_buy_streak = 0
    for r in flow:
        if r["inst_net"] <= 0:
            break
        inst_buy_streak += 1

    flow_summary = {
        "foreign_net_5d": sum(r["foreign_net"] for r in flow[:5]),
        "inst_net_5d": sum(r["inst_net"] for r in flow[:5]),
        "foreign_buy_streak": foreign_buy_streak,
        "inst_buy_streak": inst_buy_streak,
    }

    consensus = None
    try:
        with client() as c:
            resp = c.get(
                CONSENSUS_URL.format(code=code),
                headers={"Referer": "https://finance.naver.com/"},
            )
            resp.raise_for_status()
        consensus = parse_consensus(resp.text)
    except Exception:
        consensus = None

    upside_pct = None
    if flow and consensus and consensus["target_price"] is not None:
        upside_pct = round((consensus["target_price"] / flow[0]["close"] - 1) * 100, 1)

    return {
        "code": code,
        "flow": flow,
        "flow_daily": flow_daily,
        "flow_summary": flow_summary,
        "consensus": consensus,
        "upside_pct": upside_pct,
    }


def fetch_many(codes: list[str], limit: int = 8) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for i, code in enumerate(codes[:limit]):
        if i > 0:
            time.sleep(0.3)
        try:
            results[code] = fetch_stock_detail(code)
        except Exception:
            continue
    return results
