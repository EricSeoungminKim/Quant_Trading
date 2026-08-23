"""US 뉴스 헤드라인 → GICS 11섹터 → 한국 수혜주 결정론 매핑(서브프로젝트 W part 2,
2026-08-17).

사용자 요구: "US 뉴스는 그대로, KR은 S&P500 섹터→한국 수혜주 매핑으로 연결" —
`quant/collect/sources/telegram_channels.py`의 `usnews` tier 채널(walterbloomberg,
financialjuice) 등이 물어오는 미국 매크로·개별주 헤드라인을 GICS 11섹터 중
하나로 키워드 분류하고, 그 섹터에 연동되는 국내 종목을 붙인다.

**순수 함수다.** 네트워크·LLM·파일 I/O 금지 — 이 모듈이 하는 일은 문자열
키워드 매칭과 정적 딕셔너리 조회뿐이다(`CLAUDE.md` 거래 핫패스 금지 규칙과
무관한 `quant/analyze/`지만, 이 모듈 자체는 결정론을 유지한다 — LLM 판단은
채점 루프를 무의미하게 만든다).

`KR_BENEFICIARIES`는 **정적 큐레이션**이며 실시간 데이터가 아니다 — 분기마다
사람이 종목 구성·시가총액 순위·업황을 다시 보고 검토해야 한다. 여기 실린
종목·이유는 2026-08-17 기준 통상적으로 알려진 업종 대표주 매핑이며, 특정 시점의
투자 추천이 아니다.
"""
from __future__ import annotations

# GICS 11섹터 — 공식 영문 명칭을 키로 쓴다(사전순이 아니라 GICS 표준 배열순).
GICS_SECTORS: tuple[str, ...] = (
    "Energy",
    "Materials",
    "Industrials",
    "Consumer Discretionary",
    "Consumer Staples",
    "Health Care",
    "Financials",
    "Information Technology",
    "Communication Services",
    "Utilities",
    "Real Estate",
)

# 섹터별 키워드 사전(영어 중심 + 대표 티커·기업명, 한국어 키워드도 일부 포함).
# `classify_sector`가 title에 등장하는 키워드 개수로 최다 매칭 섹터를 고른다 —
# 겹치는 키워드(예: "google"은 IT·Communication Services 둘 다에 걸림)가 있어도
# 섹터별 총 히트 수 비교로 결정론적으로 하나만 고른다.
_SECTOR_KEYWORDS: dict[str, list[str]] = {
    "Energy": [
        "oil", "crude", "opec", "brent", "wti", "natural gas", "exxon", "chevron",
        "energy sector", "gasoline", "drilling", "pipeline", "석유", "원유",
        "정유", "천연가스", "유가",
    ],
    "Materials": [
        "gold", "copper", "steel", "mining", "lithium", "chemicals", "metals",
        "aluminum", "silver", "commodity", "금값", "구리", "철강", "리튬", "소재",
        "화학",
    ],
    "Industrials": [
        "boeing", "raytheon", "lockheed", "caterpillar", "aerospace", "defense",
        "airline", "logistics", "industrial", "manufacturing", "railroad",
        "shipping", "방산", "항공", "물류", "산업재", "조선",
    ],
    "Consumer Discretionary": [
        "tesla", "amazon", "nike", "starbucks", "retail", "automaker", "housing",
        "homebuilder", "luxury", "e-commerce", "tsla", "amzn", "auto sales",
        "자동차", "리테일", "소비재", "면세",
    ],
    "Consumer Staples": [
        "walmart", "costco", "procter", "coca-cola", "pepsi", "consumer staples",
        "grocery", "food", "beverage", "tobacco", "생필품", "음료", "식품",
    ],
    "Health Care": [
        "pfizer", "moderna", "fda", "biotech", "pharma", "healthcare", "drug",
        "vaccine", "clinical trial", "hospital", "obesity drug", "제약", "바이오",
        "헬스케어", "임상",
    ],
    "Financials": [
        "jpmorgan", "goldman sachs", "morgan stanley", "wells fargo", "bank",
        "fed ", "federal reserve", "interest rate", "treasury yield", "rate hike",
        "rate cut", "financial institution", "은행", "금리", "국채", "연준",
    ],
    "Information Technology": [
        "nvidia", "apple", "microsoft", "semiconductor", "chip", "ai chip",
        "software", "cloud computing", "nvda", "aapl", "msft", "philadelphia semiconductor",
        "sox index", "반도체", "필라델피아 반도체", "소프트웨어",
    ],
    "Communication Services": [
        "meta", "alphabet", "google", "netflix", "disney", "telecom", "media",
        "streaming", "social media", "search engine", "meta platforms", "통신",
        "미디어", "스트리밍",
    ],
    "Utilities": [
        "utility", "power grid", "electricity", "duke energy", "nextera",
        "utilities sector", "power plant", "grid demand", "전력", "유틸리티",
        "발전",
    ],
    "Real Estate": [
        "reit", "real estate", "housing market", "mortgage rate", "property",
        "commercial real estate", "home prices", "부동산", "리츠", "주택시장",
        "모기지",
    ],
}


