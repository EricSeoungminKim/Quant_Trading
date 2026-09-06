"""리포트 실전화 3단계 — 골든 렌더 테스트.

세 픽스처(정상 KR/정상 US/후보 적은 조용한 날, `fixtures/report_fixtures.py`)를
실제 HTML 템플릿·텔레그램 요약 렌더러에 그대로 통과시켜:

1. 결과 문자열에 "nan"/"None"/"undefined"/미렌더 Jinja 마커가 하나도 없는지
   (렌더 *결과물*에 대한 최종 방어선 — `quant/report/lint.py`는 렌더 *전* 모델을
   보므로, 템플릿 자체의 버그(예: 필터 하나가 float('nan')을 그대로 통과시키는
   경우)는 이 테스트가 아니면 못 잡는다).
2. 골든 스냅샷과 바이트 단위로 일치하는지(회귀 방지 — 의도한 템플릿 변경이면
   `tests/report/golden/`의 해당 파일을 지우고 이 테스트를 다시 돌려 재생성한다).
3. `lint_report()`가 그 모델에서 error 등급을 하나도 내지 않는지.

골든 파일이 없으면(신규 픽스처 추가 등) 이 테스트가 만들어주고 **의도적으로
실패**한다 — 그 실패 메시지를 보고 한 번 더 돌려 통과를 확인하라는 뜻이다
(스냅샷을 조용히 넘기면 첫 실행이 곧 "검증 없이 승인"이 된다).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from report_fixtures import (  # noqa: E402 — sys.path 조작 뒤 임포트
    kr_normal_fixture,
    thin_day_fixture,
    us_normal_fixture,
)

from quant.report.lint import lint_report
from quant.report.render.html import write_open_report
from quant.report.render.telegram import _format_summary

GOLDEN_DIR = Path(__file__).parent / "golden"

_FORBIDDEN = [
    re.compile(r"\bnan\b", re.IGNORECASE),
    re.compile(r"\bNone\b"),
    # 여는 델리미터만 본다 — 닫는 쪽(`}}`/`%}`)은 CSS 블록 경계에서 정상적으로
    # 나온다(`quant/report/lint.py`의 같은 주석 참고).
    re.compile(r"\{\{|\{%"),
    re.compile(r"\bundefined\b", re.IGNORECASE),
]

FIXTURES = {
    "kr_normal": kr_normal_fixture,
    "us_normal": us_normal_fixture,
    "thin_day": thin_day_fixture,
}


def _assert_no_leaks(text: str, where: str) -> None:
    for pattern in _FORBIDDEN:
        m = pattern.search(text)
        assert not m, (
            f"{where}: 금지 마커 {m.group()!r} 발견 — "
            f"주변: {text[max(0, m.start() - 60):m.start() + 60]!r}"
        )


def _check_golden(actual: str, golden_path: Path) -> None:
    if not golden_path.exists():
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        pytest.fail(
            f"골든 파일이 없어 새로 만들었다: {golden_path} — "
            "테스트를 다시 돌려 대조 통과를 확인할 것(첫 실행은 항상 실패한다)"
        )
    expected = golden_path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{golden_path.name} 렌더 결과가 골든 스냅샷과 다르다 — "
        "의도한 템플릿/픽스처 변경이면 이 파일을 지우고 테스트를 재실행해 재생성한다"
    )


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_html_render_has_no_leaks_and_matches_golden(name, tmp_path):
    model, snap = FIXTURES[name]()
    hp, _jp, _cp = write_open_report(model, snap, tmp_path)
    html = hp.read_text(encoding="utf-8")
    _assert_no_leaks(html, f"{name} HTML")
    _check_golden(html, GOLDEN_DIR / f"{name}.html")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_telegram_summary_has_no_leaks_and_matches_golden(name):
    model, _snap = FIXTURES[name]()
    text = _format_summary(model.payload)
    _assert_no_leaks(text, f"{name} 텔레그램 요약")
    _check_golden(text, GOLDEN_DIR / f"{name}_telegram.txt")


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_lint_report_has_no_errors_on_fixtures(name):
    """정상/조용한 날 픽스처는 애초에 error 등급 결함이 없어야 정상이다 —
    이게 실패하면 픽스처 자체가 내적으로 모순됐다는 뜻(§report_fixtures.py
    docstring)."""
    model, _snap = FIXTURES[name]()
    findings = lint_report(model)
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, f"{name}: lint 오류 {len(errors)}건 — {errors}"


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_lint_report_payload_only_matches_model_result(name):
    """`lint_report(model)`과 `lint_report(model.payload)`가 공통으로 낼 수
    있는 error는 같아야 한다 — payload만 있는 과거 engine.json 아카이브를
    린트할 때도 같은 결함은 같은 등급으로 잡혀야 하므로(모델 전용 검사는
    당연히 payload 경로에서 빠진다 — 그 반대, 즉 payload 로는 안 잡히던
    error가 model 에서 새로 생기는 것만 없으면 된다)."""
    model, _snap = FIXTURES[name]()
    model_errors = {f.message for f in lint_report(model) if f.severity == "error"}
    payload_errors = {f.message for f in lint_report(model.payload) if f.severity == "error"}
    assert payload_errors <= model_errors
