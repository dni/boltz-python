.PHONY: check

check:
	uv run black boltz_client
	uv run isort boltz_client
	uv run mypy boltz_client
	uv run ruff check .
	uv run pylint boltz_client
