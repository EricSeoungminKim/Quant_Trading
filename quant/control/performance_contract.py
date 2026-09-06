"""공개 포트폴리오 사이트 성과 JSON — 발행 전 마지막 방어선.

오너 요구사항(2026-09-06): "공개 사이트는 유실되거나 틀린 데이터를 절대 보여주면
안 된다." `quant.control.performance.build_performance_payload`가 만든 JSON을
`data/public/performance.json`에 쓰기 **직전**(`quant.apps.cli publish-performance`)과
그 파일을 공개 저장소로 push하기 **직전**(`server/scripts/publish_portfolio.sh` →
`quant.apps.cli validate-performance`) 두 지점에서 이 모듈의 `validate_payload`를
돌린다 — 구조가 깨졌거나(타입 오류), 숫자가 앞뒤가 안 맞거나(합계 불일치, 활성
전략 수 불일치), 직전 발행본보다 체결 수가 줄었으면(데이터 유실) 게시를 막는다.

## 순수 함수

파일 I/O·설정 로딩 전부 호출부의 몫이다 — `validate_payload(payload, ...)`는
payload(및 선택 `previous`/`strategies_cfg`/`now`/`holidays`)만 받아
`list[Finding]`을 낸다. 같은 입력엔 항상 같은 출력.

## 심각도 규칙

- **error**: 타입이 아예 틀렸거나(문자열이어야 하는데 숫자 등), NaN/Inf, null이
  허용 안 되는 자리의 null, 확률(win_rate/ci_low/ci_high)이 [0,1] 밖, 날짜가
  역순/중복, 시작자본이 음수, `enabled_count`가 settings.yaml과 불일치, 활성
  전략인데 표시명이 비었거나, paper_epoch가 있는데 전략별 시작자본이 없거나,
  전략별 시작자본 합이 `overall.seed_krw`와 안 맞거나, `generated_at`이 36시간
  넘게 낡았거나, 직전 발행본보다 누적 체결 수가 **줄었을 때**(데이터 유실).
  이 중 하나라도 있으면 호출부가 게시를 막는다.
- **warn**: 값 자체는 유효하지만 통계적으로 이례적인 것들 — 퍼센트 계열
  (cum_pct/day_pct/expectancy_bp/max_drawdown_pct/fee_drag_pct_of_gross)이
  넉넉하게 잡은 상식적 범위를 벗어나거나, 거래일 곡선에 4일 넘는 공백이
  있거나(휴장일이 몰리면 실제로도 벌어질 수 있어 error로 막지 않는다), 설정상
  활성 전략인데 아직 종결 거래가 없어 표에 안 보이거나(정상일 수 있다), help
  블록이 비어 있을 때. 게시는 막지 않고 호출부가 로그/알림으로만 남긴다.

## "data_version"에 대한 노트

이 payload 스키마엔 별도 버전 번호 필드가 없다 — `strategies[].total.trips`
(모의 시대 포함 lifetime, 원장은 append-only라 정상 운영에선 절대 줄지 않는다)
합을 이 저장소의 "데이터 버전" 대리값으로 쓴다. `period.total_fills`는 쓰지
않는다 — 그 값은 real_seeded 경계 이후 스코프라, 다음 실계좌 이식처럼 스코프
자체가 바뀌는 정상적인 사건에서도 값이 뚝 떨어져 오탐을 낸다.

## 거래일 공백(4일) 판정에 대한 노트

"4 calendar days" 를 곧이곧대로 적용하면 주말(2일)만 넘어도 걸린다 — 그래서
"영업일 환산" 공백을 쓴다: 두 날짜 사이의 달력일 수에서 주말/휴일(호출부가
넘긴 `holidays`, 없으면 빈 집합)에 해당하는 날을 뺀 값이 4를 넘을 때만 경고한다.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from quant.control.performance import FX_KRW_PER_USD

__all__ = ["Finding", "validate_payload"]


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warn"
    path: str      # 점/대괄호 표기 JSON 경로, 예: "equity_asia.rows[12].date"
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "path": self.path, "message": self.message}


class _V:
    """findings 리스트에 이어붙이는 짧은 헬퍼. 검증 함수들이 공유하는 누산기."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def error(self, path: str, message: str) -> None:
        self.findings.append(Finding("error", path, message))

    def warn(self, path: str, message: str) -> None:
        self.findings.append(Finding("warn", path, message))


