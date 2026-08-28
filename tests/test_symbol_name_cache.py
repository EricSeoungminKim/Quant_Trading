"""표시명 영속 캐시 — 유니버스에서 빠진 보유 종목의 이름이 사라지지 않는다.

2026-08-28 소유자 지적: "리포트에 한국 주식 종목 번호만 보여주고 이름을 안
보여준다. 이거 이름 항상 등록해야 한다 — 안 그러면 무슨 기업인지 내가 검색해야
하잖아."

실측한 원인: 이름 조회 대상이 `markets`(현재 유니버스)뿐이라, **보유 중이지만
워치리스트에서 빠진 종목**은 조회조차 되지 않았다. 실제로 그날 보유 8종목 중
워치리스트에 남아 있던 005930·066570만 이름이 나왔고, 빠진 6종목(042700·096770·
001450·007340·035720·000500)은 전부 번호로만 표시됐다.
"""
from __future__ import annotations

import json

from quant.apps.assembly import _load_symbol_names, _save_symbol_names


def test_saved_names_are_read_back(tmp_path):
    p = tmp_path / "symbol_names.json"
    _save_symbol_names({"005930": "삼성전자", "042700": "한미반도체"}, p)
    assert _load_symbol_names(p) == {"005930": "삼성전자", "042700": "한미반도체"}


def test_missing_file_is_empty_not_an_error(tmp_path):
    """캐시가 없다고 기동을 막지 않는다 — 첫 실행이 정상 경로다."""
    assert _load_symbol_names(tmp_path / "없음.json") == {}


def test_broken_file_is_empty_not_an_error(tmp_path):
    p = tmp_path / "symbol_names.json"
    p.write_text("{깨진 json", encoding="utf-8")
    assert _load_symbol_names(p) == {}


def test_non_dict_payload_is_rejected(tmp_path):
    p = tmp_path / "symbol_names.json"
    p.write_text(json.dumps(["삼성전자"]), encoding="utf-8")
    assert _load_symbol_names(p) == {}


def test_empty_symbol_or_name_is_dropped(tmp_path):
    """빈 이름을 캐시에 남기면 '이름을 안다'고 착각해 다음 조회를 건너뛴다."""
    p = tmp_path / "symbol_names.json"
    p.write_text(json.dumps({"005930": "삼성전자", "000000": "", "": "빈코드"}), encoding="utf-8")
    assert _load_symbol_names(p) == {"005930": "삼성전자"}


def test_dropped_symbol_keeps_its_name_across_runs(tmp_path):
    """**이 문제의 핵심 계약**: 유니버스에서 빠져도 이름은 남는다.

    1회차에 워치리스트에 있어 이름을 알았고, 2회차에는 그 종목이 유니버스에서
    빠져 신규 조회 결과에 없다 — 그래도 병합 결과에는 이름이 남아야 한다.
    """
    p = tmp_path / "symbol_names.json"
    _save_symbol_names({"000500": "가온전선", "005930": "삼성전자"}, p)

    cached = _load_symbol_names(p)          # 2회차 기동
    fresh = {"005930": "삼성전자"}           # 000500 은 유니버스에서 빠져 조회 안 됨
    cached.update(fresh)                     # assembly 의 병합 순서와 동일
    _save_symbol_names(cached, p)

    assert _load_symbol_names(p)["000500"] == "가온전선"


def test_fresh_lookup_overwrites_stale_name(tmp_path):
    """새 조회가 있으면 그 값이 이긴다 — 상장사명 변경을 캐시가 붙잡지 않는다."""
    p = tmp_path / "symbol_names.json"
    _save_symbol_names({"005930": "옛이름"}, p)
    cached = _load_symbol_names(p)
    cached.update({"005930": "삼성전자"})
    _save_symbol_names(cached, p)
    assert _load_symbol_names(p)["005930"] == "삼성전자"


def test_save_failure_does_not_raise(tmp_path):
    """쓰기 실패가 거래를 막으면 안 된다 — 경고만 남기고 통과."""
    unwritable = tmp_path / "없는디렉토리" / "sub" / "x.json"
    (tmp_path / "없는디렉토리").write_text("파일이라 mkdir 실패", encoding="utf-8")
    _save_symbol_names({"005930": "삼성전자"}, unwritable)  # 예외가 새면 실패
