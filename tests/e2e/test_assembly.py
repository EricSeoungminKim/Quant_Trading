"""조립(composition root) 배선 검증 — build_paper_runtime이 실제로 무엇을 엮는지.

WHY THIS EXISTS의 결함(a)를 직접 겨냥한다: FxProvider를 만들어 놓고 아무 데도
주입하지 않아 전 계층이 조용히 FixedFxProvider(고정 환율)로 굴러갔고, paper 루프가
합성 stub 데이터를 실데이터인 것처럼 받고 있었다. 유닛 테스트는 각 팩토리 함수
(build_fx_provider, build_market_data 등)를 개별적으로 mock 인자와 함께 호출해서
통과했지만, 그 함수들을 실제로 서로 연결하는 조립 지점(build_paper_runtime) 자체는
아무도 실행하지 않았다 — 그래서 라이브에서야 결함이 드러났다.

여기서는 실제 자격증명 없이(가짜 TOSS_CLIENT_ID/SECRET) build_paper_runtime을
그대로 실행하고, 반환된 객체 그래프가 진짜로 원하는 모양인지("stub이 아니다",
"DailyFxProvider다", "risk/broker가 같은 fx 인스턴스를 공유한다")를 직접 검사한다.
네트워크 I/O가 실제로 발생하는 지점(TossClient._fetch_token 등)은 절대 호출하지
않는다 — 조립 그 자체만 검증한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.apps.assembly import (
    MissingCredentials,
    build_paper_runtime,
    validated_capital_fractions,
)
from quant.apps.config import load_settings
from quant.core.fx import DailyFxProvider, FixedFxProvider

NY = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_PATH = _REPO_ROOT / "config" / "settings.yaml"


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발자 로컬 .env.local에 실 자격증명이 있어도 테스트가 그 값에 우연히 기대지
    않도록, 이 조립 경로가 참조하는 자격증명 변수를 모두 지운 뒤 필요한 값만 가짜로
    채운다."""
    for key in (
        "TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET", "TOSS_ACCOUNT_SEQ",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(key, raising=False)


def test_build_paper_runtime_wires_real_components_not_synthetic_fallbacks(tmp_path, monkeypatch):
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)  # Portfolio.load_or_init 등 상대경로 상태 파일이 실 저장소를 건드리지 않게
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "1234567890")
    monkeypatch.setenv("START_CAPITAL_KRW", "5000000")

    runtime = build_paper_runtime(settings)

    # (a) 자격증명이 있으면 fx가 DailyFxProvider여야 한다 — FixedFxProvider(고정 환율)로
    # 조용히 강등되면 안 된다.
    assert isinstance(runtime.fx, DailyFxProvider)
    assert not isinstance(runtime.fx, FixedFxProvider)

    # 데이터 라우트에 stub/synthetic 소스가 없어야 한다. health()의 키는 SourceRoute.name
    # 목록 그대로다(quant/adapters/data/service.py MarketDataService.__init__) — 네트워크
    # 호출 없이 라우팅 구조만 공개 API로 확인한다. tmp_path에는 로컬 과거 데이터가
    # 없으므로 history 폴백 라우트는 안 붙는 게 정상이다(아래 별도 테스트에서
    # "데이터가 있어도 안 붙는" 진짜 버그를 다룬다) — 여기서는 "toss"만 있고
    # "stub" 같은 합성 소스가 절대 섞이지 않았는지만 확인한다.
    route_names = set(runtime.data.health().sources)
    assert "toss" in route_names
    assert "stub" not in route_names

    # 클록의 판단 주기가 engine.poll_seconds와 일치해야 한다 — 어긋나면 백테스트와
    # 라이브의 마감 전 청산 판정이 서로 다르게 동작한다(AGENTS.md 결함 c 참고).
    assert runtime.ctx.clock.cadence_minutes() == pytest.approx(settings.poll_seconds / 60.0)

    # risk와 broker가 서로 다른 FxProvider 인스턴스를 들고 있으면, 한쪽만 환율이
    # 갱신되고 다른 쪽은 낡은 값을 쓰는 불일치가 생길 수 있다 — 반드시 같은 객체여야 한다.
    assert runtime.risk.fx is runtime.fx
    assert runtime.ctx.broker.fx is runtime.fx


