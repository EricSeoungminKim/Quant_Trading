"""개별 종목 투자자 수급 + 애널리스트 컨센서스 — 네이버금융 / 컴퍼니가이드.

수급은 finance.naver.com/item/frgn.naver(외국인/기관 매매동향), 컨센서스는
navercomp.wisereport.co.kr 의 컴퍼니가이드(c1010001.aspx)를 쓴다. 둘 다 실측
검증됨(2026-08-12). 컨센서스는 상장 초기·소형주에서 흔히 비어 있으므로 실패해도
수급까지 죽이면 안 된다 — fetch_stock_detail 이 개별로 감싼다.
"""
from __future__ import annotations

import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor, wait

from quant.adapters.http import client

FRGN_URL = "https://finance.naver.com/item/frgn.naver?code={code}"
CONSENSUS_URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
OPINION_LABELS = ("강력매도", "매도", "중립", "매수", "강력매수")

# fetch_many 전체 소요 시간 상한(2026-09-07 Phase 2 §5, SUMMARY.md §⑤ 시간 예산
# 가드) — 08:12 own_brief.sh 소비 전까지 리포트 빌드가 끝나야 하므로, 네이버가
# 느려지거나(client() 기본 timeout=20초) 상한(60종목)에 가까운 날 이 한 호출이
# 예산을 통째로 삼키면 안 된다. 병렬화(max_workers=4) 이후 정상 상황에서는
# 60종목도 수 초면 끝나 이 값을 건드릴 일이 거의 없다 — 이건 정상 경로의
# 최적화가 아니라 네트워크 저하 시의 안전판이다.
FETCH_MANY_TIME_BUDGET_S = 20.0

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


def fetch_many(
    codes: list[str], limit: int = 8, *, time_budget_s: float = FETCH_MANY_TIME_BUDGET_S,
) -> dict[str, dict]:
    """`codes[:limit]` 개 종목을 병렬로 조회한다 (2026-09-07 Phase 2 §5 수리 —
    원래 순차 + 심볼당 0.3초 슬립이라 상한 60에서 최대 ~35초가 걸렸다).

    `max_workers=4`로 동시 접속을 제한한다 — 무제한 병렬은 네이버 예절 문제고
    (모듈독스트링), 이 모듈에 문서화된 명시적 요청 상한은 없지만 순차 버전의
    "한 번에 하나씩" 기조를 완전히 버리지 않기 위한 보수적 상한이다. 심볼 하나의
    실패(예외)가 나머지를 막지 않는다(기존 동작 그대로) — `future.result()`를
    개별로 감싼다. 반환 dict 는 완료 순서가 아니라 **입력 `codes` 순서**로
    재구성한다 — 호출부가 순서에 의존하지 않더라도 기존 계약(순차 실행 시의
    삽입 순서)을 유지한다.

    **시간 예산(`time_budget_s`, 기본 `FETCH_MANY_TIME_BUDGET_S`)** — 전체가
    이 시간 안에 안 끝나면 아직 안 끝난 종목은 포기하고 그때까지 완료된 것만
    반환한다(그 심볼들은 결측 — 0 이나 캐시값으로 위장하지 않는다). 정상적인
    병렬 실행이라면 60종목도 몇 초면 끝나 거의 발동하지 않는다 — 네이버가
    느려지거나(개별 요청 timeout=20초) 응답이 없을 때의 안전판이다. 예산을
    넘기면 `"수급 조회 N/전체 (시간 예산)"`을 stderr 에 남긴다.
    """
    targets = codes[:limit]
    if not targets:
        return {}
    done: dict[str, dict] = {}
    executor = ThreadPoolExecutor(max_workers=4)
    try:
        future_to_code = {executor.submit(fetch_stock_detail, code): code for code in targets}
        finished, pending = wait(future_to_code, timeout=time_budget_s)
        for future in finished:
            code = future_to_code[future]
            try:
                done[code] = future.result()
            except Exception:
                continue
        if pending:
            for future in pending:
                future.cancel()
            print(f"수급 조회 {len(done)}/{len(targets)} (시간 예산)", file=sys.stderr)
    finally:
        # 아직 시작 안 한 작업만 취소되고(cancel_futures, py3.9+), 이미 실행
        # 중인 스레드는 백그라운드에서 자연 종료된다 — 여기서 더 기다리지 않는다.
        executor.shutdown(wait=False, cancel_futures=True)
    return {code: done[code] for code in targets if code in done}
