# 과거 데이터 가용성 실측 (2026-07-28)

이 문서는 "한국 브로커(Toss/Kiwoom) API만으로 TQQQ/SQQQ 15분봉 백테스트용 과거
데이터셋을 자체 구축할 수 있는가"에 대한 **실측** 결과다. 아래 수치는 전부 실제
API 호출(`.env.local`의 실 자격증명, `MODE=paper`, 시세 조회 전용 — 주문/계좌
변경 API는 호출하지 않음)로 얻었다. 실패도 그대로 기록한다 — 이 문서는 유료
벤더 필요 여부를 결정하는 입력값이다.

## 헤드라인

| 항목                           | 결과                                                                  |
| ------------------------------ | --------------------------------------------------------------------- |
| Toss 일봉(daily) 과거 데이터   | **2010-02-11까지** (TQQQ/SQQQ 상장 시점과 일치) — 사실상 전체 역사    |
| Toss 1분봉(minute) 과거 데이터 | **약 4거래일**(2026-07-24 ~ 현재)만 제공 — API가 스스로 페이징을 멈춤 |
| Toss IP allowlist 403 위험     | **발생하지 않음** — 이 Mac에서 정상 호출됨                            |
| Kiwoom mock 해외주식 차트 지원 | **검증 불가** — 토큰 발급 자체가 appkey 실전/모의 불일치로 거부됨     |

**결론: Toss 단독으로는 15분봉 백테스트에 필요한 깊이의 과거 데이터를 확보할 수
없다.** 일봉은 충분(2010~), 분봉은 4일 남짓. 아래 "권고" 절 참고.

---

## 1. Toss — 일봉(daily) 페이징 깊이

### 재현 명령

```bash
uv run python - <<'PY'
import os
from dotenv import load_dotenv
load_dotenv(".env"); load_dotenv(".env.local", override=True)
from quant.adapters.brokers.toss.client import TossClient

client = TossClient(
    client_id=os.environ["TOSS_CLIENT_ID"],
    client_secret=os.environ["TOSS_CLIENT_SECRET"],
    account_seq=os.environ.get("TOSS_ACCOUNT_SEQ", ""),
    mode="paper",
)
before = None
calls, bars = 0, 0
while True:
    params = {"symbol": "TQQQ", "interval": "day", "count": 200}
    if before:
        params["before"] = before
    result = client._request("GET", "/api/v1/candles", "MARKET_DATA_CHART", params=params)
    page = result["candles"]
    calls += 1
    if not page:
        break
    bars += len(page)
    before = result.get("nextBefore")
    if not before:
        break
print(f"calls={calls} bars={bars} earliest={page[-1] if 'page' in dir() else None}")
PY
```

(위와 동일한 로직을 `interval="minute"`으로 바꾸면 2절의 분봉 재현이 된다.)

### 결과

| 심볼 | interval | API 호출 수 | 총 봉 수 | 최신 시각  | 가장 과거 시각             | 소요 | 종료 사유                               |
| ---- | -------- | ----------- | -------- | ---------- | -------------------------- | ---- | --------------------------------------- |
| TQQQ | day      | 21          | 4138     | 2026-07-27 | **2010-02-11T14:00+09:00** | 4.7s | `nextBefore` 없음(API가 "더 없음" 응답) |
| SQQQ | day      | 21          | 4138     | 2026-07-27 | **2010-02-11T14:00+09:00** | 5.5s | 동일                                    |

2010-02-11은 TQQQ/SQQQ(ProShares UltraPro QQQ/UltraPro Short QQQ) 실제 상장일과
일치한다 — Toss가 상장 이래 전체 일봉 히스토리를 제공한다는 뜻이다. 페이징은
`before`/`nextBefore` 커서로 200개씩, 자연 종료(예산 소진이 아니라 API 스스로
더 이상 페이지가 없다고 응답)했다.

## 2. Toss — 1분봉(minute) 페이징 깊이

동일한 페이징 로직을 `interval="minute"`(API 파라미터로는 `1m`)로 실행:

