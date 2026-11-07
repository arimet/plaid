"""Docker CLI calls. Host folders are reached through throwaway containers, since the
installer itself runs in a container and only sees the host through the Docker socket."""
from __future__ import annotations

import json
import ntpath
import posixpath
import re
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


def ensure_data_dirs(src: str, puid: int, pgid: int, run: Run = default_run, create: bool = True) -> None:
    """The stack's folders and the guard's marker. `create=False` fails on a missing folder instead of making
    one (Docker Desktop, automounted disks); an empty fstab mount point exists, so the settings check warns."""
    dirs = " ".join(f"/m/{d}" for d in ("torrents/movies", "torrents/tv", "media/movies", "media/tv"))
    owned = "/m /m/torrents /m/media " + dirs
    mount = ["-v", f"{src}:/m"] if create else ["--mount", f"type=bind,src={src},dst=/m"]
    _check(run, ["docker", "run", "--rm", *mount, HELPER, "sh", "-c",
                 f"mkdir -p {dirs} && chown {puid}:{pgid} {owned} && touch /m/{MARKER}"], timeout=HELPER_TIMEOUT)


# The guard's proof that the media folder is the real disk, not the empty mount point under it.
MARKER = ".plaid"
_MARKER_SCRIPT = (f'if [ -e /m/{MARKER} ]; then echo ok; elif [ -n "$(ls -A /m)" ]; then echo unconfirmed; '
                  'else echo missing; fi')


def _probe(src: str, script: str, run: Run) -> str:
    """What `script` prints about the folder mounted read-only at /m; "missing" when the folder does not exist,
    "unknown" when the probe failed."""
    name = f"plaid-probe-{uuid.uuid4().hex[:8]}"
    result = run(["docker", "run", "--rm", "--name", name, "--mount", f"type=bind,src={src},dst=/m,readonly",
                  HELPER, "sh", "-c", script], timeout=PROBE_TIMEOUT)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 124:  # timed out: the probe may sit in "Created" forever
        run(["docker", "rm", "-f", name])
        return "unknown"
    return "missing" if "does not exist" in (result.stderr or "") else "unknown"


def marker_state(src: str, run: Run = default_run) -> str:
    """"ok", "missing" (folder gone or empty), "unconfirmed" (files but no marker) or "unknown" (probe failed)."""
    return _probe(src, _MARKER_SCRIPT, run)


def folder_state(src: str, run: Run = default_run) -> str:
    """For a new media folder: "library" (media/ and torrents/), "empty", "other", "missing" or "unknown"."""
    return _probe(src, 'if [ -d /m/media ] && [ -d /m/torrents ]; then echo library; '
                       'elif [ -z "$(ls -A /m)" ]; then echo empty; else echo other; fi', run)


def write_marker(src: str, run: Run = default_run) -> None:
    # --mount, not -v: if the disk went away meanwhile, fail instead of creating an empty folder to mark.
    _check(run, ["docker", "run", "--rm", "--mount", f"type=bind,src={src},dst=/m", HELPER, "touch", f"/m/{MARKER}"],
           timeout=HELPER_TIMEOUT)


# -- disks and folders, for the folder picker and the checks before installing

DESKTOP = ("mac", "windows")  # Docker Desktop: the host's disks come through its file shares
DISK_TYPES = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "ntfs", "ntfs3", "fuseblk", "exfat", "vfat",
              "nfs", "nfs4", "cifs", "smb3"}
NOT_DISKS = ("/boot", "/var/lib/docker", "/snap", "/run", "/dev", "/proc", "/sys")
_DF = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+\d+%\s")  # 1024-blocks, used, available, capacity
_DF_TYPED = re.compile(r"^(.*?)\s+(\S+)\s+(\d+)\s+\d+\s+(\d+)\s+\d+%\s+(.*)$")  # fs, type, blocks, avail, mount


def _space(df_line: str) -> tuple[int, int] | None:
    """(free, total) in bytes from a `df -kP` line."""
    m = _DF.search(df_line + " ")
    return (int(m.group(3)) * 1024, int(m.group(1)) * 1024) if m else None


def _under(path: str, roots) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def _helper(mounts: list[str], cmd: list[str], run: Run) -> subprocess.CompletedProcess:
    """A short throwaway container; one that hangs is cut at PROBE_TIMEOUT and removed."""
    name = f"plaid-probe-{uuid.uuid4().hex[:8]}"
    args = ["docker", "run", "--rm", "--name", name]
    for m in mounts:
        args += ["-v", m] if m.endswith(":ro") else ["--mount", m]
    result = run([*args, HELPER, *cmd], timeout=PROBE_TIMEOUT)
    if result.returncode == 124:
        run(["docker", "rm", "-f", name])
    return result


