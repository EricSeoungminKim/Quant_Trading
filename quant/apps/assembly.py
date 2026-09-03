"""Composition root — 어댑터를 코어에 배선하는 유일한 장소.

조립이 곧 안전 경계다. 여기서 실수하면 계층 구조가 아무리 깨끗해도 소용없다.
실제로 이 프로젝트에서 그런 일이 있었다: FxProvider를 만들어 놓고 아무 데서도
연결하지 않아 전 계층이 조용히 고정환율 기본값으로 돌고 있었다. 그래서 조립은
run.py에 흩뿌리지 않고 이 모듈에 모아 테스트 가능하게 만든다.

원칙:
- 라이브/paper 경로에 stub 데이터가 들어가지 않는다 (CLAUDE.md 금지사항).
- 자격증명이 없으면 조용히 대체하지 않고 큰 소리로 실패한다.
- 무엇이 활성인지(환율 소스, 데이터 라우트) 기동 시 로그로 남긴다 — 운영자가
  "지금 뭘로 돌고 있나"를 추측하지 않아도 되게.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from quant.trade.approval import ApprovalGate
from quant.apps.config import Settings
from quant.adapters.smart_flow_log import SmartFlowLogger
from quant.adapters.tick_log import TickLogger
from quant.core.models import Side, market_of
from quant.trade.control import TradingControl
from quant.trade.reconcile import OpenOrderBook, Reconciler
from quant.core.clock import WallClock
from quant.core.fx import DailyFxProvider, FixedFxProvider, FxProvider
from quant.adapters.data.service import Capability, MarketDataService, SourceRoute
from quant.core.session import TossSessionCalendar
from quant.trade.universe import (
    DEFAULT_WATCHLIST_PATH,
    CompositeUniverse,
    FileWatchlistUniverse,
    StaticUniverse,
    TossRankingUniverse,
)
from quant.core.ports import Context, EventSink, Notifier, Strategy
from quant.adapters.execution.paper import PaperBroker
from quant.control.exposure import DEFAULT_ALERT_PCT, build_report as build_exposure_report
from quant.control.ledger import TradeLedgerSink
from quant.adapters.persistence.sink import ConsoleSink, JsonlSink, MultiSink
from quant.core.portfolio.portfolio import Portfolio
from quant.adapters.regime_indicators import (
    CompositeIndicatorClient,
    FileMacroIndicatorClient,
    TossIndicatorClient,
    UpbitBitcoinAdapter,
)
from quant.trade.regime import RegimeProvider
from quant.trade.risk.books import StrategyBooks
from quant.trade.risk.manager import RiskManagerImpl
from quant.trade.strategy import build_strategies

logger = logging.getLogger(__name__)


# 표시명 영속 캐시 — 텔레그램·리포트 가독성 전용(거래 판단에 쓰이지 않는다).
# 2026-08-28: 소유자가 "리포트에 한국 주식이 번호만 보인다"고 지적했다. 원인은
# 이름 조회 대상이 현재 유니버스뿐이라 **유니버스에서 빠진 보유 종목**(예:
# 042700·000500 — 외국인 적립 전략이 계속 들고 있는데 워치리스트에서는 빠졌다)이
# 조회조차 안 된 것이었다. 한 번 알게 된 이름을 잃지 않으면 이 부류가 사라진다.
_SYMBOL_NAME_CACHE_PATH = Path("data/state/symbol_names.json")

# KR ETF 여부 영속 캐시(2026-08-30) — daily_wrap "체결 비용" 절이 KR 개별주
# (매도세 20bp 붙음, 왕복 가정 30bp)와 KR ETF(가정 4bp)를 구분해야 하는데,
# 그 리포트는 네트워크를 쓰지 않는다("읽기만 한다" 계약, cmd_daily_wrap
# docstring). 분류 자체는 여기(부팅 시 securityType 조회)에서만 가능하므로
# name_of와 같은 패턴으로 저장해 둔다 — 한 번 알게 된 분류는 잃지 않는다.
_KR_ETF_CACHE_PATH = Path("data/state/kr_etf.json")


def _load_symbol_names(path: Path | None = None) -> dict[str, str]:
    """캐시를 읽는다. 없거나 깨졌으면 빈 dict — 이름표가 없다고 기동을 막지 않는다."""
    p = path or _SYMBOL_NAME_CACHE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k and v}


def _save_symbol_names(names: dict[str, str], path: Path | None = None) -> None:
    """원자적 tmp-replace. 쓰기 실패는 경고만 — 이름표 저장 실패가 거래를 막으면 안 된다."""
    p = path or _SYMBOL_NAME_CACHE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(names, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("종목명 캐시 저장 실패(거래는 계속): %s", e)


def _load_kr_etf(path: Path | None = None) -> set[str]:
    """캐시를 읽는다. 없거나 깨졌으면 빈 set — 분류가 없다고 기동을 막지 않는다."""
    p = path or _KR_ETF_CACHE_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, list):
        return set()
    return {str(s) for s in raw if s}


def _save_kr_etf(symbols: set[str], path: Path | None = None) -> None:
    """원자적 tmp-replace. 쓰기 실패는 경고만 — 캐시 저장 실패가 거래를 막으면 안 된다."""
    p = path or _KR_ETF_CACHE_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(symbols), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("KR ETF 분류 캐시 저장 실패(거래는 계속): %s", e)


# llm_trader 인박스 경로(2026-08-30) — 별도 프로세스(판단 스크립트, server/
# 소관)가 LLM 판단을 여기 append한다. 엔진은 읽기만 한다(quant/trade/strategy/
# llm_trader.py 모듈 docstring "아키텍처" 절 — data/watchlist.yaml과 같은 패턴).
LLM_TRADER_INBOX_PATH = Path("data/state/llm_trader_inbox.jsonl")


def _read_llm_trader_inbox(path: Path = LLM_TRADER_INBOX_PATH) -> list[dict]:
    """`LlmTraderStrategy`에 주입되는 인박스 리더 — 실제 파일 I/O는 여기(composition
    root)에만 있다. `FileWatchlistUniverse`와 달리 **세션당 캐시하지 않고 사이클마다
    다시 읽는다** — 새 주문이 아무 때나 append될 수 있어 하루 1회 스냅샷이 안
    맞는다(전략 쪽 ts 필터가 "오늘" 여부를 걸러 주므로 매 사이클 재읽기 비용은
    로컬 파일 하나 read + JSON 파싱뿐이다).

    형식은 JSON Lines(한 줄에 결정 하나 — `llm_trader.py` 모듈 docstring의 스키마).
    깨진 줄은 건너뛰고 경고만 남긴다(watchlist 파싱 관례와 동일 — 한 줄이
    이상하다고 나머지 주문을 통째로 버리지 않는다). 파일이 없으면 빈 목록(아직
    아무 판단도 들어오지 않은 정상 상태 — 경고하지 않는다)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        logger.warning("llm_trader 인박스 조회 실패 (%s): %s: %s", path, type(e).__name__, e)
        return []
    out: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            logger.warning("llm_trader 인박스 %s:%d 파싱 실패: %s", path, lineno, e)
            continue
        if isinstance(row, dict):
            out.append(row)
        else:
            logger.warning("llm_trader 인박스 %s:%d 행이 객체가 아님: %r", path, lineno, row)
    return out


class MissingCredentials(RuntimeError):
    """실데이터가 필요한 경로인데 자격증명이 없다. 조용히 stub으로 내려가지 않는다."""


class _ClockBound:
    """리플레이용 피드(HistoryDataFeed)를 실시간 루프에 붙이기 위한 얇은 어댑터.

    HistoryDataFeed는 백테스트를 위해 set_now()로 시각을 주입받도록 설계됐고,
    _now가 없으면 항상 빈 프레임을 돌려준다. 백테스트에서는 run_backtest가 매
    사이클 set_now를 호출하지만 라이브 루프에는 그런 호출자가 없다 — 그대로
    꽂으면 라우트는 등록되지만 영원히 빈 값만 준다. 여기서 벽시계를 물려
    그 간극을 메운다. 조립 지점의 배선 문제이므로 서비스나 피드가 아니라
    composition root에 둔다.
    """

    def __init__(self, feed, clock) -> None:
        self._feed = feed
        self._clock = clock

    def quote(self, symbol: str):
        self._feed.set_now(self._clock.now())
        return self._feed.quote(symbol)

    def history(self, symbol: str, interval: str, n: int):
        self._feed.set_now(self._clock.now())
        return self._feed.history(symbol, interval, n)


@dataclass
class PaperRuntime:
    strategies: list[Strategy]
    ctx: Context
    risk: RiskManagerImpl
    sinks: EventSink
    notifier: Notifier | None
    data: MarketDataService
    fx: FxProvider
    control: TradingControl
    active_markets: frozenset[str]
    approval: ApprovalGate | None
    approval_notifier: object | None
    approval_cfg: dict
    reconciler: Reconciler | None
    regime: RegimeProvider | None
    universe: CompositeUniverse | None
    name_of: dict[str, str]  # 심볼 → 표시명(텔레그램 가독성용). 미확인 심볼은 없음
    # 심볼 → 레버리지 배수(절대값). 부팅 시 1회 조회한 스냅샷 — kr_etf와 같은 한계
    # (세션 중 유니버스 롤로 새로 들어온 심볼은 재시작 전까지 미상으로 취급된다).
    # run.py의 _rebuild()가 세션 롤마다 이 스냅샷을 그대로 재사용한다(재조회하지
    # 않되, 조용히 사라지지도 않게).
    leverage_of: dict[str, float]
    # 전략별 독립 명목계정(2026-08-19, capital_mode: per_strategy 전용) — shared
    # 모드(기본)면 None이고 loop.py의 books 갱신 코드는 한 줄도 실행되지 않는다.
    books: "StrategyBooks | None" = None
    # 틱 로거(2026-08-28) — 항상 주입되고, engine.tick_log.enabled: false는 이
    # 인스턴스 내부의 enabled 플래그로 표현된다(TickLogger.__init__ 참고). loop.py는
    # quant.core.ports.TickLogger Protocol로만 받고 이 어댑터를 직접 임포트하지 않는다.
    tick_logger: "TickLogger | None" = None
    # 전략 간 합산 노출 감시 클로저(2026-08-30) — loop.py는 quant.control을
    # 직접 임포트할 수 없어(아키텍처 규칙) 여기서 quant.control.exposure를
    # 감싸 넘긴다. 시그니처: (lots, prices, capital_krw) -> dict(ExposureReport.to_dict()).
    exposure_check: "Callable[[dict, dict, float | None], dict] | None" = None


