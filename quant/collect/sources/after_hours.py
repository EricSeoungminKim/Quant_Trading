"""KR 장후 시간외단일가(16:30-18:00) 등락률 상위 — 키움 REST API.

## 왜 필요한가 (2026-08-13 조사 배경)
회사 08:00 리포트는 그 시각의 "실시간" 랭킹(토스)만 보는데, 그 랭킹은 전날
정규장 마감 시점 스냅샷이다(토스 `rankings` 는 `realtime`/`1d` 등 기간만
있고 장후 시간외 세션을 별도로 구분하지 않는다 — `quant/collect/sources/toss.py`
참고). 정작 눈에 띄는 신호는 정규장 마감 후 열리는 장후 시간외단일가
(16:30-18:00)에서 이미 나타난다 — 실측 사례: 한화생명(088350)이 8/12
정규장은 -0.5%로 평범했지만, 그날 저녁 시간외단일가에서 이미 +7.23%
(종가 4,700원 → 5,040원)로 움직였고, 다음날(8/13) +6.4% 갭업 후 장중
+18.3%(5,560원)까지 뛰었다. 08:00 빌드 시점에는 전날 저녁 데이터가 이미
존재하는데도 회사 리포트도 토스 랭킹도 이걸 보지 않았다(190건 뉴스 중 0건
언급, 랭킹 4종 전부 미등장).

## 실측 확인 (2026-08-13 10:09 KST, EC2 실전키·`api.kiwoom.com`)
`ka10098`(시간외단일가등락율순위요청, POST /api/dostk/rkinfo)을 정규장 도중에
호출했는데도 088350이 사라지지 않고 그대로 남아 있었다 — `cur_prc`/`flu_rt`가
전날 저녁 시간외단일가 마감값(+5,040원, +7.23%)과 정확히 일치했다(기준가
4,700원 대비: 4700×1.0723≈5040, `pred_pre`="+340"도 일치). 즉 이 엔드포인트는
다음 정규장이 열려도 바로 리셋되지 않고 전날 저녁 스냅샷을 유지한다 —
08:00/08:40 빌드 시점에도 조회 가능할 것으로 판단한다(정확히 08:00에
재검증하지는 못했다 — 그 시각에 한 번 더 확인 권장).

`ka10087`(시간외단일가요청, 종목별 단건)로도 088350 단건을 조회해 동일한
+5,040원/+7.23%를 확인했다 — 두 엔드포인트가 서로 정합적이다.

## 등락률 정렬만으로는 안 된다 — 유동성 가중이 필요한 이유
API 자체는 등락률(`sort_base`)로만 정렬 가능하고 거래량/거래대금 정렬은
지원하지 않는다. 등락률만으로 정렬하면 한화생명(+7.23%, 640,365주 체결)이
26위로 밀리고, 1주만 체결된 잡주(+10.00%, 1주)가 1위를 차지한다(실측,
2026-08-13). 그래서 상승률/하락률 각 최대 100건을 받아 유동성(누적거래대금)
가중 점수로 이 모듈이 직접 재정렬한다.

## 이 소스가 주지 못하는 것
외국인/기관 수급은 이 엔드포인트에 없다 — 키움 스펙 전체(337개 API)를
"시간외"로 훑어도 시간외단일가 세션 전용 수급 데이터는 존재하지 않는다
(`docs/api/kiwoom/README.md` 조사 당시 기준 208개, 이번 조사 시점 337개
API 카탈로그 직접 grep, 히트 3건: ka10087/ka10098/실시간 0E 뿐). 일별 수급
(ka10059, 국내 저장소가 이미 씀)은 있지만 그건 정규장 하루 전체 합산이라
"저녁 시간외" 구간만 떼어낼 수 없다 — 그래서 이 모듈의 반환값에는 수급
필드가 아예 없다(결측을 흉내내지 않는다).

## 인증 — 실계좌 시크릿, 신중하게 다룰 것
`.env.local.example` 헤더는 "이 파일은 리포트 박스 전용이다 — 거래 시크릿
(Toss/키움/계좌번호)은 여기 오지 않는다"고 명시한다. 토스는 이미 거래
엔진과 시크릿을 공유하는 선례가 있어(`toss.py` 상단 docstring 참고) 이
모듈도 같은 패턴(`KIWOOM_APP_KEY`/`KIWOOM_SECRET_KEY`, `get_key` 경유)을
따르지만, **공개 IP로 노출된 리포트 박스에 실계좌 키움 키를 실제로 배치할지는
이 모듈이 결정할 문제가 아니다** — 사람이 명시적으로 판단해야 한다. 이 파일은
키가 없으면 그냥 raise한다(다른 소스와 동일하게 `SourceResult(ok=False)`로
흡수됨).

## IP 화이트리스트
키움 REST는 앱키 발급 시 등록한 IP에서만 동작한다 — 로컬에서는 이 모듈의
라이브 호출이 실패하는 게 정상이다(`pytest.mark.live` 만 네트워크를 탄다).
"""
from __future__ import annotations

