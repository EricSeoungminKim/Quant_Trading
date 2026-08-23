"""테마(업종) 시세 뷰 조립 — "오늘 오르는 섹터 → 소속 종목 → 수혜주" 화면의 데이터 모델
(리포트 고도화 B, 요구 3 / 네이버 패리티 H-1c, Task 3).

**순수 함수다.** 네트워크·파일 I/O 금지 — `sector_map`(A 가 만든 종목↔업종 사전),
`sector_quotes`(Task 1 `fetch_sector_quotes` 결과), `symbols`(리포트가 이미 아는
유니버스 시세), `relations`, `sector_members`(H-1a `fetch_sector_data` 산출,
`data/ledger/sector_members.json`), `themes`(H-1b `fetch_themes` 산출)는 전부
호출부(`report_cli`)가 읽어온 것을 인자로 받는다.
"""
from __future__ import annotations

from quant.analyze.render import relation_items


def _sort_key(change_pct):
    return (change_pct is None, -(change_pct or 0))


def build_sector_view(
    sector_map: dict[str, str],
    sector_quotes: list[dict],
    symbols: dict[str, dict],
    relations: dict[str, list[dict]] | None,
    sector_members: dict[str, list[dict]] | None = None,
) -> list[dict]:
    """업종별로 묶은 뷰 모델. 등락률 내림차순, 미확인은 뒤로.

    `sector_map`이 비면 `[]` — 아티팩트 없이 4,029종목을 지어내지 않는다.

    `sector_members`가 없으면(호출부 하위호환) 기존 동작 그대로 — 우리
    유니버스(`symbols`)에 종목이 하나도 없는 업종은 빠지고, 있는 업종도 그
    교집합 종목만 보인다.

    `sector_members`가 있으면 각 업종의 소속 종목을 **전체**(네이버 업종 상세
    페이지 그대로) 보여준다 — 유니버스 종목은 `in_universe: True` + 관계
    트리로 enrich, 나머지는 이름·등락률만. 표시할 업종은 **유니버스가 보유한
    업종 ∪ 등락률 상위 10 업종**(네이버 홈처럼 "오르는 업종"은 유니버스와
    무관하게 보이되, 79개 전부를 그리진 않는다). 단, 유니버스가 `sector_map`과
    전혀 겹치지 않으면(예: US 리포트에 KR 업종 데이터가 실려 있는 경우) 무관한
    시장 데이터를 새지 않도록 기존처럼 `[]`를 낸다.
    """
    if not sector_map:
        return []

    quote_by_name = {q["name"]: q for q in (sector_quotes or [])}
    symbols = symbols or {}

    # 유니버스 종목이 속한 업종 — 기존 알고리즘과 동일(하위호환 경로에도 쓰인다)
    universe_groups: dict[str, list[dict]] = {}
    for symbol, info in symbols.items():
        sector_name = sector_map.get(symbol)
        if sector_name is None:
            continue
        universe_groups.setdefault(sector_name, []).append({
            "symbol": symbol,
            "name": info.get("name", symbol),
            "change_pct": info.get("change_pct"),
            "in_universe": True,
            "relations": relation_items(symbols, relations, symbol),
        })

    if sector_members:
        if not universe_groups:
            # 이 유니버스는 sector_map 이 아는 시장이 아니다(코드 형식이 안
            # 겹친다) — 상위10 규칙을 적용하면 무관한 업종 데이터가 샌다.
            return []

        ranked = sorted(
            (q for q in (sector_quotes or []) if q.get("change_pct") is not None),
            key=lambda q: -q["change_pct"],
        )
        top10 = {q["name"] for q in ranked[:10]}
        selected = set(universe_groups) | top10

        out = []
        for sector_name in selected:
            members_raw = sector_members.get(sector_name)
            full_membership = bool(members_raw)
            if members_raw:
                members = []
                for m in members_raw:
                    code = m.get("code")
                    in_universe = code in symbols
                    members.append({
                        "symbol": code,
                        "name": m.get("name", code),
                        "change_pct": m.get("change_pct"),
                        "in_universe": in_universe,
                        "relations": relation_items(symbols, relations, code) if in_universe else [],
                    })
            else:
                # 업종 상세 크롤이 이 업종만 실패했을 수 있다 — 유니버스로
                # 아는 만큼이라도 보여준다(섹션 통째 생략보다 낫다). 단 이건
                # 전체 멤버가 아니라 교집합이므로, up/flat/down 요약을 그대로
                # 두면 "상승2·하락1인데 목록엔 1종목" — 사용자가 신고한 원래
                # 모순이 그대로 재발한다. 모르는 걸 아는 척하지 않는다: 이
                # 업종은 요약 배지째로 미확인 처리한다.
                members = universe_groups.get(sector_name, [])
            q = quote_by_name.get(sector_name)
            out.append({
                "name": sector_name,
                "change_pct": q["change_pct"] if q else None,
                "up": (q["up"] if q else None) if full_membership else None,
                "flat": (q["flat"] if q else None) if full_membership else None,
                "down": (q["down"] if q else None) if full_membership else None,
                "symbols": sorted(members, key=lambda m: _sort_key(m["change_pct"])),
            })
        return sorted(out, key=lambda s: _sort_key(s["change_pct"]))

    # 하위호환: sector_members 없으면 유니버스 교집합만(기존 동작)
    out = []
    for sector_name, members in universe_groups.items():
        q = quote_by_name.get(sector_name)
        out.append({
            "name": sector_name,
            "change_pct": q["change_pct"] if q else None,
            "up": q["up"] if q else None,
            "flat": q["flat"] if q else None,
            "down": q["down"] if q else None,
            "symbols": sorted(members, key=lambda m: _sort_key(m["change_pct"])),
        })

    return sorted(out, key=lambda s: _sort_key(s["change_pct"]))


