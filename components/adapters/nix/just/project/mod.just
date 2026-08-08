root := justfile_directory()

mod nix 'nix.just'
mod? repository 'repository.just'

[working-directory: root]
doctor:
    @test -f flake.nix
    @command -v nix >/dev/null
    @command -v nixfmt >/dev/null
    @command -v statix >/dev/null
    @command -v deadnix >/dev/null
    @command -v nil >/dev/null
    @command -v jq >/dev/null
    @echo "Nix Project Adapter doctor: PASS"

[working-directory: root]
eval:
    nix flake metadata --json >/dev/null
    nix flake check --no-build

[working-directory: root]
format-check:
    nixfmt --check $(git ls-files '*.nix')

[working-directory: root]
lint:
    statix check .
    deadnix --fail .

[working-directory: root]
test:
    nix flake check --no-build

[working-directory: root]
build:
    nix build --no-link

[working-directory: root]
check: doctor eval format-check lint test
    @echo "Nix Project Adapter check: PASS"
