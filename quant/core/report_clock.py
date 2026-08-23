"""발행 시각과 세션 윈도우. 개장 60분 전이 유일한 발행 규칙이다.

US 시각을 KST로 하드코딩하지 않는다 — 서머타임 전환 때 한 시간씩 어긋나
"장전 리포트"가 개장 후에 나오는 사고를 막기 위해서다. 항상 거래소 현지
시간대에서 계산해 KST로 환산한다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

DEFAULT_WINDOW = timedelta(hours=24)

# 개장 몇 분 전에 낼 것인가.
#
# KR 60분 = 08:00 KST.
#
# US 150분(2시간 30분)인 이유는 **거래 엔진 크론과의 충돌 회피**다. 미국 개장은
# KST 로 서머타임 22:30 / 표준시 23:30 이라 리드가 고정이어도 발행 시각은 한 시간
# 움직인다. 엔진은 21:40 에 US 관심종목을 초기화하므로 그 구간을 양쪽 체제에서
# 모두 피해야 한다 (실측 계산):
#     65분  → 21:25 / 22:25   ⚠ 서머타임에 15분 차
#     120분 → 20:30 / 21:30   ⚠ 표준시에 10분 차 — '2시간'이 오히려 더 나쁘다
#     150분 → 20:00 / 21:00   ✅ 양쪽 모두 40분 이상 여유
#
# 부수 효과가 하나 더 있다: DST 를 추종하는 리드는 **항상 미 동부 07:00 에
# 실행**된다는 뜻이라, 계절과 무관하게 시장 기준 같은 시각에 데이터를 모은다
# (프리마켓은 04:00 ET 개장이라 3시간 진행된 상태). 고정 KST 시각이었다면
# 여름과 겨울에 시장 기준 시각이 한 시간씩 어긋났을 것이다.
LEAD: dict[str, timedelta] = {
    "KR": timedelta(minutes=60),
    "US": timedelta(minutes=150),
}

_OPEN: dict[str, tuple[time, ZoneInfo]] = {
    "KR": (time(9, 0), KST),
    "US": (time(9, 30), ET),
}


def publish_at(market: str, session_date: date) -> datetime:
    """`market` 세션의 발행 시각 (KST aware)."""
    try:
        open_time, tz = _OPEN[market]
    except KeyError:
        raise ValueError(f"unknown market: {market!r} (KR|US)") from None
    local_open = datetime.combine(session_date, open_time, tzinfo=tz)
    return (local_open - LEAD[market]).astimezone(KST)


def session_window(
    publish_time: datetime, previous: datetime | None
) -> tuple[datetime, datetime]:
    """이번 리포트가 다룰 구간 (시작, 끝).

    시작점을 **직전 스냅샷의 생성시각**으로 잡는다. 요일이나 공휴일 달력을
    하드코딩하지 않으므로 주말·공휴일·장애로 하루 걸른 경우가 전부 자동으로
    메워진다. 직전이 없거나 미래면 24시간으로 떨어진다.

    윈도우를 하루로 고정하면 금요일 장 마감 후 나온 재료가 월요일 리포트에서
    통째로 사라진다 — 개장 전 리포트로서 치명적이다.
    """
    if previous is None or previous >= publish_time:
        return publish_time - DEFAULT_WINDOW, publish_time
    return previous, publish_time
