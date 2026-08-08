root := justfile_directory()

mod rust 'rust.just'
mod? repository 'repository.just'

[working-directory: root]
doctor:
    @command -v cargo >/dev/null
    @command -v rustc >/dev/null
    @command -v rustfmt >/dev/null
    @command -v clippy-driver >/dev/null || command -v cargo-clippy >/dev/null || true
    @echo "Rust Project Adapter doctor: PASS"

[working-directory: root]
format-check:
    cargo fmt --all -- --check

[working-directory: root]
lint:
    cargo clippy --all-targets --all-features -- -D warnings

[working-directory: root]
test:
    cargo test --all-targets --all-features

[working-directory: root]
build:
    cargo build --all-targets --all-features

[working-directory: root]
check: doctor format-check lint test build
    @echo "Rust Project Adapter check: PASS"
