"""1분봉 스캘프 전략 — 조기 진입 + 확실한 이익 실현. news_scalp(A)/intraday_scan(C)와
같은 관심종목 유니버스에서 **진입 방식만 달리해** 병행 운용, 채점으로 승부한다.

## 스펙 근거 (2026-08-18 사용자 지시)

`docs/superpowers/specs/2026-08-18-scalp-1m-design.md` 전문. 실측 사례(SOXL
08-17): 개장 22:30 대량 매수 폭발을 intraday_scan(5분봉)이 무시하고 개장+80분에
세션 신고가를 추격 진입, 그마저 그날 고점이었다 — "진입이 구조적으로 늦고,
이익이 나도 실현을 못 한다"가 문제 정의다. 5분봉 대기 진입을 버리고 1분봉
조기 진입 + 부분 익절로 답한다.

## 진입 (전부 1분봉, 결정론) — 개장 후 entry_window_minutes_after_open(기본 90분)만

- **패턴 A — 개장 되돌림 higher-low 재돌파**:
  1. P1 = 개장 후 세션 고점(오늘 봉들 중 마지막 완성봉 이전까지의 최고가).
     P1 형성 구간(개장~P1봉)에 거래량 서지 동반 상승봉(종가>시가, 거래량 >=
     volume_surge_mult x 직전 volume_surge_lookback봉 평균)이 존재해야 한다.
  2. L1 = P1 이후 되돌림 저점(P1봉 이후~돌파봉 이전 구간의 최저가). 시가 아래로
     뚫리면(L1 <= 시가) 패턴 무효.
  3. 마지막 완성봉 종가가 P1을 재돌파하면 진입.
- **패턴 B — 60선(1분) 지지 반등** (재진입·후속 진입용 — A가 이미 쓰인 뒤에만
  평가한다. 스펙이 B를 "재진입용"으로 명시한 것의 가장 직접적인 코드화):
  가격이 MA60까지 되돌림 → 그 봉의 저가가 MA60을 허용 오차(ma_tolerance_pct,
  기본 -0.2%) 안에서 지킴 → 다음 봉이 양봉(종가>시가) 마감 → 진입.
- 세션당 심볼당 총 진입 2회 상한(A 1회 + B 1회) — 보유 중(내 lot qty>0)이면
  신규 진입 평가 자체를 건너뛴다(순차 진입: A 청산 후에야 B를 시도할 수 있다).
- 거래량 평균(volume_surge_lookback)과 MA60은 **세션 경계를 넘는 전체 lookback
  시계열**(오늘 봉만이 아니라 `ctx.data.history`가 준 연속 1분봉) 기준으로
  계산한다 — confluence.py의 SMA 계산과 동일한 이유(개장 직후 워밍업 부족을
  피한다). 다만 "P1 형성 구간에 거래량 서지가 존재하는가"의 탐색 범위 자체는
  **오늘 봉만**이다(스펙 "P1 형성 구간" = 오늘 개장 이후).

## 청산 (이익 실현 확실화 — 이 전략의 존재 이유 절반)

1. **초기 손절**: 진입 근거 저점(L1 또는 MA60 값) × (1 − stop_buffer_pct(기본
   0.3%)/100). **하드 캡** stop_hard_cap_pct(기본 3.0% — news_scalp와 동일한
   꼬리 차단): 손절가 = max(위 값, 진입가 × (1 − 하드캡/100)) — 진입가에 더
   가까운(손실이 더 작은) 쪽을 쓴다.
2. **절반 익절**: R = 진입가 − 손절가(**최초** 손절가 — 아래 5번이 스탑을
   올려도 R 은 진입 시점 값으로 고정, 랏의 `r0`). 가격이 진입가 +
   partial_take_r(기본 1.5)×R에 도달하면 보유의 partial_fraction(기본 50%)
   시장가 청산 — 세션당 1회만(랏의 `partial_taken` 플래그).
3. **잔량 트레일**: 최근 완성 1분봉 **종가**가 MA60 아래로 마감하면 전량 청산
   (부분 익절 여부와 무관 — 아직 부분 익절 전이면 전량, 후면 잔량). MA60 위에서는
   계속 보유한다.
5. **본전 이동 + 고수위 트레일**(2026-08-27, 실측 근거는 생성자 주석): 미실현이
   breakeven_at_bp 에 닿으면 스탑을 진입가로, trail_bp 가 켜져 있으면 고수위
   (관측 최고가) − trail_bp 로 스탑을 **단조 상향**한다. 스탑이 진입가 이상으로
   올라온 뒤의 이탈은 "이익보호 청산"으로 표기한다(손절이 아니다). 고정
   take_profit_bps 와 달리 상방을 자르지 않는다 — "이득 볼 땐 많이"의 구현.
4. **당일 청산 고정**: EoD 강제청산(flatten_before_close_minutes) + 세션 롤
   오버나잇 금지 레일 — 다른 전략과 동일한 이중 보장.

## 일봉 추세/변동성 게이트 (2026-08-19 QuantConnect #407/#478 필터 이식)

전략 통째 이식은 불가(#407은 유니버스 1000종목·펀더멘털 데이터·공매도, #478은
scipy 기반 양자보행 최적화 — 둘 다 이 환경에 없다)라 **이식 가능한 필터 두 개만**
가져온다. 판정 로직은 `quant/trade/indicators/trend_gate.py`(순수 함수, 그 모듈
docstring 참고) — 여기서는 배선만 한다.

1. **추세 게이트**(#407 근거): 일봉 ADX(`adx_min`, 기본 25) + DI 확인
   (`require_di`, 기본 켜짐 — +DI>-DI). 미충족이면 패턴 A/B/프리마켓 진입
   판정 **직전**에 거부, `last_reject`에 "추세 게이트 미충족(ADX=..)" 기록.
2. **변동성 게이트**(#478 근거): 일봉 ATR/가격 비율이 `max_atr_ratio`(기본
   0.10)를 넘으면 거부, "변동성 과다(ATR/price=..)" 기록.
3. 일봉은 `ctx.data.history(symbol, "1d", 40)`으로 **세션당 심볼 1회만**
   조회하고 `_gate_cache`에 캐시한다(`_get_bars`의 분 경계 캐시와 별개 —
   일봉은 장중에 안 바뀌므로 세션 전체 재사용, 세션 롤 시 리셋). 조회 실패/
   봉 부족(`adx_di`/`atr_ratio`가 None)이면 게이트를 **통과**시킨다(기존
   동작 보존 — trend_gate.py 모듈 docstring "게이트 부재" 절과 동일 원칙,
   일봉 데이터 장애가 조용히 전략을 무력화하지 않도록).
4. `trend_gate_enabled`(기본 true)로 완전히 끌 수 있다. adx_min/max_atr_ratio
   값은 QuantConnect #407/#478에서 그대로 가져온 것으로 **이 저장소 실데이터로
   검증된 값이 아니다**(burn-in 전).
5. KR/US 대칭 — 시장 구분 없이 동일 적용(일봉 조회는 market 무관하게
   `ctx.data.history`가 처리).

## 프리마켓 (2026-08-18 사용자 결정 — KR 도입, 2026-08-18 US 대칭 확장)

KR 대체거래소 NXT 프리마켓(08:00~08:50 KST)과 US 프리마켓 마지막 구간(08:00~09:25
ET — 04:00 전체가 아니라 개장 직전 유동성 있는 90분만, "데이터" 절 실측 근거)을
같은 구조로 쓴다. `is_market_open`은 정규장만 True라 그대로 두면 이 구간이 통째로
스킵된다 — `_market_active`/`_premarket_window_state`가 `_PREMARKET_WINDOWS`에
등록된 시장(KR/US) 한정으로 프리마켓~개장을 "확장 관찰 창"으로 추가 인정한다
(정규장 게이트 자체는 건드리지 않는다). 각 시장의 창 끝~정규장 개장(KR
08:50~09:00, US 09:25~09:30)은 유동성이 가장 얇고 스프레드가 넓은 구간이라
신규 진입을 걸어 잠그고 관리만 계속한다("블랙아웃").

1. **관찰 입력(정규장 진입 가속)**: 프리마켓 구간에서 (a) 거래량 서지(직전
   20봉 평균 3배) 동반 상승봉이 고점 P_pre를 만들고 (b) 그 뒤 프리마켓 마감까지
   저가가 시가 아래로 뚫리지 않으면 "프리마켓 확인"으로 마킹한다(블랙아웃 진입
   시점에 프리마켓 봉 전체를 놓고 1회 확정 — 세션당 한 번만 판정한다). 정규장
   개장 후 이 마킹이 있는 심볼은 패턴 A에서 P1을 새로 기다리지 않고 P_pre를
   P1로 인정한다(`_check_pattern_a_accelerated`) — 되돌림(higher-low)+재돌파만
   오늘 정규장 봉에서 확인하면 되므로 지각 진입이 개장 직후로 당겨진다.
   되돌림이 정규장 시가 아래로 뚫리면 기존 규칙대로 무효.
2. **직접 진입**: `premarket_entry`(기본 true)가 켜져 있으면 프리마켓 구간
   중에도 패턴 A(그 구간 안에서 자체적으로 P1/L1/재돌파가 모두 성립)가 완성되고
   유동성 가드(`premarket_min_volume_krw`(KR)/`premarket_min_volume_usd`(US) —
   마지막 완성 프리마켓 1분봉의 거래대금 = 종가×거래량)를 통과하면 즉시
   진입한다. 패턴 B는 프리마켓에 평가하지 않는다(스펙: "패턴 A 성립"만 언급 —
   B는 재진입용이라 정규장 60선 맥락이 필요하다). 관리(손절/부분익절/60선
   트레일/EoD)는 정규장 진입과 동일 — 60선은 프리마켓+정규장 연속 시계열
   (`ctx.data.history`가 이미 그렇게 준다)로 계산되므로 별도 배선이 필요 없다.
   세션당 진입 2회 상한(A+B)은 프리마켓·정규장 합산이다(같은
   `_pattern_a_used`/`_pattern_b_used` 플래그를 공유하기 때문에 저절로 성립한다).
3. **주말만 거른다**: `_premarket_window_state`는 순수 시각 기준이라(캘린더
   미조회) 토·일요일만 걸러내고 공휴일·조기폐장은 모른다 — 그런 날은 프리마켓
   봉 자체가 비어 있어(`bars.empty`) 안전하게 아무 일도 하지 않는다(크래시도,
   오판도 없다).
4. **배선(risk 승인 게이트)**: 여기서 만든 ENTER_LONG 신호는
   `quant.trade.risk.manager.RiskManagerImpl.approve()`의 `is_market_open` 게이트
   (전략과 무관하게 전 종목·전 전략 공용)를 그대로 통과해야 실제 주문이 된다.
   `config/settings.yaml`의 `risk.extended_sessions.scalp_1m`에 KR(08:00-08:50)·
   US(08:00-09:25) 창이 등록돼 있으면(2026-08-18 기준 둘 다 등록됨) 그 게이트를
   통과한다 — 등록되지 않은 (전략, 시장) 조합은 여전히 막힌다. 창 시각은 그
   시장의 현지 시각으로 판정된다(`risk/manager.py` `_MARKET_TZ`, DST 안전).
   관찰 입력(1번)은 이 게이트와 무관하게 정상 동작한다 — 실제 주문은 정규장
   개장 이후에 나가기 때문이다.
5. **US 데이터 실측(2026-08-18, EC2)**: Toss 1m 캔들이 SOXL 기준 ET 프리마켓
   구간(04:00~09:30)에서 연속 1분봉·실거래량으로 확인됨(예: 2026-08-17 ET
   17:0x대 300~1만주대 거래량) — 정지가/스테일 데이터가 아니라 실제 체결이
   반영된 봉이다. 04:00 전체가 아니라 개장 직전 90분만 인정하는 이유는 근거
   없는 임의값이 아니라 "유동성이 특히 얇은 이른 새벽 구간을 배제"하는 보수적
   선택이다(백테스트로 검증된 값은 아니다 — paper 번인이 검증 경로).

## KR 개장 초반 진입 지연 게이트 (2026-09-02, 문헌×원장실측 교차확인)

원장 실측(138왕복 분해, `data/state/trades.jsonl`)을 진입 시각(KST)별로 나누면:
08시(프리마켓)대 13왕복 승률15% −605,764원, 09시대 46왕복 승률30% −1,077,172원,
반면 13시대는 승률50% +16,999원 — 전체 손실 −163만원 중 −158만원이 KR 08~09시
진입에 몰려 있다. 문헌(개장 첫 20~30분 변동성·거래량 서지 3~4배, 진입 승률
급락 38%→이후 54% — LuxAlgo 개장 변동성 리서치, arXiv:1005.3535)도 같은 방향을
가리킨다 — 리서치와 원장 분해가 같은 곳을 가리킨 것만 적용한다는 원칙에 따라
개장 초반 KR 신규 진입에 지연 게이트를 건다.

- **KR 심볼에 한해** 정규장 신규 진입(패턴 A/B)을 개장 후
  `kr_entry_open_delay_min`(기본 30)분 동안 차단한다(`on_cycle` 2)단계, 진입창
  상한 `entry_window_minutes`와는 별개의 **하한** 축이다).
- KR 프리마켓(08:00~08:50 NXT) 직접 진입은 이미 2026-08-26 소유자 교정으로
  구조적으로 막혀 있다(`_PREMARKET_DIRECT_ENTRY_MARKETS`가 US만 포함 — KR
  동시호가는 체결이 실재할 수 없는 거래라는 게 그 근거). 그래서 이 게이트가
  실제로 다루는 신규 주문 경로는 09:00~09:30 KST 구간이지만, 위 실측의 08시
  손실 표본은 그 교정 **이전**(과거 프리마켓 직접 진입이 아직 막히지 않았던
  시기) 거래가 섞여 있어 "KR 개장 초반 전체가 나쁘다"는 같은 결론을 보강하는
  근거로 함께 인용한다 — 08:00~08:50 NXT 직접 진입을 다시 여는 것이 아니다.
- **US는 건드리지 않는다** — US 프리마켓·개장 실측은 본전권이라 근거 없이
  건드릴 이유가 없다. 게이트는 `market == "KR"`에서만 평가된다(시장별 분기).
- **청산·손절은 시간대와 무관하게 동작한다** — 이 게이트는 `on_cycle` 2)단계
  (신규 진입 평가)에만 걸리고, 1)단계(포지션 관리: 손절/부분익절/60선 트레일/
  EoD)는 그대로 돈다 — "진입은 미뤄도 청산은 아니다" 비대칭(2026-09-02 배포
  원칙과 동일).
- `kr_entry_open_delay_min: 0`이면 기존 동작과 100% 동일(하위호환·실험 롤백
  가능). 값은 문헌·원장 둘 다 정확히 검증한 최적값이 아니라 "개장 첫 20~30분"
  이라는 문헌 폭에 맞춘 보수적 기본값이다.

## 5초 루프 상호작용 — 같은 1분봉 안에서 중복 주문이 나지 않는 이유

패턴 A/B 사용 플래그(`_pattern_a_used`/`_pattern_b_used`)는 **신호 생성 시점에
즉시**(체결 확인을 기다리지 않고) 세팅된다 — news_scalp의 `_entered_today`와
동일한 낙관적 마킹이다. 같은 사이클에 place_order가 곧바로 불려 paper/live
둘 다 사실상 동기 체결이므로(loop.py: 주문 생성 직후 같은 사이클에서 실행),
다음 5초 사이클이 도는 시점엔 이미 플래그가 서 있어 같은 완성봉이 사이클마다
재평가돼도 두 번째 신호가 나지 않는다. 부분 익절의 `partial_taken`도 같은
원리(news_momentum과 동일 패턴)로 안전하다 — `tests/test_scalp_1m.py`의
"5초 루프" 절이 이 상호작용을 직접 고정한다.

## 데이터 — Toss 1m 라이브 경로 확인 (추측 아님, 코드 추적)

`ctx.data.history(symbol, "1m", n)`은 `TossDataFeed.history()`(`quant/adapters/
brokers/toss/datafeed.py`)에서 `interval == "1m"`이면 `_load_1m()`이 증분
캐싱한 1분봉을 그대로 돌려준다 — 이미 라이브 경로에 배선돼 있다(추가 배선
불필요). `_load_1m`의 신선도 TTL은 55초, 엔진 루프는 5초 폴링이라 1분 판단에
충분하다(스펙 "데이터" 절). Toss 1분봉은 4거래일 롤링만 제공하므로(docs/
data-availability.md) 백테스트 표본이 없다 — paper 번인이 유일한 검증 경로다
(`server/scripts/backfill_1m.sh`가 매일 표본을 쌓는다).

## 조회 최적화 (2026-08-18 EC2 실측 — 사이클당 2.5초/16종목)

5초 폴링마다 유니버스 전 종목의 1m history를 Toss REST로 재조회하면 낭비가
크다: (1) 1분봉은 분당 1개만 새로 생기는데 5초마다 조회 = 최대 12배(60초/5초)
과다 조회, (2) 세션이 닫힌 시장의 심볼은 조회해도 봉이 바뀌지 않는다. 세 겹으로
줄인다:

1. **세션 게이트**: 포지션 관리(1단계)도 심볼의 시장이 `ctx.clock.is_market_open`
   가 아니면 통째로 건너뛴다(기존엔 진입 판정만 게이트돼 있었다) — 닫힌 시장
   심볼은 조회 0회.
2. **분 경계 캐시**(`_get_bars`/`_bars_cache`): 심볼당 마지막 조회 분(그 시장
   tz 기준)을 기억하고, 같은 분 안의 반복 사이클은 캐시를 그대로 재사용한다
   (1분봉 판단이므로 정보 손실 0) — 진입 판정(`_check_entry_for`)과 60선 트레일
   관리(`_manage_position`)가 캐시를 공유한다. 세션 롤 시 리셋.
3. **창 밖 최적화**: 진입창(기본 90분)이 지나고 보유 포지션도 없는 심볼은
   1단계(관리 대상인 열린 랏이 없음)·2단계(창 밖이라 진입 평가 자체를 안 함)
   양쪽 모두에서 자연히 조회되지 않는다. 보유 중이면 분 경계 조회(위 2)로
   청산 관리(손절/EoD/60선 트레일)는 계속된다.

효과(세션 중인 시장 기준, 16종목 예시): 종전 최대 16종목 × 12회/분(닫힌 시장
포함, 매 5초 사이클 재조회) → 세션이 열린 시장의 심볼만 × 최대 1회/분(분 경계
캐시로 새 분 진입 시에만 재조회).

## 비목표 / 정직성

기존 전략 수정 없음(비교 대상 보존), VWAP·DMI 등 추가 지표 없음(v1은 사용자
패턴+60선+거래량뿐). 근거 사례는 사후 확증 1건뿐이다 — 이 전략이 이길 것이라는
증거는 아직 없다. 개장 직후는 스프레드 최악 구간이라 paper 체결가와 실호가
괴리가 백테스트 부재와 함께 가장 큰 미지수다.
"""
from __future__ import annotations

