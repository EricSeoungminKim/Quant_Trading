"""텔레그램 HTML parse_mode 서식 헬퍼 — `quant/core`, 외부 의존 0(순수 함수만).

## 왜 여기 있나

소유자 요구(2026-09-04): "텔레그램 메시지가 자연스러운 챗봇처럼 읽혀야 한다."
1단계(L1, 이 파일)는 **결정론적 서식**이다 — 굵게/모노스페이스/구분선 같은
표현만 바꾸고 숫자·판단 로직은 손대지 않는다. 판단을 산문으로 바꾸는 2단계
(L2, `quant/analyze/narrator.py`)는 리포팅 레이어 전용이고 이 파일과 무관하다.

`quant/trade/loop.py`(엔진 핫패스)가 이 모듈을 직접 쓴다 — 그래서 `quant/core`에
둔다: httpx/yaml 등 어댑터 의존이 전혀 없어야 `quant.trade`가 임포트해도
`tests/test_architecture.py`("quant/trade는 HTTP·DB 라이브러리 금지")를 어기지
않는다.

## 텔레그램 Bot API HTML 서브셋

지원 태그: `<b>` `<i>` `<u>` `<s>` `<code>` `<pre>` `<a href="...">`
`<blockquote>` `<blockquote expandable>`. 리터럴로 등장하는 `&`/`<`/`>`는
전부 이스케이프해야 한다 — 안 하면 텔레그램이 태그로 오인해 400 Bad Request를
돌려주고(그러면 알림 자체가 유실된다), 그래서 `esc()`가 이 파일의 유일한
저수준 원시함수이고 나머지 래퍼는 전부 그 위에 얹는다.

## 4096자 상한과 truncate 전략

`compose()`는 절대 태그 중간을 자르지 않는다 — 블록(헤더/섹션/푸터) 단위로만
자른다. 문자열 중간을 자르면 열린 `<pre>`/`<blockquote>` 가 안 닫혀 그 뒤
전체가 깨진 HTML이 되고, 텔레그램은 통째로 400을 돌려준다(부분 표시가 아니라
발송 자체가 실패). `send()` 쪽(어댑터)의 HTML 파싱 실패 시 평문 재시도 폴백은
이 파일의 책임이 아니다 — 그건 전송 계층(`quant/adapters/notify/telegram.py`,
`server/scripts/lib/notify.sh`)의 계약이다.
"""
from __future__ import annotations

from collections.abc import Iterable

MAX_CHARS = 4096

# compose()가 블록을 덜어낼 때 붙이는 생략 안내 — 텔레그램 발송 실패보다
# "일부 생략됐다"고 정직하게 말하는 편이 낫다.
_TRUNCATION_NOTICE = "… (길이 제한으로 생략됨)"


def esc(text: object) -> str:
    """HTML 특수문자(&, <, >) 이스케이프. 순서 중요 — &부터 바꿔야 뒤의
    &lt;/&gt; 자체를 다시 이스케이프하지 않는다."""
    out = str(text)
    return out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def b(text: object) -> str:
    """굵게 — 헤더·핵심 결과에."""
    return f"<b>{esc(text)}</b>"


def i(text: object) -> str:
    """기울임 — 서술(narration) 보조 문구에."""
    return f"<i>{esc(text)}</i>"


def u(text: object) -> str:
    """밑줄 — 아껴 쓴다("오늘 눈여겨볼 것" 한 줄 같은 곳)."""
    return f"<u>{esc(text)}</u>"


def s(text: object) -> str:
    """취소선 — 정정되거나 철회된 항목에."""
    return f"<s>{esc(text)}</s>"


def code(text: object) -> str:
    """인라인 모노스페이스 — 숫자·ID에. 정렬이 필요한 표는 `pre()`를 쓴다."""
    return f"<code>{esc(text)}</code>"


def pre(text: object) -> str:
    """블록 모노스페이스(정렬 표) — 전략별 손익 표, 펄스 지표 표처럼 열이
    맞아야 읽히는 내용에."""
    return f"<pre>{esc(text)}</pre>"


def link(text: object, url: str) -> str:
    """`<a href="...">`. url 자체도 이스케이프한다(따옴표 탈출 방지)."""
    return f'<a href="{esc(url)}">{esc(text)}</a>'


def quote(text: object, expandable: bool = False) -> str:
    """인용 블록. `expandable=True`면 길어도 기본 접힘 — 종목별 상세처럼 긴
    결정론적 목록을 접어 채팅을 짧게 유지하고 싶을 때."""
    open_tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{open_tag}>{esc(text)}</blockquote>"


def section(title: object) -> str:
    """섹션 제목 한 줄 — 굵게."""
    return b(title)


def bullets(items: Iterable[object]) -> str:
    """평문 항목들을 "• " 불릿 목록으로. 각 항목은 이스케이프된 평문이다 —
    항목 안에 이미 만든 HTML을 넣고 싶으면 이 헬퍼 대신 직접 줄을 이어붙인다."""
    return "\n".join(f"• {esc(item)}" for item in items)