| 심볼 | API 호출 수 | 총 봉 수 | 최신 시각              | 가장 과거 시각             | 소요 | 종료 사유         |
| ---- | ----------- | -------- | ---------------------- | -------------------------- | ---- | ----------------- |
| TQQQ | 12          | 2391     | 2026-07-28T00:57+09:00 | **2026-07-24T09:01+09:00** | 2.6s | `nextBefore` 없음 |
| SQQQ | 12          | 2352     | 2026-07-28T00:58+09:00 | **2026-07-24T09:01+09:00** | 3.3s | `nextBefore` 없음 |

**약 4거래일치만 제공한다.** 예산 소진(호출 상한 3000회로 설정)이 아니라 API가
자연스럽게 페이징을 멈췄다 — 이것이 Toss 1분봉 API의 진짜 하드 리밋으로 보인다.
일봉(16년)과 분봉(4일)의 깊이 차이가 압도적이다.

## 3. Toss — IP allowlist 위험

전임자 문서는 Toss가 호출자의 공인 IP를 allowlist에 등록해야 한다고 경고했다.
**이 Mac에서는 403이 전혀 발생하지 않았다** — 위 모든 호출(일봉/분봉, 총 66
API 호출)이 정상 200 응답을 받았다. 이 환경의 공인 IP는 이미 등록되어 있거나,
Toss가 이 엔드포인트엔 IP 제한을 걸지 않는 것으로 보인다. (다른 네트워크/서버
환경에서는 재확인 필요.)

## 4. Kiwoom — 모의투자(mockapi.kiwoom.com) 해외주식 차트 지원

### 재현 명령

```bash
uv run python - <<'PY'
import os, httpx
from dotenv import load_dotenv
load_dotenv(".env"); load_dotenv(".env.local", override=True)

http = httpx.Client(base_url=os.environ.get("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com"), timeout=10.0)
resp = http.post("/oauth2/token", json={
    "grant_type": "client_credentials",
    "appkey": os.environ["KIWOOM_APP_KEY"],
    "secretkey": os.environ["KIWOOM_SECRET_KEY"],
})
print(resp.status_code, resp.text)
PY
```

### 결과 — 토큰 발급 단계에서 막힘

```
200 {"return_msg":"입력 값 오류입니다[8030:투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다]","return_code":2}
```

HTTP 상태는 200이지만 바디의 `return_code=2`는 에러를 뜻한다. 메시지를 번역하면
"실전/모의 투자구분이 달라서 Appkey를 사용할 수 없음" — 즉 `.env.local`의
`KIWOOM_APP_KEY`/`KIWOOM_SECRET_KEY`는 **실전(live) 계좌용으로 발급된 키**이고,
`mockapi.kiwoom.com`(모의투자 서버)은 별도의 모의투자 전용 appkey를 요구한다.
**이 appkey/secretkey로는 모의투자 서버에서 토큰조차 발급받을 수 없어, 해외주식
차트 TR 호출 자체를 시도할 수 없었다.** (실전 서버 `api.kiwoom.com`으로 시도하는
것은 범위 밖 — 실계좌 접근 위험이 있어 시도하지 않았다.)

### 문서 조사로 확인한 것 (미검증)

`https://openapi.kiwoom.com`의 API 가이드(REST, jobTpCode=03: 해외주식)에는
아래 차트 TR들이 카탈로그에 존재한다:

| TR 코드           | 이름               | 엔드포인트            |
| ----------------- | ------------------ | --------------------- |
| usa06010          | 미국주식 틱 차트   | `/api/chart/usa06010` |
| usa06011          | 미국주식 분 차트   | `/api/chart/usa06011` |
| usa06012          | 미국주식 일 차트   | `/api/chart/usa06012` |
| usa06013~usa06016 | 주/월/년/분기 차트 | `/api/chart/usa060xx` |

가이드 페이지는 base URL로 실전(`api.kiwoom.com`)과 모의(`mockapi.kiwoom.com`)를
함께 언급하지만, **분/일 차트 TR의 정확한 요청 바디 필드명(종목코드/거래소코드
파라미터명 등)은 이 페이지에서 확인되지 않았고**, 위 인증 실패로 실제 호출도
못 해봤다. 즉 "TR이 카탈로그에 존재한다"는 것과 "모의투자 서버가 실제로 데이터를
내려준다"는 것은 별개이며, 후자는 **미검증**이다.

### 결론

