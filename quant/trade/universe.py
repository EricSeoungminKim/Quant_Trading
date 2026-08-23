"""전략이 다룰 심볼 목록(유니버스)을 결정하는 프로바이더.

## 사용법 (조립 배선은 다른 워커 담당 — 여기서는 하지 않는다)

    from quant.trade.universe import StaticUniverse, TossRankingUniverse, CompositeUniverse

    static = StaticUniverse(["TQQQ", "SQQQ"])
    ranking = TossRankingUniverse(client, market="US", type="MARKET_TRADING_AMOUNT", count=20)
    universe = CompositeUniverse([static, ranking])
    symbols = universe.symbols()  # 심볼 리스트. 순서 보존 + 중복 제거.

사용자 관심종목(텔레그램 `/watch`로 편집되는 `data/watchlist.yaml`)을 섞으려면
`FileWatchlistUniverse`를 같은 `CompositeUniverse`에 넣는다. 파일을 다시 읽는
것은 `refresh()`뿐이므로, 세션 시작 경계에서 한 번 부르는 것이 호출자의 몫이다
(paper 루프의 KST 거래일 롤 — `quant/trade/loop.py`).

`TossRankingUniverse.symbols()`는 하루 1회만 실제로 API를 때린다(세션 시작 시
스냅샷 후 캐시). 장중 순위 변동으로 유니버스가 흔들리면 전략이 매 사이클 다른
종목 집합을 보게 되어 포지션 관리가 꼬인다 — 그래서 같은 거래일 안의 재호출은
캐시를 그대로 반환한다. 조회 실패 시 전일 캐시로 폴백하고, 캐시도 없으면(첫
실행 당일 실패) 빈 목록 + 경고를 남긴다. `CompositeUniverse`에 `StaticUniverse`를
함께 넣어 두면 이 경우에도 유니버스가 완전히 비지는 않는다.
"""
from __future__ import annotations

import logging
from datetime import date as dtdate
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

logger = logging.getLogger(__name__)

DEFAULT_WATCHLIST_PATH = Path("data/watchlist.yaml")


@runtime_checkable
class _UniverseSource(Protocol):
    def symbols(self) -> list[str]: ...


class StaticUniverse:
    """고정 심볼 목록. 항상 이 심볼들을 포함한다."""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = list(symbols)

    def symbols(self) -> list[str]:
        return list(self._symbols)


class TossRankingUniverse:
    """Toss `GET /api/v1/rankings` 기반 동적 유니버스. 하루 1회 스냅샷 후 캐시.

    실패 시 전일 캐시를 쓴다(당일 스냅샷을 못 갱신했을 뿐 어제 목록은 여전히
    유효한 근사치다). 캐시도 없으면 빈 목록 + 경고 로그.
    """

    def __init__(
        self, client, market: str, type: str, count: int = 20,
        duration: str = "realtime",
    ) -> None:
        self._client = client
        self._market = market
        self._type = type
        self._count = count
        self._duration = duration
        self._cached_day: dtdate | None = None
        self._cached_symbols: list[str] | None = None

    def symbols(self, today: dtdate | None = None) -> list[str]:
        today = today or dtdate.today()
        if self._cached_day == today and self._cached_symbols is not None:
            return list(self._cached_symbols)
        try:
            result = self._client.rankings(
                type=self._type, market_country=self._market,
                duration=self._duration, count=self._count,
            )
            fresh = [row["symbol"] for row in (result or {}).get("rankings", [])]
        except Exception as e:
            if self._cached_symbols is not None:
                logger.warning(
                    "TossRankingUniverse 갱신 실패, 전일 캐시 사용 (%s/%s): %s: %s",
                    self._market, self._type, type(e).__name__, e,
                )
                return list(self._cached_symbols)
            logger.warning(
                "TossRankingUniverse 갱신 실패이고 캐시도 없음 — 빈 목록 반환 (%s/%s): %s: %s",
                self._market, self._type, type(e).__name__, e,
            )
            return []
        self._cached_day = today
        self._cached_symbols = fresh
        return list(fresh)


def _parse_watchlist(raw: object) -> list[str]:
    """(하위호환 래퍼) 심볼 목록만 필요할 때."""
    return _parse_watchlist_with_names(raw)[0]