import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from quant.adapters.env import get_key
from quant.adapters.http import client

BASE_URL = "https://api.kiwoom.com"

# 정렬 기준(sort_base): 1=상승률, 3=하락률 (ka10098 스펙). 등락폭/보합은 이
# 모듈의 목적(움직인 종목 찾기)과 무관해 쓰지 않는다.
_RANK_DIRECTIONS = {"gainers": "1", "losers": "3"}

# 재정렬 후 상위 몇 개를 남길지 — 토스 랭킹 보드(count=10)보다 넉넉히 잡는다.
MOVERS_LIMIT = 30

_KST = ZoneInfo("Asia/Seoul")
_TOKEN_REFRESH_MARGIN_S = 30.0

# 액세스 토큰 메모리 캐시. 파일 캐시는 만들지 않는다 — 거래 엔진의 캐시와
# 섞이면 안 된다(toss.py와 동일한 방침).
_token_cache: dict[str, object] = {}


def _parse_expires_dt(expires_dt: str | None) -> float:
    """KST 'YYYYmmddHHMMSS' 만료 일시 문자열 -> epoch seconds.

    키움 공식 응답 필드명은 `expires_in`(초 단위 TTL)이 아니라 `expires_dt`
    (만료 "일시" 문자열)이다 — quant_trading_kiwoom의
    `quant/adapters/brokers/kiwoom/client.py`에서 실키로 검증된 것과 동일한
    파싱 로직. 파싱 실패 시 보수적으로 1시간 뒤로 본다.
    """
    if not expires_dt:
        return time.time() + 3600
    try:
        return datetime.strptime(str(expires_dt), "%Y%m%d%H%M%S").replace(tzinfo=_KST).timestamp()
    except ValueError:
        return time.time() + 3600


def _get_token(c, *, force: bool = False) -> str:
    if not force:
        expires_at = _token_cache.get("expires_at", 0.0)
        if _token_cache.get("token") and time.time() < expires_at:
            return _token_cache["token"]  # type: ignore[return-value]

    app_key = get_key("KIWOOM_APP_KEY")
    secret_key = get_key("KIWOOM_SECRET_KEY")
    if not app_key or not secret_key:
        raise RuntimeError("KIWOOM_APP_KEY/KIWOOM_SECRET_KEY 미설정")

    resp = c.post(
        f"{BASE_URL}/oauth2/token",
        json={"grant_type": "client_credentials", "appkey": app_key, "secretkey": secret_key},
    )
    resp.raise_for_status()
    body = resp.json()
    code = body.get("return_code")
    if code not in (None, 0):
        raise ValueError(f"키움 토큰 발급 오류 [{code}]: {body.get('return_msg', '')}")
    _token_cache["token"] = body["token"]
    _token_cache["expires_at"] = _parse_expires_dt(body.get("expires_dt")) - _TOKEN_REFRESH_MARGIN_S
    return _token_cache["token"]  # type: ignore[return-value]