def require_books_capable_broker(broker: object) -> None:
    """`risk.capital_mode: per_strategy` 기동의 부팅 게이트 (2026-09-02, 감사 C1).

    루프의 전략별 장부 갱신은 `ctx.broker.fx`를 duck-typing으로 읽는다. 브로커에
    그게 없으면 `books.apply_fill`이 매 체결마다 조용히 스킵되고, 그때부터
    전략별 현금·동시보유·총노출 레일이 전부 눈이 먼 채로 거래가 계속된다 —
    사이클마다 WARNING 한 줄이 남을 뿐이라 실제로 3주 가까이 묻혔다.

    배선 누락은 사이클마다 경고할 게 아니라 **부팅에서 죽어야** 한다."""
    if getattr(broker, "fx", None) is None:
        raise RuntimeError(
            "조립 배선 오류 — risk.capital_mode: per_strategy 인데 브로커"
            f"({type(broker).__name__})에 fx 가 없다. 이대로 기동하면 전략별 장부가"
            " 갱신되지 않아 현금·노출 레일이 전부 무력화된다."
            " assembly.build_paper_runtime 의 브로커 조립 분기를 확인할 것."
        )


def build_toss_client(mode: str | None = None):
    """환경변수에서 TossClient를 만든다. 자격증명이 없으면 실패한다."""
    from quant.adapters.brokers.toss.client import TossClient

    client_id = os.environ.get("TOSS_CLIENT_ID", "")
    client_secret = os.environ.get("TOSS_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        raise MissingCredentials(
            "TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 가 없다. .env.local을 확인할 것. "
            "실시세 없이 paper 루프를 돌리는 것은 의미가 없으므로 stub으로 대체하지 않는다."
        )
    return TossClient(
        client_id=client_id,
        client_secret=client_secret,
        account_seq=os.environ.get("TOSS_ACCOUNT_SEQ", ""),
        mode=mode or os.environ.get("MODE", "paper"),
    )


def build_fx_provider(client, clock, *, fallback: float = 1500.0) -> FxProvider:
    """일일 갱신 환율. client가 None이면 고정 환율로 명시적 강등(로그 남김)."""
    if client is None:
        logger.warning("환율 소스 없음 — 고정 %.0f원으로 동작한다 (실환율 아님)", fallback)
        return FixedFxProvider(fallback)
    provider = DailyFxProvider(fetch=client.usd_krw, clock=clock, fallback=fallback)
    logger.info("환율: DailyFxProvider (거래일 롤 시 Toss에서 갱신, 실패 시 %.0f원)", fallback)
    return provider


def build_market_data(client, clock, *, interval: str, symbols: list[str],
                      quote_cache_seconds: float = 0.0, cfg: dict | None = None) -> MarketDataService:
    """실시세는 (켜져 있으면) Kiwoom 웹소켓 우선 → Toss, 과거 봉은 로컬 Parquet.
    순서가 곧 우선순위.

    symbols는 반드시 넘겨야 한다 — HistoryDataFeed는 생성 시 받은 심볼만 로드하므로
    빈 리스트를 주면 디스크에 데이터가 있어도 영원히 빈 프레임만 돌려주는,
    "있는 척하는 죽은 폴백"이 된다.

    cfg["engine"]["cold_fetch_budget_per_cycle"](기본 8)는 사이클당 봉 캐시
    미스로 실제 소스를 때리는 횟수 상한이다 — 유니버스 롤 직후 신규 심볼 수십
    개가 한 사이클에 몰려 사이클이 수십 초로 늘어나는 것을 막는다
    (MarketDataService.history() 참고).
    """
    from quant.adapters.brokers.toss.datafeed import TossDataFeed
    from quant.adapters.data.history import HistoryDataFeed

    routes: list[SourceRoute] = []
    kiwoom_route = build_kiwoom_realtime_route(cfg or {}, symbols, clock)
    if kiwoom_route is not None:
        routes.append(kiwoom_route)
    kiwoom_us_route = build_kiwoom_us_route(cfg or {}, symbols, clock)
    if kiwoom_us_route is not None:
        routes.append(kiwoom_us_route)
    routes.append(
        SourceRoute(
            name="toss",
            # symbols를 미리 준다 — quote()가 심볼당 개별 호출이 아니라 이 인스턴스가
            # 아는 심볼 전체를 한 번에 묶어 /prices 배치로 조회한다(TossDataFeed 참고).
            source=TossDataFeed(client, symbols=symbols),
            capabilities=frozenset({Capability.QUOTE, Capability.BARS}),
        ),
    )
    try:
        history = HistoryDataFeed(symbols)
        # 가용성 판정은 시계와 무관해야 한다. history()는 set_now() 전에는 항상
        # 빈 프레임을 주므로 프로브로 쓰면 데이터가 있어도 "없음"이 된다.
        #
        # 심볼별 격리(2026-08-24): 069500 의 tz 혼재 파티션 하나가 여기서
        # TypeError 를 던져 **27개 심볼 전체의** 폴백 라우트가 8-19부터 꺼져
        # 있었다. 로더가 tz 를 통일해 원인 자체는 고쳤지만, 다음 번 "이상한
        # 파티션 하나"가 또 전체를 끄지 못하게 프로브를 심볼 단위로 가둔다.
        loaded = []
        for s in symbols:
            try:
                if len(history.bar_closes(s, interval)) > 0:
                    loaded.append(s)
            except Exception as e:  # noqa: BLE001 — 한 심볼의 불량이 전체 폴백을 죽이면 안 된다
                logger.warning("과거 데이터 프로브 실패 — %s 제외: %s: %s",
                               s, type(e).__name__, e)
        if loaded:
            routes.append(
                SourceRoute(
                    name="history",
                    source=_ClockBound(history, clock),
                    capabilities=frozenset({Capability.BARS}),
                    symbols=frozenset(loaded),
                    # intervals=None = 모든 간격 서빙 가능(service._candidates).
                    # 2026-09-02 (C3): 여기에 프로브 간격 하나만(=15m) 박아 두면,
                    # 실제 전략들이 요구하는 1m/5m/1d 에서는 이 라우트가 후보에도
                    # 오르지 못해 **한 번도 선택되지 않는 죽은 폴백**이 된다.
                    # HistoryDataFeed 는 1분봉을 즉석 리샘플하므로 간격을 가릴
                    # 이유가 없고, 서빙 못 하는 간격은 빈 프레임으로 정직하게
                    # 답한다(업샘플로 지어내지 않는다).
                    intervals=None,
                )
            )
            logger.info("과거 데이터 폴백 사용 가능: %s", ", ".join(sorted(loaded)))
        else:
            # 라우트를 등록하지 않는다. 데이터 없는 소스를 우선순위 목록에 넣으면
            # "폴백이 있다"고 착각하게 된다.
            logger.warning(
                "로컬 과거 데이터 없음(%s) — 폴백 라우트 없이 Toss 단독으로 동작한다. "
                "`quant.apps.cli fetch`로 백필할 것", interval,
            )
    except Exception as e:
        logger.warning("과거 데이터 폴백 라우트 비활성 (%s: %s)", type(e).__name__, e)

    logger.info("데이터 라우트(우선순위 순): %s", " > ".join(r.name for r in routes))
    return MarketDataService(
        routes=routes, clock=clock, quote_cache_seconds=quote_cache_seconds,
        bar_cache_enabled=bool((cfg or {}).get("engine", {}).get("bar_cache_enabled", True)),
        cold_fetch_budget_per_cycle=int(
            (cfg or {}).get("engine", {}).get("cold_fetch_budget_per_cycle", 8)
        ),
    )