from datetime import date as dtdate, datetime, time as dtime

import pandas as pd

from quant.core.ports import Context
from quant.core.models import Position, Signal, SignalAction, market_of_symbol
from quant.core.strategy_api import DataNeeds, Decision, StrategySnapshot
from quant.trade.fmt import fmt_price
from quant.trade.indicators import sma
from quant.trade.indicators.trend_gate import adx_di, atr_ratio
from quant.trade.structure import structure_bracket, williams_r
from quant.trade.strategy import kernel
from quant.trade.strategy.orb_scan import _SESSION_OPEN
from quant.trade.strategy.shell import PureStrategyShell

_INTERVAL = "1m"

# 프리마켓 확장 관찰 창 — 시장별(market-local time, 모듈 docstring "프리마켓" 절).
# 등록되지 않은 시장은 확장 창 자체가 없다(정규장만, 기존 동작). 정규장 개장
# 시각(_SESSION_OPEN)은 orb_scan에서 그대로 가져온다 — 여기 값은 그 이전 구간만
# 정의한다. 각 항목 = (프리마켓 시작, 블랙아웃 시작). 블랙아웃~정규장 개장은
# 신규 진입 없이 관리만(유동성이 가장 얇은 구간).
#   KR: 08:00~08:50 (NXT, Toss 1m 봉 실측 — scalp_1m.py 모듈 docstring)
#   US: 08:00~09:25 ET (개장 직전 90분만 — 04:00 전체 중 유동성 있는 마지막
#       구간, "데이터" 절 실측 근거. KR과 폭을 맞추지 않은 이유는 US 정규장
#       개장이 09:30이라 90분 폭이 자연스러운 대칭이기 때문)
_PREMARKET_WINDOWS: dict[str, tuple[dtime, dtime]] = {
    "KR": (dtime(8, 0), dtime(8, 50)),
    "US": (dtime(8, 0), dtime(9, 25)),
}

# 정규장 길이(분) — KR 09:00~15:30, US 09:30~16:00 둘 다 390분. 전-세션 진입
# 모드(entry_window_minutes=0)의 lookback 산정에 쓴다.
_FULL_SESSION_MINUTES = 390

# **프리마켓에서 직접 주문을 낼 수 있는 시장** (2026-08-26 소유자 교정).
#
# US 프리마켓(04:00~09:30 ET)은 호가가 실시간으로 체결되는 **연속 세션**이다.
# 한국장에는 그런 구간이 없다:
#   08:30 이전 — 거래 자체가 없다.
#   08:30~08:40 장전 시간외 종가 — 전일 종가 고정가(가격 발견 없음).
#   08:30~09:00 장 시작 동시호가 — 주문만 모으고 09:00 정각에 시가로 일괄 체결.
#
# 그래서 KR 프리마켓 "체결"은 실재할 수 없는 거래다. 2026-08-26 에 엔진이
# 08:27·08:46 에 진입을 기록했고, 09:00 시가 갭이 손절선(-1.0%)을 2.8% 지나쳐
# 의도한 -1% 손실이 -3.8% 가 됐다(000720 -165,356원). 페이퍼 브로커는 피드가
# 준 가격이면 무엇이든 체결시키므로 이 오류를 스스로 못 잡는다.
#
# **관찰은 KR 에서도 계속한다** — P_pre 마킹은 주문을 내지 않고, 정규장 진입의
# 재료일 뿐이다(그 재료의 품질은 별개 문제로 남겨 둔다: 동시호가 예상체결가는
# 실제 체결가가 아니다).
_PREMARKET_DIRECT_ENTRY_MARKETS = frozenset({"US"})


