{
  description = "Python + uv Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            uv
            just
            ruff
            pre-commit
            gnused
          ];

          shellHook = ''
            # uvの仮想環境をプロジェクト直下の .venv に強制
            export UV_PROJECT_ENVIRONMENT=$PWD/.venv
            
            # エージェントが誤ってグローバル環境を触らないための防御壁
            export PIP_REQUIRE_VIRTUALENV=1

            if [ -d ".git" ]; then
              pre-commit install --install-hooks -t pre-commit -t pre-push > /dev/null 2>&1
            fi
          '';
        };
      }
    );
}

