"""Install and uninstall, step by step. Every step reports a Progress whose texts are i18n keys."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from installer import apps, docker
from installer.compose import PORTS, render_compose
from installer.docker import DockerError
from installer.settings import Settings, validate

STATE_DIR = os.environ.get("ARR_STATE_DIR", "/state")
STATE_VOLUME = "plaid-state"
INSTALLER_NAME = "plaid"
WORKER_NAME = docker.WORKER
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


def _prowlarr(dk, state_dir: str):
    return apps.Arr("prowlarr", f"prowlarr:{PORTS['prowlarr']}", "v1",
                    lambda *args: dk.compose(state_dir, *args).stdout)


def reattach(dk=docker, state_dir: str = STATE_DIR) -> None:
    """A recreated assistant (new image) starts outside the stack network; the panel needs Prowlarr."""
    if load_settings(state_dir) is None:
        return
    try:
        dk.connect_network(dk.self_container_id())
    except DockerError:
        pass  # no stack network (failed install): the panel still offers Remove


def tracker_catalog(dk=docker, state_dir: str = STATE_DIR) -> list[dict]:
    return apps.list_public_indexers(_prowlarr(dk, state_dir))


def add_tracker(definition: str, dk=docker, state_dir: str = STATE_DIR) -> None:
    apps.add_indexer(_prowlarr(dk, state_dir), definition)


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


class Cancelled(Exception):
    pass


def created_folder(state_dir: str = STATE_DIR) -> bool:
    """True when the install made the media folder itself, so undoing the install may delete it."""
    return os.path.exists(os.path.join(state_dir, "created_folder"))


def install(s: Settings, report: Report, dk=docker, configure=apps.configure_all, state_dir: str = STATE_DIR,
            vpn_wait: int = VPN_WAIT, arch: str = platform.machine(),
            cancel: threading.Event | None = None) -> dict[str, int] | None:
    """Returns {app: port} once everything is linked, None on failure (the last Progress holds the error).
    Setting `cancel` stops it at the next step; rollback() then undoes what was done."""
    step = "step_prepare"

    def checkpoint(name: str) -> str:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        report(Progress(name))
        return name

    try:
        step = checkpoint(step)
        src = dk.host_path(s.data_dir, s.platform)
        # VAAPI only on Intel/AMD: a Raspberry Pi has /dev/dri too, but Jellyfin cannot transcode with it.
        has_dri = (s.platform in ("linux", "synology") and arch in ("x86_64", "AMD64")
                   and dk.has_device("/dev/dri"))
        _write_state(s, state_dir, src, has_dri)

        step = checkpoint("step_folders")
        try:
            was_empty = dk.dir_state(src)[0]
        except DockerError:
            was_empty = False  # unknown: never delete it on rollback
        if was_empty:
            open(os.path.join(state_dir, "created_folder"), "w").close()
        dk.ensure_data_dirs(src, s.puid, s.pgid)

        step = checkpoint("step_download")
        dk.compose(state_dir, "pull")

        step = checkpoint("step_start")
        try:
            dk.compose(state_dir, "up", "-d")
        except DockerError as e:
            text = str(e)
            port = re.search(r"\d+\.\d+\.\d+\.\d+:(\d+)", text)  # Desktop: "already allocated", Engine: "in use"
            if port and ("already allocated" in text or "already in use" in text):
                report(Progress(step, error=f"err_port_busy:{port.group(1)}", detail=text))
                return None
            raise
        dk.connect_network(dk.self_container_id())

        if s.vpn_provider:
            step = checkpoint("step_vpn")
            if not _wait_gluetun(dk, state_dir, vpn_wait):
                report(Progress(step, error="err_vpn_failed"))
                return None

        step = checkpoint("step_configure")

        def host_of(svc: str) -> str:
            name = "gluetun" if svc == "qbittorrent" and s.vpn_provider else svc
            return f"{name}:{PORTS[svc]}"

        configure(s, host_of, lambda app: report(Progress(f"step_app:{app}")),
                  lambda *args: dk.compose(state_dir, *args).stdout, has_dri)
        open(os.path.join(state_dir, "complete"), "w").close()
        report(Progress("step_done", done=True))
        return dict(PORTS)
    except Cancelled:
        report(Progress(step, error="err_cancelled"))
        return None
    except Exception as e:  # shown to the user with its detail; the stack stays removable
        report(Progress(step, error="err_unexpected", detail=str(e)))
        return None


GUARD_FILE = "guard.json"
GUARD_DEFAULT = {"disk": "unknown", "vpn": "off"}  # no alert until the guard has looked
GUARD_STALE = 150  # seconds: rounds come every 30 s, so an older status means the guard is stuck or gone


def guard_status(state_dir: str = STATE_DIR) -> dict:
    """What the guard saw last: {"disk": ok|missing|unconfirmed|unknown, "vpn": off|ok|down}."""
    try:
        with open(os.path.join(state_dir, GUARD_FILE), encoding="utf-8") as f:
            saved = json.load(f)
        if time.time() - float(saved.get("at", 0)) > GUARD_STALE:
            return dict(GUARD_DEFAULT)
        return {k: saved.get(k, v) for k, v in GUARD_DEFAULT.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(GUARD_DEFAULT)


def save_guard_status(status: dict, state_dir: str = STATE_DIR) -> None:
    path = os.path.join(state_dir, GUARD_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({**status, "at": time.time()}, f)
    os.replace(path + ".tmp", path)  # the panel and the terminal never read half a file


def confirm_disk(s: Settings, dk=docker, state_dir: str = STATE_DIR) -> bool:
    """The user says this folder holds their media: mark it, the guard starts the apps on its next round.
    False when the folder is no longer what the question was about (disk unplugged meanwhile) or cannot be written."""
    src = dk.host_path(s.data_dir, s.platform)
    if dk.marker_state(src) != "unconfirmed":
        return False
    try:
        dk.write_marker(src)
    except DockerError:
        return False
    save_guard_status({**guard_status(state_dir), "disk": "ok"}, state_dir)
    return True


APPLY_REQUEST, APPLY_FILE = "apply-request.json", "apply.json"


def write_json(path: str, data, mode: int = 0o600) -> None:
    """Whole file or nothing: readers in other containers never see half of it."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def start_apply(new: Settings, current: dict[str, str], dk=docker, state_dir: str = STATE_DIR) -> None:
    """Hands a settings change to a detached container that mounts the state volume."""
    write_json(os.path.join(state_dir, APPLY_REQUEST), {"settings": json.loads(new.to_json()), "current": current})
    write_json(os.path.join(state_dir, APPLY_FILE), [])
    with OPS_LOCK:  # a running guard round finishes first; the next one sees the worker and stands back
        dk.run_detached(dk.own_image(), ["--apply-worker"], {}, dk.APPLY_WORKER,
                        volumes=(f"{STATE_VOLUME}:/state",))


