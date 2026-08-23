"""향후 경제 이벤트 캘린더 — D-day 라벨과 함께.

Investing.com 경제캘린더와 BLS 공표일정은 봇 차단(403)이라 스크레이핑하지
않는다. 대신 공식 API/공식 페이지 두 곳에서 같은 정보를 얻는다:
- FRED `/fred/releases/dates` — CPI·PPI·고용·PCE·GDP·소매판매 등 미 지표 발표일.
- 연준 FOMC 캘린더 페이지 — 정형 API가 없어 HTML을 정규식으로 파싱한다.

FOMC 파싱이 실패해도 캘린더 전체를 죽이지 않는다 — FRED 쪽만으로도 절반은
쓸모가 있어서다.
"""
from __future__ import annotations

import re
import time
from datetime import date, datetime, time as dtime, timedelta

from quant.core.report_clock import ET, KST
from quant.core.terms import event_freq
from quant.adapters.env import get_key
from quant.adapters.http import client

FRED_DATES = "https://api.stlouisfed.org/fred/releases/dates"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

HIGH_IMPACT: frozenset[str] = frozenset(
    {
        "Consumer Price Index",
        "Employment Situation",
        "Personal Income and Outlays",
        "Producer Price Index",
        "Gross Domestic Product",
        "Advance Monthly Sales for Retail and Food Services",
        "Unemployment Insurance Weekly Claims Report",
        "Advance Report on Durable Goods",
        "FOMC Meeting",
    }
)

# 릴리즈명 → 발표 시각 (미 동부시간 기준, 시/분). 미 경제지표는 발표 시각이
# 고정 스케줄이다 — 08:30 ET 는 CPI/PPI/소매판매/실업수당청구/GDP/PCE 등,
# 14:00 ET 는 FOMC 성명. 매핑에 없는 릴리즈는 시각을 모른다.
RELEASE_TIME_ET: dict[str, tuple[int, int]] = {
    "Consumer Price Index": (8, 30),
    "Producer Price Index": (8, 30),
    "Employment Situation": (8, 30),
    "Personal Income and Outlays": (8, 30),
    "Gross Domestic Product": (8, 30),
    "Advance Monthly Sales for Retail and Food Services": (8, 30),
    "Unemployment Insurance Weekly Claims Report": (8, 30),
    "Advance Report on Durable Goods": (8, 30),
    "FOMC Meeting": (14, 0),
}

# 사흘 안쪽은 D-N 보다 우리말이 즉시 읽힌다. 그 밖은 D-N 이 간결하다.
_NEAR_LABELS = {0: "오늘", 1: "내일", 2: "모레"}

_WEEKDAY_KO = ("월", "화", "수", "목", "금", "토", "일")

_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)
_FOMC_RE = re.compile(
    rf"({_MONTHS})\s+(\d{{1,2}})(?:\s*[-–]\s*(\d{{1,2}}))?,?\s+(\d{{4}})"
)


def et_to_kst_hhmm(d: date, hh: int, mm: int) -> str:
    """ET 발표 시각을 해당 날짜 기준 KST `"HH:MM"` 으로 환산한다.

    KST 오프셋을 상수로 하드코딩하지 않는다 — 미국은 서머타임을 쓰고 한국은
    안 쓰기 때문에 EDT(-4)/EST(-5) 전환기마다 실제 오프셋이 13시간/14시간으로
    바뀐다. 날짜를 tz-aware로 만들어 astimezone 하면 이 전환을 자동으로 반영한다.
    """
    et_dt = datetime.combine(d, dtime(hh, mm), tzinfo=ET)
    return et_dt.astimezone(KST).strftime("%H:%M")


def _is_high_impact(name: str) -> bool:
    """**정확 일치로 판정한다.** 부분 문자열은 확실히 틀린다 — 실측(2026-08-12)에서
    "Research Consumer Price Index"가 "Consumer Price Index"에,
    "Debt to Gross Domestic Product Ratios"가 "Gross Domestic Product"에 걸려
    리포트 최상단이 가짜 고영향 이벤트로 채워졌다. FRED 릴리즈명은 안정적이라
    정확 일치가 옳다.
    """
    return name.strip() in HIGH_IMPACT


