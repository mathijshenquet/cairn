{ pkgs, config, ... }:

{
  dotenv.enable = true;

  languages.python = {
    enable = true;
    version = "3.12";
    uv = {
      enable = true;
      sync.enable = true;
    };
  };

  packages = [
    pkgs.pyright
  ];

  enterShell = ''
    export PATH="${config.devenv.root}/.devenv/state/venv/bin:$PATH"
  '';
}
