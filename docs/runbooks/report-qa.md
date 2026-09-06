# Runbook: 리포트 섹션별 QA 체크리스트

리포트·텔레그램·사이트 실전화 계획(2026-09-06) 3단계. `quant/report/lint.py`
(`lint_report()`)가 이 체크리스트의 상당 부분을 빌드 시점에 자동으로 검사한다 —
이 문서는 그 게이트가 **무엇을, 왜** 보는지, 그리고 게이트가 못 잡는(사람이
봐야 하는) 나머지를 정리한다.

## 게이트가 도는 위치

`quant/apps/report_cli.py`의 `_emit`/`_emit_close`가 `ReportModel`/
`CloseReportModel`을 다 채운 **직후, 렌더(`write_open_report`/
`write_close_report`) 직전**에 `lint_report(model)`을 부른다.

- **error 등급 하나라도 있으면 빌드를 멈춘다** — 예외를 던져 `report build`가
  0이 아닌 종료 코드로 끝나고, NOTIFY_LANE=ops로 첫 3건을 알린다(`run_report.sh`/
  `run_close_report.sh`의 기존 실패 알림과 별개로, 구체적 결함 내용까지 담아서).
  `run_report.sh`/`run_close_report.sh`의 실패 알림 본문에도 빌드 로그 마지막
  3줄이 함께 실린다(2026-09-07) — 대개 이 3줄 안에 방금 그 린트 오류 줄이
  그대로 들어 있어, 알림만 보고도 무엇이 걸렸는지 바로 알 수 있다.
- **warn 등급은 발행을 막지 않는다** — stdout/stderr에 찍히고
  `data/ledger/report_lint.jsonl`에 남는다. 사람이 나중에 훑어보는 용도.

## 오탐 대응 — 빠른 재발행 (`REPORT_LINT_GATE`, 2026-09-07)

게이트가 정상 리포트를 오탐으로 막았을 때(예: 새로 생긴 검사가 과거엔 없던
형태의 정상 데이터를 오해), 코드를 고쳐 재배포할 시간이 없는 그 순간 바로
발행을 재시도할 수 있는 우회 스위치다. `quant/apps/report_cli.py`의
`_lint_and_gate`가 환경변수 `REPORT_LINT_GATE`를 읽는다.

- **기본값(`block`, 미지정 포함)** — 위에서 설명한 기존 동작 그대로. error가
  있으면 빌드를 멈춘다.
- **`warn`** — error가 있어도 절대 빌드를 멈추지 않는다(발행이 계속된다).
  대신:
  - warn 등급과 함께 error 등급도 `data/ledger/report_lint.jsonl`에 남는다
    (평소엔 error가 원장에 안 남는다 — 빌드가 그 자리에서 멈추므로 텔레그램
    알림 하나로 충분했다; warn 모드는 멈추지 않으니 사후 감사를 위해 필요).
  - NOTIFY_LANE=ops 알림은 그대로 나간다 — 문구만 "발행 중단"→"발행
    계속(경고 모드)"로 바뀐다. **오탐인지 진짜 결함인지는 사람이 이 알림을
    보고 판단해야 한다** — warn 모드는 게이트를 끄는 것이지 결함이 사라지는
    것이 아니다.
  - 리포트 페이지·텔레그램 발행 요약 자체에는 별도 배너를 심지 않는다 —
    가시 경로는 위 텔레그램 알림 하나뿐이다.

**빠른 재발행 절차**:

```bash
REPORT_LINT_GATE=warn ./server/scripts/run_report.sh KR
# 마감 리포트라면:
REPORT_LINT_GATE=warn ./server/scripts/run_close_report.sh KR
```

