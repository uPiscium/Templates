root := justfile_directory()
environment_script := root / "just/project/environment.py"

[working-directory: root]
sync:
    python3 {{quote(environment_script)}} exec uv sync

[working-directory: root]
format:
    python3 {{quote(environment_script)}} exec uv run ruff format src tests
    python3 {{quote(environment_script)}} exec uv run ruff check --fix src tests

[working-directory: root]
typecheck:
    python3 {{quote(environment_script)}} exec uv run mypy src tests
