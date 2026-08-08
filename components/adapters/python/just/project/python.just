root := justfile_directory()

[working-directory: root]
sync:
    uv sync

[working-directory: root]
format:
    uv run ruff format .
    uv run ruff check --fix .

[working-directory: root]
typecheck:
    uv run mypy .
