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
    };
  };
}

