"""백업 번들 — 만들고, 대조하고, "빈 백업"을 성공으로 읽지 않는다.

이 저장소에서 반복해서 나온 결함 모양이 여기서도 가장 위험하다:
**"모른다"를 "이상 없음"으로 읽는 것.** 백업이 exit 0 을 내면서 빈 tarball 을
남기면, 며칠 뒤 복원할 때까지 아무도 모른다. 그래서 다음 두 개가 테스트의 핵심이다.

- 지킬 게 하나도 없으면 **실패**한다 (성공이 아니다).
- jsonl 이 **줄어들면** 잡는다 — 원장은 append-only 이므로 줄어드는 건 사고다.
  해시만 보면 "내용이 바뀜"까지만 알고 "잘렸음"을 모른다.
"""
from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import pytest

from quant.control.backup import (
    ARTIFACTS,
    MANIFEST_NAME,
    BundleExists,
    EmptyBackup,
    SecretInBundle,
    create,
    manifest,
    read_manifest,
    regressions,
    verify,
)


def _seed(root: Path) -> None:
    """지켜야 할 아티팩트 최소 구성 — 실제 레이아웃과 같은 모양."""
    (root / "data" / "state").mkdir(parents=True)
    (root / "data" / "ledger").mkdir(parents=True)
    (root / "data" / "news" / "KR").mkdir(parents=True)
    (root / "data" / "state" / "portfolio.json").write_text('{"cash": 1000}', encoding="utf-8")
    (root / "data" / "state" / "trades.jsonl").write_text(
        '{"symbol": "TQQQ"}\n{"symbol": "SQQQ"}\n', encoding="utf-8"
    )
    (root / "data" / "ledger" / "selections-2026-08.jsonl").write_text(
        '{"symbol": "069500"}\n', encoding="utf-8"
    )
    (root / "data" / "news" / "KR" / "2026-08-13.jsonl").write_text(
        '{"title": "기사"}\n', encoding="utf-8"
    )


# ── manifest ──────────────────────────────────────────────────────────────

def test_manifest_counts_lines_of_jsonl_not_only_hash(tmp_path: Path):
    """jsonl 은 줄 수까지 센다 — 해시만으로는 '잘렸다'와 '바뀌었다'를 구분 못 한다."""
    _seed(tmp_path)
    man = manifest(tmp_path)

    assert man["state/trades.jsonl"].lines == 2
    assert man["state/trades.jsonl"].bytes > 0
    assert len(man["state/trades.jsonl"].sha256) == 64
    # jsonl 이 아닌 파일은 줄 수가 의미 없다 — 0 이 아니라 None 이어야 한다.
    assert man["state/portfolio.json"].lines is None


def test_manifest_covers_all_artifact_dirs(tmp_path: Path):
    """state·ledger·news 가 전부 들어간다. 하나라도 빠지면 조용히 못 지킨다."""
    _seed(tmp_path)
    man = manifest(tmp_path)

    covered = {key.split("/")[0] for key in man}
    assert covered == set(ARTIFACTS)


def test_manifest_excludes_regenerable_data(tmp_path: Path):
    """cache·history·research 는 담지 않는다.

    parquet 봉은 벤더에서 다시 받을 수 있고 로컬만 27MB 다. 재생성 가능한 걸 담으면
    **정작 못 되찾는 것**(누적 뉴스·원장)이 전송 실패에 묻힌다.
    """
    _seed(tmp_path)
    for disposable in ("cache", "history", "research"):
        d = tmp_path / "data" / disposable
        d.mkdir(parents=True)
        (d / "big.parquet").write_bytes(b"x" * 1024)

    man = manifest(tmp_path)

    assert not [k for k in man if k.split("/")[0] not in ARTIFACTS]


# ── create / verify ───────────────────────────────────────────────────────

def test_created_bundle_verifies(tmp_path: Path):
    _seed(tmp_path)
    out = tmp_path / "bundle.tar.gz"

    stats = create(tmp_path, out)

    assert out.exists()
    assert stats["files"] == 4
    assert verify(out) == []


