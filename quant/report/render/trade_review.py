"""데일리 매매 리뷰 HTML 렌더러 — `quant.control.trade_review.build_trade_review`가
만든 순수 dict만 보고 그린다.

기존 아침/마감 리포트 렌더러(`quant/analyze/render.py`)와 같은 패턴(Jinja2 +
`_dated_dir` 출력 규약)을 따르되, `ReportModel`/`Snapshot`에 의존하지 않는다 —
이 리포트는 원장(`data/state/trades.jsonl`)과 봉 데이터만 입력으로 받는 별도
산출물이라 리포트 엔진 조립부(`quant/report/model.py`)를 몰라도 된다. 템플릿
파일은 `quant/analyze/templates/`에 같이 둔다(기존 템플릿과 같은 폰트/색상
변수를 그대로 복사해 쓰므로 한곳에 있는 편이 유지보수에 낫다).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "analyze" / "templates"

_MARKET_NAME = {"KR": "한국", "US": "미국"}


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_trade_review(review: dict, market: str, on: date) -> str:
    """`review`(`build_trade_review` 반환값) → HTML 문자열."""
    return _env().get_template("trade_review.html.j2").render(
        review=review,
        market=market,
        market_name=_MARKET_NAME.get(market, market),
        on=on,
        review_json=json.dumps(review, ensure_ascii=False),
    )


def _dated_dir(root: Path, on: date) -> Path:
    path = Path(root) / f"{on.year:04d}" / f"{on.month:02d}" / f"{on.day:02d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_trade_review(review: dict, market: str, on: date, out_root: Path) -> tuple[Path, Path]:
    """`out/YYYY/MM/DD/{market}_trade_review.{html,json}` 2종을 쓰고 경로를 반환.

    기존 아침 리포트(`write_html`/`write_machine`)와 같은 출력 규약 — 서빙
    스크립트가 심볼릭링크/nginx 룰을 추가로 안 만들어도 된다."""
    d = _dated_dir(out_root, on)
    hp = d / f"{market}_trade_review.html"
    hp.write_text(render_trade_review(review, market, on), encoding="utf-8")
    jp = d / f"{market}_trade_review.json"
    jp.write_text(json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return hp, jp
