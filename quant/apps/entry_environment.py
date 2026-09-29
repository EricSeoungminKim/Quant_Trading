"""Report-only KR entry briefing assembly and scheduled outcome refresh."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

from quant.adapters.entry_environment import ForecastStore, fetch_session_bars, read_evidence_rows
from quant.control.entry_environment_feedback import compare_context, evaluate, summarize
from quant.core.report_clock import KST


def refresh_outcomes(root: Path, *, now: datetime | None = None, fetch=fetch_session_bars) -> dict:
    now = now or datetime.now(KST)
    store = ForecastStore(root)
    previous = {r['session_date']: r for r in store.outcomes()}
    for forecast in store.forecasts():
        day = date.fromisoformat(forecast['session_date'])
        if datetime.combine(day, datetime.min.time(), KST).replace(hour=15, minute=40) > now:
            continue
        old = previous.get(day.isoformat(), {})
        if len(old.get('benchmarks', {})) == 2:
            continue
        bars = fetch(day)
        # Preserve first measured OHLC and regenerate explanations for both indices.
        bars = {**bars, **{k: v['ohlc'] for k, v in old.get('benchmarks', {}).items()}}
        result = evaluate(forecast, bars, evaluated_at=now.isoformat())
        result['missing'] = [k for k in ('KOSPI', 'KOSDAQ') if k not in result['benchmarks']]
        store.save_outcome(result)
    return summarize(store.outcomes())


def feedback_view(root: Path, session: date) -> dict:
    store = ForecastStore(root)
    forecasts = [r for r in store.forecasts() if r['session_date'] < session.isoformat()]
    if not forecasts:
        return {'summary': '이 점수의 이전 세션 평가 없음 — 첫 예측부터 관찰을 시작합니다.', 'stats': summarize([])}
    prior = forecasts[-1]
    outcome = next((r for r in store.outcomes() if r['session_date'] == prior['session_date']), None)
    if not outcome or not outcome.get('benchmarks'):
        summary = f"{prior['session_date']} 예측 기록 있음 · 시장 결과 수집 대기(결측을 실패로 채점하지 않음)"
    else:
        parts = []
        for key, value in outcome['benchmarks'].items():
            hit = value['direction_hit']
            verdict = '판단 보류' if hit is None else '방향 일치' if hit else '방향 불일치'
            error = value.get('direction_error')
            difference = f" · 방향 오차 {error}/2" if error is not None else ""
            parts.append(f"{key} 시가→종가 {value['return_pct']:+.2f}%, 장중 하락 {value['downside_pct']:+.2f}% · {verdict}{difference}")
        summary = prior['session_date'] + ' · ' + ' / '.join(parts)
    return {'session_date': prior['session_date'], 'summary': summary,
            'outcome': outcome, 'prior_factors': prior.get('factors', []),
            'stats': summarize([r for r in store.outcomes() if r['session_date'] < session.isoformat()])}


def build_view(snap, root: Path, *, news_items=()) -> dict | None:
    if snap.market != 'KR':
        return None
    from quant.analyze.entry_environment import build_entry_environment
    from quant.collect.sources.telegram_channels import CHANNELS

    messages, bad_messages = read_evidence_rows(root / 'data/ledger/telegram_msgs.jsonl')
    rows, bad_macro = read_evidence_rows(root / 'data/ledger/macro_rates.jsonl')
    if not news_items:
        source = snap.results.get('news')
        feeds = (source.data or {}).get('feeds', {}) if source and source.ok else {}
        news_items = [item for items in feeds.values() for item in items]
    view = build_entry_environment(snap, messages=messages, macro_rows=rows,
                                  news_items=news_items, expected_channels=[c['handle'] for c in CHANNELS])
    if bad_messages or bad_macro:
        view['risks'].append({'code': 'ledger_parse', 'label': f'원장 읽기 제외: 채널 {bad_messages}행 · 매크로 {bad_macro}행', 'severity': 'unknown'})
    view['feedback'] = feedback_view(root, snap.session_date)
    feedback = view['feedback']
    feedback.update(compare_context({'factors': feedback.get('prior_factors', [])}, view, feedback.get('outcome')))
    view['forecast_status'] = '장전 발행 시 동결 예정'
    return view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    stats = refresh_outcomes(args.root)
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
