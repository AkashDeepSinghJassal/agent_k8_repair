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
	@echo "see #4"

dashboard:
	@echo "see #5"