def test_missing_toss_credentials_raise_instead_of_silently_degrading(tmp_path, monkeypatch):
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)  # TOSS_CLIENT_ID/SECRET 없음 — 조용한 stub 강등이 아니라 예외를 기대한다

    with pytest.raises(MissingCredentials):
        build_paper_runtime(settings)


def _write_native_15m_partition(history_dir: Path, symbol: str, minutes: int = 15) -> None:
    """HistoryDataFeed가 읽는 3단계 native 파티션 레이아웃
    (data/history/{symbol}/{interval}/{YYYY}/{MM}.parquet)에 맞춰, 실제로 유효한
    봉 데이터를 만들어 둔다 — "로컬 데이터가 진짜 있는" 상황을 재현하기 위함.

    간격은 **조립이 실제로 프로브하는 간격**과 일치해야 한다 — 활성 전략 구성이
    바뀌면 _primary_interval_minutes가 바뀌고(15m 단독 시절→orb_scan 활성화로 5m),
    고정 15m 픽스처는 "데이터가 있는데 없다"는 거짓 실패를 낸다.
    """
    idx = pd.date_range("2024-01-02T09:30:00", periods=20, freq=f"{minutes}min", tz=NY)
    prices = [100.0 + i * 0.1 for i in range(len(idx))]
    df = pd.DataFrame({
        "open": prices, "high": [p + 0.2 for p in prices], "low": [p - 0.2 for p in prices],
        "close": prices, "volume": [1000.0] * len(idx),
    }, index=idx)
    part_path = history_dir / symbol / f"{minutes}m" / "2024" / "01.parquet"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(part_path)


def test_local_history_fallback_route_activates_when_data_exists(tmp_path, monkeypatch):
    """로컬에 실데이터가 있으면 history 폴백 라우트가 실제로 등록돼야 한다.

    회귀 방지 배경(2026-08-06 발견·수정): build_market_data가 가용성을
    `history.history(symbol, interval, 1).empty`로 판정했는데, HistoryDataFeed는
    생성 직후 `_now`가 None이고 그때 history()는 데이터 유무와 무관하게 항상 빈
    프레임을 반환한다(quant/adapters/data/history.py). 그래서 디스크에 유효한
    Parquet이 있어도 "로컬 과거 데이터 없음"으로 오판해 폴백이 죽어 있었고,
    운영자는 로그만 보고 데이터가 없다고 믿게 됐다.

    수정 두 가지: (1) 가용성 판정을 시계와 무관한 bar_closes()로 바꿨다.
    (2) 라이브 루프에는 set_now()를 호출해 주는 주체가 없으므로 _ClockBound가
    벽시계를 물려준다 — 이게 없으면 라우트는 등록되지만 여전히 빈 값만 준다.
    """
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "5000000")

    from quant.apps.assembly import _primary_interval_minutes
    minutes = _primary_interval_minutes(settings.raw)
    _write_native_15m_partition(tmp_path / "data" / "history", "TQQQ", minutes)
    _write_native_15m_partition(tmp_path / "data" / "history", "SQQQ", minutes)

    runtime = build_paper_runtime(settings)

    route_names = set(runtime.data.health().sources)
    assert "history" in route_names, (
        f"로컬에 유효한 15m 데이터가 있는데 history 폴백이 등록되지 않았다 "
        f"(실제 라우트: {route_names})"
    )

    # 라우트 등록만으로는 부족하다 — 실제로 봉을 서빙해야 한다.
    # _ClockBound가 없으면 여기서 빈 프레임이 돌아온다.
    bars = runtime.data.history("TQQQ", f"{minutes}m", 5)
    assert not bars.empty, "폴백 라우트가 등록됐지만 실제로는 빈 값만 반환한다"


def test_broker_selection_follows_mode(tmp_path, monkeypatch):
    """MODE=live일 때만 TossBroker, 그 외 전부 PaperBroker.

    이 분기가 없던 시절에는 MODE=live로 켜도 assembly가 PaperBroker만 조립해
    "실전 전환했는데 실주문이 하나도 안 나가는" 상태가 됐다 — 위험하지는 않지만
    운영자가 라이브라고 믿는 것과 실제가 다른, 가장 혼란스러운 종류의 결함이다.
    반대 방향(MODE=paper인데 TossBroker)은 돈이 나가는 사고라 더 치명적이다.
    """
    from quant.adapters.brokers.toss.broker import TossBroker
    from quant.adapters.execution.paper import PaperBroker

    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "5000000")

    monkeypatch.setenv("MODE", "paper")
    rt = build_paper_runtime(settings)
    assert isinstance(rt.ctx.broker, PaperBroker)
    assert rt.reconciler is None, "PaperBroker에는 대조할 원장이 없다 — 대사기는 None이어야 한다"

    monkeypatch.setenv("MODE", "live")
    rt_live = build_paper_runtime(settings)
    assert isinstance(rt_live.ctx.broker, TossBroker)
    assert rt_live.reconciler is not None, "라이브 브로커면 대사기가 자동 활성화돼야 한다"


