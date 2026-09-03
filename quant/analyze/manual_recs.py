"""수동 계좌 추천 레인 (2026-09-03 소유자 결정) — analyze 평면, `quant/trade/` 임포트 금지.

## 왜 이 파일이 있나

2026-09-03 소유자 결정: **자동매매는 단타·스캘핑만**이다. 오버나이트/장기 보유가
전략 정의인 네 전략(frgn_accumulate/close_bet/overnight_drift/rsi2_dip)은
`config/settings.yaml`에서 `enabled: false`로 내려갔다(코드·params·capital_fraction은
보존 — 자본 재분배 없음, 측정 기준점). 그 판단 로직 자체는 여전히 쓸모가 있다 —
다만 **엔진이 자동으로 사지 않고**, 소유자가 따로 관리하는 계좌를 위한 텔레그램
추천으로 나간다. 이 파일은 그 판단만 한다: 주문을 내지 않고, `quant/trade/`를
전혀 모른다(임포트하면 `tests/test_architecture.py`가 즉시 잡는다).

## 데이터 출처 — 전부 로컬 파일, 네트워크·LLM 없음

- `data/ledger/frgn_flow.jsonl` — 외국인 수급 시계열. `quant.analyze.foreign_trend.
  classify()`로 라벨링(frgn_accumulate/own_brief가 쓰는 것과 같은 함수 — 판정
  로직이 갈리지 않는다).
- `data/watchlist.yaml` — CLOSE_BET 태그가 붙은 종목(own_brief가 리포트의
  close_bet_view를 번역해 붙인 태그, `quant/analyze/market_brief.py
  close_bet_tokens` 참고).
- `data/history/{symbol}/1d/*.parquet` — 일봉. RSI(2) 계산 + 모든 추천의 기준가
  (`quant/analyze/opendays.py`와 같은 파티션 규칙: `YYYY/MM.parquet`, 최신 것부터).
- `out/YYYY/MM/DD/KR_close_engine.json`(있으면) — close_bet_view의 채점 근거
  문장. 없어도 추천 자체는 만든다(태그만으로 충분히 근거가 있다) — "리포트
  근거 조회 불가"로 정직하게 표시한다.

## 왜 RSI(2)를 재구현했나

`quant.trade.indicators.rsi()`를 재사용하고 싶었지만 `quant.analyze → quant.trade`
임포트는 평면 규칙 위반이다(`tests/test_architecture.py` FORBIDDEN, 루트
CLAUDE.md "뉴스가 주문으로 이어지는 경로를 코드 수준에서 끊는다"). 그래서
Wilder 평활 RSI를 여기 다시 구현한다 — 알고리즘은 `quant/trade/indicators/
__init__.py`의 `rsi()`와 동일해야 한다(시드는 첫 `period`개의 단순평균, 이후
재귀 평활). 두 구현이 갈리면 `tests/test_manual_recs.py`가 같은 입력에 대해
값을 대조해 잡는다.

## 왜 overnight_drift는 QQQ만이고 TQQQ가 아닌가

과제 지시문 예시는 "TQQQ/QQQ"였지만, `config/settings.yaml`의 실제 `overnight_drift`
설정은 `symbols: [QQQ]`뿐이다 — TQQQ는 **의도적으로 제외**돼 있다
(`quant/trade/strategy/overnight_drift.py` "왜 US ETF만인가": 레버리지 갭 리스크
3배). 그 판단을 뒤집을 근거가 없으므로 여기서도 QQQ만 추천한다 — 지시문 예시
문구보다 실제 코드의 판단을 따른다.

## 이 파일이 하지 않는 것

- 시세 조회(네트워크)를 하지 않는다 — `quant/adapters/`는 analyze 평면에서 임포트
  금지다. 기준가는 전부 로컬 parquet의 "마지막으로 알려진 종가"다(당일 실시간가
  아닐 수 있다 — 그래서 `ref_date`를 항상 함께 남긴다).
- 주문을 내지 않는다. `is_candidate: True`로 선정 원장에 남기고 텔레그램으로
  알릴 뿐, 매매는 소유자가 별도 계좌에서 직접 판단한다.
"""
from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path
from typing import Iterable