# ---------------------------------------------------------------------------
# 원시 타입 검사 — 값 하나(키 존재는 이미 확인됐다고 가정)
# ---------------------------------------------------------------------------


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_num(
    v: _V, val: object, path: str, *,
    allow_null: bool = False, min_: float | None = None, max_: float | None = None,
    severity_bounds: str = "warn",
) -> None:
    if val is None:
        if not allow_null:
            v.error(path, "null 허용 안 됨 (숫자 필요)")
        return
    if not _is_number(val):
        v.error(path, f"숫자여야 하는데 {type(val).__name__}: {val!r}")
        return
    if not math.isfinite(val):
        v.error(path, f"유한하지 않은 값(NaN/Inf 금지): {val!r}")
        return
    bound = v.error if severity_bounds == "error" else v.warn
    if min_ is not None and val < min_:
        bound(path, f"{val} < 허용 최소값 {min_}")
    if max_ is not None and val > max_:
        bound(path, f"{val} > 허용 최대값 {max_}")


def _check_int(v: _V, val: object, path: str, *, allow_null: bool = False, min_: int | None = None) -> None:
    if val is None:
        if not allow_null:
            v.error(path, "null 허용 안 됨 (정수 필요)")
        return
    if not isinstance(val, int) or isinstance(val, bool):
        v.error(path, f"정수여야 하는데 {type(val).__name__}: {val!r}")
        return
    if min_ is not None and val < min_:
        v.error(path, f"{val} < 허용 최소값 {min_}")


def _check_str(v: _V, val: object, path: str, *, allow_null: bool = False, allow_empty: bool = True) -> None:
    if val is None:
        if not allow_null:
            v.error(path, "null 허용 안 됨 (문자열 필요)")
        return
    if not isinstance(val, str):
        v.error(path, f"문자열이어야 하는데 {type(val).__name__}: {val!r}")
        return
    if not allow_empty and val.strip() == "":
        v.error(path, "빈 문자열")


def _check_bool(v: _V, val: object, path: str) -> None:
    if not isinstance(val, bool):
        v.error(path, f"bool이어야 하는데 {type(val).__name__}: {val!r}")


# ---------------------------------------------------------------------------
# 필드 래퍼 — dict + 키 이름을 받아 "필수 키 없음"까지 함께 처리
# ---------------------------------------------------------------------------


def _num_field(
    v: _V, d: dict, key: str, path: str, *,
    required: bool = True, allow_null: bool = False,
    min_: float | None = None, max_: float | None = None, bound_severity: str = "warn",
) -> None:
    if key not in d:
        if required:
            v.error(f"{path}.{key}", "필수 키 없음")
        return
    _check_num(v, d[key], f"{path}.{key}", allow_null=allow_null, min_=min_, max_=max_, severity_bounds=bound_severity)


def _int_field(v: _V, d: dict, key: str, path: str, *, required: bool = True, allow_null: bool = False, min_: int | None = None) -> None:
    if key not in d:
        if required:
            v.error(f"{path}.{key}", "필수 키 없음")
        return
    _check_int(v, d[key], f"{path}.{key}", allow_null=allow_null, min_=min_)


def _str_field(v: _V, d: dict, key: str, path: str, *, required: bool = True, allow_null: bool = False, allow_empty: bool = True) -> None:
    if key not in d:
        if required:
            v.error(f"{path}.{key}", "필수 키 없음")
        return
    _check_str(v, d[key], f"{path}.{key}", allow_null=allow_null, allow_empty=allow_empty)


def _bool_field(v: _V, d: dict, key: str, path: str, *, required: bool = True) -> None:
    if key not in d:
        if required:
            v.error(f"{path}.{key}", "필수 키 없음")
        return
    _check_bool(v, d[key], f"{path}.{key}")


# ---------------------------------------------------------------------------
# 날짜 — ISO 파싱 + 곡선 단조성/공백 검사
# ---------------------------------------------------------------------------


