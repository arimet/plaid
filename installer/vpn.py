"""VPN providers offered by the wizard, as gluetun understands them."""
from __future__ import annotations

from dataclasses import dataclass, field

# Hands the forwarded port to qBittorrent (relies on qBittorrent's "bypass auth for localhost").
PORT_UP_COMMAND = ("/bin/sh -c 'wget -O- --retry-connrefused --post-data \"json={\\\"listen_port\\\":{{PORTS}}}\" "
                   "http://127.0.0.1:8080/api/v2/app/setPreferences 2>&1'")


@dataclass
class Provider:
    id: str
    label: str
    vpn_type: str
    fields: list[str]
    port_forwarding: bool
    extra_env: dict[str, str] = field(default_factory=dict)


_WG = "wireguard"
PROVIDERS: dict[str, Provider] = {p.id: p for p in [
    Provider("protonvpn", "Proton VPN", _WG, ["WIREGUARD_PRIVATE_KEY"], True),
    Provider("private internet access", "Private Internet Access", "openvpn",
             ["OPENVPN_USER", "OPENVPN_PASSWORD"], True),
    Provider("mullvad", "Mullvad", _WG, ["WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"], False),
    Provider("nordvpn", "NordVPN", _WG, ["WIREGUARD_PRIVATE_KEY"], False),
    Provider("surfshark", "Surfshark", _WG, ["WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES"], False),
    Provider("windscribe", "Windscribe", _WG,
             ["WIREGUARD_PRIVATE_KEY", "WIREGUARD_ADDRESSES", "WIREGUARD_PRESHARED_KEY"], False),
]}


def gluetun_env(provider_id: str, fields: dict[str, str], tz: str) -> dict[str, str]:
    p = PROVIDERS[provider_id]
    missing = [f for f in p.fields if not fields.get(f, "").strip()]
    if missing:
        raise ValueError(f"missing VPN fields: {', '.join(missing)}")
    env = {"VPN_SERVICE_PROVIDER": p.id, "VPN_TYPE": p.vpn_type, "TZ": tz, **p.extra_env}
    env.update({f: fields[f].strip() for f in p.fields})
    if p.port_forwarding:
        env.update(VPN_PORT_FORWARDING="on", PORT_FORWARD_ONLY="on", VPN_PORT_FORWARDING_UP_COMMAND=PORT_UP_COMMAND)
    return env
