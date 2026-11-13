{
  config,
  lib,
  pkgs,
  ...
}: let
  cfg = config.services.nvr_bridge;

  settingsFormat = pkgs.formats.json {};

  gatewaySettings = cfg.gateway.settings // {API_KEY_FILE = cfg.apiKeyFile;};
  coreSettings = cfg.core.settings // {API_KEY_FILE = cfg.apiKeyFile;};

  gatewayConfigFile = settingsFormat.generate "nvr_bridge_gateway_config.json" gatewaySettings;
  coreConfigFile = settingsFormat.generate "nvr_bridge_core_config.json" coreSettings;

  logconfigDict = ''
    logconfig_dict = {
        "formatters": {
            "generic": {
                "format": "%(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "generic",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {"level": "INFO", "handlers": ["console"]},
        "loggers": {
            "gunicorn.error": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
                "qualname": "gunicorn.error",
            },
        },
    }
  '';

  # The gateway installs DeviceRequest (tolerates the NVR's request lines
  # without a leading slash); see nvr_gateway.device_http.
  gatewayGunicornConfig = pkgs.writeText "nvr_bridge_gateway_gunicorn.conf.py" ''
    from gunicorn.http.parser import RequestParser

    from nvr_gateway.device_http import DeviceRequest

    RequestParser.mesg_class = DeviceRequest

    worker_class = "gthread"
    workers = 1
    threads = 4
    ${logconfigDict}
  '';

  coreGunicornConfig = pkgs.writeText "nvr_bridge_core_gunicorn.conf.py" ''
    worker_class = "gthread"
    workers = 1
    threads = 4
    ${logconfigDict}
  '';

  hardening = {
    DevicePolicy = "closed";
    CapabilityBoundingSet = "";
    RestrictAddressFamilies = ["AF_UNIX" "AF_INET" "AF_INET6"];
    DeviceAllow = "";
    NoNewPrivileges = true;
    PrivateDevices = true;
    PrivateMounts = true;
    PrivateTmp = true;
    PrivateUsers = true;
    ProtectClock = true;
    ProtectControlGroups = true;
    ProtectHome = true;
    ProtectKernelLogs = true;
    ProtectKernelModules = true;
    ProtectKernelTunables = true;
    ProtectSystem = "strict";
    MemoryDenyWriteExecute = true;
    LockPersonality = true;
    RemoveIPC = true;
    RestrictNamespaces = true;
    RestrictRealtime = true;
    RestrictSUIDSGID = true;
    SystemCallArchitectures = "native";
    SystemCallFilter = [
      "@system-service"
      "~@privileged"
      "~@resources"
    ];
    ProtectProc = "invisible";
    ProtectHostname = true;
    ProcSubset = "pid";
    UMask = "0077";
  };
in {
  options.services.nvr_bridge = {
    enable = lib.mkEnableOption "NVR to Home Assistant push bridge";

    gatewayPackage = lib.mkPackageOption pkgs "nvr_bridge_gateway" {};

    corePackage = lib.mkPackageOption pkgs "nvr_bridge_core" {};

    user = lib.mkOption {
      description = "User account under which the nvr_bridge services run.";
      type = lib.types.str;
      default = "nvr_bridge";
    };

    group = lib.mkOption {
      description = "Group under which the nvr_bridge services run.";
      type = lib.types.str;
      default = "nvr_bridge";
    };

    apiKeyFile = lib.mkOption {
      description = ''
        File with the API key the gateway and core share.  At least 32
        characters, not in the Nix store.  Generate one with:

        ```
        python3 -c "import secrets; print(secrets.token_urlsafe(32))"
        ```
      '';
      type = lib.types.path;
      example = "/run/secrets/nvr_bridge_api_key";
    };

    openFirewall = lib.mkOption {
      description = "Open the firewall for the NVR push port (`gateway.settings.PORT`).";
      type = lib.types.bool;
      default = false;
    };

    imagesSocket = lib.mkOption {
      description = ''
        Unix socket the core listens on, behind a reverse proxy.
        Socket-activated, so the service starts on the first
        connection.
      '';
      type = lib.types.path;
      default = "/run/nvr_bridge/images.sock";
    };

    imagesSocketUser = lib.mkOption {
      description = ''
        Owner of the core socket, typically the reverse proxy user.
      '';
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "nginx";
    };

    gateway.settings = lib.mkOption {
      description = ''
        Configuration for the gateway.
      '';
      type = lib.types.submodule {
        freeformType = settingsFormat.type;

        options = {
          LISTEN = lib.mkOption {
            description = "Address the NVR-facing interface binds.";
            type = lib.types.str;
            default = "0.0.0.0";
            example = "192.168.0.10";
          };

          PORT = lib.mkOption {
            description = "Port for the NVR-facing interface. Point the NVR's HTTP listener at it.";
            type = lib.types.port;
            default = 8520;
          };

          CORE_URL = lib.mkOption {
            description = ''
              The core's webhook URL on the public vhost, including its
              `/api/webhook` path.  Must be https://; wrapping the NVR's
              plain HTTP in TLS is the gateway's job.
            '';
            type = lib.types.str;
            example = "https://nvr.example.com/api/webhook";
          };

          LOG_LEVEL = lib.mkOption {
            description = "Log level for the gateway journal.";
            type = lib.types.enum ["DEBUG" "INFO" "WARNING" "ERROR" "CRITICAL"];
            default = "INFO";
          };
        };
      };
      default = {};
    };

    core.settings = lib.mkOption {
      description = ''
        Configuration for the core.
      '';
      type = lib.types.submodule {
        freeformType = settingsFormat.type;

        options = {
          HA_WEBHOOK_URL = lib.mkOption {
            description = ''
              The webhook URL without the id on the end;
              HA_WEBHOOK_ID_FILE supplies the id.
            '';
            type = lib.types.str;
            example = "http://homeassistant:8123/api/webhook";
          };

          HA_WEBHOOK_ID_FILE = lib.mkOption {
            description = ''
              File with the Home Assistant webhook id, the same secret
              the automation's `webhook_id` uses.  The id is all that
              guards the webhook, so keep it out of the Nix store.
            '';
            type = lib.types.path;
            example = "/run/secrets/nvr_bridge_webhook_id";
          };

          SNAPS_DIR = lib.mkOption {
            description = "Directory where decoded snapshot JPEGs are stored.";
            type = lib.types.path;
            default = "/var/lib/nvr_bridge/snaps";
          };

          URL_RETENTION_DAYS = lib.mkOption {
            description = ''
              Days a `/snaps/<name>` URL works.  After that the image
              is only served to signed-in users (the events pages); 0
              skips the anonymous window entirely.
            '';
            type = lib.types.ints.between 0 36500;
            default = 1;
          };

          EVENT_RETENTION_DAYS = lib.mkOption {
            description = ''
              Events older than this are deleted, image files included;
              0 keeps everything forever.  The only setting in here
              that deletes anything.
            '';
            type = lib.types.ints.between 0 36500;
            default = 3650;
          };

          SECRET_KEY_FILE = lib.mkOption {
            description = ''
              File with the session secret for the events web UI.  At
              least 32 characters, not in the Nix store.  Generate one
              with:

              ```bash
              python3 -c "import secrets; print(secrets.token_hex())"
              ```
            '';
            type = lib.types.path;
            example = "/run/secrets/nvr_bridge_secret_key";
          };

          OIDC_CLIENT_ID = lib.mkOption {
            description = "OIDC client ID registered with the identity provider.";
            type = lib.types.str;
            example = "nvr_bridge";
          };

          OIDC_CLIENT_SECRET_FILE = lib.mkOption {
            description = ''
              File with the OIDC client secret, not in the Nix store.
            '';
            type = lib.types.path;
            example = "/run/secrets/nvr_bridge_oidc_client_secret";
          };

          OIDC_DISCOVERY_URL = lib.mkOption {
            description = "OIDC discovery document URL for the identity provider.";
            type = lib.types.str;
            example = "https://kanidm.example.com/oauth2/openid/nvr_bridge/.well-known/openid-configuration";
          };

          PUBLIC_URL = lib.mkOption {
            description = ''
              The core's public HTTPS base, i.e. the reverse proxy
              vhost.  OIDC redirects and the links in forwarded pushes
              are built from it, so phones have to reach it.
            '';
            type = lib.types.str;
            example = "https://nvr.example.com";
          };

          ROLES_PATH = lib.mkOption {
            description = ''
              Where the role list lives in the OIDC claims, as a JSON
              path.  Kanidm with a claim map `nvr_roles`:
                ["nvr_roles"]
              Keycloak:
                ["resource_access" "nvr_bridge" "roles"]
            '';
            type = lib.types.listOf lib.types.str;
            example = ["nvr_roles"];
          };

          USER_ROLE = lib.mkOption {
            description = "Role that grants access to the events web pages.";
            type = lib.types.str;
            default = "nvr_viewers";
          };

          SESSION_COOKIE_SAMESITE = lib.mkOption {
            description = ''
              SameSite for the session cookie.  "Lax" when the IdP is
              on another registrable domain the callback redirect
              has to carry the cookie.
            '';
            type = lib.types.enum ["Strict" "Lax"];
            default = "Lax";
          };

          DATABASE_URL = lib.mkOption {
            description = "SQLAlchemy URL for the events database.";
            type = lib.types.str;
            default = "sqlite:////var/lib/nvr_bridge/events.db";
          };

          SNAPSHOT_URLS = lib.mkOption {
            description = ''
              JPEG sources to grab on every alarm, e.g. go2rtc's
              `/api/frame.mjpeg?src=back_door`.  The first one that
              answers becomes the notification image, since the NVR's
              own AI snapshot only arrives seconds later.  Empty turns
              instant captures off.
            '';
            type = lib.types.listOf lib.types.str;
            default = [];
            example = ["http://go2rtc.home.arpa:1984/api/frame.mjpeg?src=front_door"];
          };

          GROUP_WINDOW_S = lib.mkOption {
            description = ''
              Seconds after an alarm that AI snapshots still join its
              event group what the notification's click target
              collects.
            '';
            type = lib.types.ints.positive;
            default = 60;
          };

          LOG_LEVEL = lib.mkOption {
            description = "Log level for the core journal.";
            type = lib.types.enum ["DEBUG" "INFO" "WARNING" "ERROR" "CRITICAL"];
            default = "INFO";
          };
        };
      };
      default = {};
    };
  };

  config = lib.mkIf cfg.enable {
    users.users.${cfg.user} = lib.mkIf (cfg.user == "nvr_bridge") {
      isSystemUser = true;
      group = cfg.group;
      description = "nvr_bridge service user";
    };

    users.groups.${cfg.group} = lib.mkIf (cfg.group == "nvr_bridge") {};

    networking.firewall = lib.mkIf cfg.openFirewall {
      allowedTCPPorts = [cfg.gateway.settings.PORT];
    };

    systemd.tmpfiles.settings."10-nvr_bridge"."${cfg.core.settings.SNAPS_DIR}".d = {
      group = cfg.group;
      mode = "0750";
      user = cfg.user;
    };

    # Same name as the service: the socket's default activation target is
    # the unit with the matching name, so `nvr_bridge_core.socket` activates
    # `nvr_bridge_core.service` and passes the listening fd to it (fd 3).
    systemd.sockets.nvr_bridge_core = {
      description = "nvr_bridge core socket";
      wantedBy = ["sockets.target"];
      listenStreams = [cfg.imagesSocket];
      socketConfig = {
        SocketMode = "0660";
        SocketUser = cfg.imagesSocketUser;
        RemoveOnStop = true;
        FlushPending = true;
      };
    };

    systemd.services.nvr_bridge_gateway = {
      description = "NVR push gateway (plain HTTP -> HTTPS + API key)";
      wantedBy = ["multi-user.target"];
      after = ["network-online.target"];
      wants = ["network-online.target"];

      environment = {
        NVR_GATEWAY_CONFIG_FILE = "${gatewayConfigFile}";
        # Use the NixOS system CA bundle so that private CA certificates
        # trusted via security.pki.certificateFiles are also trusted when
        # the gateway verifies the core's TLS certificate.
        SSL_CERT_FILE = config.security.pki.caBundle;
        # gunicorn's control server wants to create ~/.gunicorn for its
        # control socket, but the service user's home is the read-only
        # /var/empty; give it the private runtime directory as HOME.
        HOME = "/run/nvr_bridge_gateway";
      };
      serviceConfig =
        {
          ExecStart =
            "${lib.getExe' cfg.gatewayPackage "gunicorn"}"
            + " --config ${gatewayGunicornConfig}"
            + " --bind ${cfg.gateway.settings.LISTEN}:${toString cfg.gateway.settings.PORT}"
            + " nvr_gateway.wsgi:application";
          User = cfg.user;
          Group = cfg.group;
          Restart = "on-failure";
          RestartSec = 10;
          RuntimeDirectory = "nvr_bridge_gateway";
          BindReadOnlyPaths = [cfg.apiKeyFile];
        }
        // hardening;
    };

    systemd.services.nvr_bridge_core = {
      description = "NVR push bridge core (classify, store snaps, forward to HA)";
      # Purely socket-activated: gunicorn binds fd 3, which only exists
      # when systemd starts the service for an incoming connection (a
      # boot-time start would have no fd to bind).
      bindsTo = ["nvr_bridge_core.socket"];
      after = ["nvr_bridge_core.socket" "network-online.target"];
      wants = ["network-online.target"];

      environment = {
        NVR_CORE_CONFIG_FILE = "${coreConfigFile}";
        # The system CA bundle covers both the stdlib (authlib's token
        # endpoint via requests honors REQUESTS_CA_BUNDLE) and urllib
        # (SSL_CERT_FILE) when the IdP's certificate comes from
        # security.pki.certificateFiles.
        SSL_CERT_FILE = config.security.pki.caBundle;
        REQUESTS_CA_BUNDLE = config.security.pki.caBundle;
        # gunicorn's control server needs a writable HOME for
        # ~/.gunicorn (/var/empty is read-only).
        HOME = "/run/nvr_bridge_core";
      };

      serviceConfig =
        {
          # No --bind: gunicorn adopts the socket passed by systemd socket
          # activation (fd 3, LISTEN_FDS).  Passing an explicit `fd://3`
          # bind on top of LISTEN_FDS makes gunicorn 26 create two
          # listeners for the same fd and crash with EBADF.
          ExecStart =
            "${lib.getExe' cfg.corePackage "gunicorn"}"
            + " --config ${coreGunicornConfig}"
            + " nvr_core.wsgi:application";
          User = cfg.user;
          Group = cfg.group;
          Restart = "on-failure";
          RestartSec = 10;
          RuntimeDirectory = "nvr_bridge_core";
          StateDirectory = "nvr_bridge";
          StateDirectoryMode = "0700";
          BindPaths = [cfg.core.settings.SNAPS_DIR];
          BindReadOnlyPaths = [
            cfg.apiKeyFile
            cfg.core.settings.SECRET_KEY_FILE
            cfg.core.settings.OIDC_CLIENT_SECRET_FILE
            cfg.core.settings.HA_WEBHOOK_ID_FILE
          ];
        }
        // hardening;
    };
  };

  meta.maintainers = [lib.maintainers.newam];
}