def test_create_refuses_to_overwrite_an_existing_bundle(tmp_path: Path):
    """기존 번들을 덮어쓰지 않는다.

    실측(2026-08-13 리허설): 파일명이 초 단위라 같은 초에 두 번 돌면 이름이 겹치고,
    **백업이 백업을 지웠다**. 보관 개수가 유한하므로 조용한 덮어쓰기는 곧 손실이다.
    이름이 겹쳤다는 것 자체가 신호이므로 조용히 접미사를 붙이지 않고 실패한다.
    """
    _seed(tmp_path)
    out = tmp_path / "bundle.tar.gz"
    create(tmp_path, out)
    before = out.read_bytes()

    with pytest.raises(BundleExists):
        create(tmp_path, out)

    assert out.read_bytes() == before  # 손대지 않았다


def test_verify_catches_truncated_member(tmp_path: Path):
    """전송 중 잘린 번들을 잡는다 — 이게 오프사이트 백업의 실제 실패 모드다."""
    _seed(tmp_path)
    out = tmp_path / "bundle.tar.gz"
    create(tmp_path, out)

    tampered = tmp_path / "tampered.tar.gz"
    _rewrite(out, tampered, "state/trades.jsonl", b'{"symbol": "TQQQ"}\n')

    problems = verify(tampered)
    assert any("state/trades.jsonl" in p for p in problems)
    assert any("줄" in p for p in problems)  # 줄 수 불일치를 이름으로 말한다


def test_verify_catches_missing_member(tmp_path: Path):
    _seed(tmp_path)
    out = tmp_path / "bundle.tar.gz"
    create(tmp_path, out)

    tampered = tmp_path / "tampered.tar.gz"
    _rewrite(out, tampered, "state/trades.jsonl", None)

    assert any("state/trades.jsonl" in p for p in verify(tampered))


def test_verify_reports_problem_for_corrupt_archive(tmp_path: Path):
    """gzip 자체가 깨져도 예외로 죽지 않고 '문제 있음'으로 답한다 —
    크론이 스택트레이스로 죽으면 알림이 안 간다."""
    broken = tmp_path / "broken.tar.gz"
    broken.write_bytes(b"not a tarball")

    assert verify(broken) != []


# ── 빈 백업을 성공으로 읽지 않는다 ────────────────────────────────────────

def test_create_refuses_when_nothing_to_back_up(tmp_path: Path):
    """지킬 게 없으면 실패다.

    빈 tarball 에 exit 0 을 주면 '백업이 돌고 있다'는 착각이 며칠 이어진다.
    이 저장소는 이미 같은 모양으로 다친 적이 있다(캐시 실패 시 0 을 돌려줘
    '중복이니 건너뜀'으로 읽힌 사건).
    """
    (tmp_path / "data" / "state").mkdir(parents=True)

    with pytest.raises(EmptyBackup):
        create(tmp_path, tmp_path / "bundle.tar.gz")


def test_create_leaves_no_bundle_behind_when_it_refuses(tmp_path: Path):
    """실패했으면 파일도 남기지 않는다 — 반쯤 만든 번들이 최신 백업으로 보인다."""
    (tmp_path / "data" / "state").mkdir(parents=True)
    out = tmp_path / "bundle.tar.gz"

    with pytest.raises(EmptyBackup):
        create(tmp_path, out)

    assert not out.exists()


# ── 추가 파일(MySQL 덤프) ─────────────────────────────────────────────────

def test_extra_files_are_bundled_and_verified(tmp_path: Path):
    """MySQL 덤프는 data/ 밖에서 만들어져 들어온다."""
    _seed(tmp_path)
    dump = tmp_path / "quant-20260813.sql.gz"
    dump.write_bytes(gzip.compress(b"-- dump\n"))
    out = tmp_path / "bundle.tar.gz"

    create(tmp_path, out, extra=[dump])

    man = read_manifest(out)
    assert "mysql/quant-20260813.sql.gz" in man
    assert verify(out) == []


def test_refuses_to_bundle_secrets(tmp_path: Path):
    """번들은 오프사이트로 나간다 — 시크릿이 타면 백업이 유출 경로가 된다."""
    _seed(tmp_path)
    secret = tmp_path / ".env.local"
    secret.write_text("TELEGRAM_BOT_TOKEN=abc\n", encoding="utf-8")

    with pytest.raises(SecretInBundle):
        create(tmp_path, tmp_path / "bundle.tar.gz", extra=[secret])


# ── 회귀(줄어듦) 감지 ─────────────────────────────────────────────────────