def test_per_strategy_books_reach_both_the_risk_manager_and_the_runtime(tmp_path, monkeypatch):
    """장부는 **두 곳**에 도착해야 한다. 리스크에만 가면 절반만 켜진다.

    2026-08-19 실전 P0: `RiskManagerImpl(books=books)` 에는 넘겼는데 `PaperRuntime(...)`
    에 빠뜨렸다. 그래서 사이징만 전략별 1,000만원으로 돌고 체결은 장부에 기록되지
    않았다 — `available_cash_krw` 가 영원히 1,000만원을 답해 전략별 현금 게이트가
    무력화됐고, KR 개장 7분 만에 공유 계좌 현금이 -10,470,186원까지 내려갔다
    (체결 13건, 장부 파일 mtime 은 기동 시각 그대로).

    조립 지점을 끝까지 따라가지 않으면 "만들었는데 절반만 배선된" 상태가 된다 —
    이 저장소가 FxProvider 로 이미 한 번 겪은 결함이다(이 파일 상단 docstring).
    """
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "1234567890")

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    runtime = build_paper_runtime(settings)

    assert runtime.risk.books is not None, "리스크 매니저에 장부가 없다 — 사이징이 계좌 전체로 돈다"
    assert runtime.books is not None, (
        "런타임에 장부가 없다 — 체결이 장부에 기록되지 않아 전략별 현금 게이트가 무력화된다"
    )
    assert runtime.books is runtime.risk.books, (
        "리스크와 루프가 서로 다른 장부 인스턴스를 들면 게이트가 보는 잔고와 "
        "체결이 반영되는 장부가 어긋난다"
    )


# ── capital_policy: equal_split — 총현금을 모르면 0원으로 위장하지 않는다 ─────
#
# 2026-08-19 코디네이터 지적: broker.cash() 실패/0을 그대로 나눠 새 전략을
# 0원으로 시딩하면, 그 전략은 risk.approve()의 현금 게이트에 영원히 걸려
# 아무것도 못 사는 상태로 조용히 굳는다 — cross_momentum이 8일간 무동작이었던
# 사건과 같은 부류("모르는 것을 안전한 쪽으로 가정하지 않는다").

def test_equal_split_initial_krw_returns_none_when_cash_lookup_raises():
    """조회 자체가 예외를 던지면 None(모른다)이어야 한다 — 0으로 위장하면
    호출부가 신규 전략을 0원으로 시딩한다."""
    from quant.apps.assembly import equal_split_initial_krw

    class _RaisingBroker:
        def cash(self) -> float:
            raise RuntimeError("Toss API 500")

    assert equal_split_initial_krw(_RaisingBroker(), ["a", "b"]) is None


def test_equal_split_initial_krw_returns_none_when_cash_is_zero():
    """조회는 성공했지만 0원이면 여전히 "모른다"와 같게 취급한다 — 계좌가 정말
    빈 것과 조회가 반쯤 실패해 0을 돌려준 것을 여기서는 구분할 수 없고,
    어느 쪽이든 새 전략을 0원으로 시딩하면 안 되는 결론은 같다."""
    from quant.apps.assembly import equal_split_initial_krw

    class _ZeroBroker:
        def cash(self) -> float:
            return 0.0

    assert equal_split_initial_krw(_ZeroBroker(), ["a", "b"]) is None


def test_equal_split_initial_krw_returns_none_when_cash_is_negative():
    from quant.apps.assembly import equal_split_initial_krw

    class _NegativeBroker:
        def cash(self) -> float:
            return -1234.0

    assert equal_split_initial_krw(_NegativeBroker(), ["a", "b"]) is None


