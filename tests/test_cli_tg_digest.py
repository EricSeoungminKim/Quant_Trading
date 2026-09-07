

# ── 2026-09-07: LLM 총 시간 예산 — 예산이 지나면 부르지 않는다 ─────────────────────────
def test_budgeted_call_skips_after_deadline(capsys):
    import time

    from quant.apps.cli import _budgeted_call

    calls = []
    fn = _budgeted_call(lambda p: calls.append(p) or "ok", time.monotonic() + 100, what="digest")
    assert fn("a") == "ok" and calls == ["a"]
    late = _budgeted_call(lambda p: calls.append(p) or "ok", time.monotonic() - 1, what="stance")
    assert late("b") is None and calls == ["a"]
    assert "예산 초과" in capsys.readouterr().err
    assert _budgeted_call(None, time.monotonic() + 10, what="x") is None
