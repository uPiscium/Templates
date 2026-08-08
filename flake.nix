{
  description = "upiscium's env templates";

  outputs = { self }: {
    templates = {
      python = {
        path = ./templates/agent-python;
        description = "Compatibility alias for the Agent-ready Python + uv template";
      };
      rust = {
        path = ./templates/agent-rust;
        description = "Compatibility alias for the Agent-ready Rust template";
      };
      agent-base = {
        path = ./templates/agent-base;
        description = "Generated Agent-ready base repository scaffold";
      };
      agent-cpp-cmake = {
        path = ./templates/agent-cpp-cmake;
        description = "Generated Agent-ready C++/CMake repository scaffold";
      };
      agent-nix = {
        path = ./templates/agent-nix;
        description = "Generated Agent-ready Nix flake repository scaffold";
      };
      agent-python = {
        path = ./templates/agent-python;
        description = "Generated Agent-ready Python + uv repository scaffold";
      };
      agent-rust = {
        path = ./templates/agent-rust;
        description = "Generated Agent-ready Rust repository scaffold";
      };
    };
  };
}
