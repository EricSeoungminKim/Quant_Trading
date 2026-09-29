"""Immutable pre-open forecast storage and exact-session market outcome I/O."""
from __future__ import annotations

import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta
from pathlib import Path

from quant.adapters.http import client
from quant.core.report_clock import KST


class ForecastStore:
    def __init__(self, root: Path):
        self.path = root / 'data' / 'entry_environment'

    def forecasts(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.path / 'forecasts').glob('*.json'))]

    def outcomes(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted((self.path / 'outcomes').glob('*.json'))]

    def forecast_for(self, day: date) -> dict | None:
        path = self.path / 'forecasts' / f'{day.isoformat()}.json'
        return json.loads(path.read_text()) if path.exists() else None

    def freeze(self, forecast: dict, *, now: datetime) -> dict | None:
        if now.tzinfo is None:
            raise ValueError('aware timestamp required')
        now = now.astimezone(KST)
        day = date.fromisoformat(forecast['session_date'])
        dest = self.path / 'forecasts' / f'{day}.json'
        if day <= now.date() and dest.exists():
            return json.loads(dest.read_text())
        generated = datetime.fromisoformat(forecast['generated_at'])
        if (day != now.date() or now.time() >= time(9) or generated.tzinfo is None
                or generated > now or generated.astimezone(KST).date() != day):
            return None
        record = {**forecast, 'frozen_at': now.isoformat()}
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False)
        record['forecast_id'] = hashlib.sha256(raw.encode()).hexdigest()
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic first publication: a crash cannot leave a half-written forecast.
        temp = dest.with_name(f'.{dest.name}.{os.getpid()}.tmp')
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
        try:
            os.link(temp, dest)
        except FileExistsError:
            pass
        finally:
            temp.unlink(missing_ok=True)
        return json.loads(dest.read_text())

    def save_outcome(self, result: dict) -> None:
        dest = self.path / 'outcomes' / f"{result['session_date']}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix('.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        temp.replace(dest)


def parse_index_chart(raw: str, label: str, day: date) -> dict | None:
    chart = ET.fromstring(raw).find('chartdata')
    if chart is None or chart.get('symbol') != label:
        return None
    for item in chart.findall('item'):
        fields = (item.get('data') or '').split('|')
        if len(fields) >= 5 and fields[0] == day.strftime('%Y%m%d'):
            return {'date': day.isoformat(), 'source': f'naver:{label}',
                    **dict(zip(('open', 'high', 'low', 'close'), map(float, fields[1:5])))}
    return None


def fetch_session_bars(day: date) -> dict:
    """Exact local-session index OHLC. No adjacent-session fallback or fabricated bar."""
    import yfinance as yf

    output = {}
    for label, symbol in (('KOSPI', '^KS11'), ('KOSDAQ', '^KQ11')):
        try:
            with client(timeout=20) as session:
                response = session.get('https://fchart.stock.naver.com/sise.nhn', params={
                    'symbol': label, 'timeframe': 'day', 'count': '500', 'requestType': '0'})
                response.raise_for_status()
            bar = parse_index_chart(response.text, label, day)
            if bar and all(bar[k] > 0 for k in ('open', 'high', 'low', 'close')):
                output[label] = bar
                continue
        except Exception:
            pass
        try:
            frame = yf.Ticker(symbol).history(start=day.isoformat(),
                end=(day + timedelta(days=1)).isoformat(), auto_adjust=False)
            for stamp, row in frame.iterrows():
                if stamp.date() == day:
                    output[label] = {'date': day.isoformat(), 'source': f'yahoo:{symbol}',
                        **{k: float(row[k.title()]) for k in ('open', 'high', 'low', 'close')}}
        except Exception:
            # Caller records missing and retries; exceptions can include provider URLs.
            continue
    return output


def read_evidence_rows(path: Path) -> tuple[list[dict], int]:
    """Concurrent append can expose an incomplete last line; retain valid evidence."""
    if not path.exists():
        return [], 0
    rows, invalid = [], 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError('object required')
            rows.append(row)
        except ValueError:
            invalid += 1
    return rows, invalid