`run_report.sh`/`run_close_report.sh`는 이 값을 읽거나 가공하지 않는다 —
`$PY -m quant.apps.report_cli build` 하위 프로세스가 부모 셸의 환경을 그대로
물려받으므로, 앞에 `REPORT_LINT_GATE=warn `를 붙이기만 하면 그대로 전달된다
(`tests/test_run_report_scripts.py::test_report_lint_gate_env_reaches_the_build_subprocess`
가 이 전달 경로를 고정한다). 재발행이 끝나면 **왜 그 검사가 오탐이었는지**를
`report_lint.jsonl`이나 텔레그램 알림에서 확인해 두는 것을 권한다 — 다음
정기 빌드는 다시 `block`(기본값)으로 돈다는 것도 기억한다(이 스위치는 그
한 번의 수동 호출에만 적용되는 임시 우회다, 크론에 영구히 심는 스위치가
아니다).

## 섹션별 체크리스트

| 섹션 | 무엇을 담아야 하나 | 누가 만드나 | 어떻게 검증하나 |
|---|---|---|---|
| **스탠스**(stance) | 항상 존재. `label`은 고정 문구 `"당일 스탠스(참고)"`(`quant.analyze.briefing.STANCE_LABEL`) — 방향을 라벨에 싣지 않는다. `tier`/`score100`는 서로 부호가 맞아야 한다. | `quant.analyze.briefing.stance()` (`quant/report/collect/core.py`가 호출) | 섹션 존재(error), 문구가 고정 문구인지(warn — 2026-09-06 이전 아카이브는 구 문구가 정상), `label_100(score100,...)`을 재계산해 `tier`와 일치하는지(error) |
| **후보**(candidates = `symbols`/`auto_watch`) | 거래일이면 `symbols`가 완전히 빈 리스트면 안 된다. 각 종목은 `name`이 있어야 한다(KR — US는 티커 자체가 표시명일 수 있어 제외, 아래 "알려진 예외" 참고). `relations` 항목은 전부 `reason`이 있어야 한다. NEWS 태그(매수 후보)가 붙은 종목은 `bearish_markers`(매수 유의 근거)가 있으면 안 된다. | `quant.analyze.mentions`(추출) → `quant.analyze.render.machine_payload`/`candidates_line`(조립) | 빈 리스트(error), 이름 없음(warn, KR만), relations reason 없음(error), NEWS+bearish_markers 동시 존재(error) |
| **섹터**(sectors) | 마감 리포트는 `market_flow` 섹션이 항상 있어야 한다. `ReportModel.sector_daily`(주도 섹터, KR 전용, 렌더 전용 필드라 `payload`만으로는 못 본다)는 `rank`가 거래대금(`turnover_krw`) 내림차순과 일치해야 한다. | `quant.report.collect.sector._build_sector_daily_view`/`_build_close_flow_view` | 마감 `market_flow` 없음(error), 섹터 rank·거래대금 역전/중복(error, `ReportModel` 경로만) |
| **중기 관심종목**(midterm_watch) | 각 항목에 `name`과 `reasons`(비어있지 않은 리스트)가 있어야 한다. | `quant.analyze.midterm_watch` | 이름/근거 없음(warn) |
| **단타 후보 / AI 심층 해석**(`intraday_view`/`agent_interpret_view`, `ReportModel` 전용) | 각 항목에 `name`과 근거(`grade_reasons`/`factors`/`reasons`/`prose` 중 하나)가 있어야 한다. | `quant.report.collect.intraday`/`agent_interpret` | 이름/근거 없음(warn, `ReportModel` 경로만 — payload만으로는 이 뷰 자체가 없다) |
| **리포트 정확도 박스**(report_accuracy) | 키가 있으면 `measured`/`stance_line`/`telegram_line`/`direction`/`candidates`/`ic` 전부 있어야 하고, 각 지평 비율은 [0,1] 안이어야 한다. | `quant.control.report_accuracy.report_summary` | 필수 키 누락(error), 비율 범위 밖(error) |
| **날짜/세션** | `generated_at`의 날짜가 `session_date`와 같아야 한다. 종목 `date`(전일 종가 기준일)가 세션일보다 미래면 안 되고, 7일 넘게 낡으면 의심스럽다. | `quant.collect.snapshot`/`quant.report.collect.core` | 날짜 불일치·미래 데이터(error), 낡은 시세(warn) |
| **텔레그램 요약** | HTML과 같은 payload에서 계산되므로 후보 수·상위 종목이 저절로 일치한다(별도 재구현 없음). "nan"/"None"/미렌더 Jinja가 새면 안 되고, 후보가 있는데 "0개"라고 말하면 안 된다. | `quant.report.render.telegram._format_summary`/`_format_close_summary` | 함수를 그대로 재호출해 누출 마커·"후보 있음인데 0개" 모순 검사(error) |
| **숫자 전반** | NaN/Inf 없음(error). `*_change_pct`류는 물리적으로 ±100% 안(error), ±50% 넘으면 확인 필요(warn). `*score100`은 0~100(error). `*_prob`은 [0,1](error). count류(기사수·언급수·연속일수 등)는 음수 불가(error). | 각 수집기(`quant/report/collect/*.py`) | 재귀 스캔(`_scan_leaks`/`_scan_ranges`) |

