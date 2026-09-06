"""리포트 실전화 3단계 골든 렌더 테스트용 픽스처 3종.

`tests/report/test_render.py`/`test_render_close.py`의 최소 픽스처 패턴을 그대로
따르되(새 Snapshot 규약을 만들지 않는다), 실제 `machine_payload`/`score_all`
같은 순수 함수를 그대로 불러 payload를 조립한다 — 손으로 두 벌(view의 score와
payload의 stance)을 따로 타이핑하면 그 자체로 픽스처가 내적 모순을 담을 위험이
있다(quant/report/lint.py가 바로 그런 모순을 잡으려는 도구이므로, 픽스처가
이미 모순이면 골든 테스트가 무의미해진다).

3종:
- `kr_normal_fixture()` — 후보 여럿, 섹터/관계/정확도 박스까지 있는 평범한 KR 아침.
- `us_normal_fixture()` — US 아침(섹터/외국인 수급 없음 — US는 원래 그 섹션이 없다).
- `thin_day_fixture()` — 후보 1건뿐인 조용한 날(대부분의 선택적 섹션이 비어 있다).
"""
from __future__ import annotations

from datetime import datetime

from quant.analyze.briefing import STANCE_LABEL
from quant.analyze.render import machine_payload
from quant.analyze.scoring import label_100
from quant.analyze.symbol_score import score_all
from quant.collect.contracts import SCHEMA_VERSION, Snapshot
from quant.control import report_accuracy
from quant.core.report_clock import KST
from quant.report.model import ReportModel

_KR_AT = datetime(2026, 9, 5, 8, 0, tzinfo=KST)
_US_AT = datetime(2026, 9, 5, 20, 0, tzinfo=KST)


def _snap(market: str, at: datetime) -> Snapshot:
    return Snapshot(SCHEMA_VERSION, market, at.date(), at, {})


def _stance_view(score: int, span: int = 5, positives=None, negatives=None) -> dict:
    """`quant.analyze.briefing.stance()`와 정확히 같은 모양의 dict를 낸다 —
    `label_100`을 그대로 재사용해 score100/tier가 항상 서로 맞게 만든다
    (손타이핑 불일치를 원천적으로 없앤다, 이 모듈 docstring)."""
    from quant.analyze.scoring import to_100

    score100 = to_100(score, span)
    tier = label_100(score100, "상승 신호", "하락 신호")
    positives = positives or []
    negatives = negatives or []
    head = ", ".join(positives[:3]) if positives else ""
    tail = ", ".join(negatives[:2]) if negatives else ""
    line = f"{head or tail or '임계를 넘긴 요인이 없다'} — 참고용 스탠스"
    return {
        "label": STANCE_LABEL, "tier": tier, "score": score, "score100": score100,
        "line": line, "positives": positives, "negatives": negatives,
    }


def kr_normal_fixture() -> tuple[ReportModel, Snapshot]:
    snap = _snap("KR", _KR_AT)
    cont = {
        "005930": {
            "name": "삼성전자", "days": 5, "articles": 12, "today_articles": 6,
            "streak_days": 4, "is_new": False, "history": [True] * 10, "titles": [],
            "in_ranking": True, "ranking_bullish": True,
        },
        "000660": {
            "name": "SK하이닉스", "days": 3, "articles": 4, "today_articles": 2,
            "streak_days": 2, "is_new": False, "history": [True] * 10, "titles": [],
            "in_ranking": False, "ranking_bullish": False,
        },
    }
    delta = {}
    brief = [{"level": "watch", "text": "반도체 업종 거래대금 상위 집중"}]
    view = _stance_view(2, positives=["KOSPI +1.20%", "외국인 +3,200억 순매수"])
    sym_quotes = {
        "005930": {"close": 82000.0, "change_pct": 2.1, "date": "2026-09-04"},
        "000660": {"close": 210000.0, "change_pct": -0.4, "date": "2026-09-04"},
    }
    scores = score_all(cont, {}, {})
    baselines = {"005930": 70, "000660": 55}
    relations = {
        "005930": [
            {"dst": "009150", "kind": "beneficiary", "reason": "삼성전기 부품 공급망 수혜",
             "evidence_score": 90, "last_verified": "2026-09-01", "source": "naver_theme"},
        ],
    }
    sectors = {"005930": "반도체와반도체장비", "000660": "반도체와반도체장비"}

    payload = machine_payload(
        snap, cont, delta, brief, sym_quotes=sym_quotes, details={}, view=view,
        scores=scores, trending={}, relations=relations, sectors=sectors,
        baselines=baselines,
    )
    payload["report_accuracy"] = report_accuracy.report_summary(None)

    model = ReportModel(
        payload=payload, cont=cont, delta=delta, brief=brief, sym_quotes=sym_quotes,
        details={}, view=view, scores=scores, relations=relations,
        name_map={"005930": "삼성전자", "000660": "SK하이닉스", "009150": "삼성전기"},
        report_accuracy=payload["report_accuracy"],
    )
    return model, snap


def us_normal_fixture() -> tuple[ReportModel, Snapshot]:
    snap = _snap("US", _US_AT)
    cont = {
        "AAPL": {
            "name": "AAPL", "days": 4, "articles": 8, "today_articles": 5,
            "streak_days": 3, "is_new": False, "history": [True] * 10, "titles": [],
            "in_ranking": True, "ranking_bullish": True,
        },
        "NVDA": {
            "name": "NVDA", "days": 2, "articles": 3, "today_articles": 1,
            "streak_days": 1, "is_new": True, "history": [True] * 10, "titles": [],
            "in_ranking": False, "ranking_bullish": False,
        },
    }
    delta = {}
    brief = [{"level": "signal", "text": "나스닥 거래대금 상위 반도체 집중"}]
    view = _stance_view(-1, negatives=["VIX 21.4"])
    sym_quotes = {
        "AAPL": {"close": 230.5, "change_pct": 1.8, "date": "2026-09-04"},
        "NVDA": {"close": 118.2, "change_pct": -2.3, "date": "2026-09-04"},
    }
    scores = score_all(cont, {}, {})

    payload = machine_payload(
        snap, cont, delta, brief, sym_quotes=sym_quotes, details={}, view=view,
        scores=scores, trending={},
    )

    model = ReportModel(
        payload=payload, cont=cont, delta=delta, brief=brief, sym_quotes=sym_quotes,
        details={}, view=view, scores=scores,
    )
    return model, snap


def thin_day_fixture() -> tuple[ReportModel, Snapshot]:
    """후보 1건뿐인 조용한 날 — 섹터/관계/정확도/시세 대부분이 비어 있다.
    렌더가 결측을 NaN/None 노출 없이 정직하게 "결측"으로 보이는지 확인하는
    용도(§C 결측 표시 관례)."""
    snap = _snap("KR", _KR_AT)
    cont = {
        "005930": {
            "name": "삼성전자", "days": 1, "articles": 1, "today_articles": 1,
            "streak_days": 1, "is_new": True, "history": [True], "titles": [],
        },
    }
    delta = {}
    brief = []
    view = _stance_view(0)
    payload = machine_payload(snap, cont, delta, brief, view=view, scores=score_all(cont, {}, {}))

    model = ReportModel(
        payload=payload, cont=cont, delta=delta, brief=brief, view=view,
        scores=score_all(cont, {}, {}),
    )
    return model, snap
