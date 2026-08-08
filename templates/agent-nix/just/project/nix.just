root := justfile_directory()

[working-directory: root]
metadata:
    nix flake metadata --json | jq .

[working-directory: root]
format:
    nixfmt $(git ls-files '*.nix')

[working-directory: root]
show:
    nix flake show
