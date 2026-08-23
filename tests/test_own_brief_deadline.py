"""own_brief.sh 의 편입 데드라인 비교 — 실제 셸 산술을 그대로 돌려 검증한다.

**왜 셸을 직접 부르나.** 이 버그는 파이썬으로 옮겨 쓰면 재현되지 않는다. 원인이
`date +%H%M`이 오전에 "0841"처럼 **0으로 채운 4자리**를 낸다는 셸 특유의 사실이기
때문이다. `"1$(date +%H%M)"` = 10841(5자리)인데 데드라인을 855로 쓰면 1855(4자리)가
돼 `10841 -ge 1855`가 참이 된다 — **오전 내내 "데드라인 초과"로 오판**한다.

2026-08-13 08:40, 새 파이프라인의 첫 실전 실행이 정확히 이 버그로 후보 11개를 다
뽑아놓고 편입을 통째로 건너뛰었다. 로그에는 "데드라인 초과 — 편입 생략"만 남아
정상 동작처럼 보였다. 매일 아침 재발했을 결함이다.
"""
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "server" / "scripts" / "own_brief.sh"


def _deadlines() -> dict[str, str]:
    """스크립트에서 시장별 DEADLINE 리터럴을 그대로 읽는다."""
    text = SCRIPT.read_text(encoding="utf-8")
    return dict(re.findall(r"(KR|US)\)\s*DEADLINE=(\d+);", text))


def _exceeded(hhmm: str, deadline: str) -> bool:
    """스크립트와 **문자 그대로 같은** 비교식을 셸에서 실행한다."""
    rc = subprocess.run(
        ["bash", "-c", f'[ "1{hhmm}" -ge "1{deadline}" ]'], check=False
    ).returncode
    return rc == 0


def test_deadlines_are_zero_padded_four_digits():
    """자릿수가 시각 문자열과 어긋나면 비교가 조용히 깨진다."""
    for market, value in _deadlines().items():
        assert len(value) == 4, f"{market} DEADLINE={value} — 0으로 채운 4자리여야 한다"


@pytest.mark.parametrize("hhmm", ["0000", "0700", "0800", "0812", "0824"])
def test_kr_before_deadline_proceeds(hhmm):
    """08:25 이전에는 편입이 진행돼야 한다 — 08:12 크론이 여기 걸린다(2026-08-17 전진)."""
    assert not _exceeded(hhmm, _deadlines()["KR"])


@pytest.mark.parametrize("hhmm", ["0825", "0900", "1530", "2359"])
def test_kr_at_or_after_deadline_skips(hhmm):
    """08:27 유니버스 롤에 못 태우면 등록해봐야 그 세션엔 안 잡힌다."""
    assert _exceeded(hhmm, _deadlines()["KR"])


@pytest.mark.parametrize("hhmm", ["0840", "2140", "2150", "2204"])
def test_us_before_deadline_proceeds(hhmm):
    assert not _exceeded(hhmm, _deadlines()["US"])


@pytest.mark.parametrize("hhmm", ["2205", "2210", "2330", "2359"])
def test_us_at_or_after_deadline_skips(hhmm):
    assert _exceeded(hhmm, _deadlines()["US"])


def test_deadline_precedes_the_universe_roll_it_protects():
    """데드라인은 그 시장의 유니버스 롤(_universe_roll_bucket)보다 앞서야 의미가 있다."""
    from quant.trade.loop import _universe_roll_bucket
    from datetime import datetime
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    dl = _deadlines()
    # KR 데드라인(08:25) 시점은 아직 08:27 롤 이전 버킷이어야 한다
    at_kr = datetime(2026, 8, 13, int(dl["KR"][:2]), int(dl["KR"][2:]), tzinfo=kst)
    assert not _universe_roll_bucket(at_kr).endswith("+0827")
    # US 데드라인(22:05) 시점은 아직 22:10 롤 이전이어야 한다
    at_us = datetime(2026, 8, 13, int(dl["US"][:2]), int(dl["US"][2:]), tzinfo=kst)
    assert not _universe_roll_bucket(at_us).endswith("+2210")


# ========== KR close 세션 (13:55, 서브프로젝트 T Task 2)


def _close_deadline() -> str:
    """스크립트에서 close 세션 DEADLINE 리터럴을 그대로 읽는다 (KR/US 케이스와
    달리 `if [ "$SESSION" = "close" ]` 블록 안 단순 대입이라 별도로 파싱한다)."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r'SESSION"\s*=\s*"close"\s*\]\s*;\s*then\s*\n\s*DEADLINE=(\d+);', text)
    assert m, "close 세션 DEADLINE 리터럴을 스크립트에서 찾지 못했다"
    return m.group(1)


def test_close_deadline_is_zero_padded_four_digits():
    assert len(_close_deadline()) == 4


@pytest.mark.parametrize("hhmm", ["0000", "1200", "1357"])
def test_kr_close_before_deadline_proceeds(hhmm):
    """13:58 데드라인 전에는 오후 편입이 진행돼야 한다 — 13:55 크론이 여기 걸린다."""
    assert not _exceeded(hhmm, _close_deadline())


@pytest.mark.parametrize("hhmm", ["1358", "1400", "1530", "2359"])
def test_kr_close_at_or_after_deadline_skips(hhmm):
    """14:00 유니버스 롤에 못 태우면 등록해봐야 그날 오후 세션엔 안 잡힌다."""
    assert _exceeded(hhmm, _close_deadline())


def test_close_deadline_precedes_the_1400_universe_roll():
    from quant.trade.loop import _universe_roll_bucket
    from datetime import datetime
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    dl = _close_deadline()
    at_close = datetime(2026, 8, 13, int(dl[:2]), int(dl[2:]), tzinfo=kst)
    assert not _universe_roll_bucket(at_close).endswith("+1400")
