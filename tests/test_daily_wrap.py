"""장 마감 하루 요약(`quant.control.daily_wrap`) — 조립·렌더 규율.

이 리포트의 실패 모드는 "틀린 숫자"보다 **"없는 것을 있는 것처럼 쓰는 것"**이다:
거래가 0건인 날 빈 표를 그리거나, 트립 3개로 승률 100%를 찍거나, 종목코드만
보여줘서 소유자가 검색하게 만드는 것. 아래 테스트는 그 넷을 각각 못 박는다.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from quant.apps.cli import _wrap_consume_queue, _wrap_deferred
from quant.control.daily_wrap import (
    MIN_TRIPS_FOR_JUDGEMENT,
    build_sections,
    caption_line,
    fmt_amount,
    render_html,
    trips_closed_between,
)

KST = ZoneInfo("Asia/Seoul")
ON = date(2026, 8, 28)


def _sections(**over):
    base = dict(
        market="KR", on=ON, pnl=None, trips=[], equity_points=[],
        positions={}, session_trades=[], names={}, issues=[], commits=[],
    )
    base.update(over)
    return build_sections(**base)


def _trip(pnl: float, bps: float, sid: str = "orb_scan") -> dict:
    return {"strategy": sid, "symbol": "005930", "pnl": pnl, "bps": bps,
            "pnl_known": True, "exit_ts": "2026-08-28T14:00:00+09:00"}


# ① 빈 데이터 — 렌더가 깨지지 않고 "오늘 거래 없음"이 나온다 -----------------

def test_empty_ledger_renders_no_trades():
    html = render_html(_sections())
    assert "오늘 거래 없음" in html
    assert "오늘 지분 변경 없음" in html
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")


def test_empty_ledger_pnl_summary_shape_still_renders():
    """`session_pnl_summary` 가 has_trades=False 로 오는 실제 경로도 같아야 한다."""
    pnl = {"has_trades": False, "n_fills": 0, "n_buys": 0, "n_sells": 0,
           "net_realized": 0.0, "fees": 0.0, "unknown_sells": 0}
    html = render_html(_sections(pnl=pnl))
    assert "오늘 거래 없음" in html


# ② 표본 부족 — 승률 대신 "판단 불가" ---------------------------------------

def test_small_sample_shows_no_winrate():
    trips = [_trip(1000.0, 12.0), _trip(-500.0, -6.0), _trip(300.0, 4.0)]
    sec = _sections(pnl={"has_trades": True, "n_fills": 6, "n_buys": 3, "n_sells": 3,
                         "net_realized": 800.0, "fees": 120.0, "unknown_sells": 0},
                    trips=trips)
    row = sec["performance"]["strategies"][0]
    assert row["n"] == 3
    assert row["win_rate"] == "판단 불가"
    html = render_html(sec)
    assert "판단 불가" in html
    # 표본이 3개인데 "67%" 같은 숫자를 만들어내지 않는다.
    assert "67%" not in html


def test_sufficient_sample_shows_winrate():
    n = MIN_TRIPS_FOR_JUDGEMENT
    trips = [_trip(100.0, 5.0) for _ in range(n)]
    sec = _sections(pnl={"has_trades": True, "n_fills": n * 2, "n_buys": n, "n_sells": n,
                         "net_realized": 100.0 * n, "fees": 0.0, "unknown_sells": 0},
                    trips=trips)
    assert sec["performance"]["strategies"][0]["win_rate"] == "100%"


def test_trips_closed_between_filters_by_exit():
    start = datetime(2026, 8, 28, 9, 0, tzinfo=KST)
    end = datetime(2026, 8, 28, 15, 30, tzinfo=KST)
    inside = {"exit_ts": "2026-08-28T14:00:00+09:00"}
    before = {"exit_ts": "2026-08-27T14:00:00+09:00"}
    open_trip = {"exit_ts": None}
    assert trips_closed_between([inside, before, open_trip], start, end) == [inside]


# ③ 포지션에 종목명이 붙는다 (캐시 주입) -------------------------------------

def test_positions_show_names_from_cache():
    positions = {"042700": {"qty": 10.0, "avg_cost": 120000.0, "market": "KR"}}
    sec = _sections(positions=positions, names={"042700": "한미반도체"})
    assert sec["positions"]["holdings"][0]["name"] == "한미반도체"
    html = render_html(sec)
    assert "한미반도체(042700)" in html


def test_positions_without_name_show_code_only():
    """이름을 모르면 코드만 — 없는 이름을 지어내지 않는다."""
    sec = _sections(positions={"000500": {"qty": 5.0, "avg_cost": 1000.0}}, names={})
    html = render_html(sec)
    assert "000500" in html
    assert "(000500)" not in html


def test_position_changes_classified():
    positions = {
        "005930": {"qty": 10.0, "avg_cost": 70000.0},   # 어제 5 + 오늘 5 → 증가
        "042700": {"qty": 3.0, "avg_cost": 120000.0},   # 오늘 신규
    }
    session_trades = [
        {"symbol": "005930", "side": "BUY", "qty": 5.0},
        {"symbol": "042700", "side": "BUY", "qty": 3.0},
        {"symbol": "069500", "side": "SELL", "qty": 7.0},  # 전량 매도 → 청산
    ]
    sec = _sections(positions=positions, session_trades=session_trades,
                    names={"005930": "삼성전자"})
    kinds = {c["symbol"]: c["kind"] for c in sec["positions"]["changes"]}
    assert kinds == {"005930": "증가", "042700": "신규", "069500": "청산"}
    assert len(sec["positions"]["holdings"]) == 2


# ④ 이상 없음이면 "없음" 한 줄 ------------------------------------------------

def test_no_issues_renders_single_none_line():
    html = render_html(_sections(issues=[]))
    body = html.split("3. 문제 발견 및 개선</h2>", 1)[1].split("<h2>", 1)[0]
    assert body == "<p>없음</p>"


def test_issues_are_listed_verbatim():
    html = render_html(_sections(issues=["잡 실패 — backup: 최근 성공이 없다"]))
    assert "backup: 최근 성공이 없다" in html
    assert "<p>없음</p>" not in html


# 알림 게이트가 미뤄둔 큐 — 이 리포트가 유일한 소비자다 -----------------------

def test_deferred_queue_is_surfaced_but_not_counted_as_issue():
    rows = [
        {"ts": "2026-08-28T10:03:11+0900", "source": "backfill_1m",
         "text": "1분봉 백필 완료", "level": "defer"},
        {"ts": "2026-08-28T11:00:00+0900", "source": "governor",
         "text": "파라미터 제안 2건", "level": "auto"},
    ]
    sec = _sections(deferred=rows)
    html = render_html(sec)
    assert "장중에 미뤄둔 알림 2건" in html
    assert "1분봉 백필 완료" in html
    # 정보성 큐가 "이상"으로 둔갑하지 않는다 — 캡션은 여전히 "이상 없음".
    assert sec["issues"] == []
    assert caption_line(sec).endswith("이상 없음")
    # level=auto(장외였다면 나갔을 것)가 위로 온다.
    assert sec["deferred"]["shown"][0]["source"] == "governor"


def test_deferred_queue_is_capped_with_pointer_to_file():
    rows = [{"ts": "2026-08-28T10:00:00+0900", "source": "s", "text": f"m{i}",
             "level": "defer"} for i in range(20)]
    sec = _sections(deferred=rows)
    assert sec["deferred"]["total"] == 20
    assert len(sec["deferred"]["shown"]) == 12
    assert "…외 8건 (data/notify_queue.jsonl)" in render_html(sec)


def test_empty_deferred_queue_draws_nothing():
    body = render_html(_sections()).split("3. 문제 발견 및 개선</h2>", 1)[1].split("<h2>", 1)[0]
    assert body == "<p>없음</p>"
    assert "미뤄둔" not in render_html(_sections())


# 큐 소비 — 안 비우면 다음 리포트에 같은 줄이 또 나온다 -----------------------

def _q(root, *lines):
    p = root / "data" / "notify_queue.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")
    return p


def _line(ts, source="s", text="t", level="defer"):
    import json

    return json.dumps({"ts": ts, "source": source, "text": text, "level": level},
                      ensure_ascii=False)


def test_queue_read_is_not_date_filtered_when_consuming(tmp_path):
    """큐의 의미는 "지난 리포트 이후"다 — 날짜로 거르면 KR(16:55)과 다음날
    US(06:55, 시장 기준일이 전날) 리포트가 같은 줄을 둘 다 집는다."""
    _q(tmp_path, _line("2026-08-27T23:50:00+0900"), _line("2026-08-28T10:00:00+0900"))
    assert len(_wrap_deferred(tmp_path, ON, consume=True)) == 2


def test_consume_moves_lines_to_archive_and_empties_queue(tmp_path):
    q = _q(tmp_path, _line("2026-08-28T10:00:00+0900", text="a"),
           _line("2026-08-28T11:00:00+0900", text="b"))
    rows = _wrap_deferred(tmp_path, ON, consume=True)
    _wrap_consume_queue(tmp_path, len(rows))

    assert q.read_text(encoding="utf-8") == ""
    archive = (tmp_path / "data" / "ledger" / "notify_queue_archive.jsonl").read_text(
        encoding="utf-8")
    assert archive.count("\n") == 2 and '"a"' in archive and '"b"' in archive
    # 두 번째 리포트는 같은 줄을 다시 보지 않는다.
    assert _wrap_deferred(tmp_path, ON, consume=True) == []


def test_consume_preserves_lines_appended_after_read(tmp_path):
    """읽은 개수만큼만 덜어낸다 — 통째로 비우면 그 사이 들어온 줄을 읽지도 않고 잃는다."""
    q = _q(tmp_path, _line("2026-08-28T10:00:00+0900", text="읽음"))
    rows = _wrap_deferred(tmp_path, ON, consume=True)
    with q.open("a", encoding="utf-8") as f:  # 렌더 도중 크론이 append
        f.write(_line("2026-08-28T12:00:00+0900", text="나중") + "\n")

    _wrap_consume_queue(tmp_path, len(rows))
    assert [r["text"] for r in _wrap_deferred(tmp_path, ON, consume=True)] == ["나중"]


def test_backfill_run_reads_archive_and_does_not_consume(tmp_path):
    """`--date` 백필은 아카이브까지 보고, 큐를 삼키지 않는다."""
    archive = tmp_path / "data" / "ledger" / "notify_queue_archive.jsonl"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(_line("2026-08-28T09:00:00+0900", text="옛것") + "\n"
                       + _line("2026-08-27T09:00:00+0900", text="딴날") + "\n",
                       encoding="utf-8")
    q = _q(tmp_path, _line("2026-08-28T13:00:00+0900", text="지금"))

    rows = _wrap_deferred(tmp_path, ON, consume=False)
    assert sorted(r["text"] for r in rows) == ["옛것", "지금"]
    assert q.read_text(encoding="utf-8").strip() != ""  # 소비되지 않았다


def test_queue_missing_or_broken_lines_are_survivable(tmp_path):
    assert _wrap_deferred(tmp_path, ON, consume=True) == []
    _q(tmp_path, "not-json", _line("2026-08-28T10:00:00+0900"), "{}")
    assert len(_wrap_deferred(tmp_path, ON, consume=True)) == 1
    _wrap_consume_queue(tmp_path, 0)  # 0건 소비는 아무것도 건드리지 않는다
    assert len(_wrap_deferred(tmp_path, ON, consume=True)) == 1


def test_consume_cut_counts_valid_rows_not_raw_lines(tmp_path):
    """깨진 줄이 끼어도 마지막 유효 줄까지 잘라낸다 — 원시 줄 수로 자르면
    마지막 알림이 안 지워져 다음 리포트에 또 나온다."""
    _q(tmp_path, "not-json", _line("2026-08-28T10:00:00+0900", text="a"),
       "{}", _line("2026-08-28T11:00:00+0900", text="b"))
    rows = _wrap_deferred(tmp_path, ON, consume=True)
    assert [r["text"] for r in rows] == ["a", "b"]
    _wrap_consume_queue(tmp_path, len(rows))
    assert _wrap_deferred(tmp_path, ON, consume=True) == []


def test_changes_section_omitted_when_git_unreadable():
    """`commits=None`("git 을 못 읽었다")이면 4절 자체가 없다.
    빈 리스트("오늘 배포 없음")와 뭉개지 않는다."""
    assert "변경된 점" not in render_html(_sections(commits=None))
    assert "오늘 배포된 커밋 없음" in render_html(_sections(commits=[]))


def test_commits_capped_at_ten():
    sec = _sections(commits=[f"fix: {i}" for i in range(25)])
    assert len(sec["commits"]) == 10


# ⑦ 지수 대비 성적(알파, 2026-08-29 통합) ------------------------------------

def _alpha_series(n_up: int = 5, n_down: int = 5) -> list[tuple]:
    """`alpha.wrap_section()`이 받는 (날짜, 우리%, 지수%, 알파pp) 시퀀스를 손으로
    조립한다 — 상승일 n_up개(지수+0.5%, 우리+1.0%), 하락일 n_down개(지수-1.0%,
    우리-0.5%)로 참여율/방어율 표본(각 `MIN_SAMPLE_DAYS`=5)을 채운다."""
    from datetime import date as _d, timedelta

    series: list[tuple] = []
    d = _d(2026, 8, 1)
    for _ in range(n_up):
        our, bench = 1.0, 0.5
        series.append((d, our, bench, our - bench))
        d += timedelta(days=1)
    for _ in range(n_down):
        our, bench = -0.5, -1.0
        series.append((d, our, bench, our - bench))
        d += timedelta(days=1)
    return series


def test_alpha_section_shows_no_sample_by_default():
    """`alpha_series`를 안 주면(기본값) 5절이 "표본 없음"/"표본 부족"으로
    나온다 — 데이터가 없는데 숫자를 지어내지 않는다."""
    html = render_html(_sections())
    assert "5. 지수 대비 성적</h2>" in html
    assert "표본 없음" in html
    assert "표본 부족" in html


def test_alpha_section_renders_capture_and_recent_rows_with_sample():
    series = _alpha_series()
    sec = _sections(alpha_series=series)
    assert sec["alpha"]["summary"]["up_days"] == 5
    assert sec["alpha"]["summary"]["down_days"] == 5
    assert sec["alpha"]["summary"]["cum_alpha_pp"] is not None
    html = render_html(sec)
    assert "누적 알파" in html
    assert "참여율" in html
    assert "방어율" in html
    # 알파 절(5절) 자체는 "표본 없음"으로 후퇴하지 않아야 한다 — 페이지 전체를
    # 보면 6절(체결 비용)이 표본 없을 때 정직하게 같은 문구를 쓰므로(이 픽스처는
    # trips/spread_rows를 안 준다), 알파 절의 재료(lines)만 본다.
    assert not any("표본 없음" in str(line) for line in sec["alpha"]["lines"])
    # 최근 5일 표(alpha.wrap_section 계약 — rows는 최대 5개).
    assert len(sec["alpha"]["rows"]) == 5
    assert "알파pp" in html


# ⑤ 외부 URL 이 없다 (인라인 CSS 규율) ---------------------------------------

_EXTERNAL = re.compile(r"https?:|//[a-zA-Z0-9]|<script|<img|<link|@import|url\(")


def test_html_has_no_external_requests():
    sec = _sections(
        pnl={"has_trades": True, "n_fills": 4, "n_buys": 2, "n_sells": 2,
             "net_realized": -61853.0, "fees": 1200.0, "unknown_sells": 1},
        trips=[_trip(-61853.0, -31.0)],
        equity_points=[{"date": "2026-08-27", "total_krw": 10_000_000.0},
                       {"date": "2026-08-28", "total_krw": 9_938_147.0}],
        positions={"005930": {"qty": 10.0, "avg_cost": 70000.0}},
        session_trades=[{"symbol": "005930", "side": "BUY", "qty": 10.0}],
        names={"005930": "삼성전자"},
        issues=["워치독 발동 — engine-down"],
        commits=["feat: 마감 요약 리포트"],
    )
    html = render_html(sec)
    hit = _EXTERNAL.search(html)
    assert hit is None, f"외부 요청 흔적: {hit.group(0)!r}"
    assert "<style>" in html  # 스타일은 인라인으로 들어 있다


def test_html_is_mobile_and_theme_aware():
    html = render_html(_sections())
    assert 'name="viewport"' in html
    assert "prefers-color-scheme:dark" in html
    assert "overflow-x:auto" in html


# ⑥ 손익 부호·천단위 포맷 ----------------------------------------------------

def test_amount_formatting():
    assert fmt_amount(-61853.0, "KR") == "-61,853원"
    assert fmt_amount(1234567.0, "KR") == "+1,234,567원"
    assert fmt_amount(0.0, "KR") == "+0원"
    assert fmt_amount(-1234.5, "US") == "-$1,234.50"
    assert fmt_amount(1200.0, "KR", signed=False) == "1,200원"


def test_loss_is_red_and_signed():
    """색맹 대비 — 색(class)과 부호가 **둘 다** 붙는다."""
    sec = _sections(pnl={"has_trades": True, "n_fills": 2, "n_buys": 1, "n_sells": 1,
                         "net_realized": -61853.0, "fees": 0.0, "unknown_sells": 0})
    html = render_html(sec)
    assert '<span class="down">-61,853원</span>' in html
    sec_up = _sections(pnl={"has_trades": True, "n_fills": 2, "n_buys": 1, "n_sells": 1,
                            "net_realized": 61853.0, "fees": 0.0, "unknown_sells": 0})
    assert '<span class="up">+61,853원</span>' in render_html(sec_up)


def test_equity_delta_needs_two_points():
    one = _sections(equity_points=[{"date": "2026-08-28", "total_krw": 1000.0}])
    assert one["performance"]["equity_delta_krw"] is None
    assert "전일 값 없음" in render_html(one)
    two = _sections(equity_points=[{"date": "2026-08-27", "total_krw": 1000.0},
                                   {"date": "2026-08-28", "total_krw": 1200.0}])
    assert two["performance"]["equity_delta_krw"] == 200.0


# 캡션 — 한 줄, 파일을 열기 전에 알아야 할 것만 -------------------------------

def test_caption_is_one_line_summary():
    sec = _sections(
        pnl={"has_trades": True, "n_fills": 4, "n_buys": 2, "n_sells": 2,
             "net_realized": -61853.0, "fees": 0.0, "unknown_sells": 0},
        positions={f"00000{i}": {"qty": 1.0, "avg_cost": 100.0} for i in range(1, 9)},
    )
    cap = caption_line(sec)
    assert "\n" not in cap
    assert cap == "📄 8/28 KR 마감 — 실현 -61,853원 · 보유 8종목 · 이상 없음"


def test_caption_without_trades():
    assert caption_line(_sections()) == "📄 8/28 KR 마감 — 거래 없음 · 보유 0종목 · 이상 없음"


def test_caption_counts_issues():
    cap = caption_line(_sections(issues=["a", "b"]))
    assert cap.endswith("이상 2건")


# HTML 이스케이프 — 종목명·커밋 제목은 외부 문자열이다 ------------------------

def test_untrusted_strings_are_escaped():
    html = render_html(_sections(commits=["fix: <script>x</script> & 그것"],
                                 issues=["<b>이상</b>"]))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


# ⑥ 전략 간 합산 노출 — 2절 꼬리 한 줄 (2026-08-30) ---------------------------

def test_exposure_summary_says_no_holdings_when_flat():
    sec = _sections(positions={})
    assert sec["positions"]["exposure_summary"] == "보유 없음"
    assert "전략 간 합산 노출: 보유 없음" in render_html(sec)


def test_exposure_summary_flags_duplicate_strategy_holdings():
    """meta.lots에 2개 전략이 같은 심볼을 들고 있으면 "중복 보유"가 찍힌다."""
    positions = {
        "TQQQ": {
            "qty": 15.0, "avg_cost": 70.0, "market": "US",
            "meta": {"lots": {"donchian": {"qty": 10.0}, "mean_reversion": {"qty": 5.0}}},
        },
    }
    sec = _sections(market="US", positions=positions)
    assert "중복 보유 TQQQ" in sec["positions"]["exposure_summary"]


def test_exposure_summary_flags_offsetting_pair():
    """TQQQ 롱 + SQQQ 롱 동시 보유 — 알려진 상쇄 쌍(내장 배수로 보강)."""
    positions = {
        "TQQQ": {"qty": 10.0, "avg_cost": 70.0, "market": "US",
                 "meta": {"strategy": "donchian"}},
        "SQQQ": {"qty": 20.0, "avg_cost": 10.0, "market": "US",
                 "meta": {"strategy": "mean_reversion"}},
    }
    sec = _sections(market="US", positions=positions)
    assert "상쇄 쌍 보유 TQQQ/SQQQ" in sec["positions"]["exposure_summary"]


def test_exposure_summary_legacy_position_without_lots_still_counted():
    """meta에 lots도 strategy도 없는 레거시 포지션도 "?"로 담아 노출에 넣는다 —
    조용히 빠지면 노출 감시 자체가 사각지대가 된다. (KR 심볼을 써서 FX 환산
    없이 명목을 바로 확인한다: 10주 x 70,000원 = 700,000원.)"""
    positions = {"005930": {"qty": 10.0, "avg_cost": 70_000.0, "market": "KR"}}
    sec = _sections(market="KR", positions=positions)
    assert "700,000원" in sec["positions"]["exposure_summary"]


# ⑦ 체결 비용 — 6절 (2026-08-30) ---------------------------------------------

def _cost_trip(entry_ts, exit_ts, notional=1_000_000.0, fees=2_000.0,
              symbol="TQQQ", market="US") -> dict:
    return {"symbol": symbol, "market": market, "notional": notional, "fees": fees,
            "entry_ts": entry_ts, "exit_ts": exit_ts}


def test_cost_section_says_no_sample_without_spread_rows():
    sec = _sections(market="US", trips=[_cost_trip("2026-08-28T00:00:00+00:00",
                                                    "2026-08-28T00:10:00+00:00")])
    html = render_html(sec)
    body = html.split("6. 체결 비용</h2>", 1)[1]
    assert "표본 없음" in body


def test_cost_section_us_group_compares_observed_to_assumed():
    trips = [_cost_trip("2026-08-28T00:00:00+00:00", "2026-08-28T00:10:00+00:00")]
    spread_rows = [{"symbol": "TQQQ", "ts": "2026-08-28T00:00:10+00:00", "spread_bp": 10.0}]
    sec = build_sections(
        market="US", on=ON, pnl=None, trips=trips, equity_points=[], positions={},
        session_trades=[], names={}, issues=[], commits=[],
        spread_rows=spread_rows,
    )
    groups = sec["cost"]["groups"]
    assert len(groups) == 1 and groups[0]["label"] == "US"
    cmp = groups[0]["comparison"]
    assert cmp is not None
    assert cmp["observed_bp"] == 30.0  # fee 20 + spread 10
    assert cmp["assumed_bp"] == 26.0
    assert cmp["verdict"] == "낙관"
    html = render_html(sec)
    assert "US: 실측 30.0bp vs 가정 26.0bp (1/1건) — 가정이 낙관적" in html


def test_cost_section_kr_splits_etf_and_stock():
    trips = [
        _cost_trip("2026-08-28T00:00:00+00:00", "2026-08-28T00:10:00+00:00",
                  symbol="069500", market="KR"),  # ETF
        _cost_trip("2026-08-28T01:00:00+00:00", "2026-08-28T01:10:00+00:00",
                  symbol="005930", market="KR"),  # 개별주
    ]
    spread_rows = [
        {"symbol": "069500", "ts": "2026-08-28T00:00:10+00:00", "spread_bp": 1.0},
        {"symbol": "005930", "ts": "2026-08-28T01:00:10+00:00", "spread_bp": 5.0},
    ]
    sec = build_sections(
        market="KR", on=ON, pnl=None, trips=trips, equity_points=[], positions={},
        session_trades=[], names={}, issues=[], commits=[],
        spread_rows=spread_rows, kr_etf={"069500"},
    )
    groups = {g["label"]: g["comparison"] for g in sec["cost"]["groups"]}
    assert set(groups) == {"KR ETF", "KR 개별주"}
    assert groups["KR ETF"]["assumed_bp"] == 4.0
    assert groups["KR 개별주"]["assumed_bp"] == 30.0


def test_cost_section_without_kr_etf_treats_all_kr_as_stock():
    """kr_etf가 없으면(오프라인 리포트 흔한 경로) 전부 개별주로 본다 — 모르면
    안전한 쪽(assembly.py의 kr_etf 판정과 동일 원칙)."""
    trips = [_cost_trip("2026-08-28T00:00:00+00:00", "2026-08-28T00:10:00+00:00",
                        symbol="069500", market="KR")]
    spread_rows = [{"symbol": "069500", "ts": "2026-08-28T00:00:10+00:00", "spread_bp": 1.0}]
    sec = build_sections(
        market="KR", on=ON, pnl=None, trips=trips, equity_points=[], positions={},
        session_trades=[], names={}, issues=[], commits=[],
        spread_rows=spread_rows,  # kr_etf 미지정
    )
    groups = {g["label"]: g["comparison"] for g in sec["cost"]["groups"]}
    assert groups["KR ETF"] is None  # 069500이 개별주 그룹으로 갔으므로 ETF 그룹은 표본 없음
    assert groups["KR 개별주"] is not None
