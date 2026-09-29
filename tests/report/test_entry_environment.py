from dataclasses import replace
from datetime import date, datetime
from copy import deepcopy

from quant.collect.contracts import Snapshot, SourceResult
from quant.analyze.entry_environment import build_entry_environment


NOW = datetime.fromisoformat('2026-09-29T08:00:00+09:00')


def snapshot():
    quotes = {s: {'close': 100, 'change_pct': 0.5, 'date': '2026-09-28'}
              for s in ('^GSPC', '^IXIC', 'KRW=X', 'CL=F', 'HG=F', 'GC=F', 'BTC-USD')}
    quotes['^VIX'] = {'close': 18, 'date': '2026-09-28'}
    return Snapshot(1, 'KR', date(2026, 9, 29), NOW, {
        'market': SourceResult('market', True, {'quotes': quotes}, None, 'https://example.com', NOW, 0),
    })


def test_missing_is_abstention_and_full_snapshot_is_bounded_not_probability():
    missing = build_entry_environment(replace(snapshot(), results={}))
    assert missing['score'] is None and missing['direction'] is None
    assert missing['coverage'] == 0
    result = build_entry_environment(snapshot())
    assert 0 <= result['score'] <= 100
    assert result['coverage'] == 95
    assert sum(f['weight'] for f in result['factors']) == 100
    assert '확률' in result['note'] and '미검증' in result['note']
    assert result['market_context']['btc']['status'] == 'provisional'
    assert 'BTC-USD' not in {f['id'] for f in result['factors']}


def test_core_stale_future_and_unknown_timestamps_cannot_score():
    for bad in ('2026-09-20', '2026-09-30', None):
        snap = snapshot()
        snap.results['market'].data['quotes']['^GSPC']['date'] = bad
        assert build_entry_environment(snap)['score'] is None
    snap = snapshot()
    bad_source = replace(snap.results['market'], fetched_at=datetime.fromisoformat('2026-09-29T08:01:00+09:00'))
    assert build_entry_environment(replace(snap, results={'market': bad_source}))['coverage'] == 0


def test_weekend_gap_is_allowed_but_core_absence_and_low_coverage_abstain():
    snap = snapshot()
    for q in snap.results['market'].data['quotes'].values():
        q['date'] = '2026-09-25'
    assert build_entry_environment(snap)['score'] is not None
    snap.results['market'].data['quotes'].pop('^VIX')
    assert build_entry_environment(snap)['score'] is None
    snap = snapshot()
    snap.results['market'].data['quotes'] = {k:v for k,v in snap.results['market'].data['quotes'].items() if k in ('^GSPC', '^IXIC', '^VIX')}
    assert build_entry_environment(snap)['coverage'] == 60


def test_keyword_window_dedup_unknown_dates_and_claims_not_bullish():
    msg = {'handle':'one', 'msg_id':'1', 'published':'2026-09-29T07:00:00+09:00', 'text':'전쟁 종료 가능성과 금리 완화', 'url':'https://t.me/one/1'}
    messages = [msg, dict(msg), dict(msg, handle='two',msg_id='2'), dict(msg,msg_id='3',published=None), dict(msg,msg_id='4',published='2026-09-29T09:00:00+09:00')]
    result = build_entry_environment(snapshot(), messages=messages, expected_channels=('one','two','missing'), news_items=[{'title':'전쟁 종료설', 'published':msg['published'], 'url':'https://example.com/news'}])
    assert result['keywords']['accepted_messages'] == 1
    assert result['keywords']['excluded_messages'] == 4
    assert 'missing' in result['keywords']['missing_channels']
    assert result['news']['items'][0]['classification'] == '주장·확인 필요'
    assert result['score'] == build_entry_environment(snapshot())['score']


def test_undated_sentiment_excluded_and_hard_risk_limits_observation_band():
    snap = snapshot()
    snap.results['sentiment'] = SourceResult('sentiment', True, {'cnn_fear_greed': {'value':80}}, None, '', NOW, 0)
    assert build_entry_environment(snap)['coverage'] == 95
    snap.results['sentiment'].data['cnn_fear_greed']['as_of'] = '2026-09-28'
    assert build_entry_environment(snap)['coverage'] == 100
    snap.results['market'].data['quotes']['^VIX']['close'] = 35
    result = build_entry_environment(snap)
    assert result['exposure_band'] == '0-25%'
    assert any(r['code'] == 'vix_stress' for r in result['risks'])


def test_fed_reference_uses_prior_change_and_sector_leaders_require_breadth():
    snap = snapshot()
    snap.results['sectors'] = SourceResult('sectors', True, {'sectors':[
        {'name':str(i), 'date':'2026-09-28','change_pct':1 if i<4 else -1} for i in range(11)]}, None, '', NOW, 0)
    fed = [{'series':'fed_target_upper','date':d,'value':v,'fetched_at':NOW.isoformat()} for d,v in [('2026-09-01',5),('2026-09-15',4.75),('2026-09-28',4.75),('2026-09-30',1)]]
    result = build_entry_environment(snap, macro_rows=fed)
    assert result['market_context']['fed']['fed_target_upper']['last_change_bp'] == -25
    assert result['market_context']['sectors']['leaders'] == []


