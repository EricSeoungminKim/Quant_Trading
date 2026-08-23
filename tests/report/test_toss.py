import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from quant.adapters.env import get_key
from quant.collect.sources.toss import (
    FORBIDDEN,
    WARNING_LABELS,
    _check_path,
    _investor_net,
    _parse_ranking_item,
    _request,
    _warning_label,
    fetch_investor_trading,
    fetch_rankings,
    fetch_usd_krw,
    fetch_warnings,
)

FIXTURE = Path(__file__).parent / "fixtures" / "toss_rankings.json"


@pytest.fixture
def rankings_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- 금지 경로 차단 (최우선) ---------------------------------------------


@pytest.mark.parametrize("path", [*FORBIDDEN, "/api/v1/stocks/005930"])
def test_forbidden_paths_raise_permission_error(path):
    # 계좌 민감 경로(그리고 warnings 가 아닌 stocks/ 서브패스)는 네트워크를
    # 만지기 전에 막혀야 한다 — 클라이언트가 None 이어도 여기서 raise 되어야 통과.
    with pytest.raises(PermissionError):
        _request(None, path)


@pytest.mark.parametrize(
    "path",
    [
        # httpx가 실제 요청 시 ".." 를 정규화하므로, 문자열 검사만으로는
        # stocks//market-indicators 접두사를 통과해도 실제로는 금지 엔드포인트로
        # 나간다(실측: httpx.Request(...).url.path == "/api/v1/holdings").
        "/api/v1/stocks/../holdings",
        "/api/v1/stocks/../holdings/warnings",
        "/api/v1/stocks/005930/warnings/../../holdings",
        "/api/v1/market-indicators/../holdings",
        "/api/v1/market-indicators/KOSPI/../../holdings",
    ],
)
def test_path_traversal_bypass_is_blocked(path):
    with pytest.raises(PermissionError):
        _check_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/rankings",
        "/api/v1/exchange-rate",
        "/api/v1/stocks/005930/warnings",
        "/api/v1/market-indicators/KOSPI/investor-trading",
    ],
)
def test_allowed_paths_pass_check(path):
    _check_path(path)  # raise 하지 않으면 통과


# --- _request: 모든 응답의 {"result": ...} 래핑을 벗기는지 (실측 확인된 계약) ----


def test_request_unwraps_result_envelope():
    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"rate": "1417"}}

    class FakeClient:
        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("quant.collect.sources.toss._get_token", return_value="tok"):
        data = _request(FakeClient(), "/api/v1/exchange-rate")
    assert data == {"rate": "1417"}


# --- _request: 401은 캐시 무시 재발급 후 1회 재시도 (실측 확인된 회귀 — 거래
# 엔진과 client_id/secret을 공유하므로 엔진이 재발급하면 이 프로세스의 캐시된
# 토큰이 조용히 무효화될 수 있다) ------------------------------------------


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

    responses = [FakeResponse(401, {"error": {"code": "expired-token"}}),
                 FakeResponse(200, {"result": {"rate": "1417"}})]

    class FakeClient:
        def get(self, url, params=None, headers=None):
            return responses.pop(0)

    tokens = iter(["stale-cached-token", "freshly-reissued-token"])
    with patch("quant.collect.sources.toss._get_token", side_effect=lambda c, force=False: next(tokens)) as m:
        data = _request(FakeClient(), "/api/v1/exchange-rate")
    assert data == {"rate": "1417"}
    # 두 번째 호출은 반드시 force=True로 캐시를 무시해야 한다 — 그렇지 않으면
    # 같은(무효화된) 캐시 토큰을 그대로 재사용해 다시 401을 받는다.
    assert m.call_args_list[1].kwargs.get("force") is True


def test_request_raises_if_401_persists_after_forced_refresh():
    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            raise httpx.HTTPStatusError("still unauthorized", request=None, response=self)

        def json(self):
            return {"error": {"code": "invalid-token"}}

    class FakeClient:
        def get(self, url, params=None, headers=None):
            return FakeResponse(401)

    with patch("quant.collect.sources.toss._get_token", return_value="tok"):
        with pytest.raises(httpx.HTTPStatusError):
            _request(FakeClient(), "/api/v1/exchange-rate")


# --- 경고 라벨 매핑 ---------------------------------------------------------


def test_warning_labels_known_codes():
    assert _warning_label("INVESTMENT_WARNING") == "투자경고"
    assert _warning_label("LIQUIDATION_TRADING") == "정리매매"


def test_warning_label_vi_prefix_maps_to_변동성완화():
    assert _warning_label("VI_DYNAMIC") == "변동성완화"
    assert _warning_label("VI_STATIC") == "변동성완화"


def test_warning_label_unknown_code_falls_back_to_code_itself():
    assert _warning_label("SOME_NEW_CODE") == "SOME_NEW_CODE"


def test_warning_labels_covers_documented_types():
    assert set(WARNING_LABELS) == {
        "LIQUIDATION_TRADING",
        "OVERHEATED",
        "INVESTMENT_WARNING",
        "INVESTMENT_RISK",
        "STOCK_WARRANTS",
    }


# --- 랭킹 파싱: changeRate 문자열 -> %, 금액 문자열 -> 숫자 ------------------


