"""Builds the stack's compose.json and .env from the wizard's answers."""
from __future__ import annotations

from installer.settings import Settings
from installer.vpn import PROVIDERS, gluetun_env

LABEL = "io.arr-installer"
LABELS = {LABEL: "1"}
SERVICES = ["jellyfin", "seerr", "radarr", "sonarr", "qbittorrent", "prowlarr", "bazarr"]
PORTS = {"jellyfin": 8096, "seerr": 5055, "radarr": 7878, "sonarr": 8989, "qbittorrent": 8080,
         "prowlarr": 9696, "bazarr": 6767}
IMAGES = {
    "jellyfin": "lscr.io/linuxserver/jellyfin:latest",
    "seerr": "seerr/seerr:latest",
    "radarr": "lscr.io/linuxserver/radarr:latest",
    "sonarr": "lscr.io/linuxserver/sonarr:latest",
    "qbittorrent": "lscr.io/linuxserver/qbittorrent:latest",
    "prowlarr": "lscr.io/linuxserver/prowlarr:latest",
    "bazarr": "lscr.io/linuxserver/bazarr:latest",
    "gluetun": "qmcgaw/gluetun:latest",
}
CONFIG_DIR = {"seerr": "/app/config"}  # linuxserver images all use /config


def _port(svc: str) -> list[str]:
    return [f"{PORTS[svc]}:{PORTS[svc]}"]


def render_compose(s: Settings, data_source: str, has_dri: bool) -> dict:
    ids = {"PUID": str(s.puid), "PGID": str(s.pgid), "TZ": s.timezone}
    data = {"type": "bind", "source": data_source, "target": "/data"}
    services = {}
    for svc in SERVICES:
        services[svc] = {
            "image": IMAGES[svc],
            "environment": dict(ids),
            "volumes": [f"{svc}-config:{CONFIG_DIR.get(svc, '/config')}"],
            "ports": _port(svc),
            "restart": "unless-stopped",
            "labels": dict(LABELS),
        }
    for svc in ("jellyfin", "radarr", "sonarr", "qbittorrent", "bazarr"):
        services[svc]["volumes"].append(dict(data))
    services["seerr"].update(init=True, environment={"TZ": s.timezone})
    services["qbittorrent"]["environment"]["WEBUI_PORT"] = "8080"
    # Public resolvers: some ISP resolvers block tracker domains.
    services["prowlarr"]["dns"] = ["1.1.1.1", "9.9.9.9"]
    if has_dri:
        services["jellyfin"]["devices"] = ["/dev/dri:/dev/dri"]
    if s.vpn_provider:
        secrets = set(PROVIDERS[s.vpn_provider].fields)
        env = {k: (f"${{{k}}}" if k in secrets else v)
               for k, v in gluetun_env(s.vpn_provider, s.vpn_fields, s.timezone).items()}
        # gluetun reads {{PORTS}} itself; compose must not see a lone "$" anywhere.
        services["gluetun"] = {
            "image": IMAGES["gluetun"], "cap_add": ["NET_ADMIN"], "devices": ["/dev/net/tun:/dev/net/tun"],
            "environment": env, "ports": services["qbittorrent"].pop("ports"),
            "restart": "unless-stopped", "labels": dict(LABELS),
        }
        services["qbittorrent"]["network_mode"] = "service:gluetun"
        services["qbittorrent"]["depends_on"] = ["gluetun"]
    return {
        "services": services,
        "volumes": {f"{svc}-config": {"labels": dict(LABELS)} for svc in SERVICES},
        "networks": {"default": {"labels": dict(LABELS)}},
    }


def _quote(value: str) -> str:
    # Compose .env single quotes are literal except for \' (verified with `docker compose config`).
    # ponytail: a value ending in a backslash would break; VPN keys never do.
    return "'" + value.replace("'", "\\'") + "'"


def render_env(s: Settings) -> str:
    if not s.vpn_provider:
        return ""
    return "".join(f"{k}={_quote(s.vpn_fields[k])}\n" for k in PROVIDERS[s.vpn_provider].fields)