import pandas as pd
import yaml

from quant.analyze import foreign_trend
from quant.control import frgn_flow as frgn_flow_ledger
from quant.control import selections
from quant.core.models import market_of_symbol

PRODUCER = "manual_rec_v1"

# ------------------------------------------------------------------------
# 재사용 파라미터 — 원래 이 값들을 정한 전략은 2026-09-03 비활성화됐지만(자동매매
# 단타·스캘핑 전용 결정, config/settings.yaml 해당 블록 주석), 판단 규칙 자체는
# 그대로 옮겨온다. 코드가 아니라 여기 리터럴로 고정하는 이유: analyze는 trade를
# 몰라야 하므로 settings.yaml의 params 블록을 읽으러 trade 쪽 로더를 끌어올 수
# 없다(config 파싱은 apps 계층 소관이고, 그걸 analyze가 알면 결합이 는다) — 값
# 자체가 바뀌면(예: rsi2_dip 재활성 + 파라미터 조정) 이 상수도 손으로 맞춰야 한다.
_RSI2_ENTRY = 10.0  # rsi2_dip.py entry_rsi
_RSI2_TREND_SMA_DAYS = 200  # rsi2_dip.py trend_sma_days
_RSI2_EXIT = 60.0  # rsi2_dip.py exit_rsi — 무효화 문구용
_RSI2_HARD_STOP_PCT = 5.0  # rsi2_dip.py hard_stop_pct
_RSI2_MAX_HOLD_DAYS = 5  # rsi2_dip.py max_hold_days
_CLOSE_BET_STOP_PCT = 1.0  # close_bet.py stop_pct
_OVERNIGHT_DRIFT_STOP_PCT = 3.0  # overnight_drift.py stop_pct
# TQQQ 제외 이유는 모듈 docstring "왜 overnight_drift는 QQQ만인가" 참고.
_OVERNIGHT_DRIFT_SYMBOLS = ("QQQ",)
# rsi2_dip 원 설정의 KR 대상(config/settings.yaml symbols: ["069500", "QQQ"]) —
# watchlist에 없어도 항상 후보에 넣는다(지수 ETF라 관심종목 등록과 무관하게
# 매일 판정할 가치가 있다).
_RSI2_ALWAYS_KR_SYMBOLS = ("069500",)


# ========================================================================
# RSI(2) — quant/trade/indicators.py rsi()와 동일 알고리즘 재구현 (모듈 docstring)
# ========================================================================

