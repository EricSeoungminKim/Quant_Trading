"""운영 이상 감지 — 감시 에이전트가 읽는 입구. Phase 5.3.

## 왜 이 모듈이 필요한가

이번 주 치명적 결함 5개를 **전부 사람이 발견했다.** 그중 넷(토큰 만료, 데드라인
오판, 타임존, 시크릿 패턴)은 자동 감시가 잡을 수 있는 종류였다. 다섯째(알파벳
순서가 매수 종목을 정하던 것)는 **시스템이 정상 작동하는 것처럼 보였고** 주문도
나갔고 수익도 났다 — 그래서 사람이 체크포인트에 남아야 한다는 근거이기도 하다.

## 설계 규칙 — 상태는 셋이다

`alert`(이상) / `unknown`(모른다) / 아무것도(정상). **`unknown` 을 정상으로
합산하지 않는다.** 이 저장소가 반복해서 다친 모양이 그것이다:

- Redis 가 죽어 `stale_feeds()` 가 빈 목록을 돌려주면 "죽은 피드 없음"이 아니다.
- `coverage()` 가 `None` 이면 "커버리지 정상"이 아니다.
- 캐시 실패 시 0 을 돌려주면 "중복이니 건너뜀"으로 읽혀 데이터가 사라졌다.

## 순수하다

전부 **이미 읽어온 데이터**를 받는다. 파일·소켓·프로세스를 여기서 만지지 않는다 —
그래서 실패 상황(Redis 죽음, 권한 없음, 로그 없음)을 전부 테스트로 재현할 수 있다.
I/O 와 배선은 `quant.apps.cli health` 가 한다.

`quant.control` 은 `quant.trade` 를 임포트할 수 없다(아키텍처 규칙). 봉 신선도
임계값처럼 거래 평면과 공유해야 하는 값은 **인자로 주입받는다** — 여기 숫자를
따로 적으면 언젠가 갈라지고, 갈라진 쪽이 조용한 쪽이 된다.
"""
from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from quant.control.selections import _natural_key as _selection_natural_key
from quant.core.log_redact import redact

ALERT = "alert"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Finding:
    check: str
    level: str   # ALERT | UNKNOWN — 정상이면 Finding 을 만들지 않는다
    detail: str

    def to_dict(self) -> dict:
        return {"check": self.check, "level": self.level, "detail": self.detail}


def _age(then: str | datetime | None, now: datetime) -> timedelta | None:
    if then is None:
        return None
    if isinstance(then, str):
        try:
            then = datetime.fromisoformat(then)
        except ValueError:
            return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return now - then


# ── 작업(크론·타이머)이 최근에 성공했나 ───────────────────────────────────

def job_findings(snapshot: dict) -> list[Finding]:
    """`quant.control.opstate.snapshot()` 결과를 읽는다.

    `available: False` 는 **"이상 없음"이 아니다** — Redis 가 죽어 아무것도 모르는
    상태다. 그 자체를 하나의 `unknown` 으로 올린다.
    """
    if not snapshot.get("available"):
        return [Finding("jobs", UNKNOWN,
                        "운영 상태 저장소(Redis)를 읽지 못했다 — 작업 성공 여부를 모른다")]
    out: list[Finding] = []
    for job, state in sorted((snapshot.get("jobs") or {}).items()):
        if state.get("last") is None:
            # 기록이 **아예** 없다. "실패했다"가 아니라 "계측되지 않았거나 한 번도
            # 안 돌았다"다. 이걸 alert 로 내면 계측이 붙기 전까지 매번 거짓 경보가
            # 오고, 거짓 경보가 오는 감시는 꺼진다.
            out.append(Finding("jobs", UNKNOWN,
                               f"{job}: 기록이 없다 — 계측 전이거나 한 번도 돌지 않았다"))
        elif not state.get("fresh"):
            out.append(Finding("jobs", ALERT,
                               f"{job}: 최근 성공이 없다 (마지막 시도 {state['last']})"))
        elif not state.get("ok"):
            out.append(Finding("jobs", ALERT,
                               f"{job}: 마지막 실행이 실패 — {state.get('detail') or '상세 없음'}"))
    return out


# ── 피드가 살아 있나 ──────────────────────────────────────────────────────

def feed_findings(market: str, stale: list[str], store_healthy: bool,
                  configured: list[str] | None = None) -> list[Finding]:
    """`opstate.stale_feeds()` 의 결과. **저장소가 죽었으면 빈 목록이 나온다** —
    그걸 "죽은 피드 없음"으로 읽지 않도록 healthy 를 함께 받는다.

    `configured` 는 **현재 설정에 있는 피드 이름**이다. 넘기면 거기 없는 이름은
    걸러낸다 — "죽은 피드"와 "설정에서 빠진 피드"는 다른 사건이기 때문이다.
    2026-08-13 실측: 피드가 `연합뉴스` → `연합뉴스_경제` 로 개편됐는데 마지막 성공
    시각 zset 에 옛 이름이 남아, 감시가 영구히 "오래 성공하지 못한 피드"를 외쳤다.

    `None` 이면 걸러내지 않는다 — 설정을 못 구한 상태에서 조용해지는 쪽이 더 나쁘다.
    """
    if not store_healthy:
        return [Finding("feeds", UNKNOWN,
                        f"{market}: 상태 저장소를 읽지 못했다 — 피드 생존을 모른다")]
    if configured is not None:
        known = set(configured)
        stale = [name for name in stale if name in known]
    if stale:
        return [Finding("feeds", ALERT,
                        f"{market}: 오래 성공하지 못한 피드 {len(stale)}개 — {', '.join(sorted(stale)[:8])}")]
    return []


# ── 봉이 신선한가 (라이브 사이징에 걸린 파일) ─────────────────────────────

