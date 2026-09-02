#!/usr/bin/env python3
"""텔레그램 양방향 브리지 — 오너가 폰에서 회사 클로드와 대화.

오너 메시지 -> 레포에서 headless `claude -p` 실행 -> 답변을 봇으로 전송.
엔진 핫패스와 완전히 분리된 별도 프로세스 (ADR-2: 거래 핫패스에 LLM 없음 — 엔진은
결정론적으로 돈다. Claude/분석은 이 브리지 같은 리포팅 레이어에서만 호출된다).
읽기 전용 설계 — permission mode DEFAULT, bypassPermissions 사용 안 함.

설치: server/systemd/tg-bridge.service, server/README.md 참고.
전작(stock-algo-trade)의 scripts/tg_bridge.py를 이식.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import yaml

from quant.trade.control import TradingControl
from quant.adapters.brokers.toss.client import TossClient
from quant.core.models import market_of_symbol, trading_day

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_LOCAL = REPO_ROOT / ".env.local"
OFFSET_FILE = REPO_ROOT / "data" / "tg_bridge_offset.txt"
CONTROL_STATE_PATH = REPO_ROOT / "data" / "state" / "control.json"
PORTFOLIO_STATE_PATH = REPO_ROOT / "data" / "state" / "portfolio.json"
WATCHLIST_PATH = REPO_ROOT / "data" / "watchlist.yaml"
LEDGER_PATH = REPO_ROOT / "data" / "state" / "trades.jsonl"
KST = timezone(timedelta(hours=9))

# 브리지 자체 표시용 — 엔진(loop.py)의 라벨과 같은 표기를 유지한다
STRATEGY_LABELS = {
    "donchian": "돈치안 채널 돌파",
    "orb_scan": "개장 5분 돌파",
    "intraday_scan": "장중 신고가 돌파",
}

RATE_LIMIT_MAX = 6
RATE_LIMIT_WINDOW_SEC = 600
CLAUDE_TIMEOUT_SEC = 240
REPLY_MAX_CHARS = 4000
GETUPDATES_TIMEOUT = 50

SYSTEM_PREFIX = (
    "[텔레그램 브리지 경유 — 오너 질문. 답은 텔레그램용으로 간결하게(10줄 이내). "
    "파일 수정·설정 변경·매매 관련 행동은 절대 하지 말고, 요청받으면 "
    "'Mac 세션이나 SSH에서 진행해달라'고 안내만 하라. 조회·요약·설명만 수행.]\n\n"
)


def log(msg: str) -> None:
    print(f"[tg_bridge] {msg}", flush=True)


# ---------------------------------------------------------------------------
# env 파싱 (stdlib만 사용 — python-dotenv 의존 안 함)
# ---------------------------------------------------------------------------
def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def get_config(path: Path = ENV_LOCAL) -> tuple[str, int]:
    """.env.local에서 대화 전용 봇 토큰/chat_id를 읽는다. 없으면 exit(1).

    2봇 체제: TELEGRAM_BOT_TOKEN은 알림 전용 봇(quant.adapters.notify), 이 브리지는
    BRIDGE_* 전용 봇만 쓴다 — 폰에서 '알림 피드'와 'Claude 대화방'이 분리된다."""
    env = read_env_file(path)
    token = env.get("TELEGRAM_BRIDGE_BOT_TOKEN", "")
    chat_id_raw = env.get("TELEGRAM_BRIDGE_CHAT_ID", "")
    if not token or not chat_id_raw:
        log("TELEGRAM_BRIDGE_BOT_TOKEN / TELEGRAM_BRIDGE_CHAT_ID 누락 (.env.local) — 종료")
        sys.exit(1)
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        log(f"TELEGRAM_BRIDGE_CHAT_ID가 정수가 아님: {chat_id_raw!r} — 종료")
        sys.exit(1)
    return token, chat_id


def get_toss_client(path: Path = ENV_LOCAL) -> TossClient:
    """/watch 종목 검증용 TossClient. 자격증명이 없어도 인스턴스는 만들어진다 —
    실패는 stock_info 호출 시점에 나며, 호출부가 '미검증' 폴백으로 흡수한다."""
    env = read_env_file(path)
    return TossClient(
        client_id=env.get("TOSS_CLIENT_ID", ""),
        client_secret=env.get("TOSS_CLIENT_SECRET", ""),
        account_seq=env.get("TOSS_ACCOUNT_SEQ", ""),
    )


# ---------------------------------------------------------------------------
# offset 영속화 (재시작 시 재생 방지)
# ---------------------------------------------------------------------------
def load_offset(path: Path = OFFSET_FILE) -> Optional[int]:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def save_offset(offset: int, path: Path = OFFSET_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(offset))


# ---------------------------------------------------------------------------
# 레이트 리밋 — 슬라이딩 윈도우 (10분당 최대 6회 클로드 호출)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(
        self,
        max_calls: int = RATE_LIMIT_MAX,
        window_sec: float = RATE_LIMIT_WINDOW_SEC,
        time_fn=time.monotonic,
    ):
        self.max_calls = max_calls
        self.window_sec = window_sec
        self.time_fn = time_fn
        self._calls: deque[float] = deque()

    def allow(self) -> bool:
        now = self.time_fn()
        while self._calls and now - self._calls[0] > self.window_sec:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            return False
        self._calls.append(now)
        return True


# ---------------------------------------------------------------------------
# 메시지 필터링 / 정리
# ---------------------------------------------------------------------------
def is_allowed_chat(update: dict, allowed_chat_id: int) -> bool:
    message = update.get("message")
    if not message:
        return False
    chat = message.get("chat", {})
    return chat.get("id") == allowed_chat_id


def extract_text(update: dict) -> Optional[str]:
    message = update.get("message", {})
    text = message.get("text")
    return text if isinstance(text, str) and text.strip() else None


def truncate_reply(text: str, limit: int = REPLY_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    marker = "…(잘림)"
    return text[: limit - len(marker)] + marker


def build_claude_prompt(user_text: str) -> str:
    return SYSTEM_PREFIX + user_text


# ---------------------------------------------------------------------------
# 매매 제어 명령 (/halt·/rest, /resume·/live, /flatten[·all|day], /status) — 실거래에
# 직접 영향을 주므로 Claude 서브프로세스를 거치지 않고 TradingControl에 바로
# 반영한다. 허용 chat_id 검증은 process_update에서 이미 끝난 뒤에만 호출된다.
# /rest·/live는 각각 /halt·/resume의 별칭(같은 경로) — REST는 "관리 불가 상황에서
# 신규 진입만 막고 보유 포지션 손절·청산은 계속"이라는 기존 halt 의미론 그대로다.
# ---------------------------------------------------------------------------
def read_portfolio_summary(path: Optional[Path] = None) -> Optional[dict]:
    path = path or PORTFOLIO_STATE_PATH  # 호출 시점 해석 — 테스트 monkeypatch 격리 가능
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def handle_control_command(
    text: str, control: TradingControl, toss_client: Optional[TossClient] = None
) -> Optional[str]:
    """제어 명령이면 응답 텍스트를, 아니면(일반 대화) None을 반환한다."""
    stripped = text.strip()
    lower = stripped.lower()

    if lower == "/resume" or lower == "/live":
        control.resume()
        return "재개됨(LIVE) — 신규 진입 재허용"

    if lower == "/flatten" or lower == "/flatten all":
        control.request_flatten("all")
        return "전량 청산 요청됨 — 다음 엔진 사이클에 반영"

    if lower == "/flatten day" or lower == "/단타청산":
        control.request_flatten("day")
        return (
            "단타 보유분만 즉시 청산 요청됨 — 다음 엔진 사이클에 반영\n"
            "오버나이트 전략 보유분은 그대로 유지됩니다"
        )

    if lower == "/status":
        halted = control.is_halted()
        if not halted:
            state_value = "🟢 LIVE"
        elif control.halted_by() == "auto":
            state_value = f"⛔ 자동 중단({control.halt_reason()})"
        else:
            state_value = f"⏸ REST(수동 정지: {control.halt_reason()})"
        # "거래상태" 레이블은 기존 표시와 하위호환을 위해 유지 — 값만 LIVE/REST/자동중단으로 구분한다.
        lines = [f"거래상태: {state_value}"]
        portfolio = read_portfolio_summary()
        if portfolio is not None:
            lines.append(f"현금: {portfolio.get('cash', 0):,.0f}원")
            # 통화 분리 지갑(2026-09-01 실계좌 이식) — 달러 풀이 있으면 별도 줄.
            # 이걸 빼면 계좌의 2/3(USD)가 /status에서 보이지 않는다.
            cash_usd = float(portfolio.get("cash_usd", 0) or 0)
            if cash_usd > 0:
                lines.append(f"현금(USD): ${cash_usd:,.2f}")
            open_syms = [s for s, p in portfolio.get("positions", {}).items() if p.get("qty", 0) > 0]
            # 종목명 표시 — /balance·/daily_record와 같은 패턴(관심종목 파일에
            # 등록 시점에 적어둔 이름 재사용). 이름이 없으면 코드만(_display_symbol).
            names = _watchlist_names()
            lines.append(
                f"포지션: {', '.join(_display_symbol(s, names) for s in open_syms) if open_syms else '없음'}"
            )
        if toss_client is not None:
            lines.append("")
            lines.append(handle_daily_record(toss_client))
        return "\n".join(lines)

    if lower == "/halt" or lower.startswith("/halt ") or lower == "/rest" or lower.startswith("/rest "):
        prefix_len = len("/rest") if lower.startswith("/rest") else len("/halt")
        reason = stripped[prefix_len:].strip() or "수동 중단(텔레그램)"
        control.halt(reason, by="manual")
        return (
            f"REST 상태 진입 — 사유: {reason}\n"
            "신규 진입 중단 — 보유 포지션 손절·청산은 계속 작동"
        )

    return None


# ---------------------------------------------------------------------------
# 관심종목 명령 (/watch, /unwatch, /watchlist) — Claude 서브프로세스를 거치지 않고
# data/watchlist.yaml에 바로 반영한다. 형식은 {symbols: [{symbol, name, added_at}]}
# 로 고정 — 다음 엔진 세션 시작 시 읽어들이는 다른 컴포넌트와 형식을 공유한다.
# ---------------------------------------------------------------------------
def normalize_symbol(raw: str) -> str:
    """6자리 숫자면 KR 종목코드로 그대로, 그 외에는 대문자화해 US 심볼로 취급."""
    s = raw.strip()
    if s.isdigit() and len(s) == 6:
        return s
    return s.upper()


import fcntl
from contextlib import contextmanager


@contextmanager
def _watchlist_lock(path: Optional[Path] = None):
    """관심종목 파일의 read-modify-write 전체를 배타 잠금으로 감싼다.

    원자적 tmp-replace는 **파일 깨짐**만 막지 **유실**은 못 막는다 — 두 쓰기가
    같은 스냅샷을 읽고 각자 저장하면 나중 것이 앞 것을 덮는다(동시 15건 중 11건
    유실 실측, 2026-08-09 E2E). 브리지는 단일 스레드라 실전 노출은 작지만,
    잠금 없는 읽기-수정-쓰기는 언젠가 두 번째 쓰기 주체가 생기는 순간 조용히
    터지는 종류라 원천 차단한다. flock은 프로세스 간에도 유효하다.
    """
    path = path or WATCHLIST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def load_watchlist(path: Optional[Path] = None) -> list[dict]:
    path = path or WATCHLIST_PATH
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return []
    symbols = data.get("symbols", [])
    return symbols if isinstance(symbols, list) else []


def save_watchlist(entries: list[dict], path: Optional[Path] = None) -> None:
    path = path or WATCHLIST_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"symbols": entries}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def format_watchlist(path: Optional[Path] = None) -> str:
    entries = load_watchlist(path or WATCHLIST_PATH)
    if not entries:
        return "관심종목 없음"
    lines = ["관심종목 (다음 세션 시작에 반영됨):"]
    for e in entries:
        name = e.get("name") or "미검증"
        marker = " [auto]" if e.get("source") == "auto" else ""
        lines.append(f"- {e.get('symbol')} ({name}){marker} — {e.get('added_at', '')}")
    return "\n".join(lines)


AUTO_ENTRY_TTL_DAYS = 3
# 관심종목 상한은 **시장별**이다 (2026-08-11). 전역 하나(20)로 두니 KR 7 + US 12로
# 19가 차서 US 자동 편입이 1종목만 들어가고 나머지가 상한에 막혔다 — 두 시장이 같은
# 예산을 놓고 경쟁하는 구조가 애초에 틀렸다. 시장이 다르면 거래 시간도 다르다.
#
# 20/20 (2026-08-11 사용자 지시: "한국 20개 따로 미국 20개 따로").
#
# 비용 고지 — 이 숫자는 곧 사이클 시간이다. 전략은 열린 시장의 심볼만 판정하므로
# 한 사이클의 조회 대상은 한 시장 몫뿐인데, Toss /candles는 MARKET_DATA_CHART
# **5 TPS·배치 불가**라 20종목이면 산술적으로 최소 4초가 든다. poll_seconds는 5초라
# 사이클이 폴링 주기를 거의 다 먹고, 실측(19종목 혼재)에서도 이미 3.1~7.1초와
# 429가 3.3% 나고 있었다. 즉 20/20은 "한도가 넉넉한" 설정이 아니라 **조회 파이프를
# 포화시키는 설정**이다 — 봉 캐시 정렬·닫힌 시장 스킵·배치 quote로 조회량을 줄이는
# 작업이 선행돼야 안정적이다(대기열 최상단).
WATCHLIST_CAP_PER_MARKET = 20

# 적립 태그(FRGN/FRGN_EXIT) 보존 신선도 — own_brief가 매일 FRGN 신호가 살아있는
# 종목에 태그를 다시 붙이므로(watch-add), 이 기간 넘게 tags_updated_at 갱신이 없다는
# 것은 신호가 끝났다는 뜻이다(주말·연휴를 넘겨도 안전한 여유, 2026-08-18).
ACCUM_TAG_TTL_DAYS = 7
# 그래도 무한정 쌓이지 않도록 시장별 보존 개수 상한 — WATCHLIST_CAP_PER_MARKET(20)의
# 절반 미만으로 둬 신규 후보가 최소 12개 자리를 확보하게 한다.
ACCUM_PRESERVE_MAX = 8


def _prune_expired_auto(entries: list[dict], now: Optional[datetime] = None) -> tuple[list[dict], list[str]]:
    """source=="auto"이고 added_at이 AUTO_ENTRY_TTL_DAYS일보다 오래된 항목을 제거한다.

    자동등록(daily_brief.sh의 watch-add)은 사용자가 명시적으로 고른 게 아니므로
    무기한 남으면 관심종목이 조용히 불어난다. 사용자가 직접 등록한 항목(source
    미지정/"user")은 대상이 아니다 — 사람이 고른 건 사람이 지운다.
    added_at이 없거나 파싱 불가능하면 판단 불가로 보고 보존한다.
    """
    now = now or datetime.now()
    kept: list[dict] = []
    pruned: list[str] = []
    for e in entries:
        if e.get("source") == "auto":
            added_at = e.get("added_at")
            added_dt = None
            if added_at:
                try:
                    added_dt = datetime.fromisoformat(added_at)
                except ValueError:
                    added_dt = None
            if added_dt is not None and (now - added_dt).days > AUTO_ENTRY_TTL_DAYS:
                pruned.append(e.get("symbol"))
                continue
        kept.append(e)
    return kept, pruned


def _handle_watch_unlocked(
    arg: str, toss_client: TossClient, path: Optional[Path] = None, source: str = "user",
    tags: Optional[list] = None, tags_only: bool = False,
) -> str:
    """여러 심볼을 한 번에 받는다: `/watch AAPL NVDA 005930`.

    폰에서 쓰는 명령이라 한 줄에 몰아넣는 편의가 중요하다. 종목별로 독립 처리 —
    하나가 중복/검증실패여도 나머지는 정상 추가되고, 응답에 종목별 결과를 전부
    나열해 무엇이 어떻게 됐는지 숨기지 않는다. 파일 쓰기는 전부 처리 후 1회
    (원자적 쓰기이므로 중간 상태가 파일에 남지 않는다).

    `source`는 "user"(텔레그램 /watch, 기본값) 또는 "auto"(daily_brief.sh 자동등록)
    — provenance를 남겨야 TTL 만료 대상을 구분할 수 있다. 만료된 자동등록 정리와
    상한(WATCHLIST_CAP) 적용은 매 호출 시작 시 수행한다.

    `tags`(예: `["EVENT"]`)는 이 호출로 추가되는 **모든** 심볼에 동일하게 붙는다
    (`--source`와 같은 전역 플래그 시맨틱 — CLI 파싱은 `_parse_watch_add_argv`
    참고). 뉴스 근거(EVENT)가 있는 종목만 개장 즉시 진입하는 전략(news_momentum)이
    `FileWatchlistUniverse.tags()`로 이 값을 읽는다. None/빈 리스트면 기존 항목과
    동일하게 `tags` 키 자체를 남기지 않는다(하위호환).

    **이미 등록된 심볼에 `tags`가 주어지면 태그를 병합한다**(2026-08-17, 서브프로젝트
    T — 기존에는 "이미 있음"으로 건너뛰기만 해서 재등록일에 새 태그(예:
    EVENT_SCALP)가 조용히 유실됐다). 병합은 기존 태그를 지우지 않고 합집합만
    추가한다(사용자가 `/watch`로 태그 없이 손으로 추가한 항목도 자동 편입이 이후에
    태그를 더할 수 있어야 한다).

    `tags_only=True`면 **신규 등록을 하지 않는다** — 이미 있는 심볼의 태그만
    갱신하고, 없는 심볼은 조용히 건너뛴다(own_brief.sh의 FRGN_EXIT 경로 전용:
    외국인 이탈 신호는 "이미 보유/등록된 종목"에만 의미가 있다 — 워치리스트에
    없는 종목을 이탈 태그로 신규 등록하는 것은 그 자체로 모순이다).

    태그가 주어지면(병합이든 신규 등록이든) 해당 항목에 `tags_updated_at`을
    남긴다(2026-08-18) — 시장별 리셋의 적립 태그 보존이 이 값으로 신선도를
    판정한다(`_handle_watchlist_reset_unlocked`, `ACCUM_TAG_TTL_DAYS`).
    """
    path = path or WATCHLIST_PATH
    parts = arg.split()
    if not parts:
        return "사용법: /watch <심볼> [심볼 ...]  (예: /watch AAPL NVDA 005930)"

    entries = load_watchlist(path)
    entries, pruned = _prune_expired_auto(entries)
    by_symbol = {e.get("symbol"): e for e in entries}
    existing = set(by_symbol)
    lines: list[str] = []
    if pruned:
        lines.append(f"⏳ 자동등록 만료 제거: {', '.join(pruned)}")
    # 시장별 현재 등록 수 — 상한도 시장별로 센다.
    counts: dict[str, int] = {"KR": 0, "US": 0}
    for e in entries:
        counts[market_of_symbol(str(e.get("symbol", "")))] += 1
    added = 0
    tagged = 0
    rejected: list[str] = []
    for raw in parts:
        symbol = normalize_symbol(raw)
        if symbol in existing:
            if tags:
                entry = by_symbol[symbol]
                merged = sorted(set(entry.get("tags") or []) | set(tags))
                changed = merged != (entry.get("tags") or [])
                # 태그 내용이 그대로여도 도장은 찍는다 — own_brief가 매일 살아있는
                # 신호에 같은 태그를 다시 붙이는 것 자체가 "신호 생존" 신호이므로,
                # 이 갱신이 없으면 적립 보존 TTL(ACCUM_TAG_TTL_DAYS)이 죽은 태그를
                # 계속 살아있다고 오판한다.
                entry["tags_updated_at"] = datetime.now().isoformat(timespec="seconds")
                if changed:
                    entry["tags"] = merged
                    tagged += 1
                    lines.append(f"🏷️ {symbol} — 태그 갱신: {'+'.join(merged)}")
                else:
                    tagged += 1
                    lines.append(f"↩️ {symbol} — 이미 있음(태그 변경 없음)")
            else:
                lines.append(f"↩️ {symbol} — 이미 있음")
            continue
        if tags_only:
            lines.append(f"⏭️ {symbol} — 미등록 종목은 태그 전용 갱신 대상 아님")
            continue
        market = market_of_symbol(symbol)
        if counts[market] >= WATCHLIST_CAP_PER_MARKET:
            rejected.append(symbol)
            continue
        name = ""
        try:
            info = toss_client.stock_info(symbol)
            name = info.get("name", "") if info else ""
        except Exception as exc:  # noqa: BLE001 — 검증 실패(IP 미등록 등)는 미검증 추가로 폴백
            log(f"/watch 종목 검증 실패({symbol}): {exc}")
        entry = {
            "symbol": symbol,
            "name": name,
            "added_at": datetime.now().isoformat(timespec="seconds"),
            "source": source,
        }
        if tags:
            entry["tags"] = list(tags)
            entry["tags_updated_at"] = datetime.now().isoformat(timespec="seconds")
        entries.append(entry)
        existing.add(symbol)
        counts[market] += 1
        added += 1
        lines.append(f"✅ {symbol} ({name}) 추가" if name else f"⚠️ {symbol} 추가(미검증)")

    if added or pruned or tagged:
        save_watchlist(entries, path)
    if added:
        lines.append("— 다음 세션 시작에 반영됨")
    if rejected:
        lines.append(
            f"🚫 시장별 상한({WATCHLIST_CAP_PER_MARKET}종목) 초과로 미등록: {', '.join(rejected)}"
        )
    return "\n".join(lines)


def _handle_unwatch_unlocked(arg: str, path: Optional[Path] = None) -> str:
    """여러 심볼 동시 제거 지원: `/unwatch AAPL NVDA`. 열린 포지션에는 영향 없다 —
    유니버스는 신규 진입 대상 선정이지 청산 대상이 아니다(관리·손절은 계속된다)."""
    path = path or WATCHLIST_PATH
    parts = arg.split()
    if not parts:
        return "사용법: /unwatch <심볼> [심볼 ...]"

    entries = load_watchlist(path)
    lines: list[str] = []
    removed = 0
    for raw in parts:
        symbol = normalize_symbol(raw)
        remaining = [e for e in entries if e.get("symbol") != symbol]
        if len(remaining) == len(entries):
            lines.append(f"❓ {symbol} — 관심종목에 없음")
            continue
        entries = remaining
        removed += 1
        lines.append(f"🗑 {symbol} 제거")

    if removed:
        save_watchlist(entries, path)
        lines.append("— 다음 세션 시작에 반영됨 (열린 포지션 관리는 계속됨)")
    return "\n".join(lines)


def _handle_watchlist_reset_unlocked(
    path: Optional[Path] = None, market: Optional[str] = None
) -> str:
    """관심종목 초기화. 파일만 비운다 — 열린 포지션·주문에는 아무 영향 없다.

    `market`이 "KR"/"US"면 **그 시장 종목만** 지운다. 시장별 초기화가 필요한 이유:
    개장 전 초기화를 전역으로 하면 KR 개장 전 청소가 사용자가 넣어둔 US 종목까지
    날린다 — 두 시장은 개장 시각이 다르므로 청소 시각도 달라야 한다
    (2026-08-11 사용자 지시: "항상 장 시작 전 정해진 시간에 초기화, 그 뒤 내가 추가").

    실수 방지 확인 절차를 두지 않은 이유: 관심종목은 복구 가능한 목록이고
    (/watch로 다시 추가), 포지션을 건드리는 명령이 아니다. 위험한 명령(/flatten)과
    같은 무게로 다루면 사용자가 무게 감각을 잃는다.
    """
    path = path or WATCHLIST_PATH
    entries = load_watchlist(path)
    if market:
        market = market.upper()
        # 적립 갈래(frgn_accumulate) 태그는 시장별 리셋에서 **보존**한다(2026-08-18).
        # 이유: 08:05 크론 리셋이 태그 불문 전삭제면 08:12 own_brief의 FRGN_EXIT
        # 갱신(--tags-only, 이미 등록된 종목만)이 항상 빈 목록을 만나 **이탈 신호가
        # 유실**된다 — 적립 전략의 2단계 분할 청산은 태그가 연속으로 보여야 작동하고,
        # 그게 끊기면 '잃을 땐 줄여서 잃는다' 원칙이 무력화된다. 적립 종목은 설계상
        # 장수 명제라 매일 청소 대상이 아니다. 해제는 /unwatch 또는 전체 리셋
        # (/watchlist-reset, 시장 인자 없음 — 그쪽은 명시적 수동 명령이라 전삭제 유지).
        # 다만 무기한 보존은 아니다 — ACCUM_TAG_TTL_DAYS 넘게 태그가 갱신 안 되면
        # 신호가 끝난 것으로 보고 청소하고, ACCUM_PRESERVE_MAX를 넘는 초과분도 함께
        # 청소한다(상한 잠식 방지, 2026-08-18 P1).
        from quant.trade.strategy.frgn_accumulate import _FRGN_EXIT_TAG, _FRGN_TAG
        _keep_tags = {_FRGN_TAG, _FRGN_EXIT_TAG}

        def _is_accum(e: dict) -> bool:
            return bool(_keep_tags & set(e.get("tags") or []))

        def _freshness_dt(e: dict) -> Optional[datetime]:
            # tags_updated_at 우선, 구버전 항목(필드 없음)은 added_at으로 하위호환.
            raw = e.get("tags_updated_at") or e.get("added_at")
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                return None

        now = datetime.now()
        in_market = [e for e in entries if market_of_symbol(str(e.get("symbol", ""))) == market]
        # 신선도 만료(ACCUM_TAG_TTL_DAYS 초과)면 청소 대상 — 날짜를 판단 못 하면
        # (필드 없음/파싱 불가) 기존 TTL 프루닝(_prune_expired_auto)과 같은 관례로 보존.
        accum_candidates = [e for e in in_market if _is_accum(e)]
        fresh_accum = [
            e for e in accum_candidates
            if (dt := _freshness_dt(e)) is None or (now - dt).days <= ACCUM_TAG_TTL_DAYS
        ]
        # 보존 상한(ACCUM_PRESERVE_MAX) — 최신순으로 자르고 나머지는 청소 대상에 합류.
        fresh_accum.sort(key=lambda e: _freshness_dt(e) or datetime.min, reverse=True)
        preserved = fresh_accum[:ACCUM_PRESERVE_MAX]
        capped_out = len(fresh_accum) - len(preserved)
        preserved_symbols = {e.get("symbol") for e in preserved}
        removed = [e for e in in_market if e.get("symbol") not in preserved_symbols]
        kept = [e for e in entries if market_of_symbol(str(e.get("symbol", ""))) != market]
        label = "🇰🇷 한국" if market == "KR" else "🇺🇸 미국"
        preserved_note = f" · 적립 태그 보존 {len(preserved)}개" if preserved else ""
        if capped_out:
            preserved_note += f"(보존 상한 초과 {capped_out}개 절삭)"
        if not removed:
            if preserved:
                return (f"{label} 관심종목 삭제 대상 없음 — 적립 태그 보존 "
                        f"{len(preserved)}개 (해제는 /unwatch)")
            return f"{label} 관심종목이 이미 비어 있음"
        save_watchlist(kept + preserved, path)
        return (
            f"🧹 {label} 관심종목 {len(removed)}개 삭제 (다른 시장 {len(kept)}개는 유지"
            f"{preserved_note})\n"
            "다음 유니버스 갱신에 반영됩니다.\n"
            "(열린 포지션 관리는 계속됩니다 — 유니버스는 신규 진입 대상일 뿐입니다)"
        )
    if not entries:
        return "관심종목이 이미 비어 있음"
    save_watchlist([], path)
    return (
        f"🧹 관심종목 {len(entries)}개 전체 삭제 — 다음 세션 시작에 반영됨\n"
        "(열린 포지션 관리는 계속됨 — 유니버스는 신규 진입 대상일 뿐)"
    )




def handle_watch(
    arg: str, toss_client: TossClient, path: Optional[Path] = None, source: str = "user",
    tags: Optional[list] = None, tags_only: bool = False,
) -> str:
    with _watchlist_lock(path):
        return _handle_watch_unlocked(arg, toss_client, path, source=source, tags=tags, tags_only=tags_only)


def handle_unwatch(arg: str, path: Optional[Path] = None) -> str:
    with _watchlist_lock(path):
        return _handle_unwatch_unlocked(arg, path)


def handle_watchlist_reset(path: Optional[Path] = None, market: Optional[str] = None) -> str:
    with _watchlist_lock(path):
        return _handle_watchlist_reset_unlocked(path, market=market)


# ---------------------------------------------------------------------------
# 즉시 조회 명령 (/balance, /scoreboard, /help) — Claude 서브프로세스를 거치지 않고
# 로컬 상태 + Toss 시세로 바로 응답한다 (2026-08-10 사용자 요청: "너가 어차피
# 시세를 들고 있으니 내가 물으면 바로 알려달라").
# ---------------------------------------------------------------------------
SYMBOL_NAMES_PATH = REPO_ROOT / "data" / "state" / "symbol_names.json"


def _watchlist_names(path: Optional[Path] = None) -> dict[str, str]:
    """종목 이름표 — 관심종목 파일 + 엔진 이름 캐시(symbol_names.json).

    관심종목만 보면 **보유 중인데 유니버스에서 빠진 종목**의 이름이 사라진다:
    2026-09-02 실측으로 /status·/balance 가 삼성전자를 "005930" 코드로만
    보여줬다(이식으로 넘겨받은 보유분인데 그날 후보에서 탈락). 엔진이 이미
    쓰는 symbol_names.json 이 그 이름을 알고 있었을 뿐 브리지가 안 봤다 —
    같은 날 아침 리포트에서 고친 것(relation_items 이름표 누락)과 같은 부류다.
    관심종목 이름이 우선(사용자가 붙인 표기), 없으면 엔진 캐시로 메운다.
    """
    names: dict[str, str] = {}
    try:
        payload = json.loads(SYMBOL_NAMES_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            names.update({k: v for k, v in payload.items() if isinstance(v, str) and v})
    except (OSError, ValueError):
        pass  # 캐시가 없어도 관심종목 이름만으로 동작한다(있던 그대로)
    try:
        entries = load_watchlist(path or WATCHLIST_PATH)
        names.update({e.get("symbol"): e.get("name")
                      for e in entries if e.get("symbol") and e.get("name")})
    except Exception:  # noqa: BLE001
        pass
    return names


def _display_symbol(symbol: str, names: dict[str, str]) -> str:
    name = names.get(symbol)
    return f"{name} ({symbol})" if name else symbol


def _parse_quote_rows(rows) -> dict[str, float]:
    """Toss 시세 응답을 {symbol: price}로 — 실서버는 lastPrice(문자열)로 온다
    (2026-08-10 실측). price/close 키도 함께 허용해 스텁/구버전과 호환."""
    quotes: dict[str, float] = {}
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        price = row.get("price") or row.get("lastPrice") or row.get("close")
        try:
            if price is not None and float(price) > 0:
                quotes[row["symbol"]] = float(price)
        except (TypeError, ValueError):
            continue
    return quotes


def handle_balance(toss_client: TossClient) -> str:
    """현금 + 보유 종목별 평단가/현재가/수익률 + 총 추정자산. 시세 조회 실패 종목은
    평단가로 근사하고 그 사실을 표기한다 — 조회 실패가 명령 전체를 죽이면 안 된다."""
    portfolio = read_portfolio_summary()
    if portfolio is None:
        return "포트폴리오 상태 파일을 읽을 수 없음 (data/state/portfolio.json)"

    cash = float(portfolio.get("cash", 0))
    positions = {
        sym: p for sym, p in (portfolio.get("positions") or {}).items()
        if float(p.get("qty", 0) or 0) > 0
    }
    names = _watchlist_names()

    cash_usd = float(portfolio.get("cash_usd", 0) or 0)

    if not positions:
        extra = f" · 현금(USD) ${cash_usd:,.2f}" if cash_usd > 0 else ""
        return f"💰 현재 자산\n\n현금 {cash:,.0f}원{extra} · 보유 종목 없음"

    symbols = list(positions)
    quotes: dict[str, float] = {}
    try:
        quotes = _parse_quote_rows(toss_client.prices(symbols))
    except Exception as exc:  # noqa: BLE001
        log(f"/balance 시세 조회 실패(평단가로 근사): {exc}")

    try:
        usd_krw = float(toss_client.usd_krw())
    except Exception:  # noqa: BLE001
        usd_krw = 1400.0  # 시세 실패 시 보수적 고정 — 표기만 흔들리고 판단엔 안 쓰인다

    lines = ["💰 현재 자산", ""]
    total = cash
    for sym, p in positions.items():
        qty = float(p.get("qty", 0))
        avg = float(p.get("avg_cost", 0))
        is_kr = sym.isdigit() and len(sym) == 6
        unit = "원" if is_kr else "$"
        cur = quotes.get(sym)
        meta = p.get("meta") or {}
        lines.append(f"🏢 {_display_symbol(sym, names)}")
        lines.append(f"   💵 평단가 {avg:,.2f}{unit} · {qty:g}주")
        if cur:
            pct = (cur / avg - 1) * 100 if avg else 0.0
            mark = "🔺" if pct > 0 else ("🔻" if pct < 0 else "➖")
            lines.append(f"   📈 현재가 {cur:,.2f}{unit} · {mark} {pct:+.2f}%")
            value = qty * cur
        else:
            lines.append("   📈 현재가 조회 실패 — 평단가로 근사")
            value = qty * avg
        if meta.get("stop"):
            lines.append(f"   🛡 손절가 {float(meta['stop']):,.2f}{unit}")
        total += value * (1 if is_kr else usd_krw)
        lines.append("")
    lines.append(f"🏦 현금 {cash:,.0f}원")
    if cash_usd > 0:
        # 달러 지갑(통화 분리, 2026-09-01) — US 종목처럼 환율로 환산해 총자산에 합산.
        # 빠뜨리면 총자산이 달러 풀만큼(이식 직후 기준 계좌의 2/3) 과소 표기된다.
        lines.append(f"💵 현금(USD) ${cash_usd:,.2f} (≈{cash_usd * usd_krw:,.0f}원)")
        total += cash_usd * usd_krw
    lines.append(f"📊 총 추정자산 {total:,.0f}원")
    return "\n".join(lines)


def handle_scoreboard() -> str:
    try:
        from quant.control.ledger import load_trades, round_trips, scoreboard_text
        trips = round_trips(load_trades(LEDGER_PATH))
        return scoreboard_text(trips)
    except Exception as exc:  # noqa: BLE001
        return f"스코어보드 생성 실패: {exc}"


def _pnl_line(v: float, is_kr: bool) -> str:
    mark = "🔺" if v > 0 else ("🔻" if v < 0 else "➖")
    amount = f"{v:+,.0f}원" if is_kr else f"{v:+,.2f}달러"
    return f"{mark} {amount}"


def _today_sells_kst(trades: list[dict], now: Optional[datetime] = None) -> list[dict]:
    """원장에서 오늘 **거래일**의 매도 체결만 골라낸다 — 부분 익절도 실현 손익에 포함.

    거래일은 달력 날짜가 아니라 KR 개장~US 마감을 하나로 묶은 단위다
    (엔진의 리스크 차단기와 같은 경계 — `domain.models.trading_day`).
    """
    today = trading_day((now or datetime.now(KST)).astimezone(KST))
    out = []
    for t in trades:
        if str(t.get("side", "")).upper() == "BUY":
            continue
        try:
            ts = datetime.fromisoformat(str(t.get("ts")))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if trading_day(ts.astimezone(KST)) == today:
            out.append(t)
    return out


def handle_daily_record(toss_client: TossClient, now: Optional[datetime] = None) -> str:
    """오늘의 전적 — 금일 청산분 실현 손익 + 보유분 평가 손익 + 합계.

    실현 손익은 매도 체결의 realized_pnl에서 그 체결 수수료(세금 포함)를 뺀 값 —
    체결 알림 메시지와 같은 기준. 매수 수수료는 누적 스코어보드(/scoreboard)에서
    라운드트립 단위로 정산된다.
    """
    try:
        from quant.control.ledger import load_trades
        sells = _today_sells_kst(load_trades(LEDGER_PATH), now)
    except Exception as exc:  # noqa: BLE001
        return f"오늘의 전적 생성 실패: {exc}"

    names = _watchlist_names()
    try:
        usd_krw = float(toss_client.usd_krw())
    except Exception:  # noqa: BLE001
        usd_krw = 1400.0  # 시세 실패 시 보수적 고정 — 합계 표기에만 쓰인다

    lines = ["🗓 오늘의 전적", "   (한국장 개장 ~ 미국장 마감을 하루로 봅니다)", ""]

    # --- 청산 완료(실현) — (종목, 전략)별 합산 ---
    realized_krw = 0.0
    by_key: dict[tuple[str, str], list[dict]] = {}
    for s in sells:
        by_key.setdefault((str(s["symbol"]), str(s.get("strategy_id", "?"))), []).append(s)

    lines.append("✅ 청산 완료 · 실현 손익")
    if not by_key:
        lines.append("   오늘 청산한 종목 없음")
    for (sym, strat), fills in by_key.items():
        is_kr = sym.isdigit() and len(sym) == 6
        pnl = sum(float(f.get("realized_pnl") or 0) - float(f.get("fee") or 0) for f in fills)
        realized_krw += pnl if is_kr else pnl * usd_krw
        label = STRATEGY_LABELS.get(strat, strat)
        lines.append(f"🏢 {_display_symbol(sym, names)} · {label}")
        lines.append(f"   {_pnl_line(pnl, is_kr)} (매도 {len(fills)}건)")
    lines.append("")

    # --- 보유 중(평가) ---
    unrealized_krw = 0.0
    quote_fail = False
    portfolio = read_portfolio_summary()
    positions = {
        sym: p for sym, p in ((portfolio or {}).get("positions") or {}).items()
        if float(p.get("qty", 0) or 0) > 0
    }
    lines.append("📦 보유 중 · 평가 손익")
    if not positions:
        lines.append("   보유 종목 없음")
    else:
        quotes: dict[str, float] = {}
        try:
            quotes = _parse_quote_rows(toss_client.prices(list(positions)))
        except Exception as exc:  # noqa: BLE001
            log(f"오늘의 전적 시세 조회 실패: {exc}")
        for sym, p in positions.items():
            is_kr = sym.isdigit() and len(sym) == 6
            qty = float(p.get("qty", 0))
            avg = float(p.get("avg_cost", 0))
            cur = quotes.get(sym)
            if cur is None:
                quote_fail = True
                lines.append(f"🏢 {_display_symbol(sym, names)}")
                lines.append("   시세 조회 실패 — 합계에서 제외")
                continue
            pnl = (cur - avg) * qty
            pct = (cur / avg - 1) * 100 if avg else 0.0
            unrealized_krw += pnl if is_kr else pnl * usd_krw
            lines.append(f"🏢 {_display_symbol(sym, names)}")
            lines.append(f"   {_pnl_line(pnl, is_kr)} ({pct:+.2f}%)")
    lines.append("")

    total = realized_krw + unrealized_krw
    lines.append(f"💵 실현 합계 {realized_krw:+,.0f}원 | 평가 합계 {unrealized_krw:+,.0f}원")
    suffix = " (시세 실패분 제외)" if quote_fail else ""
    lines.append(f"📈 오늘 합계 {_pnl_line(total, True)}{suffix}")
    return "\n".join(lines)


HELP_TEXT = """📖 사용 가능한 명령어

