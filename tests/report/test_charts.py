import re

from quant.analyze import charts


def test_sparkline_is_deterministic():
    v = [1.0, 3.5, 2.25, 9.125, 4.0]
    assert charts.sparkline(v) == charts.sparkline(v)


def test_sparkline_rising_uses_up_color():
    svg = charts.sparkline([1.0, 2.0, 3.0])
    assert "var(--up)" in svg and "var(--down)" not in svg


def test_sparkline_falling_uses_down_color():
    svg = charts.sparkline([3.0, 2.0, 1.0])
    assert "var(--down)" in svg and "var(--up)" not in svg


def test_sparkline_too_few_points_is_empty_but_valid_svg():
    svg = charts.sparkline([1.0])
    assert svg.startswith("<svg") and svg.endswith("</svg>") and "polyline" not in svg


def test_sparkline_flat_series_does_not_divide_by_zero():
    svg = charts.sparkline([5.0, 5.0, 5.0])
    assert "polyline" in svg and "nan" not in svg.lower()


def test_sparkline_caps_points_for_long_history():
    """1년치(최대 252개) history 를 받아도 폴리라인 좌표는 SPARK_POINTS(60) 이하여야 한다."""
    svg = charts.sparkline([float(i) for i in range(300)])
    polyline = re.search(r'<polyline points="([^"]*)"', svg)
    assert polyline is not None
    coords = polyline.group(1).split(" ")
    assert len(coords) <= charts.SPARK_POINTS


def test_sparkline_coordinates_are_rounded():
    """부동소수 꼬리가 남으면 같은 스냅샷이 다른 바이트를 낸다."""
    svg = charts.sparkline([1.0, 1.0 / 3, 2.0 / 7, 5.0])
    for num in re.findall(r"-?\d+\.\d+", svg):
        assert len(num.split(".")[1]) <= 2, num


def test_diverging_bars_empty_is_empty_string():
    assert charts.diverging_bars([]) == ""


def test_diverging_bars_colors_by_sign():
    svg = charts.diverging_bars([("외국인", "+500억원", 500), ("개인", "-700억원", -700)])
    assert "var(--up)" in svg and "var(--down)" in svg


def test_diverging_bars_all_zero_does_not_crash():
    svg = charts.diverging_bars([("a", "0", 0), ("b", "0", 0)])
    assert svg.startswith("<svg")


def test_diverging_bars_escapes_labels_and_values():
    svg = charts.diverging_bars([("<script>", "<b>1</b>", 1)])
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert "<b>1</b>" not in svg


def test_diverging_bars_values_never_leave_the_canvas():
    """값 텍스트가 오른쪽으로 잘리던 결함(‘+2’만 보임)의 회귀 방지."""
    import re

    w = 340.0
    svg = charts.diverging_bars(
        [("개인", "-3조 1,702억원", -31702), ("외국인", "+2조 6,395억원", 26395)], w=w
    )
    for x in re.findall(r'<text x="([\d.]+)"[^>]*class="dbar-value"', svg):
        assert float(x) <= w, x


def test_diverging_bars_labels_and_values_use_separate_columns():
    """라벨과 값이 같은 x 를 쓰면 겹친다 — 열이 분리돼야 한다."""
    import re

    svg = charts.diverging_bars([("개인", "-3조 1,702억원", -31702)])
    lx = float(re.search(r'<text x="([\d.]+)"[^>]*class="dbar-label"', svg).group(1))
    vx = float(re.search(r'<text x="([\d.]+)"[^>]*class="dbar-value"', svg).group(1))
    assert vx > lx + 100


def test_event_axis_keeps_uniform_aspect():
    """preserveAspectRatio='none' 이면 원이 타원이 되고 D-day 글자가 뭉개진다."""
    svg = charts.event_axis([{"days_ahead": 2, "dday": "D-2", "high_impact": True}])
    assert 'preserveAspectRatio="none"' not in svg
    assert "xMidYMid" in svg


def test_event_axis_highlights_high_impact():
    svg = charts.event_axis([
        {"days_ahead": 1, "dday": "D-1", "high_impact": True},
        {"days_ahead": 9, "dday": "D-9", "high_impact": False},
    ])
    assert "var(--alert)" in svg and charts.MUTE in svg


def test_event_axis_drops_events_past_horizon():
    svg = charts.event_axis(
        [{"days_ahead": 30, "dday": "D-30", "high_impact": True}], horizon=14
    )
    assert "D-30" not in svg


def test_event_axis_empty_is_valid_svg():
    svg = charts.event_axis([])
    assert svg.startswith("<svg") and svg.endswith("</svg>")


def test_gauge_clamps_out_of_range():
    for value in (-50.0, 150.0):
        assert charts.gauge(value, 0, 100).startswith("<svg")


def test_gauge_zero_span_does_not_divide_by_zero():
    assert "nan" not in charts.gauge(5, 5, 5).lower()