def classify_sector(title: str) -> str | None:
    """US 뉴스 헤드라인(영/한) → GICS 11섹터 중 하나, 매칭 없으면 `None`.

    섹터별 키워드 히트 수를 세어 최다 매칭 섹터를 고른다(대소문자 무시). 동률이면
    `GICS_SECTORS` 순서상 먼저 오는 섹터(`_SECTOR_KEYWORDS` 삽입 순서, dict 순회
    순서로 결정론적). 억지 분류를 피하려고 히트가 하나도 없으면 `None`을
    돌려준다 — 아무 섹터에나 욱여넣지 않는다.
    """
    if not title:
        return None
    t = title.lower()

    best_sector: str | None = None
    best_hits = 0
    for sector, keywords in _SECTOR_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw.lower() in t)
        if hits > best_hits:
            best_hits = hits
            best_sector = sector
    return best_sector


# 섹터 → 한국 수혜주. 종목당 {symbol(6자리), name, why(한 줄 근거)}.
# **정적 큐레이션 — 분기마다 사람이 검토한다** (모듈 docstring 참고).
KR_BENEFICIARIES: dict[str, list[dict]] = {
    "Energy": [
        {"symbol": "096770", "name": "SK이노베이션", "why": "정유·석유화학, 국제유가 연동"},
        {"symbol": "010950", "name": "S-Oil", "why": "정유사, 정제마진·유가 수혜"},
        {"symbol": "078930", "name": "GS", "why": "GS칼텍스(정유) 지주사"},
        {"symbol": "037440", "name": "흥구석유", "why": "석유 유통, 유가 테마 동조"},
    ],
    "Materials": [
        {"symbol": "010130", "name": "고려아연", "why": "아연·비철금속 제련, 금속가 연동"},
        {"symbol": "005490", "name": "포스코홀딩스", "why": "철강 대표주"},
        {"symbol": "011170", "name": "롯데케미칼", "why": "화학 대표주, 원자재가 연동"},
        {"symbol": "086520", "name": "에코프로", "why": "이차전지 양극재, 리튬가 연동"},
    ],
    "Industrials": [
        {"symbol": "012450", "name": "한화에어로스페이스", "why": "방산 수출 대표주"},
        {"symbol": "064350", "name": "현대로템", "why": "방산·철도, 미 국방예산 동조"},
        {"symbol": "047810", "name": "한국항공우주", "why": "항공·방산(KAI)"},
        {"symbol": "003490", "name": "대한항공", "why": "항공 화물·여객, 물류 수혜"},
    ],
    "Consumer Discretionary": [
        {"symbol": "005380", "name": "현대차", "why": "미국 자동차 판매·관세 뉴스 연동"},
        {"symbol": "000270", "name": "기아", "why": "미국 자동차 판매 연동"},
        {"symbol": "007700", "name": "F&F", "why": "의류 리테일, 소비 심리 연동"},
        {"symbol": "008770", "name": "호텔신라", "why": "면세·여행 소비 연동"},
    ],
    "Consumer Staples": [
        {"symbol": "097950", "name": "CJ제일제당", "why": "식품 대표주"},
        {"symbol": "271560", "name": "오리온", "why": "식품·제과, 필수소비재"},
        {"symbol": "005300", "name": "롯데칠성", "why": "음료 대표주"},
    ],
    "Health Care": [
        {"symbol": "207940", "name": "삼성바이오로직스", "why": "바이오 CDMO, 미국 제약 밸류체인"},
        {"symbol": "068270", "name": "셀트리온", "why": "바이오시밀러, FDA 승인 뉴스 연동"},
        {"symbol": "000100", "name": "유한양행", "why": "제약, 임상·FDA 뉴스 연동"},
        {"symbol": "326030", "name": "SK바이오팜", "why": "신약, 미국 임상·승인 뉴스 연동"},
    ],
    "Financials": [
        {"symbol": "105560", "name": "KB금융", "why": "금리 뉴스에 민감한 은행 대장주"},
        {"symbol": "055550", "name": "신한지주", "why": "금리 뉴스에 민감한 은행지주"},
        {"symbol": "086790", "name": "하나금융지주", "why": "금리 뉴스에 민감한 은행지주"},
        {"symbol": "006800", "name": "미래에셋증권", "why": "국채금리·증시 뉴스 연동 증권주"},
    ],
    "Information Technology": [
        {"symbol": "005930", "name": "삼성전자", "why": "반도체·AI 밸류체인 대장주"},
        {"symbol": "000660", "name": "SK하이닉스", "why": "HBM, 엔비디아 밸류체인 직접 수혜"},
        {"symbol": "042700", "name": "한미반도체", "why": "HBM 본더 장비, 반도체 뉴스 연동"},
        {"symbol": "058470", "name": "리노공업", "why": "반도체 테스트 부품, 반도체 업황 연동"},
    ],
    "Communication Services": [
        {"symbol": "035420", "name": "NAVER", "why": "인터넷·AI 서비스, 빅테크 뉴스 연동"},
        {"symbol": "035720", "name": "카카오", "why": "인터넷·플랫폼, 빅테크 뉴스 연동"},
        {"symbol": "035760", "name": "CJ ENM", "why": "미디어·콘텐츠, 스트리밍 뉴스 연동"},
        {"symbol": "017670", "name": "SK텔레콤", "why": "통신 대표주"},
    ],
    "Utilities": [
        {"symbol": "015760", "name": "한국전력", "why": "전력 유틸리티 대표주"},
        {"symbol": "036460", "name": "한국가스공사", "why": "천연가스 유통, 유틸리티"},
        {"symbol": "071320", "name": "한국지역난방공사", "why": "지역난방 유틸리티"},
    ],
    "Real Estate": [
        {"symbol": "330590", "name": "롯데리츠", "why": "리츠, 미국 모기지금리 뉴스 연동"},
        {"symbol": "365550", "name": "ESR켄달스퀘어리츠", "why": "물류 리츠"},
        {"symbol": "357120", "name": "코람코라이프인프라리츠", "why": "인프라 리츠"},
        {"symbol": "000720", "name": "현대건설", "why": "주택시장 뉴스 연동 건설주"},
    ],
}


def map_us_news_to_kr(titles: list[str]) -> dict[str, dict]:
    """헤드라인 묶음 → 섹터별 히트 수 + 대표 헤드라인 2개 + 해당 KR 수혜주 목록.

    `classify_sector`로 분류되지 않는(=None) 헤드라인은 무시한다. 히트가 0인
    섹터는 반환 dict에 아예 나타나지 않는다(빈 섹터를 지어내지 않는다).

    반환: `{sector: {"hits": int, "sample_headlines": [헤드라인 최대 2개],
    "kr_beneficiaries": KR_BENEFICIARIES[sector]}}`.
    """
    matched_by_sector: dict[str, list[str]] = {}
    for title in titles:
        sector = classify_sector(title)
        if sector is None:
            continue
        matched_by_sector.setdefault(sector, []).append(title)

    return {
        sector: {
            "hits": len(matched_titles),
            "sample_headlines": matched_titles[:2],
            "kr_beneficiaries": KR_BENEFICIARIES.get(sector, []),
        }
        for sector, matched_titles in matched_by_sector.items()
    }