def test_regressions_flags_shrunken_ledger(tmp_path: Path):
    """원장은 append-only 다. 줄어들었으면 소스가 망가진 것이고, 그걸 덮어쓰면
    지난 백업까지 잃는다."""
    _seed(tmp_path)
    prev = manifest(tmp_path)
    (tmp_path / "data" / "state" / "trades.jsonl").write_text(
        '{"symbol": "TQQQ"}\n', encoding="utf-8"
    )
    cur = manifest(tmp_path)

    problems = regressions(cur, prev)
    assert any("state/trades.jsonl" in p for p in problems)


def test_regressions_silent_when_ledger_grows(tmp_path: Path):
    _seed(tmp_path)
    prev = manifest(tmp_path)
    with (tmp_path / "data" / "state" / "trades.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"symbol": "QQQ"}\n')

    assert regressions(manifest(tmp_path), prev) == []


def test_regressions_flags_disappeared_file(tmp_path: Path):
    """있던 파일이 사라진 것도 회귀다 — 뉴스 하루치가 통째로 없어질 수 있다."""
    _seed(tmp_path)
    prev = manifest(tmp_path)
    (tmp_path / "data" / "news" / "KR" / "2026-08-13.jsonl").unlink()

    assert any("news/KR/2026-08-13.jsonl" in p for p in regressions(manifest(tmp_path), prev))


# ── 도우미 ────────────────────────────────────────────────────────────────

def _rewrite(src: Path, dst: Path, member: str, body: bytes | None) -> None:
    """번들 안 한 멤버만 바꿔치기(또는 제거)한다. MANIFEST 는 그대로 둔다."""
    with tarfile.open(src, "r:gz") as tin, tarfile.open(dst, "w:gz") as tout:
        for info in tin.getmembers():
            data = tin.extractfile(info)
            payload = data.read() if data else b""
            if info.name == member:
                if body is None:
                    continue
                payload = body
                info.size = len(payload)
            elif info.name == MANIFEST_NAME:
                json.loads(payload)  # 매니페스트는 손대지 않는다는 확인
            import io

            tout.addfile(info, io.BytesIO(payload))


def test_mysql_dump_is_excluded_from_regression_checks(tmp_path: Path):
    """**MySQL 덤프는 매번 파일명이 바뀐다** — 그걸 "사라졌다"로 읽으면 안 된다.

    2026-08-14 실측: 배포 후 모든 백업이 exit 1 + 거짓 경보를 냈다. 덤프 이름에
    타임스탬프가 들어가서 회귀 검사가 매번 "지난 백업에 있었는데 사라졌다"를 냈다.
    **거짓 경보가 오는 감시는 꺼진다** — 그러면 진짜 회귀도 못 잡는다.

    회귀 검사의 목적은 **append-only 아티팩트가 줄어드는 것**을 잡는 것이다. 덤프는
    append-only 도 아니고 아티팩트에서 재적재로 복구된다 — 검사 대상이 아니다.
    """
    _seed(tmp_path)
    old_dump = tmp_path / "mysql-20260813-203629.sql.gz"
    old_dump.write_bytes(gzip.compress(b"-- old\n"))
    prev = manifest(tmp_path)
    prev["mysql/mysql-20260813-203629.sql.gz"] = _entry_of(old_dump)

    new_dump = tmp_path / "mysql-20260814-033001.sql.gz"
    new_dump.write_bytes(gzip.compress(b"-- new but bigger\n"))
    cur = manifest(tmp_path)
    cur["mysql/mysql-20260814-033001.sql.gz"] = _entry_of(new_dump)

    assert regressions(cur, prev) == []


def test_shrinking_artifact_is_still_caught_alongside_a_dump(tmp_path: Path):
    """덤프를 제외해도 **진짜 회귀는 그대로 잡아야 한다** — 필터가 감지를 삼키면 안 된다."""
    _seed(tmp_path)
    prev = manifest(tmp_path)
    prev["mysql/mysql-20260813-203629.sql.gz"] = prev["state/trades.jsonl"]
    (tmp_path / "data" / "state" / "trades.jsonl").write_text(
        '{"symbol": "TQQQ"}\n', encoding="utf-8")

    problems = regressions(manifest(tmp_path), prev)

    assert any("state/trades.jsonl" in p for p in problems)


def _entry_of(path: Path):
    from quant.control.backup import _entry

    return _entry(path)