📊 조회 (즉시 응답)
/balance (/잔고, /자산) — 현금·보유 종목별 평단가/현재가/수익률·총 자산
/scoreboard (/성적) — 전략별 누적 승률·payoff·거래당 bps
/status — 거래 상태·현금·보유 심볼 + 오늘의 전적(금일 실현/평가 손익)
/watchlist — 관심종목 목록

⚙️ 제어 (즉시 반영)
/halt [사유] (/rest [사유]) — 신규 진입 중단 REST 상태 (청산은 계속)
/resume (/live) — 재개
/flatten (/flatten all) — 전량 청산 요청
/flatten day (/단타청산) — 단타 전략 보유분만 즉시 청산 (오버나이트 전략 보유분은 유지)

📝 관심종목
/watch <심볼...> — 추가 (여러 개 가능)
/unwatch <심볼...> — 제거
/watchlist-reset — 전체 초기화
/watchlist-reset-kr — 한국 종목만 초기화 (미국은 유지)
/watchlist-reset-us — 미국 종목만 초기화 (한국은 유지)

⏰ 자동 초기화: 평일 08:30(한국) · 21:40(미국) — 각 시장 개장 전
   초기화 직후 자동 편입이 돌고, 그 뒤 직접 추가하시면 됩니다.

그 외 일반 문장은 Claude가 답합니다 (수십 초 소요)."""


def handle_query_command(text: str, toss_client: TossClient) -> Optional[str]:
    """조회 명령이면 응답 텍스트를, 아니면 None. 전부 로컬+시세 1회 — Claude 미경유."""
    lower = text.strip().lower()
    if lower in ("/balance", "/잔고", "/자산", "/portfolio"):
        return handle_balance(toss_client)
    if lower in ("/scoreboard", "/성적", "/성적표"):
        return handle_scoreboard()
    if lower in ("/help", "/명령어", "/start"):
        return HELP_TEXT
    return None


def handle_watchlist_command(text: str, toss_client: TossClient) -> Optional[str]:
    """관심종목 명령이면 응답 텍스트를, 아니면(일반 대화) None을 반환한다."""
    stripped = text.strip()
    lower = stripped.lower()

    if lower == "/watchlist":
        return format_watchlist()

    if lower in ("/watchlist-reset", "/watchlist_reset"):
        return handle_watchlist_reset()

    # 시장별 초기화: `/watchlist-reset-kr`, `/watchlist-reset-us`
    if lower in ("/watchlist-reset-kr", "/watchlist_reset_kr"):
        return handle_watchlist_reset(market="KR")
    if lower in ("/watchlist-reset-us", "/watchlist_reset_us"):
        return handle_watchlist_reset(market="US")

    if lower == "/watch" or lower.startswith("/watch "):
        return handle_watch(stripped[len("/watch"):].strip(), toss_client)

    if lower == "/unwatch" or lower.startswith("/unwatch "):
        return handle_unwatch(stripped[len("/unwatch"):].strip())

    return None


# ---------------------------------------------------------------------------
# claude -p 서브프로세스
# ---------------------------------------------------------------------------
def build_claude_argv(prompt: str, use_continue: bool = True) -> list[str]:
    argv = ["claude", "-p", prompt, "--model", "sonnet"]
    if use_continue:
        argv.append("--continue")
    return argv


def _claude_env() -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] = "0"
    return env


def run_claude(prompt: str, cwd: Path = REPO_ROOT) -> tuple[bool, str]:
    """claude -p를 실행. --continue 실패 시(최초 세션 등) 1회 폴백 재시도.

    반환: (성공 여부, stdout 또는 사유 메시지)
    """
    for use_continue in (True, False):
        argv = build_claude_argv(prompt, use_continue=use_continue)
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                env=_claude_env(),
                capture_output=True,
                text=True,
                timeout=CLAUDE_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            return False, "시간 초과 (240s)"
        except OSError as exc:
            return False, f"실행 실패: {exc}"

        if proc.returncode == 0:
            return True, proc.stdout
        if use_continue:
            log(f"--continue 실패(rc={proc.returncode}), 폴백 재시도: {proc.stderr[:200]}")
            continue
        return False, f"claude 종료 코드 {proc.returncode}: {proc.stderr[:200]}"
    return False, "알 수 없는 오류"


# ---------------------------------------------------------------------------
# 텔레그램 API
# ---------------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token: str, client: Optional[httpx.Client] = None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = client or httpx.Client(timeout=GETUPDATES_TIMEOUT + 10)

    def get_updates(self, offset: Optional[int]) -> list[dict]:
        params = {"timeout": GETUPDATES_TIMEOUT}
        if offset is not None:
            params["offset"] = offset
        resp = self.client.get(f"{self.base}/getUpdates", params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", [])

    def send_message(self, chat_id: int, text: str) -> None:
        self.client.post(
            f"{self.base}/sendMessage", data={"chat_id": chat_id, "text": text}
        )

    def send_typing(self, chat_id: int) -> None:
        self.client.post(
            f"{self.base}/sendChatAction",
            data={"chat_id": chat_id, "action": "typing"},
        )


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_sigterm(signum, frame) -> None:
    global _shutdown
    _shutdown = True


def _command_name(text: str) -> str:
    """사용자 실패 메시지에 쓸 명령 이름 — 첫 토큰만 취한다 (인자 제외)."""
    return text.strip().split(None, 1)[0] if text.strip() else "명령"


def _notify_command_failure(tg: TelegramClient, chat_id: int, command: str, exc: Exception) -> None:
    """명령 처리 실패를 사용자에게 알린다. 발송 자체가 실패할 수 있으므로 감싼다
    (무한 재귀 방지 — 실패 알림의 실패까지 사용자에게 알리려 들지 않는다)."""
    try:
        tg.send_message(
            chat_id,
            f"⚠️ {command} 처리 실패({type(exc).__name__}) — 서버 로그 확인. "
            "상태가 불확실하니 /status 로 확인하세요.",
        )
    except Exception as send_exc:  # noqa: BLE001 — 최후 방어선, 로그만 남기고 삼킨다
        log(f"실패 응답 발송도 실패({command}): {send_exc}")


def process_update(
    tg: TelegramClient,
    chat_id: int,
    limiter: RateLimiter,
    control: TradingControl,
    toss_client: TossClient,
    update: dict,
) -> None:
    if not is_allowed_chat(update, chat_id):
        uid = update.get("message", {}).get("chat", {}).get("id")
        log(f"허용되지 않은 chat_id 무시: {uid}")
        return

    text = extract_text(update)
    if text is None:
        return

    # 제어 명령/관심종목 명령은 분당 한도(RateLimiter)를 우회한다 — 클로드 호출
    # 과금/부하를 막기 위한 한도이지, 즉시 반영돼야 하는 로컬 명령까지 묶으면 안 된다.
    #
    # 아래 두 핸들러 호출은 개별적으로 try/except로 감싼다 — /halt /resume /flatten
    # (거래 제어)과 /watch /unwatch /watchlist-reset(파일 잠금·저장 I/O)에서 예외가
    # 나면 바깥 메인 루프의 광역 try/except(main())가 로그만 남기고 사용자는 완전한
    # 침묵을 받는 문제가 있었다(2026-08-26 감사 발견). 원 예외는 기존 로그 경로와
    # 같은 형식으로 여기서도 남기고, 사용자에게는 실패를 알려 상태 불확실성을
    # /status로 직접 확인하게 한다.
    try:
        control_reply = handle_control_command(text, control, toss_client)
    except Exception as exc:  # noqa: BLE001 — 사용자 알림을 위한 좁은 캐치, 삼키지 않고 로그+응답
        log(f"제어 명령 처리 실패({text!r}): {exc}")
        _notify_command_failure(tg, chat_id, _command_name(text), exc)
        return
    if control_reply is not None:
        tg.send_message(chat_id, control_reply)
        return

    try:
        watchlist_reply = handle_watchlist_command(text, toss_client)
    except Exception as exc:  # noqa: BLE001 — 사용자 알림을 위한 좁은 캐치, 삼키지 않고 로그+응답
        log(f"관심종목 명령 처리 실패({text!r}): {exc}")
        _notify_command_failure(tg, chat_id, _command_name(text), exc)
        return
    if watchlist_reply is not None:
        tg.send_message(chat_id, watchlist_reply)
        return

    query_reply = handle_query_command(text, toss_client)
    if query_reply is not None:
        tg.send_message(chat_id, query_reply)
        return

    if not limiter.allow():
        tg.send_message(chat_id, "잠시 후 다시 (분당 한도)")
        return

    tg.send_typing(chat_id)
    prompt = build_claude_prompt(text)
    ok, output = run_claude(prompt)
    if ok:
        tg.send_message(chat_id, truncate_reply(output.strip() or "(빈 응답)"))
    else:
        tg.send_message(chat_id, f"처리 실패: {output}")


def main() -> None:
    token, chat_id = get_config()
    tg = TelegramClient(token)
    limiter = RateLimiter()
    control = TradingControl(state_path=CONTROL_STATE_PATH)
    toss_client = get_toss_client()
    offset = load_offset()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    log("시작 — 롱폴링 대기")
    while not _shutdown:
        try:
            updates = tg.get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                process_update(tg, chat_id, limiter, control, toss_client, update)
                save_offset(offset)
                if _shutdown:
                    break
        except Exception as exc:  # noqa: BLE001 — 데몬은 한 번의 오류로 죽지 않는다
            log(f"루프 오류(무시하고 계속): {exc}")
            time.sleep(5)

    if offset is not None:
        save_offset(offset)
    log("SIGTERM 수신 — 정상 종료")


class _WatchAddUsageError(Exception):
    pass


def _parse_watch_add_argv(argv: list[str]) -> tuple[str, list[str], bool, str]:
    """`watch-add [--source auto] [--tags EVENT] [--tags-only] <심볼...>` 인자 파싱 →
    (source, tags, tags_only, arg).

    `--source`/`--tags`/`--tags-only`는 **어느 위치에나** 올 수 있다. 앞자리에만
    허용했더니 `watch-add INTC --source auto`(us_watch_discover.sh의 호출 형태)에서
    `--source`와 `auto`가 **심볼로 취급**돼 관심종목에 `--SOURCE`와 `AUTO`가
    등록됐다 — `AUTO`는 실재하는 종목(오토웹)이라 파싱 버그 하나로 의도치 않은
    종목을 거래할 뻔했다(2026-08-11 실측). 알 수 없는 `--` 접두 인자도 심볼로
    삼키지 않고 거부한다.

    `--tags`는 값 하나(`+`로 여러 태그 결합 가능, 예: `EVENT+TREND` — watch_scorer의
    `SYMBOL:TAG1+TAG2` 표기와 같은 구분자)를 받아 **이 호출의 모든 심볼에 동일하게**
    적용한다(`--source`와 같은 전역 플래그 시맨틱). 태그가 다른 심볼을 한 번에
    등록할 수는 없다 — daily_brief.sh처럼 태그별로 watch-add를 나눠 호출한다.

    `--tags-only`(값 없는 플래그, 2026-08-17 서브프로젝트 T)는 신규 등록을 막고
    이미 등록된 심볼의 태그만 갱신한다 — own_brief.sh의 FRGN_EXIT 경로 전용
    (`handle_watch`/`_handle_watch_unlocked` docstring 참고).

    잘못된 형태면 `_WatchAddUsageError`(사용법 메시지)를 던진다."""
    usage = "사용법: tg_bridge.py watch-add [--source auto] [--tags TAG[+TAG]] [--tags-only] <심볼> [심볼 ...]"
    source = "user"
    tags: list[str] = []
    tags_only = False
    symbols: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--source":
            if i + 1 >= len(argv):
                raise _WatchAddUsageError(usage)
            source = argv[i + 1]
            i += 2
            continue
        if tok == "--tags":
            if i + 1 >= len(argv):
                raise _WatchAddUsageError(usage)
            tags = [t for t in argv[i + 1].split("+") if t]
            i += 2
            continue
        if tok == "--tags-only":
            tags_only = True
            i += 1
            continue
        if tok.startswith("--"):
            raise _WatchAddUsageError(f"알 수 없는 옵션: {tok}\n{usage}")
        symbols.append(tok)
        i += 1
    arg = " ".join(symbols).strip()
    if not arg:
        raise _WatchAddUsageError(usage)
    return source, tags, tags_only, arg


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "watch-reset":
        # 개장 전 정기 초기화(크론) 경로 — 브리지 데몬과 같은 flock을 통과한다.
        # `watch-reset [KR|US]`. 시장 생략 시 전체 삭제.
        _mkt = sys.argv[2].upper() if len(sys.argv) >= 3 else None
        if _mkt is not None and _mkt not in ("KR", "US"):
            print("사용법: tg_bridge.py watch-reset [KR|US]")
            sys.exit(2)
        print(handle_watchlist_reset(market=_mkt))
    elif len(sys.argv) >= 2 and sys.argv[1] == "watch-add":
        # daily_brief.sh의 자동 /watch 경로 — 브리지 데몬과 같은 flock을 통과하므로
        # 데몬이 동시에 파일을 만져도 유실되지 않는다. stdout이 곧 결과 리포트.
        # --source auto로 provenance를 남겨야 TTL 만료 대상이 사용자 등록과 구분된다.
        try:
            source, tags, tags_only, arg = _parse_watch_add_argv(sys.argv[2:])
        except _WatchAddUsageError as exc:
            print(str(exc))
            sys.exit(2)
        print(handle_watch(arg, get_toss_client(), source=source, tags=tags, tags_only=tags_only))
    else:
        main()
