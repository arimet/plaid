"""The owner's family and friends: Jellyfin accounts that only watch. Plaid manages them with its own Jellyfin API
key, never with the owner's password, so an owner locked out by wrong attempts can still be unlocked from Plaid."""
from __future__ import annotations

import json
import os
import secrets

from installer import apps, media
from installer.i18n import t
from installer.settings import Settings

KEY_FILE = "jellyfin-key"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # no 0/O, 1/l/I
# Watch only: no dashboard, no deleting, every library, 3 screens at once, locked after 5 wrong passwords.
POLICY = {"IsAdministrator": False, "EnableAllFolders": True, "EnableContentDeletion": False,
          "EnableRemoteAccess": True, "MaxActiveSessions": 3, "LoginAttemptsBeforeLockout": 5, "IsDisabled": False}
LANG3 = {"fr": "fra", "en": "eng", "es": "spa", "de": "deu", "it": "ita"}  # Jellyfin's language codes


def make_password() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(16))


def _auth(key: str) -> dict:
    return {"Authorization": f'MediaBrowser Token="{key}"'}


def api_key(s: Settings, host: str, state_dir: str) -> str:
    """Plaid's own Jellyfin key, made once with the owner's password. PermissionError if Jellyfin refuses it."""
    path = os.path.join(state_dir, KEY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    try:
        auth, _ = apps._jellyfin_auth(host, s.username, s.password)
    except apps.HttpError as e:
        if e.status in (400, 401, 403):
            raise PermissionError("Jellyfin refused the owner's password") from None
        raise
    base = f"http://{host}"
    find = lambda: next((k["AccessToken"] for k in apps.call("GET", base + "/Auth/Keys", headers=auth)["Items"]
                         if k["AppName"] == "plaid"), None)
    key = find()
    if not key:
        apps.call("POST", base + "/Auth/Keys?app=plaid", headers=auth)
        key = find()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


def list_people(host: str, key: str) -> list[dict]:
    users = apps.call("GET", f"http://{host}/Users", headers=_auth(key))
    found = [{"id": u["Id"], "name": u["Name"], "last_seen": u.get("LastActivityDate"),
              "locked": bool(u["Policy"].get("IsDisabled"))} for u in users if not u["Policy"].get("IsAdministrator")]
    return sorted(found, key=lambda p: p["name"].casefold())


def _guard(host: str, key: str, user_id: str) -> dict:
    """The user, if Plaid may change it: never an administrator (the owner, or one made by hand)."""
    user = apps.call("GET", f"http://{host}/Users/{user_id}", headers=_auth(key))
    if user["Policy"].get("IsAdministrator"):
        raise PermissionError("administrators are left alone")
    return user


def _set_policy(host: str, key: str, user: dict, **changes) -> None:
    apps.call("POST", f"http://{host}/Users/{user['Id']}/Policy", {**user["Policy"], **changes}, _auth(key))


def _set_password(host: str, key: str, user_id: str) -> str:
    password = make_password()
    apps.call("POST", f"http://{host}/Users/Password?userId={user_id}", {"NewPw": password, "ResetPassword": False},
              _auth(key))
    return password


def add_person(s: Settings, host: str, key: str, name: str) -> str:
    """Creates the account and returns its password, which Plaid never keeps."""
    password = make_password()
    user = apps.call("POST", f"http://{host}/Users/New", {"Name": name, "Password": password}, _auth(key))
    _set_policy(host, key, user, **POLICY)
    chosen = media.effective(s)
    config = {**(user.get("Configuration") or {}), "SubtitleMode": "Smart", "PlayDefaultAudioTrack": False,
              "AudioLanguagePreference": LANG3.get(chosen.audio_lang, ""),
              "SubtitleLanguagePreference": LANG3.get((chosen.subtitle_langs or [""])[0], "")}
    apps.call("POST", f"http://{host}/Users/Configuration?userId={user['Id']}", config, _auth(key))
    return password


def new_password(host: str, key: str, user_id: str) -> str:
    """A new password, and the account unlocked: someone locked out usually forgot it."""
    user = _guard(host, key, user_id)
    password = _set_password(host, key, user_id)
    _set_policy(host, key, user, IsDisabled=False)
    return password


def unlock(host: str, key: str, user_id: str) -> None:
    _set_policy(host, key, _guard(host, key, user_id), IsDisabled=False)


def remove(host: str, key: str, user_id: str) -> None:
    _guard(host, key, user_id)
    apps.call("DELETE", f"http://{host}/Users/{user_id}", headers=_auth(key))


def _owner(host: str, key: str, username: str) -> dict | None:
    users = apps.call("GET", f"http://{host}/Users", headers=_auth(key))
    return next((u for u in users if u["Name"] == username), None)


def owner_locked(host: str, key: str, username: str) -> bool:
    user = _owner(host, key, username)
    return bool(user and user["Policy"].get("IsDisabled"))


def unlock_owner(host: str, key: str, username: str) -> None:
    user = _owner(host, key, username)
    if user:
        _set_policy(host, key, user, IsDisabled=False)


def invite_text(lang: str, s: Settings, host: str, name: str, password: str, funnel_url: str | None) -> str:
    """The message the owner sends: the account, every address that works for them, and which app to install."""
    lines = [t(lang, "invite_hello", name=name), "", t(lang, "invite_account", name=name, password=password), "",
             t(lang, "invite_home", url=f"http://{host}:8096")]
    if s.platform in ("linux", "synology"):
        if s.tailscale_key:
            lines.append(t(lang, "invite_tailscale", url="http://plaid:8096"))
    else:  # the desktop's own Tailscale app, if any: the owner writes the computer's name in
        lines.append(t(lang, "invite_tailscale_desktop", url=f"http://{t(lang, 'invite_computer')}:8096"))
    if funnel_url:
        lines.append(t(lang, "invite_internet", url=funnel_url))
    return "\n".join(lines + ["", t(lang, "invite_apps"), t(lang, "invite_locked")])


def funnel_status(out: str) -> tuple[bool, str | None]:
    """(Funnel allowed, public address) from `tailscale status --json`: the tailnet must give this machine both
    HTTPS certificates and the funnel attribute, which only its owner can switch on in the Tailscale console."""
    try:
        me = json.loads(out)["Self"]
        caps = me.get("CapMap") or {}
        url = "https://" + me["DNSName"].rstrip(".") if me.get("DNSName") else None
        return "https" in caps and "funnel" in caps and url is not None, url
    except (ValueError, KeyError, TypeError, AttributeError):
        return False, None