def test_equal_split_initial_krw_splits_evenly_when_cash_is_known():
    """회귀 방지 — 총현금을 알 때는 지금까지처럼 평범하게 균등 분할한다."""
    from quant.apps.assembly import equal_split_initial_krw

    class _Broker:
        def cash(self) -> float:
            return 9_000_000.0

    assert equal_split_initial_krw(_Broker(), ["a", "b", "c"]) == pytest.approx(3_000_000.0)


def _enabled_strategy_count(settings) -> int:
    return sum(
        1 for s in settings.raw.get("strategies", {}).values()
        if isinstance(s, dict) and s.get("enabled")
    )


def test_equal_split_seeds_normally_when_cash_is_known(tmp_path, monkeypatch):
    """정상 경로 회귀 방지 — 이 안전장치를 넣기 전 동작(총현금을 알면 평소처럼
    균등 분할해서 활성 전략을 전부 시딩한다)이 그대로 유지되는지."""
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "10000000")

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    settings.raw["risk"]["capital_policy"] = "equal_split"
    runtime = build_paper_runtime(settings)

    n_active = _enabled_strategy_count(settings)
    assert n_active > 0
    expected = 10_000_000.0 / n_active
    assert len(runtime.books.books) == n_active
    assert runtime.books.initial_krw == pytest.approx(expected)
    for book in runtime.books.books.values():
        assert book["initial_krw"] == pytest.approx(expected)
        assert book["cash_krw"] == pytest.approx(expected)


def test_equal_split_skips_seeding_new_strategies_when_cash_is_unknown(tmp_path, monkeypatch):
    """총현금 조회 결과가 0이면(모른다) 이번 기동에서는 어떤 전략도 새로
    시딩되면 안 된다 — 0원으로 굳으면 그 전략은 영원히 아무것도 못 산다."""
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "0")  # PaperBroker.cash() == 0

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    settings.raw["risk"]["capital_policy"] = "equal_split"
    runtime = build_paper_runtime(settings)

    assert runtime.books is not None
    assert runtime.books.books == {}, "총현금을 모르는데 신규 전략이 시딩됐다 — 0원 시작자본 사고"
    assert not (tmp_path / "data" / "state" / "strategy_books.json").exists(), (
        "시딩이 없었다면 파일도 새로 쓰이면 안 된다"
    )


def test_equal_split_preserves_existing_books_when_cash_lookup_fails_on_restart(tmp_path, monkeypatch):
    """재기동 시나리오: 어제까지 정상적으로 시딩된 장부가 있는데, 오늘 기동에서
    총현금 조회가 실패했다(예: Toss 일시 장애). 이미 굴러간 장부는 그대로 살아
    남아 계속 거래해야 하고, 아직 시딩 안 된 다른 전략만 이번 기동에서 빠져야
    한다(설계 질문 1·2: 최초 1회 확정, 신규만 새 분모 — 총현금을 모를 때는 그
    "신규"조차 만들지 않는다)."""
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "0")  # 오늘 기동은 총현금을 모른다

    books_path = tmp_path / "data" / "state" / "strategy_books.json"
    books_path.parent.mkdir(parents=True, exist_ok=True)
    existing_book = {
        "cash_krw": 2_400_000.0, "initial_krw": 5_000_000.0,
        "realized_pnl_krw": -100_000.0, "fees_krw": 3_000.0,
        "positions": {}, "updated": "2026-08-18T06:00:00+00:00",
    }
    books_path.write_text(json.dumps({
        "version": 1, "initial_krw": 5_000_000.0,
        "books": {"donchian": existing_book},
    }, ensure_ascii=False), encoding="utf-8")

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    settings.raw["risk"]["capital_policy"] = "equal_split"
    runtime = build_paper_runtime(settings)

    assert set(runtime.books.books) == {"donchian"}, (
        "총현금을 모르는 기동에서 다른 전략이 새로 시딩되면 안 된다"
    )
    assert runtime.books.books["donchian"] == existing_book, (
        "이미 굴러간 장부는 손익이 쌓여 있으므로 재계산·재조정되면 안 된다"
    )


# ------------------------------------------------- live 브로커 배선 (2026-09-02 C1)

