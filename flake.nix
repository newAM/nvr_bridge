{
  description = "NVR (Uniview-style HTTP listening) to Home Assistant push bridge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    treefmt.url = "github:numtide/treefmt-nix";
    treefmt.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = {
    self,
    nixpkgs,
    treefmt,
  }: let
    overlay = final: prev: {
      nvr_bridge_common = prev.python3Packages.callPackage ./lib/package.nix {};

      nvr_bridge_gateway = prev.callPackage ./gateway/package.nix {
        src = nixpkgs.lib.fileset.toSource {
          root = ./gateway;
          fileset = nixpkgs.lib.fileset.unions [
            ./gateway/nvr_gateway
            ./gateway/pyproject.toml
            ./gateway/tests
          ];
        };
      };

      nvr_bridge_core = prev.callPackage ./core/package.nix {
        src = nixpkgs.lib.fileset.toSource {
          root = ./core;
          fileset = nixpkgs.lib.fileset.unions [
            ./core/nvr_core
            ./core/pyproject.toml
            ./core/tests
          ];
        };
      };
    };

    forEachSystem = nixpkgs.lib.genAttrs [
      "x86_64-linux"
    ];
    importPkgs = system:
      import nixpkgs {
        inherit system;
        overlays = [overlay];
      };

    treefmtSettings = {
      projectRootFile = "flake.nix";
      settings.excludes = [
        "tmp/*"
      ];
      programs = {
        alejandra.enable = true;
        prettier.enable = true;
        ruff-format.enable = true;
        taplo.enable = true;
      };
    };
  in {
    overlays = {
      default = overlay;
      nvr_bridge = overlay;
    };

    nixosModules = {
      default = import ./nixos/module.nix;
      nvr_bridge = import ./nixos/module.nix;
    };

    apps = forEachSystem (
      system: let
        pkgs = importPkgs system;
      in {
        default = {
          type = "app";
          program = nixpkgs.lib.getExe pkgs.nvr_bridge_gateway;
          inherit (pkgs.nvr_bridge_gateway) meta;
        };
      }
    );

    packages = forEachSystem (
      system: let
        pkgs = importPkgs system;
      in {
        inherit (pkgs) nvr_bridge_common nvr_bridge_gateway nvr_bridge_core;
        default = pkgs.nvr_bridge_core;
      }
    );

    formatter = forEachSystem (
      system: (treefmt.lib.evalModule (importPkgs system) treefmtSettings).config.build.wrapper
    );

    devShells = forEachSystem (
      system: let
        pkgs = importPkgs system;
        pythonEnv = pkgs.python3.withPackages (ps:
          with ps; [
            authlib
            flask
            flask-session
            flask-sqlalchemy
            flask-wtf
            gunicorn
            pytest
            pytest-cov
            requests
            ruff
            systemd-python
            ty
          ]);
      in {
        default = pkgs.mkShell {
          packages = [pythonEnv];
          shellHook = ''
            export PYTHONPATH="$PWD/lib:$PWD/gateway:$PWD/core:$PYTHONPATH"
          '';
        };
      }
    );

    checks = forEachSystem (
      system: let
        pkgs = importPkgs system;
      in
        {
          inherit (pkgs) nvr_bridge_common nvr_bridge_gateway nvr_bridge_core;

          formatting =
            ((treefmt.lib.evalModule pkgs (nixpkgs.lib.recursiveUpdate treefmtSettings
                {
                  programs.ruff-check.enable = true;
                }))
          .config
          .build
          .check)
            self;
        }
        // nixpkgs.lib.optionalAttrs (system == "x86_64-linux") {
          nixos-basic = pkgs.callPackage ./nixos/tests/basic.nix {inherit self;};
        }
    );

    # Evaluation-only sanity check for the NixOS module (no VM test).
    nixosConfigurations.test = nixpkgs.lib.nixosSystem {
      system = "x86_64-linux";
      modules = [
        self.nixosModules.default
        {
          nixpkgs.overlays = [self.overlays.default];
          services.nvr_bridge = {
            enable = true;
            openFirewall = true;
            imagesSocketUser = "nginx";
            apiKeyFile = "/run/secrets/nvr_bridge_api_key";
            gateway.settings = {
              CORE_URL = "https://nvr.example.com/api/webhook";
            };
            core.settings = {
              HA_WEBHOOK_URL = "http://homeassistant:8123/api/webhook";
              HA_WEBHOOK_ID_FILE = "/run/secrets/nvr_bridge_webhook_id";
              SECRET_KEY_FILE = "/run/secrets/nvr_bridge_secret_key";
              OIDC_CLIENT_ID = "nvr_bridge";
              OIDC_CLIENT_SECRET_FILE = "/run/secrets/nvr_bridge_oidc_client_secret";
              OIDC_DISCOVERY_URL = "https://idp.example.com/oauth2/openid/nvr_bridge/.well-known/openid-configuration";
              PUBLIC_URL = "https://nvr.example.com";
              ROLES_PATH = ["nvr_roles"];
            };
          };
          fileSystems."/".device = "/dev/disk/by-label/nixos";
          fileSystems."/".fsType = "ext4";
          boot.loader.grub.devices = ["/dev/vda"];
          system.stateVersion = "26.05";
        }
      ];
    };
  };
}
