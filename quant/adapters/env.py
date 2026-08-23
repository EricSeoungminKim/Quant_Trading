"""`.env.local` 로딩. 키 값은 절대 로그에 남기지 않는다."""
from __future__ import annotations

from functools import cache
from pathlib import Path

# **저장소 루트는 여기서만 정의한다.**
# market_report 흡수로 패키지가 한 단 깊어졌는데 `parents[N]` 상수들이 파일마다
# 흩어져 있어 5곳이 동시에 `quant/` 를 루트로 착각했다 — 그리고 틀렸다는 사실이
# "데이터가 없다"로 위장됐다(스코어보드가 빈 원장을 읽어 "종결 트레이드 없음",
# 실제로는 26건). 파일이 옮겨질 때마다 각자 다시 세는 구조가 원인이라, 세는 곳을
# 하나로 줄인다. `tests/test_repo_root.py` 가 이 값이 실제 루트인지 지킨다.
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV = REPO_ROOT / ".env.local"


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or DEFAULT_ENV
    if not path.exists():
        return {}
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


@cache
def _env() -> dict[str, str]:
    return load_env()


def get_key(name: str) -> str | None:
    """키가 없거나 빈 문자열이면 None — 호출부가 '결측'으로 처리하게 한다."""
    return _env().get(name) or None
