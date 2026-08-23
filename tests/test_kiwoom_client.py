"""KiwoomClient 인증 계층 테스트. 네트워크 없음 (httpx MockTransport)."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from quant.adapters.brokers.kiwoom.client import (
    KiwoomClient,
    KiwoomError,
    _parse_expires_dt,
    _raise_for_kiwoom_error,
)

KST = ZoneInfo("Asia/Seoul")


def _client(tmp_path, handler) -> KiwoomClient:
    c = KiwoomClient(app_key="ak", secret_key="sk", base_url="https://mock.test", cache_dir=tmp_path)
    c._http = httpx.Client(base_url="https://mock.test", transport=httpx.MockTransport(handler))
    return c


def test_business_error_arrives_as_http_200():
    """키움은 실패해도 HTTP 200을 준다 — 2026-07-28 실측 페이로드 그대로."""
    body = {
        "return_msg": "입력 값 오류입니다[8030:투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다]",
        "return_code": 2,
    }
    with pytest.raises(KiwoomError) as exc:
        _raise_for_kiwoom_error(body)
    assert exc.value.code == 2
    assert "투자구분" in exc.value.message


def test_success_codes_pass_through():
    _raise_for_kiwoom_error({"return_code": 0, "token": "t"})
    _raise_for_kiwoom_error({"token": "t"})  # return_code 자체가 없는 응답


def test_fetch_token_rejects_mismatched_key_env(tmp_path):
    """실전 키로 모의서버 호출 시 토큰 발급 단계에서 막혀야 한다."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"return_code": 2, "return_msg": "투자구분 불일치"})

    with pytest.raises(KiwoomError):
        _client(tmp_path, handler)._fetch_token()


def test_expires_dt_is_a_kst_timestamp_not_a_duration():
    """OAuth2의 expires_in(초)이 아니라 KST 만료 시각 문자열이다."""
    expected = datetime(2026, 7, 28, 23, 59, 59, tzinfo=KST).timestamp()
    assert _parse_expires_dt("20260728235959") == pytest.approx(expected)


def test_expires_dt_missing_or_malformed_falls_back_conservatively():
    import time

    now = time.time()
    for bad in (None, "", "not-a-datetime", "2026-07-28"):
        parsed = _parse_expires_dt(bad)
        assert now + 3500 < parsed < now + 3700  # 약 1시간


def test_token_is_cached_and_reused(tmp_path):
    calls = {"n": 0}
    future = datetime(2099, 1, 1, 0, 0, 0, tzinfo=KST).strftime("%Y%m%d%H%M%S")

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"token": "tok", "token_type": "Bearer", "expires_dt": future})

    client = _client(tmp_path, handler)
    assert client._ensure_token() == "tok"
    assert client._ensure_token() == "tok"
    assert calls["n"] == 1

    # 별도 인스턴스가 디스크 캐시를 재사용한다
    fresh = _client(tmp_path, handler)
    assert fresh._ensure_token() == "tok"
    assert calls["n"] == 1


# ---------------------------------------------------------------- usa_chart_1m

def _token_and_chart_handler(chart_body: dict, captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth2/token":
            return httpx.Response(200, json={
                "token": "tok", "token_type": "Bearer",
                "expires_dt": datetime(2099, 1, 1, tzinfo=KST).strftime("%Y%m%d%H%M%S"),
            })
        assert request.url.path == "/api/us/chart"
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=chart_body)
    return handler


def test_usa_chart_1m_sends_api_id_header_and_request_body(tmp_path):
    captured: dict = {}
    body = {"return_code": 0, "result_list": [{"cur_prc": "218.5050", "cntr_tm": "20260811114600"}]}
    client = _client(tmp_path, _token_and_chart_handler(body, captured))

    rows = client.usa_chart_1m("NVDA", exchange="ND")

    assert rows == body["result_list"]
    assert captured["headers"]["api-id"] == "usa06010"
    assert captured["headers"]["authorization"] == "Bearer tok"
    assert captured["body"] == {
        "stex_tp": "ND", "stk_cd": "NVDA", "tic_scope": "1",
        "upd_stkpc_tp": "1", "exrt_appl_tp": "1",
    }


def test_usa_chart_1m_raises_kiwoom_error_on_business_failure(tmp_path):
    captured: dict = {}
    body = {"return_code": 3, "return_msg": "종목코드 오류"}
    client = _client(tmp_path, _token_and_chart_handler(body, captured))

    with pytest.raises(KiwoomError):
        client.usa_chart_1m("NOPE")


def test_usa_chart_1m_raises_when_result_list_field_is_missing(tmp_path):
    """return_code=0(성공)인데 예상 필드가 없으면 빈 값으로 위장하지 않고 에러를 던진다."""
    captured: dict = {}
    body = {"return_code": 0}  # result_list 없음
    client = _client(tmp_path, _token_and_chart_handler(body, captured))

    with pytest.raises(KiwoomError):
        client.usa_chart_1m("NVDA")
