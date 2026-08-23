"""FRED — 순유동성·금융환경·금리.

순유동성(WALCL − TGA − RRP)은 발표 주기가 서로 다른 세 시계열의 차이라,
날짜를 맞추지 않고 빼면 값이 수천억 달러 단위로 튄다. align_latest_common 이
그 정합을 강제한다.
"""
from __future__ import annotations

from quant.adapters.env import get_key
from quant.adapters.http import client

BASE = "https://api.stlouisfed.org/fred"

SERIES = {
    "WALCL": "연준 총자산",
    "WTREGEN": "재무부 일반계정(TGA)",
    "RRPONTSYD": "역레포(RRP)",
    "NFCI": "시카고연준 금융환경지수",
    "DGS10": "미국 10년",
    "DGS2": "미국 2년",
}
SPARK_POINTS = 30


def nfci_label(value: float) -> str:
    """NFCI 는 0 이 장기평균이다. 음수=완화, 양수=긴축 — 숫자만 보면 뜻을 모른다."""
    if value <= -0.2:
        return "완화"
    if value >= 0.2:
        return "긴축"
    return "중립"


def curve_label(spread: float) -> str:
    """10년-2년 스프레드. 역전은 경기침체 선행지표로 읽힌다."""
    if spread < 0:
        return "역전"
    if spread < 0.5:
        return "평탄"
    return "정상"


_NL_PARTS = ("WALCL", "WTREGEN", "RRPONTSYD")


def net_liquidity(walcl: float, tga: float, rrp: float) -> float:
    return walcl - tga - rrp


def align_latest_common(
    series: dict[str, dict[str, float]]
) -> tuple[str, dict[str, float]]:
    common = set.intersection(*(set(v) for v in series.values())) if series else set()
    if not common:
        raise ValueError("시계열 간 공통 날짜가 없다 — 순유동성 계산 불가")
    latest = max(common)
    return latest, {k: v[latest] for k, v in series.items()}


def _observations(c, series_id: str, key: str, limit: int = 60) -> dict[str, float]:
    r = c.get(
        f"{BASE}/series/observations",
        params={
            "series_id": series_id, "api_key": key, "file_type": "json",
            "sort_order": "desc", "limit": limit,
        },
    )
    r.raise_for_status()
    # FRED 는 휴장일에 "." 를 넣는다 — float() 하면 ValueError 라 반드시 거른다.
    return {
        o["date"]: float(o["value"])
        for o in r.json()["observations"]
        if o["value"] not in (".", "")
    }


def fetch_macro() -> dict:
    key = get_key("FRED_API_KEY")
    if not key:
        raise RuntimeError("FRED_API_KEY 미설정")
    with client(timeout=30.0) as c:
        obs = {sid: _observations(c, sid, key) for sid in SERIES}

    nl_date, nl = align_latest_common({k: obs[k] for k in _NL_PARTS})
    latest = {}
    for sid, v in obs.items():
        if not v:
            continue
        dates = sorted(v)
        prev = v[dates[-2]] if len(dates) > 1 else v[dates[-1]]
        latest[sid] = {
            "label": SERIES[sid],
            "date": dates[-1],
            "value": v[dates[-1]],
            "change": round(v[dates[-1]] - prev, 4),
            # 카드에 추세를 같이 보여준다 — 숫자 하나만으론 방향을 알 수 없다
            "history": [v[d] for d in dates[-SPARK_POINTS:]],
        }

    # 순유동성도 시계열로 만든다. 세 시계열의 공통 날짜에서만 계산해야
    # 발표 주기 차이로 값이 튀지 않는다.
    common = sorted(set(obs["WALCL"]) & set(obs["WTREGEN"]) & set(obs["RRPONTSYD"]))
    nl_hist = [
        net_liquidity(obs["WALCL"][d], obs["WTREGEN"][d], obs["RRPONTSYD"][d])
        for d in common[-SPARK_POINTS:]
    ]

    out = {
        "series": latest,
        "net_liquidity": {
            "date": nl_date,
            "value": net_liquidity(nl["WALCL"], nl["WTREGEN"], nl["RRPONTSYD"]),
            "unit": "백만달러",
            "history": nl_hist,
            "change": round(nl_hist[-1] - nl_hist[-2], 2) if len(nl_hist) > 1 else 0.0,
        },
    }
    if "NFCI" in latest:
        out["nfci_label"] = nfci_label(latest["NFCI"]["value"])
    if "DGS10" in latest and "DGS2" in latest:
        spread = round(latest["DGS10"]["value"] - latest["DGS2"]["value"], 2)
        out["curve"] = {"spread": spread, "label": curve_label(spread)}
    return out
