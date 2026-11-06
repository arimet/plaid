"""What the user answered in the wizard, and the checks run on it."""
from __future__ import annotations

import json
import ntpath
import posixpath
from dataclasses import asdict, dataclass, field

LANGS = ("en", "fr")
# Parents that hold everyone's data: never a stack folder, never wiped.
_SHARED_ROOTS = {"/", "/users", "/home", "/volume1", "/mnt", "/srv", "/media", "/opt", "/var", "/root"}


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
    """True for a root, a shared parent, or the home folder (and its parents)."""
    p = _norm(path)
    if _is_windows(path):
        drive_rest = p[2:]
        if drive_rest in ("", "/", "/users"):
            return True
    elif p in _SHARED_ROOTS:
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
    return errors
