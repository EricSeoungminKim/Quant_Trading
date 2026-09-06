# 리포트 산문 근거 감사 — SUMMARY

대상: 62개 리포트(engine.json+report.html 쌍), 08-13~09-06 아카이브. 검사기: `quant.report.prose_check.check_prose` (a)~(e) + 하네스 자체 계산 (f) 템플릿 반복.

## 카테고리별 총계

| 카테고리 | 건수 |
|---|---|
| unsupported_number | 323 |
| direction_contradiction | 33 |
| unsupported_causality | 6 |
| template_rot(문장, 3일 이상 반복) | 0 |

## 날짜별 건수

| 날짜 | unsupported_number | direction_contradiction | unsupported_causality | 합계 |
|---|---|---|---|---|
| 08/17 KR | 4 | 0 | 0 | 4 |
| 08/18 KR | 13 | 0 | 0 | 13 |
| 08/18 US | 7 | 1 | 0 | 8 |
| 08/19 KR | 1 | 0 | 0 | 1 |
| 08/19 US | 8 | 0 | 0 | 8 |
| 08/20 KR | 13 | 4 | 0 | 17 |
| 08/20 US | 13 | 0 | 0 | 13 |
| 08/21 KR | 11 | 1 | 0 | 12 |
| 08/21 US | 7 | 1 | 0 | 8 |
| 08/22 KR | 2 | 1 | 0 | 3 |
| 08/22 US | 9 | 0 | 0 | 9 |
| 08/24 US | 12 | 3 | 1 | 16 |
| 08/25 KR_close | 1 | 0 | 0 | 1 |
| 08/25 KR | 19 | 2 | 1 | 22 |
| 08/25 US | 7 | 1 | 0 | 8 |
| 08/26 KR | 11 | 0 | 0 | 11 |
| 08/26 US | 5 | 1 | 0 | 6 |
| 08/27 KR_close | 2 | 0 | 0 | 2 |
| 08/27 KR | 6 | 0 | 0 | 6 |
| 08/27 US | 1 | 1 | 0 | 2 |
| 08/28 KR | 9 | 1 | 1 | 11 |
| 08/28 US | 4 | 0 | 0 | 4 |
| 08/29 KR_close | 3 | 0 | 0 | 3 |
| 08/29 KR | 7 | 0 | 0 | 7 |
| 08/29 US | 5 | 0 | 0 | 5 |
| 08/30 KR | 9 | 0 | 0 | 9 |
| 08/31 KR_close | 4 | 0 | 0 | 4 |
| 08/31 KR | 10 | 2 | 0 | 12 |
| 08/31 US | 6 | 1 | 0 | 7 |
| 09/01 KR_close | 3 | 0 | 0 | 3 |
| 09/01 KR | 5 | 0 | 0 | 5 |
| 09/02 KR | 19 | 1 | 1 | 21 |
| 09/02 US | 21 | 2 | 0 | 23 |
| 09/03 KR | 13 | 0 | 1 | 14 |
| 09/03 US | 1 | 1 | 1 | 3 |
| 09/04 KR_close | 3 | 0 | 0 | 3 |
| 09/04 KR | 13 | 3 | 0 | 16 |
| 09/04 US | 7 | 4 | 0 | 11 |
| 09/05 KR_close | 1 | 0 | 0 | 1 |
| 09/05 US | 9 | 2 | 0 | 11 |
| 09/06 KR | 19 | 0 | 0 | 19 |

## 카테고리별 예시 (최대 6건, 근거 인용 포함)

### unsupported_number
- **08/17 KR** `exec_summary.flow` [warn] — unsupported_number: 근거 없는 숫자 [2980.0] — '외국인이 30조3870억원 순매수하며 지수 상승을 주도한 반면 기관은 10조2980억원 순매도로 대응했다.'
- **08/17 KR** `exec_summary.flow` [warn] — unsupported_number: 근거 없는 숫자 [5000.0] — '한국경제는 외국인이 반도체 투톱을 중심으로 주간 6조5000억원 매수세를 기록했다고 보도했다.'
- **08/17 KR** `section_advice.sentiment` [warn] — unsupported_number: 근거 없는 숫자 [34.7] — '다만 AAII 개인 투자자 조사에서 약세 비중(37.9%)이 강세(34.7%)를 웃돌아 스프레드 -3.2%를 기록, 개인 심리는 여전히 신중한 편이다.'
- **08/17 KR** `section_advice.liquidity` [warn] — unsupported_number: 근거 없는 숫자 [-45237.0] — "순유동성이 -45,237억 원 감소했음에도 NFCI가 '완화'로 평가되고 장단기 금리차 0.48%로 수익률 곡선이 '평탄' 상태를 유지하고 있다."
- **08/18 KR** `exec_summary.market` [warn] — unsupported_number: 근거 없는 숫자 [7000.0] — '코스피는 +2.42% 상승하며 지수대가 7000선 문턱까지 올라섰고, 코스닥은 +0.38%로 상승폭이 상대적으로 제한됐다.'
- **08/18 KR** `exec_summary.flow` [warn] — unsupported_number: 근거 없는 숫자 [387.0, 298.0] — '이날 외국인은 3조387억원 순매수한 반면 기관은 1조298억원 순매도로 방향이 엇갈렸다.'

