"""서버사이드 인라인 SVG 차트.

JS 차트 라이브러리를 쓰지 않는다 — 같은 스냅샷이 항상 같은 HTML을 내야 재현성
검증이 성립하는데, 클라이언트 렌더는 그걸 깬다. 외부 CDN도 안 쓴다(자기완결).

좌표는 소수 2자리로 고정 반올림한다. 부동소수 꼬리가 바뀌면 바이트가 달라진다.
"""
from __future__ import annotations

from html import escape

# 한국 관례: 상승=적, 하락=청. 미국과 반대이므로 리포트에 범례를 넣는다.
UP = "var(--up)"
DOWN = "var(--down)"
MUTE = "var(--mute)"

# history 가 1년치(최대 252개)로 늘어난 뒤에도 스파크라인은 최근 구간만 그린다 —
# 118px 폭에 252개 점을 다 찍으면 시각적으로 뭉개진다.
SPARK_POINTS = 60


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _scale(values: list[float], lo: float, hi: float, size: float) -> list[float]:
    span = hi - lo
    if span == 0:
        return [size / 2] * len(values)
    return [(v - lo) / span * size for v in values]


def sparkline(values: list[float], w: float = 160, h: float = 36) -> str:
    """지수·가격 추이. 마지막 점을 강조하고 방향에 따라 색을 바꾼다."""
    pts = [v for v in values if v is not None][-SPARK_POINTS:]
    if len(pts) < 2:
        return f'<svg class="spark" width="{_n(w)}" height="{_n(h)}" aria-hidden="true"></svg>'
    lo, hi = min(pts), max(pts)
    pad = 3.0
    ys = _scale(pts, lo, hi, h - pad * 2)
    step = (w - pad * 2) / (len(pts) - 1)
    coords = [(pad + i * step, h - pad - y) for i, y in enumerate(ys)]
    d = " ".join(f"{_n(x)},{_n(y)}" for x, y in coords)
    color = UP if pts[-1] >= pts[0] else DOWN
    lx, ly = coords[-1]
    # 면적 채움은 추세를 눈으로 잡는 데 도움이 되지만 옅게 — 선이 주인공이다
    area = f"{_n(pad)},{_n(h)} {d} {_n(w - pad)},{_n(h)}"
    return (
        f'<svg class="spark" width="{_n(w)}" height="{_n(h)}" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'role="img" aria-label="추이 스파크라인">'
        f'<polygon points="{area}" fill="{color}" opacity="0.10"/>'
        f'<polyline points="{d}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{_n(lx)}" cy="{_n(ly)}" r="2.4" fill="{color}"/></svg>'
    )


def diverging_bars(
    rows: list[tuple[str, str, float]], w: float = 340, bar_h: float = 18
) -> str:
    """0을 중심으로 좌우로 뻗는 막대. 투자자 수급처럼 부호가 의미인 데이터용.

    rows 는 `(라벨, 표시문자열, 값)`. 표시문자열을 따로 받는 이유는 단위 포맷팅
    (억원/조원)이 이 모듈의 일이 아니기 때문이다 — 여기선 그리기만 한다.

    **라벨·막대·값을 3열로 분리한다.** 이전에는 값 텍스트를 막대 끝에 붙여
    그렸는데, 막대가 길면 오른쪽으로 잘려나가고("+2" 만 보임) 짧으면 왼쪽
    라벨과 겹쳐 글자가 뭉갰다("-31,7영2"). 열을 고정하면 그 두 가지가 구조적으로
    불가능해진다.
    """
    if not rows:
        return ""
    peak = max((abs(v) for _, _, v in rows), default=0) or 1
    label_w, value_w, gap = 74.0, 92.0, 8.0
    plot_x = label_w + gap
    plot_w = w - label_w - value_w - gap * 2
    mid = plot_x + plot_w / 2
    h = len(rows) * (bar_h + 7) + 6
    parts = [
        f'<svg class="dbar" width="{_n(w)}" height="{_n(h)}" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'role="img" aria-label="투자자별 순매수">'
    ]
    for i, (label, shown, value) in enumerate(rows):
        y = 6 + i * (bar_h + 7)
        length = max(abs(value) / peak * (plot_w / 2 - 2), 1.5)  # 0 이어도 실선은 보이게
        color = UP if value > 0 else DOWN
        x = mid if value > 0 else mid - length
        parts.append(
            f'<text x="{_n(label_w)}" y="{_n(y + bar_h * 0.72)}" class="dbar-label" '
            f'text-anchor="end">{escape(label)}</text>'
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(length)}" height="{_n(bar_h)}" '
            f'fill="{color}" rx="1.5"/>'
            f'<text x="{_n(w)}" y="{_n(y + bar_h * 0.72)}" class="dbar-value" '
            f'fill="{color}" text-anchor="end">{escape(shown)}</text>'
        )
    parts.append(
        f'<line x1="{_n(mid)}" y1="2" x2="{_n(mid)}" y2="{_n(h - 2)}" '
        f'stroke="var(--rule)" stroke-width="1"/></svg>'
    )
    return "".join(parts)


