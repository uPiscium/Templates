## Python Project Adapter initialization

- `just project::environment` reports the worktree-local Python environment boundary without mutating project files.
- `just project::doctor` requires `uv`, `ruff`, `mypy`, and `pytest` to be available through that boundary.
- The managed virtual environment is always the current worktree's `.venv`; inherited `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, or `PATH` entries for another `.venv/bin` are not trusted.
- `PIP_REQUIRE_VIRTUALENV=1` is enforced by Project Adapter execution rather than inherited ambient shell state.
- Dependency synchronization is explicit through `just project::python::sync`; `/init` remains read-only and does not run it.
