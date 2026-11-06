"""Changing a setting on an installed stack: what changes, which steps that takes, and applying them."""
from __future__ import annotations

from dataclasses import replace

from installer.settings import Settings

STEPS_ORDER = ("step_folder", "step_compose", "step_vpn", "step_download_client", "step_tailscale",
               "step_rescan", "step_account", "step_save")


def merge(old: Settings, data: dict) -> Settings:
    """The settings after the form. Secrets left blank keep their current value: they are never sent back."""
    text = lambda key, current: str(data[key]).strip() if str(data.get(key) or "").strip() else current
    provider, fields = old.vpn_provider, old.vpn_fields
    if "vpn_provider" in data:
        provider = data["vpn_provider"] or None
        given = {str(k): str(v) for k, v in (data.get("vpn_fields") or {}).items() if str(v).strip()}
        if provider is None:
            fields = {}
        elif provider != old.vpn_provider:
            fields = given
        else:
            fields = {**old.vpn_fields, **given}
    tailscale = "" if data.get("remove_tailscale") else text("tailscale_key", old.tailscale_key)
    return replace(old, data_dir=text("data_dir", old.data_dir), username=text("username", old.username),
                   password=str(data.get("password") or "") or old.password, vpn_provider=provider,
                   vpn_fields=fields, timezone=text("timezone", old.timezone),
                   lang=data["lang"] if data.get("lang") in ("en", "fr") else old.lang, tailscale_key=tailscale)


def _kind(before, after) -> str | None:
    if before == after:
        return None
    return "add" if not before else "remove" if not after else "change"


def changes(old: Settings, new: Settings) -> dict[str, str]:
    found = {
        "lang": "change" if old.lang != new.lang else None,
        "timezone": "change" if old.timezone != new.timezone else None,
        "vpn": _kind(old.vpn_provider and (old.vpn_provider, old.vpn_fields),
                     new.vpn_provider and (new.vpn_provider, new.vpn_fields)),
        "tailscale": _kind(old.tailscale_key, new.tailscale_key),
        "account": "change" if (old.username, old.password) != (new.username, new.password) else None,
        "folder": "change" if old.data_dir != new.data_dir else None,
    }
    return {k: v for k, v in found.items() if v}


def steps(chg: dict[str, str], vpn_after: bool) -> list[str]:
    wanted = {"step_save"}
    if "folder" in chg:
        wanted |= {"step_folder", "step_compose", "step_rescan"}
    if "timezone" in chg:  # gluetun takes the time zone too (TZ, and nearby servers)
        wanted |= {"step_compose"} | ({"step_vpn"} if vpn_after else set())
    if "vpn" in chg:
        wanted |= {"step_compose"}
        if chg["vpn"] != "remove":
            wanted.add("step_vpn")
        if chg["vpn"] in ("add", "remove"):  # qBittorrent moves in or out of gluetun's network
            wanted.add("step_download_client")
    if "tailscale" in chg:
        wanted.add("step_compose")
        if chg["tailscale"] == "change":
            wanted.add("step_tailscale")
    if "account" in chg:
        wanted.add("step_account")
    return [step for step in STEPS_ORDER if step in wanted]


def outage(chg: dict[str, str]) -> str:
    if "timezone" in chg or "folder" in chg:
        return "outage_apps"
    if "vpn" in chg:
        return "outage_downloads"
    return "outage_none"