def bar_findings(last_bars: dict[str, str | None], now: datetime,
                 max_age: timedelta) -> list[Finding]:
    """`{"QQQ 1d": "2026-08-12T04:00:00+00:00" | None}`.

    `None` 은 `coverage()` 가 답하지 못한 것이고 **"봉 0개"와 다르다.**
    `max_age` 는 거래 평면의 상수를 주입받는다(`regime.provider.STALE_DAILY_BARS_AFTER`).
    """
    out: list[Finding] = []
    for key, last in sorted(last_bars.items()):
        age = _age(last, now)
        if age is None:
            out.append(Finding("bars", UNKNOWN,
                               f"{key}: 커버리지를 읽지 못했다 — 봉이 최신인지 모른다"))
        elif age > max_age:
            out.append(Finding("bars", ALERT,
                               f"{key}: 마지막 봉이 {age.days}일 낡았다 (임계 {max_age.days}일) — "
                               "regime 이 이 파일로 사이징 배수를 계산한다"))
    return out


# ── 1분봉 적재 신선도 (scalp_1m 표본 축적, Phase 5.3 추가) ──────────────────

def intraday_history_findings(last_bars: dict[str, str | None], now: datetime,
                              max_age: timedelta = timedelta(days=2)) -> list[Finding]:
    """`data/history/{symbol}/1m/` 적재가 살아있나 — Toss 1m은 4거래일 롤링만
    제공하므로(scalp_1m.py 모듈 docstring "데이터" 절), 매일 `backfill_1m.sh`가
    쌓지 않으면 표본이 그냥 사라진다. `bar_findings`(일봉 신선도)와 축이 다르다 —
    거래 사이징이 아니라 백테스트 표본 축적이 걸려 있다.

    `last_bars`는 호출부(`quant.apps.cli cmd_health`)가 `data/history/*/1m/`
    디렉토리를 스캔해 이미 만든 `{symbol: coverage().last_ts | None}`이다.

    **`last_bars`가 비어 있으면(빈 dict) UNKNOWN도 내지 않고 조용히 빈 목록.**
    `bar_findings`와 다른 관례를 여기서 쓰는 이유: 1m 디렉토리가 **하나도 없는
    것**은 "적재가 죽었다"가 아니라 백필 크론(05:40)이 아직 한 번도 안 돈
    신규 설치·번인 초기 상태일 수 있다 — 그때마다 경보가 오면 감시가 꺼진다
    (이 모듈이 반복해서 지키는 규율: 거짓 경보가 오는 감시는 꺼진다). 디렉토리가
    하나라도 있으면(=적재가 최소 한 번은 성공했다는 뜻) 그때부터는 심볼별로
    본다 — 개별 값이 `None`이면(coverage 실패) UNKNOWN, `max_age`(기본 2일)를
    넘기면 ALERT.
    """
    if not last_bars:
        return []
    out: list[Finding] = []
    for symbol in sorted(last_bars):
        age = _age(last_bars[symbol], now)
        if age is None:
            out.append(Finding("intraday_history", UNKNOWN,
                               f"{symbol}: 1m 적재 신선도를 읽지 못했다"))
        elif age > max_age:
            out.append(Finding("intraday_history", ALERT,
                               f"{symbol}: 1m 마지막 봉이 {age.days}일 낡았다 (임계 {max_age.days}일) — "
                               "1m 적재가 멈추면 scalp_1m 백테스트 표본이 다시 안 쌓인다"))
    return out


# ── 시계 ──────────────────────────────────────────────────────────────────

def clock_findings(utc_offset_seconds: int | None, engine_stamp: str | None,
                   now: datetime, tolerance: timedelta = timedelta(minutes=5)) -> list[Finding]:
    """호스트 타임존 + 엔진이 찍은 시각과 관측 시각의 괴리.

    타임존이 KST 가 아니면 편입 데드라인·세션 경계가 통째로 어긋난다 — 이 저장소는
    이미 9시간 오차와 데드라인 오판을 겪었다. `own_brief.sh` 도 같은 가드를 갖는다.
    """
    out: list[Finding] = []
    if utc_offset_seconds is None:
        out.append(Finding("clock", UNKNOWN, "호스트 타임존을 읽지 못했다"))
    elif utc_offset_seconds != 9 * 3600:
        out.append(Finding("clock", ALERT,
                           f"호스트 타임존이 KST(+09:00)가 아니다 (+{utc_offset_seconds // 3600:02d}:00) — "
                           "세션 경계·편입 데드라인이 어긋난다"))
    age = _age(engine_stamp, now)
    if engine_stamp is None or age is None:
        out.append(Finding("clock", UNKNOWN, "엔진이 찍은 시각을 읽지 못했다"))
    elif abs(age) > tolerance:
        out.append(Finding("clock", ALERT,
                           f"엔진 하트비트 시각이 관측 시각과 {int(abs(age).total_seconds())}초 어긋난다"))
    return out


# ── 원장 ↔ 포트폴리오 ────────────────────────────────────────────────────

def positions_from_trades(trades: list[dict], boundary_ts: datetime | None = None) -> dict[str, float]:
    """체결 원장에서 종목별 보유 수량을 재구성한다. buy 는 +, sell 은 −.

    `boundary_ts`가 있으면(실계좌 이식 경계, `quant.control.ledger.
    seeding_boundary_ts`) 그 시각 이하의 체결은 재구성에서 뺀다 — 이식 시점에
    포트폴리오 전체가 실계좌 스냅샷으로 갈아끼워지므로 그 이전 원장 잔량은
    더 이상 유효하지 않다. `quant.control.ledger.round_trips`가 라운드트립을
    셀 때 쓰는 것과 같은 경계다(2026-09-04) — 안 쓰면 이식 이전 paper 시대
    매매가 실계좌 스냅샷과 이중으로 잡혀, 이미 이관 정리로 청산된 종목이
    "원장 재구성 N vs 포트폴리오 0"으로 매시간 오경보됐다(실측: 000500/
    001450/007070/007340/GLDM/GOOGL 등, 452회 누적 10회/일).

    ts 를 못 읽는 행은 `boundary_ts` 가 있을 때 **보수적으로 제외**한다 —
    경계 이전인지 알 수 없는 걸 이후로 단정하지 않는다.

    **캐리오버 행(`quant.control.ledger.SEEDING_CARRY_MARKER`, 2026-09-05 D3)은
    이 시각 필터를 건너뛴다** — `cmd_seed_real`이 유지 종목(예: 005930)의 이월
    수량에 남기는 합성 buy는 그 정의상 경계 시각 그 자체(또는 그 이전)에
    찍힌다. 일반 규칙(`ts <= boundary_ts`면 제외)을 그대로 적용하면 이 행마저
    걸러져 유지 종목의 시작 잔량이 다시 0으로 재구성되고, 그 뒤 매도만큼
    영구히 "원장 재구성 -N vs 포트폴리오 0"으로 오탐한다(실측: 005930)."""
    from quant.control.ledger import is_seeding_carry

    qty: dict[str, float] = {}
    for rec in trades:
        if boundary_ts is not None and not is_seeding_carry(rec):
            ts: datetime | None
            try:
                ts = datetime.fromisoformat(str(rec.get("ts")))
            except ValueError:
                ts = None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts is None or ts <= boundary_ts:
                continue
        symbol = str(rec.get("symbol") or "").strip()
        side = str(rec.get("side") or "").strip().lower()
        if not symbol or side not in ("buy", "sell"):
            continue
        delta = float(rec.get("qty") or 0)
        qty[symbol] = qty.get(symbol, 0.0) + (delta if side == "buy" else -delta)
    return qty


