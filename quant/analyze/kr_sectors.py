"""KR 업종(Naver 업종분류) → GICS-11 섹터 매핑 + 전일 US 섹터 신호 (2026-09-06).

## 왜 만드나

`quant-backtest`(별도 연구 저장소) `results/sector_link/SUMMARY.md` S1이 과거
데이터로 검정한 결과: 전날 미국 섹터 ETF 종가→종가 수익률의 그날 횡단면 순위가
다음 KR 거래일 같은 섹터의 종가→종가 초과수익률(vs KODEX200) 순위를 예측한다
— Rank IC(스피어만) mean=0.070, t=4.31, n=488거래일, 2024/2025/2026 3개 연도
전부 양수. 단 S3(트레이더블 버전, walk-forward 비용 포함)는 다섯 변형 전부
NO_GO(KR 개별주 왕복 28bp가 엣지를 삼킨다, DSR 전부 0.5 미달) — **실행 신호가
아니라 리포트의 참고 신호**로만 쓴다는 게 이 모듈의 전제다.

## 매핑은 지어낸 것이 아니다

`UPJONG_TO_GICS`는 `quant-backtest/data/universe/kr_sectors.json`
["upjong_to_gics"]에서 그대로 복사했다(출처: 그 파일의 "source" 필드 — Naver
Finance 업종분류 index(https://finance.naver.com/sise/sise_group.naver?type=upjong)
+ 종목별 '동종업종비교' 블록, 2026-09-06 fetched, 79개 Naver 업종 → GICS-11
수작업 매핑). `quant/analyze/us_sector_map.py`의 `KR_BENEFICIARIES` 11개 키와
같은 taxonomy로 맞춰져 있다(교차검증 가능). `quant/analyze/us_kr_bridge.py`의
`US_TO_KR_SECTORS`(같은 목적, 반대 방향 — GICS 한글라벨→업종 목록)와도 77/79
업종이 일치(2026-09-06 확인, 차이는 "문구류" 하나뿐 — 억지로 맞추지 않고 그대로
둔다). "기타"는 GICS 매핑이 없다(None) — 억지로 채우지 않는다.

순수 함수만 — 네트워크/파일 I/O 없음.
"""
from __future__ import annotations

# Naver 업종(quant.analyze.sector_daily의 `sector` 필드, data/ledger/
# sector_members.json 키) → GICS-11 영문 섹터명. 매핑 없음은 None(모듈
# docstring "기타" 참고).
UPJONG_TO_GICS: dict[str, str | None] = {
    "석유와가스": "Energy",
    "에너지장비및서비스": "Energy",
    "화학": "Materials",
    "철강": "Materials",
    "비철금속": "Materials",
    "종이와목재": "Materials",
    "포장재": "Materials",
    "건축자재": "Materials",
    "우주항공과국방": "Industrials",
    "건축제품": "Industrials",
    "건설": "Industrials",
    "전기장비": "Industrials",
    "전기제품": "Industrials",
    "복합기업": "Industrials",
    "기계": "Industrials",
    "무역회사와판매업체": "Industrials",
    "상업서비스와공급품": "Industrials",
    "항공화물운송과물류": "Industrials",
    "항공사": "Industrials",
    "해운사": "Industrials",
    "도로와철도운송": "Industrials",
    "운송인프라": "Industrials",
    "조선": "Industrials",
    "문구류": "Industrials",
    "자동차": "Consumer Discretionary",
    "자동차부품": "Consumer Discretionary",
    "가정용기기와용품": "Consumer Discretionary",
    "레저용장비와제품": "Consumer Discretionary",
    "섬유,의류,신발,호화품": "Consumer Discretionary",
    "호텔,레스토랑,레저": "Consumer Discretionary",
    "다각화된소비자서비스": "Consumer Discretionary",
    "전문소매": "Consumer Discretionary",
    "백화점과일반상점": "Consumer Discretionary",
    "판매업체": "Consumer Discretionary",
    "가구": "Consumer Discretionary",
    "교육서비스": "Consumer Discretionary",
    "인터넷과카탈로그소매": "Consumer Discretionary",
    "식품과기본식료품소매": "Consumer Staples",
    "음료": "Consumer Staples",
    "식품": "Consumer Staples",
    "담배": "Consumer Staples",
    "가정용품": "Consumer Staples",
    "화장품": "Consumer Staples",
    "건강관리장비와용품": "Health Care",
    "건강관리업체및서비스": "Health Care",
    "건강관리기술": "Health Care",
    "생물공학": "Health Care",
    "제약": "Health Care",
    "생명과학도구및서비스": "Health Care",
    "은행": "Financials",
    "증권": "Financials",
    "기타금융": "Financials",
    "카드": "Financials",
    "생명보험": "Financials",
    "손해보험": "Financials",
    "창업투자": "Financials",
    "반도체와반도체장비": "Information Technology",
    "컴퓨터와주변기기": "Information Technology",
    "전자장비와기기": "Information Technology",
    "통신장비": "Information Technology",
    "핸드셋": "Information Technology",
    "소프트웨어": "Information Technology",
    "IT서비스": "Information Technology",
    "사무용전자제품": "Information Technology",
    "디스플레이패널": "Information Technology",
    "디스플레이장비및부품": "Information Technology",
    "전자제품": "Information Technology",
    "다각화된통신서비스": "Communication Services",
    "무선통신서비스": "Communication Services",
    "방송과엔터테인먼트": "Communication Services",
    "양방향미디어와서비스": "Communication Services",
    "게임엔터테인먼트": "Communication Services",
    "광고": "Communication Services",
    "출판": "Communication Services",
    "전기유틸리티": "Utilities",
    "가스유틸리티": "Utilities",
    "복합유틸리티": "Utilities",
    "부동산": "Real Estate",
    "기타": None,
}

