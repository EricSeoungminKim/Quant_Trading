"""RegimeProvider — 하루 1회 국면(방어/중립/공격) 판단 + risk_multiplier 캐시.

**하루 1회, 세션 시작 전에만** refresh()로 네트워크 I/O를 한다. risk_multiplier()는
캐시된 상태(메모리 → data/state/regime.json)만 읽고 절대 네트워크를 부르지 않는다 —
거래 사이클 핫패스에서 안전하게 호출할 수 있는 유일한 진입점이다(루트 CLAUDE.md
"거래 핫패스에 LLM/네트워크 호출 금지").

판단 불가(지표를 하나도 못 구함)면 공격도 방어도 아닌 중립(1.0)으로 떨어지고
RegimeState.degraded=True로 그 사실을 남긴다 — 조용한 기본값이 되지 않도록 호출부가
degraded를 보고 로그/알림을 걸 수 있게 한다. 알림 배선(Notifier 연결)은 이 모듈
범위 밖이다 — 오케스트레이터가 붙인다.

규칙 기반, 해석 가능(ML 없음). **이 점수 규칙이 수익을 낸다는 증거는 없다** —
목적은 명백한 하락 국면에서 노출을 줄이는 방어이지 알파가 아니다.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from quant.trade.regime.indicators import (
    IndicatorResult,
    bitcoin_score,
    bond_yield_score,
    kospi_score,
    qqq_trend_score,
    qqq_volatility_score,
    vix_stress,
)
from quant.trade.regime.interfaces import BitcoinPriceAdapter, MarketIndicatorClient
from quant.trade.regime.models import RegimeState

logger = logging.getLogger(__name__)

DEFAULT_MULTIPLIERS = {"defensive": 0.5, "neutral": 1.0, "aggressive": 1.3}
DEFAULT_AGGRESSIVE_MIN_SCORE = 2  # 지표 점수 합 >= 이 값 → aggressive
DEFAULT_DEFENSIVE_MAX_SCORE = -2  # 지표 점수 합 <= 이 값 → defensive
# 유효 지표(조회 성공) 개수가 이 값 미만이면 점수 합산 전에 강제로 neutral+degraded
# 로 떨어뜨린다. settings.yaml의 regime.min_valid_indicators 주석에 근거(2026-08-18
# 실측 — US 국면이 5개 지표 중 2개만으로 aggressive에 도달)와 한계를 적어 뒀다.
DEFAULT_MIN_VALID_INDICATORS = 2

# ── 2026-08-19 추가: 공격(aggressive) 승격 전용 정보 요건 (비대칭 설계) ──────
#
# min_valid_indicators(위)만으로는 08-18 재발을 못 막는다 — 그 사고는 정확히
# "유효 지표 2개"였고 min_valid_indicators 기본값도 2라서 문턱을 그대로 통과했다.
# 여기 아래 두 게이트는 **aggressive 로 올라갈 때만** 추가로 적용한다(방어는
# 부분 정보로도 그대로 발동 — "모르면 작게, 절대 크게 가지 않는다"는 비대칭이
# 이 파일의 핵심 설계 결정이다). 점수 합이 aggressive 임계를 넘어도 아래 둘 중
# 하나라도 미달이면 neutral로 강등하고 degraded=True 로 남긴다.
#
# 2026-08-19 수정: 유효 지표 하한을 절대값(3)에서 **로스터 상대 비율**로 바꿨다.
# 절대값 3은 US(로스터 5개 지표)를 기준으로 잡은 값인데, KR은 지표가 구조적으로
# kr_trend/kr_flow 2개뿐이라 3을 영원히 못 채운다 — 의도한 안전장치가 아니라
# "KR은 절대 aggressive에 못 간다"는 부작용이었다. 로스터 크기(그 시장 계산
# 함수가 실제로 만든 IndicatorResult 총 개수, 성공+실패 합)에 비율을 곱해 올림한
# 값을 하한으로 쓰면 US(ceil(5*0.6)=3, 08-18 재현 케이스는 여전히 차단)와
# KR(ceil(2*0.6)=2, 둘 다 유효하면 정상 발동) 양쪽에 공정하게 적용된다.
DEFAULT_AGGRESSIVE_MIN_VALID_RATIO = 0.6  # 로스터 크기 대비 비율 (ceil로 올림)
DEFAULT_AGGRESSIVE_MIN_SOURCES = 2  # 서로 다른 원천(source) 최소 개수

# 지표 → 원천(source) 매핑. **같은 자산에서 파생된 두 지표는 원천 다양성 관점에서
# 하나로 센다** — qqq_trend/qqq_volatility는 둘 다 QQQ 가격에서만 나온다. 상관된
# 신호 2개가 독립적인 표 2개인 것처럼 카운트되면 1.5배 레버리지를 정당화하지
# 못하는 근거로 정당화하게 된다. KR도 동일 원칙: kr_trend(KODEX200 추세)와
# kr_flow(외국인+기관 수급)는 서로 다른 원천이라 각각 1개씩 센다.
INDICATOR_SOURCE: dict[str, str] = {
    "qqq_trend": "QQQ",
    "qqq_volatility": "QQQ",
    "us_bond_yield": "US_BOND_YIELD",  # 2026-08-28: KR_BOND_YIELD(항상 None)에서 개명 — indicators.bond_yield_score 참고
    "kospi": "KOSPI",
    "bitcoin": "BITCOIN",
    "kr_trend": "KR_TREND",
    "kr_flow": "KR_FLOW",
}

# QQQ 일봉이 이보다 낡으면 지표에서 제외한다(집계 제외 = score None).
#
# 왜 6일인가: 정상적으로 벌어질 수 있는 최악의 공백을 재본 값이다. 목·금 연휴 뒤
# KST 월요일 22:10(US 개장 전 refresh)이면 마지막 완성봉은 그 전 수요일이라 ~5.4일
# 벌어진다. 6일이면 그걸 오탐하지 않는다 — **오탐하는 가드는 꺼지고, 꺼진 가드는
# 없는 가드다.** 2026-08-13 실측 사고(마지막 봉 07-31, 13일 경과)는 여유롭게 잡힌다.
STALE_DAILY_BARS_AFTER = timedelta(days=6)

# ── 2026-09-06 추가: VIX 스트레스 게이트(방어 전용) ──────────────────────────
#
# quant-backtest results/letf/SUMMARY_vix_regime.md(사전등록 리서치, report-only):
# VIX 레벨/20일선 대비가 기존 qqq_volatility_score("실현변동성 vs 60일" 비율)와
# 상관은 있지만(Spearman 0.27~0.36) 겹치지는 않고, 고-VIX일의 72%가 기존 변동성
# 플래그에 안 걸린다. 기간구조(VIX3M)는 채택하지 않는다 — 같은 리서치에서 야후
# 공급이 2026-07-17 이후 끊긴 게 실측됐고(라이브 피드로 못 씀), 메타라벨
# 피처중요도에서도 기여가 없었다(MDA 음수).
#
# **방어 전용(defensive-only)이다** — risk_multiplier를 절대 올리지 않는다.
# 그래서 이 지표는 다른 지표들(+1/0/-1, _finalize의 점수 합산)에 넣지 않고
# _apply_vix_gate에서 후처리로만 적용한다 — 점수에 넣으면 aggressive_min_valid_ratio
# 로스터 크기(len(results))가 바뀌어 기존 게이트 동작이 조용히 흔들린다.
DEFAULT_VIX_ENABLED = True
DEFAULT_VIX_LEVEL_MAX = 25.0
DEFAULT_VIX_SMA20_RATIO_MAX = 1.20

# VIX 일봉이 이보다 낡으면(거래일 기준) 지표를 건너뛴다 — 절대 거래를 막지
# 않는다("Missing/stale VIX data → indicator skipped ... never blocking"). 달력일이
# 아니라 거래일(세션) 수로 잰다 — QQQ의 STALE_DAILY_BARS_AFTER(달력일 6일)와
# 다른 단위를 쓰는 이유는 연구 지시가 "3세션"으로 명시했기 때문이다. 백필
# 스크립트(server/scripts/backfill_us_daily.sh)도 같은 상수를 임포트해서 쓴다 —
# 숫자를 따로 적으면 언젠가 갈라지고, 갈라진 쪽이 조용한 쪽이 된다(QQQ 상수와
# 같은 원칙, 위 STALE_DAILY_BARS_AFTER 주석 참고).
STALE_VIX_SESSIONS_AFTER = 3

_KST = ZoneInfo("Asia/Seoul")


def _default_now() -> datetime:
    return datetime.now(_KST)


class RegimeProvider:
    """settings.yaml의 `regime:` 블록(risk_multipliers 등)을 읽고, 로컬 QQQ 일봉
    파티션 + (주입된) Toss market-indicator 클라이언트 + (주입된) 비트코인 어댑터로
    국면을 계산한다. 두 클라이언트 모두 None이면 로컬 지표만으로 판단한다."""

    def __init__(
        self,
        settings: dict | None = None,
        indicator_client: MarketIndicatorClient | None = None,
        bitcoin_adapter: BitcoinPriceAdapter | None = None,
        history_dir: str | Path = "data/history",
        state_path: str | Path = "data/state/regime.json",
        now_fn: Callable[[], datetime] = _default_now,
        flow_client: object | None = None,
    ):
        regime_cfg = (settings or {}).get("regime", {})
        self._multipliers: dict[str, float] = regime_cfg.get("risk_multipliers", DEFAULT_MULTIPLIERS)
        self._aggressive_min = regime_cfg.get("aggressive_min_score", DEFAULT_AGGRESSIVE_MIN_SCORE)
        self._defensive_max = regime_cfg.get("defensive_max_score", DEFAULT_DEFENSIVE_MAX_SCORE)
        self._min_valid = regime_cfg.get("min_valid_indicators", DEFAULT_MIN_VALID_INDICATORS)
        # aggressive 승격 전용 게이트(비대칭 설계) — defensive/neutral 판정에는 적용 안 함.
        # 하위호환: settings에 옛 절대값 aggressive_min_valid가 있으면 그 값을 그대로
        # 하한으로 쓰고 aggressive_min_valid_ratio는 무시한다 — 운영자가 특정 시장만
        # 조이고 싶을 때(예: KR도 US처럼 3개를 요구)의 탈출구다(2026-08-19).
        self._aggressive_min_valid_abs = regime_cfg.get("aggressive_min_valid")
        self._aggressive_min_valid_ratio = regime_cfg.get(
            "aggressive_min_valid_ratio", DEFAULT_AGGRESSIVE_MIN_VALID_RATIO
        )
        self._aggressive_min_sources = regime_cfg.get("aggressive_min_sources", DEFAULT_AGGRESSIVE_MIN_SOURCES)
        # VIX 스트레스 게이트 설정(US 전용, 방어 전용) — settings.yaml의
        # regime.us.vix가 기본값 위에 병합된다. KR에는 적용하지 않는다(VIX는
        # 미국 지수 변동성이라 KR 국면과 무관 — docs/adr/0009의 KR/US 분리 원칙).
        vix_cfg_override = ((regime_cfg.get("us") or {}).get("vix") or {})
        self._vix_cfg = {
            "enabled": DEFAULT_VIX_ENABLED,
            "level_max": DEFAULT_VIX_LEVEL_MAX,
            "sma20_ratio_max": DEFAULT_VIX_SMA20_RATIO_MAX,
            **vix_cfg_override,
        }
        self._indicator_client = indicator_client
        self._bitcoin_adapter = bitcoin_adapter
        self._history_dir = Path(history_dir)
        self._state_path = Path(state_path)
        self._now_fn = now_fn
        self._state: RegimeState | None = None
        # KR 국면용 (2026-08-10): candles/investor_trading을 가진 클라이언트(TossClient
        # duck-type). None이면 KR은 항상 중립 — 기존 동작과 동일해 롤백 리스크 없음.
        self._flow_client = flow_client
        self._kr_state: RegimeState | None = None

    # ------------------------------------------------------------------ 핫패스 안전 경로

    def risk_multiplier(self, market: str = "US") -> float:
        """네트워크 호출 절대 없음. refresh()가 남긴 값(메모리 또는 캐시파일)만 읽는다.
        아무 것도 없으면(최초 실행, refresh 전) 중립(1.0).

        market="KR"이면 KR 국면(KOSPI 프록시 추세 + 투자자 수급)을 쓴다 — KR 세션을
        미국 지수로 판단하던 문제의 해소(2026-08-10). KR 상태가 없으면 중립."""
        if market == "KR":
            if self._kr_state is None:
                self._load_cache()  # markets 캐시에 KR이 있으면 함께 복원된다
            if self._kr_state is not None:
                return self._kr_state.risk_multiplier
            return self._multipliers.get("neutral", 1.0)
        state = self._cached_state()
        if state is None:
            return self._multipliers.get("neutral", 1.0)
        return state.risk_multiplier

    def kr_state(self) -> RegimeState | None:
        return self._kr_state

    def current_state(self) -> RegimeState | None:
        """네트워크 호출 없음. 메모리 → 캐시파일 순으로 조회, 둘 다 없으면 None."""
        return self._cached_state()

    def _cached_state(self) -> RegimeState | None:
        if self._state is not None:
            return self._state
        self._state = self._load_cache()
        return self._state

    # ------------------------------------------------------------------ 하루 1회 계산 경로(네트워크 I/O)

    def refresh(self, force: bool = False) -> RegimeState:
        """세션 시작 전에만 호출할 것. 캐시 날짜가 오늘(Asia/Seoul)이면 재계산하지
        않는다 — force=True면 무시하고 재계산."""
        today = self._now_fn().astimezone(_KST).date()
        cached = self._load_cache()
        if cached is not None and not force and self._is_same_day(cached, today):
            self._state = cached
            return cached  # KR 상태는 _load_cache가 markets 키에서 함께 복원한다

        state = self._compute()
        self._state = state
        self._kr_state = self._compute_kr()
        self._save_cache(state)
        return state

    @staticmethod
    def _is_same_day(state: RegimeState, today: date) -> bool:
        return state.computed_at.astimezone(_KST).date() == today

    def _compute(self) -> RegimeState:
        results: list[IndicatorResult] = [
            *self._local_indicators(),
            self._bond_yield_indicator(),
            self._kospi_indicator(),
            self._bitcoin_indicator(),
        ]
        state = self._finalize(results, self._now_fn())
        return self._apply_vix_gate(state, results)

    def _apply_vix_gate(self, state: RegimeState, results: list[IndicatorResult]) -> RegimeState:
        """VIX 스트레스 후처리 게이트(방어 전용, US만) — `_finalize`의 점수 합산
        **뒤에** 적용한다. 점수에 섞지 않는 이유는 위 STALE_VIX_SESSIONS_AFTER
        주석 참고.

        - 스트레스 아님/판단 불가(데이터 없음·낡음·비활성화): 아무것도 바꾸지
          않는다 — reasons에 사유 한 줄만 더한다. **절대 거래를 막지 않는다.**
        - 스트레스: aggressive는 neutral로 강등(공격 금지) — 무조건 적용.
        - 스트레스 + 기존 변동성 위험회피 신호(qqq_volatility_score == -1,
          SUMMARY_vix_regime.md가 "기존 vol 플래그"라 부르는 그 지표)까지 함께
          뜨면 defensive로 강등 — 서로 다른 두 지표가 독립적으로 위험회피에
          동의했을 때만 방어로 간다(비대칭 설계: 방어 강화는 절대 raise하지
          않으므로 조건을 좁게 잡을 이유가 없다 — 정보가 있으면 쓴다).
        """
        vix_reason, stress = self._vix_indicator()
        reasons = [*state.reasons, vix_reason]
        label = state.label
        if stress:
            if label == "aggressive":
                label = "neutral"
            vol_score = next((r.score for r in results if r.name == "qqq_volatility"), None)
            if vol_score == -1:
                label = "defensive"
        if label == state.label:
            return replace(state, reasons=reasons)
        return replace(
            state, label=label, risk_multiplier=self._multipliers.get(label, 1.0), reasons=reasons,
        )

    def _finalize(self, results: list[IndicatorResult], computed_at: datetime) -> RegimeState:
        """점수 합산 + 두 단계 게이트를 적용해 RegimeState 를 만든다. US/KR 공용
        (2026-08-19, 규칙은 시장 무관 공통 — CLAUDE.md "KR/US 대칭 적용" 지시).

        1단계(기존, 방향 무관): 유효 지표가 min_valid_indicators 미만이면 점수를
        아예 합산하지 않고 neutral+degraded — "판단 자체를 못 함".
        2단계(신규, aggressive 전용): 점수 합이 aggressive 임계를 넘어도 유효
        지표 수가 하한(로스터 크기 × aggressive_min_valid_ratio, 올림 — 단
        settings에 절대값 aggressive_min_valid가 있으면 그 값 그대로) 미만이거나
        서로 다른 원천이 aggressive_min_sources 미만이면 neutral로 강등 —
        defensive/neutral 판정에는 적용하지 않는다(방어는 부분 정보로도 그대로
        발동, 비대칭 설계).
        """
        available = [r for r in results if r.score is not None]
        reasons = [r.reason for r in results]

        if len(available) < self._min_valid:
            logger.warning(
                "regime: 유효 지표 %d개 (%d개 미만) — 중립으로 강제 처리",
                len(available), self._min_valid,
            )
            return RegimeState(
                label="neutral",
                risk_multiplier=self._multipliers.get("neutral", 1.0),
                reasons=reasons,
                computed_at=computed_at,
                degraded=True,
            )

        total = sum(r.score for r in available)
        if total >= self._aggressive_min:
            label = "aggressive"
        elif total <= self._defensive_max:
            label = "defensive"
        else:
            label = "neutral"

        degraded = False
        if label == "aggressive":
            n_sources = len({INDICATOR_SOURCE.get(r.name, r.name) for r in available})
            # 로스터 크기 = 이 시장의 계산 함수(_compute/_compute_kr)가 실제로 만든
            # IndicatorResult 총 개수(성공+실패). 하드코딩하지 않는다 — 지표를
            # 추가/제거해도(예: US에 6번째 지표 추가) 자동으로 따라간다.
            roster_size = len(results)
            if self._aggressive_min_valid_abs is not None:
                min_valid_floor = self._aggressive_min_valid_abs  # 하위호환: 절대값 오버라이드
            else:
                min_valid_floor = math.ceil(roster_size * self._aggressive_min_valid_ratio)
            if len(available) < min_valid_floor or n_sources < self._aggressive_min_sources:
                reasons = [
                    *reasons,
                    f"공격 강등: 유효 지표 {len(available)}개(요건 {min_valid_floor}, "
                    f"로스터 {roster_size}개) / 원천 {n_sources}개(요건 "
                    f"{self._aggressive_min_sources}) 미충족 — "
                    "공격 대신 중립 유지(모르면 작게, 크게 가지 않는다)",
                ]
                label = "neutral"
                degraded = True

        return RegimeState(
            label=label,
            risk_multiplier=self._multipliers.get(label, 1.0),
            reasons=reasons,
            computed_at=computed_at,
            degraded=degraded,
        )

    def _compute_kr(self) -> RegimeState:
        """KR 국면 — KOSPI 프록시(069500 일봉) 추세 + KRX 투자자 수급, 각 +1/0/-1.

        지표 2개뿐이라 aggressive_min/defensive_max(기본 ±2)는 둘 다 같은 방향일
        때만 중립을 벗어난다 — 보수적 설계. 클라이언트 없음/전부 실패 → 중립(degraded).
        지표 1개만 성공해도 min_valid_indicators(기본 2) 미만이면 마찬가지로
        degraded=True 중립.

        점수 합산·게이트 판정은 `_finalize`(US와 공용, 2026-08-19)로 넘긴다 —
        2026-08-19 수정: aggressive 승격의 유효 지표 하한이 절대값(3, US 5지표
        풀 기준)에서 **로스터 상대 비율**(aggressive_min_valid_ratio, 기본 0.6)로
        바뀌면서 KR(로스터 2)도 ceil(2*0.6)=2 — 즉 kr_trend/kr_flow 둘 다 유효하고
        방향이 일치하면 기본 설정으로도 aggressive에 도달한다(이전엔 절대값 3을
        영원히 못 채워 구조적으로 봉쇄돼 있었다). 원천 다양성 요건
        (aggressive_min_sources, 기본 2)은 kr_trend(KR_TREND)/kr_flow(KR_FLOW)가
        이미 서로 다른 원천이라 항상 충족된다.
        """
        computed_at = self._now_fn()
        if self._flow_client is None:
            return RegimeState(
                label="neutral", risk_multiplier=self._multipliers.get("neutral", 1.0),
                reasons=["KR: flow_client 미주입 — 중립"], computed_at=computed_at, degraded=True,
            )
        results: list[IndicatorResult] = []

        try:  # 추세: KODEX 200 종가 vs 20일 이평 (±0.5% 밴드)
            daily = self._flow_client.candles("069500", interval="day", count=30)
            close = float(daily["close"].iloc[-1])
            sma20 = float(daily["close"].tail(20).mean())
            s = 1 if close > sma20 * 1.005 else (-1 if close < sma20 * 0.995 else 0)
            results.append(IndicatorResult(
                "kr_trend", s, f"KR 추세: KODEX200 {close:,.0f} vs 20일 이평 {sma20:,.0f} ({s:+d})"
            ))
        except Exception as e:  # noqa: BLE001
            results.append(IndicatorResult(
                "kr_trend", None, f"KR 추세 조회 실패 — 지표 제외: {type(e).__name__}"
            ))

        try:  # 수급: KOSPI+KOSDAQ 외국인+기관 순매수 (±0.3조 임계 — watch_scorer와 동일)
            net = 0
            for mkt in ("KOSPI", "KOSDAQ"):
                for rec in (self._flow_client.investor_trading(mkt, "1d", 2).get("records") or [])[:2]:
                    f_amt, i_amt = rec.get("foreigner") or {}, rec.get("institution") or {}
                    day_net = (
                        int(f_amt.get("buyAmount", 0)) - int(f_amt.get("sellAmount", 0))
                        + int(i_amt.get("buyAmount", 0)) - int(i_amt.get("sellAmount", 0))
                    )
                    if day_net:  # 장 시작 전 0값 행 스킵 — 마지막 완성일 사용
                        net += day_net
                        break
            s = 1 if net > 3e11 else (-1 if net < -3e11 else 0)
            results.append(IndicatorResult(
                "kr_flow", s, f"KR 수급: 외국인+기관 순매수 {net / 1e12:+.2f}조 ({s:+d})"
            ))
        except Exception as e:  # noqa: BLE001
            results.append(IndicatorResult(
                "kr_flow", None, f"KR 수급 조회 실패 — 지표 제외: {type(e).__name__}"
            ))

        return self._finalize(results, computed_at)

    # ------------------------------------------------------------------ 지표 입력 수집

    def _local_indicators(self) -> list[IndicatorResult]:
        closes = self._load_qqq_daily_closes()
        if closes is None:
            unavailable = "QQQ 일봉 데이터 로드 실패 — 지표 제외"
            return [
                IndicatorResult("qqq_trend", None, unavailable),
                IndicatorResult("qqq_volatility", None, unavailable),
            ]
        stale = self._staleness(closes)
        if stale is not None:
            # **낡은 봉을 현재가로 쓰지 않는다.** 백필이 멈춘 채 사이징이 계속되면
            # 숫자는 멀쩡해 보이는데 근거가 없다 — 2026-08-13 실측: 마지막 봉이
            # 07-31 인데 그날은 08-13 이었고, 상태는 degraded=False 였다.
            return [
                IndicatorResult("qqq_trend", None, stale),
                IndicatorResult("qqq_volatility", None, stale),
            ]
        return [qqq_trend_score(closes), qqq_volatility_score(closes)]

    def _staleness(self, closes: pd.Series) -> str | None:
        """낡았으면 사람이 읽을 사유, 신선하면 None.

        사유가 원인을 말해야 한다 — "로드 실패"로 뭉개면 백필이 멈춘 걸 못 찾는다.
        """
        last = closes.index.max()
        if not isinstance(last, pd.Timestamp) or pd.isna(last):
            return "QQQ 일봉 인덱스가 시각이 아님 — 파티션 파손 의심, 지표 제외"
        if last.tzinfo is None:  # 구 파티션은 tz-naive 다
            last = last.tz_localize("UTC")
        now = pd.Timestamp(self._now_fn())
        now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        age = now - last
        if age <= STALE_DAILY_BARS_AFTER:
            return None
        return (
            f"QQQ 일봉이 낡음 — 마지막 봉 {last.date()}, {age.days}일 경과"
            f"(임계 {STALE_DAILY_BARS_AFTER.days}일). 백필 확인 필요 — 지표 제외"
        )

    def _load_qqq_daily_closes(self) -> pd.Series | None:
        sym_dir = self._history_dir / "QQQ" / "1d"
        if not sym_dir.exists():
            return None
        parts = sorted(sym_dir.glob("*/*.parquet"))
        if not parts:
            return None
        try:
            # **빈 파일은 버린다.** 백필은 데이터가 없는 달에도 0행 파일을 남기는데,
            # 빈 DataFrame 은 DatetimeIndex 를 잃고 RangeIndex 가 된다 — 그대로
            # concat 하면 인덱스가 혼합 타입이 되고 신선도 판정이 터진다.
            # history.py 가 ce6a755 에서 고친 것과 같은 결함이 이 경로에는 남아 있었다.
            frames = [d for d in (pd.read_parquet(p) for p in parts) if not d.empty]
            if not frames:
                return None
            df = pd.concat(frames)
        except Exception:
            logger.warning("regime: QQQ 일봉 파티션 로드 실패", exc_info=True)
            return None
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty or "close" not in df.columns:
            return None
        return df["close"]

    # ------------------------------------------------------------------ VIX 스트레스 게이트(방어 전용, US만)

    def _vix_indicator(self) -> tuple[str, bool | None]:
        """(사유 문자열, stress) 반환. stress=None은 "판단 못 함"(비활성화/데이터
        없음/낡음) — `_apply_vix_gate`는 None을 False와 동일하게 취급해 게이트를
        건너뛴다(절대 거래를 막지 않는다). 점수(+1/0/-1)가 아니라 방어 게이트라
        IndicatorResult가 아닌 튜플을 쓴다 — _finalize의 점수 합산에 섞이지
        않는다는 신호이기도 하다."""
        if not self._vix_cfg.get("enabled", DEFAULT_VIX_ENABLED):
            return "VIX 지표 비활성화(설정) — 제외", None
        closes = self._load_vix_daily_closes()
        if closes is None:
            reason = "VIX 일봉 데이터 없음 — 지표 제외"
            logger.warning("regime: %s", reason)
            return reason, None
        stale = self._vix_staleness(closes)
        if stale is not None:
            logger.warning("regime: %s", stale)
            return stale, None
        result = vix_stress(
            closes,
            level_max=float(self._vix_cfg.get("level_max", DEFAULT_VIX_LEVEL_MAX)),
            sma20_ratio_max=float(self._vix_cfg.get("sma20_ratio_max", DEFAULT_VIX_SMA20_RATIO_MAX)),
        )
        if result["level"] is None:
            return result["reason"], None
        return result["reason"], result["stress"]

    def _load_vix_daily_closes(self) -> pd.Series | None:
        """`data/history/VIX/1d` 파티션 로드 — `_load_qqq_daily_closes`와 동일한
        방어 로직(빈 파일 버림, dedup, close 컬럼 확인). server/scripts/
        backfill_us_daily.sh가 yfinance("^VIX")로 채운다(quant/collect/quotes/
        yf_source.py의 심볼 매핑 — 저장 심볼은 "VIX", 조회 심볼만 "^VIX")."""
        sym_dir = self._history_dir / "VIX" / "1d"
        if not sym_dir.exists():
            return None
        parts = sorted(sym_dir.glob("*/*.parquet"))
        if not parts:
            return None
        try:
            frames = [d for d in (pd.read_parquet(p) for p in parts) if not d.empty]
            if not frames:
                return None
            df = pd.concat(frames)
        except Exception:
            logger.warning("regime: VIX 일봉 파티션 로드 실패", exc_info=True)
            return None
        df = df[~df.index.duplicated(keep="last")].sort_index()
        if df.empty or "close" not in df.columns:
            return None
        return df["close"]

    def _vix_staleness(self, closes: pd.Series) -> str | None:
        """낡았으면(거래일 기준 STALE_VIX_SESSIONS_AFTER 세션 초과) 사람이 읽을
        사유, 신선하면 None. QQQ의 `_staleness`(달력일)와 달리 거래일(세션) 수로
        재는 이유는 위 STALE_VIX_SESSIONS_AFTER 주석 참고 — `numpy.busday_count`는
        공휴일 캘린더가 없어 주말만 뺀 근사치다(이 저장소의 다른 거래일 근사,
        예: collect/quotes/backfill.py의 `_find_gaps`와 같은 한계)."""
        last = closes.index.max()
        if not isinstance(last, pd.Timestamp) or pd.isna(last):
            return "VIX 일봉 인덱스가 시각이 아님 — 파티션 파손 의심, 지표 제외"
        last_date = (last.tz_convert("UTC") if last.tzinfo is not None else last).date()
        now_ts = pd.Timestamp(self._now_fn())
        now_date = (now_ts.tz_convert("UTC") if now_ts.tzinfo is not None else now_ts).date()
        # busday_count(A, B)는 반개구간 [A, B) 의 평일 수 — last_date 자신은 이미
        # 받은 봉이므로 그 다음 날부터, now_date는 아직 열리지 않은(오늘 세션이
        # 아직 없는) 날이므로 그 날 자체는 세지 않는다. 예: 월요일 봉으로 화요일
        # 아침(현재 시각) 판단 → busday_count(화, 화) = 0 (정상, 0세션 누락).
        # 월요일 봉으로 금요일 아침 판단 → busday_count(화, 금) = 화수목 = 3세션.
        missed = int(np.busday_count(last_date + timedelta(days=1), now_date))
        if missed <= STALE_VIX_SESSIONS_AFTER:
            return None
        return (
            f"VIX 일봉이 낡음 — 마지막 봉 {last_date}, 거래일 기준 {missed}세션 경과"
            f"(임계 {STALE_VIX_SESSIONS_AFTER}세션). 백필 확인 필요 — 지표 제외"
        )

    def _bond_yield_indicator(self) -> IndicatorResult:
        # 2026-08-28: KR_BOND_10Y(Toss 미구현, 항상 None)에서 US_BOND_10Y(FRED
        # DGS10, quant/adapters/macro/fred.py가 수집해 data/ledger/macro_rates.jsonl
        # 에 적재 → quant/adapters/regime_indicators.FileMacroIndicatorClient가
        # 읽음)로 요청 심볼을 바꿨다. indicators.bond_yield_score 문서(구
        # docstring)가 이미 지적했듯 국내 국채/코스피는 이 저장소가 거래하는
        # TQQQ/SQQQ(미국 레버리지 ETF)의 위험심리를 직접 움직이는 동인이 아니다
        # — 미국 10년물이야말로 그 자산군의 할인율이다. 대리지표를 걷어내고
        # 실제 소스로 바꾸는 것이지, 국내 지표 부재를 감추려고 이름만 바꾼 게
        # 아니다(요청 심볼도 US_BOND_10Y로 함께 바뀐다 — indicators.py의
        # IndicatorResult.name도 "us_bond_yield"로 개명).
        last, prev = self._indicator_last_prev("US_BOND_10Y")
        if last is None or prev is None:
            return bond_yield_score(None)
        # 국채 수익률은 %(포인트) 단위 시세다 — 1%p 변화 = 100bp.
        change_bp = (last - prev) * 100
        return bond_yield_score(change_bp)

    def _kospi_indicator(self) -> IndicatorResult:
        last, prev = self._indicator_last_prev("KOSPI")
        if last is None or prev is None or prev == 0:
            return kospi_score(None)
        change_pct = (last - prev) / prev * 100
        return kospi_score(change_pct)

    def _indicator_last_prev(self, symbol: str) -> tuple[float | None, float | None]:
        if self._indicator_client is None:
            return None, None
        try:
            last = self._indicator_client.indicator_price(symbol)
            prev = self._indicator_client.indicator_prev_close(symbol)
        except Exception:
            logger.warning("regime: %s 조회 실패", symbol, exc_info=True)
            return None, None
        return last, prev

    def _bitcoin_indicator(self) -> IndicatorResult:
        if self._bitcoin_adapter is None:
            return bitcoin_score(None)
        try:
            change_pct = self._bitcoin_adapter.price_change_pct()
        except Exception:
            logger.warning("regime: 비트코인 조회 실패", exc_info=True)
            change_pct = None
        return bitcoin_score(change_pct)

    # ------------------------------------------------------------------ 캐시 파일 I/O(로컬 디스크, 네트워크 아님)

    def _load_cache(self) -> RegimeState | None:
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            # markets.KR — 있으면 KR 상태도 복원 (없는 구버전 캐시는 KR=None → 중립)
            kr_raw = (data.get("markets") or {}).get("KR")
            if kr_raw and self._kr_state is None:
                try:
                    self._kr_state = RegimeState.from_dict(kr_raw)
                except Exception:
                    logger.warning("regime: KR 캐시 파싱 실패 — KR 중립", exc_info=True)
            return RegimeState.from_dict(data)
        except Exception:
            logger.warning("regime: 캐시 파일 파싱 실패 — 무시", exc_info=True)
            return None

    def _save_cache(self, state: RegimeState) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        # 최상위 필드 = US 상태(하위 호환 — watch-score가 top-level label을 읽는다).
        # markets에 시장별 상태를 병기한다.
        payload = state.to_dict()
        payload["markets"] = {"US": state.to_dict()}
        if self._kr_state is not None:
            payload["markets"]["KR"] = self._kr_state.to_dict()
        # 원자적 tmp-replace(2026-08-21) — 7개 상태 파일 중 여기만 write_text 직접
        # 이었다. 쓰다 죽으면 깨진 JSON 이 남고, 다음 부팅의 _load_cache 가 "캐시
        # 파싱 실패"로 국면을 잃는다(risk/manager._save_day_state 와 같은 패턴).
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_path)