def parse_df(out: str) -> list[dict]:
    """The host's real disks from `df -kPT` run with the host's root at /host."""
    best: dict[str, dict] = {}
    for line in out.splitlines()[1:]:
        m = _DF_TYPED.match(line.strip())
        if not m or m.group(2) not in DISK_TYPES or not _under(m.group(5), ["/host"]):
            continue
        path = m.group(5)[len("/host"):] or "/"
        if path != "/" and _under(path, NOT_DISKS):
            continue
        disk = {"path": path, "name": "" if path == "/" else posixpath.basename(path),
                "free": int(m.group(4)) * 1024, "total": int(m.group(3)) * 1024, "system": path == "/"}
        if m.group(1) not in best or len(path) < len(best[m.group(1)]["path"]):  # one disk bound twice
            best[m.group(1)] = disk
    return sorted(best.values(), key=lambda d: (not d["system"], d["path"]))


def disks(platform: str, home: str, run: Run = default_run) -> list[dict]:
    """{path, name, free, total, system} for each disk. On Docker Desktop the system disk is the home folder, read
    only for its free space: listing the Mac home through a container hung Docker Desktop."""
    if platform not in DESKTOP:
        result = _helper(["/:/host:ro"], ["df", "-kPT"], run)
        return parse_df(result.stdout) if result.returncode == 0 else []
    if platform == "mac":
        mounts = ["type=bind,src=/Users,dst=/u,readonly", "type=bind,src=/Volumes,dst=/h,readonly"]
        script = ('printf "\\t"; df -kP /u | tail -1; for d in /h/*; do [ -L "$d" ] || [ ! -d "$d" ] && continue; '
                  'printf "%s\\t" "${d#/h/}"; df -kP "$d" | tail -1; done')
    else:
        mounts = ["type=bind,src=/run/desktop/mnt/host,dst=/h,readonly"]
        script = 'for d in /h/*; do printf "%s\\t" "${d#/h/}"; df -kP "$d" | tail -1; done'
    result = _helper(mounts, ["sh", "-c", script], run)
    found = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        name, _, df_line = line.partition("\t")
        space = _space(df_line)
        if platform == "mac":
            if not space or name.startswith((".", "com.apple")):
                continue
            system, path = name == "", f"/Volumes/{name}" if name else home
        else:
            if not space or len(name) != 1 or not name.isalpha():
                continue
            system = name.lower() == "c"
            path, name = (home, "") if system else (f"{name.upper()}:\\", f"{name.upper()}:")
        found.append({"path": path, "name": name, "free": space[0], "total": space[1], "system": system})
    return sorted(found, key=lambda d: not d["system"])


def browsable(path: str, platform: str) -> bool:
    """Folders the picker may open. Never the system disk on Docker Desktop, never the kernel's own folders."""
    if "," in path:  # breaks --mount; ponytail: quote the CSV field if someone has one
        return False
    if platform == "windows":
        norm = ntpath.normpath(path)
        return bool(re.match(r"^[A-Za-z]:\\", norm)) and norm[0].lower() != "c"
    if not path.startswith("/"):
        return False
    norm = posixpath.normpath(path)
    if platform == "mac":
        parts = norm.split("/")
        return len(parts) >= 3 and parts[1] == "Volumes" and parts[2] != "Macintosh HD"
    return not _under(norm, ["/proc", "/sys", "/dev", "/run"])


_LIST = 'df -kP . | tail -1; for d in */; do [ -d "$d" ] && printf "%s\\n" "${d%/}"; done'


def browse(path: str, platform: str, run: Run = default_run) -> dict:
    """{path, parent, dirs, free, total} of a folder, or {error}: hidden folders and folders with a comma left out."""
    if not browsable(path, platform):
        return {"error": "err_folder_unreadable"}
    if platform == "mac":
        norm = posixpath.normpath(path)
        rel = norm[len("/Volumes/"):]
        # Through /Volumes, so a volume that is a link (to the Mac's own disk) is seen as one and refused.
        result = _helper(["type=bind,src=/Volumes,dst=/v,readonly"],
                         ["sh", "-c", f'[ -L "/v/${{1%%/*}}" ] && exit 3; cd "/v/$1" || exit 2; {_LIST}', "sh", rel],
                         run)
        parent = None if "/" not in rel else posixpath.dirname(norm)
    else:
        norm = ntpath.normpath(path) if platform == "windows" else posixpath.normpath(path)
        result = _helper([f"type=bind,src={host_path(norm, platform)},dst=/m,readonly"],
                         ["sh", "-c", f"cd /m || exit 2; {_LIST}"], run)
        up = (ntpath if platform == "windows" else posixpath).dirname(norm)
        parent = None if up == norm else up
    if result.returncode != 0:
        missing = result.returncode == 2 or "does not exist" in (result.stderr or "")
        return {"error": "err_folder_missing" if missing else "err_folder_unreadable"}
    lines = result.stdout.splitlines()
    space = _space(lines[0]) if lines else None
    free, total = space or (None, None)
    return {"path": norm, "parent": parent, "dirs": [d for d in lines[1:] if d and "," not in d],
            "free": free, "total": total}