def streak_strip(flags: list[bool], w: float = 108, h: float = 14) -> str:
    """종목이 최근 N일 중 언제 뉴스에 등장했는지. 왼쪽이 과거, 오른쪽이 오늘.

    연속 등장을 눈으로 잡게 하는 게 목적이다 — 숫자 '3일'보다 띄엄띄엄인지
    연달아인지가 한눈에 들어온다.
    """
    if not flags:
        return ""
    cell = w / len(flags)
    parts = [
        f'<svg class="streak" width="{_n(w)}" height="{_n(h)}" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'role="img" aria-label="최근 뉴스 등장 이력">'
    ]
    for i, on in enumerate(flags):
        fill = UP if on else "var(--rule)"
        op = "1" if on else "0.55"
        parts.append(
            f'<rect x="{_n(i * cell + 0.6)}" y="0" width="{_n(cell - 1.2)}" '
            f'height="{_n(h)}" fill="{fill}" opacity="{op}" rx="1.5"/>'
        )
    return "".join(parts) + "</svg>"


def event_axis(events: list[dict], horizon: int = 14, w: float = 560.0) -> str:
    """시간 척추의 미래 구간. NOW(0일)에서 horizon일까지 이벤트를 배치한다.

    viewBox 폭을 실제 렌더 폭에 가깝게 잡고 **preserveAspectRatio를 기본값
    (uniform)으로 둔다.** `none`으로 늘리면 원이 타원이 되고 D-day 글자가
    가로로 뭉개진다 — 실제로 그렇게 나왔다.
    """
    h = 46.0
    parts = [
        f'<svg class="axis" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="향후 {horizon}일 이벤트 축">'
        f'<line x1="0" y1="16" x2="{_n(w)}" y2="16" stroke="var(--rule)" stroke-width="0.6"/>'
        f'<line x1="0" y1="6" x2="0" y2="26" stroke="var(--ink)" stroke-width="1.2"/>'
    ]
    seen_x: list[float] = []
    pad = 10.0
    span = w - pad * 2
    for e in events:
        if e["days_ahead"] > horizon:
            continue
        x = pad + e["days_ahead"] / max(horizon, 1) * span
        # 같은 날짜에 여러 건이 겹치면 라벨을 번갈아 위아래로 내린다
        row = sum(1 for sx in seen_x if abs(sx - x) < 28) % 2
        seen_x.append(x)
        color = "var(--alert)" if e["high_impact"] else MUTE
        r = 3.4 if e["high_impact"] else 2.2
        ty = 32 + row * 11
        parts.append(
            f'<circle cx="{_n(x)}" cy="16" r="{_n(r)}" fill="{color}"/>'
            f'<text x="{_n(x)}" y="{_n(ty)}" class="axis-tick" text-anchor="middle" '
            f'fill="{color}">{escape(e["dday"])}</text>'
        )
    return "".join(parts) + "</svg>"


def gauge(value: float, lo: float, hi: float, w: float = 132, h: float = 8) -> str:
    """연속선상의 현재 위치. 백분위·확률 같은 0~100 값을 눈금 없이 보여준다."""
    frac = 0.0 if hi == lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
    x = frac * (w - 4)
    return (
        f'<svg class="gauge" width="{_n(w)}" height="{_n(h)}" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'role="img" aria-label="위치 {value:.0f}">'
        f'<rect x="0" y="{_n(h / 2 - 1)}" width="{_n(w)}" height="2" fill="var(--rule)" rx="1"/>'
        f'<rect x="{_n(x)}" y="0" width="4" height="{_n(h)}" fill="var(--ink)" rx="1"/></svg>'
    )


def daily_bars(values: list[float], w: float = 110, h: float = 30) -> str:
    """일별 순매수 미니 막대(서브프로젝트 I, 외국인 수급 추종) — 0선을 기준으로
    양수는 위, 음수는 아래로 뻗는다.

    스케일은 이 시리즈 자체의 최대 절대값 기준이다 — `diverging_bars`(투자자
    수급, 여러 주체를 같은 스케일로 비교)와 달리 여기선 종목마다 수급 규모가
    천차만별이라 통합 스케일을 쓰면 작은 종목의 막대가 안 보인다.
    """
    if not values:
        return ""
    peak = max((abs(v) for v in values), default=0) or 1
    mid = h / 2
    bar_w = w / len(values)
    parts = [
        f'<svg class="ybar" width="{_n(w)}" height="{_n(h)}" viewBox="0 0 {_n(w)} {_n(h)}" '
        f'role="img" aria-label="일별 순매수">'
        f'<line x1="0" y1="{_n(mid)}" x2="{_n(w)}" y2="{_n(mid)}" stroke="var(--rule)" stroke-width="0.6"/>'
    ]
    for i, v in enumerate(values):
        length = max(abs(v) / peak * (mid - 2), 0.6)
        color = UP if v > 0 else (DOWN if v < 0 else MUTE)
        bw = max(bar_w * 0.7, 1.0)
        x = i * bar_w + (bar_w - bw) / 2
        y = mid - length if v >= 0 else mid
        parts.append(
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(bw)}" height="{_n(length)}" '
            f'fill="{color}" rx="1"/>'
        )
    parts.append("</svg>")
    return "".join(parts)