class Scalp1mStrategy:
    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "scalp_1m"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market  # Strategy Protocol 호환용 — 실제 판정은 심볼별 시장 추론

        # 개장 후 이 시간(분) 안에만 신규 진입 판정. **0 = 진입창 없음(전 세션
        # 대기)** — 2026-08-26 소유자 지시: "단타 스캘핑은 언제든 해도 좋아,
        # 꼭 장 시작 90분이 아니더라도 언제든 시그널을 계속 대기". 양수면 기존
        # 의미(개장 후 N분) 그대로 — 미설정 시 동작 보존을 위해 기본값은 90 유지,
        # 전-세션은 settings.yaml 에서 0을 명시한다. 창 밖은 관리만 계속한다.
        self.entry_window_minutes: int = params.get("entry_window_minutes_after_open", 90)
        # KR 개장 초반 진입 지연(모듈 docstring "KR 개장 초반 진입 지연 게이트"
        # 절, 2026-09-02 원장×문헌 교차확인). US는 무관 — market == "KR"에서만
        # 평가한다. 0 = 비활성(기존 동작), 기본 30(문헌 "개장 첫 20~30분").
        self.kr_entry_open_delay_min: float = params.get("kr_entry_open_delay_min", 30)
        # 패턴 A — 거래량 서지 배수/lookback(스펙: "직전 20봉 평균의 3배 이상").
        self.volume_surge_mult: float = params.get("volume_surge_mult", 3.0)
        self.volume_surge_lookback: int = params.get("volume_surge_lookback", 20)
        # 패턴 B — 60선(1분) 지지 허용 오차(스펙: "-0.2%").
        self.ma_period: int = params.get("ma_period", 60)
        self.ma_tolerance_pct: float = params.get("ma_tolerance_pct", 0.2)
        # 초기 손절 버퍼(스펙: "L1 -0.3%") + 하드 캡(스펙: "-3%, 기존 news_scalp와
        # 동일한 꼬리 차단").
        self.stop_buffer_pct: float = params.get("stop_buffer_pct", 0.3)
        self.stop_hard_cap_pct: float = params.get("stop_hard_cap_pct", 3.0)
        # 절반 익절(스펙: "+1.5R 도달 시 보유의 50% 시장가 실현").
        self.partial_take_r: float = params.get("partial_take_r", 1.5)
        # 전량 익절 문턱(bp, 진입가 대비). 0 = 비활성(기본) — 미설정이면 동작이
        # 지금과 100% 같다.
        #
        # 2026-08-21 원장 재생(scalp_1m 18건): 현행 평균 -58.0bp/승률 22% →
        # 전량 익절 +100bp·손절 -100bp 는 평균 -5.1bp/승률 50%. **절반 익절
        # (+100bp, 50%)은 오히려 평균 -67.5bp 로 나빴다** — 남긴 절반이 마감까지
        # 끌려가 되돌림을 그대로 맞는다(같은 표본 '마감까지 보유' 평균 -194.8bp).
        # 기존 partial_fraction 은 코드가 0<x<1 로 강제해 전량 익절을 표현할 수
        # 없으므로 별도 경로를 둔다.
        self.take_profit_bps: float = params.get("take_profit_bps", 0)
        # 본전 이동 + 고수위 트레일(bp). 0 = 비활성(기본) — 미설정이면 동작이
        # 지금과 100% 같다. 켜는 건 settings.yaml.
        #
        # 2026-08-27 원장 66건 실측: 패자 29건(재생 가능분) 중 12건(41%)이 보유 중
        # +50bp 를 찍고도 손실로 끝났고(반납형), 승자 17건은 실현 중앙 +94bp 에서
        # 끊기는데 세션 MFE 중앙은 +342bp — 고정 TP 가 상방을 자른다. 반사실
        # 시뮬(57트립, 종가 판단, 왕복 20bp 차감, 탐색 6회 사전 고지):
        # BE50+트레일70 평균 -19.5bp vs 현행 근사(고정 TP100) -28.1bp → 건당
        # +8.6bp. 인샘플 반사실이므로 절대 성과 주장이 아니라 **규칙 간 우열**
        # 근거로만 쓴다 — 최종 판정은 experiments(DiD)가 한다.
        self.breakeven_at_bp: float = params.get("breakeven_at_bp", 0)
        self.trail_bp: float = params.get("trail_bp", 0)
        self.partial_fraction: float = params.get("partial_fraction", 0.5)
        # 손절 모드(2026-08-26 구조층 재작업 — quant/trade/structure.py):
        #   "basis"     — 기존: 패턴 기준가(L1/MA60) × (1-buffer), 하드캡 바닥.
        #   "structure" — 최근 스윙 저점(지지) × (1-buffer). 지지가 안 보이면
        #                 **진입하지 않는다** — "손절선을 정할 수 없는 자리"는
        #                 그 자체가 위험 정보다(structure.py 손절 철학).
        # 근거: 2026-08-24 원장 재생 — ±100bp 고정 브래킷은 세션 내 67%가 양쪽
        # 다 터치(노이즈 안). 구조 손절도 stop_hard_cap_pct 로 잘리므로 최대
        # 손실 폭은 기존과 같다. 기본 basis(미설정 시 무동작 보존) — 전환은
        # settings.yaml 에서 명시한다.
        smode = str(params.get("stop_mode", "basis")).strip().lower()
        self.stop_mode: str = smode if smode in ("basis", "structure") else "basis"
        self.structure_wing: int = int(params.get("structure_wing", 3))
        # Williams %R 과열 게이트(구조층 재작업 ②) — trend_gate 와 같은 3모드.
        # shadow 는 판정만 계산해 신호 사유에 싣는다(표본이 모이면 "차단 후보
        # 진입들의 실제 성적"으로 block 승격 판단 — trend_gate 관례 그대로).
        # 기본 off — 미설정 시 신호 문자열까지 100% 동일 보존.
        wmode = str(params.get("williams_gate_mode", "off")).strip().lower()
        self.williams_gate_mode: str = wmode if wmode in ("off", "shadow", "block") else "off"
        self.williams_period: int = int(params.get("williams_period", 14))
        self.williams_overbought: float = float(params.get("williams_overbought", -20.0))
        self.flatten_minutes: float = params.get("flatten_before_close_minutes", 1)
        # 프리마켓 직접 진입(모듈 docstring "프리마켓" 절 2번). 기본 켜짐(사용자
        # 찬성) — 유동성 가드가 실질 방어선이다.
        self.premarket_entry: bool = params.get("premarket_entry", True)
        # 분당 최소 거래대금 가드 — 프리마켓은 유동성이 얇아 1분봉 패턴 전제
        # (거래량 서지 등)가 왜곡될 수 있다. 통화가 시장마다 달라 파라미터를
        # 분리한다(KR=원, US=달러) — 하나로 합치면 원/달러 환산 없이는 어느
        # 쪽이든 자릿수가 틀린다. 기본 KR 5천만원 / US $5만(모듈 docstring
        # "프리마켓" 절 5번 — 근거 없는 보수적 기본값, paper 번인으로 재조정).
        self.premarket_min_volume_krw: float = params.get("premarket_min_volume_krw", 50_000_000.0)
        self.premarket_min_volume_usd: float = params.get("premarket_min_volume_usd", 50_000.0)
        # 일봉 추세/변동성 게이트(모듈 docstring "일봉 추세/변동성 게이트" 절,
        # QuantConnect #407/#478 이식) — 값은 이 저장소 실데이터로 검증되지 않음.
        #
        # **기본값이 shadow 인 이유(2026-08-19)**: 도입 직후 오늘(08-18) 실제
        # 진입 전수에 소급 측정해보니 게이트가 그날 최대 손실(096770 −2.37%)도,
        # news_momentum 손실 2건도 걸러내지 못했고, 042700 에서는 손실 1건과
        # **소폭 승자 1건을 함께** 막았다. 표본 1일이지만 "이걸 켜면 나아진다"는
        # 근거는 아니다 — 근거 없이 진입을 막는 것은 리더보드 상위 전략을
        # 그대로 믿는 것과 같다(다중검정 편향). 그래서 판정은 계산하되 진입은
        # 막지 않고(shadow), 신호 사유에 판정을 실어 저널에 쌓는다. 표본이
        # 모이면 "게이트 차단 후보였던 진입들의 실제 성적"으로 block 승격을
        # 판단한다. off = 계산 자체를 하지 않음(일봉 조회도 없음).
        mode = str(params.get("trend_gate_mode", "shadow")).strip().lower()
        if mode not in ("off", "shadow", "block"):
            mode = "shadow"
        # 하위호환: 옛 불리언 파라미터(trend_gate_enabled=false)는 off 로 읽는다.
        if params.get("trend_gate_enabled") is False:
            mode = "off"
        self.trend_gate_mode: str = mode
        # 마지막 게이트 판정(심볼→사유|None) — shadow 기록이자 신호 사유 재료.
        self.gate_verdict: dict[str, str | None] = {}
        self.adx_min: float = params.get("adx_min", 25.0)
        self.max_atr_ratio: float = params.get("max_atr_ratio", 0.10)
        # 진입 판정용 봉 조회 개수 — MA60 워밍업 + 거래량 20봉 평균 + 진입창
        # 전체를 넉넉히 덮는다. 전-세션 모드(0)면 진입창이 곧 세션 전체이므로
        # 정규장 길이(KR 09:00~15:30 = US 09:30~16:00 = 390분)를 쓴다. 세션
        # 경계를 넘는 연속 1분봉이 필요하다(모듈 docstring "진입" 절).
        self._lookback_bars = max(
            int(params.get("lookback_bars", 400)),
            self.ma_period + self.volume_surge_lookback
            + (self.entry_window_minutes or _FULL_SESSION_MINUTES),
        )

        if self.entry_window_minutes < 0:
            raise ValueError("entry_window_minutes_after_open은 0(전 세션) 이상이어야 합니다.")
        if self.volume_surge_mult <= 0:
            raise ValueError("volume_surge_mult는 양수여야 합니다.")
        if self.volume_surge_lookback <= 0:
            raise ValueError("volume_surge_lookback은 양수여야 합니다.")
        if self.ma_period <= 0:
            raise ValueError("ma_period는 양수여야 합니다.")
        if self.stop_hard_cap_pct <= 0:
            raise ValueError("stop_hard_cap_pct는 양수여야 합니다.")
        if self.partial_take_r <= 0:
            raise ValueError("partial_take_r은 양수여야 합니다.")
        if self.take_profit_bps < 0:
            raise ValueError("take_profit_bps는 0(비활성) 이상이어야 합니다.")
        if self.breakeven_at_bp < 0:
            raise ValueError("breakeven_at_bp는 0(비활성) 이상이어야 합니다.")
        if self.trail_bp < 0:
            raise ValueError("trail_bp는 0(비활성) 이상이어야 합니다.")
        if not 0 < self.partial_fraction < 1:
            raise ValueError("partial_fraction은 0과 1 사이여야 합니다.")
        if self.premarket_min_volume_krw < 0:
            raise ValueError("premarket_min_volume_krw는 0 이상이어야 합니다.")
        if self.structure_wing < 1:
            raise ValueError("structure_wing은 1 이상이어야 합니다.")
        if self.williams_period < 2:
            raise ValueError("williams_period는 2 이상이어야 합니다.")
        if self.premarket_min_volume_usd < 0:
            raise ValueError("premarket_min_volume_usd는 0 이상이어야 합니다.")
        if self.adx_min < 0:
            raise ValueError("adx_min은 0 이상이어야 합니다.")
        if self.max_atr_ratio <= 0:
            raise ValueError("max_atr_ratio는 양수여야 합니다.")
        if self.kr_entry_open_delay_min < 0:
            raise ValueError("kr_entry_open_delay_min은 0(비활성) 이상이어야 합니다.")

        self._session_date: dict[str, dtdate] = {}
        self._pattern_a_used: dict[str, bool] = {}
        self._pattern_b_used: dict[str, bool] = {}
        self._pending: dict[str, dict] = {}
        self.last_reject: dict[str, str] = {}
        # 분 경계 캐시 — {symbol: (분 키, bars)}. 모듈 docstring "조회 최적화" 절.
        self._bars_cache: dict[str, tuple[str, pd.DataFrame]] = {}
        # 프리마켓 확인 마킹 — {symbol: P_pre}. 모듈 docstring "프리마켓" 절 1번.
        self._premarket_confirmed: dict[str, float] = {}
        # 프리마켓 확인 판정을 이미 이번 세션에 확정했는가 — {symbol: 세션일자}.
        # 08:50(블랙아웃 진입) 시점에 1회만 계산한다(반복 재계산 방지).
        self._premarket_checked: dict[str, str] = {}
        # 일봉 추세/변동성 게이트 결과 캐시 — {symbol: (세션일자, 거부사유 또는 None)}.
        # 세션당 1회만 ctx.data.history(symbol, "1d", ...)를 조회한다(모듈 docstring
        # "일봉 추세/변동성 게이트" 절 3번) — 일봉은 장중에 안 바뀌므로 분 경계
        # 캐시(_bars_cache)보다 넓게(세션 전체) 재사용한다.
        self._gate_cache: dict[str, tuple[str, str | None]] = {}

    # ------------------------------------------------------------------ 사이클

    def _owns(self, pos: Position) -> bool:
        """news_scalp.py/intraday_scan.py와 동일 3단 판정 — CLAUDE.md "새 전략
        추가" 레시피가 요구하는 기존 상태 관리 패턴 재사용."""
        if pos.lot(self.id) is not None:
            return True
        meta = pos.meta or {}
        if meta.get("lots") is not None:
            return False
        owner = meta.get("strategy")
        if owner:
            return owner == self.id
        return pos.symbol in self.symbols

    def on_cycle(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        positions = ctx.broker.positions()
        markets_present = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 감지 + 상태 리셋 — 반드시 1)단계(포지션 관리)보다 먼저 돈다.
        #    나중에(2단계에서) 돌면, 그날 첫 사이클에 1)단계가 막 채운
        #    `_bars_cache` 를 같은 사이클 안에서 곧바로 지워버려 분 경계 캐시가
        #    매 사이클 무효화되는 사고가 난다(모듈 docstring "조회 최적화" 절 2번).
        for market in markets_present:
            if not self._market_active(market, ctx):
                continue
            tz, _ = _SESSION_OPEN[market]
            today = ctx.clock.now().astimezone(tz).date()
            if today == self._session_date.get(market):
                continue
            self._session_date[market] = today
            self._pattern_a_used = {
                s: v for s, v in self._pattern_a_used.items() if market_of_symbol(s) != market
            }
            self._pattern_b_used = {
                s: v for s, v in self._pattern_b_used.items() if market_of_symbol(s) != market
            }
            self._pending = {
                s: p for s, p in self._pending.items()
                if market_of_symbol(s) != market
                or (positions.get(s) is not None and positions.get(s).is_open)
            }
            self._bars_cache = {
                s: v for s, v in self._bars_cache.items() if market_of_symbol(s) != market
            }
            self._premarket_confirmed = {
                s: v for s, v in self._premarket_confirmed.items() if market_of_symbol(s) != market
            }
            self._premarket_checked = {
                s: v for s, v in self._premarket_checked.items() if market_of_symbol(s) != market
            }
            self._gate_cache = {
                s: v for s, v in self._gate_cache.items() if market_of_symbol(s) != market
            }

        # 1) 포지션 관리 — 유니버스와 무관하게 열려 있는 자기 포지션을 본다.
        #    세션 게이트: 심볼의 시장이 닫혀 있으면(KR은 프리마켓 확장 창 포함)
        #    조회 없이 건너뛴다(모듈 docstring "조회 최적화" 절 1번) — 닫힌
        #    시장은 가격이 안 움직이므로 관리할 것도 없고, 다음 세션이 열리면
        #    첫 사이클에 오버나잇 레일이 잡는다.
        for symbol, pos in positions.items():
            if not pos.is_open or not self._owns(pos):
                continue
            if not self._market_active(market_of_symbol(symbol), ctx):
                continue
            self._ensure_state(symbol, pos)
            signal = self._manage_position(symbol, pos, ctx)
            if signal is not None:
                signals.append(signal)

        # 2) 시장별 진입 창
        for market in markets_present:
            if not self._market_active(market, ctx):
                continue
            tz, session_open = _SESSION_OPEN[market]
            now_local = ctx.clock.now().astimezone(tz)
            today = now_local.date()

            if market in _PREMARKET_WINDOWS and self._premarket_window_state(market, now_local) in (
                "premarket", "blackout",
            ):
                # 프리마켓 확장 창 — 정규장 진입창 로직(아래)과 배타. 모듈
                # docstring "프리마켓" 절.
                for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                    pos = positions.get(symbol)
                    held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                    signal = self._observe_premarket(symbol, market, ctx, today, now_local, held_qty)
                    if signal is not None:
                        signals.append(signal)
                continue

            session_open_dt = datetime.combine(today, session_open, tzinfo=tz)
            minutes_since_open = (now_local - session_open_dt).total_seconds() / 60
            # 진입창 밖 — 위 1)단계 관리는 계속되지만 신규 진입은 없다.
            # entry_window_minutes=0 이면 상한 없음(전 세션 대기), 개장 전만 차단.
            if minutes_since_open < 0 or (
                self.entry_window_minutes and minutes_since_open > self.entry_window_minutes
            ):
                continue
            # KR 개장 초반 진입 지연 게이트(모듈 docstring 참고) — US는 무관.
            # 관리(1단계)는 이미 위에서 끝났으므로 이 continue는 신규 진입만 막는다.
            if market == "KR" and minutes_since_open < self.kr_entry_open_delay_min:
                continue

            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                pos = positions.get(symbol)
                held_qty = pos.lot_qty(self.id) if pos is not None else 0.0
                if held_qty > 0:
                    continue  # 보유 중엔 신규 진입 평가 없음 — A/B는 순차 진입.
                signal = self._check_entry_for(symbol, market, ctx, today)
                if signal is not None:
                    signals.append(signal)
        return signals

    # ------------------------------------------------------------------ 프리마켓 창

    def _market_active(self, market: str, ctx: Context) -> bool:
        """정규장(`is_market_open`) + `_PREMARKET_WINDOWS`에 등록된 시장의 확장
        관찰 창(모듈 docstring "프리마켓" 절). 프리마켓 구간은 `is_market_open`이
        False라 그대로 쓰면 통째로 스킵된다."""
        if ctx.clock.is_market_open(market):
            return True
        if market not in _PREMARKET_WINDOWS:
            return False
        tz, _ = _SESSION_OPEN[market]
        now_local = ctx.clock.now().astimezone(tz)
        return self._premarket_window_state(market, now_local) != "closed"

    @staticmethod
    def _premarket_window_state(market: str, now_local: datetime) -> str:
        """`market`의 확장 관찰 창 현재 상태 — "premarket"(관찰+직접 진입 평가) |
        "blackout"(관리만 — 정규장 개장 직전 휴지/동시호가/최저유동성 구간) |
        "closed"(그 외 — 정규장이면 `is_market_open`이 이미 True로 처리하고,
        `_PREMARKET_WINDOWS`에 없는 시장도 항상 "closed").

        순수 시각 기준이라(캘린더 미조회) 주말만 걸러낸다 — 공휴일·조기폐장은
        모른다. 그런 날은 프리마켓 봉 자체가 비어 있어 안전하게 아무 일도
        하지 않는다(모듈 docstring "프리마켓" 절 3번)."""
        win = _PREMARKET_WINDOWS.get(market)
        if win is None or now_local.weekday() >= 5:
            return "closed"
        premarket_open, blackout_start = win
        session_open = _SESSION_OPEN[market][1]
        t = now_local.time()
        if premarket_open <= t < blackout_start:
            return "premarket"
        if blackout_start <= t < session_open:
            return "blackout"
        return "closed"

    # ------------------------------------------------------------------ 조회

    def _get_bars(self, symbol: str, market: str, ctx: Context) -> pd.DataFrame:
        """분 경계 캐시 — 같은 분 안의 반복 호출(5초 폴링)은 캐시를 재사용한다.
        1분봉 판단이므로 같은 분 안에서 새로 조회해도 결과가 달라지지 않는다
        (정보 손실 0). `_check_entry_for`(진입 판정)와 `_manage_position`(60선
        트레일 관리)이 공유한다 — 모듈 docstring "조회 최적화" 절 2번."""
        tz, _ = _SESSION_OPEN[market]
        minute_key = ctx.clock.now().astimezone(tz).strftime("%Y-%m-%d %H:%M")
        cached = self._bars_cache.get(symbol)
        if cached is not None and cached[0] == minute_key:
            return cached[1]
        bars = ctx.data.history(symbol, _INTERVAL, self._lookback_bars)
        self._bars_cache[symbol] = (minute_key, bars)
        return bars

    def _trend_gate_reject(self, symbol: str, ctx: Context, today: dtdate) -> str | None:
        """일봉 추세/변동성 게이트(모듈 docstring "일봉 추세/변동성 게이트" 절).
        통과(또는 비활성/데이터 부족으로 게이트 부재)면 None, 거부면
        `last_reject`에 그대로 남길 사유 문자열. 세션당 심볼 1회만 일봉을
        조회하고 결과를 캐시한다(같은 세션의 반복 호출은 재조회하지 않음)."""
        if self.trend_gate_mode == "off":
            return None
        cached = self._gate_cache.get(symbol)
        if cached is not None and cached[0] == today.isoformat():
            self.gate_verdict[symbol] = cached[1]
            return cached[1] if self.trend_gate_mode == "block" else None

        daily_bars = ctx.data.history(symbol, "1d", 40)
        reason: str | None = None
        di = adx_di(daily_bars)
        if di is not None:
            adx, plus_di, minus_di = di
            if adx < self.adx_min or not (plus_di > minus_di):
                reason = f"추세 게이트 미충족(ADX={adx:.1f})"
        if reason is None:
            ratio = atr_ratio(daily_bars)
            if ratio is not None and ratio > self.max_atr_ratio:
                reason = f"변동성 과다(ATR/price={ratio:.3f})"

        self._gate_cache[symbol] = (today.isoformat(), reason)
        self.gate_verdict[symbol] = reason
        # shadow 는 판정만 남기고 진입을 막지 않는다(위 기본값 근거 주석).
        return reason if self.trend_gate_mode == "block" else None

    # ------------------------------------------------------------------ 진입

    @staticmethod
    def _session_bars(
        bars: pd.DataFrame, market: str, today: dtdate, *, premarket: bool = False
    ) -> pd.DataFrame:
        """오늘 날짜의 봉을 세션 시간 기준으로 나눈다. `premarket=False`(기본)면
        정규장 개장(`_SESSION_OPEN`) 이후만, `True`면 그 이전(프리마켓)만.

        날짜만으로 거르면(예전 `bar_dates == today`) 프리마켓 봉이 정규장 봉과
        섞인다 — 같은 달력 날짜를 공유하기 때문이다. 섞이면 "세션 고점"(P1)과
        "시가"의 정의가 깨진다(프리마켓 08:00봉의 시가가 정규장 시가로 오인된다).
        `_PREMARKET_WINDOWS`에 없는 시장(현재는 KR/US 둘 다 있음)이 추가되면
        이 필터는 그 시장에서 사실상 no-op이다(프리마켓 봉 자체가 없으므로)."""
        tz, session_open_t = _SESSION_OPEN[market]
        local = bars.index.tz_convert(tz)
        same_day = local.date == today
        if premarket:
            # 프리마켓 시작~정규장 개장으로 명시적으로 하한을 둔다 — 상한(정규장
            # 개장)만 걸면 자정~프리마켓 시작 사이에 우연히 같은 날짜로 잡히는 봉
            # (예: lookback 워밍업 구간)까지 "프리마켓"으로 오인해 P_pre/시가
            # 계산이 오염된다.
            premarket_open_t = _PREMARKET_WINDOWS.get(market, (session_open_t, session_open_t))[0]
            mask = same_day & (local.time >= premarket_open_t) & (local.time < session_open_t)
        else:
            mask = same_day & (local.time >= session_open_t)
        return bars[mask]

    def _check_entry_for(self, symbol: str, market: str, ctx: Context, today: dtdate) -> Signal | None:
        self.last_reject.pop(symbol, None)
        bars = self._get_bars(symbol, market, ctx)
        if bars.empty:
            self.last_reject[symbol] = "1분봉 없음"
            return None
        today_bars = self._session_bars(bars, market, today)
        if today_bars.empty:
            self.last_reject[symbol] = "오늘 세션 1분봉 없음"
            return None
        open_price = float(today_bars["open"].iloc[0])

        gate_reject = self._trend_gate_reject(symbol, ctx, today)
        if gate_reject is not None:
            self.last_reject[symbol] = gate_reject
            return None

        basis: tuple[str, float] | None = None
        if not self._pattern_a_used.get(symbol, False):
            # _premarket_confirmed는 _PREMARKET_WINDOWS에 등록된 시장의 심볼만
            # 채워진다(_observe_premarket) — market 분기 없이 조회해도 안전하다.
            p_pre = self._premarket_confirmed.get(symbol)
            if p_pre is not None:
                # 프리마켓 확인 심볼 — P1을 새로 기다리지 않고 P_pre를 P1로
                # 인정한다(모듈 docstring "프리마켓" 절 1번).
                p1_l1 = self._check_pattern_a_accelerated(today_bars, open_price, p_pre)
            else:
                p1_l1 = self._check_pattern_a(today_bars, bars, open_price)
            if p1_l1 is not None:
                basis = ("A", p1_l1[1])
            else:
                self.last_reject[symbol] = "패턴A 미충족"
        elif not self._pattern_b_used.get(symbol, False):
            ma_val = self._check_pattern_b(today_bars, bars)
            if ma_val is not None:
                basis = ("B", ma_val)
            else:
                self.last_reject[symbol] = "패턴B 미충족"
        else:
            self.last_reject[symbol] = "세션 진입 상한(A+B) 도달"

        if basis is None:
            return None
        pattern, basis_price = basis
        return self._build_entry(symbol, pattern, basis_price, today, ctx, bars=bars)

    def _entry_stop(
        self, entry_price: float, basis_price: float, bars: pd.DataFrame | None
    ) -> tuple[float | None, str, str]:
        """손절 계산 — (손절가|None, 거부 사유, 사유 노트). 상태를 건드리지
        않는다(순수 쌍둥이가 그대로 공유 — 동치의 원천을 한 벌로).

        structure 모드는 지지(스윙 저점)가 안 보이면 None — 임의의 선을 그어
        주지 않는다. 하드캡(stop_hard_cap_pct)은 두 모드 공통 바닥이다."""
        if self.stop_mode == "structure":
            if bars is None or bars.empty:
                return None, "구조 손절 계산 불가(1분봉 없음)", ""
            bracket = structure_bracket(
                entry_price, bars, wing=self.structure_wing,
                stop_buffer_pct=self.stop_buffer_pct, hard_cap_pct=self.stop_hard_cap_pct,
            )
            if bracket is None:
                return None, "구조 지지 없음 — 손절선을 정할 수 없는 자리(진입 금지)", ""
            return bracket.stop, "", f" [구조손절:{bracket.stop_basis}]"
        raw_stop = basis_price * (1 - self.stop_buffer_pct / 100)
        floor_stop = entry_price * (1 - self.stop_hard_cap_pct / 100)
        stop = max(raw_stop, floor_stop)
        if stop >= entry_price:
            return None, "손절가 계산 불가(진입가 이상)", ""
        return stop, "", ""

    def _williams_verdict(self, bars: pd.DataFrame | None) -> str | None:
        """과열(과매수) 판정 — 차단 후보면 사유 문자열, 아니면 None. 상태 없음.

        W%R 이 계산 불가(봉 부족·레인지 0)면 None — 모르는 것을 차단 근거로
        쓰지 않는다(trend_gate 의 "게이트 부재=통과" 원칙과 동일)."""
        if self.williams_gate_mode == "off" or bars is None or bars.empty:
            return None
        wr = williams_r(bars, self.williams_period)
        if wr is None or wr <= self.williams_overbought:
            return None
        return f"W%R 과매수({wr:.0f})"

    def _build_entry(
        self, symbol: str, pattern: str, basis_price: float, today: dtdate, ctx: Context,
        *, premarket: bool = False, bars: pd.DataFrame | None = None,
    ) -> Signal | None:
        """패턴 판정 이후 공통 경로 — 손절 계산·상태 마킹·Signal 생성. 정규장
        진입(`_check_entry_for`)과 프리마켓 직접 진입(`_observe_premarket`)이
        공유한다(모듈 docstring "프리마켓" 절 2번)."""
        quote = ctx.data.quote(symbol)
        if quote is None or quote.price <= 0:
            self.last_reject[symbol] = "현재가 없음"
            return None
        entry_price = quote.price

        w_verdict = self._williams_verdict(bars)
        if self.williams_gate_mode == "block" and w_verdict is not None:
            self.last_reject[symbol] = w_verdict
            return None

        stop, reject, stop_note = self._entry_stop(entry_price, basis_price, bars)
        if stop is None:
            self.last_reject[symbol] = reject
            return None

        # 세션당 최대 2회 진입(A 1회 + B 1회, 프리마켓+정규장 합산) — 균등
        # 분할(news_scalp의 1/max_entries_per_session과 동일 철학, 여기선
        # 슬롯 수가 구조적으로 2).
        target_weight = 0.5

        self._pending[symbol] = {
            "entry": entry_price, "stop": stop, "pattern": pattern,
            "session": today.isoformat(), "partial_taken": False,
            "qty_at_signal": 0.0,
        }
        if pattern == "A":
            self._pattern_a_used[symbol] = True
        else:
            self._pattern_b_used[symbol] = True

        tag = "프리마켓 " if premarket else ""
        # 게이트 판정을 사유에 싣는다 — shadow 모드의 표본은 저널의 이 문자열로
        # 쌓인다("차단후보였는데 실제로는 얼마를 벌었나/잃었나"를 나중에 집계).
        gate_note = ""
        if self.trend_gate_mode != "off":
            verdict = self.gate_verdict.get(symbol)
            gate_note = (f" [게이트:차단후보 {verdict}]" if verdict else " [게이트:통과]")
        # W%R shadow 표본도 trend_gate 와 같은 방식으로 저널 문자열에 쌓는다.
        w_note = ""
        if self.williams_gate_mode != "off":
            w_note = f" [W%R:차단후보 {w_verdict}]" if w_verdict else " [W%R:통과]"
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"1분봉 스캘프 {tag}패턴{pattern} 진입: {symbol} w={target_weight:.2f} "
                f"손절={fmt_price(stop, symbol)} (기준={fmt_price(basis_price, symbol)})"
                f"{stop_note}{gate_note}{w_note}"
            ),
            stop=stop,
        )

    def _check_pattern_a(
        self, today_bars: pd.DataFrame, full_bars: pd.DataFrame, open_price: float
    ) -> tuple[float, float] | None:
        """패턴 A — 개장 되돌림 higher-low 재돌파. 매치 시 (P1, L1), 아니면 None."""
        if len(today_bars) < 3:
            return None
        prior = today_bars.iloc[:-1]
        last = today_bars.iloc[-1]
        p1 = float(prior["high"].max())
        if float(last["close"]) <= p1:
            return None  # 재돌파 없음
        p1_idx = prior["high"].idxmax()
        after_p1 = prior.loc[prior.index > p1_idx]
        if after_p1.empty:
            return None  # 되돌림 구간 없음 — 패턴 무효
        l1 = float(after_p1["low"].min())
        if l1 <= open_price:
            return None  # 시가 아래로 뚫림 — 패턴 무효

        # 거래량 서지 존재 여부 — 탐색 범위는 오늘 개장~P1봉(P1 형성 구간).
        formation = today_bars.loc[today_bars.index <= p1_idx]
        vol_avg = full_bars["volume"].rolling(self.volume_surge_lookback, min_periods=1).mean()
        vol_avg_formation = vol_avg.loc[formation.index]
        up_bars = formation["close"] > formation["open"]
        surge = (formation["volume"] >= self.volume_surge_mult * vol_avg_formation) & up_bars
        if not bool(surge.any()):
            return None
        return (p1, l1)

    @staticmethod
    def _check_pattern_a_accelerated(
        today_bars: pd.DataFrame, open_price: float, p_pre: float
    ) -> tuple[float, float] | None:
        """패턴 A 가속 버전 — 프리마켓 확인 심볼 전용(모듈 docstring "프리마켓"
        절 1번). P_pre를 P1로 그대로 인정하므로 오늘 정규장 봉에서는 되돌림
        (higher-low)+재돌파만 확인한다 — 거래량 서지는 프리마켓에서 이미
        검증됐으므로 재요구하지 않는다. 매치 시 (P_pre, L1), 아니면 None.

        L1 후보 구간("P1 이후")은 P1(=P_pre)이 프리마켓에 있었으므로 오늘
        정규장 봉 전체(마지막 재돌파봉 제외)다 — 정규장 첫 봉 자신도 포함된다.
        그래서 **엄격한 부등호**(`<`)를 쓴다: `<=`면 저가가 시가와 같기만 해도
        (실전에서 흔한, "아래 꼬리 없는" 정상적인 첫 봉) 무효 판정이 나 이
        경로가 사실상 발동하지 않는다 — 기존(비가속) 패턴 A는 L1이 P1과 별개
        봉이라 이 문제가 없다."""
        if today_bars.empty:
            return None
        last = today_bars.iloc[-1]
        if float(last["close"]) <= p_pre:
            return None  # 재돌파 없음
        prior = today_bars.iloc[:-1]
        if prior.empty:
            return None  # 되돌림을 확인할 봉이 아직 없음(정규장 첫 봉에서 곧바로 재돌파)
        l1 = float(prior["low"].min())
        if l1 < open_price:
            return None  # 정규장 시가 아래로 뚫림 — 기존 규칙대로 무효
        return (p_pre, l1)

    def _check_pattern_b(self, today_bars: pd.DataFrame, full_bars: pd.DataFrame) -> float | None:
        """패턴 B — 60선(1분) 지지 반등. 매치 시 MA60 값(진입 근거), 아니면 None."""
        if len(today_bars) < 2:
            return None
        ma = sma(full_bars["close"], self.ma_period)
        prev = today_bars.iloc[-2]
        last = today_bars.iloc[-1]
        ma_at_prev = ma.get(prev.name)
        if ma_at_prev is None or pd.isna(ma_at_prev):
            return None
        tolerance = ma_at_prev * (self.ma_tolerance_pct / 100)
        if float(prev["low"]) < ma_at_prev - tolerance:
            return None  # 60선 지지 실패
        if float(last["close"]) <= float(last["open"]):
            return None  # 확인봉이 양봉이 아님
        ma_at_last = ma.get(last.name)
        if ma_at_last is None or pd.isna(ma_at_last):
            return None
        if float(last["close"]) < float(ma_at_last):
            # 확인봉이 양봉이어도 종가가 60선 아래면 진입 금지 (2026-08-18 실전
            # P0: 096770 10:28 — 양봉 조건만 보고 진입했는데 종가<MA60 이라
            # 다음 사이클의 '60선 종가 이탈 트레일'이 같은 봉으로 즉시 전량
            # 청산, 동일 분 왕복 -198원(전액 수수료·호가 낙차). 진입 조건과
            # 청산 조건이 상호 배타적이어야 한다 — '반등'은 60선 회복까지다.
            return None  # 확인봉 종가가 60선 미회복
        return float(ma_at_prev)

    # ------------------------------------------------------------------ 프리마켓

    def _check_premarket_pattern(
        self, pre_bars: pd.DataFrame, full_bars: pd.DataFrame, open_price: float
    ) -> float | None:
        """프리마켓 확인 — (a) 거래량 서지 동반 상승봉이 고점 P_pre 형성 +
        (b) 그 뒤 프리마켓 마감까지 저가가 시가 위 유지. 매치 시 P_pre, 아니면
        None (모듈 docstring "프리마켓" 절 1번).

        (b)는 P_pre 형성봉 **이후** 구간만 본다(패턴 A의 L1 판정과 동일 관례) —
        형성봉 자신을 포함하면 항상 실패한다: 실제 OHLC는 어느 봉이든 저가가
        그 봉 자신의 시가 이하이므로, 서지봉이 프리마켓 첫 봉이면 그 봉의
        저가는 정의상 세션 시가(=그 봉의 시가) 이하가 되어 "유지"가 성립할
        수 없다."""
        if pre_bars.empty:
            return None
        vol_avg = full_bars["volume"].rolling(self.volume_surge_lookback, min_periods=1).mean()
        vol_avg_pre = vol_avg.loc[pre_bars.index]
        up_bars = pre_bars["close"] > pre_bars["open"]
        surge = (pre_bars["volume"] >= self.volume_surge_mult * vol_avg_pre) & up_bars
        if not bool(surge.any()):
            return None
        p_pre_idx = pre_bars.loc[surge, "high"].idxmax()
        p_pre = float(pre_bars.loc[p_pre_idx, "high"])
        after = pre_bars.loc[pre_bars.index > p_pre_idx]
        if not after.empty and float(after["low"].min()) <= open_price:
            return None  # 형성 이후 시가 아래로 뚫림 — "유지" 실패
        return p_pre

    def _observe_premarket(
        self, symbol: str, market: str, ctx: Context, today: dtdate, now_local: datetime,
        held_qty: float,
    ) -> Signal | None:
        """프리마켓 관찰(+블랙아웃 진입 시점 1회 확정) + (설정 시) 직접 진입
        평가. 모듈 docstring "프리마켓" 절. `market`은 `_PREMARKET_WINDOWS`에
        등록된 시장(현재 KR/US)만 호출부(`on_cycle`)에서 넘어온다."""
        self.last_reject.pop(symbol, None)
        bars = self._get_bars(symbol, market, ctx)
        if bars.empty:
            self.last_reject[symbol] = "1분봉 없음"
            return None
        pre_bars = self._session_bars(bars, market, today, premarket=True)
        if pre_bars.empty:
            self.last_reject[symbol] = "프리마켓 1분봉 없음"
            return None
        open_price = float(pre_bars["open"].iloc[0])
        state = self._premarket_window_state(market, now_local)

        if state == "blackout":
            # 프리마켓 종료 — 관찰 구간 전체를 놓고 "프리마켓 확인" 여부를
            # 세션당 1회 확정한다(모듈 docstring "프리마켓" 절 1번).
            if self._premarket_checked.get(symbol) != today.isoformat():
                self._premarket_checked[symbol] = today.isoformat()
                p_pre = self._check_premarket_pattern(pre_bars, bars, open_price)
                if p_pre is not None:
                    self._premarket_confirmed[symbol] = p_pre
            self.last_reject[symbol] = "프리마켓 휴지(08:50~09:00) — 신규 진입 없음"
            return None

        # state == "premarket" — 직접 진입 평가(패턴 A만, 모듈 docstring
        # "프리마켓" 절 2번). 관찰 마킹은 08:50 확정 시점에 한꺼번에 하므로
        # 여기서는 하지 않는다(반복 계산 방지).
        if market not in _PREMARKET_DIRECT_ENTRY_MARKETS:
            # 구조적으로 체결이 불가능한 시장 — 유동성 가드보다 앞에서 막는다.
            self.last_reject[symbol] = (
                "프리마켓 직접 진입 불가 — 한국장은 연속 프리마켓이 없다"
                "(08:30~09:00 동시호가는 09:00 에 일괄 체결). 체결될 수 없는 주문은 내지 않는다"
            )
            return None
        if not self.premarket_entry:
            self.last_reject[symbol] = "프리마켓 직접 진입 비활성(premarket_entry=false)"
            return None
        if held_qty > 0:
            self.last_reject[symbol] = "보유 중 — 신규 진입 평가 없음"
            return None
        if self._pattern_a_used.get(symbol, False):
            self.last_reject[symbol] = "패턴A 이미 사용(세션당 1회)"
            return None
        gate_reject = self._trend_gate_reject(symbol, ctx, today)
        if gate_reject is not None:
            self.last_reject[symbol] = gate_reject
            return None

        p1_l1 = self._check_pattern_a(pre_bars, bars, open_price)
        if p1_l1 is None:
            self.last_reject[symbol] = "프리마켓 패턴A 미충족"
            return None
        p1, l1 = p1_l1

        last_bar = pre_bars.iloc[-1]
        notional = float(last_bar["close"]) * float(last_bar["volume"])
        # 유동성 가드 임계치는 시장 통화로 분리(KR=원, US=달러) — 모듈 docstring
        # "프리마켓" 절 5번.
        min_notional = self.premarket_min_volume_krw if market == "KR" else self.premarket_min_volume_usd
        if notional < min_notional:
            self.last_reject[symbol] = (
                f"프리마켓 유동성 가드 미달(거래대금={notional:,.0f} < {min_notional:,.0f})"
            )
            return None

        return self._build_entry(symbol, "A", l1, today, ctx, premarket=True, bars=bars)

    # ------------------------------------------------------------------ 관리

    def _ensure_state(self, symbol: str, pos: Position) -> None:
        lot = pos.ensure_lot(self.id)
        pending = self._pending.get(symbol)
        if pending is not None and lot.get("qty", 0.0) > pending.get("qty_at_signal", 0.0):
            self._pending.pop(symbol, None)
            fresh = {k: v for k, v in pending.items() if k != "qty_at_signal"}
            lot.update(fresh)
            return
        if "entry" in lot:
            return
        # 재시작 복구 — 진짜 진입 근거(L1/MA60)를 모른다. 하드 캡 기준 보수적
        # 폴백만으로 관리를 이어간다(news_scalp의 avg_cost 폴백과 같은 원칙).
        entry = lot.get("avg_cost", pos.avg_cost)
        stop = entry * (1 - self.stop_hard_cap_pct / 100)
        lot.update(entry=entry, stop=stop, session=None, partial_taken=False)

    def _manage_position(self, symbol: str, pos: Position, ctx: Context) -> Signal | None:
        quote = ctx.data.quote(symbol)
        if quote is None:
            return None
        price = quote.price
        lot = pos.ensure_lot(self.id)
        entry, stop = lot["entry"], lot["stop"]
        market = market_of_symbol(symbol)
        tz, _ = _SESSION_OPEN[market]

        # 오버나잇 금지 — should_flatten 하나에 기대지 않는다(다른 전략과 동일).
        entry_session = lot.get("session")
        if entry_session and entry_session != ctx.clock.now().astimezone(tz).date().isoformat():
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} 현재={fmt_price(price, symbol)}",
            )
        if ctx.clock.should_flatten(market, self.flatten_minutes):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"EoD 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}",
            )
        # 본전 이동 + 고수위 트레일 — 손절 판정 **전에** 스탑을 단조 상향한다
        # (근거는 생성자의 breakeven_at_bp 주석). 최초 리스크 R 은 상향 전 스탑
        # 으로 고정(r0) — 절반 익절 목표(+1.5R)가 스탑 상향으로 무력화되지 않게.
        lot.setdefault("r0", entry - stop)
        if self.breakeven_at_bp or self.trail_bp:
            hi = max(float(lot.get("hi", entry)), price)
            lot["hi"] = hi
            raised = stop
            # **트레일은 이익 구간에 들어간 뒤에만 작동한다**(2026-08-28 수리).
            # 이전 구현은 진입 직후부터 `hi*(1-trail)` 로 스탑을 올려, 고수위가
            # 아직 진입가일 때도 손절선을 진입가 -trail_bp 로 **조여버렸다**.
            # 실거래 확증: 096770 구조손절 117,216(-111bp) → 트레일이 117,571
            # (-70bp)로 조임 → 117,500 에서 청산. 원래 손절선이었으면 살아남았다.
            # 원장 실측이 방향을 확정한다: 손절당한 뒤 **76%(35/46)가 당일 진입가
            # 위로 회복**했고 회복 폭 중앙 +105bp — 진입이 틀린 게 아니라 손절이
            # 노이즈에 걸린 것이다(소유자 지적과 일치).
            armed = bool(lot.get("trail_armed")) or (
                self.breakeven_at_bp and hi >= entry * (1 + self.breakeven_at_bp / 1e4)
            )
            if armed:
                lot["trail_armed"] = True
                raised = max(raised, entry)          # 본전 확보
                if self.trail_bp:
                    raised = max(raised, hi * (1 - self.trail_bp / 1e4))
            if raised > stop:
                lot["stop"] = stop = raised
        if price <= stop:
            kind = "이익보호 청산(본전/트레일)" if stop >= entry else "손절"
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(
                    f"{kind}: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                    f"현재={fmt_price(price, symbol)}"
                ),
            )
        # 전량 익절 — 하드 손절 바로 다음, MA60 잔량 트레일보다 **앞**. 익절가는
        # 하드 목표라 트레일 판정보다 우선한다. 0(기본)이면 이 블록은 없는 것과
        # 같다(근거는 생성자의 take_profit_bps 주석).
        if self.take_profit_bps:
            take = entry * (1 + self.take_profit_bps / 1e4)
            if price >= take:
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(f"전량 익절(+{self.take_profit_bps:g}bp): entry={fmt_price(entry, symbol)} "
                            f"목표={fmt_price(take, symbol)} 현재={fmt_price(price, symbol)}"),
                )

        # 잔량 트레일 — 최근 완성 1분봉 종가가 MA60 아래로 마감하면 전량 청산
        # (모듈 docstring "청산" 절 3번). MA60은 세션 경계를 넘는 연속 시계열 기준.
        # 분 경계 캐시(_get_bars)를 진입 판정과 공유 — 넉넉한 lookback이라 MA60
        # 계산에 필요한 최근 ma_period+1개를 항상 포함한다.
        ma_bars = self._get_bars(symbol, market, ctx)
        if len(ma_bars) >= self.ma_period:
            ma_last = sma(ma_bars["close"], self.ma_period).iloc[-1]
            last_close = float(ma_bars["close"].iloc[-1])
            if pd.notna(ma_last) and last_close < float(ma_last):
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(
                        f"60선 이탈(잔량 트레일): 종가={fmt_price(last_close, symbol)} "
                        f"MA60={fmt_price(float(ma_last), symbol)}"
                    ),
                )

        if not lot.get("partial_taken"):
            r = float(lot.get("r0", entry - stop))
            if r > 0:
                target = entry + self.partial_take_r * r
                if price >= target:
                    return Signal(
                        strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                        target_weight=0.0, exit_fraction=self.partial_fraction,
                        reason=(
                            f"절반 익절(+{self.partial_take_r:g}R): entry={fmt_price(entry, symbol)} "
                            f"목표={fmt_price(target, symbol)} 현재={fmt_price(price, symbol)} "
                            f"{self.partial_fraction * 100:.0f}% 청산"
                        ),
                        state_update={"partial_taken": True},
                    )
        return None


