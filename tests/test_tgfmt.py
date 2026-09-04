"""quant/core/tgfmt.py — 텔레그램 HTML 서식 헬퍼."""
from __future__ import annotations

import re

from quant.core import tgfmt


def test_esc_escapes_amp_lt_gt_in_order():
    assert tgfmt.esc("A & B") == "A &amp; B"
    assert tgfmt.esc("<script>") == "&lt;script&gt;"
    # &부터 바꾸므로 &lt;를 다시 이스케이프하지 않는다.
    assert tgfmt.esc("&lt;") == "&amp;lt;"


def test_wrappers_escape_and_wrap():
    assert tgfmt.b("A&B") == "<b>A&amp;B</b>"
    assert tgfmt.i("x<y") == "<i>x&lt;y</i>"
    assert tgfmt.u("z") == "<u>z</u>"
    assert tgfmt.s("dead") == "<s>dead</s>"
    assert tgfmt.code("95.00") == "<code>95.00</code>"
    assert tgfmt.pre("a  b\nc  d") == "<pre>a  b\nc  d</pre>"


def test_link_escapes_url_and_text():
    out = tgfmt.link("리포트", 'http://x/?a=1&b="2"')
    assert out.startswith('<a href="')
    assert "&amp;b=" in out
    assert "리포트</a>" in out


def test_quote_expandable():
    assert tgfmt.quote("짧은 문장") == "<blockquote>짧은 문장</blockquote>"
    assert (
        tgfmt.quote("긴 문장", expandable=True)
        == "<blockquote expandable>긴 문장</blockquote>"
    )


def test_section_is_bold():
    assert tgfmt.section("전략별") == "<b>전략별</b>"


def test_bullets_escapes_each_item():
    out = tgfmt.bullets(["A & B", "정상"])
    assert out == "• A &amp; B\n• 정상"


def test_kv_wraps_value_in_code():
    assert tgfmt.kv("체결가", "95.00") == "체결가: <code>95.00</code>"


def test_pct_and_bp_formatting():
    assert tgfmt.pct(1.5) == "+1.50%"
    assert tgfmt.pct(-1.5) == "-1.50%"
    assert tgfmt.pct(None) == "n/a"
    assert tgfmt.bp(12.3) == "+12.3bp"
    assert tgfmt.bp(None) == "n/a"


def test_pnl_krw_and_usd_and_zero_and_none():
    assert tgfmt.pnl(150000) == "🔺 <code>+150,000원</code>"
    assert tgfmt.pnl(-5.5, currency="USD") == "🔻 <code>-$5.50</code>"
    assert tgfmt.pnl(0) == "➖ <code>+0원</code>"
    assert tgfmt.pnl(None) == "➖ <code>n/a</code>"


def _tag_names(text: str) -> list[str]:
    return re.findall(r"</?([a-z]+)[^>]*>", text)


def _tags_balanced(text: str) -> bool:
    stack: list[str] = []
    for m in re.finditer(r"<(/?)([a-z]+)[^>]*>", text):
        closing, name = m.group(1), m.group(2)
        if not closing:
            stack.append(name)
        else:
            if not stack or stack[-1] != name:
                return False
            stack.pop()
    return not stack


def test_table_pads_columns_to_widest_cell():
    out = tgfmt.table(["종목", "현재가"], [["QQQ", "500.00"], ["SPY전체", "1"]])
    lines = out.splitlines()
    # 폭은 East Asian display width 기준(F3) — "SPY전체"는 len()=5지만
    # 화면 폭은 7(한글 2글자가 2칸씩)이라 그쪽이 가장 넓다.
    width0 = max(tgfmt.display_width("종목"), tgfmt.display_width("QQQ"), tgfmt.display_width("SPY전체"))
    assert lines[0] == "종목" + " " * (width0 - tgfmt.display_width("종목")) + "  " + "현재가"
    assert lines[2] == "QQQ" + " " * (width0 - tgfmt.display_width("QQQ")) + "  " + "500.00"
    # 마지막 열은 패딩하지 않는다 — 꼬리 공백 없음.
    assert lines[3] == "SPY전체" + "  " + "1"
    assert not lines[3].endswith(" ")


def test_table_pads_by_east_asian_display_width_not_codepoint_count():
    """"KODEX코스닥150" 처럼 한글이 섞인 셀은 len()보다 화면 폭이 넓다 — 실제
    <pre> 모노스페이스 렌더에서는 표시 폭 기준으로 패딩해야 옆 ASCII 열이
    밀리지 않는다(2026-09-04 실측)."""
    out = tgfmt.table(["종목", "현재가"], [["KODEX코스닥150", "50,000"], ["QQQ", "500.00"]])
    lines = out.splitlines()
    width0 = max(
        tgfmt.display_width("종목"),
        tgfmt.display_width("KODEX코스닥150"),
        tgfmt.display_width("QQQ"),
    )
    assert lines[0] == "종목" + " " * (width0 - tgfmt.display_width("종목")) + "  " + "현재가"
    assert lines[2] == "KODEX코스닥150" + "  " + "50,000"
    assert lines[3] == "QQQ" + " " * (width0 - tgfmt.display_width("QQQ")) + "  " + "500.00"


def test_table_returns_untagged_text_wrap_with_pre():
    out = tgfmt.pre(tgfmt.table(["a"], [["<b>x</b>"]]))
    assert out.startswith("<pre>") and out.endswith("</pre>")
    assert "&lt;b&gt;x&lt;/b&gt;" in out


def test_compose_joins_header_sections_footer():
    msg = tgfmt.compose(tgfmt.b("헤더"), [tgfmt.section("섹션1") + "\n본문", "섹션2"], footer="푸터")
    assert msg.startswith("<b>헤더</b>")
    assert "섹션1" in msg and "섹션2" in msg and "푸터" in msg
    assert len(msg) <= tgfmt.MAX_CHARS


def test_compose_under_limit_returns_as_is():
    msg = tgfmt.compose("헤더", ["짧은 섹션"])
    assert msg == "헤더\n\n짧은 섹션"


def test_compose_truncates_by_dropping_trailing_blocks_never_mid_tag():
    header = tgfmt.b("헤더")
    # 각 섹션 자체는 균형 잡힌 태그를 가진 완결 블록 — compose는 블록 단위로만 잘라야 한다.
    big_sections = [tgfmt.pre("x" * 500) for _ in range(20)]
    msg = tgfmt.compose(header, big_sections, footer=tgfmt.b("푸터"))
    assert len(msg) <= tgfmt.MAX_CHARS
    assert msg.startswith(header)
    assert _tags_balanced(msg)
    assert "길이 제한으로 생략됨" in msg


def test_compose_extreme_header_alone_over_limit_truncates_safely():
    huge_header = "\n".join(f"줄 {i} " + "x" * 100 for i in range(200))
    msg = tgfmt.compose(huge_header)
    assert len(msg) <= tgfmt.MAX_CHARS
    assert "길이 제한으로 생략됨" in msg
