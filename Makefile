# Convenience targets (Linux/macOS). On Windows see scripts/demo.ps1.
DATA ?= ./cow_data
DATE ?= 2024-07-10

.PHONY: install test lint serve simulate report up seed down tf-up tf-down

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests && ruff format --check src tests && mypy

serve:
	cowshed serve --port 8000

simulate:
	cowshed simulate --data-dir $(DATA) --api-url http://127.0.0.1:8000 --concurrency 16

report:
	cowshed report --date $(DATE)

up:
	docker compose up -d --build

seed:
	COW_DATA_DIR=$(DATA) docker compose --profile seed run --rm seed

down:
	docker compose down

tf-up:
	cd terraform && terraform init && terraform apply

tf-down:
	cd terraform && terraform destroy