def ledger_portfolio_findings(trades: list[dict], portfolio: dict | None,
                              tolerance: float = 1e-6) -> list[Finding]:
    """원장에서 재구성한 수량과 포트폴리오 상태가 맞나.

    **실거래에서 20주 중 8주만 채워지면 원장은 20주로 안다** — paper 는 즉시 체결이라
    이 불일치가 안 보인다. Phase 6(OMS)가 근본 해결이고, 여기서는 감지만 한다.

    실계좌 이식 경계(`quant.control.ledger.seeding_boundary_ts`) 이전 체결은
    재구성에서 뺀다(`positions_from_trades` 참고) — 이식으로 포트폴리오가
    통째로 갈아끼워진 뒤에는 그 이전 잔량이 무의미하기 때문이다. `quant.
    control.ledger`는 이 모듈과 같은 평면(`quant/control/`)이라 임포트해도
    아키텍처 규칙에 걸리지 않는다(`selections._natural_key`를 이미 같은
    방식으로 쓴다).
    """
    if portfolio is None:
        return [Finding("ledger", UNKNOWN, "포트폴리오 상태를 읽지 못했다 — 대사 불가")]
    from quant.control.ledger import seeding_boundary_ts

    boundary_ts = seeding_boundary_ts(trades)
    from_trades = positions_from_trades(trades, boundary_ts)
    held = {
        sym: float((pos or {}).get("qty") or 0)
        for sym, pos in (portfolio.get("positions") or {}).items()
    }
    out: list[Finding] = []
    for symbol in sorted(set(from_trades) | set(held)):
        a, b = from_trades.get(symbol, 0.0), held.get(symbol, 0.0)
        if abs(a - b) > tolerance:
            out.append(Finding("ledger", ALERT,
                               f"{symbol}: 원장 재구성 {a:g} vs 포트폴리오 {b:g}"))
    return out


# ── 반복 알림 억제 (2026-09-04) ────────────────────────────────────────────

def alert_fingerprint(f: Finding) -> str:
    """Finding 하나의 안정적 지문(check+level+detail 해시) — 반복 알림 억제
    (`dedupe_repeat_alerts`)의 키. 같은 사유가 같은 문구로 다시 나오면 같은
    지문이 나온다."""
    key = f"{f.check}|{f.level}|{f.detail}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def dedupe_repeat_alerts(
    findings: list[Finding], last_alerted: dict[str, str], now: datetime,
    window: timedelta = timedelta(hours=24),
) -> tuple[list[Finding], list[Finding], dict[str, str]]:
    """반복 알림 억제 — Finding 단위 지문으로 `window`(기본 24시간) 안에 이미
    통보한 발견은 통보 목록에서 뺀다. **로그에는 여전히 남는다** — 호출부가
    원본 findings(억제 여부와 무관)를 그대로 기록한다. 이 함수는 "통보할지"만
    답한다.

    유래: `ops_watch.sh`의 기존 억제는 **보고서 전체**를 findings detail
    목록 하나로 묶어 해시화해 12시간 지나면 무조건 다시 알렸다 — 다른 발견
    하나가 새로 생기거나 사라지기만 해도 전체 해시가 바뀌어, 몇 주째 안
    변한 "원장 재구성 vs 포트폴리오" 불일치까지(452회 누적, 하루 10회) 매번
    같이 다시 통보됐다. Finding 단위로 쪼개면 그 항목만 억제되고 새로 생긴
    발견은 그대로 통과한다.

    반환: (통보할 것, 억제된 것, 갱신된 last_alerted). `last_alerted`는
    {지문: 마지막으로 **통보**한 시각 ISO} — 호출부가 디스크에 영속화한다
    (이 함수는 순수 계산만 한다). 억제된 항목은 맵의 시각을 갱신하지 않는다
    — 억제 자체가 재알림 시계를 리셋하면 window를 넘겨도 계속 억제되는
    영구 침묵이 생긴다."""
    to_notify: list[Finding] = []
    suppressed: list[Finding] = []
    updated = dict(last_alerted)
    for f in findings:
        h = alert_fingerprint(f)
        last = last_alerted.get(h)
        last_dt: datetime | None = None
        if last is not None:
            try:
                last_dt = datetime.fromisoformat(last)
            except ValueError:
                last_dt = None
            if last_dt is not None and last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        if last_dt is not None and (now - last_dt) < window:
            suppressed.append(f)
            continue
        to_notify.append(f)
        updated[h] = now.isoformat()
    return to_notify, suppressed, updated


# ── 시크릿이 로그에 새고 있나 ─────────────────────────────────────────────

