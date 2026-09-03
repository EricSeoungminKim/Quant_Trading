"""시장 펄스 다이제스트 (2026-09-03 소유자 요청) — analyze 평면, `quant/trade/` 임포트 금지.

## 왜 이 파일이 있나

소유자 원문(번역): "프로그램이 매매를 안 할 때도 참고할 수 있게, 기존 'KODEX
과매도' 알림 스타일로 주기적인 텔레그램 시그널 다이제스트를 달라 — US 세션엔
미국 지수 과매수/과매도, 금리가 과도하게 오르거나 내렸는지, 유가·금 등을 한
메시지로." 이 모듈은 그 판단만 한다: 주문을 내지 않고 선정 원장에도 쓰지 않는다
(순수 참고용 — `render_telegram` 메시지 헤더에도 "자동매매와 무관"을 항상 박는다).

## 순수 함수 — 네트워크·파일 I/O는 여기 없다(단, `load_macro_series`/
## `load_kr_regime_reasons`는 예외: 로컬 디스크 읽기)

`compute_pulse`/`render_telegram`은 이미 메모리에 있는 데이터(pandas)만 다루는
순수 함수다. 시세 조회(Toss, 네트워크)는 analyze 평면에서 임포트 금지인
`quant/adapters/`를 건드리므로 호출부(`quant.apps.cli cmd_market_pulse`)가
가져와 주입한다 — `quant/analyze/manual_recs.py`의 `price_of` 주입 관례와 같다.

`load_macro_series`/`load_kr_regime_reasons` 둘은 **로컬 파일**만 읽는다(네트워크
없음) — `manual_recs.py`가 `frgn_flow.jsonl`/`watchlist.yaml`을 analyze 평면에서
직접 읽는 것과 같은 관례(그 모듈 docstring "이 파일이 하지 않는 것" 참고, 로컬
디스크 읽기는 analyze 평면 허용 범위).

## 데이터 출처

- 지수/ETF(RSI·%b·z-score 계산 대상): Toss 일봉(`client.candles(symbol,
  interval="day", count=300)`) — CLI가 조회해 `bars_by_key`로 주입.
- 금리/VIX/달러/유가: `data/ledger/macro_rates.jsonl`(FRED 경유,
  `quant.adapters.macro.fred`가 적재) — 네트워크 없이 이미 수집된 값만 읽는다.
  **유가는 ETF 프록시가 아니라 이 파일의 `oil_wti`(FRED DCOILWTICO) 시리즈를
  쓴다** — 이미 매일 수집되고 있어 Toss 상장 여부에 기대지 않는 쪽이 더
  견고하다(gold은 FRED가 무료 시리즈를 중단해 대체가 없다 — `quant/adapters/
  macro/fred.py`의 `SERIES` 주석 참고, 그래서 gold만 ETF(GLD/132030)로 간다).
- KR 외국인 수급(`kr_flow`): `data/state/regime.json`의
  `markets.KR.reasons`(`quant/trade/regime/provider.py`가 매일 갱신하는 문구,
  "KR 수급: 외국인+기관 순매수 ±N.NN조 (...)") 에서 숫자만 정규식으로 뽑는다.
  regime.json은 시계열이 아니라 오늘 값 하나만 있으므로 RSI/%b/z-score 대상이
  아니다 — 이 지표만 별도 취급.

## 이 파일이 하지 않는 것

- KOSPI/KOSDAQ 지수 자체를 별도로 조회하지 않는다. Toss market-indicators API는
  지수 심볼을 지원하지 않는다(`quant/adapters/regime_indicators.py`
  `TossIndicatorClient` docstring 실측) — KR 로스터의 069500/229200(KODEX200/
  코스닥150 ETF)이 이미 그 프록시이므로 중복 조회다.
- TLT(장기채)는 US 로스터에만 있다. KR 로스터는 과제 지시문이 명시한
  069500/229200/122630/114800/132030 다섯 종목 그대로다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Iterable

import pandas as pd

from quant.analyze import manual_recs

# ========================================================================
# 로스터 — CLI가 이 순서 그대로 Toss 일봉을 조회해 bars_by_key 에 담는다.
# ========================================================================

US_ROSTER = ("SPY", "QQQ", "IWM", "SOXX", "GLD", "TLT")
KR_ROSTER = ("069500", "229200", "122630", "114800", "132030")

# SOXX 결측 시 CLI가 대체 조회하는 종목(반도체 3배 레버리지 ETF, 과제 지시
# "SOXX(or SOXL as proxy)"). 대체돼도 표시 키는 그대로 "SOXX" — 값만 SOXL로
# 채운다(라벨은 SOXX 그대로 두면 오해 소지가 있어 대체 여부를 별도 표기).
SOXX_FALLBACK = "SOXL"

INSTRUMENT_LABELS: dict[str, str] = {
    "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "SOXX": "SOXX(반도체)",
    "GLD": "금(GLD)", "TLT": "TLT(장기채)",
    "069500": "KODEX200", "229200": "KODEX코스닥150",
    "122630": "KODEX레버리지", "114800": "KODEX인버스", "132030": "KODEX골드",
    "dollar_index": "달러인덱스(DXY)", "usdkrw": "USD/KRW", "oil_wti": "WTI 유가",
}

# macro_rates.jsonl 시리즈 중 "가격형 지표"(RSI/%b/z-score 동일 취급) — us_10y/
# us_2y/vix는 별도 취급(RatesPulse, _compute_rates 참고)이라 여기 없다.
MACRO_INSTRUMENT_SERIES = ("dollar_index", "usdkrw", "oil_wti")

# ========================================================================
# 모델
# ========================================================================


@dataclass(frozen=True)
class InstrumentPulse:
    key: str
    label: str
    last: float | None
    chg_1d_pct: float | None
    chg_5d_pct: float | None
    chg_20d_pct: float | None
    rsi14: float | None
    pct_b: float | None
    z60: float | None
    dist_52w_high_pct: float | None  # 0 이하 — 52주 고가 대비 거리
    dist_52w_low_pct: float | None   # 0 이상 — 52주 저가 대비 거리
    state: str  # "▲과매수" | "▼과매도" | "중립" | "결측"
    extreme: bool
    missing: bool = False


@dataclass(frozen=True)
class RatesPulse:
    us10y_level: float | None
    us10y_chg20d_bp: float | None
    us10y_label: str | None  # "급등" | "급락" | None
    spread_10y2y: float | None
    spread_inverted: bool
    vix_level: float | None
    vix_bucket: str | None  # "저변동" | "보통" | "공포" | "극단"


@dataclass(frozen=True)
class KrFlowPulse:
    net_trillion: float | None
    reason: str | None
    state: str  # "▲순매수" | "▼순매도" | "중립" | "결측"


@dataclass(frozen=True)
class PulseReport:
    as_of: _date
    instruments: list[InstrumentPulse] = field(default_factory=list)
    rates: RatesPulse | None = None
    kr_flow: KrFlowPulse | None = None


# ========================================================================
# 지표 계산 — RSI(14)는 quant/analyze/manual_recs.py의 Wilder RSI(이미
# quant.trade.indicators.rsi()와 대조 검증됨, period 파라미터만 다르게 재사용)를
# 그대로 쓴다. 같은 analyze 평면 안에서 세 번째로 재구현하지 않는다.
# ========================================================================


def _rsi14_last(closes: pd.Series) -> float | None:
    if len(closes) < 15:
        return None
    series = manual_recs._wilder_rsi(closes, period=14)
    val = series.iloc[-1]
    return None if pd.isna(val) else float(val)


def _percent_b(closes: pd.Series, period: int = 20, num_std: float = 2.0) -> float | None:
    """볼린저 %b — (종가 - 하단밴드) / (상단밴드 - 하단밴드). 표준편차 0(가격
    불변 구간)이거나 데이터 부족이면 None(0.5로 위장하지 않는다)."""
    if len(closes) < period:
        return None
    tail = closes.tail(period)
    sma = tail.mean()
    std = tail.std()
    if pd.isna(std) or std == 0:
        return None
    upper = sma + num_std * std
    lower = sma - num_std * std
    if upper == lower:
        return None
    return float((closes.iloc[-1] - lower) / (upper - lower))


def _zscore(closes: pd.Series, window: int = 60) -> float | None:
    if len(closes) < window:
        return None
    tail = closes.tail(window)
    std = tail.std()
    if pd.isna(std) or std == 0:
        return None
    return float((tail.iloc[-1] - tail.mean()) / std)


def _pct_change(closes: pd.Series, back: int) -> float | None:
    if len(closes) <= back:
        return None
    prev = closes.iloc[-1 - back]
    if pd.isna(prev) or prev == 0:
        return None
    return float(closes.iloc[-1] / prev - 1) * 100


def _dist_52w(closes: pd.Series) -> tuple[float | None, float | None]:
    """52주(최대 252 영업일, 데이터가 그보다 적으면 있는 만큼) 고가/저가 대비
    현재가 거리(%). 데이터가 아예 없으면 (None, None)."""
    if closes.empty:
        return None, None
    window = closes.tail(252)
    last = float(closes.iloc[-1])
    hi, lo = window.max(), window.min()
    dist_hi = None if pd.isna(hi) or hi == 0 else (last / hi - 1) * 100
    dist_lo = None if pd.isna(lo) or lo == 0 else (last / lo - 1) * 100
    return dist_hi, dist_lo


def _state_label(rsi: float | None, pct_b: float | None) -> str:
    """RSI(14)>=70 또는 %b>=1.0 → 과매수. RSI<=30 또는 %b<=0.0 → 과매도.
    과매수 판정이 먼저다(RSI·%b가 서로 어긋나 둘 다 걸리는 경우는 실측상
    드물지만, 결정론을 위해 순서를 고정)."""
    if rsi is None and pct_b is None:
        return "중립"
    if (rsi is not None and rsi >= 70) or (pct_b is not None and pct_b >= 1.0):
        return "▲과매수"
    if (rsi is not None and rsi <= 30) or (pct_b is not None and pct_b <= 0.0):
        return "▼과매도"
    return "중립"


def _is_extreme(z: float | None) -> bool:
    return z is not None and (z >= 2 or z <= -2)


def _instrument_from_closes(key: str, closes: pd.Series) -> InstrumentPulse:
    label = INSTRUMENT_LABELS.get(key, key)
    closes = closes.dropna() if closes is not None else pd.Series(dtype=float)
    if closes.empty:
        return InstrumentPulse(
            key=key, label=label, last=None, chg_1d_pct=None, chg_5d_pct=None,
            chg_20d_pct=None, rsi14=None, pct_b=None, z60=None,
            dist_52w_high_pct=None, dist_52w_low_pct=None, state="결측",
            extreme=False, missing=True,
        )
    rsi = _rsi14_last(closes)
    pct_b = _percent_b(closes)
    z = _zscore(closes)
    dist_hi, dist_lo = _dist_52w(closes)
    return InstrumentPulse(
        key=key, label=label, last=float(closes.iloc[-1]),
        chg_1d_pct=_pct_change(closes, 1), chg_5d_pct=_pct_change(closes, 5),
        chg_20d_pct=_pct_change(closes, 20), rsi14=rsi, pct_b=pct_b, z60=z,
        dist_52w_high_pct=dist_hi, dist_52w_low_pct=dist_lo,
        state=_state_label(rsi, pct_b), extreme=_is_extreme(z), missing=False,
    )


def _compute_rates(macro: dict[str, pd.Series]) -> RatesPulse:
    us10y = macro.get("us_10y", pd.Series(dtype=float)).dropna()
    us2y = macro.get("us_2y", pd.Series(dtype=float)).dropna()
    vix = macro.get("vix", pd.Series(dtype=float)).dropna()

    us10y_level = float(us10y.iloc[-1]) if not us10y.empty else None
    us10y_chg20d_bp = None
    if len(us10y) >= 21:
        # 국채수익률은 %p 단위 시세 — 1%p 변화 = 100bp (regime/provider.py
        # _bond_yield_indicator와 동일 환산).
        us10y_chg20d_bp = float((us10y.iloc[-1] - us10y.iloc[-21]) * 100)
    us10y_label = None
    if us10y_chg20d_bp is not None:
        if us10y_chg20d_bp >= 30:
            us10y_label = "급등"
        elif us10y_chg20d_bp <= -30:
            us10y_label = "급락"

    spread = us10y_level - float(us2y.iloc[-1]) if us10y_level is not None and not us2y.empty else None
    spread_inverted = spread is not None and spread < 0

    vix_level = float(vix.iloc[-1]) if not vix.empty else None
    vix_bucket = None
    if vix_level is not None:
        if vix_level < 15:
            vix_bucket = "저변동"
        elif vix_level <= 25:
            vix_bucket = "보통"
        elif vix_level <= 35:
            vix_bucket = "공포"
        else:
            vix_bucket = "극단"

    return RatesPulse(
        us10y_level=us10y_level, us10y_chg20d_bp=us10y_chg20d_bp, us10y_label=us10y_label,
        spread_10y2y=spread, spread_inverted=spread_inverted,
        vix_level=vix_level, vix_bucket=vix_bucket,
    )


# regime.json의 KR reasons 문구(quant/trade/regime/provider.py _compute_kr):
# f"KR 수급: 외국인+기관 순매수 {net/1e12:+.2f}조 ({s:+d})"
_KR_FLOW_RE = re.compile(r"KR 수급:.*?순매수\s*([+-]?[\d.]+)조")


def _build_kr_flow(reasons: list[str]) -> KrFlowPulse:
    net: float | None = None
    reason_text: str | None = None
    for r in reasons:
        m = _KR_FLOW_RE.search(r)
        if m:
            reason_text = r
            try:
                net = float(m.group(1))
            except ValueError:
                net = None
            break
    if net is None:
        state = "결측"
    elif net > 0:
        state = "▲순매수"
    elif net < 0:
        state = "▼순매도"
    else:
        state = "중립"
    return KrFlowPulse(net_trillion=net, reason=reason_text, state=state)


def compute_pulse(
    bars_by_key: dict[str, pd.DataFrame],
    macro: dict[str, pd.Series],
    *,
    as_of: _date,
    kr_reasons: list[str] | None = None,
) -> PulseReport:
    """`bars_by_key`(심볼→OHLCV 일봉, CLI가 Toss로 조회) + `macro`(시리즈명→
    값 시계열, CLI가 `load_macro_series`로 로드)로 다이제스트를 계산한다.

    `kr_reasons`는 KR 시장일 때만 CLI가 넘긴다(빈 리스트여도 됨 — "계산은
    하되 못 찾으면 결측"과 "애초에 US라 계산 안 함"을 구분하는 신호는
    `kr_reasons is not None` 여부다). None이면 `PulseReport.kr_flow`도 None —
    US 다이제스트에는 이 줄 자체가 나오지 않는다."""
    instruments = [_instrument_from_closes(key, df.get("close") if isinstance(df, pd.DataFrame) else None)
                   for key, df in bars_by_key.items()]
    for key in MACRO_INSTRUMENT_SERIES:
        if key in macro:
            instruments.append(_instrument_from_closes(key, macro[key]))

    rates = _compute_rates(macro)
    kr_flow = _build_kr_flow(kr_reasons) if kr_reasons is not None else None
    return PulseReport(as_of=as_of, instruments=instruments, rates=rates, kr_flow=kr_flow)


# ========================================================================
# 텔레그램 렌더링
# ========================================================================

_MARKET_LABEL = {"KR": "한국", "US": "미국"}
_MAX_CHARS = 4096


def _fmt_pct(x: float | None) -> str:
    return f"{x:+.1f}%" if x is not None else "n/a"


def _render_instrument_line(inst: InstrumentPulse) -> tuple[str, bool]:
    if inst.missing:
        return f"{inst.label} 결측", False
    rsi_s = f"{inst.rsi14:.0f}" if inst.rsi14 is not None else "n/a"
    pctb_s = f"{inst.pct_b:.2f}" if inst.pct_b is not None else "n/a"
    extreme_suffix = " ⚠극단" if inst.extreme else ""
    line = (
        f"{inst.label} {inst.last:,.2f} (1d {_fmt_pct(inst.chg_1d_pct)} · "
        f"5d {_fmt_pct(inst.chg_5d_pct)} · 20d {_fmt_pct(inst.chg_20d_pct)}) "
        f"RSI {rsi_s} %b {pctb_s} → {inst.state}{extreme_suffix}"
    )
    is_notable = inst.state != "중립" or inst.extreme
    return line, is_notable


def render_telegram(report: PulseReport, market: str, lang: str = "ko") -> str:
    """텔레그램 메시지(≤4096자). `lang`은 향후 다국어 확장을 위한 자리만 —
    현재 실제 수신자는 한국어뿐이라 분기를 만들지 않는다(항상 한국어로 렌더)."""
    label = _MARKET_LABEL.get(market, market)
    lines = [f"📡 시장 펄스 (참고용 — 자동매매와 무관) — {label} {report.as_of.isoformat()}"]
    notable: list[str] = []

    lines += ["", "[지수·ETF]"]
    for inst in report.instruments:
        line, is_notable = _render_instrument_line(inst)
        lines.append(line)
        if is_notable and not inst.missing:
            notable.append(f"{inst.label} {inst.state}{' ⚠극단' if inst.extreme else ''}")

    lines += ["", "[금리·변동성]"]
    r = report.rates
    if r is not None and r.us10y_level is not None:
        chg = f"{r.us10y_chg20d_bp:+.0f}bp" if r.us10y_chg20d_bp is not None else "n/a"
        suffix = f" {r.us10y_label}" if r.us10y_label else ""
        lines.append(f"10년물 {r.us10y_level:.2f}% (20일 {chg}){suffix}")
        if r.us10y_label:
            notable.append(f"10년물 {r.us10y_label}")
    else:
        lines.append("10년물 결측")
    if r is not None and r.spread_10y2y is not None:
        inv = " (역전)" if r.spread_inverted else ""
        lines.append(f"10Y-2Y 스프레드 {r.spread_10y2y:+.2f}%p{inv}")
        if r.spread_inverted:
            notable.append("10Y-2Y 스프레드 역전")
    else:
        lines.append("10Y-2Y 스프레드 결측")
    if r is not None and r.vix_level is not None:
        lines.append(f"VIX {r.vix_level:.1f} → {r.vix_bucket}")
        if r.vix_bucket in ("공포", "극단"):
            notable.append(f"VIX {r.vix_bucket}")
    else:
        lines.append("VIX 결측")

    if report.kr_flow is not None:
        lines += ["", "[외국인 수급]"]
        kf = report.kr_flow
        if kf.net_trillion is not None:
            lines.append(f"외국인+기관 순매수 {kf.net_trillion:+.2f}조 → {kf.state}")
            if kf.state not in ("중립", "결측"):
                notable.append(f"외국인 수급 {kf.state}")
        else:
            lines.append("외국인 수급 결측")

    lines += ["", "요약"]
    if notable:
        lines += [f"- {n}" for n in notable]
    else:
        lines.append("- 특이사항 없음(전부 중립)")

    lines += ["", "⚠️ 기계적 임계값 기반 참고 지표입니다 — 투자 조언이 아닙니다."]

    msg = "\n".join(lines)
    if len(msg) > _MAX_CHARS:
        msg = msg[: _MAX_CHARS - 1] + "…"
    return msg


def label_snapshot(report: PulseReport) -> dict:
    """`--changes-only` 비교용 — 상태 라벨만 뽑는다(가격 자체가 아니라 라벨이
    바뀌었을 때만 다시 보낸다는 계약이므로, 숫자는 여기 담지 않는다)."""
    snap: dict = {
        inst.key: inst.state + ("|extreme" if inst.extreme else "")
        for inst in report.instruments
    }
    if report.rates is not None:
        snap["rates:10y_label"] = report.rates.us10y_label
        snap["rates:spread_inverted"] = report.rates.spread_inverted
        snap["rates:vix_bucket"] = report.rates.vix_bucket
    if report.kr_flow is not None:
        snap["kr_flow"] = report.kr_flow.state
    return snap


# ========================================================================
# 로컬 파일 로더 (네트워크 없음 — 모듈 docstring 참고)
# ========================================================================


def load_macro_series(path: str | Path, series: Iterable[str], limit: int = 250) -> dict[str, pd.Series]:
    """`data/ledger/macro_rates.jsonl`에서 지정된 `series` 이름들만 골라 시리즈별
    최근 `limit`행(날짜 오름차순 pd.Series, 인덱스는 DatetimeIndex)을 돌려준다.
    파일이 없거나 깨진 줄은 건너뛴다 — 매크로 원장 하나가 깨져도 전체 다이제스트가
    죽으면 안 된다(요청한 시리즈가 하나도 없으면 빈 Series로 채워 돌려준다 —
    호출부가 `.get()` 없이 그대로 딕셔너리 접근할 수 있게)."""
    path = Path(path)
    wanted = set(series)
    rows: dict[str, list[tuple[str, float]]] = {s: [] for s in wanted}
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            s = row.get("series")
            if s not in wanted:
                continue
            date_val, value = row.get("date"), row.get("value")
            if date_val is None or value is None:
                continue
            try:
                rows[s].append((date_val, float(value)))
            except (TypeError, ValueError):
                continue

    out: dict[str, pd.Series] = {}
    for s, lst in rows.items():
        lst.sort(key=lambda t: t[0])
        lst = lst[-limit:]
        if not lst:
            out[s] = pd.Series(dtype=float)
        else:
            out[s] = pd.Series([v for _, v in lst], index=pd.to_datetime([d for d, _ in lst]))
    return out


def load_kr_regime_reasons(path: str | Path) -> list[str] | None:
    """`data/state/regime.json`(`RegimeProvider._save_cache`가 쓴다)의
    `markets.KR.reasons`. 파일이 없거나 KR 상태가 아직 없으면 None — "regime.json
    없음"과 "market != KR이라 애초에 안 부름"을 구분하는 신호는 호출부
    (`cmd_market_pulse`)가 만든다: 이 함수는 있는 그대로만 돌려준다."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kr = (payload.get("markets") or {}).get("KR")
    if not isinstance(kr, dict):
        return None
    reasons = kr.get("reasons")
    if not isinstance(reasons, list):
        return None
    return [str(r) for r in reasons]
