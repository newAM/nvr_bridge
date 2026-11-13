{
  self,
  pkgs,
}: let
  nvrDomain = "nvr.home.arpa";
  kanidmDomain = "kanidm.home.arpa";
  nginxPort = 10443;
  kanidmPort = 20443;
  kanidmUrl = "https://${kanidmDomain}:${toString kanidmPort}";
  nvrUrl = "https://${nvrDomain}:${toString nginxPort}";

  apiKeyFile = "/run/nvr_bridge_api_key";
  secretKeyFile = "/run/nvr_bridge_secret_key";
  # the secret Home Assistant webhook id (nginx below stands in for HA)
  webhookId = "secret-webhook-id-0123456789abcdef";
  webhookIdFile = "/run/nvr_bridge_webhook_id";
  oidcClientId = "nvr_bridge";
  oidcClientSecretFile = pkgs.writeText "nvr_bridge_oidc_client_secret" "test-oidc-client-secret";
  oidcDiscoveryUrl = "${kanidmUrl}/oauth2/openid/${oidcClientId}/.well-known/openid-configuration";

  viewerUsername = "viewer";
  strangerUsername = "stranger";

  testJpeg = pkgs.runCommand "test.jpg" {} ''
    printf '\377\330\377\340' > $out
    head -c 512 /dev/zero >> $out
  '';
