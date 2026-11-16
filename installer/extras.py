"""Optional extras for Jellyfin: a theme and community plugins the user ticks one by one. The plugins are not
official and may break when Jellyfin changes version: Jellyfin is only updated once they are removed."""
from __future__ import annotations

import json
import os

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
