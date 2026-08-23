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
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from quant.apps.assembly import MissingCredentials, build_paper_runtime
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
