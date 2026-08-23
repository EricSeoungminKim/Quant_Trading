# 백테스트 노트북

## 실행법

```bash
uv sync                          # ipykernel/matplotlib/nbformat 포함 dev 의존성 설치
uv run jupyter lab backtest/donchian_tqqq_15m.ipynb
```

또는 VS Code에서 노트북을 열고 커널 선택 시 이 레포의 `.venv`를 선택한다.

## 백테스트 철학

- **라이브와 같은 코드 경로.** 백테스트는 `quant.backtest`를 통해 라이브와
  동일한 `run_cycle`(MarketData → Signal → Risk → Order → Fill)을 리플레이한다.
  전략/리스크 로직을 백테스트 전용으로 따로 구현하지 않는다 — 로직이 갈라지는
  순간 백테스트 결과는 의미가 없어진다.
- **look-ahead 금지.** 데이터 핸들러의 `history()`는 리플레이 시점 이전에 완성된
  봉만 반환한다. 미래 데이터를 참조하는 지표/조건은 만들지 않는다.
- **stub 데이터로 먼저 스모크.** 실데이터 연결 전, 파이프라인 자체가 도는지
  stub 소스로 확인한 뒤 실데이터로 교체한다.