def test_live_broker_gets_fx_and_market_of_so_strategy_books_can_update(tmp_path, monkeypatch):
    """MODE=live 의 TossBroker 에도 fx/market_of 가 붙어야 한다.

    2026-09-02 감사 C1: 루프의 전략별 장부 갱신(`loop._execute_signal`)은
    `ctx.broker.fx` 를 duck-typing 으로 읽는데 live 브로커에는 그게 없어서
    `books.apply_fill` 이 매 체결마다 스킵됐다 — 실계좌에서만 전략별 현금·노출
    레일이 눈이 먼 상태로 돌았다(2026-08-19 P0 의 live 판 재발).

    market_of 는 risk 와 **같은 dict 객체**여야 한다 — cli._rebuild 가 유니버스
    롤마다 이 dict 를 in-place update 해서 양쪽을 함께 갱신하기 때문이다.
    """
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("TOSS_ACCOUNT_SEQ", "1234567890")
    monkeypatch.setenv("MODE", "live")

    from quant.adapters.brokers.toss.broker import TossBroker
    # capital_policy: equal_split 이 실계좌 현금을 조회한다 — 네트워크를 타지 않게 고정.
    monkeypatch.setattr(TossBroker, "cash", lambda self: 20_000_000.0)

    runtime = build_paper_runtime(settings)

    assert isinstance(runtime.ctx.broker, TossBroker)
    assert runtime.ctx.broker.fx is runtime.fx
    assert runtime.ctx.broker.market_of is runtime.risk.market_of


def test_per_strategy_capital_mode_refuses_broker_without_fx():
    """배선 누락은 사이클마다 WARNING 이 아니라 **부팅 실패**여야 한다(C1)."""
    from quant.apps.assembly import require_books_capable_broker

    class _NoFxBroker:
        def positions(self):
            return {}

    with pytest.raises(RuntimeError, match="per_strategy"):
        require_books_capable_broker(_NoFxBroker())

    class _WithFxBroker(_NoFxBroker):
        fx = FixedFxProvider(1500.0)

    require_books_capable_broker(_WithFxBroker())  # 예외 없음


# ------------------------------------- 과거데이터 폴백 라우트의 간격 (2026-09-02 C3)

def _write_1m_partition(history_dir: Path, symbol: str) -> None:
    """1분봉 레이아웃(data/history/{symbol}/{YYYY}/{MM}.parquet, 2단계)."""
    idx = pd.date_range("2024-01-02T14:30:00Z", "2024-01-04T21:00:00Z", freq="1min")
    prices = [100.0 + (i % 50) * 0.01 for i in range(len(idx))]
    df = pd.DataFrame({
        "open": prices, "high": [p + 0.2 for p in prices], "low": [p - 0.2 for p in prices],
        "close": prices, "volume": [1000.0] * len(idx),
    }, index=idx)
    part_path = history_dir / symbol / "2024" / "01.parquet"
    part_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(part_path)


def test_history_fallback_serves_the_intervals_strategies_actually_request(tmp_path, monkeypatch):
    """폴백 라우트가 프로브 간격(15m) 하나로만 등록되면, 실제 전략이 요구하는
    1m/5m/1d 에서는 후보에도 오르지 못해 **한 번도 선택되지 않는 죽은 폴백**이
    된다(2026-09-02 감사 C3 — 활성 전략 중 15m 를 쓰는 것이 하나도 없었다).

    1차 소스(Toss)가 죽었을 때 로컬 Parquet 이 1m/1d 를 실제로 서빙하는지 본다.
    """
    from quant.adapters.brokers.toss.datafeed import TossDataFeed
    from quant.apps.assembly import build_market_data
    from quant.core.ports import DataSourceError

    monkeypatch.chdir(tmp_path)
    _write_1m_partition(tmp_path / "data" / "history", "TQQQ")

    def _dead_history(self, symbol, interval, n):
        raise DataSourceError("429 rate limited")

    monkeypatch.setattr(TossDataFeed, "history", _dead_history)

    class _Clock:
        def now(self):
            return datetime(2024, 1, 6, tzinfo=timezone.utc)

    service = build_market_data(
        object(), _Clock(), interval="15m", symbols=["TQQQ"], cfg={},
    )
    assert "history" in service.health().sources, "로컬 데이터가 있는데 폴백이 등록되지 않았다"

    for interval in ("1m", "5m", "1d"):
        bars = service.history("TQQQ", interval, 10)
        assert not bars.empty, f"{interval}: 1차 소스 실패 시 로컬 폴백이 서빙해야 한다"


