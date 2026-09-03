"""전략 레지스트리 + settings.yaml 기반 팩토리."""
from __future__ import annotations

import logging

from quant.core.models import market_of, market_of_symbol
from quant.trade.strategy.confluence import ConfluenceStrategy
from quant.trade.strategy.cross_momentum import CrossMomentumStrategy
from quant.trade.strategy.donchian import DonchianStrategy, DonchianPureShell
from quant.trade.strategy.close_bet import CloseBetPureShell, CloseBetStrategy
from quant.trade.strategy.frgn_accumulate import FrgnAccumulatePureShell, FrgnAccumulateStrategy
from quant.trade.strategy.intraday_scan import IntradayScanStrategy
from quant.trade.strategy.llm_trader import LlmTraderStrategy
from quant.trade.strategy.mean_reversion import MeanReversionStrategy
from quant.trade.strategy.news_momentum import NewsMomentumPureShell, NewsMomentumStrategy
from quant.trade.strategy.news_scalp import NewsScalpStrategy
from quant.trade.strategy.orb import OpeningRangeBreakoutStrategy
from quant.trade.strategy.overnight_drift import OvernightDriftShell
from quant.trade.strategy.orb_scan import OrbScanStrategy
from quant.trade.strategy.mr_vwap_quiet import MrVwapQuietShell
from quant.trade.strategy.pullback_impulse import PullbackImpulseShell
from quant.trade.strategy.scalp_1m import Scalp1mPureShell, Scalp1mStrategy
from quant.trade.strategy.vol_breakout import VolBreakoutShell
from quant.trade.strategy.intraday_momentum import IntradayMomentumShell
from quant.trade.strategy.gap_fade import GapFadeShell
from quant.trade.strategy.rsi2_dip import Rsi2DipShell
from quant.trade.strategy.orb_rvol import OrbRvolShell
from quant.trade.strategy.eod_reversal import EodReversalShell
from quant.trade.strategy.open_reversal import OpenReversalShell

logger = logging.getLogger(__name__)

