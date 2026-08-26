"""소식통 배달 점검 — "대표님에게 실제로 닿았는가"만 본다. 2026-08-26, 소유자
조직도 역할 6.

## 왜 ops_watch(매시)와 다른가

`quant.control.health`/`ops_watch.sh`는 매시간 **시스템 이상**(낡은 봉, 죽은
피드, 원장 불일치)을 본다. 이 모듈은 하루 한 바퀴(KR+US 정규장이 다 돈 뒤)
**그날의 산출물이 실제로 나갔는가**만 본다 — 시스템은 멀쩡한데 크론이
빠지거나(systemd 유닛 정지), 리포트가 비정상 종료해 산출물만 없는 경우가
health의 시야 밖이다.

## 날짜 계산이 "오늘"이 아니다

이 점검은 US 마감 정산 뒤(크론 제안: 화~토 06:35 KST)에 한 번 돈다. 그 시각의
"오늘"(KST)은 KR 아침 리포트(07:30경)조차 아직 나오지 않은 시점이라, KR/US
아침·마감 산출물을 "오늘" 기준으로 찾으면 매일 오탐한다. 실제로 완결된 것은
**전날**의 KR+US 사이클이다(KR 아침·마감 리포트는 전날 아침/오후에, US 리포트·
브리핑은 전날 저녁에 나갔고, 그 저녁에 시작된 US 세션이 오늘 새벽에 끝나
05:50 오늘 날짜로 US_wrap.json 이 나온다 — `quant.report.collect.uswrap.
write_us_wrap`이 실행 시점 날짜를 그대로 쓴다). 그래서:

- `KR_report.html`/`KR_engine.json`/`KR_close_engine.json`/`US_report.html`
  → **전날**(오늘-1) 기준. 전날이 평일(월~금)이 아니면 통째로 건너뛴다.
- `US_wrap.json` → **오늘** 기준. (전날이 평일이면 오늘은 자동으로 화~토가
  된다 — crontab의 `2-6` 요일 제한과 같은 산수.)

공휴일(수능일 등 개장은 하되 리포트가 정상적으로 비는 날)까지는 판정하지
않는다 — **달력 요일 판정만**(휴장일 오탐은 받아들이는 트레이드오프,
docstring에 명시).

## 상태는 셋이다 (health.py 관례와 동일)

`missing`(확인했고 없다 — 미배달) / `unknown`(확인할 수 없다 — 로그 파일
자체가 없거나 못 읽음) / 아무것도(정상, 침묵). **`unknown`을 정상으로
합산하지 않는다** — "로그가 없다"와 "로그를 봤는데 발송 흔적이 없다"는 다른
사실이다.

## 텔레그램 "발송 성공" 흔적의 정직한 한계

`own_brief.sh`/`run_report.sh`/`ai_trader.sh`는 셸에서 `curl -s ... || true`로
직접 전송한다 — 실패를 삼키므로 **셸 로그만으로는 "성공"을 증명할 수 없다**
(`data/ledger/notifications.jsonl`은 `quant.adapters.notify.telegram.
TelegramNotifier`를 통과하는 파이썬 경로만 기록하고, 이 셸 스크립트들은 그
경로를 타지 않는다 — 2026-08-26 확인). 그래서 여기서는 "발송 직전에 반드시
찍히는 로그 줄"(각 스크립트의 `log()`/`echo` 호출 지점을 코드에서 직접 확인한
것, 추측 아님)의 존재로 "발송 시도가 있었다"를 판정한다. 실제 curl 실패까지
잡고 싶으면 셸 스크립트들을 이 파이썬 어댑터 경유로 옮겨야 한다(범위 밖).

## 순수하다

전부 **이미 읽어온 데이터**를 받는다(health.py와 같은 규칙) — 파일 존재 여부·
로그 줄은 `quant.apps.cli`가 읽어서 넘긴다. 그래야 실패 상황(로그 없음, 파일
없음)을 전부 테스트로 재현할 수 있다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

MISSING = "missing"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    check: str
    level: str  # MISSING | UNKNOWN — 정상이면 만들지 않는다
    detail: str

    def to_dict(self) -> dict:
        return {"check": self.check, "level": self.level, "detail": self.detail}


@dataclass(frozen=True)
class ArtifactStatus:
    """산출물 파일 하나의 이미 읽어온 상태."""
    exists: bool
    size: int


def is_weekday(d: date) -> bool:
    return d.isoweekday() <= 5


def expected_artifacts(today: date) -> dict[str, date]:
    """오늘(KST) 이 점검 실행일일 때, 확인할 산출물 이름 → 기준 날짜.

    기준 날짜가 평일이 아닌 항목은 아예 뺀다(휴장일/주말 오탐 방지 — 달력
    요일 판정만, 공휴일까지는 안 본다). 반환이 빈 dict 면 오늘은 점검할 게
    없다는 뜻(예: 점검 스크립트를 월요일에 수동 실행한 경우 — 전날이 일요일)."""
    prior = today - timedelta(days=1)
    out: dict[str, date] = {}
    if is_weekday(prior):
        out["KR_report.html"] = prior
        out["KR_engine.json"] = prior
        out["KR_close_engine.json"] = prior
        out["US_report.html"] = prior
        out["US_wrap.json"] = today
    return out


def check_artifacts(statuses: dict[str, ArtifactStatus]) -> list[Finding]:
    """`expected_artifacts`가 고른 항목마다 이미 읽어온 존재/크기를 판정한다."""
    findings: list[Finding] = []
    for name, st in statuses.items():
        if not st.exists:
            findings.append(Finding(check=name, level=MISSING, detail=f"{name} 없음"))
        elif st.size <= 0:
            findings.append(Finding(check=name, level=MISSING, detail=f"{name} 크기 0"))
    return findings


# 발송 직전에 반드시 찍히는 로그 줄 — 각 스크립트 소스에서 직접 확인(추측 아님).
#   own_brief.sh: 리포트를 읽고 나면 항상 `log "리포트 rc=... 후보: ..."`를
#     찍은 **뒤** tg() 를 부른다(성공/실패 케이스 전부, TZ 가드/데드라인 초과로
#     조기 종료한 게 아닌 한). 실제 로그는 own_brief.sh 내부 LOG 변수
#     (`data/own_brief.log`)로 간다 — crontab이 리다이렉트하는 `data/brief.log`
#     (KR)/`data/us_discover.log`(US)는 own_brief.sh의 내부 log()를 거치지
#     않는 stdout/stderr만 받아 정상 실행 시 거의 비어 있다(own_brief.sh
#     읽고 확인 — 2026-08-26).
#   run_report.sh: 빌드 성공 시 `log "빌드 완료 (...)"` 다음 줄에서 `notify ok`
#     (텔레그램 전송)를 부른다. `data/report.log`.
#   ai_trader.sh: "조용한 게 기본값"(결근/픽 없음은 정상, 텔레그램도 정상적으로
#     안 나간다) — 그래서 텔레그램 발송 자체가 아니라 **그날 잡이 살아
#     있었는가**만 본다. 실패 종료(`실패 exit=`)만 미배달로 취급한다.
LOG_CHECKS: dict[str, dict] = {
    "own_brief_KR": {"path": "data/own_brief.log", "market_prefix": "[KR] ", "needle": "리포트 rc="},
    "own_brief_US": {"path": "data/own_brief.log", "market_prefix": "[US] ", "needle": "리포트 rc="},
    "run_report_KR": {"path": "data/report.log", "market_prefix": "[KR] ", "needle": "빌드 완료"},
    "run_report_US": {"path": "data/report.log", "market_prefix": "[US] ", "needle": "빌드 완료"},
}

# ai_trader.sh 는 다른 형식이다("[TS] $MARKET ...", 브라켓 없음) — 별도 취급.
AI_TRADER_LOG_PATH = "data/ai_trader.log"


def check_log_traces(logs: dict[str, list[str] | None], target_date: date) -> list[Finding]:
    """`LOG_CHECKS`에 정의된 각 항목을 판정한다.

    `logs[name]`이 `None`이면(파일 자체가 없거나 못 읽음) UNKNOWN. 파일은
    읽었는데 대상 날짜 + 필요한 접두어/문구를 가진 줄이 하나도 없으면
    MISSING(로그는 있는데 발송 시도 흔적이 없다)."""
    date_str = target_date.isoformat()
    findings: list[Finding] = []
    for name, spec in LOG_CHECKS.items():
        lines = logs.get(name)
        if lines is None:
            findings.append(Finding(
                check=name, level=UNKNOWN,
                detail=f"{spec['path']} 없음/읽기 실패 — 확인 못 함",
            ))
            continue
        needle = spec["market_prefix"] + spec["needle"]
        if not any(date_str in ln and needle in ln for ln in lines):
            findings.append(Finding(
                check=name, level=MISSING,
                detail=f"{spec['path']}: {date_str} {spec['market_prefix'].strip()} 발송 시도 흔적 없음",
            ))
    return findings


def check_ai_trader(lines: list[str] | None, market: str, target_date: date) -> Finding | None:
    """ai_trader.sh는 "조용한 게 기본값"(픽 없음/결근은 정상) — 그래서
    실패(`$MARKET 실패 exit=`)만 미배달로 본다. 그날 그 시장 줄이 아예 없으면
    잡 자체가 안 돈 것이므로 이것도 미배달로 본다(내용이 없더라도 스크립트는
    항상 최소 한 줄은 echo 한다 — 소스 확인)."""
    date_str = target_date.isoformat()
    if lines is None:
        return Finding(
            check=f"ai_trader_{market}", level=UNKNOWN,
            detail=f"{AI_TRADER_LOG_PATH} 없음/읽기 실패 — 확인 못 함",
        )
    marker = f"] {market} "
    today_lines = [ln for ln in lines if date_str in ln and marker in ln]
    if not today_lines:
        return Finding(
            check=f"ai_trader_{market}", level=MISSING,
            detail=f"{AI_TRADER_LOG_PATH}: {date_str} {market} 잡 실행 흔적 없음",
        )
    if any("실패 exit=" in ln for ln in today_lines):
        return Finding(
            check=f"ai_trader_{market}", level=MISSING,
            detail=f"{AI_TRADER_LOG_PATH}: {date_str} {market} 실패 종료",
        )
    return None
