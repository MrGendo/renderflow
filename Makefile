.PHONY: install dev-api dev-worker test demo-1 demo-2 demo-3 demo-4 demo-5 demo-all

install:
	cd backend && uv venv --python 3.11 && uv pip install -e ".[dev]"

dev-api:
	cd backend && uv run uvicorn renderflow.api:app --port 8900

dev-worker:
	cd backend && uv run python -m renderflow.worker

test:
	cd backend && uv run pytest -q

demo-1:
	cd backend && uv run python scripts/demo_1_normal.py
demo-2:
	cd backend && uv run python scripts/demo_2_retry_recover.py
demo-3:
	cd backend && uv run python scripts/demo_3_worker_restart.py
demo-4:
	cd backend && uv run python scripts/demo_4_cache_hit.py
demo-5:
	cd backend && uv run python scripts/demo_5_retry_exhausted.py
demo-all: demo-1 demo-2 demo-3 demo-4 demo-5
