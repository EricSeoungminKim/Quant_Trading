# quant/trade/risk/

## 한 줄 정의

**사이징 + 하드레일** — 전략이 낸 `Signal`(목표 비중)을 실제 주문 수량으로
바꾸고, 그 과정에서 넘지 말아야 할 선을 강제한다. 거래 평면 소속: 틀리면
**돈을 잃는다**(과다 사이징) 또는 **기회를 잃는다**(과소 승인). `quant.core.ports.RiskManager`
Protocol의 구현체가 여기 있다.

## 주요 파일 지도

- `manager.py` — `RiskManagerImpl`(73KB, 이 디렉터리의 핵심). 사이징 계산 +
  하드레일(최대 포지션 수, 종목당/전략당 한도, 국면 배수 적용 등) + `approve()`
  진입점.
- `books.py` — 전략별 독립 명목계정 장부(`risk.capital_mode: per_strategy`일 때
  전략마다 분리된 자본 풀을 추적).

## 핵심 불변식

- `quant/trade/` 소속이므로 상위 규칙을 그대로 물려받는다: `collect`·`analyze`·
  `adapters`·`apps` 임포트 금지, `httpx`/`redis`/`pymysql` 등 네트워크·DB 라이브러리
  금지(`tests/test_architecture.py`의 `FORBIDDEN_EXTERNAL["quant.trade"]`).
- `RiskManager.approve()`는 거부 시 `None`을 반환한다 — 예외를 던지지 않는다
  (`quant/core/ports.py` Protocol 계약, "예외가 아니라 None/빈 반환값으로 실패를
  표현").
- 사이징은 **잔여룸 기준 증분**으로 계산한다(전략별 README의 "공통 시맨틱" 참고) —
  이미 보유 중이어도 유효한 신호를 무조건 막지 않고, 여기서 레일로 폭주를 막는다.
- 국면(regime) 배수(방어 0.5x/중립 1.0x/공격 1.5x)는 시장별(US/KR)로 분리돼
  사이징에 곱해진다 — `quant/trade/regime/`이 그 배수를 계산하고 여기서 소비한다.

## 데이터 흐름

**상류**: `quant/trade/strategy/*`가 낸 `Signal`, `quant/trade/regime/*`의 국면
배수, `config/settings.yaml`의 리스크 파라미터(핫 리로드). **하류**: `Order`
(수량 확정) → `quant/trade/loop.py`가 `broker.place_order`로 넘긴다.

## 손대기 전에

- `uv run pytest`에서 `manager.py`/`books.py` 관련 테스트를 먼저 확인(사이징
  버그는 실제 주문 수량에 직결).
- `uv run python -m quant.apps.cli backtest --strategy donchian --days 90` —
  리스크 레일이 백테스트 경로에서도 동일하게 적용되는지 확인(백테스트는
  `quant.backtest`를 통해 라이브와 같은 `run_cycle`을 공유한다).
- `uv run pytest -q tests/test_architecture.py -v` — 임포트 경계 확인.
