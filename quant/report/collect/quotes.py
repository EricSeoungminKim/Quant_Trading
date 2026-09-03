"""KR 시세 조회 한 곳 — KIND 부재 폴백 포함 (2026-08-26 실사고 수리).

## 왜 이 모듈이 생겼나

KR 종목 시세는 야후에서 받는데, 야후 심볼은 `종목코드 + 시장접미사`(.KS 유가 /
.KQ 코스닥)다. 그 시장구분의 유일한 출처가 KIND 상장법인목록이었고, KIND 가
EC2 IP를 403으로 막으면서(2026-08-26 실측: User-Agent·Referer 를 붙여도 동일)
`load_market_map` 이 매번 실패했다. 이름 사전은 DART 로 폴백하지만 **DART 에는
시장구분이 없다** — 그래서 KR 리포트가 시세를 하나도 못 받았다(그날 선정 원장
13행 중 close 2건, `baseline_score100` 전무).

파급이 조용해서 더 나빴다: 기준가가 없으면 `forward_returns_bps` 가 전 지평을
None 으로 돌려주고, 그러면 리더보드·AI 트레이더 채점이 통째로 멈춘다. 예외도
경보도 없이 "값이 없는 리포트"가 매일 나올 뻔했다.

## 폴백 방식

시장구분을 모르면 **.KS/.KQ 를 둘 다 후보로 넣어 한 배치로 조회하고, 실제로
값이 온 쪽을 채택한다.** `fetch_symbol_quotes` 는 결측 심볼을 조용히 건너뛰므로
(그 함수 계약) 왕복이 늘지 않고, 틀린 접미사는 그냥 결측으로 떨어진다.

**실측(2026-08-26, EC2에서 야후 직접 호출):**

- 서로 다른 유효 심볼을 한 배치로 받아도 값이 섞이지 않는다 — 배치 조회
  `005930.KS=257,000` / `000660.KS=1,671,000` 이 개별 조회와 정확히 일치했다
  (컬럼 오배치 의심을 배제하려고 확인한 것이다).
- 야후는 **같은 6자리 코드의 두 접미사를 같은 종목으로 해석한다**:
  `000660.KQ` 가 `000660.KS` 와 같은 값(1,671,000)을 돌려줬다. 그래서 둘 다
  값이 와도 어느 쪽을 택하든 같은 종목이고, 아래에서 `.KS` 를 우선하는 것은
  **답을 날마다 흔들지 않기 위한 결정론 규칙**이지 정확성 판단이 아니다.
- 상장폐지·합병 종목(예: 091990)은 두 접미사 모두 결측이다 — 없는 것은 없다.

KIND 가 살아 있으면 종전대로 정확한 접미사 하나만 조회한다(요청 낭비 없음).
"""
from __future__ import annotations

import logging
from pathlib import Path

from quant.analyze.entities import load_market_map
from quant.collect.sources.market import fetch_symbol_quotes

logger = logging.getLogger(__name__)

_SUFFIXES = (".KS", ".KQ")  # 우선순위: 유가 → 코스닥


class _ExpectedDelistFilter(logging.Filter):
    """이번 호출이 이중 조회(`.KS`/`.KQ`)를 위해 만든 **추측 후보 심볼**에 대한
    ERROR 만 DEBUG 로 낮춘다 (D3, 2026-09-03). 실제로 지워지지 않는다 — 레코드는
    그대로 전달하되 레벨만 낮춰서, DEBUG 로 로깅을 켠 사람은 여전히 볼 수 있다.

    **메시지에 "delisted" 가 있다고 다 낮추지 않는다.** KIND 매핑으로 이미 확정된
    심볼(`mapped`)이 정말로 상장폐지됐다면 그건 진짜 장애이고 그대로 ERROR 로
    보여야 한다 — 그래서 이 호출에서 만든 추측 후보 심볼 집합(`probe_symbols`)에
    실제로 등장하는 메시지만 낮춘다.
    """

    def __init__(self, probe_symbols: frozenset[str]) -> None:
        super().__init__()
        self._probe_symbols = probe_symbols

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR and self._probe_symbols:
            msg = record.getMessage()
            if any(sym in msg for sym in self._probe_symbols):
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
        return True


