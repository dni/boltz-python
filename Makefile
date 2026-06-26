.PHONY: check format lint

check: format lint

format:
	uv run ruff check . --fix
	uv run black boltz_client
	uv run isort boltz_client

lint:
	uv run mypy boltz_client
	uv run ruff check .
	uv run pylint boltz_client
