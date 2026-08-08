## Nix Project Adapter initialization

- `just project::doctor` requires nix, nixfmt, statix, deadnix, nil, and jq.
- `flake.nix` must exist; `/init` does not update `flake.lock`.
- Normal Tasks must not modify `flake.lock` unless dependency/environment scope explicitly permits it.
- Do not run deploy, switch, profile mutation, store deletion, or other host-changing Nix operations through the Project Adapter.
