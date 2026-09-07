"""리포트 실전 품질 게이트 — 순수 함수, 파일 I/O 없음.

리포트·텔레그램·사이트 실전화 계획(2026-09-06) 3단계. `lint_report()`는
`ReportModel`/`CloseReportModel` 인스턴스 하나, 또는 그 `payload`(=engine.json/
close_engine.json 그대로)만 보고 `list[Finding]`을 낸다 — 원장 기록·텔레그램
알림·exit code 판정은 전부 호출부(`quant/apps/report_cli.py`) 몫이다(다른
`quant.control.*` 순수 계산 모듈과 같은 관례: I/O는 셸/CLI 경계에서만).

## 왜 필요한가

`quant/control/report_review.py`(1단계)와 `report_accuracy.py`는 **과거** 리포트가
맞았는지를 사후에 잰다. 이 모듈은 그보다 앞 단계 — **발행 직전**에 리포트 자체가
내부적으로 말이 되는지를 본다: NaN이 그대로 노출되지 않는지, 스탠스 라벨과
점수 부호가 서로 다른 말을 하지 않는지, 언급된 종목마다 이름과 근거가 붙어
있는지. 실측(2026-09 첫 주 engine.json 5건 감사, `docs/runbooks/report-qa.md`
참고)에서 이 검사들이 실제로 걸러낸 것: 2026-09-06 이전 리포트는 전부
`stance.label`이 옛 방향 문구("중립"/"약한 상승 신호" 등)를 그대로 쓰고 있었다
(`STANCE_LABEL` 도입 전 — 회귀가 아니라 롤아웃 이전 데이터).

## 어떻게 쓰는가

- `payload`만(과거 engine.json 파일 등)로 부르면 그 payload 안에서 계산 가능한
  것만 본다 — `sector_daily`처럼 payload에 없는 필드(렌더 전용, `ReportModel`
  에만 있다)를 요구하는 검사는 조용히 건너뛴다(과거 아카이브를 오탐으로
  채우지 않는다).
- `ReportModel`/`CloseReportModel`로 부르면(정상 빌드 경로, `report_cli._emit`/
  `_emit_close`) 추가로 렌더 전용 필드(`sector_daily`, `intraday_view`,
  `midterm_view`, `channel_digest` 등)까지 본다.

## 심각도

- `error` — 빌드를 막아야 하는 결함(NaN 누출, 물리적으로 불가능한 값, 방향
  모순, 필수 섹션 완전 공백). 호출부가 이 등급을 하나라도 보면 발행을 멈추고
  NOTIFY_LANE=ops 로 알린다.
- `warn` — 발행은 막지 않지만 사람이 봐야 하는 것(옛 문구, 이름 없는 종목,
  후보 0건인데 정상적일 수도 있는 경우). `data/ledger/report_lint.jsonl`에
  남는다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime

from quant.analyze.briefing import STANCE_LABEL
from quant.analyze.scoring import label_100
from quant.report.model import CloseReportModel, ReportModel
from quant.report.prose_check import check_prose

ERROR = "error"
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    severity: str  # "error" | "warn"
    section: str
    message: str

    def __str__(self) -> str:  # pragma: no cover — 사람이 읽는 편의 포맷
        return f"[{self.severity}] {self.section}: {self.message}"


# ──────────────────────────────────────────────────────────── 문자열/숫자 누출

# 문자열 값 안에 이미 새어든 것들 — 렌더된 뒤가 아니라 렌더 *전* 모델 단계에서
# 잡는다("nan%"는 float('nan')을 Jinja `{{ x }}%`에 넣은 결과다). 실제 Python
# `None`(JSON null)은 이 저장소 전반의 결측 계약("0/None으로 위장하지 않는다")
# 이라 값 자체는 검사하지 않는다 — 문자열 *안에* 그 단어가 새어든 경우만 본다
# (예: f"{x}"가 이미 None인 x를 문장에 그대로 박아버린 f-string 버그).
_LEAK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bnan\b", re.IGNORECASE), "nan"),
    (re.compile(r"\bNone\b"), "None"),
    (re.compile(r"\bundefined\b", re.IGNORECASE), "undefined"),
    # 여는 델리미터(`{{`/`{%`)만 본다 — 닫는 쪽(`}}`/`%}`)은 리포트에 그대로
    # 박히는 CSS(중첩 블록이 연달아 닫히면 "}}"가 정상적으로 나온다)와
    # 구분이 안 된다(2026-09-06 골든 테스트 1차 작성 중 실측: `--shadow:...;}}`
    # 처럼 CSS 블록 경계에서 오탐). Jinja는 `{{`/`{%`를 렌더 중 항상 소비하므로
    # 정상 출력엔 절대 남지 않는다 — 반대로 닫는 쪽 없이 여는 쪽만 봐도 놓치지
    # 않는다.
    (re.compile(r"\{\{|\{%"), "미렌더 Jinja 마커"),
]

_MAX_LIST_SCAN = 500  # 병리적으로 큰 리스트(수천 건 뉴스 등)에서 스캔 비용 상한


def _scan_leaks(section: str, obj: object, path: str = "") -> list[Finding]:
    """payload/모델 어디든 재귀로 훑어 NaN/Inf 숫자, 'nan'/'None'/'undefined'/
    미렌더 Jinja 마커가 섞인 문자열을 찾는다."""
    out: list[Finding] = []
    if isinstance(obj, bool):
        return out
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            out.append(Finding(ERROR, section, f"{path or '(root)'}: 숫자 필드가 NaN/Inf ({obj!r})"))
    elif isinstance(obj, str):
        for pattern, label in _LEAK_PATTERNS:
            if pattern.search(obj):
                snippet = obj if len(obj) <= 120 else obj[:117] + "..."
                out.append(Finding(ERROR, section, f"{path or '(root)'}: 문자열에 '{label}' 누출 — {snippet!r}"))
                break  # 같은 문자열에서 여러 패턴이 걸려도 한 건만 보고한다
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_scan_leaks(section, v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:_MAX_LIST_SCAN]):
            out.extend(_scan_leaks(section, v, f"{path}[{i}]"))
    return out


# ──────────────────────────────────────────────────────────── 숫자 범위

_SCORE100_KEY = re.compile(r"score100$", re.IGNORECASE)
# 등락률(=수익률) 필드만 좁게 잡는다 — `upside_pct`(목표주가 대비 상승여력,
# 정상적으로 50%를 훌쩍 넘는다)처럼 이름은 `_pct`로 끝나지만 "그날 수익률"이
# 아닌 필드까지 잡으면 실측 데이터의 절반이 오탐으로 뜬다(2026-09 첫 주
# engine.json 5건 감사에서 실제로 발생 — `docs/runbooks/report-qa.md` 참고).
_PCT_KEY = re.compile(r"change_pct$", re.IGNORECASE)
_PROB_KEY = re.compile(r"(^|_)prob$", re.IGNORECASE)
# 끝이 아니라 어디든 포함되면 카운트로 본다 — 실제 필드명이 접미사가 아닌
# 경우가 있다(예: `news_articles_today`는 "articles"가 중간에 온다).
_COUNT_KEY = re.compile(
    r"(count|articles|mentions|outlets|streak|samples|n_claims|risk_items|days)",
    re.IGNORECASE,
)


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (
        isinstance(v, float) and math.isnan(v)
    )


def _scan_ranges(section: str, obj: object, path: str = "") -> list[Finding]:
    """키 이름 패턴으로 알려진 세 부류를 범위 검사한다 — 등락률(±50% 근방),
    score100(0~100), 카운트(>=0). NaN/Inf 자체는 `_scan_leaks`가 이미 잡으므로
    여기선 범위만 본다."""
    out: list[Finding] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key_path = f"{path}.{k}" if path else str(k)
            if _is_number(v):
                if _SCORE100_KEY.search(k):
                    if not (0 <= v <= 100):
                        out.append(Finding(ERROR, section, f"{key_path}: score100 범위 밖(0~100) — {v}"))
                elif _PCT_KEY.search(k) or k == "change_pct":
                    # -100% 미만은 불가능. 위쪽은 KR 신규상장 첫날이 공모가의
                    # 60~400%(2023-06-26 개편)라 +300%까지 실제로 나온다 —
                    # 2026-09-05 KR 마감 리포트의 386380(+135%)이 그 예. 그래서
                    # +400% 초과만 오류, 그 아래는 경고(확인 필요)로 둔다 — 정상
                    # 데이터가 발행을 막으면 안 된다.
                    if v < -100 or v > 400:
                        out.append(Finding(ERROR, section, f"{key_path}: 물리적으로 불가능한 등락률 — {v:+.2f}%"))
                    elif abs(v) > 50:
                        out.append(Finding(WARN, section, f"{key_path}: 등락률이 ±50%를 넘음 — {v:+.2f}% (확인 필요)"))
                elif _PROB_KEY.search(k) or k == "prob":
                    if not (-1e-9 <= v <= 1 + 1e-9):
                        out.append(Finding(ERROR, section, f"{key_path}: 확률이 [0,1] 밖 — {v}"))
                elif _COUNT_KEY.search(k):
                    if v < 0:
                        out.append(Finding(ERROR, section, f"{key_path}: 카운트가 음수 — {v}"))
            out.extend(_scan_ranges(section, v, key_path))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj[:_MAX_LIST_SCAN]):
            out.extend(_scan_ranges(section, v, f"{path}[{i}]"))
    return out


# ──────────────────────────────────────────────────────────── 날짜/세션

def _lint_dates(payload: dict) -> list[Finding]:
    out: list[Finding] = []
    session_date_str = payload.get("session_date")
    generated_at_str = payload.get("generated_at")
    if not session_date_str or not generated_at_str:
        return out
    try:
        session_date = date.fromisoformat(session_date_str)
        generated_at = datetime.fromisoformat(generated_at_str)
    except ValueError as e:
        out.append(Finding(ERROR, "dates", f"session_date/generated_at 파싱 실패: {e}"))
        return out
    if generated_at.date() != session_date:
        out.append(Finding(
            ERROR, "dates",
            f"generated_at({generated_at.date()})와 session_date({session_date}) 날짜 불일치",
        ))
    for sym in payload.get("symbols") or []:
        d = sym.get("date")
        if not d:
            continue
        try:
            sym_date = date.fromisoformat(d)
        except ValueError:
            continue
        if sym_date > session_date:
            out.append(Finding(
                ERROR, "dates",
                f"{sym.get('symbol')}: 종가 기준일({d})이 세션일({session_date})보다 미래 — 데이터 오염 의심",
            ))
        elif (session_date - sym_date).days > 7:
            out.append(Finding(
                WARN, "dates",
                f"{sym.get('symbol')}: 종가 기준일({d})이 세션일({session_date})보다 "
                f"{(session_date - sym_date).days}일 이전 — 낡은 시세일 수 있음",
            ))
    return out


# ──────────────────────────────────────────────────────────── 필수 섹션

def _auto_watch_count(auto_watch: object) -> int:
    body = str(auto_watch or "").removeprefix("AUTO_WATCH:").strip()
    if not body or body == "없음":
        return 0
    return len(body.split())


def _lint_required_sections(payload: dict) -> list[Finding]:
    out: list[Finding] = []
    is_close = payload.get("session") == "close"
    if is_close:
        if not payload.get("intraday_view") and not payload.get("close_bet_view"):
            out.append(Finding(WARN, "candidates", "마감 리포트에 단타/종가배팅 후보가 모두 없음"))
        if "market_flow" not in payload:
            out.append(Finding(ERROR, "sectors", "마감 리포트에 market_flow 섹션 자체가 없음"))
    else:
        if "stance" not in payload or not payload.get("stance"):
            out.append(Finding(ERROR, "stance", "stance 섹션이 없음 — 스탠스는 항상 결정론 계산되는 값이다"))
        if payload.get("symbols") == []:
            out.append(Finding(ERROR, "candidates", "symbols 리스트가 완전히 비어 있음 — 거래일이면 이상 신호"))
        if "auto_watch" in payload and _auto_watch_count(payload["auto_watch"]) == 0:
            out.append(Finding(WARN, "candidates", "AUTO_WATCH 후보 0건"))
    return out


# ──────────────────────────────────────────────────────────── 스탠스 문구/부호

def _lint_stance_wording(payload: dict) -> list[Finding]:
    stance = payload.get("stance")
    if not isinstance(stance, dict) or not stance:
        return []
    label = stance.get("label")
    if label != STANCE_LABEL:
        return [Finding(
            WARN, "stance",
            f"stance.label이 정직한 고정 문구가 아님: {label!r} (기대: {STANCE_LABEL!r}) — "
            "2026-09-06 이전 아카이브는 예상된 결과",
        )]
    return []


def _polarity_check(
    section: str, obj: dict, score100_key: str, label_key: str,
    positive: str, negative: str, tier_key: str | None = None,
) -> list[Finding]:
    """`quant.analyze.scoring.label_100`이 만드는 라벨을 score100으로부터 그대로
    재계산해 실제 라벨과 비교한다 — 키워드 추측이 아니라 생성 함수 자체를
    재적용하므로 오탐이 없다. `tier_key`가 있고 값이 있으면 그쪽(신규 스탠스
    스키마)을, 없으면 `label_key`(구 스키마·index_outlook·종목별 라벨처럼
    tier가 아예 없는 뷰)를 본다."""
    if not isinstance(obj, dict):
        return []
    score100 = obj.get(score100_key)
    if not _is_number(score100):
        return []
    observed = obj.get(tier_key) if tier_key and obj.get(tier_key) is not None else obj.get(label_key)
    if observed is None:
        return []
    expected = label_100(round(score100), positive, negative)
    if observed != expected:
        return [Finding(
            ERROR, section,
            f"{score100_key}={score100}의 기대 라벨은 {expected!r}인데 실제 {label_key}={observed!r} — 방향 모순",
        )]
    return []


def _lint_score_polarity(payload: dict) -> list[Finding]:
    out: list[Finding] = []
    out.extend(_polarity_check(
        "stance", payload.get("stance") or {}, "score100", "label",
        "상승 신호", "하락 신호", tier_key="tier",
    ))
    for idx_name, idx in (payload.get("index_outlook") or {}).items():
        out.extend(_polarity_check(
            f"index_outlook.{idx_name}", idx or {}, "score100", "label", "상승 신호", "하락 신호",
        ))
    for sym in payload.get("symbols") or []:
        code = sym.get("symbol", "?")
        out.extend(_polarity_check(
            f"symbols[{code}]", sym, "ai_score100", "ai_label", "긍정 신호", "부정 신호",
        ))
        out.extend(_polarity_check(
            f"symbols[{code}]", sym, "trending_score100", "trending_label", "트렌딩", "관심 저조",
        ))
    return out


# ──────────────────────────────────────────────────────────── 매수 후보 vs 매수 유의

def _parse_auto_watch(auto_watch: object) -> dict[str, set[str]]:
    body = str(auto_watch or "").removeprefix("AUTO_WATCH:").strip()
    if not body or body == "없음":
        return {}
    out: dict[str, set[str]] = {}
    for token in body.split():
        if ":" not in token:
            continue
        sym, _, tags = token.partition(":")
        out[sym] = set(tags.split("+"))
    return out


def _lint_auto_watch_contradiction(payload: dict) -> list[Finding]:
    """"매수 후보"(AUTO_WATCH에 NEWS로 편입 — 오늘 호재로 후보에 오른 것)와
    "매수 유의"(그 종목 자체의 `bearish_markers` — 목표가 하향·어닝쇼크 등
    명백한 악재 표지)가 같은 종목에 동시에 붙으면 모순이다.

    `quant.analyze.render.candidates_line`은 `today_articles>0 and not
    bearish_markers(c)`일 때만 NEWS 태그를 준다(render.py) — 그러니 정상
    경로에서는 이 함수가 항상 빈 리스트를 내야 한다. 이게 걸리면 태그 생성과
    `bearish_markers` 계산 사이 어딘가 리팩터링으로 어긋난 것이다(회귀 감지용).

    **버렸던 대안**: 처음엔 `ranking_bullish=False`인데 RANK 태그가 붙은
    경우를 봤지만, RANK는 그날 랭킹 보드 편입(`ranking_bullish` 게이트)
    말고도 `volume_watch`/전일 KR 패턴/점수 연속강세 합류 경로가 **같은
    태그 이름을 재사용**한다(candidates_line docstring: "새 태그를 만들지
    않고 기존 RANK 를 재사용한다") — payload만 보고는 어느 경로로 RANK가
    붙었는지 구분할 수 없어 2026-09 첫 주 engine.json 5건 중 5건 모두에서
    오탐이 났다(`docs/runbooks/report-qa.md`). 그래서 뺐다.
    """
    tokens = _parse_auto_watch(payload.get("auto_watch"))
    if not tokens:
        return []
    out: list[Finding] = []
    for sym in payload.get("symbols") or []:
        code = sym.get("symbol")
        tags = tokens.get(code)
        if not tags:
            continue
        if "NEWS" in tags and sym.get("bearish_markers"):
            out.append(Finding(
                ERROR, "candidates",
                f"{sym.get('name') or code}({code}): 악재 표지({sym['bearish_markers']})가 있는데 "
                "AUTO_WATCH에 NEWS(매수 후보)로 편입 — 매수 후보/매수 유의 모순",
            ))
    return out


# ──────────────────────────────────────────────────────────── 종목명·근거 완전성

def _lint_symbol_completeness(payload: dict) -> list[Finding]:
    # 이름=코드 검사는 KR 전용이다. US는 S&P500 원표(Wikipedia 구성종목 표)
    # 자체가 "IBM"/"KKR"/"RTX"처럼 티커를 그대로 정식 표시명으로 쓰는 종목이
    # 흔해(2026-09 첫 주 engine.json 감사에서 US 리포트 5건 전부가 이걸로
    # 오탐 — 티커 자체가 이미 읽을 수 있는 식별자다) — KR 6자리 숫자 코드가
    # 이름 없이 노출되는 것과는 다른 문제다.
    is_kr = payload.get("market") == "KR"
    out: list[Finding] = []
    for sym in payload.get("symbols") or []:
        code = sym.get("symbol")
        name = sym.get("name")
        if is_kr and (not name or name == code):
            out.append(Finding(WARN, "candidates", f"{code}: 종목명 없음(코드 그대로 노출)"))
        for rel in sym.get("relations") or []:
            if not rel.get("reason"):
                out.append(Finding(
                    ERROR, "candidates",
                    f"{code}: relations 항목({rel.get('symbol')})에 reason 없음",
                ))
    for item in payload.get("midterm_watch") or []:
        code = item.get("symbol")
        if not item.get("name"):
            out.append(Finding(WARN, "candidates", f"{code}: 중기 관심종목에 이름 없음"))
        if not item.get("reasons"):
            out.append(Finding(WARN, "candidates", f"{code}: 중기 관심종목에 근거(reasons) 없음"))
    return out


def lint_trade_review_symbols(groups: list[dict] | None) -> list[Finding]:
    """매매 리뷰 카드의 종목명 완비성(2026-09-07) — `quant.control.trade_review.
    build_trade_review()`의 `groups`와 `quant.control.daily_wrap._trade_review_card()`
    출력(7절 카드) 둘 다 `symbol`/`name`/`market` 키를 공유해 이 함수 하나로
    본다. `_lint_symbol_completeness`(engine.json `payload["symbols"]`)와 같은
    규율 — KR은 이름 없음/이름=코드가 WARN, US는 이름이 아예 없을 때만 WARN
    (S&P500 원표 자체가 티커를 정식 표시명으로 쓰는 종목이 흔하다, 위 주석
    참고)."""
    out: list[Finding] = []
    for g in groups or []:
        code, name, market = g.get("symbol"), g.get("name"), g.get("market")
        # KR: 이름 없음 또는 이름=코드 둘 다 오탐. US: 이름이 아예 없을 때만
        # (S&P500 원표가 IBM 같은 티커=정식명 종목을 흔히 쓴다, 위 주석 참고).
        missing = (not name or name == code) if market == "KR" else (market == "US" and not name)
        if missing:
            out.append(Finding(WARN, "trade_review", f"{code}: 종목명 없음(코드 그대로 노출)"))
    return out


def _lint_view_completeness(section: str, items: list[dict] | None) -> list[Finding]:
    """`intraday_view`/`agent_interpret_view`처럼 `ReportModel`에만 있는 뷰용 —
    payload에는 이 리스트 자체가 없으므로 model 경로에서만 호출된다."""
    out: list[Finding] = []
    for item in items or []:
        code = item.get("symbol")
        if not item.get("name"):
            out.append(Finding(WARN, section, f"{code}: 이름 없음"))
        has_reason = item.get("grade_reasons") or item.get("factors") or item.get("reasons") or item.get("prose")
        if not has_reason:
            out.append(Finding(WARN, section, f"{code}: 근거 없음"))
    return out


# ──────────────────────────────────────────────────────────── 섹터 rank vs 점수 순서

def _lint_sector_daily_order(sector_daily: dict | None) -> list[Finding]:
    if not isinstance(sector_daily, dict) or sector_daily.get("missing"):
        return []
    sectors = sector_daily.get("sectors") or []
    out: list[Finding] = []
    seen_ranks: set[int] = set()
    prev_turnover = None
    for row in sectors:
        rank = row.get("rank")
        turnover = row.get("turnover_krw")
        if rank is None or turnover is None:
            continue
        if rank in seen_ranks:
            out.append(Finding(ERROR, "sectors", f"섹터 rank 중복: {rank} ({row.get('sector')})"))
        seen_ranks.add(rank)
        if prev_turnover is not None and turnover > prev_turnover:
            out.append(Finding(
                ERROR, "sectors",
                f"섹터 순위가 거래대금 내림차순이 아님: {row.get('sector')}"
                f"(rank={rank}, turnover={turnover} > 이전 {prev_turnover})",
            ))
        prev_turnover = turnover
    return out


# ──────────────────────────────────────────────────────────── 정확도 박스

_ACCURACY_KEYS = ("measured", "stance_line", "telegram_line", "direction", "candidates", "ic")


def _lint_accuracy_box(payload: dict) -> list[Finding]:
    acc = payload.get("report_accuracy")
    if not isinstance(acc, dict):
        return []
    out: list[Finding] = []
    for key in _ACCURACY_KEYS:
        if key not in acc:
            out.append(Finding(ERROR, "accuracy", f"report_accuracy에 '{key}' 키 없음"))
    if out:
        return out
    for horizon_dict_name in ("direction", "candidates", "ic"):
        horizons = acc[horizon_dict_name]
        if not isinstance(horizons, dict):
            continue
        for h, entry in horizons.items():
            if not isinstance(entry, dict) or not entry.get("measured"):
                continue
            for rate_key in ("rate", "value"):
                v = entry.get(rate_key)
                if rate_key == "rate" and _is_number(v) and not (0.0 <= v <= 1.0):
                    out.append(Finding(
                        ERROR, "accuracy",
                        f"report_accuracy.{horizon_dict_name}[{h}].rate가 [0,1] 밖 — {v}",
                    ))
    return out


# ──────────────────────────────────────────────────────────── 텔레그램 ↔ HTML 일치

def _lint_telegram_correspondence(payload: dict) -> list[Finding]:
    """`quant.report.render.telegram`의 결정론 요약 함수를 그대로 재호출해
    (재구현하지 않음 — 로직 두 벌이 갈라지는 걸 막는다) 그 출력에 누출 마커가
    없는지, 후보가 있는데 "0개"라고 말하는 모순이 없는지 본다."""
    from quant.report.render.telegram import _format_close_summary, _format_summary

    is_close = payload.get("session") == "close"
    try:
        text = _format_close_summary(payload) if is_close else _format_summary(payload)
    except Exception as e:  # noqa: BLE001 — 요약 생성 자체의 실패도 린트 결함이다
        return [Finding(ERROR, "telegram", f"텔레그램 요약 생성 실패: {type(e).__name__}: {e}")]

    out = _scan_leaks("telegram", text)
    if is_close:
        candidates_n = len(payload.get("intraday_view") or [])
    else:
        candidates_n = _auto_watch_count(payload.get("auto_watch"))
    if candidates_n > 0 and re.search(r"(?:마감 )?후보 0개", text):
        out.append(Finding(ERROR, "telegram", "후보가 있는데 텔레그램 요약은 '후보 0개'라고 말함"))
    return out


# ──────────────────────────────────────────────────────────── 진입점

def lint_report(model_or_payload: ReportModel | CloseReportModel | dict) -> list[Finding]:
    """`ReportModel`/`CloseReportModel` 또는 그 `payload` dict 하나를 검사해
    `list[Finding]`을 낸다. 순서는 안정적이지 않다 — 호출부가 필요하면
    `severity`/`section`으로 정렬한다."""
    if isinstance(model_or_payload, (ReportModel, CloseReportModel)):
        model = model_or_payload
        payload = model.payload
    elif isinstance(model_or_payload, dict):
        model = None
        payload = model_or_payload
    else:
        raise TypeError(
            f"lint_report는 ReportModel/CloseReportModel/dict만 받는다 — {type(model_or_payload)!r}"
        )

    # 산문 근거 검증(2026-09-07 리포트 산문 감사, results/report_prose_audit/SUMMARY.md).
    # check_prose 는 payload 의 산문 블록(money_flow.prose / midterm_watch[].prose /
    # holiday_synthesis.prose)을 보고, ReportModel 경로에서는 렌더 전용 모델 필드
    # (exec_summary/digest_prose/section_advice/stance_prose/agent_interpret_view — payload 에
    # 없다는 간극은 감사에서 확인)를 사본에 얹어 함께 본다. payload 자체는 건드리지 않는다.
    check_payload = payload
    if isinstance(model, ReportModel):
        check_payload = {
            **payload, "exec_summary": model.exec_summary, "digest_prose": model.digest_prose,
            "section_advice": model.section_advice, "stance_prose": model.stance_prose,
            "agent_interpret_view": model.agent_interpret_view,
        }
    elif isinstance(model, CloseReportModel):
        check_payload = {**payload, "agent_interpret_view": model.agent_interpret_view}

    findings: list[Finding] = []
    findings.extend(_scan_leaks("payload", payload))
    findings.extend(_scan_ranges("payload", payload))
    findings.extend(_lint_dates(payload))
    findings.extend(_lint_required_sections(payload))
    findings.extend(_lint_stance_wording(payload))
    findings.extend(_lint_score_polarity(payload))
    findings.extend(_lint_auto_watch_contradiction(payload))
    findings.extend(_lint_symbol_completeness(payload))
    findings.extend(_lint_accuracy_box(payload))
    findings.extend(_lint_telegram_correspondence(payload))
    # 산문 findings 는 **첫 주(2026-09-14 까지) 관찰 모드** — 생산자(news/midterm/agent_interpret)
    # 가 error 문장을 이미 "근거 부족으로 생략"으로 치환한 뒤라 여기서 error 가 남는 건 생산자
    # 밖 블록(money_flow/holiday_synthesis)뿐이고, 방향 판정은 휴리스틱이라 오탐이 발행을
    # 막으면 안 된다. 전부 warn 으로 내려 report_lint.jsonl 에 쌓고, 일주일 치를 보고 승격한다.
    findings.extend(
        Finding(WARN, f.section, f"[prose:{f.severity}] {f.message}") for f in check_prose(check_payload)
    )

    if isinstance(model, ReportModel):
        findings.extend(_lint_sector_daily_order(model.sector_daily))
        findings.extend(_lint_view_completeness("intraday_view", model.intraday_view))
        findings.extend(_lint_view_completeness("agent_interpret_view", model.agent_interpret_view))
    elif isinstance(model, CloseReportModel):
        findings.extend(_lint_view_completeness("intraday_view", model.intraday_view))
        findings.extend(_lint_view_completeness("agent_interpret_view", model.agent_interpret_view))

    return findings
