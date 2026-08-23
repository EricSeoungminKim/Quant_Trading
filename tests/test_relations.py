"""관계 로직. 핵심 계약: LLM 은 후보만 — 목록 밖·자기 자신·형식 위반은
0점이 아니라 **버린다** (shadow-judge 와 같은 규칙: 0점을 주면 '최하위 평가'가
되어 하류 순위를 오염시킨다)."""
from quant.analyze.relations import (
    MIN_EVIDENCE, build_extraction_prompt, evidence_score, match_codes,
    merge_relation, parse_candidates)

_NAMES = {"한미반도체": "042700", "이오테크닉스": "039030", "삼성전자": "005930"}
_TABLE = list(_NAMES.items())


def test_prompt_contains_titles_and_format_contract():
    p = build_extraction_prompt("삼성전자", [{"title": "HBM4 조기 양산", "body": "본문..."}],
                                market="KR")
    assert "삼성전자" in p and "HBM4" in p
    assert "|" in p  # 형식 계약(이름 | 종류 | 이유)이 프롬프트에 명시된다


def test_prompt_targets_korean_listing_for_kr_market():
    p = build_extraction_prompt("삼성전자", [], market="KR")
    assert "한국 상장사" in p
    assert "미국 상장사" not in p


def test_prompt_targets_us_listing_for_us_market():
    """C1 — US 프롬프트가 "한국 상장사"를 찾으라고 하면 매칭될 미국 상장사가
    구조적으로 없다(화이트리스트가 S&P500 이라). 시장별로 분기해야 한다."""
    p = build_extraction_prompt("Nvidia", [], market="US")
    assert "미국 상장사" in p
    assert "한국 상장사" not in p


def test_parse_candidates_drops_out_of_list_and_self():
    text = ("한미반도체 | 수혜주 | HBM 본딩 장비 공급\n"
            "존재하지않는회사 | 수혜주 | 이유\n"          # 목록 밖 → 버림
            "삼성전자 | 수혜주 | 자기 자신\n"              # src 자신 → 버림
            "이오테크닉스 · 수혜주 · 구분자 위반\n")       # 형식 위반 → 버림
    got = parse_candidates(text, _NAMES, src_symbol="005930")
    assert [c["dst"] for c in got] == ["042700"]
    assert got[0]["kind"] == "beneficiary"


def test_parse_candidates_none_is_empty():
    assert parse_candidates(None, _NAMES, "005930") == []


def test_parse_candidates_strips_angle_brackets_from_reason():
    """후속 B(HTML 렌더)가 LLM 이유를 그대로 붙이면 `<`/`>` 는 저장형 XSS다."""
    text = "한미반도체 | 수혜주 | <script>alert(1)</script> 공급\n"
    got = parse_candidates(text, _NAMES, src_symbol="005930")
    assert "<" not in got[0]["reason"] and ">" not in got[0]["reason"]


# ── match_codes ───────────────────────────────────────────────

def test_match_codes_longest_name_masks_substring_name():
    """실측(2026-08-15): '이닉스'(452400)가 'SK하이닉스'(000660)의 부분문자열
    이라, 마스킹 없이 `name in title` 로 세면 'SK하이닉스' 헤드라인을 전부
    '이닉스' 언급으로 잘못 잡는다 — 소스 선정에서 이닉스가 15건으로 1위,
    relations.json 에 이닉스→SK하이닉스[경쟁사] 가 들어갔다(reason 문장조차
    SK하이닉스 서술이었다). 긴 이름이 이미 차지한 구간에서는 짧은 이름이
    매칭하지 못해야 한다."""
    table = [("이닉스", "452400"), ("SK하이닉스", "000660")]
    matches = match_codes(table, ["SK하이닉스 급등", "이닉스 신규 상장"])
    assert matches[0] == {"000660"}  # SK하이닉스만 — 이닉스는 마스킹된다
    assert matches[1] == {"452400"}  # 진짜 '이닉스' 단독 언급


def test_match_codes_abbreviation_without_masking_name_is_rejected():
    """실측(2026-08-15, EC2): 마스킹은 표에 긴 이름이 있을 때만 작동한다.
    언론이 정식명 대신 약칭 '하이닉스'를 쓰면 표에 '하이닉스' 항목이 없어
    가릴 이름이 없고, 짧은 이름 '이닉스'가 '하이닉스' 속에서 그대로 매칭돼
    `data/ledger/relations.json` 에 이닉스→SK하이닉스 관계가 들어갔다(reason
    문장조차 SK하이닉스 서술이었다). 매칭 구간 바로 앞이 한글/영숫자면
    (더 긴 단어의 꼬리) 그 매칭을 버려야 한다."""
    table = [("이닉스", "452400"), ("SK하이닉스", "000660")]
    assert match_codes(table, ["하이닉스 급등"]) == [set()]


