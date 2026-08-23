"""Kiwoom REST API 어댑터 — Phase 3 스켈레톤.

인증 부분만 실동작한다. quote/candles/place_order는 NotImplementedError.
스펙 근거는 docs/api/kiwoom/README.md (키움 공식 SDK 소스에서 추출).

실키 발급 전이라 미검증인 것과, 실측으로 확인된 것을 구분해 둔다:
- 확인됨: 토큰 응답이 OAuth2 표준과 다르다 (token/expires_dt).
- 확인됨: 키움은 업무 에러에도 HTTP 200을 주고 본문 return_code로 알린다.
- 미검증: 해외주식(TQQQ) 지원 범위. 공식 포털에는 미국주식 카테고리
  (usa06011 분차트 등)가 있으나 공식 SDK의 스펙 카탈로그 208건에는 국내
  206건 + OAuth 2건뿐이고 미국주식이 0건이다. 일차 출처끼리 상충하므로,
  부작용 없는 조회 TR로 모의서버에서 먼저 확인한 뒤 어댑터를 만들 것.

주의: KIWOOM_APP_KEY는 실전/모의가 분리 발급된다. 실전 키로 mockapi에
호출하면 토큰 발급부터 거부된다 (2026-07-28 실측, 아래 _raise_for_kiwoom_error 참고).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from quant.adapters.env import REPO_ROOT
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://mockapi.kiwoom.com"
# 저장소 루트는 adapters.env.REPO_ROOT 한 곳에서만 센다 — parents[3] 은
# `quant/` 였고, 토큰 캐시가 quant/data/cache 에 따로 쌓여 갈렸다
# (토스는 client_credentials 당 토큰이 1개라 캐시가 둘이면 서로를 무효화한다).
DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache"

_NOT_IMPLEMENTED_MSG = "Kiwoom API 연동은 Phase 3 — API 키 발급 후 엔드포인트 검증 필요"
_KST = ZoneInfo("Asia/Seoul")


class KiwoomError(RuntimeError):
    """키움이 반환한 업무 에러 (HTTP는 200이지만 return_code != 0)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _raise_for_kiwoom_error(body: dict) -> None:
    """키움은 실패해도 HTTP 200을 준다 — 본문의 return_code로 판별해야 한다.

    2026-07-28 실측: 실전용 appkey로 mockapi.kiwoom.com에 토큰을 요청하면
    HTTP 200 + {"return_code": 2, "return_msg": "입력 값 오류입니다[8030:
    투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다]"}가 온다.
    raise_for_status()만 믿으면 이 실패를 성공으로 오인한다.
    """
    code = body.get("return_code")
    if code not in (None, 0):
        raise KiwoomError(int(code), str(body.get("return_msg", "")))


def _parse_expires_dt(expires_dt: str | None) -> float:
    """KST '%Y%m%d%H%M%S' 만료 시각 → epoch seconds. 파싱 실패 시 보수적으로 1시간."""
    if not expires_dt:
        return time.time() + 3600
    try:
        return datetime.strptime(str(expires_dt), "%Y%m%d%H%M%S").replace(tzinfo=_KST).timestamp()
    except ValueError:
        return time.time() + 3600