STRATEGY_REGISTRY = {
    "donchian": DonchianStrategy,
    # 순수함수 계약 파일럿(엔진 분리 설계 Phase A) — donchian과 별도 이름으로 등록,
    # settings.yaml에는 아직 배선하지 않는다(활성화는 마감 후 판단).
    "donchian_pure": DonchianPureShell,
    "orb": OpeningRangeBreakoutStrategy,
    "orb_scan": OrbScanStrategy,
    "intraday_scan": IntradayScanStrategy,
    "mean_reversion": MeanReversionStrategy,
    "cross_momentum": CrossMomentumStrategy,
    "confluence": ConfluenceStrategy,
    "news_momentum": NewsMomentumStrategy,
    # 갈래 A/B (2026-08-17 스펙 §5) — paper 등록, 태그 배선 전까지 기본 비활성.
    "news_scalp": NewsScalpStrategy,
    "frgn_accumulate": FrgnAccumulateStrategy,
    # 1분봉 스캘프 (2026-08-18 스펙) — news_scalp(A)/intraday_scan(C)와 같은
    # 유니버스에서 진입 방식만 달리한 병행 전략. 태그 없음(watchlist 유니버스만).
    "scalp_1m": Scalp1mStrategy,
    # 순수함수 계약 이전(엔진 분리 설계 Phase A, donchian_pure 다음 대상) —
    # donchian_pure와 동일하게 별도 이름으로 등록만 하고 settings.yaml은 아직
    # 건드리지 않는다(비활성).
    "scalp_1m_pure": Scalp1mPureShell,
    # 종가배팅(2026-08-25, 전략 4종 체제 ③) — 마감 강한 종목을 종가에 사서
    # 다음날 시초 갭에 판다. CLOSE_BET 태그(장중 리포트 채점)만 소비.
    "close_bet": CloseBetStrategy,
    # 순수함수 계약 이전 완료분(2026-08-28, 소유자 지시 "전략만 바꿔끼면 나머지
    # 사이클이 알아서 번역해 스며들게"). donchian_pure/scalp_1m_pure 와 같은
    # 관례로 **별도 이름 등록만** 한다 — settings.yaml 은 건드리지 않으므로
    # 등록 자체는 동작을 바꾸지 않는다(선택되지 않으면 인스턴스화되지 않는다).
    # 전환은 규칙 동결기간(2026-08-28~) 이후 사람이 명시적으로 결정한다.
    # 각 순수 구현의 docstring 에 "아직 못 하는 것"(재시작 복구·고아 포지션·
    # 조회 최적화 소실)이 정직하게 적혀 있다 — 전환 전에 반드시 읽을 것.
    "news_momentum_pure": NewsMomentumPureShell,
    "frgn_accumulate_pure": FrgnAccumulatePureShell,
    "close_bet_pure": CloseBetPureShell,
    # 병렬 스캘핑 실험(2026-08-28 소유자 지시 "스캘핑 전략을 여러 개 병렬로
    # 돌려 실전에서 살아남는 것을 찾자"). 둘 다 순수 계약 신규 전략이라
    # 레거시 쌍둥이가 없다 — 이름에 _pure 를 붙이지 않는다.
    # 근거: 우리 원장 실측(손절 후 76% 회복 → 눌림목 / 고RVOL 역상관 -0.46
    # → 저거래량 회귀). 둘 다 5분봉 — 1분봉은 왕복 20bp 에서 산수가 안 맞는다.
    "pullback_impulse": PullbackImpulseShell,
    "mr_vwap_quiet": MrVwapQuietShell,
    # 오버나이트 드리프트(2026-08-28) — 종가 매수→익일 시가 매도. 문헌 근거
    # (Lachance RFE 2023: 오버나이트 +12.9%/yr vs 인트라데이 -4.3%/yr) + 왕복
    # 1회로 비용 대비 최우위. 동시에 **일중 전략들의 벤치마크** — 이걸 못
    # 이기면 일중 매매를 할 이유가 없다. US ETF 전용 의도.
    "overnight_drift": OvernightDriftShell,
    # 대회 확장 2차(2026-08-29 소유자 지시 "다양한 전략을 웹 근거로 긁어 병렬로").
    # 전부 순수 계약 신규 — 레거시 쌍둥이 없음, 이름에 _pure 안 붙인다.
    # 문헌 근거는 각 모듈 docstring 에 출처와 **혼재/한계까지** 명시돼 있다.
    # 넷 다 백테스트 표본 0 — paper 번인이 유일한 검증 경로이고, 시행 수가
    # 늘었으므로 성적 판독은 `strategy-report --trials` 신고가 전제다.
    "vol_breakout": VolBreakoutShell,       # Larry Williams 변동성 돌파(상승 확장)
    "intraday_momentum": IntradayMomentumShell,  # SSRN 4824172 — 하방은 인버스 ETF 매수로 표현(하락장 레인)
    "gap_fade": GapFadeShell,               # 갭하락 되돌림(근거 혼재 — 소액 출전)
    "rsi2_dip": Rsi2DipShell,               # Connors RSI(2) 눌림 — 오버나이트 보유형
    # LLM 트레이더(2026-08-30 소유자 승인) — 12번째 전략, LLM 판단 실험 레인.
    # "LLM 자체에게 전략과 판단을 맡기는 게 하나의 전략이 되는 것, 똑같이
    # 1,000만원 모의, 기존 시스템 위에, 한 달 테스트." 결정은 별도 프로세스가
    # data/state/llm_trader_inbox.jsonl 에 쓰고 이 전략은 읽기만 한다 — LLM은
    # 거래 핫패스에 없다. 근거·가드레일은 llm_trader.py 모듈 docstring.
    "llm_trader": LlmTraderStrategy,
    # 문헌 기반 일중 전략 3종(2026-09-03) — 전부 롱 온리·당일 청산(오버나잇 금지),
    # KR 우선. 순수 계약 신규라 레거시 쌍둥이가 없고 이름에 `_pure` 를 붙이지 않는다.
    # **셋 다 `enabled: false` 로 등록만 한다** — 백테스트를 통과해야 켠다(각 모듈
    # docstring 의 "아직 못 하는 것" 절에 표본 0 이라고 적혀 있다).
    "orb_rvol": OrbRvolShell,           # Zarattini/Barbon/Aziz 2024 "Stocks in Play" — 개장 레인지 돌파 + rvol 선별
    "eod_reversal": EodReversalShell,   # 장 막판 일중 반전(Baltussen/Da/Soebhag 2024 + KOSPI 실증)
    "open_reversal": OpenReversalShell,  # 전일 패자 개장 매수(국내 단기 반전 문헌)
}


