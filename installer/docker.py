"""Docker CLI calls. Host folders are reached through throwaway containers, since the
installer itself runs in a container and only sees the host through the Docker socket."""
from __future__ import annotations

import ntpath
import posixpath
import socket
import subprocess
import uuid
from typing import Callable

from installer.settings import is_dangerous_path

PROJECT = "plaid"
HELPER = "alpine:3.20"
Run = Callable[[list], subprocess.CompletedProcess]


class DockerError(Exception):
    pass


# A throwaway container that hangs must not freeze the assistant. Deleting media has no limit: it can be long.
PROBE_TIMEOUT = 30  # Docker Desktop can hang on a bind of a just-deleted folder
HELPER_TIMEOUT = 120


def default_run(args: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{' '.join(args[:3])} timed out after {timeout}s")


def _check(run: Run, args: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    result = run(args, timeout=timeout)
    if result.returncode != 0:
        raise DockerError((result.stderr or result.stdout or "").strip() or f"exit {result.returncode}")
    return result


def host_path(path: str, platform: str) -> str:
    """Path as the Docker daemon sees it. Docker Desktop on Windows mounts drives under /run/desktop/mnt/host."""
    if platform == "windows" and len(path) >= 2 and path[1] == ":":
        rest = ntpath.normpath(path)[2:].replace("\\", "/")
        return f"/run/desktop/mnt/host/{path[0].lower()}{rest}"
    return path


def dir_state(src: str, run: Run = default_run) -> tuple[bool, int]:
    """(empty?, size in bytes) of a host folder. A missing folder counts as empty."""
    name = f"plaid-probe-{uuid.uuid4().hex[:8]}"
    result = run(["docker", "run", "--rm", "--name", name, "--mount", f"type=bind,src={src},dst=/m,readonly",
                  HELPER, "sh", "-c", 'echo $(ls -A /m | wc -l) $(du -sk /m | cut -f1)'], timeout=PROBE_TIMEOUT)
    if result.returncode != 0:
        if "does not exist" in (result.stderr or ""):
            return True, 0
        if result.returncode == 124:  # timed out: the probe may sit in "Created" forever
            run(["docker", "rm", "-f", name])
        raise DockerError(result.stderr.strip())
    count, kib = result.stdout.split()
    return count == "0", int(kib) * 1024


def ensure_data_dirs(src: str, puid: int, pgid: int, run: Run = default_run) -> None:
    dirs = " ".join(f"/m/{d}" for d in ("torrents/movies", "torrents/tv", "media/movies", "media/tv"))
    owned = "/m /m/torrents /m/media " + dirs
    _check(run, ["docker", "run", "--rm", "-v", f"{src}:/m", HELPER, "sh", "-c",
                 f"mkdir -p {dirs} && chown {puid}:{pgid} {owned}"], timeout=HELPER_TIMEOUT)


def wipe_dir(path: str, home: str, platform: str, run: Run = default_run) -> None:
    """Deletes the folder and everything in it. Refuses roots, shared parents and the home folder."""
    if is_dangerous_path(path, home):
        raise ValueError(f"refusing to delete {path}")
    src = host_path(path, platform).rstrip("/")
    parent, name = posixpath.split(src)
    _check(run, ["docker", "run", "--rm", "-v", f"{parent}:/p", HELPER, "rm", "-rf", f"/p/{name}"])


def stack_exists(run: Run = default_run) -> bool:
    out = _check(run, ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).stdout
    return bool(out.strip())


def compose(state_dir: str, *args: str, run: Run = default_run) -> subprocess.CompletedProcess:
    return _check(run, ["docker", "compose", "-p", PROJECT, "-f", f"{state_dir}/compose.json", *args])


def port_busy(port: int, run: Run = default_run) -> bool:
    """True when another container already publishes the port. Host programs are caught later,
    by compose's 'port is already allocated' error."""
    out = _check(run, ["docker", "ps", "-q", "--filter", f"publish={port}"]).stdout
    return bool(out.strip())


def host_has_tailscale(run: Run = default_run) -> bool:
    """True when the host already runs Tailscale: a second tailscaled on the host network would clash."""
    return run(["docker", "run", "--rm", "--network", "host", HELPER,
                "test", "-e", "/sys/class/net/tailscale0"], timeout=HELPER_TIMEOUT).returncode == 0


def has_device(path: str, run: Run = default_run) -> bool:
    return run(["docker", "run", "--rm", "--device", path, HELPER, "true"], timeout=HELPER_TIMEOUT).returncode == 0


def self_container_id() -> str:
    return socket.gethostname()  # Docker sets the hostname to the short container id


def connect_network(container: str, run: Run = default_run) -> None:
    result = run(["docker", "network", "connect", f"{PROJECT}_default", container])
    if result.returncode != 0 and "already exists" not in (result.stderr or ""):
        raise DockerError(result.stderr.strip())


SOCKET = "/var/run/docker.sock:/var/run/docker.sock"


def own_image(run: Run = default_run) -> str:
    return _check(run, ["docker", "inspect", "-f", "{{.Config.Image}}", self_container_id()]).stdout.strip()


WORKER = "plaid-uninstall"


def run_detached(image: str, args: list, env: dict, name: str, run: Run = default_run) -> None:
    """No --rm: a worker that fails stays, so the panel can show its logs. On success the reaper deletes it."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    _check(run, ["docker", "run", "-d", "--name", name, "-v", SOCKET, *env_args, image, *args])


def container_state(name: str, run: Run = default_run) -> tuple[bool, bool, int]:
    """(exists, running, exit code)."""
    result = run(["docker", "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", name])
    if result.returncode != 0:
        return False, False, 0
    running, code = result.stdout.split()
    return True, running == "true", int(code)


def logs(name: str, run: Run = default_run) -> str:
    result = run(["docker", "logs", "--tail", "30", name])
    return ((result.stdout or "") + (result.stderr or "")).strip()


def remove(kind: str, name: str, run: Run = default_run) -> None:
    """Best effort: a missing container, volume or image is fine. ("image", "self") removes the
    installer's own image (and the folder helper) once no container runs it any more."""
    if kind == "image" and name == "self":
        image = own_image(run)
        # A container cannot delete its own image: a reaper on the small helper image waits until nothing
        # runs the installer any more, then deletes it through the Docker API (the helper has no docker CLI).
        api = "curl -s --unix-socket /var/run/docker.sock"
        # Bounded wait (5 min): offline, curl cannot be installed and the reaper must still go away.
        script = (f"apk add --no-cache curl >/dev/null 2>&1; "
                  f"for i in $(seq 150); do [ \"$({api} -G http://d/containers/json "
                  f"--data-urlencode 'filters={{\"ancestor\":[\"{image}\"]}}')\" = '[]' ] && break; sleep 2; done; "
                  f"{api} -X DELETE 'http://d/containers/{WORKER}?force=true' >/dev/null; "
                  f"{api} -X DELETE http://d/images/{image} >/dev/null")
        run(["docker", "run", "-d", "--rm", "-v", SOCKET, HELPER, "sh", "-c", script])
        return
    cmd = {"container": ["rm", "-f"], "volume": ["volume", "rm", "-f"], "network": ["network", "rm"],
           "image": ["rmi"]}[kind]
    run(["docker", *cmd, name])
