#!/usr/bin/env python3
"""llm_trader 판단 레인 — 엔진 밖에서 도는 컨텍스트 조립기 + 인박스 기록기.

server/scripts/llm_trader.sh 가 이 스크립트를 두 단계에서 부른다:
  llm_trader.py context   → 프롬프트에 실을 시스템 상태 텍스트를 stdout 에 낸다.
  llm_trader.py record    → claude -p 의 원문 출력을 stdin 으로 받아 JSON 계약대로
                             파싱·검증한 뒤 data/state/llm_trader_inbox.jsonl 에 append.

quant/ 를 직접 임포트하지는 않는다(이 판단 프로세스 자체는 엔진과 분리된
별도 프로세스 — CLAUDE.md 거래 핫패스 금지, ADR-0002) — 다만 읽기 전용 시세
조회(`quant.apps.cli peek`)는 **서브프로세스로 우리 CLI를 호출**해서 쓴다.
아래 인박스 파일 하나가 엔진(별도 소비 전략을 배선 중인 워커)과의 유일한
쓰기 접점이다.

## peek(시세 데이터)를 왜 모델이 아니라 이 스크립트가 대신 부르는가

2026-08-30 소유자 지시 원문은 "claude -p 호출 시 모델이 Bash 로 peek 을
직접 실행하게 하되, 도구 허용을 WebSearch/WebFetch + 그 Bash 패턴만으로
제한하라"였다. 실측 결과(EC2, claude CLI 2.1.233): `--allowedTools`/
`--disallowedTools`/`--settings permissions.{allow,deny}` 네 가지 조합
전부 — Bash 를 막으면 패턴이 있어도 Bash 도구 자체가 통째로 사라지고,
Bash 를 막지 않으면 임의 명령(`id` 등)이 아무 제한 없이 그대로 실행됐다.
**이 CLI 버전은 "Bash 를 특정 명령 패턴만 허용"하는 부분 허용을 지원하지
않는다** — 전부 허용 아니면 전부 차단 둘 중 하나뿐이다. "무제한 Bash 는
금지"라는 안전 원칙이 우선이므로, 모델에게는 Bash 자체를 아예 안 준다
(llm_trader.sh 의 `--disallowedTools`에 `Bash` 포함). 그 대신 이 스크립트가
후보 심볼(리포트 상위 + 내 포지션)에 대해 **직접** peek 을 호출해 그 결과를
컨텍스트 텍스트에 미리 실어 보낸다 — "우리 데이터 API 활용"이라는 목적은
달성하되, 모델이 임의 셸 명령을 실행할 수단은 주지 않는다. peek 이 아직
그 서버에 배포되지 않았거나 실패해도(모듈 없음/타임아웃/파싱 실패) 그
심볼만 조용히 건너뛴다 — "조회 실패=그 정보 없이 판단"(CLAUDE.md 원칙과
동일하게 관대해야 한다).

인박스 계약(엔진 쪽과 합의, 2026-08-30 horizon 필드 추가):
  {"id": "<uuid>", "ts": "<iso KST>", "action": "buy"|"sell", "symbol": "<6자리 KR코드>",
   "weight": 0.0~1.0(buy) | null(sell), "horizon": "단타"|"스윙"|"장기",
   "reason": "<한 줄 근거>"}
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTFOLIO_PATH = REPO_ROOT / "data" / "state" / "portfolio.json"
INBOX_PATH = REPO_ROOT / "data" / "state" / "llm_trader_inbox.jsonl"
TRADES_PATH = REPO_ROOT / "data" / "state" / "trades.jsonl"
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
KST = timezone(timedelta(hours=9))
STRATEGY_ID = "llm_trader"
# 2026-09-01 실계좌 이식 후: 가상 1천만원 실험에서 실계좌 규모 KR 레인 배분
# (~4.5M × 8%)으로 축소 존속. 데뷔일 순손실의 진범이 판단력이 아니라 회전율로
# 진단돼(수수료 30,795원 > 총손실), 성적표 주입과 함께 소액으로 계속 관찰한다.
BUDGET_KRW = 400_000.0
SYMBOL_RE = re.compile(r"^\d{6}$")
WEIGHT_MIN, WEIGHT_MAX = 0.1, 0.34
HORIZONS = ("단타", "스윙", "장기")
PEEK_MAX_SYMBOLS = 6
PEEK_TIMEOUT_SEC = 20


def _now_kst() -> datetime:
    return datetime.now(KST)


# ---------------------------------------------------------------------------
# context — 판단은 안 한다. 순수 조회 + 서식만.
# ---------------------------------------------------------------------------
def _load_report_payload() -> tuple[dict | None, str]:
    """(payload, 없을/실패했을 때 보여줄 안내문). payload 는 성공 시에만 dict."""
    now = _now_kst()
    path = REPO_ROOT / "out" / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}" / "KR_engine.json"
    if not path.exists():
        return None, f"(오늘자 KR 리포트 없음: {path.relative_to(REPO_ROOT)})"
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"(리포트 읽기 실패: {exc})"


def _report_summary(payload: dict | None, empty_note: str) -> tuple[str, list[str]]:
    """리포트 요약 텍스트와, peek 조회에 쓸 상위 후보 심볼 목록을 함께 낸다."""
    if payload is None:
        return empty_note, []

    lines: list[str] = []
    stance = payload.get("stance") or {}
    if stance.get("score100") is not None:
        lines.append(f"종합 {int(stance['score100'])}점/100 — {stance.get('label') or '?'}")
    if stance.get("line"):
        lines.append(str(stance["line"]))
    pos = [str(p) for p in (stance.get("positives") or [])]
    neg = [str(n) for n in (stance.get("negatives") or [])]
    if pos:
        lines.append("호재: " + " · ".join(pos))
    if neg:
        lines.append("악재: " + " · ".join(neg))

    idx = payload.get("index_outlook") or {}
    for key, label in (("kospi", "코스피"), ("kosdaq", "코스닥")):
        entry = idx.get(key)
        if not entry:
            continue
        score100 = entry.get("score100")
        prob = (entry.get("probability") or {}).get("prob")
        bits = [label]
        if score100 is not None:
            bits.append(f"{int(score100)}점({entry.get('label') or '?'})")
        if prob is not None:
            bits.append(f"익일상승확률 {prob * 100:.0f}%")
        if len(bits) > 1:
            lines.append(" ".join(bits))

    symbols = payload.get("symbols") or []
    ranked = sorted(
        symbols,
        key=lambda s: (s.get("ai_score100") if s.get("ai_score100") is not None else -1),
        reverse=True,
    )
    top_symbols: list[str] = []
    if ranked:
        lines.append("상위 후보:")
        for s in ranked[:8]:
            sym = s.get("symbol") or "?"
            name = s.get("name") or sym
            score = s.get("ai_score100")
            score_txt = f"{int(score)}점" if score is not None else "?"
            lines.append(f"  {sym} {name} ({score_txt})")
            if isinstance(sym, str) and SYMBOL_RE.match(sym):
                top_symbols.append(sym)

    text = "\n".join(lines) if lines else "(리포트에 표시할 항목 없음)"
    return text, top_symbols


def _positions_summary() -> tuple[str, list[str]]:
    """포지션 요약 텍스트와, peek 조회에 쓸 보유 심볼 목록을 함께 낸다."""
    if not PORTFOLIO_PATH.exists():
        text = "(포트폴리오 상태 파일 없음)\n가용 자본(추정): 약 {:,.0f}원 (기준 {:,.0f}원)".format(
            BUDGET_KRW, BUDGET_KRW
        )
        return text, []
    try:
        payload = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"(포트폴리오 읽기 실패: {exc})", []

    lines: list[str] = []
    symbols: list[str] = []
    used = 0.0
    for symbol, pos in (payload.get("positions") or {}).items():
        lot = ((pos.get("meta") or {}).get("lots") or {}).get(STRATEGY_ID)
        if not lot:
            continue
        qty = float(lot.get("qty", 0.0) or 0.0)
        if qty <= 0:
            continue
        avg_cost = float(lot.get("avg_cost", 0.0) or 0.0)
        used += qty * avg_cost
        lines.append(f"{symbol}: {qty:g}주 @ {avg_cost:,.0f}원 (평가 {qty * avg_cost:,.0f}원)")
        symbols.append(symbol)

    remaining = BUDGET_KRW - used
    body = "\n".join(lines) if lines else "(없음)"
    cash_line = (
        f"가용 자본(추정): 약 {remaining:,.0f}원 "
        f"(기준 {BUDGET_KRW:,.0f}원 - 현재 포지션 평가액 {used:,.0f}원)"
    )
    return f"{body}\n{cash_line}", symbols


def _scorecard() -> str:
    """자기 성적표 — **수수료 차감 후** 순손익과 종목별 재매매 횟수.

    2026-09-01 실측으로 드러난 착시를 메운다: 이 컨텍스트에는 평단만 있고
    수수료가 없어서, 트레이더가 "평단 26,107 대비 +1.1% 수익 중인 승자"라고
    판단한 종목이 실제로는 왕복 비용(KR 주식 왕복 ~23bp)에 먹혀 마이너스였다.
    첫날+둘째날 합계가 수수료 전 -10,579원인데 수수료가 30,795원이라, 판단의
    질과 무관하게 **재매매 빈도 자체가 손실의 주원인**이었다. 그 사실을 숨긴 채
    한 달을 돌리면 실험 데이터가 통째로 무의미해진다 — 그래서 판단을 대신
    내려주지 않고(자율성 유지) 자기 숫자만 정직하게 보여준다.
    """
    if not TRADES_PATH.exists():
        return "(체결 기록 없음)"
    today = datetime.now(KST).strftime("%Y-%m-%d")
    gross = fee = 0.0
    gross_today = fee_today = 0.0
    fills_today: dict[str, int] = {}
    try:
        for line in TRADES_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("strategy_id") != STRATEGY_ID:
                continue
            f = float(row.get("fee") or 0.0)
            g = float(row.get("realized_pnl") or 0.0) if str(row.get("side", "")).lower() == "sell" else 0.0
            gross += g
            fee += f
            if str(row.get("ts", "")).startswith(today):
                gross_today += g
                fee_today += f
                sym = str(row.get("symbol", ""))
                fills_today[sym] = fills_today.get(sym, 0) + 1
    except OSError as exc:
        return f"(체결 기록 읽기 실패: {exc})"

    churn = ", ".join(
        f"{s} {n}회" for s, n in sorted(fills_today.items(), key=lambda kv: -kv[1]) if n >= 2
    )
    return (
        f"오늘: 수수료 전 {gross_today:+,.0f}원 / 수수료 {fee_today:,.0f}원 / "
        f"**순손익 {gross_today - fee_today:+,.0f}원**\n"
        f"누적: 수수료 전 {gross:+,.0f}원 / 수수료 {fee:,.0f}원 / "
        f"**순손익 {gross - fee:+,.0f}원**\n"
        f"오늘 같은 종목 재매매: {churn or '없음'}\n"
        "※ 한국 주식은 왕복 약 23bp(수수료+거래세)가 든다 — 같은 종목을 하루에 "
        "여러 번 사고팔면 판단이 맞아도 비용이 먼저 먹는다. 위 '순손익'이 네 진짜 성적이다."
    )


def _recent_orders(n: int = 5) -> str:
    if not INBOX_PATH.exists():
        return "(없음)"
    lines = [ln.strip() for ln in INBOX_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return "(없음)"
    return "\n".join(lines[-n:])


def _peek_one(symbol: str) -> str | None:
    """읽기 전용 시세 조회 하나. 실패는 전부 삼키고 None — 판단은 그 정보 없이 계속된다."""
    if not PYTHON_BIN.exists():
        return None
    try:
        proc = subprocess.run(
            [str(PYTHON_BIN), "-m", "quant.apps.cli", "peek",
             "--symbol", symbol, "--interval", "5m", "--n", "20"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=PEEK_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"peek({symbol}) 실행 실패: {exc}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        print(f"peek({symbol}) exit={proc.returncode}: {proc.stderr[:300]}", file=sys.stderr)
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"peek({symbol}) JSON 파싱 실패: {exc}", file=sys.stderr)
        return None

    quote = data.get("quote") or {}
    price = quote.get("price")
    bars = data.get("bars") or []
    if not bars and price is None:
        return None
    bits = [symbol]
    if price is not None:
        bits.append(f"현재가 {price:,.0f}")
    if bars:
        highs = [b["high"] for b in bars if b.get("high") is not None]
        lows = [b["low"] for b in bars if b.get("low") is not None]
        vols = [b["volume"] for b in bars if b.get("volume") is not None]
        if highs and lows:
            bits.append(f"최근{len(bars)}봉(5m) 고{max(highs):,.0f} 저{min(lows):,.0f}")
        if vols:
            bits.append(f"거래량합{sum(vols):,.0f}")
    return " · ".join(bits)


def _peek_summary(candidates: list[str]) -> str:
    """리포트 상위 후보 + 내 포지션 심볼에 한해 우리 데이터 API(peek)로 조회한다.

    모델에게 Bash 로 peek 을 직접 호출하게 하지 않는 이유는 파일 상단 주석
    참고 — 이 스크립트가 대신 조회해 컨텍스트에 실어 보낸다."""
    seen: list[str] = []
    for sym in candidates:
        if sym in seen:
            continue
        seen.append(sym)
        if len(seen) >= PEEK_MAX_SYMBOLS:
            break
    if not seen:
        return "(조회 대상 없음)"

    lines = [line for sym in seen if (line := _peek_one(sym)) is not None]
    if not lines:
        return "(조회 실패 또는 peek 명령 미배포 — 이 정보 없이 판단하라)"
    return "\n".join(lines)


def cmd_context() -> int:
    payload, empty_note = _load_report_payload()
    report_text, top_symbols = _report_summary(payload, empty_note)
    positions_text, position_symbols = _positions_summary()
    candidates = position_symbols + [s for s in top_symbols if s not in position_symbols]

    print("===== [오늘 아침 KR 리포트 요약] =====")
    print(report_text)
    print()
    print("===== [현재 내 포지션 (llm_trader)] =====")
    print(positions_text)
    print()
    print("===== [참고 시세 데이터 (우리 데이터 API 조회 — 리포트 상위 후보 + 내 포지션)] =====")
    print(_peek_summary(candidates))
    print()
    print("===== [내 성적표 (수수료 차감 후 — 이게 진짜 성적이다)] =====")
    print(_scorecard())
    print()
    print("===== [최근 자기 주문 이력 (최근 5건)] =====")
    print(_recent_orders())
    return 0


# ---------------------------------------------------------------------------
# record — claude 출력 파싱·검증·인박스 append.
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _extract_json_array(raw: str) -> list | None:
    """첫 `[` 부터 괄호 깊이를 세어 그 배열이 닫히는 지점까지만 잘라 파싱한다.

    WebSearch 를 허용했으므로 모델이 배열 뒤에 "Sources: [제목](url)" 같은
    마크다운 링크를 덧붙이는 게 실제로 관찰된다(EC2 스모크 테스트) — 그 안의
    `[`/`]`가 문자열도 아닌 배열 뒤 텍스트라 rfind("]") 로 마지막 `]`를 잡으면
    그 링크까지 통째로 삼켜 파싱이 깨진다. 문자열 리터럴 안의 대괄호는
    건너뛰고, 배열 바깥의 깊이가 0으로 돌아오는 첫 지점에서 자른다."""
    text = _FENCE_RE.sub("", raw).strip()
    start = text.find("[")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return data if isinstance(data, list) else None
    return None


def _validate(item: object) -> tuple[dict, None] | tuple[None, str]:
    if not isinstance(item, dict):
        return None, f"딕셔너리 아님: {item!r}"
    action = item.get("action")
    symbol = item.get("symbol")
    reason = item.get("reason")
    weight = item.get("weight")
    horizon = item.get("horizon")

    if action not in ("buy", "sell"):
        return None, f"action 불량: {item!r}"
    if not isinstance(symbol, str) or not SYMBOL_RE.match(symbol):
        return None, f"symbol 불량(6자리 KR코드 아님): {item!r}"
    if not isinstance(reason, str) or not reason.strip():
        return None, f"reason 없음: {item!r}"
    if horizon not in HORIZONS:
        return None, f"horizon 불량({'/'.join(HORIZONS)} 중 하나여야 함): {item!r}"

    if action == "buy":
        try:
            weight = float(weight)
        except (TypeError, ValueError):
            return None, f"buy인데 weight 불량: {item!r}"
        if not (WEIGHT_MIN <= weight <= WEIGHT_MAX):
            return None, f"weight 범위 밖({WEIGHT_MIN}~{WEIGHT_MAX}): {item!r}"
    else:
        weight = None

    return {
        "id": str(uuid.uuid4()),
        "ts": _now_kst().isoformat(),
        "action": action,
        "symbol": symbol,
        "weight": weight,
        "horizon": horizon,
        "reason": reason.strip(),
    }, None


def cmd_record() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        print("NO_OUTPUT")
        return 0

    data = _extract_json_array(raw)
    if data is None:
        print("PARSE_FAIL")
        print(raw[:2000])
        return 0

    if not data:
        print("NO_TRADE")
        return 0

    accepted: list[dict] = []
    skipped: list[str] = []
    for item in data:
        record, err = _validate(item)
        if record is None:
            skipped.append(err or "알 수 없는 사유")
        else:
            accepted.append(record)

    if accepted:
        INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
        with INBOX_PATH.open("a", encoding="utf-8") as f:
            for record in accepted:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"RECORDED: {len(accepted)}건")
    for record in accepted:
        print(
            f"  {record['action']} {record['symbol']} weight={record['weight']} "
            f"horizon={record['horizon']} — {record['reason']}"
        )
    if skipped:
        print(f"SKIPPED: {len(skipped)}건")
        for s in skipped:
            print(f"  {s}")
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("context", "record"):
        print("사용법: llm_trader.py {context|record}", file=sys.stderr)
        return 2
    return {"context": cmd_context, "record": cmd_record}[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
