"""`quant.analyze.tg_digest` — 텔레그램 인텔리전스 다이제스트(2026-09-05,
"텔레그램 인텔리전스 레인" 착수, 소유자 요구 (2)(4))."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from quant.analyze.tg_digest import (
    DIGEST_MAX_CHARS,
    build_digest,
    persist_stance,
    render_telegram,
)

UTC = timezone.utc
NOW = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 4, 22, 0, tzinfo=UTC)

KR_TABLE = [("삼성전자", "005930"), ("SK하이닉스", "000660")]


def _row(handle: str, msg_id: str, text: str, published: str = "2026-09-04T23:00:00Z") -> dict:
    return {"handle": handle, "msg_id": msg_id, "text": text, "published": published,
            "links": [], "images": []}


# ── build_digest: 결정론 부분 ────────────────────────────────────────────


def test_build_digest_empty_messages_has_no_content():
    digest = build_digest([], "KR", NOW, since=SINCE)
    assert digest.has_content() is False
    assert digest.candidates == []
    assert digest.channel_notices == {}


def test_build_digest_is_deterministic_across_calls():
    messages = [_row("tazastock", "1", "삼성전자 강세, 71,000원 돌파.")]
    d1 = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    d2 = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert d1.candidates == d2.candidates
    assert d1.channel_entries == d2.channel_entries
    assert render_telegram(d1) == render_telegram(d2)


def test_build_digest_filters_messages_by_market_channel():
    # pikachu_aje: market=KR 전용 — US 다이제스트가 보면 안 된다.
    messages = [_row("pikachu_aje", "1", "AAPL surges")]
    digest = build_digest(messages, "US", NOW, since=SINCE)
    assert digest.channel_entries == {}


def test_build_digest_entity_extraction_ties_symbol_to_sentence():
    messages = [_row("tazastock", "1", "오늘은 흐리다. 삼성전자 강세, 반도체 업황 개선 기대.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert len(digest.candidates) == 1
    cand = digest.candidates[0]
    assert cand.symbol == "005930"
    assert cand.name == "삼성전자"
    assert "삼성전자 강세" in cand.sentence
    assert cand.channels == ("tazastock",)
    assert cand.message_count == 1


def test_build_digest_candidate_accumulates_channels_without_duplicates():
    messages = [
        _row("tazastock", "1", "삼성전자 강세"),
        _row("daegurr", "2", "삼성전자 목표가 상향"),
        _row("tazastock", "3", "삼성전자 추가 매수"),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert len(digest.candidates) == 1
    assert set(digest.candidates[0].channels) == {"tazastock", "daegurr"}
    assert digest.candidates[0].message_count == 3  # 서로 다른 msg_id 3건


def test_build_digest_ranks_candidates_by_distinct_message_count_then_symbol():
    """랭킹 기준은 채널 수가 아니라 **서로 다른 메시지 수**다(소유자 지시,
    2026-09-05 EC2 원장 리뷰: 목록형 메시지 하나에 90개 종목이 스쳐가도
    "메시지 1건"일 뿐이어야 한다). SK하이닉스는 1개 채널이지만 메시지 2건,
    삼성전자는 2개 채널이지만 메시지 1건뿐 — 채널 수 기준이면 삼성전자가
    앞서지만, 메시지 수 기준이면 SK하이닉스가 앞서야 한다."""
    messages = [
        _row("tazastock", "1", "SK하이닉스 강세"),
        _row("tazastock", "2", "SK하이닉스 추가 매수"),
        _row("tazastock", "3", "삼성전자 목표가 상향, SK하이닉스 언급"),
        _row("daegurr", "4", "삼성전자 반도체 업황 개선"),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    by_symbol = {c.symbol: c for c in digest.candidates}
    assert by_symbol["000660"].message_count == 3  # SK하이닉스: msg 1,2,3
    assert by_symbol["005930"].message_count == 2  # 삼성전자: msg 3,4 (2개 채널)
    assert [c.symbol for c in digest.candidates] == ["000660", "005930"]


def test_build_digest_no_name_table_still_returns_channel_entries():
    messages = [_row("tazastock", "1", "삼성전자 강세")]
    digest = build_digest(messages, "KR", NOW, since=SINCE)  # name_table 없음
    assert digest.candidates == []
    assert "tazastock" in digest.channel_entries


def test_build_digest_caps_channel_items_at_5():
    messages = [_row("tazastock", str(i), f"메시지 {i}") for i in range(8)]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    assert len(digest.channel_entries["tazastock"]) == 5


# ── 리스크 키워드 태깅 ────────────────────────────────────────────────────


def test_build_digest_tags_risk_keywords_regardless_of_symbol_mention():
    # "지정학"/"금리" 문장엔 종목명이 없다 — 그래도 리스크 항목이어야 한다
    # (실측 회귀 가드, 2026-09-05: hits 없으면 통째로 건너뛰던 버그).
    messages = [_row("daegurr", "1", "미국 금리 발표 예정, 지정학 리스크 확대 우려.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    keywords = {r.keyword for r in digest.risk_items}
    assert keywords == {"금리", "지정학"}


def test_build_digest_no_risk_keywords_means_empty_list():
    messages = [_row("tazastock", "1", "삼성전자 강세, 반도체 업황 개선.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.risk_items == []


# ── 채널 명시적 결측(clawnewssummary text_not_supported 등) ────────────────


def test_build_digest_blank_text_channel_gets_explicit_notice():
    messages = [_row("clawnewssummary", "1", "")]
    digest = build_digest(messages, "US", NOW, since=SINCE)
    assert digest.channel_notices == {"clawnewssummary": "미리보기 없음"}
    assert "clawnewssummary" not in digest.channel_entries


def test_build_digest_channel_with_some_blank_and_some_text_is_not_flagged():
    messages = [
        _row("tazastock", "1", ""),
        _row("tazastock", "2", "삼성전자 강세"),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    assert "tazastock" not in digest.channel_notices
    assert len(digest.channel_entries["tazastock"]) == 1


# ── 숫자 검증(결정론 NumberClaim) ────────────────────────────────────────


def test_build_digest_price_claim_verified_within_tolerance():
    messages = [_row("tazastock", "1", "삼성전자 71,000원 돌파.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 71_500.0,  # 0.7% 오차 — 2% 이내
    )
    assert len(digest.number_claims) == 1
    assert digest.number_claims[0].status == "✓"


def test_build_digest_price_claim_rejected_outside_tolerance():
    messages = [_row("tazastock", "1", "삼성전자 71,000원 돌파.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 50_000.0,
    )
    assert digest.number_claims[0].status == "✗"


def test_build_digest_price_claim_unverified_without_quotes_lookup():
    messages = [_row("tazastock", "1", "삼성전자 71,000원 돌파.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.number_claims[0].status == "미확인"


def test_build_digest_percent_claim_always_unverified():
    messages = [_row("tazastock", "1", "삼성전자 +3.5% 상승.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 71_000.0,
    )
    percent_claims = [c for c in digest.number_claims if "%" in c.value]
    assert percent_claims and all(c.status == "미확인" for c in percent_claims)


def test_build_digest_quotes_lookup_failure_falls_back_to_unverified():
    def _boom(sym):
        raise RuntimeError("시세 조회 실패")

    messages = [_row("tazastock", "1", "삼성전자 71,000원 돌파.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE, quotes_lookup=_boom)
    assert digest.number_claims[0].status == "미확인"


# ── 숫자 클레임 정밀도(소유자 요구, 2026-09-05 EC2 원장 리뷰) ────────────────
#
# 실측: LG전자(066570) 기사의 "2026년 9월 4일"이 (066570, "2026")/(066570, "9")/
# (066570, "4") 로 뽑혀 "채널 주장 ✗"로 표시됐다 — 날짜를 숫자 클레임으로
# 착각한 것. 단위(원/만원/억/달러/$/%/bp/포인트/p)가 없거나 가격 단어 근방
# 3자리+ 숫자가 아니면 클레임 자체를 만들지 않는다.


def test_build_digest_drops_bare_dates_and_small_integers():
    messages = [_row("tazastock", "1", "LG전자는 2026년 9월 4일 세미나를 개최했다.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=[("LG전자", "066570")])
    assert digest.number_claims == []


def test_build_digest_drops_bare_small_negative_integer():
    messages = [_row("tazastock", "1", "삼성전자 -6 정도 조정 받았다.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.number_claims == []


def test_build_digest_accepts_manwon_unit_and_scales_to_won():
    """"7.1만원"은 71,000원과 같아야 한다 — 단위 환산 없이 비교하면 오탐."""
    messages = [_row("tazastock", "1", "삼성전자 7.1만원 목표.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 71_000.0,
    )
    assert len(digest.number_claims) == 1
    assert digest.number_claims[0].value == "7.1만원"
    assert digest.number_claims[0].status == "✓"


def test_build_digest_accepts_eok_unit_and_scales():
    messages = [_row("tazastock", "1", "삼성전자 시가총액 500억 달성.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 50_000_000_000.0,
    )
    assert digest.number_claims[0].status == "✓"


def test_build_digest_accepts_dollar_prefixed_number():
    messages = [_row("rafikiresearch", "1", "AAPL $250 목표가 제시.")]
    digest = build_digest(
        messages, "US", NOW, since=SINCE, name_table=[("Apple", "AAPL")],
        quotes_lookup=lambda sym: 250.0,
    )
    values = [c.value for c in digest.number_claims]
    assert "$250" in values


def test_build_digest_bare_number_accepted_only_near_price_word():
    """유효숫자 3자리 이상이어도 목표가/주가/종가/저가/고가 같은 가격 단어
    근방(15자)이 아니면 클레임으로 만들지 않는다."""
    near = [_row("tazastock", "1", "삼성전자 목표가 85000 상향.")]
    far = [_row("tazastock", "2", "삼성전자 오늘 거래량은 85000이다 그런데 아무 상관없는 얘기를 길게 하다가 목표가를 언급함.")]
    digest_near = build_digest(near, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    digest_far = build_digest(far, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert any(c.value == "85000" for c in digest_near.number_claims)
    assert not any(c.value == "85000" for c in digest_far.number_claims)


def test_build_digest_bare_number_under_3_sig_digits_rejected_even_near_price_word():
    messages = [_row("tazastock", "1", "삼성전자 목표가 71 상향.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.number_claims == []


def test_build_digest_dedupes_identical_symbol_value_channel():
    messages = [
        _row("tazastock", "1", "삼성전자 71,000원 돌파. 삼성전자 71,000원 재확인."),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    values = [(c.symbol, c.value, c.handle) for c in digest.number_claims]
    assert len(values) == len(set(values))


def test_render_telegram_shows_no_verifiable_claims_message_when_empty():
    """검증 가능한 클레임이 하나도 없으면(날짜만 있던 경우 등) 그렇다고
    명시한다 — 섹션이 조용히 사라지지 않는다(소유자 지시)."""
    messages = [_row("tazastock", "1", "LG전자는 2026년 9월 4일 세미나를 개최했다.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=[("LG전자", "066570")])
    text = render_telegram(digest)
    assert "검증 가능한 수치 주장 없음" in text


# ── 후보 정밀도(소유자 요구, 2026-09-05 EC2 원장 리뷰) ──────────────────────


def test_build_digest_us_rejects_stoplisted_acronym_even_with_dollar_prefix():
    """HBM 은 스톱리스트에 있다 — $ 접두가 있어도 제외한다(흔한 반도체
    용어라 오탐 위험이 스톱리스트 사유 자체보다 크다고 판단)."""
    messages = [_row("rafikiresearch", "1", "$HBM 수요가 급증하고 있다.")]
    digest = build_digest(messages, "US", NOW, since=SINCE, name_table=[("HBM Corp", "HBM")])
    assert digest.candidates == []


def test_build_digest_us_bare_ticker_rejected_without_context():
    """티커 원형만 매칭되고 $ 접두도, 가격/종목 맥락도 없으면 제외한다
    (실측 오탐: "AI 데이터센터 냉각"류 문장에서 GAA 같은 약어가 후보로 잡혔다)."""
    messages = [_row("rafikiresearch", "1", "GAA 구조가 차세대 트랜지스터의 핵심이다.")]
    digest = build_digest(messages, "US", NOW, since=SINCE, name_table=[("Some Corp", "GAA")])
    assert digest.candidates == []


def test_build_digest_us_bare_ticker_accepted_with_dollar_prefix():
    messages = [_row("rafikiresearch", "1", "$NVDA 급등세를 이어가고 있다.")]
    digest = build_digest(messages, "US", NOW, since=SINCE, name_table=[("NVIDIA", "NVDA")])
    assert any(c.symbol == "NVDA" for c in digest.candidates)


def test_build_digest_us_bare_ticker_accepted_with_price_context_word():
    messages = [_row("rafikiresearch", "1", "NVDA 목표가가 상향 조정됐다.")]
    digest = build_digest(messages, "US", NOW, since=SINCE, name_table=[("NVIDIA", "NVDA")])
    assert any(c.symbol == "NVDA" for c in digest.candidates)


def test_build_digest_us_accepts_full_company_name_without_extra_context():
    """회사명(예: Microsoft)으로 매칭되면 티커 전용 규칙(맥락 요구)을 적용하지
    않는다 — 이름 자체가 이미 충분한 신호다."""
    # 한글 조사가 영문 뒤에 바로 붙으면(예: "Microsoft가") entities.extract_us
    # 의 \b 단어 경계 자체가 성립하지 않는다(한글도 \w) — 공백으로 분리한
    # 현실적인 표기(괄호/구두점)로 검증한다.
    messages = [_row("rafikiresearch", "1", "Microsoft. 신규 데이터센터 투자를 발표했다.")]
    digest = build_digest(messages, "US", NOW, since=SINCE, name_table=[("Microsoft", "MSFT")])
    assert any(c.symbol == "MSFT" for c in digest.candidates)


def test_build_digest_kr_generic_name_rejected_without_suffix_or_code():
    """"유니온"은 일반 단어이기도 하다 — 법인 접미사나 종목코드 없이 단독
    매칭되면 제외한다(실측 오탐, 목록형 메시지)."""
    messages = [_row("tazastock", "1", "오늘의 상한가: 유니온, 가온전선.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE,
        name_table=[("유니온", "003100"), ("가온전선", "000500")],
    )
    assert not any(c.symbol == "003100" for c in digest.candidates)


def test_build_digest_kr_generic_name_accepted_with_corp_suffix():
    messages = [_row("tazastock", "1", "삼성전자가 신제품을 공개했다.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=[("삼성전자", "005930")])
    assert any(c.symbol == "005930" for c in digest.candidates)


def test_build_digest_kr_generic_name_accepted_with_code_present():
    messages = [_row("tazastock", "1", "유니온(003100) 상한가 진입.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=[("유니온", "003100")])
    assert any(c.symbol == "003100" for c in digest.candidates)


# ── CANDS(소유자 요구, 2026-09-05) — 서로 다른 메시지 ≥2건, 최대 10개 ──────


def test_cands_excludes_symbols_mentioned_in_only_one_message():
    messages = [_row("tazastock", "1", "삼성전자 강세")]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.candidates and digest.candidates[0].symbol == "005930"
    assert digest.cands == ()


def test_cands_includes_symbols_mentioned_in_two_distinct_messages():
    messages = [
        _row("tazastock", "1", "삼성전자 강세"),
        _row("daegurr", "2", "삼성전자 목표가 상향"),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    assert digest.cands == ("005930",)


def test_cands_never_exceeds_10():
    table = [(f"종목{i}", f"{100000+i:06d}") for i in range(15)]
    messages = []
    for i in range(15):
        messages.append(_row("tazastock", f"{i}a", f"종목{i} 강세"))
        messages.append(_row("daegurr", f"{i}b", f"종목{i} 목표가 상향"))
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=table)
    assert len(digest.cands) <= 10


# ── 리스크 항목 — 보일러플레이트 제거 + 메시지당 태그당 1건 ──────────────────


def test_build_digest_strips_collection_complete_tail_from_risk_analysis():
    messages = [_row(
        "rafikiresearch", "1",
        "국채 시장 리스크가 커지고 있다.수집 완료: 2026년 8월 24일 (월) 06:30 KST"
        "출처: CNBC, Reuters검증 통과 인원: 37인 중 2인다음 브리핑: 내일",
    )]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    sentences = [r.sentence for r in digest.risk_items]
    assert not any("출처" in s or "다음 브리핑" in s for s in sentences)


def test_build_digest_strips_no_comment_boilerplate_block():
    messages = [_row(
        "rafikiresearch", "1",
        "[기업가 및 테크 리더]제이미 다이먼 (JP모건)금일 발언 없음(최근 스탠스: "
        "국채 시장이 불안하다고 언급)일론 머스크금일 발언 없음(참고: 없음)",
    )]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    # "금일 발언 없음(...)" 괄호 안의 "국채" 언급까지 제거되므로 리스크 항목이
    # 없어야 한다 — 보일러플레이트 블록 자체가 신호로 잡히면 안 된다.
    assert digest.risk_items == []


def test_build_digest_same_message_same_keyword_tagged_once():
    """같은 메시지에서 같은 리스크 태그가 여러 문장에 걸쳐 반복되면 1건만
    남긴다(실측: aetherjapanresearch NBIM 기사에서 "국채"가 연속 4문장에
    반복됐다)."""
    messages = [_row(
        "aetherjapanresearch", "1",
        "국채 비중 축소 검토중이다. 국채 하위지수 비중을 낮출 것을 권고했다. "
        "국채 비중은 34%로 축소된다. 국채 보유자들이 압박을 받고 있다.",
    )]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    guk_chae_items = [r for r in digest.risk_items if r.keyword == "국채"]
    assert len(guk_chae_items) == 1


def test_build_digest_same_message_different_keywords_both_tagged():
    """한 문장에 서로 다른 리스크 축이 함께 있으면 둘 다 남긴다 — 근거리
    중복이 아니라 서로 다른 신호다."""
    messages = [_row("daegurr", "1", "미국 금리 발표 예정, 지정학 리스크 확대 우려.")]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    assert {r.keyword for r in digest.risk_items} == {"금리", "지정학"}


def test_risk_groups_shows_channel_once_per_group_and_caps_at_6():
    rows = []
    for i, kw in enumerate(["금리", "관세", "지정학", "실적", "규제", "유동성", "환율"]):
        rows.append(_row("daegurr", str(i), f"{kw} 관련 소식이 있다."))
    digest = build_digest(rows, "KR", NOW, since=SINCE)
    groups = digest.risk_groups()
    assert len(groups) == 6  # cap
    for keyword, text in groups:
        assert text.count("daegurr:") == 1  # 채널명은 그룹당 한 번


# ── 스탠스 대체 문구(소유자 요구, 2026-09-05) — 서술기 미가용 시 정직하게 ──


def test_stance_display_shows_risk_tag_counts_when_llm_unavailable():
    messages = [
        _row("daegurr", "1", "금리 인상 우려가 커지고 있다."),
        _row("daegurr", "2", "지정학 리스크가 확대되고 있다."),
        _row("tazastock", "3", "규제 강화 소식이 전해졌다."),
        _row("tazastock", "4", "규제 완화 논의가 있었다."),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE)
    text = digest.stance_display()
    assert "서술기 미가용" in text
    assert "자동 판정 안 함" in text
    assert "금리 1" in text or "지정학 1" in text or "규제" in text


def test_stance_display_reports_no_risk_tags_when_none_found():
    digest = build_digest([_row("tazastock", "1", "오늘은 평온한 하루였다.")], "KR", NOW, since=SINCE)
    assert digest.stance_display() == "서술기 미가용 — 리스크 태그 없음 (자동 판정 안 함)"


def test_stance_display_uses_real_stance_when_llm_succeeds():
    reply = (
        "[STANCE]\n방어 — 리스크 확대\n"
        "[SUMMARY]\n오늘은 조심스러운 흐름이다. 변동성 확대 우려. 관망 권고.\n"
    )
    digest = build_digest(
        [_row("tazastock", "1", "오늘은 평온한 하루였다.")], "KR", NOW, since=SINCE,
        llm_call=lambda p: reply,
    )
    assert digest.stance_display() == "방어 — 리스크 확대"


def test_render_telegram_always_shows_stance_section():
    """LLM 없이도(서술기 미가용) [스탠스] 섹션 자체는 항상 나온다 — 조용히
    생략되지 않는다."""
    digest = build_digest([_row("daegurr", "1", "금리 인상 우려.")], "KR", NOW, since=SINCE)
    text = render_telegram(digest)
    assert "[스탠스]" in text
    assert "서술기 미가용" in text


# ── LLM 부분(all-or-nothing, narrator.verify_numbers 재사용) ─────────────


def _kr_digest_with_candidate():
    messages = [_row("tazastock", "1", "삼성전자 71,000원 돌파. 반도체 업황 개선.")]
    return messages


def test_build_digest_llm_not_called_when_no_channel_entries():
    calls = []
    build_digest([], "KR", NOW, since=SINCE, llm_call=lambda p: calls.append(p) or "무시됨")
    assert calls == []


def test_build_digest_llm_accepted_when_numbers_verbatim():
    reply = (
        "[STANCE]\n중립 — 특별한 방향성 신호 없음\n"
        "[SUMMARY]\n오늘은 특별한 이슈가 없다. 조용한 하루다. 관망이 적절하다.\n"
        "[CANDIDATES]\n005930: 71,000원대에서 매수세 유입\n"
    )
    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE,
        llm_call=lambda p: reply,
    )
    assert digest.llm_used is True
    assert digest.stance == "중립"
    assert digest.candidate_reasons["005930"] == "71,000원대에서 매수세 유입"


def test_build_digest_llm_rejected_when_number_hallucinated():
    reply = (
        "[STANCE]\n공격 — 목표가 90,000원 돌파 기대\n"  # 90,000은 원문에 없다
        "[SUMMARY]\n강한 매수 신호가 포착됐다. 목표가 상향이 예상된다. 적극 매수 구간이다.\n"
    )
    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE,
        llm_call=lambda p: reply,
    )
    assert digest.llm_used is False
    assert digest.stance is None


def test_build_digest_llm_rejected_when_marker_missing():
    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE,
        llm_call=lambda p: "그냥 평범한 텍스트, 마커 없음",
    )
    assert digest.llm_used is False


def test_build_digest_llm_rejected_when_stance_word_missing():
    reply = "[STANCE]\n애매함 — 판단 보류\n[SUMMARY]\n특이사항 없음, 조용, 관망, 대기.\n"
    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE,
        llm_call=lambda p: reply,
    )
    assert digest.llm_used is False


def test_build_digest_llm_failure_falls_back_to_deterministic():
    def _raise(prompt):
        raise RuntimeError("LLM 다운")

    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE, llm_call=_raise,
    )
    assert digest.llm_used is False
    assert len(digest.candidates) == 1  # 결정론 부분은 그대로 살아 있다


def test_build_digest_llm_none_reply_falls_back():
    digest = build_digest(
        _kr_digest_with_candidate(), "KR", NOW, since=SINCE, name_table=KR_TABLE,
        llm_call=lambda p: None,
    )
    assert digest.llm_used is False


# ── render_telegram ──────────────────────────────────────────────────────


def test_render_telegram_empty_digest_says_no_new_messages():
    digest = build_digest([], "KR", NOW, since=SINCE)
    text = render_telegram(digest)
    assert "새 메시지 없음" in text


def test_render_telegram_stays_under_char_budget():
    # 채널당 상한(5)까지 채워도 예산을 넘지 않는지 — 극단적으로 긴 문장으로 압박.
    long_text = "삼성전자 " * 40 + "강세"
    messages = [_row("tazastock", str(i), long_text) for i in range(5)] + [
        _row("daegurr", str(i), "미국 금리 발표 예정, 지정학 리스크 확대 우려." * 3)
        for i in range(5)
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    text = render_telegram(digest)
    assert len(text) <= DIGEST_MAX_CHARS


def test_render_telegram_html_tags_are_balanced():
    messages = [
        _row("tazastock", "1", "삼성전자 71,000원 돌파."),
        _row("daegurr", "2", "미국 금리 발표 예정, 지정학 리스크 확대."),
    ]
    digest = build_digest(messages, "KR", NOW, since=SINCE, name_table=KR_TABLE)
    text = render_telegram(digest)
    for tag in ("b", "i"):
        assert text.count(f"<{tag}>") == text.count(f"</{tag}>")
    assert len(re.findall(r"<blockquote[^>]*>", text)) == text.count("</blockquote>")
    assert text.count("<a ") == text.count("</a>")


def test_render_telegram_includes_report_url_link():
    digest = build_digest(
        [_row("tazastock", "1", "삼성전자 강세")], "KR", NOW, since=SINCE, name_table=KR_TABLE,
    )
    text = render_telegram(digest, report_url="https://example.com/report.html")
    assert "https://example.com/report.html" in text


def test_render_telegram_shows_channel_notice():
    digest = build_digest([_row("clawnewssummary", "1", "")], "US", NOW, since=SINCE)
    text = render_telegram(digest)
    assert "clawnewssummary" in text
    assert "미리보기 없음" in text


# ── persist_stance ───────────────────────────────────────────────────────


def test_persist_stance_writes_expected_shape(tmp_path):
    reply = (
        "[STANCE]\n방어 — 리스크 확대\n"
        "[SUMMARY]\n오늘은 조심스러운 흐름이다. 변동성 확대 우려. 관망 권고.\n"
        "[CANDIDATES]\n005930: 강세 언급\n"
    )
    digest = build_digest([_row("tazastock", "1", "삼성전자 강세")], "KR", NOW, since=SINCE,
                           name_table=KR_TABLE, llm_call=lambda p: reply)
    path = tmp_path / "tg_stance.json"
    persist_stance(path, digest)
    import json
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["market"] == "KR"
    assert payload["stance"] == "방어"
    assert payload["why"]
    assert payload["at"] == NOW.isoformat()
    assert payload["sources"] == ["tazastock"]


def test_persist_stance_noop_when_stance_none(tmp_path):
    digest = build_digest([], "KR", NOW, since=SINCE)
    path = tmp_path / "tg_stance.json"
    persist_stance(path, digest)
    assert not path.exists()


def test_persist_stance_does_not_overwrite_previous_when_stance_none(tmp_path):
    path = tmp_path / "tg_stance.json"
    path.write_text('{"market": "KR", "stance": "공격"}', encoding="utf-8")
    digest = build_digest([], "KR", NOW, since=SINCE)  # 이번엔 LLM 없음/실패
    persist_stance(path, digest)
    assert "공격" in path.read_text(encoding="utf-8")


def test_build_digest_non_price_magnitude_is_unverified_not_wrong():
    # 시총·매출 같은 큰 숫자(현재가의 0.5~2배 밖)는 가격 주장이 아니다 — "✗"(틀린 가격)
    # 로 낙인찍지 않고 "미확인" 으로 둔다(2026-09-05 실 원장 리뷰: "GOOGL 7,710억 ✗").
    messages = [_row("tazastock", "1", "삼성전자 시총 4,500,000억원 돌파, 주가 71,000원.")]
    digest = build_digest(
        messages, "KR", NOW, since=SINCE, name_table=KR_TABLE,
        quotes_lookup=lambda sym: 71_500.0,
    )
    statuses = {c.value: c.status for c in digest.number_claims}
    assert statuses.get("71,000원") == "✓"
    assert statuses.get("4,500,000억") == "미확인"


# ── 프로그램 스탠스(결정론, 소유자 결정 2026-09-05 — "유료 레인 불필요") ────


def test_program_stance_display_renders_label_multiplier_and_reasons():
    regime = {"label": "neutral", "risk_multiplier": 1.0,
              "reasons": ["QQQ 20일선 대비 −0.01%", "5일 변동성 0.61배"]}
    digest = build_digest([_row("tazastock", "1", "삼성전자 강세")], "KR", NOW, since=SINCE, regime=regime)
    assert digest.program_stance_display() == (
        "프로그램 스탠스: 중립(1.0x) — QQQ 20일선 대비 −0.01%, 5일 변동성 0.61배"
    )


def test_program_stance_display_maps_defensive_and_aggressive_labels():
    d1 = build_digest([], "KR", NOW, since=SINCE,
                       regime={"label": "defensive", "risk_multiplier": 0.5, "reasons": []})
    d2 = build_digest([], "KR", NOW, since=SINCE,
                       regime={"label": "aggressive", "risk_multiplier": 1.5, "reasons": []})
    assert d1.program_stance_display().startswith("프로그램 스탠스: 방어(0.5x)")
    assert d2.program_stance_display().startswith("프로그램 스탠스: 공격(1.5x)")


def test_program_stance_display_appends_channel_risk_tag_counts():
    messages = [
        _row("daegurr", "1", "금리 인상 우려가 커지고 있다."),
        _row("daegurr", "2", "지정학 리스크가 확대되고 있다."),
    ]
    regime = {"label": "neutral", "risk_multiplier": 1.0, "reasons": ["근거 문구"]}
    digest = build_digest(messages, "KR", NOW, since=SINCE, regime=regime)
    text = digest.program_stance_display()
    assert "리스크 태그" in text
    assert "금리 1" in text or "지정학 1" in text


def test_program_stance_display_missing_regime_is_honest_about_it():
    digest = build_digest([_row("tazastock", "1", "삼성전자 강세")], "KR", NOW, since=SINCE)
    assert digest.program_stance_display() == "프로그램 스탠스: 판정 불가 (regime.json 없음)"


def test_program_stance_display_ignores_regime_without_label():
    digest = build_digest([], "KR", NOW, since=SINCE, regime={"risk_multiplier": 1.0, "reasons": []})
    assert "판정 불가" in digest.program_stance_display()


def test_render_telegram_shows_program_stance_line_before_llm_stance_line():
    """(a) 결정론 프로그램 스탠스가 [스탠스] 섹션의 첫 줄, (b) LLM 스탠스(또는
    미가용 대체문)가 둘째 줄 — 소유자 지시(2026-09-05): "결정론이 먼저"."""
    regime = {"label": "neutral", "risk_multiplier": 1.0, "reasons": ["근거"]}
    digest = build_digest([_row("daegurr", "1", "금리 인상 우려.")], "KR", NOW, since=SINCE, regime=regime)
    text = render_telegram(digest)
    stance_block = text.split("[스탠스]")[1].split("\n\n")[0]
    prog_idx = stance_block.find("프로그램 스탠스")
    llm_idx = stance_block.find("서술기 미가용")
    assert prog_idx != -1 and llm_idx != -1
    assert prog_idx < llm_idx


# ── LLM 스탠스 마이크로프롬프트(선택, 모듈 docstring "LLM 스탠스" 절) ────────


def _kr_messages_with_channel_entry():
    return [_row("tazastock", "1", "오늘은 평온한 하루였다.")]


def test_stance_llm_call_fills_stance_when_valid():
    digest = build_digest(
        _kr_messages_with_channel_entry(), "KR", NOW, since=SINCE,
        stance_llm_call=lambda p: {"stance": "방어", "why": "리스크 확대"},
    )
    assert digest.stance == "방어"
    assert digest.stance_why == "리스크 확대"
    assert digest.stance_display() == "방어 — 리스크 확대"


def test_stance_llm_call_receives_prompt_asking_for_json_only():
    seen = {}

    def _call(prompt):
        seen["prompt"] = prompt
        return {"stance": "중립", "why": "특이사항 없음"}

    build_digest(_kr_messages_with_channel_entry(), "KR", NOW, since=SINCE, stance_llm_call=_call)
    assert '"stance"' in seen["prompt"]
    assert '"why"' in seen["prompt"]
    assert "오늘은 평온한 하루였다" in seen["prompt"]


def test_stance_llm_call_rejected_when_stance_value_not_in_enum():
    """`stance_only`가 이미 엄격 검증하지만, 다른 콜러블이 주입돼도 방어한다
    (모듈 원칙: 절반만 맞는 판정은 안 믿느니만 못하다)."""
    digest = build_digest(
        _kr_messages_with_channel_entry(), "KR", NOW, since=SINCE,
        stance_llm_call=lambda p: {"stance": "공격적", "why": "x"},
    )
    assert digest.stance is None
    assert "서술기 미가용" in digest.stance_display()


def test_stance_llm_call_rejected_when_not_a_dict():
    digest = build_digest(
        _kr_messages_with_channel_entry(), "KR", NOW, since=SINCE,
        stance_llm_call=lambda p: "방어 — 리스크 확대",
    )
    assert digest.stance is None


def test_stance_llm_call_exception_falls_back_gracefully():
    def _boom(p):
        raise RuntimeError("OpenRouter 다운")

    digest = build_digest(
        _kr_messages_with_channel_entry(), "KR", NOW, since=SINCE, stance_llm_call=_boom,
    )
    assert digest.stance is None
    assert digest.has_content() is True  # 결정론 부분은 그대로 살아 있다


def test_stance_llm_call_not_invoked_when_no_channel_entries():
    calls = []
    build_digest([], "KR", NOW, since=SINCE, stance_llm_call=lambda p: calls.append(p) or None)
    assert calls == []


def test_stance_llm_call_skipped_when_llm_call_already_produced_a_stance():
    """`llm_call`(큰 프롬프트, [STANCE] 마커)이 이미 유효한 스탠스를 만들었으면
    `stance_llm_call`은 호출되지 않는다(중복 호출 방지, 하위호환)."""
    reply = (
        "[STANCE]\n방어 — 큰 프롬프트 판정\n"
        "[SUMMARY]\n특이사항 없음, 조용, 관망, 대기.\n"
    )
    calls = []
    digest = build_digest(
        _kr_messages_with_channel_entry(), "KR", NOW, since=SINCE,
        llm_call=lambda p: reply,
        stance_llm_call=lambda p: calls.append(p) or {"stance": "공격", "why": "무시돼야 함"},
    )
    assert digest.stance == "방어"
    assert calls == []
