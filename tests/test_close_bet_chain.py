"""종가배팅 체인(2026-08-25 전략 4종 체제 ③) 테스트 공백 메우기.

체인: `_build_close_bet_view`(거래대금 보드 + 등락 하한 + 외국인 수급 라벨 +
뉴스 언급 → 결정론 채점 top-5) → `close_bet_tokens`(SYM:CLOSE 토큰 추출) →
`engine_tokens`의 번역표(CLOSE→CLOSE_BET) → `watch_scorer._parse_token`
(CLOSE_BET이 유효 태그인지). 소스 코드는 건드리지 않는다 — 이 파일은 이미
구현된 동작을 고정할 뿐이고, 실패하면 소스 버그로 본다.
"""
from __future__ import annotations

import json
import types

import pytest

from quant.analyze.market_brief import close_bet_tokens, engine_tokens
from quant.analyze.watch_scorer import _VALID_TAGS, _parse_token
from quant.report.collect.close import _build_close_bet_view


# ── 픽스처 헬퍼 ──────────────────────────────────────────────────────────

def _snap(board_items: list[dict], board_name: str = "거래대금"):
    """`_build_close_bet_view`가 읽는 snap.results["toss_rankings"] 모양."""
    return types.SimpleNamespace(
        results={
            "toss_rankings": types.SimpleNamespace(
                ok=True,
                data={"boards": {board_name: board_items}},
            ),
        },
    )


