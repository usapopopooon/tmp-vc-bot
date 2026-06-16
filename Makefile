.PHONY: setup install dev test test-db test-db-start test-db-stop lint typecheck yamlcheck ci run clean

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
TEST_DB_CONTAINER := tmp-vc-bot-test-db
TEST_DATABASE_URL := postgresql+asyncpg://user:password@localhost:5433/tmp_vc_bot_test
TEST_DATABASE_URL_SYNC := postgresql://user:password@localhost:5433/tmp_vc_bot_test

setup: $(VENV)/bin/activate

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

install: setup

dev: setup
	$(PIP) install -e ".[dev]"

test: setup
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_DATABASE_URL_SYNC=$(TEST_DATABASE_URL_SYNC) $(PYTHON) -m pytest

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
	-docker rm -f $(TEST_DB_CONTAINER)
	docker run -d --name $(TEST_DB_CONTAINER) \
		-e POSTGRES_USER=user \
		-e POSTGRES_PASSWORD=password \
		-e POSTGRES_DB=tmp_vc_bot_test \
		-p 127.0.0.1:5433:5432 \
		postgres:17-alpine
	@echo "Waiting for PostgreSQL..."
	@until docker exec $(TEST_DB_CONTAINER) pg_isready -U user -d tmp_vc_bot_test > /dev/null 2>&1; do sleep 1; done
	@echo "PostgreSQL is ready at localhost:5433"

test-db-stop:
	docker rm -f $(TEST_DB_CONTAINER)

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
