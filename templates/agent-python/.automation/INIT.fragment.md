## Python Project Adapter initialization

- `just project::doctor` requires `uv`, `ruff`, `mypy`, and `pytest` to be available.
- The managed virtual environment is project-local at `.venv` through `UV_PROJECT_ENVIRONMENT=$PWD/.venv`.
- `PIP_REQUIRE_VIRTUALENV=1` must remain enabled in the development shell.
- Dependency synchronization is explicit through `just project::python::sync`; `/init` remains read-only and does not run it.
