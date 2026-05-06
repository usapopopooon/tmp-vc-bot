.PHONY: setup install dev test test-db test-db-start test-db-stop lint typecheck yamlcheck ci run clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

setup: $(VENV)/bin/activate

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

install: setup

dev: setup
	$(PIP) install -e ".[dev]"

test: setup
	$(PYTHON) -m pytest

lint: setup
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check .

typecheck: setup
	$(VENV)/bin/mypy src

yamlcheck:
	yamllint -s .

run: setup
	$(PYTHON) -m src.main

clean:
	rm -rf $(VENV)
	rm -rf __pycache__
	rm -rf .pytest_cache
	rm -rf .mypy_cache
	rm -rf .ruff_cache
	rm -rf *.egg-info
	rm -rf .coverage
	rm -rf htmlcov
	find . -type d -name "__pycache__" -exec rm -rf {} +

# PostgreSQL を使ったテスト
test-db-start:
	docker compose --profile dev up -d test-db
	@echo "Waiting for PostgreSQL..."
	@until docker compose exec -T test-db pg_isready -U test_user -d tmp_vc_bot_test > /dev/null 2>&1; do sleep 1; done
	@echo "PostgreSQL is ready at localhost:5433"

test-db-stop:
	docker compose --profile dev down

# CI チェック (GitHub Actions と同じ)
ci: setup
	@echo "=== YAML Lint ==="
	yamllint -s .
	@echo "=== Ruff Format ==="
	$(VENV)/bin/ruff format --check .
	@echo "=== Ruff Lint ==="
	$(VENV)/bin/ruff check src tests
	@echo "=== Type Check ==="
	$(VENV)/bin/mypy src
	@echo "=== All CI checks passed! ==="