def _parse_date(s: object) -> date | None:
    if not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_dt(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _check_date_curve(v: _V, rows: list[dict], path: str, *, holidays: set[str]) -> None:
    """`rows`(전부 dict, `date` 필드 보유)의 날짜가 ISO고, 엄격 증가하며, 인접
    포인트 사이 공백이 영업일 환산 4일을 넘지 않는지 본다. 역순/중복은 error
    (데이터 정렬이 깨졌다는 뜻), 공백은 warn(모듈 docstring 참고)."""
    prev: date | None = None
    for i, row in enumerate(rows):
        raw = row.get("date")
        d = _parse_date(raw)
        if d is None:
            v.error(f"{path}[{i}].date", f"ISO 날짜(YYYY-MM-DD) 아님: {raw!r}")
            continue
        if prev is not None:
            if d <= prev:
                v.error(f"{path}[{i}].date", f"날짜가 역순/중복: {d.isoformat()} <= {prev.isoformat()}")
            else:
                gap = (d - prev).days
                non_business = sum(
                    1 for n in range(1, gap)
                    if (prev + timedelta(days=n)).weekday() >= 5
                    or (prev + timedelta(days=n)).isoformat() in holidays
                )
                business_gap = gap - non_business
                if business_gap > 4:
                    v.warn(
                        f"{path}[{i}].date",
                        f"거래일 공백 {gap}일(주말/휴일 제외 {business_gap}일) — "
                        f"{prev.isoformat()} → {d.isoformat()}",
                    )
        prev = d


# ---------------------------------------------------------------------------
# 공용 서브구조 — ChartYAxis / MarketStats / StrategyTotal
# ---------------------------------------------------------------------------


def _check_y_axis(v: _V, chart: dict, path: str) -> None:
    if "y_axis" not in chart:
        v.error(f"{path}.y_axis", "필수 키 없음")
        return
    axis = chart["y_axis"]
    if not isinstance(axis, dict):
        v.error(f"{path}.y_axis", f"object여야 하는데 {type(axis).__name__}")
        return
    p = f"{path}.y_axis"
    _num_field(v, axis, "min", p)
    _num_field(v, axis, "max", p)
    _num_field(v, axis, "zero", p)
    if "ticks" not in axis:
        v.error(f"{p}.ticks", "필수 키 없음")
    elif not isinstance(axis["ticks"], list):
        v.error(f"{p}.ticks", f"배열이어야 하는데 {type(axis['ticks']).__name__}")
    else:
        for i, t in enumerate(axis["ticks"]):
            if not _is_number(t) or not math.isfinite(t):
                v.error(f"{p}.ticks[{i}]", f"유한한 숫자가 아님: {t!r}")
    mn, mx = axis.get("min"), axis.get("max")
    if _is_number(mn) and _is_number(mx) and mn > mx:
        v.error(p, f"min({mn}) > max({mx})")


def _check_market_stats(v: _V, d: object, path: str) -> None:
    """MarketStats: trips/wins/win_rate/ci_low/ci_high/expectancy_bp/verdict/sample_warning."""
    if not isinstance(d, dict):
        v.error(path, f"object여야 하는데 {type(d).__name__}")
        return
    _int_field(v, d, "trips", path, min_=0)
    _int_field(v, d, "wins", path, min_=0)
    _num_field(v, d, "win_rate", path, min_=0.0, max_=1.0, bound_severity="error")
    _num_field(v, d, "ci_low", path, min_=0.0, max_=1.0, bound_severity="error")
    _num_field(v, d, "ci_high", path, min_=0.0, max_=1.0, bound_severity="error")
    _num_field(v, d, "expectancy_bp", path, min_=-100_000, max_=100_000, bound_severity="warn")
    _str_field(v, d, "verdict", path, allow_empty=False)
    _bool_field(v, d, "sample_warning", path)
    trips, wins = d.get("trips"), d.get("wins")
    if isinstance(trips, int) and isinstance(wins, int) and not isinstance(trips, bool) and not isinstance(wins, bool):
        if wins > trips:
            v.error(f"{path}.wins", f"wins({wins}) > trips({trips})")
    lo, hi = d.get("ci_low"), d.get("ci_high")
    if _is_number(lo) and _is_number(hi) and lo > hi:
        v.error(path, f"ci_low({lo}) > ci_high({hi})")


def _check_strategy_total(v: _V, d: object, path: str) -> None:
    """StrategyTotal = MarketStats + markets: Market[]."""
    if not isinstance(d, dict):
        v.error(path, f"object여야 하는데 {type(d).__name__}")
        return
    _check_market_stats(v, d, path)
    if "markets" not in d:
        v.error(f"{path}.markets", "필수 키 없음")
        return
    markets = d["markets"]
    if not isinstance(markets, list):
        v.error(f"{path}.markets", f"배열이어야 하는데 {type(markets).__name__}")
        return
    for i, m in enumerate(markets):
        if m not in ("KR", "US"):
            v.error(f"{path}.markets[{i}]", f"알 수 없는 market: {m!r}")


# ---------------------------------------------------------------------------
# 최상위 섹션
# ---------------------------------------------------------------------------


def _check_period(v: _V, payload: dict) -> None:
    if "period" not in payload:
        v.error("period", "필수 키 없음")
        return
    period = payload["period"]
    if not isinstance(period, dict):
        v.error("period", f"object여야 하는데 {type(period).__name__}")
        return
    _str_field(v, period, "start", "period", allow_null=True, allow_empty=False)
    _str_field(v, period, "end", "period", allow_null=True, allow_empty=False)
    _int_field(v, period, "sessions", "period", min_=0)
    _int_field(v, period, "total_fills", "period", min_=0)
    _str_field(v, period, "scope", "period", required=False, allow_empty=False)
    _str_field(v, period, "note", "period", required=False, allow_empty=False)
    _str_field(v, period, "note_en", "period", required=False, allow_empty=False)
    start, end = period.get("start"), period.get("end")
    ds, de = _parse_date(start) if isinstance(start, str) else None, _parse_date(end) if isinstance(end, str) else None
    if ds is not None and de is not None and ds > de:
        v.error("period", f"start({start}) > end({end})")


def _check_phases(v: _V, payload: dict) -> None:
    if "phases" not in payload:
        v.error("phases", "필수 키 없음")
        return
    phases = payload["phases"]
    if not isinstance(phases, list):
        v.error("phases", f"배열이어야 하는데 {type(phases).__name__}")
        return
    for i, p in enumerate(phases):
        path = f"phases[{i}]"
        if not isinstance(p, dict):
            v.error(path, "object가 아님")
            continue
        _str_field(v, p, "id", path, allow_empty=False)
        _str_field(v, p, "label", path, allow_empty=False)
        _str_field(v, p, "label_en", path, required=False, allow_empty=False)
        _str_field(v, p, "from", path, allow_empty=False)
        _str_field(v, p, "to", path, allow_null=True, allow_empty=False)
        _num_field(v, p, "seed_krw", path, allow_null=True, min_=0, bound_severity="error")
        _str_field(v, p, "seed_basis", path, required=False, allow_empty=False)
        _str_field(v, p, "seed_basis_en", path, required=False, allow_empty=False)
        _str_field(v, p, "note", path, allow_empty=False)
        _str_field(v, p, "note_en", path, required=False, allow_empty=False)
        frm = p.get("from")
        if isinstance(frm, str) and _parse_dt(frm) is None:
            v.error(f"{path}.from", f"ISO 타임스탬프 아님: {frm!r}")
        to = p.get("to")
        if isinstance(to, str) and _parse_dt(to) is None:
            v.error(f"{path}.to", f"ISO 타임스탬프 아님: {to!r}")


def _check_equity_book(v: _V, payload: dict, key: str, currency: str, holidays: set[str]) -> None:
    if key not in payload:
        v.error(key, "필수 키 없음")
        return
    book = payload[key]
    if not isinstance(book, dict):
        v.error(key, f"object여야 하는데 {type(book).__name__}")
        return
    if book.get("currency") != currency:
        v.error(f"{key}.currency", f"{currency!r} 이어야 하는데 {book.get('currency')!r}")
    _num_field(v, book, "seed", key, allow_null=True, min_=0, bound_severity="error")
    _str_field(v, book, "seed_basis", key, allow_empty=False)
    _str_field(v, book, "seed_basis_en", key, required=False, allow_empty=False)
    _num_field(v, book, "max_drawdown_pct", key, required=False, allow_null=True, min_=0, max_=1000, bound_severity="warn")

    if "rows" not in book:
        v.error(f"{key}.rows", "필수 키 없음")
    elif not isinstance(book["rows"], list):
        v.error(f"{key}.rows", f"배열이어야 하는데 {type(book['rows']).__name__}")
    else:
        rows = book["rows"]
        for i, r in enumerate(rows):
            path = f"{key}.rows[{i}]"
            if not isinstance(r, dict):
                v.error(path, "object가 아님")
                continue
            _str_field(v, r, "date", path, allow_empty=False)
            _num_field(v, r, "cum_pct", path, allow_null=True, min_=-100, max_=5000, bound_severity="warn")
            _num_field(v, r, "day_pct", path, allow_null=True, min_=-100, max_=100, bound_severity="warn")
            _int_field(v, r, "fills", path, min_=0)
            _str_field(v, r, "phase", path, allow_empty=False)
        _check_date_curve(v, [r for r in rows if isinstance(r, dict)], f"{key}.rows", holidays=holidays)

    if "chart" not in book:
        v.error(f"{key}.chart", "필수 키 없음")
    elif not isinstance(book["chart"], dict):
        v.error(f"{key}.chart", f"object여야 하는데 {type(book['chart']).__name__}")
    else:
        chart = book["chart"]
        _check_y_axis(v, chart, f"{key}.chart")
        if "phase_boundaries" not in chart:
            v.error(f"{key}.chart.phase_boundaries", "필수 키 없음")
        elif not isinstance(chart["phase_boundaries"], list):
            v.error(f"{key}.chart.phase_boundaries", f"배열이어야 하는데 {type(chart['phase_boundaries']).__name__}")
        else:
            for i, m in enumerate(chart["phase_boundaries"]):
                path = f"{key}.chart.phase_boundaries[{i}]"
                if not isinstance(m, dict):
                    v.error(path, "object가 아님")
                    continue
                _int_field(v, m, "index", path, min_=0)
                _str_field(v, m, "phase", path, allow_empty=False)
                _str_field(v, m, "label", path, allow_empty=False)
                _str_field(v, m, "label_en", path, required=False, allow_empty=False)


def _check_strategy_curve_points(v: _V, pts: object, path: str, holidays: set[str], *, epoch: bool) -> None:
    """`StrategyCurvePoint[]`(epoch=False) 또는 `PaperEpochCurvePoint[]`(epoch=True)."""
    if pts is None:
        return
    if not isinstance(pts, list):
        v.error(path, f"배열이어야 하는데 {type(pts).__name__}")
        return
    for j, pt in enumerate(pts):
        ppath = f"{path}[{j}]"
        if not isinstance(pt, dict):
            v.error(ppath, "object가 아님")
            continue
        _str_field(v, pt, "date", ppath, allow_empty=False)
        if epoch:
            _num_field(v, pt, "day_native", ppath)
            _num_field(v, pt, "cum_native", ppath)
            _int_field(v, pt, "trips", ppath, min_=0)
        else:
            _num_field(v, pt, "cum_net", ppath)
            _num_field(v, pt, "day_net", ppath)
            _int_field(v, pt, "cum_trips", ppath, min_=0)
        _num_field(v, pt, "cum_pct", ppath, required=False, allow_null=True, min_=-100, max_=100_000, bound_severity="warn")
    _check_date_curve(v, [p for p in pts if isinstance(p, dict)], path, holidays=holidays)


def _check_strategies(v: _V, payload: dict, holidays: set[str]) -> None:
    if "strategies" not in payload:
        v.error("strategies", "필수 키 없음")
        return
    strategies = payload["strategies"]
    if not isinstance(strategies, list):
        v.error("strategies", f"배열이어야 하는데 {type(strategies).__name__}")
        return
    seen_ids: set[str] = set()
    for i, s in enumerate(strategies):
        path = f"strategies[{i}]"
        if not isinstance(s, dict):
            v.error(path, "object가 아님")
            continue
        _str_field(v, s, "id", path, allow_empty=False)
        sid = s.get("id")
        if isinstance(sid, str):
            if sid in seen_ids:
                v.error(f"{path}.id", f"중복 전략 id: {sid}")
            seen_ids.add(sid)
        _str_field(v, s, "name_ko", path, allow_empty=False)
        _str_field(v, s, "name_en", path, required=False, allow_empty=False)

        if "total" not in s:
            v.error(f"{path}.total", "필수 키 없음")
        else:
            _check_strategy_total(v, s["total"], f"{path}.total")

        if "by_market" not in s:
            v.error(f"{path}.by_market", "필수 키 없음")
        elif not isinstance(s["by_market"], dict):
            v.error(f"{path}.by_market", f"object여야 하는데 {type(s['by_market']).__name__}")
        else:
            by_market = s["by_market"]
            for mkey in ("asia", "us"):
                if mkey not in by_market:
                    v.error(f"{path}.by_market.{mkey}", "필수 키 없음")
                elif by_market[mkey] is not None:
                    _check_market_stats(v, by_market[mkey], f"{path}.by_market.{mkey}")

        _num_field(v, s, "trades_per_day", path, required=False, min_=0, bound_severity="warn")
        _num_field(v, s, "avg_hold_minutes", path, required=False, min_=0, bound_severity="warn")
        _bool_field(v, s, "enabled", path, required=False)

        help_block = s.get("help")
        if help_block is not None and not isinstance(help_block, dict):
            v.error(f"{path}.help", f"object 또는 null이어야 하는데 {type(help_block).__name__}")

        curve = s.get("curve")
        if curve is not None:
            if not isinstance(curve, dict):
                v.error(f"{path}.curve", f"object 또는 null이어야 하는데 {type(curve).__name__}")
            else:
                for mkey in ("asia", "us"):
                    if mkey in curve:
                        _check_strategy_curve_points(v, curve[mkey], f"{path}.curve.{mkey}", holidays, epoch=False)


def _check_excluded(v: _V, payload: dict) -> None:
    if "excluded" not in payload:
        v.error("excluded", "필수 키 없음")
        return
    excluded = payload["excluded"]
    if not isinstance(excluded, dict):
        v.error("excluded", f"object여야 하는데 {type(excluded).__name__}")
        return
    sl = excluded.get("seeding_liquidation")
    if sl is None:
        return
    path = "excluded.seeding_liquidation"
    if not isinstance(sl, dict):
        v.error(path, f"object여야 하는데 {type(sl).__name__}")
        return
    _int_field(v, sl, "fills", path, min_=0)
    _str_field(v, sl, "note", path, allow_empty=False)
    _str_field(v, sl, "note_en", path, required=False, allow_empty=False)
    _num_field(v, sl, "krw_impact", path)
    _num_field(v, sl, "usd_impact", path)


def _check_costs(v: _V, payload: dict) -> None:
    if "costs" not in payload:
        v.error("costs", "필수 키 없음")
        return
    costs = payload["costs"]
    if not isinstance(costs, dict):
        v.error("costs", f"object여야 하는데 {type(costs).__name__}")
        return
    _num_field(v, costs, "kr_stock_roundtrip_bp", "costs", min_=0, max_=1000, bound_severity="warn")
    _num_field(v, costs, "kr_etf_roundtrip_bp", "costs", min_=0, max_=1000, bound_severity="warn")
    _num_field(v, costs, "us_roundtrip_bp", "costs", min_=0, max_=1000, bound_severity="warn")
    _num_field(v, costs, "kr_tax_bp", "costs", min_=0, max_=1000, bound_severity="warn")
    _str_field(v, costs, "note", "costs", allow_empty=False)
    _str_field(v, costs, "note_en", "costs", required=False, allow_empty=False)
    _num_field(v, costs, "fee_drag_pct_of_gross", "costs", required=False, allow_null=True, min_=-100_000, max_=100_000, bound_severity="warn")


def _check_prior_paper(v: _V, payload: dict) -> None:
    pp = payload.get("prior_paper")
    if pp is None:
        return  # 선택 필드
    if not isinstance(pp, dict):
        v.error("prior_paper", f"object여야 하는데 {type(pp).__name__}")
        return
    if not pp:
        return  # {} — 이식 전 기록 없음, 유효한 값
    _int_field(v, pp, "sessions", "prior_paper", min_=0)
    _int_field(v, pp, "fills", "prior_paper", min_=0)
    _num_field(v, pp, "net_krw", "prior_paper")
    _str_field(v, pp, "note", "prior_paper", allow_empty=False)
    _str_field(v, pp, "note_en", "prior_paper", required=False, allow_empty=False)


def _check_paper_epoch(v: _V, payload: dict, holidays: set[str]) -> None:
    pe = payload.get("paper_epoch")
    if pe is None:
        return  # 선택 필드(부재)
    if not isinstance(pe, dict):
        v.error("paper_epoch", f"object여야 하는데 {type(pe).__name__}")
        return
    if not pe:
        return  # {} — 착륙 전/에폭 미도래, 유효한 값

    _str_field(v, pe, "epoch", "paper_epoch", allow_empty=False)
    epoch = pe.get("epoch")
    if isinstance(epoch, str) and _parse_dt(epoch) is None:
        v.error("paper_epoch.epoch", f"ISO 타임스탬프 아님: {epoch!r}")

    if "account_model" not in pe:
        v.error("paper_epoch.account_model", "필수 키 없음")
    elif not isinstance(pe["account_model"], dict):
        v.error("paper_epoch.account_model", f"object여야 하는데 {type(pe['account_model']).__name__}")
    else:
        am = pe["account_model"]
        p = "paper_epoch.account_model"
        _num_field(v, am, "kr_start_krw", p, min_=0, bound_severity="error")
        _num_field(v, am, "us_start_usd", p, min_=0, bound_severity="error")
        _str_field(v, am, "note_ko", p, allow_empty=False)
        _str_field(v, am, "note_en", p, allow_empty=False)

    if "overall" not in pe:
        v.error("paper_epoch.overall", "필수 키 없음")
    elif not isinstance(pe["overall"], dict):
        v.error("paper_epoch.overall", f"object여야 하는데 {type(pe['overall']).__name__}")
    else:
        overall = pe["overall"]
        p = "paper_epoch.overall"
        if overall.get("currency") != "KRW":
            v.error(f"{p}.currency", f"'KRW' 이어야 하는데 {overall.get('currency')!r}")
        _num_field(v, overall, "seed_krw", p, allow_null=True, min_=0, bound_severity="error")
        _str_field(v, overall, "fx_source_note", p, allow_empty=False)
        _str_field(v, overall, "fx_source_note_en", p, required=False, allow_empty=False)
        _num_field(v, overall, "max_drawdown_pct", p, required=False, allow_null=True, min_=0, max_=1000, bound_severity="warn")
        if "rows" not in overall:
            v.error(f"{p}.rows", "필수 키 없음")
        elif not isinstance(overall["rows"], list):
            v.error(f"{p}.rows", f"배열이어야 하는데 {type(overall['rows']).__name__}")
        else:
            rows = overall["rows"]
            for i, r in enumerate(rows):
                rpath = f"{p}.rows[{i}]"
                if not isinstance(r, dict):
                    v.error(rpath, "object가 아님")
                    continue
                _str_field(v, r, "date", rpath, allow_empty=False)
                _num_field(v, r, "day_krw", rpath)
                _num_field(v, r, "cum_krw", rpath)
                _num_field(v, r, "cum_pct", rpath, allow_null=True, min_=-100, max_=5000, bound_severity="warn")
            _check_date_curve(v, [r for r in rows if isinstance(r, dict)], f"{p}.rows", holidays=holidays)
        if "chart" not in overall:
            v.error(f"{p}.chart", "필수 키 없음")
        elif not isinstance(overall["chart"], dict):
            v.error(f"{p}.chart", f"object여야 하는데 {type(overall['chart']).__name__}")
        else:
            _check_y_axis(v, overall["chart"], f"{p}.chart")

    if "strategies" not in pe:
        v.error("paper_epoch.strategies", "필수 키 없음")
        return
    if not isinstance(pe["strategies"], list):
        v.error("paper_epoch.strategies", f"배열이어야 하는데 {type(pe['strategies']).__name__}")
        return
    for i, s in enumerate(pe["strategies"]):
        path = f"paper_epoch.strategies[{i}]"
        if not isinstance(s, dict):
            v.error(path, "object가 아님")
            continue
        _str_field(v, s, "id", path, allow_empty=False)

        if "start_capital" not in s:
            v.error(f"{path}.start_capital", "필수 키 없음 (paper_epoch가 있으면 전략마다 시작자본이 있어야 함)")
        elif not isinstance(s["start_capital"], dict) or not s["start_capital"]:
            v.error(f"{path}.start_capital", "빈 값 — paper_epoch가 있으면 전략마다 시작자본이 있어야 함")
        else:
            sc = s["start_capital"]
            for cur in ("KRW", "USD"):
                if cur in sc:
                    _check_num(v, sc[cur], f"{path}.start_capital.{cur}", min_=0, severity_bounds="error")

        if "curve" not in s:
            v.error(f"{path}.curve", "필수 키 없음")
        elif not isinstance(s["curve"], dict):
            v.error(f"{path}.curve", f"object여야 하는데 {type(s['curve']).__name__}")
        else:
            curve = s["curve"]
            for mkey in ("asia", "us"):
                if mkey not in curve:
                    v.error(f"{path}.curve.{mkey}", "필수 키 없음")
                else:
                    _check_strategy_curve_points(v, curve[mkey], f"{path}.curve.{mkey}", holidays, epoch=True)


# ---------------------------------------------------------------------------
# 교차 필드 / 외부 대조 검증 — settings.yaml, 직전 발행본, 현재 시각
# ---------------------------------------------------------------------------


def _check_enabled_count(v: _V, payload: dict, strategies_cfg: dict | None) -> None:
    if strategies_cfg is None:
        return  # 설정을 안 받았으면 대조 불가 — 지어내지 않는다
    expected = sum(1 for cfg in strategies_cfg.values() if isinstance(cfg, dict) and cfg.get("enabled"))
    if "enabled_count" not in payload:
        v.error("enabled_count", "필수 키 없음 (settings.yaml 대조 불가)")
        return
    actual = payload["enabled_count"]
    if not isinstance(actual, int) or isinstance(actual, bool):
        return  # 타입 오류는 구조 검증(_check_int 계열)이 이미 잡는다(여기선 중복 보고 안 함)
    if actual != expected:
        v.error("enabled_count", f"config/settings.yaml 활성 전략 수({expected})와 불일치: {actual}")


def _check_enabled_strategies_present(v: _V, payload: dict, strategies_cfg: dict | None) -> None:
    if strategies_cfg is None:
        return
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return
    by_id = {s.get("id"): s for s in strategies if isinstance(s, dict)}
    for sid, cfg in strategies_cfg.items():
        if not (isinstance(cfg, dict) and cfg.get("enabled")):
            continue
        entry = by_id.get(sid)
        if entry is None:
            v.warn(f"strategies[id={sid}]", "설정상 활성 전략이지만 아직 종결 왕복이 없어 표에 없음(신규 전략이면 정상)")
            continue
        name_ko, name_en = entry.get("name_ko"), entry.get("name_en")
        if not isinstance(name_ko, str) or not name_ko.strip():
            v.error(f"strategies[id={sid}].name_ko", "활성 전략인데 한글 표시명이 없음")
        if not isinstance(name_en, str) or not name_en.strip():
            v.error(f"strategies[id={sid}].name_en", "활성 전략인데 영문 표시명이 없음")
        if not entry.get("help"):
            v.warn(f"strategies[id={sid}].help", "활성 전략인데 전략 설명(help)이 없음")


def _check_paper_epoch_overall_sum(v: _V, payload: dict) -> None:
    pe = payload.get("paper_epoch")
    if not pe or not isinstance(pe, dict):
        return
    strategies = pe.get("strategies")
    overall = pe.get("overall")
    if not isinstance(strategies, list) or not isinstance(overall, dict):
        return
    declared = overall.get("seed_krw")
    if declared is None or not _is_number(declared):
        return
    computed = 0.0
    for s in strategies:
        if not isinstance(s, dict):
            continue
        sc = s.get("start_capital")
        if not isinstance(sc, dict):
            continue
        krw, usd = sc.get("KRW"), sc.get("USD")
        if _is_number(krw):
            computed += float(krw)
        if _is_number(usd):
            computed += float(usd) * FX_KRW_PER_USD
    tolerance = max(1.0, abs(declared) * 1e-4)
    if abs(computed - declared) > tolerance:
        v.error(
            "paper_epoch.overall.seed_krw",
            f"전략별 start_capital 합(KRW 환산 {computed:.2f})과 불일치 — 선언값 {declared}",
        )


def _check_generated_at_freshness(v: _V, payload: dict, now: datetime | None) -> None:
    raw = payload.get("generated_at")
    if not isinstance(raw, str):
        return  # 타입 오류는 구조 검증이 이미 잡는다
    dt = _parse_dt(raw)
    if dt is None:
        v.error("generated_at", f"ISO 타임스탬프 아님: {raw!r}")
        return
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ref = now if now is not None else datetime.now(UTC)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=UTC)
    age_hours = abs((ref - dt).total_seconds()) / 3600
    if age_hours > 36:
        v.error("generated_at", f"생성 시각이 {age_hours:.1f}시간 전 — 36시간 기준 초과(발행 파이프라인 정지 의심)")


