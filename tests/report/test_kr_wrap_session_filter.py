"""전일 KR 세션 종합의 **정규장 필터** — 2026-08-26 실데이터 회귀.

실사고: `kr_wrap.classify_session` 은 docstring 이 못 박은 대로 "KR 정규장
381분(09:00~15:30)" 프레임을 전제하는데, `gather_kr_wrap` 이 파케이를 **날짜로만**
걸러 넘겼다. Toss 1분봉 파티션의 실제 하루는 **08:01~20:00(720분, 장전+시간외
포함)** 이라, 세 패턴이 전부 엉뚱한 창에서 계산됐다:

- "초반 60분" → 08:01~09:00 프리마켓
- "마지막 60분"(매수 파동) → 19:00~20:00 시간외
- "마감 90분 내 전고 돌파" → 18:30~20:00 시간외

결과: 2026-08-25 세션에서 패턴 0건 → wrap 의 KR 절반이 통째로 누락 → 아침
리포트의 전일 패턴 후보 합류도 0건. **컴파일 에러도 테스트 실패도 없었다** —
기존 테스트는 정규장만 담긴 합성 프레임을 썼기 때문이다.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from quant.report.collect import uswrap

DAY = date(2026, 8, 25)


def _extended_day_frame() -> pd.DataFrame:
    """실데이터와 같은 모양: 08:01~20:00 KST 1분봉(720행), UTC 인덱스 저장."""
    idx = pd.date_range(f"{DAY.isoformat()} 08:01", periods=720, freq="1min",
                        tz="Asia/Seoul").tz_convert("UTC")
    return pd.DataFrame(
        {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 10.0},
        index=idx,
    )


def _write_partition(root: Path, symbol: str, df: pd.DataFrame) -> None:
    part = root / "data" / "history" / symbol / str(DAY.year) / f"{DAY.month:02d}.parquet"
    part.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(part)


def test_gather_passes_only_regular_session_bars(tmp_path, monkeypatch):
    """정규장(09:00~15:30, 391봉)만 분류기에 넘어간다 — 장전/시간외 봉이 섞이면
    '초반 60분'이 프리마켓이 되고 '마지막 60분'이 시간외가 된다."""
    _write_partition(tmp_path, "005930", _extended_day_frame())

    seen: dict = {}

    def _capture(bars_by_symbol, names=None, flow_summary=None, **kw):
        seen.update(bars_by_symbol)
        return {"patterns": {}, "symbols": []}

    monkeypatch.setattr("quant.analyze.kr_wrap.build_kr_session_wrap", _capture)
    uswrap.gather_kr_wrap(tmp_path, DAY)

    assert "005930" in seen, "그날 봉이 있는 심볼은 분류 대상이다"
    got = seen["005930"]
    local = got.index.tz_convert("Asia/Seoul")
    assert len(got) == 391, f"정규장 391봉이어야 하는데 {len(got)}봉"
    assert str(local.min().time()) == "09:00:00"
    assert str(local.max().time()) == "15:30:00"


def test_symbol_without_regular_session_bars_is_dropped(tmp_path, monkeypatch):
    """장전 봉만 있는 심볼은 아예 넘기지 않는다 — 반나절 데이터로 패턴을
    지어내지 않는다(classify_session 의 120봉 하한과 같은 원칙)."""
    idx = pd.date_range(f"{DAY.isoformat()} 08:01", periods=50, freq="1min",
                        tz="Asia/Seoul").tz_convert("UTC")
    _write_partition(tmp_path, "000660", pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx))

    seen: dict = {}
    monkeypatch.setattr("quant.analyze.kr_wrap.build_kr_session_wrap",
                        lambda bars_by_symbol, **kw: seen.update(bars_by_symbol) or None)
    uswrap.gather_kr_wrap(tmp_path, DAY)
    assert seen == {}


def test_real_shape_frame_can_now_match_a_pattern(tmp_path, monkeypatch):
    """필터가 붙으면 정규장 모양의 패턴이 실제로 잡힌다 — 필터 이전에는 같은
    데이터가 프리마켓 창에서 계산돼 0건이었다."""
    # 09:00~10:00 에 +2% 급등 후 유지, 그 앞뒤로 밋밋한 장전/시간외를 붙인다.
    pre = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
                        "volume": 1.0},
                       index=pd.date_range(f"{DAY.isoformat()} 08:01", periods=59,
                                           freq="1min", tz="Asia/Seoul"))
    rise = [100 + 2.0 * (i / 59) for i in range(60)]
    hold = [102.0] * 331
    session_px = rise + hold
    session = pd.DataFrame(
        {"open": session_px, "high": [p + 0.05 for p in session_px],
         "low": [p - 0.05 for p in session_px], "close": session_px,
         "volume": [10.0] * len(session_px)},
        index=pd.date_range(f"{DAY.isoformat()} 09:00", periods=len(session_px),
                            freq="1min", tz="Asia/Seoul"))
    post = pd.DataFrame({"open": 102.0, "high": 102.0, "low": 102.0, "close": 102.0,
                         "volume": 1.0},
                        index=pd.date_range(f"{DAY.isoformat()} 15:31", periods=100,
                                            freq="1min", tz="Asia/Seoul"))
    frame = pd.concat([pre, session, post])
    frame.index = frame.index.tz_convert("UTC")
    _write_partition(tmp_path, "005930", frame)

    out = uswrap.gather_kr_wrap(tmp_path, DAY)
    assert out is not None
    assert "초반강세지속" in (out.get("patterns") or {}), out
