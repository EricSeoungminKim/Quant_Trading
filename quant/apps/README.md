# quant/apps/

## 한 줄 정의

**진입점 3개** — 이 시스템을 밖에서 두드리는 유일한 문. CLI 파싱, 설정 로딩,
어댑터 배선(composition root)이 여기 모인다. 어느 4평면에도 속하지 않지만
`quant/core/`·`quant/adapters/`(→ `quant/trade/`) 바로 위에서 전부를 조립한다.
틀리면: 잘못된 설정이 그대로 엔진에 전달되거나, 배선이 꼬여 엉뚱한 브로커/데이터
소스가 붙는다.

## 주요 파일 지도

- `cli.py` — CLI 엔트리포인트: `python -m quant.apps.cli {backtest|paper|report}`
  (216KB, 저장소 최대 파일 — 서브커맨드가 계속 늘어난 결과. `scoreboard`,
  `health`, `narrate`, `outcomes`, `shadow-judge`, `backup` 등도 여기 붙는다).
- `assembly.py` — **Composition root** — 어댑터를 코어에 배선하는 유일한 장소
  (62KB). 브로커/데이터피드/알림 선택은 여기서 `config/settings.yaml` 값에
  따라 결정된다.
- `config.py` — 설정 로더: `config/settings.yaml` + `.env`/`.env.local`. 핫
  리로드 지원. 시크릿은 `.env(.local)`에서, 전략 파라미터는 `settings.yaml`
  에서 — 원칙적으로 섞지 않는다. `auto_params.yaml` 오버레이를 `settings.yaml`
  위에 깊은 병합(거버너가 주석 없는 이 파일만 소유 — `settings.yaml`의 사람이
  쓴 주석을 기계가 지우지 않도록).
- `report_cli.py` — 리포트 CLI(44KB). Phase 1은 크론 없이 손으로 돌려 검증.
- `deepdive.py` — 야간 심화 배치: 본문 → LLM 후보 → 결정론 검증 → 관계 사전.
- `warehouse_cli.py` — 분석 저장소 CLI: `python -m quant.apps.warehouse_cli
  {migrate,ingest,status}`.

## 핵심 불변식

- 여기가 **유일하게 4평면 전체(core/collect/analyze/trade/control/adapters)를
  다 알아도 되는 곳**이다 — 나머지 평면들이 서로를 모르게 짜여 있는 만큼,
  조립 책임은 `apps/`에 집중된다.
- `assembly.py`가 composition root라는 규칙을 지킨다 — 배선 로직을 다른 평면에
  흩뿌리지 않는다(예: 새 브로커 배선은 `assembly.py` 또는 `cli.py`에서, 브로커
  코드 안에서가 아니다).
- 시크릿은 `.env.local`에서만 읽는다(`env.py`를 통해) — `settings.yaml`에 시크릿
  값을 직접 넣지 않는다.

## 데이터 흐름

**상류**: `config/settings.yaml`, `.env.local`, CLI 인자. **하류**: `quant/adapters/*`
인스턴스를 조립해 `quant/trade/loop.py`(엔진), `quant/report/*`(리포트 렌더)로
넘긴다. systemd(`server/systemd/quant-engine.service`)와 crontab이 실제로
`.venv/bin/python -m quant.apps.cli ...` 형태로 이 모듈들을 호출한다.

## 손대기 전에

- `uv run python -m quant.apps.cli backtest --strategy donchian --days 90` —
  거래 스모크(루트 CLAUDE.md의 완료 전 필수 검증 커맨드).
- `uv run python -m quant.apps.report_cli --help` — 리포트 스모크.
- `config.py`(설정 로더)를 건드렸다면 핫 리로드 경로(`reload_if_changed`)를
  직접 확인 — 엔진이 장중에 이 함수로 파라미터를 다시 읽는다.
- `assembly.py`에 새 배선을 추가했다면 `uv run pytest -q tests/test_architecture.py -v`
  로 의존 방향이 아직 깨지지 않았는지 확인.
