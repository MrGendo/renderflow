.PHONY: install dev-api dev-worker test

install:
	cd backend && uv venv --python 3.11 && uv pip install -e ".[dev]"

dev-api:
	cd backend && uv run uvicorn renderflow.api:app --port 8900

dev-worker:
	cd backend && uv run python -m renderflow.worker

test:
	cd backend && uv run pytest -q
