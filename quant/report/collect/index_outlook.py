"""index_outlook 조립 — 스냅샷 + 로컬 parquet 히스토리를 순수 계산 모듈에 주입한다.

`quant.analyze.index_outlook`(순수 함수, 네트워크·디스크 없음)을 스냅샷에 이미
있는 시세/수급/VIX/캘린더와 로컬 `data/history/{symbol}/1d` parquet(확률 계산용
종가)로 채운다. 나스닥 "반도체 앵커" 요인 하나만 예외적으로 소량의 실시간 조회를
한다(아래 `_NASDAQ_ANCHOR_SYMBOL` 참고) — 그 외에는 **추가 네트워크 호출이
없다**(report_cli._derive()가 이미 받아 둔 스냅샷/캐시를 재사용).

`quant.report.collect.core._derive()`가 만드는 `stance`/`machine_payload`와는
완전히 별개의 최상위 payload 키(`index_outlook`)로만 얹힌다 — 기존 계약은
건드리지 않는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from quant.analyze import index_outlook as idx_calc
from quant.analyze.briefing import (
    ANCHOR_STRONG_PCT, FLOW_BIG, INDEX_STRONG_PCT, VIX_CALM, VIX_STRESS,
)
from quant.collect.contracts import Snapshot
from quant.collect.sources.market import fetch_symbol_quotes
from quant.core.terms import event_term

# 확률 계산용 대리 심볼(ETF) — 지수 자체(^KS11 등)가 아니라 실제 거래되는
# 프록시를 쓴다. 지수 시계열은 2차 소스 교차검증 사각지대라 드물게 물리적으로
# 불가능한 값이 섞인다(briefing.implausible_moves docstring — 2026-08-12 KOSPI
# +17.91% 실측 오염). ETF 프록시는 이미 `data/history/{symbol}/1d`에 로컬
# 백필돼 있다(EC2 — 069500/229200/QQQ, docs/plans/재설계-phase4-8.md 실측).
# SPY 는 아직 백필 확인 안 됨 — 없으면 `probability.prob=None`으로 정직하게
# 드러난다(지어내지 않는다).
PROXY_SYMBOL = {"kospi": "069500", "kosdaq": "229200", "sp500": "SPY", "nasdaq": "QQQ"}

# 나스닥 쪽 "반도체 앵커" — KR 앵커(삼성전자/SK하이닉스 평균)에 대응하는 요인이
# US 엔 없다(quant.collect.sources.market.ANCHORS["US"] == {}). 기존 ANCHORS
# 딕셔너리에 얹지 않는 이유: 그러면 `briefing.stance()`가 US 리포트에서도 이
# 앵커를 읽어 **기존 스탠스 점수/라벨이 바뀐다**(요청 범위 밖 — "briefing.py의
# 기존 함수 시그니처·동작은 깨지 마라"). 그래서 이 요인 하나만을 위해 별도로
# 단일 심볼을 조회한다(시장당 최대 1회 배치 조회, US 리포트에서만).
# SOXX(iShares Semiconductor ETF)는 유동성 높은 단일 대표 심볼이라 KR의
# "삼성전자·SK하이닉스 평균"과 같은 역할을 한다.
_NASDAQ_ANCHOR_SYMBOL = "SOXX"
_NASDAQ_ANCHOR_LABEL = "반도체(SOXX)"


def _ok(snap: Snapshot, key: str) -> dict | None:
    r = snap.results.get(key)
    return r.data if r is not None and r.ok and r.data else None


def load_daily_closes(symbol: str, root: Path) -> list[float] | None:
    """`data/history/{symbol}/1d/*/*.parquet`에서 종가 리스트(오름차순)를 읽는다.

    파티션이 없거나 전부 빈 파일이면 `None` — 0건으로 위장하지 않는다
    (`quant.analyze.opendays.anchor_dir_for`와 같은 디렉토리 관례). 봉 마감
    시각으로 리플레이하지 않고 종가 값만 쓰므로 `quant/adapters/data/history.py`
    수준의 tz 정합성 처리는 필요 없다 — 그래도 여러 tz 표기가 섞인 파티션이
    있으면 인덱스 정렬만으로 시간순은 보장된다.
    """
    hist_dir = root / "data" / "history" / symbol / "1d"
    parts = sorted(hist_dir.glob("*/*.parquet"))
    if not parts:
        return None
    frames = []
    for p in parts:
        try:
            d = pd.read_parquet(p)
        except Exception:
            continue
        if d.empty or "close" not in d.columns:
            continue
        frames.append(d)
    if not frames:
        return None
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    closes = [float(v) for v in df["close"].dropna().tolist()]
    return closes or None


def _imminent_event_text(snap: Snapshot) -> str | None:
    """`briefing.stance()`와 동일한 판정(고영향·D-1 이내)의 관망 압력 문구."""
    cal = _ok(snap, "calendar") or {}
    imminent = [
        e for e in cal.get("events", [])
        if e.get("high_impact") and e.get("days_ahead", 99) <= 1
    ]
    if not imminent:
        return None
    return f"{event_term(imminent[0]['name'])} {imminent[0]['dday']}"


def _index_entry(
    *, closes_symbol: str, index_label: str, index_change_pct: float | None,
    flow_row: dict | None, vix: float | None, anchor_avg_pct: float | None,
    anchor_label: str | None, imminent_event_text: str | None, root: Path,
) -> dict:
    outlook = idx_calc.factor_outlook(
        index_label=index_label, index_change_pct=index_change_pct,
        index_strong_pct=INDEX_STRONG_PCT, flow_row=flow_row, flow_big=FLOW_BIG,
        vix=vix, vix_calm=VIX_CALM, vix_stress=VIX_STRESS,
        anchor_avg_pct=anchor_avg_pct, anchor_label=anchor_label,
        anchor_strong_pct=ANCHOR_STRONG_PCT, imminent_event_text=imminent_event_text,
    )
    closes = load_daily_closes(closes_symbol, root)
    if closes is None:
        probability = {
            "prob": None, "n": 0, "method": None,
            "reason": "일봉 없음(로컬 파티션 없음)",
        }
    else:
        probability = idx_calc.empirical_probability(closes)
    outlook["probability"] = probability
    outlook["proxy_symbol"] = closes_symbol
    return outlook


def build_index_outlook(snap: Snapshot, root: Path) -> dict:
    """리포트 payload에 얹을 `index_outlook` 키. 시장당 지수 2개.

    KR: `{"kospi": ..., "kosdaq": ...}`. US: `{"sp500": ..., "nasdaq": ...}`.
    값이 없는 요인은 `factor_outlook`이 span에서 스스로 제외하고, 확률은
    결측이면 `probability.prob=None` + `reason`으로 그대로 드러난다 — 이
    함수는 예외를 던지지 않는다(리포트 발행을 막지 않는다는 이 파이프라인의
    기존 관례를 그대로 따른다).
    """
    market_data = _ok(snap, "market") or {}
    quotes = market_data.get("quotes", {})
    anchors = market_data.get("anchors", {})
    vix = (quotes.get("^VIX") or {}).get("close")
    imminent_text = _imminent_event_text(snap)

    def _pct(sym: str) -> float | None:
        return (quotes.get(sym) or {}).get("change_pct")

    def _label(sym: str, default: str) -> str:
        return (quotes.get(sym) or {}).get("label", default)

    if snap.market == "KR":
        kr_anchor_pcts = [
            a["change_pct"] for a in anchors.values() if a.get("change_pct") is not None
        ]
        kr_anchor_avg = sum(kr_anchor_pcts) / len(kr_anchor_pcts) if kr_anchor_pcts else None
        kr_anchor_label = "·".join(a["label"] for a in anchors.values()) if anchors else None

        kospi_row = ((_ok(snap, "kospi_flow") or {}).get("rows") or [None])[0]
        kosdaq_row = ((_ok(snap, "kosdaq_flow") or {}).get("rows") or [None])[0]

        return {
            "kospi": _index_entry(
                closes_symbol=PROXY_SYMBOL["kospi"],
                index_label=_label("^KS11", "KOSPI"), index_change_pct=_pct("^KS11"),
                flow_row=kospi_row, vix=vix,
                anchor_avg_pct=kr_anchor_avg, anchor_label=kr_anchor_label,
                imminent_event_text=imminent_text, root=root,
            ),
            "kosdaq": _index_entry(
                closes_symbol=PROXY_SYMBOL["kosdaq"],
                index_label=_label("^KQ11", "KOSDAQ"), index_change_pct=_pct("^KQ11"),
                flow_row=kosdaq_row, vix=vix,
                anchor_avg_pct=None, anchor_label=None,
                imminent_event_text=imminent_text, root=root,
            ),
        }

    if snap.market == "US":
        nasdaq_anchor_avg = None
        try:
            anchor_q = fetch_symbol_quotes([_NASDAQ_ANCHOR_SYMBOL])
            pct = (anchor_q.get(_NASDAQ_ANCHOR_SYMBOL) or {}).get("change_pct")
            if pct is not None:
                nasdaq_anchor_avg = pct
        except Exception as e:  # noqa: BLE001 — 앵커 조회 실패가 리포트를 막지 않는다
            print(f"나스닥 반도체 앵커 생략: {type(e).__name__}: {e}", file=sys.stderr)

        return {
            "sp500": _index_entry(
                closes_symbol=PROXY_SYMBOL["sp500"],
                index_label=_label("^GSPC", "S&P500"), index_change_pct=_pct("^GSPC"),
                flow_row=None, vix=vix,
                anchor_avg_pct=None, anchor_label=None,
                imminent_event_text=imminent_text, root=root,
            ),
            "nasdaq": _index_entry(
                closes_symbol=PROXY_SYMBOL["nasdaq"],
                index_label=_label("^IXIC", "NASDAQ"), index_change_pct=_pct("^IXIC"),
                flow_row=None, vix=vix,
                anchor_avg_pct=nasdaq_anchor_avg, anchor_label=_NASDAQ_ANCHOR_LABEL,
                imminent_event_text=imminent_text, root=root,
            ),
        }

    raise ValueError(f"알 수 없는 시장: {snap.market!r}")
