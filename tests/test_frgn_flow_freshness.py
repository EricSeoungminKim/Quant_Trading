"""Live foreign-flow evidence must belong to completed, consecutive KR sessions."""
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from quant.control import frgn_flow


def ledger(tmp_path, dates, values=None):
    path = tmp_path / 'data/ledger/frgn_flow.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [dict(date=d, symbol='005930', foreign_net=v, inst_net=0)
            for d, v in zip(dates, values or [100] * len(dates))]
    path.write_text('\n'.join(json.dumps(r) for r in rows))
    return path


@pytest.mark.parametrize('dates,as_of,expected', [
    (['2026-09-08', '2026-09-09'], '2026-10-23', []),
    (['2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26'], '2026-10-23', ['2026-10-21', '2026-10-22']),
    (['2026-10-15', '2026-10-16'], '2026-10-19', ['2026-10-15', '2026-10-16']),
    (['2026-10-07', '2026-10-08'], '2026-10-12', ['2026-10-07', '2026-10-08']),
    (['2026-09-09', '2026-10-22'], '2026-10-23', []),
    (['2026-10-20', '2026-10-22'], '2026-10-23', []),
    (['2026-10-22'], '2026-10-23', []),
])
def test_completed_session_evidence(tmp_path, dates, as_of, expected):
    path = ledger(tmp_path, dates)
    rows = frgn_flow.load_fresh_series(path, '005930', as_of=date.fromisoformat(as_of))
    assert [r['date'] for r in rows] == expected
    assert len(frgn_flow.load_series(path, '005930')) == len(dates)  # history unchanged


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), 'unknown', True, False])
def test_invalid_latest_value_fails_closed(tmp_path, value):
    path = ledger(tmp_path, ['2026-10-21', '2026-10-22'], [100, value])
    assert frgn_flow.load_fresh_series(path, '005930', as_of=date(2026,10,23)) == []


def test_brief_never_renews_stale_frgn(tmp_path):
    sys.path.insert(0, str(Path(__file__).parents[1] / 'server/scripts'))
    import brief_from_report
    ledger(tmp_path, ['2026-09-08', '2026-09-09'])
    assert brief_from_report._foreign_flow_labels(tmp_path, {'005930'}, as_of=date(2026,10,23)) == {}


def test_composition_filters_tags_preserving_held_and_other_tags(tmp_path, monkeypatch):
    from quant.apps import assembly
    path = ledger(tmp_path, ['2026-09-08', '2026-09-09'])
    tags = {'005930':['FRGN','EVENT'], '000660':['FRGN_EXIT','TREND'], 'AAPL':['EVENT']}
    result = assembly._fresh_foreign_tags(tags, path=path, as_of=date(2026,10,23))
    assert result == {'005930':['EVENT'], '000660':['TREND'], 'AAPL':['EVENT']}
    assert tags['005930'] == ['FRGN','EVENT']
    monkeypatch.setattr(assembly, '_fresh_foreign_tags', lambda original: result)
    cfg = {'strategies': {'vol_breakout_cat': {'enabled': True, 'class':'vol_breakout', 'universe':'watchlist',
           'symbols':[], 'markets':['KR'], 'universe_filter':{'KR':{'require_any':['FRGN']}},
           'capital_fraction':{'KR':1}, 'params':{}}}}
    class Universe:
        def symbols(self): return ['005930','000660']
        def tags(self): return tags
    strategies, _, _ = assembly.rebuild_strategies(cfg, Universe(), held_symbols=['005930'])
    assert strategies[0].symbols == ['005930']


def test_october_substitute_holiday_preserves_full_verified_session_window(tmp_path):
    # Session dates from Naver's real 20-row 2026-10-22 response; no fabricated holiday row.
    dates = ['2026-10-22', '2026-10-21', '2026-10-20', '2026-10-19',
             '2026-10-16', '2026-10-15', '2026-10-14', '2026-10-13',
             '2026-10-12', '2026-10-08', '2026-10-07', '2026-10-06',
             '2026-10-02', '2026-10-01', '2026-09-30', '2026-09-29',
             '2026-09-28', '2026-09-23', '2026-09-22', '2026-09-21']
    path = ledger(tmp_path, dates)
    rows = frgn_flow.load_fresh_series(path, '005930', as_of=date(2026, 10, 23))
    assert [r['date'] for r in rows] == sorted(dates)


def test_fresh_opposite_flow_cannot_revive_persisted_frgn(tmp_path):
    from quant.apps import assembly
    path = ledger(tmp_path, ['2026-10-21', '2026-10-22'], [-100, -100])
    assert assembly._fresh_foreign_tags({'005930': ['FRGN', 'EVENT']}, path=path,
                                        as_of=date(2026, 10, 23)) == {'005930': ['EVENT']}
    path = ledger(tmp_path, ['2026-10-21', '2026-10-22'], [100, 100])
    assert assembly._fresh_foreign_tags({'005930': ['FRGN_EXIT', 'EVENT']}, path=path,
                                        as_of=date(2026, 10, 23)) == {'005930': ['EVENT']}