def secret_findings(lines: list[str]) -> list[Finding]:
    """로그 줄에 시크릿 형태가 남아 있나.

    **찾은 값을 절대 그대로 올리지 않는다** — 경보 자체가 유출 경로가 되면 안 된다.
    `core.log_redact.redact()` 를 그대로 쓴다(형태 기반 패턴이 이미 실측 사례로
    회귀 고정돼 있다: 텔레그램 봇 토큰이 이틀간 381번 평문으로 남았던 사건).
    """
    leaked = 0
    samples: list[str] = []
    for line in lines:
        masked = redact(line)
        if masked != line:
            leaked += 1
            if len(samples) < 3:
                samples.append(masked.strip()[:160])
    if not leaked:
        return []
    return [Finding("secrets", ALERT,
                    f"로그에 시크릿 형태가 {leaked}줄 남아 있다 (마스킹된 예: "
                    + " | ".join(samples) + ")")]


# ── 타이머가 발화했나 ────────────────────────────────────────────────────

def timer_findings(units: dict[str, str | None], expected: list[str]) -> list[Finding]:
    """`{unit: 마지막 발화 ISO 시각 | None}`. `expected` 에 있는데 없으면 이상.

    `None`(한 번도 발화 안 함)은 **설치 직후엔 정상**이라 여기서 구분하지 않는다 —
    "유닛이 아예 없다"만 잡는다. 최근 성공 여부는 `job_findings` 가 본다.
    """
    out: list[Finding] = []
    for unit in expected:
        if unit not in units:
            out.append(Finding("timers", ALERT, f"{unit}: 타이머가 등록돼 있지 않다"))
    return out


# ── 설치본이 저장소와 갈렸나 ─────────────────────────────────────────────

