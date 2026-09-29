import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from quant.adapters.focus_capture import capture_focus_inputs


def test_capture_preserves_known_inputs_and_records_missing_ticks(tmp_path):
    (tmp_path/'config').mkdir()
    (tmp_path/'data').mkdir()
    (tmp_path/'config/settings.yaml').write_text('strategies:\n  vol_breakout_cat:\n    enabled: true\n    markets: [KR]\n    params: {k: 0.5}\nexecution: {slippage_bps: 2.5}\n')
    watch=tmp_path/'data/watchlist.yaml'
    watch.write_text('symbols: ["005930"]\nadded_at: 2026-09-30T08:00:00+09:00\n')
    now=datetime(2026,9,30,8,28,tzinfo=ZoneInfo('Asia/Seoul'))
    path=capture_focus_inputs(tmp_path,now=now)
    watch.write_text('symbols: ["000660"]\n')
    same=capture_focus_inputs(tmp_path,now=now)
    assert same==path
    record=json.loads(path.read_text())
    assert record['watchlist']['symbols']==['005930']
    assert record['watchlist']['added_at']=='2026-09-30T08:00:00+09:00'
    assert 'params: {k: 0.5}' in record['config_yaml']
    assert record['observed_at']==now.isoformat()
    assert record['tick_file']['exists'] is False
    assert record['strategy']['params']['k']==0.5
    assert 'not historical tag availability' in record['note']


def test_capture_rejects_naive_time(tmp_path):
    with pytest.raises(ValueError,match='aware'):
        capture_focus_inputs(tmp_path,now=datetime(2026,9,30))
