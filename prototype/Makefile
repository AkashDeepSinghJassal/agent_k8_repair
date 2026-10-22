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
	@echo "see #5"
