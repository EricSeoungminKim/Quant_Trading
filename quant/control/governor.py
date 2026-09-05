"""파라미터 자동 반영 거버너 — "한 번에 크게 망하지 않게" 하는 방어층.

사용자 결정(2026-08-13): 하네스가 찾은 파라미터를 **자동 반영한다**. 단
"자동 반영 했을때 한번에 크게 망할 수 있으니깐 방어층을 두껍게".

## 층 0 — 폭발 반경 (다른 어떤 층보다 중요하다)

**자동 반영은 리스크를 줄이는 방향만. 나머지는 전부 제안.**(소유자 승인,
2026-08-30 — 기존 자본 강등 장치 `quant/control/allocator.py`의 "줄이기만
자동, 늘리기는 사람"과 같은 철학.) `ALLOWED` 의 각 항목은 방향까지 못 박는다:

    이름                                          방향          의미
    ────────────────────────────────────────    ──────────    ──────────────
    strategies.*.params.min_stop_bp              raise_only    손절을 넓히는(=문턱을
                                                                엄격히 하는) 쪽만
    engine.cold_fetch_budget_per_cycle            lower_only    사이클 부하를 줄이는
                                                                쪽만

반대 방향 제안(손절을 좁히거나 예산을 늘리는 쪽)은 6층을 다 통과해도 거부된다
— evaluate()의 "0-direction" 층. `FORBIDDEN`(사이징·손절 자체를 끄는 값 등
회복 불가능한 리스크 파라미터)은 방향과 무관하게 항상 거부다: 사이징을 잘못
키우면 **계좌가 날아가고** 되돌릴 자산이 남지 않는다. 실수의 비용이
비대칭이므로 권한도 비대칭이어야 한다.

`ALLOWED` 밖의 이름은 어떤 근거를 들고 와도 거부한다 — 근거의 품질을 심사하는
층이 아니라, **애초에 그 문을 열지 않는** 층이다.

2026-08-30 재정의 배경: 이전 ALLOWED 7개(min_articles 등, analyze 평면 선정
문턱)는 `config/settings.yaml` 어디에도 없는 이름이었다(2026-08-28 실측,
`quant/apps/cli.py`의 옛 `GOVERNOR_SETTINGS_PATH` 주석 참고) — 제안은 나와도
반영될 곳이 없어 이 거버너 전체가 죽은 코드였다. 이번 ALLOWED 는 이름 자체가
`config/settings.yaml` 의 점(.) 표기 경로다 — 매핑 테이블이 따로 필요 없고,
스키마 이탈은 `tests/test_governor_wiring.py::test_allowed_paths_all_exist_in_real_settings_yaml`
가 실제 파일로 매 실행마다 고정한다.

2026-09-02 감사 실측 대응(작업1~2) — `ALLOWED`(숫자 봉투)만으로는 실제 제안
소스 둘을 못 받는다는 게 드러났다: (a) param_propose.sh 의 LLM 제안 중
문자열 enum 파라미터(예: trend_gate_mode), (b) `quant.control.experiments`의
사망 판정 지속 → 전략 on/off. 그래서 같은 층 0 철학(강화하는 방향만 자동)을
쓰는 두 표를 추가했다 — `ALLOWED_ORDINAL`(순서형 문자열, 봉투 대신 "완화→강화
순으로 나열한 상태 목록")과 `ALLOWED_KILL_SWITCH`(부울, True→False 전환만).
`evaluate()`는 이름이 어느 표에 있는지로 분기한다. 이 시점까지 param_propose.sh
가 실제로 낸 제안은 2026-W35 한 주(scalp_1m 의 trend_gate_mode/volume_surge_mult)
뿐이었다 — 그 둘만 실측 근거로 등재했고, 그 밖의 이름은 여전히 기본값(제안만,
반영은 사람)이다.

## 층 1~6

1. **봉투(envelope)** — 각 파라미터의 하드 min/max. 밖이면 거부.
2. **보폭(step)** — 1회 변경 폭 상한. "3 → 30" 같은 도약을 막는다.
3. **냉각(cooldown)** — 같은 파라미터를 N일 안에 다시 못 바꾼다. 매일 흔들면
   그 파라미터의 성과를 영영 측정할 수 없다.
4. **증거(evidence)** — 최소 표본 수 + 개선 폭. 표본이 적으면 우연이다.
5. **동시 변경 상한** — 한 번에 하나만. 둘을 같이 바꾸면 어느 쪽이 효과였는지
   모른다(그리고 둘 다 나빴을 때 원인을 못 가린다).
6. **자동 롤백** — 반영 후 성과가 기준 이하로 나빠지면 되돌린다.

모든 결정은 `decisions.jsonl`에 append-only로 남는다 — 거부도 남긴다. 무엇이
거부됐는지가 방어층이 실제로 일하는지 보여주는 유일한 증거다. 실반영(파일
쓰기)은 여기서 끝나지 않는다 — `quant/apps/cli.py`의 `cmd_governor_apply`가
`--live` 플래그로 한 겹 더 게이트한다(이 모듈은 판단만, 배선은 거기).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

# --- 층 0: 폭발 반경 -------------------------------------------------------
# 이름(=config/settings.yaml 점 표기 경로) → (방향, 하한, 상한, 1회 최대 보폭, 냉각일)
#
# 방향: "raise_only" = 올리는 제안만 자동 반영 대상(문턱 강화 = 리스크 축소),
#       "lower_only" = 내리는 제안만 자동 반영 대상(부하 축소 = 리스크 축소).
# 반대 방향 제안은 6층을 다 통과해도 evaluate()의 "0-direction" 층에서 거부되고
# decisions.jsonl 에 거부 사유로 남는다(=사람에게는 "제안"으로 보인다).
#
# 각 하한은 아래 값이 config/settings.yaml 의 **현재 실제 기본값**과 모순되지
# 않게 잡았다(2026-08-30 대조 — min_stop_bp 3종 전부 40, cold_fetch_budget 8).
ALLOWED: dict[str, tuple[str, float, float, float, int]] = {
    "strategies.vol_breakout.params.min_stop_bp":      ("raise_only", 40, 120, 20, 3),
    "strategies.intraday_momentum.params.min_stop_bp": ("raise_only", 40, 120, 20, 3),
    "strategies.gap_fade.params.min_stop_bp":           ("raise_only", 40, 120, 20, 3),
    "engine.cold_fetch_budget_per_cycle":               ("lower_only", 4, 8, 2, 3),
    # watch_conditions 블록은 다른 워커가 작업 도중에 실제로 추가했다(2026-08-30,
    # config/settings.yaml, 기본값 cooldown_minutes=60 — 아래 하한 30과 모순
    # 없음 확인). 재알림 간격을 늘리는 쪽 = 알림 스팸을 줄이는 쪽 = 리스크 축소로
    # 분류해 raise_only 로 등재한다.
    "watch_conditions.cooldown_minutes": ("raise_only", 30, 240, 30, 1),
    # 소유자 지시에 있었지만 지금 config/settings.yaml 에 그 경로가 없어 뺐다 —
    # 있지도 않은 경로를 자동 반영 화이트리스트에 올리면 2026-08-28 결함(위
    # 배경 문단)이 그대로 재발한다. 아래는 실제로 확인한 부재 사유다:
    #
    #   strategies.scalp_1m.params.min_stop_bp
    #     scalp_1m 은 min_stop_bp 가 아니라 stop_hard_cap_pct(%, 하드캡) +
    #     stop_mode: structure(스윙 저점 기반 손절)를 쓴다 — 단위(bp vs %)도
    #     의미(고정 최소폭 vs 구조적 상한)도 달라 같은 이름에 억지로 태우지
    #     않는다.
    #
    #   strategies.pullback_impulse.params.min_stop_bp
    #     pullback_impulse 의 params 에는 손절 관련 키가 아예 없다(min_impulse_bp/
    #     pullback_min_pct/pullback_max_pct/ema_period/atr_buffer_mult/
    #     target_mult/timeout_minutes/target_weight 뿐).
    #
    # 2026-09-02 (감사 실측 대응) — param_propose.sh(주간 LLM 제안, 06:40 크론)가
    # 실제로 낸 제안은 저장소 역사상 딱 한 주(2026-W35)뿐이었고 둘 다 scalp_1m
    # 이었다(data/ledger/param_proposals.jsonl EC2 실측, cli.py의 name 결선 버그를
    # 고치기 전엔 이 둘조차 governor 형태로 못 넘어왔다 — 아래 참고). 그 둘 중
    # 숫자형인 volume_surge_mult 만 여기 등재한다:
    "strategies.scalp_1m.params.volume_surge_mult": ("raise_only", 3.0, 6.0, 1.0, 7),
    # ↑ raise_only: 거래량 서지 배수를 올리는 것 = 진입 문턱을 엄격히 하는 것
    # (같은 논리를 min_stop_bp 에도 쓴다 — 문턱 강화는 표본을 줄이는 대신
    # 리스크를 줄인다). 하한 3.0 = 현재 기본값(모순 없음). 상한 6.0 = 현재의
    # 2배 — 실측 분포 없이 잡은 보수적 캡이다(그 이상은 진입 자체가 말라
    # 전략을 사실상 죽일 수 있어, 그 판단은 사람이 하도록 봉투 밖으로 둔다).
    # 보폭 1.0 = 실제 관측된 유일한 제안(3.0→4.0)과 같은 크기. 냉각 7일 =
    # 이 제안 소스의 주기(주간)와 맞춰 같은 주간 사이클 안에서 두 번 흔들리지
    # 않게 한다.
}

# 층 0 확장 — 순서형(문자열 enum) 파라미터. 숫자 envelope 로 표현할 수 없어
# ALLOWED 와 분리한다: 값은 (완화→강화 순으로 나열한 허용 상태들, 냉각일).
# 방향 규칙은 "한 단계 이상 강화하는 쪽만" — 완화(되돌리기)는 항상 사람 몫이다
# (숫자형의 raise_only/lower_only 와 같은 철학). 목록 밖의 값(예: LLM 환각)은
# 그 값이 current 든 proposed 든 즉시 거부된다.
ALLOWED_ORDINAL: dict[str, tuple[tuple[str, ...], int]] = {
    # 2026-09-02 — param_propose.sh 2026-W35 제안: trend_gate_mode shadow→active.
    # "active" 는 scalp_1m.py 가 실제로 아는 상태가 아니다(코드 관례는
    # off/shadow/block 셋뿐 — scalp_1m.py:358 부근 주석 "동일 관례(off/shadow/
    # block)"). LLM 환각이었을 가능성이 높고, 이 표에 없는 상태는 아래처럼
    # 자동 거부된다 — 그 자체가 방어다(고쳐서 통과시키지 않는다).
    # shadow(계산만, 진입 안 막음)→block(진입 차단)이 강화 방향: off/shadow 는
    # 필터가 사실상 꺼진 상태고 block 만 실제로 진입을 줄인다.
    "strategies.scalp_1m.params.trend_gate_mode": (("off", "shadow", "block"), 7),
}

# 층 0 확장 — 사망 판정 전략 자동 비활성(2026-09-02, 작업 2). 부울이라 봉투/
# 보폭 개념이 없다: True(enabled)→False(disabled) 전환만 자동 대상, 되살리는
# 것은 항상 사람이 settings.yaml 을 고친다. 값은 냉각일.
#
# 대상 = config/settings.yaml 상 현재 enabled: true 인 전략 중 보호 목록
# (config/settings.yaml governor.protected_strategies, 2026-08-30 소유자 지시로
# scalp_1m 은 항상 제외) 밖 전부. 이미 enabled: false 인 전략은 끌 것이 없어
# 등재하지 않는다(등재해도 무해하지만 실측 근거 없는 항목을 늘리지 않는다).
ALLOWED_KILL_SWITCH: dict[str, int] = {
    "strategies.news_momentum.enabled": 5,
    "strategies.frgn_accumulate.enabled": 5,
    "strategies.close_bet.enabled": 5,
    "strategies.overnight_drift.enabled": 5,
    "strategies.pullback_impulse.enabled": 5,
    "strategies.mr_vwap_quiet.enabled": 5,
    "strategies.vol_breakout.enabled": 5,
    "strategies.intraday_momentum.enabled": 5,
    "strategies.gap_fade.enabled": 5,
    "strategies.rsi2_dip.enabled": 5,
    "strategies.llm_trader.enabled": 5,
    # A/B 갈래(2026-09-03, `<id>_cat`). 촉매 갈래도 죽으면 꺼야 한다 — 다만
    # scalp_1m 계열은 아래처럼 통째로 뺀다(보호 상속).
    "strategies.pullback_impulse_cat.enabled": 5,
    "strategies.vol_breakout_cat.enabled": 5,
    "strategies.news_scalp.enabled": 5,      # 2026-09-03 재활성 — 이제 끌 대상이 있다
    # 문헌 기반 일중 3종(2026-09-03). 지금은 enabled: false 라 끌 것이 없지만,
    # 번인으로 켜지는 순간 사망 판정 자동 비활성 경로가 함께 살아 있어야 한다 —
    # 켤 때 이 표를 같이 고치는 것을 잊는 쪽이 훨씬 위험하다.
    "strategies.orb_rvol.enabled": 5,
    "strategies.eod_reversal.enabled": 5,
    "strategies.open_reversal.enabled": 5,
    # 15분봉 추세일 지속(2026-09-04) — 같은 이유로 미리 등재한다.
    "strategies.trend_day.enabled": 5,
    # 레버리지 ETF 페어 전환(2026-09-05) — 같은 이유로 미리 등재한다. 설정 id가
    # 둘(letf_pair_qqq/letf_pair_sox, 같은 클래스의 다른 signal_symbol)이라
    # `_cat`처럼 접미사로 벗겨지지 않는다 — 둘 다 직접 등재한다.
    "strategies.letf_pair_qqq.enabled": 5,
    "strategies.letf_pair_sox.enabled": 5,
    # strategies.scalp_1m.enabled — 의도적으로 뺐다(보호 목록, 위 설명).
    # strategies.scalp_1m_cat.enabled — 같은 이유로 뺐다(2026-09-03): `_cat` 은
    # 같은 클래스의 다른 유니버스 갈래라 보호를 **상속**한다. 한쪽만 자동으로
    # 꺼지면 남은 갈래가 계속 돌아 A/B 비교 자체가 무의미해진다
    # (`quant.apps.cli.cmd_governor_apply` 가 base_strategy_id 로 판정한다).
}

# 자동 반영이 절대 닿으면 안 되는 이름. ALLOWED 화이트리스트만으로도 막히지만,
# **이름을 명시해 두면 실수로 ALLOWED 에 추가하는 것을 테스트가 잡는다.**
FORBIDDEN = frozenset({
    "capital_fraction", "stop_loss_pct", "max_entries_per_session",
    "risk_budget_pct", "max_leverage", "threshold", "partial_take_pct",
    "full_take_pct", "entry_window_seconds", "burn_in_max_capital_fraction",
})

MIN_SAMPLES = 30          # 이 저장소가 이미 쓰는 최소 표본선(ledger.MIN_TRIPS_FOR_JUDGEMENT)
MIN_IMPROVEMENT = 0.05    # 기대 개선이 5% 미만이면 흔들 가치가 없다
MAX_CHANGES_PER_RUN = 1   # 층 5
ROLLBACK_DEGRADE = -0.10  # 반영 후 10% 이상 악화되면 되돌린다


@dataclass
class Proposal:
    """하네스(LLM)가 내는 제안. **LLM 은 여기까지만 한다** — 채점과 반영은 코드다."""
    name: str
    current: float
    proposed: float
    samples: int
    expected_improvement: float   # 0.08 = 8% 개선 기대
    rationale: str = ""


@dataclass
class Decision:
    proposal: Proposal
    accepted: bool
    reason: str
    applied_value: float | None = None
    layer: str = ""               # 어느 층에서 걸렸나
    # (사망 코드 정리, 2026-08-30) 예전엔 여기에 `blocked_layers: list[str]`
    # 필드가 있었지만 어디서도 채워지지 않았다 — evaluate()가 첫 실패 층에서
    # 즉시 return 하는 구조라 결정 하나는 항상 최대 한 층에서만 막힌다. 그
    # 한 층은 위 `layer`(단수)로 이미 완전히 표현되므로, 여러 층을 동시에
    # 담는 리스트가 채워질 경로 자체가 없었다.


def _history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def last_change(name: str, decisions: list[dict]) -> date | None:
    """이 파라미터가 마지막으로 **실제 반영된** 날. 거부는 세지 않는다."""
    best: date | None = None
    for row in decisions:
        if row.get("name") != name or not row.get("accepted"):
            continue
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if best is None or d > best:
            best = d
    return best


def _evaluate_ordinal(p: Proposal, today: date, decisions: list[dict],
                       changes_this_run: int) -> Decision:
    """ALLOWED_ORDINAL 전용 층 0~5 (숫자형과 같은 순서, 봉투/보폭만 순서형으로
    바뀐다). 층 6(자동 롤백)은 숫자·문자 구분 없이 공용 rollback_candidates 가
    담당하므로 여기 없다."""
    order, cooldown_days = ALLOWED_ORDINAL[p.name]

    if p.current not in order or p.proposed not in order:
        unknown = p.proposed if p.proposed not in order else p.current
        return Decision(
            p, False, f"알 수 없는 상태 {unknown!r} (허용: {order})", layer="1-envelope")

    cur_idx, prop_idx = order.index(p.current), order.index(p.proposed)
    if prop_idx < cur_idx:
        return Decision(
            p, False,
            f"완화 방향 제안 거부(강화하는 것만 자동 반영 대상): {p.current} → {p.proposed}",
            layer="0-direction",
        )

    if p.samples < MIN_SAMPLES:
        return Decision(p, False, f"표본 부족 {p.samples}/{MIN_SAMPLES}", layer="4-evidence")
    if p.expected_improvement < MIN_IMPROVEMENT:
        return Decision(
            p, False,
            f"기대 개선 {p.expected_improvement:.1%} < {MIN_IMPROVEMENT:.0%}",
            layer="4-evidence",
        )

    if changes_this_run >= MAX_CHANGES_PER_RUN:
        return Decision(p, False, f"이번 회차 변경 상한 {MAX_CHANGES_PER_RUN}건 도달", layer="5-one-at-a-time")

    prev = last_change(p.name, decisions)
    if prev is not None and (today - prev).days < cooldown_days:
        return Decision(
            p, False,
            f"냉각 중 — 마지막 변경 {prev} ({cooldown_days}일 필요)",
            layer="3-cooldown",
        )

    if prop_idx - cur_idx > 1:
        clamped = order[cur_idx + 1]
        return Decision(
            p, True,
            f"보폭 제한으로 {p.proposed} → {clamped} 로 축소 반영",
            applied_value=clamped, layer="2-step-limit",
        )

    return Decision(p, True, "모든 층 통과", applied_value=p.proposed, layer="")


def _evaluate_kill_switch(p: Proposal, today: date, decisions: list[dict],
                          changes_this_run: int) -> Decision:
    """ALLOWED_KILL_SWITCH 전용. 부울이라 봉투/보폭이 없다 — True→False 전환
    하나만 있고 그 전환 자체가 이미 "가장 강한 강화"라 클램프할 중간값이 없다."""
    cooldown_days = ALLOWED_KILL_SWITCH[p.name]

    if p.current is not True or p.proposed is not False:
        return Decision(
            p, False,
            f"킬스위치는 활성(True) → 비활성(False) 전환만 자동 반영 대상: "
            f"{p.current!r} → {p.proposed!r}",
            layer="0-direction",
        )

    if p.samples < MIN_SAMPLES:
        return Decision(p, False, f"표본 부족 {p.samples}/{MIN_SAMPLES}", layer="4-evidence")
    if p.expected_improvement < MIN_IMPROVEMENT:
        return Decision(
            p, False,
            f"기대 개선 {p.expected_improvement:.1%} < {MIN_IMPROVEMENT:.0%}",
            layer="4-evidence",
        )

    if changes_this_run >= MAX_CHANGES_PER_RUN:
        return Decision(p, False, f"이번 회차 변경 상한 {MAX_CHANGES_PER_RUN}건 도달", layer="5-one-at-a-time")

    prev = last_change(p.name, decisions)
    if prev is not None and (today - prev).days < cooldown_days:
        return Decision(
            p, False,
            f"냉각 중 — 마지막 변경 {prev} ({cooldown_days}일 필요)",
            layer="3-cooldown",
        )

    return Decision(p, True, "모든 층 통과 — 사망 판정 전략 자동 비활성", applied_value=False, layer="")


def evaluate(proposal: Proposal, today: date, decisions: list[dict],
             changes_this_run: int = 0) -> Decision:
    """제안 하나를 모든 층에 통과시킨다. 하나라도 걸리면 거부."""
    p = proposal

    # 층 0 — 폭발 반경
    if p.name in FORBIDDEN:
        return Decision(p, False, f"리스크 파라미터는 자동 반영 금지: {p.name}", layer="0-blast-radius")
    if p.name in ALLOWED_ORDINAL:
        return _evaluate_ordinal(p, today, decisions, changes_this_run)
    if p.name in ALLOWED_KILL_SWITCH:
        return _evaluate_kill_switch(p, today, decisions, changes_this_run)
    if p.name not in ALLOWED:
        return Decision(p, False, f"자동 반영 허용 목록에 없음: {p.name}", layer="0-blast-radius")

    direction, lo, hi, max_step, cooldown_days = ALLOWED[p.name]

    # 층 0b — 방향 (2026-08-30: "자동 반영은 리스크를 줄이는 방향만, 나머지는
    # 전부 제안" — 소유자 승인). 6층을 다 통과해도 방향이 틀리면 여기서 막힌다.
    if direction == "raise_only" and p.proposed < p.current:
        return Decision(
            p, False,
            f"내리는 방향 제안 거부(올리는 것만 자동 반영 대상): {p.current} → {p.proposed}",
            layer="0-direction",
        )
    if direction == "lower_only" and p.proposed > p.current:
        return Decision(
            p, False,
            f"올리는 방향 제안 거부(내리는 것만 자동 반영 대상): {p.current} → {p.proposed}",
            layer="0-direction",
        )

    # 층 4 — 증거
    if p.samples < MIN_SAMPLES:
        return Decision(p, False, f"표본 부족 {p.samples}/{MIN_SAMPLES}", layer="4-evidence")
    if p.expected_improvement < MIN_IMPROVEMENT:
        return Decision(
            p, False,
            f"기대 개선 {p.expected_improvement:.1%} < {MIN_IMPROVEMENT:.0%}",
            layer="4-evidence",
        )

    # 층 5 — 동시 변경 상한
    if changes_this_run >= MAX_CHANGES_PER_RUN:
        return Decision(p, False, f"이번 회차 변경 상한 {MAX_CHANGES_PER_RUN}건 도달", layer="5-one-at-a-time")

    # 층 3 — 냉각
    prev = last_change(p.name, decisions)
    if prev is not None and (today - prev).days < cooldown_days:
        return Decision(
            p, False,
            f"냉각 중 — 마지막 변경 {prev} ({cooldown_days}일 필요)",
            layer="3-cooldown",
        )

    # 층 1 — 봉투
    if not (lo <= p.proposed <= hi):
        return Decision(p, False, f"봉투 밖: {p.proposed} ∉ [{lo}, {hi}]", layer="1-envelope")

    # 층 2 — 보폭 (봉투 안이라도 한 번에 크게 못 움직인다)
    step = abs(p.proposed - p.current)
    if step > max_step:
        clamped = p.current + max_step * (1 if p.proposed > p.current else -1)
        clamped = min(hi, max(lo, clamped))
        return Decision(
            p, True,
            f"보폭 제한으로 {p.proposed} → {clamped} 로 축소 반영",
            applied_value=clamped, layer="2-step-limit",
        )

    return Decision(p, True, "모든 층 통과", applied_value=p.proposed, layer="")


def decide(proposals: list[Proposal], today: date, ledger_path: Path) -> list[Decision]:
    """제안 묶음을 심사한다. 통과분은 층 5 때문에 최대 MAX_CHANGES_PER_RUN 건.

    **기대 개선이 큰 순으로 심사한다** — 상한이 1건이므로 순서가 곧 우선순위다.
    """
    decisions = _history(ledger_path)
    out: list[Decision] = []
    accepted = 0
    for p in sorted(proposals, key=lambda x: -x.expected_improvement):
        d = evaluate(p, today, decisions, changes_this_run=accepted)
        if d.accepted:
            accepted += 1
        out.append(d)
    return out


def record(decisions: list[Decision], today: date, path: Path) -> int:
    """결정을 append-only 로 남긴다. **거부도 남긴다** — 방어층이 실제로 일하는지는
    거부 기록으로만 확인할 수 있다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for d in decisions:
            f.write(json.dumps({
                "date": today.isoformat(),
                "name": d.proposal.name,
                "current": d.proposal.current,
                "proposed": d.proposal.proposed,
                "applied": d.applied_value,
                "accepted": d.accepted,
                "reason": d.reason,
                "layer": d.layer,
                "samples": d.proposal.samples,
                "expected_improvement": d.proposal.expected_improvement,
                "rationale": d.proposal.rationale[:500],
            }, ensure_ascii=False) + "\n")
    return len(decisions)


