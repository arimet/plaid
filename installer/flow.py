"""Install and uninstall, step by step. Every step reports a Progress whose texts are i18n keys."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import time
from dataclasses import dataclass
from typing import Callable

from installer import apps, docker
from installer.compose import PORTS, render_compose
from installer.docker import DockerError
from installer.settings import Settings, validate

STATE_DIR = os.environ.get("ARR_STATE_DIR", "/state")
STATE_VOLUME = "arr-installer-state"
INSTALLER_NAME = "arr-installer"
STATE_FILES = ("compose.json", "settings.json")
VPN_WAIT = 180


@dataclass
class Progress:
    step: str
    done: bool = False
    error: str | None = None
    detail: str = ""


Report = Callable[[Progress], None]


def is_complete(state_dir: str = STATE_DIR) -> bool:
    """True once an install went all the way; a failed one leaves only what Remove needs."""
    return os.path.exists(os.path.join(state_dir, "complete"))


def load_settings(state_dir: str = STATE_DIR) -> Settings | None:
    try:
        with open(os.path.join(state_dir, "settings.json"), encoding="utf-8") as f:
            return Settings.from_json(f.read())
    except FileNotFoundError:
        return None


def media_size(s: Settings, dk=docker) -> int:
    try:
        return dk.dir_state(dk.host_path(s.data_dir, s.platform))[1]
    except DockerError:
        return 0


def preflight(s: Settings, dk=docker) -> list[str]:
    """Blocking errors (err_*) first, then warnings the user must confirm (warn_*)."""
    errors = validate(s)
    if errors:
        return errors
    if dk.stack_exists():
        return ["err_stack_exists"]
    if (s.vpn_provider or s.tailscale_key) and s.platform in ("linux", "synology") and not dk.has_device("/dev/net/tun"):
        return ["err_no_tun"]
    if s.tailscale_key and dk.host_has_tailscale():
        return ["err_tailscale_exists"]
    found = [f"err_port_busy:{port}" for port in PORTS.values() if dk.port_busy(port)]
    try:
        empty, _ = dk.dir_state(dk.host_path(s.data_dir, s.platform))
    except DockerError:
        empty = True  # unknown: the install step creates the folders and reports a real failure
    if not empty:
        found.append("warn_dir_not_empty")
    if not s.vpn_provider:
        found.append("warn_no_vpn")
    return found


def _write_state(s: Settings, state_dir: str, data_source: str, has_dri: bool) -> None:
    os.makedirs(state_dir, exist_ok=True)
    files = {"compose.json": json.dumps(render_compose(s, data_source, has_dri), indent=1, ensure_ascii=False),
             "settings.json": s.to_json()}
    for name, text in files.items():
        path = os.path.join(state_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o600)


def _wait_gluetun(dk, state_dir: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while True:
        out = dk.compose(state_dir, "ps", "--format", "json", "gluetun").stdout
        if '"healthy"' in out.replace(" ", ""):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def install(s: Settings, report: Report, dk=docker, configure=apps.configure_all, state_dir: str = STATE_DIR,
            vpn_wait: int = VPN_WAIT, arch: str = platform.machine()) -> dict[str, int] | None:
    """Returns {app: port} once everything is linked, None on failure (the last Progress holds the error)."""
    step = "step_prepare"
    try:
        report(Progress(step))
        src = dk.host_path(s.data_dir, s.platform)
        # VAAPI only on Intel/AMD: a Raspberry Pi has /dev/dri too, but Jellyfin cannot transcode with it.
        has_dri = (s.platform in ("linux", "synology") and arch in ("x86_64", "AMD64")
                   and dk.has_device("/dev/dri"))
        _write_state(s, state_dir, src, has_dri)

        step = "step_folders"
        report(Progress(step))
        dk.ensure_data_dirs(src, s.puid, s.pgid)

        step = "step_download"
        report(Progress(step))
        dk.compose(state_dir, "pull")

        step = "step_start"
        report(Progress(step))
        try:
            dk.compose(state_dir, "up", "-d")
        except DockerError as e:
            port = re.search(r":(\d+) failed: port is already allocated|port (\d+).*already", str(e))
            if port:
                report(Progress(step, error=f"err_port_busy:{port.group(1) or port.group(2)}", detail=str(e)))
                return None
            raise
        dk.connect_network(dk.self_container_id())

        if s.vpn_provider:
            step = "step_vpn"
            report(Progress(step))
            if not _wait_gluetun(dk, state_dir, vpn_wait):
                report(Progress(step, error="err_vpn_failed"))
                return None

        step = "step_configure"
        report(Progress(step))

        def host_of(svc: str) -> str:
            name = "gluetun" if svc == "qbittorrent" and s.vpn_provider else svc
            return f"{name}:{PORTS[svc]}"

        configure(s, host_of, lambda app: report(Progress(f"step_app:{app}")),
                  lambda *args: dk.compose(state_dir, *args).stdout, has_dri)
        open(os.path.join(state_dir, "complete"), "w").close()
        report(Progress("step_done", done=True))
        return dict(PORTS)
    except Exception as e:  # shown to the user with its detail; the stack stays removable
        report(Progress(step, error="err_unexpected", detail=str(e)))
        return None


def pack_state(state_dir: str = STATE_DIR) -> str:
    files = {}
    for name in STATE_FILES:
        try:
            with open(os.path.join(state_dir, name), encoding="utf-8") as f:
                files[name] = f.read()
        except FileNotFoundError:
            pass
    return base64.b64encode(json.dumps(files).encode()).decode()


def uninstall(remove_media: bool, report: Report, dk=docker, state_dir: str = STATE_DIR) -> None:
    """Hands the work to a detached container: this one is deleted along the way."""
    report(Progress("step_uninstall"))
    args = ["--uninstall-worker"] + (["--remove-media"] if remove_media else [])
    dk.run_detached(dk.own_image(), args, {"ARR_STATE": pack_state(state_dir)})


def uninstall_worker(payload: str, remove_media: bool, dk=docker, work_dir: str = "/tmp/arr-state") -> None:
    files = json.loads(base64.b64decode(payload))
    os.makedirs(work_dir, exist_ok=True)
    for name, text in files.items():
        with open(os.path.join(work_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
    if "compose.json" in files and '"tailscale"' in files["compose.json"]:
        try:  # take the machine off the user's tailnet before deleting its state
            dk.compose(work_dir, "exec", "-T", "tailscale", "tailscale", "logout")
        except docker.DockerError:
            pass
    if "compose.json" in files:
        dk.compose(work_dir, "down", "-v", "--rmi", "all", "--remove-orphans")
    s = Settings.from_json(files["settings.json"]) if "settings.json" in files else None
    if remove_media and s:
        dk.wipe_dir(s.data_dir, s.home, s.platform)
    dk.remove("container", INSTALLER_NAME)
    dk.remove("network", f"{docker.PROJECT}_default")  # compose could not: the installer was attached to it
    dk.remove("volume", STATE_VOLUME)
    dk.remove("image", "self")