def _write_frgn_flow(tmp_path, symbol: str, values: list[float]) -> None:
    """data/ledger/frgn_flow.jsonl 실제 스키마(quant/control/frgn_flow.py):
    {"date", "symbol", "foreign_net", "inst_net"} 한 줄에 한 행, 날짜 오름차순."""
    path = tmp_path / "data" / "ledger" / "frgn_flow.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, v in enumerate(values, start=1):
        row = {
            "date": f"2026-08-{i:02d}",
            "symbol": symbol,
            "foreign_net": v,
            "inst_net": 0,
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 외국인 수급: 이탈(-50) → 재유입(+100, 직전 이탈 절대값 50을 초과) = "매수
# 시그널(재유입)" 라벨(foreign_trend.classify 판정 규칙 그대로).
_INFLOW_SERIES = [-50.0, 100.0]
# 계속 순매도만 있으면(길이 2 이상 연속 순매도) "이탈 추세" — 매수 라벨이 아니다.
_OUTFLOW_SERIES = [-30.0, -40.0]


# ── _build_close_bet_view: 채점 ─────────────────────────────────────────

def test_excludes_items_below_change_pct_floor(tmp_path):
    """당일 등락 3.0% 하한 미달 종목은 후보에서 아예 빠진다(1차 필터가 아니라
    스펙이 명시한 하한 — 약한 종목까지 채점 테이블에 올리면 안 된다)."""
    snap = _snap([
        {"symbol": "000001", "name": "미달", "change_pct": 2.9, "rank": 1,
         "trading_amount": 1000},
    ])
    out = _build_close_bet_view(snap, tmp_path, cont={})
    assert out == []


def test_foreign_inflow_label_adds_three_points(tmp_path):
    """외국인 수급 라벨이 매수 계열("매수 시그널(재유입)")이면 +3점 — 근거 축
    중 가장 무겁다(수급 > 등락 > 뉴스, docstring 채점 순서)."""
    _write_frgn_flow(tmp_path, "000002", _INFLOW_SERIES)
    snap = _snap([
        {"symbol": "000002", "name": "매수유입", "change_pct": 3.0, "rank": 1,
         "trading_amount": 1000},
    ])
    out = _build_close_bet_view(snap, tmp_path, cont={})
    assert len(out) == 1
    # 등락 하한 통과(+2) + 외국인 매수(+3) = 5점, 뉴스 언급 없음(cont={})
    assert out[0]["score"] == 5
    assert any("외국인" in r for r in out[0]["reasons"])


def test_news_mention_adds_one_point(tmp_path):
    """오늘 뉴스에 언급된 종목(cont dict에 심볼 존재)은 +1점."""
    snap = _snap([
        {"symbol": "000003", "name": "뉴스종목", "change_pct": 3.0, "rank": 1,
         "trading_amount": 1000},
    ])
    out = _build_close_bet_view(snap, tmp_path, cont={"000003": {"titles": ["호재"]}})
    assert len(out) == 1
    # 등락 하한 통과(+2) + 뉴스 언급(+1) = 3점, 수급 원장 없음(가점 없음)
    assert out[0]["score"] == 3
    assert "오늘 뉴스 언급" in out[0]["reasons"]


def test_top_five_cut_keeps_highest_scored(tmp_path):
    """6개 후보 중 상위 5개만 남는다(CLOSE_BET_TOP=5) — 소유자 실전 감각과
    맞춘 상한. score desc, change desc 정렬을 함께 확인한다."""
    board = [
        {"symbol": f"{i:06d}", "name": f"종목{i}", "change_pct": 3.0 + i, "rank": i,
         "trading_amount": 1000}
        for i in range(1, 7)
    ]
    snap = _snap(board)
    out = _build_close_bet_view(snap, tmp_path, cont={})
    assert len(out) == 5
    # 뉴스/수급 가점이 전부 동률(0)이므로 change_pct 내림차순이 결정한다 —
    # 가장 낮은 change_pct(000001, 4.0%)가 잘려나가야 한다.
    symbols = [item["symbol"] for item in out]
    assert "000001" not in symbols
    assert symbols == sorted(symbols, key=lambda s: -board[int(s) - 1]["change_pct"])


def test_empty_when_trading_amount_board_missing(tmp_path):
    """거래대금 보드가 없으면(랭킹 실패든 다른 보드만 있든) 빈 리스트 — 1차
    필터 자체가 성립하지 않으므로 채점을 시도하지 않는다."""
    snap = _snap([], board_name="상승률")  # 거래대금 보드 없음, 다른 보드만 존재
    out = _build_close_bet_view(snap, tmp_path, cont={})
    assert out == []

    snap_no_data = types.SimpleNamespace(
        results={"toss_rankings": types.SimpleNamespace(ok=False, data=None)},
    )
    assert _build_close_bet_view(snap_no_data, tmp_path, cont={}) == []


def test_missing_frgn_flow_file_does_not_raise(tmp_path):
    """frgn_flow.jsonl이 아예 없어도(경로 미생성) 예외 없이 진행 — 수급
    가점만 빠지고 등락/뉴스 채점은 살아있어야 한다(원장 문제로 후보 전체를
    버리지 않는다는 docstring의 명시적 방어)."""
    # tmp_path 밑에 data/ledger 디렉터리조차 만들지 않는다.
    snap = _snap([
        {"symbol": "000004", "name": "원장없음", "change_pct": 3.5, "rank": 1,
         "trading_amount": 1000},
    ])
    out = _build_close_bet_view(snap, tmp_path, cont={"000004": {}})
    assert len(out) == 1
    # 등락 하한 통과(+2) + 뉴스 언급(+1) = 3점 — 외국인 가점 없음
    assert out[0]["score"] == 3
    assert not any("외국인" in r for r in out[0]["reasons"])


def test_outflow_label_does_not_add_points(tmp_path):
    """외국인 라벨이 이탈 추세("이탈 추세")면 "매수" 문자열이 없으므로 가점이
    붙지 않는다 — +3은 매수 계열 라벨 전용."""
    _write_frgn_flow(tmp_path, "000005", _OUTFLOW_SERIES)
    snap = _snap([
        {"symbol": "000005", "name": "이탈", "change_pct": 3.0, "rank": 1,
         "trading_amount": 1000},
    ])
    out = _build_close_bet_view(snap, tmp_path, cont={})
    assert len(out) == 1
    assert out[0]["score"] == 2
    assert not any("외국인" in r for r in out[0]["reasons"])


# ── close_bet_tokens: payload → SYM:CLOSE ───────────────────────────────

def test_close_bet_tokens_extracts_symbol_close_pairs():
    """close_bet_view의 각 항목에서 심볼을 뽑아 SYM:CLOSE 토큰으로 낸다."""
    payload = {
        "close_bet_view": [
            {"symbol": "005930", "name": "삼성전자", "score": 5},
            {"symbol": "000660", "name": "SK하이닉스", "score": 3},
        ],
    }
    assert close_bet_tokens(payload) == ["005930:CLOSE_BET", "000660:CLOSE_BET"]


def test_close_bet_tokens_empty_when_key_missing():
    """close_bet_view 키가 없는 payload(아침판 engine.json)는 빈 리스트."""
    assert close_bet_tokens({}) == []
    assert close_bet_tokens({"close_bet_view": []}) == []


# ── engine_tokens 번역: CLOSE → CLOSE_BET ───────────────────────────────

def test_engine_tokens_translates_close_to_close_bet():
    """AUTO_WATCH 줄의 CLOSE 태그가 watch-score 어휘 CLOSE_BET으로 번역된다
    (market_brief._TAG_MAP["CLOSE"] = "CLOSE_BET"). 번역 없이 나가면
    watch_scorer._parse_token이 "알 수 없는 태그"로 강등한다(모듈 docstring
    실측 사례와 같은 실패 모드)."""
    payload = {
        "schema": 1,
        "market": "KR",
        "session_date": "2026-08-25",
        "auto_watch": "AUTO_WATCH: 005930:CLOSE",
        "symbols": [],
    }
    tokens = engine_tokens(payload, "KR")
    assert tokens == ["005930:CLOSE_BET"]


def test_close_bet_tokens_output_survives_watch_scorer_parsing():
    """실제 운영 배선(server/scripts/brief_from_report.py:127)은
    `close_bet_tokens(payload)`가 낸 토큰을 `engine_tokens`의 번역표를 거치지
    않고 그대로 `tokens` 리스트에 이어붙인다("CLOSE → CLOSE_BET 번역은
    engine_tokens 와 같은 표"라는 그 파일의 주석과 달리, 실제로는 어떤 번역도
    거치지 않는다). 그 토큰이 결국 watch_scorer._parse_token으로 들어갔을 때
    태그가 살아남아야 close_bet 전략이 이 후보를 실제로 소비할 수 있다."""
    payload = {"close_bet_view": [{"symbol": "005930", "name": "삼성전자", "score": 5}]}
    tokens = close_bet_tokens(payload)
    assert tokens == ["005930:CLOSE_BET"]  # 엔진 어휘 직접 발행(수정 후 계약)

    symbol, tags, report_date, reasons = _parse_token(tokens[0])
    # 이 assert가 실패한다면 소스 버그다: close_bet_tokens가 리터럴 "CLOSE"를
    # 내는데(market_brief.py의 _build 부분, _TAG_MAP 미적용) watch_scorer의
    # 유효 태그는 "CLOSE_BET"뿐이다(_VALID_TAGS) — 운영 경로에서 이 태그는
    # "알 수 없는 태그"로 강등되어 close_bet 전략이 이 후보를 CLOSE_BET으로
    # 인식하지 못한다(engine_tokens.py 상단 docstring이 경고하는 바로 그 실패
    # 모드, 다만 이번엔 다른 함수에서 발생).
    assert tags == ["CLOSE_BET"], (
        f"close_bet_tokens가 낸 토큰이 watch_scorer 파서에서 살아남지 못함: "
        f"tags={tags!r} reasons={reasons!r}"
    )


# ── watch_scorer 파서: CLOSE_BET이 유효 태그인가 ────────────────────────

def test_close_bet_is_a_valid_tag():
    """CLOSE_BET이 _VALID_TAGS에 있어야 파서가 무태그로 강등하지 않는다."""
    assert "CLOSE_BET" in _VALID_TAGS


def test_parse_token_keeps_close_bet_tag_instead_of_demoting():
    """_parse_token("005930:CLOSE_BET")이 태그를 그대로 보존해야 한다 —
    "알 수 없는 태그"로 강등되면 close_bet 전략이 이 종목을 영영 진입시키지
    못한다(engine_tokens docstring의 실측 실패 모드와 동일 구조)."""
    symbol, tags, report_date, reasons = _parse_token("005930:CLOSE_BET")
    assert symbol == "005930"
    assert tags == ["CLOSE_BET"]
    assert reasons == []
