.PHONY: test lint run eval

test:
	uv run pytest -q

lint:
	uv run ruff check app tests scripts evals

run:
	uv run uvicorn app.main:create_app --factory --reload --port 8000

eval:
	uv run python evals/run.py
