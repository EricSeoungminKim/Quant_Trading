"""공개 사이트 "전략 설명" 드로어용 콘텐츠 — 전략별 가설·진입·청산·사이징·근거
(2026-09-04).

`quant.control.performance.build_performance_payload`가 `strategies[].help`에
이 모듈의 `build_strategy_help(sid, strategies_cfg)` 출력을 그대로 싣는다.
방문자가 "이 전략은 뭘 믿고 뭘 하는가"를 코드를 읽지 않고 알 수 있게 하는 것이
목적이다 — 종목/계좌 잔고 같은 비공개 값은 여전히 내지 않는다(성과 대시보드와
같은 공개 안전 규칙).

## 계약

- 키: `category`(intraday|swing|experimental), `theory_ko/en`, `entry_ko/en`,
  `exit_ko/en`, `sizing_ko/en`, `evidence_ko/en`, `refs`(0~3개, `{label, url}`).
  모든 문자열 필드는 평문(마크다운 금지)이고 400자 이하, KO/EN 둘 다 채운다.
- **기준(base) 전략 id로 키를 잡는다** — `quant.core.strategy_ids.base_strategy_id`
  로 `_cat`(A/B 촉매 갈래)를 벗긴 뒤 조회하고, 촉매 갈래에는 실제
  `config/settings.yaml`의 `universe_filter`를 읽어 "이 갈래가 어떤 태그를
  보는가" 한 문장을 진입 규칙 뒤에 덧붙인다(하드코딩 아님 — 촉매 정의가 바뀌면
  이 문장도 같이 바뀐다).
- **파라미터는 가능하면 `strategies_cfg`(=`config/settings.yaml`의
  `strategies:` 블록, `quant.apps.config.Settings.strategies`)에서 그 자리에서
  읽는다.** 값이 없으면 그 숫자를 지어내지 않고 문장에서 통째로 뺀다(`_clause`
  헬퍼 — 필요한 kwarg 중 하나라도 `None`이면 그 절 자체를 버린다). 정적
  텍스트(가설·근거)는 문헌 인용·2026-09-03/04 실측 결론처럼 설정으로 바뀌지
  않는 사실이라 이 파일에 고정 문자열로 둔다.
- **레지스트리에 없는(또는 아직 이 파일에 못 채운) id는 크래시 대신 일반
  fallback**(`category: experimental`, `evidence: "문서 없음"`)을 낸다 —
  루트 CLAUDE.md "테스트를 약화시켜 통과시키지 않는다" 원칙과 같은 이유로,
  모르는 걸 아는 척 지어내지 않는다.
- **근거(`evidence_*`)는 2026-09-03/04 실측 결론을 반영한다**(`docs/vault/
  변경기록.md` 상단): US 일중 계열은 10.7년 OOS에서 전부 왕복 비용(10.5bp)에
  패배(scalp_1m −2.2bp / orb_rvol −1.2bp / pullback_impulse +0.9bp, 게이트
  통과 0건). KR 1년 분봉 게이트는 scalp_1m만 결론이 났다(6폴드 양수 0건,
  NO_GO) — orb_rvol·eod_reversal·open_reversal 도 2026-09-04 KR 1년 분봉 게이트에서
  전부 NO_GO(scalp_1m 포함 4/4).
  **아는 것과 모르는 것을 구분해 정직하게 적는다** — 
  open_reversal에 "NO_GO"라고 단정하지 않는다(사실이 아니다). 오버나이트형
  6종(frgn_accumulate/close_bet/overnight_drift/rsi2_dip/mean_reversion/
  cross_momentum)은 "자동매매는 단타·스캘핑만" 정책(2026-09-03)으로
  `enabled: false`다 — 성과가 나빠서가 아니라 정책상 대상이라는 점을 명시한다.
"""
from __future__ import annotations

from quant.core.strategy_ids import base_strategy_id, is_catalyst_arm

__all__ = ["build_strategy_help"]

_HARD_STOP_PCT = 5.0
_TARGET_CAP_PCT = 10.0
_LIMIT_UP_PCT = 29.5

# `config/settings.yaml`의 `risk.overnight_strategies`(인트라데이 하드레일 제외
# 목록)를 표시용으로 미러링한다 — 이 파일은 quant/control/(제어 평면)이라
# quant/trade/risk/manager.py(거래 평면)를 임포트할 수 없다(평면 규칙). 두 목록이
# 갈라지면 `tests/test_strategy_help.py`가 settings.yaml과 대조해 잡는다.
_OVERNIGHT_STRATEGIES = {
    "cross_momentum", "frgn_accumulate", "mean_reversion", "close_bet",
    "overnight_drift", "rsi2_dip",
}


def _pct(x: float) -> str:
    return f"{x * 100:g}"


def _clause(ko_tmpl: str, en_tmpl: str, **kwargs) -> tuple[str | None, str | None]:
    """kwargs 중 하나라도 없으면(None) 이 절 전체를 버린다 — 없는 파라미터의
    숫자를 지어내지 않는다."""
    if any(v is None for v in kwargs.values()):
        return None, None
    return ko_tmpl.format(**kwargs), en_tmpl.format(**kwargs)


def _join(*clauses: tuple[str | None, str | None], sep_ko: str = " · ", sep_en: str = "; ") -> tuple[str, str]:
    ko = sep_ko.join(c[0] for c in clauses if c[0])
    en = sep_en.join(c[1] for c in clauses if c[1])
    return ko, en


def _p(params: dict, *keys, default=None):
    cur = params
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur


# ---------------------------------------------------------------------------
# 전략별 진입/청산 절 빌더. 각 함수는 그 전략의 `params` 딕셔너리(settings.yaml
# `strategies.<id>.params`)를 받아 (ko, en) 문자열 쌍을 낸다.
# ---------------------------------------------------------------------------

def _entry_donchian(p):
    return _join(
        _clause(
            "{iv}분봉 {lb}봉 돈치안 채널 상단/하단 돌파 + 거래량 {vm}배 이상 확인 후 추세추종 진입(TQQQ/SQQQ, 역방향 동시보유 금지)",
            "Enter on a {iv}-min, {lb}-bar Donchian channel breakout confirmed by {vm}x average volume (TQQQ/SQQQ, no simultaneous opposite-direction holding)",
            iv=_p(p, "interval_minutes"), lb=_p(p, "lookback_bars"), vm=_p(p, "volume_mult"),
        ),
    )


