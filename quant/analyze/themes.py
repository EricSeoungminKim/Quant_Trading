"""뉴스 테마 집계 — 오늘 시장이 무엇을 중심으로 도는지 빈도로 센다.

**서사형 요약이 아니다.** "연준이 매파적이라 기술주가 눌렸다" 같은 문장은 기사
본문을 읽고 인과를 세워야 나오고, 그건 Phase 2 에서 LLM 이 할 일이다. 여기서는
**세어지는 것만 센다** — 어떤 주제의 헤드라인이 몇 건인지, 그 안에 어떤 제목이
있는지. 근거 없는 인과를 템플릿으로 흉내내면 그게 곧 짜깁기다.

이 집계가 Phase 2 의 입력이 된다: LLM 은 97건을 통째로 읽는 대신 테마별로
묶인 것을 보고 해석한다.
"""
from __future__ import annotations

import re

# 테마명 → 매칭 키워드. 소문자로 비교하며, 단어 경계로만 잡는다
# ("AI" 가 "said"·"chain" 안에서 잡히면 집계가 통째로 무의미해진다).
THEMES: dict[str, dict[str, tuple[str, ...]]] = {
    "US": {
        "연준·금리": ("fed", "fomc", "powell", "rate cut", "rate hike", "hawkish",
                    "dovish", "yield", "treasury", "central bank", "basis points"),
        "물가": ("inflation", "cpi", "ppi", "pce", "deflation", "consumer price",
               "producer price"),
        "AI·반도체": ("ai", "chip", "chips", "semiconductor", "nvidia", "gpu",
                   "data center", "openai", "artificial intelligence"),
        "실적": ("earnings", "guidance", "beat estimates", "misses", "revenue",
               "quarterly results", "profit"),
        "무역·관세": ("tariff", "tariffs", "trade war", "export controls",
                  "sanctions", "trade deal"),
        "에너지·원자재": ("oil", "crude", "opec", "natural gas", "gasoline",
                    "gold", "copper"),
        "지정학": ("war", "conflict", "iran", "russia", "ukraine", "strait",
                "military", "geopolitical"),
        "고용": ("jobs", "payroll", "payrolls", "unemployment", "labor market",
               "jobless", "hiring", "layoffs"),
        "암호화폐": ("bitcoin", "crypto", "ethereum", "stablecoin"),
        "주택·소비": ("housing", "mortgage", "retail sales", "consumer spending",
                  "home sales"),
    },
    "KR": {
        "반도체": ("반도체", "삼성전자", "sk하이닉스", "메모리", "hbm", "파운드리"),
        "환율": ("환율", "원달러", "달러", "원화", "외환"),
        "금리·통화": ("금리", "한국은행", "금통위", "국고채", "채권"),
        "수급": ("외국인", "기관", "순매수", "순매도", "수급"),
        "실적": ("실적", "영업이익", "매출", "어닝", "컨센서스"),
        "정책·규제": ("정부", "규제", "금융위", "금감원", "과세", "정책"),
        "가계·신용": ("가계대출", "신용융자", "연체", "부채", "대출"),
        "AI": ("ai", "인공지능", "데이터센터"),
    },
}

MIN_COUNT = 2  # 1건짜리 테마는 흐름이 아니라 잡음이다


def _pattern(keyword: str) -> re.Pattern:
    """단어 경계 매칭. 한글은 \\b 가 동작하지 않아 그대로 포함 검사한다."""
    if re.search(r"[가-힣]", keyword):
        return re.compile(re.escape(keyword))
    return re.compile(rf"\b{re.escape(keyword)}\b")


_COMPILED: dict[str, dict[str, list[re.Pattern]]] = {
    market: {theme: [_pattern(k) for k in kws] for theme, kws in themes.items()}
    for market, themes in THEMES.items()
}


def cluster(titles: list[str], market: str, min_count: int = MIN_COUNT) -> list[dict]:
    """헤드라인을 테마별로 묶어 건수 순으로 돌려준다.

    한 제목이 여러 테마에 걸릴 수 있다 — "Fed's rate decision hits chip stocks"
    는 연준·금리이자 AI·반도체다. 배타적으로 나누면 오히려 왜곡된다.
    """
    compiled = _COMPILED.get(market, {})
    buckets: dict[str, list[str]] = {t: [] for t in compiled}
    for title in titles:
        low = title.lower()
        for theme, patterns in compiled.items():
            if any(p.search(low) for p in patterns):
                buckets[theme].append(title)

    total = len(titles) or 1
    out = [
        {
            "theme": theme,
            "count": len(items),
            "share_pct": round(len(items) / total * 100, 1),
            "samples": items[:3],
        }
        for theme, items in buckets.items()
        if len(items) >= min_count
    ]
    return sorted(out, key=lambda x: (-x["count"], x["theme"]))


def summarize(clusters: list[dict], total: int, market: str) -> str:
    """한 줄 흐름 요약. 세어진 것만 말한다.

    '무엇이 이야기되는가'까지가 이 함수의 한계다. '왜 그런가'는 본문을 읽어야
    나오고 그건 다음 단계다.
    """
    label = {"US": "미국장", "KR": "한국장"}.get(market, market)
    if not clusters:
        return f"{label} 뉴스 {total}건에서 두드러진 테마가 없다."
    top = clusters[:3]
    parts = ", ".join(f"{c['theme']}({c['count']}건)" for c in top)
    return f"{label} 뉴스 {total}건은 {parts} 중심으로 돈다."
