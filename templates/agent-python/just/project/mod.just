root := justfile_directory()
bootstrap_script := root / ".automation/bin/project_bootstrap.py"
environment_script := root / "just/project/environment.py"
lockfiles_script := root / "just/project/lockfiles.py"

mod python 'python.just'
mod? repository 'repository.just'

[working-directory: root]
bootstrap name='':
    python3 {{quote(bootstrap_script)}} {{quote(name)}}
    python3 {{quote(lockfiles_script)}} ensure

[working-directory: root]
environment:
    python3 {{quote(environment_script)}} report

[working-directory: root]
doctor:
    python3 {{quote(environment_script)}} exec uv --version >/dev/null
    python3 {{quote(environment_script)}} exec ruff --version >/dev/null
    python3 {{quote(environment_script)}} exec mypy --version >/dev/null
    python3 {{quote(environment_script)}} exec pytest --version >/dev/null
    @echo "Python Project Adapter doctor: PASS"

[working-directory: root]
format-check:
    python3 {{quote(environment_script)}} exec uv run --locked ruff format --check src tests

[working-directory: root]
lint:
    python3 {{quote(environment_script)}} exec uv run --locked ruff check src tests
    python3 {{quote(environment_script)}} exec uv run --locked mypy src tests

[working-directory: root]
test:
    python3 {{quote(environment_script)}} exec uv run --locked pytest -v

[working-directory: root]
build:
    @echo "Python Project Adapter build: SKIPPED (no build artifact contract configured)"

[working-directory: root]
check: doctor format-check lint test build
    @echo "Python Project Adapter check: PASS"
