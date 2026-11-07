"""What the user answered in the wizard, and the checks run on it."""
from __future__ import annotations

import json
import ntpath
import posixpath
from dataclasses import asdict, dataclass, field

LANGS = ("en", "fr")
# Second-level folders that are whole disks or other people's homes (/Volumes/Disk, /mnt/usb, /home/other).
_DISK_PARENTS = {"volumes", "mnt", "media", "home", "users"}


@dataclass
class Settings:
    data_dir: str
    username: str
    password: str
    vpn_provider: str | None
    vpn_fields: dict[str, str] = field(default_factory=dict)
    timezone: str = "UTC"
    lang: str = "en"
    puid: int = 1000
    pgid: int = 1000
    home: str = ""
    platform: str = "linux"  # linux | mac | windows | synology
    tailscale_key: str = ""  # optional: joins the machine to the user's tailnet (Linux and Synology)
    # Languages and quality (media.py). None = never chosen: installs made before these existed.
    audio_lang: str | None = None
    profiles: list[str] | None = None
    default_profile: str | None = None
    subtitle_langs: list[str] | None = None
    metadata_lang: str | None = None
    arr_ids: dict = field(default_factory=dict)  # {"radarr": {"formats": {...}, "profiles": {...}}, "sonarr": ...}
    backup_dir: str | None = None  # None: <data_dir>/backups (backup.py)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> Settings:
        return cls(**json.loads(text))


def _is_windows(path: str) -> bool:
    return len(path) >= 2 and path[1] == ":"


def _norm(path: str) -> str:
    if _is_windows(path):
        return ntpath.normpath(path).replace("\\", "/").rstrip("/").lower() or "/"
    return posixpath.normpath(path).rstrip("/").lower() or "/"


def is_absolute(path: str) -> bool:
    return path.startswith("/") or (_is_windows(path) and path[2:3] in ("\\", "/"))


def is_dangerous_path(path: str, home: str) -> bool:
    """True for anything that may hold more than the stack: a root or top-level folder (/etc, /volume2),
    a whole disk or another home (/Volumes/Disk, /mnt/usb, /home/other), or the home folder and its parents."""
    p = _norm(path)
    parts = [x for x in (p[2:] if _is_windows(path) else p).split("/") if x]
    if _is_windows(path):
        if not parts or (parts[0] == "users" and len(parts) <= 2):
            return True
    elif len(parts) <= 1 or (len(parts) == 2 and parts[0] in _DISK_PARENTS):
        return True
    if home:
        h = _norm(home)
        if p == h or h.startswith(p + "/"):
            return True
    return False


def validate(s: Settings) -> list[str]:
    from installer.vpn import PROVIDERS

    errors = []
    if not s.username.strip():
        errors.append("err_username")
    if len(s.password) < 8:
        errors.append("err_password_short")
    if s.lang not in LANGS:
        errors.append("err_lang")
    if not is_absolute(s.data_dir):
        errors.append("err_path_absolute")
    elif is_dangerous_path(s.data_dir, s.home):
        errors.append("err_dangerous_path")
    if s.vpn_provider is not None:
        provider = PROVIDERS.get(s.vpn_provider)
        if provider is None:
            errors.append("err_vpn_provider")
        elif any(not s.vpn_fields.get(f, "").strip() for f in provider.fields):
            errors.append("err_vpn_fields")
    if s.tailscale_key and not s.tailscale_key.strip().startswith("tskey-"):
        errors.append("err_tailscale_key")
    from installer import media
    return errors + media.validate(s)
