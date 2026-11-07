"""Backups of the apps' settings and Plaid's own, made when the user asks, and putting one back. Both run in the
settings worker
(plaid-apply): one operation on the stack at a time, and the guard stands back meanwhile."""
from __future__ import annotations

import json
import ntpath
import os
import posixpath
from dataclasses import replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from installer import docker, flow
from installer.compose import DISK_APPS, SERVICES
from installer.flow import Progress, Report
from installer.settings import Settings

STATE_FILE = "backup.json"
STOP_TIMEOUT = 300


def status(state_dir: str = flow.STATE_DIR) -> dict:
    """{"at", "name", "size", "dir"} of the last backup, {"failed_at", "error"} of the last failure."""
    try:
        with open(os.path.join(state_dir, STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def note(state_dir: str, **fields) -> None:
    flow.write_json(os.path.join(state_dir, STATE_FILE), {**status(state_dir), **fields})


def local_now(s: Settings, utc: datetime | None = None) -> datetime:
    """The time in the stack's zone (the one chosen in Plaid), not the container's: a backup is named in it."""
    utc = utc or datetime.now(timezone.utc)
    try:
        return utc.astimezone(ZoneInfo(s.timezone))
    except Exception:  # an unknown zone: UTC rather than no backup at all
        return utc


def target(s: Settings, dk=docker) -> tuple[str, str, bool]:
    """(host folder to mount, sub-folder inside it, on the media disk?). The default lives in the media folder."""
    if s.backup_dir:
        return dk.host_path(s.backup_dir, s.platform), "", False
    return dk.host_path(s.data_dir, s.platform), "backups", True


def where(s: Settings) -> str:
    """The backup folder as the user writes it."""
    return s.backup_dir or (ntpath if s.platform == "windows" else posixpath).join(s.data_dir, "backups")


def _stack(state_dir: str, dk) -> tuple[list[str], list[str]]:
    """(the apps to stop, the volumes to save). gluetun and Tailscale keep running: no database, and stopping them
    would cut a user who backs up over Tailscale. Tailscale's identity is the machine's, not a setting."""
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        spec = json.load(f)
    return ([svc for svc in spec["services"] if svc in SERVICES],
            [f"{dk.PROJECT}_{v}" for v in spec.get("volumes", {}) if v != "tailscale-state"])


def _start_again(s: Settings, dk, state_dir: str) -> None:
    """The disk apps only with the disk there: otherwise the guard starts them when it comes back."""
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        services = list(json.load(f)["services"])  # all, gluetun and Tailscale too: up leaves running ones alone
    disk_ok = dk.marker_state(dk.host_path(s.data_dir, s.platform)) == "ok"
    dk.compose(state_dir, "up", "-d", *[svc for svc in services if svc not in DISK_APPS or disk_ok],
               timeout=STOP_TIMEOUT)


def _operate(kind: str, work, report: Report, dk, state_dir: str, s: Settings) -> bool:
    """Stops the apps, does the work, starts them again whatever happened. False on failure, reported."""
    step, stopped = f"step_{kind}_stop", False
    try:
        report(Progress(step))
        services, _ = _stack(state_dir, dk)
        dk.compose(state_dir, "stop", *services, timeout=STOP_TIMEOUT)
        stopped = True
        step = f"step_{kind}_copy"
        report(Progress(step))
        work()
        step = f"step_{kind}_start"
        report(Progress(step))
        _start_again(flow.load_settings(state_dir) or s, dk, state_dir)
        return True
    except Exception as e:
        if stopped and step != f"step_{kind}_start":
            try:
                _start_again(s, dk, state_dir)
            except Exception:
                pass  # the guard's next round starts the disk apps; the error below says what broke
        report(Progress(step, error=f"err_{kind}_failed", detail=str(e)))
        if kind == "backup":
            note(state_dir, failed_at=datetime.now().timestamp(), error=str(e))
        return False


def run(report: Report, dk=docker, state_dir: str = flow.STATE_DIR, now=None) -> str | None:
    """One backup. Returns its file name, or None on failure."""
    s = flow.load_settings(state_dir)
    when = now() if now else local_now(s)
    name = when.strftime(f"{docker.BACKUP_PREFIX}%Y-%m-%d-%H%M.tar.gz")
    dest, sub, on_media = target(s, dk)
    # Never onto the empty mount point of an absent disk: the media disk has its marker, a backup folder Plaid
    # already wrote to has its own.
    away = (dk.marker_state(dest) != "ok" if on_media else
            status(state_dir).get("dir") == s.backup_dir and dk.backup_folder_state(dest) != "ok")
    if away:
        key = "err_backup_disk" if on_media else "err_backup_folder"
        report(Progress("step_backup_stop", error=key))
        note(state_dir, failed_at=when.timestamp(), error=key)
        return None
    size = {}
    _, volumes = _stack(state_dir, dk)
    work = lambda: size.setdefault("n", dk.backup_archive(volumes, dest, sub, name))
    if not _operate("backup", work, report, dk, state_dir, s):
        return None
    note(state_dir, at=when.timestamp(), name=name, size=size["n"], dir=s.backup_dir, failed_at=None, error=None)
    report(Progress("step_done", done=True, detail=name))
    return name


# Where things are and how the machine reaches out stay as they are today: that day's folder may be gone, its
# backup folder would hide the other backups, its Tailscale or VPN key may be revoked.
KEPT_ON_RESTORE = ("data_dir", "backup_dir", "tailscale_key", "vpn_provider", "vpn_fields", "puid", "pgid", "home",
                   "platform")


def restore(name: str, report: Report, dk=docker, state_dir: str = flow.STATE_DIR) -> bool:
    """Puts back the apps' settings and Plaid's own as backup `name` has them, but for KEPT_ON_RESTORE."""
    s = flow.load_settings(state_dir)
    dest, sub, _ = target(s, dk)
    _, volumes = _stack(state_dir, dk)
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        has_dri = "/dev/dri" in f.read()

    def work():
        dk.restore_archive(volumes, dest, sub, name)
        restored = flow.load_settings(state_dir) or s
        now = replace(restored, **{k: getattr(s, k) for k in KEPT_ON_RESTORE})
        flow._write_state(now, state_dir, dk.host_path(now.data_dir, now.platform), has_dri)  # compose follows
    if not _operate("restore", work, report, dk, state_dir, s):
        return False
    report(Progress("step_done", done=True, detail=name))
    return True