def build_top_movers(
    sector_quotes: list[dict],
    themes: dict[str, dict],
    sector_members: dict[str, list[dict]] | None = None,
) -> dict:
    """네이버 홈 "업종상위/테마상위" 카드 — 등락률 상위 3개 + 대표 종목 2개(등락률순).

    순수 함수. `sector_members`가 없으면(호출부 하위호환) 업종 카드는 이름·
    등락률만 보이고 대표 종목은 빈 목록 — 0 이나 지어낸 종목으로 위장하지
    않는다. 테마는 `change_pct` 키가 없는 테마를 상위 카드에서 제외한다
    (parse_theme_quotes 실패/미수집 — 역시 0 위장 금지). `sectors_all`/
    `themes_all`은 "더보기" 전체 목록(이름·등락률만, 가볍게)이다.
    """
    def _top_n_members(members: list[dict] | None, n: int = 2) -> list[dict]:
        ranked = sorted(
            (m for m in (members or []) if m.get("change_pct") is not None),
            key=lambda m: -m["change_pct"],
        )
        return [
            {"code": m["code"], "name": m["name"], "change_pct": m["change_pct"]}
            for m in ranked[:n]
        ]

    sector_members = sector_members or {}

    ranked_sectors = sorted(
        (q for q in (sector_quotes or []) if q.get("change_pct") is not None),
        key=lambda q: -q["change_pct"],
    )
    sector_cards = [
        {
            "name": s["name"],
            "change_pct": s["change_pct"],
            "symbols": _top_n_members(sector_members.get(s["name"])),
        }
        for s in ranked_sectors[:3]
    ]
    sectors_all = [
        {"name": s["name"], "change_pct": s["change_pct"]} for s in ranked_sectors
    ]

    theme_list = list((themes or {}).values())
    ranked_themes = sorted(
        (t for t in theme_list if t.get("change_pct") is not None),
        key=lambda t: -t["change_pct"],
    )
    theme_cards = [
        {
            "name": t["name"],
            "change_pct": t["change_pct"],
            "symbols": _top_n_members(t.get("symbols")),
        }
        for t in ranked_themes[:3]
    ]
    themes_all = sorted(
        ({"name": t["name"], "change_pct": t.get("change_pct")} for t in theme_list),
        key=lambda t: _sort_key(t["change_pct"]),
    )

    return {
        "sectors": sector_cards,
        "themes": theme_cards,
        "sectors_all": sectors_all,
        "themes_all": themes_all,
    }
