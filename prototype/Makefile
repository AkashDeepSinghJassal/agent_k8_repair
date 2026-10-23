install:
	uv sync

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

cluster-up:
	bash cluster/bootstrap.sh

cluster-down:
	bash cluster/teardown.sh

operator:
	uv run kopf run -m repair_operator.main --all-namespaces --verbose

dashboard:
	uv run uvicorn repair_dashboard.app:app --port 8090

demo-up:
	bash scripts/start_demo.sh

demo-down:
	bash scripts/stop_demo.sh
