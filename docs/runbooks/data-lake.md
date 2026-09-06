# Runbook: 데이터 레이크 지도 (연구 저장소 quant-backtest)

백테스트 연구 저장소([`../quant-backtest`](../../../quant-backtest))가 쓰는 로컬
데이터 레이크가 무엇을 어디에 담는지, 그리고 KR 1분봉이 어떻게 채워지는지의
지도. 전략·엔진·백테스트 코드는 이 저장소(`Quant_Trading`)에만 있다 —
`quant-backtest`는 데이터 + walk-forward 러너일 뿐이다(그 저장소 README.md 상단).

> **주의**: `quant-backtest/docs/DATA_CONVENTIONS.md`라는 별도 문서는 **존재하지
> 않는다**(2026-09-06 확인). 레이아웃 규약은 그 저장소 `README.md`의
> "데이터 레이크" 절에 있다 — 이 문서의 표는 그 절 + 디스크 실측을 정리한 것이고,
> 링크는 그 절을 가리킨다.

## 무엇이 어디에 있나

`quant-backtest/data/` 아래, 실측(2026-09-06) 기준:

| 경로 | 내용 | 소스 | 채우는 주기 |
| --- | --- | --- | --- |
| `data/lake/{symbol}/{YYYY}/{MM}.parquet` | **US 1분봉** (2016~, Alpaca SIP) | `qb/fetch_alpaca.py` | 사람이 필요할 때 수동 (`qb.fetch_alpaca.fetch()`) |
| `data/lake/{symbol}/{YYYY}/{MM}.parquet` | **KR 1분봉** (Kiwoom `ka10080`, EC2 전용 — 아래 "KR 1분봉" 절) | `qb/fetch_kiwoom.py` | **이 저장소가 자동화**(server/scripts/kr_minute_backfill.sh, 토 05:00 KST) → `make lake-pull-kr` |
| `data/lake/{symbol}/1d/{YYYY}/{MM}.parquet` | KR 일봉(FinanceDataReader, 무인증) | `qb/fetch_pykrx.py` | 사람이 필요할 때 수동 |
| `data/lake/_manifest.json` | `(symbol, interval)` → 구간·행수·출처(디스크 실측, 자기신고 아님) | `qb/lake.py` | 백필이 갱신, 또는 `rebuild_manifest()`/`update_manifest()` 수동 호출 |
| `data/lake_kr_flow/{symbol}/{YYYY}/{MM}.parquet` | KR 종목별 외국인/기관/개인 순매수(Kiwoom `ka10059`) | `qb/fetch_kiwoom_flow.py` | 사람이 필요할 때 수동 (EC2 전용 — 키움 인증 제약은 1분봉과 동일) |
| `data/lake_kr_daily/` | KR 일봉(별도 경로, `qb/fetch_fdr_kr_daily.py`) | 위와 별개 모듈 | 사람이 필요할 때 수동 |
| `data/lake_us_raw/` | US 원시(조정 전) 데이터 | `qb/fetch_alpaca.py` (`DEFAULT_LAKE_US_RAW`) | 사람이 필요할 때 수동 |
| `data/lake_macro/` | 매크로 지표(yfinance 등) | 수집 모듈 미확인(2026-09-06 조사 시점 `qb/` 안에서 특정하지 못함) | — |
| `data/lake_kr/` | **레거시** — 엔진 자체 `data/history/`를 읽기 전용 복제하던 예전 경로(README.md의 낡은 절, ka10080이 이 맥에서 인증 거부되기 전) | — | 더 이상 채우지 않는다 — 새 파이프라인의 목적지 아님 |
| `data/universe/` | `qb/universe.py`가 레이크를 걸어 만드는 유니버스 스냅샷 | `qb/universe.py` | `build_universe_snapshots()` 수동 호출 |

레이아웃 규약(1분봉: `{symbol}/{YYYY}/{MM}.parquet`, 그 외 간격:
`{symbol}/{interval}/{YYYY}/{MM}.parquet`, 시장/간격 축은 경로가 아니라
`_manifest.json`이 답한다)은 트레이딩 저장소의 `HistoryDataFeed` 레이아웃을
그대로 따른다 — 자세한 근거는 `quant-backtest/README.md` "## 데이터 레이크" 절.

## KR 1분봉 — 어떻게 채워지나 (2026-09-06 자동화)