# 태그(관심종목 파일의 `tags: [...]`) → 그 태그를 실제로 소비하는 전략 id.
# 2026-08-26, 소유자 조직도 역할 3 — "선정된 종목을 알맞는 전략에 지정" 배정을
# 코드로 명시한다. 실제 소비 코드를 grep 으로 전수 추적해 작성했다(추측 아님):
# 각 전략 클래스 생성자가 `tags_of`를 받는지(`quant/trade/strategy/*.py`,
# `_TAGS_OF_CONSUMERS`와 정확히 같은 집합이어야 한다 — `tests/test_tag_assignment.py`가
# 대조한다), 받는다면 어떤 태그 상수로 게이트하는지(각 파일의 `_EVENT_TAG` 등).
#
# "*" 는 특정 태그 게이트가 없는 나머지(무태그·TREND·REBOUND 등, watch_scorer.
# _VALID_TAGS 중 여기 다른 키에 없는 것 전부) — `universe: watchlist`로 유니버스
# 전체를 심볼로 받지만 `tags_of`를 아예 모르는 전략들이다. 이 전략들은 태그와
# 무관하게 유니버스에 들어온 심볼이면 다 감시 대상이라 "전체감시"로 뭉뚱그린다.
# **`_pure` 껍질은 이 표에 넣지 않는다**(2026-08-28): 전략 id 는 settings.yaml 의
# **설정 키**이고 순수 구현으로의 전환은 그 블록의 `class:` 만 바꾸는 것이라,
# id 는 그대로 "news_momentum" 이다. 여기에 `_pure` 를 넣으면 텔레그램 편입
# 알림에 실제로 감시하지 않는 이름이 표시된다(이 표는 사용자에게 보이는
# 배정 표시다). 반면 아래 `_TAGS_OF_CONSUMERS` 는 **클래스** 집합이라 껍질을
# 반드시 포함해야 한다 — 두 목록의 단위가 다르다.
TAG_ASSIGNMENT: dict[str, list[str]] = {
    # NewsMomentumStrategy._EVENT_TAG — 뉴스 촉매 있는 종목만 개장 즉시 진입.
    "EVENT": ["news_momentum"],
    # NewsScalpStrategy._SCALP_TAG — 갈래 A(2026-08-17). 2026-08-26 기준
    # config/settings.yaml `news_scalp.enabled: false`(paper 등록만, 비활성)지만
    # 코드 경로는 여전히 이 태그만 읽는다 — 활성화되면 그대로 유효.
    "EVENT_SCALP": ["news_scalp"],
    # FrgnAccumulateStrategy._FRGN_TAG/_FRGN_EXIT_TAG — 갈래 B, 외국인 적립
    # 매수/이탈 신호. 두 태그 다 같은 전략 하나만 읽는다.
    "FRGN": ["frgn_accumulate"],
    "FRGN_EXIT": ["frgn_accumulate"],
    # CloseBetStrategy._TAG — 종가배팅(2026-08-25, 전략 4종 체제 ③).
    "CLOSE_BET": ["close_bet"],
    # 게이트 없음 — universe: watchlist 인데 tags_of를 안 받는 전략들
    # (intraday_scan/orb_scan/cross_momentum/confluence/scalp_1m). 2026-08-26
    # 기준 이 중 scalp_1m만 enabled: true(전략 4종 체제).
    # 2026-09-03 추가 3종도 태그 게이트가 없다 — `universe: watchlist` 로 유니버스
    # 전체를 심볼로 받고 `tags_of` 를 아예 모른다(생성자에 그 인자가 없다).
    "*": ["intraday_scan", "orb_scan", "cross_momentum", "confluence", "scalp_1m",
          "orb_rvol", "eod_reversal", "open_reversal"],
}


