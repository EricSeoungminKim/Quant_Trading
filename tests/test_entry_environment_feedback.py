from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.adapters.entry_environment import ForecastStore
from quant.control.entry_environment_feedback import evaluate, summarize

KST = ZoneInfo('Asia/Seoul')


def forecast():
    return {'session_date': '2026-09-29', 'generated_at': '2026-09-29T07:40:00+09:00',
            'score_version': 'v1', 'score': 70, 'direction': 'up', 'factors': []}


def test_freeze_is_first_write_and_cannot_backdate(tmp_path):
    store = ForecastStore(tmp_path)
    now = datetime(2026, 9, 29, 8, tzinfo=KST)
    first = store.freeze(forecast(), now=now)
    changed = {**forecast(), 'score': 20}
    assert store.freeze(changed, now=now) == first
    assert store.forecasts()[0]['score'] == 70
    assert store.freeze({**forecast(), 'session_date': '2026-09-28'}, now=now) is None
    assert ForecastStore(tmp_path / 'late').freeze(forecast(), now=now.replace(hour=9)) is None


def test_future_cutoff_and_naive_clock_rejected(tmp_path):
    store = ForecastStore(tmp_path)
    now = datetime(2026, 9, 29, 8, tzinfo=KST)
    assert store.freeze({**forecast(), 'generated_at': '2026-09-29T08:30:00+09:00'}, now=now) is None
    with pytest.raises(ValueError):
        store.freeze(forecast(), now=now.replace(tzinfo=None))


def test_exact_session_ohlc_and_direction():
    bars = {'KOSPI': {'date': '2026-09-29', 'open': 100, 'close': 99, 'low': 97, 'high': 101}}
    result = evaluate(forecast(), bars, evaluated_at='2026-09-30T07:00:00+09:00')
    assert result['benchmarks']['KOSPI']['return_pct'] == pytest.approx(-1)
    assert result['benchmarks']['KOSPI']['downside_pct'] == pytest.approx(-3)
    assert result['benchmarks']['KOSPI']['direction_hit'] is False
    assert result['benchmarks']['KOSPI']['direction_error'] == 2
    assert result['causal_status'] == 'unverified'
    assert 'KOSDAQ' in result['missing']
    bars['KOSPI']['date'] = '2026-09-28'
    assert not evaluate(forecast(), bars, evaluated_at='2026-09-30T07:00:00+09:00')['benchmarks']


def test_no_outcome_before_close_and_invalid_prices():
    bars = {'KOSPI': {'date': '2026-09-29', 'open': 100, 'close': 99, 'low': 97, 'high': 101}}
    assert not evaluate(forecast(), bars, evaluated_at='2026-09-29T15:00:00+09:00')['benchmarks']
    bars['KOSPI']['open'] = float('nan')
    assert not evaluate(forecast(), bars, evaluated_at='2026-09-30T07:00:00+09:00')['benchmarks']


def test_abstention_not_counted_as_wrong_or_right():
    bars = {'KOSPI': {'date': '2026-09-29', 'open': 100, 'close': 99, 'low': 97, 'high': 101}}
    result = evaluate({**forecast(), 'score': None, 'direction': None}, bars,
                      evaluated_at='2026-09-30T07:00:00+09:00')
    assert result['benchmarks']['KOSPI']['direction_hit'] is None
    stats = summarize([result])
    assert stats['benchmarks']['KOSPI']['n_forecasts'] == 0
    assert stats['benchmarks']['KOSPI']['n_outcomes'] == 1


def test_refresh_retries_missing_and_does_not_rewrite_completed(tmp_path):
    from quant.apps.entry_environment import feedback_view, refresh_outcomes
    store = ForecastStore(tmp_path)
    store.freeze(forecast(), now=datetime(2026, 9, 29, 8, tzinfo=KST))
    now = datetime(2026, 9, 30, 7, tzinfo=KST)
    bar = {'date': '2026-09-29', 'open': 100, 'close': 99, 'low': 97, 'high': 101}
    refresh_outcomes(tmp_path, now=now, fetch=lambda d: {'KOSPI': bar})
    refresh_outcomes(tmp_path, now=now, fetch=lambda d: {'KOSDAQ': bar})
    assert not store.outcomes()[0]['missing']
    refresh_outcomes(tmp_path, now=now, fetch=lambda d: pytest.fail('completed session fetched again'))
    assert '방향 불일치' in feedback_view(tmp_path, now.date())['summary']


def test_mismatch_review_uses_recorded_factor_changes_without_retuning():
    from quant.control.entry_environment_feedback import compare_context
    prior = {'factors': [{'id': 'vix', 'label': 'VIX', 'value': 15}]}
    current = {'factors': [{'id': 'vix', 'label': 'VIX', 'value': 30, 'status': 'ok'}]}
    outcome = {'benchmarks': {'KOSPI': {'direction_hit': False}}, 'review': []}
    result = compare_context(prior, current, outcome)
    assert result['factor_changes'][0]['change'] == 15
    assert result['weight_action'] == 'unchanged'
    assert '인과' in result['review'][0]


def test_after_open_rebuild_keeps_first_forecast(tmp_path):
    store = ForecastStore(tmp_path)
    first = store.freeze(forecast(), now=datetime(2026, 9, 29, 8, tzinfo=KST))
    later = {**forecast(), 'score': 10, 'direction': 'down'}
    assert store.freeze(later, now=datetime(2026, 9, 29, 16, tzinfo=KST)) == first


def test_partial_retry_keeps_original_mismatch_explanation(tmp_path):
    from quant.apps.entry_environment import refresh_outcomes
    store = ForecastStore(tmp_path)
    store.freeze(forecast(), now=datetime(2026, 9, 29, 8, tzinfo=KST))
    now = datetime(2026, 9, 30, 7, tzinfo=KST)
    bar = {'date': '2026-09-29', 'open': 100, 'close': 99, 'low': 97, 'high': 101}
    refresh_outcomes(tmp_path, now=now, fetch=lambda d: {'KOSPI': bar})
    refresh_outcomes(tmp_path, now=now, fetch=lambda d: {'KOSDAQ': {**bar, 'close':101}})
    assert any('KOSPI' in text for text in store.outcomes()[0]['review'])


def test_bad_optional_ledger_is_reported_without_losing_brief(tmp_path):
    from quant.apps.entry_environment import build_view
    from quant.collect.contracts import Snapshot
    now = datetime(2026, 9, 29, 8, tzinfo=KST)
    path = tmp_path / 'data/ledger/macro_rates.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text('{broken\n')
    result = build_view(Snapshot(1, 'KR', now.date(), now, {}), tmp_path)
    assert result['score'] is None
    assert any(r['code'] == 'ledger_parse' for r in result['risks'])


def test_naver_index_ohlc_parser_requires_exact_session_and_symbol():
    from datetime import date
    from quant.adapters.entry_environment import parse_index_chart
    raw = '<protocol><chartdata symbol="KOSPI"><item data="20260929|6844.41|6898.36|6782.99|6870.81|221683" /></chartdata></protocol>'
    bar = parse_index_chart(raw, 'KOSPI', date(2026, 9, 29))
    assert bar['open'] == 6844.41
    assert bar['source'] == 'naver:KOSPI'
    assert parse_index_chart(raw, 'KOSDAQ', date(2026, 9, 29)) is None
    assert parse_index_chart(raw, 'KOSPI', date(2026, 9, 30)) is None