def _request(c, api_id: str, path: str, body: dict) -> dict:
    token = _get_token(c)
    resp = c.post(f"{BASE_URL}{path}", json=body, headers={"api-id": api_id, "authorization": f"Bearer {token}"})
    if resp.status_code == 401:
        # 거래 엔진이 같은 앱키로 토큰을 재발급하면 이 프로세스의 캐시가 조용히
        # 무효화될 수 있다(toss.py와 동일한 우려) — 캐시 무시 재발급 후 1회 재시도.
        token = _get_token(c, force=True)
        resp = c.post(f"{BASE_URL}{path}", json=body, headers={"api-id": api_id, "authorization": f"Bearer {token}"})
    resp.raise_for_status()
    data = resp.json()
    code = data.get("return_code")
    if code not in (None, 0):
        raise ValueError(f"키움 API 오류 [{code}]: {data.get('return_msg', '')}")
    return data


def _parse_row(row: dict) -> dict:
    """ka10098 응답 1행 -> 파싱된 dict. 숫자 필드는 부호(+/-) 접두사가 붙어
    올 수 있는데 Python `int()`/`float()`가 선행 `+`를 그대로 처리하므로
    별도 strip이 필요 없다(실측 확인: `int("+5040")==5040`).

    `acc_trde_prica`(누적거래대금)는 스펙상 단위가 백만원이라 원 단위로
    환산한다. `score`는 등락률 절대값에 거래대금의 로그를 곱한 유동성 가중
    점수 — 1주만 체결된 잡주가 상위를 차지하는 문제(모듈 docstring 참고)를
    막기 위한 것으로, 이 모듈이 직접 정의한 값이지 키움 API가 주는 값이 아니다.
    """
    trading_value_krw = int(row["acc_trde_prica"]) * 1_000_000
    change_pct = float(row["flu_rt"])
    score = round(abs(change_pct) * math.log1p(trading_value_krw), 4)
    return {
        "symbol": row["stk_cd"],
        "name": row["stk_nm"],
        "price": int(row["cur_prc"]),
        "change_pct": change_pct,
        "prev_close": int(row["tdy_close_pric"]),
        "volume": int(row["acc_trde_qty"]),
        "trading_value_krw": trading_value_krw,
        "score": score,
    }


def fetch_after_hours_movers() -> dict:
    """장후 시간외단일가(16:30-18:00) 등락률 상위를 상승/하락 합쳐 유동성 가중
    점수로 반환한다.

    ka10098을 두 번 호출(상승률/하락률 정렬)해 병합한다. 한쪽 방향이 실패해도
    나머지로 계속 진행하고, 둘 다 실패하면 raise한다(다른 소스와 동일한
    "부분 실패는 흡수, 전부 실패는 raise" 관례 — `toss.fetch_rankings` 참고).
    """
    rows: list[dict] = []
    with client() as c:
        for sort_base in _RANK_DIRECTIONS.values():
            try:
                data = _request(c, "ka10098", "/api/dostk/rkinfo", {
                    "mrkt_tp": "000", "sort_base": sort_base, "stk_cnd": "0",
                    "trde_qty_cnd": "0", "crd_cnd": "0", "trde_prica": "0",
                })
            except Exception:
                continue
            rows.extend(_parse_row(r) for r in data.get("ovt_sigpric_flu_rt_rank", []))
    if not rows:
        raise ValueError("키움 시간외단일가 순위를 하나도 못 가져왔다")

    # 상승률/하락률 두 조회가 같은 종목을 다시 줄 수 있다 — 실측에서는 안
    # 겹쳤지만(상승률 조회엔 상승 종목만, 하락률 조회엔 하락 종목만 나옴)
    # 방어적으로 심볼당 최고 점수만 남긴다.
    best: dict[str, dict] = {}
    for r in rows:
        cur = best.get(r["symbol"])
        if cur is None or r["score"] > cur["score"]:
            best[r["symbol"]] = r

    movers = sorted(best.values(), key=lambda r: r["score"], reverse=True)[:MOVERS_LIMIT]
    return {"session": "장후시간외단일가", "movers": movers}
