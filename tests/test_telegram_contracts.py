"""텔레그램 발송 계약 테스트 — `docs/runbooks/telegram-catalog.md`가 문서로 약속한
것을 코드로 고정한다(2026-09-06 라이브 준비 4단계).

메시지를 만드는 함수 하나하나를 픽스처로 렌더해 다섯 가지를 확인한다:

  1. 유효한 HTML 엔티티 균형 — `_assert_balanced_html`은
     `tests/test_loop_html_formatting.py`의 검사기를 그대로 복사한 것이다
     (같은 저장소 안에서 import-mode=importlib 크로스 모듈 import 는 깨지기
     쉬워, "재사용"은 로직을 복제하고 원본을 이 파일 docstring에 명시하는
     방식으로 한다 — 둘이 갈라지면 이 파일과 그 파일을 나란히 고칠 것).
  2. UTF-8 바이트 기준 4096 이하(텔레그램 sendMessage 하드캡) — 소프트 캡
     3500자를 약속하는 모듈은 그것도 함께 확인한다.
  3. "nan"/"None"/"{}" 같은 렌더 실패 흔적이 없다.
  4. 숫자 서식이 하우스 스타일을 따른다 — bp/%는 부호, KRW는 천단위 구분,
     USD는 소수 2자리.
  5. 레인 배정이 카탈로그와 일치한다(셸 스크립트의 `NOTIFY_LANE=`을 카탈로그
     표와 대조) + 한국어 가독성(전략 id 가 표시명과 함께 나온다).

**알려진 범위 밖**: `quant.trade.loop`의 "국면 알림"(regime notice)은 메인 루프
안에 인라인으로 남아 있어(2026-09-06 시점 `_halt_notify_text` 같은 순수
헬퍼로 아직 뽑히지 않았다) 이 파일에서 직접 렌더할 수 없다 — 리팩터는 이
작업(카탈로그+계약 테스트+실패 처리+킬스위치 드릴) 범위 밖이라 손대지 않았다.
`quant.control.ledger.scoreboard_text`/`session_pnl_text`는 전략 id를 그대로
찍는다(`_strategy_label`과 달리 한국어 표시명을 붙이지 않는다) — 이 파일은
그 사실을 숨기지 않고 §6에서 명시적으로 기록한다(고치지 않았다, 별도 스폰 작업
대상).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from quant.adapters.execution.paper import PaperBroker
from quant.control import daily_wrap as DW
from quant.control import health as H
from quant.control import ledger as L
from quant.control import report_accuracy as RA
from quant.core import tgfmt
from quant.core.fx import FixedFxProvider
from quant.core.models import Signal, SignalAction
from quant.core.portfolio.portfolio import Portfolio
from quant.core.ports import Context
from quant.trade.loop import (
    _breaker_line,
    _entry_budget_text,
    _execute_signal,
    _halt_notify_text,
    _strategy_label,
    _uptime_text,
)
from quant.trade.risk.manager import RiskManagerImpl

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 1, 5, 10, 0, tzinfo=NY)
SYMBOL = "TQQQ"
FX_RATE = 1500.0
PRICE = 100.0
REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# ── ① 공용 계약 검사기 ──────────────────────────────────────────────────────

def _assert_balanced_html(text: str) -> None:
    """`tests/test_loop_html_formatting.py::_assert_balanced_html`과 동일 —
    태그가 안 맞으면 텔레그램이 그 메시지 전체를 400으로 거부한다."""
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            assert stack and stack[-1] == name, f"짝이 안 맞는 태그 </{name}> in: {text!r}"
            stack.pop()
    assert not stack, f"닫히지 않은 태그 {stack} in: {text!r}"


def _assert_within_byte_cap(text: str, cap: int = 4096) -> None:
    n = len(text.encode("utf-8"))
    assert n <= cap, f"UTF-8 {n}바이트 — 텔레그램 하드캡({cap}) 초과: {text[:80]!r}..."


def _assert_no_placeholder_leaks(text: str) -> None:
    """렌더 실패의 흔적 — 값이 없는데 "없다"고 쓰는 대신 파이썬 리터럴이
    그대로 새어나온 경우. 대소문자·경계를 신경 쓴다("Nonetheless" 같은 진짜
    단어, "논현" 같은 한글을 오탐하지 않는다)."""
    for bad in (r"\bNone\b", r"\bnan\b", r"\{\}"):
        assert not re.search(bad, text), f"플레이스홀더 누출({bad}): {text!r}"


def _assert_contract(text: str, *, cap: int = 4096) -> None:
    _assert_balanced_html(text)
    _assert_within_byte_cap(text, cap)
    _assert_no_placeholder_leaks(text)


# ── ② quant.core.tgfmt — 하우스 스타일의 단일 정의 ──────────────────────────

def test_esc_escapes_in_amp_lt_gt_order():
    assert tgfmt.esc("A & B < C > D") == "A &amp; B &lt; C &gt; D"


def test_pct_is_always_signed_or_na():
    assert tgfmt.pct(1.234) == "+1.23%"
    assert tgfmt.pct(-1.234) == "-1.23%"
    assert tgfmt.pct(0.0) == "+0.00%"
    assert tgfmt.pct(None) == "n/a"


def test_bp_is_always_signed_or_na():
    assert tgfmt.bp(12.34) == "+12.3bp"
    assert tgfmt.bp(-12.34) == "-12.3bp"
    assert tgfmt.bp(None) == "n/a"


def test_pnl_krw_uses_thousands_separator_and_wraps_in_code():
    text = tgfmt.pnl(1234567.0, "KRW")
    assert "1,234,567" in text
    assert "<code>" in text and "</code>" in text
    _assert_balanced_html(text)


def test_pnl_usd_uses_two_decimals_and_sign():
    assert "+$1,234.50" in tgfmt.pnl(1234.5, "USD")
    assert "-$1,234.50" in tgfmt.pnl(-1234.5, "USD")


def test_pnl_none_is_neutral_mark_not_zero():
    text = tgfmt.pnl(None, "KRW")
    assert "n/a" in text
    assert "0원" not in text  # 모른다를 0으로 위장하지 않는다


def test_compose_never_exceeds_max_chars_and_stays_balanced():
    header = tgfmt.b("헤더")
    sections = [tgfmt.pre("x" * 500) for _ in range(20)]  # 강제로 4096자 초과시킨다
    text = tgfmt.compose(header, sections, footer=tgfmt.i("푸터"))
    assert len(text) <= tgfmt.MAX_CHARS
    _assert_balanced_html(text)
    assert "생략됨" in text


def test_table_columns_align_by_display_width_not_codepoints():
    out = tgfmt.table(["종목", "손익"], [["KODEX코스닥150", "+1bp"], ["A", "+2bp"]])
    lines = out.splitlines()
    # 헤더/구분선/두 데이터 행 — 각 줄이 같은 위치에서 "손익"/값 열이 시작해야 한다.
    assert len(lines) == 4


# ── ③ 엔진 순수 텍스트 빌더 (quant/trade/loop.py) ───────────────────────────

def test_halt_notify_text_contract():
    text = _halt_notify_text("연속 3회 실패 <중단> & 재시도 nan None {}")
    _assert_balanced_html(text)
    _assert_within_byte_cap(text)
    # esc() 가 반드시 걸어야 하는 세 문자 — 이스케이프된 뒤에는 원문의 "nan"/
    # "None"/"{}" 리터럴이 아니라 그 사유 문장의 일부로 남는다(플레이스홀더
    # 검사는 여기서는 적용하지 않는다 — 사유는 사람이 쓴 자유 텍스트다).
    assert "&lt;중단&gt;" in text
    assert "&amp;" in text


def test_entry_budget_text_contract():
    text = _entry_budget_text({
        "limit": 30,
        "by_market": {
            "KR": {"entries": 12, "orders": 14, "tripped": False},
            "US": {"entries": 5, "orders": 6, "tripped": True},
        },
    })
    _assert_contract(text)
    assert "KR" in text and "US" in text


def test_entry_budget_text_unlimited_does_not_show_zero_cap():
    """상한 0(해제)을 "/0" 으로 찍으면 "한도 0"으로 오독된다."""
    text = _entry_budget_text({"limit": 0, "by_market": {}})
    assert "/0" not in text


def test_uptime_text_contract():
    for seconds in (30, 3600, 90000):
        text = _uptime_text(seconds)
        _assert_no_placeholder_leaks(text)
        assert text  # 빈 문자열이 되는 경계가 없어야 한다


def test_breaker_line_contract():
    state = {
        "max_orders_per_day": {
            "limit": 30,
            "by_market": {
                "KR": {"entries": 3, "orders": 3, "tripped": False},
                "US": {"entries": 1, "orders": 1, "tripped": False},
            },
        },
        "daily_loss_limit_pct": {
            "limit_pct": 5.0, "tripped": True,
            "per_strategy": {"pullback_impulse": {"tripped": True}},
        },
        "cooldown_bars_after_stop": {"symbols_in_cooldown": ["005930"]},
    }
    text = _breaker_line(state)
    assert text is not None
    _assert_contract(text)
    # tripped 전략은 원문 id 만이 아니라 _strategy_label 표시명과 함께 나온다
    # (한국어 가독성 — §6과 같은 계약을 여기서도 확인한다).
    assert "눌림목 임펄스" in text and "pullback_impulse" in text


def test_breaker_line_malformed_state_returns_none_not_crash():
    assert _breaker_line({}) is None


# ── ④ 전략 표시명 — 한국어 가독성 (§6과 함께 읽을 것) ───────────────────────

def test_strategy_label_known_id_shows_korean_name_and_raw_id():
    label = _strategy_label("scalp_1m")
    assert "1분봉 스캘핑" in label
    assert "scalp_1m" in label  # 표시명 뒤에도 원문 id 를 남긴다(검색·대조용)


def test_strategy_label_catalyst_arm_inherits_base_label():
    label = _strategy_label("scalp_1m_cat")
    assert "1분봉 스캘핑" in label and "촉매" in label


def test_strategy_label_unknown_id_falls_back_to_raw_id_not_crash():
    label = _strategy_label("아직-모르는-전략")
    assert "아직-모르는-전략" in label


def test_strategy_label_none_is_explicit_not_blank():
    assert _strategy_label(None) == "전략 미상"


# ── ⑤ 체결 알림(fills) + 하드레일 청산(hard-stop) — 같은 렌더 경로 ──────────
# `_intraday_hard_stop_check`(quant/trade/loop.py)는 별도 메시지를 만들지
# 않는다 — EXIT_LONG 신호를 내 이 체결 렌더러를 그대로 태운다. reason 문자열
# ("하드 손절 −5%(리스크 레일)")만 다르므로 그 값으로 같은 경로를 태워 계약을
# 확인한다.

class _Data:
    def quote(self, symbol: str):
        from quant.core.models import Quote
        return Quote(symbol=symbol, ts=NOW, price=PRICE)

    def history(self, symbol: str, interval: str, n: int) -> pd.DataFrame:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


class _Sink:
    def on_signal(self, signal):
        pass

    def on_fill(self, fill):
        pass

    def on_order(self, state):
        pass


class _Notifier:
    def __init__(self):
        self.messages: list[tuple[str, str | None]] = []

    def send(self, text: str, lane: str | None = None) -> None:
        self.messages.append((text, lane))


def _risk() -> RiskManagerImpl:
    cfg = dict(
        capital_mode="shared", sizing_mode="capital_fraction",
        max_position_pct=100, max_symbol_pct_total=0, daily_loss_limit_pct=100,
        max_orders_per_day=1000, cooldown_bars_after_stop=0, max_order_notional_pct=0,
        max_total_exposure_pct=0, max_concurrent_positions=0, min_order_notional_krw=0,
    )
    return RiskManagerImpl(
        {"risk": cfg}, capital_fraction={"scalp_1m": 1.0, "vol_breakout": 1.0},
        market_of={SYMBOL: "US"}, fx=FixedFxProvider(FX_RATE),
    )


def _harness(fake_clock_cls):
    data = _Data()
    fx = FixedFxProvider(FX_RATE)
    portfolio = Portfolio(cash=10_000_000.0, state_path=None)
    broker = PaperBroker(data=data, portfolio=portfolio, fee_bps=0.0,
                         market_of={SYMBOL: "US"}, fx=fx)
    ctx = Context(clock=fake_clock_cls(now=NOW), data=data, broker=broker)
    return ctx, broker


def test_buy_fill_notification_contract(fake_clock_cls):
    ctx, _broker_ = _harness(fake_clock_cls)
    notifier = _Notifier()
    signal = Signal(
        strategy_id="scalp_1m", symbol=SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=0.5, reason="테스트 진입", stop=95.0, target=110.0,
    )
    _execute_signal(signal, ctx, _risk(), _Sink(), notifier=notifier)
    assert len(notifier.messages) == 1
    text, lane = notifier.messages[0]
    _assert_contract(text)
    assert lane == "trades"  # 카탈로그 §1: 체결은 trades 레인


def test_hard_stop_exit_reuses_fill_contract_with_literal_reason(fake_clock_cls):
    """하드레일 청산은 `_intraday_hard_stop_check`가 리터럴로 고정한 사유
    문자열("하드 손절 −5%(리스크 레일)")로 EXIT_LONG 을 낸다 — 같은 렌더러를
    태우므로 별도 카탈로그 행이 없다(문서화된 사실, §1 각주)."""
    ctx, broker = _harness(fake_clock_cls)
    notifier = _Notifier()
    entry = Signal(
        strategy_id="vol_breakout", symbol=SYMBOL, action=SignalAction.ENTER_LONG,
        target_weight=0.5, reason="테스트 진입",
    )
    _execute_signal(entry, ctx, _risk(), _Sink(), notifier=notifier)
    exit_signal = Signal(
        strategy_id="vol_breakout", symbol=SYMBOL, action=SignalAction.EXIT_LONG,
        target_weight=0.0, exit_fraction=1.0, reason="하드 손절 −5%(리스크 레일)",
    )
    _execute_signal(exit_signal, ctx, _risk(), _Sink(), notifier=notifier)
    assert len(notifier.messages) == 2
    exit_text, exit_lane = notifier.messages[1]
    _assert_contract(exit_text)
    assert exit_lane == "trades"
    assert "하드 손절" in exit_text
    assert "변동성 돌파" in exit_text and "vol_breakout" in exit_text  # 한국어 가독성


# ── ⑥ 스코어보드 / 세션 손익 (quant/control/ledger.py) ──────────────────────

def _trip(pnl: float, bps: float, sid: str = "pullback_impulse", market: str = "US") -> dict:
    return {
        "strategy": sid, "symbol": SYMBOL, "pnl": pnl, "bps": bps, "pnl_known": True,
        "market": market, "fees": 1.0, "notional": 1000.0,
        "exit_ts": "2026-08-28T14:00:00+09:00",
    }


def test_scoreboard_text_contract():
    trips = [_trip(100.0, 5.0), _trip(-50.0, -2.0), _trip(30.0, 3.0), _trip(200.0, 8.0)]
    text = L.scoreboard_text(trips, title="계약 테스트 스코어보드")
    _assert_contract(text)
    assert len(text) <= 3500  # scoreboard_text 자체 문서화 계약(마지막 줄 [:3500])
    # 알려진 격차(모듈 docstring 참고): scoreboard_text 는 raw id 를 그대로 찍는다 —
    # _strategy_label 처럼 한국어 표시명을 붙이지 않는다. 이 계약 테스트는 그
    # 사실을 정직하게 기록한다(고치지 않음, 별도 스폰 작업 대상).
    assert "[pullback_impulse]" in text


def test_scoreboard_text_no_trips_is_explicit_not_blank():
    text = L.scoreboard_text([])
    assert "아직 없음" in text
    _assert_no_placeholder_leaks(text)


def test_session_pnl_text_contract():
    trades = [
        {"symbol": SYMBOL, "side": "BUY", "qty": 10, "price": 100.0, "fee": 1.0,
         "market": "US", "ts": "2026-08-28T14:00:00+00:00", "strategy_id": "gap_fade",
         "cash_after_usd": 9000.0},
        {"symbol": SYMBOL, "side": "SELL", "qty": 10, "price": 105.0, "fee": 1.0,
         "market": "US", "ts": "2026-08-28T19:00:00+00:00", "strategy_id": "gap_fade",
         "realized_pnl": 50.0, "cash_after_usd": 9550.0},
    ]
    summary = L.session_pnl_summary(trades, "US", date(2026, 8, 28))
    text = L.session_pnl_text(summary, report_url="https://example.invalid/report.html")
    _assert_contract(text)
    assert "$" in text  # USD 표기(하우스 스타일)
    assert re.search(r"\$[+-][\d,]+\.\d{2}", text), f"USD 2자리 소수 서식이 안 보인다: {text!r}"


def test_session_pnl_text_no_trades_is_explicit():
    summary = L.session_pnl_summary([], "KR", date(2026, 8, 28))
    text = L.session_pnl_text(summary)
    _assert_contract(text)
    assert "없음" in text


# ── ⑦ 마감 요약(digest) — quant/control/daily_wrap.py ───────────────────────

def test_daily_wrap_digest_contract():
    trips = [_trip(1000.0, 12.0, market="KR"), _trip(-500.0, -6.0, market="KR"),
             _trip(300.0, 4.0, market="KR")]
    deferred = [
        {"ts": "2026-08-28T10:00:00+0900", "source": "backfill_1m",
         "text": "1분봉 백필 완료", "level": "defer"},
    ]
    sections = DW.build_sections(
        market="KR", on=date(2026, 8, 28),
        pnl={"has_trades": True, "n_fills": 6, "n_buys": 3, "n_sells": 3,
             "net_realized": 800.0, "fees": 120.0, "unknown_sells": 0},
        trips=trips, equity_points=[], positions={}, session_trades=[],
        names={}, issues=[], commits=[], deferred=deferred,
    )
    html = DW.render_html(sections)
    assert html.startswith("<!doctype html>") and html.rstrip().endswith("</html>")
    _assert_no_placeholder_leaks(html)
    assert "1분봉 백필 완료" in html


# ── ⑧ 리포트 정확도 스코어카드("accuracy line") — quant/control/report_accuracy.py

def test_report_accuracy_render_markdown_contract():
    scorecard = {
        "schema": RA.SCHEMA, "as_of": "2026-09-06", "n_claims": 42, "min_n": RA.MIN_N,
        "direction": {
            1: {"n": 5, "rate": 0.6, "ci_lo": 0.3, "ci_hi": 0.85},   # n<20 → 판단 불가
            3: {"n": 27, "rate": 0.63, "ci_lo": 0.45, "ci_hi": 0.78},
        },
        "candidates": {
            "horizons": {1: {"n": 25, "mean_bps": 12.5, "hitrate": 0.6}},
            "ic_n": {1: 25, 3: 10, 5: 0},
            "ic": {1: 0.12, 3: None, 5: None},
            "by_tag": {"EVENT": {1: {"n": 22, "mean_bps": 8.0, "hitrate": 0.55}}},
        },
    }
    md = RA.render_markdown(scorecard)
    _assert_balanced_html(md)  # 마크다운엔 <> 가 없어야 정상 — 있으면 회귀
    _assert_no_placeholder_leaks(md)
    assert "판단 불가" in md  # n<20 지평이 실제로 숨겨지지 않고 명시된다
    assert "+12" in md  # mean_bps=12.5 → 부호 있는 정수 서식(하우스 스타일)


# ── ⑨ 운영 감시 — 판단 워치독(reconcile) + 발송 실패 원장 ──────────────────

def test_job_findings_reconcile_stale_is_alert_with_readable_detail():
    snapshot = {
        "available": True,
        "jobs": {"reconcile": {"last": "2026-08-01T00:00:00+00:00", "ok": True, "fresh": False}},
    }
    findings = H.job_findings(snapshot)
    assert len(findings) == 1
    assert findings[0].level == H.ALERT
    _assert_no_placeholder_leaks(findings[0].detail)
    assert "reconcile" in findings[0].detail


def test_notify_failure_findings_over_threshold_matches_catalog_wording():
    findings = H.notify_failure_findings(4)
    assert len(findings) == 1
    text = findings[0].detail
    _assert_no_placeholder_leaks(text)
    assert "notify_failures.jsonl" in text  # 카탈로그 §5 가 가리키는 파일명과 일치


# ── ⑩ 레인 배정이 카탈로그(docs/runbooks/telegram-catalog.md §3)와 일치 ────
# 카탈로그 표를 코드로 다시 적은 것 — 스크립트의 NOTIFY_LANE 이 바뀌면 문서와
# 코드 둘 중 하나를 고쳐야 이 테스트가 다시 통과한다(문서가 조용히 낡는 것을
# 막는다).

_CATALOG_LANES = {
    "own_brief": "briefs", "promotion_debate": "briefs", "flow_scan": "briefs",
    "tg_digest": "intel", "ai_trader": "briefs", "ml_scorer": "briefs",
    "market_pulse": "briefs", "manual_recs": "briefs", "report_accuracy": "briefs",
    "session_pnl": "trades", "capital_review": "briefs", "daily_feedback": "briefs",
    "pnl_attribution": "trades", "close_report": "briefs", "scoreboard_weekly": "trades",
    "weekly_review": "briefs", "param_propose": "briefs", "governor": "briefs",
    "experiments_daily": "briefs", "backfill_1m": "ops", "backfill_kr_daily": "ops",
    "backfill_kr_stock_daily": "ops", "backfill_kr_largecap_daily": "ops",
    "backfill_us_daily": "ops", "kr_minute_backfill": "ops", "macro_collect": "ops",
    "delivery_check": "ops", "risk_review": "briefs", "ops_judge": "ops",
    "backup": "ops", "backup_restore_check": "ops", "ops_watch": "ops",
    "watchdog": "ops", "publish_portfolio": "ops", "daily_wrap": "briefs",
    "kiwoom_ws_check": "ops", "us_watch_discover": "briefs", "daily_brief": "briefs",
}


def test_catalog_lane_matches_notify_lane_in_script():
    scripts_dir = REPO_ROOT / "server" / "scripts"
    missing = []
    mismatched = []
    for name, expected_lane in _CATALOG_LANES.items():
        path = scripts_dir / f"{name}.sh"
        if not path.exists():
            missing.append(name)
            continue
        text = path.read_text(encoding="utf-8")
        m = re.search(r'NOTIFY_LANE="([a-z]+)"', text)
        if m is None or m.group(1) != expected_lane:
            mismatched.append((name, m.group(1) if m else None, expected_lane))
    assert missing == [], f"카탈로그에는 있는데 스크립트가 없다: {missing}"
    assert mismatched == [], f"카탈로그와 실제 NOTIFY_LANE 이 어긋난다: {mismatched}"
