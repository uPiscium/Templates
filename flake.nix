{
  description = "upiscium's env templates";

  outputs = { self }: {
    templates = {
      python = {
        path = ./python;
        description = "uv + just based environment";
      };
      rust = {
        path = ./rust;
        description = "cargo + just based environment";
      };
      agent-base = {
        path = ./templates/agent-base;
        description = "Generated Agent-ready base repository scaffold";
      };
    };
  };
}
