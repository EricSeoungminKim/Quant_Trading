# quant/research/

## 한 줄 정의

**파라미터 최적화 + walk-forward 검증 레이어** — 전략 파라미터를 탐색하고,
그 결과가 과최적화가 아닌지 검증한다. `quant/backtest/`(엔진 자체) 위에서
동작하는 실험 도구 층. 틀리면: 과최적화된 파라미터를 "검증됐다"고 착각해
라이브에 올리게 된다 — 돈으로 직결되지는 않지만 그 다음 단계(제어 평면의
자동 반영)를 오염시킨다.

## 주요 파일 지도

- `optimize.py` — Optuna 기반 파라미터 탐색.
- `param_spaces.py` — 전략별 기본 param space — `quant.apps.cli optimize`가
  `--param-space` 없이도 동작하게 하는 기본값.
- `walkforward.py` — Rolling-window walk-forward 검증 — 과최적화 방지의 핵심 장치.
- `pathstats.py` — 자산곡선의 **경로**를 보는 통계 — 끝점 하나로 요약된 성과가
  숨기는 것들(최대낙폭 지속기간, 회복 시간 등).
- `signalquality.py` — 진입 **신호 자체**의 품질을 청산 규칙과 완전히 분리해서 본다.
- `sample_guard.py` — 표본 크기 정직성 가드 — 표본이 작을 때 "권장 파라미터"를
  확신에 찬 것처럼 보여주지 않게 막는다.
- `report.py` — walk-forward 결과 렌더링: 평문 요약(항상 동작) + quantstats
  HTML(선택 의존성).

## 핵심 불변식

- 파라미터 탐색 결과는 반드시 walk-forward(OOS) 검증을 거친다 — in-sample
  최적화 결과 단독으로 "좋은 파라미터"라고 주장하지 않는다.
- `sample_guard.py`가 가드하는 원칙: 표본이 작으면 확신을 낮춘다 — 숫자를
  부풀려 보여주지 않는다.
- `quant/backtest/engine.py`(라이브와 같은 `run_cycle`)를 그대로 재사용한다 —
  탐색 전용 시뮬레이션 로직을 별도로 만들지 않는다.

## 데이터 흐름

**상류**: `quant/backtest/engine.py`(리플레이 엔진), `quant/trade/strategy/*`
(탐색 대상 파라미터). **하류**: `quant/apps/cli.py optimize` 서브커맨드가
호출, 결과는 `quant/analyze/param_proposer.py`(AI 트레이더 파라미터 제안
단계)나 사람의 판단으로 `config/settings.yaml`에 반영된다 — 자동 반영은
`quant/control/governor.py`를 거친다.

## 손대기 전에

- `uv run python -m quant.apps.cli backtest --strategy donchian --days 90` —
  최소한 엔진 자체가 도는지 먼저 확인(탐색은 이 위에서 반복 실행되므로 느리다).
- 탐색 결과를 파라미터 채택 근거로 쓰려면 `quant-expert` 스킬 발동이 필수
  (백테스트/최적화 숫자 해석은 적대적 교차검증 대상, 루트 CLAUDE.md).
- `uv run pytest`에서 `tests/test_walkforward.py`, `tests/test_optimize.py` 확인.