def install_drift_findings(installed: str | None, repo: str | None,
                           what: str = "crontab") -> list[Finding]:
    """설치된 설정과 저장소 파일의 실질 차이.

    **2026-08-13 실측**: EC2 crontab 이 Phase 1 이전 것이라 없어진 모듈
    (`quant_engine.run`)을 부르고 있었다. 저장소는 옳고 설치본만 낡는 이 부류는
    테스트로도 배포로도 안 잡힌다 — 아무도 대조하지 않았기 때문이다.

    주석·빈 줄·순서는 무시한다(같은 내용이 다르게 보이면 사람이 경보를 끈다).
    """
    if installed is None or repo is None:
        return [Finding("install_drift", UNKNOWN, f"{what}: 설치본 또는 저장소 쪽을 읽지 못했다")]

    def _norm(text: str) -> set[str]:
        return {
            " ".join(line.split())
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    only_installed = _norm(installed) - _norm(repo)
    only_repo = _norm(repo) - _norm(installed)
    if not only_installed and not only_repo:
        return []
    parts = []
    if only_installed:
        parts.append(f"설치본에만 {len(only_installed)}줄")
    if only_repo:
        parts.append(f"저장소에만 {len(only_repo)}줄")
    sample = sorted(only_installed or only_repo)[0][:120]
    return [Finding("install_drift", ALERT,
                    f"{what}: 설치본과 저장소가 다르다 ({', '.join(parts)}). 예: {sample}")]


# ── 원장이 살아있나 (DART·텔레그램·외국인 수급·선정) ────────────────────

# 각 원장이 마르면 조용히 죽는 게 무엇인지. 경보만 보고도 "왜 신경써야 하나"가
# 바로 보이게 한다(2026-08-14 리포트 결측 사고와 같은 이유 — 사실은 이미 기록돼
# 있었는데 아무도 읽지 않았다). 목록에 없는 이름은 일반 문구로 대체한다.
_LEDGER_WHY: dict[str, str] = {
    "disclosures": "DART 공시가 끊기면 뉴스 없는 공시 이벤트 축이 조용히 죽는다",
    "telegram_msgs": "텔레그램 원장이 마르면 장중 시황방 근거가 리포트에서 빠진다",
    "frgn_flow": "frgn_flow 가 마르면 단타 점수의 외국인 축이 조용히 0이 된다",
    "selections": "선정 원장이 안 쌓이면 전방 수익률 표본이 끊겨 되먹임 고리가 죽는다",
}


def ledger_findings(last_ts_by_name: dict[str, str | None], now: datetime,
                    max_age_by_name: dict[str, timedelta]) -> list[Finding]:
    """원장별 마지막 기록 시각이 신선한가.

    `last_ts_by_name[name]` 이 `None` 이면 파일이 없거나 마지막 줄을 못 읽은
    것이다 — **"원장이 비었다"가 아니라 "쌓이는지 모른다"**다(이 모듈의 반복되는
    규율: 모름을 정상으로 합산하지 않는다). 나이가 임계를 넘으면 며칠 낡았는지와
    임계, 그리고 왜 중요한지를 한 줄에 같이 낸다 — 그래야 사람이 "장중이라
    그런가"로 미루지 않는다.
    """
    out: list[Finding] = []
    for name in sorted(last_ts_by_name):
        last = last_ts_by_name[name]
        age = _age(last, now)
        if age is None:
            out.append(Finding("ledgers", UNKNOWN,
                               f"{name}: 원장을 읽지 못했다 — 쌓이는지 모른다"))
            continue
        max_age = max_age_by_name[name]
        if age > max_age:
            why = _LEDGER_WHY.get(name, "이 원장이 마르면 의존하는 선정/점수가 조용히 비어간다")
            out.append(Finding("ledgers", ALERT,
                               f"{name}: 마지막 기록이 {age.days}일 낡았다 (임계 {max_age.days}일) — {why}"))
    return out


# ── 백업이 최근에 성공했나 ───────────────────────────────────────────────

def backup_findings(bundle_stamp: str | None, pull_stamp: str | None, now: datetime,
                    max_bundle_age: timedelta = timedelta(days=2),
                    max_pull_age: timedelta = timedelta(days=7)) -> list[Finding]:
    """번들 생성 시각과 **오프사이트로 당겨간** 시각을 따로 본다.

    번들만 최신이면 아직 EC2 디스크 한 곳에 있는 것이고, 그게 원래 위험이었다.
    당겨가는 쪽이 Mac 이라 며칠 꺼져 있을 수 있어 임계를 넉넉히 잡되, 조용히 몇 주
    안 당겨오는 상태는 잡는다.
    """
    out: list[Finding] = []
    bundle_age = _age(bundle_stamp, now)
    if bundle_age is None:
        out.append(Finding("backup", UNKNOWN, "번들 생성 시각을 읽지 못했다 — 백업이 도는지 모른다"))
    elif bundle_age > max_bundle_age:
        out.append(Finding("backup", ALERT,
                           f"최신 번들이 {bundle_age.days}일 낡았다 (임계 {max_bundle_age.days}일)"))
    pull_age = _age(pull_stamp, now)
    if pull_age is None:
        out.append(Finding("backup", UNKNOWN,
                           "오프사이트 사본 시각을 읽지 못했다 — 번들이 EC2 디스크 한 곳에만 있을 수 있다"))
    elif pull_age > max_pull_age:
        out.append(Finding("backup", ALERT,
                           f"오프사이트로 당겨간 지 {pull_age.days}일 됐다 (임계 {max_pull_age.days}일) — "
                           "지금 디스크가 죽으면 그만큼 잃는다"))
    return out


# ── 앵커 봉 값 자체가 타당한가 (신선도와 다른 축) ────────────────────────

def bar_sanity_findings(bars_by_key: dict[str, list[dict] | None],
                        now: datetime) -> list[Finding]:
    """봉이 **있다**만으로는 안 잡히는 오염을 본다 — `bar_findings` 는 신선도만 본다.

    유래: yfinance KR 1d 가 tz 변환으로 **하루 어긋난 날짜**를 저장한 실측 사고
    (e87efa4) — regime 이 이 파일로 사이징 배수를 계산하다 13일간 잘못 사이징했다.
    "봉이 최신이다"는 참이었는데 값 자체가 틀렸다.

    `bars_by_key["QQQ 1d"]` 는 **오래된 것 → 최신 순**으로 정렬된 마지막 2개 봉
    (`{"ts", "open", "high", "low", "close"}`, ts 는 ISO 문자열 또는 datetime).
    `None`/빈 리스트는 못 읽은 것 — `bar_findings` 와 같은 관례로 UNKNOWN(0개는
    "정상"이 아니다).
    """
    out: list[Finding] = []
    for key in sorted(bars_by_key):
        bars = bars_by_key[key]
        if not bars:
            out.append(Finding("bar_sanity", UNKNOWN, f"{key}: 봉을 읽지 못했다"))
            continue
        last = bars[-1]
        close, high, low = last.get("close"), last.get("high"), last.get("low")
        contaminated = (
            close is None or close <= 0
            or (high is not None and low is not None and high < low)
        )
        if contaminated:
            out.append(Finding("bar_sanity", ALERT,
                               f"{key}: 마지막 봉 값이 오염됐다 "
                               f"(close={close}, high={high}, low={low}) — 데이터 오염 의심"))
        age = _age(last.get("ts"), now)
        if age is not None and age < timedelta(0):
            out.append(Finding("bar_sanity", ALERT,
                               f"{key}: 마지막 봉 날짜가 미래다 (ts={last.get('ts')}) — "
                               "tz 재해석 사고 재발 신호"))
        if not contaminated and len(bars) >= 2:
            prev_close = bars[-2].get("close")
            if prev_close:
                ret = (close - prev_close) / prev_close
                if abs(ret) > 0.25:
                    out.append(Finding("bar_sanity", ALERT,
                                       f"{key}: 직전 봉 대비 {ret:+.1%} — 지수 ETF 앵커가 하루에 "
                                       "이렇게 못 움직인다 (단위 오류/스플릿 미조정 의심, "
                                       "KR 상하한 30%보다 안쪽)"))
    return out


# ── 뉴스 유량 이상 ────────────────────────────────────────────────────────

def flow_anomaly_findings(today_count: int | None, trailing_counts: list[int],
                          label: str, zero_check_active: bool) -> list[Finding]:
    """수집기가 조용히 죽는 것(0건)과 dedup 이 깨져 폭증하는 것, 두 방향 모두 이
    저장소 전례가 있다.

    `trailing_counts` 는 오늘을 제외한 직전 며칠의 일별 기사 수. **3일 미만이면
    UNKNOWN 이 아니라 빈 목록이다** — 신규 설치 직후 매시간 unknown 이 오면
    사람이 감시를 끈다("거짓 경보가 오는 감시는 꺼진다").

    `zero_check_active` 는 호출부가 KST 기준 지금이 오후 이후인지 판단해
    주입한다 — 오전엔 아직 쌓이는 중이라 0건이 정상이다. 폭증 검사는 상시다
    (dedup 은 하루 중 언제든 깨질 수 있다).
    """
    if len(trailing_counts) < 3:
        return []
    median = statistics.median(trailing_counts)
    if today_count is None:
        return [Finding("news_flow", UNKNOWN, f"{label}: 오늘 기사 수를 읽지 못했다")]
    out: list[Finding] = []
    if zero_check_active and median >= 20 and today_count == 0:
        out.append(Finding("news_flow", ALERT,
                           f"{label}: 직전 {len(trailing_counts)}일 중앙값 {median:g}건인데 "
                           "오늘 0건 — 수집기가 죽었거나 조용히 멈췄을 수 있다"))
    if median > 0 and today_count > median * 8:
        out.append(Finding("news_flow", ALERT,
                           f"{label}: 오늘 {today_count}건 — 직전 중앙값({median:g}건)의 8배 초과, "
                           "dedup 붕괴 의심"))
    return out


# ── 텔레그램 전채널 동시 침묵 ─────────────────────────────────────────────

def telegram_silence_findings(newest_by_channel: dict[str, str | None] | None,
                              now: datetime,
                              threshold: timedelta = timedelta(hours=36)) -> list[Finding]:
    """개별 방 침묵은 정상, **전 채널 동시 침묵**만 수집 경로 차단 신호다.

    유래: t.me/s 웹 프리뷰는 채널 단위로 차단될 수 있다(12방 중 1방은 이미 차단
    실측, 2026-08-17). 채널마다 발행 빈도가 달라 "이 채널이 조용하다"는 늘 있는
    일이라 개별로는 경보가 못 된다 — 전부 동시에 막히는 것만 신호다.

    `None`(원장을 못 읽음)은 UNKNOWN 1건. 타임스탬프를 읽을 수 있는 채널이 3개
    미만이면 판단할 신호가 부족해 빈 목록이다(`flow_anomaly_findings` 와 같은
    이유).
    """
    if newest_by_channel is None:
        return [Finding("telegram_silence", UNKNOWN,
                        "텔레그램 원장을 읽지 못했다 — 채널 침묵 여부를 모른다")]
    ages = [a for a in (_age(v, now) for v in newest_by_channel.values()) if a is not None]
    if len(ages) < 3:
        return []
    if all(a > threshold for a in ages):
        oldest_days = max(ages).days
        return [Finding("telegram_silence", ALERT,
                        f"관측된 채널 {len(ages)}개가 전부 {int(threshold.total_seconds() // 3600)}"
                        f"시간 이상 침묵(최장 {oldest_days}일) — 수집 경로 차단 의심")]
    return []


# ── 외국인 수급 원장 퇴화 ────────────────────────────────────────────────

def frgn_flow_degenerate_findings(recent_rows: list[dict]) -> list[Finding]:
    """상류 파싱 필드가 개편되면 0으로 조용히 채워지는 유형(연합뉴스 피드 개명
    사고와 같은 모양). frgn_flow 가 전부 0이면 단타 점수의 외국인 축이 조용히
    무의미해진다 — 신선도(`ledger_findings`)로는 안 잡힌다(파일은 계속 쌓인다).

    `recent_rows` 는 원장의 최근 기록(예: 마지막 200행, `{"date", "symbol",
    "foreign_net", ...}`). 서로 다른 날짜가 3일 이상 있고 그 안의 **모든** 행이
    `foreign_net == 0` 이면 ALERT — 시장 전체가 3일 연속 전 종목 순매수 0일 수는
    없다. 행이 없으면 빈 목록(신선도는 `ledger_findings` 가 이미 본다 — 이중 경보
    금지).
    """
    if not recent_rows:
        return []
    dates = {r.get("date") for r in recent_rows if r.get("date")}
    if len(dates) < 3:
        return []
    if all(r.get("foreign_net") == 0 for r in recent_rows):
        return [Finding("frgn_flow_degenerate", ALERT,
                        f"최근 {len(recent_rows)}행({len(dates)}일)이 전부 foreign_net=0 — "
                        "파싱 붕괴 의심(시장 전체가 여러 날 연속 전 종목 순매수 0일 수 없다)")]
    return []


# ── 선정 원장 중복 자연키 ────────────────────────────────────────────────

def selection_dup_findings(recent_rows: list[dict]) -> list[Finding]:
    """producer_version 규율(같은 (date,market,symbol,producer)은 한 번) — dedup 이
    깨지면 검증 하네스 표본이 오염돼 n≥30 승격 판정이 무의미해진다.

    자연키는 `quant.control.selections._natural_key` 를 그대로 쓴다 — 여기서
    다시 정의하면 언젠가 갈라지고, 갈라진 쪽이 조용한 쪽이 된다.

    `recent_rows` 는 원장의 최근 기록(예: 마지막 500행). 같은 자연키가 2회 이상
    나오면 ALERT(중복 종류 개수 + 예시 1개). 행이 없으면 빈 목록.
    """
    if not recent_rows:
        return []
    counts: dict[tuple, int] = {}
    for row in recent_rows:
        key = _selection_natural_key(row)
        counts[key] = counts.get(key, 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}
    if not dupes:
        return []
    (date, market, symbol, producer), n = max(dupes.items(), key=lambda kv: kv[1])
    example = f"{date}/{market}/{symbol}" + (f"/{producer}" if producer else "")
    return [Finding("selection_dup", ALERT,
                    f"선정 원장 자연키 중복 {len(dupes)}종 — 예: {example} {n}회 "
                    "(dedup 붕괴 의심, 표본이 오염된다)")]


# ── 합산 ─────────────────────────────────────────────────────────────────

def summarize(findings: list[Finding]) -> dict:
    """감시 에이전트/셸이 읽는 형태. **`unknown` 을 정상에 합산하지 않는다.**"""
    alerts = [f for f in findings if f.level == ALERT]
    unknowns = [f for f in findings if f.level == UNKNOWN]
    if alerts:
        verdict = ALERT
    elif unknowns:
        verdict = UNKNOWN
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "n_alert": len(alerts),
        "n_unknown": len(unknowns),
        "findings": [f.to_dict() for f in alerts + unknowns],
    }