def _trade_count(payload: dict) -> int | None:
    """`strategies[].total.trips` 합 — 이 payload 스키마엔 없는 "data_version"의
    대리값(모듈 docstring 참고). 원장은 append-only라 정상 운영에선 절대 줄지
    않는다."""
    strategies = payload.get("strategies")
    if not isinstance(strategies, list):
        return None
    total = 0
    for s in strategies:
        if not isinstance(s, dict):
            continue
        block = s.get("total")
        trips = block.get("trips") if isinstance(block, dict) else None
        if _is_number(trips) and not isinstance(trips, bool):
            total += int(trips)
    return total


def _check_trade_count_monotonic(v: _V, payload: dict, previous: dict | None) -> None:
    if previous is None:
        return
    current, prior = _trade_count(payload), _trade_count(previous)
    if current is None or prior is None:
        return
    if current < prior:
        v.error(
            "strategies",
            f"누적 체결 왕복 수가 직전 발행본보다 줄었음: {prior} → {current} (데이터 유실 의심)",
        )


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def validate_payload(
    payload: dict,
    *,
    previous: dict | None = None,
    strategies_cfg: dict | None = None,
    now: datetime | None = None,
    holidays: Iterable[str] | None = None,
) -> list[Finding]:
    """`build_performance_payload`가 만든 JSON 하나를 검증해 `Finding` 목록을 낸다.

    - `previous`: 직전 발행본(같은 형식의 dict) — 있으면 체결 수 역행(데이터
      유실)까지 검사한다. 없으면 그 검사만 건너뛴다.
    - `strategies_cfg`: `config/settings.yaml`의 `strategies:` 블록 — 있으면
      `enabled_count`/활성 전략 표시명까지 대조한다. 없으면 그 검사들만 건너뛴다.
    - `now`: 신선도(`generated_at` 36시간 이내) 판정 기준 시각 — 생략하면
      `datetime.now(timezone.utc)`.
    - `holidays`: "YYYY-MM-DD" 문자열 집합 — 거래일 공백 판정에서 주말과 함께
      영업일 아님으로 취급한다. 생략하면 주말만 본다.

    알려지지 않은 최상위 키(예: 다른 워커가 얹는 `report_accuracy`)는 무시한다 —
    이 함수는 자신이 아는 키만 검증하고 나머지는 통과시킨다."""
    v = _V()
    holidays_set = set(holidays or ())

    if not isinstance(payload, dict):
        v.error("$", f"payload는 object여야 하는데 {type(payload).__name__}")
        return v.findings

    _str_field(v, payload, "generated_at", "$", allow_empty=False)
    _str_field(v, payload, "disclaimer", "$", allow_empty=False)
    _str_field(v, payload, "disclaimer_en", "$", required=False, allow_empty=False)

    _check_period(v, payload)
    _check_phases(v, payload)
    _check_equity_book(v, payload, "equity_asia", "KRW", holidays_set)
    _check_equity_book(v, payload, "equity_us", "USD", holidays_set)
    _check_strategies(v, payload, holidays_set)
    _check_excluded(v, payload)
    _check_costs(v, payload)
    _check_prior_paper(v, payload)
    _check_paper_epoch(v, payload, holidays_set)

    _check_enabled_count(v, payload, strategies_cfg)
    _check_enabled_strategies_present(v, payload, strategies_cfg)
    _check_paper_epoch_overall_sum(v, payload)
    _check_generated_at_freshness(v, payload, now)
    _check_trade_count_monotonic(v, payload, previous)

    return v.findings
