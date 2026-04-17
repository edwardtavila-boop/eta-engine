.PHONY: test lint verify backtest-demo firm-gate preflight all clean

PY ?= python
SPEC ?= eta_engine/docs/firm_spec_crypto_perp.json

test:
	pytest tests/ -q

lint:
	ruff check eta_engine
	ruff format --check eta_engine

verify:
	$(PY) -m eta_engine.scripts.verify_all

backtest-demo:
	$(PY) -m eta_engine.scripts.run_backtest_demo

firm-gate:
	$(PY) -m eta_engine.scripts.engage_firm_board --spec $(SPEC)

preflight:
	$(PY) -m eta_engine.scripts.preflight

all: lint test verify

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache coverage.xml .coverage