def fetch_kr_quotes(
    codes: list[str], cache_dir: Path, *, map_loader=None, quote_fetcher=None,
) -> tuple[dict[str, dict], str]:
    """KR 6자리 코드 → {코드: 시세}. 반환 `(시세, 경로 설명)`.

    경로 설명은 호출부가 로그에 남겨 "KIND 로 받았는지 폴백이었는지"를 사람이
    알 수 있게 하기 위한 것이다 — 조용한 강등을 만들지 않는다.

    `map_loader`/`quote_fetcher` 는 **호출부가 자기 모듈의 함수를 그대로 넘기라고**
    있는 자리다(기본값은 이 모듈의 것). 리포트 파이프라인은 이 두 경계를 모듈
    속성 교체로 막아 테스트를 네트워크 없이 돌려 왔는데, 조회 로직만 이 모듈로
    옮기면 그 seam 이 조용히 끊긴다 — 옮긴 쪽이 자기 참조를 쓰기 때문이다.
    주입을 받으면 호출부의 seam 이 그대로 살아 있다.
    """
    _load_map = map_loader or load_market_map
    _fetch = quote_fetcher or fetch_symbol_quotes
    codes = [c for c in dict.fromkeys(codes) if c]
    if not codes:
        return {}, "대상 없음"

    mapped: dict[str, str] = {}
    route = "KIND market_map"
    try:
        mmap = _load_map(cache_dir) or {}
        mapped = {c: mmap[c] for c in codes if c in mmap}
    except Exception as e:  # noqa: BLE001 — KIND 실패는 폴백으로 간다
        logger.warning("KIND market_map 실패 — .KS/.KQ 양쪽 조회로 폴백: %s: %s",
                       type(e).__name__, e)
        route = "KIND 실패 → .KS/.KQ 폴백"

    unmapped = [c for c in codes if c not in mapped]
    if unmapped and route == "KIND market_map":
        route = "KIND market_map + 일부 .KS/.KQ 폴백"

    # 후보 야후 심볼 → 원 코드 역인덱스. 같은 코드의 후보가 여러 개면 _SUFFIXES
    # 순서가 곧 우선순위다.
    candidates: list[str] = []
    owner: dict[str, str] = {}
    for code in codes:
        if code in mapped:
            sym = mapped[code]
            candidates.append(sym)
            owner.setdefault(sym, code)
            continue
        for suf in _SUFFIXES:
            sym = f"{code}{suf}"
            candidates.append(sym)
            owner.setdefault(sym, code)

    # D3(2026-09-03): .KS/.KQ 이중 조회는 한쪽이 결측인 게 **정상**이다(§폴백 방식).
    # yfinance 는 그 결측을 자기 로거("yfinance")에 ERROR 로 찍는데, KIND 가 죽어
    # 다수 종목이 이 경로를 타면 매 종목마다 ERROR 가 찍혀 진짜 장애(배치 전체
    # 실패 등)가 로그에 묻힌다. 이번에 만든 추측 후보 심볼(unmapped 의 .KS/.KQ)
    # 에 대해서만 DEBUG 로 낮춘다 — KIND 로 이미 확정된 심볼의 진짜 실패는 그대로
    # ERROR 로 보인다.
    _probe_symbols = frozenset(f"{c}{suf}" for c in unmapped for suf in _SUFFIXES)
    _yf_logger = logging.getLogger("yfinance")
    _filter = _ExpectedDelistFilter(_probe_symbols)
    _yf_logger.addFilter(_filter)
    try:
        raw = _fetch(candidates) or {}
    except Exception as e:  # noqa: BLE001 — 시세 실패가 리포트를 막지 않는다
        logger.warning("KR 시세 조회 실패: %s: %s", type(e).__name__, e)
        return {}, f"{route} · 조회 실패({type(e).__name__})"
    finally:
        _yf_logger.removeFilter(_filter)

    out: dict[str, dict] = {}
    # candidates 순서대로 훑어 먼저 잡힌 후보가 이긴다(.KS 우선 — 결정론).
    for sym in candidates:
        code = owner.get(sym)
        if code is None or code in out:
            continue
        q = raw.get(sym)
        if q:
            out[code] = q
    return out, route
