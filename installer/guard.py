"""The guard: runs in the assistant container, which Docker restarts at boot, and every 30 s keeps the apps
that read the media folder off an absent disk, and qBittorrent on the network of the current VPN container."""
from __future__ import annotations

import sys
import threading
import time
import traceback

from installer import docker, flow
from installer.compose import DISK_APPS
from installer.docker import DockerError

INTERVAL = 30


def decide(disk: str, vpn_on: bool, containers: dict, blind: set[str]) -> tuple[list[tuple], str]:
    """(actions in order, VPN state). `blind`: running disk apps that no longer see the marker."""
    actions = []
    g, q = containers.get("gluetun"), containers.get("qbittorrent")
    vpn = "off"
    if vpn_on:
        vpn = "down" if not g or not g["running"] or g["health"] == "unhealthy" else "ok"
        # qBittorrent lives in gluetun's network: when gluetun restarted or was recreated, it holds a dead one.
        # Only with the disk there, or the disk rule would stop it again on every round.
        if disk == "ok" and g and g["running"] and g["health"] == "healthy" and q and (
                q["network"] != f"container:{g['id']}" or
                (q["running"] and q["started"][:19] < g["started"][:19])):  # Docker trims trailing zeros
            actions.append(("recreate_qbittorrent",))
    if disk == "ok":
        actions.append(("up",))
        actions += [("restart", svc) for svc in sorted(blind)]
    elif disk in ("missing", "unconfirmed") and any(containers.get(svc, {}).get("running") for svc in DISK_APPS):
        actions.append(("stop",))
    return actions, vpn


def cycle(dk=docker, fl=flow, state_dir: str = flow.STATE_DIR) -> dict | None:
    """One round. Does nothing while an install is not finished or a removal runs: they own the stack then."""
    if not fl.is_complete(state_dir) or dk.container_state(dk.WORKER)[0]:
        return None
    s = fl.load_settings(state_dir)
    disk = dk.marker_state(dk.host_path(s.data_dir, s.platform))
    containers = dk.stack_containers()
    blind = set()
    if disk == "ok":
        blind = {svc for svc in DISK_APPS
                 if containers.get(svc, {}).get("running") and not dk.sees_marker(state_dir, svc)}
    actions, vpn = decide(disk, bool(s.vpn_provider), containers, blind)
    commands = {"recreate_qbittorrent": ("up", "-d", "--force-recreate", "--no-deps", "qbittorrent"),
                "up": ("up", "-d", *DISK_APPS), "stop": ("stop", *DISK_APPS)}
    for action in actions:
        try:
            dk.compose(state_dir, *(commands.get(action[0]) or action))
        except DockerError as e:  # the next action, and the next round, may still fix things
            print(f"guard: {action[0]} failed: {e}", file=sys.stderr)
    status = {"disk": disk, "vpn": vpn}
    fl.save_guard_status(status, state_dir)
    return status


def _loop(interval: int) -> None:
    while True:
        try:
            cycle()
        except Exception:  # a round that breaks must not end the guard
            traceback.print_exc()
        time.sleep(interval)


def start(interval: int = INTERVAL) -> threading.Thread:
    thread = threading.Thread(target=_loop, args=(interval,), daemon=True, name="guard")
    thread.start()
    return thread