def test_today_daily_quotes_are_provisional_and_sector_days_must_match():
    snap = snapshot()
    snap.results['market'].data['quotes']['^GSPC']['date'] = '2026-09-29'
    assert build_entry_environment(snap)['score'] is None
    snap = snapshot()
    snap.results['sectors'] = SourceResult('sectors', True, {'sectors':[
        {'name':str(i), 'date':'2026-09-28' if i<5 else '2026-09-25', 'change_pct':1}
        for i in range(11)]}, None, '', NOW, 0)
    assert build_entry_environment(snap)['market_context']['sectors']['leaders'] == []


def test_nonfinite_values_excluded_and_inputs_are_not_mutated():
    snap = snapshot()
    snap.results['market'].data['quotes']['HG=F']['change_pct'] = float('inf')
    before = deepcopy(snap)
    result = build_entry_environment(snap)
    assert result['coverage'] == 90
    assert snap == before
    valid = [f for f in result['factors'] if f['score'] is not None]
    assert result['score'] == round(sum(f['weight']*f['score'] for f in valid)/90, 1)


def test_fed_future_availability_not_backfilled_and_empty_keywords_not_neutral():
    fed = [{'series':'fed_target_upper', 'date':'2026-09-28', 'value':4.75,
            'fetched_at':'2026-09-29T09:00:00+09:00'}]
    result = build_entry_environment(snapshot(), macro_rows=fed)
    assert result['market_context']['fed']['fed_target_upper']['status'] == 'missing'
    assert result['keywords']['status'] == 'missing'


def test_price_context_and_future_cnn_observation_are_explicit():
    snap = snapshot()
    snap.results['sentiment'] = SourceResult('sentiment', True, {'cnn_fear_greed': {
        'value': 80, 'as_of': '2026-09-28', 'observed_at': '2026-09-29T09:00:00+09:00',
    }}, None, '', NOW, 0)
    result = build_entry_environment(snap)
    assert result['coverage'] == 95
    assert result['market_context']['quotes']['sentiment']['status'] == 'future'
    assert result['market_context']['quotes']['^GSPC']['close'] == 100
    assert result['market_context']['quotes']['^GSPC']['change_pct'] == 0.5
    assert result['market_context']['btc']['close'] == 100


def test_rss_news_and_curated_sector_keywords_keep_message_provenance():
    msg = {'handle':'one','msg_id':3,'published':'Mon, 28 Sep 2026 22:00:00 GMT',
           'text':'반도체 AI 전력 2차전지 바이오 방산'}
    result = build_entry_environment(snapshot(), messages=[msg],
        news_items=[{'title':'확인 필요한 지정학 보도', 'published':msg['published'], 'link':'https://example.com/rss'}])
    assert len(result['news']['items']) == 1
    topics = {r['keyword'] for r in result['keywords']['topics']}
    assert {'반도체','AI','전력','2차전지','바이오','방산'} <= topics
    record = result['keywords']['messages'][0]
    assert record['handle'] == 'one' and record['msg_id'] == '3'
    assert len(record['text_sha256']) == 64
    assert record['published'] and record['url'] == 'https://t.me/one/3'
    assert '사전' in result['keywords']['note']


def test_cross_source_core_conflict_abstains_and_retains_evidence():
    snap = snapshot()
    warning = '^VIX: yfinance 16.07 vs fred 14.21 (13.1% 괴리)'
    snap.results['market'].data['crosscheck'] = {'checked':['^VIX'], 'warnings':[warning]}
    result = build_entry_environment(snap)
    assert result['score'] is None
    assert result['market_context']['quotes']['^VIX']['status'] == 'source_conflict'
    assert warning in [r['label'] for r in result['risks']]


def test_news_prioritizes_relevant_latest_and_labels_bearish_marker():
    news = [{'title':f'일반 기사 {i}', 'published':'2026-09-29T07:30:00+09:00'} for i in range(15)]
    news.extend([{'title':'반도체 기업 실적 부진', 'published':'2026-09-29T07:00:00+09:00'},
                 {'title':'연준 금리 발언', 'published':'2026-09-29T07:20:00+09:00'}])
    result = build_entry_environment(snapshot(), news_items=news)
    assert result['news']['items'][0]['title'] == '연준 금리 발언'
    assert result['news']['items'][1]['direction_tag'] == '악재 표지'


def test_stock_bad_news_prioritized_over_unrelated_crime():
    news = [{'title': '떡집 직원 횡령 사건', 'published': NOW.isoformat()},
            {'title': '삼성전자 주식 실적 개선', 'published': NOW.isoformat()}]
    result = build_entry_environment(snapshot(), news_items=news)
    assert result['news']['items'][0]['title'] == news[1]['title']


def test_kr_midnight_cannot_score_still_open_us_session():
    early = datetime.fromisoformat('2026-09-29T01:00:00+09:00')
    snap = snapshot()
    snap = replace(snap, generated_at=early,
                   results={key: replace(value, fetched_at=early) for key,value in snap.results.items()})
    assert build_entry_environment(snap)['score'] is None