def to_dday(events: list[dict], today: date) -> list[dict]:
    """과거 이벤트를 버리고, 각 이벤트에 D-day 라벨을 붙여 정렬한다."""
    out = []
    for ev in events:
        ev_date = date.fromisoformat(ev["date"])
        days_ahead = (ev_date - today).days
        if days_ahead < 0:
            continue
        dday = _NEAR_LABELS.get(days_ahead, f"D-{days_ahead}")
        name = ev["name"]
        time_et = RELEASE_TIME_ET.get(name.strip())
        time_kst = et_to_kst_hhmm(ev_date, *time_et) if time_et else ""
        out.append(
            {
                **ev,
                "dday": dday,
                "days_ahead": days_ahead,
                "high_impact": _is_high_impact(name),
                "weekday": _WEEKDAY_KO[ev_date.weekday()],
                "time_kst": time_kst,
                "freq": event_freq(name),
                "is_today": days_ahead == 0,
                "is_tomorrow": days_ahead == 1,
            }
        )
    out.sort(key=lambda e: (e["days_ahead"], e["name"]))
    return out


def parse_fomc(html: str) -> list[dict]:
    """연준 캘린더 페이지에서 회의 날짜를 뽑는다.

    "August 18-19, 2026" 처럼 범위로 쓰여 있다 — 회의 결과가 나오는 종료일
    기준으로 잡는다.
    """
    months = _MONTHS.split("|")
    out = []
    seen: set[str] = set()
    for month, start_day, end_day, year in _FOMC_RE.findall(html):
        day = end_day or start_day
        month_num = months.index(month) + 1
        iso = date(int(year), month_num, int(day)).isoformat()
        if iso in seen:
            continue
        seen.add(iso)
        out.append({"name": "FOMC Meeting", "date": iso, "source": "Federal Reserve"})
    return out


FRED_TIMEOUT = 60.0
FRED_RETRIES = 2
_RETRY_BACKOFF_SEC = 2.0


def _get_with_retry(c, url: str, params: dict):
    """일시적 타임아웃을 흡수한다. 마지막 시도까지 실패하면 예외를 그대로 올려
    SourceResult(ok=False) 로 기록되게 둔다 — 조용히 빈 캘린더를 반환하면
    '오늘은 이벤트가 없다'와 구분이 안 된다."""
    last: Exception | None = None
    for attempt in range(FRED_RETRIES + 1):
        try:
            resp = c.get(url, params=params)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last = e
            if attempt < FRED_RETRIES:
                time.sleep(_RETRY_BACKOFF_SEC * (attempt + 1))
    raise last  # type: ignore[misc]


def fetch_calendar(today: date, horizon_days: int = 14) -> dict:
    """FRED + FOMC 이벤트를 합쳐 향후 `horizon_days`일 캘린더를 만든다."""
    api_key = get_key("FRED_API_KEY")
    if not api_key:
        raise RuntimeError("FRED_API_KEY 미설정")

    events: list[dict] = []
    # FRED releases/dates 는 실측 기준 6~7초가 걸린다(59KB, 440건). 수집기가
    # 7개 소스를 동시에 돌리므로 기본 20초로는 여유가 없어 실제로 ReadTimeout 이
    # 반복됐다. 넉넉히 잡고, 일시적 지연은 재시도로 흡수한다 — 캘린더는 이
    # 리포트에서 '미리 알아야 할 것'을 담당하는 칸이라 조용히 비면 안 된다.
    with client(timeout=FRED_TIMEOUT) as c:
        resp = _get_with_retry(
            c,
            FRED_DATES,
            {
                "api_key": api_key,
                "file_type": "json",
                "realtime_start": today.isoformat(),
                "realtime_end": (today + timedelta(days=horizon_days)).isoformat(),
                "include_release_dates_with_no_data": "true",
                "limit": 1000,
            },
        )
        for item in resp.json().get("release_dates", []):
            events.append(
                {
                    "name": item["release_name"],
                    "date": item["date"],
                    "source": "FRED",
                }
            )

        try:
            fomc_resp = c.get(FOMC_URL)
            fomc_resp.raise_for_status()
            events.extend(parse_fomc(fomc_resp.text))
        except Exception:
            pass  # FOMC 실패는 캘린더 전체를 죽이지 않는다

    dday_events = [e for e in to_dday(events, today) if e["days_ahead"] <= horizon_days]
    return {"horizon_days": horizon_days, "events": dday_events}