### direction_contradiction
- **08/18 US** `midterm_watch[NVDA]` [error] — direction_contradiction: change_pct=-0.06%(하락)인데 문장은 상승 어조 — '다만 전력 및 냉각 인프라 제약, HBM4e 가격 상승 가능성, 그리고 CoWoS 생산 capacity 한계가 중장기 성장 속도를 다소 제한할 수 있으므로, 이러한 구조적 요인들을 함께 고려하면서 투자 판단을 내려야 합니다.'
- **08/20 KR** `midterm_watch[005940]` [error] — direction_contradiction: 외국인 실제 수급은 순매수(+17,079)인데 문장은 '외국인 순매도' 주장 — '자료상 NH투자증권 하락은 회사 개별 악재가 아니라 증권 테마 전체가 밀린 결과입니다 — 미 국채금리 19년 만의 최고치 경신과 반도체·기술주 급락으로 코스피가 위험회피에 들어갔고, 외국인·기관이 코스피에서 각각 2조8천억·1조3천억 가까이 순매도하면서 키움·미래에셋·삼성증권과 함...'
- **08/20 KR** `midterm_watch[007340]` [error] — direction_contradiction: 외국인 실제 수급은 순매수(+17,079)인데 문장은 '외국인 순매도' 주장 — '즉 실적이라는 개별 재료는 살아 있으나, 외국인·기관 동반 순매도와 중동 지정학 리스크 같은 시장 전체 변수가 당장은 주가를 좌우하는 국면이라 변동성이 클 수 있습니다.'
- **08/20 KR** `midterm_watch[006400]` [error] — direction_contradiction: change_pct=-0.20%(하락)인데 문장은 상승 어조 — '전공지사항으로, 오늘 장세의 강세 축은 반도체·금융·바이오 쪽에 몰려 있어 2차전지가 시장 주도 테마는 아니었다는 점도 같이 보시는 게 좋습니다.'
- **08/20 KR** `midterm_watch[000660]` [error] — direction_contradiction: change_pct=-9.75%(하락)인데 문장은 상승 어조 — '이런 흐름이 HBM 수요 기대로 직결되며 SK하이닉스가 5일 연속 상승, ADR도 +3.4%를 기록한 만큼 당분간은 메모리 업종의 모멘텀이 살아 있다고 보는 게 자연스럽습니다.'
- **08/21 KR** `midterm_watch[035720]` [error] — direction_contradiction: 외국인 실제 수급은 순매수(+17,068)인데 문장은 '외국인 순매도' 주장 — '최근 텔레그램 자료에 따르면 코스닥 시장에서 외국인과 기관의 순매도가 지속되며 소프트웨어 업종 전반에 차익실현 매물이 출회돼 카카오도 약\u202f‑4.5% 정도 하락한 모습을 보였다.'

### unsupported_causality
- **08/24 US** `exec_summary.catalyst` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — '국제 유가는 호르무즈 해협을 조용히 통과하며 공급 우려가 완화됐다.'
- **08/25 KR** `exec_summary.catalyst` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — '미국발 재료로는 미 재무부의 TGA 활용 국채 바이백 검토에 따른 장기 금리 하락과, 엔비디아의 AI 칩 가격 인상 통보에 따른 반도체 투매·AI 비용 부담 우려가 겹치며 뉴욕증시가 혼조 마감한 점이 제시됐다.'
- **08/28 KR** `exec_summary.market` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — '매일경제 보도에 따르면 엔비디아 실적과 금리 인상 우려가 맞물리며 코스피가 상승 마감했다.'
- **09/02 KR** `exec_summary.catalyst` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — "국내에서는 ETF가 소수 대장주에 집중 투자하는 '초집중형' 상품으로 재편되며 자금 쏠림이 심화됐고, 이는 하락 국면에서 증시 충격을 키울 수 있다는 우려가 제기됐다(한국경제)."
- **09/03 KR** `exec_summary.catalyst` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — '미국 금리 인상 우려가 겹쳐 외국인과 기관의 대규모 매도세가 이어졌다.'
- **09/03 US** `agent_interpret_view[EU]` [warn] — unsupported_causality: 인과 표현에 근거(숫자·뉴스·이유) 인용 없음 — '이 종목이 후보로 올랐는 이유는 주로 Jefferies의 긍정적 평가와 원자력 산업에 대한 기대감, 그리고 텔레그램에서의 논의로 보입니다.'

## template_rot — 3일 이상 반복된 동일 문장 (상위 15건)
