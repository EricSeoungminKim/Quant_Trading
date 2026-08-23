"""어젯밤 미국장 → 오늘 한국장 브리지 (2026-08-21 소유자 지시).

> "전날에 미국장의 특정 섹터 혹은 미국장 자체가 지수적으로 오르면, 한국장도
> 다음날에 오르기 마련이거든. (...) 어제 미국장에서 올랐던 섹터가 한국장에서도
> 오를 확률이 높기에, 그 특정 섹터들 종목을 가져오는걸 추가 혹은 개선해도
> 좋을것같아."

시각 구조(소유자 확인): 미국 정규장은 KST 새벽(서머타임 05:00)에 끝나고, 같은 날
아침 09:00 한국장이 열린다. KR 아침 리포트는 07:30(빌드 리드) 시점에 만들어지므로
**방금 끝난 미국 세션**의 섹터 등락이 이미 스냅샷(`sectors` 소스, S&P 섹터 ETF
11종)에 들어 있다 — 새 수집기가 필요 없다.

## 매핑은 지어낸 것이 아니다

네이버 업종 79개(`sector_members.json`의 키)는 GICS 산업(industry) 분류의
한국어판이다 — "반도체와반도체장비"(Semiconductors & Semiconductor Equipment),
"생물공학"(Biotechnology), "다각화된통신서비스"(Diversified Telecommunication
Services). 미국 섹터 ETF 11종(XLK...)은 같은 GICS의 **섹터(상위)** 레벨이므로,
아래 표는 GICS 섹터→산업 공식 소속 관계를 그대로 옮긴 것이다. 분류가 애매한
것("기타")은 어디에도 넣지 않는다 — 억지로 채우는 것보다 빠뜨리는 쪽이 정직하다.

## 이것은 표시·근거 계층이다 — 자동매수 소스가 아니다

여기서 고른 KR 종목은 리포트 카드와 engine.json에만 실린다. AUTO_WATCH 토큰
(자동편입 → 자동매수)에는 넣지 않는다 — 백로그 §6 "자동매수 소스 추가 = 사용자
결정" 그대로다. 예측 주장도 하지 않는다: 지수 방향 요약은 "상승 N개 / 하락 M개"
집계이지 "오른다"가 아니다.

순수 함수만 — 네트워크/파일 I/O 없음(수집·적재는 호출부 report_cli 소관).
"""
from __future__ import annotations

# US 섹터 ETF(수집기 technical.SECTORS의 한국어 라벨) → 네이버 업종명 목록.
# GICS 섹터→산업 소속 관계를 그대로 옮긴 표다(모듈 docstring). 네이버 업종명은
# data/ledger/sector_members.json의 실제 키(2026-08-21 실측 79개)와 문자까지
# 일치해야 한다 — 오타는 조용히 빈 매핑이 된다(테스트가 교집합을 검증한다).
US_TO_KR_SECTORS: dict[str, list[str]] = {
    "기술": [
        "IT서비스", "소프트웨어", "반도체와반도체장비", "전자장비와기기",
        "컴퓨터와주변기기", "통신장비", "핸드셋", "디스플레이장비및부품",
        "디스플레이패널", "사무용전자제품", "전자제품",
    ],
    "헬스케어": [
        "제약", "생물공학", "생명과학도구및서비스", "건강관리장비와용품",
        "건강관리업체및서비스", "건강관리기술",
    ],
    "금융": ["은행", "증권", "손해보험", "생명보험", "카드", "기타금융", "창업투자"],
    "에너지": ["석유와가스", "에너지장비및서비스"],
    "산업재": [
        "기계", "건설", "조선", "우주항공과국방", "전기장비", "전기제품",
        "도로와철도운송", "항공사", "항공화물운송과물류", "해운사", "운송인프라",
        "무역회사와판매업체", "상업서비스와공급품", "복합기업", "건축제품",
    ],
    "소재": ["화학", "철강", "비철금속", "종이와목재", "포장재", "건축자재"],
    "임의소비재": [
        "자동차", "자동차부품", "호텔,레스토랑,레저", "전문소매",
        "백화점과일반상점", "인터넷과카탈로그소매", "섬유,의류,신발,호화품",
        "레저용장비와제품", "가구", "가정용기기와용품", "교육서비스",
        "다각화된소비자서비스", "판매업체",
    ],
    "필수소비재": ["식품", "음료", "담배", "식품과기본식료품소매", "가정용품", "화장품"],
    "커뮤니케이션": [
        "무선통신서비스", "다각화된통신서비스", "게임엔터테인먼트",
        "방송과엔터테인먼트", "양방향미디어와서비스", "광고", "출판",
    ],
    "유틸리티": ["전기유틸리티", "가스유틸리티", "복합유틸리티"],
    "부동산": ["부동산"],
}