def _exit_donchian(p):
    return _join(
        _clause(
            "손절폭 하한 {smb}bp, 목표 손익비 R:R={rr}",
            "stop distance floored at {smb}bp, target risk:reward = {rr}",
            smb=_p(p, "stop_min_bps"), rr=_p(p, "risk_reward"),
        ),
        _clause(
            "{r}R 도달 시 보유의 {f}% 부분익절 후 본전 이동",
            "at {r}R, take partial profit on {f}% of the position and move stop to breakeven",
            r=_p(p, "position_mgmt", "scale_out_at_r"), f=_p(p, "position_mgmt", "scale_out_fraction") and round(_p(p, "position_mgmt", "scale_out_fraction") * 100, 1),
        ),
        _clause(
            "마감 {fb}분 전 강제청산",
            "force-flatten {fb} minutes before close",
            fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_orb(p):
    return _join(
        _clause(
            "개장 {orm}분 레인지 상단 돌파 시 매수(TQQQ), 시가~종가 변화가 {doji}% 미만인 도지 봉은 스킵",
            "Buy TQQQ on a breakout above the {orm}-minute opening range; skip doji bars where open-to-close move is under {doji}%",
            orm=_p(p, "opening_range_minutes"), doji=_p(p, "doji_threshold_pct"),
        ),
        _clause(
            "레인지가 음봉이면 인버스(SQQQ)를 매수해 하락 방향도 롱으로 표현",
            "If the range bar is bearish, buy the inverse (SQQQ) instead — expresses downside as a long position",
        ),
    )


def _exit_orb(p):
    return _join(
        _clause(
            "손절 = 일봉 ATR{ap} x {asm}, 목표가 없이 마감 {fb}분 전까지 보유 후 청산",
            "Stop = daily ATR{ap} x {asm}; no fixed target, hold until flattened {fb} minutes before close",
            ap=_p(p, "atr_period"), asm=_p(p, "atr_stop_mult"), fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_intraday_scan(p):
    return _join(
        _clause(
            "개장 {es}분 이후부터 {bi}분봉 세션 신고가 돌파 + 거래량 {vm}배 이상 시 진입(관심종목 전체, 롱 온리)",
            "After {es} minutes from open, enter on a {bi}-min-bar session-high breakout confirmed by {vm}x volume (across the watchlist universe, long only)",
            es=_p(p, "entry_start_minutes_after_open"), bi=_p(p, "bar_interval_minutes"), vm=_p(p, "volume_mult"),
        ),
    )


def _exit_scan_common(p):
    return _join(
        _clause(
            "목표 {r}R(=진입가+R×(진입가−손절가)), 손절폭 상한 {smb}bp",
            "Target {r}R (entry + R x (entry - stop)), stop distance capped at {smb}bp",
            r=_p(p, "profit_target_r"), smb=_p(p, "stop_max_bps"),
        ),
        _clause(
            "마감 {fb}분 전 강제청산",
            "force-flatten {fb} minutes before close",
            fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_orb_scan(p):
    return _join(
        _clause(
            "개장 {orm}분 레인지 상단 돌파 시 매수(관심종목 전체, 도지 {doji}% 미만 제외, 인버스 쌍이 없어 롱 온리 — 음봉은 스킵)",
            "Buy on a breakout above the {orm}-minute opening range across the watchlist universe (excludes doji under {doji}%; long only since there is no inverse pair — bearish range bars are skipped)",
            orm=_p(p, "opening_range_minutes"), doji=_p(p, "doji_threshold_pct"),
        ),
    )


def _entry_mean_reversion(p):
    turnover = _p(p, "min_turnover")
    return _join(
        _clause(
            "{zw}일 z-score ≤ {ez} 이고 RSI({rp}) ≤ {er}일 때 매수(069500/229200, {tw}일 평균 거래대금 {mt:g}억원 이상만)",
            "Buy when the {zw}-day z-score is ≤ {ez} and RSI({rp}) is ≤ {er} (069500/229200 only; requires {tw}-day average turnover of at least {mt:g}0k KRW-million)",
            zw=_p(p, "zscore_window"), ez=_p(p, "entry_z"), rp=_p(p, "rsi_period"), er=_p(p, "entry_rsi"),
            tw=_p(p, "turnover_window"), mt=turnover / 1e8 if turnover else None,
        ),
    )


def _exit_mean_reversion(p):
    return _join(
        _clause(
            "z-score ≥ {xz} 회복 시 청산, ATR{ap} x {asm} 손절, 최대 {mh}세션 보유 후 시간청산(오버나이트 보유 허용)",
            "Exit once the z-score recovers to ≥ {xz}; stop at ATR{ap} x {asm}; time-exit after {mh} sessions max hold (overnight holding allowed)",
            xz=_p(p, "exit_z"), ap=_p(p, "atr_period"), asm=_p(p, "atr_stop_mult"), mh=_p(p, "max_hold_sessions"),
        ),
    )


def _entry_cross_momentum(p):
    return _join(
        _clause(
            "매주 {wd}요일 관심종목 중 최근 {lb}일 수익률 상위 {tn}종목을 편입(횡단면 상대모멘텀 로테이션, 롱 온리)",
            "Weekly rebalance: buy the top {tn} watchlist symbols by trailing {lb}-day return (cross-sectional relative-momentum rotation, long only)",
            wd=(["월", "화", "수", "목", "금", "토", "일"][_p(p, "rebalance_weekday")] if _p(p, "rebalance_weekday") is not None else None),
            lb=_p(p, "lookback_sessions"), tn=_p(p, "top_n"),
        ),
    )


def _exit_cross_momentum(p):
    return _join(
        _clause(
            "ATR{ap} x {asm} 손절, 리밸런스 시 상위권에서 밀려난 종목은 교체 청산(오버나이트 보유 허용)",
            "Stop at ATR{ap} x {asm}; symbols dropping out of the top ranks are rotated out at rebalance (overnight holding allowed)",
            ap=_p(p, "atr_period"), asm=_p(p, "atr_stop_mult"),
        ),
    )


def _entry_confluence(p):
    return _join(
        _clause(
            "{bi}분봉 SMA{sf}/{ss}·RSI{rp}·MACD({mf},{ms},{msig})·볼린저({bb}봉,{bs}σ)·박스권 5축 중 최소 {mc}축 동의 + 거래량 {vm}배",
            "On {bi}-min bars, requires at least {mc} of 5 signals (SMA{sf}/{ss}, RSI{rp}, MACD({mf},{ms},{msig}), Bollinger({bb},{bs}sigma), range-box) to agree, plus {vm}x volume",
            bi=_p(p, "bar_interval_minutes"), sf=_p(p, "sma_fast"), ss=_p(p, "sma_slow"), rp=_p(p, "rsi_period"),
            mf=_p(p, "macd_fast"), ms=_p(p, "macd_slow"), msig=_p(p, "macd_signal"),
            bb=_p(p, "bb_period"), bs=_p(p, "bb_num_std"), mc=_p(p, "min_confluence"), vm=_p(p, "volume_mult"),
        ),
    )


def _exit_confluence(p):
    return _join(
        _clause(
            "ATR{ap} x {asm}(분봉) 손절, {r}R 도달 시 {f}% 부분익절",
            "Stop at ATR{ap} x {asm} (intraday); at {r}R, take partial profit on {f}%",
            ap=_p(p, "atr_period"), asm=_p(p, "atr_stop_mult"), r=_p(p, "scale_out_at_r"),
            f=_p(p, "scale_out_fraction") and round(_p(p, "scale_out_fraction") * 100, 1),
        ),
        _clause("마감 {fb}분 전 강제청산", "force-flatten {fb} minutes before close", fb=_p(p, "flatten_before_close_minutes")),
    )


def _entry_news_momentum(p):
    return _join(
        _clause(
            "당일 뉴스 EVENT 태그 종목만, 개장 첫 1분봉이 양봉으로 마감해야 진입(개장 후 {ew}초 이내), 세션당 최대 {me}종목",
            "Only symbols tagged EVENT (news) that day; enters only if the first 1-min bar closes bullish (within {ew} seconds of open), up to {me} symbols per session",
            ew=_p(p, "entry_window_seconds"), me=_p(p, "max_entries_per_session"),
        ),
    )


def _exit_news_momentum(p):
    return _join(
        _clause(
            "손절 −{sl}%, +{pt}%에서 {pf}% 부분익절, +{ft}% 전량 익절",
            "Stop at -{sl}%, partial take-profit ({pf}%) at +{pt}%, full exit at +{ft}%",
            sl=_p(p, "stop_loss_pct"), pt=_p(p, "partial_take_pct"),
            pf=_p(p, "partial_fraction") and round(_p(p, "partial_fraction") * 100, 1), ft=_p(p, "full_take_pct"),
        ),
        _clause("시간청산 없음, 마감 {fb}분 전 강제청산", "no time-based exit; force-flatten {fb} minutes before close", fb=_p(p, "flatten_before_close_minutes")),
    )


def _entry_news_scalp(p):
    return _join(
        _clause(
            "당일 뉴스 EVENT_SCALP 태그 종목만, 개장 후 {ew}초 이내 즉시 진입, 세션당 최대 {me}종목",
            "Only symbols tagged EVENT_SCALP that day; enters immediately within {ew} seconds of open, up to {me} symbols per session",
            ew=_p(p, "entry_window_seconds"), me=_p(p, "max_entries_per_session"),
        ),
    )


def _exit_news_scalp(p):
    return _join(
        _clause(
            "손절 −{sl}%, 목표가 없이 마감까지 보유",
            "Stop at -{sl}%, no fixed target — holds until close",
            sl=_p(p, "stop_loss_pct"),
        ),
        _clause("마감 {fb}분 전 강제청산", "force-flatten {fb} minutes before close", fb=_p(p, "flatten_before_close_minutes")),
    )


def _entry_frgn_accumulate(p):
    return _join(
        _clause(
            "외국인 순매수 지속 태그(FRGN)가 붙은 종목에, 개장 {ea}분 후 평가 시점마다 {bq}주씩 매수(일 1회)",
            "For symbols tagged FRGN (sustained net foreign buying), buys {bq} share(s) at an evaluation point {ea} minutes after open (once per day)",
            ea=_p(p, "eval_after_minutes_after_open"), bq=_p(p, "buy_qty"),
        ),
    )


def _exit_frgn_accumulate(p):
    return _join(
        _clause(
            "태그가 FRGN_EXIT(이탈)로 바뀌면 보유의 {ef}%를 먼저 매도, 나머지는 다음 이탈 평가에서 청산 — 오버나이트 보유형",
            "When the tag flips to FRGN_EXIT, sells {ef}% of the position first, exiting the rest at the next re-evaluation — an overnight-carry design",
            ef=_p(p, "exit_fraction_first") and round(_p(p, "exit_fraction_first") * 100, 1),
        ),
    )


def _entry_close_bet(p):
    start = _p(p, "entry_start_hhmm")
    end = _p(p, "entry_end_hhmm")
    start_s = f"{start[0]:02d}:{start[1]:02d}" if start else None
    end_s = f"{end[0]:02d}:{end[1]:02d}" if end else None
    return _join(
        _clause(
            "{s}~{e} 사이 마감 강세 태그(CLOSE_BET) 종목 중 양봉이고 마감강도 ≥ {mc}인 종목을 종가 부근에 매수",
            "Between {s} and {e}, buys symbols tagged CLOSE_BET (strong-close candidates) that are bullish with a closing-strength score ≥ {mc}, near the closing price",
            s=start_s, e=end_s, mc=_p(p, "min_close_strength"),
        ),
    )


def _exit_close_bet(p):
    return _join(
        _clause(
            "손절 −{sp}%, 익절 +{tp}%, 다음날 개장 후 {ed}분 이내 반드시 매도(상승 갭 실현) — 오버나이트 보유형",
            "Stop at -{sp}%, take-profit at +{tp}%, and always sells within {ed} minutes of the next day's open to realize the overnight gap — an overnight-carry design",
            sp=_p(p, "stop_pct"), tp=_p(p, "take_profit_pct"), ed=_p(p, "exit_deadline_minutes_after_open"),
        ),
    )


def _entry_overnight_drift(p):
    return _join(
        _clause(
            "종가 {eb}분 전 QQQ 매수",
            "Buys QQQ {eb} minutes before the close",
            eb=_p(p, "entry_before_close_minutes"),
        ),
    )


def _exit_overnight_drift(p):
    return _join(
        _clause(
            "익일 개장 {eo}분 이내 매도, 손절 −{sp}% — 오버나이트 보유형(왕복 1회, 일중 전략들의 벤치마크 역할도 겸함)",
            "Sells within {eo} minutes of the next open; stop at -{sp}% — an overnight-carry design (one round trip; also serves as the benchmark intraday strategies must beat)",
            eo=_p(p, "exit_after_open_minutes"), sp=_p(p, "stop_pct"),
        ),
    )


def _entry_pullback_impulse(p):
    return _join(
        _clause(
            "{bi}분봉에서 {mi}bp 이상 임펄스 발생 후 그 되돌림이 {pmin}~{pmax}% 구간(EMA{ep} 부근)일 때 매수 — 파동 고점이 아니라 눌림목에서 진입",
            "On {bi}-min bars, after a {mi}bp+ impulse move, buys the pullback once retracement reaches {pmin}-{pmax}% (near EMA{ep}) — enters on the dip, not the impulse peak",
            bi=_p(p, "bar_interval_minutes"), mi=_p(p, "min_impulse_bp"),
            pmin=_p(p, "pullback_min_pct"), pmax=_p(p, "pullback_max_pct"), ep=_p(p, "ema_period"),
        ),
    )


def _exit_pullback_impulse(p):
    return _join(
        _clause(
            "손절 = ATR 버퍼 x {ab}, 목표 = 임펄스 x {tm}, {to}분 타임아웃 청산",
            "Stop = ATR buffer x {ab}; target = impulse size x {tm}; times out after {to} minutes",
            ab=_p(p, "atr_buffer_mult"), tm=_p(p, "target_mult"), to=_p(p, "timeout_minutes"),
        ),
    )


def _entry_mr_vwap_quiet(p):
    return _join(
        _clause(
            "{bi}분봉, 상대거래량 ≤ {rv}·ADX ≤ {ax}인 '조용한' 종목만 대상 — VWAP 밴드({bk}σ) 밖으로 나갔다가 안으로 복귀한 종가에 매수(개장 {ao}분 후 ~ 마감 {bc}분 전, 세션당 최대 {me}회)",
            "On {bi}-min bars, only trades 'quiet' symbols (relative volume ≤ {rv}, ADX ≤ {ax}) — buys when the close moves back inside a {bk}-sigma VWAP band from outside it (between {ao} min after open and {bc} min before close, up to {me} entries/session)",
            bi=_p(p, "bar_interval_minutes"), rv=_p(p, "rvol_max"), ax=_p(p, "adx_max"), bk=_p(p, "band_k"),
            ao=_p(p, "entry_after_open_minutes"), bc=_p(p, "entry_before_close_minutes"), me=_p(p, "max_entries_per_session"),
        ),
    )


def _exit_mr_vwap_quiet(p):
    return _join(
        _clause(
            "목표 최소 {tm}bp, {to}분 타임아웃 청산",
            "Targets at least {tm}bp; times out after {to} minutes",
            tm=_p(p, "target_min_bp"), to=_p(p, "timeout_minutes"),
        ),
    )


def _entry_vol_breakout(p):
    return _join(
        _clause(
            "시가 + 전일 레인지 x {k}(Larry Williams 원 공식) 상향 돌파 시 매수, 목표 없이 추종",
            "Buys on a breakout above open + prior-day range x {k} (Larry Williams' original formula); no fixed target, follows the trend",
            k=_p(p, "k"),
        ),
    )


def _exit_vol_breakout(p):
    return _join(
        _clause(
            "손절폭 하한 {ms}bp, 마감 {ee}분 전 청산",
            "Stop distance floored at {ms}bp; flattens {ee} minutes before close",
            ms=_p(p, "min_stop_bp"), ee=_p(p, "eod_exit_min"),
        ),
    )


def _entry_intraday_momentum(p):
    return _join(
        _clause(
            "{bi}분봉 QQQ 변동성 밴드({bm}x, {lb}일 σ) 상단 이탈 시 TQQQ 매수, 하단 이탈 시 SQQQ(인버스) 매수 — 롱온리 계좌에서 하락장도 롱으로 표현",
            "On {bi}-min bars, buys TQQQ when QQQ breaks above a {bm}x, {lb}-day volatility band, or buys SQQQ (inverse) on a break below — expresses downside as a long position",
            bi=_p(p, "bar_interval_minutes"), bm=_p(p, "band_mult"), lb=_p(p, "lookback_days"),
        ),
    )


def _exit_intraday_momentum(p):
    return _join(
        _clause(
            "손절 −{sp}%(하한 {ms}bp), 하루 같은 방향 진입 최대 {me}회, 마감 {fb}분 전 강제청산",
            "Stop at -{sp}% (floored at {ms}bp); up to {me} same-direction entries per day; force-flattens {fb} minutes before close",
            sp=_p(p, "stop_pct"), ms=_p(p, "min_stop_bp"), me=_p(p, "max_same_direction_entries_per_day"), fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_gap_fade(p):
    return _join(
        _clause(
            "시가 갭이 {gmin}~{gmax}bp인 종목을 개장 {ew}분 안에 갭의 {fr}%가 메워지는 방향으로 매수(고거래량 갭·{gr}bp 초과 대형 갭은 문헌 필터로 제외)",
            "Buys symbols gapping {gmin}-{gmax}bp, betting on {fr}% gap-fill within {ew} minutes of open (excludes high-relative-volume gaps and gaps over {gr}bp per literature-based filters)",
            gmin=_p(p, "gap_min_bp"), gmax=_p(p, "gap_max_bp"), ew=_p(p, "entry_window_min"),
            fr=_p(p, "fill_ratio") and round(_p(p, "fill_ratio") * 100, 1), gr=_p(p, "gap_size_reject_bp"),
        ),
    )


def _exit_gap_fade(p):
    return _join(
        _clause(
            "손절폭 하한 {ms}bp, 최대 {mh}분 보유, 마감 {fb}분 전 강제청산",
            "Stop distance floored at {ms}bp; holds at most {mh} minutes; force-flattens {fb} minutes before close",
            ms=_p(p, "min_stop_bp"), mh=_p(p, "max_hold_min"), fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_rsi2_dip(p):
    return _join(
        _clause(
            "069500/QQQ가 {sd}일 추세선 위에 있을 때, 마감 {eb}분 전 RSI(2)가 {er} 미만이면 매수",
            "When 069500/QQQ trade above their {sd}-day trend average, buys {eb} minutes before close if RSI(2) is below {er}",
            sd=_p(p, "trend_sma_days"), eb=_p(p, "entry_before_close_minutes"), er=_p(p, "entry_rsi"),
        ),
    )


def _exit_rsi2_dip(p):
    return _join(
        _clause(
            "RSI(2)가 {xr} 이상으로 회복하면 청산, 최대 {mh}일 보유, 하드 손절 −{hs}% — 오버나이트 보유형",
            "Exits once RSI(2) recovers to {xr}+; holds at most {mh} days; hard stop at -{hs}% — an overnight-carry design",
            xr=_p(p, "exit_rsi"), mh=_p(p, "max_hold_days"), hs=_p(p, "hard_stop_pct"),
        ),
    )


def _entry_scalp_1m(p):
    return _join(
        _clause(
            "1분봉에서 직전 {vl}봉 평균 대비 {vm}배 이상인 상승봉(거래량 서지) 또는 {ma}선(1분) 지지(허용오차 {mt}%)를 확인해 진입",
            "Enters on a 1-min volume-surge bar ({vm}x the trailing {vl}-bar average) or a bounce off the {ma}-period 1-min moving average (tolerance {mt}%)",
            vl=_p(p, "volume_surge_lookback"), vm=_p(p, "volume_surge_mult"), ma=_p(p, "ma_period"), mt=_p(p, "ma_tolerance_pct"),
        ),
        _clause(
            "KR은 개장 {kd}분간 진입 지연(원장 실측: 08시대 승률 15%·09시대 30%)",
            "In KR, entries are delayed {kd} minutes after open (ledger data: 15% win rate in the 8am hour vs 30% in the 9am hour)",
            kd=_p(p, "kr_entry_open_delay_min"),
        ),
    )


def _exit_scalp_1m(p):
    return _join(
        _clause(
            "손절 = 최근 스윙 저점(구조) 아래, 하드캡 −{sc}%, +{pr}R 도달 시 {pf}% 부분익절",
            "Stop is placed below the recent swing low (structure-based), hard-capped at -{sc}%; takes partial profit ({pf}%) at +{pr}R",
            sc=_p(p, "stop_hard_cap_pct"), pr=_p(p, "partial_take_r"),
            pf=_p(p, "partial_fraction") and round(_p(p, "partial_fraction") * 100, 1),
        ),
        _clause(
            "+{be}bp 도달 시 본전 이동 + {tb}bp 트레일링 스톱, 마감 {fb}분 전 강제청산",
            "at +{be}bp moves the stop to breakeven and trails by {tb}bp; force-flattens {fb} minutes before close",
            be=_p(p, "breakeven_at_bp"), tb=_p(p, "trail_bp"), fb=_p(p, "flatten_before_close_minutes"),
        ),
    )


def _entry_llm_trader(p):
    return _join(
        _clause(
            "별도 프로세스(거래 핫패스 밖)가 내린 LLM 판단을 인박스 파일로 읽어 그대로 집행 — 최대 {mp}종목, 종목당 최대 {mw}% 비중",
            "Reads LLM trading decisions produced by a separate process outside the trading hot path from an inbox file and executes them as-is — up to {mp} positions, max {mw}% weight per position",
            mp=_p(p, "max_positions"), mw=_p(p, "max_weight_per_position") and round(_p(p, "max_weight_per_position") * 100, 1),
        ),
    )


def _exit_llm_trader(p):
    return _join(
        _clause(
            "하드 손절 −{sp}%, 마감 강제청산(오버나이트 금지)",
            "Hard stop at -{sp}%; force-flattened at close (no overnight holding)",
            sp=_p(p, "stop_pct"),
        ),
    )


def _entry_orb_rvol(p):
    return _join(
        _clause(
            "개장 상대거래량(직전 {rd}세션 평균 대비)이 {rm} 이상인 종목 중 상위 {tk}종목만 그날의 대상으로 골라, 개장 레인지 상단을 {ew}분 안에 돌파하면 매수",
            "Selects the top {tk} symbols by opening relative volume (vs. the trailing {rd}-session average, requires ≥{rm}) as that day's tradeable set, then buys a breakout above the opening range within {ew} minutes",
            rd=_p(p, "rvol_days"), rm=_p(p, "rvol_min"), tk=_p(p, "top_k"), ew=_p(p, "entry_window_min"),
        ),
    )


def _exit_orb_rvol(p):
    return _join(
        _clause(
            "손절 = 진입가 − 일봉 ATR{ap} x {saf}, 마감 {ee}분 전 청산",
            "Stop = entry price - daily ATR{ap} x {saf}; flattens {ee} minutes before close",
            ap=_p(p, "atr_period"), saf=_p(p, "stop_atr_frac"), ee=_p(p, "eod_exit_min"),
        ),
    )


def _entry_eod_reversal(p):
    return _join(
        _clause(
            "마감 {eb}분 전, 그날 세션 수익률 하위 {bp}% 중 −{md}% 이상 빠지고 거래대금이 {tk}~{tx}원 밴드 안인 KR 종목을 매수(장 막판 반전을 종가까지만 노림)",
            "{eb} minutes before close, buys KR symbols in the bottom {bp}% of session return that dropped at least {md}% with turnover inside the {tk}-{tx} KRW band (captures the intraday reversal into the close only)",
            eb=_p(p, "eval_minutes_before_close"), bp=_p(p, "bottom_pct"), md=_p(p, "min_drop_pct"),
            tk=_p(p, "min_turnover_krw"), tx=_p(p, "max_turnover_krw"),
        ),
    )


def _exit_eod_reversal(p):
    return _join(
        _clause(
            "손절 −{sp}%, 목표 없이 마감 {ee}분 전 전량 청산(오버나이트 금지)",
            "Stop at -{sp}%; no fixed target, flattens entirely {ee} minutes before close (no overnight holding)",
            sp=_p(p, "stop_pct"), ee=_p(p, "eod_exit_min"),
        ),
    )


def _entry_trend_day(p):
    return _join(
        _clause(
            "시장 대리 지수가 {rm}일선 위(상승 국면)이고 당일 시가가 전일 종가 이상일 때만 본다",
            "Only looks when the market proxy closed above its {rm}-day moving average (up regime) and the day opened at or above the prior close",
            rm=_p(p, "regime_ma_days"),
        ),
        _clause(
            "개장 {om}분 레인지가 ATR14의 {oa}배를 넘는 '추세일'에서, {bi}분봉 종가가 그 레인지 고가와 세션 VWAP을 동시에 넘으면 매수(개장 후 {ew}분까지)",
            "On a 'trend day' whose first {om} minutes range exceeds {oa}x ATR14, buys when a {bi}-minute bar closes above both that range high and the session VWAP (until {ew} minutes after open)",
            om=_p(p, "or_minutes"), oa=_p(p, "or_atr_mult"),
            bi=_p(p, "bar_interval_minutes"), ew=_p(p, "entry_window_min"),
        ),
    )


def _exit_trend_day(p):
    return _join(
        _clause(
            "손절은 진입가가 아니라 **진입 시점 세션 VWAP − {sa}×ATR14**, 목표 없이 마감 {ee}분 전 전량 청산(오버나이트 금지)",
            "The stop is not entry-based but **session VWAP at entry minus {sa}x ATR14**; no target, flattens entirely {ee} minutes before close (no overnight holding)",
            sa=_p(p, "stop_atr_mult"), ee=_p(p, "eod_exit_min"),
        ),
    )


def _entry_open_reversal(p):
    return _join(
        _clause(
            "전일 종가 대비 하위 {bk}종목 중 −{mp}% 이상 빠진 KR 종목을 개장 {ew}분 안에 매수 — 단, 갭이 −{mg}%보다 더 크면(악재 지속 가능성) 진입하지 않음",
            "Buys KR symbols among the bottom {bk} by prior-day close-to-close return that dropped at least {mp}%, within {ew} minutes of open — skips if the open gap is worse than -{mg}% (possible continuing bad news)",
            bk=_p(p, "bottom_k"), mp=_p(p, "min_prev_drop_pct"), ew=_p(p, "entry_window_min"), mg=_p(p, "max_gap_down_pct"),
        ),
    )


def _exit_open_reversal(p):
    return _join(
        _clause(
            "손절 −{sp}%, 목표 없이 마감 {ee}분 전 전량 청산(오버나이트 금지)",
            "Stop at -{sp}%; no fixed target, flattens entirely {ee} minutes before close (no overnight holding)",
            sp=_p(p, "stop_pct"), ee=_p(p, "eod_exit_min"),
        ),
    )


# ---------------------------------------------------------------------------
# 전략별 정적 콘텐츠(가설·근거) + 위 진입/청산 빌더 결합.
# ---------------------------------------------------------------------------

_SPECS: dict[str, dict] = {
    "donchian": {
        "category": "intraday",
        "theory_ko": "15분봉 40봉 신고가/신저가 채널을 거래량 확인 후 돌파하면 그 방향 추세가 이어진다는 추세추종 가설(TQQQ/SQQQ).",
        "theory_en": "Trend-following hypothesis: a volume-confirmed breakout of a 40-bar, 15-min Donchian channel tends to continue in that direction (TQQQ/SQQQ).",
        "entry": _entry_donchian, "exit": _exit_donchian,
        "evidence_ko": "verified — 전작 stock-algo-trade에서 라이브 검증된 파라미터를 계승하고 본 저장소 백테스트/회계 사다리를 통과했다(ADR-0007). 2026-08-25 한국장 4종 체제 재편으로 현재 비활성(성과 저하가 아니라 재배분).",
        "evidence_en": "verified — inherits parameters live-validated in the predecessor repo stock-algo-trade and passed this repo's backtest/accounting ladder (ADR-0007). Disabled since 2026-08-25's KR-focused 4-strategy realignment (a reallocation, not a performance failure).",
        "refs": [],
    },
    "orb": {
        "category": "intraday",
        "theory_ko": "개장 초반 레인지를 돌파하면 그날 방향이 정해진다는 Opening Range Breakout 가설(Zarattini & Aziz).",
        "theory_en": "Opening Range Breakout hypothesis: a break of the early-session range sets the day's direction (Zarattini & Aziz).",
        "entry": _entry_orb, "exit": _exit_orb,
        "evidence_ko": "미검증 — 조합 15개 이상을 탐색한 in-sample 결과(다중검정 미보정)라 채택 근거가 아니다. Walk-forward OOS는 총 +59.0%지만 유의성(t≈1.9)이 약하고, 같은 기간 TQQQ 단순보유(+2,980%)에 크게 뒤진다. enabled: false.",
        "evidence_en": "Unvalidated — the reported result comes from searching 15+ parameter combinations in-sample (no multiple-testing correction), so it is not adoption evidence. Walk-forward OOS is +59.0% but weakly significant (t≈1.9), and trails simple TQQQ buy-and-hold (+2,980%) over the same period by a wide margin. enabled: false.",
        "refs": [{"label": "Zarattini & Aziz (2023), SSRN 4416622", "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622"}],
    },
    "intraday_scan": {
        "category": "intraday",
        "theory_ko": "개장 30분 이후에도 세션 신고가를 거래량과 함께 돌파하는 종목은 그날 남은 시간에도 상승이 이어진다는 가설(orb_scan이 놓치는 구간을 담당).",
        "theory_en": "Hypothesis that a volume-confirmed session-high breakout occurring after the first 30 minutes still tends to continue for the rest of the day (covers the window orb_scan misses).",
        "entry": _entry_intraday_scan, "exit": _exit_scan_common,
        "evidence_ko": "burn_in — 원장 재생 27건(전체 종결 63건 중 1분봉 보유분)에서 현행 청산 규칙 중앙 −85.4bp를 익절+100bp/손절−100bp 규칙이 중앙 −7.6bp로 개선했지만 표본이 작아 판단 불가에 가깝다. 2026-08-25 한국장 4종 체제 재편으로 비활성.",
        "evidence_en": "burn_in — replaying 27 ledger exits (of 63 total, minute-bar-holdable subset) showed the current exit rule's median -85.4bp improving to -7.6bp under a fixed +100bp/-100bp rule, but the sample is small enough to be near-inconclusive. Disabled since 2026-08-25's KR-focused realignment.",
        "refs": [],
    },
    "orb_scan": {
        "category": "intraday",
        "theory_ko": "orb의 임의 종목판 — 개장 레인지 돌파가 특정 두 종목(TQQQ/SQQQ)만이 아니라 관심종목 전반에서도 그날 방향을 예고한다는 가설.",
        "theory_en": "The any-symbol version of orb — hypothesis that an opening-range breakout signals the day's direction not just for TQQQ/SQQQ but across the watchlist universe.",
        "entry": _entry_orb_scan, "exit": _exit_scan_common,
        "evidence_ko": "burn_in — TQQQ 전용으로 최적화된 파라미터를 임의 종목에 그대로 이식한 것이라 KR 재측정 전까지 근거가 없다. 종결 표본 0건. 2026-08-25 한국장 4종 체제 재편으로 비활성.",
        "evidence_en": "burn_in — reuses parameters optimized for TQQQ on arbitrary symbols without re-validation. Zero closed trades so far. Disabled since 2026-08-25's KR-focused realignment.",
        "refs": [],
    },
    "mean_reversion": {
        "category": "swing",
        "theory_ko": "과매도(20일 z-score·RSI(2) 저점) 구간의 KR ETF는 반등한다는 평균회귀 가설(NYU Stern 백테스트 서베이 2025, PAMR/CWMR/OLMAR 계열).",
        "theory_en": "Mean-reversion hypothesis that oversold KR ETFs (low 20-day z-score and RSI(2)) tend to bounce back (NYU Stern backtest survey 2025, PAMR/CWMR/OLMAR family).",
        "entry": _entry_mean_reversion, "exit": _exit_mean_reversion,
        "evidence_ko": "burn_in — 백테스트 미검증(문헌 인용). 오버나이트 보유가 전략 정의라 2026-09-03 '자동매매는 단타·스캘핑만' 정책으로 비활성(성과가 나빠서가 아니라 정책 대상).",
        "evidence_en": "burn_in — unvalidated by backtest (a literature citation only). Disabled since the 2026-09-03 'auto-trading is intraday/scalping only' policy because holding overnight is core to this strategy's definition, not because of poor performance.",
        "refs": [],
    },
    "cross_momentum": {
        "category": "swing",
        "theory_ko": "관심종목 중 최근 상대 수익률이 높은 종목이 계속 앞선다는 횡단면 모멘텀 가설(Jegadeesh & Titman 계열 상대강도 로테이션).",
        "theory_en": "Cross-sectional momentum hypothesis that watchlist symbols with the strongest trailing relative return keep outperforming (Jegadeesh & Titman-style relative-strength rotation).",
        "entry": _entry_cross_momentum, "exit": _exit_cross_momentum,
        "evidence_ko": "burn_in — 백테스트 미검증(문헌 인용). 모멘텀 크래시 위험(급락 후 급반등 국면에서 대규모 손실 보고)을 모듈 docstring이 고지한다. 오버나이트 보유가 전략 정의라 2026-09-03 정책으로 비활성.",
        "evidence_en": "burn_in — unvalidated by backtest (literature citation only). The module docstring discloses momentum-crash risk (reported large losses in sharp-drop-then-rebound regimes). Disabled since the 2026-09-03 intraday-only policy because overnight holding is core to its definition.",
        "refs": [],
    },
    "confluence": {
        "category": "intraday",
        "theory_ko": "MACD·볼린저·RSI·이동평균·박스권 5개 보조지표 중 다수(3개 이상)가 동시에 동의할 때만 진입하면 단일 지표보다 신뢰도가 높다는 다중 신호 합류 가설.",
        "theory_en": "Signal-confluence hypothesis: requiring a majority (3+) of five indicators (MACD, Bollinger, RSI, moving averages, range-box) to agree simultaneously is more reliable than any single indicator.",
        "entry": _entry_confluence, "exit": _exit_confluence,
        "evidence_ko": "burn_in — 파라미터 전부 문헌 표준값(튜닝 없음), 백테스트 미검증. 2026-08-25 한국장 4종 체제 재편으로 비활성.",
        "evidence_en": "burn_in — all parameters use textbook default values (no tuning), unvalidated by backtest. Disabled since 2026-08-25's KR-focused realignment.",
        "refs": [],
    },
    "news_momentum": {
        "category": "intraday",
        "theory_ko": "당일 뉴스 호재(EVENT 태그)가 붙은 종목은 개장 직후 매수해도 그 상승세가 장중 이어진다는 뉴스 모멘텀 가설.",
        "theory_en": "News-momentum hypothesis that symbols tagged with same-day positive news (EVENT) keep rising through the session even if bought right at the open.",
        "entry": _entry_news_momentum, "exit": _exit_news_momentum,
        "evidence_ko": "burn_in — 초기 표본에서 전 전략 중 최악(6청산 1승, 순 −27,018원)이었으나 원인(정보 없이 시가를 사던 결함)을 찾아 '첫봉 양봉 확인' 규칙을 추가했다. 2026-09-03부터 news_scalp를 '같은 유니버스·다른 진입시점' 대조군으로 삼은 A/B로 재검증 중(`run scoreboard --ab`).",
        "evidence_en": "burn_in — an early sample was the worst of all strategies (6 exits, 1 win, net -27,018 KRW); the root cause (buying at the open with zero price information) was found and a 'first bar must close bullish' rule was added. Since 2026-09-03 it is being re-evaluated in an A/B pairing against news_scalp ('same universe, different entry timing'; see `run scoreboard --ab`).",
        "refs": [],
    },
    "news_scalp": {
        "category": "intraday",
        "theory_ko": "news_momentum과 같은 뉴스 촉매 종목(EVENT_SCALP)을 대상으로 부분익절 없이 단순 손절+마감청산만 쓰면, '진입 시점'만 다른 결과를 news_momentum과 비교해 볼 수 있다는 A/B 설계.",
        "theory_en": "An A/B design: trading the same news-catalyst symbols (EVENT_SCALP) as news_momentum but with a simpler stop+close-only exit (no partial take-profit) isolates the effect of entry timing when compared against news_momentum.",
        "entry": _entry_news_scalp, "exit": _exit_news_scalp,
        "evidence_ko": "burn_in — 2026-08-25 비활성 이전 표본 n=6, −174.5bp(표본 밖이라 판단 불가). 2026-09-03 A/B 목적으로 재활성했고, 배분을 프로토콜 하한 바로 위(KR 4%)로 최소화해 표본을 쌓는 중이다.",
        "evidence_en": "burn_in — before its 2026-08-25 deactivation, a sample of n=6 showed -174.5bp (too small to be conclusive). Reactivated 2026-09-03 for A/B purposes at a minimal allocation (KR 4%, just above the protocol floor) while it accumulates a sample.",
        "refs": [],
    },
    "frgn_accumulate": {
        "category": "swing",
        "theory_ko": "외국인 순매수가 여러 날 이어지는(FRGN 태그) 종목을 매일 조금씩 사서 추세를 따라가고, 이탈(FRGN_EXIT) 시 나눠 파는 외국인 수급 추세추종 가설.",
        "theory_en": "Foreign-flow trend-following hypothesis: accumulate small daily buys in symbols with sustained net foreign buying (tagged FRGN), and scale out on a reversal (tagged FRGN_EXIT).",
        "entry": _entry_frgn_accumulate, "exit": _exit_frgn_accumulate,
        "evidence_ko": "burn_in — 백테스트 미검증(원장 20거래일 승격 기준 도달 전). 오버나이트 보유가 전략 정의라 2026-09-03 정책으로 비활성. 과거 일별 재평가가 그날 리포트 랭킹 안 종목만 대상이라 랭킹 밖 보유 종목이 영영 재평가 안 되던 결함(매수 34/매도 1 불균형)을 2026-09-03 수리했다.",
        "evidence_en": "burn_in — unvalidated by backtest (had not yet reached the 20-trading-day promotion threshold). Disabled since the 2026-09-03 policy because overnight holding is core to its definition. A bug where daily re-evaluation only covered symbols still in that day's report ranking (causing a 34-buy/1-sell imbalance) was fixed 2026-09-03.",
        "refs": [],
    },
    "close_bet": {
        "category": "swing",
        "theory_ko": "장중 리포트가 수급·거래대금·뉴스 지속성으로 채점한 마감 강세 종목(CLOSE_BET 태그)을 오후 마감 직전에 사면, 다음날 아침 상승 갭이 실현된다는 오버나이트 갭 실현 가설.",
        "theory_en": "Overnight-gap-realization hypothesis: buying a strong-close candidate (tagged CLOSE_BET, scored intraday by flow/turnover/news persistence) just before the afternoon close captures a next-morning upward gap.",
        "entry": _entry_close_bet, "exit": _exit_close_bet,
        "evidence_ko": "burn_in — 웹 리서치 근거, 원장 표본은 experiments 루프가 판정. 배분 부족(예산 95,888원으로 대형주 1주도 못 삼)으로 8/25 이후 체결 0건이던 결함을 target_weight 0.1→0.5로 수리(2026-09-03). 오버나이트 보유가 전략 정의라 같은 날 정책으로 비활성.",
        "evidence_en": "burn_in — web-research-based; sample judgment is left to the experiments loop. A budget bug (95,888 KRW couldn't buy even 1 large-cap share) causing zero fills since 8/25 was fixed by raising target_weight 0.1→0.5 (2026-09-03). Disabled the same day since overnight holding is core to its definition.",
        "refs": [],
    },
    "overnight_drift": {
        "category": "swing",
        "theory_ko": "지수 ETF 수익의 대부분이 장중이 아니라 '밤'(종가~다음 개장)에서 발생한다는 오버나이트 드리프트 가설(Lachance, Review of Financial Economics 2023 등).",
        "theory_en": "Overnight-drift hypothesis that most index-ETF returns accrue overnight (close to next open) rather than intraday (Lachance, Review of Financial Economics 2023, among others).",
        "entry": _entry_overnight_drift, "exit": _exit_overnight_drift,
        "evidence_ko": "burn_in — 표본 0(문헌 인용이지 우리 실측이 아니다). 오버나이트 보유가 전략 정의라 2026-09-03 정책으로 비활성.",
        "evidence_en": "burn_in — zero samples so far (a literature citation, not our own measurement). Disabled since the 2026-09-03 policy because overnight holding is core to its definition.",
        "refs": [],
    },
    "pullback_impulse": {
        "category": "intraday",
        "theory_ko": "원장 실측 — scalp_1m 손절 건의 76%(35/46)가 당일 진입가 위로 회복(중앙 +105bp): 방향은 맞았지만 파동 고점에서 샀다는 뜻이라, 같은 임펄스 신호를 눌림목까지 기다렸다 사는 가설.",
        "theory_en": "Ledger-derived hypothesis: 76% (35/46) of scalp_1m stop-outs recovered above the entry price same-day (median +105bp) — the direction was right but entries were made at the impulse peak, so this strategy buys the same impulse signal on its pullback instead.",
        "entry": _entry_pullback_impulse, "exit": _exit_pullback_impulse,
        "evidence_ko": "burn_in — 백테스트 표본 0(Toss 5분봉 롤링 히스토리 한계). 리서치 스크리닝(US 1분봉 10.7년 OOS)에서 gross +0.9bp로 유일하게 양수였지만 실측 왕복비용 10.5bp를 못 이겨 순손실 — 게이트 통과 0건. paper 번인이 유일한 검증 경로.",
        "evidence_en": "burn_in — zero backtest samples (limited by Toss's rolling 5-min history). A 10.7-year US OOS screen found the only positive gross edge among the three tested (+0.9bp), but it still lost net after the measured 10.5bp round-trip cost — zero strategies passed the gate.",
        "refs": [],
    },
    "mr_vwap_quiet": {
        "category": "intraday",
        "theory_ko": "원장 실측 — 고거래량(RVOL) 진입은 오히려 나쁘다(D+1 스피어만 −0.46, 장중 고RVOL −79.8bp vs 저RVOL −31.3bp): 정반대로 '조용한' 종목이 밴드 밖에서 안으로 복귀할 때만 매매하는 가설.",
        "theory_en": "Ledger-derived hypothesis (high relative-volume entries measured worse: Spearman -0.46 on D+1, -79.8bp for high-RVOL vs -31.3bp for low-RVOL intraday): trades only 'quiet' symbols reverting from outside a band back inside it.",
        "entry": _entry_mr_vwap_quiet, "exit": _exit_mr_vwap_quiet,
        "evidence_ko": "burn_in — 백테스트 표본 0. 문헌이 '스프레드 조정 시 단기 반전의 유의성 급감'을 반복 경고 — KR 개별주에서 먼저 죽고 US·KR ETF에서만 살아남을 수 있다는 것을 알고 가동한다.",
        "evidence_en": "burn_in — zero backtest samples. The literature repeatedly warns that short-term reversal significance collapses after spread adjustment; this is run knowing it may fail first on KR individual stocks and survive only on US/KR ETFs, if at all.",
        "refs": [],
    },
    "vol_breakout": {
        "category": "intraday",
        "theory_ko": "시가에 전일 레인지의 일정 비율을 더한 값을 상향 돌파하면 그날 상승이 확장된다는 변동성 돌파 가설(Larry Williams 원 공식).",
        "theory_en": "Volatility-breakout hypothesis that a break above open + a fraction of the prior day's range signals expanding upside for the day (Larry Williams' original formula).",
        "entry": _entry_vol_breakout, "exit": _exit_vol_breakout,
        "evidence_ko": "burn_in — Larry Williams 원 공식 인용, 우리 원장 실측 0. KR은 ETF 전용(왕복 ~4bp 실측 — 비용을 이기는 유일한 KR venue), US는 TQQQ.",
        "evidence_en": "burn_in — a citation of Larry Williams' original formula, zero measurements from our own ledger. KR trades ETFs only (measured ~4bp round-trip cost, the only KR venue that beats cost); US trades TQQQ.",
        "refs": [],
    },
    "intraday_momentum": {
        "category": "intraday",
        "theory_ko": "장중 변동성 밴드를 이탈한 방향으로 추세가 이어진다는 노이즈 밴드 돌파 가설(Zarattini·Aziz·Barbon 2024, SPY 2007~2024 비용 차감 후 Sharpe 1.33 보고).",
        "theory_en": "Noise-band breakout hypothesis that a break outside an intraday volatility band tends to continue in that direction (Zarattini, Aziz & Barbon 2024, reporting a cost-adjusted Sharpe of 1.33 on SPY 2007-2024).",
        "entry": _entry_intraday_momentum, "exit": _exit_intraday_momentum,
        "evidence_ko": "burn_in — 문헌 인용(SPY 대상), 우리 원장 실측 0. 롱온리 계좌라 하락 신호는 인버스(SQQQ) 매수로 표현 — 이 저장소의 '하락장 레인'.",
        "evidence_en": "burn_in — a literature citation (measured on SPY), zero measurements from our own ledger. Since the account is long-only, downside signals are expressed by buying the inverse (SQQQ) — this repo's 'bear-market lane'.",
        "refs": [{"label": "Zarattini, Aziz & Barbon (2024), SSRN 4824172", "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172"}],
    },
    "gap_fade": {
        "category": "intraday",
        "theory_ko": "야간에 생긴 시가 갭은 개장 후 일정 부분 되돌려진다는 갭 페이드 가설(Della Corte·Kosowski, Akbas 2022 등) — 단, 선물 시장 반증 연구가 있어 근거가 혼재된 전략이다.",
        "theory_en": "Gap-fade hypothesis that an overnight opening gap partially reverts after the open (Della Corte & Kosowski, Akbas 2022, among others) — evidence is mixed, as a futures-market study contradicts it.",
        "entry": _entry_gap_fade, "exit": _exit_gap_fade,
        "evidence_ko": "burn_in — 근거가 8개 레인 중 가장 약해 소액으로 출전 중. 원장 실측 9왕복 승률 11%(사실상 전패) — 이 방향과 일치하는 문헌 필터(고RVOL·대형 갭 제외)만 2026-09-02 추가했다.",
        "evidence_en": "burn_in — the weakest-evidenced of the 8 parallel scalping lanes, run at minimal size for that reason. Ledger data (9 round trips) showed an 11% win rate (near-total losses) — literature-based filters (excluding high-RVOL and oversized gaps) matching that direction were added 2026-09-02.",
        "refs": [{"label": "arXiv:2605.04004 (gap-fill fade study)", "url": "https://arxiv.org/abs/2605.04004"}],
    },
    "rsi2_dip": {
        "category": "swing",
        "theory_ko": "추세 위 지수 ETF가 단기 과매도(RSI(2)<10)에 빠지면 반등한다는 Connors RSI(2) 눌림매수 가설(문헌 승률 75~79%).",
        "theory_en": "Connors RSI(2) dip-buying hypothesis: index ETFs above their trend tend to bounce after brief oversold readings (RSI(2)<10); the literature reports a 75-79% win rate.",
        "entry": _entry_rsi2_dip, "exit": _exit_rsi2_dip,
        "evidence_ko": "burn_in — 우리 원장 실측이 없다(승률 수치는 Connors 원전 인용). 원전은 손절이 없지만 실계좌 레일로 하드 손절을 추가했다. 오버나이트 보유형이라 2026-09-03 정책으로 비활성.",
        "evidence_en": "burn_in — no measurement of our own (the win-rate figures are a citation of the Connors original). The original has no stop-loss; a hard stop was added as a live-account safety rail. Disabled since the 2026-09-03 policy because it is an overnight-carry design.",
        "refs": [],
    },
    "scalp_1m": {
        "category": "intraday",
        "theory_ko": "1분봉 거래량 서지 또는 이동평균 지지를 짧게 잡아 스캘핑하되, 손절은 구조(스윙 저점) 기반으로 두고 익절은 본전 이동+트레일링으로 상방을 열어 둔다는 가설 — 원장 실측 반사실 분석으로 규칙을 여러 차례 다듬었다.",
        "theory_en": "Scalps short 1-min volume-surge or moving-average-support setups, using a structure-based (swing-low) stop and a breakeven-plus-trailing exit to leave upside open — rules have been iteratively refined via counterfactual replay of ledger data.",
        "entry": _entry_scalp_1m, "exit": _exit_scalp_1m,
        "evidence_ko": "burn_in — KR 1년 분봉 게이트 NO_GO(6폴드 전부 비양수, 2026-09-04). US 10.7년 OOS(12폴드)에서도 gross −2.2bp로 실측 왕복비용 10.5bp를 못 이겨 순손실, DSR≈0, 게이트 통과 0건. '수수료가 엣지보다 크다'는 원장의 결론을 10년 데이터로 재현한 사례.",
        "evidence_en": "burn_in — the 1-year KR minute-bar gate returned NO_GO (all 6 folds non-positive, 2026-09-04). A 10.7-year US OOS screen (12 folds) also showed a -2.2bp gross edge, losing net against the measured 10.5bp round-trip cost, DSR≈0, zero gates passed.",
        "refs": [{"label": "arXiv:1005.3535 (opening-volatility research cited for KR entry delay)", "url": "https://arxiv.org/abs/1005.3535"}],
    },
    "llm_trader": {
        "category": "experimental",
        "theory_ko": "규칙을 사람이 코드로 정하는 대신, 그 판단 자체를 LLM에 맡기면 규칙 기반 전략과 다른 방식으로 기회를 잡을 수 있는지 시험하는 실험 레인(12번째 참가자, 한 달 테스트).",
        "theory_en": "An experimental lane testing whether handing the trading decision itself to an LLM — instead of hand-coded rules — can find opportunities the rule-based strategies miss (the 12th entrant, run as a one-month trial).",
        "entry": _entry_llm_trader, "exit": _exit_llm_trader,
        "evidence_ko": "burn_in — 백테스트 표본 0(LLM 판단은 리플레이 불가). paper 번인(한 달 테스트)이 유일한 검증 경로. LLM 호출은 거래 핫패스 밖 별도 프로세스에서만 이뤄진다(엔진은 인박스 파일을 읽기만 함).",
        "evidence_en": "burn_in — zero backtest samples (an LLM's judgment cannot be replayed). Paper burn-in (a one-month trial) is the only validation path. The LLM call happens only in a separate process outside the trading hot path — the engine only reads an inbox file.",
        "refs": [],
    },
    "orb_rvol": {
        "category": "intraday",
        "theory_ko": "그날 개장 상대거래량이 유독 높은 종목('stocks in play')만 골라 개장 레인지 돌파를 매매하면 신호 품질이 올라간다는 가설(Zarattini, Barbon & Aziz 2024).",
        "theory_en": "Hypothesis that filtering opening-range breakouts to only the day's highest-relative-volume symbols ('stocks in play') improves signal quality (Zarattini, Barbon & Aziz 2024).",
        "entry": _entry_orb_rvol, "exit": _exit_orb_rvol,
        "evidence_ko": "burn_in, enabled: false — US 10.7년 OOS gross −1.2bp 순손실, KR 1년 분봉(40종목) 게이트 NO_GO(2026-09-04): 96왕복 승률 12.5%, 손절 0.10×ATR14가 너무 얕아 86%가 손절, 마감까지 버틴 13건만 +367bp. US 400종목 stocks-in-play 재현도 상대거래량 단조성 미확인. 논문(in-sample) 손절폭이 KR 변동성에 안 맞는다는 신호.",
        "evidence_en": "burn_in, enabled: false — US 10.7-year OOS gross −1.2bp (net loss). KR 1-year minute-bar gate (40 names) NO_GO on 2026-09-04: 96 round trips, 12.5% win rate, the 0.10×ATR14 stop is so tight that 86% were stopped out while the 13 that held to close averaged +367bp. The US 400-name stocks-in-play replication also failed to reproduce relative-volume monotonicity. The paper's (in-sample) stop width does not fit KR volatility.",
        "refs": [{"label": "Zarattini, Barbon & Aziz (2024), \"Stocks in Play\", SSRN 4729284", "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284"}],
    },
    "eod_reversal": {
        "category": "intraday",
        "theory_ko": "하루 수익 중 시가~종가(주간) 반전 효과가 장 마지막 한 시간에 집중된다는 문헌(Baltussen·Da·Soebhag 2024 계열 + KOSPI 일중 반전 실증)에 따라, 마감 45분 전 그날 밀린 종목을 사서 종가에만 파는 가설.",
        "theory_en": "Hypothesis, drawn from literature finding that intraday (open-to-close) reversal concentrates in the final trading hour (Baltussen, Da & Soebhag 2024-family research plus KOSPI intraday-reversal evidence), that buying that day's losers 45 minutes before close and selling at the close captures that reversal.",
        "entry": _entry_eod_reversal, "exit": _exit_eod_reversal,
        "evidence_ko": "burn_in, enabled: false — KR 1년 분봉(40종목) 게이트 NO_GO(2026-09-04): OOS 1,141왕복 기대값 −11.5bp(비용 2배 −44.7bp), 손절 36건이 −178bp로 손실 주도. US 317종목 스크리닝도 순 −23.6bp(t=−16). 문헌의 마감 반전은 롱숏 포트폴리오 효과라 롱온리 개별 진입으로는 비용을 못 넘는다.",
        "evidence_en": "burn_in, enabled: false — zero samples; will be enabled only after a human confirms it passes backtest. KR 1-year minute-bar gate (40 names) NO_GO on 2026-09-04: 1,141 OOS round trips, expectancy −11.5bp (−44.7bp at 2× cost), 36 stops averaging −178bp drove the loss; the US 317-name screen was also net −23.6bp (t=−16). The literature effect is a long-short portfolio effect and does not clear cost as long-only single-name entries.",
        "refs": [],
    },
    "trend_day": {
        "category": "intraday",
        "theory_ko": "1분·5분 아이디어가 전부 왕복 비용에 죽은 뒤 세운 가설 — 거래당 총 엣지를 키우려면 봉을 15분으로 늘리고, 개장 30분 레인지가 ATR 대비 크게 벌어진 '추세일'만 골라, 넓은 VWAP 기준 손절로 마감까지 들고 가야 한다(Zarattini 계열 ORB + trend day 실무 통계).",
        "theory_en": "A hypothesis formed after every 1- and 5-minute idea died on round-trip cost: to make the gross edge per trade larger, widen bars to 15 minutes, take only 'trend days' whose first 30-minute range is wide relative to ATR, and hold to the close behind a wide VWAP-anchored stop (Zarattini-family ORB plus practitioner trend-day statistics).",
        "entry": _entry_trend_day, "exit": _exit_trend_day,
        "evidence_ko": "burn_in, enabled: false — 원장 표본 0. US 400종목 1분봉(2024-09~2026-08) 스크리닝에서 7,472거래 총 −9.4bp로 게이트 미달(2026-09-04). 국면 분리는 실제로 작동했다(상승 +3.2bp / 하락 −30.1bp)지만 좋은 쪽조차 왕복 25.2bp의 1/3이다. 개장 레인지 문턱을 올리면 거래당 엣지가 단조 증가(2.5배에서 +41bp)하나 그 구간은 n=82로 날짜군집 t≈0이다.",
        "evidence_en": "burn_in, enabled: false — zero ledger samples. Screening on 400 US names (1-minute bars, 2024-09 to 2026-08) gave 7,472 trades at −9.4bp gross, below the gate (2026-09-04). The regime split did work (up +3.2bp vs down −30.1bp), but even the good half is a third of the 25.2bp round-trip cost. Raising the opening-range threshold makes per-trade edge rise monotonically (+41bp at 2.5x), but that slice has n=82 and a date-clustered t near zero.",
        "refs": [{"label": "Zarattini, Barbon & Aziz (2024), \"Stocks in Play\", SSRN 4729284", "url": "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284"}],
    },
    "open_reversal": {
        "category": "intraday",
        "theory_ko": "eod_reversal의 형제 전략 — 전일 크게 하락한 KR 종목이 다음날 개장 직후 반전한다는 국내 단기 반전 문헌(KCI 등재 2023, KAIST 계열 야간/일중 분해 연구) 기반 가설.",
        "theory_en": "A sibling strategy to eod_reversal — the hypothesis, based on Korean short-term-reversal literature (a 2023 KCI-indexed study and KAIST-affiliated overnight/intraday decomposition research), that KR symbols that fell sharply the prior day reverse right after the next day's open.",
        "entry": _entry_open_reversal, "exit": _exit_open_reversal,
        "evidence_ko": "burn_in, enabled: false — KR 1년 분봉(40종목) 게이트 NO_GO(2026-09-04): OOS 324왕복 기대값 −21.9bp(비용 2배 −34.3bp), 최악 폴드 −42.6bp. 전일 낙폭 종목의 시가 매수는 KR 개별주에서 비용을 못 넘었다. 국내 문헌의 반전 근거는 월간·주간 지평선이라 1일 지평선에는 직접 적용되지 않는다는 점도 확인.",
        "evidence_en": "burn_in, enabled: false — KR 1-year minute-bar gate (40 names) NO_GO on 2026-09-04: 324 OOS round trips, expectancy −21.9bp (−34.3bp at 2× cost), worst fold −42.6bp. Buying prior-day losers at the open did not clear cost on KR single names; the Korean reversal literature is monthly/weekly-horizon and does not transfer to a 1-day horizon.",
        "refs": [],
    },
}


def _cap(text: str, limit: int = 400) -> str:
    """`limit`자 넘으면 마지막 공백에서 자른다(단어 중간 절단 방지) — 정상
    콘텐츠는 애초에 이 한도 안에 들게 쓴다, 이건 방어선일 뿐이다."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    sp = cut.rfind(" ")
    return cut[:sp] if sp > 0 else cut


def _generic_help() -> dict:
    return {
        "category": "experimental",
        "theory_ko": "문서화되지 않은 전략 — 이 전략의 가설을 아직 정리하지 못했다.",
        "theory_en": "Undocumented strategy — its hypothesis has not been written up yet.",
        "entry_ko": "진입 규칙 문서 없음.",
        "entry_en": "No entry-rule documentation available.",
        "exit_ko": "청산 규칙 문서 없음.",
        "exit_en": "No exit-rule documentation available.",
        "evidence_ko": "문서 없음.",
        "evidence_en": "No documentation.",
        "refs": [],
    }


def _sizing(strategies_cfg: dict, sid: str, base: str) -> tuple[str, str]:
    cfg = strategies_cfg.get(sid) or {}
    cf = cfg.get("capital_fraction") or {}
    params = cfg.get("params") or {}
    # 레거시 설정 중 일부(예: 비활성 `orb`)는 capital_fraction이 시장별 dict가
    # 아니라 스칼라다 — 시장 분리(2026-08-12) 이전 형식. 그 경우 두 시장에
    # 같은 값을 적용한다(지어내지 않는다 — 설정에 실제로 있는 값 그대로).
    if isinstance(cf, dict):
        kr = cf.get("KR") or 0
        us = cf.get("US") or 0
    else:
        kr = us = float(cf or 0)
    clauses_ko: list[str] = []
    clauses_en: list[str] = []
    if kr:
        clauses_ko.append(f"KR 자본배분 {_pct(kr)}%")
        clauses_en.append(f"KR capital allocation {_pct(kr)}%")
    if us:
        clauses_ko.append(f"US 자본배분 {_pct(us)}%")
        clauses_en.append(f"US capital allocation {_pct(us)}%")
    if not clauses_ko:
        clauses_ko.append("현재 배분 없음(0%)")
        clauses_en.append("no current allocation (0%)")
    tw = params.get("target_weight")
    if tw is not None:
        clauses_ko.append(f"1회 진입 규모 = 전략자본 x {_pct(tw)}%(target_weight)")
        clauses_en.append(f"per-entry size = strategy capital x {_pct(tw)}% (target_weight)")
    if base in _OVERNIGHT_STRATEGIES:
        clauses_ko.append("오버나이트 보유형 — 장중 손절 −5%/목표 +10% 하드레일 적용 제외")
        clauses_en.append(f"overnight-carry design — exempt from the intraday -{_HARD_STOP_PCT:g}%/+{_TARGET_CAP_PCT:g}% hard stop/target rail")
    else:
        clauses_ko.append(f"장중 하드레일: 손절 상한 −{_HARD_STOP_PCT:g}%/목표 상한 +{_TARGET_CAP_PCT:g}%")
        clauses_en.append(f"intraday hard rail: stop capped at -{_HARD_STOP_PCT:g}%, target capped at +{_TARGET_CAP_PCT:g}%")
    if kr:
        clauses_ko.append(f"KR: 전일·당일 상한가(+{_LIMIT_UP_PCT:g}%) 종목 진입 금지 레일 적용")
        clauses_en.append(f"KR: blocked from entering prior-day/same-day limit-up (+{_LIMIT_UP_PCT:g}%) stocks")
    return " · ".join(clauses_ko), "; ".join(clauses_en)


def _catalyst_sentence(strategies_cfg: dict, sid: str, base: str) -> tuple[str, str]:
    """A/B 촉매 갈래(`<id>_cat`)의 진입 규칙 뒤에 붙이는 한 문장 — 실제
    `universe_filter`(config/settings.yaml)를 읽어 어떤 태그를 보는지 그
    자리에서 낸다(하드코딩 아님)."""
    uf = (strategies_cfg.get(sid) or {}).get("universe_filter") or {}
    tags: set[str] = set()
    for market_filter in uf.values():
        if isinstance(market_filter, dict):
            for key in ("require_any", "require_all", "exclude_any", "exclude_all"):
                tags.update(market_filter.get(key) or [])
    tag_str = "/".join(sorted(tags)) if tags else "촉매"
    ko = f" (A/B 촉매 갈래 — {tag_str} 태그가 붙은 종목만 대상)"
    en = f" (A/B catalyst arm — only trades symbols tagged {tag_str})"
    return ko, en


def build_strategy_help(sid: str, strategies_cfg: dict | None = None) -> dict:
    """전략 id → 공개 사이트용 `help` 객체. `strategies_cfg`는
    `config/settings.yaml`의 `strategies:` 블록(`quant.apps.config.Settings.
    strategies`)과 같은 형식 — 없거나(`None`) 그 id가 없으면 파라미터 의존
    문장은 해당 절만 조용히 빠진다(지어내지 않는다, `_clause` 참고).

    레지스트리에 아직 못 채운 id(또는 완전히 낯선 id — 예: 테스트 픽스처)는
    크래시 대신 `category: experimental`/`evidence: "문서 없음"` 일반
    fallback을 낸다."""
    strategies_cfg = strategies_cfg or {}
    base = base_strategy_id(sid)
    spec = _SPECS.get(base)
    own_cfg = strategies_cfg.get(sid) or {}
    params = own_cfg.get("params") or {}

    if spec is None:
        help_ = _generic_help()
    else:
        entry_ko, entry_en = spec["entry"](params)
        exit_ko, exit_en = spec["exit"](params)
        help_ = {
            "category": spec["category"],
            "theory_ko": spec["theory_ko"],
            "theory_en": spec["theory_en"],
            "entry_ko": entry_ko or "진입 규칙을 계산할 파라미터가 없다.",
            "entry_en": entry_en or "No parameters available to describe the entry rule.",
            "exit_ko": exit_ko or "청산 규칙을 계산할 파라미터가 없다.",
            "exit_en": exit_en or "No parameters available to describe the exit rule.",
            "evidence_ko": spec["evidence_ko"],
            "evidence_en": spec["evidence_en"],
            "refs": list(spec.get("refs") or []),
        }

    if is_catalyst_arm(sid):
        cat_ko, cat_en = _catalyst_sentence(strategies_cfg, sid, base)
        help_["entry_ko"] = help_["entry_ko"] + cat_ko
        help_["entry_en"] = help_["entry_en"] + cat_en
        note_ko = f" {base}와 파라미터는 동일, 유니버스만 다른 A/B 짝 — `run scoreboard --ab`(양쪽 n≥30 전엔 판단 불가)로 판정."
        note_en = f" An A/B pair with {base}: identical parameters, different universe — judged via `run scoreboard --ab` (n>=30 both arms)."
        help_["evidence_ko"] = help_["evidence_ko"] + note_ko
        help_["evidence_en"] = help_["evidence_en"] + note_en

    sizing_ko, sizing_en = _sizing(strategies_cfg, sid, base)
    help_["sizing_ko"] = sizing_ko
    help_["sizing_en"] = sizing_en

    for key in ("theory_ko", "theory_en", "entry_ko", "entry_en", "exit_ko", "exit_en",
                "sizing_ko", "sizing_en", "evidence_ko", "evidence_en"):
        help_[key] = _cap(help_[key])

    return help_
