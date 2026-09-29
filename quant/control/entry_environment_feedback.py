"""Pure, prospective KR session evaluation; no tuning or trading side effects."""
from __future__ import annotations

import math
from datetime import datetime

from quant.control.report_accuracy import spearman_ic, wilson_ci
from quant.core.report_clock import KST

BENCHMARKS = ('KOSPI', 'KOSDAQ')
DIRECTION_DEADBAND_PCT = 0.2  # Fixed initial observation rule, not fitted.


def evaluate(forecast: dict, bars: dict, *, evaluated_at: str) -> dict:
    day = forecast['session_date']
    at = datetime.fromisoformat(evaluated_at)
    cutoff = datetime.fromisoformat(day).replace(hour=15, minute=40, tzinfo=KST)
    result = {'session_date': day, 'forecast_id': forecast.get('forecast_id'),
              'version': forecast.get('version', forecast.get('score_version')),
              'score': forecast.get('score'), 'direction': forecast.get('direction'),
              'evaluated_at': evaluated_at, 'benchmarks': {}, 'missing': [],
              'causal_status': 'unverified', 'review': []}
    for key in BENCHMARKS:
        bar = bars.get(key) or {}
        vals = [bar.get(k) for k in ('open', 'high', 'low', 'close')]
        valid = all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and v > 0 for v in vals)
        if at.tzinfo is None or at < cutoff or bar.get('date') != day or not valid:
            result['missing'].append(key)
            continue
        op, hi, lo, cl = vals
        if not lo <= min(op, cl) <= max(op, cl) <= hi:
            result['missing'].append(key)
            continue
        ret = (cl / op - 1) * 100
        actual = 'up' if ret > DIRECTION_DEADBAND_PCT else 'down' if ret < -DIRECTION_DEADBAND_PCT else 'flat'
        predicted = forecast.get('direction')
        ordinal = {'down': -1, 'flat': 0, 'up': 1}
        hit = actual == predicted if predicted in ordinal else None
        result['benchmarks'][key] = {
            'return_pct': round(ret, 6), 'downside_pct': round((lo / op - 1) * 100, 6),
            'actual_direction': actual, 'direction_hit': hit,
            'direction_error': abs(ordinal[actual] - ordinal[predicted]) if predicted in ordinal else None,
            'ohlc': bar, 'target': 'same_session_open_to_close',
        }
        if hit is False:
            result['review'].append(f'{key}: 장전 {predicted} / 실제 {actual}. 미국장 전달과 국내 장중 수급·신규 공시를 재확인할 것 — 원인 미확정.')
    if not result['review']:
        result['review'].append('일치 여부만으로 예측력이나 원인을 확정하지 않는다. 고정 규칙으로 다음 표본을 관찰한다.')
    return result


def summarize(rows: list[dict]) -> dict:
    """Per-index sample sizes; abstentions and missing outcomes stay visible."""
    output = {'benchmarks': {}, 'rule': 'fixed_v1_no_daily_retuning'}
    for key in BENCHMARKS:
        outcomes = [(r, r['benchmarks'][key]) for r in rows if key in r.get('benchmarks', {})]
        judged = [(r, b) for r, b in outcomes if b['direction_hit'] is not None]
        hits = sum(b['direction_hit'] for _, b in judged)
        n = len(judged)
        pairs = [(r['score'], b['return_pct']) for r, b in judged if r.get('score') is not None]
        output['benchmarks'][key] = {
            'n_outcomes': len(outcomes), 'n_forecasts': n, 'hits': hits,
            'accuracy_pct': round(hits / n * 100, 1) if n else None,
            'accuracy_ci': list(wilson_ci(hits, n)) if n else None,
            'rank_ic': spearman_ic(pairs) if len(pairs) >= 3 else None,
            'always_up_hits': sum(b['actual_direction'] == 'up' for _, b in judged),
            'always_flat_hits': sum(b['actual_direction'] == 'flat' for _, b in judged),
        }
    return output


def compare_context(prior: dict, current: dict, outcome: dict | None) -> dict:
    """Diagnostic hypotheses, not causal attribution or one-day coefficient fitting."""
    old = {f['id']: f for f in prior.get('factors', [])}
    changes = []
    for factor in current.get('factors', []):
        previous = old.get(factor['id'], {}).get('value')
        value = factor.get('value')
        if (factor.get('status') == 'ok' and isinstance(previous, (int, float))
                and isinstance(value, (int, float)) and previous != value):
            changes.append({'label': factor['label'], 'previous': previous, 'current': value,
                            'change': round(value - previous, 4)})
    mismatch = any(b.get('direction_hit') is False for b in (outcome or {}).get('benchmarks', {}).values())
    review = list((outcome or {}).get('review', []))
    if mismatch:
        review.insert(0, '예측 불일치: 아래 변화는 다음 아침까지 관측된 차이이며 인과관계는 미확정입니다. 미국장 신호 전달 실패, 국내 수급, 장중 신규 뉴스의 대안 가설을 확인합니다.')
        if not changes:
            review.append('확인 가능한 요인 변화 없음: 자료 부족과 모형 누락 요인을 구분할 추가 근거가 필요합니다.')
        else:
            review.append('다음 관찰 과제: 변화한 요인이 반복해서 오차와 동행하는지 누적 표본·항상 상승/보합 기준선과 비교합니다.')
    return {'factor_changes': changes, 'review': review, 'weight_action': 'unchanged'}
