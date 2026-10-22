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
	uv run kopf run -m repair_operator.main --all-namespaces --verbose

dashboard:
	@echo "see #5"
