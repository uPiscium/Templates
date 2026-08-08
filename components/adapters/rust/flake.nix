{
  description = "Agent-ready Rust Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = nixpkgs.legacyPackages.${system}; in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            cargo rustc rustfmt clippy rust-analyzer just git gh jq pre-commit gnused
          ];
          shellHook = ''
            if [ -d .git ]; then
              pre-commit install --install-hooks -t pre-commit -t pre-push >/dev/null 2>&1 || true
            fi
          '';
        };
      });
}
