"""Updating the apps in one click: new images, the containers recreated on them, the old images deleted. Runs in
the settings worker (plaid-apply), so the guard and a second click see it running."""
from __future__ import annotations

import json
import os
import time

from installer import docker, flow
from installer.compose import DISK_APPS
from installer.flow import Progress, Report

UPDATE_FILE = "update.json"


def last(state_dir: str = flow.STATE_DIR) -> dict | None:
    """{"at": epoch seconds, "updated": [services]} of the last update that went through."""
    try:
        with open(os.path.join(state_dir, UPDATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run(report: Report, dk=docker, state_dir: str = flow.STATE_DIR) -> list[str] | None:
    """The services whose image changed, or None on failure. Nothing is undone: a pulled image only takes room."""
    s = flow.load_settings(state_dir)
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        services = list(json.load(f)["services"])
    images = lambda: {svc: c.get("image") for svc, c in dk.stack_containers().items()}
    step = "step_pull"
    try:
        report(Progress(step))
        before = images()
        dk.compose(state_dir, "pull")
        step = "step_recreate"
        report(Progress(step))
        # The disk apps only on the disk: otherwise the guard starts them on their new image when it comes back.
        disk_ok = dk.marker_state(dk.host_path(s.data_dir, s.platform)) == "ok"
        dk.compose(state_dir, "up", "-d", *[svc for svc in services if svc not in DISK_APPS or disk_ok])
        after = images()
        changed = [svc for svc in services if before.get(svc) and after.get(svc) and before[svc] != after[svc]]
        step = "step_cleanup_images"
        report(Progress(step))
        for svc in changed:
            dk.remove("image", before[svc])  # best effort: another container may still use it
    except Exception as e:
        report(Progress(step, error="err_update_failed", detail=str(e)))
        return None
    flow.write_json(os.path.join(state_dir, UPDATE_FILE), {"at": time.time(), "updated": changed})
    report(Progress("step_done", done=True, detail=",".join(changed)))
    return changed
