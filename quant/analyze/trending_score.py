"""토스 랭킹으로 "실제로 지금 몰리고 있다"를 정량화하는 결정론적 점수.

`mentions.mark_origin`의 `in_ranking` 은 "랭킹 어딘가에 있다/없다"만 본다 —
같은 대형주가 매일 거래대금 1위인 것과, 오늘 갑자기 튀어 순위에 든 것을
구분하지 못한다. 이 모듈은 두 근거를 더한다:

1. **보드 순위** — 어떤 보드(거래대금/상승률/하락률/토스 사용자 거래대금)에
   몇 위로 올랐나. 여러 보드에 동시에 뜰수록, 상위일수록 가점이 쌓인다.
2. **상대 거래량** — 오늘 거래대금을 그 종목의 최근 베이스라인
   (`quant/analyze/rank_history.py`, 과거 스냅샷에서만 읽는다)과 비교한 배율.
   베이스라인이 부족(신규/희소 종목)하면 그 요인은 아예 안 넣는다 — "몰랐다"를
   0점(=평소와 같음)으로 채우면 거짓 신호가 된다.

3. **방향** — 오르고 있나 내리고 있나. 관심의 *크기*는 방향이 정한 부호를 따른다.

`symbol_score.py`와 같은 형태(±점, factors 노출, `scoring.to_100`)를 따른다 —
근거를 못 펼치는 점수는 검증도 채점도 불가능하다는 원칙이 여기도 적용된다.

## 왜 방향이 필요한가 (2026-08-13)

이 모듈은 처음에 보드 종류를 가리지 않고 가점했다. 그래서 **하락률 보드에 오른
CMG제약이 트렌딩 55점**을 받았다 — 폭락 중인 종목이 "지금 몰린다"는 이유로 매수
후보에 올랐다. 우리 전략은 전부 롱 온리다.

거래가 몰리는 것과 **매수세**가 몰리는 것은 다르다. 가격을 올리는 건 후자다.
거래대금 보드는 방향을 모른다 — 공황 매도도 거래대금 1위를 만든다.

규칙은 하나다: **방향 보드(상승률/하락률)는 자기 부호를 갖고, 관심 크기
(거래대금·토스 사용자 거래대금·상대 거래량)는 방향이 정한 부호를 따른다.**
크기를 부호로 곱하는 것이라 새로 정할 상수가 없다 — 하락 중 거래 급증은 같은
크기의 감점이 된다(그게 매도세의 크기다).

방향 미상(`change_pct is None`)은 하락으로 취급하지 않는다. 데이터가 없는 것은
하락의 증거가 아니고, 실제로 보드에 오른 종목은 항상 `change_pct`가 온다.
"""
from __future__ import annotations

from quant.analyze.scoring import label_100, to_100

# 보드 상위 몇 위까지를 "톱"으로 볼지. 토스 랭킹 보드는 top 10만 온다.
RANK_TOP = 3
BOARD_TOP_POINTS = 2
BOARD_LOWER_POINTS = 1

# 상대 거래량 배율 구간(오늘 거래대금 / 최근 베이스라인 중앙값)과 가점.
VOL_SURGE_RATIO = 3.0
VOL_ELEVATED_RATIO = 1.5
VOL_SURGE_POINTS = 3
VOL_ELEVATED_POINTS = 2
VOL_NORMAL_POINTS = 1

# 베이스라인으로 인정할 최소 과거 일수. 미만이면 상대 거래량을 계산하지 않는다
# (신규/희소 종목의 배율이 극단값으로 튀는 것을 막는다).
MIN_HISTORY_DAYS = 3

# 방향 보드 — 자기 부호를 갖는다. 나머지(거래대금·토스 사용자 거래대금)는 방향을
# 모르므로 change_pct 가 정한 부호를 따른다.
BULLISH_BOARD = "상승률"
BEARISH_BOARD = "하락률"

# 이론상 최대 가점: 상승률 톱(2) + 거래대금 톱(2) + 토스 사용자 거래대금 톱(2)
# + 거래량 급증(3) = 9. 하락률은 상승률과 동시에 오를 수 없으므로 분모에 안 든다.
# 고정 분모인 이유는 symbol_score.SPAN과 같다 — 근거가 부족할수록 50 근처에 머문다.
_POSITIVE_BOARDS_MAX = 3
SPAN = _POSITIVE_BOARDS_MAX * BOARD_TOP_POINTS + VOL_SURGE_POINTS


def symbol_ranking_info(
    boards: dict[str, list[dict]], symbol: str
) -> tuple[dict[str, int], int | None, float | None]:
    """symbol이 오른 보드별 순위, 오늘 거래대금, 등락률.

    거래대금과 등락률은 있는 보드 중 아무 값이나 쓰되 거래대금 보드 값을 우선한다
    (같은 종목이면 어느 보드에서 읽든 같아야 하지만, 보드마다 스냅샷 시각이
    미세하게 다를 수 있어 기준을 하나로 고정한다).
    """
    ranks: dict[str, int] = {}
    amount: int | None = None
    change_pct: float | None = None
    for board, items in boards.items():
        for item in items:
            if item["symbol"] == symbol:
                ranks[board] = item["rank"]
                if amount is None or board == "거래대금":
                    amount = item.get("trading_amount")
                if change_pct is None or board == "거래대금":
                    raw = item.get("change_pct")
                    change_pct = float(raw) if raw is not None else change_pct
                break
    return ranks, amount, change_pct