# GICS-11 영문 섹터명 → 회사 리포트 표기 한글 라벨. quant.collect.sources.
# technical.SECTORS(티커→한글)/_GICS_TO_KR 와 같은 라벨셋이다 — analyze 평면은
# collect 를 임포트하지 않는 기존 관례(us_kr_bridge.py/us_sector_map.py 모두
# 같은 이유로 이 11개 라벨을 각자 들고 있다)를 따라 여기서도 독립 사본을 둔다.
GICS_TO_KR_LABEL: dict[str, str] = {
    "Energy": "에너지",
    "Materials": "소재",
    "Industrials": "산업재",
    "Consumer Discretionary": "임의소비재",
    "Consumer Staples": "필수소비재",
    "Health Care": "헬스케어",
    "Financials": "금융",
    "Information Technology": "기술",
    "Communication Services": "커뮤니케이션",
    "Utilities": "유틸리티",
    "Real Estate": "부동산",
}

# US 섹터 ETF 티커(quant.collect.sources.technical.SECTORS 와 같은 11종) →
# GICS-11 영문 섹터명. 리포트가 매일 아침 이미 읽는 `sectors` 소스의 원본
# 티커를 GICS 영문으로 다시 붙이는 표 — 새 데이터 소스가 아니라 기존 값의
# 재라벨링이다.
US_SECTOR_ETF_TO_GICS: dict[str, str] = {
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLF": "Financials",
    "XLK": "Information Technology",
    "XLV": "Health Care",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLC": "Communication Services",
    "XLRE": "Real Estate",
}


def gics_for_upjong(upjong: str) -> str | None:
    """Naver 업종명 → GICS-11 영문 섹터명. 매핑이 없으면 None(있는 걸 지어내지
    않는다) — `UPJONG_TO_GICS`에 아예 없는 업종명도 마찬가지로 None."""
    return UPJONG_TO_GICS.get(upjong)


def us_sector_signal(us_sector_returns: dict[str, float]) -> dict[str, float]:
    """전일 US 섹터 ETF 종가→종가 수익률(GICS-11 영문 키, fraction 단위 —
    예: 0.012 = +1.2%) → 검증된 신호값.

    입력은 호출부(quant.report.collect.sector)가 이미 조립한 것을 받는다 —
    리포트가 매일 아침 이미 읽는 `sectors` 소스(quant.collect.sources.
    technical.fetch_sectors, S&P 섹터 ETF 11종)에서 변환한다(새 네트워크 호출
    없음). 이 함수 자체는 `quant.analyze.us_sector_map.KR_BENEFICIARIES`의
    11개 키 밖의 값·None 값을 걸러내는 검증 게이트다 — 오타·미지원 섹터가
    조용히 섞이는 걸 막는다(단일 진입점, 나중에 신호 정의를 바꿔도 여기만
    고치면 된다).

    근거는 모듈 docstring 의 S1 결과(Rank IC mean=0.070, t=4.31, n=488) —
    close→close 호라이즌이 open→close 보다 뚜렷이 강해(t=1.405) 이 값을 쓴다.
    S3(트레이더블 버전)는 NO_GO 이므로 이 신호를 실행 결정에 쓰지 않는다
    (quant.analyze.sector_daily.US_LINK_WEIGHT 기본값 0 참고).
    """
    from quant.analyze.us_sector_map import KR_BENEFICIARIES

    known = KR_BENEFICIARIES.keys()
    return {
        sector: ret for sector, ret in (us_sector_returns or {}).items()
        if sector in known and ret is not None
    }


def us_sector_returns_from_source(sectors: list[dict] | None) -> dict[str, float]:
    """리포트의 `sectors` 소스(quant.collect.sources.technical.fetch_sectors
    결과, `[{"ticker","name","change_pct"(퍼센트포인트)}, ...]`, S&P 섹터 ETF
    11종)를 GICS-11 영문 키·fraction 수익률로 변환하고 `us_sector_signal`로
    검증한다.

    이 함수가 "새 US 가격 수집"이 아니라는 게 핵심이다 — `sectors` 는
    `quant.analyze.us_kr_bridge.build_us_kr_bridge`에도 넘겨지는 바로 그
    스냅샷 소스 데이터(report_cli.py가 아침 리포트마다 이미 fetch)를
    그대로 재사용한다.
    """
    raw: dict[str, float] = {}
    for s in sectors or []:
        if not isinstance(s, dict):
            continue
        gics = US_SECTOR_ETF_TO_GICS.get(s.get("ticker"))
        pct = s.get("change_pct")
        if gics is None or pct is None:
            continue
        raw[gics] = pct / 100.0
    return us_sector_signal(raw)