def kv(label: object, value: object) -> str:
    """"라벨: 값" 한 줄 — 값은 모노스페이스(`code()`)로 강조한다."""
    return f"{esc(label)}: {code(value)}"


def pct(value: float | None, digits: int = 2) -> str:
    """부호 있는 퍼센트. None이면 "n/a"(0으로 위장하지 않는다)."""
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}%"


def bp(value: float | None, digits: int = 1) -> str:
    """부호 있는 bp. None이면 "n/a"."""
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}bp"


def pnl(value: float | None, currency: str = "KRW") -> str:
    """손익 한 줄 — 부호 이모지(🔺/🔻/➖) + 모노스페이스 금액.

    `currency`: "KRW"(기본, 원 단위 정수) | "USD"(달러, 소수 2자리) |
    그 외 문자열은 그대로 접미사로 붙는다(정수 표기).
    """
    if value is None:
        return f"➖ {code('n/a')}"
    mark = "🔺" if value > 0 else ("🔻" if value < 0 else "➖")
    if currency == "USD":
        body = f"+${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"
    elif currency == "KRW":
        body = f"{value:+,.0f}원"
    else:
        body = f"{value:+,.0f}{currency}"
    return f"{mark} {code(body)}"


def table(headers: Iterable[object], rows: Iterable[Iterable[object]]) -> str:
    """정렬된 모노스페이스 표 — 태그 없는 순수 텍스트다(`pre()`로 감싸 쓴다).
    각 열은 그 열에서 가장 넓은 값(헤더 포함)에 맞춰 공백으로 좌측 정렬한다 —
    market-pulse 지표 표·session-pnl 전략별 표·manual-recs 추천 목록처럼 "열이
    맞아야 읽히는" 데이터에 쓴다. 이스케이프는 `pre()`가 완성된 문자열 전체에
    한 번에 하므로 여기서는 하지 않는다."""
    header_cells = [str(h) for h in headers]
    row_cells = [[str(c) for c in r] for r in rows]
    n_cols = len(header_cells)
    widths = [len(header_cells[i]) for i in range(n_cols)]
    for r in row_cells:
        for i in range(n_cols):
            widths[i] = max(widths[i], len(r[i]))

    def _fmt_row(cells: list[str]) -> str:
        # 마지막 열은 패딩하지 않는다 — 안 그러면 꼬리 공백이 붙는다.
        return "  ".join(
            cells[i] if i == n_cols - 1 else cells[i].ljust(widths[i])
            for i in range(n_cols)
        )

    lines = [_fmt_row(header_cells), "  ".join("-" * w for w in widths)]
    lines += [_fmt_row(r) for r in row_cells]
    return "\n".join(lines)


def compose(header: str, sections: Iterable[str] | None = None, footer: str | None = None) -> str:
    """헤더 + 섹션들 + 푸터를 빈 줄로 이어붙이고 4096자 이내로 맞춘다.

    **블록 단위로만 자른다** — 문자열 중간 절단은 절대 하지 않는다(모듈
    docstring "4096자 상한과 truncate 전략" 참고). 헤더는 항상 남긴다(잘려도
    무엇에 대한 메시지인지는 보여야 한다); 넘치면 뒤쪽 섹션부터, 그다음
    푸터까지 순서대로 덜어내고 생략 안내를 붙인다. 헤더 하나만으로도 넘치는
    극단적인 경우엔 헤더 자체를 안전하게(태그를 안 자르도록 줄 단위로) 줄인다.
    """
    section_list = [str(sec) for sec in (sections or []) if sec]
    blocks = [header] + section_list + ([footer] if footer else [])
    full = "\n\n".join(blocks)
    if len(full) <= MAX_CHARS:
        return full

    # 뒤에서부터 블록을 하나씩 떼어내며 다시 맞춰본다(헤더는 항상 유지).
    kept = list(blocks)
    while len(kept) > 1:
        candidate = "\n\n".join(kept + [_TRUNCATION_NOTICE])
        if len(candidate) <= MAX_CHARS:
            return candidate
        kept.pop()  # 가장 마지막(푸터 → 뒤 섹션 순) 블록부터 제거

    # 헤더 하나만 남았는데도 넘친다 — 줄 단위로 안전하게 줄인다(태그 중간 절단 금지).
    header_only = kept[0]
    budget = MAX_CHARS - len(_TRUNCATION_NOTICE) - 2  # "\n\n" 구분자만큼 여유
    if len(header_only) <= budget:
        return header_only + "\n\n" + _TRUNCATION_NOTICE
    lines = header_only.split("\n")
    out_lines: list[str] = []
    used = 0
    for line in lines:
        added = len(line) + (1 if out_lines else 0)
        if used + added > budget:
            break
        out_lines.append(line)
        used += added
    truncated = "\n".join(out_lines) if out_lines else header_only[:budget]
    return truncated + "\n\n" + _TRUNCATION_NOTICE