def apply_status(dk=docker, state_dir: str = STATE_DIR) -> dict:
    """{"state": idle|running|done|failed, "events": [Progress as dict]}. A stopped worker is removed here, so
    the guard does not stay paused and the next change can start."""
    try:
        with open(os.path.join(state_dir, APPLY_FILE), encoding="utf-8") as f:
            events = json.load(f)
    except (OSError, ValueError):
        events = []
    last = events[-1] if events else {}
    final = "done" if last.get("done") else "failed" if last.get("error") else None
    exists, running, _ = dk.container_state(dk.APPLY_WORKER)
    if exists and running:
        return {"state": "running", "events": events}
    if exists:
        if not final:  # it stopped without a last word: it crashed
            events.append(asdict(Progress(last.get("step", "step_save"), error="err_apply_failed",
                                          detail=dk.logs(dk.APPLY_WORKER))))
            final = "failed"
        dk.remove("container", dk.APPLY_WORKER)
    return {"state": final or "idle", "events": events}


def check_logins(s: Settings) -> list[str]:
    from installer import reconfigure
    return apps.check_logins(s, reconfigure.host_of_for(s))


def settings_check(old: Settings, new: Settings) -> list[str]:
    from installer import reconfigure
    return reconfigure.check(old, new)


def pack_state(state_dir: str = STATE_DIR) -> str:
    files = {}
    for name in STATE_FILES:
        try:
            with open(os.path.join(state_dir, name), encoding="utf-8") as f:
                files[name] = f.read()
        except FileNotFoundError:
            pass
    return base64.b64encode(json.dumps(files).encode()).decode()


# Held by a guard round, and by a removal while it starts its worker: the guard must not start the apps again
# while the worker takes the stack down.
OPS_LOCK = threading.Lock()


def uninstall(remove_media: bool, report: Report, dk=docker, state_dir: str = STATE_DIR) -> None:
    """Hands the work to a detached container: this one is deleted along the way."""
    report(Progress("step_uninstall"))
    args = ["--uninstall-worker"] + (["--remove-media"] if remove_media else [])
    with OPS_LOCK:  # a running guard round finishes first; the next one sees the worker and stands back
        dk.run_detached(dk.own_image(), args, {"ARR_STATE": pack_state(state_dir)}, WORKER_NAME)


def rollback(report: Report, dk=docker, state_dir: str = STATE_DIR) -> None:
    """Undoes an interrupted install: everything goes, the media folder too if the install made it."""
    uninstall(created_folder(state_dir), report, dk=dk, state_dir=state_dir)


def uninstall_status(dk=docker) -> tuple[str, str]:
    """("idle" | "running" | "done" | "failed", detail). A worker that stopped with an error while we still
    answer means the removal failed: show its logs. One that stopped cleanly is done; the reaper deletes it
    once no container runs the installer image (the terminal mode is one of those, so it must not wait)."""
    exists, running, code = dk.container_state(WORKER_NAME)
    if not exists:
        return "idle", ""
    if running:
        return "running", ""
    if code == 0:
        return "done", ""
    detail = dk.logs(WORKER_NAME)
    dk.remove("container", WORKER_NAME)
    return "failed", detail


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
