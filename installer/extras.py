"""Optional extras for Jellyfin: a theme and community plugins the user ticks one by one. The plugins are not
official and may break when Jellyfin changes version: Jellyfin is only updated once they are removed."""
from __future__ import annotations

import json
import os
import urllib.parse

STATE_FILE = "extras.json"
THEME_URL = "https://cdn.jsdelivr.net/gh/mihaif7/elegantfin-jf12@main/Theme/ElegantFin-jf12-modern-latest.css"
THEME_LINE = f'@import url("{THEME_URL}");'
PARADOX = "https://www.iamparadox.dev/jellyfin/plugins/manifest.json"
REPOS = {
    PARADOX: "IAmParadox27",
    "https://raw.githubusercontent.com/n00bcodr/jellyfin-plugins/main/manifest.json": "n00bcodr",
    "https://raw.githubusercontent.com/TheIntroDB/jellyfin-plugin/main/manifest.json": "TheIntroDB",
    "https://raw.githubusercontent.com/Namo2/InPlayerEpisodePreview/master/manifest.json": "Namo2",
    "https://raw.githubusercontent.com/angeluriot/jellyfin-plugin-better-home-sections/main/manifest.json":
        "Better Home Sections",
    "https://raw.githubusercontent.com/angeluriot/jellyfin-plugin-genre-mapper/main/manifest.json": "Genre Mapper",
}
_REPO = {name: url for url, name in REPOS.items()}
FT = ("File Transformation", PARADOX)
# id, then the packages it needs as (name, repository URL), its own first.
CATALOG = [
    {"id": "theme", "packages": []},
    {"id": "enhanced", "packages": [("Jellyfin Enhanced", _REPO["n00bcodr"]), ("Custom Tabs", PARADOX),
                                    ("Plugin Pages", PARADOX), FT]},
    {"id": "mediabar", "packages": [("Media Bar", PARADOX), FT]},
    {"id": "introdb", "packages": [("TheIntroDB", _REPO["TheIntroDB"]), FT]},
    {"id": "episodes", "packages": [("InPlayerEpisodePreview", _REPO["Namo2"]), FT]},
    {"id": "homesections", "packages": [("Better Home Sections", _REPO["Better Home Sections"]), FT]},
    {"id": "genremapper", "packages": [("Genre Mapper", _REPO["Genre Mapper"])]},
]
BY_ID = {x["id"]: x for x in CATALOG}


def packages_for(chosen: list[str]) -> set[str]:
    return {name for x in chosen if x in BY_ID for name, _ in BY_ID[x]["packages"]}


def repos_for(chosen: list[str]) -> set[str]:
    return {url for x in chosen if x in BY_ID for _, url in BY_ID[x]["packages"]}


def has_plugins(chosen: list[str]) -> bool:
    return bool(packages_for(chosen))


def with_theme(css: str, on: bool) -> str:
    """The user's own CSS kept as it is; Plaid's one line first (an @import must come before any rule)."""
    rest = "\n".join(line for line in (css or "").split("\n") if line.strip() != THEME_LINE).strip("\n")
    return f"{THEME_LINE}\n{rest}".rstrip("\n") if on else rest


def load_state(state_dir: str) -> dict:
    state = {"chosen": [], "installed": [], "removed_for_update": []}
    try:
        with open(os.path.join(state_dir, STATE_FILE), encoding="utf-8") as f:
            saved = json.load(f)
        state.update({k: list(saved.get(k) or []) for k in state})
    except (OSError, ValueError, AttributeError):
        pass
    return state


def save_state(state: dict, state_dir: str) -> None:
    from installer.flow import write_json  # flow imports this module's users: no import at load time
    write_json(os.path.join(state_dir, STATE_FILE), state)


WAIT = 300  # seconds for Jellyfin to come back with its new plugins


def _jellyfin(s, state_dir: str, host: str):
    """A call to Jellyfin with Plaid's own key."""
    from installer import apps, people
    key = people.api_key(s, host, state_dir)
    return lambda method, path, body=None: apps.call(method, f"http://{host}{path}", body, people._auth(key))


def apply(s, chosen: list[str], report, state_dir: str, host: str = "jellyfin:8096", wait: int = WAIT) -> list[str]:
    """Installs what is ticked and removes what was unticked. Returns the ticked extras that could not be installed;
    the others are done. Plaid only ever removes the packages it installed itself."""
    import time
    from installer import apps
    from installer.flow import Progress
    jf = _jellyfin(s, state_dir, host)
    state = load_state(state_dir)
    chosen = [x for x in chosen if x in BY_ID]
    report(Progress("step_extras"))
    present = {p["Name"]: p for p in jf("GET", "/Plugins")}
    wanted, mine = packages_for(chosen), set(state["installed"])
    repos = jf("GET", "/Repositories")
    missing = [{"Name": REPOS[u], "Url": u, "Enabled": True} for u in sorted(repos_for(chosen))
               if u not in {r.get("Url") for r in repos}]
    if missing:
        jf("POST", "/Repositories", repos + missing)
    tried, refused = [], set()
    for name in sorted(wanted - set(present)):
        try:
            jf("POST", "/Packages/Installed/" + urllib.parse.quote(name))
            tried.append(name)
        except apps.HttpError:  # not in its repository, or no version for this Jellyfin
            refused.add(name)
    gone = []
    for name in sorted((mine - wanted) & set(present)):
        jf("DELETE", f"/Plugins/{present[name]['Id']}/{present[name]['Version']}")
        gone.append(name)
    active = {n for n, p in present.items() if p.get("Status") == "Active"}
    if tried or gone:
        report(Progress("step_extras_restart"))
        jf("POST", "/System/Restart")
        deadline = time.time() + wait
        time.sleep(10)  # it still answers for a moment before it stops
        while True:
            try:
                active = {p["Name"] for p in jf("GET", "/Plugins") if p.get("Status") == "Active"}
                if set(tried) <= active or time.time() >= deadline:
                    break
            except (OSError, apps.HttpError):
                if time.time() >= deadline:
                    raise RuntimeError("Jellyfin did not come back after its restart") from None
            time.sleep(3)
    css = jf("GET", "/System/Configuration/branding")
    themed = with_theme(css.get("CustomCss") or "", "theme" in chosen)
    if themed != (css.get("CustomCss") or ""):
        jf("POST", "/System/Configuration/branding", {**css, "CustomCss": themed})
    failed = [x for x in chosen if not {n for n, _ in BY_ID[x]["packages"]} <= active]
    state["installed"] = sorted((mine - set(gone)) | (set(tried) & active))
    state["chosen"] = [x for x in chosen if x not in failed]
    state["removed_for_update"] = [x for x in state["removed_for_update"] if x not in state["chosen"]]
    save_state(state, state_dir)
    report(Progress("step_done", done=True, detail=",".join(failed)))
    return failed
