{
  description = "Agent-ready Nix flake repository";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in {
        packages.default = pkgs.writeText "agent-nix-smoke" "ok\n";
        checks.default = self.packages.${system}.default;
        formatter = pkgs.nixfmt-rfc-style;
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            nix
            just
            git
            gh
            jq
            yq-go
            nil
            nixfmt-rfc-style
            statix
            deadnix
          ];
        };
      });
}
