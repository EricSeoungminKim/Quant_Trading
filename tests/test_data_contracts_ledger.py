"""데이터 계약 테스트 — 원장/보고 jsonl 파일들이 실제로 스키마·불변식을 지키는가.

2026-09-07 데이터 계보(lineage) 감사(오너 지시: "계속 돌릴수록 데이터가 무용지물이
되지 않아야 한다")의 일환. 이 파일들은 `data/`(gitignore, 로컬/EC2 전용 상태)에
있어 이 저장소 checkout 마다 존재 여부가 다르다 — **파일이 없으면 스킵**한다
(그 자체가 결함이 아니다: 아직 그 이벤트가 한 번도 안 일어났을 뿐일 수 있다,
예: `capital_decisions.jsonl`은 자본 강등이 한 번도 없었으면 없다).

무엇을 검증하나:
- 모든 줄이 유효한 JSON으로 파싱된다(잘림/손상 없음 — append-only 파일의 최소
  구조적 보증).
- 필수 필드가 있고 타입이 맞다.
- tz-aware ISO 타임스탬프(오프셋 없는 naive 문자열은 이 저장소가 반복해서 다친
  타임존 버그의 재료다 — `quant/control/ledger.py`, `quant/control/warehouse.py`
  둘 다 "naive를 로컬시각으로 오해하지 않는다"는 코멘트를 남겨뒀다).
- 스키마 버전 필드(`schema`) 유무 — 있는 파일은 정수인지만 확인한다(버전이
  올라도 이 테스트는 안 깨진다는 뜻). **없는 파일은 UNVERSIONED로 알려진
  상태다** — 이 테스트가 그 사실을 강제하진 않는다(하드 실패시키면 지금 당장
  스키마를 넣으라는 요구가 되는데, 그건 이 세션의 범위 밖 트레이드오프
  판단이다) — 대신 `test_known_unversioned_files`가 그 목록을 문서화해, 누군가
  조용히 스키마를 넣었는데 이 목록을 안 지우면 stale 테스트로 알려준다.

`trades.jsonl`은 **append-order가 ts-order와 다르다**(실측, 2026-09-07: 최대
611초 역전 — 같은 개장 사이클에서 여러 전략/종목의 체결이 브로커 확인 지연 순으로
쓰이기 때문). 그래서 이 파일은 monotonic-ts를 강제하지 **않는다** — 대신
`round_trips`/`session_cash_delta_*`처럼 실제로 순서에 의존하는 소비자들이 이미
`sorted(..., key=ts)`로 방어하고 있는지만 별도로 확인한다(코드 존재 확인, 이
파일 자체의 책임은 아니지만 회귀 감지용 앵커로 남긴다).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE = REPO_ROOT / "data" / "state"
LEDGER = REPO_ROOT / "data" / "ledger"


def _load_jsonl_strict(path: Path) -> list[dict]:
    """모든 비어있지 않은 줄이 유효한 JSON dict여야 한다 — 하나라도 깨지면
    실패(운영 로더 `ledger.load_trades`와 달리 여기선 깨진 줄을 조용히 넘기지
    않는다: 계약 테스트의 목적은 "손상이 있었는지"를 드러내는 것이다)."""
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for i, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError as e:
            pytest.fail(f"{path.name}:{i + 1} 잘린/손상된 JSON 줄: {e}")
        assert isinstance(row, dict), f"{path.name}:{i + 1} 최상위가 dict가 아님: {type(row)}"
        rows.append(row)
    return rows


def _assert_tz_aware_iso(value: str, *, where: str) -> None:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, f"{where}: naive(오프셋 없는) 타임스탬프 — {value!r}"


def _skip_if_absent(path: Path) -> list[dict]:
    if not path.exists():
        pytest.skip(f"{path} 없음 — 로컬/EC2 전용 상태, 아직 안 만들어졌을 수 있음")
    rows = _load_jsonl_strict(path)
    if not rows:
        pytest.skip(f"{path} 비어 있음")
    return rows


# --- trades.jsonl (data/state) ------------------------------------------------

def test_trades_jsonl_contract():
    rows = _skip_if_absent(STATE / "trades.jsonl")
    required = {"ts", "strategy_id", "symbol", "side", "qty", "price", "fee", "market"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"trades.jsonl:{i} 필수 필드 누락: {missing}"
        assert isinstance(row["symbol"], str) and row["symbol"], f"trades.jsonl:{i} symbol"
        assert row["side"] in ("BUY", "SELL", "buy", "sell"), f"trades.jsonl:{i} side={row['side']!r}"
        assert isinstance(row["qty"], (int, float)), f"trades.jsonl:{i} qty 타입"
        assert isinstance(row["price"], (int, float)), f"trades.jsonl:{i} price 타입"
        assert row["market"] in ("KR", "US"), f"trades.jsonl:{i} market={row['market']!r}"
        _assert_tz_aware_iso(str(row["ts"]), where=f"trades.jsonl:{i} ts")
        # realized_pnl은 None이 허용된다(models.py 경고 — 브로커가 원가를 모를 수
        # 있다) — 있으면 숫자여야 한다.
        if row.get("realized_pnl") is not None:
            assert isinstance(row["realized_pnl"], (int, float)), f"trades.jsonl:{i} realized_pnl"
        # params_fingerprint/schema(2026-09-07, 진화가능성 평가 투자 #1) — 이
        # 시점 이전 행에는 아예 없다(구버전, `test_known_unversioned_files_...`가
        # 더는 다루지 않는다는 뜻이지 필드가 강제된다는 뜻은 아니다). 있으면
        # 타입만 확인한다 — None(지문 맵 미배선)도 유효한 값이다.
        if "params_fingerprint" in row and row["params_fingerprint"] is not None:
            assert isinstance(row["params_fingerprint"], str), (
                f"trades.jsonl:{i} params_fingerprint 타입"
            )
        if "schema" in row:
            assert isinstance(row["schema"], int), f"trades.jsonl:{i} schema 타입"


def test_trades_jsonl_no_duplicate_lines():
    """완전히 동일한 (ts, strategy_id, symbol, side, qty, price) 조합이 중복되면
    accidental double-append 의심 — `on_fill`은 체결당 정확히 한 줄을 쓴다는
    계약이다."""
    rows = _skip_if_absent(STATE / "trades.jsonl")
    seen: dict[tuple, int] = {}
    dupes = []
    for row in rows:
        key = (row.get("ts"), row.get("strategy_id"), row.get("symbol"),
               row.get("side"), row.get("qty"), row.get("price"))
        seen[key] = seen.get(key, 0) + 1
    dupes = [(k, n) for k, n in seen.items() if n > 1]
    assert not dupes, f"trades.jsonl 완전 중복 행 {len(dupes)}종: {dupes[:5]}"


def test_trades_jsonl_consumers_sort_by_ts():
    """`trades.jsonl`은 append-order != ts-order다(실측 611초 역전, 2026-09-07).
    ts에 의존하는 집계는 반드시 자체적으로 재정렬해야 한다 — 이 앵커 테스트는
    그 방어가 코드에서 없어지면(리팩터 중 실수로 `sorted(...)`를 지우면) 이
    테스트가 아니라 `test_ledger.py`/`test_replay_consistency.py`가 순서 의존
    버그로 잡아야 정상이지만, 최소한 소스에 그 패턴이 남아있는지 문서화한다."""
    import inspect

    from quant.control import ledger
    src = inspect.getsource(ledger.round_trips)
    assert "sorted(" in src, "round_trips가 더 이상 ts로 정렬하지 않음 — append-order 의존 회귀"


# --- selections.jsonl ----------------------------------------------------------

def test_selections_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "selections.jsonl")
    required = {"schema", "date", "market", "symbol"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"selections.jsonl:{i} 필수 필드 누락: {missing}"
        assert isinstance(row["schema"], int), f"selections.jsonl:{i} schema 타입"
        assert row["market"] in ("KR", "US"), f"selections.jsonl:{i} market={row['market']!r}"
        # date는 YYYY-MM-DD (naive 날짜 — 시각이 아니므로 tz 요구 없음)
        datetime.strptime(row["date"], "%Y-%m-%d")


# --- report_claims.jsonl --------------------------------------------------------

def test_report_claims_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "report_claims.jsonl")
    required = {"schema", "date", "market", "session", "recorded_at", "generated_at"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"report_claims.jsonl:{i} 필수 필드 누락: {missing}"
        assert isinstance(row["schema"], int), f"report_claims.jsonl:{i} schema 타입"
        assert row["market"] in ("KR", "US"), f"report_claims.jsonl:{i} market={row['market']!r}"
        assert row["session"] in ("open", "close"), f"report_claims.jsonl:{i} session={row['session']!r}"
        _assert_tz_aware_iso(str(row["recorded_at"]), where=f"report_claims.jsonl:{i} recorded_at")
        _assert_tz_aware_iso(str(row["generated_at"]), where=f"report_claims.jsonl:{i} generated_at")


# --- capital_decisions.jsonl ----------------------------------------------------

def test_capital_decisions_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "capital_decisions.jsonl")
    required = {"date", "strategy", "market", "current", "proposed", "applied", "reason"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"capital_decisions.jsonl:{i} 필수 필드 누락: {missing}"
        assert isinstance(row["applied"], bool), f"capital_decisions.jsonl:{i} applied 타입"
        datetime.strptime(row["date"], "%Y-%m-%d")


# --- notify_sent.jsonl / notify_failures.jsonl ----------------------------------

def test_notify_sent_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "notify_sent.jsonl")
    required = {"ts", "source", "lane", "text"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"notify_sent.jsonl:{i} 필수 필드 누락: {missing}"
        _assert_tz_aware_iso(str(row["ts"]), where=f"notify_sent.jsonl:{i} ts")


def test_notify_failures_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "notify_failures.jsonl")
    required = {"ts", "source", "text", "error"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"notify_failures.jsonl:{i} 필수 필드 누락: {missing}"
        _assert_tz_aware_iso(str(row["ts"]), where=f"notify_failures.jsonl:{i} ts")


# --- report_review.jsonl / report_lint.jsonl ------------------------------------

def test_report_review_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "report_review.jsonl")
    required = {"schema", "date", "market", "session", "generated_at"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"report_review.jsonl:{i} 필수 필드 누락: {missing}"
        assert isinstance(row["schema"], int), f"report_review.jsonl:{i} schema 타입"
        assert row["market"] in ("KR", "US"), f"report_review.jsonl:{i} market={row['market']!r}"


def test_report_lint_jsonl_contract():
    rows = _skip_if_absent(LEDGER / "report_lint.jsonl")
    required = {"date", "market", "session", "severity", "section", "message"}
    for i, row in enumerate(rows):
        missing = required - row.keys()
        assert not missing, f"report_lint.jsonl:{i} 필수 필드 누락: {missing}"
        assert row["severity"] in ("warn", "error", "info"), f"report_lint.jsonl:{i} severity={row['severity']!r}"


# --- id 중복 검사(있으면) --------------------------------------------------------

def test_no_files_have_undocumented_id_field():
    """이 목록의 파일들엔 명시적 `id`/`fill_id` 필드가 없다(실측, 2026-09-07) —
    중복 검사를 "id가 있으면"이라는 조건으로 만들어도 지금은 전부 통과한다.
    누군가 미래에 id 필드를 추가하면 이 테스트가 그 사실을 알려주고, 그때
    실제 "no duplicate id" 검증을 추가해야 한다(지금 조용히 아무것도 안 하는
    assert가 되지 않도록 하는 안전핀)."""
    checked = []
    for name, base in (
        ("trades.jsonl", STATE), ("selections.jsonl", LEDGER),
        ("report_claims.jsonl", LEDGER), ("capital_decisions.jsonl", LEDGER),
        ("notify_sent.jsonl", LEDGER), ("notify_failures.jsonl", LEDGER),
        ("report_review.jsonl", LEDGER), ("report_lint.jsonl", LEDGER),
    ):
        path = base / name
        if not path.exists():
            continue
        rows = _load_jsonl_strict(path)
        if not rows:
            continue
        checked.append(name)
        has_id = any(("id" in r or "fill_id" in r) for r in rows)
        assert not has_id, (
            f"{name}에 id/fill_id 필드가 생겼다 — 이 테스트에 실제 "
            "no-duplicate-id 검증을 추가하세요 (현재는 '없음'만 확인)"
        )
    if not checked:
        pytest.skip("검사 대상 파일이 로컬에 하나도 없음")


def test_known_unversioned_files_still_lack_schema_field():
    """`schema` 필드가 없는 것으로 확인된 파일 목록(2026-09-07 감사, 같은 날
    저녁 `trades.jsonl` 스키마 도입으로 이 목록에서 뺐다 — 아래 참고).
    누군가 스키마 필드를 추가했는데 이 목록을 안 지우면 실패해서 알려준다 —
    "버전 추가를 깜빡하고 이 문서만 남은" 상황을 방지.

    `trades.jsonl`은 2026-09-07(진화가능성 평가 투자 #1, `TradeLedgerSink.
    on_fill`)부터 매 체결에 `schema: 2`를 찍는다 — 그 시점 이전 행에는 필드
    자체가 없다(혼재가 정상, `test_trades_jsonl_contract`가 있을 때만 타입을
    확인한다). 그래서 UNVERSIONED 목록에서 뺐다: 이 파일은 이제 "언젠가 버전이
    생기면 알려줘"가 아니라 "이미 버전이 있다"이므로 여기서 다룰 대상이 아니다."""
    known_unversioned = {
        "capital_decisions.jsonl": LEDGER,
        "notify_sent.jsonl": LEDGER,
        "notify_failures.jsonl": LEDGER,
        "report_lint.jsonl": LEDGER,
    }
    checked = []
    for name, base in known_unversioned.items():
        path = base / name
        if not path.exists():
            continue
        rows = _load_jsonl_strict(path)
        if not rows:
            continue
        checked.append(name)
        assert not any("schema" in r for r in rows), (
            f"{name}이 이제 schema 필드를 갖는다 — UNVERSIONED 목록(이 테스트,"
            " 그리고 감사 보고서)에서 지우고 버전 관리 규칙 문서화 필요"
        )
    if not checked:
        pytest.skip("검사 대상 파일이 로컬에 하나도 없음")