_LINK = ('df -kP /m | tail -1; f=/m/.plaid-link-$$; if touch "$f" 2>/dev/null; then ln "$f" "$f.2" 2>/dev/null; '
         'stat -c %h "$f"; rm -f "$f" "$f.2"; else echo -; fi')


def disk_check(path: str, platform: str, home: str, run: Run = default_run) -> dict:
    """{free, hardlinks} of the disk a media folder is on, from the folder or, not made yet, its parent. A hard link
    is tried and removed: False means every finished download gets copied. None when it cannot be told."""
    unknown = {"free": None, "hardlinks": None}
    mod = ntpath if platform == "windows" else posixpath
    for target in (path, mod.dirname(mod.normpath(path))):
        if platform in DESKTOP and mod.normpath(target).lower() == mod.normpath(home).lower():
            share = "/Users" if platform == "mac" else host_path(target[:3], platform)
            result = _helper([f"type=bind,src={share},dst=/m,readonly"], ["sh", "-c", "df -kP /m | tail -1"], run)
            space = _space(result.stdout) if result.returncode == 0 else None
            return {"free": space[0], "hardlinks": None} if space else unknown
        result = _helper([f"type=bind,src={host_path(target, platform)},dst=/m"], ["sh", "-c", _LINK], run)
        if result.returncode == 0:
            lines = result.stdout.splitlines() + ["", ""]
            space = _space(lines[0])
            links = lines[1].strip()
            return {"free": space[0] if space else None, "hardlinks": None if links in ("-", "") else links == "2"}
        if "does not exist" not in (result.stderr or ""):
            return unknown
    return unknown


def stack_containers(run: Run = default_run) -> dict[str, dict]:
    """{compose service: {id, running, started, health, network}} for every container of the stack."""
    ids = _check(run, ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"]).stdout.split()
    if not ids:
        return {}
    found = {}
    for c in json.loads(_check(run, ["docker", "inspect", *ids]).stdout):
        state, labels = c.get("State") or {}, c["Config"]["Labels"]
        service = labels["com.docker.compose.service"]
        # Skip `compose run` containers, and a stopped leftover when compose recreated the service.
        if labels.get("com.docker.compose.oneoff") == "True" or (service in found and found[service]["running"]):
            continue
        found[service] = {
            "id": c["Id"], "running": bool(state.get("Running")), "started": state.get("StartedAt", ""),
            "health": (state.get("Health") or {}).get("Status", ""), "network": c["HostConfig"]["NetworkMode"]}
    return found


def sees_marker(state_dir: str, svc: str, run: Run = default_run) -> bool:
    """False when a running app lost the disk: a container keeps its old view of a mount that came back."""
    result = run(["docker", "compose", "-p", PROJECT, "-f", f"{state_dir}/compose.json", "exec", "-T", svc,
                  "test", "-e", f"/data/{MARKER}"], timeout=PROBE_TIMEOUT)
    # Only `test` saying "absent" counts: an app restarting or a slow NAS timing out would restart it every round.
    return not (result.returncode == 1 and not (result.stderr or "").strip())


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


def compose(state_dir: str, *args: str, run: Run = default_run, timeout: float | None = None) -> subprocess.CompletedProcess:
    return _check(run, ["docker", "compose", "-p", PROJECT, "-f", f"{state_dir}/compose.json", *args], timeout=timeout)


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


APPLY_WORKER = "plaid-apply"


def run_detached(image: str, args: list, env: dict, name: str, run: Run = default_run,
                 volumes: tuple[str, ...] = ()) -> None:
    """No --rm: a worker that fails stays, so the panel can show its logs. On success the reaper deletes it."""
    env_args = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    vol_args = [a for v in volumes for a in ("-v", v)]
    _check(run, ["docker", "run", "-d", "--name", name, "-v", SOCKET, *vol_args, *env_args, image, *args])


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
