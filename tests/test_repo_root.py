"""저장소 루트 경로 — 2026-08-14 운영 장애 회귀.

## 무슨 일이 있었나

`market_report` 를 흡수하면서 패키지가 한 단 깊어졌는데(`report/` → `quant/...`)
`Path(__file__).parents[N]` 상수들이 그대로 남아 **전부 `quant/` 를 저장소 루트로
착각**했다. 사용자가 `.env.local` 경로를 먼저 고쳤고(FRED/Toss 키가 통째로 결측돼
리포트가 깨졌다), 같은 부류가 5곳 더 남아 있었다.

실측 피해:

- **스코어보드가 빈 원장을 읽었다.** `quant/data/state/trades.jsonl` 은 존재하지
  않으므로 `종결된 트레이드가 아직 없음` 을 출력했다 — 실제 원장에는 종결 26건이
  있었다. 루트 CLAUDE.md 가 "숫자가 자본 배분을 결정한다"고 못 박은 그 숫자다.
- **watch-score 가 국면을 못 봤다.** `regime.json` 은 `data/state/` 에 있는데
  `quant/data/state/` 를 봤으므로 조용히 neutral 로 떨어졌다.
- **토큰 캐시가 갈렸다.** `quant/data/cache/toss_token.json` 이 따로 쌓였다.
  토스는 client_credentials 당 토큰이 **1개**라 캐시가 둘이면 서로를 무효화한다.

## 이 테스트가 막는 것

경로를 **한 곳에서만** 정의하게 만들고, 그 한 곳이 실제 저장소 루트인지 확인한다.
`parents[N]` 을 파일마다 세는 방식은 파일이 옮겨질 때마다 조용히 틀린다 — 그리고
틀렸다는 사실이 "데이터가 없다"로 위장된다.
"""
from __future__ import annotations

from pathlib import Path

from quant.adapters.env import DEFAULT_ENV, REPO_ROOT


def test_repo_root_is_where_pyproject_lives():
    """저장소 루트의 정의 = `pyproject.toml` 이 있는 곳. 다른 판별 기준은 없다."""
    assert (REPO_ROOT / "pyproject.toml").is_file(), f"REPO_ROOT 가 틀렸다: {REPO_ROOT}"


def test_repo_root_is_not_the_package_dir():
    """**이게 실제로 일어난 착오다.** `quant/` 를 루트로 보면 모든 데이터가 사라진다."""
    assert REPO_ROOT.name != "quant"
    assert (REPO_ROOT / "quant").is_dir()


def test_secrets_are_read_from_the_repo_root():
    assert DEFAULT_ENV == REPO_ROOT / ".env.local"


def test_every_data_path_constant_resolves_under_the_repo_root():
    """`data/` 를 가리키는 상수가 **하나도** 패키지 안을 가리키지 않는지.

    5곳이 동시에 틀려 있었고, 각각이 다른 기능을 조용히 망가뜨렸다. 개별 경로를
    외우는 대신 "패키지 안에 data 를 만들지 않는다"는 불변식으로 고정한다.
    """
    from quant.adapters.brokers.kiwoom.client import DEFAULT_CACHE_DIR as KIWOOM_CACHE
    from quant.adapters.brokers.toss.client import DEFAULT_CACHE_DIR as TOSS_CACHE
    from quant.adapters.brokers.toss.datafeed import DEFAULT_CANDLE_CACHE_DIR

    package_dir = REPO_ROOT / "quant"
    for name, path in [
        ("toss.client.DEFAULT_CACHE_DIR", TOSS_CACHE),
        ("kiwoom.client.DEFAULT_CACHE_DIR", KIWOOM_CACHE),
        ("toss.datafeed.DEFAULT_CANDLE_CACHE_DIR", DEFAULT_CANDLE_CACHE_DIR),
    ]:
        assert path.is_relative_to(REPO_ROOT), f"{name} 이 저장소 밖이다: {path}"
        assert not path.is_relative_to(package_dir), (
            f"{name} 이 패키지 안(quant/)을 가리킨다: {path} — "
            "데이터가 조용히 다른 곳에 쌓인다"
        )


def test_cli_state_paths_resolve_under_the_repo_root():
    """스코어보드와 국면이 읽는 경로. 틀리면 **빈 데이터를 정상으로 읽는다.**"""
    from quant.apps.cli import ledger_state_path, regime_state_path

    package_dir = REPO_ROOT / "quant"
    for name, path in [("원장", ledger_state_path()), ("국면", regime_state_path())]:
        assert not Path(path).is_relative_to(package_dir), (
            f"{name} 경로가 패키지 안을 가리킨다: {path}"
        )
        assert Path(path).is_relative_to(REPO_ROOT / "data" / "state")


def test_cli_never_counts_the_repo_root_by_hand():
    """`Path(__file__).parents[1]` 은 `quant/apps/cli.py` 에서 **저장소 루트가 아니라
    `quant/`** 다. 이 착오가 세 번 반복됐고 매번 조용히 빈 데이터를 정상으로 읽었다:

    1. 국면 캐시 → `quant/data/state/regime.json` 을 못 찾아 neutral 로 떨어짐(08-14)
    2. 스코어보드 원장 → "종결된 트레이드 없음"(실제로는 26건)
    3. 세션 손익 → 08-18 KR 에 실제 체결 16건이 있는데 매일 15:35 텔레그램에
       "이 세션에 체결된 거래 없음" 을 보냄. 같은 날 전략별 장부도 "장부 파일 없음"
       으로 찍혔다(08-19 배포 검증에서 발견).

    헬퍼(`ledger_state_path`/`regime_state_path`)만 검사하는 위 테스트로는 인라인
    계산을 못 잡는다. 루트를 세는 곳은 `adapters.env.REPO_ROOT` 하나뿐이어야 한다.
    """
    src = (REPO_ROOT / "quant" / "apps" / "cli.py").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "__file__" in line and "parents[" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "cli.py 가 저장소 루트를 손으로 센다 — REPO_ROOT 를 쓸 것:\n  "
        + "\n  ".join(offenders)
    )
