# Templates

Nix flake templates for reproducible, Agent-ready development environments.

## Published templates

Python and Rust keep their original public flake names, but those names now point
to generated Agent Core + Project Adapter templates:

```sh
nix flake init -t github:upiscium/Templates#python
nix flake init -t github:upiscium/Templates#rust
```

The explicit Agent-ready names are equivalent:

```sh
nix flake init -t github:upiscium/Templates#agent-python
nix flake init -t github:upiscium/Templates#agent-rust
nix flake init -t github:upiscium/Templates#agent-base
```

Compatibility mapping:

```text
python       -> templates/agent-python
agent-python -> templates/agent-python
rust         -> templates/agent-rust
agent-rust   -> templates/agent-rust
```

The historical top-level `python/` and `rust/` directories are retained temporarily
as migration reference material, but are no longer the targets of the published
`#python` and `#rust` flake templates.

## Generated Agent-ready templates

Generated templates are artifacts. Do not edit files under `templates/<name>/`
directly. Their sources live under:

```text
components/agent-core/
components/adapters/<adapter>/
```

The generator fails on component path collisions instead of silently selecting an
overwrite order. It preserves dotfiles, symlinks, and executable bits.

Project Adapters implement the same stable public API:

```text
just project::doctor
just project::format-check
just project::lint
just project::test
just project::build
just project::check
```

Python preserves the uv/Ruff/Mypy/Pytest workflow and project-local `.venv`.
Its `project::build` is an explicit `SKIPPED` contract until a build artifact is
configured. Rust exposes rustfmt, Clippy, cargo test, and cargo build through the
same stable interface.

## Template generation API

The Templates repository exposes generation through the `template` Just module:

```sh
just template::render agent-base
just template::render agent-python
just template::render agent-rust
just template::render-all
just template::check
```

- `render` rebuilds one registered template from Agent Core plus its Adapter.
- `render-all` rebuilds all registered generated templates deterministically.
- `check` renders into temporary directories and fails when committed generated
  output has drifted from component sources. It also rejects unregistered generated
  template directories.

Template registrations live in `templates/manifest.json`.

## Existing repositories

Existing repositories can adopt Agent Core without losing repository-owned files.
When no dedicated Adapter is available or detection is ambiguous, the mandatory
`base` Adapter is used instead of creating an Adapter-less repository.

```sh
just template::adopt-plan /path/to/repository
just template::adopt-apply /path/to/repository base
just template::adapter-migrate-plan /path/to/repository python
```

Adoption does not commit, push, merge, stash, or reset the target repository.
