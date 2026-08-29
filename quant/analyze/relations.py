"""수혜주/공급사/경쟁사 관계 — LLM 후보 + 결정론 검증 (스펙 접근 ①).

**순수 모듈이다.** 네트워크·DB·파일을 만지지 않는다 — 전부 인자로 받는다.
analyze→adapters 임포트는 KNOWN_DEBT 부류이고, 여기서 새로 만들지 않는다.

증거 가중치는 초기값이다 — EC2 실코퍼스로 분포를 보고 조정한다(뉴스 방향
규칙을 1,425건에 돌려 조정한 선례). 감으로 확정하지 않는다.
"""
from __future__ import annotations

import re

_KIND_KO = {"수혜주": "beneficiary", "공급사": "supplier", "경쟁사": "competitor"}
MIN_EVIDENCE = 50
MAX_REASON_CHARS = 120

_LINE_RE = re.compile(
    r"^\s*(?P<name>[^|]{2,40}?)\s*\|\s*(?P<kind>수혜주|공급사|경쟁사)\s*\|\s*"
    rf"(?P<reason>.{{5,{MAX_REASON_CHARS}}})\s*$")


def build_extraction_prompt(src_name: str, articles: list[dict], market: str) -> str:
    """형식 계약(`회사명 | 종류 | 이유`, 종류는 수혜주/공급사/경쟁사)은 시장과
    무관하게 동일하다 — `parse_candidates` 는 시장을 모른다. 시장별로 다른 건
    "무엇을 찾으라 하는가"뿐(KR=한국 상장사, US=미국 상장사+영문 회사명)."""
    if market == "US":
        target = "**미국 상장사**를 찾아라. 회사명은 영문 그대로 적어라."
    else:
        target = "**한국 상장사**를 찾아라."
    lines = [
        f"다음은 '{src_name}' 관련 최근 뉴스다. 이 뉴스로 수혜/공급/경쟁 관계에 있는",
        f"{target} 형식: 한 줄에 하나, `회사명 | 종류 | 한줄이유`.",
        "종류는 수혜주/공급사/경쟁사 중 하나. 확실하지 않으면 내지 마라. 최대 5줄.",
        "",
    ]
    for a in articles:
        lines.append(f"제목: {a.get('title', '')}")
        body = (a.get("body") or "").strip()
        if body:
            lines.append(body)
        lines.append("")
    return "\n".join(lines)


def parse_candidates(text: str | None, name_to_code: dict[str, str],
                     src_symbol: str) -> list[dict]:
    """목록 밖·자기 자신·형식 위반은 **버린다** — 0점 오염 방지(shadow 규칙)."""
    if not text:
        return []
    out, seen = [], set()
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        code = name_to_code.get(m["name"].strip())
        if code is None or code == src_symbol or code in seen:
            continue
        seen.add(code)
        # `<`/`>` 제거 — 후속 B(HTML 렌더)가 LLM 이유를 그대로 붙이면 저장형 XSS다.
        reason = m["reason"].strip().replace("<", "").replace(">", "")
        out.append({"src": src_symbol, "dst": code,
                    "kind": _KIND_KO[m["kind"]],
                    "reason": reason})
    return out