Kiwoom 경로는 appkey 재발급(모의투자 전용 키) 전까지 완전히 막혀 있다. 이 appkey
발급은 이 작업 범위 밖(사용자의 키움 개발자센터 계정 작업)이다.

---

## 5. Yahoo Finance(yfinance) — interval별 과거 데이터 깊이 실측 (2026-07-28)

Toss/Kiwoom 모두 15분봉 백테스트에 필요한 깊이를 못 주는 상황에서, **인증이
전혀 필요 없는** Yahoo Finance를 실측했다. `quant/collect/quotes/yf_source.py`의
`YFinanceCandleSource`가 구현체다.

### 재현 명령

```bash
uv run python - <<'PY'
import yfinance as yf
for symbol in ["TQQQ", "SQQQ"]:
    for interval in ["1m", "15m", "1h", "1d"]:
        df = yf.Ticker(symbol).history(period="max", interval=interval)
        print(symbol, interval, len(df), df.index[0], df.index[-1], df.index.tz)
PY
```

### 결과 (TQQQ 기준, SQQQ 동일 패턴)

| interval | 실측 깊이(현재 시점 기준)                           | 봉 수(`period="max"`) | tz               | 비고                                                                                                                                                                  |
| -------- | --------------------------------------------------- | --------------------- | ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1m       | 약 5~7거래일                                        | 2113                  | America/New_York | 요청당 8일 상한도 별도 존재("Only 8 days worth of 1m granularity data are allowed to be fetched per request") — 총 깊이도 얕아 **의도적으로 미구현**(`fetch_1m` 없음) |
| 15m      | **정확히 60일**(그 이상 요청 시 에러 없이 빈 결과)  | 1041(≈40거래일)       | America/New_York | `YFinanceCandleSource("15m")`가 지원. 하루 26봉(09:30-16:00 정규장) 확인됨                                                                                            |
| 1h       | **정확히 730일**(그 이상 요청 시 에러 없이 빈 결과) | 3467(≈2년)            | America/New_York | `YFinanceCandleSource("1h")`가 지원                                                                                                                                   |
| 1d       | 상장 이후 전체(2010-02-11~)                         | 4138                  | America/New_York | Toss 일봉 실측과 정확히 일치                                                                                                                                          |

경계값도 실측 확인: 15m은 60일 전 요청까지는 성공(1025봉), 61일 전부터는 0봉(에러
없이 조용히 빈 결과 — Yahoo API가 `possibly delisted; no price data found`라는
오해하기 쉬운 로그만 남긴다). 1h도 730일에서 동일 패턴(730일 성공/3467봉, 731일
실패/0봉). 그래서 `YFinanceCandleSource.fetch()`는 이 하드리밋보다 1일 안전
여유를 두고 clamp한다(경계 근처 서버 시계 오차로 정확히 한도만큼 요청해도
실패하는 경우가 실측됐음).

### 설계: native interval을 정직하게 다루기

Yahoo는 1분봉을 깊게 못 주지만 15분봉/1시간봉/일봉은 각각 다른 깊이로 **네이티브**
제공한다 — 1분봉을 리샘플해서 만든 게 아니라 Yahoo가 그 간격으로 직접 집계한
봉이다. 기존 `CandleSource.fetch_1m()` 계약(1분봉 전용)에 억지로 끼워맞추는 대신,
`quant/collect/quotes/source.py`에 `MultiIntervalCandleSource` Protocol을
추가했다 — `native_interval` 속성으로 실제 간격을 선언하고, `fetch()`가 그 간격
그대로의 봉만 반환한다.

- `backfill()`은 `interval` 파라미터를 받는다. `interval="1m"`(기본)이면 기존
  경로(`data/history/{symbol}/{YYYY}/{MM}.parquet`) 그대로 — 완전히 하위 호환.
  `interval="15m"` 등이면 `data/history/{symbol}/{interval}/{YYYY}/{MM}.parquet`
  로 **별도 하위 디렉터리**에 쓴다 — 경로만 봐도 실제 봉 간격을 오인할 수 없다.
  또한 `source.native_interval`이 요청한 `interval`과 정확히 일치하는지 확인한
  뒤에만 받는다(불일치 시 `ValueError`) — 다른 간격을 그 간격인 척 저장하는 걸
  원천 차단.