def _wilder_rsi(close: pd.Series, period: int = 2) -> pd.Series:
    """Wilder 평활 RSI. 워밍업 구간(NaN) 그대로 — 0으로 위장하지 않는다."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    avg_gain = pd.Series(index=close.index, dtype=float)
    avg_loss = pd.Series(index=close.index, dtype=float)

    if len(close) > period:
        avg_gain.iloc[period] = gain.iloc[1:period + 1].mean()
        avg_loss.iloc[period] = loss.iloc[1:period + 1].mean()
        for i in range(period + 1, len(close)):
            avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
            avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    result = 100 - (100 / (1 + rs))
    result = result.mask(avg_loss == 0, 100.0)
    result = result.mask((avg_gain == 0) & (avg_loss == 0), 50.0)
    return result


def _sma(close: pd.Series, period: int) -> pd.Series:
    return close.rolling(period).mean()


# ========================================================================
# 일봉 I/O — quant/analyze/opendays.py와 같은 파티션 규칙(root/data/history/
# {symbol}/1d/YYYY/MM.parquet), analyze 평면에서 직접 읽는다(adapters 임포트 금지).
# ========================================================================

def _read_daily_closes(history_root: Path, symbol: str, months: int = 14) -> pd.Series:
    """`symbol`의 최근 `months`개월치 종가(오름차순). 데이터 없으면 빈 Series.

    `months=14`는 RSI(2) 트렌드 필터(SMA 200 영업일 ≈ 9.5개월)에 워밍업 여유를
    더한 값이다. 파일이 없거나 깨져도 예외를 던지지 않는다 — 추천 하나 못 만드는
    것과 전체 명령이 죽는 것은 전혀 다르다.
    """
    d = history_root / symbol / "1d"
    parts = sorted(d.glob("*/*.parquet"))
    if not parts:
        return pd.Series(dtype=float)
    frames = []
    for p in parts[-months:]:
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001 — 파손된 파티션 하나가 전체를 죽이면 안 된다
            continue
        if not df.empty and "close" in df.columns:
            frames.append(df)
    if not frames:
        return pd.Series(dtype=float)
    full = pd.concat(frames).sort_index()
    return full["close"].dropna()


def latest_close(history_root: Path, symbol: str) -> tuple[float, str] | None:
    """(마지막 종가, 그 종가의 날짜 "YYYY-MM-DD"). 데이터 없으면 None.

    실시간 시세가 아니다 — analyze 평면은 `quant/adapters/`(네트워크)를 임포트할
    수 없으므로, "로컬에 마지막으로 저장된 종가"가 기준가다. 그래서 `close_date`를
    항상 함께 돌려준다 — 얼마나 낡은 값인지 숨기지 않는다.
    """
    closes = _read_daily_closes(history_root, symbol, months=2)
    if closes.empty:
        return None
    ts = closes.index[-1]
    d = ts.date() if hasattr(ts, "date") else ts
    return float(closes.iloc[-1]), str(d)


# ========================================================================
# watchlist.yaml — FileWatchlistUniverse(quant/trade/universe.py)와 같은 두 형식을
# 받는다(손 편집 `symbols: [A, B]` / 브리지가 쓰는 `symbols: [{symbol:..., tags:...}]`).
# ========================================================================

def _load_watchlist_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    symbols = raw.get("symbols") if isinstance(raw, dict) else None
    if not isinstance(symbols, list):
        return []
    out: list[dict] = []
    for item in symbols:
        if isinstance(item, dict) and item.get("symbol"):
            out.append(item)
        elif isinstance(item, str) and item:
            out.append({"symbol": item})
    return out


def _kr_symbols_from_watchlist(path: Path) -> set[str]:
    return {
        str(e["symbol"]) for e in _load_watchlist_entries(path)
        if market_of_symbol(str(e["symbol"])) == "KR"
    }


def _distinct_symbols(flow_path: Path) -> set[str]:
    """frgn_flow.jsonl에 기록이 있는 전체 심볼(중복 제거). 손상된 줄은 건너뛴다."""
    if not flow_path.exists():
        return set()
    out: set[str] = set()
    for line in flow_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("symbol"):
            out.add(str(row["symbol"]))
    return out


def _rec_row(
    *, symbol: str, name: str | None, market: str, kind: str, reason: str,
    ref_price: float | None, ref_date: str | None, invalidation: str, horizon: str,
) -> dict:
    return {
        "symbol": symbol, "name": name, "market": market, "kind": kind,
        "reason": reason, "ref_price": ref_price, "ref_date": ref_date,
        "invalidation": invalidation, "horizon": horizon,
    }


# ========================================================================
# (a) 외국인 적립 추세 — frgn_accumulate가 쓰던 classify()를 그대로 재사용
# ========================================================================

def foreign_accumulate_recs(
    root: Path, symbols: Iterable[str] | None = None, days: int = 20,
) -> list[dict]:
    """외국인 수급 라벨이 `foreign_trend.LABEL_INFLOW`(매수 시그널/재유입)인 KR
    종목 추천. `symbols`를 안 주면 `frgn_flow.jsonl`에 기록이 있는 전 종목을 본다
    (own_brief처럼 그날 리포트 랭킹에 갇히지 않는다 — 이 레인은 매일 새로 뽑는
    자동 편입이 아니라, 이미 쌓인 수급 이력에서 사람이 볼 후보를 고르는 것이다)."""
    flow_path = root / "data" / "ledger" / "frgn_flow.jsonl"
    history_root = root / "data" / "history"
    candidates = sorted(symbols) if symbols is not None else sorted(_distinct_symbols(flow_path))

    out: list[dict] = []
    for sym in candidates:
        series = frgn_flow_ledger.load_series(flow_path, sym, days=days)
        if not series:
            continue
        info = foreign_trend.classify(series)
        if info["label"] != foreign_trend.LABEL_INFLOW:
            continue
        ref = latest_close(history_root, sym)
        reason = (
            f"외국인 {info['label']}(최근 {info['days']}일 누적 순매수 "
            f"{info['residual']:+,.0f}주" + (" · 기관 동반매수" if info["inst_follows"] else "") + ")"
        )
        out.append(_rec_row(
            symbol=sym, name=None, market="KR", kind="frgn_accumulate", reason=reason,
            ref_price=ref[0] if ref else None, ref_date=ref[1] if ref else None,
            invalidation="FRGN_EXIT(이탈 추세 전환) 확인 시 청산 고려",
            horizon="D+20",
        ))
    return out


# ========================================================================
# (b) 종가배팅 후보 — watchlist.yaml의 CLOSE_BET 태그 + (있으면) 리포트 근거
# ========================================================================

def _close_bet_reasons(root: Path, on: _date) -> dict[str, list[str]]:
    """`out/YYYY/MM/DD/KR_close_engine.json`의 `close_bet_view`에서 심볼별 채점
    근거 문장(`quant/report/collect/close.py _build_close_bet_view`가 만든
    `reasons`)을 뽑는다. 리포트가 없으면 빈 dict — 호출부가 "근거 조회 불가"로
    정직하게 표시한다."""
    path = root / "out" / f"{on.year:04d}" / f"{on.month:02d}" / f"{on.day:02d}" / "KR_close_engine.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[str]] = {}
    for item in payload.get("close_bet_view") or []:
        sym = item.get("symbol")
        if sym:
            out[str(sym)] = [str(r) for r in (item.get("reasons") or [])]
    return out


def close_bet_recs(root: Path, on: _date) -> list[dict]:
    entries = _load_watchlist_entries(root / "data" / "watchlist.yaml")
    candidates = [e for e in entries if "CLOSE_BET" in (e.get("tags") or [])]
    if not candidates:
        return []
    reasons_by_symbol = _close_bet_reasons(root, on)
    history_root = root / "data" / "history"

    out: list[dict] = []
    for e in candidates:
        sym = str(e["symbol"])
        ref = latest_close(history_root, sym)
        detail = reasons_by_symbol.get(sym)
        reason = (
            "종가배팅(CLOSE_BET) 태그 — " + " · ".join(detail) if detail
            else "종가배팅(CLOSE_BET) 태그 — 리포트 근거 조회 불가(오늘자 마감 리포트 없음), watchlist 태그만 확인"
        )
        out.append(_rec_row(
            symbol=sym, name=e.get("name"), market="KR", kind="close_bet", reason=reason,
            ref_price=ref[0] if ref else None, ref_date=ref[1] if ref else None,
            invalidation=f"기준가 대비 -{_CLOSE_BET_STOP_PCT:g}%",
            horizon="D+5",
        ))
    return out


# ========================================================================
# (c) RSI(2) 눌림 — rsi2_dip.py와 같은 진입 규칙(과매도 + 추세 위)을 일봉에 재현
# ========================================================================

def rsi2_dip_recs(root: Path, symbols: Iterable[str]) -> list[dict]:
    """`entry_rsi`(10) 미만 + 종가가 `trend_sma_days`(200) SMA 위인 KR 종목.

    `rsi2_dip.py`의 실계좌 판정과 완전히 같지 않다 — 그 전략은 "마감 N분 전"의
    장중 근사치를 쓰고(연장 시리즈), 여기는 **완성된 마지막 일봉**만 본다. 텔레그램
    추천은 다음날 아침에 읽으므로 이 차이가 실질적 문제가 되지 않는다."""
    history_root = root / "data" / "history"
    out: list[dict] = []
    min_bars = max(_RSI2_TREND_SMA_DAYS, 5) + 1
    for sym in sorted(set(symbols)):
        closes = _read_daily_closes(history_root, sym)
        if len(closes) < min_bars:
            continue
        rsi = _wilder_rsi(closes, period=2)
        sma = _sma(closes, _RSI2_TREND_SMA_DAYS)
        rsi_today, sma_today, close_today = rsi.iloc[-1], sma.iloc[-1], closes.iloc[-1]
        if pd.isna(rsi_today) or pd.isna(sma_today):
            continue
        if not (rsi_today < _RSI2_ENTRY and close_today > sma_today):
            continue
        ts = closes.index[-1]
        ref_date = str(ts.date() if hasattr(ts, "date") else ts)
        reason = (
            f"RSI(2) {rsi_today:.1f} < {_RSI2_ENTRY:g}(과매도) · "
            f"종가 {close_today:,.2f} > SMA{_RSI2_TREND_SMA_DAYS} {sma_today:,.2f}(추세 위)"
        )
        out.append(_rec_row(
            symbol=sym, name=None, market="KR", kind="rsi2_dip", reason=reason,
            ref_price=float(close_today), ref_date=ref_date,
            invalidation=(
                f"기준가 대비 -{_RSI2_HARD_STOP_PCT:g}% 또는 RSI(2) > {_RSI2_EXIT:g} 회복 시 "
                f"청산 — 최대 {_RSI2_MAX_HOLD_DAYS}거래일 보유"
            ),
            horizon="D+5",
        ))
    return out


# ========================================================================
# (d) 오버나이트 드리프트 — 조건 없음(문헌 근거 자체가 무조건 보유), 매일 충족
# ========================================================================

def overnight_drift_recs(root: Path) -> list[dict]:
    """`overnight_drift.py`의 진입 필터는 기본 전부 비활성 — "무조건 오버나이트
    보유"가 문헌 근거 자체다(그 모듈 docstring "왜 필터 기본값이 전부 비활성인가").
    그래서 조건 판정이 없다: 매일 이 한 줄을 낸다. 심볼은 QQQ만(모듈 docstring
    "왜 overnight_drift는 QQQ만인가")."""
    history_root = root / "data" / "history"
    out: list[dict] = []
    for sym in _OVERNIGHT_DRIFT_SYMBOLS:
        ref = latest_close(history_root, sym)
        out.append(_rec_row(
            symbol=sym, name=sym, market="US", kind="overnight_drift",
            reason=(
                f"오버나이트 드리프트 조건 충족: {sym}(마감 직전 매수 → 익일 개장 직후 매도. "
                "조건부 필터 없음 — 문헌 근거가 '무조건 보유'다, overnight_drift.py 참고)"
            ),
            ref_price=ref[0] if ref else None, ref_date=ref[1] if ref else None,
            invalidation=f"익일 시가가 기준가 대비 -{_OVERNIGHT_DRIFT_STOP_PCT:g}% 이상 갭다운 시 즉시 청산",
            horizon="D+5",
        ))
    return out


# ========================================================================
# 시장별 조립
# ========================================================================

def build_recs(root: Path, market: str, on: _date) -> list[dict]:
    """시장별 추천 목록. KR = 외국인 적립 + 종가배팅 + RSI(2) 눌림. US = 오버나이트
    드리프트. `on`은 close_bet의 리포트 조회 + 향후 결정론 확장을 위해 받는다."""
    if market == "KR":
        wl_path = root / "data" / "watchlist.yaml"
        rsi2_symbols = _kr_symbols_from_watchlist(wl_path) | set(_RSI2_ALWAYS_KR_SYMBOLS)
        return (
            foreign_accumulate_recs(root)
            + close_bet_recs(root, on)
            + rsi2_dip_recs(root, rsi2_symbols)
        )
    if market == "US":
        return overnight_drift_recs(root)
    raise ValueError(f"알 수 없는 시장: {market!r}")


# ========================================================================
# 선정 원장 기록 — 캐노니컬 writer(quant.control.selections.append) 재사용.
# build_rows()는 리포트 엔진 payload 전용 스키마라 여기 맞지 않는다(속성 벡터가
# 다르다) — `_record_watch_join_selections`(quant/report/collect/ledger.py)가
# 같은 이유로 행을 직접 만들어 append()만 재사용하는 것과 같은 관례.
# ========================================================================

def to_selection_rows(recs: list[dict], today: str) -> list[dict]:
    rows: list[dict] = []
    for r in recs:
        row: dict = {
            "schema": selections.SCHEMA,
            "date": today,
            "market": r["market"],
            "producer": PRODUCER,
            "symbol": r["symbol"],
            "is_candidate": True,
            "kind": r["kind"],
            "reason": r["reason"],
            "invalidation": r["invalidation"],
            "horizon": r["horizon"],
            "outcome_filled": False,
        }
        if r.get("name"):
            row["name"] = r["name"]
        # close/close_date(2026-09-03) — outcomes.apply_outcome이 이 두 필드로
        # D+1/5/20 을 자동 채점한다(quant/control/outcomes.py base_session_date).
        # 기준가를 못 구했으면 키 자체를 생략한다(0으로 위장하지 않는다).
        if r.get("ref_price") is not None:
            row["close"] = r["ref_price"]
        if r.get("ref_date") is not None:
            row["close_date"] = r["ref_date"]
        rows.append(row)
    return rows


def write_recs(recs: list[dict], root: Path, today: str) -> int:
    rows = to_selection_rows(recs, today)
    path = root / "data" / "ledger" / "selections.jsonl"
    return selections.append(rows, path)


# ========================================================================
# 텔레그램 메시지
# ========================================================================

_MARKET_LABEL = {"KR": "한국", "US": "미국"}


def render_telegram_message(recs: list[dict], market: str, max_n: int = 8) -> str:
    label = _MARKET_LABEL.get(market, market)
    header = f"📌 수동 계좌 추천 (자동매매 아님) — {label}"
    if not recs:
        return f"{header}\n오늘은 추천 후보 없음"

    lines = [header]
    for r in recs[:max_n]:
        name = r.get("name") or r["symbol"]
        if r.get("ref_price") is not None:
            price = f"{r['ref_price']:,.0f}" + (f"({r['ref_date']} 종가)" if r.get("ref_date") else "")
        else:
            price = "기준가 없음(로컬 일봉 없음)"
        lines.append(
            f"\n[{r['kind']}] {name}({r['symbol']}) · 기준가 {price} · 지평 {r['horizon']}\n"
            f"  근거: {r['reason']}\n"
            f"  무효화: {r['invalidation']}"
        )
    if len(recs) > max_n:
        lines.append(f"\n… 외 {len(recs) - max_n}건 생략")
    return "\n".join(lines)


# ========================================================================
# 성적표 — producer manual_rec_v1 D+5 적중률/평균bp (표본 부족 시 "판단 불가")
# ========================================================================

_SCORECARD_MIN_N = 30


def scorecard_stats(rows: list[dict], producer: str = PRODUCER, horizon: int = 5) -> dict:
    """`selections.jsonl` 행 목록 → producer의 D+`horizon` 표본 통계.
    `outcome_d{horizon}_bps`가 채워진 행만 센다(None은 "아직 만기 전/조회 실패" —
    적중률 계산에서 제외, 0으로 위장하지 않는다)."""
    key = f"outcome_d{horizon}_bps"
    filled = [
        r for r in rows
        if r.get("producer") == producer and r.get(key) is not None
    ]
    n = len(filled)
    if n == 0:
        return {"n": 0, "horizon": horizon, "hit_rate": None, "mean_bp": None}
    hits = sum(1 for r in filled if float(r[key]) > 0)
    mean_bp = sum(float(r[key]) for r in filled) / n
    return {"n": n, "horizon": horizon, "hit_rate": hits / n, "mean_bp": mean_bp}


def scorecard_text(rows: list[dict], producer: str = PRODUCER, horizon: int = 5) -> str:
    stats = scorecard_stats(rows, producer, horizon)
    n = stats["n"]
    if n < _SCORECARD_MIN_N:
        return f"{producer} 성적표: 판단 불가 (D+{horizon} 표본 n={n} < {_SCORECARD_MIN_N})"
    return (
        f"{producer} 성적표(D+{horizon}, n={n}): "
        f"적중률 {stats['hit_rate'] * 100:.0f}% · 평균 {stats['mean_bp']:+.1f}bp"
    )