def test_parse_ranking_item_converts_change_rate_to_percent(rankings_fixture):
    item = _parse_ranking_item(rankings_fixture["rankings"][0])
    assert item["symbol"] == "000660"
    assert item["price"] == 1516000.0
    assert item["change_pct"] == 6.38
    assert item["trading_amount"] == 105702242000


def test_parse_ranking_item_second_row(rankings_fixture):
    item = _parse_ranking_item(rankings_fixture["rankings"][1])
    assert item["change_pct"] == 1.02


# --- fetch_rankings: 보드별 실패 흡수, 전부 실패시 raise --------------------


def test_fetch_rankings_parses_all_boards(rankings_fixture):
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", return_value=rankings_fixture) as m:
            result = fetch_rankings("KR")
    assert set(result["boards"]) == {"거래대금", "상승률", "하락률", "토스 사용자 거래대금"}
    assert result["ranked_at"] == "2026-08-12T16:59:03.980+09:00"
    assert len(result["boards"]["거래대금"]) == 2
    assert m.call_count == 4


def test_fetch_rankings_one_board_failure_does_not_kill_others(rankings_fixture):
    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("network down")
        return rankings_fixture

    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", side_effect=side_effect):
            result = fetch_rankings("KR")
    assert len(result["boards"]) == 3


def test_fetch_rankings_all_boards_failing_raises():
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", side_effect=RuntimeError("down")):
            with pytest.raises(ValueError, match="토스 랭킹"):
                fetch_rankings("KR")


# --- fetch_warnings: 심볼당 실패는 건너뛴다 ---------------------------------


def test_fetch_warnings_parses_result_and_labels():
    # _request 는 이미 {"result": ...} 래핑을 벗겨서 반환한다 — 여기선 경고 리스트 그대로.
    warnings = [
        {"warningType": "INVESTMENT_WARNING", "startDate": "2026-08-01", "endDate": "2026-08-15"},
    ]
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", return_value=warnings):
            result = fetch_warnings(["005930"])
    assert result["005930"] == [
        {"type": "INVESTMENT_WARNING", "label": "투자경고", "start": "2026-08-01", "end": "2026-08-15"}
    ]


def test_fetch_warnings_no_warnings_returns_empty_list():
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", return_value=[]):
            result = fetch_warnings(["005930"])
    assert result["005930"] == []


def test_fetch_warnings_per_symbol_failure_does_not_kill_others():
    def side_effect(c, path, params=None):
        if "005930" in path:
            raise httpx.HTTPStatusError("404", request=None, response=None)
        return []

    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", side_effect=side_effect):
            result = fetch_warnings(["005930", "000660"])
    assert result["005930"] == []
    assert result["000660"] == []


# --- fetch_investor_trading: 순매수 계산 ------------------------------------


def test_investor_net_computes_buy_minus_sell():
    record = {"individual": {"buyAmount": "19024467609341", "sellAmount": "22958462488738"}}
    assert _investor_net(record, "individual") == 19024467609341 - 22958462488738


def test_fetch_investor_trading_builds_kospi_kosdaq():
    record = {
        "date": "2026-08-12",
        "updatedAt": "2026-08-12T15:30:00+09:00",
        "individual": {"buyAmount": "100", "sellAmount": "80"},
        "foreigner": {"buyAmount": "50", "sellAmount": "60"},
        "institution": {"buyAmount": "30", "sellAmount": "10"},
    }
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", return_value={"records": [record]}):
            result = fetch_investor_trading()
    assert set(result) == {"KOSPI", "KOSDAQ"}
    assert result["KOSPI"]["individual_net"] == 20
    assert result["KOSPI"]["foreigner_net"] == -10
    assert result["KOSPI"]["institution_net"] == 20
    assert result["KOSPI"]["updated_at"] == "2026-08-12T15:30:00+09:00"


# --- fetch_usd_krw -----------------------------------------------------------


def test_fetch_usd_krw_returns_float_rate():
    with patch("quant.collect.sources.toss.client"):
        with patch("quant.collect.sources.toss._request", return_value={"rate": 1350.5}):
            result = fetch_usd_krw()
    assert result == {"rate": 1350.5}


# --- 라이브 (자격증명 없거나 403 이면 skip) ----------------------------------


def _skip_if_unreachable(fn, *args):
    try:
        return fn(*args)
    except (RuntimeError, ValueError) as e:
        pytest.skip(str(e))
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 403:
            pytest.skip("IP 화이트리스트 — 로컬에서는 403 이 정상")
        raise


@pytest.mark.live
def test_live_fetch_rankings_kr():
    if not get_key("TOSS_CLIENT_ID") or not get_key("TOSS_CLIENT_SECRET"):
        pytest.skip("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정")
    result = _skip_if_unreachable(fetch_rankings, "KR")
    assert result["boards"]


@pytest.mark.live
def test_live_fetch_usd_krw():
    if not get_key("TOSS_CLIENT_ID") or not get_key("TOSS_CLIENT_SECRET"):
        pytest.skip("TOSS_CLIENT_ID/TOSS_CLIENT_SECRET 미설정")
    result = _skip_if_unreachable(fetch_usd_krw)
    assert result["rate"] > 0
