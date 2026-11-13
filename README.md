# nvr_bridge

A bridge between my `NVR-3104-4P-AI-V2` NVR and Home Assistant to forward motion
notifications to my phone with images.

This isn't made for other people to consume, it's for reference if anyone has a
similar NVR.

This is largely coded by GLM-5.3 with little human oversight.

## Design

The NVR detects motion events, and has a feature to send HTTP requests to an
arbitrary URL upon these events.

There are two applications to handle the NVR events:

- **`nvr_bridge_gateway`** - the NVR-facing edge. The NVR can neither
  authenticate nor encrypt (properly), so the gateway accepts the device's
  plain-HTTP POSTs on any path, answers `200` immediately and relays each push
  byte-for-byte to the core over HTTPS with a shared API key.
- **`nvr_bridge_core`** - the real forwarder, behind nginx on a systemd
  socket-activated unix socket. Per push from the NVR: classify it, decode and
  store the AI snapshot JPEGs, record the event, answer fast, forward to the HA
  webhook asynchronously. There is a WebUI behind OIDC SSO which lists the most
  recent events.

## NixOS setup

```nix
{
  services.nvr_bridge = {
    enable = true;
    openFirewall = true;               # opens gateway.settings.PORT only
    imagesSocketUser = "nginx";        # let the reverse proxy connect
    apiKeyFile = "/run/secrets/nvr_bridge_api_key";
    gateway.settings = {
      PORT = 8520;                     # point the NVR's HTTP listener here
      CORE_URL = "https://nvr.example.com/api/webhook";
    };
    core.settings = {
      HA_WEBHOOK_URL = "http://homeassistant:8123/api/webhook";  # + /<secret id>
      HA_WEBHOOK_ID_FILE = "/run/secrets/nvr_bridge_webhook_id";
      SNAPSHOT_URLS = [ "http://go2rtc.home.arpa:1984/api/frame.mjpeg?src=back_door" ];
      # SSO for the events web UI
      SECRET_KEY_FILE = "/run/secrets/nvr_bridge_secret_key";
      OIDC_CLIENT_ID = "nvr_bridge";
      OIDC_CLIENT_SECRET_FILE = "/run/secrets/nvr_bridge_oidc_client_secret";
      OIDC_DISCOVERY_URL = "https://kanidm.example.com/oauth2/openid/nvr_bridge/.well-known/openid-configuration";
      PUBLIC_URL = "https://nvr.example.com";  # auth callbacks + image links
      ROLES_PATH = ["nvr_roles"];
    };
  };
}
```

The core socket is proxied by nginx, one vhost serves both audiences:

```nix
services.nginx.virtualHosts."nvr.example.com" = {
  enableACME = true;
  forceSSL = true;
  locations."/".proxyPass = "http://unix:/run/nvr_bridge/images.sock";
};
```

## Home Assistant setup

Create a webhook-triggered automation.

The webhook ID is the only credential, make it long, random, and secret.
The bridge reads the ID from `core.settings.HA_WEBHOOK_ID_FILE`, which must
contain the same value as the automation's `!secret nvr_bridge_webhook_id`.

```yaml
automation:
  - alias: NVR motion
    id: nvr_motion
    mode: queued
    max: 10
    trigger:
      - platform: webhook
        webhook_id: !secret nvr_bridge_webhook_id
        allowed_methods: [POST]
    condition:
      - condition: template
        value_template: "{{ 'nvr_bridge' in trigger.json }}"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.json.nvr_bridge.title | default('Motion') }}"
          message: "{{ trigger.json.nvr_bridge.message | default('') }}"
          data:
            image: "{{ trigger.json.nvr_bridge.image_url | default('') }}"
            tag: "nvr-{{ trigger.json.nvr_bridge.group }}" # replaces in place
            ttl: 0
            channel: alarm
            importance: high
            priority: high
            clickAction: "{{ trigger.json.nvr_bridge.url }}" # the group page
            url: "{{ trigger.json.nvr_bridge.url }}"
            attachment: # iOS
              url: "{{ trigger.json.nvr_bridge.background_url | default(trigger.json.nvr_bridge.image_url | default('')) }}"
              content-type: jpg
            push:
              interruption-level: time-sensitive
              apns-collapse-id: "nvr-{{ trigger.json.nvr_bridge.group }}"
```