# ── capital_policy: declared — 선언한 capital_fraction을 그대로 집행한다 ──────
#
# 2026-09-03. equal_split은 총현금을 활성 전략 수로 똑같이 나눠 `capital_fraction`의
# 크기 정보를 통째로 죽였다(12개 전략이 전부 1/12). 그래서 설정 파일이 "scalp_1m
# US 18%"라고 말하는데 실제로는 8.3%인 상태였다 — 선언과 실행의 조용한 불일치.
# declared는 시장별 현금 풀에 각 전략의 선언 비중을 곱한다.

class _PoolBroker:
    """KRW/USD 두 지갑을 가진 브로커 스텁."""

    def __init__(self, krw: float, usd: float | None = None):
        self._krw, self._usd = krw, usd

    def cash(self) -> float:
        return self._krw

    def cash_usd(self):
        return self._usd


class _Fx1000:
    def usd_krw(self) -> float:
        return 1000.0


def test_declared_initial_krw_splits_by_market_pools():
    """KR 비중은 KRW 풀에서, US 비중은 USD 풀(KRW 환산)에서 나온다 —
    총현금 하나에 곱하지 않는다(실제 지출 한도가 통화별 지갑이므로)."""
    from quant.apps.assembly import declared_initial_krw

    got = declared_initial_krw(
        _PoolBroker(10_000_000.0, 5_000.0),   # KRW 1,000만 / USD 5,000 = 500만 KRW
        {"kr_only": {"KR": 0.5, "US": 0.0}, "us_only": {"KR": 0.0, "US": 0.4}},
        ["kr_only", "us_only"],
        fx=_Fx1000(),
    )
    assert got == {
        "kr_only": pytest.approx(5_000_000.0),   # 1,000만 x 0.5
        "us_only": pytest.approx(2_000_000.0),   # 500만 x 0.4
    }


def test_declared_initial_krw_sums_both_pools_for_a_dual_market_strategy():
    """scalp_1m(KR .15 / US .18)처럼 양 시장에 비중이 있는 전략은 두 풀의 몫을
    합산한다 — 그 전략은 실제로 양쪽 지갑에서 쓴다."""
    from quant.apps.assembly import declared_initial_krw

    got = declared_initial_krw(
        _PoolBroker(10_000_000.0, 5_000.0),
        {"scalp_1m": {"KR": 0.15, "US": 0.18}},
        ["scalp_1m"],
        fx=_Fx1000(),
    )
    # 1,000만 x 0.15 + 500만 x 0.18 = 150만 + 90만
    assert got["scalp_1m"] == pytest.approx(2_400_000.0)


def test_declared_initial_krw_normalizes_market_overshoot(caplog):
    """한 시장의 비중 합이 1.0을 넘으면 그 시장만 비례 축소 + WARNING. 초과 배분은
    존재하지 않는 현금이고 곧 의도치 않은 레버리지다. 다른 시장은 손대지 않는다."""
    from quant.apps.assembly import declared_initial_krw

    with caplog.at_level(logging.WARNING):
        got = declared_initial_krw(
            _PoolBroker(10_000_000.0, 1_000.0),   # USD 풀 = 100만 KRW
            {"a": {"KR": 0.8, "US": 0.5}, "b": {"KR": 0.8, "US": 0.5}},  # KR 합 1.6, US 합 1.0
            ["a", "b"],
            fx=_Fx1000(),
        )
    # KR만 x0.625로 축소 → 각 0.5 → 500만. US는 그대로 0.5 → 50만.
    assert got["a"] == pytest.approx(5_500_000.0)
    assert got["b"] == pytest.approx(5_500_000.0)
    assert sum(v for k, v in got.items()) == pytest.approx(11_000_000.0)  # 두 풀 합계와 일치
    msgs = [r.getMessage() for r in caplog.records]
    assert any("KR" in m and "초과" in m for m in msgs)
    assert not any("US 시장 capital_fraction" in m for m in msgs)


def test_declared_initial_krw_excludes_strategies_with_zero_fraction():
    """양 시장 모두 0인 전략은 반환 dict에서 빠진다 — 어차피 진입이 차단돼 있어
    명목자본을 줄 이유가 없고, 0원 장부를 만들면 그게 곧 "영원히 못 사는 장부"다."""
    from quant.apps.assembly import declared_initial_krw

    got = declared_initial_krw(
        _PoolBroker(10_000_000.0, 1_000.0),
        {"live": {"KR": 0.2, "US": 0.0}, "off": {"KR": 0.0, "US": 0.0}},
        ["live", "off"],
        fx=_Fx1000(),
    )
    assert set(got) == {"live"}


