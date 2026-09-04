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

## 단기반전/거래량충격 — 스윙 시그널(2026-09-03, quant-backtest 워크포워드)

`quant/analyze/swing_signals.py`의 `short_term_reversal_candidates`/
`volume_shock_candidates`(둘 다 순수 pandas)를 감싼다. 이 둘도 스윙(각 5일/10일
보유)이라 자동매매 대상이 아니다 — 위 결정과 같은 이유로 수동 계좌 추천으로만
나간다. 유니버스는 `data/state/kr_largecap_universe.json`
(`quant/collect/kr_largecap_daily.py`가 채운다, 시총≥3,000억 상위 ~300종목)에서
읽는다 — 그 파일이 없으면(백필이 아직 안 됐거나 로컬 dev 환경) 이 두 생산자는
**조용히 0건**을 낸다(에러 아님, 다른 추천 종류에는 영향 없음).

## 이 파일이 하지 않는 것

- 시세 조회(네트워크)를 하지 않는다 — `quant/adapters/`는 analyze 평면에서 임포트
  금지다. 기준가는 전부 로컬 parquet의 "마지막으로 알려진 종가"다(당일 실시간가
  아닐 수 있다 — 그래서 `ref_date`를 항상 함께 남긴다).
- 주문을 내지 않는다. `is_candidate: True`로 선정 원장에 남기고 텔레그램으로
  알릴 뿐, 매매는 소유자가 별도 계좌에서 직접 판단한다.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd
import yaml

from quant.analyze import foreign_trend, swing_signals
from quant.control import frgn_flow as frgn_flow_ledger
from quant.control import selections
from quant.core.models import market_of_symbol
from quant.core import tgfmt

logger = logging.getLogger(__name__)

PRODUCER = "manual_rec_v1"

# 기준가 조회 훅(2026-09-03, F1) — (가격, 날짜, 출처"history"|"toss") 또는 심볼을
# 못 찾으면 None. analyze 평면은 네트워크를 모르므로(모듈 docstring "이 파일이
# 하지 않는 것"), 이 콜러블은 항상 호출부(quant.apps.cli)가 주입한다 — 기본값은
# `_history_price_of`(로컬 일봉만, 네트워크 없음)라 이 모듈은 그대로 순수하다.
PriceLookup = Callable[[str], "tuple[float, str, str] | None"]

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

# 단기반전/거래량충격(2026-09-03, quant-backtest 워크포워드) — 프로듀서 ID는
# manual_rec_v1과 분리한다(과제 지시: 스코어보드가 신호별로 따로 채점해야
# 한다). 상한 5건/일(과제 지시) — 신호 강도(swing_signals가 이미 정렬)순 앞부터.
PRODUCER_STR = "manual_rec_str_v1"  # 단기반전(5일)
PRODUCER_VSP = "manual_rec_vsp_v1"  # 거래량충격(10일)
_STR_MAX_RECS = 5
_VSP_MAX_RECS = 5
_STR_KIND = "단기반전(5일)"
_VSP_KIND = "거래량충격(10일)"
# 텔레그램에 한 번만 붙이는 근거 문구(kind별) — quant-backtest 워크포워드(KR
# 일봉 2016→2026, 유니버스=시총≥3,000억+20일 중앙값 거래대금≥50억, 왕복비용
# 23bp) OOS 실측. 인샘플이 아니라는 점과 표본 정의를 항상 함께 밝힌다.
_KIND_EVIDENCE = {
    _STR_KIND: (
        "OOS 백테스트 2016→2026: 기준선 대비 +17.5bp/거래 (t=3.2) — 인샘플 아님, "
        "표본 KR 대형주(시총≥3,000억+20일 중앙값 거래대금≥50억) walk-forward"
    ),
    _VSP_KIND: (
        "OOS 백테스트 2016→2026: 기준선 대비 +51bp/거래 (t=2.95) — 인샘플 아님, "
        "표본 KR 대형주(시총≥3,000억+20일 중앙값 거래대금≥50억) walk-forward"
    ),
}


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

