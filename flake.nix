{
  description = "upiscium's env templates";

  outputs = { self }: {
    templates = {
      python = {
        path = ./python;
        description = "Python + uv + just based environment";
      };
      # 将来的に python-js などのテンプレートをここに追加します
    };
  };
}

