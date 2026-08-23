#!/usr/bin/env python3
"""`.env.local` 의 API 키가 실제로 동작하는지 확인한다.

키 값은 **절대 출력하지 않는다** — 도달 여부와 발급처가 돌려준 메타데이터만 보여준다.
표준 라이브러리만 쓰므로 venv 없이도 돈다:

    python3 scripts/check_keys.py
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env.local"
TIMEOUT = 20


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        sys.exit(f"✗ {path} 가 없다. `cp .env.local.example .env.local` 후 값을 채운다.")
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


def get(url: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # 네트워크/DNS/타임아웃
        return 0, f"{type(e).__name__}: {e}"


def check_fred(key: str) -> tuple[bool, str]:
    q = urllib.parse.urlencode({"series_id": "WALCL", "api_key": key, "file_type": "json"})
    status, body = get(f"https://api.stlouisfed.org/fred/series?{q}")
    if status == 200:
        # 발표일정도 같이 확인 — 이벤트 캘린더가 여기 의존한다.
        q2 = urllib.parse.urlencode({"api_key": key, "file_type": "json", "limit": "1"})
        s2, _ = get(f"https://api.stlouisfed.org/fred/releases/dates?{q2}")
        cal = "releases/dates OK" if s2 == 200 else f"releases/dates {s2}"
        return True, f"series OK, {cal}"
    return False, _brief(status, body)


def check_eia(key: str) -> tuple[bool, str]:
    q = urllib.parse.urlencode(
        {"api_key": key, "frequency": "weekly", "data[0]": "value", "length": "1"}
    )
    status, body = get(f"https://api.eia.gov/v2/petroleum/stoc/wstk/data/?{q}")
    return (True, "OK") if status == 200 else (False, _brief(status, body))


def check_dart(key: str) -> tuple[bool, str]:
    """DART OpenAPI 도달 확인. 오늘 하루 창으로 목록을 하나 조회해 status 만 본다
    — 데이터 유무(013)는 정상, 000 도 정상. 그 외 status(020 한도초과 등)는 실패."""
    from datetime import date

    d = date.today().strftime("%Y%m%d")
    q = urllib.parse.urlencode(
        {"crtfc_key": key, "bgn_de": d, "end_de": d, "page_no": "1", "page_count": "1"}
    )
    status, body = get(f"https://opendart.fss.or.kr/api/list.json?{q}")
    if status != 200:
        return False, _brief(status, body)
    try:
        api_status = json.loads(body).get("status")
    except json.JSONDecodeError:
        return False, "OK (응답 파싱 실패)"
    if api_status in ("000", "013"):
        return True, f"OK (status={api_status})"
    return False, f"status={api_status}"


def check_openrouter(key: str) -> tuple[bool, str]:
    """한도 등급까지 조회한다 — ':free' 모델의 req/day 상한을 실측으로 확인하는 자리."""
    status, body = get(
        "https://openrouter.ai/api/v1/auth/key", {"Authorization": f"Bearer {key}"}
    )
    if status != 200:
        return False, _brief(status, body)
    try:
        d = json.loads(body).get("data", {})
    except json.JSONDecodeError:
        return True, "OK (한도 파싱 실패)"
    # rate_limit 필드는 응답 스스로 "deprecated and safe to ignore" 라고 표시하므로 읽지 않는다.
    bits = []
    if (tier := d.get("is_free_tier")) is not None:
        bits.append("등급=유료(1,000 req/day)" if tier is False else "등급=무료(50 req/day)")
    if (used := d.get("usage")) is not None:
        bits.append(f"누적사용=${used}")
    return True, ("OK  " + "  ".join(bits)) if bits else "OK"


CHECKS = [
    ("FRED_API_KEY", "FRED (순유동성·금리·발표일정)", check_fred, True),
    ("EIA_API_KEY", "EIA (원유 재고)", check_eia, False),
    ("OPENROUTER_API_KEY", "OpenRouter (Phase 2)", check_openrouter, False),
    ("DART_API_KEY", "DART (전자공시, H-2)", check_dart, False),
]


def main() -> int:
    env = load_env(ENV_PATH)
    failed_required = False

    print(f"검사 대상: {ENV_PATH}\n")
    for name, label, fn, required in CHECKS:
        value = env.get(name, "")
        tag = "필수" if required else "선택"
        if not value:
            mark = "✗" if required else "·"
            print(f"{mark} {label:<34} [{tag}] 미설정")
            failed_required |= required
            continue
        if fn is None:
            print(f"· {label:<34} [{tag}] 설정됨 (자동 검증 없음)")
            continue
        ok, detail = fn(value)
        print(f"{'✓' if ok else '✗'} {label:<34} [{tag}] {detail}")
        failed_required |= required and not ok

    if failed_required:
        print("\n필수 키가 빠졌거나 거부됐다. 리포트는 생성되지만 해당 섹션은 결측 처리된다.")
        return 1
    print("\n필수 키 확인 완료.")
    return 0


def _brief(status: int, body: str) -> str:
    """오류 본문을 짧게 — 키가 에코될 가능성을 차단하려 120자로 자른다."""
    body = " ".join(body.split())[:120]
    return f"HTTP {status} {body}" if status else body


if __name__ == "__main__":
    sys.exit(main())