class KiwoomClient:
    def __init__(
        self,
        app_key: str,
        secret_key: str,
        base_url: str | None = None,
        cache_dir: Path = DEFAULT_CACHE_DIR,
    ) -> None:
        self.app_key = app_key
        self.secret_key = secret_key
        self.base_url = base_url or os.environ.get("KIWOOM_BASE_URL", DEFAULT_BASE_URL)
        self._cache_dir = Path(cache_dir)
        self._token_path = self._cache_dir / "kiwoom_token.json"
        self._http = httpx.Client(base_url=self.base_url, timeout=10.0)
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ auth
    def _load_token_cache(self) -> dict | None:
        try:
            return json.loads(self._token_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _save_token_cache(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._token_path.write_text(json.dumps({
            "access_token": self._access_token,
            "expires_at": self._expires_at,
            # 모의(mockapi)/실전(api) 토큰은 호환되지 않는다 — base_url이 다르면
            # 캐시를 버려야 한다 (2026-08-10 실측: mock 토큰을 실전에 재사용해
            # 조용히 실패, 종목별 수급이 폴백으로 빠짐).
            "base_url": self.base_url,
        }))

    def invalidate_token(self) -> None:
        """캐시된 토큰을 버려 다음 요청이 새로 발급받게 한다.

        **왜 필요한가 (2026-08-11~20 실측, 9일간 방치됨)**: 키움은 새 토큰을
        발급하면 이전 토큰을 무효화하는 것으로 보인다(Toss `client_credentials`
        와 같은 부류). 그런데 우리는 만료 시각을 **로컬 시계로만** 판단해
        캐시를 재사용했다 — 서버가 이미 폐기한 토큰을 "아직 안 만료됐다"고
        믿고 영원히 다시 쓴 것이다.

        결과: 웹소켓 LOGIN 이 `[805004] Token이 유효하지 않습니다` 로 9일간
        거부됐고(하루 243회), 그동안 실시간 시세 없이 REST 폴백으로만 돌았다.
        REST 는 같은 키로 683건 성공하고 있었으므로 키 문제가 아니었다 —
        **캐시를 버릴 길이 없다는 것이 문제였다.**

        호출부(웹소켓 피드)가 인증 실패를 감지하면 이걸 부르고 재접속한다.
        """
        self._access_token = None
        self._expires_at = 0.0
        try:
            self._token_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:  # noqa: BLE001 — 캐시 삭제 실패가 재접속을 막으면 안 된다
            logger.warning("키움 토큰 캐시 삭제 실패(무시): %s", e)

    def _fetch_token(self) -> str:
        """POST /oauth2/token → {token, token_type, expires_dt}.

        응답 스키마는 키움 공식 SDK 소스(Kiwoom-Securities/Kiwoom-REST-API,
        kiwoom/core/auth.py)에서 확인했다. OAuth2 표준과 다르다:
        access_token/expires_in이 아니라 token/expires_dt이며, expires_dt는
        초 단위 수명이 아니라 KST 만료 시각 문자열(%Y%m%d%H%M%S)이다.
        """
        resp = self._http.post("/oauth2/token", json={
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.secret_key,
        })
        resp.raise_for_status()
        body = resp.json()
        _raise_for_kiwoom_error(body)
        self._access_token = body["token"]
        self._expires_at = _parse_expires_dt(body.get("expires_dt")) - 30  # 30초 일찍 갱신
        self._save_token_cache()
        return self._access_token

    def _ensure_token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        cached = self._load_token_cache()
        if (
            cached
            and time.time() < cached.get("expires_at", 0)
            and cached.get("base_url") == self.base_url  # 모의/실전 토큰 교차 사용 금지
        ):
            self._access_token = cached["access_token"]
            self._expires_at = cached["expires_at"]
            return self._access_token
        return self._fetch_token()

    def access_token(self) -> str:
        """실시간 웹소켓 LOGIN처럼 REST 밖에서 토큰이 필요한 곳을 위한 공개 접근자.

        `_ensure_token()`과 동일 — 캐시/만료 처리를 그대로 재사용한다."""
        return self._ensure_token()

    # ---------------------------------------------------- 종목별 수급 (검증됨)
    def investor_flow_daily(self, symbol: str, date_yyyymmdd: str) -> list[dict]:
        """ka10059 종목별투자자기관별요청 — 일별 개인/외국인/기관(세부 7분류) 순매수.

        2026-08-10 실전 서버(api.kiwoom.com) 실키로 검증됨: return_code 0,
        rows 100개, 필드 dt/cur_prc/frgnr_invsr(외국인)/orgn(기관 합계)/
        pen_fund(연기금)/pri_fund(사모펀드) 등. amt_qty_tp="1"(수량),
        trde_tp="0"(순매수), unit_tp="1000". 모의서버도 같은 스키마로 응답하지만
        수치는 합성값이다 — 숫자를 믿는 건 실전 base_url일 때만.

        조회 전용 — 주문 아님. 실패는 KiwoomError로 던진다(호출부 어댑터에서 흡수).
        """
        token = self._ensure_token()
        resp = self._http.post(
            "/api/dostk/stkinfo",
            json={
                "dt": date_yyyymmdd, "stk_cd": symbol,
                "amt_qty_tp": "1", "trde_tp": "0", "unit_tp": "1000",
            },
            headers={"api-id": "ka10059", "authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        _raise_for_kiwoom_error(body)
        for value in body.values():
            if isinstance(value, list):
                return value
        return []

    # ---------------------------------------------- 해외주식(미국) 분봉 (검증됨)
    def usa_chart_1m(self, symbol: str, exchange: str = "ND") -> list[dict]:
        """usa06010 미국주식틱차트조회 — POST /api/us/chart, header api-id=usa06010.

        2026-08-12 실전 서버(api.kiwoom.com) 해외증권(global) 키로 검증됨(NVDA,
        stex_tp=ND): return_code=0, result_list 100행. 각 행:
        {"cur_prc": "218.5050", "trde_qty": "609", "open_pric": ..., "high_pric": ...,
        "low_pric": ..., "cntr_tm": "20260811114600", "bus_dt": "20260811", ...}.
        가격은 문자열이고 부호(+/-)가 붙을 수 있다(실측 "+304.3400") — 호출부가 abs로
        정규화한다. cntr_tm(YYYYMMDDHHMMSS)은 [미확인] 미국 동부시간으로 보인다
        (실측 20260811114600이 11:46 ET와 일치하는 정황) — 호출부(us_datafeed.py)가
        America/New_York으로 해석한다.

        100행보다 더 과거를 페이지네이션으로 받을 수 있는지는 [미확인] — 여기서는
        시도하지 않는다(brokers/CLAUDE.md: 미검증 스펙 위에 기능을 넓히지 않는다).
        exchange(stex_tp): NA/ND/NY 중 하나로 보이나 정확한 거래소 매핑은 [미확인] —
        "ND"는 NASDAQ 종목(NVDA)으로 검증됨.

        조회 전용 — 주문 아님. 실패는 KiwoomError로 던진다(호출부 어댑터에서 흡수).
        """
        token = self._ensure_token()
        resp = self._http.post(
            "/api/us/chart",
            json={
                "stex_tp": exchange, "stk_cd": symbol, "tic_scope": "1",
                "upd_stkpc_tp": "1", "exrt_appl_tp": "1",
            },
            headers={"api-id": "usa06010", "authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        body = resp.json()
        _raise_for_kiwoom_error(body)
        rows = body.get("result_list")
        if rows is None:
            raise KiwoomError(-1, "usa06010 응답에 result_list 필드가 없음 — 스키마 변경 가능성")
        return rows

    # -------------------------------------------------------------- stubs
    def quote(self, symbol: str) -> dict:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def candles(self, symbol: str, interval: str = "1m", count: int = 120):
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)

    def place_order(self, symbol: str, side: str, order_type: str, **kwargs) -> dict:
        raise NotImplementedError(_NOT_IMPLEMENTED_MSG)
