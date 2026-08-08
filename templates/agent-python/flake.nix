{
  description = "Agent-ready Python + uv Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let pkgs = nixpkgs.legacyPackages.${system}; in {
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            uv just git gh jq ruff mypy python3Packages.pytest pre-commit gnused
          ];
          shellHook = ''
            export UV_PROJECT_ENVIRONMENT=$PWD/.venv
            export PIP_REQUIRE_VIRTUALENV=1
            if [ -d .git ]; then
              pre-commit install --install-hooks -t pre-commit -t pre-push >/dev/null 2>&1 || true
            fi
          '';
        };
      });
}
