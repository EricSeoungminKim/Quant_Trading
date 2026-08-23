"""웹소켓 인증 실패 시 토큰 캐시를 버리는가.

2026-08-11~20 실장애(9일, 하루 243회): 웹소켓 LOGIN 이
`[805004] Token이 유효하지 않습니다` 로 거부되는데 엔진이 영원히 같은 토큰으로
두드렸다. 원인은 키가 아니었다 — 같은 키로 REST 는 하루 683건 성공 중이었고,
프로브를 돌리니 웹소켓도 정상 접속·구독·틱 수신이 됐다.

진짜 원인: `access_token()` 이 **로컬 시계 기준** 만료 전이면 디스크 캐시를 그대로
돌려준다. 키움은 새 토큰을 발급하면 이전 토큰을 무효화하는 것으로 보이는데
(Toss client_credentials 와 같은 부류), 서버가 이미 폐기한 토큰을 우리는 "아직 안
만료됐다"고 믿고 계속 재사용했다. 캐시를 버릴 길이 아예 없었다.

웹소켓 코드는 인증 실패를 네트워크 실패와 구분까지 해 놓고 주석에 "토큰 거부는
같은 주기로 두드려도 낫지 않는다"라고 적어 뒀는데, 정작 토큰을 새로 받지 않았다.
"""
from __future__ import annotations

import json
import time

from quant.adapters.brokers.kiwoom.client import KiwoomClient
from quant.adapters.brokers.kiwoom.websocket import KiwoomRealtimeFeed


def _client(tmp_path) -> KiwoomClient:
    return KiwoomClient(app_key="k", secret_key="s", cache_dir=str(tmp_path))


def test_invalidate_token_clears_memory_and_disk(tmp_path):
    c = _client(tmp_path)
    c._access_token = "dead-token"
    c._expires_at = time.time() + 3600
    c._save_token_cache()
    assert c._token_path.exists()

    c.invalidate_token()

    assert c._access_token is None
    assert c._expires_at == 0.0
    assert not c._token_path.exists(), "디스크 캐시가 남으면 다음 프로세스가 죽은 토큰을 다시 쓴다"


def test_cached_token_is_reused_until_invalidated(tmp_path):
    """이게 장애의 핵심 — 무효화 없이는 만료 전까지 같은 토큰이 계속 나온다."""
    c = _client(tmp_path)
    c._access_token = "dead-token"
    c._expires_at = time.time() + 3600
    c._save_token_cache()

    assert c.access_token() == "dead-token"
    assert c.access_token() == "dead-token"  # 몇 번을 불러도 같다 — 재접속해도 안 낫는다

    c.invalidate_token()
    fetched: list[str] = []
    c._fetch_token = lambda: (fetched.append("x"), "fresh-token")[1]  # type: ignore[method-assign]
    assert c.access_token() == "fresh-token"
    assert fetched, "무효화 후에는 반드시 새로 발급받아야 한다"


def test_invalidate_is_safe_when_cache_file_missing(tmp_path):
    """캐시가 없어도 조용히 통과해야 한다 — 무효화 실패가 재접속을 막으면 안 된다."""
    c = _client(tmp_path)
    c.invalidate_token()
    c.invalidate_token()


def test_feed_accepts_and_stores_invalidator():
    """피드가 무효화 콜백을 받아 들고 있어야 재접속 루프에서 부를 수 있다."""
    calls: list[int] = []
    feed = KiwoomRealtimeFeed(
        access_token=lambda: "t",
        invalidate_token=lambda: calls.append(1),
        symbols=["005930"],
    )
    assert feed._invalidate_token is not None
    feed._invalidate_token()
    assert calls == [1]


def test_feed_without_invalidator_still_works():
    """기존 호출부(테스트·프로브)가 안 넘겨도 깨지면 안 된다."""
    feed = KiwoomRealtimeFeed(access_token="static", symbols=["005930"])
    assert feed._invalidate_token is None


def test_assembly_wires_invalidator_into_feed():
    """만든 것과 배선된 것은 다르다 — 조립 지점이 실제로 넘기는지 소스로 고정한다."""
    from pathlib import Path

    from quant.adapters.env import REPO_ROOT

    src = (REPO_ROOT / "quant" / "apps" / "assembly.py").read_text(encoding="utf-8")
    assert "invalidate_token=client.invalidate_token" in src, (
        "assembly 가 무효화 콜백을 피드에 넘기지 않는다 — 캐시가 영원히 안 지워진다"
    )
    assert Path(REPO_ROOT).exists()


def test_cached_token_json_shape_unchanged(tmp_path):
    """캐시 스키마를 바꾸지 않았다 — 기존 파일과 호환돼야 한다."""
    c = _client(tmp_path)
    c._access_token = "t"
    c._expires_at = 123.0
    c._save_token_cache()
    d = json.loads(c._token_path.read_text())
    assert set(d) == {"access_token", "expires_at", "base_url"}