# ── 리포트가 스스로 기록한 결측 ───────────────────────────────────────────

def report_findings(missing_by_market: dict[str, list[str] | None],
                    required: list[str]) -> list[Finding]:
    """리포트 `engine.json` 의 `missing` 을 읽는다.

    **왜 이게 감시에 있어야 하나.** 리포트가 API 키를 못 읽어 소스 5개가 결측인 채로
    발행됐고, 그 사실을 리포트는 `missing` 에 **이미 기록하고 있었는데 아무도 읽지
    않았다.** 사람이 빌드 출력의 "결측 5건"을 보고도 "장중이라 그런가"로 미뤘다 —
    이 저장소가 반복해서 다친 "모른다를 정상으로 읽기" 그대로다(2026-08-14).

    `required` 만 경보한다. `after_hours` 처럼 시간대에 따라 정상적으로 빠지는 소스를
    alert 로 내면 거짓 경보가 되고, 거짓 경보가 오는 감시는 꺼진다.

    `None`(오늘 리포트를 못 읽음)은 **"결측 없음"이 아니다** — 발행 자체가 실패했을 수
    있다.
    """
    out: list[Finding] = []
    for market in sorted(missing_by_market):
        missing = missing_by_market[market]
        if missing is None:
            out.append(Finding("report", UNKNOWN,
                               f"{market}: 오늘 리포트를 읽지 못했다 — 발행 실패일 수 있다"))
            continue
        blocking = sorted(set(missing) & set(required))
        if blocking:
            out.append(Finding("report", ALERT,
                               f"{market}: 필수 소스 결측 {len(blocking)}건 — "
                               f"{', '.join(blocking)} (API 키·네트워크 확인)"))
    return out


