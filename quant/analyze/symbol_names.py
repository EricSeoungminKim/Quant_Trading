"""종목코드 → 표시용 회사명 통합 리졸버.

**왜 필요한가.** 리포트 여러 곳(매매 리뷰·마감 요약·스코어보드·텔레그램 다이제스트)이
각자 "이름이 있으면 이름(코드), 없으면 코드"를 베껴 쓰고 있었다(`daily_wrap._label`,
`report/render/telegram.py`, `market_brief.py`, `collect/close.py`, `sector.py`의
`name or symbol`, Jinja `name|default(symbol)`). 그리고 결정론 사전(KIND/DART/
S&P500, `quant.analyze.entities.load_name_map`)에 없는 종목(신규 상장·워치리스트
밖 보유 종목 등)은 계속 코드 그대로 노출됐다.

이 모듈은 두 문제를 한 곳에서 푼다: (1) 표시 규칙 `label()` 단일 정의,
(2) 여러 소스를 순서대로 시도하는 `SymbolNameResolver.names_for()` — 마지막
수단으로 **한 번의 배치 LLM 호출**을 쓴다.

**평면 규칙**: 여기는 `quant/analyze/`다 — `quant.adapters`를 임포트할 수 없다
(`tests/test_architecture.py` FORBIDDEN). 그래서 Toss 조회와 LLM 호출기는
**주입받는다**(`toss_lookup`/`narrator` 콜러블) — 실제 어댑터 조립은 호출부
(`quant/apps/`, `quant/report/`)가 한다.

해석 순서: (1) `symbol_names.json` 캐시(단, LLM이 채운 항목은 잠정적이라 (2)에서
결정론 사전이 최신값을 찾으면 덮어쓴다) → (2) `load_name_map` KR/US 결정론
사전 → (3) 워치리스트(`data/watchlist.yaml`)의 `symbols[].name` → (4) `toss_lookup(symbol)`
(네트워크, 예외는 삼킨다) → (5) 아직 불명인 것만 모아 **한 번의** LLM 배치
호출 → (6) 그래도 없으면 결과에서 빠진다(호출부가 코드를 그대로 보여준다).

새로 확인된 이름은 전부 `symbol_names.json`(플랫 `{code: name}`, 기존 캐시와
호환)에 저장하고, 출처는 `symbol_names_meta.json`(`{code: {"source", "ts"}}`)에
남긴다. **결정론 소스는 이후 실행에서 LLM 항목을 덮어쓰지만, LLM은 결정론
항목을 절대 덮어쓰지 않는다** — 이는 알고리즘 순서 자체로 보장된다: 캐시가
비-LLM 출처면 그 자리에서 확정하고 뒤 단계로 넘기지 않는다.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from quant.analyze.entities import load_name_map
from quant.core.models import market_of_symbol

logger = logging.getLogger(__name__)

DEFAULT_LLM_BATCH_MAX = 60
_MAX_NAME_LEN = 40
_JUNK_RE = re.compile(r"[.\n]")


class _Narrator(Protocol):
    def narrate(self, prompt: str) -> str | None: ...


def label(symbol: str, name: str | None = None) -> str:
    """공용 표시 규칙 — 이름이 있고 코드와 다르면 "이름(코드)", 아니면 코드.

    없는 이름을 지어내지 않는다(`daily_wrap._label`과 동일 계약) — 여러 곳에
    흩어져 있던 같은 로직(`daily_wrap._label`, `report/render/telegram.py`
    `market_brief.py`/`collect/close.py`/`sector.py`의 `name or symbol`)을
    이 함수 하나로 대체한다.
    """
    if name and name != symbol:
        return f"{name}({symbol})"
    return symbol


def _read_json_dict(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _atomic_write_json(path: Path, data: dict) -> None:
    """원자적 tmp-replace. 쓰기 실패는 경고만 — 캐시 저장 실패가 리포트를 막으면
    안 된다(`quant.apps.assembly._save_symbol_names`와 동일 관례)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("종목명 캐시 저장 실패: %s", e)


