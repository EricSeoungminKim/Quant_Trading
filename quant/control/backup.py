"""백업 번들 — 만들고, 대조하고, "빈 백업"을 성공으로 읽지 않는다.

## 왜 이게 Phase 5 의 첫 항목인가

**아티팩트가 진실인데 EC2 디스크 한 곳에만 있다.** DB 엔진 선택보다 큰 위험이다.
그리고 누적 뉴스는 되찾을 수 없다 — 실측으로 RSS 를 9시간 뒤 재수집하면 주요 피드
겹침이 **0** 이었다. 지난 기사는 다시 받을 수 없고, 원장은 애초에 재생성이 없다.

## 무엇을 담고 무엇을 버리나

담는다: `data/{state,ledger,news}` + (호출자가 넘기는) MySQL 덤프.
버린다: `data/{cache,history,research}`. 봉 parquet 은 벤더에서 다시 받을 수 있고
로컬만 27MB 다. **재생성 가능한 걸 담으면 정작 못 되찾는 것이 전송 실패에 묻힌다.**

MySQL 은 아티팩트에서 재적재하면 복구되지만(`control/warehouse.py`) 덤프를 함께
담는다 — 재적재는 몇 시간이고, 장애 중에 몇 시간은 길다.

## 이 모듈이 막는 실패

`exit 0` 을 내면서 빈 tarball 을 남기는 것. 그러면 "백업이 돌고 있다"는 착각이
며칠 이어지고, 복원할 때가 되어서야 알게 된다 — 이 저장소가 반복해서 다친 모양
**"모른다"를 "이상 없음"으로 읽는 것**의 백업 버전이다. 그래서:

- 지킬 게 하나도 없으면 `EmptyBackup` 으로 **실패**한다.
- 번들 안에 매니페스트를 넣고, 만든 직후 **풀어서 대조**한다.
- jsonl 은 줄 수까지 센다. 해시만 보면 "바뀜"까지만 알고 **"잘림"** 을 모른다.
- 지난 번들보다 줄어들면 `regressions()` 가 잡는다 — 원장은 append-only 다.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

# 지켜야 할 아티팩트. `data/` 아래 이 디렉토리들만 담는다.
ARTIFACTS = ("state", "ledger", "news")

MANIFEST_NAME = "MANIFEST.json"

# 번들은 오프사이트로 나간다 — 시크릿이 타면 백업이 유출 경로가 된다.
# 값이 아니라 **이름**으로 막는다: 내용 검사는 놓칠 수 있고, 애초에 이 경로로
# 시크릿을 보낼 이유가 없다.
_SECRET_NAMES = (".env", ".pem", ".key", "id_rsa", "id_ed25519", "credentials")


class EmptyBackup(RuntimeError):
    """담을 게 없다. **성공이 아니다.**"""


class SecretInBundle(RuntimeError):
    """시크릿으로 보이는 파일을 번들에 넣으려 했다."""


class BundleExists(RuntimeError):
    """그 경로에 이미 번들이 있다. **덮어쓰지 않는다** — 백업이 백업을 지운다."""


@dataclass(frozen=True)
class Entry:
    bytes: int
    sha256: str
    lines: int | None  # jsonl 만. None = "줄 수가 의미 없는 파일"이고 0 과 다르다.


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return sum(1 for line in f if line.strip())


def _entry(path: Path) -> Entry:
    return Entry(
        bytes=path.stat().st_size,
        sha256=_sha256(path),
        lines=_count_lines(path) if path.suffix == ".jsonl" else None,
    )


def _looks_secret(path: Path) -> bool:
    name = path.name.lower()
    return any(marker in name for marker in _SECRET_NAMES)


def manifest(root: Path | str) -> dict[str, Entry]:
    """`root/data/{state,ledger,news}` 의 모든 파일. 키는 `data/` 기준 상대경로."""
    data = Path(root) / "data"
    out: dict[str, Entry] = {}
    for artifact in ARTIFACTS:
        base = data / artifact
        if not base.is_dir():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            out[str(path.relative_to(data))] = _entry(path)
    return out


def _manifest_bytes(entries: dict[str, Entry]) -> bytes:
    body = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": {k: asdict(v) for k, v in sorted(entries.items())},
    }
    return json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def create(root: Path | str, out: Path | str, extra: Sequence[Path] = ()) -> dict:
    """번들을 만들고 **곧바로 대조한다.** 대조가 실패하면 예외로 알린다.

    `extra` 는 `data/` 밖에서 만들어진 파일(MySQL 덤프) — 번들 안에서 `mysql/` 아래
    놓인다. 담기 전에 이름으로 시크릿을 걸러낸다.
    """
    root, out = Path(root), Path(out)
    if out.exists():
        # 이름이 겹쳤다는 것 자체가 신호다(같은 초에 두 번 돌았다). 조용히 접미사를
        # 붙이면 왜 두 번 돌았는지 아무도 안 보게 된다.
        raise BundleExists(f"이미 있는 번들을 덮어쓰지 않는다: {out}")
    entries = manifest(root)

    members: list[tuple[str, Path]] = [(k, root / "data" / k) for k in entries]
    for path in extra:
        path = Path(path)
        if _looks_secret(path):
            raise SecretInBundle(
                f"시크릿으로 보이는 파일을 번들에 넣을 수 없다: {path.name} — "
                "번들은 오프사이트로 나간다"
            )
        key = f"mysql/{path.name}"
        entries[key] = _entry(path)
        members.append((key, path))

    if not entries:
        # 파일을 만들기 **전에** 실패한다 — 반쯤 만든 번들이 최신 백업으로 보인다.
        raise EmptyBackup(
            f"백업할 아티팩트가 없다: {root / 'data'} 아래 {list(ARTIFACTS)} 가 모두 비었다"
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for key, path in members:
            tar.add(path, arcname=key)
        blob = _manifest_bytes(entries)
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(blob)
        tar.addfile(info, io.BytesIO(blob))

    problems = verify(out)
    if problems:
        raise RuntimeError("번들을 만든 직후 대조가 실패했다:\n  " + "\n  ".join(problems))

    return {
        "bundle": str(out),
        "files": len(entries),
        "bytes": out.stat().st_size,
        "jsonl_lines": sum(e.lines or 0 for e in entries.values()),
    }


def read_manifest(bundle: Path | str) -> dict[str, Entry]:
    with tarfile.open(bundle, "r:gz") as tar:
        member = tar.extractfile(MANIFEST_NAME)
        if member is None:
            raise RuntimeError(f"번들에 {MANIFEST_NAME} 이 없다")
        body = json.loads(member.read().decode("utf-8"))
    return {k: Entry(**v) for k, v in body["entries"].items()}


def verify(bundle: Path | str) -> list[str]:
    """번들을 풀어 매니페스트와 대조한다. 문제 목록(비면 정상).

    **예외를 던지지 않는다.** 크론에서 스택트레이스로 죽으면 알림이 안 간다.
    """
    try:
        expected = read_manifest(bundle)
    except Exception as e:  # noqa: BLE001 — gzip 파손·매니페스트 부재 모두 "문제 있음"
        return [f"매니페스트를 읽을 수 없다: {type(e).__name__}: {e}"]

    problems: list[str] = []
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            present = {m.name for m in tar.getmembers()}
            for key, want in sorted(expected.items()):
                if key not in present:
                    problems.append(f"{key}: 번들에 없다")
                    continue
                f = tar.extractfile(key)
                payload = f.read() if f else b""
                if len(payload) != want.bytes:
                    problems.append(
                        f"{key}: 크기 불일치 (매니페스트 {want.bytes} / 실제 {len(payload)})"
                    )
                got_sha = hashlib.sha256(payload).hexdigest()
                if got_sha != want.sha256:
                    problems.append(f"{key}: 해시 불일치")
                if want.lines is not None:
                    got_lines = sum(
                        1 for line in payload.decode("utf-8", "replace").splitlines()
                        if line.strip()
                    )
                    if got_lines != want.lines:
                        problems.append(
                            f"{key}: 줄 수 불일치 (매니페스트 {want.lines} / 실제 {got_lines})"
                        )
    except Exception as e:  # noqa: BLE001
        return [f"번들을 열 수 없다: {type(e).__name__}: {e}"]
    return problems


def regressions(cur: dict[str, Entry], prev: dict[str, Entry]) -> list[str]:
    """지난 번들 대비 **줄어든** 것. 원장·뉴스는 append-only 이므로 줄어들면 사고다.

    이 검사가 없으면 망가진 소스를 그대로 백업해 지난 백업까지 덮어쓴다.
    """
    problems: list[str] = []
    for key, was in sorted(prev.items()):
        if key.startswith("mysql/"):
            # **덤프는 매번 파일명이 바뀐다**(타임스탬프). 그걸 "사라졌다"로 읽으면
            # 모든 백업이 거짓 경보를 낸다 — 2026-08-14 실측: 배포 후 매 회차 exit 1.
            # 그리고 이 검사의 목적은 append-only 아티팩트가 줄어드는 것을 잡는
            # 것인데, 덤프는 append-only 도 아니고 아티팩트에서 재적재로 복구된다.
            continue
        now = cur.get(key)
        if now is None:
            problems.append(f"{key}: 지난 백업에 있었는데 사라졌다")
            continue
        if was.lines is not None and now.lines is not None and now.lines < was.lines:
            problems.append(f"{key}: 줄이 줄었다 ({was.lines} → {now.lines})")
    return problems
