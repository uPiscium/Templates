## Rust Project Adapter initialization

- `just project::doctor` requires `cargo`, `rustc`, `rustfmt`, and `clippy`.
- `cargo fmt --check`, clippy, tests, and build are exposed only through stable `project::*` recipes.
- `/init` is read-only and does not fetch, update, or add dependencies.
