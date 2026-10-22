install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

cluster-up:
	@echo "see #3"

cluster-down:
	@echo "see #3"

operator:
	@echo "see #4"

dashboard:
	uv run uvicorn repair_dashboard.app:app --port 8090