def _median(values: list[int]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def relative_volume(today_amount: int | None, baseline: list[int]) -> float | None:
    """오늘 거래대금 / 최근 베이스라인 중앙값.

    베이스라인이 `MIN_HISTORY_DAYS` 미만이거나 오늘 값이 없으면 None — "모른다"를
    1.0(평소와 같음)으로 채우면 확인 안 된 것과 실제로 평이한 것이 섞인다.
    """
    if today_amount is None or len(baseline) < MIN_HISTORY_DAYS:
        return None
    base = _median(baseline)
    if base <= 0:
        return None
    return today_amount / base


def trending_score(
    boards_ranks: dict[str, int],
    today_amount: int | None,
    baseline: list[int],
    change_pct: float | None = None,
) -> dict:
    """`{"score","score100","label","factors","boards","relative_volume",
    "baseline_days","change_pct"}`.

    boards_ranks: symbol_ranking_info()가 준 {보드명: 순위}.
    today_amount: symbol_ranking_info()가 준 오늘 거래대금(없으면 None).
    baseline: rank_history.load_trading_amount_history() 결과.
    change_pct: 오늘 등락률(%). 관심 크기 요인의 **부호**를 정한다 — 모듈 docstring
        참고. None(미상)은 하락으로 취급하지 않는다.
    """
    factors: list[dict] = []
    declining = change_pct is not None and change_pct < 0
    # 관심의 크기는 방향이 정한 부호를 따른다. 하락 중 거래 급증은 매도세의 크기다.
    attention_sign = -1 if declining else 1

    def add(key: str, delta: int, text: str) -> None:
        factors.append({"key": key, "delta": delta, "text": text})

    if declining:
        add("direction", 0, f"하락 {change_pct:.1f}% — 관심을 매수세로 세지 않는다")

    for board, rnk in sorted(boards_ranks.items()):
        magnitude = BOARD_TOP_POINTS if rnk <= RANK_TOP else BOARD_LOWER_POINTS
        if board == BULLISH_BOARD:
            sign = 1        # 방향 보드는 자기 부호를 갖는다
        elif board == BEARISH_BOARD:
            sign = -1
        else:
            sign = attention_sign
        add(f"board:{board}", sign * magnitude, f"{board} {rnk}위")

    rel_vol = relative_volume(today_amount, baseline)
    if rel_vol is not None:
        suffix = " 급증(매도)" if declining else " 급증"
        if rel_vol >= VOL_SURGE_RATIO:
            add("rel_volume", attention_sign * VOL_SURGE_POINTS,
                f"평소 대비 거래대금 {rel_vol:.1f}배{suffix}")
        elif rel_vol >= VOL_ELEVATED_RATIO:
            add("rel_volume", attention_sign * VOL_ELEVATED_POINTS,
                f"평소 대비 거래대금 {rel_vol:.1f}배")
        elif rel_vol >= 1.0:
            add("rel_volume", attention_sign * VOL_NORMAL_POINTS,
                f"평소 대비 거래대금 {rel_vol:.1f}배")
        # rel_vol < 1.0(평소보다 적음)은 가점 없음 — factor 자체를 안 남긴다.

    score = sum(f["delta"] for f in factors)
    score100 = to_100(score, SPAN)
    label = label_100(score100, "트렌딩", "관심 저조")
    return {
        "score": score,
        "score100": score100,
        "label": label,
        "factors": factors,
        "boards": dict(boards_ranks),
        "relative_volume": round(rel_vol, 2) if rel_vol is not None else None,
        "baseline_days": len(baseline),
        "change_pct": change_pct,
    }


def score_all(
    cont: dict[str, dict],
    ranking_boards: dict[str, list[dict]],
    market: str,
    session_date,
    snap_root,
) -> dict[str, dict]:
    """cont(오늘 뉴스에 잡힌 종목) 전부에 트렌딩 점수를 매긴다.

    랭킹에 전혀 없는 종목은 boards={}, today_amount=None → factors=[] →
    score100=50(중립)이 된다. "근거 없음"과 "근거가 나쁨"을 섞지 않기 위해
    50을 중립으로 쓰는 to_100/label_100의 기존 규약을 그대로 따른다.
    """
    from quant.analyze.rank_history import load_trading_amount_history

    out: dict[str, dict] = {}
    for symbol in cont:
        ranks, amount, change_pct = symbol_ranking_info(ranking_boards, symbol)
        baseline = load_trading_amount_history(market, symbol, session_date, snap_root)
        out[symbol] = trending_score(ranks, amount, baseline, change_pct)
    return out
