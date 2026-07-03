# Development and deployment tasks for the serverless ETL pipeline.

PYTHON      ?= python3.12
VENV        := .venv
BIN         := $(VENV)/bin
DIST        := dist
TF_DIR      := infra

.PHONY: help install lint fmt test package deploy plan destroy clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Create a virtualenv and install dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

lint: ## Run ruff checks and verify formatting
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests

fmt: ## Auto-format the codebase
	$(BIN)/ruff check --fix src tests
	$(BIN)/ruff format src tests

test: ## Run the test suite with coverage
	$(BIN)/pytest --cov=src --cov-report=term-missing

package: ## Build Lambda deployment zips (layer + functions) into dist/
	rm -rf $(DIST)
	mkdir -p $(DIST)/layer/python
	cp -R src/shared $(DIST)/layer/python/shared
	find $(DIST)/layer -name '__pycache__' -type d -exec rm -rf {} +
	cd $(DIST)/layer && zip -qr ../layer.zip python
	cd src/functions/ingest && zip -qj ../../../$(DIST)/ingest.zip handler.py
	cd src/functions/alert && zip -qj ../../../$(DIST)/alert.zip handler.py
	@ls -lh $(DIST)/*.zip

plan: package ## Terraform plan
	terraform -chdir=$(TF_DIR) init
	terraform -chdir=$(TF_DIR) plan

deploy: package ## Terraform apply
	terraform -chdir=$(TF_DIR) init
	terraform -chdir=$(TF_DIR) apply

destroy: ## Tear the stack down
	terraform -chdir=$(TF_DIR) destroy

clean: ## Remove build artifacts and caches
	rm -rf $(DIST) .pytest_cache .ruff_cache .coverage htmlcov
	find . -name '__pycache__' -type d -not -path './$(VENV)/*' -exec rm -rf {} +
