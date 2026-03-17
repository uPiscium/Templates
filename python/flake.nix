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
            # Python本体はuvに管理させるため、ここにはあえて記述しない（または最小限にする）という戦略も取れます。
            # 今回はuvの自己完結性に委ねます。
          ];

          shellHook = ''
            echo "========================================"
            echo " Harness Environment Activated"
            echo " - uv: $(uv --version)"
            echo " - just: $(just --version)"
            echo "========================================"
            
            # uvの仮想環境をプロジェクト直下の .venv に強制
            export UV_PROJECT_ENVIRONMENT=$PWD/.venv
            
            # エージェントが誤ってグローバル環境を触らないための防御壁
            export PIP_REQUIRE_VIRTUALENV=1
          '';
        };
      }
    );
}

