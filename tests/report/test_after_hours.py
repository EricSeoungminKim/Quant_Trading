import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from quant.adapters.env import get_key
from quant.collect.sources.after_hours import (
    MOVERS_LIMIT,
    _parse_expires_dt,
    _parse_row,
    _request,
    fetch_after_hours_movers,
)

GAINERS_FIXTURE = Path(__file__).parent / "fixtures" / "kiwoom_after_hours_gainers.json"
LOSERS_FIXTURE = Path(__file__).parent / "fixtures" / "kiwoom_after_hours_losers.json"


@pytest.fixture
def gainers_fixture():
    return json.loads(GAINERS_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def losers_fixture():
    return json.loads(LOSERS_FIXTURE.read_text(encoding="utf-8"))


# --- _parse_row: 부호 붙은 숫자 문자열 파싱 + 유동성 가중 점수 -------------------


def test_parse_row_handles_signed_numeric_strings(gainers_fixture):
    # 088350 한화생명 실측 행 — 2026-08-13 EC2 실전키로 확인된 실제 응답.
    row = gainers_fixture["ovt_sigpric_flu_rt_rank"][3]
    parsed = _parse_row(row)
    assert parsed["symbol"] == "088350"
    assert parsed["name"] == "한화생명"
    assert parsed["price"] == 5040
    assert parsed["change_pct"] == 7.23
    assert parsed["prev_close"] == 5170
    assert parsed["volume"] == 640365
    assert parsed["trading_value_krw"] == 3259 * 1_000_000


def test_parse_row_liquidity_score_demotes_illiquid_mover(gainers_fixture):
    # 실측 함정: 1주만 체결된 잡주가 +10.00%로 등락률 1위를 차지하지만,
    # 유동성 가중 점수로는 64만주 체결된 한화생명(+7.23%)에 밀려야 한다.
    rows = gainers_fixture["ovt_sigpric_flu_rt_rank"]
    illiquid = _parse_row(rows[0])  # 0088N0, 1주 체결, +10.00%
    hanwha = _parse_row(rows[3])  # 088350, 64만주 체결, +7.23%
    assert illiquid["change_pct"] > hanwha["change_pct"]
    assert illiquid["score"] < hanwha["score"]


# --- _request: return_code 검사 (HTTP 200이어도 업무 오류면 raise) --------------


def test_request_raises_on_nonzero_return_code():
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"return_code": 3, "return_msg": "종목코드 오류"}

    class FakeClient:
        def post(self, url, json=None, headers=None):
            return FakeResponse()

    with patch("quant.collect.sources.after_hours._get_token", return_value="tok"):
        with pytest.raises(ValueError, match="종목코드 오류"):
            _request(FakeClient(), "ka10098", "/api/dostk/rkinfo", {})


def test_request_retries_once_after_401_with_forced_token_refresh():
    class FakeResponse:
        def __init__(self, status_code, body):
            self.status_code = status_code
            self._body = body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("error", request=None, response=self)

        def json(self):
            return self._body

    responses = [
        FakeResponse(401, {"return_code": 1, "return_msg": "토큰 만료"}),
        FakeResponse(200, {"return_code": 0, "return_msg": "", "ovt_sigpric_flu_rt_rank": []}),
    ]

    class FakeClient:
        def post(self, url, json=None, headers=None):
            return responses.pop(0)

    tokens = iter(["stale-cached-token", "freshly-reissued-token"])
    with patch(
        "quant.collect.sources.after_hours._get_token",
        side_effect=lambda c, force=False: next(tokens),
    ) as m:
        data = _request(FakeClient(), "ka10098", "/api/dostk/rkinfo", {})
    assert data["ovt_sigpric_flu_rt_rank"] == []
    assert m.call_args_list[1].kwargs.get("force") is True


# --- _parse_expires_dt: 키움 고유 만료 "일시" 문자열 파싱 -----------------------


def test_parse_expires_dt_parses_kst_datetime_string():
    epoch = _parse_expires_dt("20260813235959")
    assert epoch > 0


def test_parse_expires_dt_falls_back_on_missing_or_malformed():
    import time

    now = time.time()
    assert _parse_expires_dt(None) > now
    assert _parse_expires_dt("garbage") > now