def _wait_for_connection(feed, timeout: float, poll_interval: float = 0.2, debounce: float = 1.0) -> bool:
    """`feed.health().connected`가 안정적으로 True가 될 때까지 최대 timeout초 폴링한다.

    구독(REG) 실패는 connect() 성공 직후 곧바로 connected=False로 되돌아가는 짧은
    flicker를 만든다(KiwoomRealtimeFeed.run()의 connect→subscribe→예외→재접속 사이클).
    한 번 True를 봤다고 바로 믿으면 이 flicker를 "연결됨"으로 오판할 수 있다 — 그래서
    True를 본 뒤 `debounce`초 뒤에도 여전히 True인지 한 번 더 확인한다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if feed.health().connected:
            time.sleep(min(debounce, max(deadline - time.monotonic(), 0)))
            return feed.health().connected
        time.sleep(poll_interval)
    return feed.health().connected


def build_smart_flow(cfg: dict) -> tuple[SmartFlowLogger | None, list[str]]:
    """세력 신호 수집기 + 웹소켓 구독 타입 목록을 만든다.

    반환은 `(싱크, 구독 타입)`. 플래그(`kiwoom.realtime.smart_flow_enabled`)가
    꺼져 있으면 `(None, ["0B"])` — **구독 타입 자체가 늘지 않는다.** 켜고 끄는 걸
    싱크의 enabled 플래그로만 표현하면 REG 프레임에는 여전히 0w/0F 가 실려 나가
    "안 켰는데 등록은 돼 있는" 상태가 된다. 실서버 검증 전이니 기본값은 **false**.

    왜 타입을 늘리는 방식인가(2026-08-28 감사): **키움 WS 세션은 계정당 1개**다.
    두 번째 연결을 열면 첫 연결이 끊긴다 — 그래서 별도 피드를 만들지 않고 기존
    연결의 REG 프레임에 타입만 얹는다(`KiwoomRealtimeFeed(types=...)`).

    이건 수집이다. 알림도, 주문도, 전략도 여기서 나오지 않는다.
    """
    from quant.adapters.brokers.kiwoom.websocket import (
        REALTIME_TICK_TYPE,
        SMART_FLOW_TYPES,
    )

    rt_cfg = (cfg.get("kiwoom", {}) or {}).get("realtime", {}) or {}
    if not rt_cfg.get("smart_flow_enabled", False):
        return None, [REALTIME_TICK_TYPE]
    sink = SmartFlowLogger(
        flush_seconds=float(rt_cfg.get("smart_flow_flush_seconds", 30.0)),
    )
    return sink, [REALTIME_TICK_TYPE, *SMART_FLOW_TYPES]


def build_kiwoom_realtime_route(cfg: dict, symbols: list[str], clock) -> SourceRoute | None:
    """켜져 있고 자격증명이 있으면 Kiwoom 실시간 웹소켓을 시세 최우선 라우트로 준비한다.

    기본 비활성(config/settings.yaml `kiwoom.realtime.enabled: false`) — 실키가 아직
    등록 전이라 실서버로 검증된 적이 없다. 켰을 때도 안전하게 실패해야 한다:

    - 플래그 꺼짐 / 심볼 없음 / 자격증명 없음 / 토큰 발급 실패 → None(라우트 없이
      Toss 단독 진행, 경고 로그). "폴백이 있는 척하는" 라우트를 등록하지 않는다는
      점에서 build_market_data의 history 폴백과 같은 원칙이다.
    - 웹소켓을 백그라운드 스레드에서 기동하고 `startup_timeout_seconds` 안에 연결이
      안정되지 않으면 라우트 없이 진행한다(스레드 자체는 백그라운드에서 계속
      재접속을 시도하지만, 이번 세션에서는 쓰지 않는다).
    - 연결됐어도 **개별 심볼 단위**의 안전장치는 여기가 아니라
      `KiwoomRealtimeSource.quote()`가 담당한다 — 틱이 없거나 오래되면
      DataSourceError를 던져 MarketDataService가 toss로 폴백한다.
    - **US 심볼은 구독하지 않는다**(2026-08-29). 예전에는 KR+US를 섞어 한 REG에
      실었는데, 연결이 3시간 안정적으로 유지된 동안에도 REAL 프레임이 0건이었다
      — 유일하게 검증된 성공 사례(2026-08-10)는 005930 단일 심볼이었다. 혼합
      구독이 REAL 무전달의 용의자로 남았고, US 심볼은 애초에 이 경로로 틱이 온
      적이 없어 Toss 폴백으로 전량 처리되고 있었으므로 빼도 잃는 게 없다.
      KR 심볼만 있으면 이 함수는 원래와 동일하게 동작한다.
    """
    rt_cfg = (cfg.get("kiwoom", {}) or {}).get("realtime", {}) or {}
    if not rt_cfg.get("enabled", False):
        return None
    if not symbols:
        return None

    app_key = os.environ.get("KIWOOM_APP_KEY", "")
    secret_key = os.environ.get("KIWOOM_SECRET_KEY", "")
    if not (app_key and secret_key):
        logger.warning(
            "kiwoom.realtime.enabled=true인데 KIWOOM_APP_KEY/KIWOOM_SECRET_KEY가 없다 — "
            "실시간 라우트 없이 Toss만으로 동작한다"
        )
        return None

    from quant.adapters.brokers.kiwoom.client import KiwoomClient
    from quant.adapters.brokers.kiwoom.datafeed import KiwoomRealtimeSource, is_kr_symbol
    from quant.adapters.brokers.kiwoom.websocket import DEFAULT_WS_URL, KiwoomRealtimeFeed

    kr_symbols = [s for s in symbols if is_kr_symbol(s)]
    us_symbols = [s for s in symbols if not is_kr_symbol(s)]
    if us_symbols:
        logger.warning(
            "US 심볼 %d개는 실시간 구독에서 제외(2026-08-29: 혼합 구독이 REAL 무전달의 "
            "용의자, 단일 심볼 검증(08-10)과의 간극) — 시세는 Toss 폴백 그대로: %s",
            len(us_symbols), ", ".join(us_symbols),
        )
    if not kr_symbols:
        return None

    try:
        client = KiwoomClient(app_key=app_key, secret_key=secret_key)
        client.access_token()  # 자격증명 조기 검증 — 여기서 실패하면 라우트를 아예 안 만든다
    except Exception as e:
        logger.warning("Kiwoom 실시간 피드 비활성 — 토큰 발급 실패: %s: %s", type(e).__name__, e)
        return None

    base_url = os.environ.get("KIWOOM_BASE_URL", "")
    # 실전 base_url이면 실전 WS 호스트로 치환한다(문서 5.1 [확인됨]: 실전
    # wss://api.kiwoom.com:10000 + 경로, 모의 wss://mockapi.kiwoom.com:10000 + 경로).
    # base_url 미설정/모의면 안전한 기본값(모의) 그대로 둔다.
    ws_url = (
        DEFAULT_WS_URL.replace("mockapi.kiwoom.com", "api.kiwoom.com")
        if "mockapi" not in base_url and "api.kiwoom.com" in base_url
        else DEFAULT_WS_URL
    )
    # 토큰 **문자열**이 아니라 발급자를 넘긴다 — 재접속마다 다시 호출되므로 토큰이
    # 만료/폐기돼도 스스로 회복한다. 문자열을 캡처해 넘기던 이전 구현은 24시간짜리
    # 토큰이 만료되는 순간 재접속 루프가 죽은 토큰으로 영원히 실패했다
    # (2026-08-11~13 실장애: 805004가 20시간 넘게 30초마다 반복, 재시작 전까지 회복 불가).
    #
    # 세력 신호 수집(2026-08-28) — 기본 꺼짐. 켜면 같은 연결의 REG 에 0w/0F 가
    # 얹히고, 0B 프레임의 최우선호가(FID 27/28)도 원장에 쌓인다. 꺼져 있으면
    # types=["0B"], sink=None 이라 이 경로는 종전과 완전히 동일하게 동작한다.
    smart_flow, rt_types = build_smart_flow(cfg)
    feed = KiwoomRealtimeFeed(
        access_token=client.access_token,
        # 인증 실패 시 캐시를 버려야 다음 접속이 진짜로 새 토큰을 받는다 —
        # 이 배선이 없어 죽은 토큰으로 9일간 두드렸다(2026-08-11~20).
        invalidate_token=client.invalidate_token,
        ws_url=ws_url,
        symbols=kr_symbols,
        types=rt_types,
        smart_flow_sink=smart_flow,
    )
    if smart_flow is not None:
        logger.info(
            "세력 신호 수집 활성 — 구독 타입=%s, 적재=data/ledger/smart_flow.jsonl "
            "(수집 전용: 알림·주문 없음, 실서버 프레임 검증 전)", ", ".join(rt_types),
        )

    def _runner() -> None:
        try:
            asyncio.run(feed.run())
        except Exception as e:  # 백그라운드 스레드 — 여기서 죽으면 조용히 사라지므로 반드시 로깅
            logger.error("Kiwoom 실시간 피드 스레드 종료: %s: %s", type(e).__name__, e)

    threading.Thread(target=_runner, name="kiwoom-realtime", daemon=True).start()

    startup_timeout = float(rt_cfg.get("startup_timeout_seconds", 10.0))
    if not _wait_for_connection(feed, startup_timeout):
        logger.warning(
            "Kiwoom 실시간 웹소켓이 %.0f초 내 연결되지 않음 — 라우트 없이 진행(Toss 단독). "
            "스레드는 백그라운드에서 계속 재접속을 시도하지만 이번 세션에서는 쓰지 않는다",
            startup_timeout,
        )
        return None

    stale_seconds = float(rt_cfg.get("stale_seconds", 30.0))
    source = KiwoomRealtimeSource(feed, clock, stale_seconds=stale_seconds)
    logger.info(
        "Kiwoom 실시간 웹소켓 라우트 활성 — 심볼=%s, stale 임계=%.0f초",
        ", ".join(kr_symbols), stale_seconds,
    )
    return SourceRoute(
        name="kiwoom_rt",
        source=source,
        capabilities=frozenset({Capability.QUOTE}),
        symbols=frozenset(kr_symbols),
    )


def build_kiwoom_us_route(cfg: dict, symbols: list[str], clock) -> SourceRoute | None:
    """미국 종목 시세를 위한 키움 해외증권 REST 라우트 — Toss와 **별도의 rate limit
    예산**을 쓰므로 429가 근본적으로 완화된다(2026-08-12 실호출로 토큰 발급 +
    usa06010 분봉 조회 검증 완료).

    - `KIWOOM_GLOBAL_APP_KEY`/`KIWOOM_GLOBAL_SECRET_KEY`가 없거나 토큰 발급이
      실패하면 라우트를 등록하지 않는다(조용히 Toss 단독) —
      `build_kiwoom_realtime_route`와 같은 안전 실패 패턴.
    - KR 심볼은 절대 다루지 않는다(`is_kr_symbol` 게이트) — US 심볼이 하나도 없으면
      네트워크를 아예 타지 않고 None을 돌려준다.
    - **QUOTE만 등록하고 BARS는 등록하지 않는다.** `usa06010`(해외주식분봉차트)이
      실측으로 준 것은 최근 100행(~100분)뿐이고, 더 과거를 페이지네이션으로 받을
      수 있는지는 [미확인]이다. donchian(`lookback_bars=40`, 15분봉 → 600분 필요)
      같은 전략에 이 소스로 BARS를 서빙하면 MarketDataService는 예외가 아니라
      "짧지만 유효한" DataFrame을 정상 응답으로 받아들여 폴백하지 않는다 — 즉
      룩백이 조용히 얕아진 채로 전략이 계속 돈다. `KiwoomUSDataFeed.history()`는
      완전히 구현·테스트돼 있으니 usa06010의 페이지네이션이 [확인됨]으로 바뀌면
      capabilities에 Capability.BARS를 더하기만 하면 된다.
    """
    from quant.adapters.brokers.kiwoom.client import DEFAULT_CACHE_DIR, KiwoomClient
    from quant.adapters.brokers.kiwoom.datafeed import is_kr_symbol
    from quant.adapters.brokers.kiwoom.us_datafeed import KIWOOM_GLOBAL_BASE_URL, KiwoomUSDataFeed

    us_symbols = [s for s in symbols if not is_kr_symbol(s)]
    if not us_symbols:
        return None

    app_key = os.environ.get("KIWOOM_GLOBAL_APP_KEY", "")
    secret_key = os.environ.get("KIWOOM_GLOBAL_SECRET_KEY", "")
    if not (app_key and secret_key):
        logger.info(
            "KIWOOM_GLOBAL_APP_KEY/KIWOOM_GLOBAL_SECRET_KEY 없음 — kiwoom_us 라우트 없이 "
            "Toss 단독으로 US 시세를 조회한다"
        )
        return None

    # 별도 cache_dir — 국내 실시간용 KiwoomClient(KIWOOM_APP_KEY)와 토큰 캐시 파일이
    # 겹치면(둘 다 base_url이 api.kiwoom.com인 실전 환경) 서로 다른 appkey의 토큰을
    # 같은 파일에 써서 덮어쓰게 된다 — 반드시 분리한다.
    client = KiwoomClient(
        app_key=app_key,
        secret_key=secret_key,
        base_url=os.environ.get("KIWOOM_GLOBAL_BASE_URL", KIWOOM_GLOBAL_BASE_URL),
        cache_dir=DEFAULT_CACHE_DIR / "kiwoom_global",
    )
    try:
        client.access_token()
    except Exception as e:
        logger.warning("kiwoom_us 라우트 비활성 — 토큰 발급 실패: %s: %s", type(e).__name__, e)
        return None

    source = KiwoomUSDataFeed(client, clock)
    logger.info("kiwoom_us 라우트 활성 — US 심볼=%s (Toss와 별도 rate limit 예산)", ", ".join(us_symbols))
    return SourceRoute(
        name="kiwoom_us",
        source=source,
        capabilities=frozenset({Capability.QUOTE}),
        symbols=frozenset(us_symbols),
    )


WATCHLIST_OPT_IN = "watchlist"


def build_universe(cfg: dict, client=None) -> CompositeUniverse | None:
    """그날의 거래 유니버스. `universe.watchlist.enabled`가 꺼져 있으면 None.

    None을 돌려주면 호출부(전략 조립, paper 루프)는 유니버스가 없던 때와 100%
    동일하게 동작한다 — 이 기능은 끄면 흔적이 남지 않아야 한다.

    소스 순서 = 우선순위(`CompositeUniverse`는 먼저 나온 것을 남긴다):
    설정의 고정 바스켓(us+kr) → 관심종목 파일 → (켜져 있으면) Toss 랭킹.
    고정 바스켓을 앞에 두는 이유는 관심종목 파일이 비거나 깨져도 유니버스가
    통째로 비지 않게 하기 위해서다.
    """
    universe_cfg = cfg.get("universe", {})
    watchlist_cfg = universe_cfg.get("watchlist", {}) or {}
    if not watchlist_cfg.get("enabled"):
        return None

    sources: list = []
    base = list(universe_cfg.get("us") or []) + list(universe_cfg.get("kr") or [])
    if base:
        sources.append(StaticUniverse(base))
    sources.append(FileWatchlistUniverse(watchlist_cfg.get("path", DEFAULT_WATCHLIST_PATH)))
    ranking_cfg = universe_cfg.get("ranking", {}) or {}
    if ranking_cfg.get("enabled"):
        if client is None:
            logger.warning("랭킹 유니버스가 켜져 있지만 브로커 클라이언트가 없다 — 랭킹 소스 제외")
        else:
            sources.append(TossRankingUniverse(
                client, market=ranking_cfg.get("market", "US"),
                type=ranking_cfg.get("type", "MARKET_TRADING_AMOUNT"),
                count=int(ranking_cfg.get("count", 20)),
            ))
    logger.info("유니버스 활성 — 소스: %s", " + ".join(type(s).__name__ for s in sources))
    return CompositeUniverse(sources)


_MARKETS = ("KR", "US")


def _normalize_capital_fraction(sid: str, value: float | dict) -> dict[str, float]:
    """capital_fraction 설정값(스칼라 또는 시장별 dict)을 시장별 dict로 정규화한다.

    스칼라 → 양 시장에 동일하게 적용한다(2026-08-12 이전의 유일한 동작이었고,
    지금도 하위호환을 위해 100% 동일하게 유지된다). dict → 명시된 시장만 반영하고
    빠진 시장은 0.0으로 취급한다 — "모르면 안전한 쪽": 명시하지 않은 시장에는
    자본을 주지 않는다(RiskManagerImpl이 진입 자체를 차단한다).
    """
    if isinstance(value, dict):
        missing = [m for m in _MARKETS if m not in value]
        if missing:
            logger.info(
                "%s: capital_fraction에 %s 시장이 없음 — 0.0으로 취급(그 시장에서 진입 차단)",
                sid, ", ".join(missing),
            )
        return {m: float(value.get(m, 0.0)) for m in _MARKETS}
    v = float(value)
    return {m: v for m in _MARKETS}


def validated_capital_fractions(cfg: dict) -> dict[str, dict[str, float]]:
    """전략 검증 게이트 — 미검증(burn_in) 전략의 자본 배분을 상한으로 캡핑한다.

    2026-08-11 사용자 지시("검증 규율을 따르게끔 구축")의 강제 지점. 검증 규율을
    문서가 아니라 조립 루트가 강제한다 — 조립 루트는 이 저장소의 안전 경계다.

    반환값은 `{strategy_id: {market: capital_fraction}}`이다(2026-08-12 시장별
    배분 분리 — 이전에는 `{strategy_id: float}`였다). config의 capital_fraction이
    스칼라든 시장별 dict든 `_normalize_capital_fraction`이 먼저 시장별 dict로
    맞추고, 이후의 burn_in 캡·합계 초과 축소는 **시장마다 독립적으로** 판정한다.

    규칙:
    - `validation.status: verified` + `evidence`(비어있지 않은 근거 문자열) → 선언된
      capital_fraction 그대로(시장별로).
    - `verified`인데 evidence가 없으면 **burn_in으로 강등** + 경고. 근거 없는 검증
      선언은 검증이 아니다.
    - `backtest_pass`(2026-09-03, `quant/control/promotion.py`의 승격 CLI가 붙이는
      상태 — 백테스트 게이트 GO는 통과했지만 아직 실거래/paper 라운드트립으로
      검증되진 않았다) → capital_fraction 캡은 `burn_in`과 동일(0.2)하되, evidence
      요약을 info 로그로 남기고 로그 메시지 자체로 burn_in과 구분한다("승격 근거는
      있지만 실거래 검증 전"이라는 상태를 기동 로그에서 바로 읽을 수 있어야 한다).
    - `burn_in`(또는 validation 블록 자체가 없음 — 기본값이 안전한 쪽) →
      `validation_gate.burn_in_max_capital_fraction`(기본 0.2)으로 시장별 각각 캡.
    - 알 수 없는 status → burn_in 취급 + 경고. 오타가 게이트를 우회하면 안 된다.

    승격은 사람이 한다: 스코어보드 종결 30건 + 승률 신뢰구간이 50%를 위로 벗어나면
    (ledger.py 판정과 동일 기준) evidence를 채워 verified로 올린다. 코드가 자동
    승격하지 않는다 — 성적이 좋아 보이는 순간이 과최적화가 시작되는 순간이다.
    """
    cap = float((cfg.get("validation_gate") or {}).get("burn_in_max_capital_fraction", 0.2))
    out: dict[str, dict[str, float]] = {}
    for sid, c in cfg["strategies"].items():
        declared = _normalize_capital_fraction(sid, c.get("capital_fraction", 1.0))
        v = c.get("validation") or {}
        status = str(v.get("status", "burn_in"))
        evidence = str(v.get("evidence") or "").strip()

        if status == "verified" and evidence:
            out[sid] = declared
            continue
        if status == "verified":
            logger.warning(
                "검증 게이트: %s가 verified를 선언했지만 evidence가 없음 — burn_in으로 강등", sid
            )
        elif status == "backtest_pass":
            # burn_in과 같은 캡을 받지만(아래 capped 계산 공용) 로그로 구분한다 —
            # "백테스트 게이트는 통과했지만 실거래/paper 검증 전"이라는 중간
            # 상태를 burn_in(애초에 게이트를 안 돌렸을 수도 있음)과 뭉뚱그리면
            # 기동 로그만 보고 승격 진행 상황을 알 수 없다.
            logger.info(
                "검증 게이트: %s는 backtest_pass — 백테스트 게이트 GO, 실거래/paper 검증은 "
                "아직(30 라운드트립 + 승률 유의 전까지 burn_in과 동일 상한 0.2 적용). 근거: %s",
                sid, evidence or "(evidence 없음)",
            )
        elif status != "burn_in":
            logger.warning(
                "검증 게이트: %s의 validation.status=%r는 알 수 없음 — burn_in 취급", sid, status
            )
        capped = {m: min(declared[m], cap) for m in _MARKETS}
        if any(capped[m] < declared[m] for m in _MARKETS):
            logger.warning(
                "검증 게이트: %s는 미검증(burn_in) — capital_fraction %s → %s 캡핑"
                " (승격 조건: 스코어보드 종결 30건 + 승률 유의)",
                sid, declared, capped,
            )
        out[sid] = capped

    # 활성 전략의 배분 합이 시장별로 100%를 넘으면 그 시장만 비례 축소한다.
    #
    # 2026-08-12 감사에서 실제로 걸린 함정이다: 전략을 하나 추가하거나 burn_in을
    # verified로 승격하는 순간 합이 조용히 1.0을 넘는다(그때 실측 1.10). 각 전략의
    # 선언값만 보면 아무도 틀리지 않았는데 **합계를 검산하는 곳이 없었다.**
    # 초과 배분은 곧 의도치 않은 레버리지다 — 조용히 넘어가면 안 된다. 시장별
    # 배분 분리 이후에는 KR 합이 넘쳤다고 US까지 같이 축소하면 안 되므로 시장마다
    # 독립적으로 판정한다.
    active = {sid: f for sid, f in out.items() if cfg["strategies"][sid].get("enabled", True)}
    for m in _MARKETS:
        total = sum(f[m] for f in active.values())
        if total > 1.0 + 1e-9:
            scale = 1.0 / total
            logger.error(
                "검증 게이트: 활성 전략의 %s 시장 자본 배분 합이 %.2f로 100%%를 초과 — "
                "비례 축소(x%.3f). config/settings.yaml의 capital_fraction 합계를 "
                "1.0 이하로 맞출 것: %s",
                m, total, scale, {k: round(v[m], 3) for k, v in active.items()},
            )
            for sid in active:
                out[sid][m] = out[sid][m] * scale
    return out


def rebuild_strategies(
    cfg: dict, universe=None, held_symbols: list[str] | None = None,
    leverage_of: dict[str, float] | None = None,
    tags_of: dict[str, list[str]] | None = None,
    inbox_reader: Callable[[], list[dict]] | None = None,
):
    """유니버스 심볼을 반영해 전략을 (재)조립한다 → (strategies, markets, active_markets).

    ## 전략-유니버스 연결은 opt-in이다

    전략 설정에 `universe: watchlist`가 있는 전략만 심볼을 갈아끼운다. 지금
    구현된 전략은 전부 **심볼 쌍의 의미가 파라미터에 박혀 있다** — donchian은
    TQQQ/SQQQ 역방향 쌍을 전제로 `max_concurrent_names: 1`을 걸고, orb 계열은
    `long_symbol`/`inverse_symbol`로 방향을 판정한다. 여기에 임의의 관심종목을
    부으면 에러 없이 조용히 다른 전략이 된다. 그래서 심볼을 받겠다고 명시한
    전략에만 준다. 아직 그런 전략이 없으면 그 사실을 로그로 남긴다(관심종목이
    "적용된 것처럼 보이는" 상태가 제일 위험하다).

    ## 열린 포지션은 유니버스에서 빠져도 남긴다

    `held_symbols`(현재 열린 포지션)는 항상 심볼 목록에 유지된다. 유니버스는
    **신규 진입 후보**를 고르는 장치이지 청산 대상을 고르는 장치가 아니다 —
    관심종목에서 빼는 순간 전략이 그 종목을 못 보게 되면, 이미 들고 있는
    포지션의 손절/청산 로직이 통째로 사라진다(그 포지션은 영원히 방치된다).

    `leverage_of`는 그대로 `build_strategies`에 전달돼 `MeanReversionStrategy`
    같은 레버리지 인지 전략에 꽂힌다. 여기서는 조회하지 않는다(네트워크 I/O는
    호출부, 즉 부팅 시점의 `build_paper_runtime`에서만) — 세션 롤마다 이 함수를
    다시 부르는 `run.py`의 `_rebuild()`도 부팅 시점 스냅샷을 그대로 넘겨야
    한다(재조회하지 않으면 세션이 바뀔 때마다 이 정보가 조용히 사라지는 사고를
    막는다).

    `tags_of`는 leverage_of와 달리 **명시적으로 넘기지 않으면 이 함수가 매번
    `universe.tags()`에서 직접 채운다.** 레버리지는 브로커 API 조회가 필요해
    부팅 시점 스냅샷을 재사용해야 하지만(위 문단), 태그는 이미 메모리에 있는
    관심종목 파일 스냅샷(`FileWatchlistUniverse.tags()`)을 읽는 것뿐이라 매
    세션 롤마다 다시 읽어도 비용이 없고, 오히려 그래야 그 세션에 새로 등록된
    EVENT 태그가 바로 반영된다(스냅샷을 재사용하면 태그가 세션 하나만큼
    지연된다). `NewsMomentumStrategy`가 이 값을 받는다.

    `inbox_reader`도 명시적으로 넘기지 않으면 이 함수가 기본값
    `_read_llm_trader_inbox`(실제 파일 읽기)로 채운다 — tags_of와 같은 이유
    (매 호출마다 다시 읽어도 비용이 로컬 파일 하나뿐이다). `LlmTraderStrategy`가
    이 값을 받는다. 테스트에서 파일 I/O 없이 부르려면 `inbox_reader=lambda: []`
    등 명시적으로 넘기면 된다.
    """
    if inbox_reader is None:
        inbox_reader = _read_llm_trader_inbox
    markets = market_of(cfg.get("universe", {}))
    universe_symbols = list(universe.symbols()) if universe is not None else []
    if tags_of is None and universe is not None:
        tags_fn = getattr(universe, "tags", None)
        if callable(tags_fn):
            try:
                tags_of = tags_fn()
            except Exception as e:  # noqa: BLE001 — 태그 조회 실패가 재조립 전체를 막으면 안 된다
                logger.warning(
                    "관심종목 태그 조회 실패 — news_momentum EVENT 게이트 비활성: %s: %s",
                    type(e).__name__, e,
                )
    consumers = [
        sid for sid, strat_cfg in cfg.get("strategies", {}).items()
        if strat_cfg.get("enabled", True) and strat_cfg.get("universe") == WATCHLIST_OPT_IN
    ]

    effective_cfg = cfg
    if consumers:
        strategies_cfg = dict(cfg.get("strategies", {}))
        held = [s for s in (held_symbols or []) if s not in universe_symbols]
        for sid in consumers:
            symbols = universe_symbols + held
            if not symbols:
                logger.warning(
                    "전략 %s: 유니버스가 비어 있음 — 기존 심볼 %s 유지",
                    sid, strategies_cfg[sid].get("symbols"),
                )
                continue
            strategies_cfg[sid] = {**strategies_cfg[sid], "symbols": symbols}
        effective_cfg = {**cfg, "strategies": strategies_cfg}
        if held:
            logger.info("유니버스에서 빠졌지만 보유 중이라 유지하는 종목: %s", ", ".join(held))
    elif universe_symbols:
        logger.info(
            "관심종목 %d개 로드됨 — 소비하는 전략 없음(전략들이 고정 심볼 쌍 기반): %s. "
            "전략 설정에 `universe: %s`를 넣은 전략만 이 목록을 받는다",
            len(universe_symbols), ", ".join(universe_symbols), WATCHLIST_OPT_IN,
        )

    # inbox_reader 를 여기서 반드시 전달한다 — 2026-08-31 실사고: 파라미터를
    # 받아 기본값(_read_llm_trader_inbox)까지 채워놓고 이 호출에서 빠뜨려,
    # llm_trader 가 스텁 리더(빈 목록)로 조립됐다. 전략 쪽 `None이면 빈 목록`
    # 폴백이 실패를 무증상으로 만들었다(판단 13건 무체결). 배선 끝까지의
    # 주입은 tests/test_llm_trader_wiring.py 가 대조한다.
    strategies = build_strategies(effective_cfg, leverage_of=leverage_of, tags_of=tags_of,
                                  inbox_reader=inbox_reader)
    symbols = sorted({s for st in strategies for s in st.symbols})
    # 관심종목으로 새로 들어온 심볼은 settings의 us/kr 목록에 없어 매핑이 비는데,
    # 하위 계층은 전부 `.get(sym, "US")` 폴백을 쓴다 — KR 6자리 코드가 US로
    # 떨어지면 KRW 표시가격에 환율(1500)을 곱해 **명목가치가 1,500배 부풀고**,
    # 세션 판정도 미국 시간으로 어긋난다. KR 심볼은 구조적으로 식별 가능하므로
    # (6자리 숫자 — docs/api/toss/QUICKREF + brokers/toss/broker.py의 동일 정규식)
    # 매핑이 만들어지는 이 한 곳에서 채워 넣는다.
    for sym in symbols:
        if sym not in markets:
            markets[sym] = "KR" if (sym.isdigit() and len(sym) == 6) else "US"
    active_markets = frozenset(markets[s] for s in symbols)
    return strategies, markets, active_markets


def equal_split_initial_krw(
    broker, active_strategy_ids: list[str], fx: FxProvider | None = None
) -> float | None:
    """`capital_policy: equal_split`의 전략당 시작 명목자본 = 실제 총현금 ÷
    활성 전략 수.

    **총현금에는 USD 풀도 KRW 환산으로 포함한다(fx가 주어질 때).** 2026-09-01
    실계좌 이식 첫 기동에서 KRW 풀(2.98M)만 나눠 전략당 248k가 됐는데, 계좌의
    실질 2/3는 USD($6.2k≈8.5M)였다 — US 레인 명목 예산이 $180 수준이 되어 US
    주식 대부분이 1주도 못 사는(int 내림 → 0주) 침묵 무거래가 될 뻔했다.
    명목 예산을 KRW 합산으로 잡아도 초과 지출은 불가능하다 — 실제 주문은
    통화별 지갑(cash/cash_usd)과 리스크 게이트가 각자 clamp하므로 환전 없이
    한도가 지켜진다. 명목은 배분 비율일 뿐, 지출 한도는 지갑이다.

    **`None`은 "0원"이 아니다 — "모른다"다.** `broker.cash()`가 예외를 던지거나
    0 이하를 돌려주면 총현금을 모르는 것인데, 그걸 0으로 위장해 새 전략을
    시딩하면 그 전략은 **영원히 아무것도 못 사는 상태로 조용히 굳는다**(2026-08-19
    사용자 지적) — 이 저장소가 이미 겪은 "실패를 빈 값으로 위장"·"모르는 것을
    안전한 쪽으로 가정"과 같은 부류의 사고다(`cross_momentum` 8일 무동작 사건).
    호출부(`build_paper_runtime`)는 `None`이면 이번 기동에 신규 전략을 시딩하지
    않는다 — 이미 존재하는 전략의 장부는 생성 시점에 자기 `initial_krw`가
    못박혀 있으므로(`quant/trade/risk/books.py` `_ensure`) 영향받지 않고 계속
    거래한다.

    어댑터(`TossBroker.cash`)는 이미 자기 네트워크 예외를 삼키고 0.0을 돌려주는
    관례라 여기서 예외가 실제로 올라오는 일은 드물지만, `broker.cash()` 호출부가
    이 값을 신뢰해 재무 판단(전략 시작 자본)을 내리는 지점이라 방어적으로도
    한 번 더 감싼다 — "모르면 안전한 쪽" 원칙을 어댑터 경계 하나에만 맡기지 않는다.
    """
    try:
        total_cash = broker.cash()
    except Exception as e:  # noqa: BLE001 — 실패를 0원처럼 위장하지 않는다
        logger.warning(
            "전략별 자본 정책 equal_split: 총현금 조회 실패(%s: %s) — 이번 기동은 신규 전략"
            " 시딩을 보류한다(기존 장부는 그대로 유지, 계속 거래)",
            type(e).__name__, e,
        )
        return None
    if total_cash is None or total_cash <= 0:
        logger.warning(
            "전략별 자본 정책 equal_split: 총현금 조회 결과 %r(0 이하 또는 없음) — 이번 기동은"
            " 신규 전략 시딩을 보류한다(기존 장부는 그대로 유지, 계속 거래)",
            total_cash,
        )
        return None
    # USD 풀 합산 — cash_usd()가 없거나(단일 통화 브로커) None(비활성/조회 실패)이면
    # KRW만으로 계산한다(기존 동작 그대로). 환율 조회 실패는 FxProvider 가 자체
    # fallback을 갖고 있어 여기까지 예외가 오지 않는 게 정상이지만, 명목 예산
    # 계산이 기동을 죽이면 안 되므로 한 번 더 감싼다.
    total_cash += _usd_pool_krw(broker, fx, policy="equal_split")
    return total_cash / len(active_strategy_ids) if active_strategy_ids else 0.0


def _usd_pool_krw(broker, fx: FxProvider | None, policy: str) -> float:
    """USD 현금 풀의 KRW 환산. `cash_usd()`가 없거나(단일 통화 브로커) None
    (비활성/조회 실패)이면 0 — 그때는 KRW 풀만으로 계산한다(기존 동작 그대로).

    환율 조회 실패는 FxProvider가 자체 fallback을 갖고 있어 여기까지 예외가 오지
    않는 게 정상이지만, 명목 예산 계산이 기동을 죽이면 안 되므로 한 번 더 감싼다.
    """
    if fx is None:
        return 0.0
    try:
        cash_usd_fn = getattr(broker, "cash_usd", None)
        usd = cash_usd_fn() if callable(cash_usd_fn) else None
        if usd is not None and usd > 0:
            return float(usd) * fx.usd_krw()
    except Exception as e:  # noqa: BLE001 — 명목 예산 계산 실패로 기동을 막지 않는다
        logger.warning("%s: USD 풀 환산 실패 — KRW만으로 계산: %s: %s",
                       policy, type(e).__name__, e)
    return 0.0


def declared_initial_krw(
    broker,
    capital_fraction: dict[str, dict[str, float]],
    active_strategy_ids: list[str],
    fx: FxProvider | None = None,
) -> dict[str, float] | None:
    """`capital_policy: declared`의 **전략별** 시작 명목자본.

    `strategy_equity[sid] = KRW풀 x capital_fraction[sid]["KR"]
                          + USD풀(KRW환산) x capital_fraction[sid]["US"]`

    ## 왜 이 정책이 필요한가 (2026-09-03)

    `equal_split`은 총현금을 활성 전략 수로 **똑같이** 나눈다. 그래서
    `strategies.*.capital_fraction`은 per_strategy 모드에서 크기 정보를 전부 잃고
    `<= 0` on/off 게이트로만 남았다(risk 블록의 2026-09-02 주석). 즉 "scalp_1m에
    US 18%, mr_vwap_quiet에 6%"라고 선언해 놓고 실제로는 둘 다 1/12씩 받았다 —
    설정 파일이 사실과 다른 말을 하는 상태였고, 그건 이 저장소가 가장 싫어하는
    실패 모드다(선언과 실행의 조용한 불일치). `declared`는 그 선언을 그대로
    집행한다.

    ## 왜 총현금이 아니라 **시장별 풀**로 나누나

    KR 전략은 KRW로만, US 전략은 USD로만 산다 — 실제 지출 한도는 통화별 지갑이다
    (`quant/trade/risk/manager.py`의 현금 게이트). 총현금 하나에 비중을 곱하면
    KR 비중 14%가 실제로는 존재하지 않는 USD를 포함한 금액이 되어, 명목은 크고
    지갑은 비는 어긋남이 생긴다. 시장별 풀로 나누면 각 시장의 명목 합계가 그
    시장의 실제 현금을 넘지 않는다. 두 시장 모두 비중이 있는 전략(scalp_1m
    KR .15 / US .18 등)은 **두 풀의 몫을 합산**한다 — 그 전략은 실제로 양쪽
    지갑에서 쓰기 때문이다.

    비중 합이 한 시장에서 1.0을 넘으면 그 시장만 비례 축소하고 WARNING을 남긴다.
    (`validated_capital_fractions`가 이미 같은 축소를 하므로 정상 경로에서는
    no-op이지만, 이 함수는 그 정규화를 신뢰하지 않고 스스로 검산한다 — 초과
    배분은 곧 의도치 않은 레버리지다.)

    **`None`은 "0원"이 아니라 "모른다"다** — `equal_split_initial_krw`와 같은
    계약이다. 총현금 조회가 실패하면 신규 전략 시딩을 보류해야지, 0원으로
    시딩해 그 전략을 영원히 못 사는 상태로 굳히면 안 된다.

    비중이 양 시장 모두 0인 전략은 **반환 dict에서 빠진다** — 그 전략은 어느
    시장에서도 진입이 차단돼 있으므로(`_capital_fraction_for`) 명목자본을 줄
    이유가 없다.
    """
    try:
        krw_pool = broker.cash()
    except Exception as e:  # noqa: BLE001 — 실패를 0원처럼 위장하지 않는다
        logger.warning(
            "전략별 자본 정책 declared: 총현금 조회 실패(%s: %s) — 이번 기동은 신규 전략"
            " 시딩을 보류한다(기존 장부는 그대로 유지, 계속 거래)",
            type(e).__name__, e,
        )
        return None
    if krw_pool is None or krw_pool <= 0:
        logger.warning(
            "전략별 자본 정책 declared: 총현금 조회 결과 %r(0 이하 또는 없음) — 이번 기동은"
            " 신규 전략 시딩을 보류한다(기존 장부는 그대로 유지, 계속 거래)",
            krw_pool,
        )
        return None
    pools = {"KR": float(krw_pool), "US": _usd_pool_krw(broker, fx, policy="declared")}

    fractions = {
        sid: {m: float((capital_fraction.get(sid) or {}).get(m, 0.0)) for m in _MARKETS}
        for sid in active_strategy_ids
    }
    for m in _MARKETS:
        total = sum(f[m] for f in fractions.values())
        if total > 1.0 + 1e-9:
            scale = 1.0 / total
            logger.warning(
                "capital_policy declared: 활성 전략의 %s 시장 capital_fraction 합이 %.3f로 "
                "100%%를 초과 — 비례 축소(x%.4f). 초과분 %.3f는 존재하지 않는 현금이다. "
                "config/settings.yaml의 합계를 1.0 이하로 맞출 것: %s",
                m, total, scale, total - 1.0,
                {sid: round(f[m], 3) for sid, f in fractions.items() if f[m] > 0},
            )
            for f in fractions.values():
                f[m] *= scale

    out: dict[str, float] = {}
    for sid, f in fractions.items():
        amount = sum(pools[m] * f[m] for m in _MARKETS)
        if amount > 0:
            out[sid] = amount
    return out


def build_paper_runtime(settings: Settings) -> PaperRuntime:
    """paper 루프에 필요한 것을 전부 배선한다."""
    cfg = settings.raw
    # 순서 주의: 시계가 장 운영 캘린더에 의존하고, 캘린더가 브로커 클라이언트에
    # 의존한다. 캘린더 없이 시계를 만들면 09:30~16:00 고정 시간표로 내려가고,
    # 조기폐장일(연 수 회, 13:00 마감)에 엔진이 장 마감 후 3시간 동안 포지션을
    # 관리하며 시간외 호가창으로 청산 주문을 낸다.
    client = build_toss_client()
    # 유니버스를 전략보다 먼저 만든다 — 관심종목이 첫 세션부터 반영되려면 심볼
    # 목록이 확정된 뒤에 전략/과거데이터 라우트를 조립해야 한다. 비활성이면
    # None이고 rebuild_strategies는 build_strategies와 동일하게 동작한다.
    universe = build_universe(cfg, client)
    strategies, markets, active_markets = rebuild_strategies(cfg, universe)
    if not strategies:
        raise RuntimeError("활성화된 전략이 없다 — config/settings.yaml의 enabled를 확인할 것")

    interval = f"{_primary_interval_minutes(cfg)}m"
    symbols = sorted({s for st in strategies for s in st.symbols})
    clock = WallClock(
        poll_seconds=settings.poll_seconds, calendar=TossSessionCalendar(client),
    )
    fx = build_fx_provider(client, clock)
    data = build_market_data(
        client, clock, interval=interval, symbols=symbols,
        quote_cache_seconds=float(cfg.get("engine", {}).get("quote_cache_seconds", 2.0)),
        cfg=cfg,
    )

    # 브로커 선택 — 실주문은 **MODE=live일 때만**, 그 외 전부 PaperBroker.
    # MODE는 systemd Environment가 단일 진실 소스다(프로세스 환경 > .env.local,
    # config.load_settings 참고). TossBroker 내부에도 MODE!=live면 HTTP 전에
    # 거부하는 가드가 한 겹 더 있다 — 여기 분기와 그 가드는 서로 독립적인
    # 이중 방어이지 중복이 아니다(조립 실수와 호출 실수를 각각 막는다).
    mode = os.environ.get("MODE", "paper")
    start_cash = float(os.environ.get("START_CAPITAL_KRW", 5_000_000))

    # 종목 표시명 + KR ETF 분류 — 부팅 1회(조립 단계라 네트워크 허용).
    # 이름은 텔레그램 메시지 가독성용("088350" 대신 "한화생명"), ETF 여부는 매도
    # 거래세 면제 판정용. 조회 실패/미분류 KR 심볼은 개별주로 취급(과세) — 비용
    # 과소평가가 가짜 엣지를 만드는 것보다 낫다. 세션 중 유니버스 롤로 새로 들어온
    # 심볼은 재시작 전까지 이름 없이(코드로) 표시되고 개별주로 과세된다.
    # 레버리지 배수(절대값) — leverageFactor. 3배 ETF(TQQQ/SOXL 등)/인버스2X(KODEX
    # 252670 등) 사이징 헤어컷(risk.manager._leverage_haircut)과 mean_reversion의
    # 레버리지 금지 게이트가 이 dict를 쓴다. 필드가 없거나 조회 실패한 심볼은
    # dict에서 빠진다 — 1.0(비레버리지)으로 단정하지 않는다(호출부가 "모름"으로
    # 보수적으로 처리한다).
    # 표시명은 **영속 캐시**에서 시작한다(2026-08-28 소유자 지적: "리포트에 한국
    # 주식이 번호만 보인다"). 원인은 이 조회 대상이 `markets`(현재 유니버스)뿐이라
    # **유니버스에서 빠진 보유 종목은 조회조차 되지 않는 것**이었다 — 보유는
    # 남는데 이름만 사라져 사용자가 종목코드를 검색해야 했다. 한 번 알게 된
    # 이름은 잃지 않는다(캐시는 커지기만 하고, 새 조회가 있으면 덮어쓴다).
    name_of: dict[str, str] = _load_symbol_names()
    kr_etf: set[str] = set()
    leverage_of: dict[str, float] = {}
    for sym, mkt in markets.items():
        try:
            info = client.stock_info(sym) or {}
        except Exception:  # noqa: BLE001 — 이름/유형/레버리지 미확인은 치명적이지 않다
            continue
        if info.get("name"):
            name_of[sym] = str(info["name"])
        if mkt == "KR" and info.get("securityType") in ("ETF", "FOREIGN_ETF"):
            kr_etf.add(sym)
        raw_leverage = info.get("leverageFactor")
        if raw_leverage is not None:
            try:
                leverage_of[sym] = abs(float(raw_leverage))
            except (TypeError, ValueError):
                pass
    _save_symbol_names(name_of)
    # 이 세션에서 새로 확인된 분류만 캐시에 **더한다**(합집합) — 위 kr_etf
    # 변수 자체는 건드리지 않는다(브로커 수수료 판정에 쓰이는 실거래 경로라
    # 이 조립 함수의 기존 동작을 그대로 유지한다). 저장 목적은 daily_wrap
    # "체결 비용" 절(오프라인, 네트워크 없음)이 KR ETF/개별주를 구분하는
    # 것뿐이다 — 그 리포트가 며칠 전 세션에서 확인된 종목도 알아보게 한다.
    _save_kr_etf(_load_kr_etf() | kr_etf)
    logger.info(
        "종목명 %d개 확보(영속 캐시 포함) · KR ETF(매도 거래세 면제): %s · 레버리지 확인: %s",
        len(name_of), ", ".join(sorted(kr_etf)) or "없음",
        ", ".join(f"{s}={v:g}x" for s, v in sorted(leverage_of.items())) or "없음",
    )
    # mean_reversion처럼 leverage_of를 생성자에서 받는 전략이 있다 — 위 조회가
    # markets(=최초 rebuild_strategies의 출력)에 의존하므로 전략을 다시 조립해야
    # 한다. 심볼/시장 구성은 최초 조립과 동일하다(leverage_of는 mean_reversion의
    # 게이트 여부만 바꾸지 심볼 목록을 바꾸지 않는다) — 위에서 이미 확정한
    # interval/symbols/clock/data는 그대로 유효하다.
    strategies, markets, active_markets = rebuild_strategies(cfg, universe, leverage_of=leverage_of)
    if mode == "live":
        from quant.adapters.brokers.toss.broker import TossBroker

        broker = TossBroker(client)
        # 2026-09-02 (C1): live 브로커에도 fx/market_of 를 붙인다. 루프의 전략별
        # 장부 갱신(`loop._execute_signal`)은 `ctx.broker.fx`/`.market_of` 를
        # duck-typing 으로 읽는데(PaperBroker 는 생성자로 받는다) live 에는 없어서
        # 매 체결마다 `books.apply_fill` 이 통째로 스킵됐다 — available_cash_krw /
        # 노출 게이트가 실계좌에서만 눈이 먼다. 아래 books= 주석의 2026-08-19 P0
        # 와 같은 부류("만든 것 ≠ 배선된 것")의 live 판 재발이다.
        # market_of 는 risk 와 **같은 dict 객체**를 공유해야 한다 — cli._rebuild 가
        # 유니버스 롤마다 이 dict 를 in-place update 해서 양쪽을 함께 갱신한다.
        broker.fx = fx
        broker.market_of = markets
        logger.warning(
            "실주문 브로커 활성 — TossBroker (MODE=live). 이 프로세스는 실제 계좌에 "
            "주문을 낸다. 엔진 소유 원장 밖의 보유(사용자 수동 매매)는 건드리지 않는다."
        )
    else:
        portfolio = Portfolio.load_or_init(start_cash=start_cash)
        broker = PaperBroker(
            data=data, portfolio=portfolio,
            fee_bps=cfg["execution"]["fee_bps"], market_of=markets, fx=fx,
            slippage_bps=cfg["execution"].get("slippage_bps", 0.0),
            kr_stock_sell_tax_bps=cfg["execution"].get("kr_stock_sell_tax_bps", 0.0),
            kr_etf_symbols=kr_etf,
            # 토스증권 미국주식 실제 요율(2026-08-19, execution.us_sec_fee_bps 등
            # — paper.py `_commission`/`_us_sec_fee` docstring 참고).
            us_sec_fee_bps=cfg["execution"].get("us_sec_fee_bps", 0.0),
            us_sec_fee_min_usd=cfg["execution"].get("us_sec_fee_min_usd", 0.0),
            us_taf_per_share=cfg["execution"].get("us_taf_per_share", 0.0),
            us_taf_cap_usd=cfg["execution"].get("us_taf_cap_usd", 0.0),
            us_free_commission_notional_usd=cfg["execution"].get(
                "us_free_commission_notional_usd", 0.0),
            # 통화별 지갑 분리(2026-09-01 소유자 지시). 기본값 False로 명시적
            # opt-in — 설정에 없는 환경(과거 settings.yaml, 다른 프로파일)은
            # 기존 단일 KRW 풀 동작 그대로다. paper.py dual_currency 참고.
            dual_currency=bool(cfg["execution"].get("dual_currency_cash", False)),
        )
    capital_fraction = validated_capital_fractions(cfg)
    # 전략별 독립 명목계정(2026-08-19, 모의투자 중 전략별 성장곡선 비교용 —
    # 사용자 직접 지시). `risk.capital_mode: per_strategy`일 때만 만든다 —
    # 기본값 shared에서는 books가 None이라 파일도 생기지 않고(흔적 없음),
    # risk.approve()도 계좌 전체 equity 기준으로 기존과 100% 동일하게
    # 사이징한다. `capital_mode: shared` 한 줄로 언제든 즉시 복귀된다.
    risk_cfg = cfg.get("risk", {}) or {}
    capital_mode = str(risk_cfg.get("capital_mode", "shared"))
    # capital_policy(2026-08-19 Phase B, 실전 전환의 다리): fixed(기본)는 지금
    # 동작 그대로 — 전략마다 설정값(기본 1,000만원) 고정. equal_split은 실전
    # 전용 — **실제 총현금 ÷ 활성 전략 수**로 매 전략의 시작 명목자본을 정해
    # 명목 합계가 실계좌와 정확히 일치하게 한다(설계:
    # docs/superpowers/specs/2026-08-19-engine-separation-design.md Phase B).
    # declared(2026-09-03) — equal_split과 같은 총현금을 쓰되 **똑같이 나누지 않고**
    # 각 전략이 선언한 capital_fraction을 시장별 현금 풀에 곱한다
    # (`declared_initial_krw` docstring). equal_split이 capital_fraction을 사이징에서
    # 무의미하게 만들어 설정 파일이 사실과 다른 말을 하던 것을 끝낸다.
    capital_policy = str(risk_cfg.get("capital_policy", "fixed"))
    books: StrategyBooks | None = None
    if capital_mode == "per_strategy":
        require_books_capable_broker(broker)
        active_strategy_ids = [
            sid for sid, s in (cfg.get("strategies", {}) or {}).items()
            if isinstance(s, dict) and s.get("enabled")
        ]
        books_path = Path("data/state/strategy_books.json")
        seed_new_strategies = True
        if capital_policy == "equal_split":
            # broker는 위에서 이미 조립됐다 — paper는 Portfolio 현금(모의),
            # live는 TossBroker.cash()(실제 buying power)를 그대로 쓴다. 같은
            # 코드가 모의·실전 양쪽을 돈다는 게 이 정책의 핵심 이점이다.
            per_strategy_initial_krw = equal_split_initial_krw(broker, active_strategy_ids, fx=fx)
            if per_strategy_initial_krw is None:
                # 총현금을 모른다(조회 실패 또는 0 이하) — `equal_split_initial_krw`가
                # 이미 경고 로그를 남겼다. 여기서 신규 전략을 시딩하면 0원 시작
                # 자본이 조용히 굳으므로, 기존 장부만 그대로 로드하고 seed()는
                # 건너뛴다. 컨테이너 initial_krw는 override하지 않는다 — 파일에
                # 남아있는(있다면) 마지막으로 성공한 계산값을 그대로 쓴다.
                books = StrategyBooks.load(books_path, initial_krw=0.0)
                seed_new_strategies = False
            else:
                books = StrategyBooks.load(books_path, initial_krw=per_strategy_initial_krw)
                # equal_split은 재기동마다 실제 총현금이 달라진다. 컨테이너의
                # initial_krw를 파일에 저장된(첫 실행 때 잠긴) 값이 아니라 이번
                # 실행에서 새로 계산한 값으로 강제 갱신해야 "신규 전략만 새 분모를
                # 받는다"(설계 질문 2)가 성립한다 — 이미 존재하는 전략의 장부는
                # `books.py` `_ensure`가 생성 시점에만 initial_krw를 못박으므로 이
                # 갱신에 영향받지 않는다(설계 질문 1: 최초 1회 확정, 재조정 없음).
                books.initial_krw = per_strategy_initial_krw
        elif capital_policy == "declared":
            # 전략마다 금액이 다르므로 스칼라 하나로 표현할 수 없다 —
            # `books.initial_by_strategy`(2026-09-03)에 전략별 시작금을 싣고,
            # `_ensure`가 신규 장부를 만들 때 그 값을 쓴다. 이미 존재하는 장부는
            # equal_split과 똑같이 재조정되지 않는다(생성 시점 확정).
            declared = declared_initial_krw(
                broker, capital_fraction, active_strategy_ids, fx=fx,
            )
            if declared is None:
                # 총현금 미상 — 0원 시딩으로 전략을 영구 무거래 상태로 굳히지 않는다
                # (equal_split 분기와 같은 계약).
                books = StrategyBooks.load(books_path, initial_krw=0.0)
                seed_new_strategies = False
            else:
                books = StrategyBooks.load(books_path, initial_krw=0.0)
                books.initial_by_strategy = declared
                # 비중이 양 시장 모두 0인 전략은 declared에 없다 — 명목자본을 줄
                # 이유가 없으므로 시딩 대상에서도 뺀다(0원 장부를 만들면 그게 곧
                # "영원히 못 사는 장부"다).
                skipped = [sid for sid in active_strategy_ids if sid not in declared]
                if skipped:
                    logger.info(
                        "capital_policy declared: capital_fraction이 양 시장 모두 0인 전략은"
                        " 장부를 만들지 않는다(어차피 진입이 차단된다): %s",
                        ", ".join(sorted(skipped)),
                    )
                active_strategy_ids = [s for s in active_strategy_ids if s in declared]
                per_strategy_initial_krw = (
                    sum(declared.values()) / len(declared) if declared else 0.0
                )
        else:
            per_strategy_initial_krw = float(risk_cfg.get("per_strategy_initial_krw", 10_000_000))
            books = StrategyBooks.load(books_path, initial_krw=per_strategy_initial_krw)
        # 활성 전략의 출발선을 기동 시에 세워 둔다 — 첫 체결까지 기다리면 그
        # 전까지 전략별 리포트가 "장부 없음"으로 비어 보이고, 이 기능의 목적
        # (전략마다 얼마가 어떻게 자라는지 처음부터 보기)이 절반 사라진다.
        # 총현금을 모르는 equal_split 기동에서는 이 시딩 자체를 건너뛴다(위 참고).
        seeded = books.seed(active_strategy_ids) if seed_new_strategies else 0
        if seeded:
            books.save()
        if seed_new_strategies and capital_policy == "declared":
            # declared는 전략마다 금액이 달라 "전략당 N원"이 성립하지 않는다 —
            # 배분표를 그대로 남긴다(기동 로그가 그날의 자본 배분 증거가 된다).
            logger.info(
                "전략별 독립 명목계정 활성(declared) — 기존 장부 %d개 로드 + %d개 신규,"
                " 선언 배분: %s (data/state/strategy_books.json)",
                len(books.books) - seeded, seeded,
                {sid: round(v) for sid, v in sorted(books.initial_by_strategy.items())},
            )
        elif seed_new_strategies:
            logger.info(
                "전략별 독립 명목계정 활성(%s) — 전략당 %.0f원, 기존 장부 %d개 로드 + %d개 신규"
                " (data/state/strategy_books.json)",
                capital_policy, per_strategy_initial_krw, len(books.books) - seeded, seeded,
            )
        else:
            logger.warning(
                "전략별 독립 명목계정 활성(%s) — 총현금 미확인으로 신규 전략 시딩 보류,"
                " 기존 장부 %d개만 로드 (data/state/strategy_books.json)",
                capital_policy, len(books.books),
            )
    # 미체결 장부(Phase 6.5) — 루프가 주문 상태를 여기에 흘리고, 대사가 그걸 읽어
    # "사용자 수동 보유"와 "우리 주문의 늦은 체결"을 가른다. 둘을 섞으면 엔진이 자기
    # 포지션을 모르는 상태가 정보성 로그로 묻힌다. risk보다 먼저 만든다 — 아래
    # RiskManagerImpl가 같은 장부를 중복 진입 가드(pending_entry_qty)로도 쓴다.
    open_orders = OpenOrderBook()

    # state_path: 일일 회로차단기 상태(손실 한도 기준자산·주문 수·손절 쿨다운)를
    # 재시작에도 유지한다. 재시작은 "나쁜 날"에 더 자주 일어나므로(자동 halt 후
    # 재개, 배포) 레일이 가장 필요한 순간에 풀리던 문제 — 2026-08-12 감사 A-3.
    risk = RiskManagerImpl(
        cfg, capital_fraction=capital_fraction, market_of=markets, fx=fx,
        state_path=Path("data/state/risk_day.json"), leverage_of=leverage_of,
        books=books,
        # 미체결 중복 진입 가드(2026-09-01) — side=Side.BUY로 좁힌다: 같은
        # (전략,종목)에 미체결 매도(청산)가 있어도 새 진입은 막지 않는다(청산은
        # 절대 막지 않는다는 이 파일 전체의 원칙과 동일선상).
        pending_entry_qty=lambda symbol, strategy_id: open_orders.pending_qty(
            symbol, strategy_id=strategy_id, side=Side.BUY
        ),
    )

    control = TradingControl()
    approval, approval_notifier, approval_cfg = build_approval(cfg)
    notifier = build_notifier(cfg)
    reconciler = build_reconciler(broker, control, notifier, cfg,
                                  pending_qty=open_orders.pending_qty)
    logger.info(
        "엔진 조립 완료 — 전략=%s, 판단주기=%.2f분, 시작현금=%.0f원",
        [s.id for s in strategies], clock.cadence_minutes(), start_cash,
    )
    logger.info(
        "킬 스위치 상태 — halted=%s%s",
        control.is_halted(), f" (사유: {control.halt_reason()})" if control.is_halted() else "",
    )
    # 국면(regime) 프로바이더. US는 로컬 QQQ 일봉(추세/변동성) + 국채/코스피/비트코인
    # (2026-08-24 — 그전까지 이 셋은 구현체가 없어 태어나서 한 번도 값을 준 적이
    # 없었다: TossIndicatorClient는 국채는 아직 미구현(None 유지)이고 코스피는 ETF
    # 프록시로 산다, UpbitBitcoinAdapter는 Upbit 공개 API), KR은 flow_client
    # (Toss candles 069500 + investor_trading 수급)로 시장별 국면을 나눠 계산한다
    # (2026-08-10 — KR 세션을 미국 지수로 판단하던 문제 해소). refresh()는 여기서
    # 부르지 않는다 — 네트워크/디스크 I/O는 run_paper_loop의 거래일 경계에서 1회.
    #
    # 2026-08-28: indicator_client를 CompositeIndicatorClient로 합성했다 —
    # FileMacroIndicatorClient(US_BOND_10Y, data/ledger/macro_rates.jsonl만 읽음,
    # 네트워크 없음)를 먼저 시도하고, KOSPI/KOSDAQ 프록시(+ 여전히 None인
    # KR_BOND_*)는 그대로 TossIndicatorClient가 답한다. 두 클라이언트의 지원
    # 심볼이 겹치지 않아 합성해도 KOSPI/KOSDAQ 프록시 동작은 그대로다(회귀 없음).
    regime = RegimeProvider(
        settings=cfg,
        flow_client=client,
        indicator_client=CompositeIndicatorClient([
            FileMacroIndicatorClient(),
            TossIndicatorClient(client),
        ]),
        bitcoin_adapter=UpbitBitcoinAdapter(),
    )
    logger.info("국면 모듈 준비 — 현재 캐시: %s",
                (regime.current_state().label if regime.current_state() else "없음(첫 세션에 계산)"))

    # 틱 로거(2026-08-28 소유자 지시) — Toss 1분봉이 4거래일 롤링이라 소급 불가하니
    # 엔진이 읽는 시세를 우리가 직접 초 단위로 쌓는다. tick_log.enabled: false여도
    # TickLogger(enabled=False)를 그대로 넘긴다(항상 주입) — 켜고 끄는 것은 이
    # 인스턴스 내부의 enabled 플래그가 한다: record()/flush_if_due()/close() 전부
    # 즉시 반환이라 버퍼도 안 쌓고 디스크도 안 건드린다(흔적 없음).
    # 전략 간 합산 노출 감시 클로저(2026-08-30) — quant/control/exposure.py(순수)를
    # 여기서 감싸 loop.py에 주입한다(loop.py는 quant.control을 직접 임포트할 수
    # 없다 — tests/test_architecture.py FORBIDDEN). 임계값은 risk_cfg의 퍼센트
    # 표기(예: 100)를 코드 내부 표기(1.0)로 여기서 한 번만 나눈다 — 다른
    # max_*_pct 값들과 같은 관례(risk/manager.py 참고).
    exposure_alert_pct = float(
        risk_cfg.get("cross_strategy_leverage_alert_pct", DEFAULT_ALERT_PCT * 100)
    ) / 100

    def _exposure_check(lots: dict, prices: dict, capital_krw: float | None) -> dict:
        report = build_exposure_report(
            lots=lots, prices=prices, leverage_of=leverage_of,
            capital_krw=capital_krw, alert_threshold_pct=exposure_alert_pct, fx=fx,
        )
        return report.to_dict()

    tick_log_cfg = cfg.get("engine", {}).get("tick_log", {}) or {}
    tick_logger = TickLogger(
        Path("data/ticks"),
        flush_seconds=float(tick_log_cfg.get("flush_seconds", 30.0)),
        enabled=bool(tick_log_cfg.get("enabled", True)),
    )

    return PaperRuntime(
        strategies=strategies,
        ctx=Context(clock=clock, data=data, broker=broker),
        risk=risk,
        # TradeLedgerSink: 체결을 data/state/trades.jsonl에 영속화 — 누적 스코어보드
        # (`run scoreboard`)의 원천. 래퍼라 기존 sink 구성은 그대로다.
        # OpenOrderBook 을 sink 체인 **안**에 넣는다 — 루프는 `on_order` 를 가진
        # 싱크에만 주문 상태를 준다(isinstance 판정). 체인 밖에 두면 장부가 영원히
        # 비어 있고 대사는 아무 변화도 못 느낀다("적용했다고 믿기").
        sinks=TradeLedgerSink(MultiSink([ConsoleSink(), JsonlSink(), open_orders])),
        notifier=notifier,
        data=data,
        fx=fx,
        control=control,
        active_markets=active_markets,
        approval=approval,
        approval_notifier=approval_notifier,
        approval_cfg=approval_cfg,
        reconciler=reconciler,
        regime=regime,
        name_of=name_of,
        universe=universe,
        leverage_of=leverage_of,
        # 2026-08-19 실전 P0: 위에서 RiskManagerImpl(books=books)에는 넘겼는데 여기
        # PaperRuntime 에 빠뜨렸다. 그래서 **사이징만** 전략별 1,000만원으로 돌고
        # 체결은 장부에 기록되지 않았다 — available_cash_krw 가 영원히 1,000만원을
        # 답해 전략별 현금 게이트가 무력화됐고, 개장 7분 만에 공유 계좌 현금이
        # -10,470,186원까지 내려갔다(체결 13건, 장부 파일은 mtime 그대로).
        # "만든 것 ≠ 배선된 것" — 조립 지점을 끝까지 따라가지 않으면 절반만 켜진다.
        books=books,
        tick_logger=tick_logger,
        exposure_check=_exposure_check,
    )


def build_reconciler(broker, control, notifier, cfg: dict,
                     pending_qty=None) -> Reconciler | None:
    """엔진 소유 원장을 노출하는 브로커에서만 대사기를 만든다.

    PaperBroker는 portfolio.json이 곧 엔진 소유라 대조할 상대가 없다 — None을
    돌려주고 루프는 대사가 없던 때와 100% 동일하게 동작한다."""
    interval_minutes = float(cfg.get("engine", {}).get("reconcile_interval_minutes", 5))
    stale_order_seconds = float(cfg.get("engine", {}).get("stale_order_seconds", 120))
    reconciler = Reconciler(broker, control, notifier, interval_minutes=interval_minutes,
                            pending_qty=pending_qty, stale_order_seconds=stale_order_seconds)
    if not reconciler.supported:
        logger.info("브로커 대사 비활성 — %s는 엔진 소유 원장을 노출하지 않는다", type(broker).__name__)
        return None
    logger.info("브로커 대사 활성 — %.0f분 주기, 불일치 시 신규 진입 halt", interval_minutes)
    return reconciler


def build_approval(cfg: dict, mode: str | None = None):
    """신규 진입 승인 게이트를 조립한다 → (gate, bot, approval_cfg).

    비활성이면 (None, None, {})를 돌려주고, 루프는 승인 게이트가 없던 때와 100%
    동일하게 동작한다.

    텔레그램이 미설정이어도 게이트는 켜둔 채 돌려준다 — 이 조합에서는 신규 진입이
    전부 차단된다(fail-closed). 조용히 게이트를 꺼서 승인 없는 실거래 진입을
    허용하는 것보다, 아무 진입도 안 되는 편이 안전한 방향이다."""
    approval_cfg = cfg.get("approval", {})
    if not approval_cfg.get("enabled"):
        return None, None, {}
    mode = mode or os.environ.get("MODE", "paper")
    required_modes = approval_cfg.get("required_modes", ["live"])
    if mode not in required_modes:
        logger.info("승인 게이트 비활성 — MODE=%s (required_modes=%s)", mode, required_modes)
        return None, None, {}

    from quant.adapters.notify.telegram_approval import TelegramApprovalBot

    gate = ApprovalGate()
    bot = TelegramApprovalBot.from_env()
    if not bot.enabled:
        logger.error(
            "승인 게이트가 켜져 있는데 텔레그램 토큰/챗ID가 없다 — 신규 진입이 전부 "
            "차단된다(fail-closed). .env.local의 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 확인할 것"
        )
    logger.info(
        "승인 게이트 활성 (MODE=%s) — 만료 %s초, 가격 드리프트 한도 %s%%. 청산은 승인 없이 즉시 실행된다",
        mode, approval_cfg.get("expire_seconds"), approval_cfg.get("max_price_drift_pct"),
    )
    if gate.pending():
        logger.warning("이전 실행에서 남은 승인 대기 %d건 — 만료 시각이 지났으면 첫 사이클에 정리된다",
                       len(gate.pending()))
    return gate, bot, approval_cfg


def build_notifier(cfg: dict) -> Notifier | None:
    if not cfg.get("notifications", {}).get("telegram", {}).get("enabled"):
        return None
    try:
        from quant.adapters.notify.telegram import TelegramNotifier

        notifier = TelegramNotifier.from_env()
    except Exception as e:
        logger.warning("텔레그램 알림 비활성 (%s: %s)", type(e).__name__, e)
        return None
    if not getattr(notifier, "enabled", False):
        logger.warning("텔레그램 토큰/챗ID 미설정 — 알림 없이 진행한다")
        return None
    logger.info("텔레그램 알림 활성")
    return notifier


def _primary_interval_minutes(cfg: dict) -> int:
    """활성 전략들이 쓰는 봉 간격. 서로 다르면 가장 짧은 쪽에 맞춘다.

    전략마다 파라미터 이름이 다르다(donchian=`interval_minutes`,
    orb=`bar_interval_minutes`). 한쪽만 읽으면 다른 전략을 켤 때 조용히 기본값 15분이
    잡히고, 과거 데이터 폴백 라우트가 실제로 쓰는 간격과 어긋난 채로 기동한다 —
    에러가 아니라 "틀린 간격으로 정상 동작"이라 더 위험하다.
    """
    minutes = [
        int(params.get("interval_minutes", params.get("bar_interval_minutes", 15)))
        for s in cfg.get("strategies", {}).values()
        if s.get("enabled")
        for params in [s.get("params", {})]
    ]
    # 2026-09-02 (C3): 아무 전략도 간격을 선언하지 않으면 위 식은 조용히 15분으로
    # 떨어진다 — 실제로 활성 전략 전부가 1m/5m/1d 만 쓰는데 조립은 "15m"으로
    # 기동해 과거데이터 프로브가 엉뚱한 간격을 봤다. 침묵 대신 경고를 남긴다.
    declared = [
        s for s in cfg.get("strategies", {}).values()
        if s.get("enabled")
        and ("interval_minutes" in (s.get("params") or {})
             or "bar_interval_minutes" in (s.get("params") or {}))
    ]
    if minutes and not declared:
        logger.warning(
            "활성 전략 중 봉 간격(interval_minutes/bar_interval_minutes)을 선언한 것이 "
            "하나도 없다 — 기본값 15분으로 기동한다. 과거 데이터 프로브가 전략이 실제로 "
            "쓰는 간격과 다른 간격을 볼 수 있다."
        )
    return min(minutes) if minutes else 15