def match_codes(table: list[tuple[str, str]], titles: list[str]) -> list[set[str]]:
    """제목마다 매칭되는 코드 집합(긴 이름 우선) — `evidence_score`·소스 집계가
    공유하는 단일 매칭 로직. 순수 함수: 표와 제목만 받는다.

    **긴 이름이 이미 차지한 구간에서는 짧은 이름이 매칭하지 못한다.**
    실측(2026-08-15): '이닉스'(452400)가 'SK하이닉스'의 부분문자열이라, 이
    마스킹 없이 `name in title` 로 세면 'SK하이닉스' 헤드라인을 전부 '이닉스'
    언급으로 잘못 잡는다 — 소스 선정에서 이닉스가 언급 15건으로 1위,
    `evidence_score` 도 그 오염된 코퍼스로 임계를 넘겨 관계 사전에
    `이닉스→SK하이닉스[경쟁사]` 를 넣었다(reason 문장조차 SK하이닉스
    서술이었다 — LLM 이 애초에 SK하이닉스 기사를 읽은 것).

    **선행 경계 검사 — 위 마스킹만으로는 부족하다.** 언론은 정식명 대신 약칭을
    쓴다('SK하이닉스' 대신 '하이닉스'). 이 경우 표에 '하이닉스' 항목이 없으니
    가릴 긴 이름이 없고, 짧은 이름 '이닉스'가 '하이닉스' 속에서 그대로 매칭돼
    버린다. 그래서 매칭 구간 **바로 앞** 글자가 한글 음절이거나 영숫자면(=더 긴
    단어의 꼬리일 뿐이면) 그 매칭을 버린다 (`str.isalnum()` 이 한글 음절도
    True 다). **뒤(다음) 글자는 검사하지 않는다** — 한국어는 조사가 명사에
    바로 붙는다('삼성전자가', '삼성전자의'). 뒤를 막으면 정상적인 단독 언급까지
    전부 마스킹된다.

    호출부가 **한 번 계산해 재사용해야** 한다(같은 표·같은 제목 목록이면 결과가
    같다) — `evidence_score` 를 후보마다 다시 부르면서 매번 다시 매칭하면
    비용도 크고, 두 곳이 각자 매칭하면 또 갈린다.
    """
    ordered = sorted(table, key=lambda nc: -len(nc[0]))
    out: list[set[str]] = []
    for t in titles:
        spans: list[tuple[int, int]] = []
        codes: set[str] = set()
        for name, code in ordered:
            for m in re.finditer(re.escape(name), t):
                s, e = m.span()
                if s > 0 and t[s - 1].isalnum():
                    continue  # 더 긴 단어의 꼬리(예: '하이닉스' 속 '이닉스')
                if any(s < je and e > js for js, je in spans):
                    continue  # 더 긴 이름이 이미 차지한 구간
                spans.append((s, e))
                codes.add(code)
                break  # 이름 하나당 제목 하나에서 1회만 세면 충분
        out.append(codes)
    return out


def evidence_score(dst_code: str, src_code: str, title_matches: list[set[str]]) -> int:
    """결정론 채점 — LLM 말이 아니라 코퍼스가 뒷받침해야 사전에 들어간다.

    `title_matches` 는 `match_codes(table, titles)` 의 결과다 — 이름 부분문자열이
    아니라 마스킹을 거친 코드로 비교해야 오탐(위 `match_codes` docstring)을
    피한다. 호출부가 코퍼스 전체에 대해 한 번만 계산해 넘긴다.

    초기 가중치(실데이터로 조정 예정): 언급 ≥3건 40 / ≥1건 25,
    src·dst 동시출현 ≥1건 +30, 언급 다양성(서로 다른 제목 ≥5건) +30.
    """
    mention_flags = [dst_code in codes for codes in title_matches]
    n = sum(mention_flags)
    score = 40 if n >= 3 else (25 if n >= 1 else 0)
    if any(src_code in codes for codes, hit in zip(title_matches, mention_flags) if hit):
        score += 30
    if n >= 5:
        score += 30
    return score


def merge_relation(existing: dict | None, cand: dict, score: int,
                   today: str) -> dict:
    row = {
        "src": cand["src"], "dst": cand["dst"], "kind": cand["kind"],
        "reason": cand["reason"][:MAX_REASON_CHARS],
        "evidence_score": score,
        "first_seen": existing["first_seen"] if existing else today,
        "last_verified": today,
    }
    # dst_name 은 테마 경로(`_theme_relations`)만 채워 보낸다 — LLM 경로(cand 에
    # 이 키가 없음)는 하위호환으로 그대로 키 자체가 빠진다. relation_items 가
    # 이 값을 이름 해석 1순위로 쓴다(리뷰 결함 c).
    if "dst_name" in cand:
        row["dst_name"] = cand["dst_name"]
    return row
