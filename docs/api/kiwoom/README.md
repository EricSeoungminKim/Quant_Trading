# 키움증권 REST API + WebSocket 구현 레퍼런스

> **상태 노트 (2026-08-10)**: 이 문서는 조사 시점의 리서치 기록이다. 본문에서
> "미검증 스켈레톤"으로 지칭하는 구현은 이후 실전 서버(`api.kiwoom.com`)에서
> 검증 완료됐다 — 토큰, 웹소켓 실시간 체결(005930), ka10059 종목별 기관-외국인
> 수급 모두 실호출 성공. §7의 우려 중 토큰 필드명·재구독 문제는 구현에 반영됐다.
> 미국주식 API의 [미확인/상충] 항목들은 여전히 미검증이다.


이 문서는 실키(app key / secret key) 발급 후 `quant/adapters/brokers/kiwoom/` 어댑터를
채워 넣기 위한 구현 참고 자료다. **라이브 API 호출은 하지 않았다** — 전부 공개된
1차/2차 소스(공식 개발자 포털, 키움증권 공식 GitHub 저장소의 소스 코드와 머신
리더블 스펙 JSON, Python 래퍼 저장소, 한국어 개발 블로그)를 근거로 작성했다.

각 항목에 **[확인됨]** / **[미확인]** / **[상충]** 마킹을 달았다. **[확인됨]**은
아래 "가장 신뢰할 수 있는 소스"에서 나온 것이고, **[미확인]**은 2차 소스 추정,
**[상충]**은 소스 간에 서로 다른 말을 하는 경우다. 필드명이 그럴듯해 보여도
[미확인] 태그가 있으면 실키로 검증 전까지 코드에 하드코딩하지 말 것.

## 가장 신뢰할 수 있는 소스 (사용한 근거 자료 등급)

1. **최고 신뢰도 — 키움증권 공식 GitHub 저장소 소스 코드 그 자체**
   `https://github.com/Kiwoom-Securities/Kiwoom-REST-API` (organization: Kiwoom-Securities,
   즉 키움증권 본사 명의 공식 저장소). 이 저장소의 `kiwoom/core/auth.py`,
   `kiwoom/core/client.py`, `kiwoom/core/ws_client.py`를 raw로 직접 읽었다 — 이건
   "블로그의 설명"이 아니라 키움이 실제로 실서버에 요청을 보내고 응답 필드를
   파싱하는 **실행 코드**이므로 이 문서에서 가장 강한 근거로 취급했다.
2. **매우 높음 — 같은 저장소의 `kiwoom/_data/kiwoom_api_spec.json`** (2.4MB, 208개
   API 항목의 머신 리더블 명세: api-id, HTTP method, path, 요청/응답 필드명·타입·필수여부·한글명).
   이것도 키움 공식 저장소 소유물이다.
3. **높음 — `openapi.kiwoom.com` 공식 개발자 포털의 API 가이드 페이지** (`/m/guide/apiguide?jobTpCode=NN`).
   페이지별로 직접 fetch해서 필드명을 확인했다.
4. **중간 — 서드파티 Python 래퍼 저장소** (younghwan91/kiwoom-rest-api, bamjun/kiwoom-rest-api,
   kiwoom-restful). 공식 문서를 재해석한 것이라 필드명이 요약·의역되어 있을 수 있음.
5. **낮음 — 한국어 블로그 포스트** (algolab, pabburi 등). 정황 설명(레이트리밋 체감치,
   함정 목록)에는 유용하지만 verbatim 필드명 근거로는 쓰지 않았다.

---

## 1. 인증 (OAuth) — [확인됨]

**근거:** `kiwoom/core/auth.py` (공식 저장소, 실행 코드) + `kiwoom_api_spec.json`의
`au10001`/`au10002` 항목.

### 1.1 접근토큰 발급 (`au10001`)

```
POST {base_url}/oauth2/token
Content-Type: application/json;charset=UTF-8
```

- `base_url`은 모의투자 `https://mockapi.kiwoom.com`, 실전 `https://api.kiwoom.com`.
- 공식 소스 코드(`auth.py`)는 이 요청에 **`authorization` 헤더를 보내지 않는다** —
  스펙 JSON에는 `authorization`이 "필수" 헤더로 나열되어 있지만 이건 모든 API에
  공통으로 붙는 템플릿 항목이고, 토큰 발급 자체는 아직 토큰이 없으므로 실제로는
  필요 없다. **[상충 주의]**: 스펙 메타데이터를 그대로 믿고 `Authorization` 헤더를
  실으면 안 된다 — 실제 공식 클라이언트 코드가 안 보내는 걸 근거로 뺐다.

**요청 바디:**

```json
{
  "grant_type": "client_credentials",
  "appkey": "<APP_KEY>",
  "secretkey": "<APP_SECRET>"
}
```

필드명은 정확히 `appkey`, `secretkey` (camelCase나 `app_key`가 아니다) — 공식
소스 코드에서 그대로 확인.

**응답 바디 (공식 스펙 JSON 기준, 필드명 3개뿐):**

```json
{
  "return_code": 0,
  "return_msg": "",
  "token": "<ACCESS_TOKEN>",
  "token_type": "bearer",
  "expires_dt": "20260729235959"
}
```