**배경.** 키움 `ka10080`(1분봉)의 실전 앱키는 **등록된 단말/IP에서만** 토큰이
발급된다 — 등록된 단말은 EC2뿐이다(맥에서 호출하면 `[8050] 지정단말기 인증에
실패했습니다`, `quant-backtest/README.md` 실측표). 그래서 1분봉은 **EC2에서만**
받을 수 있고, 맥은 그 결과를 읽기 전용으로 복제하는 수밖에 없다.

**EC2 쪽 (`server/scripts/kr_minute_backfill.sh`, 토 05:00 KST).**
`~/qb_backfill`(quant-backtest 체크아웃, 엔진과 별도)에서 `qb.fetch_kiwoom.fetch()`를
직접 호출한다 — 이 함수에 CLI 엔트리(`python -m qb.fetch_kiwoom`)는 없다(라이브러리
함수만 노출, 2026-09-06 확인). 대상은 watchlist ∪ KR 레버리지/인버스 ETF 고정셋
(069500/122630/252670/229200/233740/251340/114800) ∪ 대형주 유니버스 캐시
(`data/state/kr_largecap_universe.json`, ~300종목) — 6자리 코드만 남긴다. 신규/장기
결측 심볼은 깊게(max_pages=150), 최근 2개월 안에 이미 데이터가 있는 심볼은
얕게(max_pages=20, 주간 갱신분만) 페이지를 넘긴다 — 상세 근거는 스크립트 상단
주석. 토요일 고정인 이유: KR·US 정규장이 둘 다 없어 키움 쿼터가 엔진과 겹치지
않는 유일한 창.

결과는 `~/qb_backfill/lake/{symbol}/{YYYY}/{MM}.parquet`에 쌓이고, 실패/성공
카운트를 텔레그램 `NOTIFY_LANE=ops`(마감 다이제스트로만, `notify_defer`)로
한 줄 보낸다. `~/qb_backfill` 자체가 없으면 1회만 경보하고 조용히 멈춘다.

**맥 쪽 (`local/lake/pull_kr_minute.sh`, `make lake-pull-kr`).** EC2의
`~/qb_backfill/lake/`를 rsync로 로컬 `quant-backtest/data/lake/`에 병합한다
(같은 루트 — US 1분봉과 KR 1분봉이 심볼 디렉터리로만 구분된다, 위 표 참고).
사람이 필요할 때 직접 돌린다(원버튼, `local/ml/run.sh`와 같은 정신 — 클로드가
대신 돌리지 않는다). 동기화 후 매니페스트 갱신 커맨드를 화면에 출력하지만
**실행하지는 않는다** — quant-backtest에서 사람이 확인 후 직접 돌린다(전체
`rebuild_manifest()`는 쓰지 않는다: US 심볼 항목까지 source 라벨을 덮어써
버리기 때문 — `kr_after_backfill.sh`가 이미 같은 이유로 심볼별
`update_manifest()`만 쓴다).

**주간 케이던스.** 토 05:00(EC2 백필) → 아무 때나(사람이 `make lake-pull-kr`) →
확인되면 quant-backtest에서 매니페스트 갱신 → (선택) `scripts/kr_after_backfill.sh`
로 노트북 03 재실행 + 게이트. 하루라도 EC2 백필을 건너뛰면 그 주 데이터가
영구히 사라지는 것은 아니다(다음 주 실행이 여전히 서버가 주는 만큼 받는다) —
다만 Kiwoom 서버 쪽 1분봉 보유 기간 자체가 유한하므로(실측 근거 약 13개월,
`quant-backtest/results/letf/SUMMARY_kr.md`) 너무 오래 건너뛰면 그 사이 구간이
서버에서도 로컬에서도 영구히 빈다 — 자동화가 필요한 이유다.

## 참고

- `quant-backtest/README.md` "## 데이터 레이크", "## 소유자가 실제로 돌리는 순서" 절
- `quant-backtest/scripts/kr_after_backfill.sh` — EC2 백필 완료 후 로컬 동기화 +
  노트북 재실행 + 게이트를 한 번에 묶은 수동 스크립트(이 러너와는 별개 용도 —
  "받은 뒤 바로 검증까지" 하고 싶을 때 손으로 돌린다)
- `quant-backtest/results/letf/SUMMARY_kr.md` — KR 1분봉 실측 깊이/구간 기록
- [`live-readiness.md`](live-readiness.md) B6 — 분할·배당 조정 데이터 검증
