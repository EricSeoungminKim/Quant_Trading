"""Frozen-input KR entry environment heuristic; no I/O or trading decisions.

Weights and transforms are predeclared observations, not fitted probabilities.
Unknown/stale inputs are absent evidence; news frequency never earns points.
"""
from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from quant.analyze.bullish_markers import classify_titles
from quant.analyze.market_digest import STOCK_KEYWORDS
from quant.collect.sources.feeds import parse_published

KST = ZoneInfo('Asia/Seoul')
VERSION = 'kr_entry_environment_v1'
TOPICS = ('금리', '관세', '지정학', '전쟁', '제재', '유동성', '환율', '인플레이션', '연준', '원유',
          '반도체', 'AI', '전력', '2차전지', '바이오', '방산', '조선', '로봇', '자동차')
# id, label, weight, field, linear score: intercept + slope * value
FACTORS = (
    ('^GSPC', 'S&P500 등락', 20, 'change_pct', 50, 10),
    ('^IXIC', 'NASDAQ 등락', 15, 'change_pct', 50, 10),
    ('^VIX', 'VIX 수준', 25, 'close', 110, -3),
    ('KRW=X', '원달러 등락', 15, 'change_pct', 50, -15),
    ('CL=F', 'WTI 변동 충격', 10, 'change_pct', 70, -10),
    ('HG=F', '구리 등락', 5, 'change_pct', 50, 5),
    ('GC=F', '금 등락', 5, 'change_pct', 50, -5),
    ('sentiment', '날짜 확인된 CNN 심리', 5, 'value', 0, 1),
)


def _number(value):
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) and not isinstance(value, bool) else None


def _timestamp(value):
    dt = value if isinstance(value, datetime) else parse_published(str(value)) if value else None
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else None


def _dated_status(value, now, max_days=4):
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return 'unknown_date'
    age = (now.astimezone(KST).date() - d).days
    if age < 0:
        return 'future'
    return 'stale' if age > max_days else 'ok'


def _source(snap, name, inputs):
    source = snap.results.get(name)
    fetched = _timestamp(source.fetched_at) if source else None
    status = 'missing'
    if source and source.ok and isinstance(source.data, dict):
        status = ('unknown_timestamp' if fetched is None else 'future' if fetched > snap.generated_at
                  else 'stale' if snap.generated_at - fetched > timedelta(days=1) else 'ok')
    inputs[name] = {'status': status, 'fetched_at': fetched.isoformat() if fetched else None}
    return source.data if status == 'ok' else {}


def _keywords(messages, now, expected_channels):
    since = now - timedelta(hours=72)
    ids, texts, channels, topics = set(), set(), set(), {}
    provenance = []
    accepted = excluded = 0
    for row in messages:
        dt = _timestamp(row.get('published'))
        handle = str(row.get('handle') or '')
        mid = str(row.get('msg_id') or '')
        content = re.sub(r'\s+', ' ', str(row.get('text') or '')).strip()
        key = (handle, mid)
        if dt is None or not since <= dt <= now or not content or not handle or not mid:
            excluded += 1
            continue
        channels.add(handle)
        if key in ids or content in texts:
            excluded += 1
            continue
        ids.add(key)
        texts.add(content)
        accepted += 1
        url = row.get('url') or f'https://t.me/{handle.lstrip("@")}/{mid}'
        provenance.append({'handle': handle, 'msg_id': mid, 'published': dt.isoformat(),
                           'text_sha256': hashlib.sha256(content.encode()).hexdigest(), 'url': url})
        for word in TOPICS:
            matches = bool(re.search(r'(?<![A-Za-z])AI(?![A-Za-z])', content, re.IGNORECASE)) if word == 'AI' else word in content
            if not matches:
                continue
            topic = topics.setdefault(word, {'keyword': word, 'count': 0, 'channels': set(), 'urls': set()})
            topic['count'] += 1
            topic['channels'].add(handle)
            topic['urls'].add(url)
    return {
        'status': 'observed' if accepted else 'missing',
        'window_start': since.isoformat(), 'window_end': now.isoformat(),
        'accepted_messages': accepted, 'excluded_messages': excluded,
        'messages': provenance,
        'missing_channels': sorted(set(expected_channels) - channels),
        'topics': [{**v, 'channels': sorted(v['channels']), 'urls': sorted(v['urls'])}
                   for v in sorted(topics.values(), key=lambda v: (-v['count'], v['keyword']))],
        'note': '사전 정의한 매크로·섹터 어휘의 단순 언급 빈도이며 방향·사실 확인·호재 점수가 아닙니다. 결측은 위험 부재가 아닙니다.',
    }