in
  pkgs.testers.nixosTest {
    name = "nvr-bridge-basic";

    nodes.machine = {config, ...}: {
      imports = [self.nixosModules.default];
      nixpkgs.overlays = [self.overlays.default];

      networking.hosts."127.0.0.1" = [nvrDomain kanidmDomain];
      networking.firewall.allowedTCPPorts = [nginxPort kanidmPort];

      # kanidm identity provider
      services.kanidm = {
        package = pkgs.kanidmWithSecretProvisioning_1_11;
        server = {
          enable = true;
          settings = {
            version = "2";
            bindaddress = "0.0.0.0:${toString kanidmPort}";
            domain = kanidmDomain;
            origin = kanidmUrl;
            tls_chain = ./kanidm.home.arpa.cert.pem;
            tls_key = ./kanidm.home.arpa.key.pem;
          };
        };
        client = {
          enable = true;
          settings = {
            uri = kanidmUrl;
            verify_ca = true;
            verify_hostnames = true;
          };
        };
        provision = {
          enable = true;
          systems.oauth2.${oidcClientId} = {
            displayName = "nvr_bridge";
            public = false;
            enableLegacyCrypto = false;
            preferShortUsername = true;
            basicSecretFile = oidcClientSecretFile;
            originUrl = "${nvrUrl}/auth/callback";
            originLanding = nvrUrl;
            # Everyone can authenticate (the app enforces its own role
            # gate); only members of nvr_viewers get the role claim.
            scopeMaps."idm_all_persons" = ["openid" "email" "profile"];
            removeOrphanedClaimMaps = true;
            claimMaps."nvr_roles".valuesByGroup."nvr_viewers" = ["nvr_viewers"];
          };
          persons.${viewerUsername} = {
            displayName = "Viewer";
            mailAddresses = ["viewer@example.com"];
          };
          persons.${strangerUsername} = {
            displayName = "Stranger";
            mailAddresses = ["stranger@example.com"];
          };
          groups."nvr_viewers".members = [viewerUsername];
        };
      };

      # the bridge
      services.nvr_bridge = {
        enable = true;
        imagesSocketUser = config.services.nginx.user;
        inherit apiKeyFile;
        gateway.settings.CORE_URL = "${nvrUrl}/api/webhook";
        core.settings = {
          SECRET_KEY_FILE = secretKeyFile;
          OIDC_CLIENT_SECRET_FILE = toString oidcClientSecretFile;
          OIDC_CLIENT_ID = oidcClientId;
          OIDC_DISCOVERY_URL = oidcDiscoveryUrl;
          PUBLIC_URL = nvrUrl;
          # .arpa is on the public suffix list, so the browser treats
          # nvr.home.arpa and kanidm.home.arpa as cross-site: use Lax so
          # the IdP callback redirect carries the session cookie.
          SESSION_COOKIE_SAMESITE = "Lax";
          ROLES_PATH = ["nvr_roles"];
          # instant notification images: the nginx-served test JPEG stands
          # in for go2rtc
          SNAPSHOT_URLS = ["${nvrUrl}/test.jpg"];
          # no HA in this test: nginx's /api/webhook/ stub below stands in
          # for it, so forwarding still runs end to end
          HA_WEBHOOK_URL = "${nvrUrl}/api/webhook";
          HA_WEBHOOK_ID_FILE = webhookIdFile;
          # DEBUG so the forwarder's success line reaches the journal
          LOG_LEVEL = "DEBUG";
        };
      };

      services.nginx = {
        enable = true;
        defaultSSLListenPort = nginxPort;
        virtualHosts.${nvrDomain} = {
          onlySSL = true;
          sslCertificate = ./nvr.home.arpa.cert.pem;
          sslCertificateKey = ./nvr.home.arpa.key.pem;
          locations."/".proxyPass = "http://unix:${config.services.nvr_bridge.imagesSocket}";
          # stands in for go2rtc: an instant JPEG source the core fetches
          # on every alarm push
          locations."=/test.jpg".alias = "${testJpeg}";
          # stands in for Home Assistant: the forwarded webhook URL (base +
          # secret id) lands here.  The trailing slash keeps the relay
          # target `/api/webhook` itself on the core.
          locations."/api/webhook/".return = "200";
        };
      };

      security.pki.certificateFiles = [
        ./nvr.home.arpa.cert.pem
        ./kanidm.home.arpa.cert.pem
      ];

      # Write the test secrets before the services start
      systemd.services.nvr-bridge-test-secrets = {
        description = "Write test secrets for nvr_bridge";
        wantedBy = ["nvr_bridge_core.service" "nvr_bridge_gateway.service"];
        before = ["nvr_bridge_core.service" "nvr_bridge_gateway.service"];
        script = ''
          install -m600 -o nvr_bridge -g nvr_bridge /dev/null ${apiKeyFile}
          head -c 64 /dev/urandom | base64 | tr -d '\n' > ${apiKeyFile}
          chown nvr_bridge:nvr_bridge ${apiKeyFile}
          chmod 600 ${apiKeyFile}

          echo test-secret-key-do-not-use-in-production-12345 > ${secretKeyFile}
          chown nvr_bridge:nvr_bridge ${secretKeyFile}
          chmod 600 ${secretKeyFile}

          echo ${webhookId} > ${webhookIdFile}
          chown nvr_bridge:nvr_bridge ${webhookIdFile}
          chmod 600 ${webhookIdFile}
        '';
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
      };

      environment.systemPackages = with pkgs; [
        htmlq
      ];
    };

    testScript = ''
      import base64
      import json
      import re

      def get_csrf(path: str) -> str:
        machine.succeed(
          "curl -sSf -b nvr_cookies.txt -o csrf.html ${nvrUrl}" + path,
          "htmlq 'input[name=csrf_token]' --attribute value --filename csrf.html --output csrf.txt",
        )
        return machine.succeed("head -1 csrf.txt").rstrip()

      def kanidm_login(username: str, password: str) -> str:
        """Drive the kanidm login dance; returns the callback URL."""
        # follow /login -> kanidm authorize redirect; capture the SSO login
        # URL.  The session cookie (which may carry a stashed post-login
        # redirect target) is sent and refreshed here.
        sso_login_url: str = machine.succeed(
          "curl -sSf -b nvr_cookies.txt -c nvr_cookies.txt -w %{redirect_url} -o /dev/null ${nvrUrl}/login"
        ).rstrip()
        print(f"{sso_login_url=}")

        # fetch the username form page
        machine.succeed(
          f"curl -sSf -b sso_cookies.txt -c sso_cookies.txt -o login_page.html '{sso_login_url}'",
          "htmlq '#login' --attribute action --filename login_page.html --output username_form_action.txt",
        )
        username_post_url: str = "${kanidmUrl}" + machine.succeed("cat username_form_action.txt").rstrip()

        # post username, get the password form page
        machine.succeed(
          f"curl -sSf -b sso_cookies.txt -c sso_cookies.txt -o password_page.html"
          f" -d 'username={username}' -d 'password=' -d 'totp='"
          f" '{username_post_url}'",
          "htmlq '#login' --attribute action --filename password_page.html --output password_form_action.txt",
        )
        password_post_url: str = "${kanidmUrl}" + machine.succeed("cat password_form_action.txt").rstrip()

        # post password, get resume session redirect
        resume_url: str = machine.succeed(
          f"curl -sSf -b sso_cookies.txt -c sso_cookies.txt -w %{{redirect_url}} -o /dev/null"
          f" -d 'password={password}' '{password_post_url}'"
        ).rstrip()
        assert resume_url.startswith("https://"), f"invalid resume URL: {resume_url!r}"

        # follow resume url, get consent page
        machine.succeed(
          f"curl -sSf -b sso_cookies.txt -c sso_cookies.txt -o consent_page.html '{resume_url}'",
          "htmlq '#login' --attribute action --filename consent_page.html --output consent_form_action.txt",
          "htmlq '#consent_token' --attribute value --filename consent_page.html --output consent_token.txt",
        )
        consent_post_url: str = "${kanidmUrl}" + machine.succeed("cat consent_form_action.txt").rstrip()
        consent_token: str = machine.succeed("cat consent_token.txt").rstrip()

        # post consent, get callback redirect back to the core
        callback_url: str = machine.succeed(
          f"curl -sSf -b sso_cookies.txt -c sso_cookies.txt -w %{{redirect_url}} -o /dev/null"
          f" -d 'consent_token={consent_token}' '{consent_post_url}'"
        ).rstrip()
        assert "/auth/callback" in callback_url, f"unexpected callback: {callback_url!r}"
        return callback_url

      machine.wait_for_unit("kanidm.service")
      machine.wait_for_open_port(${toString kanidmPort})
      machine.wait_until_succeeds("curl -sSf ${kanidmUrl}")

      # recover passwords for the test users
      viewer_pw = machine.succeed(
        "kanidmd recover-account ${viewerUsername} 2>&1 | grep -oP '[A-Za-z0-9]{48}'"
      ).strip()
      stranger_pw = machine.succeed(
        "kanidmd recover-account ${strangerUsername} 2>&1 | grep -oP '[A-Za-z0-9]{48}'"
      ).strip()

      machine.wait_for_unit("nvr_bridge_core.socket")
      machine.wait_for_unit("nvr_bridge_gateway.service")
      machine.wait_for_unit("nginx.service")
      # the core service is socket-activated: the first request below
      # starts it (queued connections wait for gunicorn to boot)

      with subtest("Unauthenticated events page redirects to the IdP"):
        status = machine.succeed(
          "curl -s -o /dev/null -w '%{http_code}' ${nvrUrl}/events"
        ).strip()
        assert status == "302", f"expected 302, got: {status}"
        login_redirect: str = machine.succeed(
          "curl -s -o /dev/null -w '%{redirect_url}' ${nvrUrl}/events"
        ).rstrip()
        assert "login" in login_redirect, f"expected login redirect, got: {login_redirect}"

      with subtest("Seed an alarm (instant capture) and its AI snapshot"):
        # Both go through the gateway's relay target (API key auth), not
        # the NVR-facing interface and not Home Assistant.
        alarm_push = {
            "data": {
                "alarm_list": [
                    {"channel_alarm": [{"channel": "CH2", "int_alarm": {"alarm_val": True}}]}
                ],
                "device_name": "testing",
            }
        }
        jpeg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"test image" * 8).decode()
        snapshot_push = {
            "data": {
                "ai_snap_picture": {
                    "SnapedObjInfo": [
                        {
                            "Chn": 1,
                            "StrChn": "CH2",
                            "SnapId": 7001,
                            "ObjectImage": jpeg,
                            "Background": jpeg,
                        }
                    ]
                },
                "device_name": "testing",
            }
        }
        machine.succeed("cat > /tmp/alarm.json <<'EOF'\n" + json.dumps(alarm_push) + "\nEOF")
        machine.succeed("cat > /tmp/push.json <<'EOF'\n" + json.dumps(snapshot_push) + "\nEOF")

        api_key = machine.succeed("cat ${apiKeyFile}").strip()
        for payload in ("/tmp/alarm.json", "/tmp/push.json"):
          status = machine.succeed(
            f"curl -s -o /dev/null -w '%{{http_code}}'"
            f" -H 'Authorization: Bearer {api_key}'"
            " -H 'Content-Type: application/json'"
            f" --data @{payload} ${nvrUrl}/api/webhook"
          ).strip()
          assert status == "200", f"expected 200 seeding {payload}, got: {status}"
        # unauthenticated pushes stay rejected
        status = machine.succeed(
          "curl -s -o /dev/null -w '%{http_code}'"
          " -H 'Content-Type: application/json'"
          " --data @/tmp/push.json ${nvrUrl}/api/webhook"
        ).strip()
        assert status == "401", f"expected 401 without API key, got: {status}"

        # the alarm captured the instant JPEG: pull the group token from
        # the journal ("captured live snapshot ... snap_<token>_live0.jpg")
        group_token: str = machine.succeed(
          "journalctl -t nvr_bridge_core --no-pager"
          " | grep -oP 'snap_\\K[0-9a-f]{32}(?=_live0)' | head -1"
        ).strip()
        assert re.fullmatch(r"[0-9a-f]{32}", group_token), "no live capture in journal"

        # forwarding ran end to end: the composed webhook URL (base from
        # settings + the secret id file) was POSTed and accepted
        machine.wait_until_succeeds(
          "journalctl -t nvr_bridge_core --no-pager | grep -q 'forwarded'"
        )
        # the secret webhook id itself never reaches the core's journal
        machine.fail(
          "journalctl -t nvr_bridge_core --no-pager | grep -F ${webhookId}"
        )

      with subtest("Login as an unprivileged user is denied"):
        callback_url = kanidm_login("${strangerUsername}", stranger_pw)
        # callback -> redirect to /events -> 403 (no role): expected
        final_status = machine.succeed(
          f"curl -L -s -b nvr_cookies.txt -c nvr_cookies.txt -o /dev/null"
          f" -w '%{{http_code}}' '{callback_url}'"
        ).strip()
        assert final_status == "403", f"expected 403 without role, got: {final_status}"
        status = machine.succeed(
          "curl -s -b nvr_cookies.txt -o /dev/null -w '%{http_code}' ${nvrUrl}/events"
        ).strip()
        assert status == "403", f"expected 403 without role, got: {status}"
        # log the stranger back out of the IdP session
        machine.succeed("rm -f sso_cookies.txt")

      with subtest("Notification click target: SSO returns to the group page"):
        # fresh session; visit the notification's click target first, so
        # the post-login redirect must bring us back to it
        machine.succeed("rm -f sso_cookies.txt nvr_cookies.txt")
        status = machine.succeed(
          f"curl -s -c nvr_cookies.txt -o /dev/null -w '%{{http_code}}'"
          f" ${nvrUrl}/events/group/{group_token}"
        ).strip()
        assert status == "302", f"expected 302 to login, got: {status}"

        callback_url = kanidm_login("${viewerUsername}", viewer_pw)
        final_url: str = machine.succeed(
          f"curl -L -sSf -b nvr_cookies.txt -c nvr_cookies.txt -o group_page.html"
          f" -w '%{{url_effective}}' '{callback_url}'"
        ).rstrip()
        assert f"/events/group/{group_token}" in final_url, (
          f"expected to land back on the group page, got: {final_url}"
        )
        group_html: str = machine.succeed("cat group_page.html")
        # the group page shows the instant capture AND the AI snapshots
        assert "_live0.jpg" in group_html, "go2rtc capture missing from group page"
        assert "_object.jpg" in group_html, "AI object snapshot missing from group page"
        assert "_background.jpg" in group_html, "AI background snapshot missing"
        # alarm and snapshot share the group (correlated within the window)
        assert group_html.count("kind-alarm") == 1
        assert group_html.count("kind-snapshot") == 1

        # every image on the group page is served: the alarm's still, the
        # snapshot's own still, and the AI object/background pair
        page_images = sorted(
          set(re.findall(r"/snaps/(snap_[0-9a-f]{32}_\w+\.jpg)", group_html))
        )
        assert len(page_images) == 4, f"expected 4 images, got: {page_images}"
        for name in page_images:
          status = machine.succeed(
            f"curl -s -o /dev/null -w '%{{http_code}}' -b nvr_cookies.txt ${nvrUrl}/snaps/{name}"
          ).strip()
          assert status == "200", f"image {name}: expected 200, got {status}"

      with subtest("Events list shows the cluster"):
        events_html: str = machine.succeed(
          "curl -sSf -b nvr_cookies.txt ${nvrUrl}/events"
        )
        assert "Webhook events" in events_html, "events page did not render"
        assert "snapshot" in events_html and "alarm" in events_html
        # the list links to the group
        assert f"/events/group/{group_token}" in events_html

        # event detail page shows the redacted payload (event 2 = snapshot)
        detail_html: str = machine.succeed(
          "curl -sSf -b nvr_cookies.txt ${nvrUrl}/events/2"
        )
        assert "SnapId" in detail_html, "payload missing from detail page"

      with subtest("Logout"):
        csrf_token: str = get_csrf("/events")
        final_url: str = machine.succeed(
          f"curl -sSf -L -b nvr_cookies.txt -c nvr_cookies.txt -o /dev/null -w '%{{url_effective}}'"
          f" -d 'csrf_token={csrf_token}' ${nvrUrl}/logout"
        ).rstrip()
        assert "logged-out" in final_url or "logout" in final_url, (
          f"expected logout landing, got: {final_url}"
        )

        # session cleared: /events redirects to login again
        redirect_after_logout: str = machine.succeed(
          "curl -s -b nvr_cookies.txt -o /dev/null -w '%{redirect_url}' ${nvrUrl}/events"
        ).rstrip()
        assert "login" in redirect_after_logout, (
          f"expected login redirect after logout, got: {redirect_after_logout}"
        )
    '';
  }
