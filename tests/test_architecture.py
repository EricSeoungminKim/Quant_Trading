"""평면 경계를 임포트 그래프로 강제한다.

**왜 테스트인가.** 디렉토리 구조는 규칙을 *제안*할 뿐 강제하지 못한다. 두 저장소가
갈려 있던 시절에도 규칙은 문서에 있었지만, 실제로는 `trade/strategy/__init__.py`가
`apps.config`를 임포트하고 있었다 — `trade/strategy/CLAUDE.md`가 명시적으로 금지한
바로 그 의존이다. 아무도 몰랐던 이유는 아무것도 확인하지 않았기 때문이다.

**가장 중요한 규칙은 `collect`/`analyze` → `trade` 금지다.** 스크래핑한 뉴스가
주문으로 이어지는 경로를 코드 수준에서 끊는다. 메시지 큐로는 못 하는 일이다 —
큐는 누가 무엇을 발행하든 받아준다.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "quant"

# 평면 간 금지된 의존. (출발, 도착) — 도착을 임포트하면 안 된다.
FORBIDDEN = {
    # core 는 아무것도 의존하지 않는다. 여기가 뚫리면 도메인이 오염된다.
    ("quant.core", "quant.adapters"),
    ("quant.core", "quant.apps"),
    ("quant.core", "quant.trade"),
    ("quant.core", "quant.collect"),
    ("quant.core", "quant.analyze"),
    ("quant.core", "quant.control"),
    # 거래 평면은 장중에 스크래핑하거나 리포트를 만들지 않는다.
    ("quant.trade", "quant.collect"),
    ("quant.trade", "quant.analyze"),
    ("quant.trade", "quant.control"),
    ("quant.trade", "quant.adapters"),
    ("quant.trade", "quant.apps"),
    # ★ 뉴스가 주문을 내지 못하게 한다.
    ("quant.collect", "quant.trade"),
    ("quant.analyze", "quant.trade"),
    # 자동 튜닝이 돌아가는 엔진을 직접 건드리지 못하게 한다 — settings.yaml 을 쓰고
    # 엔진이 다음 리로드에 읽는다.
    ("quant.control", "quant.trade"),
    # 어댑터는 코어의 Protocol 을 구현할 뿐, 거래 로직을 알지 않는다.
    ("quant.adapters", "quant.trade"),
    # 수집은 분석·설정을 몰라야 한다 (아티팩트를 쓰고 끝낸다).
    ("quant.collect", "quant.analyze"),
    ("quant.collect", "quant.apps"),
    ("quant.analyze", "quant.adapters"),
}

# 아직 남은 위반. **줄어들기만 해야 한다.**
#
# 2026-08-13 재배치 시점 19건 → 5건 → 2026-08-17 3건(telegram_approval/entities.py/
# calendar.py 상환 — ApprovalRequest를 core로, KIND/SP500/SymDir 다운로드를
# collect/listed_companies.py로, terms.py를 core로 옮겼다). 나머지는 단순
# 배치 문제가 아니라 실제 결합이라 별도 리팩터링이 필요하다. 목록이 조용히
# 늘지 않도록 아래 stale 테스트가 지킨다.
KNOWN_DEBT: set[tuple[str, str]] = set()
# 2026-08-24 부채 완납 — 이 집합은 이제 비어 있고, 계속 비어 있어야 한다.
#   (지불 기록)
#   quant/collect/sources/__init__.py → quant.analyze:
#     symbol→회사명 resolver 를 분석 평면(entities.make_symbol_resolver)이 만들어
#     호출부(report/apps)가 주입한다 — 수집은 팩토리를 받아 쓸 뿐 종목 사전을
#     모른다. build_sources 의 죽은 cache_dir 파라미터도 함께 제거.
#   quant/trade/loop.py → quant.apps.config:
#     루프가 Settings 에서 실제로 쓰던 것은 raw·poll_seconds·reload_if_changed
#     셋뿐 — trade/loop.py 의 EngineSettings Protocol 로 구조적 계약만 남기고
#     임포트를 끊었다. apps.config.Settings 는 코드 변경 없이 이를 만족한다.

# 평면별 금지 외부 라이브러리.
FORBIDDEN_EXTERNAL = {
    # 도메인은 stdlib + pandas 타입힌트만 (quant/core/CLAUDE.md).
    "quant.core": {"httpx", "requests", "websockets", "yaml", "dotenv", "jinja2",
                   "yfinance", "lxml", "pymysql", "MySQLdb", "sqlalchemy"},
    # 거래 평면은 네트워크도 DB 도 직접 만지지 않는다. 09:15 에 MySQL 이 딸꾹질했다고
    # 매매가 멈추면 안 된다.
    #
    # **redis 는 특히 그렇다.** 휘발성이라 재시작을 견디지 않는데, 포지션·주문
    # 상태·control 플래그는 견뎌야 한다. 캐시에 담아도 되는 건 다시 계산할 수
    # 있는 것뿐이다(`core.ports.KeyValue` docstring).
    "quant.trade": {"httpx", "requests", "websockets", "lxml", "jinja2",
                    "pymysql", "MySQLdb", "sqlalchemy", "aiomysql",
                    "redis", "valkey", "duckdb"},
    # 코어도 마찬가지 — Protocol 만 두고 구현은 어댑터가 갖는다.
    "quant.core": {"redis", "duckdb"},
    # 전략은 파라미터를 생성자 인자로만 받는다.
    "quant.trade.strategy": {"yaml", "dotenv"},
}


def _plane(module: str, depth: int = 2) -> str:
    parts = module.split(".")
    return ".".join(parts[:depth])


def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def _source_files() -> list[pathlib.Path]:
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in str(p))


def _edges() -> list[tuple[str, str, str]]:
    """(파일 상대경로, 출발 평면, 임포트한 모듈)."""
    out = []
    for f in _source_files():
        rel = str(f.relative_to(ROOT))
        own = _plane(str(f.relative_to(ROOT).with_suffix("")).replace("/", "."))
        for mod in _imports(f):
            out.append((rel, own, mod))
    return out


def test_no_forbidden_cross_plane_imports():
    """평면 경계를 넘는 임포트는 KNOWN_DEBT 에 등재된 것만 허용한다."""
    violations = [
        f"{rel} → {mod}"
        for rel, own, mod in _edges()
        if mod.startswith("quant.")
        and (own, _plane(mod)) in FORBIDDEN
        and (rel, mod) not in KNOWN_DEBT
    ]
    assert not violations, (
        "새 평면 위반이 생겼다. 의존 방향을 고치거나, 정말 불가피하면 이유를 적어\n"
        "KNOWN_DEBT 에 등재해라 (등재는 부채를 인정하는 것이지 면제가 아니다):\n  "
        + "\n  ".join(violations)
    )


def test_known_debt_has_no_stale_entries():
    """부채를 갚으면 목록에서도 지운다 — 안 그러면 목록이 거짓말을 시작한다."""
    live = {(rel, mod) for rel, _, mod in _edges()}
    stale = KNOWN_DEBT - live
    assert not stale, (
        "KNOWN_DEBT 에 이미 해소된 항목이 남아 있다. 지워라:\n  "
        + "\n  ".join(f"{r} → {m}" for r, m in sorted(stale))
    )


def test_known_debt_only_shrinks():
    """2026-08-13 재배치 직후 5건. 늘어나면 실패한다."""
    assert len(KNOWN_DEBT) <= 5, (
        f"부채가 {len(KNOWN_DEBT)}건으로 늘었다. 새 위반은 KNOWN_DEBT 에 넣는 게 아니라 고친다."
    )


@pytest.mark.parametrize("plane_prefix", sorted(FORBIDDEN_EXTERNAL))
def test_plane_does_not_import_forbidden_libraries(plane_prefix: str):
    """네트워크·DB·템플릿 라이브러리가 순수 평면에 새어들지 않는지."""
    depth = len(plane_prefix.split("."))
    banned = FORBIDDEN_EXTERNAL[plane_prefix]
    violations = [
        f"{rel} → {mod}"
        for rel, _, mod in _edges()
        if _plane(str(pathlib.Path(rel).with_suffix("")).replace("/", "."), depth) == plane_prefix
        and mod.split(".")[0] in banned
    ]
    assert not violations, (
        f"{plane_prefix} 에 금지된 외부 의존이 들어왔다:\n  " + "\n  ".join(violations)
    )


def test_core_imports_nothing_from_quant_outside_core():
    """core 는 quant 안에서 자기 자신만 안다 — 의존 방향의 바닥."""
    violations = [
        f"{rel} → {mod}"
        for rel, own, mod in _edges()
        if own == "quant.core" and mod.startswith("quant.") and not mod.startswith("quant.core")
    ]
    assert not violations, "core 가 바깥을 임포트한다:\n  " + "\n  ".join(violations)


def test_every_plane_directory_exists():
    """레이아웃이 조용히 무너지지 않게 — 평면이 사라지면 위 규칙들이 공허해진다."""
    missing = [p for p in ("core", "collect", "analyze", "trade", "control", "adapters", "apps")
               if not (PKG / p).is_dir()]
    assert not missing, f"평면 디렉토리가 없다: {missing}"