def _fed_context(rows, now):
    out = {}
    for name in ('fed_target_lower', 'fed_target_upper'):
        valid = {}
        for row in rows:
            if row.get('series') != name or _dated_status(row.get('date'), now, 3650) != 'ok':
                continue
            available = _timestamp(row.get('fetched_at') or row.get('recorded_at'))
            value = _number(row.get('value'))
            if value is not None and available is not None and available <= now:
                valid[row['date']] = value
        points = sorted(valid.items())
        if not points:
            out[name] = {'status': 'missing', 'value': None, 'last_change_bp': None}
            continue
        day, value = points[-1]
        change = next(((d, round((v - previous) * 100, 4))
                       for (pd, previous), (d, v) in reversed(list(zip(points, points[1:])))
                       if v != previous), None)
        out[name] = {'status': _dated_status(day, now, 8), 'date': day, 'value': value,
                     'last_change_bp': change[1] if change else None,
                     'last_change_date': change[0] if change else None}
    return out


def build_entry_environment(snap, *, messages=(), macro_rows=(), news_items=(), expected_channels=()):
    """Build a transparent, abstaining observation from one frozen snapshot."""
    now = _timestamp(snap.generated_at)
    if now is None:
        raise ValueError('Snapshot generated_at must be timezone-aware')
    inputs = {}
    market = _source(snap, 'market', inputs)
    quotes = market.get('quotes') or {}
    crosscheck = market.get('crosscheck') or {}
    conflicts = {str(w).split(':', 1)[0]: str(w) for w in crosscheck.get('warnings', [])}
    sentiment = (_source(snap, 'sentiment', inputs).get('cnn_fear_greed') or {})
    factors, context_quotes, risks = [], {}, []
    for sid, label, weight, field, intercept, slope in FACTORS:
        entry = sentiment if sid == 'sentiment' else quotes.get(sid) or {}
        day = entry.get('as_of') if sid == 'sentiment' else entry.get('date')
        status = _dated_status(day, now, 4)
        value = _number(entry.get(field))
        if not entry:
            status = 'missing'
        elif value is None:
            status = 'missing_value'
        elif entry.get('stale'):
            status = 'stale'
        elif sid == 'sentiment' and not 0 <= value <= 100 or field == 'close' and value <= 0:
            status = 'invalid_value'
        elif sid != 'sentiment' and day == now.astimezone(KST).date().isoformat():
            status = 'provisional'
        if status == 'ok' and sid in ('^GSPC', '^IXIC', '^VIX'):
            session_close = datetime.fromisoformat(day).replace(
                hour=16, minute=15, tzinfo=ZoneInfo('America/New_York'))
            if now < session_close:
                status = 'provisional'
        if sid == 'sentiment' and entry.get('observed_at') is not None:
            observed = _timestamp(entry['observed_at'])
            if observed is None:
                status = 'unknown_timestamp'
            elif observed > now:
                status = 'future'
        if sid in conflicts:
            status = 'source_conflict'
            risks.append({'code': 'source_conflict', 'label': conflicts[sid], 'severity': 'unknown'})
        score = None
        if status == 'ok':
            score = round(max(0, min(100, intercept + slope * (abs(value) if sid == 'CL=F' else value))), 2)
        factors.append({'id': sid, 'label': label, 'weight': weight, 'value': value,
                        'date': day, 'status': status, 'score': score,
                        'reason': (f'clip({intercept} + {slope} × ' + ('|값|' if sid == 'CL=F' else '값') + ', 0, 100)') if score is not None else status})
        context_quotes[sid] = {'date': day, 'value': value, 'status': status, 'label': label,
                               'close': _number(entry.get('close')), 'change_pct': _number(entry.get('change_pct'))}
        if status == 'ok':
            for condition, code, risk_label in (
                (sid == '^VIX' and value >= 30, 'vix_stress', 'VIX 30 이상'),
                (sid == 'CL=F' and abs(value) >= 5, 'oil_shock', 'WTI 일간 변동 절댓값 5% 이상'),
                (sid == 'KRW=X' and value >= 2, 'fx_stress', '원달러 일간 상승 2% 이상'),
            ):
                if condition:
                    risks.append({'code': code, 'label': risk_label, 'severity': 'high'})
    coverage = sum(f['weight'] for f in factors if f['score'] is not None)
    core = all(context_quotes[s]['status'] == 'ok' for s in ('^GSPC', '^IXIC', '^VIX'))
    score = round(sum(f['weight'] * f['score'] for f in factors if f['score'] is not None) / coverage, 1) if core and coverage >= 60 and snap.market == 'KR' else None
    direction = None if score is None else 'up' if score >= 60 else 'down' if score <= 40 else 'flat'
    label = {None: '판단 보류', 'up': '공격 관찰', 'flat': '중립 관찰', 'down': '방어 관찰'}[direction]
    band = {None: '판단 보류', 'up': '50-75%', 'flat': '25-50%', 'down': '0-25%'}[direction]
    if risks and score is not None:
        band = '0-25%'
    if score is None:
        risks.append({'code': 'insufficient_inputs', 'label': '핵심 자료 결측 또는 가용 비중 부족 — 판단 보류', 'severity': 'unknown'})

    btc = quotes.get('BTC-USD') or {}
    btc_status = _dated_status(btc.get('date'), now)
    if btc_status == 'ok':
        btc_end = datetime.combine(date.fromisoformat(btc['date']) + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
        btc_status = 'provisional' if btc_end > now else 'reference_only'
    sector_data = _source(snap, 'sectors', inputs)
    dated_sectors = [s for s in sector_data.get('sectors', [])
                     if _dated_status(s.get('date') or sector_data.get('date'), now) == 'ok'
                     and (s.get('date') or sector_data.get('date')) < now.astimezone(KST).date().isoformat()
                     and _number(s.get('change_pct')) is not None]
    latest_sector_day = max((s.get('date') or sector_data.get('date') for s in dated_sectors), default=None)
    sectors = [s for s in dated_sectors if (s.get('date') or sector_data.get('date')) == latest_sector_day]
    breadth = round(sum(float(s['change_pct']) > 0 for s in sectors) / len(sectors) * 100, 1) if len(sectors) >= 8 else None
    leaders = sorted([{'name': s.get('name'), 'change_pct': float(s['change_pct']), 'date': s.get('date') or sector_data.get('date')}
                      for s in sectors if float(s['change_pct']) > 0], key=lambda s: -s['change_pct'])[:3] if breadth is not None and breadth >= 50 else []
    news = []
    seen_news = set()
    for row in news_items:
        dt = _timestamp(row.get('published'))
        title = str(row.get('title') or '')
        if dt is None or not now - timedelta(hours=72) <= dt <= now or not title or title in seen_news:
            continue
        seen_news.add(title)
        markers = classify_titles([title])
        news.append({'title': title, 'url': row.get('url') or row.get('link'), 'published': dt.isoformat(),
                     'classification': '주장·확인 필요',
                     'direction_tag': '악재 표지' if markers['bearish'] else '호재 표지' if markers['bullish_types'] else '방향 미확정',
                     'matched_types': markers['bullish_types'],
                     'relevant': any(word.lower() in title.lower() for word in (*TOPICS, *STOCK_KEYWORDS))})
    news.sort(key=lambda item: (item['relevant'], item['published']), reverse=True)
    return {
        'schema': 1, 'version': VERSION, 'market': snap.market,
        'session_date': snap.session_date.isoformat(), 'generated_at': now.isoformat(),
        'score': score, 'label': label, 'direction': direction, 'exposure_band': band,
        'note': '확률이 아닌 사전 고정 환경점수입니다. 가중치·산식은 미검증 초기 가정입니다. 비중 구간은 관찰 기준이며 주문·설정에 반영하지 않습니다.',
        'context_note': 'Fed 금리·업종·뉴스·키워드는 맥락으로 함께 제시합니다. 발표 주기 차이, 시장요인과의 중복, 방향 검증 부족 때문에 초기 점수에는 가산하지 않습니다. 날짜 확인된 CNN 심리만 5% 가중치입니다.',
        'coverage': coverage, 'factors': factors, 'inputs': inputs, 'risks': risks,
        'market_context': {'quotes': context_quotes, 'crosscheck': crosscheck,
                           'btc': {'status': btc_status, 'date': btc.get('date'), 'close': _number(btc.get('close')), 'change_pct': _number(btc.get('change_pct')), 'score_included': False},
                           'fed': _fed_context(macro_rows, now),
                           'sectors': {'status': 'observed' if breadth is not None else 'insufficient_dated_sectors', 'positive_breadth_pct': breadth, 'n_dated': len(sectors), 'leaders': leaders}},
        'keywords': _keywords(messages, now, expected_channels),
        'news': {'status': 'observed' if news else 'missing', 'items': news[:12]},
    }
