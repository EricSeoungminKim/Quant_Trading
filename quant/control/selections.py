"""선정 원장 — 리포트가 그날 올린 종목의 **속성 벡터**를 영속화한다.

## 왜 지금 만드나

목표는 `리포트 → 검증 → 매매 → 수익 → 파라미터 조정`의 되먹임 고리다. 앞의 넷은
이미 돈다. 없는 건 마지막 화살표 하나인데, 그걸 만들려면 **"어떤 속성의 종목이
실제로 돈이 됐나"**를 계산할 수 있어야 한다. 지금은 그 계산의 입력이 없다.

**속성은 썩는다. 가격은 안 썩는다.** 2026-08-13 삼성전자의 트렌딩 점수가 68점,
뉴스 13건, 거래대금 1위였다는 사실은 오늘 남기지 않으면 내일 재구성할 수 없다.
반면 그 종목이 D+1에 얼마였는지는 몇 달 뒤에도 언제든 조회된다. 그래서 이 모듈은
**속성만** 남기고, 전방 수익률은 나중에 채운다(`outcome_*` 필드를 비워 둔다).

## 왜 매매한 종목만으로는 부족한가

실제 체결은 하루 3건 남짓이다(세션 상한). 30건 표본을 모으는 데 열흘이 걸린다.
그런데 리포트는 매일 **30종목**을 올린다. 사지 않은 종목도 "샀으면 어땠을까"가
그대로 데이터다 — 표본이 하루 3개에서 30개로 열 배가 된다. 게다가 매매 데이터만
보면 **확신도 엔진이 떨어뜨린 종목**의 성과를 영영 알 수 없어, 문턱이 너무 높은지
낮은지 판단할 방법이 없다. 그래서 후보 여부(`is_candidate`)와 탈락 여부를 함께
남긴다.

## 재현성

리포트 스냅샷과 달리 이 파일은 **append-only 누적**이다. 같은 (날짜, 시장, 종목)이
다시 들어오면 갱신하지 않고 건너뛴다 — 하루에 리포트를 두 번 돌려도 표본이
중복되지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

# 하네스가 나중에 읽을 스키마. 필드를 **지우거나 이름을 바꾸지 않는다** — 과거
# 행이 못 읽히면 그날 데이터를 통째로 잃는다. 추가는 자유롭다(없는 필드는 None).
SCHEMA = 1


def _attributes(sym: dict) -> dict:
    """엔진 페이로드의 종목 항목 → 학습용 속성 벡터.

    `.get()`으로만 읽는다 — 리포트가 새 필드를 더하거나 빼도 원장 기록이 죽지
    않아야 한다. 없는 값은 None 으로 남긴다(0으로 위장하지 않는다: "트렌딩 0점"과
    "트렌딩을 계산 못 했다"는 전혀 다른 사건이다).
    """
    boards = sym.get("trending_boards") or {}
    return {
        "symbol": sym.get("symbol"),
        "name": sym.get("name"),
        # --- 리포트가 판단한 점수 ---
        "ai_score100": sym.get("ai_score100"),
        "trending_score100": sym.get("trending_score100"),
        "trending_label": sym.get("trending_label"),
        # judgment v2(§E-2)의 점수 출처 — 없으면 selection_judgment 가 영원히
        # trending_score100 폴백만 탄다.
        "baseline_score100": sym.get("baseline_score100"),
        # --- 뉴스 근거 ---
        "news_articles_today": sym.get("news_articles_today"),
        "news_streak_days": sym.get("news_streak_days"),
        "news_days_in_window": sym.get("news_days_in_window"),
        # --- 실제 거래 쏠림 근거 ---
        "in_ranking": sym.get("in_ranking"),
        "trending_boards": boards,
        "best_board_rank": min(boards.values()) if boards else None,
        "n_boards": len(boards),
        "relative_volume": sym.get("relative_volume"),
        "trending_baseline_days": sym.get("trending_baseline_days"),
        # --- 수급·컨센서스 ---
        "foreign_buy_streak": sym.get("foreign_buy_streak"),
        "inst_buy_streak": sym.get("inst_buy_streak"),
        "analyst_opinion_score": sym.get("analyst_opinion_score"),
        "analyst_target_price": sym.get("analyst_target_price"),
        "upside_pct": sym.get("upside_pct"),
        # --- 선정 당시 시세 (전방 수익률의 기준가) ---
        "close": sym.get("close"),
        "change_pct": sym.get("change_pct"),
        "origin": sym.get("origin"),
        "is_new": sym.get("is_new"),
    }


def build_rows(payload: dict, candidate_symbols: set[str],
               params: dict | None = None,
               news_z_by_symbol: dict[str, float] | None = None,
               producer: str | None = None) -> list[dict]:
    """엔진 페이로드 → 선정 원장 행들.

    `candidate_symbols`: `AUTO_WATCH` 줄에 실제로 실린 심볼. 리포트가 올린 전체
    종목 중 어디까지가 후보였는지가 문턱 튜닝의 핵심 변수다.

    `news_z_by_symbol`: 뉴스 발행량 z-score(H-2 Task 4, `quant.analyze.news_momentum`).
    이 값은 엔진 페이로드에 없다(별도 원장 `mentions.jsonl` 집계) — 그래서
    `_attributes()` 의 명시적 허용목록이 아니라 여기서 직접 얹는다. 계산 불가
    (표본 부족·σ=0)면 **키 자체를 생략**한다 — `baseline_score100` 처럼 None 을
    남기지 않는 이유는, 이 속성이 아직 모든 선정 심볼에 적용되지 않는 실험적
    피처라 "이 필드를 아예 안 본 옛 행"과 "봤는데 계산이 안 된 행"을 구분할
    필요가 없기 때문이다(리더보드는 결측 키를 결측 값과 동일하게 취급한다).

    `producer`: 이 선정을 낸 생산자(예: `"intraday_scorer"`). 기본값 `None`이면
    **행에 `producer` 키 자체를 넣지 않는다** — 기존 리포트 본선 호출부는
    바이트 단위로 하위호환이다. 값을 주면 `append()` 의 자연키에 producer가
    포함돼(그 함수 docstring 참고) 같은 (날짜,시장,종목)을 다른 producer가
    각자 독립적으로 기록할 수 있다 — 서브프로젝트 K(단타 스코어러)가 첫
    사용처다.
    """
    session_date = payload.get("session_date")
    market = payload.get("market")
    stance = payload.get("stance") or {}
    features = payload.get("features") or {}
    news_z_by_symbol = news_z_by_symbol or {}

    rows: list[dict] = []
    for sym in payload.get("symbols") or []:
        attrs = _attributes(sym)
        row = {
            "schema": SCHEMA,
            "date": session_date,
            "market": market,
            **attrs,
            # 후보로 뽑혔는가 — 안 뽑힌 종목의 성과가 문턱이 옳은지 알려준다
            "is_candidate": attrs["symbol"] in candidate_symbols,
            # 그날의 시장 국면. 같은 속성이라도 국면에 따라 결과가 달라진다
            "market_stance100": stance.get("score100"),
            "market_stance_label": stance.get("label"),
            "market_vix": features.get("vix"),
            "market_risk_flags": features.get("risk_flags"),
            "missing_sources": features.get("missing_sources"),
            # 어떤 임계값이 적용된 결과인지 — 없으면 비교 자체가 무의미하다
            "params": params or {},
            # 전방 수익률은 나중에 채운다. 필드를 미리 만들어 두어 스키마가
            # 흔들리지 않게 한다.
            "outcome_filled": False,
        }
        if producer is not None:
            row["producer"] = producer
        news_z = news_z_by_symbol.get(attrs["symbol"])
        if news_z is not None:
            row["news_z"] = news_z
        rows.append(row)
    return rows


# 이 값이 없으면 전방 수익률을 **영원히** 계산할 수 없다 — 그날 표본이 통째로 죽는다.
_BASE_PRICE_KEY = "close"


def _natural_key(row: dict) -> tuple:
    """append()/existing_keys() 의 자연키 — (날짜, 시장, 종목, producer).

    producer 를 키에 포함하는 이유(서브프로젝트 K): 같은 (날짜,시장,종목)을
    여러 producer가 각자 독립적으로 채점해 원장에 남길 수 있어야 한다 — 하나가
    다른 하나를 "중복"으로 가리고 스킵되면 그 producer의 그날 표본이 통째로
    사라진다. `producer` 키가 없는 행(리포트 본선, 기존 전량)은 `.get()`이
    `None`을 돌려주므로 **기존 동작과 완전히 동일하게** 서로 겹친다 — 이
    변경은 하위호환이다.
    """
    return (row.get("date"), row.get("market"), row.get("symbol"), row.get("producer"))


def append(rows: list[dict], path: Path) -> int:
    """중복(같은 날짜·시장·종목·producer)은 건너뛰고 추가한다. 추가된 행 수를
    반환한다.

    **예외 하나: 기존 행에 기준가가 없고 새 행에 있으면 승격(교체)한다.**

    2026-08-14 실측 사고: API 키가 깨진 상태로 빌드가 돌아 `close=None` 인 123행이
    먼저 들어갔고, 키를 고친 뒤 재빌드한 행은 전부 중복으로 스킵됐다. 중복 방지는
    의도된 설계지만(하루 두 번 돌려도 표본이 중복되지 않게), 그 때문에 **깨진 첫
    기록이 그날을 영구히 오염시켰다.**

    승격은 **결측 → 존재 한 방향뿐이다.** 좋은 값을 덮어쓰지 않으므로 그날의 판단
    근거가 소급 변경되는 위험이 없다(소급 변경되면 채점이 무의미해진다). 사후에
    채운 `outcome_*` 도 보존한다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {_natural_key(r): r for r in load(path)}
    upgrades: dict[tuple, dict] = {}
    fresh: list[dict] = []

    for row in rows:
        key = _natural_key(row)
        if not row.get("symbol"):
            continue
        old = existing.get(key)
        if old is None:
            if key not in {_natural_key(r) for r in fresh}:
                fresh.append(row)
            continue
        if old.get(_BASE_PRICE_KEY) is None and row.get(_BASE_PRICE_KEY) is not None:
            # 사후에 채운 값(outcome_*)은 새 행이 모르므로 기존 것을 살린다.
            merged = {**row}
            for k, v in old.items():
                if k.startswith("outcome_") and v is not None:
                    merged[k] = v
            upgrades[key] = merged

    if upgrades:
        rewrite([upgrades.get(k, r) for k, r in existing.items()], path)

    added = 0
    with path.open("a", encoding="utf-8") as f:
        for row in fresh:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            added += 1
    return added + len(upgrades)