- `HistoryDataFeed`는 1분봉이 있는 심볼은 기존처럼 리샘플하고, 1분봉이 없는
  심볼(예: yfinance 15m 전용 백필)은 native 저장소에서 **정확히 일치하는
  interval만** 서빙한다. 15분봉만 있는데 5분봉을 요청하면(업샘플이 필요한
  경우) 리샘플/보간을 지어내지 않고 빈 결과를 반환한다.

### 실제 백필 + 백테스트 (2026-07-28)

```bash
uv run python -m quant.apps.cli fetch --symbol TQQQ --source yfinance --interval 15m --start 2026-05-30 --end 2026-07-28
uv run python -m quant.apps.cli fetch --symbol SQQQ --source yfinance --interval 15m --start 2026-05-30 --end 2026-07-28
uv run python -m quant.apps.cli backtest --strategy donchian --interval 15m --source history --days 90
```

TQQQ/SQQQ 각 1000봉(≈38거래일 + 진행 중인 당일 일부) 실제 15분봉을 확보했다.
데이터 품질 점검: 정상 거래일(38일) 전부 정확히 26봉/일, 주말/휴장일 봉 0개,
중복 타임스탬프 0개, 가격대는 TQQQ $61.8~87.7 / SQQQ $35.9~46.3로 플로서블,
인덱스 오름차순 정렬 확인. Donchian 백테스트 실행 결과: `total_return_pct=-0.29,
mdd_pct=-2.34, sharpe=-0.31, win_rate=62.5%, n_trades=8, n_bars=1000`.

**정직한 평가: 이 표본은 통계적으로 의미 있다고 보기 어렵다.** 38거래일(약 2개월)
치, 8건의 트레이드로는 전략의 우위(edge)를 판단할 근거가 되지 않는다 — Yahoo의
15분봉 60일 하드리밋 자체가 원인이다. 이 결과는 "파이프라인이 실제로 동작하고
실데이터로 백테스트가 돈다"는 것의 증거일 뿐, "이 전략이 수익성이 있다/없다"의
증거로 쓰면 안 된다. 위기 구간(2018 변동성 스파이크, 2020 COVID, 2022 약세장)을
포함한 의미 있는 표본이 필요하면 아래 6절의 Alpaca 경로가 필요하다.

`orb` 전략(`quant/trade/strategy/orb.py`, 이 작업과 동시에 다른 워커가
작성 중이던 파일)도 동일한 실데이터로 시도했다: 에러 없이 실행됐고 `n_trades=0`
(0건 진입) — 전략 로직 자체의 튜닝/디버깅은 이 작업 범위 밖이라 건드리지 않았다.

## 6. Alpaca Markets — 구현 완료, 키 발급 대기 (2026-07-28)

`quant/collect/quotes/alpaca_source.py`의 `AlpacaCandleSource`를 구현했다 —
`data.alpaca.markets`의 historical bars REST API(`GET /v2/stocks/{symbol}/bars`)를
`next_page_token`으로 페이징하며, `.env.local`의 `ALPACA_API_KEY_ID` /
`ALPACA_API_SECRET_KEY`(`.env.example`에 안내 추가됨)를 읽는다.

**계정 생성/키 발급은 사용자 본인 작업**이라 이 작업에서 시도하지 않았다 — 키가
없으면 `AlpacaCandleSource()` 생성 시점에 바로 `RuntimeError`로 실패하며 어떤
env var가 필요한지 이름을 명시한다(조용한 저하 금지).

- **예상 깊이**: 무료 티어(IEX feed)는 분봉을 대략 2016년까지 제공 —
  2018-02 변동성 스파이크, 2018 Q4, 2020-03 COVID 크래시, 2022 약세장 등 이
  시스템이 실제로 검증해야 하는 위기 구간을 전부 포함한다. (미검증 — 실제 키가
  없어 실측하지 못함. 키 발급 후 `AlpacaCandleSource(interval="1m").fetch(...)`
  로 직접 재현/검증 필요.)
- **IEX 거래량 caveat(HONESTY)**: 무료 IEX feed는 consolidated tape 거래량의
  약 2~3%에 불과하다. Donchian 전략의 거래량 필터(봉 거래량 > 배수 × 추세 평균
  거래량)는 **상대** 비교라서 IEX 거래량이 그럴듯한 대리 지표는 될 수 있지만,
  절대 거래량과는 다르다 — 이건 실제 caveat이지 무시해도 되는 문제가 아니다.
  유료 SIP feed를 쓰면 완전한 consolidated 거래량을 받는다.
