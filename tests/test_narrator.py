"""quant/analyze/narrator.py — L2 서술: 프롬프트 조립 + 숫자 검증(순수 함수)."""
from __future__ import annotations

from quant.analyze import narrator


def test_narrate_returns_none_without_injected_call():
    """call을 안 주입하면(게이트 꺼짐) 절대 시도하지 않는다."""
    assert narrator.narrate("session_pnl", {"net_realized": "+15,918원"}) is None


def test_narrate_returns_text_when_all_numbers_verbatim():
    facts = {"net_realized": "+15,918원", "n_fills": 4}
    called_with = {}

    def _call(prompt: str) -> str:
        called_with["prompt"] = prompt
        return "오늘 순손익은 +15,918원이고 체결은 4건이었습니다. 오늘 눈여겨볼 것: 없음."

    text = narrator.narrate("session_pnl", facts, call=_call)
    assert text == "오늘 순손익은 +15,918원이고 체결은 4건이었습니다. 오늘 눈여겨볼 것: 없음."
    assert "session_pnl" in called_with["prompt"]
    assert "net_realized: +15,918원" in called_with["prompt"]


def test_narrate_discards_invented_number():
    facts = {"net_realized": "+15,918원"}

    def _call(prompt: str) -> str:
        return "오늘 순손익은 +99,999원이었습니다."  # facts에 없는 숫자

    assert narrator.narrate("session_pnl", facts, call=_call) is None


def test_narrate_discards_when_sign_flipped():
    """부호를 뒤집는 것이 가장 위험한 환각 — 별개 숫자로 취급해 폐기한다."""
    facts = {"net_realized": "+15,918원"}

    def _call(prompt: str) -> str:
        return "오늘 순손익은 -15,918원이었습니다."

    assert narrator.narrate("session_pnl", facts, call=_call) is None


def test_narrate_allows_reformatted_but_equal_numbers():
    """1,234.50과 1234.5는 같은 숫자 — 콤마·꼬리 0 차이로 폐기하지 않는다."""
    facts = {"day_pnl_pct": 0.50}

    def _call(prompt: str) -> str:
        return "오늘 손익률은 +0.5%였습니다."

    assert narrator.narrate("session_pnl", facts, call=_call) is not None


def test_narrate_ignores_dashes_in_dates_as_false_negative_numbers():
    """"2026-09-04" 같은 날짜의 '-'는 부호가 아니다 — 잘못 음수로 해석해
    폐기하면 안 된다."""
    facts = {"date": "2026-09-04", "n_fills": 3}

    def _call(prompt: str) -> str:
        return "2026년 9월 4일 세션에서 체결은 3건이었습니다."

    assert narrator.narrate("session_pnl", facts, call=_call) is not None


def test_narrate_returns_none_when_call_raises():
    def _call(prompt: str) -> str:
        raise RuntimeError("timeout")

    assert narrator.narrate("session_pnl", {"x": 1}, call=_call) is None


def test_narrate_returns_none_when_call_returns_empty():
    assert narrator.narrate("session_pnl", {"x": 1}, call=lambda p: "") is None
    assert narrator.narrate("session_pnl", {"x": 1}, call=lambda p: None) is None
    assert narrator.narrate("session_pnl", {"x": 1}, call=lambda p: "   ") is None


def test_build_prompt_includes_kind_and_flattened_facts():
    prompt = narrator.build_prompt("market_pulse", {"spy": {"state": "중립", "rsi": 55}})
    assert "market_pulse" in prompt
    assert "spy.state: 중립" in prompt
    assert "spy.rsi: 55" in prompt


def test_build_prompt_omits_none_and_empty_facts_entirely():
    """실측(2026-09-04, market-pulse 실호출): facts에 None을 "없음"으로 바꿔
    프롬프트에 넣었더니 모델이 그 문구를 그대로 따라 써 "금리 10년물 라벨은
    없음"처럼 있지도 않은 사실을 언급했다. 값이 없으면 애초에 그 항목 자체를
    프롬프트에서 빼야 한다 — "없음" 치환도, "None" 리터럴도 등장하면 안 된다."""
    prompt = narrator.build_prompt(
        "session_pnl", {"cash_delta_krw": None, "note": "", "n_fills": 3},
    )
    assert "cash_delta_krw" not in prompt
    assert "note" not in prompt
    assert "없음" not in prompt
    assert "None" not in prompt
    assert "n_fills: 3" in prompt


def test_build_prompt_handles_empty_facts():
    prompt = narrator.build_prompt("daily_feedback", {})
    assert "(없음)" in prompt


def test_verify_numbers_true_for_subset_and_false_for_extra():
    facts = {"a": 100, "b": "50%"}
    assert narrator.verify_numbers("100과 50이 있다", facts) is True
    assert narrator.verify_numbers("100과 999가 있다", facts) is False


def test_verify_numbers_true_when_no_numbers_in_text():
    assert narrator.verify_numbers("숫자가 전혀 없는 문장입니다", {"a": 1}) is True


# --------------------------------------------------------------------------- 서술 문단 예산(F1, 2026-09-04)


def test_narrate_truncates_overlong_text_at_last_sentence_not_mid_word():
    """실측(2026-09-04, market-pulse): 응답이 예산을 넘기면 "...스프레"처럼
    단어 중간에서 잘렸다. 마지막 문장 종결부호 뒤에서만 자르고, 그 뒤 조각은
    통째로 버려야 한다."""
    facts = {"x": 1}
    long_sentence = "그리고 " * 200 + "스프레드가 있습니다."  # 종결부호는 맨 끝에만

    def _call(prompt: str) -> str:
        return f"문장 하나입니다. {long_sentence}"

    text = narrator.narrate("market_pulse", facts, call=_call)
    assert text is not None
    assert len(text) <= narrator.NARRATION_MAX_CHARS
    assert text.endswith(("다.", "!", "?", "。"))
    assert "스프레" not in text  # 잘린 문장은 아예 버려졌다 — 단어 조각이 안 남는다


def test_narrate_leaves_short_text_untouched():
    facts = {"n_fills": 3}

    def _call(prompt: str) -> str:
        return "체결은 3건이었습니다. 오늘 눈여겨볼 것: 없음."

    text = narrator.narrate("session_pnl", facts, call=_call)
    assert text == "체결은 3건이었습니다. 오늘 눈여겨볼 것: 없음."


def test_sentence_safe_truncate_falls_back_to_hard_cut_without_terminator():
    """종결부호가 아예 없으면(극단적 사고 유출 등) 안전망으로 limit에서 자른다."""
    text = "가" * 50
    assert narrator._sentence_safe_truncate(text, 10) == "가" * 10