def secret_findings_for(env: dict[str, str] | None,
                        required: list[str]) -> list[Finding]:
    """필수 시크릿이 **앱이 실제로 쓰는 경로로** 읽히는지.

    "파일에 있는지"가 아니라 "앱이 읽을 수 있는지"를 본다. 2026-08-14 에 그 차이가
    사고를 만들었다 — 검증 도구(`scripts/check_keys.py`)는 자기 로더로 루트의 파일을
    읽어 "필수 키 확인 완료"를 냈고, 앱의 `DEFAULT_ENV` 는 `quant/.env.local` 을 봐서
    전부 결측이었다. **검증과 애플리케이션이 다른 코드 경로로 같은 파일을 읽으면
    둘 다 "정상"일 수 있다.**

    값은 절대 싣지 않는다 — 이름과 개수만 말한다.
    """
    if env is None:
        return [Finding("secrets", UNKNOWN,
                        "앱 경로로 .env 를 읽지 못했다 — 필수 키 도달 여부를 모른다")]
    absent = [k for k in required if not (env.get(k) or "").strip()]
    if not absent:
        return []
    return [Finding("secrets", ALERT,
                    f"필수 시크릿 {len(absent)}건이 앱 경로로 읽히지 않는다: "
                    + ", ".join(sorted(absent)))]


# ── 발행↔편입 정합 ───────────────────────────────────────────────────────

def report_intake_findings(report_exists: dict[str, bool],
                           intake_tags: dict[str, list[str] | None]) -> list[Finding]:
    """오늘 리포트가 실제로 편입에 반영됐나.

    유래(실측 사고): 2026-08-14~17 나흘간 own_brief 의 리포트 경로 기본값이 옛
    체크아웃을 가리켜 매일 rc=3(리포트 없음) → 랭킹 폴백만 돌았다. 리포트는
    정상 발행됐고 편입도 "성공"했으므로 기존 감시는 전부 정상이었다 — 아무도
    나흘간 몰랐다.

    `report_exists[market]` 는 오늘 `{market}_engine.json` 존재 여부다(파일
    존재 확인은 결정론적이라 True/False 뿐, unknown 이 없다). `intake_tags
    [market]` 는 오늘 자동 편입(`data/watchlist.yaml`, `source=="auto"` +
    `added_at` 이 오늘)된 항목의 태그 합집합 — 못 읽으면 `None`.

    리포트 유래 태그(`EVENT`/`EVENT_SCALP`/`FRGN` — 뉴스·단타·외국인 수급,
    전부 own_brief 가 리포트 engine.json 을 실제로 읽어야만 나오는 태그다)가
    하나도 없이 랭킹 유래(`TREND`)만 있으면, 리포트는 발행됐는데 편입은 랭킹
    폴백으로만 돌았다는 뜻이다 — 리포트 읽기가 조용히 실패했을 때의 모양과
    정확히 같다. 오늘 신규 편입 자체가 없으면(빈 태그 목록) 판단 대상이
    아니다(후보 0건 축은 `report_quality_findings` 가 본다).

    `report_exists[market]` 가 False 면(주말·휴장) 검사 자체를 건너뛴다 —
    리포트가 없는 게 정상인 날 매번 울리면 거짓 경보가 되고, 거짓 경보가
    오는 감시는 꺼진다.
    """
    REPORT_ORIGIN_TAGS = {"EVENT", "EVENT_SCALP", "FRGN"}
    out: list[Finding] = []
    for market in sorted(report_exists):
        if not report_exists[market]:
            continue
        tags = intake_tags.get(market)
        if tags is None:
            out.append(Finding("report_intake", UNKNOWN,
                               f"{market}: 오늘 편입 태그를 읽지 못했다"))
            continue
        if not tags:
            continue
        if "TREND" in tags and not (set(tags) & REPORT_ORIGIN_TAGS):
            out.append(Finding("report_intake", ALERT,
                               f"{market}: 오늘 리포트는 발행됐는데 편입 태그가 전부 랭킹 유래"
                               f"(TREND)뿐이다 — 리포트 읽기 실패 의심 (태그: "
                               f"{', '.join(sorted(tags))})"))
    return out


# ── 리포트 품질 회귀 ─────────────────────────────────────────────────────