def _read_daily_bars(history_root: Path, symbol: str, months: int = 14) -> pd.DataFrame:
    """`symbol`의 최근 `months`개월치 일봉 전체 컬럼(OHLCV, 오름차순). 데이터
    없으면 빈 DataFrame.

    `months=14`는 RSI(2) 트렌드 필터(SMA 200 영업일 ≈ 9.5개월)에 워밍업 여유를
    더한 값이다. 파일이 없거나 깨져도 예외를 던지지 않는다 — 추천 하나 못 만드는
    것과 전체 명령이 죽는 것은 전혀 다르다.

    (2026-09-03) 원래 종가만 돌려주던 `_read_daily_closes`에서 분리했다 —
    `swing_signals.volume_shock_candidates`가 open/volume도 필요해서다. 파티션
    읽기 로직은 하나뿐이고, `_read_daily_closes`가 이 함수를 감싼다."""
    d = history_root / symbol / "1d"
    parts = sorted(d.glob("*/*.parquet"))
    if not parts:
        return pd.DataFrame()
    frames = []
    for p in parts[-months:]:
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001 — 파손된 파티션 하나가 전체를 죽이면 안 된다
            continue
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index()


def _read_daily_closes(history_root: Path, symbol: str, months: int = 14) -> pd.Series:
    """`symbol`의 최근 `months`개월치 종가(오름차순). 데이터 없으면 빈 Series."""
    df = _read_daily_bars(history_root, symbol, months=months)
    if df.empty or "close" not in df.columns:
        return pd.Series(dtype=float)
    return df["close"].dropna()


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


def _history_price_of(history_root: Path) -> PriceLookup:
    """`price_of` 기본 구현(2026-09-03, F1) — 로컬 일봉만 본다(네트워크 없음).

    호출부(`quant.apps.cli.cmd_manual_recs`)가 Toss 실시세 폴백을 얹은 콜러블을
    따로 주입하지 않으면(테스트 등) 이 구현으로 떨어진다 — `price_source`는
    항상 "history"."""
    def _fn(symbol: str) -> tuple[float, str, str] | None:
        ref = latest_close(history_root, symbol)
        if ref is None:
            return None
        return ref[0], ref[1], "history"
    return _fn


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
    price_source: str | None = None,
) -> dict:
    return {
        "symbol": symbol, "name": name, "market": market, "kind": kind,
        "reason": reason, "ref_price": ref_price, "ref_date": ref_date,
        "invalidation": invalidation, "horizon": horizon, "price_source": price_source,
    }


# ========================================================================
# (a) 외국인 적립 추세 — frgn_accumulate가 쓰던 classify()를 그대로 재사용
# ========================================================================

