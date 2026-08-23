"""백테스트 밀폐성 — **소켓을 막고 끝까지 돈다.**

## 왜 이게 필요한가

적합도 함수(Phase 8 하네스가 최적화할 대상)는 재현 가능해야 한다. 네트워크를
한 번이라도 타면 같은 입력에 다른 답이 나올 수 있고, 그러면 "이 변형이 더
낫다"는 비교 자체가 무의미해진다.

`import` 목록을 눈으로 확인하는 것으로는 부족하다 — 지연 임포트 하나, 캐시
미스 시 폴백 하나면 조용히 밖으로 나간다. **커널에게 물어보는 게 유일하게
믿을 만한 방법이다**: 소켓 생성을 막고 그래도 끝까지 도는지 본다.
"""
from __future__ import annotations

import socket

import pytest


class NetworkAccessDenied(RuntimeError):
    pass


@pytest.fixture
def no_network(monkeypatch):
    """소켓 생성 자체를 막는다. AF_UNIX 는 허용 — 로컬 IPC 는 네트워크가 아니다."""
    real_socket = socket.socket

    def blocked(family=socket.AF_INET, *a, **k):
        if family in (socket.AF_INET, socket.AF_INET6):
            raise NetworkAccessDenied(
                "백테스트가 네트워크를 탔다 — 밀폐성이 깨지면 같은 입력에 다른 "
                "답이 나올 수 있고, 그러면 하네스의 비교가 무의미해진다."
            )
        return real_socket(family, *a, **k)

    monkeypatch.setattr(socket, "socket", blocked)
    for name in ("create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, lambda *a, **k: (_ for _ in ()).throw(
            NetworkAccessDenied(f"백테스트가 socket.{name} 을 호출했다")))
    yield


def test_the_guard_itself_actually_blocks(no_network):
    """가드가 작동하는지 먼저 증명한다 — 안 그러면 아래 테스트가 공허하다."""
    with pytest.raises(NetworkAccessDenied):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(NetworkAccessDenied):
        socket.getaddrinfo("example.com", 443)


def test_stub_backtest_completes_without_network(no_network):
    """`run backtest --strategy donchian` 이 실제로 타는 경로."""
    from quant.backtest.engine import run_backtest

    result = run_backtest(strategy_id="donchian", days=60)
    assert result.equity_curve is not None and len(result.equity_curve) > 0
    assert "total_return_pct" in result.metrics


def test_fitness_evaluates_without_network(no_network):
    """적합도 계산까지 통째로 밀폐돼야 한다 — 하네스가 부르는 건 여기다."""
    from quant.backtest.engine import run_backtest
    from quant.backtest.fitness import evaluate

    result = run_backtest(strategy_id="donchian", days=60)
    f = evaluate(result, require_costs=result.trades is not None and not result.trades.empty)
    assert f.to_dict()["n_fills"] == f.n_fills


def test_same_input_gives_identical_metrics(no_network):
    """재현성 — 하네스가 '이 변형이 더 낫다'고 말하려면 이게 참이어야 한다."""
    from quant.backtest.engine import run_backtest

    a = run_backtest(strategy_id="donchian", days=60)
    b = run_backtest(strategy_id="donchian", days=60)
    assert a.metrics == b.metrics
    assert len(a.trades) == len(b.trades)
