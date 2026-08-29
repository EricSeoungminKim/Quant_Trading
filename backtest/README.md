# backtest/

## 한 줄 정의

**백테스트 노트북 폴더** — 사람이 직접 돌리는 실험 공간(Jupyter). `quant/backtest/`
(엔진 코드, git 추적되는 패키지)와 짝을 이루지만 다른 것 — 여기는 그 엔진을
불러다 실험하는 자리다. 틀리면: 백테스트 결과를 실제 성과로 오인하게 되어
나쁜 전략을 라이브에 올리게 된다(직접 돈을 잃지는 않지만 판단을 그르친다).

## 주요 파일 지도

- `donchian_tqqq_15m.ipynb` — Donchian 전략(TQQQ/SQQQ) 백테스트 노트북.
- `portfolio_analysis.ipynb` — 포트폴리오/자본곡선 분석 노트북(273KB, gs-quant
  대조 등 최신 분석 포함, 2026-08-29 갱신).
- `data/state/` — 노트북 실행 중간 산출물(런타임, git 추적 여부는 `.gitignore`
  참고).

## 핵심 불변식

- **라이브와 같은 코드 경로.** 백테스트는 `quant.backtest`를 통해 라이브와
  동일한 `run_cycle`(MarketData → Signal → Risk → Order → Fill)을 리플레이한다.
  전략/리스크 로직을 백테스트 전용으로 따로 구현하지 않는다 — 로직이 갈라지는
  순간 백테스트 결과는 의미가 없어진다.
- **look-ahead 금지.** 데이터 핸들러의 `history()`는 리플레이 시점 이전에 완성된
  봉만 반환한다. 미래 데이터를 참조하는 지표/조건은 만들지 않는다.
- **stub 데이터로 먼저 스모크.** 실데이터 연결 전, 파이프라인 자체가 도는지
  stub 소스로 확인한 뒤 실데이터로 교체한다.

## 데이터 흐름

**상류**: `quant.backtest.engine`(리플레이 엔진), `quant/trade/strategy/*`(전략
로직, 라이브와 동일), `data/history/`(로컬 Parquet) 또는 `quant/adapters/data/stub.py`
(합성 데이터). **하류**: 노트북 안에서 그래프/통계로 소비될 뿐 — 결과가 자동으로
`config/settings.yaml`에 반영되지 않는다(사람이 판단해 반영).

## 손대기 전에

```bash
uv sync                          # ipykernel/matplotlib/nbformat 포함 dev 의존성 설치
uv run jupyter lab backtest/donchian_tqqq_15m.ipynb
```

또는 VS Code에서 노트북을 열고 커널 선택 시 이 레포의 `.venv`를 선택한다.

- 새 실험 노트북을 추가하기 전에 `quant.backtest.engine`이 여전히 정상 동작하는지
  `uv run python -m quant.apps.cli backtest --strategy donchian --days 90`로 먼저
  확인.
- 성과 지표를 근거로 전략 채택을 논의하려면 `quant-expert` 스킬 발동이 필수
  (루트 CLAUDE.md) — 백테스트 숫자 해석은 적대적 서브에이전트 교차검증 대상이다.