class Scalp1mPureStrategy:
    """`Scalp1mStrategy`와 동일한 판단을 하는 순수함수 구현 — 엔진 분리 설계
    Phase A 두 번째 이전 대상(`docs/superpowers/specs/2026-08-19-engine-separation-design.md`).
    파일럿(`DonchianPureStrategy`)과 같은 원칙(`ctx`도 인스턴스 가변 상태도 읽지
    않는다, entry/stop/pattern/session/partial_taken은 전부 `state`↔`next_state`로만
    다닌다)을 따르되, 이 전략 고유의 두 가지를 추가로 다룬다.

    **왜 `Scalp1mStrategy` 인스턴스(`self._legacy`)를 들고 있는가.** 패턴 A/A가속/B/
    프리마켓 판정(`_check_pattern_a`, `_check_pattern_a_accelerated`,
    `_check_pattern_b`, `_check_premarket_pattern`)과 세션 분할(`_session_bars`),
    프리마켓 창 상태(`_premarket_window_state`)는 원래도 순수 계산이었다 —
    `self.<설정값>`(volume_surge_mult 등, 생성자 이후 불변)과 인자만 읽고 인스턴스
    가변 상태(`_pattern_a_used` 등)는 건드리지 않는다. 이 300줄 가까운 임계값
    로직을 손으로 다시 옮기면 전사 오류로 동치성이 몰래 깨질 위험이 크다 —
    그래서 **재구현하지 않고 그대로 재사용**한다(`self._legacy`는 `on_cycle`을
    절대 호출하지 않는다 — 오직 이 순수 헬퍼 메서드 재사용 용도뿐이다). 생성자
    파라미터 검증(`ValueError` 등)과 `_lookback_bars` 파생값 계산도 마찬가지
    이유로 위임한다 — 이중 유지하면 두 구현이 조용히 갈라진다.

    **가변 상태 10개(레거시 문서상 "16개"는 config 파라미터까지 넉넉히 잡은
    이전 추정치다 — 실제 인스턴스 가변 dict는 10개, 아래 표 참고) → next_state 매핑**
    은 클래스 상단 주석이 아니라 이 저장소 관례대로 작업 보고서에 표로 남긴다.

    **구조적으로 없어지는 버그**: `_pattern_a_used`/`_pattern_b_used`/
    `partial_taken`을 신호 생성과 동시에(같은 `decide()` 호출 안에서) `next_state`에
    반영하는 것은 레거시와 동일하지만(같은 사이클 중복 방지 목적, 5초 루프 절
    참고), 이제 그 반영이 **`signals`와 `next_state`가 같은 반환값의 두 필드로
    묶여 있어** 신호 없이 상태만 바뀌거나 상태 없이 신호만 나가는 경로가
    코드 구조상 존재할 수 없다(레거시는 `self._pattern_a_used[symbol] = True`와
    `return Signal(...)`가 별개의 문장이라 향후 리팩터링이 둘을 갈라놓을 여지가
    있었다). 또한 매 `decide()` 호출이 인자로 받은 `state`의 **사본**만 변경해
    반환하므로(원본 dict를 in-place mutate하지 않음), 같은 인스턴스를 여러
    스레드/사이클에서 재진입 호출해도 상태가 서로 오염될 수 없다 — 순수 함수
    계약이 구조적으로 보장한다.

    **아직 못 하는 것(정직하게, `shell.py`가 이미 인정한 한계 + 이 전략 고유)**:
    1. `next_state`는 체결 확인 여부와 무관하게 매 사이클 그대로 적용된다
       (Phase A 공통 한계, `shell.py` 클래스 docstring 참고) — risk 거부/주문
       실패에도 패턴 A/B "사용" 판정과 부분익절 플래그는 되돌릴 수 없다.
       레거시도 동일하게 취약하므로 **동치성은 유지된다** — "구조적으로
       없어지는 버그"는 위 문단의 재현-불가능성(신호/상태 반영 결합) 쪽이다.
    2. 재시작 복구(레거시 `_ensure_state`의 `avg_cost` 폴백 경로)는 이번 범위
       밖이다 — `donchian_pure` 선례와 동일한 이유(`StrategySnapshot.lots`가
       lot 필드만 주지 `pos.avg_cost` 같은 심볼 합산 필드는 주지 않는다).
       `pending`에도 `open`에도 없는 심볼은 관리를 건너뛴다.
    3. 조회 최적화(분 경계 캐시 `_get_bars`, 세션 게이트로 조회 자체를 건너뛰는
       최적화)가 사라진다 — `DataNeeds`는 사이클마다 정적으로 같은 것을
       선언하므로, 껍질은 시장이 닫혀 있어도·같은 1분봉이 반복돼도 매 사이클
       전 심볼의 1분봉(+활성 시 일봉)을 전부 재조회한다. 데이터 내용은
       동일하므로(완성봉만 반환) **신호 정확성에는 영향이 없다** — 순수한
       조회 횟수/지연시간 회귀다. scalp_1m은 아직 `config/settings.yaml`에
       `scalp_1m_pure`로 배선돼 있지 않으므로 지금 당장의 운영 영향은 없다.
    4. `_owns()`의 "다른 전략이 이미 소유한 심볼"(관심종목 유니버스를 여러
       전략이 공유) 판정을 `StrategySnapshot.lots`만으로는 완전히 재현할 수
       없다 — `lots[symbol]`은 `pos.lot(self.id)`(내 lot, 순수 조회) 결과를
       `pos.is_open`(심볼 합산 qty) 게이트로만 채우므로(`shell.py`), "내 lot은
       없지만 다른 전략이 채워 이미 합산 qty>0"인 경우와 "방금 내가 체결됐는데
       아직 lot qty 필드가 없는 경우"를 스냅샷만으로 구분할 수 없다. 이 구현은
       `pending`/`open` 딕셔너리(내가 실제로 진입 시도한 심볼만 기록)로 이
       모호성을 우회한다 — 내가 시도하지 않은 심볼은 `pending`에도 `open`에도
       없으므로 관리하지 않는다(위 2번과 동일한 "복구 불가, 건너뛴다" 경로로
       자연히 흡수된다). **다만** 레거시 `on_cycle`은 `ctx.broker.positions()`
       전체를 순회해 `self.symbols`에 없는 심볼(유니버스에서 빠진 뒤에도 남은
       보유분)까지 `_owns()`로 관리하는데, `DataNeeds`가 정적으로 `self.symbols`만
       선언하므로 이 구현은 그런 "고아 포지션"을 볼 수조차 없다 — 관심종목
       기반 전략 전체(orb_scan/intraday_scan/news_scalp 등)를 이전할 때 공통으로
       부딪힐 문제로 예상된다.
    """

    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "scalp_1m_pure"):
        self.id = id
        self.symbols = list(symbols)
        self.market = market

        # 파라미터 파싱/검증/파생값 계산은 레거시에 위임한다(클래스 docstring
        # "왜 self._legacy" 절). on_cycle은 절대 호출하지 않는다.
        self._legacy = Scalp1mStrategy(list(symbols), params, market=market, id=f"{id}__helper")

        self.entry_window_minutes = self._legacy.entry_window_minutes
        self.kr_entry_open_delay_min = self._legacy.kr_entry_open_delay_min
        self.stop_buffer_pct = self._legacy.stop_buffer_pct
        self.stop_hard_cap_pct = self._legacy.stop_hard_cap_pct
        self.partial_take_r = self._legacy.partial_take_r
        self.partial_fraction = self._legacy.partial_fraction
        self.take_profit_bps = self._legacy.take_profit_bps
        self.breakeven_at_bp = self._legacy.breakeven_at_bp
        self.trail_bp = self._legacy.trail_bp
        self.flatten_minutes = self._legacy.flatten_minutes
        self.premarket_entry = self._legacy.premarket_entry
        self.premarket_min_volume_krw = self._legacy.premarket_min_volume_krw
        self.premarket_min_volume_usd = self._legacy.premarket_min_volume_usd
        self.trend_gate_mode = self._legacy.trend_gate_mode
        self.stop_mode = self._legacy.stop_mode
        self.structure_wing = self._legacy.structure_wing
        self.williams_gate_mode = self._legacy.williams_gate_mode
        self.williams_period = self._legacy.williams_period
        self.williams_overbought = self._legacy.williams_overbought
        self.adx_min = self._legacy.adx_min
        self.max_atr_ratio = self._legacy.max_atr_ratio
        self.ma_period = self._legacy.ma_period
        self._lookback_bars = self._legacy._lookback_bars

    # ------------------------------------------------------------------ 계약

    def requirements(self) -> DataNeeds:
        """1분봉은 전 심볼 정적 선언(분 경계 캐시가 없으므로 매 사이클 전량
        재조회 — 클래스 docstring "아직 못 하는 것" 3번). 일봉은
        `trend_gate_mode`가 "off"가 아닐 때만 선언한다 — `trend_gate_mode`는
        생성자 이후 불변이라(사이클마다 안 바뀜) 정적 선언으로 안전하게 표현
        가능하다(모듈 docstring 지시: "필요한 만큼만 선언")."""
        bars = tuple((s, _INTERVAL, self._lookback_bars) for s in self.symbols)
        if self.trend_gate_mode != "off":
            bars += tuple((s, "1d", 40) for s in self.symbols)
        # `fetch_when_closed=True` (2026-09-02): 이 전략은 정규장 밖(프리마켓)에서
        # 실제로 판단하고 진입한다 — `risk.extended_sessions.scalp_1m` 에 KR/US
        # 창이 등록돼 있고 그 구간의 1분봉/현재가가 진입 근거다. 껍질의 기본
        # 게이트("닫힌 시장은 조회하지 않는다")를 그대로 두면 프리마켓 진입이
        # 데이터 없이 죽는다. 다른 순수 전략은 이 값을 켜지 않는다.
        return DataNeeds(
            bars=bars,
            quotes=tuple(self.symbols),
            needs_positions=True,
            fetch_when_closed=True,
        )

    def decide(self, snap: StrategySnapshot, state: Mapping[str, Any]) -> Decision:
        session_date: dict[str, dtdate] = dict(state.get("session_date", {}))
        pattern_a_used: dict[str, bool] = dict(state.get("pattern_a_used", {}))
        pattern_b_used: dict[str, bool] = dict(state.get("pattern_b_used", {}))
        pending: dict[str, dict] = {s: dict(p) for s, p in state.get("pending", {}).items()}
        open_: dict[str, dict] = {s: dict(o) for s, o in state.get("open", {}).items()}
        premarket_confirmed: dict[str, float] = dict(state.get("premarket_confirmed", {}))
        premarket_checked: dict[str, str] = dict(state.get("premarket_checked", {}))
        gate_cache: dict[str, tuple[str, str | None]] = dict(state.get("gate_cache", {}))

        signals: list[Signal] = []
        markets_present = sorted({market_of_symbol(s) for s in self.symbols})

        # 0) 세션 롤 감지 + 상태 리셋 — 반드시 1)단계보다 먼저(레거시와 동일 순서,
        #    모듈 docstring 원본 on_cycle 0번 절 참고).
        for market in markets_present:
            if not self._market_active(market, snap):
                continue
            tz, _ = _SESSION_OPEN[market]
            today = snap.now.astimezone(tz).date()
            if today == session_date.get(market):
                continue
            session_date[market] = today
            for d in (pattern_a_used, pattern_b_used, premarket_confirmed, premarket_checked, gate_cache):
                for s in [s for s in d if market_of_symbol(s) == market]:
                    d.pop(s, None)
            # pending: 레거시는 `positions.get(s).is_open`(심볼 합산, 체결
            # 확정)으로 살아있는 pending만 남긴다 — snap.lots가 정확히 같은
            # 조건으로 채워지므로(shell.py) 그대로 대응한다.
            for s in [s for s in pending if market_of_symbol(s) == market and s not in snap.lots]:
                pending.pop(s, None)

        # 1) 포지션 관리 — self.symbols만(클래스 docstring "아직 못 하는 것" 4번 —
        #    고아 포지션은 볼 수 없다).
        for symbol in self.symbols:
            market = market_of_symbol(symbol)
            if not self._market_active(market, snap):
                continue
            if symbol not in open_:
                if symbol in snap.lots and symbol in pending:
                    open_[symbol] = pending.pop(symbol)
                else:
                    continue  # 내 것이 아니거나 복구 불가 — 관리하지 않는다.
            elif symbol not in snap.lots:
                open_.pop(symbol, None)  # 외부적으로(체결 확정 등) 청산됨 — 정리.
                continue
            signal = self._manage(symbol, open_[symbol], market, snap)
            if signal is not None:
                signals.append(signal)

        # 2) 시장별 진입 창
        for market in markets_present:
            if not self._market_active(market, snap):
                continue
            tz, session_open_t = _SESSION_OPEN[market]
            now_local = snap.now.astimezone(tz)
            today = now_local.date()

            if market in _PREMARKET_WINDOWS and Scalp1mStrategy._premarket_window_state(
                market, now_local
            ) in ("premarket", "blackout"):
                for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                    signal = self._observe_premarket(
                        symbol, market, snap, today, now_local, symbol in open_,
                        pattern_a_used, pattern_b_used, premarket_confirmed, premarket_checked,
                        gate_cache, pending,
                    )
                    if signal is not None:
                        signals.append(signal)
                continue

            session_open_dt = datetime.combine(today, session_open_t, tzinfo=tz)
            minutes_since_open = (now_local - session_open_dt).total_seconds() / 60
            # 레거시와 동일: 0 = 상한 없음(전 세션 대기), 개장 전만 차단.
            if minutes_since_open < 0 or (
                self.entry_window_minutes and minutes_since_open > self.entry_window_minutes
            ):
                continue
            # KR 개장 초반 진입 지연 게이트 — 레거시와 동일(모듈 docstring "KR
            # 개장 초반 진입 지연 게이트" 절).
            if market == "KR" and minutes_since_open < self.kr_entry_open_delay_min:
                continue

            for symbol in sorted(s for s in self.symbols if market_of_symbol(s) == market):
                if symbol in open_:
                    continue  # 보유 중 — 신규 진입 평가 없음(A/B 순차 진입).
                signal = self._check_entry_for(
                    symbol, market, snap, today,
                    pattern_a_used, pattern_b_used, premarket_confirmed, gate_cache, pending,
                )
                if signal is not None:
                    signals.append(signal)

        next_state = {
            "session_date": session_date, "pattern_a_used": pattern_a_used,
            "pattern_b_used": pattern_b_used, "pending": pending, "open": open_,
            "premarket_confirmed": premarket_confirmed, "premarket_checked": premarket_checked,
            "gate_cache": gate_cache,
        }
        return Decision(signals=tuple(signals), next_state=next_state)

    # ------------------------------------------------------------------ 시장 상태

    def _market_active(self, market: str, snap: StrategySnapshot) -> bool:
        """`Scalp1mStrategy._market_active`와 동치 — `ctx.clock.is_market_open`
        대신 `snap.market_open`, `_premarket_window_state`는 그대로 재사용(정적
        메서드, 순수)."""
        if snap.market_open.get(market, False):
            return True
        if market not in _PREMARKET_WINDOWS:
            return False
        tz, _ = _SESSION_OPEN[market]
        now_local = snap.now.astimezone(tz)
        return Scalp1mStrategy._premarket_window_state(market, now_local) != "closed"

    def _should_flatten(self, market: str, snap: StrategySnapshot) -> bool:
        """`Clock._should_flatten`(quant/core/clock.py) 재현 — `kernel.
        should_flatten_calendar` 참고."""
        mtc = snap.minutes_to_close.get(market)
        return kernel.should_flatten_calendar(mtc, snap.cadence_minutes, self.flatten_minutes)

    # ------------------------------------------------------------------ 추세/변동성 게이트

    def _trend_gate_verdict(
        self, symbol: str, snap: StrategySnapshot, today: dtdate,
        gate_cache: dict[str, tuple[str, str | None]],
    ) -> str | None:
        """`Scalp1mStrategy._trend_gate_reject`의 순수 재구현 — raw 판정(모드와
        무관한 원본 사유)을 반환한다. block 여부는 호출부가 `trend_gate_mode`로
        직접 판단한다(레거시의 `self.gate_verdict` 크로스 사이클 속성 없이도
        `_build_entry`에 그대로 전달할 수 있게)."""
        if self.trend_gate_mode == "off":
            return None
        today_iso = today.isoformat()
        cached = gate_cache.get(symbol)
        if cached is not None and cached[0] == today_iso:
            return cached[1]

        daily_bars = snap.bars.get((symbol, "1d"))
        reason: str | None = None
        di = adx_di(daily_bars) if daily_bars is not None else None
        if di is not None:
            adx, plus_di, minus_di = di
            if adx < self.adx_min or not (plus_di > minus_di):
                reason = f"추세 게이트 미충족(ADX={adx:.1f})"
        if reason is None:
            ratio = atr_ratio(daily_bars) if daily_bars is not None else None
            if ratio is not None and ratio > self.max_atr_ratio:
                reason = f"변동성 과다(ATR/price={ratio:.3f})"

        gate_cache[symbol] = (today_iso, reason)
        return reason

    # ------------------------------------------------------------------ 진입

    def _check_entry_for(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        pattern_a_used: dict[str, bool], pattern_b_used: dict[str, bool],
        premarket_confirmed: dict[str, float], gate_cache: dict[str, tuple[str, str | None]],
        pending: dict[str, dict],
    ) -> Signal | None:
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        today_bars = Scalp1mStrategy._session_bars(bars, market, today)
        if today_bars.empty:
            return None
        open_price = float(today_bars["open"].iloc[0])

        verdict = self._trend_gate_verdict(symbol, snap, today, gate_cache)
        if self.trend_gate_mode == "block" and verdict is not None:
            return None

        basis: tuple[str, float] | None = None
        if not pattern_a_used.get(symbol, False):
            p_pre = premarket_confirmed.get(symbol)
            if p_pre is not None:
                p1_l1 = Scalp1mStrategy._check_pattern_a_accelerated(today_bars, open_price, p_pre)
            else:
                p1_l1 = self._legacy._check_pattern_a(today_bars, bars, open_price)
            if p1_l1 is not None:
                basis = ("A", p1_l1[1])
        elif not pattern_b_used.get(symbol, False):
            ma_val = self._legacy._check_pattern_b(today_bars, bars)
            if ma_val is not None:
                basis = ("B", ma_val)

        if basis is None:
            return None
        pattern, basis_price = basis
        return self._build_entry(
            symbol, pattern, basis_price, today, snap, verdict, premarket=False,
            pending=pending, pattern_a_used=pattern_a_used, pattern_b_used=pattern_b_used,
            bars=bars,
        )

    def _build_entry(
        self, symbol: str, pattern: str, basis_price: float, today: dtdate,
        snap: StrategySnapshot, gate_verdict: str | None, *, premarket: bool,
        pending: dict[str, dict], pattern_a_used: dict[str, bool], pattern_b_used: dict[str, bool],
        bars: pd.DataFrame | None = None,
    ) -> Signal | None:
        quote = snap.quotes.get(symbol)
        if quote is None or quote.price <= 0:
            return None
        entry_price = quote.price

        # 손절/과열 판정은 레거시의 상태 없는 헬퍼를 그대로 공유한다(동치의
        # 원천을 한 벌로 — 클래스 docstring "왜 self._legacy" 절).
        w_verdict = self._legacy._williams_verdict(bars)
        if self.williams_gate_mode == "block" and w_verdict is not None:
            return None
        stop, _reject, stop_note = self._legacy._entry_stop(entry_price, basis_price, bars)
        if stop is None:
            return None

        target_weight = 0.5
        pending[symbol] = {
            "entry": entry_price, "stop": stop, "pattern": pattern,
            "session": today.isoformat(), "partial_taken": False,
        }
        if pattern == "A":
            pattern_a_used[symbol] = True
        else:
            pattern_b_used[symbol] = True

        tag = "프리마켓 " if premarket else ""
        gate_note = ""
        if self.trend_gate_mode != "off":
            gate_note = f" [게이트:차단후보 {gate_verdict}]" if gate_verdict else " [게이트:통과]"
        w_note = ""
        if self.williams_gate_mode != "off":
            w_note = f" [W%R:차단후보 {w_verdict}]" if w_verdict else " [W%R:통과]"
        return Signal(
            strategy_id=self.id,
            symbol=symbol,
            action=SignalAction.ENTER_LONG,
            target_weight=target_weight,
            reason=(
                f"1분봉 스캘프 {tag}패턴{pattern} 진입: {symbol} w={target_weight:.2f} "
                f"손절={fmt_price(stop, symbol)} (기준={fmt_price(basis_price, symbol)})"
                f"{stop_note}{gate_note}{w_note}"
            ),
            stop=stop,
        )

    # ------------------------------------------------------------------ 프리마켓

    def _observe_premarket(
        self, symbol: str, market: str, snap: StrategySnapshot, today: dtdate,
        now_local: datetime, is_open: bool,
        pattern_a_used: dict[str, bool], pattern_b_used: dict[str, bool],
        premarket_confirmed: dict[str, float],
        premarket_checked: dict[str, str], gate_cache: dict[str, tuple[str, str | None]],
        pending: dict[str, dict],
    ) -> Signal | None:
        bars = snap.bars.get((symbol, _INTERVAL))
        if bars is None or bars.empty:
            return None
        pre_bars = Scalp1mStrategy._session_bars(bars, market, today, premarket=True)
        if pre_bars.empty:
            return None
        open_price = float(pre_bars["open"].iloc[0])
        state = Scalp1mStrategy._premarket_window_state(market, now_local)

        if state == "blackout":
            if premarket_checked.get(symbol) != today.isoformat():
                premarket_checked[symbol] = today.isoformat()
                p_pre = self._legacy._check_premarket_pattern(pre_bars, bars, open_price)
                if p_pre is not None:
                    premarket_confirmed[symbol] = p_pre
            return None

        # state == "premarket" — 직접 진입 평가(패턴 A만).
        # 레거시와 동일: 연속 호가창이 없는 시장(KR)은 구조적으로 진입 불가.
        if market not in _PREMARKET_DIRECT_ENTRY_MARKETS:
            return None
        if not self.premarket_entry:
            return None
        if is_open:
            return None
        if pattern_a_used.get(symbol, False):
            return None
        verdict = self._trend_gate_verdict(symbol, snap, today, gate_cache)
        if self.trend_gate_mode == "block" and verdict is not None:
            return None

        p1_l1 = self._legacy._check_pattern_a(pre_bars, bars, open_price)
        if p1_l1 is None:
            return None
        p1, l1 = p1_l1

        last_bar = pre_bars.iloc[-1]
        notional = float(last_bar["close"]) * float(last_bar["volume"])
        min_notional = self.premarket_min_volume_krw if market == "KR" else self.premarket_min_volume_usd
        if notional < min_notional:
            return None

        return self._build_entry(
            symbol, "A", l1, today, snap, verdict, premarket=True,
            pending=pending, pattern_a_used=pattern_a_used, pattern_b_used=pattern_b_used,
            bars=bars,
        )

    # ------------------------------------------------------------------ 관리

    def _manage(self, symbol: str, lot: dict, market: str, snap: StrategySnapshot) -> Signal | None:
        """`lot`은 `decide()`가 만든 이번 사이클 로컬 사본(`open_[symbol]`)이다 —
        여기서의 in-place 갱신은 `next_state`에만 반영되고 `Position.meta`는
        건드리지 않는다(donchian_pure `_manage`와 동일 원칙)."""
        quote = snap.quotes.get(symbol)
        if quote is None:
            return None
        price = quote.price
        entry, stop = lot["entry"], lot["stop"]
        tz, _ = _SESSION_OPEN[market]

        entry_session = lot.get("session")
        if entry_session and entry_session != snap.now.astimezone(tz).date().isoformat():
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"세션 롤 강제청산(오버나잇 금지): 진입 {entry_session} 현재={fmt_price(price, symbol)}",
            )
        if self._should_flatten(market, snap):
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=f"EoD 청산: entry={fmt_price(entry, symbol)} 현재={fmt_price(price, symbol)}",
            )
        # 본전 이동 + 고수위 트레일 — 레거시 `_manage_position`과 동일(그쪽 주석
        # 참고). `lot`은 이번 사이클 로컬 사본이라 in-place 갱신이 next_state 로만
        # 흘러간다(partial_taken 과 같은 경로).
        lot.setdefault("r0", entry - stop)
        if self.breakeven_at_bp or self.trail_bp:
            hi = max(float(lot.get("hi", entry)), price)
            lot["hi"] = hi
            raised = stop
            # **트레일은 이익 구간에 들어간 뒤에만 작동한다**(2026-08-28 수리).
            # 이전 구현은 진입 직후부터 `hi*(1-trail)` 로 스탑을 올려, 고수위가
            # 아직 진입가일 때도 손절선을 진입가 -trail_bp 로 **조여버렸다**.
            # 실거래 확증: 096770 구조손절 117,216(-111bp) → 트레일이 117,571
            # (-70bp)로 조임 → 117,500 에서 청산. 원래 손절선이었으면 살아남았다.
            # 원장 실측이 방향을 확정한다: 손절당한 뒤 **76%(35/46)가 당일 진입가
            # 위로 회복**했고 회복 폭 중앙 +105bp — 진입이 틀린 게 아니라 손절이
            # 노이즈에 걸린 것이다(소유자 지적과 일치).
            armed = bool(lot.get("trail_armed")) or (
                self.breakeven_at_bp and hi >= entry * (1 + self.breakeven_at_bp / 1e4)
            )
            if armed:
                lot["trail_armed"] = True
                raised = max(raised, entry)          # 본전 확보
                if self.trail_bp:
                    raised = max(raised, hi * (1 - self.trail_bp / 1e4))
            if raised > stop:
                lot["stop"] = stop = raised
        if price <= stop:
            kind = "이익보호 청산(본전/트레일)" if stop >= entry else "손절"
            return Signal(
                strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                target_weight=0.0, exit_fraction=1.0,
                reason=(
                    f"{kind}: entry={fmt_price(entry, symbol)} stop={fmt_price(stop, symbol)} "
                    f"현재={fmt_price(price, symbol)}"
                ),
            )
        # 전량 익절 — 하드 손절 바로 다음, MA60 잔량 트레일보다 **앞**. 익절가는
        # 하드 목표라 트레일 판정보다 우선한다. 0(기본)이면 이 블록은 없는 것과
        # 같다(근거는 생성자의 take_profit_bps 주석).
        if self.take_profit_bps:
            take = entry * (1 + self.take_profit_bps / 1e4)
            if price >= take:
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(f"전량 익절(+{self.take_profit_bps:g}bp): entry={fmt_price(entry, symbol)} "
                            f"목표={fmt_price(take, symbol)} 현재={fmt_price(price, symbol)}"),
                )

        ma_bars = snap.bars.get((symbol, _INTERVAL))
        if ma_bars is not None and len(ma_bars) >= self.ma_period:
            ma_last = sma(ma_bars["close"], self.ma_period).iloc[-1]
            last_close = float(ma_bars["close"].iloc[-1])
            if pd.notna(ma_last) and last_close < float(ma_last):
                return Signal(
                    strategy_id=self.id, symbol=symbol, action=SignalAction.EXIT_LONG,
                    target_weight=0.0, exit_fraction=1.0,
                    reason=(
                        f"60선 이탈(잔량 트레일): 종가={fmt_price(last_close, symbol)} "
                        f"MA60={fmt_price(float(ma_last), symbol)}"
                    ),
                )

        if not lot.get("partial_taken"):
            r = float(lot.get("r0", entry - stop))
            if r > 0:
                target = entry + self.partial_take_r * r
                if price >= target:
                    lot["partial_taken"] = True
                    return Signal(
                        strategy_id=self.id, symbol=symbol, action=SignalAction.SCALE_OUT,
                        target_weight=0.0, exit_fraction=self.partial_fraction,
                        reason=(
                            f"절반 익절(+{self.partial_take_r:g}R): entry={fmt_price(entry, symbol)} "
                            f"목표={fmt_price(target, symbol)} 현재={fmt_price(price, symbol)} "
                            f"{self.partial_fraction * 100:.0f}% 청산"
                        ),
                        state_update={"partial_taken": True},
                    )
        return None


class Scalp1mPureShell(PureStrategyShell):
    """`STRATEGY_REGISTRY`/`build_strategies`가 기존 전략과 같은 방식으로
    생성할 수 있게 하는 얇은 팩토리 — `donchian.py`의 `DonchianPureShell`과
    동일 패턴."""

    def __init__(self, symbols: list[str], params: dict, market: str = "US", id: str = "scalp_1m_pure"):
        super().__init__(Scalp1mPureStrategy(symbols, params, market=market, id=id))