## 알려진 예외 (오탐이 아니다 — 이 목록에 없는 새 종류의 오탐을 보면 검사를 고치기 전에 여기 먼저 추가한다)

- **US 종목의 `name == symbol`은 정상이다.** S&P500 구성종목 표(Wikipedia)
  자체가 IBM/KKR/RTX/PVH/CRH/MSCI/CEVA/TPG/EQT/JOYY 같은 종목의 정식 표시명을
  티커 그대로 쓴다 — KR의 6자리 숫자 코드가 이름 없이 노출되는 것과는 다른
  문제라 이 검사는 KR 시장에만 적용한다.
- **`upside_pct`(목표주가 대비 상승여력)는 50%를 넘어도 정상이다.** 등락률이
  아니라 애널리스트 목표가 대비 괴리율이라 그날 수익률과 무관하게 큰 값을
  가질 수 있다 — `_pct` 접미사 필드 중 `change_pct`류만 등락률 범위 검사
  대상이다.
- **RANK 태그는 "매수 후보/매수 유의" 모순 검사 대상이 아니다.** `candidates_line`
  이 그날 랭킹 보드 편입(`ranking_bullish` 게이트)과 최근 거래대금 반복 편입
  (`volume_watch`, 게이트 없음)에 **같은 태그 이름을 재사용**해서, payload만
  보고는 어느 경로로 RANK가 붙었는지 구분할 수 없다 — 대신 NEWS 태그(매수
  후보)와 `bearish_markers`(매수 유의) 동시 존재를 본다(이건 같은 코드 경로
  안에서 나오는 값이라 오탐이 없다).
- **IPO 첫날 등의 물리적으로 큰 등락률은 error로 뜨는 게 의도된 동작이다.**
  KR 상장 첫날은 ±30% 가격제한폭이 적용되지 않아 100%+ 등락이 실제로 가능하다.
  그런 날은 error가 "발행을 막고 사람이 한 번 보라"는 신호로 정확히 작동한
  것이다 — 확인 후 그대로 발행하면 된다(코드를 고칠 결함이 아니다).
- **2026-09-06 이전 리포트의 `stance.label`이 방향 문구("중립"/"약한 상승
  신호" 등)인 것은 회귀가 아니다.** `STANCE_LABEL` 고정 문구 도입이 그날 커밋
  됐고, 그 이전 빌드는 옛 스키마를 그대로 쓴다 — warn 등급인 이유다.

## 게이트가 못 잡는 것 (사람이 봐야 한다)

- 산문(AI 심층 해석·중기 관심종목 전망·Executive Summary)의 **내용이 실제로
  맞는지** — 문법·형식만 본다. 사실 관계 검증은 `quant/control/report_review.py`
  (날짜별 회고, 1단계)와 `report_accuracy.py`(누적 채점) 몫이다.
- 뉴스 태그(호재/악재)의 **판정 자체가 옳은지** — `bearish_markers`가 비어
  있다고 해서 그 종목에 실제로 악재가 없다는 보장은 없다(추출 규칙의 recall
  한계, `report_review.py` 모듈 docstring).
- 사이트/텔레그램 **발송 자체의 성공 여부**(네트워크 실패, 봇 뮤트 등) —
  이건 4단계(텔레그램 발송 카탈로그) 영역이다.
