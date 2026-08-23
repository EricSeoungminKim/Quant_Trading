"""어젯밤 미국장 → 오늘 한국장 브리지 (2026-08-21 소유자 지시).

핵심 계약: (1) 매핑 표의 업종명이 실제 네이버 업종명과 문자까지 일치할 것 —
오타는 조용히 빈 매핑이 된다. (2) 예측하지 않는다 — tone 은 집계 라벨.
(3) 없는 데이터를 지어내지 않는다 — 입력이 비면 None.
"""
from __future__ import annotations

from quant.analyze.us_kr_bridge import US_TO_KR_SECTORS, build_us_kr_bridge

# 실측 네이버 업종 79개(EC2 data/ledger/sector_members.json 키, 2026-08-21).
_NAVER_SECTORS = {
    "IT서비스", "가구", "가스유틸리티", "가정용기기와용품", "가정용품", "건강관리기술",
    "건강관리업체및서비스", "건강관리장비와용품", "건설", "건축자재", "건축제품",
    "게임엔터테인먼트", "광고", "교육서비스", "기계", "기타", "기타금융",
    "다각화된소비자서비스", "다각화된통신서비스", "담배", "도로와철도운송",
    "디스플레이장비및부품", "디스플레이패널", "레저용장비와제품", "무선통신서비스",
    "무역회사와판매업체", "문구류", "반도체와반도체장비", "방송과엔터테인먼트",
    "백화점과일반상점", "복합기업", "복합유틸리티", "부동산", "비철금속",
    "사무용전자제품", "상업서비스와공급품", "생명과학도구및서비스", "생명보험",
    "생물공학", "석유와가스", "섬유,의류,신발,호화품", "소프트웨어", "손해보험",
    "식품", "식품과기본식료품소매", "양방향미디어와서비스", "에너지장비및서비스",
    "우주항공과국방", "운송인프라", "은행", "음료", "인터넷과카탈로그소매",
    "자동차", "자동차부품", "전기유틸리티", "전기장비", "전기제품", "전문소매",
    "전자장비와기기", "전자제품", "제약", "조선", "종이와목재", "증권", "창업투자",
    "철강", "출판", "카드", "컴퓨터와주변기기", "통신장비", "판매업체", "포장재",
    "항공사", "항공화물운송과물류", "해운사", "핸드셋", "호텔,레스토랑,레저",
    "화장품", "화학",
}


def test_mapping_names_match_real_naver_sector_names():
    """매핑 표의 모든 업종명이 실제 네이버 업종명 집합의 부분집합이어야 한다."""
    mapped = {sec for secs in US_TO_KR_SECTORS.values() for sec in secs}
    unknown = mapped - _NAVER_SECTORS
    assert not unknown, f"실제 네이버 업종에 없는 이름(오타?): {unknown}"


def test_mapping_covers_all_11_us_sector_labels():
    from quant.collect.sources.technical import SECTORS

    assert set(US_TO_KR_SECTORS) == set(SECTORS.values()), (
        "수집기의 섹터 ETF 라벨과 매핑 표가 어긋나면 그 섹터는 조용히 빈 focus 가 된다"
    )


_US = [
    {"ticker": "XLV", "name": "헬스케어", "change_pct": 3.5},
    {"ticker": "XLE", "name": "에너지", "change_pct": 1.2},
    {"ticker": "XLK", "name": "기술", "change_pct": 0.4},
    {"ticker": "XLF", "name": "금융", "change_pct": -1.1},
]

_MEMBERS = {
    "제약": [
        {"code": "000100", "name": "유한양행", "change_pct": 2.1},
        {"code": "128940", "name": "한미약품", "change_pct": -0.5},
    ],
    "생물공학": [{"code": "207940", "name": "삼성바이오로직스", "change_pct": 4.2}],
    "석유와가스": [{"code": "010950", "name": "S-Oil", "change_pct": 0.9}],
    "은행": [{"code": "105560", "name": "KB금융", "change_pct": 1.5}],
}


def test_focus_only_includes_us_sectors_above_threshold():
    out = build_us_kr_bridge(_US, _MEMBERS, min_us_change_pct=1.0)
    assert [f["us_name"] for f in out["focus"]] == ["헬스케어", "에너지"]
    # 기술(+0.4%)은 문턱 미달, 금융(-1.1%)은 하락 — focus 에 없다.


def test_focus_stocks_sorted_by_kr_change_desc_and_tagged_with_sector():
    out = build_us_kr_bridge(_US, _MEMBERS)
    health = out["focus"][0]
    assert [s["code"] for s in health["stocks"]] == ["207940", "000100", "128940"]
    assert health["stocks"][0]["kr_sector"] == "생물공학"


def test_tone_is_aggregation_not_prediction():
    up9 = [{"ticker": f"X{i}", "name": "기술", "change_pct": 0.5} for i in range(9)]
    down2 = [{"ticker": f"Y{i}", "name": "금융", "change_pct": -0.5} for i in range(2)]
    out = build_us_kr_bridge(up9 + down2, {})
    assert out["tone"] == "상승 우위"
    assert (out["up_count"], out["down_count"]) == (9, 2)

    mixed = build_us_kr_bridge(up9[:5] + down2 * 2, {})
    assert mixed["tone"] == "혼조"


def test_empty_inputs_return_none_not_fabrication():
    assert build_us_kr_bridge(None, _MEMBERS) is None
    assert build_us_kr_bridge([], _MEMBERS) is None


def test_missing_members_yield_empty_stocks_not_error():
    out = build_us_kr_bridge(_US, None)
    assert out["focus"][0]["stocks"] == []
