"""Updating the apps in one click: new images, the containers recreated on them, the old images deleted. Runs in
the settings worker (plaid-apply), so the guard and a second click see it running."""
from __future__ import annotations

import json
import os
import time

from installer import docker, flow
from installer.compose import DISK_APPS
from installer.docker import DockerError
from installer.flow import Progress, Report

UPDATE_FILE = "update.json"
RETRY_WAITS = (5, 15)  # seconds before the 2nd and 3rd try: lscr.io refuses bursts ("retry-after")


def last(state_dir: str = flow.STATE_DIR) -> dict | None:
    """{"at": epoch seconds, "updated": [services]} of the last update that went through."""
    try:
        with open(os.path.join(state_dir, UPDATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run(report: Report, dk=docker, state_dir: str = flow.STATE_DIR, wait=time.sleep) -> list[str] | None:
    """The services whose image changed, or None on failure. Nothing is undone: a pulled image only takes room."""
    s = flow.load_settings(state_dir)
    with open(os.path.join(state_dir, "compose.json"), encoding="utf-8") as f:
        services = list(json.load(f)["services"])
    images = lambda: {svc: c.get("image") for svc, c in dk.stack_containers().items()}
    step = "step_pull"
    try:
        report(Progress(step))
        before = images()
        skipped = _pull(dk, state_dir, services, wait)
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
    report(Progress("step_done", done=True, detail=",".join(changed), skipped=skipped))
    return changed


def _pull(dk, state_dir: str, services: list[str], wait) -> list[str]:
    """One app at a time, three tries each: a registry refusing one app does not hold back the others. Returns the
    apps left on their old version; raises when none could be downloaded (no network)."""
    skipped, error = [], None
    for svc in services:
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                dk.compose(state_dir, "pull", svc)
                break
            except DockerError as e:
                error = e
                if attempt < len(RETRY_WAITS):
                    wait(RETRY_WAITS[attempt])
        else:
            skipped.append(svc)
    if len(skipped) == len(services):
        raise error
    return skipped