def foreign_accumulate_recs(
    root: Path, symbols: Iterable[str] | None = None, days: int = 20,
    price_of: PriceLookup | None = None,
) -> list[dict]:
    """외국인 수급 라벨이 `foreign_trend.LABEL_INFLOW`(매수 시그널/재유입)인 KR
    종목 추천. `symbols`를 안 주면 `frgn_flow.jsonl`에 기록이 있는 전 종목을 본다
    (own_brief처럼 그날 리포트 랭킹에 갇히지 않는다 — 이 레인은 매일 새로 뽑는
    자동 편입이 아니라, 이미 쌓인 수급 이력에서 사람이 볼 후보를 고르는 것이다).

    2026-09-03(F1) — `frgn_flow.jsonl`은 전 종목을 쌓지만 로컬 일봉
    (`data/history`)은 그중 일부뿐이라, 기준가 없는 후보가 실측 12/26(46%)에
    달했다(감사 B8) — 기준가 없는 행은 `outcomes`가 채점할 수 없으므로 텔레그램에
    보내봤자 판단 불가능한 추천이다. `price_of`(기본: 로컬 일봉만)가 그래도
    가격을 못 주면 **추천 자체를 드롭한다** — 채점 불가능한 추천은 보내지
    않는다는 원칙(모듈 docstring)을 라벨 필터만이 아니라 가격 유무에도 적용."""
    flow_path = root / "data" / "ledger" / "frgn_flow.jsonl"
    history_root = root / "data" / "history"
    get_price = price_of or _history_price_of(history_root)
    candidates = sorted(symbols) if symbols is not None else sorted(_distinct_symbols(flow_path))

    out: list[dict] = []
    for sym in candidates:
        series = frgn_flow_ledger.load_series(flow_path, sym, days=days)
        if not series:
            continue
        info = foreign_trend.classify(series)
        if info["label"] != foreign_trend.LABEL_INFLOW:
            continue
        ref = get_price(sym)
        if ref is None:
            logger.warning(
                "frgn_accumulate: %s 기준가 조회 실패(history+toss 모두) — 추천 드롭"
                "(채점 불가능한 추천은 보내지 않는다)", sym,
            )
            continue
        price, ref_date, source = ref
        # F3(2026-09-03) — 분류가 실제로 쓴 "최근 run" 숫자를 보여준다. 20일
        # 누계(`residual`)만 찍으면 최근 run이 재유입이어도 누계가 여전히 음수인
        # 종목에서 "매수 시그널(재유입)"과 부호가 반대인 문장이 나갔다(감사 #3,
        # 004990 실측 20일 누계 -112,519주인데도 재유입 라벨). `foreign_trend.classify`는
        # 자르지 않는다(F3 지시) — 여기서 같은 run 계산을 재사용해 문구만 고친다.
        runs = foreign_trend._runs(series)
        _, last_len, last_sum = runs[-1]
        reason = (
            f"외국인 {info['label']} — 최근 {last_len}일 순매수 {last_sum:+,.0f}주 "
            f"({info['days']}일 누계 {info['residual']:+,.0f}주)"
            + (" · 기관 동반매수" if info["inst_follows"] else "")
        )
        out.append(_rec_row(
            symbol=sym, name=None, market="KR", kind="frgn_accumulate", reason=reason,
            ref_price=price, ref_date=ref_date, price_source=source,
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


def close_bet_recs(root: Path, on: _date, price_of: PriceLookup | None = None) -> list[dict]:
    """2026-09-03(F1) — watchlist CLOSE_BET 태그 종목도 로컬 일봉이 없을 수
    있다(frgn_accumulate와 같은 이유, `foreign_accumulate_recs` docstring
    참고). `price_of`가 가격을 못 주면 그 종목은 드롭한다."""
    entries = _load_watchlist_entries(root / "data" / "watchlist.yaml")
    candidates = [e for e in entries if "CLOSE_BET" in (e.get("tags") or [])]
    if not candidates:
        return []
    reasons_by_symbol = _close_bet_reasons(root, on)
    history_root = root / "data" / "history"
    get_price = price_of or _history_price_of(history_root)

    out: list[dict] = []
    for e in candidates:
        sym = str(e["symbol"])
        ref = get_price(sym)
        if ref is None:
            logger.warning(
                "close_bet: %s 기준가 조회 실패(history+toss 모두) — 추천 드롭"
                "(채점 불가능한 추천은 보내지 않는다)", sym,
            )
            continue
        price, ref_date, source = ref
        detail = reasons_by_symbol.get(sym)
        reason = (
            "종가배팅(CLOSE_BET) 태그 — " + " · ".join(detail) if detail
            else "종가배팅(CLOSE_BET) 태그 — 리포트 근거 조회 불가(오늘자 마감 리포트 없음), watchlist 태그만 확인"
        )
        out.append(_rec_row(
            symbol=sym, name=e.get("name"), market="KR", kind="close_bet", reason=reason,
            ref_price=price, ref_date=ref_date, price_source=source,
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
# (e)/(f) 단기반전(5일) / 거래량충격(10일) — swing_signals.py 감싸기
# (2026-09-03, quant-backtest 워크포워드 — 모듈 docstring 참고)
# ========================================================================

def _largecap_symbols(root: Path) -> list[str]:
    """`quant.collect.kr_largecap_daily`가 채우는 시총 상위 유니버스 캐시에서
    심볼만 뽑는다. `quant.collect`를 임포트하지 않는다 — JSON 하나 읽는 데 평면을
    가로지를 이유가 없다(`_close_bet_reasons`가 리포트 엔진 JSON을 직접 읽는
    것과 같은 관례). 파일이 없거나 깨졌으면 빈 리스트 — 두 생산자가 조용히
    0건을 내게 한다(모듈 docstring)."""
    path = root / "data" / "state" / "kr_largecap_universe.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        return []
    return [str(s["symbol"]) for s in symbols if isinstance(s, dict) and s.get("symbol")]


def _largecap_daily_bars(root: Path) -> dict[str, pd.DataFrame]:
    """대형주 유니버스 심볼별 일봉(OHLCV) — `swing_signals`에 그대로 넘긴다.
    데이터가 없는 심볼(아직 백필 전)은 빈 DataFrame이라 `swing_signals`가
    알아서 건너뛴다."""
    history_root = root / "data" / "history"
    return {sym: _read_daily_bars(history_root, sym) for sym in _largecap_symbols(root)}


def short_term_reversal_recs(root: Path) -> list[dict]:
    """대형주 유니버스에서 직전 5일 수익률 하위 10분위 후보(최대 5건, 신호
    강도순 — `swing_signals`가 이미 정렬). 기준가는 신호 계산에 쓴 바로 그
    종가라 항상 존재한다(rsi2_dip과 같은 이유로 `price_of` 불필요)."""
    bars = _largecap_daily_bars(root)
    candidates = swing_signals.short_term_reversal_candidates(bars)[:_STR_MAX_RECS]
    return [
        _rec_row(
            symbol=c["symbol"], name=None, market="KR", kind=_STR_KIND,
            reason=f"최근 5일 수익률 {c['value'] * 100:+.1f}% (대형주 유니버스 하위 10분위)",
            ref_price=c["ref_price"], ref_date=c["ref_date"], price_source="history",
            invalidation=c["invalidation"], horizon=c["horizon"],
        )
        for c in candidates
    ]


def volume_shock_recs(root: Path) -> list[dict]:
    """대형주 유니버스에서 거래대금 20일 중앙값 대비 2.5배 이상 + 상승마감
    후보(최대 5건, 배율 내림차순 — `swing_signals`가 이미 정렬)."""
    bars = _largecap_daily_bars(root)
    candidates = swing_signals.volume_shock_candidates(bars)[:_VSP_MAX_RECS]
    return [
        _rec_row(
            symbol=c["symbol"], name=None, market="KR", kind=_VSP_KIND,
            reason=f"거래대금 20일 중앙값 대비 {c['value']:.1f}배 · 상승마감",
            ref_price=c["ref_price"], ref_date=c["ref_date"], price_source="history",
            invalidation=c["invalidation"], horizon=c["horizon"],
        )
        for c in candidates
    ]


# ========================================================================
# 시장별 조립
# ========================================================================

def build_recs(root: Path, market: str, on: _date, price_of: PriceLookup | None = None) -> list[dict]:
    """시장별 추천 목록. KR = 외국인 적립 + 종가배팅 + RSI(2) 눌림 + 단기반전(5일)
    + 거래량충격(10일). US = 오버나이트 드리프트. `on`은 close_bet의 리포트
    조회 + 향후 결정론 확장을 위해 받는다.

    `price_of`(2026-09-03, F1)는 frgn_accumulate/close_bet에만 넘어간다 —
    rsi2_dip과 단기반전/거래량충격은 신호 계산에 쓴 바로 그 일봉 종가를
    기준가로 쓰므로(항상 존재) 별도 조회가 필요 없고, overnight_drift는 US
    고정 지수 ETF(QQQ) 하나뿐이라 로컬 일봉 결측이 실측된 적 없다(감사 B8은
    KR 12/26에 한정된 문제)."""
    if market == "KR":
        wl_path = root / "data" / "watchlist.yaml"
        rsi2_symbols = _kr_symbols_from_watchlist(wl_path) | set(_RSI2_ALWAYS_KR_SYMBOLS)
        return (
            foreign_accumulate_recs(root, price_of=price_of)
            + close_bet_recs(root, on, price_of=price_of)
            + rsi2_dip_recs(root, rsi2_symbols)
            + short_term_reversal_recs(root)
            + volume_shock_recs(root)
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

# kind → producer(2026-09-03) — 단기반전/거래량충격만 별도 producer id를 쓴다
# (과제 지시: "distinct so the scorecard separates them"). 나머지 4종(kind가
# 여기 없는 경우)은 그대로 PRODUCER(manual_rec_v1)로 떨어진다 — 기존 행/테스트와
# 하위 호환.
_KIND_TO_PRODUCER = {
    _STR_KIND: PRODUCER_STR,
    _VSP_KIND: PRODUCER_VSP,
}


def to_selection_rows(recs: list[dict], today: str) -> list[dict]:
    rows: list[dict] = []
    for r in recs:
        row: dict = {
            "schema": selections.SCHEMA,
            "date": today,
            "market": r["market"],
            "producer": _KIND_TO_PRODUCER.get(r["kind"], PRODUCER),
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
        # price_source(2026-09-03, F1) — "history"(로컬 일봉) | "toss"(실시세
        # 폴백). 기준가가 없으면(드롭되지 않고 여기까지 온 예외적 호출부) 같이 없다.
        if r.get("price_source") is not None:
            row["price_source"] = r["price_source"]
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


def render_telegram_message(recs: list[dict], market: str, max_n: int = 8,
                            report_url: str | None = None) -> str:
    """텔레그램 HTML 메시지(tgfmt, 2026-09-04) — 종목·종류·기준가·무효화·지평을
    정렬된 표로 보여준다. `report_url`이 있으면(그날의 회사 리포트 HTML) 맨
    끝에 링크를 붙인다."""
    label = _MARKET_LABEL.get(market, market)
    header = tgfmt.b(f"📌 수동 계좌 추천 (자동매매 아님) — {label}")
    if not recs:
        return tgfmt.compose(header, ["오늘은 추천 후보 없음"])

    shown = recs[:max_n]
    shown_evidence: set[str] = set()
    evidence_lines: list[str] = []
    rows = []
    for r in shown:
        # 근거 문구(2026-09-03) — kind별로 한 번만, 그 kind의 첫 추천 앞에 붙인다
        # (_KIND_EVIDENCE에 없는 kind는 그냥 스킵 — 기존 4종은 이 문구가 없다).
        kind = r["kind"]
        if kind in _KIND_EVIDENCE and kind not in shown_evidence:
            evidence_lines.append(tgfmt.esc(f"— {kind} 근거: {_KIND_EVIDENCE[kind]}"))
            shown_evidence.add(kind)
        name = r.get("name") or r["symbol"]
        if r.get("ref_price") is not None:
            # price_source(F1) — "toss"면 로컬 일봉이 없어 실시세로 대체한
            # 기준가라는 걸 라벨로 구분한다("종가"라고 하면 거짓말이 된다).
            date_label = "실시간" if r.get("price_source") == "toss" else "종가"
            price = f"{r['ref_price']:,.0f}" + (f"({r['ref_date']} {date_label})" if r.get("ref_date") else "")
        else:
            # F2(2026-09-03) — 이전엔 "기준가 {price}"의 {price} 자리에 "기준가
            # 없음..."을 또 넣어 "기준가 기준가 없음(...)"으로 중복 렌더링됐다.
            # F1 이후 가격 없는 추천은 애초에 드롭돼 이 분기에 도달하지 않지만,
            # (테스트 등) 직접 호출 경로를 위해 문구 자체는 정확하게 고쳐둔다.
            price = "없음(기준가 조회 실패)"
        rows.append((f"{name}({r['symbol']})", kind, price, str(r["invalidation"]), str(r["horizon"])))

    sections: list[str] = []
    if evidence_lines:
        sections.append("\n".join(evidence_lines))
    sections.append(tgfmt.pre(tgfmt.table(["종목", "종류", "기준가", "무효화", "지평"], rows)))

    footer = None
    if len(recs) > max_n:
        footer = tgfmt.esc(f"… 외 {len(recs) - max_n}건 생략")
    if report_url:
        link = tgfmt.link("전체 리포트", report_url)
        footer = f"{footer}\n{link}" if footer else link

    return tgfmt.compose(header, sections, footer)


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