def existing_keys(path: Path) -> set[tuple]:
    """이미 기록된 (날짜, 시장, 종목, producer). 깨진 줄은 건너뛴다."""
    if not path.exists():
        return set()
    out: set[tuple] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        out.add(_natural_key(row))
    return out


def load(path: Path) -> list[dict]:
    """원장 전체. 깨진 줄은 건너뛴다 — 일부 손상이 분석 전체를 죽이면 안 된다."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("symbol"):
            rows.append(row)
    return rows


def rewrite(rows: list[dict], path: Path) -> int:
    """행 전체를 원자적으로 다시 쓴다 — **전방 수익률 갱신 전용** (Phase 7.2).

    이 파일은 append-only 누적이지만 `outcome_*` 는 처음부터 **사후 갱신** 대상으로
    설계돼 있었다(`pending_outcomes` 가 그 전제다). 그 갱신에만 이 함수를 쓴다 —
    속성 벡터를 고치는 데 쓰면 그날의 판단 근거가 소급 변경되어 채점이 무의미해진다.

    tmp-replace 로 쓴다: 쓰다가 죽으면 원본이 남아야 한다. 원장을 반쯤 쓴 상태로
    남기면 그날 표본을 통째로 잃는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return len(rows)


def pending_outcomes(rows: list[dict], today: str, min_age_days: int = 1) -> list[dict]:
    """전방 수익률을 아직 못 채운, 그리고 채울 만큼 시간이 지난 행들.

    `min_age_days` 미만이면 아직 D+1이 오지 않았다는 뜻이라 건너뛴다.
    """
    from datetime import date

    try:
        today_d = date.fromisoformat(today)
    except (TypeError, ValueError):
        return []
    out = []
    for row in rows:
        if row.get("outcome_filled"):
            continue
        try:
            age = (today_d - date.fromisoformat(row["date"])).days
        except (TypeError, ValueError, KeyError):
            continue
        if age >= min_age_days:
            out.append(row)
    return out
