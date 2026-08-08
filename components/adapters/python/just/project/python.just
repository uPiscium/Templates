root := justfile_directory()

[working-directory: root]
sync:
    uv sync

[working-directory: root]
format:
    uv run ruff format src tests
    uv run ruff check --fix src tests

[working-directory: root]
typecheck:
    uv run mypy src tests
