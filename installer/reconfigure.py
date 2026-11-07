"""Changing a setting on an installed stack: what changes, which steps that takes, and applying them."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Callable

from installer import apps, docker, flow
from installer.compose import PORTS
from installer.flow import Progress
from installer.settings import Settings, validate

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


# ---------------------------------------------------------------- checking and applying

# An empty folder may be the mount point of a disk that is not plugged in (Linux, fstab): it cannot be told
# apart from a new folder on the system disk, so it is a warning that asks, not a refusal.
FOLDER_CHECK = {"missing": "err_folder_missing", "unknown": "err_folder_unreadable",
                "empty": "warn_folder_empty", "other": "warn_folder_no_library"}


def check(old: Settings, new: Settings, dk=docker) -> list[str]:
    """What stops the change (err_*) or deserves a warning (warn_*), before anything is touched."""
    found = list(validate(new))
    chg = changes(old, new)
    if chg.get("vpn") == "add" and not dk.has_device("/dev/net/tun"):
        found.append("err_no_tun")
    if chg.get("tailscale") == "add" and dk.host_has_tailscale():
        found.append("err_tailscale_exists")
    if "folder" in chg:
        state = dk.folder_state(dk.host_path(new.data_dir, new.platform))
        if state in FOLDER_CHECK:
            found.append(FOLDER_CHECK[state])
    return found


def host_of_for(s: Settings) -> Callable[[str], str]:
    """Where each app answers on the stack network: qBittorrent sits behind gluetun with a VPN."""
    return lambda svc: f"{'gluetun' if svc == 'qbittorrent' and s.vpn_provider else svc}:{PORTS[svc]}"


def default_ops(new: Settings, dk, state_dir: str) -> SimpleNamespace:
    host_of = host_of_for(new)
    compose_out = lambda *args: dk.compose(state_dir, *args).stdout
    arr = lambda name: apps.Arr(name, host_of(name), "v3", compose_out)
    return SimpleNamespace(
        set_host=lambda app, host: arr(app).set_download_client_host(host),
        change_account=lambda old, new_, current: apps.change_account(old, new_, host_of, compose_out, current,
                                                                      lambda app: None),
        rescan=lambda s: apps.rescan_all(s, host_of, compose_out))


def _qbit_host(s: Settings) -> str:
    return "gluetun" if s.vpn_provider else "qbittorrent"


def apply(old: Settings, new: Settings, current: dict[str, str], report: flow.Report, dk=docker,
          state_dir: str = flow.STATE_DIR, vpn_wait: int = flow.VPN_WAIT, ops=None) -> list[str] | None:
    """Runs the steps the change needs. Returns the apps whose account did not change ([] when all did),
    None on failure: everything but the account is then put back as it was."""
    chg = changes(old, new)
    todo = steps(chg, bool(new.vpn_provider))
    ops = ops or default_ops(new, dk, state_dir)
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        has_dri = "/dev/dri" in f.read()
    src_new = dk.host_path(new.data_dir, new.platform)
    logout = lambda: _try(lambda: dk.compose(state_dir, "exec", "-T", "tailscale", "tailscale", "logout"))
    started: list[str] = []
    step = todo[0]
    try:
        for step in todo:
            if step in ("step_account", "step_save"):  # reported below, after the part that is never undone
                break
            started.append(step)
            report(Progress(step))
            if step == "step_folder":
                dk.ensure_data_dirs(src_new, new.puid, new.pgid, create=False)
            elif step == "step_compose":
                if chg.get("tailscale") == "remove":  # off the user's tailnet before its state goes
                    logout()
                # The apps still have the old account until step_account: keep signing in with it meanwhile.
                flow._write_state(replace(new, username=old.username, password=old.password), state_dir, src_new,
                                  has_dri)
                dk.compose(state_dir, "up", "-d", "--remove-orphans")
                if chg.get("tailscale") == "remove":
                    dk.remove("volume", f"{docker.PROJECT}_tailscale-state")
            elif step == "step_vpn":
                _vpn_up(dk, state_dir, vpn_wait)
            elif step == "step_download_client":
                for app in ("radarr", "sonarr"):
                    ops.set_host(app, _qbit_host(new))
            elif step == "step_tailscale":  # a joined machine ignores a new key: start over on the new account
                logout()
                dk.compose(state_dir, "rm", "-sf", "tailscale")
                dk.remove("volume", f"{docker.PROJECT}_tailscale-state")
                dk.compose(state_dir, "up", "-d", "tailscale")
            elif step == "step_rescan":
                # Best effort: a scan that fails (Jellyfin still starting, password changed by hand) must not
                # undo the new folder; the apps also scan on their own schedule.
                _try(lambda: ops.rescan(old))  # the account changes last: the current one opens the apps here
    except Exception as e:  # noqa: BLE001 - shown with its detail; the stack goes back to how it was
        _rollback(old, started, dk, state_dir, has_dri, ops, vpn_wait)
        report(Progress(step, error="err_apply_failed", detail=str(e)))
        return None

    not_changed: list[str] = []
    saved = new
    if "step_account" in todo:
        report(Progress("step_account"))
        not_changed = ops.change_account(old, new, current)
        if len(not_changed) == len(ACCOUNT_APPS):  # nothing took the new password: Plaid keeps the old one
            saved = replace(new, username=old.username, password=old.password)
    report(Progress("step_save"))
    flow._write_state(saved, state_dir, src_new, has_dri)
    if "folder" in chg:  # Plaid did not make this folder: removing the stack must not offer to delete it
        _try(lambda: os.remove(os.path.join(state_dir, "created_folder")))
    report(Progress("step_done", done=True, detail=",".join(not_changed)))
    return not_changed


ACCOUNT_APPS = ("radarr", "sonarr", "prowlarr", "bazarr", "qbittorrent", "jellyfin")


def _vpn_up(dk, state_dir: str, vpn_wait: int) -> None:
    if not flow._wait_gluetun(dk, state_dir, vpn_wait):
        raise RuntimeError("The VPN did not connect")
    dk.compose(state_dir, "up", "-d", "--force-recreate", "--no-deps", "qbittorrent")


def _rollback(old: Settings, started: list[str], dk, state_dir: str, has_dri: bool, ops, vpn_wait: int) -> None:
    """Best effort: the error the user sees is the one that stopped the change, not one from here."""
    flow._write_state(old, state_dir, dk.host_path(old.data_dir, old.platform), has_dri)
    if "step_compose" in started:
        _try(lambda: dk.compose(state_dir, "up", "-d", "--remove-orphans"))
        if old.vpn_provider:
            _try(lambda: _vpn_up(dk, state_dir, vpn_wait))
    if "step_download_client" in started:
        for app in ("radarr", "sonarr"):
            _try(lambda app=app: ops.set_host(app, _qbit_host(old)))


def _try(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001
        pass


def run_worker(dk=docker, state_dir: str = flow.STATE_DIR) -> int:
    """The detached container's job: closing the terminal or the browser cannot stop a change half way."""
    path = os.path.join(state_dir, flow.APPLY_REQUEST)
    with open(path, encoding="utf-8") as f:
        request = json.load(f)
    os.remove(path)  # it holds secrets
    old, new = flow.load_settings(state_dir), Settings(**request["settings"])
    events: list[dict] = []

    def report(p: Progress) -> None:
        events.append(asdict(p))
        flow.write_json(os.path.join(state_dir, flow.APPLY_FILE), events)
    dk.connect_network(dk.self_container_id())
    return 0 if apply(old, new, request.get("current") or {}, report, dk=dk, state_dir=state_dir) is not None else 1
