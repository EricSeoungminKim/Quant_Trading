"""자동 판정 루프 — "바꿨다 → 쌓인다 → **자동으로 판정이 온다**" (2026-08-24).

## 왜

`forensics`(2026-08-22)까지 만들고 나서도 루프의 마지막 칸이 비어 있었다:
2026-08-22에 익절+100bp/손절-100bp를 넣었는데, **그게 효과가 있었는지 알려줄
장치가 없었다.** 원장은 쌓이지만 누군가 물어봐야 판정이 나왔다. 소유자가 다른
프로젝트로 자리를 비우면 그 "누군가"가 없다.

여기서 채우는 것: 설정이 바뀐 것을 **스스로 알아채고**, 표본이 찰 때까지 조용히
기다렸다가, 차면 **먼저 알려준다**.

## 세 부분

1. **변경 감지** — `config/settings.yaml`의 전략별 파라미터 지문(해시)을 매일
   찍어 `data/ledger/param_changes.jsonl`에 남긴다. 사람이 기록하지 않는다 —
   기록을 사람 규율에 맡기면 자리를 비운 동안 반드시 끊긴다.
2. **이중차분(DiD) 비교** — 순진한 전후 비교는 **장세에 오염된다**. 8월에 바꾸고
   9월에 좋아졌다면 그게 내 변경 때문인지 장이 좋아진 것인지 알 수 없다.
   그래서 같은 기간 **파라미터가 바뀌지 않은 전략들**을 대조군으로 쓴다:

       DiD = (변경군 after - 변경군 before) - (대조군 after - 대조군 before)

   대조군도 같은 장을 겪었으므로, 두 변화량의 차이가 곧 "장세를 뺀 순효과"다.
   추가 데이터가 필요 없다 — 이미 있는 원장만으로 성립한다.
3. **순열검정(permutation test)** — 표본이 수십 건이고 bps 분산이 크다. 정규성을
   가정하는 t검정 대신, 라벨을 섞어 관측된 차이가 우연히 나올 확률을 직접 센다.
   가정이 없고 표본이 작아도 정직하다. 시드 고정(42, 저장소 관례)이라 같은
   원장이면 같은 답이 나온다.

## 정직성 규칙 (이 파일이 지켜야 하는 것)

- **표본이 안 차면 판정하지 않는다.** "아직 모른다"가 기본값이고, 그 상태로
  조용히 기다린다(알림도 안 보낸다 — 매일 "모릅니다"를 보내면 사람이 안 읽는다).
- **인과를 주장하지 않는다.** DiD는 교란을 줄이지 그것을 없애지 않는다. 대조군
  전략들이 변경군과 같은 종목/시간대를 거래하지 않으면 그 전제도 깨진다 —
  출력에 대조군이 누구였는지 항상 밝힌다.
- **나쁜 결과를 숨기지 않는다.** 악화 판정도 개선과 같은 경로로 나간다.

`quant/control/` 소속 — 원장을 읽어 다음 세션을 낫게 하는 층. 거래 평면을
임포트하지 않고, 설정 파일도 **쓰지 않는다**(판정만 하고 반영은 사람이 한다).
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics as st
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 판정에 필요한 최소 종결 건수(변경군 기준, 전/후 각각). 30은 `ledger.py`의
# 스코어보드 표본 문턱과 같은 값 — 같은 원장을 같은 기준으로 읽는다.
MIN_SAMPLE = 30
# 순열검정 반복. 2,000이면 p 해상도 0.0005 — 이 표본 크기에 충분하고 1초 안에 끝난다.
PERMUTATIONS = 2000
_SEED = 42

DEFAULT_CHANGES_PATH = "data/ledger/param_changes.jsonl"


# ── 1. 변경 감지 ──────────────────────────────────────────────────────────

def params_fingerprint(strategy_cfg: dict) -> str:
    """전략 설정 → 안정적인 지문. `params` + `enabled`만 본다.

    `symbols`는 **일부러 뺀다** — 관심종목은 매일 바뀌므로 포함하면 매일이
    "변경"이 되어 판정이 영원히 리셋된다. 우리가 판정하려는 건 파라미터
    변경의 효과다."""
    payload = {
        "params": strategy_cfg.get("params") or {},
        "enabled": strategy_cfg.get("enabled", True),
        "class": strategy_cfg.get("class"),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def load_changes(path: Path | str = DEFAULT_CHANGES_PATH) -> list[dict]:
    """변경 원장. 없거나 깨진 줄은 건너뛴다 — 복원 실패가 감시를 막으면 안 된다."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def record_fingerprints(
    strategies_cfg: dict, on: date, path: Path | str = DEFAULT_CHANGES_PATH,
) -> list[dict]:
    """오늘 지문을 찍어, **바뀐 것만** 원장에 덧붙인다. 새로 추가된 항목 반환.

    첫 실행은 전 전략의 기준선을 남기고 판정은 하지 않는다(비교 대상이 없다).
    같은 날 두 번 불러도 지문이 같으면 아무것도 안 쓴다(멱등)."""
    existing = load_changes(path)
    last: dict[str, str] = {}
    for r in existing:
        if r.get("strategy") and r.get("fingerprint"):
            last[r["strategy"]] = r["fingerprint"]

    added = []
    for sid, cfg in sorted((strategies_cfg or {}).items()):
        fp = params_fingerprint(cfg)
        if last.get(sid) == fp:
            continue
        added.append({
            "date": on.isoformat(),
            "strategy": sid,
            "fingerprint": fp,
            "baseline": sid not in last,
            "params": cfg.get("params") or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    if added:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for row in added:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return added


def pending_experiments(changes: list[dict], today: date) -> list[dict]:
    """판정 대상 = 기준선이 아닌 변경들. 각 변경의 관측 창은 다음 변경까지다
    (그 뒤는 다른 실험이므로 섞으면 둘 다 못 읽는다).

    반환: `[{strategy, change_date, until, prev_date}, ...]`"""
    by_strategy: dict[str, list[dict]] = {}
    for r in changes:
        if r.get("strategy") and r.get("date"):
            by_strategy.setdefault(r["strategy"], []).append(r)

    out = []
    for sid, rows in by_strategy.items():
        rows = sorted(rows, key=lambda r: r["date"])
        for i, r in enumerate(rows):
            if r.get("baseline"):
                continue
            nxt = rows[i + 1]["date"] if i + 1 < len(rows) else None
            out.append({
                "strategy": sid,
                "change_date": r["date"],
                "prev_date": rows[i - 1]["date"] if i > 0 else None,
                "until": nxt or today.isoformat(),
                "superseded": nxt is not None,
            })
    return sorted(out, key=lambda x: (x["change_date"], x["strategy"]))


# ── 2. 전후 분할 + 이중차분 ───────────────────────────────────────────────

def _trip_date(trip: dict) -> str | None:
    ts = trip.get("exit_ts") or trip.get("entry_ts")
    return str(ts)[:10] if ts else None


def split_trips(
    trips: list[dict], strategy: str, change_date: str,
    *, since: str | None = None, until: str | None = None,
) -> tuple[list[float], list[float]]:
    """그 전략의 종결을 변경일 기준 전/후 bps 리스트로 나눈다.

    경계: 변경일 **당일은 후(after)** 에 넣는다 — 배포는 장 마감 후에만 하므로
    그날 이후 체결부터 새 설정이 적용된다(runbook 규칙). `since`가 있으면 그
    이전은 버린다(직전 변경보다 오래된 건 다른 실험의 데이터다)."""
    before, after = [], []
    for t in trips:
        if t.get("strategy") != strategy:
            continue
        d = _trip_date(t)
        bps = t.get("bps")
        if d is None or bps is None:
            continue
        if until is not None and d >= until:
            continue
        if d >= change_date:
            after.append(float(bps))
        elif since is None or d >= since:
            before.append(float(bps))
    return before, after


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def permutation_p(before: list[float], after: list[float]) -> float | None:
    """평균 차이가 우연일 확률. 양쪽 5건 미만이면 None(셀 가치가 없다).

    라벨을 섞어 |차이|가 관측치 이상인 비율을 센다 — 분포 가정이 없다.
    시드 고정이라 같은 입력이면 항상 같은 답(재현 가능)."""
    if len(before) < 5 or len(after) < 5:
        return None
    observed = abs((sum(after) / len(after)) - (sum(before) / len(before)))
    pool = before + after
    n_a = len(after)
    rng = random.Random(_SEED)
    hits = 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pool)
        a, b = pool[:n_a], pool[n_a:]
        if abs((sum(a) / len(a)) - (sum(b) / len(b))) >= observed:
            hits += 1
    return hits / PERMUTATIONS


def did_compare(
    trips: list[dict], strategy: str, change_date: str,
    *, control_strategies: list[str], since: str | None = None, until: str | None = None,
) -> dict:
    """이중차분. 대조군 = 같은 기간 파라미터가 안 바뀐 전략들의 종결 전부.

    `treated_delta`  변경군 (after - before) 평균 bp
    `control_delta`  대조군 (after - before) 평균 bp — 이게 곧 장세 몫
    `did`            둘의 차 = 장세를 뺀 순효과 추정
    """
    t_before, t_after = split_trips(trips, strategy, change_date, since=since, until=until)

    c_before: list[float] = []
    c_after: list[float] = []
    for sid in control_strategies:
        b, a = split_trips(trips, sid, change_date, since=since, until=until)
        c_before += b
        c_after += a

    t_b, t_a = _mean(t_before), _mean(t_after)
    c_b, c_a = _mean(c_before), _mean(c_after)
    treated_delta = (t_a - t_b) if (t_a is not None and t_b is not None) else None
    control_delta = (c_a - c_b) if (c_a is not None and c_b is not None) else None
    did = (treated_delta - control_delta) if (
        treated_delta is not None and control_delta is not None
    ) else None

    return {
        "strategy": strategy,
        "change_date": change_date,
        "n_before": len(t_before), "n_after": len(t_after),
        "mean_before": t_b, "mean_after": t_a,
        "median_before": st.median(t_before) if t_before else None,
        "median_after": st.median(t_after) if t_after else None,
        "treated_delta": treated_delta,
        "control_n_before": len(c_before), "control_n_after": len(c_after),
        "control_delta": control_delta,
        "control_strategies": list(control_strategies),
        "did": did,
        "p_value": permutation_p(t_before, t_after),
    }


def verdict(cmp: dict, min_sample: int = MIN_SAMPLE) -> tuple[str, str]:
    """`(라벨, 사유)`. **표본이 안 차면 판정하지 않는다** — 그게 기본값이다.

    라벨: `pending`(아직 모른다) / `improved` / `worsened` / `no_effect`
    """
    nb, na = cmp["n_before"], cmp["n_after"]
    if nb < min_sample or na < min_sample:
        return "pending", f"표본 부족 (변경 전 {nb}/{min_sample} · 후 {na}/{min_sample})"

    did, p = cmp["did"], cmp["p_value"]
    if did is None:
        return "pending", "대조군 표본이 없어 장세를 분리할 수 없다"
    if p is None:
        return "pending", "순열검정 표본 부족"
    if p > 0.10:
        return "no_effect", f"차이가 표본 노이즈와 구분되지 않는다 (p={p:.3f})"
    if did > 0:
        return "improved", f"장세 제외 순효과 {did:+.1f}bp/건 (p={p:.3f})"
    return "worsened", f"장세 제외 순효과 {did:+.1f}bp/건 (p={p:.3f})"


# ── 3. 사람이 읽는 출력 ───────────────────────────────────────────────────

def _bp(v: float | None) -> str:
    return f"{v:+.1f}bp" if v is not None else "—"


def experiment_text(cmp: dict, label: str, reason: str) -> str:
    icon = {"improved": "✅", "worsened": "❌", "no_effect": "➖", "pending": "⏳"}[label]
    name = {"improved": "개선", "worsened": "악화", "no_effect": "효과 없음",
            "pending": "판정 보류"}[label]
    lines = [
        f"{icon} [{cmp['strategy']}] {cmp['change_date']} 변경 — {name}",
        f"   {reason}",
        f"   변경군: 전 {cmp['n_before']}건 {_bp(cmp['mean_before'])} → "
        f"후 {cmp['n_after']}건 {_bp(cmp['mean_after'])} (Δ {_bp(cmp['treated_delta'])})",
    ]
    if cmp["control_delta"] is not None:
        lines.append(
            f"   대조군: 전 {cmp['control_n_before']}건 → 후 {cmp['control_n_after']}건 "
            f"(Δ {_bp(cmp['control_delta'])}) — {', '.join(cmp['control_strategies']) or '없음'}"
        )
    return "\n".join(lines)


def _sign_flip_permutation_p(vals: list[float]) -> float:
    """평균이 0과 구분되는가(부호검정형 순열). death_watch 와
    record_death_watch(작업2, 2026-09-02)가 공유하는 원시 계산 — 후자는
    K일 연속 판정을 위해 death_watch 보다 넓은(유의하지 않은 것 포함) 대상에
    같은 검정을 적용해야 해서 분리했다."""
    rng = random.Random(_SEED)
    mean = sum(vals) / len(vals)
    observed = abs(mean)
    hits = 0
    for _ in range(PERMUTATIONS):
        flipped = [v if rng.random() < 0.5 else -v for v in vals]
        if abs(sum(flipped) / len(flipped)) >= observed:
            hits += 1
    return hits / PERMUTATIONS


def _strategy_bps(trips: list[dict]) -> dict[str, list[float]]:
    by: dict[str, list[float]] = {}
    for t in trips:
        sid, bps = t.get("strategy"), t.get("bps")
        if sid and bps is not None:
            by.setdefault(sid, []).append(float(bps))
    return by


def _negative_edge_stats(trips: list[dict], min_sample: int) -> dict[str, dict]:
    """전략별 (n, mean_bp, p_value) — 표본이 충분하고 평균이 음수인 것만.
    death_watch 가 쓰는 원시 통계(양수 평균은 "사망" 개념 자체가 성립하지
    않으므로 애초에 계산하지 않는다 — 기존 death_watch 동작과 동일)."""
    out: dict[str, dict] = {}
    for sid, vals in sorted(_strategy_bps(trips).items()):
        if len(vals) < min_sample:
            continue
        mean = sum(vals) / len(vals)
        if mean >= 0:
            continue
        out[sid] = {"n": len(vals), "mean_bp": mean, "p_value": _sign_flip_permutation_p(vals)}
    return out


def _all_edge_stats(trips: list[dict], min_sample: int) -> dict[str, dict]:
    """전략별 (n, mean_bp, p_value|None) — 표본이 충분하면 **부호 무관 전부**.
    record_death_watch 전용: K거래일 연속 판정은 "매일의 상태"가 필요한데,
    _negative_edge_stats 처럼 음수 평균만 계산하면 회복(양수 전환)일에 아무
    줄도 안 남아 그 회복이 있었다는 사실 자체가 사라진다 — 그러면 회복 전후의
    사망일들이 원장에서 그냥 이어붙어 보여 K일 연속을 부풀린다(2026-09-02
    테스트 test_consecutive_dead_candidates_streak_breaks_on_recovery 로 고정).
    양수 평균은 애초에 "사망"이 아니므로 순열검정을 돌리지 않는다(p_value=None)
    — dead 판정에 필요 없는 계산이다."""
    out: dict[str, dict] = {}
    for sid, vals in sorted(_strategy_bps(trips).items()):
        if len(vals) < min_sample:
            continue
        mean = sum(vals) / len(vals)
        p = _sign_flip_permutation_p(vals) if mean < 0 else None
        out[sid] = {"n": len(vals), "mean_bp": mean, "p_value": p}
    return out


def death_watch(trips: list[dict], min_sample: int = MIN_SAMPLE) -> list[dict]:
    """전략 사망 판정 — 표본이 충분한데 실현 엣지가 유의하게 음수인 전략.

    소유자가 자리를 비운 동안 **묻지 않아도 소리쳐야 하는** 유일한 것이다
    (quant-expert §6이 요구하는 방어선). 판정만 하고 끄지는 않는다 —
    자동 정지는 사이징 권한과 같은 급이라 사람 결정이다(governor 층 0 원칙).

    "유의하게 음수" = 순열검정으로 평균이 0과 구분되는가. 부호만 보면 표본
    노이즈에 매주 다른 답이 나온다. 알림 문턱은 p<=0.05 — 아래
    record_death_watch/consecutive_dead_candidates(작업2, 2026-09-02)의 자동
    비활성 문턱(p<0.01, K일 연속)보다 느슨하다: 이 함수는 "사람에게 알릴
    가치가 있는가", 그쪽은 "사람 개입 없이 꺼도 되는가"라 바가 달라야 맞다."""
    stats = _negative_edge_stats(trips, min_sample)
    return [
        {"strategy": sid, **s} for sid, s in stats.items() if s["p_value"] <= 0.05
    ]


# ── 7. 사망 판정 지속 감시 → 자동 비활성 후보 (작업 2, 2026-09-02) ──────────
#
# death_watch()의 알림 문턱(p<=0.05, 하루짜리 스냅샷)은 "사람에게 알릴 가치가
# 있는가"였다. 자동으로 전략을 끄는 건 사이징 권한과 같은 급의 결정이라
# 문턱을 훨씬 엄격하게 잡는다: 표본 n>=30 + p<0.01 + **그 상태가 K거래일
# 연속(기본 5)** — 하루짜리 나쁜 스냅샷으로 전략을 죽이지 않는다.
#
# "K거래일 연속"을 판정하려면 매일의 상태가 필요한데, death_watch()는
# 매번 원장 전체에서 새로 계산하는 무상태 함수라 어제 뭐였는지 모른다.
# 그래서 하루 한 번(quant.apps.cli의 cmd_experiments, 16:30 크론) 스냅샷을
# 이 원장에 append한다 — record_fingerprints와 같은 멱등 관례로, 숫자가
# 어제와 똑같으면(주말 등 새 거래가 없는 날) 쓰지 않는다. 그래야 장이 안
# 열린 날이 "5일 연속"을 부풀리지도, 끊지도 않는다.
DEFAULT_DEATH_WATCH_PATH = "data/ledger/death_watch.jsonl"
KILL_STREAK_DAYS = 5      # "K일 연속" 기본값
KILL_P_THRESHOLD = 0.01   # 자동 비활성 문턱 — death_watch 알림 문턱(0.05)보다 엄격


def record_death_watch(
    trips: list[dict], today: date, path: Path | str = DEFAULT_DEATH_WATCH_PATH,
    min_sample: int = MIN_SAMPLE,
) -> list[dict]:
    """오늘의 표본충분 전략 전부(부호 무관)의 통계를 원장에 append한다. 직전
    기록과 (n, mean_bp, p_value)가 완전히 같으면(=새 거래 없음, 주말 등) 쓰지
    않는다 — record_fingerprints와 동일한 멱등 관례. 회복(양수 전환)일에도
    dead=false 로 줄이 남으므로(_all_edge_stats 참고) consecutive_dead_candidates
    가 그 지점에서 스트릭을 정확히 끊을 수 있다."""
    existing = load_changes(path)
    last: dict[str, dict] = {}
    for r in existing:
        sid = r.get("strategy")
        if sid:
            last[sid] = r

    stats = _all_edge_stats(trips, min_sample)
    added = []
    for sid, s in stats.items():
        dead = s["mean_bp"] < 0 and s["p_value"] is not None and s["p_value"] < KILL_P_THRESHOLD
        prev = last.get(sid)
        if (prev is not None and prev.get("n") == s["n"]
                and prev.get("mean_bp") == s["mean_bp"] and prev.get("p_value") == s["p_value"]):
            continue
        added.append({
            "date": today.isoformat(), "strategy": sid,
            "n": s["n"], "mean_bp": s["mean_bp"], "p_value": s["p_value"], "dead": dead,
        })

    if added:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            for row in added:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return added


def consecutive_dead_candidates(
    path: Path | str = DEFAULT_DEATH_WATCH_PATH, k_days: int = KILL_STREAK_DAYS,
) -> list[dict]:
    """최근 기록이 K개 연속 `dead: true`인 전략들 — 자동 비활성 후보.

    record_death_watch가 값이 안 바뀐 날은 안 쓰므로, 여기서 "최신 K개 기록"은
    곧 "최신 K번의 실제 변화가 전부 사망 판정"이라는 뜻이다. 회복(양수 전환
    또는 p>=0.01로 개선)이 한 번이라도 그 사이에 있었다면 그 시점에 새 기록이
    생기고(양수면 아예 기록이 없거나, 개선이면 dead=false 기록) 스트릭이
    끊긴다."""
    rows = load_changes(path)
    by_strategy: dict[str, list[dict]] = {}
    for r in rows:
        sid = r.get("strategy")
        if sid:
            by_strategy.setdefault(sid, []).append(r)

    out = []
    for sid, entries in sorted(by_strategy.items()):
        entries = sorted(entries, key=lambda r: r.get("date", ""))
        tail = entries[-k_days:]
        if len(tail) < k_days or not all(e.get("dead") for e in tail):
            continue
        latest = tail[-1]
        out.append({
            "strategy": sid, "streak_days": k_days,
            "n": latest["n"], "mean_bp": latest["mean_bp"], "p_value": latest["p_value"],
            "since": tail[0]["date"], "until": latest["date"],
        })
    return out


def daily_report(
    trips: list[dict], changes: list[dict], today: date, *, min_sample: int = MIN_SAMPLE,
) -> tuple[str | None, list[str]]:
    """매일 도는 판정. `(보낼 메시지 | None, 새로 확정된 실험 키 목록)`.

    **판정할 게 없으면 None** — 매일 "아직 모릅니다"를 보내면 사람이 안 읽고,
    안 읽는 알림은 없는 것보다 나쁘다(진짜 경보까지 같이 묻힌다).
    """
    exps = pending_experiments(changes, today)
    changed_recent = {e["strategy"] for e in exps if not e["superseded"]}
    all_strategies = {t.get("strategy") for t in trips if t.get("strategy")}
    controls = sorted(all_strategies - changed_recent)

    blocks, settled = [], []
    for e in exps:
        if e["superseded"]:
            continue
        cmp = did_compare(
            trips, e["strategy"], e["change_date"],
            control_strategies=controls, since=e["prev_date"], until=None,
        )
        label, reason = verdict(cmp, min_sample)
        if label == "pending":
            continue
        blocks.append(experiment_text(cmp, label, reason))
        settled.append(f"{e['strategy']}@{e['change_date']}")

    deaths = death_watch(trips, min_sample)
    if deaths:
        blocks.append("💀 전략 사망 경보 — 표본 충분 + 실현 엣지가 유의하게 음수")
        for d in deaths:
            blocks.append(
                f"   [{d['strategy']}] {d['n']}건 평균 {d['mean_bp']:+.1f}bp/건 "
                f"(p={d['p_value']:.3f}) — 자동 정지는 하지 않는다, 판단은 사람 몫"
            )

    if not blocks:
        return None, []

    header = f"🧪 자동 판정 — {today.isoformat()}"
    footer = (
        "※ 이중차분(DiD)은 장세 교란을 줄이지 없애지 않는다. 대조군이 변경군과 "
        "다른 종목·시간대를 거래하면 전제가 약해진다 — 인과가 아니라 증거다."
    )
    return "\n".join([header, "", *blocks, "", footer]), settled
