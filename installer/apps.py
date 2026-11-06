"""Configures and links the seven apps through their own APIs. Safe to re-run: what exists is left alone.
Ported from arr-stack/scripts/configure.py."""
from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from installer.docker import DockerError
from installer.settings import Settings

Log = Callable[[str], None]
ComposeOut = Callable[..., str]  # runs `docker compose <args>` for the stack, returns stdout

MOVIES, TV = "/data/media/movies", "/data/media/tv"
LIBRARY_NAMES = {"fr": ("Films", "Séries"), "en": ("Movies", "Shows")}
JELLYFIN_LOCALE = {"fr": ("fr-FR", "FR", "fr"), "en": ("en-US", "US", "en")}
# Events that change files, so Jellyfin refreshes its library without a manual scan.
LIBRARY_EVENTS = ["onDownload", "onUpgrade", "onRename", "onMovieDelete", "onMovieFileDelete",
                  "onMovieFileDeleteForUpgrade", "onSeriesDelete", "onEpisodeFileDelete",
                  "onEpisodeFileDeleteForUpgrade", "onImportComplete"]
DEFAULT_PROFILE = "HD-1080p"
WAIT = 300
TIMEOUT = 120  # one API call; Prowlarr tests an indexer against the internet before saving it

opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


class HttpError(Exception):
    def __init__(self, status, url, body):
        super().__init__(f"HTTP {status} on {url}: {body[:300]}")
        self.status = status


def call(method, url, body=None, headers=None, form=False):
    headers = dict(headers or {})
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode() or "null"
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, url, e.read().decode(errors="replace")) from None
    except OSError as e:  # timeouts and dropped connections: say which call, for the error detail
        raise type(e)(f"{method} {url}: {e}") from None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def wait_for(url, ok_status=False, seconds=WAIT):
    """Waits until url answers 2xx (or any HTTP status when ok_status)."""
    deadline = time.time() + seconds
    while True:
        try:
            opener.open(url, timeout=5).close()
            return
        except urllib.error.HTTPError:
            if ok_status:
                return
        except OSError:
            pass
        if time.time() > deadline:
            raise TimeoutError(f"{url} does not answer")
        time.sleep(3)


def read_when(compose_out: ComposeOut, svc: str, path: str, pattern: str, seconds=WAIT) -> str:
    """Reads a file inside a container until pattern matches; returns group 1."""
    deadline = time.time() + seconds
    while True:
        try:
            m = re.search(pattern, compose_out("exec", "-T", svc, "cat", path), re.M)
            if m:
                return m.group(1)
        except DockerError:
            pass
        if time.time() > deadline:
            raise TimeoutError(f"{svc}: {path} not ready")
        time.sleep(3)


# ---------------------------------------------------------------- qBittorrent

def configure_qbittorrent(s: Settings, host: str, compose_out: ComposeOut, log: Log) -> str:
    """Returns qBittorrent's API key: Radarr 6 fails the password login against qBittorrent 5.3
    (session cookie renamed QBT_SID_<port>), the key works."""
    base = f"http://{host}/api/v2"
    wait_for(base + "/app/version", ok_status=True)
    # qBittorrent rejects requests whose Host header does not name its own port (8080).
    headers = {"Host": host}

    def login(name, pw):
        try:  # success: 204 (v5) or "Ok." (v4); wrong password: 401 (v5) or "Fails." (v4)
            return call("POST", base + "/auth/login", {"username": name, "password": pw}, headers, form=True) != "Fails."
        except HttpError:
            return False

    # Few attempts on purpose: qBittorrent bans the address after 5 failed logins.
    if not login(s.username, s.password):
        deadline = time.time() + WAIT
        temp = []
        while not temp and time.time() < deadline:
            temp = re.findall(r"session: (\S+)", compose_out("logs", "--no-log-prefix", "qbittorrent"))
            time.sleep(0 if temp else 3)
        if not (temp and login("admin", temp[-1])):
            raise RuntimeError("qBittorrent refused the login")
    prefs = {"web_ui_username": s.username, "web_ui_password": s.password, "bypass_local_auth": True,
             "save_path": "/data/torrents", "auto_tmm_enabled": True}
    call("POST", base + "/app/setPreferences", {"json": json.dumps(prefs)}, headers, form=True)
    key = call("GET", base + "/app/preferences", headers=headers).get("web_ui_api_key")
    if not key:
        key = call("POST", base + "/app/rotateAPIKey", headers=headers)["apiKey"]
    log("qbittorrent")
    return key


# ---------------------------------------------------------------- Jellyfin

