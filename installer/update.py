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
# The guard stands back while the worker runs: a stalled download must not keep it away for hours.
PULL_TIMEOUT, UP_TIMEOUT = 600, 600


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
        refs = {svc: spec.get("image", "") for svc, spec in json.load(f)["services"].items()}
    services = list(refs)
    stale = (last(state_dir) or {}).get("stale", [])
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
        up = [svc for svc in services if svc not in DISK_APPS or disk_ok]
        dk.compose(state_dir, "up", "-d", *up, timeout=UP_TIMEOUT)
        after = images()
        changed = [svc for svc in services if before.get(svc) and after.get(svc) and before[svc] != after[svc]]
        # Left for the guard with a new image ready: their old image goes at the next update, once replaced.
        waiting = [svc for svc in services if svc not in up and svc not in skipped and before.get(svc)
                   and dk.image_id(refs[svc]) not in ("", before[svc])]
        step = "step_cleanup_images"
        report(Progress(step))
        for image in [before[svc] for svc in changed] + stale:
            dk.remove("image", image)  # best effort: a container still using it keeps it
    except Exception as e:
        report(Progress(step, error="err_update_failed", detail=str(e)))
        return None
    flow.write_json(os.path.join(state_dir, UPDATE_FILE),
                    {"at": time.time(), "updated": changed, "stale": [before[svc] for svc in waiting]})
    report(Progress("step_done", done=True, detail=",".join(changed), skipped=skipped, waiting=waiting))
    return changed


def _pull(dk, state_dir: str, services: list[str], wait, errors: dict | None = None) -> list[str]:
    """One app at a time, three tries each: a registry refusing one app does not hold back the others. Returns the
    apps left on their old version; raises when none could be downloaded (no network)."""
    skipped, error = [], None
    for svc in services:
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                dk.compose(state_dir, "pull", svc, timeout=PULL_TIMEOUT)
                break
            except DockerError as e:
                error = e
                if errors is not None:
                    errors[svc] = str(e)
                if "timed out" in str(e):  # stalled: another try would stall as long
                    skipped.append(svc)
                    break
                if attempt < len(RETRY_WAITS):
                    wait(RETRY_WAITS[attempt])
        else:
            skipped.append(svc)
    if len(skipped) == len(services):
        raise error
    return skipped