def _valid_llm_name(code: str, value: object) -> str | None:
    """LLM 답변 검증 — 비어있지 않고, 40자 이하, 코드와 다르고, 숫자만이 아니고,
    문장(마침표/개행)이 아니어야 한다. 하나라도 어기면 거부한다(없는 이름을
    지어내는 것보다 코드 그대로 보여주는 게 낫다)."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MAX_NAME_LEN:
        return None
    if name == code:
        return None
    if name.isdigit():
        return None
    if _JUNK_RE.search(name):
        return None
    return name


def _usable(symbol: str, name: object) -> str | None:
    """이름으로 쓸 수 있는 값만 — 비어 있거나 코드와 같으면 '모른다'(None)."""
    if not isinstance(name, str):
        return None
    nm = name.strip()
    if not nm or nm.upper() == symbol.upper():
        return None
    return nm


def _toss_display_name(symbol: str, info: object) -> str | None:
    """Toss `stock_info` 응답에서 표시용 이름. ETF 는 `name` 이 티커 그대로 오고
    (EWY→"EWY", 2026-09-07 EC2 실측) `englishName` 에만 이름이 있다 — 코드와 같은
    이름은 "모른다"로 취급해야 캐시가 코드=이름 항목으로 영원히 굳지 않는다."""
    if not isinstance(info, dict):
        return None
    for key in ("name", "englishName"):
        nm = info.get(key)
        if isinstance(nm, str) and nm.strip() and nm.strip().upper() != symbol.upper():
            return nm.strip()
    return None


@dataclass
class SymbolNameResolver:
    """`build_resolver()`로 만든다 — 직접 생성자를 호출하지 않는다."""

    cache_dir: Path
    state_dir: Path
    watchlist_paths: tuple[Path, ...] = ()
    toss_lookup: Callable[[str], dict | None] | None = None
    narrator: _Narrator | None = None
    llm_batch_max: int = DEFAULT_LLM_BATCH_MAX

    _names_path: Path = field(init=False, repr=False)
    _meta_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._names_path = self.state_dir / "symbol_names.json"
        self._meta_path = self.state_dir / "symbol_names_meta.json"

    # -- 캐시 I/O -----------------------------------------------------------

    def _cache(self) -> dict[str, str]:
        raw = _read_json_dict(self._names_path)
        return {str(k): str(v) for k, v in raw.items() if k and v}

    def _meta(self) -> dict[str, dict]:
        return _read_json_dict(self._meta_path)

    def _watchlist_names(self) -> dict[str, str]:
        """`data/watchlist.yaml`(`quant.trade.universe.FileWatchlistUniverse`가
        쓰는 파일, YAML)에서 `symbols: [{symbol, name, ...}]`의 이름만 뽑는다.

        `quant.analyze`는 `quant.trade`를 임포트할 수 없어(FORBIDDEN) 여기
        파서를 독립적으로 작게 다시 둔다 — `universe._parse_watchlist_with_names`
        전체(태그·중복 경고 등)를 가져올 필요 없이 이름 필드만 있으면 된다."""
        out: dict[str, str] = {}
        for p in self.watchlist_paths:
            try:
                raw = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(raw, dict):
                continue
            for entry in raw.get("symbols") or []:
                if not isinstance(entry, dict):
                    continue
                sym, nm = entry.get("symbol"), entry.get("name")
                if sym and nm:
                    out.setdefault(str(sym).strip(), str(nm).strip())
        return out

    # -- 해석 -----------------------------------------------------------

    def names_for(self, symbols: Iterable[str], market: str | None = None) -> dict[str, str]:
        """해석 순서: 캐시(비-LLM) → 결정론 사전 → 워치리스트 → Toss → LLM 배치.

        모든 심볼이 캐시(비-LLM)나 이미 알려진 값으로 즉시 풀리면 네트워크도
        LLM 호출도 일어나지 않는다."""
        symbols = list(dict.fromkeys(s for s in symbols if s))
        if not symbols:
            return {}

        cache = self._cache()
        meta = self._meta()
        result: dict[str, str] = {}
        pending: list[str] = []
        llm_fallback: dict[str, str] = {}
        for sym in symbols:
            nm = cache.get(sym)
            src = (meta.get(sym) or {}).get("source")
            # 코드=이름 항목(엔진 부팅 Toss 채움이 ETF 에서 남긴다)은 "모른다"와 같다.
            if nm and nm.upper() != sym.upper() and src != "llm":
                result[sym] = nm
                continue
            pending.append(sym)
            if nm and src == "llm" and nm.upper() != sym.upper():
                llm_fallback[sym] = nm

        if not pending:
            return result

        new_entries: dict[str, tuple[str, str]] = {}

        # (2) 결정론 사전 — 시장별로 묶어 조회
        by_market: dict[str, list[str]] = {}
        for sym in pending:
            by_market.setdefault(market or market_of_symbol(sym), []).append(sym)
        for mkt, syms in by_market.items():
            try:
                table = load_name_map(self.cache_dir, mkt)
            except Exception as e:  # noqa: BLE001 — 이름표 실패가 나머지 해석을 막지 않는다
                logger.warning("결정론 이름표 조회 실패(%s): %s", mkt, e)
                table = {}
            source = "kind" if mkt == "KR" else "sp500"
            for sym in syms:
                nm = _usable(sym, table.get(sym))
                if nm:
                    result[sym] = nm
                    new_entries[sym] = (nm, source)
        pending = [s for s in pending if s not in result]

        # (3) 워치리스트
        if pending:
            wl = self._watchlist_names()
            for sym in pending:
                # 워치리스트는 모르는 종목의 name 에 코드를 그대로 넣는다(watch-add) — 이름이 아니다.
                nm = _usable(sym, wl.get(sym))
                if nm:
                    result[sym] = nm
                    new_entries[sym] = (nm, "watchlist")
            pending = [s for s in pending if s not in result]

        # (4) Toss 조회(네트워크) — 주입 안 됐으면 건너뛴다
        if pending and self.toss_lookup is not None:
            for sym in pending:
                try:
                    info = self.toss_lookup(sym)
                except Exception:  # noqa: BLE001 — 조회 실패는 삼킨다
                    info = None
                nm = _toss_display_name(sym, info)
                if nm:
                    result[sym] = nm
                    new_entries[sym] = (nm, "toss")
            pending = [s for s in pending if s not in result]

        # (5) LLM 배치 — 이전에 이미 LLM으로 확인된 적 있는 심볼은 매번 다시
        # 묻지 않는다(아래 llm_fallback으로 재사용). 진짜 미확인만 배치에 태운다.
        llm_candidates = [s for s in pending if s not in llm_fallback]
        if llm_candidates and self.narrator is not None:
            batch = llm_candidates[: self.llm_batch_max]
            for sym, nm in self._llm_lookup(batch).items():
                result[sym] = nm
                new_entries[sym] = (nm, "llm")
            pending = [s for s in pending if s not in result]

        # 이전 LLM 캐시를 최종 폴백으로 쓴다(위 단계가 더 나은 값을 못 찾았으면).
        for sym in pending:
            if sym in llm_fallback:
                result[sym] = llm_fallback[sym]

        if new_entries:
            self._persist(new_entries, cache, meta)
        return result

    def _llm_lookup(self, symbols: list[str]) -> dict[str, str]:
        prompt = (
            "다음 종목코드/티커의 공식 상장명을 JSON 객체로만 답하라. "
            '형식: {"코드": "이름"}. 한국 6자리 코드는 한글 정식 종목명, '
            "미국 티커는 영문 정식 회사명. 확실하지 않으면 값을 null로 두고 "
            "절대 추측하지 마라.\n종목: " + ", ".join(symbols)
        )
        try:
            raw = self.narrator.narrate(prompt)
        except Exception as e:  # noqa: BLE001
            logger.warning("LLM 종목명 조회 실패: %s", e)
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except (ValueError, json.JSONDecodeError):
            logger.warning("LLM 종목명 응답 파싱 실패")
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, str] = {}
        for sym in symbols:
            nm = _valid_llm_name(sym, data.get(sym))
            if nm:
                out[sym] = nm
        return out

    def _persist(
        self, new_entries: dict[str, tuple[str, str]], cache: dict[str, str], meta: dict[str, dict]
    ) -> None:
        now = datetime.now(UTC).isoformat()
        for sym, (nm, source) in new_entries.items():
            cache[sym] = nm
            meta[sym] = {"source": source, "ts": now}
        _atomic_write_json(self._names_path, cache)
        _atomic_write_json(self._meta_path, meta)


def build_resolver(
    cache_dir: Path,
    state_dir: Path,
    *,
    watchlist_paths: Iterable[Path] = (),
    toss_lookup: Callable[[str], dict | None] | None = None,
    narrator: _Narrator | None = None,
    llm_batch_max: int = DEFAULT_LLM_BATCH_MAX,
) -> SymbolNameResolver:
    """`SymbolNameResolver` 조립 — 실제 어댑터(Toss 클라이언트/narrator)는 호출부가
    `quant.apps`/`quant.report`에서 만들어 콜러블로 넘긴다."""
    return SymbolNameResolver(
        cache_dir=Path(cache_dir),
        state_dir=Path(state_dir),
        watchlist_paths=tuple(Path(p) for p in watchlist_paths),
        toss_lookup=toss_lookup,
        narrator=narrator,
        llm_batch_max=llm_batch_max,
    )
