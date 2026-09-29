"""Prospective strategy-input archive; never reconstruct historical knowledge."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from pathlib import Path

import yaml

from quant.core.report_clock import KST


def _json_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f'Unsupported JSON value: {type(value).__name__}')


def capture_focus_inputs(root: Path, *, now: datetime) -> Path:
    if now.tzinfo is None:
        raise ValueError('aware timestamp required')
    now = now.astimezone(KST)
    dest = root / 'data/research/vol_breakout_cat/inputs' / now.strftime('%Y-%m-%d/%H%M%S%f.json')
    if dest.exists():
        return dest
    cfg_raw = (root / 'config/settings.yaml').read_bytes()
    cfg = yaml.safe_load(cfg_raw)
    watch_path = root / 'data/watchlist.yaml'
    watch_raw = watch_path.read_bytes() if watch_path.exists() else None
    ticks = root / 'data/ticks/KR' / f'{now.date()}.jsonl'
    record = {
        'schema': 1, 'observed_at': now.isoformat(), 'market': 'KR',
        'strategy_id': 'vol_breakout_cat',
        'strategy': cfg['strategies']['vol_breakout_cat'],
        'execution': cfg.get('execution', {}),
        'risk': cfg.get('risk', {}),
        'config_sha256': hashlib.sha256(cfg_raw).hexdigest(),
        'config_yaml': cfg_raw.decode(),
        'code_sha256': {str(path): hashlib.sha256((root / path).read_bytes()).hexdigest()
                        for path in (Path('quant/trade/strategy/vol_breakout.py'),
                                     Path('quant/trade/strategy/__init__.py'),
                                     Path('quant/trade/risk/manager.py'),
                                     Path('quant/adapters/execution/paper.py'))
                        if (root / path).is_file()},
        'watchlist': yaml.safe_load(watch_raw) if watch_raw is not None else None,
        'watchlist_yaml': watch_raw.decode() if watch_raw is not None else None,
        'watchlist_sha256': hashlib.sha256(watch_raw).hexdigest() if watch_raw is not None else None,
        'tick_file': {'path': str(ticks.relative_to(root)), 'exists': ticks.exists(),
                      'bytes_observed': ticks.stat().st_size if ticks.exists() else 0},
        'note': 'Base settings file snapshot; auto_params and environment overlays are not included. '
                'File snapshot at observed_at, not historical tag availability or proof of the engine cached universe. '
                'Tick file is a growing source reference, not a full candle coverage guarantee.',
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False, default=_json_date))
    try:
        os.link(temp, dest)
    except FileExistsError:
        pass
    finally:
        temp.unlink(missing_ok=True)
    return dest