def rollback_candidates(decisions: list[dict], realized: dict[str, float],
                        threshold: float = ROLLBACK_DEGRADE) -> list[dict]:
    """반영 후 성과가 나빠진 변경들 — 되돌릴 대상.

    `realized`: {파라미터명: 반영 전후 성과 변화율}. -0.12 면 12% 악화.
    **개선을 기대하고 바꿨는데 나빠졌으면 되돌린다** — 하네스의 가설이 틀릴 수
    있다는 전제가 이 층의 존재 이유다.
    """
    out = []
    for row in decisions:
        if not row.get("accepted"):
            continue
        delta = realized.get(row.get("name"))
        if delta is not None and delta <= threshold:
            out.append({**row, "realized_change": delta})
    return out


def summary(decisions: list[Decision]) -> str:
    """텔레그램용 한 문단. 무엇이 왜 거부됐는지가 본문이다."""
    ok = [d for d in decisions if d.accepted]
    no = [d for d in decisions if not d.accepted]
    lines = [f"🎛 파라미터 자동 반영 — 제안 {len(decisions)}건 · 반영 {len(ok)}건"]
    for d in ok:
        lines.append(f"  ✅ {d.proposal.name}: {d.proposal.current} → {d.applied_value}"
                     f" (표본 {d.proposal.samples}건)")
        if d.layer:
            lines.append(f"     ↳ {d.reason}")
    for d in no:
        lines.append(f"  ⛔ {d.proposal.name} [{d.layer}] {d.reason}")
    return "\n".join(lines)
