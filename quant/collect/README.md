# quant/collect/

## 한 줄 정의

**수집 평면** — 외부 세계(웹/API/RSS)에서 원자재 데이터를 긁어와 아티팩트로
저장한다. 틀리면 잃는 것: **데이터가 빈다**. 스크래핑 실패·재시도·LLM 호출 모두
허용되지만(리포트 발행 시각까지만 맞으면 된다), 여기서 만든 데이터가 곧바로
주문으로 이어지면 안 된다 — 그 경로는 코드 수준에서 끊겨 있다(아래 참고).

## 주요 파일 지도

- `collector.py` — 뉴스 누적 수집기(리포트 빌드에서 뉴스 수집을 분리).
- `contracts.py` — 수집 계층의 데이터 계약(타입 정의).
- `listed_companies.py` — 상장 종목 목록 원본 캐시(KIND 상장사, 나스닥 SymDir, S&P500).
- `snapshot.py` — 여러 소스를 병렬 실행해 하나의 Snapshot으로 합친다.
- `spread.py` — 호가창 스프레드 **실측** 수집(스캘핑 비용 가정을 숫자로 검증).
- `quotes/` — 과거 데이터 백필 레이어(벤더 중립 `source.py: CandleSource` +
  `backfill.py`). 구현체: `toss_source.py`, `alpaca_source.py`, `yf_source.py`.
- `sources/` — 개별 소스 어댑터 20여 개. 시장 데이터(`market.py`, `toss.py`),
  뉴스/공시(`feeds.py`, `dart.py`, `dart_financials.py`, `article_body.py`),
  네이버 3종(`naver_flow.py`/`naver_quant.py`/`naver_sector.py`/`naver_theme.py`/
  `naver_research.py`/`stock_detail.py`), 매크로(`fred.py`, `technical.py`,
  `sentiment.py`), 수급(`kr_flow.py`), 브리핑(`youtube_brief.py`, `blog_brief.py`,
  `telegram_channels.py`), 캘린더(`calendar.py`), 시간외(`after_hours.py`),
  랭킹(`naver_quant.py`), 시드 뉴스(`seeded_news.py`).

## 핵심 불변식

- **`quant/collect/`는 `quant/trade/`를 임포트하지 않는다** — 저장소 전체에서 가장
  중요한 규칙(`tests/test_architecture.py`의 `FORBIDDEN`, 상단 docstring 인용:
  "스크래핑한 뉴스가 주문으로 이어지는 경로를 코드 수준에서 끊는다"). 뉴스는
  *유니버스만* 편집하고, 진입 판단은 전략이 가격으로 한다.
- `quant/collect/`는 `quant/analyze/`·`quant/apps/`도 임포트하지 않는다 — 아티팩트를
  쓰고 끝낸다(자기 결과를 스스로 해석하지 않는다).
- 어댑터 성격이 강한 코드라 벤더 예외는 소스 파일 안에서 흡수하고, 실패해도 리포트
  발행 자체는 멈추지 않는 것이 원칙(가용한 것만 채워 발행).

## 데이터 흐름

**상류**: 외부 API/웹(토스, 키움 REST, DART, FRED, 네이버금융, RSS, 유튜브 등).
**하류**: `quant/report/collect/`와 `quant/analyze/*`가 이 평면이 만든 원자재를
읽어 리포트/스코어로 가공한다. `quant/trade/`로는 절대 직접 흐르지 않는다 —
유니버스 편입은 `own_brief.sh`(watch-score)를 거쳐야 한다.

## 손대기 전에

- `uv run pytest -q tests/test_architecture.py -v` — `quant.collect → quant.trade`
  임포트가 생기지 않았는지 확인(생기면 즉시 실패).
- 새 소스를 추가하면 `sources/__init__.py`의 레지스트리에 등록하고, 실패 시
  빈 값을 반환하도록(예외를 상위로 전파하지 않도록) 확인.
- `uv run python -m quant.apps.report_cli --help` — 리포트 조립 스모크.