def configure_jellyfin(s: Settings, host: str, has_dri: bool, log: Log) -> tuple[str, list[str]]:
    """Returns (API key for Radarr/Sonarr, library names)."""
    base = f"http://{host}"
    wait_for(base + "/System/Info/Public")
    deadline = time.time() + WAIT
    while True:  # still starting: the connection may drop and the field appears once the server is ready
        try:
            info = call("GET", base + "/System/Info/Public") or {}
            if "StartupWizardCompleted" in info:
                break
        except (OSError, HttpError):
            pass
        if time.time() > deadline:
            raise TimeoutError("Jellyfin is not ready")
        time.sleep(3)
    client = 'MediaBrowser Client="Plaid", Device="Plaid", DeviceId="plaid", Version="1.0"'
    culture, country, lang = JELLYFIN_LOCALE[s.lang]
    if not info["StartupWizardCompleted"]:
        call("POST", base + "/Startup/Configuration",
             {"UICulture": culture, "MetadataCountryCode": country, "PreferredMetadataLanguage": lang})
        call("GET", base + "/Startup/User")
        call("POST", base + "/Startup/User", {"Name": s.username, "Password": s.password})
        call("POST", base + "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        call("POST", base + "/Startup/Complete")
    token = call("POST", base + "/Users/AuthenticateByName", {"Username": s.username, "Pw": s.password},
                 {"Authorization": client})["AccessToken"]
    auth = {"Authorization": f'{client}, Token="{token}"'}

    existing = {lib["Name"] for lib in call("GET", base + "/Library/VirtualFolders", headers=auth)}
    names = LIBRARY_NAMES[s.lang]
    for name, kind, path in ((names[0], "movies", MOVIES), (names[1], "tvshows", TV)):
        if name not in existing:
            query = urllib.parse.urlencode({"name": name, "collectionType": kind, "refreshLibrary": "false"})
            options = {"PathInfos": [{"Path": path}], "PreferredMetadataLanguage": lang, "MetadataCountryCode": country}
            call("POST", f"{base}/Library/VirtualFolders?{query}", {"LibraryOptions": options}, auth)

    if has_dri:
        encoding = call("GET", base + "/System/Configuration/encoding", headers=auth)
        encoding.update(HardwareAccelerationType="vaapi", VaapiDevice="/dev/dri/renderD128")
        call("POST", base + "/System/Configuration/encoding", encoding, auth)

    def arr_key():
        return next((k["AccessToken"] for k in call("GET", base + "/Auth/Keys", headers=auth)["Items"]
                     if k["AppName"] == "arr"), None)
    if not arr_key():
        call("POST", base + "/Auth/Keys?app=arr", headers=auth)
    log("jellyfin")
    return arr_key(), list(names)


# ---------------------------------------------------------------- Radarr, Sonarr, Prowlarr

class Arr:
    def __init__(self, name: str, host: str, version: str, compose_out: ComposeOut):
        self.name = name
        self.base = f"http://{host}/api/{version}"
        self.key = read_when(compose_out, name, "/config/config.xml", r"<ApiKey>([^<]+)</ApiKey>")
        wait_for(f"{self.base}/system/status?apikey={self.key}")

    def __call__(self, method, path, body=None):
        return call(method, self.base + path, body, {"X-Api-Key": self.key})

    def login(self, user, password):
        host = self("GET", "/config/host")
        host.update(authenticationMethod="forms", authenticationRequired="enabled",
                    username=user, password=password, passwordConfirmation=password)
        self("PUT", f"/config/host/{host['id']}", host)

    def add(self, kind, implementation, name, fields, flags=()):
        """Creates a download client, notification or application from the app's own template."""
        if any(item["name"] == name for item in self("GET", f"/{kind}")):
            return
        item = next(t for t in self("GET", f"/{kind}/schema") if t["implementation"] == implementation)
        item.pop("presets", None)
        item.update(name=name, enable=True)
        for field in item["fields"]:
            if field["name"] in fields:
                field["value"] = fields[field["name"]]
        for flag in flags:
            if flag in item:
                item[flag] = True
        self("POST", f"/{kind}", item)

    def root_folder(self, path):
        if path not in {f["path"].rstrip("/") for f in self("GET", "/rootfolder")}:
            self("POST", "/rootfolder", {"path": path})

    def profile(self):
        profiles = self("GET", "/qualityprofile")
        return next((p for p in profiles if p["name"] == DEFAULT_PROFILE), profiles[0])


def configure_arr(app: Arr, s: Settings, root: str, category_field: str, category: str,
                  qbit_host: str, qbit_key: str, jellyfin_key: str, log: Log) -> None:
    app.login(s.username, s.password)
    app.root_folder(root)
    app.add("downloadclient", "QBittorrent", "qBittorrent",
            {"host": qbit_host, "port": 8080, "apiKey": qbit_key,
             category_field: category})
    app.add("notification", "MediaBrowser", "Jellyfin",
            {"host": "jellyfin", "port": 8096, "apiKey": jellyfin_key, "updateLibrary": True}, LIBRARY_EVENTS)
    log(app.name)


def _public(item: dict) -> bool:
    # Public torrent trackers need no account: they can be added with their defaults. Private ones go
    # through Prowlarr, where the user fills in their credentials.
    return item.get("privacy") == "public" and item.get("protocol") == "torrent"


def list_public_indexers(prowlarr: Arr) -> list[dict]:
    """Prowlarr's whole public catalogue, alphabetical, none put forward."""
    added = {i.get("definitionName") for i in prowlarr("GET", "/indexer")}
    found = [{"id": t["definitionName"], "name": t["name"], "language": t.get("language") or "",
              "description": t.get("description") or "", "added": t["definitionName"] in added}
             for t in prowlarr("GET", "/indexer/schema") if _public(t)]
    return sorted(found, key=lambda t: t["name"].lower())


def add_indexer(prowlarr: Arr, definition: str) -> None:
    """Adds a public tracker; Prowlarr then hands it to Radarr and Sonarr. KeyError if unknown or private."""
    if any(i.get("definitionName") == definition for i in prowlarr("GET", "/indexer")):
        return
    item = next((t for t in prowlarr("GET", "/indexer/schema")
                 if t.get("definitionName") == definition and _public(t)), None)
    if item is None:
        raise KeyError(definition)
    # Saving an enabled indexer runs a live search that some sites take minutes to answer (archive.org on
    # Docker Desktop, even with forceSave). A disabled one saves at once; the bulk edit enables it untested.
    item.pop("presets", None)
    item.update(enable=False, appProfileId=1)
    created = prowlarr("POST", "/indexer", item)
    prowlarr("PUT", "/indexer/bulk", {"ids": [created["id"]], "enable": True})


def add_default_indexer(prowlarr: Arr) -> None:
    """Internet Archive: legal (public-domain films), so the stack works on day one. A plain Prowlarr
    indexer the user can disable or delete."""
    add_indexer(prowlarr, "internetarchive")


def configure_prowlarr(prowlarr: Arr, s: Settings, radarr: Arr, sonarr: Arr, log: Log) -> None:
    prowlarr.login(s.username, s.password)
    for app, port in ((radarr, 7878), (sonarr, 8989)):
        name = app.name.capitalize()
        prowlarr.add("applications", name, name,
                     {"prowlarrUrl": "http://prowlarr:9696", "baseUrl": f"http://{app.name}:{port}", "apiKey": app.key})
    # After the apps: Prowlarr hands an indexer to Radarr and Sonarr when it is enabled, not when an app is added.
    try:
        add_default_indexer(prowlarr)
    except (HttpError, OSError, StopIteration):
        pass  # a convenience: the stack works without it, and the done page explains how to add trackers
    log("prowlarr")


# ---------------------------------------------------------------- Bazarr

def configure_bazarr(s: Settings, host: str, radarr: Arr, sonarr: Arr, compose_out: ComposeOut, log: Log) -> str:
    key = read_when(compose_out, "bazarr", "/config/config/config.yaml", r"^\s+apikey: '?([0-9a-f]{8,})")
    base = f"http://{host}/api"
    wait_for(f"{base}/system/status?apikey={key}")
    langs = [s.lang] + ([] if s.lang == "en" else ["en"])
    # No cutoff: Bazarr fetches every language instead of stopping at the first one found.
    profile = {"profileId": 1, "name": " + ".join(langs), "cutoff": None, "mustContain": [], "mustNotContain": [],
               "originalFormat": False, "tag": None,
               "items": [{"id": i, "language": code, "audio_exclude": "False", "audio_only_include": "False",
                          "hi": "False", "forced": "False"} for i, code in enumerate(langs, 1)]}
    settings = {
        "settings-auth-type": "form", "settings-auth-username": s.username, "settings-auth-password": s.password,
        "settings-general-use_sonarr": "true", "settings-sonarr-ip": "sonarr", "settings-sonarr-port": "8989",
        "settings-sonarr-apikey": sonarr.key,
        "settings-general-use_radarr": "true", "settings-radarr-ip": "radarr", "settings-radarr-port": "7878",
        "settings-radarr-apikey": radarr.key,
        "languages-enabled": langs, "languages-profiles": json.dumps([profile]),
        "settings-general-serie_default_enabled": "true", "settings-general-serie_default_profile": "1",
        "settings-general-movie_default_enabled": "true", "settings-general-movie_default_profile": "1",
        "settings-general-enabled_providers": ["podnapisi", "addic7ed"],
        # Align every downloaded subtitle on the audio track (ffsubsync), whatever its score.
        "settings-subsync-use_subsync": "true",
        "settings-subsync-use_subsync_threshold": "false",
        "settings-subsync-use_subsync_movie_threshold": "false",
    }
    call("POST", base + "/system/settings", settings, {"X-API-KEY": key}, form=True)
    log("bazarr")
    return key


# ---------------------------------------------------------------- Seerr

def configure_seerr(s: Settings, host: str, radarr: Arr, sonarr: Arr, log: Log) -> dict[str, int]:
    """API calls found in the plan's spike 0b. Returns the number of Radarr/Sonarr servers declared."""
    base = f"http://{host}/api/v1"
    wait_for(base + "/status")
    public = call("GET", base + "/settings/public")
    login = {"username": s.username, "password": s.password}
    if public.get("mediaServerType") != 2:  # first run: also declares the Jellyfin server
        login.update(hostname="jellyfin", port=8096, useSsl=False, urlBase="", serverType=2,
                     email=f"{s.username}@localhost")
    call("POST", base + "/auth/jellyfin", login)

    call("POST", base + "/settings/jellyfin/library/sync")
    for lib in call("GET", base + "/settings/jellyfin/library"):
        if not lib.get("enabled"):
            call("PUT", f"{base}/settings/jellyfin/library/{lib['id']}", {"enabled": True})

    counts = {}
    for app, port, root in ((radarr, 7878, MOVIES), (sonarr, 8989, TV)):
        servers = call("GET", f"{base}/settings/{app.name}")
        if not servers:
            profile = app.profile()
            server = {"name": app.name.capitalize(), "hostname": app.name, "port": port, "apiKey": app.key,
                      "useSsl": False, "baseUrl": "", "activeProfileId": profile["id"],
                      "activeProfileName": profile["name"], "activeDirectory": root, "is4k": False,
                      "isDefault": True, "externalUrl": "", "syncEnabled": False, "preventSearch": False, "tags": []}
            if app.name == "radarr":
                server["minimumAvailability"] = "released"
            else:
                server.update(activeAnimeProfileId=profile["id"], activeAnimeProfileName=profile["name"],
                              activeAnimeDirectory=root, enableSeasonFolders=True, animeTags=[])
            call("POST", f"{base}/settings/{app.name}", server)
            servers = [server]
        counts[app.name] = len(servers)
    if not public.get("initialized"):
        call("POST", base + "/settings/initialize")
    log("seerr")
    return counts


# ---------------------------------------------------------------- all

def configure_all(s: Settings, host_of: Callable[[str], str], log: Log, compose_out: ComposeOut,
                  has_dri: bool) -> dict:
    """host_of("radarr") -> "radarr:7878" as seen from the caller. Returns API keys and what was set up."""
    qbit_host = "gluetun" if s.vpn_provider else "qbittorrent"
    qbit_key = configure_qbittorrent(s, host_of("qbittorrent"), compose_out, log)
    jellyfin_key, libraries = configure_jellyfin(s, host_of("jellyfin"), has_dri, log)
    radarr = Arr("radarr", host_of("radarr"), "v3", compose_out)
    sonarr = Arr("sonarr", host_of("sonarr"), "v3", compose_out)
    configure_arr(radarr, s, MOVIES, "movieCategory", "movies", qbit_host, qbit_key, jellyfin_key, log)
    configure_arr(sonarr, s, TV, "tvCategory", "tv", qbit_host, qbit_key, jellyfin_key, log)
    prowlarr = Arr("prowlarr", host_of("prowlarr"), "v1", compose_out)
    configure_prowlarr(prowlarr, s, radarr, sonarr, log)
    bazarr_key = configure_bazarr(s, host_of("bazarr"), radarr, sonarr, compose_out, log)
    servers = configure_seerr(s, host_of("seerr"), radarr, sonarr, log)
    return {"radarr": radarr.key, "sonarr": sonarr.key, "prowlarr": prowlarr.key, "bazarr": bazarr_key,
            "jellyfin": jellyfin_key, "jellyfin_libraries": libraries, "seerr_servers": servers}