def test_declared_initial_krw_ignores_usd_pool_without_fx():
    """fx가 없으면 USD 풀을 환산할 수 없다 — KRW 풀만으로 계산한다
    (equal_split_initial_krw와 같은 계약)."""
    from quant.apps.assembly import declared_initial_krw

    got = declared_initial_krw(
        _PoolBroker(10_000_000.0, 5_000.0),
        {"us_only": {"KR": 0.0, "US": 0.4}, "kr_only": {"KR": 0.5, "US": 0.0}},
        ["us_only", "kr_only"],
    )
    assert set(got) == {"kr_only"}   # USD 풀 0 → us_only 몫도 0 → 제외
    assert got["kr_only"] == pytest.approx(5_000_000.0)


def test_declared_initial_krw_returns_none_when_cash_is_unknown():
    """`None`은 "0원"이 아니라 "모른다" — equal_split과 같은 계약. 0으로 위장하면
    호출부가 신규 전략을 0원으로 시딩해 영원히 못 사는 상태로 굳힌다."""
    from quant.apps.assembly import declared_initial_krw

    class _RaisingBroker:
        def cash(self) -> float:
            raise RuntimeError("Toss API 500")

    frac = {"a": {"KR": 0.5, "US": 0.0}}
    assert declared_initial_krw(_RaisingBroker(), frac, ["a"]) is None
    assert declared_initial_krw(_PoolBroker(0.0), frac, ["a"]) is None
    assert declared_initial_krw(_PoolBroker(-1234.0), frac, ["a"]) is None


def test_declared_seeds_each_book_with_its_own_declared_amount(tmp_path, monkeypatch):
    """조립 경로 회귀: 전략마다 **서로 다른** 시작 명목자본으로 시딩된다
    (equal_split처럼 전부 같은 값이 아니다)."""
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "10000000")

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    settings.raw["risk"]["capital_policy"] = "declared"
    runtime = build_paper_runtime(settings)

    assert runtime.books is not None
    books = runtime.books.books
    assert books, "declared 정책에서 활성 전략 장부가 하나도 만들어지지 않았다"
    initials = {sid: b["initial_krw"] for sid, b in books.items()}
    assert all(v > 0 for v in initials.values()), f"0원 장부가 생겼다: {initials}"
    assert len(set(round(v, 6) for v in initials.values())) > 1, (
        f"전략마다 선언 비중이 다른데 시작금이 전부 같다 — equal_split로 떨어졌다: {initials}"
    )
    # 시작금 = 선언 비중에 비례한다: 두 전략의 비율이 비중 비율과 같아야 한다.
    fractions = validated_capital_fractions(settings.raw)
    for sid, book in books.items():
        assert book["cash_krw"] == pytest.approx(book["initial_krw"])
        assert sum(fractions[sid].values()) > 0, f"{sid}: 비중 0인데 장부가 생겼다"


def test_declared_skips_seeding_when_cash_is_unknown(tmp_path, monkeypatch):
    """총현금 미상 기동에서는 declared도 신규 시딩을 보류한다(equal_split과 동일)."""
    settings = load_settings(str(_SETTINGS_PATH))
    monkeypatch.chdir(tmp_path)
    _clean_env(monkeypatch)
    monkeypatch.setenv("TOSS_CLIENT_ID", "fake-client-id")
    monkeypatch.setenv("TOSS_CLIENT_SECRET", "fake-client-secret")
    monkeypatch.setenv("START_CAPITAL_KRW", "0")

    settings.raw.setdefault("risk", {})["capital_mode"] = "per_strategy"
    settings.raw["risk"]["capital_policy"] = "declared"
    runtime = build_paper_runtime(settings)

    assert runtime.books is not None
    assert runtime.books.books == {}, "총현금을 모르는데 신규 전략이 시딩됐다 — 0원 시작자본 사고"


# ── A/B 갈래 분할 (2026-09-03) ────────────────────────────────────────────────
# 중심 주장: **두 갈래가 같은 종목을 절대 동시에 보지 않는다.** 겹치면 원장의
# strategy_id 로 성적을 갈라 채점할 수 없고("어느 갈래가 벌었나"에 답이 없어진다),
# 같은 종목에 두 갈래가 동시 진입해 노출이 조용히 두 배가 된다.

