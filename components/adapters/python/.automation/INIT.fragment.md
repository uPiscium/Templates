## Python Project Adapter initialization

- `just project::environment` reports the worktree-local Python environment boundary without mutating project files.
- `flake.lock` and `uv.lock` are repository-owned dependency identity. `just project::bootstrap [name]` resolves the project-name placeholder first, then creates only missing lockfiles; existing lockfiles are preserved byte-for-byte.
- `.venv`, `.direnv`, and Python caches are ignored local artifacts; lockfiles are not caches and are not ignored.
- `just project::doctor` requires `uv`, `ruff`, `mypy`, and `pytest` to be available through that boundary.
- Verification recipes use `uv run --locked`, so missing or stale `uv.lock` blocks verification instead of updating dependency identity.
- The managed virtual environment is always the current worktree's `.venv`; inherited `VIRTUAL_ENV`, `UV_PROJECT_ENVIRONMENT`, or `PATH` entries for another `.venv/bin` are not trusted.
- `PIP_REQUIRE_VIRTUALENV=1` is enforced by Project Adapter execution rather than inherited ambient shell state.
- Dependency synchronization is explicit through `just project::python::sync`; it may update `uv.lock` when project metadata requires it. `/init` remains read-only and does not create or update either lockfile.
