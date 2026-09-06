.DEFAULT_GOAL := help

.PHONY: help install test test-fast backtest paper report fetch lint deploy ml

help: ## 사용 가능한 타겟 목록
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install: ## 의존성 설치 (uv sync)
	uv sync

test: ## 전체 테스트 실행
	uv run pytest

test-fast: ## 전체 테스트 실행 (pytest-xdist 병렬, 로컬 개발용 — CLAUDE.md 검증 커맨드는 여전히 `make test`)
	uv run pytest -q -n auto

backtest: ## Donchian 전략 stub 백테스트 (90일)
	uv run python -m quant.apps.cli backtest --strategy donchian --days 90

paper: ## 모의투자(paper) 루프 실행
	uv run python -m quant.apps.cli paper

report: ## Private Banker 일일 계좌 진단 리포트 (Toss 실계좌 읽기 전용 — paper 에서도 동작)
	uv run python -m quant.apps.cli report

fetch: ## 과거 봉 수집 (yfinance / Alpaca / Toss)
	uv run python -m quant.apps.cli fetch

lint: ## ruff 정적 검사 (설정: pyproject.toml [tool.ruff])
	uv run ruff check quant tests

deploy: ## EC2에 배포 (QT_SSH_HOST=ubuntu@<ElasticIP> make deploy). 절차: docs/runbooks/deploy.md
	./server/scripts/deploy.sh

ml: ## 로컬 맥 원버튼 ML 파이프라인 — EC2 동기화 → 표본 게이트 → (충족시) 학습 (local/ml/README.md)
	./local/ml/run.sh