- `token` — 접근토큰 (⚠️ **`access_token`이 아니라 `token`이다.** 아래 "구현 시
  주의점" 참고).
- `token_type` — 기본값 소문자 `"bearer"`로 처리되는 걸 코드에서 확인(`.lower()`
  호출), 실제 서버가 `"Bearer"`로 줄 수도 있음 — 대소문자 신뢰하지 말고 항상
  `Bearer {token}`으로 직접 조립할 것.
- `expires_dt` — ⚠️ **초 단위 TTL(`expires_in`)이 아니라 만료 "일시" 문자열이다.**
  공식 코드가 파싱하는 포맷은 `%Y%m%d%H%M%S` (예: `20260729235959`), 그리고 이
  값은 **KST**로 해석한 뒤 UTC로 변환한다 (`timezone(timedelta(hours=9))`).
- `return_code` / `return_msg` — 토큰 발급 응답에도 다른 모든 API와 동일한
  성공/실패 컨벤션이 붙는다 (`0` = 성공).

**토큰 사용법 (모든 후속 요청):**

```
authorization: Bearer <token>
```

- 헤더 값은 `token_type` 그대로 붙이는 게 아니라 **항상 고정 문자열 `Bearer`를
  직접 앞에 붙인다** — 공식 코드: `f"Bearer {self.get_access_token()}"`.

### 1.2 접근토큰 폐기 (`au10002`)

```
POST {base_url}/oauth2/revoke
Content-Type: application/json;charset=UTF-8
```

```json
{
  "appkey": "<APP_KEY>",
  "secretkey": "<APP_SECRET>",
  "token": "<ACCESS_TOKEN>"
}
```

응답은 `return_code`/`return_msg`만 있고 바디 데이터는 없다.

### 1.3 리프레시 플로우 — [확인됨: 없음]

별도의 refresh-token 그랜트는 없다. 만료 전에 **동일한 `client_credentials`
플로우로 재발급**하는 것이 유일한 방법이다 (공식 코드도 refresh를
"재발급 요청"으로 구현했지 refresh_token 파라미터를 쓰지 않는다).

### 1.4 모의 vs 실전 차이 — [확인됨]

- Base URL만 다르다 (`api.kiwoom.com` vs `mockapi.kiwoom.com`); 토큰 발급/폐기
  경로, 필드명, 헤더 컨벤션은 동일.
- 앱키는 실전용/모의용이 **분리 발급**되며 섞어 쓸 수 없다 — 공식 에러코드
  `8030`(`투자구분(실전/모의)이 달라서 Appkey를 사용할수가 없습니다`),
  `8031`(토큰 버전 동일 문제)이 이를 명시적으로 뒷받침한다. **[확인됨]**
  (근거: `kiwoom_api_spec.json`의 `error_codes` 목록).
- 에러코드 `8104`: `"모의투자에서 지원하지 않는 API 입니다."` — 즉 **일부 API는
  모의투자에서 아예 거부된다**는 것이 공식 에러코드 테이블에 명시돼 있다. 어떤
  API가 해당되는지 목록은 공개 스펙에 없음 — **[미확인]**, 실키로만 확인 가능.

---

## 2. 요청 컨벤션 — [확인됨]

**근거:** `kiwoom/core/client.py` (공식 저장소).

### 2.1 `api-id` 헤더 디스패치

키움 REST는 국내주식·계좌·주문 대부분을 **소수의 공유 경로**(`/api/dostk/mrkcond`,
`/api/dostk/chart`, `/api/dostk/ordr`, `/api/dostk/acnt` 등)로 몰아넣고, 실제로
어떤 TR(트랜잭션)을 실행할지는 **`api-id` 요청 헤더**로 구분한다. URL 경로만
보고 오퍼레이션을 알 수 없다 — 반드시 `api-id`를 같이 봐야 한다.

**확정 요청 헤더 세트 (모든 REST 호출 공통):**

| 헤더            | 필수              | 설명                               |
| --------------- | ----------------- | ---------------------------------- |
| `Content-Type`  | Y                 | `application/json;charset=UTF-8`   |
| `api-id`        | Y                 | TR 코드 (예: `ka10004`, `kt10000`) |
| `authorization` | Y (토큰발급 제외) | `Bearer {token}`                   |
| `cont-yn`       | N                 | 연속조회 시 `"Y"`                  |
| `next-key`      | N                 | 연속조회 커서                      |

### 2.2 연속조회 (페이지네이션)

`cont-yn` / `next-key`는 **요청 헤더로 보내고, 응답도 바디가 아니라 응답
헤더(HTTP header)로 돌아온다.** 공식 클라이언트 코드가 정확히 이렇게 처리:

```python
cont_yn = response_headers.get("cont-yn") or response_headers.get("Cont-Yn")
next_key = response_headers.get("next-key") or response_headers.get("Next-Key")
has_next = cont_yn == "Y"
```

다음 페이지를 가져올 때는 이전 응답 헤더의 `next-key` 값을 그대로 다음 요청의
`next-key` 헤더에 실어 보내고 `cont-yn: "Y"`를 같이 보낸다.

### 2.3 응답 바디 공통 필드

모든 응답 바디에 `return_code`(0=성공), `return_msg`가 공통으로 붙는다. HTTP
상태코드가 200이어도 `return_code != 0`이면 실패로 처리해야 한다 (공식 클라이언트
코드가 실제로 `return_code`를 별도로 검사).

### 2.4 인증 만료 재시도 컨벤션 — [확인됨]

공식 클라이언트는 다음 두 조건 중 하나면 **토큰을 재발급하고 요청을 1회
자동 재시도**한다:

- HTTP 401
- 바디의 `return_code`가 `{8005, 8031, 8103}` 중 하나
  (`8005`=토큰 유효하지 않음, `8031`=투자구분 불일치로 토큰 사용 불가,
  `8103`=토큰/단말기 인증 실패)

### 2.5 Rate limit — [확인됨: 존재 / 미확인: 정확한 수치]

공식 에러코드 테이블에 명시된 것 (존재는 확인됨, 숫자는 비공개):

- `1700`: "허용된 API 요청 개수를 초과 (유량={?}, API_ID={?})" — **TR(api-id)별
  개별 유량 제한**이 있다는 뜻.
- `1701`: "허용된 전체 요청 개수를 초과 (총유량={?})" — 계좌/앱키 전체 총량 제한.
- `1702`: "허용된 그룹 요청 개수를 초과" — 그룹 단위 제한도 별도로 있음.
- `1687`: "재귀 호출이 발생하여 API 호출을 제한합니다" — 재시도 로직이 무한
  루프를 만들면 별도로 차단됨.

정확한 초당/일별 수치는 공개 문서에서 찾지 못했다 — **[미확인]**. 한국어
블로그(algolab)의 실측 체감치는 "TR당 약 1 req/s, burst 2" 수준이라고 하지만
이건 3rd-party 관찰치이지 공식 발표 수치가 아니다 — 참고만 하고 신뢰하지 말 것.
레거시 OpenAPI+(OCX)는 초당 약 5회였다는 회고담이 있으나 REST는 별개 시스템이라
그대로 적용 불가.

### 2.6 거래소 구분 코드 — [확인됨]

2025년 넥스트레이드(NXT) 출범 이후 종목코드/거래소 구분이 3분화됐다:

- `KRX` — 기존 코스피/코스닥 (예: `005930`)
- `NXT` — 넥스트레이드 (예: `005930_NX`)
- `SOR` — Smart Order Routing, 최선 체결 (예: `005930_AL`)

`dmst_stex_tp` (국내거래소구분) 필드에 `KRX`/`NXT`/`SOR` 문자열을 그대로 넣는다
(주문 API). 실시간시세 구독 시 종목코드 접미사로도 구분한다.

---

## 3. 국내주식 (국내주식) 엔드포인트 — [확인됨]

**근거:** `kiwoom_api_spec.json`에서 직접 추출 (208개 API 중 206개가 국내주식).
아래는 요청하신 6개 카테고리를 대표하는 api-id만 뽑았다 — 전체 목록은 206개다.

공통: 모든 요청에 `Content-Type: application/json;charset=UTF-8`,
`api-id`, `authorization: Bearer {token}` 헤더, 그리고 옵션으로
`cont-yn`/`next-key`.

### 3.1 현재가/호가 조회 — `ka10004` 주식호가요청

```
POST /api/dostk/mrkcond
```

- 요청 바디: `stk_cd` (종목코드, 예: `005930`, `005930_NX`, `005930_AL`) — 필수
- 응답: 매도/매수 10단계 호가 및 잔량 (`sel_1th_pre_bid` ~ `sel_10th_pre_bid`,
  `buy_1th_pre_bid` ~ 등), `bid_req_base_tm` (호가잔량기준시간)

### 3.2 분봉 차트 — `ka10080` 주식분봉차트조회요청

```
POST /api/dostk/chart
```

- 요청 바디:
  - `stk_cd` (필수)
  - `tic_scope` (필수, 2자리) — `"1"`=1분, `"3"`=3분, `"5"`=5분, `"10"`=10분,
    `"15"`=15분, `"30"`=30분, `"45"`=45분, `"60"`=60분
  - `upd_stkpc_tp` (필수, 1자리) — 수정주가구분, `0` or `1`
  - `base_dt` (선택, 8자리 `YYYYMMDD`)
- 응답: `stk_min_pole_chart_qry` (LIST) 안에 `cur_prc`(현재가/종가), `trde_qty`(거래량),
  `cntr_tm`(체결시간), `open_pric`, `high_pric`, `low_pric`, `pred_pre`,
  `pred_pre_sig`

같은 `/api/dostk/chart` 경로 + 다른 `api-id`로: 틱차트 `ka10079` (`tic_scope`
단위가 분이 아니라 틱: `1/3/5/10/30`), 일봉 `ka10081`, 주봉 `ka10082`,
월봉 `ka10083`, 년봉 `ka10094`. 일/주/월/년봉은 `base_dt` (기준일자) 요청 필드를
쓴다.

### 3.3 매수 주문 — `kt10000` 주식 매수주문

```
POST /api/dostk/ordr
```

- 요청 바디:
  - `dmst_stex_tp` (필수, `KRX`/`NXT`/`SOR`)
  - `stk_cd` (필수)
  - `ord_qty` (필수, 1주 단위)
  - `ord_uv` (선택, 원 단위 — 시장가 등에는 불필요)
  - `trde_tp` (필수, 2자리) — `0`=보통, `3`=시장가, `5`=조건부지정가,
    `81`=장마감후시간외, `61`=장시작전시간외, `62`=시간외단일가
  - `cond_uv` (선택, 조건단가)
- 응답: `ord_no` (주문번호), `dmst_stex_tp`

매도 주문 `kt10001`도 동일한 경로/필드 (`/api/dostk/ordr`), 반대 방향일 뿐.

### 3.4 정정/취소 주문

- **정정** `kt10002` — `POST /api/dostk/ordr`. 요청: `dmst_stex_tp`, `orig_ord_no`
  (원주문번호, 7자리), `stk_cd`, `mdfy_qty` (`'0'` 입력 시 잔량 전부 정정),
  `mdfy_uv`, `mdfy_cond_uv`(선택). 응답: `ord_no`, `base_orig_ord_no`(모주문번호),
  `mdfy_qty`, `dmst_stex_tp`.
- **취소** `kt10003` — `POST /api/dostk/ordr`. 요청: `dmst_stex_tp`, `orig_ord_no`,
  `stk_cd`, `cncl_qty` (`'0'` 입력 시 잔량 전부 취소). 응답: `ord_no`,
  `base_orig_ord_no`, `cncl_qty`.

(신용 주문은 별도 api-id `kt10006`~`kt10009`, 경로 `/api/dostk/crdordr` — 이번
Phase 범위는 아니지만 존재만 기록해둔다.)

### 3.5 잔고/보유종목 — `kt00005` 체결잔고요청

```
POST /api/dostk/acnt
```

- 요청 바디: `dmst_stex_tp` (`KRX`/`NXT`)
- 응답: 보유종목 리스트 — `stk_cd`, `stk_nm`, `cur_qty`(보유수량), `cur_prc`,
  `buy_uv`(매입단가), `evlt_amt`(평가금액), `evltv_prft`(평가손익), `pl_rt`(손익율)

계좌 총괄 평가는 별도 api-id **`kt00004`** 계좌평가현황요청 (같은 경로) — 요청
`qry_tp`(0:전체, 1:상장폐지종목제외), `dmst_stex_tp`; 응답에 `aset_evlt_amt`,
`tot_pur_amt`, `tot_pl_amt`, `tot_pl_rt` + 종목별 리스트.

### 3.6 예수금 / 주문가능금액 — `kt00001` 예수금상세현황요청

```
POST /api/dostk/acnt
```

- 요청 바디: `qry_tp` (필수, `2`=일반조회, `3`=추정조회)
- 응답 (일부): `entr`(예수금), `profa_ch`(주식증거금현금), `uncl_stk_amt`(미수확보금),
  `shrts_prica`(공매도대금), `repl_amt`(대용금평가금액) 등 30여개 필드

주문 가능 수량/금액을 종목·가격 단위로 정밀 계산하려면 별도 api-id
**`kt00010`** 주문인출가능금액요청 (`stk_cd`, `trde_tp`, `uv` 요청) 또는
**`kt00011`** 증거금율별주문가능수량조회를 쓴다. 계좌 총 추정자산은 `kt00003`.

---

## 4. 해외주식 (미국주식) — 이번 태스크의 핵심 질문, **[상충 — 반드시 실키로 검증]**

이 부분이 가장 중요하고 가장 불확실하다. 두 1차 소스가 서로 다른 그림을 준다:

### 4.1 포털 문서는 "지원한다"고 말한다 — [확인됨: 문서 페이지 존재]

`openapi.kiwoom.com`의 API 가이드 사이드바에는 국내주식과 **동급의 최상위
카테고리로 "미국주식"**이 존재하고, 그 아래 계좌/관심종목/순위정보/시세/
실시간시세/업종/조건검색/종목정보/**주문**/차트/투자정보/**환전** 하위
메뉴까지 갖춰져 있다. 실제로 fetch해서 확인한 개별 API 페이지:

| API명                       | api-id     | Method/Path              | 비고                                                                           |
| --------------------------- | ---------- | ------------------------ | ------------------------------------------------------------------------------ |
| 미국주식 매수 주문          | `ust20000` | `POST /api/us/ordr`      | `stex_tp`(NA/ND/NY), `stk_cd`, `ord_qty`, `ord_uv`, `trde_tp`                  |
| 미국주식 틱 차트            | `usa06010` | `POST /api/us/chart`     | `stex_tp`, `stk_cd`, `tic_scope`, `upd_stkpc_tp`, `exrt_appl_tp`(환율적용구분) |
| 미국주식 리서치(주식/ETF)   | `usa24300` | `POST /api/us/invtinfo`  | 현재가/등락 등 시세성 필드                                                     |
| 미국주식 실시간종목조회순위 | `usa01980` | `POST /api/us/rkinfo`    | 순위 조회                                                                      |
| 미국주식 업종별 기간수익률  | `usa23000` | `POST /api/us/sect`      | `stex_tp`(0:전체,1:NYSE,2:AMEX,3:NASDAQ)                                       |
| 미국주식 조건검색 목록조회  | `usa20280` | `POST /api/us/websocket` | 조건검색 WS 경로도 `/api/us/websocket`으로 별도 존재                           |

각 페이지에는 **"모의투자 도메인: `https://mockapi.kiwoom.com`"** 문구가
반복적으로 등장한다 — 포털 문서 텍스트만 보면 모의투자에서도 미국주식 API가
동작하는 것처럼 보인다.

`stex_tp` 값 `NA`/`ND`/`NY` 는 아마 NASDAQ/AMEX(구 NASDAQ표기?)/NYSE 계열
거래소 코드로 추정되나 정확한 대응은 **[미확인]** — 포털 텍스트에서 명시적
정의를 찾지 못했다.

### 4.2 그러나 키움 공식 GitHub 저장소(실행 코드+스펙)에는 미국주식이 전무하다 — [확인됨, 매우 중요]

`Kiwoom-Securities/Kiwoom-REST-API` 저장소를 직접 조사한 결과:

- `examples/` 아래에는 `OAuth 인증`과 `국내주식` 두 디렉터리만 있다.
  `미국주식`/`해외주식` 예제 디렉터리는 **없음**.
- `kiwoom_docs/` 아래 16개 마크다운 문서(ELW, ETF, 계좌, 공매도, 기관-외국인,
  대차거래, 순위정보, 시세, 실시간시세, 업종, 조건검색, 종목정보, 주문, 차트,
  테마) 전부 국내주식 카테고리이고, 해외/미국 관련 문서 파일이 **없음**.
- 결정적으로, 이 저장소의 머신 리더블 스펙 `kiwoom/_data/kiwoom_api_spec.json`
  (208개 API 전량)을 직접 파싱해서 category별로 집계하면:

  ```
  OAuth 인증   : 2개
  국내주식     : 206개
  ```

  **`미국주식`/`usa*`/`ust*` 접두 api-id는 이 파일에 단 하나도 없다.** (직접
  `usa`/`ust`/`us` 접두어로 grep했지만 0건.)

즉 키움증권 본사가 관리하는 **공식 Python SDK 저장소는 국내주식만 구현되어
있고, 해외/미국주식 API는 전혀 포함돼 있지 않다.** 이건 "래퍼가 아직 안 만들어서"
일 수도 있고, "포털 문서 페이지는 있지만 실제로 아직 오픈 안 된 API"일 수도
있고, "별도 신청/승인이 필요해서 SDK에서 의도적으로 제외"했을 수도 있다 —
**어느 쪽인지는 실키로 실제 호출해보기 전까지 알 수 없다.**

### 4.3 결론 — 우리가 확실히 아는 것과 모르는 것

- **[확인됨]** 포털에 미국주식 API 문서 페이지가 존재하고, 매수 주문
  (`ust20000`)까지 문서화되어 있다 — 즉 최소한 "존재를 밝힌 상태"이다.
- **[확인됨]** 키움 공식 GitHub SDK에는 미국주식이 전혀 구현/포함돼 있지 않다.
- **[미확인/상충]** 모의투자에서 미국주식 주문·시세가 **실제로** 동작하는지 —
  포털 텍스트는 "모의투자 도메인 지원"이라고 쓰지만, 이건 페이지마다 반복되는
  boilerplate 문구일 가능성이 있고(모든 API 페이지에 실전/모의 도메인을 둘 다
  나열하는 템플릿일 수 있음), 실제 모의투자 서버가 그 요청을 진짜로 처리하는지
  검증된 근거는 없다. 에러코드 `8104`("모의투자에서 지원하지 않는 API")가
  존재하는 것 자체가 "일부 API는 모의투자에서 거부된다"는 메커니즘이 실재함을
  보여준다 — 미국주식이 그 대상인지는 **호출해봐야 안다**.
  - 조건검색 API 문서(`usa20280`)에 실제로 등장한 WS URL은
    `wss://mockapi.kiwoom.com:10000` 였다는 점은 최소한 "모의 엔드포인트
    자체는 준비돼 있다"는 신호이긴 하다. 다만 이것도 도메인 존재 ≠ 정상 동작.
- **[미확인]** 해외선물(`FS_JOB_TP`, 별도 "해외파생 Open API-W")은 이번 조사
  범위에서 REST API와는 무관한, 완전히 별개의 레거시 제품으로 보인다 (다운로드
  PDF `kiwoom_openapi_w_devguide_ver_1.0.pdf`) — TQQQ 같은 미국 "주식"과는
  관계 없음, 혼동하지 말 것.
- **TQQQ 관련 실전적 결론**: 코드베이스에 어댑터를 짤 때 해외주식 경로
  (`/api/us/*`)는 **모의투자로 먼저 실제 호출해서 200/return_code=0이 오는지,
  계좌에 해외주식 매매 신청이 별도로 필요한지부터 스파이크 테스트**해야 한다.
  포털 문서만 보고 "된다"고 가정하고 어댑터를 완성하면 안 된다 — ADR-0005가
  이미 정확히 이 우려를 기록해뒀다.

---

## 5. WebSocket 실시간 API — [확인됨]

**근거:** `kiwoom/core/ws_client.py` (공식 저장소, 실행 코드) +
`kiwoom_docs/실시간시세.md` (공식 저장소 문서).

### 5.1 접속 URL

- 실전: `wss://api.kiwoom.com:10000` + 경로 (국내주식은 `/api/dostk/websocket`)
- 모의: `wss://mockapi.kiwoom.com:10000` + 경로

공식 코드는 base URL을 환경변수로도 override 가능하게 만들어뒀다
(`W_PRD`/`W_MOCK` — 단, 이 환경변수명은 이 특정 오픈소스 래퍼의 자체 관례이지
키움 공식 문서의 이름은 아니다. **[미확인/래퍼 고유 컨벤션]**).

### 5.2 로그인 핸드셰이크 — [확인됨, verbatim]

접속 직후 클라이언트가 먼저 보낸다:

```json
{ "trnm": "LOGIN", "token": "<ACCESS_TOKEN>" }
```

서버 응답(로그인 ack)은 `trnm`이 `"LOGIN"`(대소문자 무관하게 비교됨)인 메시지로
오고, `return_code`(0=성공)를 검사해야 한다. 로그인 ack을 받기 전에 다른 메시지가
오면 클라이언트는 이를 프로토콜 위반으로 취급한다 (공식 코드: "login ack was
not received before other messages"라는 에러를 던짐 — 즉 로그인 ack이 항상
먼저 온다고 가정하면 안 되고 방어적으로 처리해야 함).

### 5.3 구독 (등록/해지) — [확인됨, verbatim — `kiwoom_docs/실시간시세.md`]

**등록 (REG):**

```json
{
  "trnm": "REG",
  "grp_no": "1",
  "refresh": "1",
  "data": [{ "item": ["005930"], "type": ["0B"] }]
}
```

- `grp_no`: 그룹번호 (최대 4자)
- `refresh`: `"1"` = 기존 등록 유지(기본값), `"0"` = 기존 등록 해지 후 신규 등록만
- `item`: 종목코드 배열 (거래소 접미사 포함 가능: `005930`, `005930_NX`, `005930_AL`)
- `type`: 등록 타입 코드 배열 (2자리, 아래 표)

**해지 (REMOVE):**

```json
{
  "trnm": "REMOVE",
  "grp_no": "1",
  "data": [{ "item": ["005930"], "type": ["0B"] }]
}
```

**등록 확인 응답:**

```json
{ "trnm": "REG", "return_code": 0, "return_msg": "" }
```

### 5.4 실시간 등록 타입 코드 — [확인됨, `실시간시세.md`]

| 코드 | 한글명                            |
| ---- | --------------------------------- |
| `00` | 주문체결                          |
| `04` | 잔고                              |
| `0A` | 주식기세                          |
| `0B` | 주식체결                          |
| `0C` | 주식우선호가                      |
| `0D` | 주식호가잔량                      |
| `0E` | 주식시간외호가                    |
| `0F` | 주식당일거래원                    |
| `0G` | ETF NAV                           |
| `0H` | 주식예상체결                      |
| `0J` | 업종지수                          |
| `0U` | 업종등락                          |
| `0g` | 주식종목정보 (상하한가/VI상태 등) |
| `0m` | ELW 이론가                        |
| `0s` | 장시작시간                        |
| `0u` | ELW 지표                          |
| `0w` | 종목프로그램매매                  |
| `1h` | VI발동/해제                       |

`00`(주문체결)과 `04`(잔고)는 종목코드(`item`)와 무관하게 **해당 계좌에 체결/잔고
변동이 생기면 자동으로 오는** 타입이라고 문서에 명시돼 있다.

### 5.5 실시간 데이터 프레임 — [확인됨, verbatim]

```json
{
  "trnm": "REAL",
  "data": [
    {
      "type": "0B",
      "name": "주식체결",
      "item": "005930",
      "values": {
        "20": "165208",
        "10": "-20800",
        "11": "-50",
        "12": "-0.24",
        "27": "-20800",
        "28": "-20700"
      }
    }
  ]
}
```

`values` 안의 키는 **숫자 코드**(레거시 OpenAPI+와 동일한 FID 체계로 보임)이고
필드명 매핑은 타입별로 다르다. 부호는 `+`/`-`/공백 접두사로 표현된다. 예시로
확인된 일부: `10`=현재가, `11`=전일대비, `12`=등락율(%), `13`=누적거래량,
`20`=체결시간(HHMMSS), `27`=최우선매도호가, `28`=최우선매수호가. 주문체결(`00`)은
`9201`=계좌번호, `9203`=주문번호, `913`=주문상태, `910`=체결가, `911`=체결량.
잔고(`04`)는 `930`=보유수량, `931`=매입단가, `950`=당일실현손익. **전체 FID
매핑표는 `kiwoom_docs/실시간시세.md` 원문에 더 있으니 실제 구현 시 그 문서를
직접 열어서 타입별 전체 목록을 확인할 것** — 여기 옮긴 건 예시일 뿐 전체가
아니다.

### 5.6 PING/PONG — [확인됨, verbatim, 매우 중요한 함정]

서버가 `{"trnm": "PING", ...}` (또는 순수 문자열 `"PING"`) 메시지를 보내면,
클라이언트는 **받은 메시지를 그대로(echo) 돌려보내야 한다.** 공식 코드:

```python
if _is_ping_message(parsed):
    await self._send_packet(parsed)  # 받은 그대로 재전송
```

WebSocket 프로토콜 레벨의 ping/pong(RFC 6455)은 명시적으로 꺼져 있다
(`ping_interval=None`으로 `websockets.connect` 호출) — 즉 **애플리케이션
레벨 JSON PING/PONG만 존재하고, 이걸 처리 안 하면 연결이 죽는다.** 이 로직을
빠뜨리는 게 가장 흔한 구현 실수일 것으로 보인다 — 지금 스켈레톤
(`quant/adapters/brokers/kiwoom/websocket.py`)의 `_dispatch_loop`에는 이 echo
로직이 아직 없다.

### 5.7 재연결 / 연결 제한 — [미확인]

- 공식 코드에는 자동 재연결 로직이 없다 — 연결이 끊기면 상위 호출자가 직접
  재연결·재로그인·재구독을 해야 한다 (지금 스켈레톤 주석이 지적한 우려가 맞다).
- 최대 동시 구독 종목 수, 커넥션당 그룹(`grp_no`) 개수 제한은 공개 문서에서
  수치를 찾지 못했다 — **[미확인]**. "키움 REST API 100개 제한?" 이라는 제목의
  한국어 유튜브 영상이 검색되는 걸 보면 실무에서 100개 안팎의 제한을 체감하는
  사용자가 있는 것으로 보이나, 영상 내용을 직접 확인하지 않았으므로 수치를
  본 문서에 확정 기재하지 않는다.

---

## 6. Python 래퍼 라이브러리 조사 — [확인됨: REST vs 레거시 OCX 구분]

| 저장소                                                                                    | REST or 레거시 OCX?                                         | WebSocket 지원             | 비고                                                                                                                         |
| ----------------------------------------------------------------------------------------- | ----------------------------------------------------------- | -------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| [Kiwoom-Securities/Kiwoom-REST-API](https://github.com/Kiwoom-Securities/Kiwoom-REST-API) | **REST (공식)**                                             | O (async/pubsub 양쪽 예제) | 키움증권 본사 명의 공식 저장소. 국내주식 206개 + OAuth 2개만 구현, 미국주식 없음. `kiwoomcli` CLI + OS 키체인 자격증명 저장. |
| [younghwan91/kiwoom-rest-api](https://github.com/younghwan91/kiwoom-rest-api)             | REST (서드파티)                                             | O                          | "207개 엔드포인트 + 실시간 WebSocket" 표방. 모의(`is_mock=True`)/실전 전환 지원.                                             |
| [bamjun/kiwoom-rest-api](https://github.com/bamjun/kiwoom-rest-api)                       | REST (서드파티)                                             | O                          | `api.kiwoom.com`/`mockapi.kiwoom.com` 둘 다 base URL로 지원.                                                                 |
| [kiwoom-restful (PyPI)](https://pypi.org/project/kiwoom-restful/)                         | REST (서드파티)                                             | O                          | v0.4.0, RC 단계. Async HTTP + WS 논블로킹 콜백.                                                                              |
| [dongbin300/KiwoomRestApi.Net](https://github.com/dongbin300/KiwoomRestApi.Net)           | REST (서드파티, **.NET**, Python 아님)                      | 문서상 언급 있음           | 참고용, 우리 스택(Python)과 무관.                                                                                            |
| [sharebook-kr/pykiwoom](https://github.com/sharebook-kr/pykiwoom)                         | **⚠️ 레거시 OCX/OpenAPI+ (Windows COM)**                    | 레거시 방식                | `CommConnect()`, `GetMasterCodeName()` 등 COM 패턴 확인 — **이건 REST API가 아니다. 절대 이 패턴으로 구현하면 안 됨.**       |
| [breadum/kiwoom](https://github.com/breadum/kiwoom)                                       | **⚠️ 레거시 OCX/OpenAPI+ (Windows COM), "심플 라이브러리"** | 레거시 방식                | 마찬가지로 구형 Windows 전용 OCX 래퍼로 보임 — REST와 무관, 오검색 주의.                                                     |
| [me2nuk/stockOpenAPI](https://github.com/me2nuk/stockOpenAPI)                             | **⚠️ 레거시 OCX로 추정** (이름 자체가 구형 "OpenAPI")       | —                          | 상세 미조사, 이름 패턴상 레거시일 가능성 높음. 사용 전 직접 확인 필요.                                                       |

**결론:** 참고할 만한 REST 래퍼는 위 4개(공식 + younghwan91 + bamjun +
kiwoom-restful)이고, 나머지는 2025년 이전 Windows 전용 OCX/OpenAPI+
(`CommConnect`, `SetInputValue`, `CommRqData` 같은 COM 메서드가 특징) 계열이라
**이번 REST 어댑터 구현에는 참고 가치가 없다.**

---

## 7. 출처 목록

- https://github.com/Kiwoom-Securities/Kiwoom-REST-API (공식 저장소 — 이 문서의 핵심 근거)
  - `kiwoom/core/auth.py`, `kiwoom/core/client.py`, `kiwoom/core/ws_client.py`,
    `kiwoom/core/errors.py`, `kiwoom/specs.py`,
    `kiwoom/_data/kiwoom_api_spec.json`
  - `kiwoom_docs/실시간시세.md`, `주문.md`, `시세.md`, `계좌.md`, `차트.md`
  - `examples/OAuth 인증/접근토큰발급/create_access_token.py`
- https://openapi.kiwoom.com/ , https://openapi.kiwoom.com/guide/index?dummyVal=0
- https://openapi.kiwoom.com/m/guide/apiguide?jobTpCode=03 (국내주식 가이드)
- https://openapi.kiwoom.com/m/guide/apiguide?jobTpCode=15 (실시간/조건검색 가이드)
- https://openapi.kiwoom.com/m/guide/apiguide?jobTpCode=34,35,36,37,38,39 (미국주식 가이드 — 업종/순위/차트/조건검색/주문/투자정보)
- https://openapi.kiwoom.com/assist/assist0202 (FAQ — AI 코딩 어시스턴트 위주, REST 인프라 관련 FAQ는 못 찾음)
- https://github.com/younghwan91/kiwoom-rest-api
- https://github.com/bamjun/kiwoom-rest-api
- https://pypi.org/project/kiwoom-restful/
- https://github.com/sharebook-kr/pykiwoom (레거시 OCX — 참고용 반례)
- https://github.com/breadum/kiwoom (레거시 OCX로 추정 — 참고용 반례)
- https://algolab.co.kr/blog/kiwoom-rest-api-algotrading-guide-2026 (정황/함정 설명용, 필드명 근거로는 미사용)
- https://www.pabburi.co.kr/content/php/키움증권-rest-api-접근토큰-발급/ (토큰 발급 curl 예제)
- https://www.pabburi.co.kr/content/php/키움증권-rest-api-실시간시세조회-및-조건검색/

---

## 8. 구현 시 주의점

1. **토큰 응답 필드명 — 지금 스켈레톤이 잘못 가정하고 있다.** `client.py`의
   `_fetch_token()`은 `body["token"]`은 맞게 짚었지만 `body.get("expires_in", 86400)`로
   "초 단위 TTL"을 가정한다. 공식 스펙과 공식 코드 둘 다 실제 필드는
   **`expires_dt`(만료 "일시" 문자열, `YYYYMMDD HHMMSS`류 포맷, KST)**이다.
   `expires_in`이라는 필드 자체가 존재하지 않을 가능성이 높다 — 이 부분은
   실키 발급 즉시 가장 먼저 검증하고 고쳐야 할 지점이다. (일부 2차 소스는
   일반적인 OAuth2 관행을 따라 `access_token`/`expires_in`이라고 얘기하기도
   했는데, 이건 키움 공식 소스와 상충하며 공식 소스를 신뢰해야 한다.)
2. **`authorization` 헤더를 토큰 발급 요청에 넣지 말 것.** 스펙 메타데이터의
   "필수" 표시는 템플릿 아티팩트다. 공식 코드는 `Content-Type`만 보낸다.
3. **`Bearer` 접두사는 고정 문자열로 직접 조립.** 서버가 준 `token_type`
   값을 그대로 쓰지 말 것 (대소문자·값 자체를 신뢰하지 않는 게 공식 구현
   방침).
4. **해외주식(미국주식) 지원 여부가 가장 큰 미해결 리스크.** 포털 문서에는
   `/api/us/*` 경로와 `usa*`/`ust*` api-id가 있지만, 키움 공식 GitHub SDK에는
   전혀 구현돼 있지 않다. 모의투자에서 실제로 동작하는지, 별도 해외주식
   거래 신청이 계좌에 필요한지 **실키로 스파이크 테스트 없이는 절대 확정
   짓지 말 것.** TQQQ 어댑터를 만들기 전에 반드시 `/api/us/rkinfo`나
   `/api/us/invtinfo`처럼 부작용 없는 조회성 API부터 모의투자로 먼저 찔러봐야
   한다.
5. **WebSocket PING 에코를 반드시 구현.** 프로토콜 레벨 ping/pong이 꺼져 있고
   애플리케이션 레벨 `{"trnm": "PING", ...}` 메시지를 그대로 되돌려 보내지
   않으면 연결이 유지되지 않을 가능성이 높다. 현재
   `quant/adapters/brokers/kiwoom/websocket.py`의 `_dispatch_loop`에는 이 로직이
   없다 — 반드시 추가해야 한다.
6. **WS 로그인 ack 순서를 가정하지 말 것.** 로그인 ack이 항상 다른 메시지보다
   먼저 온다고 하드코딩하면 안 된다 (공식 코드도 이걸 방어적으로 처리).
7. **모의/실전 앱키를 절대 섞어 쓰지 말 것.** 에러 `8030`/`8031`이 이를 위한
   전용 에러코드로 존재한다 — 즉 키움 서버가 이 실수를 적극적으로 감지해서
   막는다는 뜻이고, 흔한 실수라는 방증이기도 하다.
8. **레거시 OCX 자료와 절대 혼동하지 말 것.** "키움 Open API+"(대문자 API+,
   `download.kiwoom.com`의 devguide PDF, `CommConnect`/`SetInputValue` 같은
   COM 메서드, Windows 전용)는 2025년 이전 완전히 별개의 구형 제품이다. 이번
   REST API(`openapi.kiwoom.com`, `api.kiwoom.com`, `api-id` 헤더 방식)와는
   프로토콜이 전혀 다르다. `sharebook-kr/pykiwoom`, `breadum/kiwoom`은 전부
   레거시 쪽이니 코드 패턴을 베끼면 안 된다.
9. **해외선물(OpenAPI-W)과 해외"주식"을 혼동하지 말 것.** "해외파생 Open API"는
   선물/옵션 전용 별도 제품이고 TQQQ 같은 미국 상장 주식과 무관하다.
10. **`return_code`를 HTTP 상태코드보다 우선 신뢰.** 200 OK가 와도 바디의
    `return_code != 0`이면 실패다 — 이 컨벤션은 토큰 발급까지 포함해서 전
    API에 공통.
11. **연속조회(`cont-yn`/`next-key`)는 응답 "헤더"에서 읽어야 한다.** 응답
    바디 안에서 찾으면 안 된다 — 공식 클라이언트가 HTTP 헤더에서 읽는 걸
    직접 확인했다.
12. **Rate limit 수치는 비공개.** TR별/전체/그룹별 3중 제한이 존재한다는
    것(에러코드 1700/1701/1702)만 확인됐고 정확한 초당 허용치는 공식 문서에
    없다. 서킷브레이커/백오프를 TR 단위로 두는 게 안전하다.
13. **`stex_tp`(해외) / `dmst_stex_tp`(국내) 값 체계가 다르다.** 국내는
    `KRX`/`NXT`/`SOR` 3분류가 확인됐지만 해외 `stex_tp`의 `NA`/`ND`/`NY` 정확한
    대응(어느 게 NASDAQ/NYSE/AMEX인지)은 미확인이니 실키로 확인 후 상수화할 것.