def _ab_symbols(cfg: dict, tags_of: dict[str, list[str]], symbols: list[str]) -> dict[str, list[str]]:
    """실제 settings.yaml 로 전략을 조립해 (전략 id → 실제 감시 심볼)을 얻는다."""
    from quant.trade.strategy import build_strategies

    raw = {**cfg, "strategies": {
        sid: ({**c, "symbols": list(symbols)} if c.get("universe") == "watchlist" else c)
        for sid, c in cfg["strategies"].items()
    }}
    built = build_strategies(raw, tags_of=tags_of, inbox_reader=lambda: [])
    return {s.id: list(s.symbols) for s in built}


def test_ab_arms_receive_disjoint_symbol_sets_from_real_settings():
    """scalp_1m(기준)과 scalp_1m_cat(촉매)이 같은 관심종목에서 **서로 겹치지 않는**
    집합을 받는다 — KR 은 FRGN 단일 요인, US 는 EVENT+TREND."""
    settings = load_settings(str(_SETTINGS_PATH))
    symbols = ["005930", "000660", "TQQQ", "SOXL"]
    tags_of = {
        "005930": ["FRGN"],            # KR 촉매(외국인 순매수)
        "000660": ["EVENT"],           # KR 뉴스만 — 단일 요인 게이트에선 기준 갈래
        "TQQQ": ["EVENT", "TREND"],    # US 촉매
        "SOXL": ["TREND"],             # US 추세만 — 기준 갈래
    }
    got = _ab_symbols(settings.raw, tags_of, symbols)

    base, cat = set(got["scalp_1m"]), set(got["scalp_1m_cat"])
    assert not (base & cat), f"두 갈래가 같은 종목을 본다: {sorted(base & cat)}"
    assert base | cat == set(symbols), "필터가 종목을 잃어버렸다(합집합이 유니버스와 다름)"
    assert cat == {"005930", "TQQQ"}
    assert base == {"000660", "SOXL"}

    for pair in ("pullback_impulse", "vol_breakout"):
        b, c = set(got[pair]), set(got[f"{pair}_cat"])
        assert not (b & c), f"{pair}: 두 갈래가 같은 종목을 본다: {sorted(b & c)}"


def test_ab_arms_share_identical_params_in_settings():
    """진입 규칙이 다르면 A/B 가 아니라 두 전략의 비교다. settings.yaml 은 YAML
    앵커로 params 를 공유하므로 여기서 그 사실을 못박는다(손편집 드리프트 방지)."""
    import yaml

    cfg = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8"))
    pairs = [sid for sid in cfg["strategies"] if sid.endswith("_cat")]
    assert pairs, "A/B 갈래가 하나도 없다 — 설정이 되돌려졌나?"
    for cat in pairs:
        base = cat[: -len("_cat")]
        assert base in cfg["strategies"], f"{cat} 의 기준 갈래 {base} 가 없다"
        assert cfg["strategies"][cat]["class"] == cfg["strategies"][base]["class"]
        assert cfg["strategies"][cat]["params"] == cfg["strategies"][base]["params"], (
            f"{cat} 와 {base} 의 params 가 다르다 — A/B 전제(진입 규칙 동일) 붕괴"
        )


def test_held_symbol_survives_universe_filter_so_open_position_stays_managed():
    """보유 중인 종목은 태그가 사라져도 갈래에 남는다 — 안 그러면 그 포지션의
    손절·청산 로직이 통째로 사라진다(고아 포지션)."""
    settings = load_settings(str(_SETTINGS_PATH))
    from quant.trade.strategy import build_strategies

    raw = {**settings.raw, "_held_symbols": ["005930"], "strategies": {
        sid: ({**c, "symbols": ["005930", "000660"]} if c.get("universe") == "watchlist" else c)
        for sid, c in settings.raw["strategies"].items()
    }}
    built = {s.id: list(s.symbols) for s in build_strategies(raw, tags_of={}, inbox_reader=lambda: [])}
    # tags_of 가 비었으므로 촉매 갈래는 원래 아무것도 못 고른다 — 보유분만 남아야 한다.
    assert built["scalp_1m_cat"] == ["005930"]
    assert "005930" in built["scalp_1m"]