def _parse_watchlist_with_names(raw: object) -> tuple[list[str], dict[str, str], dict[str, list[str]]]:
    """watchlist.yaml 본문 → (심볼 목록, 이름 매핑, 태그 매핑). 형식이 아예 다르면 ValueError.

    항목 단위로 깨진 것(심볼 키가 없는 dict, 빈 문자열)은 건너뛴다 — 한 줄이
    이상하다고 나머지 관심종목을 통째로 버릴 이유는 없다. 반대로 최상위 구조가
    다르면(문자열, 리스트) 사용자가 의도한 것과 다른 파일이므로 실패로 본다.

    태그(`tags: [EVENT]` 등)는 브리지의 `watch-add --tags`가 적어둔다 — 뉴스 근거
    (EVENT)가 있는 종목만 개장 즉시 진입하는 전략(news_momentum)이 이걸 읽는다.
    tags가 없거나 빈 리스트인 항목(기존 항목 전부 포함 — 하위호환)은 매핑에서
    빠진다.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"최상위가 매핑이 아니다: {type(raw).__name__}")
    entries = raw.get("symbols") or []
    if not isinstance(entries, list):
        raise ValueError(f"symbols가 리스트가 아니다: {type(entries).__name__}")
    out: list[str] = []
    names: dict[str, str] = {}
    tags: dict[str, list[str]] = {}
    seen: set[str] = set()
    for entry in entries:
        symbol = entry.get("symbol") if isinstance(entry, dict) else entry
        if symbol is None or isinstance(symbol, (list, dict)):
            continue
        symbol = str(symbol).strip()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        out.append(symbol)
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if name:
                names[symbol] = name
            raw_tags = entry.get("tags")
            if isinstance(raw_tags, list):
                cleaned = [str(t).strip() for t in raw_tags if str(t).strip()]
                if cleaned:
                    tags[symbol] = cleaned
    return out, names, tags


class FileWatchlistUniverse:
    """사용자 관심종목 파일 기반 유니버스. 하루 1회 스냅샷. 엔진은 **읽기 전용**.

    ## 왜 `config/`가 아니라 `data/`인가

    이 파일을 쓰는 것은 엔진이 아니라 텔레그램 브리지다(사용자가 폰에서
    `/watch TQQQ`). `config/`는 git 추적 대상이라, 서버에서 수정된 파일이 있는
    상태로 배포(`git pull`)하면 충돌로 pull이 멈춘다 — 관심종목 한 줄 때문에
    엔진 배포가 막히는 것은 받아들일 수 없다. 그래서 포지션·컨트롤 파일과 같은
    런타임 상태로 취급해 `.gitignore`된 루트 `/data/` 아래에 둔다.

    ## 형식 (둘 다 받는다)

        symbols: [TQQQ, SQQQ]                                    # 손으로 편집할 때
        symbols:                                                 # 브리지가 쓸 때
          - {symbol: TQQQ, note: "돌파 감시", added_at: "2026-08-08T09:00:00+09:00"}

    ## 스냅샷 시맨틱

    `symbols()`는 캐시만 돌려주고, 파일을 다시 읽는 것은 `refresh()`뿐이다.
    장중에 폰으로 종목을 추가해도 그날의 거래 유니버스는 바뀌지 않고 다음 세션에
    반영된다 — 사이클마다 종목 집합이 달라지면 전략이 매번 다른 세계를 보게 되어
    포지션 관리가 꼬인다(`TossRankingUniverse`가 하루 1회만 API를 때리는 것과
    같은 이유).

    파일이 없거나 파싱이 실패하면 빈 목록으로 내려간다. 경고는 사유가 바뀔 때만
    한 번 남긴다 — 파일이 계속 없는 상태에서 매 세션 같은 줄을 찍으면 로그가
    무의미해진다. `CompositeUniverse`에 기본 바스켓을 함께 넣어 두면 이 경우에도
    유니버스가 완전히 비지는 않는다.
    """

    def __init__(self, path: Path | str = DEFAULT_WATCHLIST_PATH) -> None:
        self.path = Path(path)
        self._symbols: list[str] | None = None
        self._names: dict[str, str] = {}
        self._tags: dict[str, list[str]] = {}
        self._last_warning: str | None = None

    def names(self) -> dict[str, str]:
        """심볼 → 표시명. 브리지가 `/watch` 시점에 조회해 파일에 적어둔 값이다.

        엔진의 이름표(`loop._SYMBOL_NAMES`)는 부팅 시점 스냅샷이라 **그 뒤 편입된
        종목은 이름 없이 코드만 표시**된다(2026-08-12 실측: 02:50 부팅, 08:41 편입된
        192820이 "192820"으로만 표시). `market_of` 스테일과 같은 부류다 —
        세션 롤에서 이 값을 다시 읽어 갱신하면 네트워크 조회 없이 해결된다."""
        if self._symbols is None:
            self.refresh()
        return dict(self._names)

    def tags(self) -> dict[str, list[str]]:
        """심볼 → 태그 목록(예: `["EVENT"]`). 브리지의 `watch-add --tags`가 `/watch`
        시점에 파일에 적어둔 값이다 — names()와 같은 스냅샷 시맨틱(세션 롤에서만
        갱신). 태그 없는 항목(기존 항목 전부 포함)은 매핑에서 빠진다."""
        if self._symbols is None:
            self.refresh()
        return dict(self._tags)

    def symbols(self) -> list[str]:
        if self._symbols is None:
            self.refresh()  # 최초 1회는 지연 로드 — 호출자가 refresh를 잊어도 빈 목록이 되지 않게
        return list(self._symbols or [])

    def refresh(self) -> list[str]:
        """파일을 다시 읽어 스냅샷을 교체한다. 실패해도 예외를 올리지 않는다 —
        어댑터 계층의 I/O 실패는 여기서 삼키고 빈 목록으로 표현한다."""
        try:
            parsed, names, tags = _parse_watchlist_with_names(
                yaml.safe_load(self.path.read_text(encoding="utf-8"))
            )
        except FileNotFoundError:
            self._warn_once(f"관심종목 파일 없음 ({self.path}) — 빈 목록으로 진행한다")
            self._symbols = []
            return []
        except Exception as e:
            self._warn_once(
                f"관심종목 파일 파싱 실패 ({self.path}): {type(e).__name__}: {e} — 빈 목록으로 진행한다"
            )
            self._symbols = []
            return []
        self._last_warning = None
        self._symbols = parsed
        self._names = names
        self._tags = tags
        return list(parsed)

    def _warn_once(self, message: str) -> None:
        if message != self._last_warning:
            logger.warning("%s", message)
            self._last_warning = message


class CompositeUniverse:
    """여러 유니버스 소스의 합집합. 순서 보존 + 중복 제거(먼저 나온 소스가 우선)."""

    def __init__(self, sources: list[_UniverseSource]) -> None:
        self._sources = list(sources)

    def refresh(self) -> list[str]:
        """`refresh()`를 가진 소스만 다시 읽는다. 자체 일자 캐시로 갱신하는 소스
        (`TossRankingUniverse`)와 고정 목록(`StaticUniverse`)은 건드릴 것이 없다."""
        for source in self._sources:
            do_refresh = getattr(source, "refresh", None)
            if callable(do_refresh):
                do_refresh()
        return self.symbols()

    def symbols(self) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for source in self._sources:
            for sym in source.symbols():
                if sym not in seen:
                    seen.add(sym)
                    out.append(sym)
        return out

    def tags(self) -> dict[str, list[str]]:
        """소스 중 `tags()`를 가진 것만 병합한다(`FileWatchlistUniverse`만 해당 —
        `StaticUniverse`/`TossRankingUniverse`는 태그 개념이 없어 조용히 건너뛴다).
        같은 심볼이 여러 소스에 있으면 먼저 나온 소스의 태그를 쓴다(symbols()와
        같은 우선순위 규칙)."""
        out: dict[str, list[str]] = {}
        for source in self._sources:
            tags_fn = getattr(source, "tags", None)
            if not callable(tags_fn):
                continue
            for sym, t in (tags_fn() or {}).items():
                if sym not in out:
                    out[sym] = t
        return out
