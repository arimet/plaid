"""Languages and quality: what the user picks, its defaults, and the Radarr/Sonarr formats and profiles it
becomes. Pure data: apps.py sends it."""
from __future__ import annotations

import copy
import re
from dataclasses import replace

from installer.i18n import t

LANGS = ("fr", "en", "es", "de", "it")
ARR_LANG = {"en": 1, "fr": 2, "es": 3, "de": 4, "it": 5}  # Radarr and Sonarr share these IDs
ORIGINAL, ANY = -2, -1
COUNTRY = {"fr": "FR", "en": "US", "es": "ES", "de": "DE", "it": "IT"}
MODES = ("mine", "orig", "multi")
QUALITIES = {"720p": 720, "1080p": 1080, "4k": 2160}
KEYS = tuple(f"{mode}-{q}" for mode in MODES for q in QUALITIES)
DEFAULT_PROFILE = "mine-1080p"
# Low enough that no release is refused for its language: a profile ranks languages, it never waits for one.
MIN_SCORE = -1000
GROUP_ID = 1000
EXCLUDED = re.compile(r"Remux|BR-DISK|Raw-HD", re.I)  # disc images and remuxes: 30-80 GB a film

# Format scores per audio mode. "subbed" (VOSTFR...) is parsed as the subtitle language by Radarr: it must cancel
# "lang" where the user wants that language spoken.
SCORES = {
    "mine": {"lang": 100, "multi": 100, "original": 0, "subbed": -150},
    "orig": {"lang": 0, "multi": 100, "original": 100, "subbed": 10},
    "multi": {"lang": 100, "multi": 200, "original": 0, "subbed": -150},
}
DUAL = r"\b(MULTi|DUAL|DL|ML)\b"  # German DL, Spanish/Italian DUAL: only says "two tracks", not which
SUBBED = {"fr": r"\b(VOSTFR|SUBFRENCH)\b"}
SUBBED_OTHER = r"\b(SUBBED|VOST|SUB)\b"


def configured(s) -> bool:
    return s.profiles is not None


def effective(s):
    """The settings with every unset choice at its default (installs made before these choices existed)."""
    return replace(
        s,
        audio_lang=s.audio_lang or (s.lang if s.lang in LANGS else "en"),
        profiles=list(s.profiles) if s.profiles is not None else [DEFAULT_PROFILE],
        default_profile=s.default_profile if s.profiles is not None else DEFAULT_PROFILE,
        subtitle_langs=list(s.subtitle_langs) if s.subtitle_langs is not None
        else [s.lang] + ([] if s.lang == "en" else ["en"]),
        metadata_lang=s.metadata_lang or (s.lang if s.lang in LANGS else "en"))


def validate(s) -> list[str]:
    e = effective(s)
    errors = []
    if e.audio_lang not in LANGS or e.metadata_lang not in LANGS or any(x not in LANGS for x in e.subtitle_langs):
        errors.append("err_media_lang")
    if not e.profiles or any(k not in KEYS for k in e.profiles):
        errors.append("err_profiles_none")
    elif e.default_profile not in e.profiles:
        errors.append("err_default_profile")
    if not e.subtitle_langs:
        errors.append("err_subtitles_none")
    return errors


def scores(mode: str) -> dict[str, int]:
    return dict(SCORES[mode])


def _spec(name: str, implementation: str, value, required: bool = False) -> dict:
    fields = [{"name": "value", "value": value}]
    if implementation == "LanguageSpecification":
        fields.append({"name": "exceptLanguage", "value": False})
    return {"name": name, "implementation": implementation, "negate": False, "required": required, "fields": fields}


def _format(name: str, *specs: dict) -> dict:
    return {"name": name, "includeCustomFormatWhenRenaming": False, "specifications": list(specs)}


def formats(lang: str) -> dict[str, dict]:
    """The four custom formats a profile scores. Names are fixed: Plaid finds them by ID, the user never sees them."""
    if lang == "fr":  # the French scene's MULTi always means French + original
        multi = _format("Plaid · MULTi", _spec("MULTi", "ReleaseTitleSpecification", r"\bMULTi\b"))
    else:
        multi = _format("Plaid · MULTi", _spec("Language", "LanguageSpecification", ARR_LANG[lang], True),
                        _spec("Dual audio", "ReleaseTitleSpecification", DUAL, True))
    return {
        "lang": _format(f"Plaid · {lang}", _spec("Language", "LanguageSpecification", ARR_LANG[lang])),
        "original": _format("Plaid · VO", _spec("Original", "LanguageSpecification", ORIGINAL)),
        "multi": multi,
        "subbed": _format("Plaid · subbed", _spec("Subbed", "ReleaseTitleSpecification",
                                                  SUBBED.get(lang, SUBBED_OTHER))),
    }


def _qualities(items: list[dict]):
    for item in items:
        if item.get("items"):  # one of the schema's own groups (WEB 1080p): its qualities, loose
            yield from _qualities(item["items"])
        else:
            yield item


def profile_body(schema: dict, key: str, name: str, format_ids: dict[str, int], radarr: bool) -> dict:
    """A quality profile for one grid cell. Its resolution's qualities share one group, so web and Blu-ray tie and
    the language score decides; no upgrade, ever."""
    mode, quality = key.split("-")
    body = copy.deepcopy(schema)
    body.pop("id", None)
    wanted, others = [], []
    for item in _qualities(body["items"]):
        item = {**item, "allowed": False, "items": []}
        q = item["quality"]
        (wanted if q["resolution"] == QUALITIES[quality] and not EXCLUDED.search(q["name"]) else others).append(item)
    group = {"id": GROUP_ID, "name": quality, "allowed": True, "items": wanted}
    body.update(name=name, items=others + [group], cutoff=GROUP_ID, upgradeAllowed=False,
                minFormatScore=MIN_SCORE, cutoffFormatScore=0,
                formatItems=[{"format": format_ids[k], "score": v} for k, v in SCORES[mode].items()])
    if radarr:
        body["language"] = {"id": ANY, "name": "Any"}
    else:
        body.pop("language", None)
    return body


def profile_name(key: str, ui_lang: str, audio_lang: str) -> str:
    mode, quality = key.split("-")
    audio = {"mine": t(ui_lang, f"lang_{audio_lang}"), "orig": t(ui_lang, "profile_orig"), "multi": "MULTi"}[mode]
    return f"{audio} · {'4K' if quality == '4k' else quality}"