- `interval="1m"`으로 생성하면 기존 `CandleSource.fetch_1m()` 계약도 만족해
  `backfill()`의 1분봉 기본 경로에 그대로 꽂힌다(Toss와 동일 패턴) — Yahoo와
  달리 Alpaca 무료 티어는 분봉을 실제로 깊게 제공하기 때문에 이게 가능하다.
  `interval="15m"`/`"1h"`/`"1d"`로 생성하면 `MultiIntervalCandleSource`로
  동작해 5절의 native interval 경로(`backfill(..., interval=...)`)에 꽂힌다.

## 권고 (갱신, 2026-07-28)

1. **일봉 기반 레짐/트렌드 분석은 Toss로 충분하다** — TQQQ/SQQQ 상장 이래
   전체 히스토리를 무료로 확보 가능. (`quant.apps.cli fetch`는 현재 1분봉만
   구현되어 있음 — 일봉 백필이 필요해지면 같은 `TossCandleSource` 패턴으로
   `interval="day"` 소스를 추가하면 된다.)
2. **15분봉 Donchian 백테스트에 "지금 당장" 필요한 깊이는 yfinance로 확보
   가능해졌다 — 단, 60일치뿐이다.** Toss 1분봉(4거래일)보다는 훨씬 낫지만,
   60일(약 40거래일)은 전략의 우위를 판단하기엔 짧다. **일봉을 리샘플/보간해서
   분봉을 만들어내는 것은 여전히 금지** — 봉 내부 경로(intrabar path)를 조작해
   만든 숫자는 의미가 없다(이 문서 상단 HONESTY CONSTRAINT).
3. **의미 있는 표본(위기 구간 포함)이 필요하면 Alpaca가 유일한 현실적 경로다.**
   Toss(4일)나 yfinance(60일)로는 2018/2020/2022 같은 구간을 절대 커버할 수
   없다. Alpaca 무료 IEX feed가 ~2016년까지 커버한다는 게 사실이라면(위 6절,
   미검증) 이 요구를 만족하는 유일한 무료 경로다 — 단 키 발급(사용자 작업)이
   선행돼야 한다.
4. **세 가지 현실적 경로:**
   - (a) **자연 축적**: `quant.apps.cli fetch --source toss`를 매일 cron으로
     돌려 앞으로의 1분봉을 하루씩 쌓는다. idempotent/resumable하게 설계되어
     있어 매일 실행 비용이 거의 없다(갭만 재조회). 수개월~1년 뒤엔 1분봉 기반
     실데이터 백테스트가 가능해진다.
   - (b) **yfinance 60일 창**: 인증 없이 지금 바로 40거래일치 15분봉을 확보할
     수 있다(구현 완료, 이 절 참고) — 파이프라인 검증/최근 구간 점검용으로는
     충분하지만 전략 검증용 표본으로는 짧다.
   - (c) **Alpaca(키 발급 필요)**: 위기 구간을 포함한 다년치 15분/1시간/일봉이
     필요하면 이 경로가 필요하다(구현 완료, 키만 있으면 바로 사용 가능).
5. **Kiwoom은 모의투자 전용 appkey를 재발급받기 전까지 이 결정에 기여할 수
   없다** — 재발급 후 위 재현 명령으로 다시 검증 필요.

---

## 실측에 사용한 범위

- 실 자격증명(`TOSS_CLIENT_ID/SECRET`, `TOSS_ACCOUNT_SEQ`, `KIWOOM_APP_KEY/SECRET_KEY`),
  `MODE=paper` 유지.
- 호출한 엔드포인트: Toss `GET /api/v1/candles`(시세 조회, 66회), Kiwoom
  `POST /oauth2/token`(토큰 발급 시도, 1회) — **주문/계좌 변경 API는 전혀
  호출하지 않았다.**
- 이 조사에 쓴 스크립트는 저장소 밖 scratchpad에서 실행하고 폐기했다 — 위
  재현 명령이 그 내용을 그대로 재현한다.
