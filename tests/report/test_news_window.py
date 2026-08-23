"""리포트 뉴스 표본 시작점 — 2026-08-14 운영 장애 회귀.

## 무슨 일이 있었나

`market-report@KR` 이 08:00 에 **크래시**했다:

    report_cli.py:310  datetime.fromisoformat(prev.generated_at)
    TypeError: fromisoformat: argument must be str

`Snapshot.generated_at` 은 **이미 `datetime`** 이다(`Snapshot.from_json` 이 파싱한다).
호출부가 그걸 한 번 더 파싱하려 했다 — 이중 파싱이다.

## 왜 이제야 터졌나

`prev` 가 있을 때만 터진다. Phase 1 저장소 통합으로 스냅샷이 새 경로에 쌓이기
시작했고, 첫날(08-13)은 직전 스냅샷이 없어 통과했다. 다음 날 08-13 스냅샷이
존재하자마자 터졌다. **버그가 새로 생긴 게 아니라, 정상 상태가 되자 드러났다.**

## 왜 조용했나

리포트가 없어도 `own_brief.sh` 가 랭킹 발굴만으로 계속 돌도록 설계돼 있다(의도된
저하). 실제로 그날 KR 8종목이 편입됐다 — 다만 **전부 TREND 태그**였다. 뉴스 촉매
차원이 그날 종목 선정에서 통째로 빠졌고, 그건 알림 한 줄로만 남았다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from quant.apps.report_cli import news_since_for
from quant.collect.contracts import Snapshot

KST = ZoneInfo("Asia/Seoul")


def _snapshot(generated_at: datetime) -> Snapshot:
    return Snapshot(schema_version=1, market="KR", session_date=date(2026, 8, 13),
                    generated_at=generated_at, results={})


def test_previous_snapshot_datetime_is_used_directly_not_reparsed():
    """**이 테스트가 2026-08-14 KR 리포트 장애를 재현한다.**

    `generated_at` 은 이미 datetime 이다. 문자열로 취급하면 TypeError 로 빌드가 죽는다.
    """
    prev = _snapshot(datetime(2026, 8, 13, 8, 3, 54, tzinfo=KST))
    now = datetime(2026, 8, 14, 8, 0, tzinfo=KST)

    since = news_since_for(prev, now)

    assert since == prev.generated_at


def test_missing_previous_snapshot_falls_back_to_a_day():
    """첫 실행에는 직전이 없다 — 그때는 24시간으로 떨어진다(session_window 계약)."""
    now = datetime(2026, 8, 14, 8, 0, tzinfo=KST)

    since = news_since_for(None, now)

    assert since == now - timedelta(days=1)


def test_round_trip_snapshot_keeps_generated_at_a_datetime():
    """디스크에서 읽어온 스냅샷도 datetime 이다 — 호출부가 문자열로 오해할 여지를 없앤다."""
    prev = _snapshot(datetime(2026, 8, 13, 8, 3, 54, tzinfo=KST))

    revived = Snapshot.from_json(prev.to_json())

    assert isinstance(revived.generated_at, datetime)
    assert news_since_for(revived, datetime(2026, 8, 14, 8, 0, tzinfo=KST)) \
        == revived.generated_at