def test_match_codes_full_name_still_matches_after_boundary_check():
    table = [("이닉스", "452400"), ("SK하이닉스", "000660")]
    assert match_codes(table, ["SK하이닉스 HBM4 양산"]) == [{"000660"}]


def test_match_codes_standalone_short_name_still_matches():
    table = [("이닉스", "452400"), ("SK하이닉스", "000660")]
    assert match_codes(table, ["이닉스, 전기차 부품 수주"]) == [{"452400"}]


def test_match_codes_trailing_particle_does_not_block_match():
    """뒤(다음) 글자는 검사하지 않는다 — 한국어 조사가 명사 뒤에 바로 붙는다."""
    assert match_codes(_TABLE, ["삼성전자가 HBM 공급"]) == [{"005930"}]


def test_match_codes_independent_titles_still_match_own_names():
    matches = match_codes(_TABLE, ["한미반도체 수주 확대", "무관한 제목"])
    assert matches[0] == {"042700"}
    assert matches[1] == set()


def test_match_codes_multiple_names_in_one_title():
    matches = match_codes(_TABLE, ["삼성전자·한미반도체 협력"])
    assert matches[0] == {"005930", "042700"}


# ── evidence_score ───────────────────────────────────────────

def test_evidence_score_needs_corpus_presence():
    """`evidence_score` 는 이제 코드 + `match_codes` 결과(title_matches)를
    받는다 — 이름 부분문자열이 아니라 마스킹을 거친 코드로 비교해야
    '이닉스'/'SK하이닉스' 류 오탐을 피한다."""
    titles = ["한미반도체, HBM 장비 수주 확대", "삼성전자·한미반도체 협력 강화"]
    matches = match_codes(_TABLE, titles)
    strong = evidence_score("042700", "005930", matches)  # 한미반도체 / 삼성전자
    zero = evidence_score("039030", "005930", matches)    # 이오테크닉스 / 삼성전자
    assert strong >= MIN_EVIDENCE       # 언급 + 동시출현
    assert zero < MIN_EVIDENCE          # 코퍼스에 없으면 LLM 말만으로 못 들어간다


def test_evidence_score_does_not_count_substring_contaminated_mentions():
    """마스킹 전 코드라면 실패했을 회귀 케이스: 'SK하이닉스' 헤드라인만 있는
    코퍼스에서 '이닉스'(452400)의 언급 수가 0이어야 한다."""
    table = [("이닉스", "452400"), ("SK하이닉스", "000660")]
    titles = ["SK하이닉스 급등", "SK하이닉스 실적 발표", "SK하이닉스 신고가",
             "SK하이닉스 목표가 상향", "SK하이닉스 HBM 증설"]
    matches = match_codes(table, titles)
    ininics_score = evidence_score("452400", "000660", matches)
    hynix_score = evidence_score("000660", "452400", matches)
    assert ininics_score == 0            # 이닉스는 이 코퍼스에 한 번도 안 나온다
    assert hynix_score >= MIN_EVIDENCE   # SK하이닉스는 5건(다양성 보너스 포함)


def test_merge_keeps_first_seen_updates_verification():
    old = {"src": "005930", "dst": "042700", "kind": "beneficiary",
           "reason": "옛 이유", "evidence_score": 55,
           "first_seen": "2026-08-01", "last_verified": "2026-08-01"}
    new = merge_relation(old, {"src": "005930", "dst": "042700",
                               "kind": "beneficiary", "reason": "새 이유"},
                         score=70, today="2026-08-15")
    assert new["first_seen"] == "2026-08-01"      # 이력 보존
    assert new["last_verified"] == "2026-08-15"
    assert new["evidence_score"] == 70 and new["reason"] == "새 이유"


def test_merge_new_relation():
    row = merge_relation(None, {"src": "005930", "dst": "042700",
                                "kind": "beneficiary", "reason": "r"},
                         score=60, today="2026-08-15")
    assert row["first_seen"] == row["last_verified"] == "2026-08-15"


def test_merge_preserves_dst_name_when_candidate_has_it():
    """테마 경로(_theme_relations)가 dst_name 을 실어 보내면 병합 결과에 남는다
    (리뷰 결함 c) — relation_items 가 이 값을 이름 해석 1순위로 쓴다."""
    row = merge_relation(None, {"src": "005930", "dst": "042700",
                                "kind": "beneficiary", "reason": "r",
                                "dst_name": "한미반도체"},
                         score=60, today="2026-08-15")
    assert row["dst_name"] == "한미반도체"


def test_merge_omits_dst_name_when_candidate_lacks_it():
    """LLM 경로(cand 에 dst_name 없음)는 하위호환 — 키 자체가 안 생긴다."""
    row = merge_relation(None, {"src": "005930", "dst": "042700",
                                "kind": "beneficiary", "reason": "r"},
                         score=60, today="2026-08-15")
    assert "dst_name" not in row
