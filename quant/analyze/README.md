# quant/analyze/

## 한 줄 정의

**분석 평면** — 수집된 원자재를 점수·등급·요약·HTML/JSON으로 가공한다. 틀리면
잃는 것: **선정이 나빠진다**(관심종목 편입 품질, 리포트 해석 품질) — 돈이 바로
나가지는 않지만, `watch_scorer`가 게이트를 잘못 열면 나쁜 종목이 유니버스에
들어와 결국 `quant/trade/`가 그 위에서 거래한다. LLM 호출·느린 배치가 허용되는
유일한 평면 중 하나다.

## 주요 파일 지도 (50여 개 파일 중 핵심만)

- **관심종목 채점**: `watch_scorer.py`(자동 스코어링 v2 — 프리퍼시티 게이트 +
  논지 태그별 증거점수, 유니버스 자동 편입의 심장부), `entry_grade.py`(진입 시점
  5단계 등급), `scalp_grade.py`(당일 단타 행동 등급), `symbol_score.py`(종목별
  엔진 점수).
- **리포트 조립/렌더**: `render.py`(Snapshot → HTML + engine.json, 최대 파일),
  `briefing.py`(오늘 확인할 것, 규칙 기반), `market_brief.py`(자체 리포트 →
  아침 브리핑 + 자동 편입 후보), `exec_summary.py`(Executive Summary AI 통합
  요약), `market_digest.py`/`kr_wrap.py`/`us_kr_bridge.py`(시황 요약/브리지),
  `charts.py`(서버사이드 SVG), `units.py`(단위 포맷터).
- **엔티티/관계**: `entities.py`(뉴스→상장종목 추출), `relations.py`(수혜주/공급사/
  경쟁사, LLM 후보+결정론 검증), `us_sector_map.py`(US 뉴스→GICS→KR 수혜주),
  `sector_view.py`/`themes.py`/`theme_search.py`(업종·테마 뷰).
- **뉴스 신호**: `news_direction.py`(방향 판정, 좁은 거부권), `news_momentum.py`
  (발행량 z-score), `news_cluster.py`(제목 shingle 클러스터), `bullish_markers.py`
  (호재 마커 사전+분류기).
- **수급**: `foreign_flow_v2.py`/`foreign_trend.py`(외국인 수급 라벨러).
- **학습형/AI**: `ml_scorer.py`(릿지 회귀 학습 선정자), `ai_trader.py`(신입사원
  AI 트레이더, 수습 단계), `param_proposer.py`(전략 파라미터 제안), `agent_interpret.py`
  (툴콜링 해석 에이전트).
- 기타: `opendays.py`(개장일 판정, 실데이터 기준), `carryover.py`/`holiday_synthesis.py`
  (휴장 기간 집계), `index_outlook.py`(지수 전망), `mentions.py`(종목 언급 원장),
  `scoring.py`(가감점→0~100 공용 변환), `intraday_score.py`(당일 단타 스코어러),
  `midterm_watch.py`(중기 관심 종목), `telegram_view.py`(공개채널 뷰), `volume_watch.py`
  (반복 거래대금 상위), `rank_history.py`(토스 랭킹 이력), `trending_score.py`
  (토스 랭킹 정량화), `foreign_trend.py`, `section_advice.py`(AI 포지션 해석),
  `baseline.py`, `delta.py`(전 세션 대비 변화), `indicators.py`(순수 지표 계산),
  `flow_scan.py`(장중 거래대금 발굴).
- `templates/` — Jinja2 템플릿(`report.html.j2`, `close_report.html.j2`).

## 핵심 불변식

- **`quant/analyze/`는 `quant/trade/`를 임포트하지 않는다** — collect와 함께
  가장 중요한 규칙(`tests/test_architecture.py`). 분석 결과는 유니버스 편입
  후보를 만들 뿐, 전략 진입 판단에 직접 관여하지 않는다.
- `quant/analyze/`는 `quant/adapters/`를 임포트하지 않는다(네트워크/DB I/O는
  하류인 `quant/report/collect/`나 `quant/apps/`가 주입).
- `theme_search.py` 등 일부 모듈은 **순수 모듈**임을 명시(네트워크·DB·파일 접근 없음).
- `news_direction.py`는 "감성 분석"이 아니라 "좁고 정밀한 거부권"으로 스스로를
  제한한다 — 과잉 해석을 막기 위한 설계 원칙.

## 데이터 흐름

**상류**: `quant/collect/*`가 만든 Snapshot/아티팩트, `quant/report/collect/*`가
주입하는 조립 데이터. **하류**: `quant/report/render/*`(HTML/engine.json 최종
렌더), `server/scripts/own_brief.sh`(engine.json을 읽어 `watch-score`로 유니버스
자동 편입 — 이 경로에는 LLM이 없다), `.claude/skills/daily-market-brief/`.

## 손대기 전에

- 관심종목 채점 규칙을 바꾸면 `.claude/skills/daily-market-brief/`도 함께 확인
  (CLAUDE.md 라우팅 테이블).
- `uv run pytest -q tests/test_architecture.py -v` — `analyze → trade` 임포트가
  생기지 않았는지.
- `uv run python -m quant.apps.report_cli --help` — 리포트 조립 스모크(가장 빠른
  회귀 신호).