def report_quality_findings(market: str, today: dict | None,
                            trailing: list[dict]) -> list[Finding]:
    """오늘 리포트가 어제보다 빈약해졌나 — 매일 발행은 되지만 아무도 안 본다.

    유래: 2026-08-14 결측 사고도 engine.json 의 `missing` 에 이미 기록돼
    있었는데 읽는 사람이 없었다(이 파일의 `report_findings` 가 그 축을 본다).
    여기는 다른 축이다 — 후보 수·AI 해석 상태가 조용히 나빠지는 것.

    실측 확인(`quant/apps/report_cli.py`): 아침 engine.json(`write_machine`
    이 쓰는 morning payload)에는 `intraday_view` 키가 없다 — 그건 close
    세션 payload(`_close_engine_json_path`) 전용이다. "단타" 대응으로 쓸 수
    있는 값은 `auto_watch` 토큰 수(`_auto_watch_count`, 뉴스+랭킹 통합 후보
    라인 — `_format_summary` 가 텔레그램 요약에 쓰는 것과 같은 값)뿐이라
    여기선 그것을 `candidates`, `midterm_watch` 길이를 `midterm` 으로 쓴다
    (코드 확인 근거: report_cli.py 의 `_auto_watch_count`/`_format_summary`,
    `payload["midterm_watch"] = midterm_view`). `agent_interpret` 상태값은
    `_build_agent_interpret` docstring 이 정의한 6종
    (`ok`/`ok_midterm_fallback`/`failed`/`failed_midterm_fallback`/
    `skipped_no_candidates`/`skipped_no_key`) 중 `failed*` 만 이상이다 —
    `skipped_no_key` 는 무료 API 키 미설정 같은 정상 상태라 alert 로 내면
    거짓 경보가 된다.

    `today`/`trailing` 원소는 `{"candidates": int, "midterm": int,
    "agent_interpret": str|None, "missing": int}` — 그날 engine.json 을 못
    읽으면 그 날은 애초에 `trailing` 에 넣지 않는다(호출부 책임,
    `flow_anomaly_findings` 와 같은 관례). trailing 표본이 3일 미만이면
    **UNKNOWN 도 내지 않고 빈 목록**이다(신규 설치 직후 소음 방지 —
    `flow_anomaly_findings`/`telegram_silence_findings` 와 동일 규율. 이
    창에서는 agent_interpret 실패 감지도 함께 미뤄지는데, LLM 호출 자체의
    실패율은 `llm_health_findings` 가 표본 요구 없이 독립적으로 보고 있어
    첫날부터의 안전망은 이미 있다).
    """
    if len(trailing) < 3:
        return []
    if today is None:
        return [Finding("report_quality", UNKNOWN,
                        f"{market}: 오늘 engine.json 요약치를 읽지 못했다")]
    out: list[Finding] = []
    labels = {"candidates": "전체 후보(auto_watch)", "midterm": "중기 관심 종목"}
    for key, label in labels.items():
        counts = [t[key] for t in trailing if isinstance(t.get(key), int)]
        if len(counts) < 3:
            continue
        median = statistics.median(counts)
        today_n = today.get(key)
        if isinstance(today_n, int) and median >= 1 and today_n == 0:
            out.append(Finding("report_quality", ALERT,
                               f"{market}: {label} 수가 0 — 직전 {len(counts)}개 개장일 "
                               f"중앙값 {median:g}건에서 급감"))
    status = str(today.get("agent_interpret") or "")
    if status.startswith("failed"):
        out.append(Finding("report_quality", ALERT,
                           f"{market}: agent_interpret 상태가 {status!r} — "
                           "AI 심층 해석이 실패했다"))
    return out


# ── 국면(regime) 강등 지속 ───────────────────────────────────────────────

def regime_findings(snapshot: dict | None, now: datetime,
                    persist: timedelta = timedelta(hours=2)) -> list[Finding]:
    """`data/state/regime.json`(quant.trade.regime.provider._save_cache 산출물)을
    읽어 시장별 국면이 **강등(degraded)된 채 오래 지속**되면 ALERT.

    유래(2026-08-18~19): US 국면이 지표 5개 중 2개만 유효한 상태로 하루 종일
    aggressive(1.5x)를 유지했다 — 조회 실패 자체는 provider가 `degraded` 플래그로
    이미 기록하고 있었는데, 그 사실을 사람이 보는 경로가 없었다. `degraded=True`
    자체는 이상이 아니다(장 시작 직후 일시적 API 지연 등으로도 하루 나올 수 있다)
    — **몇 시간째 지속**되는 것만 이상이다(`persist`, 기본 2시간. 세션당 국면은
    하루 1회만 갱신되므로 짧게 잡으면 정상적인 refresh 지연에도 매번 울린다).

    `snapshot`이 없으면(파일 없음/파싱 실패) UNKNOWN — "강등 없음"이 아니다.
    `snapshot["markets"]`가 없는 구버전 캐시는 최상위 필드(=US)만 본다.
    """
    if not snapshot:
        return [Finding("regime", UNKNOWN, "국면 스냅샷(regime.json)을 읽지 못했다")]
    markets: dict = snapshot.get("markets") or {"US": snapshot}
    out: list[Finding] = []
    for market in sorted(markets):
        state = markets[market] or {}
        if not state.get("degraded"):
            continue
        age = _age(state.get("computed_at"), now)
        if age is None:
            out.append(Finding("regime", UNKNOWN,
                               f"{market}: 국면 계산 시각을 읽지 못했다 — 강등 지속 여부를 모른다"))
            continue
        if age >= persist:
            reasons = "; ".join(state.get("reasons") or [])[:200]
            out.append(Finding("regime", ALERT,
                               f"{market}: 유효 지표 부족으로 중립 강등된 상태가 "
                               f"{age.total_seconds() / 3600:.1f}시간 지속 "
                               f"(사유: {reasons or '없음'})"))
    return out


# ── LLM 호출 계측 ─────────────────────────────────────────────────────────

def llm_health_findings(stats_by_lane: dict[str, dict | None]) -> list[Finding]:
    """`opstate.llm_stats()` 결과 — narrate/chat_with_tools(OpenRouter 무료
    레인)의 최근 24시간 실패율.

    유래: 무료 레인 요청 수·실패율·지연을 아무도 기록하지 않아 한도에 걸리기
    시작해도 몰랐다. `narrate.py` 가 이제 매 호출을 계측한다(`record_llm_call`).

    호출이 0건이면 빈 목록이다 — narrate 는 상시 도는 경로가 아니라(경보가
    있을 때만 불린다) 조용한 날마다 매시간 경보가 오면 감시가 꺼진다.
    실패율 임계를 50%로 높게 잡은 이유는 무료 레인 특유의 간헐 502
    (`narrate.py` 상단 실측 주석 — 당일 실패율 ~50%도 재시도로 흡수되는
    정상 변동)를 정상으로 흡수하기 위해서다 — 그 이상만 "레인 자체가
    맛이 갔다"로 본다. `stats` 를 못 읽으면(Redis 죽음) UNKNOWN.
    """
    out: list[Finding] = []
    for lane in sorted(stats_by_lane):
        stats = stats_by_lane[lane]
        if stats is None:
            out.append(Finding("llm", UNKNOWN, f"{lane}: 호출 계측을 읽지 못했다"))
            continue
        total = stats.get("total", 0)
        failed = stats.get("failed", 0)
        if not total:
            continue
        rate = failed / total
        if rate > 0.5:
            out.append(Finding("llm", ALERT,
                               f"{lane}: 최근 24시간 호출 {total}건 중 {failed}건 실패"
                               f"({rate:.0%}) — 무료 레인 임계(50%) 초과"))
    return out