def assignment_for(tags: list[str]) -> list[str]:
    """태그 목록(빈 리스트=무태그) → 이 태그를 실제로 소비하는 전략 id 목록
    (중복 제거, 순서 보존). `TAG_ASSIGNMENT`에 없는 태그(TREND/REBOUND 등
    게이트가 없는 태그 포함)는 "*"(유니버스 전체 감시) 버킷으로 떨어진다."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in (tags or ["*"]):
        for sid in TAG_ASSIGNMENT.get(tag, TAG_ASSIGNMENT["*"]):
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
    return out


def describe_tags(tags: list[str]) -> str:
    """텔레그램 표시용 축약 — 특정 전략을 소비하는 태그는 전략 id 그대로,
    게이트가 없는 태그("*" 버킷: 무태그/TREND/REBOUND 등)는 "전체감시"로
    한 번만 뭉친다(전체감시 전략이 5개인데 매번 나열하면, 그 목록이 바뀔 때마다
    브리핑 문구도 흔들린다). 예: `["TREND", "EVENT"]` → "news_momentum·전체감시"."""
    specific: list[str] = []
    seen: set[str] = set()
    has_catchall = False
    for tag in (tags or ["*"]):
        if tag in TAG_ASSIGNMENT and tag != "*":
            for sid in TAG_ASSIGNMENT[tag]:
                if sid not in seen:
                    seen.add(sid)
                    specific.append(sid)
        else:
            has_catchall = True
    if has_catchall:
        specific.append("전체감시")
    return "·".join(specific) if specific else "전체감시"


# ── 유니버스 필터 (2026-09-03, A/B 분할) ──────────────────────────────────────
# 같은 전략 클래스를 **두 갈래로 나란히** 돌리기 위한 설정 레벨 장치다. 기준
# 갈래(baseline)는 촉매 태그가 붙은 종목을 빼고, 촉매 갈래(`<id>_cat`)는 그
# 종목만 본다 — 두 갈래의 심볼 집합이 서로 겹치지 않으므로 원장의 `strategy_id`
# 만으로 A/B 비교가 성립한다(`quant.control.ledger.ab_compare`).
#
# **왜 전략 코드가 아니라 여기인가.** 진입 규칙을 한 글자도 바꾸지 않아야
# 비교가 성립한다. 태그를 읽는 전략들(news_momentum 등)은 클래스가 `tags_of`를
# 소비하지만 그건 "태그가 곧 진입 신호"인 설계다. 여기는 반대로 **감시 대상만**
# 자르고 진입 판단은 전략에 그대로 맡긴다 — 그래서 클래스에 손대지 않는다.
# (`TAG_ASSIGNMENT`는 이 장치를 반영하지 않는다: 그 표는 "생성자가 `tags_of`를
# 받아 태그로 진입을 게이트하는 클래스"의 표이고, `tests/test_tag_assignment.py`
# 가 소스에서 그 사실을 대조한다. 설정 레벨 필터를 그 표에 섞으면 대조가 거짓이
# 된다 — 두 장치는 단위가 다르다.)
_FILTER_KEYS = ("require_all", "require_any", "exclude_all", "exclude_any")


def _filter_clause_for(spec: dict, symbol: str) -> dict:
    """flat 형태(`{require_all: [...]}`)와 시장별 형태(`{KR: {...}, US: {...}}`)를
    둘 다 받는다. 시장별 형태인데 그 심볼의 시장에 절이 없으면 무필터(빈 dict) —
    KR 전용 촉매 규칙을 걸어도 US 심볼이 조용히 전멸하지 않는다."""
    if any(k in spec for k in _FILTER_KEYS):
        return spec
    clause = spec.get(market_of_symbol(symbol))
    return clause if isinstance(clause, dict) else {}


def filter_universe(
    symbols: list[str], spec: dict, tags_of: dict[str, list[str]] | None,
    held_symbols: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> tuple[list[str], list[str]]:
    """`universe_filter` 를 심볼 목록에 적용한다 → (남긴 심볼, 버린 심볼).

    절(clause)은 넷이고 전부 선택적이다(순서 무관, 여러 개를 함께 걸면 AND):

    - `require_all: [EVENT, FRGN]` — 두 태그를 **모두** 가진 심볼만 남긴다.
    - `require_any: [EVENT, TREND]` — 둘 중 **하나라도** 가지면 남긴다.
    - `exclude_all: [EVENT, FRGN]` — 두 태그를 **모두** 가진 심볼만 버린다.
    - `exclude_any: [EVENT]` — 하나라도 가지면 버린다.

    **`tags_of`가 없으면**(None/빈 dict) 모든 심볼이 무태그로 취급된다 — 즉
    `require_*`는 아무것도 고르지 못하고(전략이 조용히 무동작), `exclude_*`는
    아무것도 버리지 않는다. 그 자체가 옳은 안전 방향이다(태그를 모르는데 촉매
    갈래가 거래하면 그건 A/B 가 아니라 다른 실험이다). 호출부가 WARNING 을
    한 번 남긴다.

    **보유 중인 종목(`held_symbols`)은 필터를 무조건 통과한다.** 유니버스는
    신규 진입 후보를 고르는 장치이지 청산 대상을 고르는 장치가 아니다 — 태그가
    사라졌다고 전략이 그 종목을 못 보게 되면 이미 들고 있는 포지션의 손절/청산
    로직이 통째로 사라진다(`quant/apps/assembly.py rebuild_strategies` 의 같은
    규칙과 한 몸이다).
    """
    held = set(held_symbols or ())
    kept: list[str] = []
    dropped: list[str] = []
    for sym in symbols:
        clause = _filter_clause_for(spec, sym)
        tags = set((tags_of or {}).get(sym) or [])
        req_all = set(clause.get("require_all") or [])
        req_any = set(clause.get("require_any") or [])
        exc_all = set(clause.get("exclude_all") or [])
        exc_any = set(clause.get("exclude_any") or [])
        ok = True
        if req_all and not req_all <= tags:
            ok = False
        if req_any and not (req_any & tags):
            ok = False
        if exc_all and exc_all <= tags:
            ok = False
        if exc_any and (exc_any & tags):
            ok = False
        if ok or sym in held:
            kept.append(sym)
        else:
            dropped.append(sym)
    return kept, dropped


def build_strategies(
    cfg: dict, leverage_of: dict[str, float] | None = None,
    tags_of: dict[str, list[str]] | None = None,
    inbox_reader=None,
    held_symbols: list[str] | None = None,
) -> list:
    """cfg["strategies"] 블록을 읽어 활성화된 전략 인스턴스 리스트를 만든다.

    `leverage_of`는 `MeanReversionStrategy`처럼 레버리지 정보를 생성자에서 받는
    전략에만, `tags_of`는 `NewsMomentumStrategy`/`NewsScalpStrategy`/
    `FrgnAccumulateStrategy`처럼 관심종목 태그(EVENT/EVENT_SCALP/FRGN 등)를
    생성자에서 받는 전략에만, `inbox_reader`는 `LlmTraderStrategy`처럼 LLM 판단
    인박스를 읽는 콜러블을 생성자에서 받는 전략에만 전달한다 — 다른 전략 클래스는
    이 kwarg를 모르므로 무조건 넘기면 TypeError가 난다.

    `inbox_reader`가 `None`이면 `LlmTraderStrategy`가 자체 기본값(항상 빈 목록)을
    쓴다 — 실제 파일 I/O는 이 함수가 아니라 `quant/apps/assembly.py`(composition
    root)가 주입한다. 이 함수는 `quant/trade/` 소속이라 파일 I/O를 모른다
    (`quant/trade/strategy/CLAUDE.md`).

    `held_symbols`(현재 열린 포지션)는 `universe_filter`(2026-09-03 A/B 분할)를
    무조건 통과한다 — `filter_universe` docstring 참고. 인자로 안 주면
    `cfg["_held_symbols"]`에서 읽는다: 이 함수의 실제 호출부는
    `assembly.rebuild_strategies`인데 그 함수는 자기 `held_symbols`를 **심볼
    목록에 합쳐 넣을 뿐** 여기로 전달하지 않는다(어느 심볼이 보유분인지 구분이
    사라진다). 호출 체인 중간을 고치지 않고도 보유 종목을 보존할 수 있게 cfg에
    실어 보내는 경로를 함께 둔다 — `rebuild_strategies`가 `{**cfg, ...}`로
    통과시키므로 그대로 도착한다(`quant/apps/cli.py _rebuild` 가 채운다)."""
    markets = market_of(cfg.get("universe", {}))
    held = set(held_symbols if held_symbols is not None else (cfg.get("_held_symbols") or []))
    filtered_ids: list[str] = []   # universe_filter 를 건 전략들 — tags_of 결손 경고용
    strategies = []
    # 순수 껍질도 같은 태그 소비자다 — **누락하면 `tags_of`가 전달되지 않아 후보가
    # 0개가 되고 전략이 조용히 아무것도 하지 않는다**(2026-08-28 이관 워커들이
    # 배선 위험으로 명시). `TAG_ASSIGNMENT`와 이 튜플의 일치는
    # `tests/test_tag_assignment.py`가 대조한다.
    _TAGS_OF_CONSUMERS = (
        NewsMomentumStrategy, NewsScalpStrategy, FrgnAccumulateStrategy, CloseBetStrategy,
        NewsMomentumPureShell, FrgnAccumulatePureShell, CloseBetPureShell,
    )
    _INBOX_READER_CONSUMERS = (LlmTraderStrategy,)
    for strat_id, strat_cfg in cfg.get("strategies", {}).items():
        if not strat_cfg.get("enabled", True):
            continue
        cls = STRATEGY_REGISTRY[strat_cfg["class"]]
        symbols = strat_cfg["symbols"]
        # `market`은 **필터 전** 심볼로 정한다 — 필터가 목록을 비워도 전략의
        # 시장 판정이 US 로 미끄러지지 않게(기존 동작 보존).
        market = markets.get(symbols[0], "US") if symbols else "US"
        spec = strat_cfg.get("universe_filter")
        if spec:
            filtered_ids.append(strat_id)
            symbols, dropped = filter_universe(symbols, spec, tags_of, held)
            logger.info(
                "전략 %s: universe_filter 적용 — 유지 %d / 제외 %d (보유 유지 %d)",
                strat_id, len(symbols), len(dropped),
                len([s for s in symbols if s in held]),
            )
        kwargs = {}
        if cls is MeanReversionStrategy:
            kwargs["leverage_of"] = leverage_of
        if cls in _TAGS_OF_CONSUMERS:
            kwargs["tags_of"] = tags_of
        if cls in _INBOX_READER_CONSUMERS:
            kwargs["inbox_reader"] = inbox_reader
        strategies.append(cls(symbols=symbols, params=strat_cfg["params"], market=market, id=strat_id, **kwargs))
    if filtered_ids and not tags_of:
        # 심볼마다 찍지 않고 조립 1회당 한 줄 — 유니버스 롤은 하루 3번이라
        # 이 정도가 "태그 배선이 끊겼다"를 알아채기에 충분하고 로그를 안 죽인다.
        logger.warning(
            "universe_filter 를 건 전략(%s)이 있는데 tags_of 가 비었다 — 촉매(require_*)"
            " 갈래는 이번 조립에서 아무 종목도 감시하지 않는다(제외 갈래는 무영향)",
            ", ".join(filtered_ids),
        )
    return strategies