# --- fetch_after_hours_movers: 병합 + 재정렬 + 부분 실패 흡수 ------------------


def test_fetch_after_hours_movers_merges_gainers_and_losers(gainers_fixture, losers_fixture):
    with patch("quant.collect.sources.after_hours.client"):
        with patch(
            "quant.collect.sources.after_hours._request",
            side_effect=[gainers_fixture, losers_fixture],
        ):
            result = fetch_after_hours_movers()
    assert result["session"] == "장후시간외단일가"
    symbols = [m["symbol"] for m in result["movers"]]
    assert "088350" in symbols
    assert "900001" in symbols  # 하락 종목도 병합됨
    # 유동성 가중 점수 내림차순 — 한화생명이 잡주(0088N0)보다 앞에 와야 한다.
    assert symbols.index("088350") < symbols.index("0088N0")


def test_fetch_after_hours_movers_caps_at_limit(gainers_fixture, losers_fixture):
    with patch("quant.collect.sources.after_hours.client"):
        with patch(
            "quant.collect.sources.after_hours._request",
            side_effect=[gainers_fixture, losers_fixture],
        ):
            result = fetch_after_hours_movers()
    assert len(result["movers"]) <= MOVERS_LIMIT


def test_fetch_after_hours_movers_one_direction_failure_does_not_kill_the_other(
    gainers_fixture,
):
    def side_effect(*args, **kwargs):
        # 첫 호출(상승률)은 성공, 두 번째(하락률)는 실패.
        if side_effect.calls == 0:
            side_effect.calls += 1
            return gainers_fixture
        raise RuntimeError("network down")

    side_effect.calls = 0

    with patch("quant.collect.sources.after_hours.client"):
        with patch("quant.collect.sources.after_hours._request", side_effect=side_effect):
            result = fetch_after_hours_movers()
    assert any(m["symbol"] == "088350" for m in result["movers"])


def test_fetch_after_hours_movers_all_directions_failing_raises():
    with patch("quant.collect.sources.after_hours.client"):
        with patch("quant.collect.sources.after_hours._request", side_effect=RuntimeError("down")):
            with pytest.raises(ValueError, match="키움 시간외단일가"):
                fetch_after_hours_movers()


def test_fetch_after_hours_movers_dedupes_symbol_keeping_higher_score(gainers_fixture):
    # 동일 종목이 상승률/하락률 두 조회 모두에 나타나는 방어적 케이스 —
    # 점수가 더 높은 쪽만 남아야 한다.
    dup_response = {
        "ovt_sigpric_flu_rt_rank": [gainers_fixture["ovt_sigpric_flu_rt_rank"][3]],  # 088350
        "return_code": 0,
        "return_msg": "",
    }
    low_score_dup = {
        "ovt_sigpric_flu_rt_rank": [
            {**gainers_fixture["ovt_sigpric_flu_rt_rank"][3], "flu_rt": "+0.10", "acc_trde_prica": "1"}
        ],
        "return_code": 0,
        "return_msg": "",
    }
    with patch("quant.collect.sources.after_hours.client"):
        with patch(
            "quant.collect.sources.after_hours._request",
            side_effect=[dup_response, low_score_dup],
        ):
            result = fetch_after_hours_movers()
    matches = [m for m in result["movers"] if m["symbol"] == "088350"]
    assert len(matches) == 1
    assert matches[0]["change_pct"] == 7.23  # 낮은 점수 중복이 아니라 높은 쪽이 남음


# --- 라이브 (자격증명 없거나 IP 화이트리스트 밖이면 skip) -----------------------


def _skip_if_unreachable(fn, *args):
    try:
        return fn(*args)
    except (RuntimeError, ValueError) as e:
        pytest.skip(str(e))
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code in (401, 403):
            pytest.skip("IP 화이트리스트/시크릿 문제 — 로컬에서는 실패가 정상")
        raise


@pytest.mark.live
def test_live_fetch_after_hours_movers():
    if not get_key("KIWOOM_APP_KEY") or not get_key("KIWOOM_SECRET_KEY"):
        pytest.skip("KIWOOM_APP_KEY/KIWOOM_SECRET_KEY 미설정")
    result = _skip_if_unreachable(fetch_after_hours_movers)
    assert result["movers"]