def build_us_kr_bridge(
    us_sectors: list[dict] | None,
    sector_members: dict[str, list[dict]] | None,
    *,
    min_us_change_pct: float = 1.0,
    top_sectors: int = 3,
    stocks_per_sector: int = 5,
) -> dict | None:
    """어젯밤 미국 섹터 등락 → 오늘 주목할 KR 업종·종목 뷰.

    입력:
      us_sectors      — 스냅샷 `sectors` 소스의 `data["sectors"]`
                        (`[{"ticker","name","change_pct"}, ...]`).
      sector_members  — `data/ledger/sector_members.json`
                        (`{업종명: [{"code","name","change_pct"}, ...]}`).

    반환(없으면 None — 있는 걸 없다고 하지 않지만 없는 걸 지어내지도 않는다):
      {"us_sectors": [...11개 등락순...],
       "up_count": n, "down_count": n,
       "tone": "상승 우위" | "하락 우위" | "혼조",
       "focus": [{"us_name","us_ticker","us_change_pct",
                  "kr_sectors": [업종명...],
                  "stocks": [{"code","name","change_pct","kr_sector"}...]}]}

    focus 는 `min_us_change_pct` 이상 오른 미국 섹터 상위 `top_sectors`개만.
    각 섹터의 KR 종목은 매핑된 업종들의 멤버를 **전일 KR 등락률 내림차순**으로
    `stocks_per_sector`개 — 이 기준의 뜻은 "그 업종 안에서 이미 수급이 도는
    종목"이다. 시가총액 데이터가 원장에 없어 대장주 기준은 쓸 수 없다(없는
    데이터로 고르는 척하지 않는다).

    tone 판정은 예측이 아니라 집계다: 11개 중 상승이 하락의 2배 이상이면 "상승
    우위", 반대면 "하락 우위", 그 외 "혼조".
    """
    if not us_sectors:
        return None
    rows = [
        s for s in us_sectors
        if isinstance(s, dict) and s.get("name") and s.get("change_pct") is not None
    ]
    if not rows:
        return None
    rows = sorted(rows, key=lambda s: s["change_pct"], reverse=True)

    up = sum(1 for s in rows if s["change_pct"] > 0)
    down = sum(1 for s in rows if s["change_pct"] < 0)
    if up >= max(down * 2, down + 3):
        tone = "상승 우위"
    elif down >= max(up * 2, up + 3):
        tone = "하락 우위"
    else:
        tone = "혼조"

    members = sector_members or {}
    focus = []
    for s in rows:
        if len(focus) >= top_sectors or s["change_pct"] < min_us_change_pct:
            break
        kr_sectors = US_TO_KR_SECTORS.get(s["name"], [])
        stocks = [
            {**m, "kr_sector": sec}
            for sec in kr_sectors
            for m in (members.get(sec) or [])
            if m.get("code") and m.get("change_pct") is not None
        ]
        stocks.sort(key=lambda m: m["change_pct"], reverse=True)
        focus.append({
            "us_name": s["name"],
            "us_ticker": s.get("ticker"),
            "us_change_pct": s["change_pct"],
            "kr_sectors": kr_sectors,
            "stocks": stocks[:stocks_per_sector],
        })

    return {
        "us_sectors": rows,
        "up_count": up,
        "down_count": down,
        "tone": tone,
        "focus": focus,
    }
